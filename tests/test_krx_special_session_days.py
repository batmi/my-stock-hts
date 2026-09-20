"""특수 세션일(수능일·연초 개장일) — 세션 경계가 함께 밀리는가(2026-09-20 감사).

수능일 KRX 정규장은 10:00~16:30 이다. 종전에는 모든 게이트가 09:00/15:30 리터럴이라
15:30 에 '정규장 종료'로 읽어 시장이 열린 마지막 한 시간을 감시 없이 두고, 15:40 가격을
확정 종가로 굳히며, 09:30 부터 낸 매수가 10:00 시가 단일가에 들어갔다.
"""
from datetime import datetime
from unittest.mock import patch

import pytest

pytestmark = pytest.mark.real_session_window   # 휴게 판정 자체를 검증한다(conftest 자동 고정 제외)

import api
import config
from modules.auto_trade import common

SUNEUNG = "20261119"          # 목요일


@pytest.fixture(autouse=True)
def _table(monkeypatch):
    monkeypatch.setattr(config, "KRX_SESSION_SHIFT_DAYS", {SUNEUNG: (60, 60)}, raising=False)
    api.sessions._SHIFT_CACHE.clear()
    yield
    api.sessions._SHIFT_CACHE.clear()


@pytest.fixture
def default_hours():
    """설정은 config.settings 에 직접 넣고 되돌린다 — config 모듈은 __getattr__ 로 settings 를 비추므로
    monkeypatch.setattr(config, ...) 은 복원 때 **모듈 속성을 새로 만들어 settings 를 가린다**
    (test_nxt_market.nxt_hours 주석과 같은 함정)."""
    saved = (config.settings.SYSTEM_TRADING_START_TIME, config.settings.SYSTEM_TRADING_END_TIME,
             config.settings.SYSTEM_ENTRY_OPEN_DELAY_USE, config.settings.SYSTEM_ENTRY_OPEN_DELAY_MINUTES)
    config.settings.SYSTEM_TRADING_START_TIME = "0900"
    config.settings.SYSTEM_TRADING_END_TIME = "1530"
    config.settings.SYSTEM_ENTRY_OPEN_DELAY_USE = True
    config.settings.SYSTEM_ENTRY_OPEN_DELAY_MINUTES = 30
    yield
    (config.settings.SYSTEM_TRADING_START_TIME, config.settings.SYSTEM_TRADING_END_TIME,
     config.settings.SYSTEM_ENTRY_OPEN_DELAY_USE, config.settings.SYSTEM_ENTRY_OPEN_DELAY_MINUTES) = saved


def _at(hhmm, day=SUNEUNG):
    return datetime.strptime(day + hhmm, "%Y%m%d%H%M")


def test_krx_hm_은_수능일에만_경계를_옮긴다():
    assert api.krx_hm("0900", "open", SUNEUNG) == "1000"
    assert api.krx_hm("1530", "close", SUNEUNG) == "1630"
    assert api.krx_hm("0900", "open", "20261118") == "0900"
    assert api.krx_session_shift("20261118") == (0, 0)


def test_연초_첫_거래일은_개장만_한_시간_늦다(monkeypatch):
    monkeypatch.setattr(api, "is_holiday_on", lambda d: d in ("20260101",) or datetime.strptime(d, "%Y%m%d").weekday() >= 5)
    assert api.krx_session_shift("20260102") == (60, 0)      # 금요일, 1/1 휴장 뒤 첫 거래일
    assert api.krx_hm("0900", "open", "20260102") == "1000"
    assert api.krx_hm("1530", "close", "20260102") == "1530"
    assert api.krx_session_shift("20260105") == (0, 0)


@pytest.mark.parametrize("hhmm, phase", [("0930", "nxt_pre"), ("1000", "krx"), ("1600", "krx"),
                                          ("1630", "nxt_after"), ("1700", "krx_after")])
def test_수능일_세션_단계가_한_시간_밀린다(monkeypatch, hhmm, phase):
    monkeypatch.setattr(api, "is_holiday_today", lambda: False)
    with patch("api.sessions.datetime") as dt:
        dt.now.return_value = _at(hhmm); dt.strptime = datetime.strptime; dt.side_effect = lambda *a, **k: datetime(*a, **k)
        assert api.domestic_session_phase() == phase


def test_수능일_16시는_휴게가_아니고_16시30분이_휴게다():
    assert api.domestic_break_window(_at("1600")) is False
    assert api.domestic_break_window(_at("1630")) is True
    assert api.domestic_break_window(_at("1530", "20261118")) is True


def test_수능일_15시30분에는_매매_감시가_멈추지_않는다(monkeypatch, default_hours):
    monkeypatch.setattr(api, "is_holiday_today", lambda: False)
    seen = {}
    for hhmm in ("0930", "1000", "1545", "1615", "1625", "1635"):
        with patch("modules.auto_trade.common.datetime") as dt:
            dt.now.return_value = _at(hhmm)
            seen[hhmm] = common.is_system_market_open()
    assert seen == {"0930": False, "1000": True, "1545": True, "1615": True,
                    "1625": False,      # 종가 단일가(16:20~16:30)
                    "1635": False}


def test_수능일_개장_직후_보류는_10시부터_센다(default_hours):
    assert common.entry_open_delay_remaining(_at("0945")) == 0      # 개장 전 — 매매 자체가 닫혀 있다
    assert common.entry_open_delay_remaining(_at("1015")) == 15 * 60
    assert common.entry_open_delay_remaining(_at("1031")) == 0


def test_수능일_확정_종가_기준선은_16시40분이다(monkeypatch):
    monkeypatch.setattr(api, "market_today", lambda overseas=False: SUNEUNG)
    with patch("api.sessions.datetime") as dt:
        dt.now.return_value = _at("1600"); dt.strptime = datetime.strptime
        assert api._krx_close_passed_at() is None
        dt.now.return_value = _at("1641")
        assert api._krx_close_passed_at() == _at("1640")


def test_기동_점검은_올해_수능일이_없으면_경고한다(monkeypatch):
    with patch("api.sessions.datetime") as dt:
        dt.now.return_value = datetime(2027, 11, 1)
        ok, msg = api.krx_session_status_text()
    assert ok is False and "2027" in msg
    with patch("api.sessions.datetime") as dt:
        dt.now.return_value = datetime(2026, 11, 1)
        ok, msg = api.krx_session_status_text()
    assert ok is True and SUNEUNG in msg


def test_시간대가_KST_가_아니면_자동매매_시간_게이트가_닫힌다(monkeypatch):
    from datetime import timezone, timedelta as td
    assert api.local_tz_is_kst(datetime(2026, 9, 21, 10, tzinfo=timezone(td(hours=9))))
    assert not api.local_tz_is_kst(datetime(2026, 9, 21, 10, tzinfo=timezone.utc))
    monkeypatch.setattr(api, "is_holiday_today", lambda: False)
    monkeypatch.setattr(api, "local_tz_is_kst", lambda now=None: False)
    with patch("modules.auto_trade.common.datetime") as dt:
        dt.now.return_value = _at("1100", "20261118")
        assert common.is_system_market_open() is False
    monkeypatch.setattr(api, "local_tz_is_kst", lambda now=None: True)
    with patch("modules.auto_trade.common.datetime") as dt:
        dt.now.return_value = _at("1100", "20261118")
        assert common.is_system_market_open() is True
