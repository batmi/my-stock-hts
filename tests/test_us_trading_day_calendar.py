"""미국 '오늘'(market_today) 판정이 NYSE 달력을 쓰는지 검증한다.

[배경] api/market_calendar 는 EXCHANGE_CALENDARS 주석에서 "증시는 성금요일에 쉬고
콜럼버스데이·재향군인의 날에는 열어서, 연방공휴일로 보면 양방향으로 틀렸다"고 적고
is_us_holiday_on 을 XNYS 달력으로 고쳤는데, _is_closed_day 만 연방공휴일에 남아 있었다.
이 함수는 market_today(True) → last_trading_day 경로로 '미국 종목의 오늘'을 정한다.

틀리면 양방향으로 봉이 오염된다.
 · 성금요일(휴장인데 거래일로 봄) → 가짜 당일 봉이 붙어 등락률이 0%
 · 콜럼버스데이·재향군인의 날(개장인데 휴장으로 봄) → 그날의 현재가가 **확정된
   전날 봉을 덮어쓴다**
"""
from datetime import datetime

import pytest

import api


def _ltd(y, m, d):
    return api.last_trading_day(datetime(y, m, d), 'US')


@pytest.mark.parametrize("y,m,d", [(2026, 4, 3), (2027, 3, 26), (2025, 4, 18)])
def test_good_friday_is_a_holiday_for_nyse(y, m, d):
    """성금요일은 연방공휴일이 아니지만 NYSE 는 쉰다 → 직전 거래일로 되돌려야 한다."""
    assert _ltd(y, m, d) != datetime(y, m, d).strftime('%Y%m%d')


@pytest.mark.parametrize("y,m,d", [
    (2026, 10, 12),   # 콜럼버스데이
    (2026, 11, 11),   # 재향군인의 날
    (2025, 10, 13),
    (2025, 11, 11),
])
def test_federal_only_holidays_are_trading_days(y, m, d):
    """연방공휴일이지만 NYSE 는 연다 → 그날이 그대로 거래일이어야 한다."""
    assert _ltd(y, m, d) == datetime(y, m, d).strftime('%Y%m%d')


def test_real_nyse_holiday_still_rolls_back():
    """독립기념일(2026-07-03 대체휴장, 금)은 실제 휴장이라 되돌린다."""
    assert _ltd(2026, 7, 3) == '20260702'


def test_domestic_path_unchanged(monkeypatch):
    """국내는 종전대로 — 오늘은 실시간 캘린더, 그 외 일자는 holidays(KR)."""
    monkeypatch.setattr(api, 'is_holiday_today', lambda: False)
    assert api.last_trading_day(datetime(2026, 5, 5), 'KR') == '20260504'   # 어린이날
