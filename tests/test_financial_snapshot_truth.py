"""재무 스냅샷 — 못 읽은 칸과 값이 없는 칸을 같은 글자로 찍지 않는다.

[왜 · 2026-09-08] 이 화면은 종목 하나가 통째로 실패하는 것은 이미 ScanFailures 로
 밝히고 있었다. 그런데 종목은 살아 있고 **칸만 못 읽은** 경우 — 단독분기·ROE·부채비율
 조회가 `except Exception` 안에서 통째로 삼켜져 '-' 가 됐다. DART 는 분기 지표를 아예
 주지 않는 회사가 흔해 '-' 가 일상이므로, 못 읽은 칸이 그 사이에 완전히 숨는다.
 관심종목 40여 개가 8워커로 동시에 도는 화면이라 한도 초과는 상시 조건이다.

 그리고 단독분기 영업이익은 두 보고서를 빼서 만든다. 두 보고서의 연결/개별 기준이
 다르면 그 뺄셈은 의미가 없는데, 종전에는 기준을 보지 않고 뺐다 — 값이 비는 것보다
 **틀린 값이 확신에 차서 찍히는 쪽**이 나쁘다.
"""
from unittest.mock import patch

import pytest

from modules.manage import financials as F


class DartQueryError(Exception):
    """call_dart 가 실패에 던지는 예외의 대역(조회 실패 = 예외라는 계약만 쓴다)."""


def _row(fs, nm, cur, prev):
    return {"fs_div": fs, "sj_div": "IS", "account_nm": nm,
            "thstrm_amount": str(cur), "frmtrm_amount": str(prev), "thstrm_nm": "제56기"}


def _is(fs, op_cur=1000, op_prev=900):
    return [_row(fs, "매출액", op_cur * 10, op_prev * 10),
            _row(fs, "영업이익", op_cur, op_prev),
            _row(fs, "당기순이익", op_cur // 2, op_prev // 2)]


# ---------------------------------------------------------------------------
# 연결 − 개별을 빼지 않는다
# ---------------------------------------------------------------------------

def test_a_standalone_quarter_is_not_computed_across_different_statement_bases():
    """당기=연결 / 직전누적=개별이면 단독분기는 계산하지 않는다."""
    def fin(code, year, reprt):
        return _is("CFS", 1000, 900) if reprt == "11012" else _is("OFS", 200, 180)

    with patch("api.get_dart_financials", side_effect=fin), \
         patch("api.get_dart_financial_index", return_value=[]):
        r = F._collect("005930", "삼성", [(2026, "11012")])

    assert r["basis"].endswith("연결")
    assert r["op_q"] is None, f"연결누적 − 개별누적을 빼서 {r['op_q']} 를 만들었다"


def test_a_standalone_quarter_is_still_computed_when_both_are_consolidated():
    """게이트가 정상 계산까지 막아 버리면 안 된다."""
    def fin(code, year, reprt):
        return _is("CFS", 1000, 900) if reprt == "11012" else _is("CFS", 200, 180)

    with patch("api.get_dart_financials", side_effect=fin), \
         patch("api.get_dart_financial_index", return_value=[]):
        r = F._collect("005930", "삼성", [(2026, "11012")])

    assert r["op_q"] == (800.0, 720.0)


# ---------------------------------------------------------------------------
# 못 읽은 칸은 '?' 로 남는다
# ---------------------------------------------------------------------------

def _collect_with_index_failure():
    def fin(code, year, reprt):
        return _is("CFS")

    def idx(code, year, reprt, cl):
        raise DartQueryError("fnlttSinglIndx: 응답 코드 020 (사용한도를 초과하였습니다)")

    with patch("api.get_dart_financials", side_effect=fin), \
         patch("api.get_dart_financial_index", side_effect=idx):
        return F._collect("005930", "삼성", [(2026, "11012")])


def test_an_index_query_failure_is_recorded_not_swallowed():
    r = _collect_with_index_failure()
    assert r is not None, "종목 전체를 잃어서는 안 된다 — 손익 본체는 읽혔다"
    assert r["roe"] is None and r["debt"] is None
    assert any(u.startswith("지표") for u in r["unread"]), r.get("unread")
    assert "020" in " ".join(r["unread"]), "무엇이 실패했는지 원인이 남아야 한다"


def test_a_standalone_query_failure_is_recorded_separately():
    """단독분기 조회 실패는 지표 실패와 따로 센다(칸이 다르므로)."""
    calls = {"n": 0}

    def fin(code, year, reprt):
        if reprt == "11013":        # 직전 누적 조회
            raise DartQueryError("fnlttSinglAcnt: 응답 코드 020")
        calls["n"] += 1
        return _is("CFS")

    with patch("api.get_dart_financials", side_effect=fin), \
         patch("api.get_dart_financial_index", return_value=[]):
        r = F._collect("005930", "삼성", [(2026, "11012")])

    assert r["op_q"] is None
    assert [u.split("(")[0] for u in r["unread"]] == ["단독Q"], r["unread"]


def test_a_company_that_simply_has_no_index_leaves_no_unread_mark():
    """DART 가 지표를 안 주는 것은 실패가 아니다 — '?' 가 붙으면 안 된다."""
    with patch("api.get_dart_financials", side_effect=lambda *a: _is("CFS")), \
         patch("api.get_dart_financial_index", return_value=[]):
        r = F._collect("005930", "삼성", [(2026, "11012")])

    assert r["roe"] is None and r["debt"] is None
    assert r["unread"] == [], r["unread"]


# ---------------------------------------------------------------------------
# 화면이 그 구분을 실제로 보여 준다
# ---------------------------------------------------------------------------

def _render(collected):
    """show_financial_snapshot 을 태우고 화면에 찍힌 글자를 모은다."""
    import config
    printed = []

    class _Console:
        def print(self, *a, **k):
            printed.append(" ".join(str(x) for x in a))

        def __getattr__(self, _n):
            return lambda *a, **k: None

    class _Prog:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def add_task(self, *a, **k):
            return 1

        def advance(self, *a, **k):
            pass

    with patch.object(config, "console", _Console()), \
         patch.object(config, "DART_API_KEY", "x"), \
         patch.object(F, "Progress", lambda *a, **k: _Prog()), \
         patch.object(F, "_kr_stocks", return_value=[("005930", "삼성")]), \
         patch.object(F, "_collect", side_effect=lambda c, n, cand: collected), \
         patch.object(F.utils, "clear_screen", lambda: None):
        F.show_financial_snapshot()
    # rich Table 은 console.print(table) 로 넘어가므로 셀 문자열은 표 객체에서 읽는다
    tables = [p for p in printed if "Table" in p or "rich.table" in p]
    return printed, tables


def test_the_screen_says_some_cells_could_not_be_read():
    r = _collect_with_index_failure()
    printed, _ = _render(r)
    body = "\n".join(printed)
    assert "조회하지 못했습니다" in body, body
    assert "'?'" in body


def test_the_screen_stays_quiet_when_nothing_failed():
    with patch("api.get_dart_financials", side_effect=lambda *a: _is("CFS")), \
         patch("api.get_dart_financial_index", return_value=[]):
        r = F._collect("005930", "삼성", [(2026, "11012")])
    printed, _ = _render(r)
    assert "조회하지 못했습니다" not in "\n".join(printed)
