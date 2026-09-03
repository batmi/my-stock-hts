import pytest
import os
import sys
from unittest.mock import patch, MagicMock
from datetime import datetime, time

sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import config
from modules.scheduler import SystemScheduler

@pytest.fixture
def scheduler():
    """스케줄러 싱글톤 초기화용 피스처"""
    s = SystemScheduler()
    s.is_running = False
    s.last_holiday_notified_date = None
    s.last_briefing_date = None
    return s

def test_scheduler_start_stop(scheduler):
    """스케줄러 스레드 시작 및 중지 테스트"""
    config.ENABLE_TELEGRAM = True
    scheduler.start()
    assert scheduler.is_running is True
    scheduler.stop()
    assert scheduler.is_running is False

@patch('modules.scheduler.api.is_holiday_today')
@patch('modules.scheduler.api.is_us_holiday_today')
@patch('modules.scheduler.api.get_holiday_name')
@patch('modules.scheduler.api.send_telegram_message')
def test_check_holiday_notification_both_closed(mock_tg, mock_name, mock_us_holiday, mock_kr_holiday, scheduler):
    """국내 및 미국 시장 모두 휴장 시 알림 테스트"""
    mock_kr_holiday.return_value = True
    mock_us_holiday.return_value = True
    mock_name.side_effect = ["어린이날", "독립기념일"]
    
    with patch('modules.scheduler.datetime') as mock_dt:
        now = datetime(2023, 5, 1) # 평일(월요일)로 고정하여 주말 예외 회피
        # 시간을 알림 전송 타겟 시간(08:30~09:30)으로 강제 모킹
        mock_dt.now.return_value = datetime.combine(now.date(), time(8, 45))
        mock_dt.today.return_value = now
        mock_dt.strptime = datetime.strptime
        mock_dt.combine = datetime.combine
        
        scheduler._check_holiday_notification()
        
    mock_tg.assert_called_once()
    assert "모두 휴장" in mock_tg.call_args[0][0]
    assert scheduler.last_holiday_notified_date == mock_dt.now.return_value.strftime("%Y-%m-%d")

@patch('modules.scheduler.theme_analysis.generate_morning_briefing')
@patch('modules.scheduler.theme_analysis._get_macro_context_str')
@patch('modules.scheduler.api.send_telegram_message')
def test_execute_briefing(mock_tg, mock_macro, mock_generate, scheduler):
    """장전 브리핑 전송 로직 검증"""
    mock_macro.return_value = "Macro Context Data"
    mock_generate.return_value = "Morning Briefing Content"
    
    scheduler.execute_briefing()
    
    mock_macro.assert_called_once()
    mock_generate.assert_called_once_with("Macro Context Data")
    mock_tg.assert_called_once_with("Morning Briefing Content")

@patch('modules.scheduler.theme_analysis.generate_daily_closing_report')
@patch('modules.scheduler.api.get_domestic_balance')
@patch('modules.scheduler.api.get_deposit_balance')
@patch('modules.scheduler.api.send_telegram_message')
def test_execute_daily_closing_report(mock_tg, mock_dep, mock_bal, mock_generate, scheduler):
    """장 마감 종합 브리핑 전송 로직 검증"""
    # 모의 잔고 반환 설정
    mock_bal.return_value = (
        [{'prdt_name': '삼성전자', 'evlu_amt': '1000000', 'evlu_pfls_rt': '5.5', 'hldg_qty': '10'}], 
        []
    )
    mock_dep.return_value = {'d2_deposit': 5000000}
    mock_generate.return_value = "Closing Report Content"
    
    scheduler.execute_daily_closing_report()
    
    mock_bal.assert_called_once()
    mock_dep.assert_called_once()
    mock_generate.assert_called_once()
    
    # 프롬프트 포트폴리오 텍스트가 올바르게 생성되어 전달되었는지 검증
    called_args = mock_generate.call_args[0][0]
    assert "삼성전자" in called_args
    assert "총 자산: 6,000,000원" in called_args
    
    mock_tg.assert_called_once()
    assert "Closing Report Content" in mock_tg.call_args[0][0]

def _calendar_scheduler(scheduler):
    scheduler.last_calendar_alert_date = None
    return scheduler


@patch('modules.scheduler.threading.Thread')
def test_check_calendar_alerts_fires_once_a_day(mock_thread, scheduler):
    """발송 시각 이후 첫 순회에 한 번만 트리거되고, 같은 날 재호출은 무시된다."""
    _calendar_scheduler(scheduler)
    config.settings.AUTO_CALENDAR_ALERT_TIME = "0820"

    with patch('modules.scheduler.datetime') as mock_dt:
        mock_dt.now.return_value = datetime(2026, 7, 29, 8, 30)
        mock_dt.strptime = datetime.strptime
        scheduler._check_calendar_alerts()
        scheduler._check_calendar_alerts()

    assert scheduler.last_calendar_alert_date == "2026-07-29"
    assert mock_thread.call_count == 1
    from modules.manage import events as calendar_events
    assert mock_thread.call_args.kwargs["target"] is calendar_events.check_and_alert_calendar


@patch('modules.scheduler.threading.Thread')
def test_check_calendar_alerts_skips_outside_window(mock_thread, scheduler):
    """발송 시각 전이거나 창(3시간)을 넘긴 시각에는 트리거되지 않는다."""
    _calendar_scheduler(scheduler)
    config.settings.AUTO_CALENDAR_ALERT_TIME = "0820"

    for hour in (7, 23):
        scheduler.last_calendar_alert_date = None
        with patch('modules.scheduler.datetime') as mock_dt:
            mock_dt.now.return_value = datetime(2026, 7, 29, hour, 0)
            mock_dt.strptime = datetime.strptime
            scheduler._check_calendar_alerts()
        assert scheduler.last_calendar_alert_date is None


# ==========================================================
# [감사 2026-09-04] 하트비트 상황 정보 · 시장정지 점검 게이트
# ==========================================================

@pytest.mark.parametrize("flags,expected", [
    ({"is_paper": True, "is_toss": False}, "가상투자"),
    ({"is_paper": False, "is_toss": True}, "토스"),
    ({"is_paper": False, "is_toss": False}, "실전"),
])
def test_heartbeat_context_reports_the_actual_mode(scheduler, monkeypatch, flags, expected):
    """사망 알림의 '모드'가 실제로 뜬 모드여야 한다.

    종전에는 mode = "실전" 이 분기 밖에 있어 무엇으로 떴든 항상 '실전'으로 덮였다.
    파이(가상투자)와 맥북(실전)을 함께 돌리는데 둘 다 '실전'이라고 알리면, 어느 쪽이
    죽었는지 모른 채 실계좌부터 확인하게 된다.
    """
    for k, v in flags.items():
        monkeypatch.setattr(config.session, k, v, raising=False)
    assert scheduler._heartbeat_context()["mode"] == expected


@pytest.mark.parametrize("cb_on,vi_on,should_check", [
    (True, False, True),      # CB 만 켬 — 종전에도 돌았다
    (False, True, True),      # VI 만 켬 — 종전에는 아무 일도 일어나지 않았다
    (True, True, True),
    (False, False, False),    # 둘 다 끄면 들어가지 않는다
])
def test_market_halt_gate_honours_both_switches(scheduler, monkeypatch, cb_on, vi_on, should_check):
    """CB 스위치 하나가 VI 까지 막으면 안 된다(메뉴 토글이 거짓말을 한다)."""
    monkeypatch.setattr(config, 'MARKET_HALT_ALERT_USE', cb_on, raising=False)
    monkeypatch.setattr(config, 'MARKET_HALT_VI_USE', vi_on, raising=False)
    monkeypatch.setattr(config, 'AUTO_MORNING_BRIEFING_USE', False, raising=False)

    called = []
    monkeypatch.setattr(scheduler, '_check_holiday_notification', lambda: None)
    monkeypatch.setattr(scheduler, '_check_heartbeat', lambda: None)
    monkeypatch.setattr(scheduler, '_check_market_halt', lambda: called.append(True))

    #  루프를 한 바퀴만 돌린다 — sleep 이 곧 종료 신호다.
    def _stop(_sec):
        scheduler.is_running = False
    monkeypatch.setattr('modules.scheduler.time.sleep', _stop)

    scheduler.is_running = True
    try:
        scheduler._run_loop()
    finally:
        scheduler.is_running = False

    assert bool(called) is should_check
