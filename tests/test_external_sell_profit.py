"""앱/HTS 외부 주문·예약 주문의 매도 체결도 실현손익을 적는다.

[실측 2026-09-14] 맥북에서 낸 토스 애프터 주문(3,455 매수 → 3,445 매도 1주)을 파이5B 가 외부 주문으로
감지했더니 거래 평가에 '+0원 (+0.00%)' 로 찍혔다. 원 주문 행이 없어 buy_price 가 0 이었기 때문인데,
같은 원장에 그 매수 체결 행이 있다 — 마지막 매도 이후 매수 체결의 수량가중 평단으로 손익을 만든다.
"""
import inspect
from unittest.mock import patch

import config
from modules import db_manager
from modules.auto_trade import conclusion


def _real():
    return patch.object(config.session, 'is_paper', False, create=True)


def test_ledger_buy_price_prefers_fills_and_weights_by_qty():
    rows = {'018880': [
        {'odno': 'A', 'order_status': '접수', 'price': 3400.0, 'qty': 1},
        {'odno': 'A', 'order_status': '체결', 'price': 3455.0, 'qty': 1},
        {'odno': 'B', 'order_status': '체결', 'price': 3475.0, 'qty': 3},
    ]}
    with patch.object(db_manager.db, 'get_buy_trades_for_current_holdings', return_value=rows) as m:
        bp = conclusion._ledger_buy_price('018880', '189-01')
    assert bp == (3455 * 1 + 3475 * 3) / 4
    assert m.call_args.kwargs.get('account') == '189-01', "남의 계좌 매수로 평단을 만들면 안 된다"


def test_ledger_buy_price_falls_back_to_accepted_rows_once_per_odno():
    rows = {'X': [
        {'odno': 'A', 'order_status': '접수', 'price': 100.0, 'qty': 2},
        {'odno': 'A', 'order_status': '정정', 'price': 100.0, 'qty': 2},
    ]}
    with patch.object(db_manager.db, 'get_buy_trades_for_current_holdings', return_value=rows):
        assert conclusion._ledger_buy_price('X', None) == 100.0


def test_ledger_buy_price_unknown_is_zero():
    with patch.object(db_manager.db, 'get_buy_trades_for_current_holdings', return_value={'X': []}):
        assert conclusion._ledger_buy_price('X', None) == 0.0
    with patch.object(db_manager.db, 'get_buy_trades_for_current_holdings', side_effect=RuntimeError("db")):
        assert conclusion._ledger_buy_price('X', None) == 0.0


def test_fill_path_derives_profit_for_external_sell():
    src = inspect.getsource(conclusion.ConclusionMonitor._check_conclusions)
    i = src.index('_bp = _ledger_buy_price(code, f"{cano}-{acnt}")')
    block = src[i - 400:i + 600]
    assert '"매도" in type_name and _bp <= 0 and not profit_amt' in block, "원 주문이 매입가를 실어 왔으면 건드리지 않는다"
    assert 'trading_cost.realized_profit(' in block, "손익 정책(총차익·비용 별도)은 SSOT 하나"
    # 알림도 방금 구한 값을 쓴다
    assert 'if not profit_msg and _bp > 0:' in src


def test_external_sell_example_is_minus_ten():
    from core import trading_cost
    with _real():
        amt, rate, cost = trading_cost.realized_profit(3455, 3445, 1)
    assert amt == -10 and cost > 0
