from datetime import datetime, timedelta
from unittest.mock import patch

import pytest

import config
import api
from modules.manage import events as calendar_events
from modules.manage import econ_events


@pytest.fixture(autouse=True)
def _mute_econ_events():
    """show_calendar이 부르는 경제 이벤트 조회를 막는다(테스트가 외부 API를 타지 않도록)."""
    with patch.object(econ_events, "render"):
        yield


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


def test_build_telegram_message_includes_both_sections():
    """/calendar 메시지에 경제 이벤트와 예정 일정이 함께 담긴다."""
    _set_watchlist(us=[{"code": "NVDA", "name": "NVIDIA"}])
    ex = (datetime.now() + timedelta(days=2)).date()

    with patch.object(econ_events, "build_lines",
                      return_value=["▸ 주요 경제 이벤트", "• 07-30(목) D-1 FOMC 금리결정 [Fed]"]), \
         patch.object(calendar_events, "_collect_watchlist_events",
                      return_value=([{"code": "NVDA", "name": "NVIDIA", "type": "배당락",
                                      "date": ex}], [])):
        msg = calendar_events.build_telegram_message(days=30)

    assert "주요 경제 이벤트" in msg and "FOMC 금리결정" in msg
    assert "예정 일정" in msg and "NVIDIA (NVDA)" in msg and "D-2" in msg
    # 일정 줄은 이모지 없이 '•'로만 나열한다
    assert "💰" not in msg and "📊" not in msg
    assert "• " in msg.split("▸ 예정 일정")[1]


def test_build_telegram_message_drops_events_beyond_horizon():
    """지정한 일수 밖의 일정은 빠진다."""
    _set_watchlist(us=[{"code": "NVDA", "name": "NVIDIA"}])
    far = (datetime.now() + timedelta(days=90)).date()

    with patch.object(econ_events, "build_lines", return_value=["▸ 주요 경제 이벤트"]), \
         patch.object(calendar_events, "_collect_watchlist_events",
                      return_value=([{"code": "NVDA", "name": "NVIDIA", "type": "배당락",
                                      "date": far}], [])):
        msg = calendar_events.build_telegram_message(days=30)

    assert "NVDA" not in msg and "표시할 예정 일정이 없습니다" in msg


def test_build_telegram_message_without_watchlist():
    """관심종목이 없어도 경제 이벤트만으로 메시지가 나간다."""
    _set_watchlist([], [])
    with patch.object(econ_events, "build_lines",
                      return_value=["▸ 주요 경제 이벤트", "• 07-30(목) D-1 FOMC 금리결정 [Fed]"]):
        msg = calendar_events.build_telegram_message()
    assert "FOMC 금리결정" in msg and "등록된 관심종목이 없습니다" in msg


def _alert_env(econ=None, stock_events=None, notified=()):
    """check_and_alert_calendar 호출용 공통 모킹 묶음."""
    from modules import db_manager
    return (
        patch.object(econ_events, "get_events",
                     return_value=(econ or [], {"stale_since": None, "complete": True})),
        patch.object(calendar_events, "_collect_watchlist_events",
                     return_value=(stock_events or [], [])),
        patch.object(db_manager.db, "is_disclosure_notified", side_effect=lambda k: k in notified),
        patch.object(db_manager.db, "mark_disclosure_notified"),
        patch.object(api, "send_telegram_message"),
    )


def test_calendar_alert_sends_digest_for_today_and_tomorrow():
    """D-DAY·D-1 일정을 하루 한 통의 요약으로 묶어 보낸다."""
    _set_watchlist(kr=[{"code": "005930", "name": "삼성전자"}])
    today = datetime.now().date()
    econ = [{"date": today.strftime("%Y-%m-%d"), "name": "미국 CPI", "weight": 1, "source": "FRED"}]
    stocks = [{"code": "005930", "name": "삼성전자", "type": "배당락",
               "date": today + timedelta(days=1), "estimated": True, "freq": "분기배당"}]

    p1, p2, p3, p4, p5 = _alert_env(econ, stocks)
    with p1, p2, p3, p4 as mark, p5 as send:
        assert calendar_events.check_and_alert_calendar() == 1

    assert send.call_count == 1  # 건별이 아니라 요약 한 통
    msg = send.call_args.args[0]
    assert "캘린더 알림" in msg
    assert "오늘" in msg and "미국 CPI" in msg
    assert "내일" in msg and "삼성전자 (005930)" in msg
    assert "📊" not in msg and "💰" not in msg   # 종목 줄도 '•'로 통일
    assert "• 삼성전자 (005930)" in msg
    assert mark.call_count == 2  # 경제 1건 + 종목 1건


def test_calendar_alert_skips_already_notified():
    """이미 보낸 일정만 남으면 아무것도 발송하지 않는다."""
    _set_watchlist([], [])
    today = datetime.now().date()
    econ = [{"date": today.strftime("%Y-%m-%d"), "name": "미국 CPI", "weight": 1, "source": "FRED"}]
    key = calendar_events._alert_key("econ", today.strftime("%Y-%m-%d"), "미국 CPI", 0)

    p1, p2, p3, p4, p5 = _alert_env(econ, notified={key})
    with p1, p2, p3, p4, p5 as send:
        assert calendar_events.check_and_alert_calendar() == 0
    send.assert_not_called()


def test_calendar_alert_ignores_far_events():
    """D-2 이후 일정은 아직 알리지 않는다."""
    _set_watchlist([], [])
    today = datetime.now().date()
    econ = [{"date": (today + timedelta(days=5)).strftime("%Y-%m-%d"),
             "name": "미국 CPI", "weight": 1, "source": "FRED"}]

    p1, p2, p3, p4, p5 = _alert_env(econ)
    with p1, p2, p3, p4, p5 as send:
        assert calendar_events.check_and_alert_calendar() == 0
    send.assert_not_called()


def test_calendar_alert_does_not_mark_when_send_fails():
    """발송이 실패하면 '보냄'으로 기록하지 않는다 (다음 순회에 재시도)."""
    _set_watchlist([], [])
    today = datetime.now().date()
    econ = [{"date": today.strftime("%Y-%m-%d"), "name": "미국 CPI", "weight": 1, "source": "FRED"}]

    p1, p2, p3, p4, p5 = _alert_env(econ)
    with p1, p2, p3, p4 as mark, p5 as send:
        send.side_effect = RuntimeError("network down")
        assert calendar_events.check_and_alert_calendar() == 0
    mark.assert_not_called()
