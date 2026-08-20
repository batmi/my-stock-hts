"""백테스트·관찰모드·실거래가 같은 거래비용 자(尺)를 쓰는가.

[배경] 2026-08-10 이전에는 세 환경의 비용 모델이 전부 달랐다.

  - 백테스트: 슬리피지 편도 0.2% + 매도 수수료 0.23%(이름 없는 리터럴), **매수 수수료 없음**
  - 관찰모드: 수수료 매수 0.015% / 매도 0.23%, **슬리피지 없음** (지정가 그대로 체결)
  - 실거래 DB: 매도 '발주 판단 시점'의 평가손익을 그대로 실현손익으로 기록 —
    체결 확인 단계에서 실제 체결가를 알고 있으면서도 갱신하지 않았고, 비용도 안 뺐다

세 자가 다르면 성과 차이가 전략 때문인지 비용 모델 때문인지 분리되지 않는다.
관찰모드는 라즈베리파이에서 전략을 검증하는 도구라 특히 문제였다 — 백테스트 결과와
직접 비교할 수 없었다.

여기서는 요율의 '값'이 아니라 **세 경로가 한 소스를 쓰는가**를 고정한다. 요율 자체는
증권사·세법이 정하는 사실이라 config 하나에서 바뀌어야 한다.
"""
import pytest

import config
from modules import trading_cost


# ─────────────────────────────────────────────
# 1. 단일 소스
# ─────────────────────────────────────────────

def test_paper_broker_reads_the_shared_rates():
    """관찰모드가 요율을 따로 들고 있으면 안 된다."""
    from modules import paper_broker
    assert paper_broker.BUY_FEE_RATE == config.BUY_FEE_RATE
    assert paper_broker.SELL_FEE_RATE == config.SELL_FEE_RATE


def test_backtest_has_no_hardcoded_fee_literal():
    """백테스트에 무명 리터럴이 남아 있으면 요율을 바꿔도 한쪽만 바뀐다."""
    import inspect
    from modules import backtest
    src = inspect.getsource(backtest)
    assert "0.0023" not in src, "매도 수수료가 아직 하드코딩돼 있다"
    assert "trading_cost." in src, "백테스트가 공용 비용 계산을 쓰지 않는다"


def test_rate_change_propagates_everywhere(monkeypatch):
    """config 하나만 바꾸면 세 경로가 함께 움직인다."""
    monkeypatch.setattr(config, 'SELL_FEE_RATE', 0.01)
    assert trading_cost.sell_fee(1_000_000) == 10_000


# ─────────────────────────────────────────────
# 2. 왕복 비용 · 실현손익
# ─────────────────────────────────────────────

def test_net_profit_subtracts_both_legs():
    """실현손익은 매수·매도 양쪽 비용을 모두 뺀다."""
    amt, rate = trading_cost.net_realized_profit(10_000, 11_000, 10)
    gross = 10_000
    cost = trading_cost.buy_fee(100_000) + trading_cost.sell_fee(110_000)
    assert amt == pytest.approx(gross - cost)
    assert rate == pytest.approx((gross - cost) / 100_000 * 100)


def test_a_thin_gain_becomes_a_loss_after_costs():
    """총이익이 왕복 비용보다 작으면 실제로는 손실이다 — '승'으로 세면 안 된다.

    이 한 줄이 이번 수정의 핵심이다. 승률·손익비는 파라미터 판단의 근거이고,
    비용을 빼지 않으면 그 왜곡이 그대로 설정 결정으로 넘어간다.
    """
    amt, _ = trading_cost.net_realized_profit(100_000, 100_100, 10)   # 총 +0.1%
    assert amt < 0, "왕복 비용(약 0.235%)보다 작은 이익이 흑자로 잡혔다"


def test_break_even_price_is_above_the_buy_price():
    """본전이 되려면 매수가보다 비용만큼 위에서 팔아야 한다."""
    buy = 100_000
    same, _ = trading_cost.net_realized_profit(buy, buy, 10)
    assert same < 0
    higher, _ = trading_cost.net_realized_profit(buy, buy * 1.01, 10)
    assert higher > 0


@pytest.mark.parametrize("qty,buy,sell", [(0, 100, 200), (10, 0, 200), (10, 100, 0)])
def test_degenerate_inputs_return_zero(qty, buy, sell):
    """수량·가격이 비면 0을 준다(0으로 나누지 않는다)."""
    assert trading_cost.net_realized_profit(buy, sell, qty) == (0.0, 0.0)


def test_overseas_uses_its_own_rates():
    """증권거래세는 국내 세목이다 — 해외 매도에 국내 요율을 쓰면 없는 세금을 물린다.

    반대로 국내(0.015%)보다 비싼 해외 위탁수수료가 매수에서 빠져 있었다. 결과적으로
    2026-08-10 이전 해외 종목 백테스트는 왕복 비용을 절반쯤으로 축소 계상했다.
    """
    amt = 1_000_000
    assert trading_cost.sell_fee(amt, is_overseas=True) == pytest.approx(
        amt * config.OVERSEAS_SELL_FEE_RATE)
    assert trading_cost.buy_fee(amt, is_overseas=True) == pytest.approx(
        amt * config.OVERSEAS_BUY_FEE_RATE)
    # 해외는 국내 요율을 쓰지 않는다
    assert trading_cost.sell_fee(amt, is_overseas=True) != trading_cost.sell_fee(amt)
    assert trading_cost.buy_fee(amt, is_overseas=True) != trading_cost.buy_fee(amt)


def test_overseas_round_trip_is_not_cheaper_than_domestic():
    """해외 왕복 비용이 국내보다 싸게 나오면 모델이 뒤집힌 것이다."""
    dom = trading_cost.round_trip_cost(100_000, 110_000, 10)
    ovs = trading_cost.round_trip_cost(100_000, 110_000, 10, is_overseas=True)
    assert ovs > dom


def test_domestic_fees_are_truncated_to_won():
    """국내 수수료는 원 단위 절사. 해외는 소수점을 유지한다."""
    assert trading_cost.sell_fee(1_000_333) == int(1_000_333 * config.SELL_FEE_RATE)
    assert isinstance(trading_cost.sell_fee(1_000_333), int)
    assert isinstance(trading_cost.sell_fee(1_000_333, is_overseas=True), float)


# ─────────────────────────────────────────────
# 3. 슬리피지
# ─────────────────────────────────────────────

def test_slippage_always_moves_against_the_trader():
    buy = trading_cost.apply_slippage(10_000, 'buy')
    sell = trading_cost.apply_slippage(10_000, 'sell')
    assert buy > 10_000 > sell
    assert buy == pytest.approx(10_000 * (1 + config.SLIPPAGE_RATE))
    assert sell == pytest.approx(10_000 * (1 - config.SLIPPAGE_RATE))


def test_slippage_can_be_disabled(monkeypatch):
    monkeypatch.setattr(config, 'SLIPPAGE_RATE', 0.0)
    assert trading_cost.apply_slippage(10_000, 'buy') == 10_000


def test_paper_fill_price_slips_market_orders_only():
    """관찰모드 체결가: 지정가는 주문가 그대로, 시장가만 슬리피지를 얹는다.

    [2026-08-20 정정] 종전 이 테스트는 소스에 "apply_slippage" 문자열이 있는지만 봤고
     문구는 '지정가 그대로면 안 된다'였다. 그 전제가 틀렸다 — 지정가 주문가는 호출부가
     이미 현재가 × (1 ± SLIPPAGE_RATE)로 만든 값이라, 거기 또 얹으면 백테스트의 두 배를
     문다. 문자열 검사로는 이중 부과를 잡을 수 없어 실제 값을 본다.
    """
    from modules import paper_broker
    assert config.SLIPPAGE_RATE > 0
    assert paper_broker.fill_price(70_000, 'buy') == 70_000
    assert paper_broker.fill_price(70_000, 'sell') == 70_000
    assert paper_broker.fill_price(70_000, 'buy', market=True) > 70_000
    assert paper_broker.fill_price(70_000, 'sell', market=True) < 70_000


# ─────────────────────────────────────────────
# 4. 실거래 — 체결 확인 시 재계산
# ─────────────────────────────────────────────

def _origin(**over):
    t = {'type': 'sell(AUTO)', 'buy_price': 100_000.0}
    t.update(over)
    return t


def test_realized_profit_is_recomputed_from_the_actual_fill():
    """발주 시점 추정치가 아니라 실제 체결가로 다시 계산해야 한다."""
    from modules.auto_trade.conclusion import _recalc_realized
    amt, rate = _recalc_realized(_origin(), fill_price=110_000, fill_qty=10,
                                 is_overseas=False, fallback_amt=999_999, fallback_rate=99.9)
    expected, exp_rate = trading_cost.net_realized_profit(100_000, 110_000, 10)
    assert amt == int(expected)
    assert rate == pytest.approx(exp_rate)
    assert amt != 999_999, "주문 시점 추정치가 그대로 남았다"


def test_buy_fills_are_left_alone():
    """매수 체결에는 실현손익이 없다 — 건드리면 안 된다."""
    from modules.auto_trade.conclusion import _recalc_realized
    assert _recalc_realized(_origin(type='buy(AUTO)'), 110_000, 10, False, 0, 0.0) == (0, 0.0)


@pytest.mark.parametrize("origin", [None, {}, _origin(buy_price=0)])
def test_missing_buy_price_keeps_the_previous_value(origin):
    """매입가를 모르면 기존 값을 둔다 — 없는 정보를 추측해 덮어쓰지 않는다."""
    from modules.auto_trade.conclusion import _recalc_realized
    assert _recalc_realized(origin, 110_000, 10, False, 12_345, 1.23) == (12_345, 1.23)


def test_partial_fill_uses_the_filled_quantity():
    """부분 체결이면 체결된 수량만큼만 실현된다."""
    from modules.auto_trade.conclusion import _recalc_realized
    full, _ = _recalc_realized(_origin(), 110_000, 10, False, 0, 0.0)
    half, _ = _recalc_realized(_origin(), 110_000, 5, False, 0, 0.0)
    assert half == pytest.approx(full / 2, rel=0.01)


def test_recalc_never_raises_on_bad_input():
    """손익 재계산이 터져서 체결 기록 자체를 잃으면 안 된다."""
    from modules.auto_trade.conclusion import _recalc_realized
    assert _recalc_realized(_origin(buy_price="말도안됨"), "?", None,
                            False, 7, 0.7) == (7, 0.7)


# ─────────────────────────────────────────────
# 5. 요율의 산출 근거 (2026-08-10 실측)
# ─────────────────────────────────────────────
#
# 종전 값(매수 0.015% / 매도 0.23%)은 출처 없이 승계된 어림값이었다. 아래는 요율이
# **구성 요소의 합**임을 고정한다 — 누가 한 자리를 고치면 근거와 어긋나 바로 깨진다.

KIS_ONLINE_COMMISSION = 0.000140527   # 뱅키스 온라인 위탁수수료 0.0140527%
KRX_INSTITUTION_COST = 0.000036396    # 유관기관 제비용 0.0036396%
SECURITIES_TAX_2026 = 0.002           # 증권거래세 0.20% (2026-01-01 시행)


def test_domestic_buy_rate_is_commission_plus_institution_cost():
    """매수에는 세금이 없다 — 수수료 + 유관기관 제비용뿐이다."""
    assert config.BUY_FEE_RATE == pytest.approx(
        KIS_ONLINE_COMMISSION + KRX_INSTITUTION_COST, abs=1e-7)


def test_domestic_sell_rate_adds_the_transaction_tax():
    """매도 = 매수와 같은 수수료 + 증권거래세 0.20%."""
    assert config.SELL_FEE_RATE == pytest.approx(
        config.BUY_FEE_RATE + SECURITIES_TAX_2026, abs=1e-7)


def test_transaction_tax_is_the_2026_rate_not_the_2025_one():
    """2025년 0.15%로 되돌리지 말 것 — 금투세 폐지로 2026-01-01부터 0.20%로 환원됐다.

    '거래세는 계속 인하 중'이라는 인상이 남아 있어 되돌리기 쉬운 자리다.
    """
    implied_tax = config.SELL_FEE_RATE - config.BUY_FEE_RATE
    assert implied_tax == pytest.approx(0.002, abs=1e-7), \
        f"매도-매수 차이가 거래세 0.20%가 아니다: {implied_tax:.6%}"


def test_overseas_sell_includes_the_sec_fee():
    """미국 매도분 SEC Fee 0.00206%는 매수에 붙지 않는다."""
    assert config.OVERSEAS_SELL_FEE_RATE == pytest.approx(
        config.OVERSEAS_BUY_FEE_RATE + 0.0000206, abs=1e-9)
    assert config.OVERSEAS_BUY_FEE_RATE == pytest.approx(0.0025)
