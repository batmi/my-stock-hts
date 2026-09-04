"""AI 브리핑에 들어가는 52주 고점은 화면·스코어링과 같은 자를 써야 한다.

[배경 · 2026-09-04 감사] _get_macro_context_str 이 만드는 문장은 "이 수치들과 현재 상태를
절대적인 팩트로 반영할 것"이라는 지시와 함께 AI 에 들어간다. 그 안의 '52주 고점대비 -x%'와
evaluate_market_indicator 의 국면 문구('신고가 근접/초강세' ↔ '침체/약세장 진입')를
정하는 값이 52주 고점이다.

그런데 코스피·코스닥·미국채만 close.tail(250).max() 로 직접 세고 있었다. 창이 52주보다
넓고(250거래일=실측 373일) 종가만 봐서 장중 고가를 놓친다. 같은 표의 나머지 지표는
벤더의 52주 고가(year_high)를 쓰므로 한 표 안에서 잣대가 갈렸다. _w52_band 는 바로 그
어긋남을 없애려고 만든 단일 진입점이다.
"""
import numpy as np
import pandas as pd
import pytest

from modules import analysis, theme_analysis


def _df(days=400, high_at=None, high_val=None):
    dates = pd.date_range(end=pd.Timestamp.now().normalize(), periods=days)
    close = pd.Series(np.linspace(100.0, 120.0, days))
    df = pd.DataFrame({
        "date": dates.strftime("%Y%m%d"),
        "open": close, "high": close + 1.0, "low": close - 1.0,
        "close": close, "volume": 1.0,
    })
    if high_at is not None:
        df.loc[high_at, "high"] = high_val
    return df


def test_it_delegates_to_the_shared_band():
    df = _df()
    assert theme_analysis._yh_52w(df) == pytest.approx(analysis._w52_band(df)[0])


def test_intraday_high_is_not_lost():
    """종가만 세면 장중에만 찍힌 고점이 사라져 '신고가 근접'으로 오독된다."""
    df = _df(high_at=380, high_val=500.0)
    assert theme_analysis._yh_52w(df) == pytest.approx(500.0)
    assert float(df["close"].tail(250).max()) < 500.0     # 옛 방식은 못 본다


@pytest.mark.parametrize("days_ago,inside", [(300, True), (370, False)])
def test_the_window_is_365_days(days_ago, inside):
    """창은 날짜로 자른다 — 250'거래일'은 실측 373일이라 52주보다 넓고, 그 경계 밖
    극값 하나가 밴드를 통째로 왜곡한다(_w52_high_low 가 tail(250)을 버린 이유)."""
    days = 500
    df = _df(days=days, high_at=days - 1 - days_ago, high_val=9999.0)
    assert (theme_analysis._yh_52w(df) == pytest.approx(9999.0)) is inside


@pytest.mark.parametrize("bad", [None, pd.DataFrame()])
def test_unusable_frames_give_none_not_zero(bad):
    """0을 주면 '52주 고점대비 -100%'라는 거짓 팩트가 프롬프트에 들어간다."""
    assert theme_analysis._yh_52w(bad) is None


def test_macro_lines_use_it_for_domestic_indices():
    """게이트가 실제로 배선돼 있는지 — 헬퍼만 있고 안 쓰면 소용없다."""
    import inspect

    src = inspect.getsource(theme_analysis._get_macro_context_str)
    assert "_yh_52w(" in src
    assert "tail(250).max()" not in src, "52주 고점을 다시 직접 세고 있다"
