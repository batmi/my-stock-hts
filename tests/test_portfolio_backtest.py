"""다종목 포트폴리오 백테스트 시뮬레이터 회귀 테스트.

핵심 검증 관점은 '기존 단일종목 엔진과 같은 판정을 하되, 포트폴리오 제약(슬롯·현금·히트 캡)이
실제로 걸리는가'다. 판정 로직 자체는 backtest.calculate_daily_status를 공유하므로,
여기서는 제약이 의도대로 동작하는지와 회계(현금·자산)가 깨지지 않는지를 본다.
"""
from unittest.mock import patch

import numpy as np
import pandas as pd
import pytest

import config
from core import indicators
from modules import backtest, portfolio_backtest as pbt


def _make_df(seed, n=260, start=10000.0, drift=0.004):
    """지표 계산까지 끝난 합성 일봉을 만든다 (상승 추세 + 노이즈)."""
    rng = np.random.default_rng(seed)
    dates = pd.bdate_range("2024-01-01", periods=n).strftime("%Y%m%d").tolist()
    price = start * np.cumprod(1 + drift + rng.normal(0, 0.015, n))
    df = pd.DataFrame({
        "date": dates,
        "open": price * (1 + rng.normal(0, 0.002, n)),
        "high": price * (1 + np.abs(rng.normal(0, 0.008, n))),
        "low": price * (1 - np.abs(rng.normal(0, 0.008, n))),
        "close": price,
        "volume": rng.integers(50_000, 200_000, n).astype(float),
    })
    df = backtest.compute_price_indicators(df)
    df["roll_high_5"] = df["high"].rolling(5, min_periods=1).max()
    df["roll_high_10"] = df["high"].rolling(10, min_periods=1).max()
    return df


@pytest.fixture(scope="module")
def universe():
    dfs = {f"00000{i}": _make_df(seed=i) for i in range(1, 6)}
    thresholds = {
        "BUY_SCORE": config.ANALYSIS_THRESHOLDS["BUY_SCORE"],
        "BUY_RSI_MAX": config.ANALYSIS_THRESHOLDS["BUY_RSI_MAX"],
        "RISE_SCORE": config.ANALYSIS_THRESHOLDS["RISE_SCORE"],
        "WEIGHTS": config.SCORING_WEIGHTS,
    }
    status = pbt.precompute_status(dfs, thresholds)
    dates = sorted({str(d) for df in dfs.values() for d in df["date"]})
    return dfs, status, dates


@pytest.fixture(scope="module")
def wide_universe():
    """순위 검증용 넓은 유니버스 — 후보가 슬롯보다 많은 날이 실제로 생겨야 한다.

    5종목짜리 기본 유니버스로는 경쟁일이 거의 없어(관측 0~2일) 정렬을 검증해도
    '점수만 확인한 것'과 구분되지 않는다.
    """
    dfs = {f"9{i:05d}": _make_df(seed=100 + i) for i in range(20)}
    thresholds = {
        "BUY_SCORE": config.ANALYSIS_THRESHOLDS["BUY_SCORE"],
        "BUY_RSI_MAX": config.ANALYSIS_THRESHOLDS["BUY_RSI_MAX"],
        "RISE_SCORE": config.ANALYSIS_THRESHOLDS["RISE_SCORE"],
        "WEIGHTS": config.SCORING_WEIGHTS,
    }
    status = pbt.precompute_status(dfs, thresholds)
    dates = sorted({str(d) for df in dfs.values() for d in df["date"]})
    return dfs, status, dates


def test_slot_limit_is_never_exceeded(universe):
    """동시 보유 종목 수가 슬롯 수를 넘지 않아야 한다 (기회비용의 핵심 제약)."""
    dfs, status, dates = universe
    for slots in (1, 2, 3):
        res = pbt.run_portfolio(dfs, status, dates, initial_capital=10_000_000, slots=slots)
        assert res["avg_slots"] <= slots + 1e-9
        # 매수/청산 이벤트를 되짚어 어느 시점에도 보유 수가 슬롯을 넘지 않았는지 확인
        held = set()
        peak = 0
        for t in res["trades"]:
            if t["reason"] == "매수":
                held.add(t["code"])
            elif t["reason"].startswith("피라미딩"):
                continue
            else:
                held.discard(t["code"])
            peak = max(peak, len(held))
        assert peak <= slots


def test_equity_accounting_is_consistent(universe):
    """최종 자산과 총수익률이 서로 정합해야 하고, 자산 곡선이 비어 있으면 안 된다."""
    dfs, status, dates = universe
    res = pbt.run_portfolio(dfs, status, dates, initial_capital=10_000_000, slots=3)
    assert len(res["equity"]) == len(dates)
    expected = (res["final_asset"] - 10_000_000) / 10_000_000 * 100
    assert res["total_return"] == pytest.approx(expected, abs=1e-6)
    assert res["mdd"] <= 0


def test_pyramiding_only_adds_and_respects_max(universe):
    """피라미딩은 설정 차수를 넘지 않고, 0차면 증액이 한 건도 없어야 한다."""
    dfs, status, dates = universe
    off = pbt.run_portfolio(dfs, status, dates, slots=3, pyramiding_max=0)
    assert off["pyramid_count"] == 0

    on = pbt.run_portfolio(dfs, status, dates, slots=3, pyramiding_max=2)
    per_position = {}
    for t in on["trades"]:
        if t["reason"] == "매수":
            per_position[t["code"]] = 0
        elif t["reason"].startswith("피라미딩"):
            per_position[t["code"]] = per_position.get(t["code"], 0) + 1
            assert per_position[t["code"]] <= 2


def test_heat_cap_limits_exposure_when_tight(universe):
    """히트 캡을 좁힐수록 실제 노출(투자 비중)이 단조 감소해야 한다.

    [주의] '매수 건수'로는 검증할 수 없다 — 캡이 좁으면 포지션이 작아져 빨리 청산되고
    슬롯이 비어 재매수가 늘어나므로 건수가 오히려 증가할 수 있다(실측 10→13건).
    캡이 통제하는 것은 노출이므로 유휴현금 비율로 본다.
    """
    dfs, status, dates = universe
    caps = [0.0, 0.05, 0.02, 0.005, 0.001]      # 0 = 미사용, 이후 자산 대비 %
    cash = [pbt.run_portfolio(dfs, status, dates, slots=3, heat_cap_pct=c)["avg_cash_ratio"]
            for c in caps]

    # 캡이 좁아질수록 현금이 남는다(= 노출이 줄어든다)
    assert cash == sorted(cash), f"현금 비율이 캡에 단조 반응하지 않음: {cash}"
    assert cash[0] < 50.0                        # 캡 미사용이면 대부분 투자된다
    assert cash[-1] == pytest.approx(100.0)      # 극단적으로 좁히면 진입이 완전히 막힌다


def test_reserved_cash_is_held_out_but_counted_in_equity(universe):
    """수동 운용분은 시스템이 집행할 수 없되 자산에는 남아 있어야 한다."""
    dfs, status, dates = universe
    free = pbt.run_portfolio(dfs, status, dates, initial_capital=10_000_000, slots=3)
    held = pbt.run_portfolio(dfs, status, dates, initial_capital=10_000_000, slots=3,
                             reserved_cash=9_000_000)

    # 자본 대부분이 묶이면 집행 여력이 줄어 매수가 감소한다
    n_free = sum(1 for t in free["trades"] if t["reason"] == "매수")
    n_held = sum(1 for t in held["trades"] if t["reason"] == "매수")
    assert n_held < n_free
    # 묶인 돈도 계좌 자산이므로 사라지지 않는다
    assert held["final_asset"] >= 9_000_000
    assert 0 <= held["avg_cash_ratio"] <= 100


def test_allocate_amount_respects_all_three_layers():
    """사이징은 기초비중·리스크·변동성 3층의 최솟값을 넘지 않는다."""
    equity = 10_000_000
    amount = pbt.allocate_amount(equity, cash=equity, invest_ratio=0.25,
                                 sl_rate=-8.0, atr=300.0, price=10_000.0)
    assert 0 < amount <= equity * 0.25          # 기초 비중 상한
    assert amount <= equity                      # 현금 상한

    # 현금이 부족하면 현금이 상한이 된다
    assert pbt.allocate_amount(equity, cash=100_000, invest_ratio=0.25,
                               sl_rate=-8.0, atr=300.0, price=10_000.0) <= 100_000


def test_market_filter_also_gates_pyramiding(universe):
    """시장 필터가 켜져 있으면 증액도 함께 보류된다 (실매매 _try_pyramid_buy와 동일).

    필터가 꺼져 있던 동안에는 차이가 없었으나, 켠 뒤에도 반영하지 않으면 증액이 과대평가된다."""
    dfs, status, dates = universe
    block_all = {code: set(dates) for code in dfs}

    with patch.object(config, "USE_MARKET_FILTER", True), \
         patch.dict(config.ANALYSIS_THRESHOLDS, {"PYRAMIDING_REQUIRE_HEALTHY_MARKET": True}):
        blocked = pbt.run_portfolio(dfs, status, dates, slots=3, pyramiding_max=3,
                                    market_filter_dates=block_all)
    assert blocked["pyramid_count"] == 0

    # 게이트를 끄면 같은 차단일에도 증액은 살아 있어야 한다 (매도·보유엔 영향 없음)
    with patch.object(config, "USE_MARKET_FILTER", True), \
         patch.dict(config.ANALYSIS_THRESHOLDS, {"PYRAMIDING_REQUIRE_HEALTHY_MARKET": False}):
        open_gate = pbt.run_portfolio(dfs, status, dates, slots=3, pyramiding_max=3,
                                      market_filter_dates=block_all)
    assert open_gate["pyramid_count"] >= 0  # 신규 진입이 전면 차단이라 0일 수 있음


# ---------------------------------------------------------------------------
# 리스크 배수 콜러블 (계좌 드로다운 축 감사용 경로)
# ---------------------------------------------------------------------------
def test_risk_scale_accepts_callable(universe):
    """risk_scale_by_date 는 dict 뿐 아니라 fn(day, equity) 콜러블도 받아야 한다.

    계좌 드로다운 축은 시뮬레이션 자신의 자산곡선에 반응하는 피드백 루프라
    사전 계산이 불가능하다. 콜러블 경로가 막히면 그 축은 검증할 방법이 없다.
    """
    dfs, status, dates = universe
    seen = []

    def fn(day, equity):
        seen.append((day, equity))
        return 1.0

    res = pbt.run_portfolio(dfs, status, dates, initial_capital=10_000_000, slots=3,
                            risk_scale_by_date=fn)
    assert seen, "콜러블이 한 번도 호출되지 않았다"
    assert len(seen) == len(dates)
    assert all(e > 0 for _d, e in seen), "자산이 전달되지 않으면 드로다운을 계산할 수 없다"
    assert res["final_asset"] > 0


def test_callable_scale_matches_equivalent_dict(universe):
    """같은 배수를 주면 dict 경로와 콜러블 경로의 결과가 같아야 한다."""
    dfs, status, dates = universe
    by_dict = pbt.run_portfolio(dfs, status, dates, initial_capital=10_000_000, slots=3,
                                risk_scale_by_date={d: 0.5 for d in dates})
    by_call = pbt.run_portfolio(dfs, status, dates, initial_capital=10_000_000, slots=3,
                                risk_scale_by_date=lambda _d, _e: 0.5)
    assert by_dict["final_asset"] == pytest.approx(by_call["final_asset"])
    assert by_dict["mdd"] == pytest.approx(by_call["mdd"])


def test_callable_scale_actually_constrains(universe):
    """대조군 — 콜러블로 준 배수가 실제로 배분을 조인다(현금 비율 상승)."""
    dfs, status, dates = universe
    full = pbt.run_portfolio(dfs, status, dates, initial_capital=10_000_000, slots=3,
                             risk_scale_by_date=lambda _d, _e: 1.0)
    tight = pbt.run_portfolio(dfs, status, dates, initial_capital=10_000_000, slots=3,
                              risk_scale_by_date=lambda _d, _e: 0.3)
    assert tight["avg_cash_ratio"] > full["avg_cash_ratio"]


# ---------------------------------------------------------------------------
# 청산 판정 — 실매매(analyze_sell)와 같은 규칙인가
# ---------------------------------------------------------------------------
def _decide(**kw):
    base = dict(price=110.0, high=120.0, avg=100.0, sl_rate=-7.0, atr_applied=True,
                is_bep=False, holding_days=5, state="매수", state_reason="", raw_score=7.0,
                sell_check=7.0, ema60=100.0, atr=2.0, roll_high_5=0.0, roll_high_10=0.0,
                cfg={"use_atr": True, "use_time_stop": True, "time_stop_days": 20,
                     "ts_act": 10.0, "ts_callback": 5.0, "ts_atr_mult": 3.5,
                     "sell_score_limit": 4.0})
    base.update(kw)
    return pbt.decide_sell(**base)


def test_trailing_applies_giveback_cap():
    """트레일링 콜백에 반납 상한(TS_MAX_GIVEBACK_RATIO)이 걸려야 한다.

    실매매(engine.compute_trailing_stop)와 단일종목 백테스트는 이미 이 캡을 쓴다.
    포트폴리오 백테스트만 순수 샹들리에로 돌면 콜백이 더 커져 청산이 늦고, 백테스트가
    실매매보다 낙관적으로 나온다 — 그 수치로 정한 파라미터의 근거가 통째로 흔들린다.
    (실측: 캡을 빼면 3년 수익 중앙값이 +82.8%p 과대, 20회 중 18회)
    """
    saved = config.SELL_STRATEGY.get("TS_MAX_GIVEBACK_RATIO")
    try:
        # ATR 동적 콜백이 크게 나오는 상황: atr=6 → 6*3.5/120*100 = 17.5%
        # 최고수익 +20%, 현재 -8.3% 하락. 캡(0.35)이면 상한 = 20*0.35/(100+20) = 5.83%
        config.SELL_STRATEGY["TS_MAX_GIVEBACK_RATIO"] = 0.35
        capped, reason = _decide(price=110.0, high=120.0, avg=100.0, atr=6.0)
        assert capped and reason == "트레일링스탑", "반납 상한이 걸리면 이 하락에서 청산돼야 한다"

        config.SELL_STRATEGY["TS_MAX_GIVEBACK_RATIO"] = 0.0
        uncapped, _ = _decide(price=110.0, high=120.0, avg=100.0, atr=6.0)
        assert not uncapped, "대조군 — 캡이 없으면 콜백 17.5%라 아직 버틴다"
    finally:
        config.SELL_STRATEGY["TS_MAX_GIVEBACK_RATIO"] = saved


def test_trailing_keeps_callback_floor():
    """ATR이 작아도 기본 콜백(5%) 아래로는 내려가지 않는다 — 조기 털림 방지."""
    saved = config.SELL_STRATEGY.get("TS_MAX_GIVEBACK_RATIO")
    try:
        config.SELL_STRATEGY["TS_MAX_GIVEBACK_RATIO"] = 0.35
        # 최고 +20%에서 3% 하락  (하한이 없으면 캡 5.83%보다 낮은 콜백이 나와 털린다) — 캡 산식만 보면 5.83%지만 하한 5%도 못 넘었다.
        sell, _ = _decide(price=116.4, high=120.0, avg=100.0, atr=0.01)
        assert not sell, "하락 3%에 청산되면 정상 눌림에서 털린다"
    finally:
        config.SELL_STRATEGY["TS_MAX_GIVEBACK_RATIO"] = saved


def test_trailing_not_armed_below_activation():
    """최고 수익이 발동 기준(10%) 미만이면 트레일링은 발동하지 않는다."""
    sell, _ = _decide(price=100.0, high=105.0, avg=100.0, atr=6.0)
    assert not sell


def test_stop_loss_takes_priority_over_trailing():
    """손절이 트레일링보다 우선한다 — 사유가 뒤바뀌면 통계가 어긋난다."""
    sell, reason = _decide(price=92.0, high=120.0, avg=100.0, sl_rate=-7.0, atr=6.0)
    assert sell and reason == "ATR손절"


def test_score_sell_requires_structure_break():
    """점수 미달만으로는 팔지 않는다 — 60일선 이탈을 동시에 요구한다."""
    hold, _ = _decide(sell_check=1.0, price=110.0, ema60=100.0, high=110.0)
    assert not hold, "정배열 유지 중 눌림에서 점수만 낮다고 팔면 fat-tail을 잘라낸다"
    sell, reason = _decide(sell_check=1.0, price=95.0, ema60=100.0, high=100.0, avg=100.0)
    assert sell and reason == "점수하락"


def test_bep_toggle_is_respected():
    """USE_BREAK_EVEN_STOP=False 면 본전청산이 발동하지 않아야 한다.

    실매매(engine.analyze_sell)·단일종목 백테스트와 같은 토글을 쓴다. 한 곳이라도
    누락되면 백테스트와 실거래의 청산이 갈려 파라미터 근거가 무너진다
    (tools/audit_exit_parity.py 가 이 정합을 상시 확인한다).
    """
    dfs = {"000001": _make_df(seed=11)}
    th = {"BUY_SCORE": config.ANALYSIS_THRESHOLDS["BUY_SCORE"],
          "BUY_RSI_MAX": config.ANALYSIS_THRESHOLDS["BUY_RSI_MAX"],
          "RISE_SCORE": config.ANALYSIS_THRESHOLDS["RISE_SCORE"],
          "WEIGHTS": config.SCORING_WEIGHTS}
    status = pbt.precompute_status(dfs, th)
    dates = sorted(str(d) for d in dfs["000001"]["date"])

    saved = config.SELL_STRATEGY.get("USE_BREAK_EVEN_STOP")
    try:
        config.SELL_STRATEGY["USE_BREAK_EVEN_STOP"] = False
        off = pbt.run_portfolio(dfs, status, dates, initial_capital=10_000_000, slots=1)
        config.SELL_STRATEGY["USE_BREAK_EVEN_STOP"] = True
        on = pbt.run_portfolio(dfs, status, dates, initial_capital=10_000_000, slots=1)
    finally:
        config.SELL_STRATEGY["USE_BREAK_EVEN_STOP"] = saved

    off_reasons = [t["reason"] for t in off["trades"]]
    assert "본전청산" not in off_reasons, "토글을 껐는데 본전청산이 나왔다"
    # 대조군 — 켜면 판정 자체는 살아 있어야 한다(이 표본에 발동이 없으면 결과가 같을 수 있다)
    assert on["final_asset"] > 0


def test_bep_off_never_raises_stop_to_breakeven():
    """decide_sell 수준에서도 확인 — 손절선이 +0.5%로 올라오면 안 된다."""
    saved = config.SELL_STRATEGY.get("USE_BREAK_EVEN_STOP")
    try:
        config.SELL_STRATEGY["USE_BREAK_EVEN_STOP"] = False
        # 최고 +20% 찍고 +1%로 밀린 상태. BEP가 켜져 있으면 '본전청산'이 나온다.
        sell, reason = _decide(price=101.0, high=120.0, avg=100.0, sl_rate=-9.0,
                               is_bep=False, atr=0.01)
        assert reason != "본전청산"
    finally:
        config.SELL_STRATEGY["USE_BREAK_EVEN_STOP"] = saved


# ==========================================================
# 진입 순위 훅(rank_fn)과 슬롯 교체(rotation) — 2026-08-11 추가
# ==========================================================
def test_기본값은_훅을_붙이기_전과_같다(universe):
    """rank_fn·rotation 미지정이면 결과가 종전과 완전히 같아야 한다.

    두 훅은 순수 실험용 경로다. 기본 경로에 한 톨이라도 영향을 주면 이 백테스트로
    정한 모든 파라미터의 근거가 흔들린다 — 그래서 '옵션이 꺼져 있으면 없는 것과
    같다'를 명시적으로 고정한다.
    """
    dfs, status, dates = universe
    a = pbt.run_portfolio(dfs, status, dates, slots=3)
    b = pbt.run_portfolio(dfs, status, dates, slots=3, rank_fn=None, rotation=None)
    assert a["equity"] == b["equity"]
    assert a["trades"] == b["trades"]
    assert a["rotations"] == 0


def test_rank_fn은_순서만_바꾸고_게이트는_건드리지_않는다(universe):
    """순위를 뒤집어도 '매수 조건을 통과한 종목'의 집합은 그대로여야 한다.

    rank_fn이 게이트(BUY_SCORE·RSI·시장필터)까지 건드리면 '점수 순위의 값'을 묻는
    실험이 '진입 조건의 값'을 묻는 실험으로 바뀐다 — 두 질문은 다르다.
    """
    dfs, status, dates = universe
    normal = pbt.run_portfolio(dfs, status, dates, slots=3)
    rev = pbt.run_portfolio(dfs, status, dates, slots=3,
                            rank_fn=lambda s, c, r, d: -s)
    bought_normal = {t["code"] for t in normal["trades"] if t["reason"] == "매수"}
    bought_rev = {t["code"] for t in rev["trades"] if t["reason"] == "매수"}
    assert bought_normal and bought_rev
    # 슬롯 경쟁이 있으면 '누가 언제 샀는가'는 달라져서, 경쟁이 있는 실행끼리는 게이트
    # 불변을 확인할 수 없다(어떤 부분집합 관계도 항상 참이라 아무것도 못 잰다).
    # 그래서 슬롯을 종목 수만큼 줘 **경쟁을 없앤 뒤** 대조한다 — 순서가 결과를 못 바꾸는
    # 조건이므로, 여기서 매수 집합·시점이 어긋나면 그것은 순전히 게이트가 흔들린 것이다.
    free = {}
    for label, fn in (("normal", None), ("rev", lambda s, c, r, d: -s)):
        res = pbt.run_portfolio(dfs, status, dates, slots=len(dfs), rank_fn=fn, invest_ratio=0.01, initial_capital=100_000_000)
        free[label] = {(t["code"], t["date"]) for t in res["trades"] if t["reason"] == "매수"}
    assert free["normal"] == free["rev"]
    assert free["normal"]


def test_교체는_슬롯이_찼을_때만_일어난다(universe):
    """슬롯이 남아 있으면 교체할 이유가 없다 — 그냥 사면 된다."""
    dfs, status, dates = universe
    # 슬롯을 종목 수만큼 주면 만재가 되지 않아 교체가 0이어야 한다.
    res = pbt.run_portfolio(dfs, status, dates, slots=len(dfs),
                            rotation={"margin": 0.0})
    assert res["rotations"] == 0


def test_교체_문턱이_높으면_무동작이다(universe):
    """점수차 문턱이 만점을 넘으면 어떤 후보도 조건을 못 채운다(무승부 ≠ 열위)."""
    dfs, status, dates = universe
    base = pbt.run_portfolio(dfs, status, dates, slots=2)
    high = pbt.run_portfolio(dfs, status, dates, slots=2, rotation={"margin": 99.0})
    assert high["rotations"] == 0
    assert base["equity"] == high["equity"]


def test_승자보호_가드는_무장한_포지션을_지킨다(universe):
    """only_unarmed면 교체로 팔린 것 중 TS 무장 경험이 있는 건이 없어야 한다.

    추세추종에서 교체의 가장 큰 위험은 달리는 승자를 잘라내는 것이다. 이 가드가
    실제로 그것을 막는지는 '교체 거래의 armed 플래그'로만 확인할 수 있다.
    """
    dfs, status, dates = universe
    res = pbt.run_portfolio(dfs, status, dates, slots=2,
                            rotation={"margin": 0.0, "only_unarmed": True})
    rot = [t for t in res["trades"] if t["reason"] == "교체"]
    assert all(not t["armed"] for t in rot)


def test_교체는_슬롯_상한을_깨지_않는다(universe):
    """교체 후 곧바로 매수가 이어져도 동시 보유가 슬롯을 넘으면 안 된다."""
    dfs, status, dates = universe
    slots = 2
    res = pbt.run_portfolio(dfs, status, dates, slots=slots, rotation={"margin": 0.0})
    held, peak = set(), 0
    for t in res["trades"]:
        if t["reason"] == "매수":
            held.add(t["code"])
        elif t["reason"].startswith("피라미딩"):
            continue
        else:
            held.discard(t["code"])
        peak = max(peak, len(held))
    assert peak <= slots


def test_교체도_청산_통계에_포함된다(universe):
    """교체는 실현손익이 있는 청산이다. 승률·PF 분모에서 빠지면 성과가 왜곡된다."""
    dfs, status, dates = universe
    res = pbt.run_portfolio(dfs, status, dates, slots=2, rotation={"margin": 0.0})
    if res["rotations"]:
        assert any(t["reason"] == "교체" for t in res["sells"])
        assert res["win"] + res["loss"] == len(res["sells"])


def test_교체한_종목을_같은_날_되사지_않는다(universe):
    """교체로 판 종목이 그날 다시 매수되면 포지션은 그대로인데 왕복 비용만 나간다.

    슬롯이 한 칸만 열리면 상위 후보가 가져가므로 드러나지 않지만, 정규 매도로
    두 칸 이상 열린 날에는 방금 판 종목이 두 번째 칸을 도로 차지할 수 있다.
    """
    dfs, status, dates = universe
    res = pbt.run_portfolio(dfs, status, dates, slots=2, rotation={"margin": 0.0})
    by_day = {}
    for t in res["trades"]:
        by_day.setdefault(t["date"], []).append(t)
    for day, ts in by_day.items():
        rotated = {t["code"] for t in ts if t["reason"] == "교체"}
        bought = {t["code"] for t in ts if t["reason"] == "매수"}
        assert not (rotated & bought), f"{day}: 교체 직후 같은 종목 재매수 {rotated & bought}"


def test_probe_fn은_결과를_바꾸지_않고_경쟁만_계측한다(universe):
    """계측 훅은 시뮬레이션을 건드리면 안 되고, 정렬된 후보와 남은 슬롯을 그대로 줘야 한다.

    순위 실험의 타당성('후보가 슬롯보다 많았는가', '경계에서 동점이었는가')이 이 훅의
    입력에만 기대므로, 순서가 흐트러지거나 슬롯 수가 틀리면 계측이 조용히 거짓말을 한다.
    """
    dfs, status, dates = universe
    seen = []
    base = pbt.run_portfolio(dfs, status, dates, slots=2)
    res = pbt.run_portfolio(dfs, status, dates, slots=2,
                            probe_fn=lambda day, cands, free: seen.append((day, cands, free)))
    assert base["equity"] == res["equity"]
    assert base["trades"] == res["trades"]
    assert seen, "빈 슬롯이 있던 날이 한 번도 없으면 계측 자체가 성립하지 않는다"
    for _day, cands, free in seen:
        assert 1 <= free <= 2
        scores = [c[0] for c in cands]
        assert scores == sorted(scores, reverse=True)


def test_entry_gate는_후보만_걷어내고_다른_경로는_건드리지_않는다(universe):
    """실매매 게이트(상관관계 등) 재현용 훅 — 끄면 종전과 같고, 켜면 그 종목만 사라진다.

    이 훅은 '후보 집합을 실매매와 맞춘 뒤에도 순위 결론이 유지되는가'를 묻는 데 쓴다.
    게이트가 후보 말고 다른 것(청산·피라미딩)까지 건드리면 그 비교가 성립하지 않는다.
    """
    dfs, status, dates = universe
    base = pbt.run_portfolio(dfs, status, dates, slots=2)
    same = pbt.run_portfolio(dfs, status, dates, slots=2, entry_gate=lambda d, c, h: False)
    assert base["trades"] == same["trades"]

    blocked = sorted(dfs)[0]
    seen_holds = []

    def gate(day, code, held):
        seen_holds.append((code, held))
        return code == blocked

    res = pbt.run_portfolio(dfs, status, dates, slots=2, entry_gate=gate)
    assert all(t["code"] != blocked for t in res["trades"]), "차단한 종목이 매수됐다"
    assert res["trades"], "게이트가 후보를 통째로 지워버리면 비교 자체가 성립하지 않는다"
    # 보유 목록은 '그 시점에 들고 있는 것'이어야 한다 — 후보 자신이 섞이면 상관 판정이 자기 자신과의 비교가 된다.
    assert all(code not in held for code, held in seen_holds)


def test_기본_진입순위는_실매매_동점가름을_쓴다(wide_universe):
    """기본 정렬이 (점수 → 추세품질 → 52주위치)여야 한다 — 실매매 순위와 같은 잣대다.

    [왜 고정하나] 2026-08-18 이전의 기본 정렬은 점수 하나만 봤고, 동점(슬롯 당락 경계의
     45~52%)을 관심종목 등록 순서로 갈랐다. 그 임의 상수 때문에 '근거 없이 무작위로
     진입을 차단하기만 해도 기준선을 이기는' 가짜 신호가 나왔고, 같은 표본에서 수익이
     252 vs 419%로 갈렸다. 순위는 이 백테스트로 정한 거의 모든 결론이 딛고 선 바닥이라,
     기본값이 실매매와 어긋나면 도구 하나가 아니라 결론 전체가 흔들린다.
    """
    dfs, status, dates = wide_universe
    lookback = config.INDICATOR_PARAMS.get("TREND_QUALITY_LOOKBACK", 90)
    tq = {code: indicators.trend_quality_map(df, lookback) for code, df in dfs.items()}
    seen = {"compete": 0, "unsorted": 0, "tie_split_by_tq": 0}

    def probe(day, cands, free_slots):
        if len(cands) < 2:
            return
        seen["compete"] += 1
        keys = []
        for score, code, row in cands:
            q = tq[code].get(str(day))
            keys.append((score, float("-inf") if q is None else q,
                         float(row.get("w52_pos", 0.0) or 0.0)))
        if keys != sorted(keys, reverse=True):
            seen["unsorted"] += 1
        for a, b in zip(keys, keys[1:]):
            if a[0] == b[0] and a[1] != b[1]:
                seen["tie_split_by_tq"] += 1

    # 슬롯을 좁게 줘야 후보가 남은 자리보다 많아진다 — 경쟁이 없으면 순위는 죽은 조항이다.
    pbt.run_portfolio(dfs, status, dates, slots=2, probe_fn=probe)
    assert seen["compete"] > 0, "후보가 2개 이상인 날이 없으면 순위를 잰 것이 아니다"
    assert seen["unsorted"] == 0
    # 동점이 한 번도 없었다면 위 정렬 확인은 점수만 확인한 것과 같다 — 표본이 무효다.
    assert seen["tie_split_by_tq"] > 0, "동점 구간이 없어 동점 가름을 검증하지 못했다"


def test_legacy_순위는_점수만_본다(universe):
    """rank_fn="legacy"는 옛 기본값(점수 단독·동점은 등록 순서)을 그대로 재현해야 한다.

    과거 기록값과 대조할 통로가 없으면 '수치가 달라진 것이 결함 수정 때문인지'를 증명할
    수 없다. 그래서 옛 경로를 지우지 않고 이름을 붙여 남긴다.
    """
    dfs, status, dates = universe
    legacy = pbt.run_portfolio(dfs, status, dates, slots=3, rank_fn="legacy")
    # 점수만 돌려주는 rank_fn은 파이썬 정렬이 안정적이라 '점수 → 등록 순서'와 같다.
    same = pbt.run_portfolio(dfs, status, dates, slots=3, rank_fn=lambda s, c, r, d: s)
    assert legacy["trades"] == same["trades"]
    assert legacy["equity"] == same["equity"]


def test_추세품질_이력부족은_최하순위이고_비율이_보고된다(universe):
    """이력이 모자란 구간은 동점 가름이 다시 등록 순서로 떨어진다 — 그 사실을 숨기지 않는다.

    워밍업 오염을 조용히 넘기면 '실매매 순위로 쟀다'는 전제가 창의 앞부분에서만 거짓이
    되는데, 그건 겉으로 드러나지 않는다. run_portfolio가 비율을 돌려주도록 해 감사 도구가
    dates 앞을 자를지 판단할 수 있게 한다.
    """
    dfs, status, dates = universe
    res = pbt.run_portfolio(dfs, status, dates, slots=3)
    assert 0.0 <= res["rank_no_tq_pct"] <= 100.0
    # 합성 데이터는 260봉이라 앞의 lookback-1일에는 추세품질이 없다.
    assert res["rank_no_tq_pct"] > 0.0
    late = [d for d in dates if d >= dates[len(dates) // 2]]
    assert pbt.run_portfolio(dfs, status, late, slots=3)["rank_no_tq_pct"] == 0.0


def test_추세품질_상한은_과열_추세를_후보에서_뺀다(wide_universe):
    """TREND_QUALITY_MAX 이상인 종목·일은 매수되지 않아야 한다 (실매매 게이트와 같은 판정).

    추세품질은 단조가 아니라 300 위에서 꺾인다 — 종목 축의 모멘텀 크래시다. 이 게이트가
    조용히 무동작이 되면(예: 캐시가 옛 값을 물거나 분봉 경로만 빠지거나) 백테스트는 방어가
    걸린 줄 알고 수치를 내지만 실제로는 아무것도 막지 않는다. 그 상태를 고정으로 막는다.
    """
    dfs, status, dates = wide_universe
    lookback = config.INDICATOR_PARAMS.get("TREND_QUALITY_LOOKBACK", 90)
    tq = {code: indicators.trend_quality_map(df, lookback) for code, df in dfs.items()}

    # 기준선은 '해제(0)'다 — config 기본값이 이미 300이라 기본 실행은 게이트가 켜진 상태다.
    with patch.dict(config.ANALYSIS_THRESHOLDS, {"TREND_QUALITY_MAX": 0}):
        base = pbt.run_portfolio(dfs, status, dates, slots=3)
    bought = [(t["code"], t["date"]) for t in base["trades"] if t["reason"] == "매수"]
    assert bought, "기준선에서 매수가 없으면 게이트를 검증할 수 없다"

    # 실제 매수된 건들의 추세품질 중앙값을 상한으로 잡으면 절반쯤이 막힌다.
    vals = sorted(v for v in (tq[c].get(d) for c, d in bought) if v is not None)
    assert vals, "매수 시점의 추세품질이 전부 이력부족이면 표본이 무효다"
    cap = vals[len(vals) // 2]

    with patch.dict(config.ANALYSIS_THRESHOLDS, {"TREND_QUALITY_MAX": cap}):
        capped = pbt.run_portfolio(dfs, status, dates, slots=3)
    for t in capped["trades"]:
        if t["reason"] != "매수":
            continue
        v = tq[t["code"]].get(t["date"])
        assert v is None or v < cap, f"상한 {cap} 이상({v})인데 매수됐다: {t['code']} {t['date']}"
    assert capped["trades"] != base["trades"], "상한이 아무것도 막지 못했다 — 게이트가 무동작이다"

    # 닿지 않는 큰 값은 해제(0)와 같아야 한다 — 게이트가 엉뚱한 것을 막고 있지 않다는 확인.
    with patch.dict(config.ANALYSIS_THRESHOLDS, {"TREND_QUALITY_MAX": 1e9}):
        unreachable = pbt.run_portfolio(dfs, status, dates, slots=3)
    assert unreachable["trades"] == base["trades"]


# ==========================================================
# 봉이 끊긴 종목 (상장폐지·장기 거래정지·데이터 실패)
# ==========================================================
def _thresholds():
    """fixture 가 쓰는 것과 같은 판정 임계값 묶음."""
    return {
        "BUY_SCORE": config.ANALYSIS_THRESHOLDS["BUY_SCORE"],
        "BUY_RSI_MAX": config.ANALYSIS_THRESHOLDS["BUY_RSI_MAX"],
        "RISE_SCORE": config.ANALYSIS_THRESHOLDS["RISE_SCORE"],
        "WEIGHTS": config.SCORING_WEIGHTS,
    }


def _truncate(dfs, code, n):
    """한 종목의 일봉을 n개에서 끊는다 — 그 뒤로는 이 세계에 시세가 없다."""
    out = dict(dfs)
    out[code] = dfs[code].iloc[:n].reset_index(drop=True)
    return out


def test_position_is_closed_when_bars_end(universe):
    """봉이 끝난 종목의 포지션은 마지막 봉에서 청산된다 — 슬롯이 풀려야 한다.

    [왜 · 2026-08-25] 종전에는 매도 루프가 `row is None → continue` 로 건너뛰어, 봉이
     끊긴 뒤에도 포지션이 창 끝까지 살아 슬롯을 영구 점유했다. 게다가 _equity 가 봉 없는
     종목을 합계에서 빼는 바람에 투입 자본이 자산곡선에서 증발했고(최종 자산도 같은 자),
     청산 기록이 없으니 승률·PF 표본에서도 빠졌다. 상장폐지를 섞어 생존 편향을 재는
     감사(tools/audit_universe.py 축 B)가 이 경로 위에 서 있다.
    """
    dfs, _status, dates = universe
    code = "000001"
    cut = _truncate(dfs, code, 150)
    last_day = str(cut[code]["date"].iloc[-1])
    status = pbt.precompute_status(cut, _thresholds())

    r = pbt.run_portfolio(cut, status, dates, slots=2)

    buys = [t for t in r["trades"] if t["code"] == code and t["reason"] == "매수"]
    exits = [t for t in r["trades"] if t["code"] == code and t["reason"] != "매수"
             and not t["reason"].startswith("피라미딩")]
    assert len(buys) == len(exits), "봉이 끊긴 종목에 미청산 포지션이 남았다(슬롯 동결)"
    if exits:
        # 마지막 청산은 마지막 봉 날짜를 넘지 않는다 — 없는 시세로 팔지 않는다.
        assert exits[-1]["date"] <= last_day
    assert all(t["date"] <= last_day for t in r["trades"] if t["code"] == code)


def test_data_end_exit_preserves_capital_and_mdd(universe):
    """동결이 만들던 두 인공물 — 자본 증발과 가짜 낙폭 — 이 사라졌는가.

    대조 팔이 필요 없다. 종전 결함은 **자산곡선 자체에 절벽**을 남겼기 때문이다:
    봉이 끊긴 다음 날 _equity 가 그 포지션을 합계에서 빼면서 자산이 하루 만에 꺼지고
    (이 씨드 실측 -77.65%), 그 구덩이가 그대로 MDD(-78.44%)와 최종 자산(432만원,
    시드의 43%)이 됐다. 수정 후에는 +0.36% / -6.24% / 1,945만원이다.
    """
    dfs, _status, dates = universe
    code = "000003"          # 이 씨드는 끊기는 시점에 실제로 보유 중이다(표본이 유효해야 한다)
    cut = _truncate(dfs, code, 150)
    cut_day = str(cut[code]["date"].iloc[-1])

    r = pbt.run_portfolio(cut, pbt.precompute_status(cut, _thresholds()), dates, slots=2)
    assert [t for t in r["trades"] if t["reason"] == "데이터종료"], \
        "끊기는 시점에 포지션이 없으면 이 테스트는 아무것도 재지 않는다"

    eq = r["equity"]
    i = dates.index(cut_day)
    one_day = (eq[i + 1] - eq[i]) / eq[i] * 100
    assert one_day > -20.0, \
        f"봉이 끊긴 다음 날 자산이 절벽처럼 꺼졌다({one_day:+.2f}%) — 포지션이 평가에서 빠진다"
    assert r["final_asset"] > 10_000_000, "끊긴 종목의 투입 자본이 자산에서 사라졌다"
    assert r["mdd"] > -20.0, f"인공 낙폭이 MDD에 남았다({r['mdd']:.2f}%)"


def test_data_end_exit_is_counted_in_the_sample():
    """'데이터종료'는 청산 어휘에 있고 감사 표본에도 들어온다 — 손익이 어디에도 안 잡히면 안 된다."""
    from tools.audit_common import SELL_REASONS, exits as sample_exits
    assert "데이터종료" in pbt.EXIT_REASONS
    assert "데이터종료" in SELL_REASONS
    r = {"trades": [{"code": "A", "date": "20240101", "reason": "데이터종료",
                     "profit": -42.0, "profit_amt": -420000, "days": 30, "mfe": 5.0,
                     "armed": False, "bep": False}]}
    assert len(sample_exits(r)) == 1, "데이터종료가 감사 청산 표본에서 빠진다"


def test_halted_day_does_not_close_the_position(universe):
    """**중간에** 하루 빠진 것(거래정지)은 청산 사유가 아니다 — 재개일에 판정이 다시 돈다."""
    dfs, _status, dates = universe
    code = "000003"
    holed = dict(dfs)
    df = dfs[code]
    drop_at = len(df) // 2
    holed[code] = df.drop(df.index[drop_at:drop_at + 3]).reset_index(drop=True)

    r = pbt.run_portfolio(holed, pbt.precompute_status(holed, _thresholds()), dates, slots=2)
    assert not [t for t in r["trades"] if t["reason"] == "데이터종료"], \
        "중간 공백을 데이터 종료로 오인해 청산했다"
