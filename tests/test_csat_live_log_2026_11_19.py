"""2026-11-19 수능일 파이(가상투자) 로그에서 드러난 세 자리 — 2026-11-23.

[1] 단일가 휴게 판정(is_single_price_break)이 리터럴 경계라 수능일을 몰랐다. 16:20(밀린 종가 단일가)에
    거래 시간 판정은 닫혔는데 '휴게 아님'이라 답해, 루프가 '거래 종료'로 읽고 종가 전에 마감 알림·AI 브리핑을 냈다.
[2] AutoTrader() 를 부를 때마다 initialized·last_session_phase 가 지워졌다(생성 블록 밖 대입). 가상투자에서는
    체결 감시가 60초마다 AutoTrader() 를 불러 세션 전환 알림이 들쭉날쭉 빠졌고, 수능일 아침 두 번이 모두 빠졌다.
[3] 휴게 라벨이 '15:30~16:00' 고정이라 수능일 16:30 에도 그 시각이 찍혔다.
"""
from datetime import datetime as _real_dt

import pytest

import api
import config
from modules.auto_trade import AutoTrader, common


@pytest.fixture
def csat(monkeypatch):
    monkeypatch.setattr(config, "KRX_SESSION_SHIFT_DAYS", {"20261119": (60, 60)}, raising=False)
    monkeypatch.setattr(api, "is_holiday_today", lambda: False)
    api.sessions._SHIFT_CACHE.clear()
    yield
    api.sessions._SHIFT_CACHE.clear()


def _at(day, hm):
    return _real_dt(int(day[:4]), int(day[4:6]), int(day[6:]), int(hm[:2]), int(hm[2:]), 30)


@pytest.mark.parametrize("hm,expected", [
    ("0850", False), ("0859", False),     # 보통날의 시가 단일가 자리 — 그날은 아직 장 전
    ("0950", True), ("0959", True),       # 그날의 시가 단일가
    ("1000", False),
    ("1520", False), ("1529", False),     # 보통날의 종가 단일가 자리 — 그날은 연속매매
    ("1620", True), ("1629", True),       # 그날의 종가 단일가 (11-19 실측: 여기서 '거래 종료'로 읽혔다)
    ("1630", False),
])
def test_single_price_break_follows_csat_shift(csat, hm, expected):
    assert common.is_single_price_break(_at("20261119", hm)) is expected


@pytest.mark.parametrize("hm,expected", [
    ("0849", False), ("0850", True), ("0859", True), ("0900", False),
    ("1519", False), ("1520", True), ("1529", True), ("1530", False),
])
def test_single_price_break_unchanged_on_ordinary_day(csat, hm, expected):
    assert common.is_single_price_break(_at("20261118", hm)) is expected


def test_break_and_market_open_agree_at_csat_closing_auction(csat, monkeypatch):
    """거래 시간이 닫히는 순간(16:20)을 휴게로 읽어야 루프가 '거래 종료'·마감 브리핑으로 가지 않는다."""
    class _Fixed(_real_dt):
        @classmethod
        def now(cls, tz=None):
            return _at("20261119", "1620")
    monkeypatch.setattr(common, "datetime", _Fixed)
    monkeypatch.setattr(config, "SYSTEM_TRADING_END_TIME", "2000", raising=False)
    monkeypatch.setattr(api, "local_tz_is_kst", lambda now=None: True)
    assert common.is_system_market_open() is False
    assert common.is_single_price_break() is True


def test_autotrader_call_does_not_reset_running_instance_state():
    """두 번째 AutoTrader() 는 같은 인스턴스를 돌려줄 뿐 상태를 지우지 않는다."""
    t = AutoTrader()
    t.last_session_phase = "krx"
    t.initialized = True
    again = AutoTrader()
    assert again is t
    assert t.last_session_phase == "krx"
    assert t.initialized is True


def test_fresh_autotrader_still_starts_uninitialized():
    AutoTrader._instance = None
    t = AutoTrader()
    assert t.initialized is False and t.last_session_phase is None


def test_break_label_shows_the_days_window(csat, monkeypatch):
    class _Fixed(_real_dt):
        NOW = _at("20261119", "1640")

        @classmethod
        def now(cls, tz=None):
            return cls.NOW
    monkeypatch.setattr(api.sessions, "datetime", _Fixed)
    assert api.market_session_label(False, False)[0] == "KRX 휴게(16:30~17:00) · 정규장 종가"
    _Fixed.NOW = _at("20261118", "1540")
    assert api.market_session_label(False, False)[0] == "KRX 휴게(15:30~16:00) · 정규장 종가"
