"""휴게(15:30~16:00)에 걸린 미체결 — 취소 보류와 생존 주문 점검.

[배경 · 2026-11-27 토스 Open API 1.2.31] timeInForce=DAY 의 설명이 '정규장 종료까지 미체결
 자동 취소'에서 '국내는 늦어도 애프터마켓 종료 후 취소(정규장 접수분도 체결 시장에 따라
 그 뒤까지 살 수 있음)'로 바뀌었다. 15:30 에 사라진다고 믿던 주문이 16:00 KRX 애프터에서
 체결될 수 있다.
  ① 휴게 중 취소는 'order-hours-closed' 로 거부된다(거래소가 쉬는 것). 종전 코드는 이를
     평범한 실패로 세어 '손절이 멈췄으니 HTS 에서 직접 취소하라'는 긴급 경보를 잘못 냈다.
  ② 로컬 대기 주문이 없으면(외부·수동 주문) 휴게엔 아무도 그 주문을 보지 않았다.
"""
from datetime import datetime
from unittest.mock import patch

import pytest

import config
from modules.auto_trade import AutoTrader, ConclusionMonitor
from modules.auto_trade.common import OrderStatus

#  휴게 판정 자체가 검증 대상이다 — conftest 의 '휴게 아님' 고정(krx_break_window_closed)을 끈다.
pytestmark = pytest.mark.real_session_window

ODNO = '12345'
CODE = '005930'
_NOW = {'t': datetime(2026, 11, 26, 15, 35, 0)}   # 목요일 · 평일 · 휴게 중


class _FakeDatetime(datetime):
    @classmethod
    def now(cls, tz=None):
        t = _NOW['t']
        return cls(t.year, t.month, t.day, t.hour, t.minute, t.second)


@pytest.fixture(autouse=True)
def reset_singleton():
    AutoTrader._instance = None
    ConclusionMonitor._instance = None
    yield


@pytest.fixture
def om():
    trader = AutoTrader()
    m = trader.order_manager
    m.cancel_failures.clear()
    m.cancel_deferred.clear()
    m.pending_orders = {CODE: {ODNO: OrderStatus.ORDER_SENT}}
    yield m
    m.pending_orders = {}


def _row(odno=ODNO, code=CODE, qty='10'):
    return {'odno': odno, 'pdno': code, 'prdt_name': '삼성전자', 'rmn_qty': qty,
            'ord_tmd': '151500'}


def _run(om, cancel_res, at, survivor_done=True):
    """at 시각에 manage_unfilled_orders 한 번. 반환: (취소 mock, 텔레그램 mock)."""
    _NOW['t'] = at
    if survivor_done:
        om.break_survivor_checked_day = at.strftime("%Y%m%d")
    with patch('modules.auto_trade.api.get_unfilled_orders', return_value=[_row()]), \
         patch('modules.auto_trade.api.revise_cancel_order', return_value=cancel_res) as cancel, \
         patch('modules.auto_trade.api.send_telegram_message') as tg, \
         patch('modules.auto_trade.engine.datetime', _FakeDatetime), \
         patch('modules.auto_trade.db_manager.db.get_trade_by_odno', return_value={'type': 'buy'}), \
         patch('modules.auto_trade.db_manager.db.insert_trade'), \
         patch.object(om.trader, 'is_market_open', return_value=False):
        config.UNFILLED_ORDER_CANCEL_SECONDS = 120
        om.manage_unfilled_orders()
    return cancel, tg


_CLOSED = {'rt_cd': '1', 'msg_cd': 'order-hours-closed', 'msg1': '현재 해당 주문을 접수할 수 없는 시간입니다.'}


def test_break_rejection_is_deferred_not_counted(om):
    """[핵심] 휴게 거부는 실패로 세지 않고, 보류 알림을 한 번 낸다."""
    cancel, tg = _run(om, _CLOSED, datetime(2026, 11, 26, 15, 35, 0))
    assert cancel.called
    assert ODNO not in om.cancel_failures, "휴게 거부가 취소 실패로 세어졌다 — 손절 멈춤 경보가 잘못 나간다"
    assert ODNO in om.cancel_deferred
    assert tg.call_count == 1 and "취소 보류" in tg.call_args[0][0]


def test_repeated_break_rejections_never_raise_the_operator_alert(om):
    """한도(3회)를 넘겨 거부돼도 '직접 취소하라' 긴급 경보는 휴게에선 나가지 않는다."""
    with patch('modules.auto_trade.alert_delivered', return_value=True) as alert:
        tgs = []
        for i in range(om.CANCEL_FAILURE_ALERT_THRESHOLD + 2):
            _, tg = _run(om, _CLOSED, datetime(2026, 11, 26, 15, 35 + i, 0))
            tgs.append(tg.call_count)
    assert not alert.called
    assert ODNO not in om.cancel_failures
    assert sum(tgs) == 1, f"보류 알림은 첫 거부에만 — {tgs}"


def test_retry_is_throttled_inside_the_break(om):
    _run(om, _CLOSED, datetime(2026, 11, 26, 15, 35, 0))
    cancel, _ = _run(om, _CLOSED, datetime(2026, 11, 26, 15, 35, 10))
    assert not cancel.called, "30초가 안 지났는데 다시 취소를 냈다"
    cancel, _ = _run(om, _CLOSED, datetime(2026, 11, 26, 15, 35, 31))
    assert cancel.called, "간격이 지나면 다시 시도해야 한다"


def test_cancel_after_the_break_clears_the_deferral(om):
    """16:00 애프터가 열리면 평소처럼 취소하고, 성공하면 보류 기록을 지운다."""
    _run(om, _CLOSED, datetime(2026, 11, 26, 15, 59, 50))
    cancel, _ = _run(om, {'rt_cd': '0', 'output': {'ODNO': 'C1'}}, datetime(2026, 11, 26, 16, 0, 5))
    assert cancel.called
    assert ODNO not in om.cancel_deferred


def test_same_code_outside_the_break_is_still_a_failure(om):
    """대조군 — 휴게 밖의 '접수 불가 시간'은 평소처럼 세어 경보까지 간다(안전장치 유지)."""
    _run(om, _CLOSED, datetime(2026, 11, 26, 20, 30, 0))
    assert om.cancel_failures.get(ODNO) == 1
    assert ODNO not in om.cancel_deferred


# ── 생존 주문 점검 ─────────────────────────────────────────────

def _survivors(om, at, rows, trades):
    _NOW['t'] = at
    with patch('modules.auto_trade.api.get_unfilled_orders', return_value=rows) as q, \
         patch('modules.auto_trade.api.send_telegram_message') as tg, \
         patch('modules.auto_trade.db_manager.db.get_trade_by_odno',
               side_effect=lambda odno, on_date=None: trades.get(odno)):
        om._check_break_survivors(_FakeDatetime.now())
    return q, tg


def test_survivors_are_reported_once_with_their_handling(om):
    om.break_survivor_checked_day = None
    rows = [_row('A1', '005930'), _row('B2', '000660', '3')]
    trades = {'A1': {'type': '매수'}}            # B2 는 DB 에 없음 = 외부 주문
    q, tg = _survivors(om, datetime(2026, 11, 26, 15, 33, 0), rows, trades)
    assert q.call_count == 1 and tg.call_count == 1
    msg = tg.call_args[0][0]
    assert "2건" in msg
    assert "자동 취소 대상" in msg and "외부 주문 — 손대지 않음" in msg

    q, tg = _survivors(om, datetime(2026, 11, 26, 15, 40, 0), rows, trades)
    assert not q.called and not tg.called, "같은 날 두 번 점검했다"


def test_survivor_check_waits_for_the_closing_auction_to_settle(om):
    """15:30 직후엔 종가 단일가 체결이 목록에 안 실려 체결된 주문이 '생존'으로 잡힌다."""
    om.break_survivor_checked_day = None
    q, tg = _survivors(om, datetime(2026, 11, 26, 15, 31, 0), [_row()], {})
    assert not q.called


def test_survivor_check_is_retried_when_the_lookup_fails(om):
    """조회 실패(None)는 '없음'이 아니다 — 점검 완료로 표시하지 않는다."""
    om.break_survivor_checked_day = None
    _survivors(om, datetime(2026, 11, 26, 15, 33, 0), None, {})
    assert om.break_survivor_checked_day is None


def test_no_survivor_check_outside_the_break(om):
    om.break_survivor_checked_day = None
    q, _ = _survivors(om, datetime(2026, 11, 26, 10, 0, 0), [_row()], {})
    assert not q.called
