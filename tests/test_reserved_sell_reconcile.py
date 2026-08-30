"""예약 매도 발주 직전의 수량 대사 — 청산이 통째로 실패하거나 절반만 나가지 않는가.

예약 수량은 **등록 시점에 굳는다**. 그 사이 HTS·MTS로 직접 팔거나 더 사면 수량이
어긋나는데, 어긋난 채로 내면 두 방향 모두 나쁘다.
  · 초과분 → 증권사가 거부해 **청산이 통째로 실패**한다(그 포지션은 무방비로 남는다).
  · 부족분 → 일부만 팔리고 나머지가 남는다.
수동 계좌에서는 이 어긋남이 예외가 아니라 일상이다.

[왜 취소가 아니라 축소인가] 가격·신호 조건은 그대로 유효하고 어긋난 것은 수량 하나다.
전량 취소하면 남은 수량의 손절이 조용히 사라진다 — 팔 것이 하나도 없을 때만 취소한다.

[조회 실패는 진행] fetch_sellable_quantity 는 실패를 None 으로 돌려 '모름'과 '0주'를
가른다. 모름을 0으로 읽으면 일시적 조회 실패가 청산을 거른다.

이 대사 함수는 전체 스위트에서 36%만 실행됐다(2026-08-30 커버리지 실측) — 축소·취소·
조회 실패 분기가 검증되지 않은 상태였다.
"""
import pytest
from unittest.mock import patch

from modules.reserved_order_monitor import ReservedOrderMonitor


@pytest.fixture
def monitor():
    m = ReservedOrderMonitor()
    m.is_running = False
    return m


def _order(qty=10, condition='PRICE_BELOW', market='KR'):
    return {'id': 1, 'cano': '44048158', 'acnt': '01', 'code': '005930', 'name': '삼성전자',
            'market': market, 'order_type': 'sell', 'qty': qty, 'order_price': 70000,
            'condition_type': condition}


def _reconcile(monitor, order, sellable):
    with patch('modules.reserved_order_monitor.api.fetch_sellable_quantity',
               return_value=sellable), \
         patch.object(monitor, '_cancel_on_position_gone') as canceled:
        return monitor._reconcile_sell_qty(order), canceled


def test_an_untouched_position_orders_the_registered_quantity(monitor):
    (res, note), canceled = _reconcile(monitor, _order(10), 10)
    assert (res, note) == (10, "")
    canceled.assert_not_called()


def test_a_shrunken_position_shrinks_the_order(monitor):
    """[핵심] 외부 매도로 보유가 줄면 그만큼만 판다 — 초과 주문은 청산 전체를 실패시킨다."""
    (res, note), canceled = _reconcile(monitor, _order(10), 4)
    assert res == 4 and "축소" in note
    canceled.assert_not_called()


def test_more_holdings_than_registered_are_left_alone(monitor):
    """추가 매수분까지 파는 것은 예약의 의도가 아니다(전량 청산 신호가 아닌 한)."""
    (res, note), _ = _reconcile(monitor, _order(10), 25)
    assert (res, note) == (10, "")


def test_a_full_exit_signal_sells_everything_held(monitor):
    """전량 청산(HOLDING_EXIT)은 등록 후 늘어난 몫까지 함께 정리한다."""
    (res, note), _ = _reconcile(monitor, _order(10, condition='HOLDING_EXIT'), 25)
    assert res == 25 and "전량 청산" in note

    (res2, note2), _ = _reconcile(monitor, _order(10, condition='HOLDING_EXIT'), 10)
    assert (res2, note2) == (10, "")


def test_an_empty_position_cancels_instead_of_ordering(monitor):
    """[핵심] 팔 것이 없으면 발주하지 않는다 — 실패만 쌓인다."""
    res, canceled = _reconcile(monitor, _order(10), 0)
    assert res is None, "보유 0인데 주문 수량을 돌려줬다"
    canceled.assert_called_once()


def test_an_unreadable_balance_proceeds_with_the_registered_quantity(monitor):
    """[핵심] 조회 실패를 '0주'로 읽으면 일시적 장애가 손절을 거른다 — 등록 수량으로 진행한다."""
    (res, note), canceled = _reconcile(monitor, _order(10), None)
    assert (res, note) == (10, "")
    canceled.assert_not_called()


def test_overseas_orders_skip_the_reconciliation(monitor):
    """해외는 이 조회(국내 TR)가 없다 — 대사 없이 등록 수량으로 나간다."""
    with patch('modules.reserved_order_monitor.api.fetch_sellable_quantity') as fetch:
        res = monitor._reconcile_sell_qty(_order(10, market='US'))
    assert res == (10, "")
    fetch.assert_not_called()
