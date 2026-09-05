"""부분체결 중인 주문의 취급 — 운영 정책과 실제 동작이 일치하는가.

운영 정책: "부분체결 중인 포지션은 운영자가 취소하기 전까지 추가 대기한다."

이 파일은 그 정책이 코드에서 실제로 성립하는지 확인한다. 두 갈래를 본다.
  ① 잔량이 시스템에 의해 자동 취소되지는 않는가 (대기가 실제로 유지되는가)
  ② 대기하는 동안 이미 체결된 물량은 손절·트레일링의 보호를 받는가

②가 특히 중요하다. is_pending(code)가 True인 동안 매도 워커는 그 종목을 통째로
건너뛰므로(trader._sell_worker), 대기가 길어질수록 체결분이 무방비로 남는다.
"추세추종 원칙: 탈출 전략이 없다면 포지션을 잡지 마라"와 직접 충돌하는 구간이다.
"""
from datetime import datetime, timedelta

import pandas as pd
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
    t.market_index_status = {CODE: {}}
    return t


def _stale_unfilled(minutes_ago=10, ord_qty=100, rmn_qty=70):
    """부분체결 상태로 오래 남아 있는 미체결 주문 (100주 중 30주 체결, 70주 잔량)."""
    ts = (datetime.now() - timedelta(minutes=minutes_ago)).strftime("%H%M%S")
    return [{
        'odno': ODNO, 'pdno': CODE, 'prdt_name': NAME,
        'ord_qty': str(ord_qty), 'rmn_qty': str(rmn_qty),
        'ord_tmd': ts, 'ord_unpr': '70000',
        'sll_buy_dvsn_cd': '02', 'sll_buy_dvsn_cd_name': '매수',
    }]


def _holding(qty=30, prpr=60000, buy=70000, profit_rate=-14.3):
    """부분체결로 확보된 30주. 손절선(-7%)을 크게 이탈한 상태."""
    return [{'pdno': CODE, 'prdt_name': NAME, 'ord_psbl_qty': str(qty),
             'evlu_pfls_rt': str(profit_rate), 'prpr': str(prpr),
             'pchs_avg_pric': str(buy), 'evlu_pfls_amt': '-300000'}]


# ---------------------------------------------------------------------------
# ① 잔량이 자동 취소되는가 (정책: 운영자가 취소할 때까지 대기)
# ---------------------------------------------------------------------------
@patch('modules.auto_trade.api.send_telegram_message')
@patch('modules.auto_trade.db_manager.db.insert_trade')
def test_partial_fill_remainder_is_auto_cancelled(mock_insert, mock_tg, trader):
    """[실제 동작] 잔량은 UNFILLED_ORDER_CANCEL_SECONDS 경과 시 시스템이 자동 취소한다.

    운영 정책('운영자가 취소할 때까지 대기')과 어긋나는 지점이다. 부분체결 여부를 보는
    분기가 없어, 타임아웃만 지나면 전량 미체결과 똑같이 잔량 취소가 나간다.
    (외부 주문 '(외부)' 태그만 예외로 보호된다)
    """
    trade = {'type': '매수', 'name': NAME}
    with patch('modules.auto_trade.api.get_unfilled_orders', return_value=_stale_unfilled()), \
         patch('modules.auto_trade.db_manager.db.get_trade_by_odno', return_value=trade), \
         patch('modules.auto_trade.api.revise_cancel_order',
               return_value={'rt_cd': '0', 'output': {}}) as mock_cancel, \
         patch.object(trader, 'is_market_open', return_value=True):
        trader.order_manager.manage_unfilled_orders()

    assert mock_cancel.called, (
        "잔량이 자동 취소되지 않았다 — 정책과 코드가 이미 일치한다면 이 테스트를 뒤집을 것")
    assert mock_cancel.call_args[0][2] == ODNO


@patch('modules.auto_trade.api.send_telegram_message')
def test_external_order_is_protected_from_auto_cancel(mock_tg, trader):
    """대조군 — 외부(MTS/HTS) 주문만 자동 취소에서 보호된다."""
    trade = {'type': '매수(외부)', 'name': NAME}
    with patch('modules.auto_trade.api.get_unfilled_orders', return_value=_stale_unfilled()), \
         patch('modules.auto_trade.db_manager.db.get_trade_by_odno', return_value=trade), \
         patch('modules.auto_trade.api.revise_cancel_order') as mock_cancel, \
         patch.object(trader, 'is_market_open', return_value=True):
        trader.order_manager.manage_unfilled_orders()

    mock_cancel.assert_not_called()


# ---------------------------------------------------------------------------
# ② 대기 중 체결분이 손절 보호를 받는가
# ---------------------------------------------------------------------------
@patch('modules.auto_trade.api.send_telegram_message')
@patch('modules.auto_trade.load_restricted_stocks', return_value={})
@patch('modules.auto_trade.api.fetch_sellable_quantity', return_value=30)
@patch('modules.auto_trade.api.get_chart_data')
@patch('modules.auto_trade.DefaultStrategy.analyze_sell')
def test_filled_portion_is_unprotected_while_order_pending(
        mock_analyze, mock_chart, mock_qty, mock_restricted, mock_tg, trader):
    """[위험] 부분체결 대기 중에는 이미 체결된 물량의 손절 판정이 아예 돌지 않는다.

    is_pending(code)가 True면 _sell_worker가 그 종목을 통째로 건너뛴다. 손절 신호가
    확실한 상황(-14%)에서도 analyze_sell 호출 자체가 일어나지 않는다.
    """
    mock_chart.return_value = pd.DataFrame({
        'close': [60000], 'high': [60000], 'low': [60000], 'open': [60000], 'volume': [1000]})
    mock_analyze.return_value = {'action': 'sell', 'reason': '손절', 'score': 2.0,
                                 'state': '매도', 'ind': {'rsi': 20, 'adx': 30, 'cci': -100}}

    trader.order_manager.pending_orders = {CODE: {ODNO: OrderStatus.PARTIAL_FILLED}}
    with patch.object(trader.order_manager, 'send_order') as mock_send:
        trader._check_sell_conditions(_holding(), is_market_open=True)

    mock_analyze.assert_not_called(), "판정이 돌았다면 보호 공백이 없다는 뜻 — 전제 재확인 필요"
    mock_send.assert_not_called()


@patch('modules.auto_trade.api.send_telegram_message')
@patch('modules.auto_trade.load_restricted_stocks', return_value={})
@patch('modules.auto_trade.api.fetch_sellable_quantity', return_value=30)
@patch('modules.auto_trade.api.get_chart_data')
@patch('modules.auto_trade.DefaultStrategy.analyze_sell')
def test_stop_loss_works_once_order_settles(
        mock_analyze, mock_chart, mock_qty, mock_restricted, mock_tg, trader):
    """대조군 — 주문이 종결되면(pending 해제) 같은 상황에서 손절이 정상 동작한다."""
    mock_chart.return_value = pd.DataFrame({
        'close': [60000], 'high': [60000], 'low': [60000], 'open': [60000], 'volume': [1000]})
    mock_analyze.return_value = {'action': 'sell', 'reason': '손절', 'score': 2.0,
                                 'state': '매도', 'ind': {'rsi': 20, 'adx': 30, 'cci': -100}}

    trader.order_manager.pending_orders = {}
    with patch.object(trader.order_manager, 'send_order', return_value='1') as mock_send:
        trader._check_sell_conditions(_holding(), is_market_open=True)

    mock_send.assert_called_once()
    assert mock_send.call_args[0][2] == 'sell'


def test_partial_filled_is_not_a_terminal_state(trader):
    """PARTIAL_FILLED는 종결 상태가 아니라 pending이 유지된다(대기의 근거)."""
    trader.order_manager.pending_orders = {CODE: {ODNO: OrderStatus.ORDER_SENT}}
    trader.order_manager.update_order_status(CODE, ODNO, OrderStatus.PARTIAL_FILLED)
    assert trader.order_manager.is_pending(CODE) is True

    trader.order_manager.update_order_status(CODE, ODNO, OrderStatus.FILLED)
    assert trader.order_manager.is_pending(CODE) is False


# ---------------------------------------------------------------------------
# ③ [보완 가] 손절 상황이면 미체결 매수를 즉시 취소해 청산 경로를 연다
# ---------------------------------------------------------------------------
def _pending_buy(trader, status=OrderStatus.PARTIAL_FILLED):
    trader.order_manager.pending_orders = {CODE: {ODNO: status}}


@patch('modules.auto_trade.api.send_telegram_message')
@patch('modules.auto_trade.load_restricted_stocks', return_value={})
@patch('modules.auto_trade.api.get_chart_data')
def test_pending_buy_is_cancelled_when_position_breaches_stop(
        mock_chart, mock_restricted, mock_tg, trader):
    """부분체결분이 손절선을 이탈하면, 타임아웃을 기다리지 않고 미체결 매수를 즉시 취소한다."""
    mock_chart.return_value = pd.DataFrame({
        'close': [60000], 'high': [60000], 'low': [60000], 'open': [60000], 'volume': [1000]})
    _pending_buy(trader)

    with patch('modules.auto_trade.db_manager.db.get_trade_by_odno',
               return_value={'type': '매수', 'name': NAME}), \
         patch('modules.auto_trade.api.revise_cancel_order',
               return_value={'rt_cd': '0', 'output': {}}) as mock_cancel:
        trader._check_sell_conditions(_holding(profit_rate=-14.3), is_market_open=True)

    mock_cancel.assert_called_once()
    args = mock_cancel.call_args[0]
    assert args[1] == 'cancel' and args[2] == ODNO
    assert args[4] == 0, "잔량 전부 취소(qty=0)여야 한다 — 잔량을 모른 채 부분 취소하면 안 된다"


@patch('modules.auto_trade.api.send_telegram_message')
@patch('modules.auto_trade.load_restricted_stocks', return_value={})
@patch('modules.auto_trade.api.get_chart_data')
def test_pending_buy_is_kept_when_position_is_healthy(
        mock_chart, mock_restricted, mock_tg, trader):
    """대조군 — 손절선 위라면 미체결 매수를 건드리지 않는다(정상 대기)."""
    mock_chart.return_value = pd.DataFrame({
        'close': [72000], 'high': [72000], 'low': [72000], 'open': [72000], 'volume': [1000]})
    _pending_buy(trader)

    with patch('modules.auto_trade.db_manager.db.get_trade_by_odno',
               return_value={'type': '매수', 'name': NAME}), \
         patch('modules.auto_trade.api.revise_cancel_order') as mock_cancel:
        trader._check_sell_conditions(_holding(prpr=72000, profit_rate=2.9), is_market_open=True)

    mock_cancel.assert_not_called()


@patch('modules.auto_trade.api.send_telegram_message')
@patch('modules.auto_trade.load_restricted_stocks', return_value={})
@patch('modules.auto_trade.api.get_chart_data')
def test_pending_sell_is_never_cancelled(mock_chart, mock_restricted, mock_tg, trader):
    """매도(청산) 주문은 취소 대상이 아니다 — 취소하면 포지션이 남아 손절이 무산된다."""
    mock_chart.return_value = pd.DataFrame({
        'close': [60000], 'high': [60000], 'low': [60000], 'open': [60000], 'volume': [1000]})
    _pending_buy(trader)

    with patch('modules.auto_trade.db_manager.db.get_trade_by_odno',
               return_value={'type': '매도', 'name': NAME}), \
         patch('modules.auto_trade.api.revise_cancel_order') as mock_cancel:
        trader._check_sell_conditions(_holding(profit_rate=-14.3), is_market_open=True)

    mock_cancel.assert_not_called()


@patch('modules.auto_trade.api.send_telegram_message')
@patch('modules.auto_trade.load_restricted_stocks', return_value={})
@patch('modules.auto_trade.api.get_chart_data')
def test_mixed_pending_cancels_only_the_buy(mock_chart, mock_restricted, mock_tg, trader):
    """매수·매도가 함께 걸려 있으면 매수만 거둔다.

    청산 중인 종목에 추가로 담는 것을 막는 쪽이 항상 옳고, 청산 주문을 취소하면
    포지션이 그대로 남는다. 두 주문을 구분하지 않으면 어느 쪽이든 잘못된다.
    """
    SELL_ODNO = "0000999999"
    mock_chart.return_value = pd.DataFrame({
        'close': [60000], 'high': [60000], 'low': [60000], 'open': [60000], 'volume': [1000]})
    trader.order_manager.pending_orders = {
        CODE: {ODNO: OrderStatus.PARTIAL_FILLED, SELL_ODNO: OrderStatus.ACCEPTED}}

    types = {ODNO: {'type': '매수', 'name': NAME}, SELL_ODNO: {'type': '매도', 'name': NAME}}
    with patch('modules.auto_trade.db_manager.db.get_trade_by_odno',
               side_effect=lambda o: types.get(o)), \
         patch('modules.auto_trade.api.revise_cancel_order',
               return_value={'rt_cd': '0', 'output': {}}) as mock_cancel:
        trader._check_sell_conditions(_holding(profit_rate=-14.3), is_market_open=True)

    cancelled = [c[0][2] for c in mock_cancel.call_args_list]
    assert cancelled == [ODNO], f"매수만 취소돼야 하는데 {cancelled} 를 취소했다"


# ---------------------------------------------------------------------------
# ④ [보완 나] 취소가 계속 실패하면 경보한다 (자동 복구되지 않는 상태)
# ---------------------------------------------------------------------------
@patch('modules.auto_trade.api.send_telegram_message')
def test_repeated_cancel_failure_raises_alert(mock_tg, trader):
    """취소 연속 실패가 한도에 닿으면 1회 경보한다 — 매 주기 스팸은 내지 않는다."""
    om = trader.order_manager
    fail = {'rt_cd': '1', 'msg1': '취소 불가 종목'}
    trade = {'type': '매수', 'name': NAME}

    with patch('modules.auto_trade.api.get_unfilled_orders', return_value=_stale_unfilled()), \
         patch('modules.auto_trade.db_manager.db.get_trade_by_odno', return_value=trade), \
         patch('modules.auto_trade.api.revise_cancel_order', return_value=fail), \
         patch.object(trader, 'is_market_open', return_value=True):
        for _ in range(om.CANCEL_FAILURE_ALERT_THRESHOLD + 2):
            om.manage_unfilled_orders()

    alerts = [c for c in mock_tg.call_args_list if '미체결 취소 실패' in str(c)]
    assert len(alerts) == 1, f"경보가 {len(alerts)}회 발송됐다 (한도 도달 시 1회여야 한다)"
    assert om.cancel_failures.get(ODNO) == om.CANCEL_FAILURE_ALERT_THRESHOLD + 2


@patch('modules.auto_trade.api.send_telegram_message')
def test_cancel_success_clears_failure_counter(mock_tg, trader):
    """취소에 성공하면 실패 카운터가 정리되어 다음 장애를 새로 센다."""
    om = trader.order_manager
    om.cancel_failures[ODNO] = 2
    trade = {'type': '매수', 'name': NAME}

    with patch('modules.auto_trade.api.get_unfilled_orders', return_value=_stale_unfilled()), \
         patch('modules.auto_trade.db_manager.db.get_trade_by_odno', return_value=trade), \
         patch('modules.auto_trade.db_manager.db.insert_trade'), \
         patch('modules.auto_trade.api.revise_cancel_order',
               return_value={'rt_cd': '0', 'output': {}}), \
         patch.object(trader, 'is_market_open', return_value=True):
        om.manage_unfilled_orders()

    assert ODNO not in om.cancel_failures


# ---------------------------------------------------------------------------
# ③ 매수/매도 구분을 못 하면 조용히 넘어가지 않는가 (2026-09-05)
# ---------------------------------------------------------------------------
#  _cancel_pending_buy_on_stop_loss 는 미체결 주문의 side 를 **DB 조회로만** 안다
#  (pending_orders 는 상태만 들고 side 를 모른다). 그 조회가 깨지면 매수 주문이
#  취소 대상에서 빠져, 손절 중인 종목에 매수가 그대로 열려 있게 된다.
#  종전에는 `except Exception: pass` 라 그 사실이 어디에도 남지 않았다.

@patch('modules.auto_trade.api.send_telegram_message')
@patch('modules.auto_trade.api.revise_cancel_order')
def test_주문_종류를_모르면_알리고_취소하지_않는다(mock_cancel, mock_tg, trader):
    trader.order_manager.pending_orders = {CODE: {ODNO: OrderStatus.ORDER_SENT}}
    logged = []
    with patch.object(trader, 'log', side_effect=lambda m, *a, **k: logged.append(m)), \
         patch('modules.auto_trade.db_manager.db.get_trade_by_odno',
               side_effect=RuntimeError("database is locked")):
        trader._cancel_pending_buy_on_stop_loss(CODE, NAME, _holding()[0])

    #  매도를 잘못 취소하면 청산 자체가 무산되므로 강행하지 않는다.
    mock_cancel.assert_not_called()
    assert any("확인하지 못해" in m for m in logged), (
        "손절 보호가 동작하지 않은 사실이 어디에도 남지 않는다")
    assert any(str(ODNO) in m for m in logged), "어느 주문인지 말해야 손으로 확인할 수 있다"


@patch('modules.auto_trade.api.send_telegram_message')
@patch('modules.auto_trade.api.revise_cancel_order',
       return_value={'rt_cd': '0', 'output': {'ODNO': '9'}})
def test_매수로_확인되면_종전대로_취소한다(mock_cancel, mock_tg, trader):
    """대조군 — 조회가 되면 보호는 그대로 동작한다."""
    trader.order_manager.pending_orders = {CODE: {ODNO: OrderStatus.ORDER_SENT}}
    logged = []
    with patch.object(trader, 'log', side_effect=lambda m, *a, **k: logged.append(m)), \
         patch('modules.auto_trade.db_manager.db.get_trade_by_odno',
               return_value={'type': '매수'}):
        trader._cancel_pending_buy_on_stop_loss(CODE, NAME, _holding()[0])

    mock_cancel.assert_called_once()
    assert not any("확인하지 못해" in m for m in logged)
