from datetime import datetime, timedelta
from unittest.mock import patch

import config
import api
from modules.manage import events as calendar_events


def _set_watchlist(kr=None, us=None, kr_etf=None, us_etf=None):
    config.session.stock_data = {
        "stocks_kr": kr or [], "etfs_kr": kr_etf or [],
        "stocks_us": us or [], "etfs_us": us_etf or [],
    }


def test_show_calendar_empty_watchlist():
    """관심종목이 없으면 안내 후 조용히 종료한다."""
    _set_watchlist([], [])
    with patch("config.console.print") as mock_print:
        calendar_events.show_calendar()
    assert any("관심종목이 없습니다" in str(c.args) for c in mock_print.call_args_list)


def test_show_calendar_renders_with_mocked_sources():
    """국내(DART)·해외(yfinance) 소스를 모킹했을 때 정상 렌더링된다."""
    _set_watchlist(
        kr=[{"code": "005930", "name": "삼성전자"}],
        us=[{"code": "NVDA", "name": "NVIDIA"}],
    )
    config.DART_API_KEY = "DUMMY"
    ex = (datetime.now() + timedelta(days=2)).date()

    with patch.object(api, "get_dart_dividend",
                      return_value={"year": "2025", "주당배당금": 361.0, "시가배당률": 1.8}), \
         patch.object(api, "get_dart_acc_month", return_value="12"), \
         patch.object(calendar_events, "_kr_yf_dividends", return_value=None), \
         patch.object(calendar_events, "_collect_us",
                      return_value=[{"code": "NVDA", "name": "NVIDIA", "type": "배당락", "date": ex}]), \
         patch("config.console.print") as mock_print:
        calendar_events.show_calendar()

    out = " ".join(str(c.args) for c in mock_print.call_args_list)
    assert "예정 일정" in out and "국내 배당 정보" in out


def test_gather_watchlist_excludes_etfs():
    """ETF는 배당·실적 공시 대상이 아니므로 캘린더 조회 대상에서 제외된다 (yfinance 404 방지)."""
    _set_watchlist(
        kr=[{"code": "005930", "name": "삼성전자"}],
        us=[{"code": "AAPL", "name": "Apple"}],
        kr_etf=[{"code": "069500", "name": "KODEX 200"}],
        us_etf=[{"code": "QQQ", "name": "Invesco QQQ"}],
    )
    kr, us = calendar_events._gather_watchlist()
    assert kr == [("005930", "삼성전자")]
    assert us == [("AAPL", "Apple")]


def test_month_end_ex_date_matches_known_years():
    """연말 배당락일(2023·2024-12-27)과 추정 로직이 일치한다."""
    assert str(calendar_events._month_end_ex_date(2023, 12)) == "2023-12-27"
    assert str(calendar_events._month_end_ex_date(2024, 12)) == "2024-12-27"


def test_kr_dividend_plan_frequency_mapping():
    """배당 지급 횟수 -> 주기 라벨/기준일 월 매핑."""
    assert calendar_events._kr_dividend_plan(4, "12") == ([3, 6, 9, 12], "분기배당")
    assert calendar_events._kr_dividend_plan(2, "12") == ([6, 12], "반기배당")
    assert calendar_events._kr_dividend_plan(1, "12") == ([12], "연배당")
    # 결산월이 12월이 아니면 연배당 기준일은 결산월
    assert calendar_events._kr_dividend_plan(1, "3") == ([3], "연배당")


def test_next_kr_ex_date_picks_nearest_future():
    """분기배당이면 오늘 이후 가장 가까운 분기말 배당락일을 고른다."""
    from datetime import date
    nxt = calendar_events._next_kr_ex_date([3, 6, 9, 12], date(2026, 6, 21))
    assert nxt == calendar_events._month_end_ex_date(2026, 6)  # 6월말이 다음 기준일


def test_project_next_ex_date_uses_history_pattern():
    """과거 배당락일을 1년 미뤄 오늘 이후 가장 가까운 날짜를 고른다."""
    import pandas as pd
    from datetime import date
    # 분기 배당 패턴: 작년 6/26 -> 올해 6/26(거래일) 투영
    idx = pd.to_datetime(["2025-06-26", "2025-09-29", "2025-12-29", "2026-03-30"])
    div = pd.Series([361, 361, 361, 361], index=idx)
    nxt = calendar_events._project_next_ex_date(div, date(2026, 6, 21))
    assert nxt == date(2026, 6, 26)


def test_kr_year_end_holiday_rolls_back_from_weekend():
    """12/31이 주말이면 연말 휴장일은 직전 평일이다 (2023: 12/31 일 -> 12/29 금)."""
    from datetime import date
    assert calendar_events._kr_year_end_holiday(2023) == date(2023, 12, 29)
    assert calendar_events._kr_year_end_holiday(2024) == date(2024, 12, 31)
    # 연말 휴장일은 거래일이 아니어야 함
    assert calendar_events._is_kr_trading_day(date(2023, 12, 29)) is False


def test_get_dart_dividend_parses_alotmatter():
    """alotMatter 응답에서 주당배당금/시가배당률을 추출한다."""
    rows = [
        {"se": "주당 현금배당금(원)", "thstrm": "361"},
        {"se": "현금배당수익률(%)", "thstrm": "1.8"},
    ]
    with patch.object(api, "get_dart_corp_map", return_value={"005930": "00126380"}), \
         patch.object(api, "call_dart", return_value=rows):
        res = api.get_dart_dividend("005930", year=2025)
    assert res["주당배당금"] == 361.0
    assert res["시가배당률"] == 1.8


def test_get_dart_dividend_returns_none_when_no_corp():
    """corp_code 매핑이 없으면 None."""
    with patch.object(api, "get_dart_corp_map", return_value={}):
        assert api.get_dart_dividend("999999") is None


def test_call_dart_returns_none_without_key():
    """API 키가 없으면 호출하지 않고 None."""
    saved = config.DART_API_KEY
    config.DART_API_KEY = ""
    try:
        assert api.call_dart("alotMatter.json", {"corp_code": "x"}) is None
    finally:
        config.DART_API_KEY = saved


def test_parse_us_date_variants():
    """yfinance의 다양한 날짜 표현을 date로 정규화한다."""
    assert calendar_events._parse_us_date(None) is None
    assert calendar_events._parse_us_date("2026-06-23") == datetime(2026, 6, 23).date()
    assert calendar_events._parse_us_date([datetime(2026, 6, 23)]) == datetime(2026, 6, 23).date()
