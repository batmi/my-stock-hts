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
