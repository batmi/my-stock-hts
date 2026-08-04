"""장애 대기/복구 루프의 Kill Switch 교착 방지 회귀 테스트.

2026-07-10 KIS 서버 장애 시, 장애 중 누적된 ConclusionMonitor 에러 카운트가
복구 후에도 리셋되지 않아 '대기 모드 진입 → 서버 복구 → Kill Switch 재발동'이
무한 반복되던 버그에 대한 검증.
"""
import threading
from unittest.mock import patch, MagicMock

import pytest

import config
from modules import auto_trade


def test_kill_switch_triggers_monitor_recheck():
    """Kill Switch 발동 시 모니터를 즉시 깨워(check_now) 자가 회복 기회 부여"""
    trader = auto_trade.AutoTrader()
    trader.is_running = True
    trader.thread = threading.current_thread()
    trader.consecutive_errors = 0
    trader.last_wait_alert_time = 0

    monitor = auto_trade.ConclusionMonitor()
    monitor.consecutive_errors = 99  # 불량 상태

    config.SYSTEM_MAX_CONSECUTIVE_ERRORS = 5

    try:
        with patch.object(monitor, 'check_now') as mock_check_now, \
             patch.object(trader, 'is_market_open', return_value=True), \
             patch.object(trader, 'log') as mock_log, \
             patch('time.sleep', side_effect=InterruptedError):  # 첫 사이클 후 루프 탈출
            try:
                trader._run_loop()
            except InterruptedError:
                pass

        mock_check_now.assert_called_once()
        assert any("체결 감시 시스템 불안정" in str(c) for c in mock_log.call_args_list)
    finally:
        monitor.consecutive_errors = 0
        trader.consecutive_errors = 0


def test_monitor_resets_stale_errors_while_idle():
    """장 마감·미체결 없음으로 조회를 쉬는 동안에는 누적 에러 카운트 리셋"""
    monitor = auto_trade.ConclusionMonitor()
    monitor.initialized = True
    monitor.consecutive_errors = 7  # 장애 중 누적된 상태
    monitor.is_running = True
    monitor.thread = threading.current_thread()

    # 미체결 주문 없음 보장 (싱글톤 상태 격리)
    trader = auto_trade.AutoTrader()
    with trader.order_manager._lock:
        saved_pending = dict(trader.order_manager.pending_orders)
        trader.order_manager.pending_orders.clear()

    # event.wait(60) 1회 후 루프 종료 유도
    mock_event = MagicMock()
    mock_event.is_set.return_value = False

    def stop_loop(*args, **kwargs):
        monitor.is_running = False

    mock_event.wait.side_effect = stop_loop
    saved_event = monitor.event
    monitor.event = mock_event

    try:
        with patch.object(monitor, '_is_market_open', return_value=False), \
             patch('time.sleep'):  # 초기 지연 생략
            monitor._run_loop()

        assert monitor.consecutive_errors == 0
        assert monitor.is_healthy()
    finally:
        monitor.event = saved_event
        monitor.is_running = False
        with trader.order_manager._lock:
            trader.order_manager.pending_orders.update(saved_pending)


@patch('modules.auto_trade.api.send_telegram_message')
def test_wait_mode_entry_alert_cooldown(mock_tg):
    """대기 모드 진입 알림은 쿨타임(10분) 내 반복 진입 시 생략"""
    import time as _time

    trader = auto_trade.AutoTrader()
    trader.is_running = True
    trader.thread = threading.current_thread()
    trader.consecutive_errors = 1
    trader.last_wait_alert_time = _time.time()  # 방금 알림을 보낸 상태
    trader._wait_alert_sent = False

    config.SYSTEM_MAX_CONSECUTIVE_ERRORS = 2

    monitor = auto_trade.ConclusionMonitor()
    monitor.consecutive_errors = 0

    recovery_called = []

    def fake_recovery():
        recovery_called.append(True)
        trader.is_running = False  # 복구 후 루프 종료

    try:
        with patch.object(trader, 'is_market_open', return_value=True), \
             patch.object(trader, '_wait_for_server_recovery', side_effect=fake_recovery), \
             patch('modules.auto_trade.api.get_domestic_balance',
                   side_effect=Exception("Fatal Network Error")), \
             patch('time.sleep'):
            trader._run_loop()

        assert recovery_called  # 대기 모드에는 진입했지만
        # [수정] '텔레그램 전무'로 단언하면 안 된다. 같은 주기에서 시장 필터 알림 등
        #  무관한 알림이 정상적으로 나가면 쿨타임과 상관없이 실패한다(오탐).
        #  검증 대상은 '대기 모드 진입 알림'이 생략됐는가 하나다.
        wait_alerts = [c for c in mock_tg.call_args_list
                       if "시스템 긴급 대기" in str(c)]
        assert not wait_alerts, "쿨타임 내인데 대기 모드 진입 알림이 다시 발송됐다"
        assert trader._wait_alert_sent is False
    finally:
        trader.consecutive_errors = 0
        trader.last_wait_alert_time = 0
        config.SYSTEM_MAX_CONSECUTIVE_ERRORS = 5
