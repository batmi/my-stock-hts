"""실거래 손익은 총차익(비용 제외)이고, 비용은 cost_amt 로 따로 남아 원금 불변량만 그것을 뺀다.

[운용자 결정 2026-09-14] 한온시스템 3,460 매수 → 애프터 최유리 매도 3,455 체결의 손익은 −5원이다
(−12원이 아니다). 가상투자만 종전대로 순손익. 체결 확인이 실제 체결가로 다시 계산하려면 기록에
매입가가 있어야 하는데, 수동 매도만 insert_trade 에 buy_price 를 넘기지 않아 추정치(−7원)가 굳었다.
"""
import inspect
from unittest.mock import patch

import config
from core import trading_cost
from modules import trading
from modules.auto_trade import conclusion


def _real():
    return patch.object(config.session, 'is_paper', False, create=True)


def _paper():
    return patch.object(config.session, 'is_paper', True, create=True)


def test_real_trading_profit_is_gross_and_cost_is_separate():
    with _real():
        amt, rate, cost = trading_cost.realized_profit(3460, 3455, 1)
    assert amt == -5 and round(rate, 2) == -0.14
    assert cost == trading_cost.round_trip_cost(3460, 3455, 1) and cost > 0
    assert amt - cost == trading_cost.net_realized_profit(3460, 3455, 1)[0], "총차익 − 비용 = 순손익"


def test_paper_trading_keeps_net_profit():
    with _paper():
        amt, rate, cost = trading_cost.realized_profit(3460, 3455, 1)
    assert amt == trading_cost.net_realized_profit(3460, 3455, 1)[0] == -12
    assert cost == 0.0, "가상투자는 비용이 이미 손익에 빠져 있어 따로 남기지 않는다(불변량 이중 차감 방지)"


def test_recalc_needs_buy_price_and_uses_actual_fill():
    origin = {'type': '매도(수동)', 'buy_price': 3460.0, 'profit_amt': -7, 'profit_rate': -0.2}
    with _real():
        amt, rate, cost = conclusion._recalc_realized(origin, 3455, 1, False, -7, -0.2)
    assert amt == -5 and cost > 0
    # 매입가가 없으면 추정치를 그대로 둔다 — 이것이 실측에서 −7원이 굳은 경로다
    assert conclusion._recalc_realized({**origin, 'buy_price': 0.0}, 3455, 1, False, -7, -0.2) == (-7, -0.2, None)


def test_manual_order_record_carries_buy_price_and_cost():
    src = inspect.getsource(trading.send_order)
    i = src.index('insert_trade(f"{t_type}(수동)"')
    call = src[i:src.index("\n", i)]
    assert "buy_price=" in call and "cost_amt=" in call


def test_fill_row_carries_buy_price_and_cost():
    src = inspect.getsource(conclusion.ConclusionMonitor._check_conclusions)
    i = src.index('order_status="체결", reason=reason_to_save')
    line = src[i:src.index("\n", i)]
    assert "buy_price=_bp" in line and "cost_amt=_cost_amt" in line


def test_invariant_subtracts_cost():
    from modules import db_manager
    src = inspect.getsource(db_manager.DBManager.get_realized_profit_between)
    assert "profit_amt - COALESCE(cost_amt, 0)" in src, "총차익을 그대로 원금 식에 넣으면 왕복 비용이 가짜 출금이 된다"


def test_intraday_principal_uses_net_realized():
    from modules.auto_trade import trader as _t
    src = inspect.getsource(_t.AutoTrader._monitor_account_status)
    assert "float(t.get('cost_amt') or 0)" in src, "장중 원금 식도 총차익에서 비용을 빼야 한다"
