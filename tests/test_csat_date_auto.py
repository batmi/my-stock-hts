"""수능일(특수 세션일)은 한국교육과정평가원(KICE) 메인 '주요 일정'에서 자동으로 받는다.

[2026-09-20] 종전에는 config.KRX_SESSION_SHIFT_DAYS 를 연 1회 손으로 채우라고 했다. 일정은 자동 수집이
규약이라([[schedules-auto-fetch-not-manual]]) 기동 점검이 KICE 를 조회해 json/krx_session_shift_auto.json 에
저장하고, 세션 판정은 저장분 ∪ 수기 표를 본다. 픽스처는 실제 페이지(09-20)의 주요 일정 블록이다 —
모의평가(6월·9월) 블록에도 '시험 실시'가 있어 '대학수학능력시험' dl 안에서만 읽어야 한다.
"""
import json
import os
from datetime import datetime
from unittest.mock import patch

import pytest

import api
import config

FIXTURE = os.path.join(os.path.dirname(__file__), "fixtures", "kice_main_schedule_2027.html")


def _page():
    return open(FIXTURE, encoding="utf-8").read()


def test_parses_the_csat_day_not_the_mock_exams():
    assert api.sessions._parse_kice_csat_date(_page()) == "20261119"
    assert _page().count("시험 실시") >= 2, "픽스처에 모의평가 블록이 없으면 이 테스트는 구분을 검증하지 못한다"


def test_redesigned_page_gives_none():
    assert api.sessions._parse_kice_csat_date("<html><dl><dt>대학수학능력시험</dt><dd>없음</dd></dl></html>") is None


class _Res:
    def __init__(self, text):
        self.text = text

    def raise_for_status(self):
        return None


def test_refresh_saves_the_day_and_the_lookup_sees_it(monkeypatch):
    monkeypatch.setattr(config, "KRX_SESSION_SHIFT_DAYS", {}, raising=False)
    api.sessions._SHIFT_CACHE.clear()
    assert api.krx_session_shift("20261119") == (0, 0)
    with patch("requests.get", return_value=_Res(_page())):
        ok, msg = api.sessions.refresh_session_shift_table()
    assert ok and "20261119" in msg
    saved = json.load(open(api.sessions._shift_auto_file(), encoding="utf-8"))
    assert saved["csat"] == {"2026": "20261119"} and saved["fetched"]
    assert api.krx_session_shift("20261119") == api.sessions.CSAT_SHIFT
    assert api.krx_hm("0900", "open", "20261119") == "1000"


def test_manual_table_overrides_the_auto_file(monkeypatch):
    with patch("requests.get", return_value=_Res(_page())):
        api.sessions.refresh_session_shift_table()
    monkeypatch.setattr(config, "KRX_SESSION_SHIFT_DAYS", {"20261119": (30, 30)}, raising=False)
    api.sessions._SHIFT_CACHE.clear()
    assert api.krx_session_shift("20261119") == (30, 30)


def test_fetch_failure_keeps_the_stored_day(monkeypatch):
    monkeypatch.setattr(config, "KRX_SESSION_SHIFT_DAYS", {}, raising=False)
    with patch("requests.get", return_value=_Res(_page())):
        api.sessions.refresh_session_shift_table()
    api.sessions._SHIFT_CACHE.clear()
    with patch("requests.get", side_effect=OSError("offline")):
        ok, msg = api.sessions.refresh_session_shift_table()
    assert ok is False and "20261119" in msg and "offline" in msg
    assert api.krx_session_shift("20261119") == api.sessions.CSAT_SHIFT


def test_startup_status_refreshes_then_reports(monkeypatch):
    monkeypatch.setattr(config, "KRX_SESSION_SHIFT_DAYS", {}, raising=False)
    api.sessions._SHIFT_CACHE.clear()
    with patch("requests.get", return_value=_Res(_page())), patch("api.sessions.datetime") as dt:
        dt.now.return_value = datetime(2026, 10, 5)
        ok, msg = api.krx_session_status_text()
    assert ok and "KICE" in msg and "20261119" in msg


def test_startup_warns_in_october_when_no_source_knows_this_year(monkeypatch):
    monkeypatch.setattr(config, "KRX_SESSION_SHIFT_DAYS", {}, raising=False)
    api.sessions._SHIFT_CACHE.clear()
    with patch("requests.get", side_effect=OSError("offline")), patch("api.sessions.datetime") as dt:
        dt.now.return_value = datetime(2026, 10, 5)
        ok, msg = api.krx_session_status_text()
    assert ok is False and "2026" in msg


def test_a_broken_auto_file_does_not_break_the_lookup(monkeypatch):
    monkeypatch.setattr(config, "KRX_SESSION_SHIFT_DAYS", {}, raising=False)
    api.sessions._SHIFT_CACHE.clear()
    open(api.sessions._shift_auto_file(), "w", encoding="utf-8").write("{not json")
    assert api.krx_session_shift("20261119") == (0, 0)
