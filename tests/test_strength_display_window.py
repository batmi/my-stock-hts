"""장중 체결 지표(체결강도·매도잔량비)는 장이 열려 있을 때만 표기한다.

거래일 08:00~20:00(NXT 프리~애프터) 밖에는 호가가 서지 않아 값이 굳거나 0으로 내려온다.
굳은 값을 그대로 두면 '지금 이 종목의 체결강도'로 읽히므로, 값이 아니라 **컬럼 표기
자체를 생략**한다. 종전에는 토스 매도비만 이 창을 지켰고 KIS 체결강도는 시간과 무관하게
늘 표기됐다 — 같은 자리의 같은 성격의 값이 모드마다 다른 규칙을 따랐다.
"""
import inspect
from datetime import datetime

import pytest

import api
import config
from modules import analysis


@pytest.fixture
def trading_day(monkeypatch):
    monkeypatch.setattr(api, "is_holiday_today", lambda: False)


@pytest.mark.parametrize("hhmm,expected", [
    ("0759", False),   # NXT 프리마켓 전
    ("0800", True),    # NXT 프리마켓 시작
    ("0900", True),    # KRX 정규장
    ("1530", True),    # NXT 애프터마켓
    ("2000", True),    # 애프터마켓 종료 시각
    ("2001", False),   # 모든 장 종료
    ("0300", False),
])
def test_window_covers_nxt_pre_to_after(trading_day, monkeypatch, hhmm, expected):
    class _Now:
        @staticmethod
        def now():
            return datetime.strptime(f"20260904 {hhmm}", "%Y%m%d %H%M")

    monkeypatch.setattr("api.quotes.price.datetime", _Now)
    assert api.is_strength_display_window() is expected


def test_holiday_is_closed(monkeypatch):
    monkeypatch.setattr(api, "is_holiday_today", lambda: True)
    assert api.is_strength_display_window() is False


def test_toss_window_is_the_same_definition(trading_day):
    """매도비 창과 체결강도 창은 같은 것이어야 한다 — 정의가 갈리면 또 어긋난다."""
    assert api.is_toss_ask_bid_window() == api.is_strength_display_window()


def test_header_suffix_is_gated_for_both_modes():
    """헤더의 ' [강도]'·' [매도비]' 가 같은 게이트 아래에 있어야 한다."""
    src = inspect.getsource(analysis.print_table)
    head = src[src.index("col_header = "):src.index("table.add_column(col_header")]
    assert "is_strength_display_window()" in head
    assert '" [강도]"' in head and '" [매도비]"' in head
    #  게이트 밖에서 접미사를 붙이는 자리가 남아 있으면 안 된다
    assert head.count("col_header +=") == 1


def test_cell_is_emptied_outside_the_window():
    """KIS 체결강도 셀도 창 밖에서는 비운다(헤더만 지우면 값이 떠 있는다)."""
    src = inspect.getsource(analysis._analyze_table_row)
    assert "elif not api.is_strength_display_window():" in src


def test_quotes_are_not_fetched_outside_the_window():
    """어차피 안 쓰는 값을 종목마다 REST 로 받아 오지 않는다."""
    src = inspect.getsource(analysis._collect_table_data)
    vol = src[src.index("fut_vol = "):src.index("fut_detail = ")]
    assert "is_strength_display_window()" in vol
    ab = src[src.index("fut_ab = "):]
    assert "is_strength_display_window()" in ab.split("\n\n")[0]
