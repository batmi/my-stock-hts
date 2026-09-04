"""동시만기 계산은 거래소 달력을 봐야 한다 — 연방공휴일이 아니라.

증시는 성금요일에 쉬고 콜럼버스데이·재향군인의 날에는 연다. 만기는 거래소 일정이므로
연방공휴일로 판정하면 양방향으로 틀린다. 실측상 두 달력이 갈리는 분기 만기일은
2015~2050 중 2021-06-18 하나뿐이다 — 준틴스데이가 이틀 전 연방공휴일로 지정됐지만
NYSE 는 그날 정상 개장했다. 드물다는 것이 안전하다는 뜻은 아니다.
"""
from datetime import date, datetime

import api
from modules.manage import econ_events as econ


def _us_expiries(year):
    got = econ._option_expiry(date(year, 1, 1), date(year, 12, 31))
    return [e["date"] for e in got if e["country"] == "US"]


def test_juneteenth_2021_did_not_move_the_witching():
    """연방공휴일로 보면 6/17 로 하루 앞당겨진다 — NYSE 는 열려 있었다."""
    assert "2021-06-18" in _us_expiries(2021)
    assert "2021-06-17" not in _us_expiries(2021)


def test_every_computed_expiry_is_a_real_trading_day():
    """계산 결과가 휴장일을 가리키면 캘린더가 없는 것보다 나쁘다."""
    bad = []
    for year in range(2020, 2036):
        for d in _us_expiries(year):
            dt = datetime.strptime(d, "%Y-%m-%d")
            if dt.weekday() >= 5 or api.is_exchange_holiday(dt, "XNYS"):
                bad.append(d)
    assert not bad, f"휴장일에 만기를 적었다: {bad}"


def test_the_exchange_calendar_is_what_gets_consulted():
    """폴백(연방공휴일)으로 조용히 되돌아가 있지 않은지 본다."""
    seen = []
    real = api.is_exchange_holiday
    try:
        api.is_exchange_holiday = lambda dt, mic: (seen.append(mic), real(dt, mic))[1]
        econ._option_expiry(date(2026, 1, 1), date(2026, 12, 31))
    finally:
        api.is_exchange_holiday = real
    assert seen and set(seen) == {"XNYS"}, seen
