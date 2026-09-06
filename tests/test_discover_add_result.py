"""탐색 추가(메뉴 7-5): **성공 경로가 실제로 끝까지 간다**.

[왜 이 파일이 있나 · 2026-09-07]
2026-09-06 에 '저장을 확인한 뒤에만 성공으로 본다'를 넣으면서 저장·안내 부분을
_commit_additions 로 떼어냈다. 그런데 성공 문구를 찍는 두 줄이 원래 자리에 남았고,
그 줄이 쓰는 `added` 는 **떼어낸 함수의 지역 변수**였다. 그때부터 메뉴 7-5 의 성공
경로는 NameError 로 죽는다(실측: name 'added' is not defined).

가장 나쁜 모양이다 — 종목은 이미 저장된 **뒤에** 죽는다. 사람은 실패로 보고 같은
작업을 다시 돌리고, 두 번째 실행은 "이미 있다"로 0종목을 넣은 뒤 또 죽는다.
기존 테스트는 _commit_additions 를 직접 부르는 단위 테스트라 이 이음매를 지나가지
않았다. 이 파일은 함수를 거치지 않고 **화면이 도달하는 경로 그대로** 확인한다.

계약도 셋으로 갈랐다: 넣은 수(0 포함) / None(저장 실패). 0 을 실패로 읽으면
'고른 것이 전부 이미 있었다'가 오류로 둔갑한다.
"""
from unittest.mock import patch

import pytest

import config
from modules.manage import discover


@pytest.fixture
def empty_watchlist(monkeypatch):
    monkeypatch.setattr(config.session, "stock_data",
                        {"stocks_kr": [], "etfs_kr": [], "stocks_us": [], "etfs_us": []})


def _run(monkeypatch, commit_result, picked=None):
    """탐색 화면을 조회부터 확정까지 한 번 흘린다."""
    picked = picked if picked is not None else [
        {"name": "삼성전자", "code": "005930", "exchange": "KOSPI"}]
    printed = []
    answers = iter(["4", "500", "y", "1", "y"])   # 목표수 · 풀 · 지주제외 · 선택 · 진행

    monkeypatch.setattr(discover, "_fetch_candidates",
                        lambda *a, **k: ([], [], {}, 100, len(picked)))
    monkeypatch.setattr(discover, "_verify_data", lambda *a, **k: (list(picked), 0))
    monkeypatch.setattr(discover, "_render_result", lambda *a, **k: None)
    monkeypatch.setattr(discover, "_print_rules", lambda *a, **k: None)
    monkeypatch.setattr(discover, "_print_prompt_help", lambda *a, **k: None)
    monkeypatch.setattr(discover, "_commit_additions", lambda *a, **k: commit_result)
    monkeypatch.setattr(discover.Prompt, "ask", lambda *a, **k: next(answers))
    monkeypatch.setattr(config.console, "print",
                        lambda *a, **k: printed.append(str(a[0]) if a else ""))
    return discover.discover_candidates(), "\n".join(printed)


def test_추가에_성공하면_죽지_않고_결과를_알린다(empty_watchlist, monkeypatch):
    result, body = _run(monkeypatch, commit_result=1)

    assert result is True
    assert "1종목을 추가했습니다" in body


def test_저장에_실패하면_추가했다고_말하지_않는다(empty_watchlist, monkeypatch):
    result, body = _run(monkeypatch, commit_result=None)

    assert result is False
    assert "추가했습니다" not in body


def test_전부_이미_있었으면_실패가_아니다(empty_watchlist, monkeypatch):
    """0 은 '넣을 것이 없었다'이지 오류가 아니다 — False 로 접으면 둘이 같아진다."""
    result, body = _run(monkeypatch, commit_result=0)

    assert result is True
    assert "이미 관심종목에 있습니다" in body
    assert "0종목을 추가했습니다" not in body


def test_넣은_수를_돌려준다(empty_watchlist, monkeypatch):
    """_commit_additions 의 계약 자체 — 이 값이 화면 문구의 근거다."""
    from core import jsonio

    monkeypatch.setattr(jsonio, "save_json", lambda *a, **k: True)
    monkeypatch.setattr(config.session, "load_stock_config", lambda: None)
    console = type("C", (), {"print": lambda self, *a, **k: None})()

    cands = [{"code": "035420", "name": "NAVER", "exchange": "KOSPI"},
             {"code": "000660", "name": "SK하이닉스", "exchange": "KOSPI"}]
    assert discover._commit_additions(cands, console=console) == 2
    # 두 번째 호출은 이미 다 들어 있으므로 0 — 실패가 아니다.
    assert discover._commit_additions(cands, console=console) == 0
