"""한은 금통위 일정은 시드 파일 수기 입력이 아니라 한은 연간 일정 페이지에서 자동 수집한다.

[2026-09-20] 종전 시드 파일 안내문은 '한은이 향후 일정을 구조화해 내주지 않는다'고 했지만 틀렸다 —
연간 일정 표(회의일자 열)에 그 해 남은 회의일까지 실려 있다(실측 09-20: 10/22·11/26 포함).
픽스처는 실제 페이지의 표를 회의일자 열만 남기고 줄인 것이다.
"""
import os
from datetime import date, datetime
from unittest.mock import patch

import pytest

from modules.manage import econ_events

FIXTURE = os.path.join(os.path.dirname(__file__), "fixtures", "bok_mpc_2026.html")


def _page():
    return open(FIXTURE, encoding="utf-8").read()


def test_parses_all_meeting_days_of_the_year():
    days = econ_events._parse_bok_html(_page(), 2026)
    assert days == [date(2026, 1, 15), date(2026, 2, 26), date(2026, 4, 10), date(2026, 5, 28),
                    date(2026, 7, 16), date(2026, 8, 27), date(2026, 10, 22), date(2026, 11, 26)]


def test_missing_table_is_none_not_empty():
    assert econ_events._parse_bok_html("<html><body>개편됨</body></html>", 2026) is None


class _Res:
    def __init__(self, text):
        self.text = text

    def raise_for_status(self):
        return None


def test_fetch_filters_to_the_window_and_marks_source():
    with patch.object(econ_events.requests, "get", return_value=_Res(_page())) as g:
        out, ok = econ_events._fetch_bok(date(2026, 9, 20), date(2026, 12, 31))
    assert ok
    assert [e["date"] for e in out] == ["2026-10-22", "2026-11-26"]
    assert all(e["source"] == "BOK" and e["country"] == "KR" and e["weight"] == 1
               and e["name"] == "한은 금통위" for e in out)
    assert g.call_args.kwargs["params"] == {"pYear": 2026}


def test_a_window_spanning_two_years_asks_for_both():
    """다음 해는 연말 공표 전이라 빈 표여도 실패가 아니다 — 올해 것만 나온다."""
    empty = "<table><tr><th>회의일자</th></tr></table>"
    calls = []

    def fake_get(url, params=None, **kw):
        calls.append(params["pYear"])
        return _Res(_page() if params["pYear"] == 2026 else empty)

    with patch.object(econ_events.requests, "get", side_effect=fake_get), \
         patch.object(econ_events, "datetime") as dt:
        dt.now.return_value = datetime(2026, 9, 20)
        out, ok = econ_events._fetch_bok(date(2026, 9, 20), date(2027, 3, 31))
    assert calls == [2026, 2027] and ok
    assert [e["date"] for e in out] == ["2026-10-22", "2026-11-26"]


def test_an_empty_table_for_the_current_year_is_a_failure():
    """올해 표가 비면 페이지 개편이다 — 빈 목록을 성공으로 돌리면 금통위가 조용히 사라진다."""
    empty = "<table><tr><th>회의일자</th></tr></table>"
    with patch.object(econ_events.requests, "get", return_value=_Res(empty)), \
         patch.object(econ_events, "datetime") as dt:
        dt.now.return_value = datetime(2026, 9, 20)
        with pytest.raises(ValueError):
            econ_events._fetch_bok(date(2026, 9, 20), date(2026, 12, 31))


def test_bok_is_wired_into_collect_and_counted_as_a_source():
    ev = [{"date": "2026-10-22", "name": "한은 금통위", "country": "KR", "weight": 1, "source": "BOK"}]
    with patch.object(econ_events, "_fetch_fred", return_value=([], True)), \
         patch.object(econ_events, "_fetch_fed", return_value=([], True)), \
         patch.object(econ_events, "_fetch_boj", return_value=([], True)), \
         patch.object(econ_events, "_fetch_bok", return_value=(ev, True)), \
         patch.object(econ_events, "_option_expiry", return_value=[]), \
         patch.object(econ_events, "_load_seed", return_value=[]):
        ticks = []
        out, complete, failed = econ_events._collect(date(2026, 9, 20), date(2026, 12, 31),
                                                     on_progress=lambda: ticks.append(1))
    assert out == ev and complete and not failed
    assert len(ticks) == econ_events._SOURCE_COUNT


def test_seed_help_no_longer_asks_for_manual_bok_entry():
    import json
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))     # 저장소의 실제 시드 파일
    seed = json.load(open(os.path.join(root, "json", "econ_calendar_seed.json"), encoding="utf-8"))
    help_text = " ".join(seed.get("_help", []))
    assert "자동 수집이 불가능" not in help_text
    assert "자동 수집" in help_text
