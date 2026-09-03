"""사이징 패리티 — 백테스트 allocate_amount vs 실매매 RiskManager.allocate_budget.

[왜 고정하나] `portfolio_backtest.allocate_amount`의 독스트링은 "engine.RiskManager.
 allocate_budget과 동일한 3층 min 결합"이라고 선언한다. 그런데 그 동일성을 **대조하는
 테스트가 없었다.** 두 구현이 각자 따로 테스트될 뿐이었다(test_risk_manager.py /
 test_portfolio_backtest.py).

 이 저장소는 정확히 같은 형태의 결함을 이미 한 번 겪었다 — 진입 순위가 실매매
 (`trader.candidate_priority_key`)와 백테스트(기본 정렬)에서 달랐고, 양쪽 다 테스트가
 있었지만 **서로를 대조하지 않아** 오래 살아남았다. 재구현은 갈라진다. 선언이 아니라
 테스트가 붙들어야 한다.

[실측된 차이 — 2026-08-18]
 실매매는 리스크 스케일을 **기초비중과 리스크층 둘 다**에 곱한다:
     base   = 자산 × 비중 × scale
     risk층 = 자산 × (RISK_PER_TRADE × scale / 100) / (손절폭 × 갭버퍼)
 백테스트는 **기초비중에만** 곱한다(리스크층에는 scale이 없다).
 인위적 격자(손절·ATR을 독립으로 흔듦)에서는 504점 중 36점이 갈리고 최대 괴리 46.7%였다.
 그러나 **실데이터 615건 매수에서는 불일치 0건**이다(스케일<1이 97.1%였는데도).

 이유: 갈라지는 조합은 '넓은 손절 + 낮은 ATR'인데, 손절폭이 ATR×ATR_STOP_MULTIPLIER로
 **ATR에 묶여 있어** 그 조합이 물리적으로 발생하지 않는다. 리스크층은 4슬롯 구성에서
 한 번도 구속하지 않는다(config.SYSTEM_RISK_PER_TRADE 주석의 0-45-0 실측과 같은 결론).

[이 테스트가 지키는 것]
 ① 도달 가능한 영역(손절폭 = ATR×배수)에서는 두 구현이 **원 단위까지 같아야 한다.**
 ② 도달 불가 영역의 차이는 알고 남겨둔 것임을 명시한다 — 나중에 손절이 ATR과
    분리되거나(고정 %손절 도입), 슬롯 수·ATR 배수가 바뀌어 리스크층이 구속하기
    시작하면 ①이 먼저 깨져서 알려준다.
"""
import math
import os
import sys

import pytest
from unittest.mock import patch

sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import config  # noqa: E402
from modules.auto_trade import RiskManager  # noqa: E402
from modules.portfolio_backtest import allocate_amount  # noqa: E402

EQUITY = 10_000_000
PRICE = 50_000.0


class _Trader:
    """RiskManager가 요구하는 최소 인터페이스."""

    def __init__(self, initial_asset=EQUITY):
        self.initial_asset = initial_asset
        self.risk_scale_reason = ""
        self.risk_scale_reason_by_market = {}

    def log(self, msg):
        pass


def _live(scale, sl_rate, atr, cash=EQUITY, ratio=0.25):
    """실매매 경로. risk_scale은 current_risk_scale을 통해 주입한다."""
    rm = RiskManager(_Trader())
    with patch.object(RiskManager, "current_risk_scale", return_value=scale):
        return rm.allocate_budget(cash, ratio, stop_loss_rate=sl_rate,
                                  atr=atr, current_price=PRICE)


def _backtest(scale, sl_rate, atr, cash=EQUITY, ratio=0.25):
    """백테스트 경로. 호출부(run_portfolio)가 비중에 scale을 곱해 넘긴다."""
    return allocate_amount(EQUITY, cash, ratio * scale, sl_rate, atr, PRICE)


@pytest.fixture(autouse=True)
def _fixed_config():
    """두 구현이 같은 파라미터를 보게 고정한다(기본값 폴백 차이를 배제)."""
    keep = (config.TARGET_VOLATILITY, config.VOLATILITY_SCALING_MAX,
            config.VOLATILITY_SCALING_MIN, config.USE_VOLATILITY_TARGETING,
            config.SYSTEM_RISK_PER_TRADE)
    config.TARGET_VOLATILITY = 0.25
    config.VOLATILITY_SCALING_MAX = 2.0
    config.VOLATILITY_SCALING_MIN = 0.4
    config.USE_VOLATILITY_TARGETING = True
    config.SYSTEM_RISK_PER_TRADE = 4.0
    yield
    (config.TARGET_VOLATILITY, config.VOLATILITY_SCALING_MAX,
     config.VOLATILITY_SCALING_MIN, config.USE_VOLATILITY_TARGETING,
     config.SYSTEM_RISK_PER_TRADE) = keep


# 리스크 스케일이 실제로 취하는 값들(국면 배수 × 드로다운 배수의 곱).
SCALES = [1.0, 0.9, 0.8, 0.75, 0.6, 0.5, 0.45, 0.375, 0.25]
# ATR/가격 비율 — 실측 분포는 중앙 2.7~3.6%, 95%tile 4.1~10.9%
ATR_PCTS = [1.0, 1.5, 2.0, 2.7, 3.5, 5.0, 7.0, 10.0, 15.0]


@pytest.mark.parametrize("scale", SCALES)
@pytest.mark.parametrize("atr_pct", ATR_PCTS)
def test_도달가능_영역에서_두_구현은_원단위까지_같다(scale, atr_pct):
    """손절폭 = ATR × ATR_STOP_MULTIPLIER — 실제로 발생하는 조합만 본다."""
    mult = float(config.SELL_STRATEGY.get("ATR_STOP_MULTIPLIER", 2.0))
    atr = PRICE * atr_pct / 100.0
    sl_rate = -(atr * mult / PRICE) * 100.0     # 실매매·백테스트 공통 산식

    live = _live(scale, sl_rate, atr)
    back = _backtest(scale, sl_rate, atr)
    assert live == back, (
        f"사이징이 갈렸다 — 스케일 {scale} · ATR {atr_pct}% · 손절 {sl_rate:.2f}%: "
        f"실매매 {live:,} vs 백테스트 {back:,}")


@pytest.mark.parametrize("scale", [1.0, 0.5])
def test_손절캡에_걸린_넓은_손절도_같다(scale):
    """ATR×배수가 MAX_ATR_STOP_LOSS_RATE에 잘리는 경우 — 손절폭이 ATR과 살짝 어긋난다.

    캡은 진입일에 드물게(약 0.7%) 걸리지만, 걸리면 '손절은 -15%인데 ATR은 그보다
    넓다'는 상태가 되어 리스크층과 변동성층의 균형이 평시와 달라진다. 여기서도
    두 구현이 같아야 한다.
    """
    cap = abs(float(config.SELL_STRATEGY.get("MAX_ATR_STOP_LOSS_RATE", -15.0)))
    for atr_pct in (8.0, 10.0, 15.0):
        atr = PRICE * atr_pct / 100.0
        sl_rate = -cap                                  # 캡에 잘린 손절폭
        assert _live(scale, sl_rate, atr) == _backtest(scale, sl_rate, atr), \
            f"손절캡 구간에서 갈렸다 — 스케일 {scale} · ATR {atr_pct}%"


def test_현금이_배분액보다_적으면_현금이_상한이다():
    """두 구현 모두 마지막에 가용현금으로 자른다."""
    atr = PRICE * 3.0 / 100.0
    sl_rate = -6.0
    for cash in (100_000, 500_000, 1_500_000):
        assert _live(1.0, sl_rate, atr, cash=cash) == _backtest(1.0, sl_rate, atr, cash=cash), \
            f"현금 상한 처리가 갈렸다 — 현금 {cash:,}"


@pytest.mark.parametrize("scale", SCALES)
def test_리스크층은_최종액을_정하지_않는다(scale):
    """이 불변식이 위 패리티를 성립시킨다 — 깨지면 리스크층 스케일 차이가 결과로 나온다.

    실매매는 리스크층에도 risk_scale을 곱하고 백테스트는 곱하지 않는다. 그 차이가
    결과에 안 나오는 유일한 이유는 **1층·3층 중 더 작은 쪽이 항상 2층보다 작다**는
    것이다(config engine.allocate_budget 주석의 "3)이 상시 구속"과 같은 말).

    [주의 — 2층이 1층보다 작아지는 것 자체는 정상이다] ATR 7% 이상이면 리스크캡이
    기초비중 아래로 내려간다. 그래도 그 구간에서는 변동성층이 하한 0.4에 걸려 훨씬
    더 작아지므로 2층은 여전히 최종액을 정하지 못한다. 그래서 1층이 아니라
    **min(1층, 3층)** 과 비교해야 한다.
    """
    mult = float(config.SELL_STRATEGY.get("ATR_STOP_MULTIPLIER", 2.0))
    cap = abs(float(config.SELL_STRATEGY.get("MAX_ATR_STOP_LOSS_RATE", -15.0)))
    gap = max(1.0, float((getattr(config, "RISK_SCALING_PARAMS", {}) or {})
                         .get("GAP_RISK_BUFFER", 1.2)))
    ratio = 0.25
    for atr_pct in ATR_PCTS:
        sl = min(atr_pct * mult, cap) / 100.0            # 캡까지 반영한 손절 비율
        base = EQUITY * ratio * scale                     # 1층
        annual_vol = (atr_pct / 100.0) * math.sqrt(252)
        vs = max(config.VOLATILITY_SCALING_MIN,
                 min(config.VOLATILITY_SCALING_MAX, config.TARGET_VOLATILITY / annual_vol))
        vol_amt = min(base * vs, base)                    # 3층
        # 2층은 실매매식(스케일 적용)이 더 작다 — 더 빡빡한 쪽으로 검사한다.
        risk_cap = EQUITY * (config.SYSTEM_RISK_PER_TRADE * scale / 100.0) / (sl * gap)
        assert min(base, vol_amt) <= risk_cap, (
            f"리스크층이 최종액을 정하기 시작했다 — 스케일 {scale} · ATR {atr_pct}%: "
            f"min(기초 {base:,.0f}, 변동성 {vol_amt:,.0f}) > 리스크캡 {risk_cap:,.0f}. "
            f"이 경우 실매매(스케일 적용)와 백테스트(미적용)가 갈린다 — "
            f"portfolio_backtest.allocate_amount의 리스크층에도 스케일을 곱해야 한다.")


def test_알려진_차이는_도달_불가_영역에만_있다():
    """넓은 손절 + 낮은 ATR = 두 구현이 갈리는 조합. 이 조합이 실제로 불가능함을 못박는다.

    손절폭은 ATR×배수(캡 적용)로 정해지므로 '손절 -25%인데 ATR 1.5%'는 나올 수 없다.
    이 테스트는 차이를 '고치라'는 뜻이 아니라, 차이가 **왜 무해한지**를 코드에 남긴다.
    """
    atr = PRICE * 1.5 / 100.0
    sl_rate = -25.0                                     # ATR 1.5%로는 도달 불가
    live, back = _live(0.5, sl_rate, atr), _backtest(0.5, sl_rate, atr)
    assert live != back, (
        "도달 불가 영역에서 차이가 사라졌다 — 두 구현이 통합됐다면 이 테스트를 지우고 "
        "위 test_리스크층은_최종액을_정하지_않는다 의 설명도 함께 고칠 것.")
    assert live < back, "실매매가 더 보수적이어야 한다(리스크층에 스케일을 곱하므로)"

    mult = float(config.SELL_STRATEGY.get("ATR_STOP_MULTIPLIER", 2.0))
    cap = abs(float(config.SELL_STRATEGY.get("MAX_ATR_STOP_LOSS_RATE", -15.0)))
    reachable = min(1.5 * mult, cap)                    # ATR 1.5%에서 나올 수 있는 손절폭
    assert reachable < 25.0, (
        f"ATR 1.5%에서 손절 -25%가 도달 가능해졌다(최대 -{reachable:.1f}%). "
        f"이제 위 차이가 실제 결과를 바꾼다 — 백테스트 리스크층에도 스케일을 곱해야 한다.")


# ==========================================================
# 증액(피라미딩) 게이트 패리티
# ==========================================================
#
# [2026-08-19 한계 해소] 종전에는 백테스트의 증액 게이트가 `run_portfolio` 안에 인라인이라
#  조건을 여기에 옮겨 적어 대조했다. 옮겨 적은 것을 대조하면 '두 구현이 같다'를 증명하지
#  못한다 — 백테스트가 바뀌면 테스트는 통과한 채 거짓이 된다. 이제 판정식은
#  `engine.pyramid_gate_ok` 하나뿐이고, 실매매(analyze_pyramid)와 백테스트의 세 경로
#  (익일시가·분봉·종가)가 모두 그것을 부른다. 그래서 이 테스트는 '옮겨 적은 것끼리의
#  대조'가 아니라 **실매매 진입점과 SSOT가 같은 답을 내는가**를 본다.

_BUY_STATES = ("매수", "강매수")


def _backtest_pyramid_allowed(state, profit_rate, pyr_count, pyr_max, trigger):
    """백테스트가 실제로 부르는 그 함수(engine.pyramid_gate_ok)를 그대로 부른다.

    백테스트 쪽 래퍼(_pyramid_gate)는 '가격 → 수익률' 환산만 하므로, 판정 자체는
    이 호출과 같다. pyr_max <= 0(증액 OFF)은 run_portfolio가 블록 진입 전에 거른다.
    """
    from modules.auto_trade import engine

    if pyr_max <= 0:
        return False                                    # pyr_use = ... and pyr_max > 0
    return engine.pyramid_gate_ok(profit_rate, state, pyr_count, pyr_max, trigger)


@pytest.mark.parametrize("state", ["매수", "강매수", "상승", "대기", "관망", "매도", "역매수"])
@pytest.mark.parametrize("profit_rate", [-5.0, 0.0, 9.9, 10.0, 10.1, 25.0])
@pytest.mark.parametrize("pyr_count", [0, 1, 2, 3, 5])
def test_증액_게이트가_실매매와_같은_답을_낸다(state, profit_rate, pyr_count):
    from modules.auto_trade import DefaultStrategy

    trigger = float(config.ANALYSIS_THRESHOLDS.get("PYRAMIDING_PROFIT_TRIGGER", 10.0))
    pyr_max = int(config.ANALYSIS_THRESHOLDS.get("PYRAMIDING_MAX_COUNT", 3))

    live, _reason = DefaultStrategy().analyze_pyramid(
        profit_rate=profit_rate, state=state, score=7.0, pyramid_count=pyr_count)
    back = _backtest_pyramid_allowed(state, profit_rate, pyr_count, pyr_max, trigger)

    assert bool(live) == bool(back), (
        f"증액 판정이 갈렸다 — 상태 {state} · 수익 {profit_rate}% · {pyr_count}차: "
        f"실매매 {live} vs 백테스트 {back}")


def test_증액_OFF는_양쪽_모두_막는다():
    from modules.auto_trade import DefaultStrategy

    with patch.dict(config.ANALYSIS_THRESHOLDS, {"PYRAMIDING_USE": False}):
        live, _ = DefaultStrategy().analyze_pyramid(
            profit_rate=50.0, state="강매수", score=9.0, pyramid_count=0)
    assert live is False
    assert _backtest_pyramid_allowed("강매수", 50.0, 0, 0, 10.0) is False


# ==========================================================
# 사이징 3층 중 실제로 구속하는 층 (2026-09-01 원칙 감사)
#
# [실측 10년·사이징 421회] 변동성 99.0% · 기초비중 1.0% · **리스크한도 0회**.
# 배수 중앙 0.513(사분위 0.400~0.599), 하한(0.4)에 붙은 것은 25.7%뿐 — 즉 이 층은
# 일률 삭감이 아니라 실제로 변동성에 비례해 조절한다.
#
# 여기서 고정하는 것은 **확대가 봉인돼 있다**는 사실이다. VOLATILITY_SCALING_MAX(2.0)는
# `min(int(base_amt * scale), base_amt)` 때문에 사문이고, 실효 상한은 1.0 이다.
# 이걸 모르면 '저변동성 종목은 두 배까지 키운다'로 오독한다(화면도 그렇게 적고 있었다).
# ==========================================================

def test_the_volatility_layer_can_shrink_but_never_expand():
    """[핵심] 배수가 1을 넘어도 기초 비중을 못 넘는다."""
    import config
    from modules import portfolio_backtest as pb

    equity, ratio = 10_000_000, 0.25
    base = int(equity * ratio)
    # 아주 낮은 변동성 → scale = TARGET_VOLATILITY / annual_vol 이 1을 크게 넘는다
    got = pb.allocate_amount(equity, cash=equity, invest_ratio=ratio,
                             sl_rate=-7.0, atr=1.0, price=100_000.0)
    assert got <= base, f"확대 봉인이 풀렸다 — 기초 {base:,} 인데 {got:,}"

    # 그 반대: 높은 변동성이면 확실히 깎인다
    small = pb.allocate_amount(equity, cash=equity, invest_ratio=ratio,
                               sl_rate=-7.0, atr=8_000.0, price=100_000.0)
    assert small < base, "고변동성인데 안 깎였다"
    floor = getattr(config, "VOLATILITY_SCALING_MIN", 0.4)
    assert small >= int(base * floor) - 1, "하한 아래로 깎였다"


def test_the_risk_layer_is_not_what_binds():
    """리스크 한도를 크게 흔들어도 배분액이 안 변한다 — 이 층은 구속하지 않는다.

    'SYSTEM_RISK_PER_TRADE 를 낮추면 MDD 가 준다'는 옛 결론은 곱 결합 시절의 것이다
    (min 결합 fix 이후 실효 다이얼은 TARGET_VOLATILITY 로 옮겨갔다).
    """
    import config
    from modules import portfolio_backtest as pb

    kw = dict(equity=10_000_000, cash=10_000_000, invest_ratio=0.25,
              sl_rate=-7.0, atr=3_000.0, price=100_000.0)
    orig = getattr(config, "SYSTEM_RISK_PER_TRADE", 4.0)
    try:
        config.SYSTEM_RISK_PER_TRADE = 4.0
        a = pb.allocate_amount(**kw)
        config.SYSTEM_RISK_PER_TRADE = 3.0
        b = pb.allocate_amount(**kw)
    finally:
        config.SYSTEM_RISK_PER_TRADE = orig

    assert a == b, f"리스크 층이 구속하고 있다 (4% → {a:,} / 3% → {b:,})"
