"""OBV 셀은 어느 표에서나 같은 뜻이어야 한다.

[배경 · 2026-09-04] 같은 포맷팅 블록이 세 표(기간별 시세 · 관심종목 국내/해외 상세)에
복제돼 있었고, **OBV 이동평균이 없을 때의 색이 갈렸다**.
 · 기간별 시세  — 판정 불가 → white
 · 관심종목 상세 — 판정 불가 → True 로 폴백 → red(상승)
결측 표기도 한쪽은 '[dim]-[/dim]', 다른 쪽은 '-'였다. 모르는 것을 상승으로 칠하면
화면이 없는 신호를 만들어 낸다.
"""
import inspect

import pandas as pd
import pytest

from core import utils


@pytest.mark.parametrize("obv,ma,expected_color", [
    (1_500_000_000, 1_000_000_000, "red"),      # 이평 위 = 상승
    (1_000_000_000, 1_500_000_000, "blue"),     # 이평 아래 = 하락
    (1_500_000_000, float("nan"), "white"),     # 판정 불가 = 모른다
    (1_500_000_000, None, "white"),
])
def test_trend_colour(obv, ma, expected_color):
    assert utils.format_obv_cell(obv, ma).startswith(f"[{expected_color}]")


@pytest.mark.parametrize("obv", [None, float("nan")])
def test_missing_obv_is_dim_dash(obv):
    assert utils.format_obv_cell(obv, 1) == "[dim]-[/dim]"


@pytest.mark.parametrize("obv,text", [
    (1_500_000_000_000, "1.5T"),
    (1_500_000_000, "1.5B"),
    (1_500_000, "1.5M"),
    (-950_000, "-950K"),
    (500, "500"),
    (0, "0"),
])
def test_abbreviation(obv, text):
    assert text in utils.format_obv_cell(obv, None)


def test_boundaries_round_up_to_the_next_unit():
    """반올림 경계 — 999,950,000 은 'B'로 올라간다(999.9M 로 찍히면 자릿수가 어긋난다)."""
    assert "1.0B" in utils.format_obv_cell(999_950_000, None)
    assert "999.9M" in utils.format_obv_cell(999_949_999, None)


def test_all_period_tables_use_the_single_source():
    """표가 자기만의 OBV 블록을 다시 갖지 않는지 — 갈라지면 색이 또 어긋난다."""
    from modules import analysis
    from modules.manage import watchlist

    src = inspect.getsource(watchlist.show_extended_info)
    assert src.count("utils.format_obv_cell") == 2, "국내·해외 상세 두 표가 모두 써야 한다"
    assert "obv_trend = obv_val > row['OBV_MA']" not in src, "복제된 판정이 남아 있다"

    period = inspect.getsource(analysis._print_period_price_common)
    assert "utils.format_obv_cell" in period
