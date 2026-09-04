"""52주 위치를 실매매와 백테스트가 같은 창으로 재는가.

[배경] 실매매는 2026-07-24 에 52주 창을 `tail(250)`(250거래일)에서 **365 달력일**로 바꿨다
(analysis._w52_high_low). 250거래일은 실측 373일치라 52주보다 8일 넓고, 그 경계 밖 극값이
밴드를 통째로 왜곡한다 — TIGER 조선TOP10 이 20.2% → 11.0% 로 바뀐 그 사고다.

그런데 백테스트(`compute_price_indicators`)는 옛 정의로 남아 있었다. w52_pos 는 점수 항목이자
게이트 입력이므로(MOMENTUM_W52_NEAR 80), 두 창이 다르면 **같은 봉에 다른 점수가 매겨진다.**
실측(10종목·약 450일): 차이가 하루의 9.9% 에서 1%p 를 넘고 최대 14.1%p.
"""
import numpy as np
import pandas as pd
import pytest

from modules import backtest as bt


def _frame(days, prices=None, start="2023-01-02"):
    idx = pd.bdate_range(start, periods=days)
    c = np.asarray(prices if prices is not None else np.linspace(100, 200, days), dtype=float)
    return pd.DataFrame({"date": idx.strftime("%Y%m%d"), "high": c * 1.01,
                         "low": c * 0.99, "close": c})


def test_a_spike_just_outside_52_weeks_is_not_counted():
    """250거래일 창이 잡아 오던 그 극값 — 날짜로 자르면 빠져야 한다."""
    n = 400
    c = np.full(n, 100.0)
    c[0] = 1000.0                       # 400영업일 전 = 약 560일 전의 고점
    df = _frame(n, c)
    bt.apply_w52_position(df)
    assert float(df["roll_high_52w"].iloc[-1]) < 200, \
        "52주 밖 극값이 밴드에 들어왔다"


def test_a_spike_inside_52_weeks_is_counted():
    """대조군 — 창 안의 극값은 반드시 잡혀야 한다."""
    n = 400
    c = np.full(n, 100.0)
    c[-60] = 1000.0
    df = _frame(n, c)
    bt.apply_w52_position(df)
    assert float(df["roll_high_52w"].iloc[-1]) > 900


def test_the_window_matches_the_live_definition():
    """실매매의 _w52_band 와 마지막 봉에서 값이 같아야 한다."""
    import datetime as dt

    from modules import analysis as an

    rng = np.random.default_rng(3)
    n = 500
    c = 100 * np.exp(np.cumsum(rng.normal(0, 0.02, n)))
    df = _frame(n, c)
    bt.apply_w52_position(df)

    class _Fixed(dt.datetime):
        @classmethod
        def now(cls, tz=None):
            return dt.datetime.strptime(df["date"].iloc[-1], "%Y%m%d")

    orig = an.datetime
    an.datetime = _Fixed
    try:
        h, l = an._w52_band(df)
    finally:
        an.datetime = orig

    assert float(df["roll_high_52w"].iloc[-1]) == pytest.approx(h, rel=1e-12)
    assert float(df["roll_low_52w"].iloc[-1]) == pytest.approx(l, rel=1e-12)


def test_a_short_history_falls_back_to_everything_held():
    """신규상장·워밍업 앞머리 — 좁아진 밴드를 쓰면 52주 위치가 부풀려진다."""
    df = _frame(60)
    bt.apply_w52_position(df)
    assert float(df["roll_high_52w"].iloc[-1]) == pytest.approx(float(df["high"].max()))
    assert float(df["roll_low_52w"].iloc[-1]) == pytest.approx(float(df["low"].min()))


def test_the_position_is_bounded_and_defined():
    df = _frame(400)
    bt.apply_w52_position(df)
    p = df["w52_pos"]
    assert p.notna().all() and (p >= -1e-9).all() and (p <= 100 + 1e-9).all()


def test_a_flat_series_does_not_divide_by_zero():
    """고가=저가면 밴드 폭이 0이다 — nan/inf 가 점수로 흘러가면 안 된다."""
    n = 300
    df = pd.DataFrame({"date": pd.bdate_range("2023-01-02", periods=n).strftime("%Y%m%d"),
                       "high": [100.0] * n, "low": [100.0] * n, "close": [100.0] * n})
    bt.apply_w52_position(df)
    assert df["w52_pos"].notna().all()
    assert np.isfinite(df["w52_pos"].to_numpy()).all()


def test_unreadable_dates_fall_back_loudly(caplog):
    """날짜를 못 읽으면 창을 자를 수 없다 — 조용히 옛 정의로 돌아가면 안 된다."""
    df = _frame(300)
    df.loc[10, "date"] = "없음"
    with caplog.at_level("WARNING", logger=bt.logger.name):
        bt.apply_w52_position(df)
    assert any("52주 창" in r.message for r in caplog.records)
    assert df["w52_pos"].notna().all()


def test_only_one_place_defines_the_window():
    """창 정의가 두 벌이면 한쪽만 고쳐진다 — 실제로 그래서 이 결함이 생겼다."""
    import inspect
    src = inspect.getsource(bt)
    assert "rolling(250, min_periods=1).max()" not in src.replace(
        inspect.getsource(bt.apply_w52_position), "")
