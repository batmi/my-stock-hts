"""pending 주문이 영원히 안 풀릴 수 있는가 — PARTIAL_FILLED / ACCEPTED 고아 주문.

is_pending(code)가 True인 동안 그 종목은 매도 워커에서 통째로 빠진다. 따라서
'pending이 풀리지 않는 상태'는 그 종목의 손절이 무기한 정지된다는 뜻이다.

pending에서 빠지는 경로는 두 갈래뿐이다.
  ① update_order_status 가 FILLED/CANCELED/REJECTED 를 받는다 (= API 체결 이력이 알려줘야 한다)
  ② manage_unfilled_orders 의 로컬 폴백 (API 미체결 목록에 없는 주문을 강제 취소)

②는 `status == ORDER_SENT` 인 주문만 본다. 즉 API가 한 번이라도 상태를 진행시킨 뒤
(ACCEPTED·PARTIAL_FILLED) 목록에서 사라지면 어느 경로에도 걸리지 않는다.

이 파일은 그 고아 상태가 실제로 성립하는지 확인하고, 성립한다면 상한(경보)이
붙는지 본다. 재기동하면 pending은 비므로 위험은 '세션 내'로 한정된다.
"""
from datetime import datetime, timedelta

import pytest
from unittest.mock import MagicMock, patch

import config
from modules.auto_trade import AutoTrader
from modules.auto_trade.common import OrderStatus

CODE = "005930"
NAME = "삼성전자"
ODNO = "0000123456"


@pytest.fixture
def trader():
    AutoTrader._instance = None
    t = AutoTrader()
    t.is_running = True
    t.order_manager.pending_orders.clear()
    t.order_manager.cancel_failures.clear()
    yield t
    t.order_manager.pending_orders.clear()
    t.order_manager.cancel_failures.clear()


def _register(trader, status):
    """주문을 pending에 올리고 지정한 상태로 만든다."""
    om = trader.order_manager
    om.register_manual_order(CODE, ODNO)
    if status != OrderStatus.ORDER_SENT:
        with om._lock:
            om.pending_orders[CODE][ODNO] = status


def _old_trade(minutes_ago=30):
    return {'type': '매수', 'name': NAME, 'qty': 100, 'price': 70000,
            'time': (datetime.now() - timedelta(minutes=minutes_ago)).strftime("%Y-%m-%d %H:%M:%S")}


def _sweep(trader):
    """API 미체결 목록이 비어 있는(= 주문이 사라진) 상태로 미체결 관리를 1회 돌린다."""
    with patch.object(trader, 'is_market_open', return_value=True), \
         patch('modules.auto_trade.api.get_unfilled_orders', return_value=[]), \
         patch('modules.auto_trade.db_manager.db.get_trade_by_odno', return_value=_old_trade()), \
         patch('modules.auto_trade.db_manager.db.insert_trade'), \
         patch('modules.auto_trade.api.send_telegram_message') as tg, \
         patch('modules.auto_trade.api.revise_cancel_order',
               return_value={'rt_cd': '0', 'output': {'ODNO': 'C1'}}):
        trader.order_manager.manage_unfilled_orders()
    return tg


# ─────────────────────────────── 대조군 ───────────────────────────────

# ─────────────────────── 고아 상태가 성립하는가 ───────────────────────

@pytest.mark.parametrize("status", [OrderStatus.PARTIAL_FILLED, OrderStatus.ACCEPTED])
def test_progressed_orphan_is_not_swept_by_local_fallback(trader, status):
    """[현상] API가 상태를 진행시킨 뒤 사라지면 로컬 폴백이 건너뛴다.

    폴백이 ORDER_SENT만 보기 때문이다. 이 자체는 설계상 의도일 수 있으나,
    그 결과로 pending이 무기한 유지되면 손절이 함께 멈춘다.
    """
    _register(trader, status)
    _sweep(trader)
    assert trader.order_manager.is_pending(CODE), \
        "전제 확인용 — 폴백이 이 상태도 처리한다면 아래 경보 요구는 불필요하다"


@pytest.mark.parametrize("status", [OrderStatus.PARTIAL_FILLED, OrderStatus.ACCEPTED])
def test_stuck_pending_is_alerted(trader, status):
    """무기한 pending 은 운영자에게 알려야 한다.

    자동 복구가 불가능한 상태다(API가 알려주지 않으면 영원히 안 풀린다). 손절이
    멈춘 종목이 조용히 방치되면 안 되므로, 취소 실패 경보와 같은 취급을 한다.
    """
    _register(trader, status)
    tg = _sweep(trader)
    sent = " ".join(str(c) for c in tg.call_args_list)
    assert tg.called and CODE in sent, (
        "API에서 사라진 주문이 pending으로 남았는데 아무 경보도 없다 — "
        "그 종목의 손절 판정이 세션 내내 멈춘 채 방치된다")


def test_real_account_order_sent_orphan_is_alerted(trader):
    """실계좌에는 로컬 폴백이 아예 없다 — ORDER_SENT 고아도 갇히므로 경보 대상이다.

    로컬 폴백이 없는 경로이므로, '폴백이 처리하니 괜찮다'고 판단하면
    정작 실계좌가 무방비가 된다.
    """
    _register(trader, OrderStatus.ORDER_SENT)
    tg = _sweep(trader)

    assert trader.order_manager.is_pending(CODE), "실계좌에는 폴백이 없다는 전제 확인"
    assert tg.called, "실계좌에서 사라진 주문이 pending으로 남았는데 경보가 없다"


def test_alert_is_sent_once_not_every_cycle(trader):
    """경보는 1회만 — 주기마다 반복되면 텔레그램이 도배된다."""
    _register(trader, OrderStatus.PARTIAL_FILLED)
    first = _sweep(trader)
    second = _sweep(trader)
    assert first.called
    assert not second.called, "같은 고아 주문으로 매 주기 경보가 나간다"


def test_alert_clears_when_order_resolves(trader):
    """주문이 종결되면 경보 상태도 정리돼 다음 사건을 다시 알릴 수 있어야 한다."""
    _register(trader, OrderStatus.PARTIAL_FILLED)
    _sweep(trader)

    trader.order_manager.update_order_status(CODE, ODNO, OrderStatus.FILLED)
    assert not trader.order_manager.is_pending(CODE)

    _register(trader, OrderStatus.PARTIAL_FILLED)
    again = _sweep(trader)
    assert again.called, "이전 경보 기록이 남아 새 사건을 알리지 못한다"


def test_fresh_progressed_order_is_not_alerted(trader):
    """[오탐 방지] 방금 접수된 주문은 경보 대상이 아니다.

    ACCEPTED는 정상적인 대기 상태다. 취소 타임아웃도 지나지 않았는데 경보하면
    평범한 지정가 대기가 전부 알림이 된다.
    """
    _register(trader, OrderStatus.ACCEPTED)
    with patch.object(trader, 'is_market_open', return_value=True), \
         patch('modules.auto_trade.api.get_unfilled_orders', return_value=[]), \
         patch('modules.auto_trade.db_manager.db.get_trade_by_odno',
               return_value=_old_trade(minutes_ago=0)), \
         patch('modules.auto_trade.db_manager.db.insert_trade'), \
         patch('modules.auto_trade.api.send_telegram_message') as tg, \
         patch('modules.auto_trade.api.revise_cancel_order',
               return_value={'rt_cd': '0', 'output': {}}):
        trader.order_manager.manage_unfilled_orders()

    assert not tg.called, "접수 직후의 정상 대기 주문에 경보가 나갔다"


# ─────────────── 거래소 대사로 고아를 스스로 푸는가 ───────────────
#
# [왜 이걸 넣었나] 자동 정리를 막았던 이유는 하나였다 — "주문이 실제로 살아 있는데
# pending을 풀면 매수를 열어 둔 채 매도가 나가 서로 싸운다." 맞는 걱정이지만 그 전제는
# '살아 있는지 알 수 없다'는 것이다. 당일 주문내역을 보면 알 수 있고(주문 무응답 대사가
# 이미 같은 방식을 쓴다), 확인된 것만 풀면 그 위험이 성립하지 않는다.
# 방치하면 그 종목의 손절이 세션 내내 멈춘다 — 라즈베리파이는 수 주간 재기동하지 않는다.

def _sweep_real(trader, history_rows):
    """실계좌 모드에서, 당일 주문내역이 주어진 상태로 미체결 관리를 1회 돌린다."""
    with patch.object(trader, 'is_market_open', return_value=True), \
         patch('modules.auto_trade.api.get_unfilled_orders', return_value=[]), \
         patch('modules.auto_trade.api.get_today_history',
               return_value={'rt_cd': '0', 'output1': history_rows}), \
         patch('modules.auto_trade.db_manager.db.get_trade_by_odno', return_value=_old_trade()), \
         patch('modules.auto_trade.db_manager.db.insert_trade'), \
         patch('modules.auto_trade.api.send_telegram_message'):
        trader.order_manager.manage_unfilled_orders()


@pytest.mark.parametrize("status", [OrderStatus.PARTIAL_FILLED, OrderStatus.ACCEPTED])
def test_settled_orphan_is_released_in_real_account(trader, status):
    """잔량 0(종결)이 확인되면 대기를 푼다 — 그래야 손절 판정이 다시 돈다."""
    _register(trader, status)
    _sweep_real(trader, [{'odno': ODNO, 'rmn_qty': '0', 'pdno': CODE}])
    assert not trader.order_manager.is_pending(CODE), \
        "거래소에서 종결이 확인됐는데도 대기가 풀리지 않았다 — 손절이 계속 멈춘다"


@pytest.mark.parametrize("status", [OrderStatus.PARTIAL_FILLED, OrderStatus.ACCEPTED])
def test_live_order_is_never_released(trader, status):
    """잔량이 남아 있으면 살아 있는 주문이다 — 풀면 주문끼리 싸운다."""
    _register(trader, status)
    _sweep_real(trader, [{'odno': ODNO, 'rmn_qty': '50', 'pdno': CODE}])
    assert trader.order_manager.is_pending(CODE), "살아 있는 주문의 대기를 풀었다"


@pytest.mark.parametrize("rows", [[], [{'odno': '9999999999', 'rmn_qty': '0'}]])
def test_unconfirmed_orphan_is_left_alone(trader, rows):
    """내역에 없으면 '종결'이 아니라 '모름'이다 — 추측으로 풀지 않는다."""
    _register(trader, OrderStatus.ACCEPTED)
    _sweep_real(trader, rows)
    assert trader.order_manager.is_pending(CODE)


def test_history_failure_leaves_pending(trader):
    """대사 조회가 실패하면 아무것도 단정하지 않는다."""
    _register(trader, OrderStatus.ACCEPTED)
    with patch.object(trader, 'is_market_open', return_value=True), \
         patch('modules.auto_trade.api.get_unfilled_orders', return_value=[]), \
         patch('modules.auto_trade.api.get_today_history', side_effect=RuntimeError("down")), \
         patch('modules.auto_trade.db_manager.db.get_trade_by_odno', return_value=_old_trade()), \
         patch('modules.auto_trade.db_manager.db.insert_trade'), \
         patch('modules.auto_trade.api.send_telegram_message'):
        trader.order_manager.manage_unfilled_orders()
    assert trader.order_manager.is_pending(CODE)


def test_odno_zero_padding_is_matched(trader):
    """발주 응답과 내역의 주문번호는 앞자리 0 패딩이 다를 수 있다."""
    _register(trader, OrderStatus.ACCEPTED)
    _sweep_real(trader, [{'odno': ODNO.lstrip('0'), 'rmn_qty': '0', 'pdno': CODE}])
    assert not trader.order_manager.is_pending(CODE), "0 패딩 차이로 같은 주문을 놓쳤다"
