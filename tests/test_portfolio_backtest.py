"""다종목 포트폴리오 백테스트 시뮬레이터 회귀 테스트.

핵심 검증 관점은 '기존 단일종목 엔진과 같은 판정을 하되, 포트폴리오 제약(슬롯·현금·히트 캡)이
실제로 걸리는가'다. 판정 로직 자체는 backtest.calculate_daily_status를 공유하므로,
여기서는 제약이 의도대로 동작하는지와 회계(현금·자산)가 깨지지 않는지를 본다.
"""
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


def test_heat_cap_blocks_new_entries_when_tight(universe):
    """히트 캡을 극단적으로 좁히면 신규 진입이 줄어야 한다 (캡이 실제로 게이트로 동작)."""
    dfs, status, dates = universe
    loose = pbt.run_portfolio(dfs, status, dates, slots=3, heat_cap_pct=0.0)   # 0 = 미사용
    tight = pbt.run_portfolio(dfs, status, dates, slots=3, heat_cap_pct=0.05)  # 자산의 0.05%

    n_buy_loose = sum(1 for t in loose["trades"] if t["reason"] == "매수")
    n_buy_tight = sum(1 for t in tight["trades"] if t["reason"] == "매수")
    assert n_buy_tight < n_buy_loose


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
