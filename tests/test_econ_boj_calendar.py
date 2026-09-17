"""BOJ 금융정책결정회의(MPM) 일정 — 투자 캘린더에 일본은행을 더한다(2026-09-17).

소스는 boj.or.jp 영문 페이지의 연도별 표(caption 'Table : 2026'). 첫 칸이 회의 이틀
('Sept. 17 (Thurs.), 18 (Fri.)'), 둘째 칸이 전망보고서 발표일(없으면 '-'). 결정·발표는
회의 **둘째 날**이므로 그 날을 적는다. 표를 하나도 못 읽으면 실패로 올린다 — 빈 목록을
성공으로 돌리면 페이지 개편 때 BOJ 가 조용히 사라진다([[unknown-vs-empty]]).
"""
from datetime import date
from unittest.mock import patch

from modules.manage import econ_events as E

PAGE = """
<table><caption>Table : 2026</caption>
<tr><th>Date of MPM</th><th>Release Schedule</th></tr>
<tr><th>Outlook Report (The Bank's View)</th><th>Summary of Opinions</th></tr>
<tr><td>Jan. 22 (Thurs.), 23 (Fri.) <a>[PDF 171KB]</a></td><td>Jan. 23 (Fri.) [PDF 1,117KB]</td></tr>
<tr><td>June 15 (Mon.), 16 (Tues.)</td><td>-</td></tr>
<tr><td>Sept. 17 (Thurs.), 18 (Fri.)</td><td>-</td></tr>
<tr><td>Oct. 29 (Thurs.), 30 (Fri.)</td><td>Oct. 30 (Fri.)</td></tr>
<tr><td>Dec. 17 (Thurs.), 18 (Fri.)</td><td>-</td></tr>
</table>
<table><caption>Table : 2027</caption>
<tr><th>Date of MPM</th><th>Release Schedule</th></tr>
<tr><td>Jan. 21 (Thurs.), 22 (Fri.)</td><td>Jan. 22 (Fri.)</td></tr>
</table>
"""


def test_결정일은_회의_둘째_날이다():
    got = dict(E._parse_boj_html(PAGE))
    assert date(2026, 1, 23) in got and date(2026, 1, 22) not in got
    assert date(2026, 9, 18) in got
    assert date(2027, 1, 22) in got, "두 번째 연도 표도 읽어야 한다"


def test_전망보고서_회의를_구분한다():
    got = dict(E._parse_boj_html(PAGE))
    assert got[date(2026, 1, 23)] is True and got[date(2026, 10, 30)] is True
    assert got[date(2026, 6, 16)] is False and got[date(2026, 9, 18)] is False


def test_머리행은_일정으로_읽지_않는다():
    days = [d for d, _ in E._parse_boj_html(PAGE)]
    assert len(days) == 6 and len(set(days)) == 6


def test_달을_넘기는_회의도_읽는다():
    assert E._boj_meeting_days(2026, "Oct. 31 (Thurs.), Nov. 1 (Fri.)") == [date(2026, 10, 31), date(2026, 11, 1)]
    assert E._boj_meeting_days(2026, "June 15 (Mon.), 16 (Tues.)") == [date(2026, 6, 15), date(2026, 6, 16)]
    assert E._boj_meeting_days(2026, "Date of MPM") == []


class _Res:
    def __init__(self, text): self.text = text
    def raise_for_status(self): pass


def test_fetch_는_기간_안의_결정일만_이벤트로_만든다():
    with patch.object(E.requests, "get", return_value=_Res(PAGE)):
        out, ok = E._fetch_boj(date(2026, 9, 17), date(2026, 12, 31))
    assert ok
    assert [e["date"] for e in out] == ["2026-09-18", "2026-10-30", "2026-12-18"]
    assert out[1]["name"] == "BOJ 금리결정 (전망보고서)" and out[0]["name"] == "BOJ 금리결정"
    assert all(e["source"] == "BOJ" and e["country"] == "JP" and e["weight"] == 1 for e in out)


def test_표를_못_읽으면_실패를_올린다():
    """빈 목록을 성공으로 돌리면 페이지 개편 때 BOJ 가 조용히 사라진다."""
    import pytest
    with patch.object(E.requests, "get", return_value=_Res("<html><p>redesigned</p></html>")):
        with pytest.raises(ValueError):
            E._fetch_boj(date(2026, 1, 1), date(2026, 12, 31))


def test_collect_이_BOJ_를_네트워크_소스로_센다():
    """진행 막대 총계(_SOURCE_COUNT)는 on_progress 호출 수와 같아야 한다 — 어긋나면 막대가 100%에 안 닿거나 넘친다."""
    calls = []
    with patch.object(E, "_fetch_fred", return_value=([], True)), \
         patch.object(E, "_fetch_fed", return_value=([], True)), \
         patch.object(E, "_fetch_boj", return_value=([{"date": "2026-09-18", "name": "BOJ 금리결정",
                                                       "country": "JP", "weight": 1, "source": "BOJ"}], True)), \
         patch.object(E, "_option_expiry", return_value=[]), \
         patch.object(E, "_load_seed", return_value=[]):
        out, complete = E._collect(date(2026, 9, 17), date(2026, 10, 31), on_progress=lambda: calls.append(1))
    assert complete and [e["source"] for e in out] == ["BOJ"]
    assert len(calls) == E._SOURCE_COUNT


def _boom(start, end):
    raise ValueError("down")


def test_BOJ_실패는_불완전으로_표시된다():
    with patch.object(E, "_fetch_fred", return_value=([], True)), \
         patch.object(E, "_fetch_fed", return_value=([], True)), \
         patch.object(E, "_fetch_boj", _boom), \
         patch.object(E, "_option_expiry", return_value=[]), \
         patch.object(E, "_load_seed", return_value=[]):
        _out, complete = E._collect(date(2026, 9, 17), date(2026, 10, 31))
    assert complete is False, "BOJ 가 빠졌는데 '완전'이라고 하면 화면이 누락을 말하지 않는다"
