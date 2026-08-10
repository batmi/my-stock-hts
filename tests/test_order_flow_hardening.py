"""메인메뉴 [8] 종목 주문 관리 흐름의 회귀 테스트 (2026-08-10).

세 갈래를 다룬다.
 1. 수동 주문(send_order)에서 입력값이 주문을 반쯤 성공시키던 경로
 2. 예약 등록의 단계 이동·즉시발동 방어·유효기간 해석
 3. 예약 목록의 발동 거리 계산과 정렬
"""
import os
import sys
from unittest.mock import patch

import pytest

sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from modules import trading


# ==========================================================
# 1. 수동 주문 입력 정규화
# ==========================================================
@patch('rich.prompt.Prompt.ask')
@patch('modules.trading.api.place_order')
@patch('modules.trading.select_stock_from_balance')
@patch('modules.trading.api.fetch_sellable_quantity')
@patch('modules.trading.api.get_current_price')
def test_fractional_sell_quantity_does_not_break_after_send(
        mock_price, mock_qty, mock_select, mock_place, mock_ask):
    """소수점 수량은 정수로 확정된다.

    [배경] 검증은 float(qty)였는데 이후 코드는 int(qty)를 썼다. '1.5'는 검증을 통과해
    주문이 나간 뒤 int()에서 터졌고, 텔레그램 알림·트레일링 정리·예약 일괄취소가
    통째로 건너뛴 채 "통신/시스템 에러"만 떴다.
    """
    mock_ask.side_effect = ["1.5", "0", "y"]
    mock_select.return_value = ("005930", "삼성전자", False, "KRX", {'qty': 10})
    mock_qty.return_value = 10
    mock_price.return_value = 60000
    mock_place.return_value = {'rt_cd': '0', 'output': {'ODNO': '12345'}}

    with patch('config.console.print'), \
         patch('modules.trading.api.send_telegram_message'), \
         patch('modules.trading.db_manager.db.insert_trade'), \
         patch('modules.trading.analysis.get_snapshot', return_value=None), \
         patch('modules.trading.auto_trade.ConclusionMonitor'):
        trading.send_order('sell')

    mock_place.assert_called_once()
    assert mock_place.call_args[0][3] == "1", "소수점 수량이 정수로 확정되지 않았다"


@patch('rich.prompt.Prompt.ask')
@patch('modules.trading.api.place_order')
@patch('modules.trading.select_stock_from_balance')
@patch('modules.trading.api.fetch_sellable_quantity')
@patch('modules.trading.api.get_current_price')
def test_zero_quantity_is_rejected_before_sending(
        mock_price, mock_qty, mock_select, mock_place, mock_ask):
    """0주 주문은 전송 전에 막는다."""
    mock_ask.side_effect = ["0"]
    mock_select.return_value = ("005930", "삼성전자", False, "KRX", {'qty': 10})
    mock_qty.return_value = 10
    mock_price.return_value = 60000

    with patch('config.console.print'):
        trading.send_order('sell')

    mock_place.assert_not_called()


@patch('rich.prompt.Prompt.ask')
@patch('modules.trading.api.place_order')
@patch('modules.trading.select_stock_from_balance')
@patch('modules.trading.api.fetch_sellable_quantity')
@patch('modules.trading.api.get_current_price')
def test_domestic_limit_price_is_snapped_to_tick(
        mock_price, mock_qty, mock_select, mock_place, mock_ask):
    """국내 지정가는 호가단위로 보정한다 (소수점 입력에도 죽지 않는다).

    utils.adjust_to_tick은 예약 발동 경로만 쓰고 수동 주문 경로는 쓰지 않고 있었다.
    """
    mock_ask.side_effect = ["1", "60123.5", "y"]
    mock_select.return_value = ("005930", "삼성전자", False, "KRX", {'qty': 10})
    mock_qty.return_value = 10
    mock_price.return_value = 60000
    mock_place.return_value = {'rt_cd': '0', 'output': {'ODNO': '12345'}}

    with patch('config.console.print'), \
         patch('modules.trading.api.send_telegram_message'), \
         patch('modules.trading.db_manager.db.insert_trade'), \
         patch('modules.trading.analysis.get_snapshot', return_value=None), \
         patch('modules.trading.auto_trade.ConclusionMonitor'):
        trading.send_order('sell')

    sent_price = mock_place.call_args[0][4]
    # 60,000원대 호가단위는 100원 — 60,123.5는 60,100으로 붙는다
    assert sent_price == "60100", f"호가단위 보정이 되지 않았다: {sent_price}"


@patch('rich.prompt.Prompt.ask')
@patch('modules.trading.api.place_order')
@patch('modules.trading.select_stock_from_balance')
@patch('modules.trading.api.fetch_sellable_quantity')
@patch('modules.trading.api.get_current_price')
def test_unknown_sellable_quantity_is_not_treated_as_full_sell(
        mock_price, mock_qty, mock_select, mock_place, mock_ask):
    """매도가능수량을 못 구했을 때 부분 매도를 '전량'으로 단정하지 않는다.

    max_qty가 0이면 int(qty) >= int(max_qty)는 항상 참이라, 트레일링 앵커를 지우고
    예약 매도를 일괄 취소했다. '모른다'와 '전량이다'는 다르다.
    """
    mock_ask.side_effect = ["3", "0", "y"]
    # 잔고 선택 결과에도 수량이 없어 폴백조차 0이 되는 상황
    mock_select.return_value = ("005930", "삼성전자", False, "KRX", {'qty': 0})
    mock_qty.return_value = None       # 조회 실패
    mock_price.return_value = 60000
    mock_place.return_value = {'rt_cd': '0', 'output': {'ODNO': '12345'}}

    with patch('config.console.print'), \
         patch('modules.trading.api.send_telegram_message'), \
         patch('modules.trading.db_manager.db.insert_trade'), \
         patch('modules.trading.db_manager.db.cancel_reserved_sell_orders') as mock_cancel, \
         patch('modules.trading.db_manager.db.delete_trailing_stop') as mock_del, \
         patch('modules.trading.analysis.get_snapshot', return_value=None), \
         patch('modules.trading.auto_trade.ConclusionMonitor'):
        trading.send_order('sell')

    mock_cancel.assert_not_called()
    mock_del.assert_not_called()


# ==========================================================
# 2. 예약 등록 — 단계 이동·방어
# ==========================================================
@pytest.fixture
def reserve_env():
    """예약 등록의 외부 의존을 전부 막는다."""
    with patch('modules.trading.select_account', return_value=("12345678", "01", "실전투자")), \
         patch('modules.trading.utils.select_target_stock', return_value=("005930", "삼성전자", False)), \
         patch('modules.trading.api.get_current_price', return_value=80000.0), \
         patch('modules.trading.api.get_deposit_balance', return_value=None), \
         patch('modules.trading.analysis.print_table'), \
         patch('modules.trading.db_manager.db.get_pending_reserved_orders', return_value=[]), \
         patch('modules.trading.api.send_telegram_message'), \
         patch('modules.trading._print_reserved_orders_table'), \
         patch('modules.trading.utils.pause'), \
         patch('config.console.print'):
        yield


@patch('modules.trading.db_manager.db.insert_reserved_order')
@patch('modules.trading.utils.show_menu')
@patch('modules.trading.Prompt.ask')
def test_back_returns_to_previous_step(mock_ask, mock_menu, mock_insert, reserve_env):
    """b는 등록 전체를 버리지 않고 직전 단계로 되돌린다.

    종전에는 어느 단계든 b/q가 모두 '등록 취소'라, 마지막 유효기간에서 오타를 내면
    계좌 선택부터 아홉 단계를 다시 입력해야 했다.
    """
    mock_menu.side_effect = ["1", "7"]           # 매수 → 상태 진입(STATE)
    # 상태 '1' → 단가 '0' → 수량 '10' → 유효기간에서 'b' → 수량 다시 '5' → 유효 '4' → 확인 'y'
    mock_ask.side_effect = ["1", "0", "10", "b", "5", "4", "y"]

    trading.register_reserved_order()

    mock_insert.assert_called_once()
    assert mock_insert.call_args[1]['qty'] == 5, "b가 직전 단계(수량)로 되돌리지 않았다"


@patch('modules.trading.db_manager.db.insert_reserved_order')
@patch('modules.trading.utils.show_menu')
@patch('modules.trading.Prompt.ask')
def test_immediate_trigger_target_is_challenged(mock_ask, mock_menu, mock_insert, reserve_env):
    """돌파 조건에 현재가보다 낮은 목표가를 넣으면 되묻는다.

    방향을 반대로 적은 실수가 곧바로 시장가 체결로 이어지는 유일한 경로다.
    """
    mock_menu.side_effect = ["1", "2"]           # 매수 → BREAKOUT
    # 목표가 70,000(현재가 80,000 아래 → 즉시 발동) → 'n'으로 물러남 → 90,000 재입력
    # → 단가 엔터(목표가와 동일) → 수량 10 → 유효 4 → 확인 y
    mock_ask.side_effect = ["70000", "n", "90000", "", "10", "4", "y"]

    trading.register_reserved_order()

    mock_insert.assert_called_once()
    assert mock_insert.call_args[1]['target_price'] == 90000


@patch('modules.trading.db_manager.db.insert_reserved_order')
@patch('modules.trading.utils.show_menu')
@patch('modules.trading.Prompt.ask')
def test_bad_expiry_is_not_silently_indefinite(mock_ask, mock_menu, mock_insert, reserve_env):
    """유효기간 오타를 '무기한'으로 떨어뜨리지 않는다.

    종전에는 8자리가 아니면 전부 20991231이 됐다 — 실수가 가장 오래 사는 주문이 되는 방향.
    """
    mock_menu.side_effect = ["1", "7"]
    # 유효기간에 '1231'(4자리 오타) → 거부 후 재입력 '4'(무기한)
    mock_ask.side_effect = ["1", "0", "10", "1231", "4", "y"]

    trading.register_reserved_order()

    mock_insert.assert_called_once()
    assert mock_insert.call_args[1]['expire_dt'] == "20991231"


@patch('modules.trading.db_manager.db.insert_reserved_order')
@patch('modules.trading.utils.show_menu')
@patch('modules.trading.Prompt.ask')
def test_nonprice_condition_defaults_to_market_price(mock_ask, mock_menu, mock_insert, reserve_env):
    """비가격 조건의 주문 단가 기본값은 시장가다.

    종전 기본값은 '등록 시점 현재가' 고정이었다. 신고가 돌파처럼 발동 시점에 가격이
    이미 올라 있는 조건에서는 그 지정가에 영원히 안 붙어, 조건은 맞았는데 미체결로 남았다.
    """
    mock_menu.side_effect = ["1", "8"]           # 매수 → NEW_HIGH
    # 기준 '1'(52주) → 단가 엔터(기본값이 그대로 쓰임) → 수량 10 → 유효 4 → 확인 y
    mock_ask.side_effect = ["1", "0", "10", "4", "y"]

    trading.register_reserved_order()

    # 단가 프롬프트의 기본값이 "0"(시장가)인지 확인
    price_calls = [c for c in mock_ask.call_args_list if "주문 단가" in str(c)]
    assert price_calls, "주문 단가 프롬프트가 호출되지 않았다"
    assert price_calls[0].kwargs.get('default') == "0"
    assert mock_insert.call_args[1]['order_price'] == 0.0


@patch('modules.trading.db_manager.db.insert_reserved_order')
@patch('modules.trading.utils.show_menu')
@patch('modules.trading.Prompt.ask')
def test_zero_reserved_quantity_is_rejected(mock_ask, mock_menu, mock_insert, reserve_env):
    """0주 예약은 등록되지 않는다."""
    mock_menu.side_effect = ["1", "7"]
    mock_ask.side_effect = ["1", "0", "0", "10", "4", "y"]

    trading.register_reserved_order()

    assert mock_insert.call_args[1]['qty'] == 10


# ==========================================================
# 3. 조건 표기·발동 거리
# ==========================================================
def test_condition_text_covers_every_type():
    """조건 표기는 한 곳에서만 만든다 (등록·목록·텔레그램이 갈리지 않도록)."""
    cases = [
        ("STOP", 70000, "70,000원"),
        ("BREAKOUT", 90000, "90,000원"),
        ("TRAILING_BUY", 3.0, "바닥 대비 3.0% 반등"),
        ("TRAILING_SELL", 3.0, "고점 대비 3.0% 하락"),
        ("SMART_MONEY", 0, "외국인/기관 순매수 전환"),
        ("NEW_HIGH", 250, "52주 신고가 경신"),
        ("NEW_HIGH", 0, "사상 최고가 경신"),
        ("STATE_STRONGBUY", 0, "강매수 진입"),
        ("EMA_UP", 20, "EMA 20일선 상향돌파"),
        ("EMA_DOWN", 60, "EMA 60일선 하향이탈"),
        ("RSI_DOWN", 35, "RSI 35 이하"),
        ("SCORE_UP", 8.0, "점수 8.0점 이상"),
    ]
    for ct, tp, expected in cases:
        assert trading._condition_text(ct, tp) == expected, ct


def test_unknown_distance_sorts_last():
    """거리를 계산할 수 없는 조건은 '가장 임박한 것'처럼 위로 올라오면 안 된다."""
    near = {'condition_type': 'BREAKOUT', 'target_price': 81000.0}
    unknown = {'condition_type': 'SMART_MONEY', 'target_price': 0.0}

    near_key, near_str = trading._reserved_distance(near, 80000.0)
    unk_key, unk_str = trading._reserved_distance(unknown, 80000.0)

    assert near_str == "+1.25%"
    assert unk_str == "-"
    assert unk_key > near_key


def test_distance_is_unknown_without_a_price():
    """현재가를 못 구하면 거리는 '-'다 (0%로 위장하지 않는다)."""
    order = {'condition_type': 'BREAKOUT', 'target_price': 81000.0}
    key, text = trading._reserved_distance(order, 0)
    assert text == "-"
    assert key > 1000


def test_trailing_distance_uses_tracked_extreme():
    """트레일링은 추적된 최저/최고가에서 발동가를 역산한다."""
    buy = {'condition_type': 'TRAILING_BUY', 'target_price': 3.0, 'lowest_price': 100000.0}
    _, text = trading._reserved_distance(buy, 100000.0)
    assert text == "+3.00%"

    sell = {'condition_type': 'TRAILING_SELL', 'target_price': 3.0, 'highest_price': 100000.0}
    _, text = trading._reserved_distance(sell, 100000.0)
    assert text == "-3.00%"


def test_expiring_orders_sort_to_the_top():
    """오늘 만료되는 주문이 목록 맨 위로 온다."""
    from datetime import datetime
    today = datetime.now().strftime("%Y%m%d")
    orders = [
        {'id': 1, 'code': 'A', 'market': 'KR', 'condition_type': 'BREAKOUT',
         'target_price': 80100.0, 'expire_dt': '20991231'},
        {'id': 2, 'code': 'B', 'market': 'KR', 'condition_type': 'BREAKOUT',
         'target_price': 99000.0, 'expire_dt': today},
    ]
    with patch('modules.trading.db_manager.db.get_pending_reserved_orders', return_value=orders), \
         patch('modules.trading.api.get_current_price', return_value=80000.0):
        result = trading._load_reserved_orders_with_context()

    assert [o['id'] for o in result] == [2, 1]


def test_expiry_input_rejects_the_past_and_typos():
    """유효기간 해석: 1~4는 그대로, 8자리가 아니거나 과거면 None."""
    assert trading._rsv_resolve_expire("4") == "20991231"
    assert trading._rsv_resolve_expire("1231") is None
    assert trading._rsv_resolve_expire("20200101") is None
    assert trading._rsv_resolve_expire("abcd") is None
    assert trading._rsv_resolve_expire("20991231") == "20991231"
