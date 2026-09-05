"""휴장 판정: **조회 실패를 확정으로 굳히지 않는다**, 그리고 연말 폐장일은 정의가 하나다.

[왜 이 파일이 있나 · 2026-09-05]

① 실패한 답이 하루 종일 굳었다.
   `is_holiday_on` 은 KIS 휴장일 TR(또는 토스 market-calendar)을 먼저 묻고, 실패하면
   holidays 라이브러리로 메웠다. 그런데 그 폴백 값을 `_HOLIDAY_CACHE` 에 그대로 넣어서
   `if date_str in _HOLIDAY_CACHE: return` 에 걸려 **API 를 두 번 다시 묻지 않았다**.
   기동 직후 — 라즈베리파이 부팅, 토큰 발급 전, cron 이 네트워크보다 먼저 뜨는 순간 —
   한 번 실패하면 끝이다(실측 재현: 복구 후에도 재조회 0회).

   방향이 나쁘다. holidays 라이브러리는 **임시공휴일을 모른다**(그 해에 정해지므로 설치된
   버전에 없다). 즉 실패의 기본값이 '거래일'이고, 닫힌 시장에 매매 루프가 하루 종일 돈다.
   이 판정은 market_today → 당일 봉 · is_system_market_open → 주문까지 이어진다.

② 연말 폐장일의 정의가 두 개였다.
   api/market_calendar 는 **무조건 12/31**, modules/manage/events 는 **주말이면 직전 평일**.
   2026~2040 중 4년(2028·2033·2034·2039)이 갈라진다. 매매 경로가 쓰는 쪽이 틀린 쪽이었다.

관련: [[unknown-vs-empty]] · [[config-fallback-literals]]
"""
from unittest.mock import patch

import pytest

import api
import config
from api import market_calendar as mc
from modules.manage import events


WEEKDAY = "20260908"          # 화요일, 공휴일 아님


@pytest.fixture(autouse=True)
def clean_cache():
    mc._HOLIDAY_CACHE.clear()
    mc._HOLIDAY_PROVISIONAL.clear()
    yield
    mc._HOLIDAY_CACHE.clear()
    mc._HOLIDAY_PROVISIONAL.clear()


@pytest.fixture
def kis_mode(monkeypatch):
    monkeypatch.setattr(config.session, "is_toss", False, raising=False)


# --------------------------------------------------------------------------
# ① 실패한 답을 굳히지 않는다
# --------------------------------------------------------------------------
def test_조회_실패한_답은_확정_캐시에_들어가지_않는다(kis_mode):
    with patch.object(api, "check_holiday", return_value=None):
        api.is_holiday_on(WEEKDAY)
    assert WEEKDAY not in mc._HOLIDAY_CACHE, "실패로 메운 답이 확정으로 굳었다"
    assert api.holiday_answer_provisional(WEEKDAY) is True


def test_재조회_시각이_지나면_다시_묻는다(kis_mode):
    """[핵심] 네트워크가 돌아오면 임시공휴일을 알아채야 한다."""
    with patch.object(api, "check_holiday", return_value=None):
        assert api.is_holiday_on(WEEKDAY) is False        # 라이브러리 = 거래일

    value, _ = mc._HOLIDAY_PROVISIONAL[WEEKDAY]
    mc._HOLIDAY_PROVISIONAL[WEEKDAY] = (value, 0.0)       # 재조회 시각 경과

    calls = []
    with patch.object(api, "check_holiday", side_effect=lambda d: calls.append(d) or True):
        assert api.is_holiday_on(WEEKDAY) is True, "복구된 API 의 '휴장'을 받아들이지 못했다"
    assert len(calls) == 1
    assert api.holiday_answer_provisional(WEEKDAY) is False


def test_확정된_답은_다시_묻지_않는다(kis_mode):
    calls = []
    with patch.object(api, "check_holiday", side_effect=lambda d: calls.append(d) or False):
        api.is_holiday_on(WEEKDAY)
        api.is_holiday_on(WEEKDAY)
        api.is_holiday_on(WEEKDAY)
    assert len(calls) == 1, f"확정 답을 {len(calls)}번이나 물었다 — TPS 낭비"


def test_재조회_창_안에서는_잠정값을_쓴다(kis_mode):
    """실패했다고 매 호출 API 를 두드리면 그것대로 사고다(장애 중 폭주)."""
    calls = []
    with patch.object(api, "check_holiday", side_effect=lambda d: calls.append(d) or None):
        for _ in range(5):
            api.is_holiday_on(WEEKDAY)
    assert len(calls) == 1, f"재조회 창 안에서 {len(calls)}번 물었다"


def test_주말은_물어볼_것도_없이_확정이다(kis_mode):
    saturday = "20260912"                                 # 토요일
    with patch.object(api, "check_holiday", side_effect=AssertionError("주말은 묻지 않는다")):
        assert api.is_holiday_on(saturday) is True
    assert saturday in mc._HOLIDAY_CACHE


def test_토스모드의_과거_날짜는_라이브러리가_유일한_답이라_확정이다(monkeypatch):
    """물어볼 곳이 없는 것과 물었는데 실패한 것은 다르다."""
    monkeypatch.setattr(config.session, "is_toss", True, raising=False)
    past = "20260810"                                     # 월요일, 오늘이 아님
    api.is_holiday_on(past)
    assert past in mc._HOLIDAY_CACHE
    assert api.holiday_answer_provisional(past) is False


def test_거래일_캐시가_잠정값을_확정으로_승격시키지_않는다(kis_mode):
    """market_today 가 굳혀 버리면 아래 계층의 재조회가 무의미해진다."""
    from datetime import datetime
    with patch.object(api, "check_holiday", return_value=None):
        api.market_today(is_overseas=False)
    today = datetime.now().strftime("%Y%m%d")
    if api.holiday_answer_provisional(today):             # 주말이면 잠정이 아니다
        assert (today, 'KR') not in api._TRADING_DAY_CACHE


# --------------------------------------------------------------------------
# ①-2 미국 쪽에도 같은 규칙이 걸려 있다 (2026-09-05)
#
#  한국 쪽만 고치고 is_us_holiday_on 은 `_HOLIDAY_CACHE[...] = ...` 직접 대입으로
#  남아 있었다. 토스 모드에서는 market-calendar 가 정본이고 라이브러리는 대타다 —
#  대타 답을 굳히면 기동 직후 한 번 실패한 판정이 그 날 내내 다시 물어지지 않는다.
# --------------------------------------------------------------------------
US_WEEKDAY = "20260908"       # 화요일, NYSE 휴장일 아님


@pytest.fixture
def frozen_weekday(monkeypatch):
    """'오늘'을 평일로 고정한다.

    이 절의 판정은 `date_str == 오늘` 일 때만 토스 달력을 묻는다. 벽시계에 맡기면
    주말에 통째로 건너뛰어, 토·일 이틀은 아무도 안 보는 테스트가 된다.
    """
    from datetime import datetime as _real

    class _Fixed(_real):
        @classmethod
        def now(cls, tz=None):
            return cls(2026, 9, 8, 10, 0, 0)      # 화요일

    monkeypatch.setattr(mc, "datetime", _Fixed)
    return US_WEEKDAY


def test_토스모드_미국_달력_실패는_굳지_않는다(monkeypatch, frozen_weekday):
    monkeypatch.setattr(config.session, "is_toss", True, raising=False)
    today = frozen_weekday
    with patch("brokers.toss_api.get_market_calendar", side_effect=RuntimeError("net")):
        api.is_us_holiday_on(today)
    assert f"US_{today}" not in mc._HOLIDAY_CACHE, "실패한 답이 확정 캐시에 굳었다"
    assert api.holiday_answer_provisional(today, country="US") is True


def test_토스모드_미국_달력_성공은_확정이다(monkeypatch, frozen_weekday):
    monkeypatch.setattr(config.session, "is_toss", True, raising=False)
    today = frozen_weekday
    # 먼저 실패시켜 잠정값을 만들어 둔다 — 성공이 그것을 지워야 한다.
    with patch("brokers.toss_api.get_market_calendar", side_effect=RuntimeError("net")):
        api.is_us_holiday_on(today)
    assert api.holiday_answer_provisional(today, country="US") is True

    # 재조회 시각을 앞당긴다(잠정 답은 HOLIDAY_RETRY_SEC 동안 그대로 쓰인다).
    val, _ = mc._HOLIDAY_PROVISIONAL[f"US_{today}"]
    mc._HOLIDAY_PROVISIONAL[f"US_{today}"] = (val, 0.0)

    with patch("brokers.toss_api.get_market_calendar",
               return_value={"today": {"regularMarket": {"open": "09:30"}}}):
        assert api.is_us_holiday_on(today) is False
    assert f"US_{today}" in mc._HOLIDAY_CACHE
    assert api.holiday_answer_provisional(today, country="US") is False, (
        "확정 답이 들어왔는데 잠정 항목이 남아 재조회가 계속 열려 있다")


def test_토스가_아니면_라이브러리가_정본이라_확정이다(kis_mode):
    """KIS 해외 휴장일 TR 은 404다(2026-08-22) — 물어볼 권위가 없다."""
    assert api.is_us_holiday_on(US_WEEKDAY) is False
    assert f"US_{US_WEEKDAY}" in mc._HOLIDAY_CACHE
    assert api.holiday_answer_provisional(US_WEEKDAY, country="US") is False


def test_확정_캐시에_직접_손대는_곳이_없다():
    """`_HOLIDAY_CACHE[...] = ...` 를 우회로로 쓰면 잠정/확정 구분이 조용히 무너진다."""
    import ast
    import os
    src = open(mc.__file__.replace(".pyc", ".py"), encoding="utf-8").read()
    tree = ast.parse(src)
    owner = next(n for n in tree.body
                 if isinstance(n, ast.FunctionDef) and n.name == "_remember_holiday")
    owner_lines = set(range(owner.lineno, (owner.end_lineno or owner.lineno) + 1))
    bad = []
    for node in ast.walk(tree):
        if not isinstance(node, (ast.Assign, ast.AugAssign)):
            continue
        targets = node.targets if isinstance(node, ast.Assign) else [node.target]
        for t in targets:
            if (isinstance(t, ast.Subscript) and isinstance(t.value, ast.Name)
                    and t.value.id == "_HOLIDAY_CACHE" and node.lineno not in owner_lines):
                bad.append(node.lineno)
    assert not bad, (
        "_remember_holiday 밖에서 확정 캐시에 직접 쓴다 — 잠정 답이 굳는다: "
        + ", ".join(f"{os.path.basename(mc.__file__)}:{ln}" for ln in bad))


# --------------------------------------------------------------------------
# ② 연말 폐장일은 정의가 하나다
# --------------------------------------------------------------------------
def test_연말_폐장일_정의가_하나다():
    """두 곳이 서로 다른 규칙을 들고 있었다 — 15년 중 4년이 갈라졌다."""
    diff = [y for y in range(2020, 2061)
            if api.kr_year_end_closing_day(y) != events._kr_year_end_holiday(y)]
    assert not diff, f"연말 폐장일 정의가 갈라진 해: {diff}"


@pytest.mark.parametrize("year, expected", [
    (2026, "20261231"),     # 목요일 — 12/31 그대로
    (2028, "20281229"),     # 일요일 — 직전 금요일이 폐장일
    (2033, "20331230"),     # 토요일 — 직전 금요일
    (2034, "20341229"),     # 일요일
])
def test_12월_31일이_주말이면_직전_평일이_폐장일이다(year, expected):
    assert api.kr_year_end_closing_day(year).strftime("%Y%m%d") == expected


def test_폐장일이_휴장으로_판정된다():
    """정의만 맞추고 판정에 안 닿으면 소용없다."""
    assert api.get_holiday_name("20281229", country="KR") == "연말 폐장일"
    assert api.get_holiday_name("20281228", country="KR") is None   # 그 전날은 거래일


def test_폐장일_다음의_직전_거래일을_바르게_짚는다():
    """last_trading_day → market_today 로 이어져 '오늘 봉'을 정하는 자리다."""
    from datetime import datetime
    # 2028-12-30(토) 에 물으면 12/29 는 폐장이므로 12/28(목)이 답이다.
    assert api.last_trading_day(datetime(2028, 12, 30), 'KR') == "20281228"
