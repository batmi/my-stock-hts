"""미체결 취소가 **예외로** 실패했을 때도 실패로 세어지는가.

[배경 · 2026-09-06 감사] manage_unfilled_orders 의 주문 1건 처리 블록 전체가
`except Exception: pass` 로 감싸여 있었다. 그 안에는 취소 실패 누적·경보
(_note_cancel_failure)가 들어 있다 — 그 함수의 주석은 "운영자 개입 없이는 복구
불가한 상태"라며 전달 확인까지 붙여 뒀는데, 바깥의 한 줄이 그것을 통째로 지웠다.

revise_cancel_order 는 OrderOutcomeUnknown 만 잡아 dict 로 바꾼다. 그 밖의 예외
(네트워크·JSON 파싱·계좌 파라미터 준비 실패)는 그대로 올라온다. 그러면
  · 취소는 안 됐는데 실패로 세어지지 않는다 → 경보가 영원히 오지 않는다
  · 주문은 pending 으로 남는다 → is_pending 인 그 종목은 매도 워커에서 통째로 빠져
    **손절 판정이 멈춘다**
"""
from datetime import datetime
from unittest.mock import patch

import pytest

import config
from modules.auto_trade import AutoTrader, ConclusionMonitor


@pytest.fixture(autouse=True)
def reset_singleton():
    AutoTrader._instance = None
    ConclusionMonitor._instance = None
    yield


class _FakeDatetime(datetime):
    @classmethod
    def now(cls):
        return cls(2023, 1, 2, 10, 0, 0)


def _run(trader, cancel_side_effect):
    with patch('modules.auto_trade.api.get_unfilled_orders', return_value=[{
                   'odno': '12345', 'pdno': '005930', 'prdt_name': 'Samsung',
                   'rmn_qty': '10', 'ord_tmd': '090000'}]), \
         patch('modules.auto_trade.api.revise_cancel_order', **cancel_side_effect), \
         patch('modules.auto_trade.engine.datetime', _FakeDatetime), \
         patch('modules.auto_trade.db_manager.db.get_trade_by_odno',
               return_value={'type': 'buy'}), \
         patch.object(trader, 'is_market_open', return_value=True):
        config.UNFILLED_ORDER_CANCEL_SECONDS = 10
        trader.order_manager.manage_unfilled_orders()


def test_a_cancel_that_raises_is_still_counted_as_a_failure():
    """[핵심] 예외로 실패한 취소도 누적 카운터에 잡혀야 경보까지 갈 수 있다."""
    trader = AutoTrader()
    om = trader.order_manager
    om.cancel_failures.clear()

    _run(trader, {'side_effect': ConnectionError("전송 중 끊김")})

    assert om.cancel_failures.get('12345') == 1, (
        f"예외로 실패한 취소가 세어지지 않았다: {dict(om.cancel_failures)}")


def test_repeated_raising_cancels_reach_the_operator_alert():
    """한도에 도달하면 경보가 나가야 한다 — 그 상태는 스스로 낫지 않는다."""
    trader = AutoTrader()
    om = trader.order_manager
    om.cancel_failures.clear()

    with patch('modules.auto_trade.alert_delivered', return_value=True) as alert:
        for _ in range(om.CANCEL_FAILURE_ALERT_THRESHOLD):
            _run(trader, {'side_effect': ConnectionError("전송 중 끊김")})

    assert alert.called, "연속 실패가 한도를 넘었는데 경보가 나가지 않았다"
    assert "미체결 취소 실패" in alert.call_args[0][0]


def test_a_dict_failure_is_counted_the_same_way():
    """대조군 — 종전에도 동작하던 경로(dict 로 실패가 돌아오는 경우)는 그대로다."""
    trader = AutoTrader()
    om = trader.order_manager
    om.cancel_failures.clear()

    _run(trader, {'return_value': {'rt_cd': '1', 'msg1': '취소 불가'}})
    assert om.cancel_failures.get('12345') == 1


def test_a_successful_cancel_clears_the_counter():
    trader = AutoTrader()
    om = trader.order_manager
    om.cancel_failures['12345'] = 2

    with patch('modules.auto_trade.api.send_telegram_message'), \
         patch('modules.auto_trade.db_manager.db.insert_trade'):
        _run(trader, {'return_value': {'rt_cd': '0', 'output': {'ODNO': 'C1'}}})
    assert '12345' not in om.cancel_failures
