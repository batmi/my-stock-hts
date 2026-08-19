"""감사 도구의 '청산 표본'이 증액(피라미딩)을 청산으로 세지 않는가.

[왜 이 테스트인가] run_portfolio의 거래 기록에는 신규 매수(reason="매수")와
증액(reason="피라미딩N차")이 청산과 같은 리스트에 섞여 있고, 증액은 profit=0 · days=0
으로 기록된다. 감사 도구 셋이 청산을 `reason != "매수"`로 걸러 증액을 '수익 0%·보유 0일
청산'으로 표본에 넣고 있었다(2026-08-19 수정). 수익·MDD·PF는 시뮬레이터가 직접 내므로
무손상이지만 꼬리 지표·표본 수·보유일·사유 비중이 흔들렸고, 팔마다 증액 횟수가 다르므로
편향이 한쪽 팔만 깎았다 — make_scale_fn 오염과 같은 실패 유형이다.

계측기가 조용히 갈라지는 것을 막는 유일한 방법은 '시뮬레이터가 승률·PF 분모로 쓰는
그 집합'과 감사 표본이 같음을 고정하는 것이다.
"""
import numpy as np
import pandas as pd
import pytest

import config
from modules import backtest, portfolio_backtest as pbt
from tools.audit_common import exits


def _make_df(seed, n=520, start=10000.0, drift=0.004):
    """증액이 실제로 일어날 만큼 긴 상승 추세를 가진 합성 일봉."""
    rng = np.random.default_rng(seed)
    dates = pd.bdate_range("2023-01-02", periods=n).strftime("%Y%m%d").tolist()
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
def result():
    dfs = {f"9{i:05d}": _make_df(seed=200 + i) for i in range(12)}
    thresholds = {
        "BUY_SCORE": config.ANALYSIS_THRESHOLDS["BUY_SCORE"],
        "BUY_RSI_MAX": config.ANALYSIS_THRESHOLDS["BUY_RSI_MAX"],
        "RISE_SCORE": config.ANALYSIS_THRESHOLDS["RISE_SCORE"],
        "WEIGHTS": config.SCORING_WEIGHTS,
    }
    status = pbt.precompute_status(dfs, thresholds)
    dates = sorted({str(d) for df in dfs.values() for d in df["date"]})
    r = pbt.run_portfolio(dfs, status, dates, slots=4, pyramiding_max=3)
    # 증액이 한 건도 없으면 이 테스트는 아무것도 검증하지 못한다 — 표본 무효를 먼저 막는다.
    assert r["pyramid_count"] > 0, "증액이 발생하지 않아 오염 여부를 검증할 수 없다"
    return r


def test_증액과_신규매수는_청산_표본에서_빠진다(result):
    sample = exits(result)
    assert sample, "청산이 한 건도 없다 — 표본이 무효다"
    for t in sample:
        assert not str(t["reason"]).startswith("피라미딩"), f"증액이 청산으로 셌다: {t}"
        assert t["reason"] != "매수"


def test_감사_표본은_시뮬레이터의_승패_분모와_같은_집합이다(result):
    """감사 지표와 시뮬레이터 지표(승률·PF)가 다른 표본 위에 서면 안 된다."""
    assert exits(result) is result["sells"]
    wins = sum(1 for t in exits(result) if t["profit_amt"] > 0)
    assert wins == result["win"]


def test_옛_결과_dict에도_같은_규칙이_적용된다(result):
    """sells 키가 없던 시절의 결과를 넣어도 답이 같아야 한다(폴백 경로)."""
    legacy = {k: v for k, v in result.items() if k != "sells"}
    assert [t["date"] for t in exits(legacy)] == [t["date"] for t in result["sells"]]


def test_느슨한_필터는_표본을_부풀린다(result):
    """[대조] 종전 규칙과의 차이가 실제로 크다는 사실 자체를 남긴다.

    이 단언이 깨진다면 증액이 없어진 것이므로 위 테스트들도 무효다.
    """
    loose = [t for t in result["trades"] if t["reason"] != "매수"]
    assert len(loose) > len(exits(result))
    median_days_loose = float(np.median([t["days"] for t in loose]))
    median_days_true = float(np.median([t["days"] for t in exits(result)]))
    assert median_days_loose < median_days_true
