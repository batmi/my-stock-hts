"""전량 매도 주문이 끝났을 때 트레일링 앵커를 언제 지우는가.

[왜 이 자리가 위험한가] 앵커(trailing_stops의 최고가)는 청산선의 근거다. 잘못 지우면
다음 포지션의 감시가 현재가에서 다시 시작되고, 잘못 남기면 옛 최고가로 max_profit이
과대 계산돼 매수 직후 BEP/TS가 오발동한다. 실제로 두 사고가 다 있었다
(재매수 직후 즉시 청산 / 추가 매수 체결가가 앵커를 덮어씀).

그래서 규약은 **체결 확정(FILLED) 시에만 정리**다 — 접수 시점에 지우면 미체결 취소로
포지션이 그대로 남았는데 앵커만 리셋되어 샹들리에 TS 감시가 느슨해진다.

이 규약을 지키는 코드(OrderManager.update_order_status의 sell_cleanup_odnos 분기)는
전체 스위트에서 한 번도 실행되지 않았다(2026-08-30 커버리지 실측). 정리 방향이 뒤집혀도
아무 테스트도 붉어지지 않는 상태였다.
"""
import pytest
from unittest.mock import patch

from modules.auto_trade import AutoTrader
from modules.auto_trade.common import OrderStatus

CODE = "005930"
ODNO = "0000999001"


@pytest.fixture
def trader():
    AutoTrader._instance = None
    t = AutoTrader()
    t.is_running = True
    om = t.order_manager
    om.pending_orders.clear()
    om.sell_cleanup_odnos.clear()
    yield t
    om.pending_orders.clear()
    om.sell_cleanup_odnos.clear()


def _arm_full_sell(trader):
    """전량 매도 주문이 나간 직후의 상태를 만든다."""
    om = trader.order_manager
    om.pending_orders[CODE] = {ODNO: OrderStatus.ORDER_SENT}
    om.sell_cleanup_odnos[str(ODNO)] = CODE
    trader.half_tp_cache.add(CODE)
    trader.trailing_stop_cache[CODE] = {'highest_price': 70_000}


def _finish(trader, status):
    with patch('modules.auto_trade.db_manager.db.delete_half_tp') as del_half, \
         patch('modules.auto_trade.db_manager.db.delete_trailing_stop') as del_ts, \
         patch('modules.auto_trade.db_manager.db.get_trade_by_odno', return_value=None), \
         patch('modules.auto_trade.api.send_telegram_message'), \
         patch('modules.auto_trade.trader.threading.Thread'):   # 지연 잔고 출력 스레드 차단
        trader.order_manager.update_order_status(CODE, ODNO, status)
    return del_half, del_ts


def test_a_confirmed_full_sell_clears_the_anchor(trader):
    """체결 확정이면 앵커·반익절 기록을 지운다 — 다음 진입이 옛 최고가를 물려받지 않게."""
    _arm_full_sell(trader)
    del_half, del_ts = _finish(trader, OrderStatus.FILLED)

    del_ts.assert_called_once_with(CODE)
    del_half.assert_called_once_with(CODE)
    assert CODE not in trader.trailing_stop_cache, "메모리 앵커가 남았다"
    assert CODE not in trader.half_tp_cache


@pytest.mark.parametrize("status", [OrderStatus.CANCELED, OrderStatus.REJECTED],
                         ids=["canceled", "rejected"])
def test_an_unfilled_full_sell_keeps_the_anchor(trader, status):
    """[핵심] 취소·거부는 포지션이 그대로 남는다 — 앵커를 지우면 감시가 느슨해진다."""
    _arm_full_sell(trader)
    del_half, del_ts = _finish(trader, status)

    del_ts.assert_not_called()
    del_half.assert_not_called()
    assert trader.trailing_stop_cache.get(CODE) == {'highest_price': 70_000}, \
        "체결되지도 않았는데 앵커를 버렸다"


def test_cleanup_marker_is_consumed_either_way(trader):
    """정리 표식은 주문이 끝나면 어느 쪽이든 회수된다 — 남기면 다음 주문번호와 엉킨다."""
    _arm_full_sell(trader)
    _finish(trader, OrderStatus.CANCELED)
    assert str(ODNO) not in trader.order_manager.sell_cleanup_odnos


def test_a_partial_sell_never_arms_the_cleanup(trader):
    """반익절(부분 매도)은 표식을 달지 않으므로 체결돼도 앵커가 살아 있어야 한다."""
    om = trader.order_manager
    om.pending_orders[CODE] = {ODNO: OrderStatus.ORDER_SENT}
    trader.trailing_stop_cache[CODE] = {'highest_price': 70_000}

    del_half, del_ts = _finish(trader, OrderStatus.FILLED)

    del_ts.assert_not_called()
    assert trader.trailing_stop_cache.get(CODE) == {'highest_price': 70_000}
