"""holidays 패키지 자동 갱신 — 운용자 cron 없이 앱이 7일마다 갱신하되, 바뀐 휴장일은 반드시 알린다.

[2026-09-20] 종전 결정(기동 때 조용히 업그레이드하지 않는다)은 지키고, 운용자 입력(주간 cron)만 없앤다:
갱신 전후 스냅샷을 새 인터프리터에서 받아 날짜 단위로 비교·알린다. pip·서브프로세스는 전부 가짜로 대체한다.
"""
import json
import os
from datetime import datetime, timedelta
from unittest.mock import patch

import pytest

import config
from modules import holiday_calendar_update as H


def _snap(version, kr, xnys=()):
    return {"version": version, "calendars": {"KR": sorted(kr), "XNYS": sorted(xnys)}}


def test_due_when_never_checked_or_older_than_a_week():
    assert H.is_due(state={}) is True
    now = datetime(2026, 9, 20)
    assert H.is_due(now=now, state={"last_check": "2026-09-13"}) is True
    assert H.is_due(now=now, state={"last_check": "2026-09-15"}) is False
    assert H.is_due(now=now, state={"last_check": "not-a-date"}) is True


def test_diff_lists_added_and_removed_dates_per_calendar():
    before = _snap("0.103", ["2026-10-03", "2026-10-09"], ["2026-11-26"])
    after = _snap("0.104", ["2026-10-03", "2026-10-09", "2026-10-10"], [])
    d = H.diff_snapshots(before, after)
    assert d == {"KR": {"added": ["2026-10-10"], "removed": []},
                 "XNYS": {"added": [], "removed": ["2026-11-26"]}}
    assert H.diff_snapshots(before, before) == {}


def test_run_reports_changes_and_notifies(monkeypatch):
    before = _snap("0.103", ["2026-10-03"])
    after = _snap("0.104", ["2026-10-03", "2026-10-10"])       # 임시공휴일이 새로 실렸다
    sent = []
    with patch.object(H, "_snapshot_in_fresh_interpreter", side_effect=[before, after]), \
         patch.object(H, "_pip_upgrade", return_value=(True, "")), \
         patch("api.send_telegram_message", side_effect=lambda m, **k: sent.append(m)):
        res = H.run_if_due(force=True, now=datetime(2026, 9, 20))
    assert res["ok"] and res["changes"]["KR"]["added"] == ["2026-10-10"]
    assert sent and "2026-10-10" in sent[0] and "다음 기동부터" in sent[0]
    state = json.load(open(H._state_file(), encoding="utf-8"))
    assert state["last_check"] == "2026-09-20" and state["version"] == "0.104"
    assert H.is_due(now=datetime(2026, 9, 25)) is False


def test_no_change_is_quiet(monkeypatch):
    same = _snap("0.103", ["2026-10-03"])
    sent = []
    with patch.object(H, "_snapshot_in_fresh_interpreter", side_effect=[same, dict(same)]), \
         patch.object(H, "_pip_upgrade", return_value=(True, "")), \
         patch("api.send_telegram_message", side_effect=lambda m, **k: sent.append(m)):
        res = H.run_if_due(force=True, now=datetime(2026, 9, 20))
    assert res["ok"] and res["changes"] == {} and sent == []


def test_pip_failure_does_not_stamp_the_week(monkeypatch):
    """실패한 주를 '점검 완료'로 찍으면 임시공휴일 반영이 한 주 더 미뤄진다 — 내일 다시 시도한다."""
    before = _snap("0.103", ["2026-10-03"])
    with patch.object(H, "_snapshot_in_fresh_interpreter", return_value=before), \
         patch.object(H, "_pip_upgrade", return_value=(False, "Could not fetch URL")):
        res = H.run_if_due(force=True, now=datetime(2026, 9, 20))
    assert res["ok"] is False and "Could not fetch URL" in res["message"]
    assert H.is_due(now=datetime(2026, 9, 21)) is True


def test_not_due_means_no_work():
    with patch.object(H, "_load_state", return_value={"last_check": datetime.now().strftime("%Y-%m-%d")}), \
         patch.object(H, "_snapshot_in_fresh_interpreter") as snap:
        assert H.run_if_due() is None
    snap.assert_not_called()


def test_snapshot_shape_covers_every_exchange_calendar():
    """실제 라이브러리로 한 번 — 달력 이름이 market_calendar 의 거래소 표와 같아야 비교가 의미 있다."""
    pytest.importorskip("holidays")
    from api import market_calendar
    snap = H.calendar_snapshot(horizon_days=30, today=datetime(2026, 9, 20).date())
    assert set(snap["calendars"]) == {"KR", "US", *market_calendar.EXCHANGE_CALENDARS}
    assert "2026-10-03" in snap["calendars"]["KR"]       # 개천절
    assert all(len(d) == 10 for ds in snap["calendars"].values() for d in ds)


def test_scheduler_and_startup_are_wired():
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    sched = open(os.path.join(root, "modules", "scheduler.py"), encoding="utf-8").read()
    main = open(os.path.join(root, "main.py"), encoding="utf-8").read()
    assert "_check_holidays_package" in sched and "holiday_calendar_update" in sched
    assert "holiday_calendar_update" in main and "start_background_check" in main
