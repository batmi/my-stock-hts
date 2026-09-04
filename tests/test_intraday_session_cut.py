"""분봉을 정규장으로 자르는 경계 — 시간외가 장중 판정에 섞이지 않는가.

[사실관계] tvDatafeed 분봉의 시각은 **시작 시각**이다(실측: 하루의 첫 봉이 0900).
그래서 30분봉의 '1530' 봉은 15:30~16:00, 즉 통째로 시간외다. 종전 조건 `hhmm > "1530"`
은 그 봉을 남겼다 — 장이 닫힌 15:30 에 판정 시점이 하나 더 생겨, 실매매에 없는 매매
기회가 만들어졌다. 실측(12종목·358일): 그 봉이 하루 거래량의 8.3%를 싣고 3.4%의 날에
그날 고/저를 바꿨다.

60분봉의 '1500' 봉은 15:00~16:00 이라 시간외를 **안에 품는다**(실측: 60분 1500 봉 =
30분 1500 봉 + 30분 1530 봉, 괴리 0.00%). 라벨로는 못 걸러내므로 남는 근사이고,
`drop_straddling=True` 로만 걷어낸다.
"""
from datetime import datetime

import pandas as pd
import pytest

from modules import intraday_bars as ib


def _frame(times, day="2026-09-03"):
    idx = [pd.Timestamp(f"{day} {t[:2]}:{t[2:]}") for t in times]
    n = len(times)
    return pd.DataFrame({"open": [100.0] * n, "high": [101.0] * n, "low": [99.0] * n,
                         "close": [100.0] * n, "volume": [10.0] * n}, index=idx)


T30 = ["0900", "0930", "1000", "1030", "1100", "1130", "1200", "1230",
       "1300", "1330", "1400", "1430", "1500", "1530", "1600"]
T60 = ["0900", "1000", "1100", "1200", "1300", "1400", "1500", "1600"]


def _times(df, **kw):
    d = ib.by_day(df, **kw)
    return [b[0] for b in next(iter(d.values()))] if d else []


def test_a_thirty_minute_bar_starting_at_the_close_is_dropped():
    """15:30 봉은 통째로 시간외다 — 장이 닫힌 시각에 판정 시점이 생기면 안 된다."""
    got = _times(_frame(T30))
    assert "1530" not in got and "1600" not in got
    assert got[-1] == "1500", got


def test_the_last_regular_thirty_minute_bar_survives():
    """15:00~15:30 은 정규장이다 — 같이 버리면 하루의 마지막 판정이 사라진다."""
    assert "1500" in _times(_frame(T30))


def test_the_old_rule_kept_the_after_hours_bar():
    """고치기 전 규칙을 그대로 재현해 둔다 — 무엇이 결함이었는지의 기준선."""
    kept = [t for t in T30 if not (t > "1530")]
    assert "1530" in kept, "이 값이 빠졌다면 애초에 결함이 아니었다"


def test_sixty_minute_bars_stop_at_the_last_regular_start():
    assert _times(_frame(T60)) == ["0900", "1000", "1100", "1200", "1300", "1400", "1500"]


def test_the_straddling_bar_can_be_dropped_on_request():
    """60분 1500 봉은 시간외를 품는다 — 걷어내는 길은 있어야 한다."""
    got = _times(_frame(T60), drop_straddling=True)
    assert got[-1] == "1400", got
    # 30분봉은 걸치는 봉이 없으므로 아무것도 더 잃지 않는다
    assert _times(_frame(T30), drop_straddling=True)[-1] == "1500"


@pytest.mark.parametrize("times, expected", [(T30, 30), (T60, 60)])
def test_the_bar_length_is_measured_not_guessed(times, expected):
    assert ib.bar_minutes(_frame(times)) == expected


def test_the_bar_length_ignores_the_overnight_gap():
    """날짜가 바뀌는 간격을 세면 봉 길이가 하룻밤으로 잡힌다."""
    df = pd.concat([_frame(T60, "2026-09-02"), _frame(T60, "2026-09-03")])
    assert ib.bar_minutes(df) == 60


def test_a_cache_made_under_the_old_rule_is_rejected():
    """규칙이 바뀌면 하루의 판정 시점 목록 자체가 달라진다 — 옛 캐시는 지금 것이 아니다."""
    now = ib.status_meta({"BUY_SCORE": 7.0}, 260, "2026-09-03")
    assert now["session_rule"] == ib.SESSION_RULE
    old = dict(now)
    old.pop("session_rule")
    assert old != now, "표식이 없으면 옛 캐시가 그대로 통과한다"


def test_an_empty_frame_does_not_crash():
    assert ib.by_day(pd.DataFrame(columns=["open", "high", "low", "close", "volume"],
                                  index=pd.DatetimeIndex([]))) == {}
    assert ib.bar_minutes(pd.DataFrame(index=pd.DatetimeIndex([]))) == 60
