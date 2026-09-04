"""DART 병렬 조회 실패를 '해당 없음'으로 읽게 두지 않는다.

2026-09-04 감사: 공시(6-6)·수급/오버행(6-7)·배당실적 캘린더(6-5) 세 화면이 모두
`ThreadPoolExecutor` + `as_completed` 로 종목을 병렬 조회하면서 워커 예외를
`except Exception: pass` 로 버렸다. DART 레이트리밋·네트워크 순단으로 절반이 실패해도
표는 멀쩡해 보이고 운영자는 '해당 없음'으로 읽는다.

오버행(잠재 매도물량)·자기주식 취득은 '없다'로 읽는 순간 판단이 반대로 간다.
같은 파일의 `rem_estimated` 가 이미 '모르면 위험 쪽'을 택하고 있는데, 그보다 앞단인
조회 실패는 통째로 조용했다.
"""
from unittest.mock import patch

import pytest

import config
from modules.manage import disclosure, events, financials, insider
from modules.manage.scan import ScanFailures


# --------------------------------------------------------------------------
# 수집기 자체
# --------------------------------------------------------------------------
def test_no_failures_says_nothing():
    f = ScanFailures("공시")
    assert not f
    assert f.note() is None
    assert f.telegram_note() is None
    assert f.announce() is False


def test_note_names_the_stocks_and_folds_the_rest():
    f = ScanFailures("공시")
    for i in range(8):
        f.record(f"00593{i}", RuntimeError("rate limit"))
    note = f.note()
    assert "8개 종목" in note
    assert "005930" in note and "005934" in note
    assert "외 3개" in note
    assert "'해당 없음'이 아닙니다" in note, "무엇을 오독하면 안 되는지 말해야 경고다"
    assert len(f) == 8


def test_telegram_note_has_no_markup():
    f = ScanFailures("캘린더")
    f.record("005930", OSError("boom"))
    assert "[" not in f.telegram_note()


def test_same_code_twice_counts_once():
    f = ScanFailures("공시")
    f.record("005930", OSError("a"))
    f.record("005930", OSError("b"))
    assert len(f) == 1


# --------------------------------------------------------------------------
# 공시 화면 · 텔레그램
# --------------------------------------------------------------------------
@pytest.fixture
def one_stock(monkeypatch):
    monkeypatch.setattr(config.session, 'stock_data',
                        {"stocks_kr": [{"code": "005930", "name": "삼성전자"}],
                         "etfs_kr": [], "stocks_us": [], "etfs_us": []}, raising=False)
    monkeypatch.setattr(config, 'DART_API_KEY', 'key', raising=False)


def test_gather_records_failures(one_stock):
    f = ScanFailures("공시")
    with patch.object(disclosure, 'collect_disclosures', side_effect=RuntimeError("429")):
        out = disclosure._gather([("005930", "삼성전자")], 14, 1, quiet=True, failures=f)
    assert out == []
    assert "005930" in f.failed


def test_gather_without_collector_still_works(one_stock):
    """수집기를 안 주면 종전대로 조용히 넘어간다(옛 호출부 호환)."""
    with patch.object(disclosure, 'collect_disclosures', side_effect=RuntimeError("429")):
        assert disclosure._gather([("005930", "삼성전자")], 14, 1, quiet=True) == []


def test_disclosure_telegram_says_it_could_not_check(one_stock):
    with patch.object(disclosure, 'collect_disclosures', side_effect=RuntimeError("429")):
        msg = disclosure.build_telegram_message(days=14)
    assert "조회하지 못했습니다" in msg, msg
    assert "005930" in msg


def test_disclosure_screen_announces_failures(one_stock, capsys):
    with patch.object(disclosure, 'collect_disclosures', side_effect=RuntimeError("429")), \
         patch.object(disclosure.utils, 'clear_screen', lambda: None):
        disclosure.show_disclosures(days=14)
    out = capsys.readouterr().out
    assert "조회하지 못했습니다" in out
    assert "최근 14일간 주요 공시가 없습니다" not in out, (
        "조회를 못 했는데 '공시가 없습니다'라고 하면 안 된다")


# --------------------------------------------------------------------------
# 수급 · 물량 화면
# --------------------------------------------------------------------------
def test_insider_screen_announces_failures(one_stock, capsys):
    with patch.object(insider, '_collect', side_effect=RuntimeError("429")), \
         patch.object(insider, '_collect_supply', side_effect=RuntimeError("429")), \
         patch.object(insider.utils, 'clear_screen', lambda: None):
        insider.show_insider_trades(days=90)
    out = capsys.readouterr().out
    assert "조회하지 못했습니다" in out
    assert "수급·물량 관련 보고가 없습니다" not in out, (
        "오버행·자기주식을 '없다'로 읽으면 판단이 반대로 간다")


# --------------------------------------------------------------------------
# 캘린더
# --------------------------------------------------------------------------
def test_calendar_collect_records_failures(one_stock):
    f = ScanFailures("캘린더")
    with patch.object(events, '_collect_kr', side_effect=RuntimeError("boom")), \
         patch.object(events, '_collect_kr_earnings_est', side_effect=RuntimeError("boom")):
        evs, rows = events._collect_watchlist_events([("005930", "삼성전자")], [], failures=f)
    assert evs == [] and rows == []
    assert "005930" in f.failed


def test_calendar_telegram_says_it_could_not_check(one_stock):
    with patch.object(events, '_collect_kr', side_effect=RuntimeError("boom")), \
         patch.object(events, '_collect_kr_earnings_est', side_effect=RuntimeError("boom")), \
         patch('modules.manage.econ_events.build_lines', return_value=["(경제 일정 없음)"]):
        msg = events.build_telegram_message(days=30)
    assert "조회하지 못했습니다" in msg, msg


# --------------------------------------------------------------------------
# 재무 스냅샷
# --------------------------------------------------------------------------
def test_financials_screen_announces_failures(one_stock, capsys):
    with patch.object(financials, '_collect', side_effect=RuntimeError("429")), \
         patch.object(financials.utils, 'clear_screen', lambda: None):
        financials.show_financial_snapshot()
    out = capsys.readouterr().out
    assert "조회하지 못했습니다" in out
    assert "조회된 재무 정보가 없습니다" not in out


def test_financials_says_how_many_are_missing(one_stock, capsys, monkeypatch):
    """보고서 미제출로 빠진 줄도 밝힌다 — 몇 줄이 정상인지 알 수 없으면 표가 거짓말한다."""
    monkeypatch.setattr(config.session, 'stock_data',
                        {"stocks_kr": [{"code": "005930", "name": "삼성전자"},
                                       {"code": "000660", "name": "SK하이닉스"}],
                         "etfs_kr": [], "stocks_us": [], "etfs_us": []}, raising=False)
    row = {"code": "005930", "name": "삼성전자", "basis": "2026 반기·연결",
           "rev": (1e12, 9e11), "op": (1e11, 9e10), "net": (8e10, 7e10),
           "op_q": None, "roe": 12.0, "debt": 40.0}
    with patch.object(financials, '_collect', side_effect=lambda c, n, cand: row if c == "005930" else None), \
         patch.object(financials.utils, 'clear_screen', lambda: None):
        financials.show_financial_snapshot()
    out = capsys.readouterr().out
    assert "2개 중 1개 표시" in out


# --------------------------------------------------------------------------
# 가드
# --------------------------------------------------------------------------
def test_no_silent_swallow_in_parallel_gathers():
    """`as_completed` 루프에서 예외를 통째로 버리는 형태가 다시 생기지 않게 막는다."""
    import ast
    import pathlib

    root = pathlib.Path(__file__).resolve().parent.parent
    offenders = []
    for rel in ("modules/manage/disclosure.py", "modules/manage/insider.py",
                "modules/manage/events.py", "modules/manage/financials.py"):
        tree = ast.parse((root / rel).read_text(encoding='utf-8'))
        for fn in ast.walk(tree):
            if not isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            if not any(isinstance(n, ast.Name) and n.id == 'as_completed'
                       or isinstance(n, ast.Attribute) and n.attr == 'as_completed'
                       for n in ast.walk(fn)):
                continue
            for h in ast.walk(fn):
                if isinstance(h, ast.ExceptHandler) and h.name is None \
                        and len(h.body) == 1 and isinstance(h.body[0], ast.Pass):
                    offenders.append(f"{rel}:{h.lineno} ({fn.name})")
    assert not offenders, (
        "병렬 조회 실패를 조용히 버린다 — 화면이 '해당 없음'으로 읽힌다: "
        + ", ".join(offenders))
