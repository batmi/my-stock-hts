"""메모 삭제: **지웠다·없었다·모른다는 서로 다른 답이다**.

[왜 이 파일이 있나 · 2026-09-07]
반환이 True/False 둘뿐이었고, 그 둘이 실제 의미와 어긋나 있었다(셋 다 실측 재현):

 · 없는 ID 를 지워도 SQL DELETE 는 **0행에 성공**하므로 True 가 돌아온다 →
   "🗑 메모(ID: 999999)가 삭제되었습니다." 없던 것을 지웠다고 답한다.
 · DB 오류면 False → "⚠️ 메모 삭제 실패. (존재하지 않는 ID)". ID 는 실재하는데
   엉뚱한 이유를 댄다 — 사용자는 없는 문제를 찾아 헤맨다.
 · 종목 단위 전체 삭제는 반환을 **아예 보지 않았다** → 메모가 그대로 남아 있는데도
   "모든 메모가 삭제되었습니다"(실측: 남은 메모 1건).

DELETE 가 0행에 성공하는 것은 SQL 의 정상 동작이라, 성공/실패 두 칸으로는 답을 담을
수 없다. 지운 행 수를 돌려주고 실패는 None(=모른다)으로 올린다 ([[unknown-vs-empty]]).
운영기는 램 1GB·SD 카드 라즈베리파이라 DB 쓰기 실패가 실재한다.
"""
import os
import tempfile

import pytest

import config
from core import utils


@pytest.fixture
def memo_db(monkeypatch):
    path = os.path.join(tempfile.mkdtemp(), "memo.db")
    monkeypatch.setattr(config, "DB_FILE_PATH", path)
    utils.init_memo_db()
    return path


@pytest.fixture
def broken_db(monkeypatch):
    monkeypatch.setattr(config, "DB_FILE_PATH", "/nonexistent-dir-for-tests/x.db")


# --------------------------------------------------------------------------
# 계약
# --------------------------------------------------------------------------
def test_지운_행이_있으면_그_수를_돌려준다(memo_db):
    utils.add_stock_memo("005930", "삼성전자", "메모1")
    utils.add_stock_memo("005930", "삼성전자", "메모2")

    assert utils.delete_all_stock_memos("005930") == 2
    assert utils.get_stock_memos("005930") == []


def test_없는_ID는_0이지_성공이_아니다(memo_db):
    assert utils.delete_stock_memo_by_id(999999) == 0


def test_없는_종목은_0이지_성공이_아니다(memo_db):
    assert utils.delete_all_stock_memos("999999") == 0


def test_DB_오류는_None이지_없음이_아니다(memo_db, broken_db):
    """0(없었다)과 None(모른다)이 같은 값이면 호출부가 둘을 가를 수 없다."""
    assert utils.delete_stock_memo_by_id(1) is None
    assert utils.delete_all_stock_memos("005930") is None


# --------------------------------------------------------------------------
# 호출부가 그 셋을 갈라 말하는가
# --------------------------------------------------------------------------
def _bot():
    from modules import telegram_bot
    bot = object.__new__(telegram_bot.TelegramCommander)
    bot._resolve_stock = lambda kw: ("005930", "삼성전자", None)
    return bot


def test_텔레그램_없는_ID에_삭제되었다고_답하지_않는다(memo_db):
    out = _bot()._cmd_memo(["d", "999999"])
    assert "삭제되었습니다" not in out
    assert "찾을 수 없" in out


def test_텔레그램_DB_오류를_존재하지_않는_ID로_설명하지_않는다(memo_db, broken_db):
    out = _bot()._cmd_memo(["d", "1"])
    assert "존재하지 않는" not in out
    assert "DB 오류" in out


def test_텔레그램_종목_전체삭제는_실패를_성공으로_답하지_않는다(memo_db, broken_db):
    out = _bot()._cmd_memo(["d", "삼성전자"])
    assert "모든 메모가 삭제" not in out
    assert "DB 오류" in out


def test_텔레그램_삭제_성공은_건수를_말한다(memo_db):
    utils.add_stock_memo("005930", "삼성전자", "메모1")
    out = _bot()._cmd_memo(["d", "삼성전자"])
    assert "1건" in out and "삭제" in out


# --------------------------------------------------------------------------
# 같은 계열: 만들지 못한 파일을 "새로 생성했습니다"라고 말하지 않는다
# --------------------------------------------------------------------------
#  2026-09-06 에 save_stock_config 가 성공 여부를 돌려주게 하고 호출부 여섯 곳을 고쳤다.
#  이 부트스트랩 자리가 남아 있었다 — 저장이 실패하면 바로 뒤의 load_stock_config() 가
#  없는 파일을 읽어 목록을 빈 채로 되돌리는데, 화면에는 생성 완료 문구가 뜬다.
def test_관심종목_파일을_만들지_못하면_생성했다고_말하지_않는다(monkeypatch, tmp_path, capsys):
    from unittest.mock import patch
    from modules import analysis
    import core.jsonio as jsonio

    monkeypatch.setattr(config, "STOCK_DATA_FILE", str(tmp_path / "없는파일.json"))
    monkeypatch.setattr(config.session, "stock_data",
                        {"stocks_kr": [], "etfs_kr": [], "stocks_us": [], "etfs_us": []})
    #  저장이 실패했다 — 파일은 만들어지지 않았다. 화면이 무엇이라 말하는가만 본다.
    monkeypatch.setattr(config.session, "save_stock_config", lambda data: False)
    with patch("rich.prompt.Prompt.ask", side_effect=["1", "q"]):
        analysis.show_stock_analysis()

    out = capsys.readouterr().out
    assert "새로 생성했습니다" not in out
    assert "만들지 못했습니다" in out


def test_종목_삭제_후_메모_DB가_죽어도_삭제되었다고_말하지_않는다(tmp_path, monkeypatch):
    """관심종목 삭제는 성공했지만 메모 DB 쓰기가 실패한 경우다.

    같은 화면에 '삭제되었습니다'가 두 번 뜨는데 하나는 참, 하나는 거짓이었다.
    """
    from core import jsonio
    from modules.manage import watchlist

    path = tmp_path / "stock.json"
    monkeypatch.setattr(config, "STOCK_DATA_FILE", str(path), raising=False)
    jsonio.save_json(str(path), {
        "stocks_kr": [{"name": "삼성전자", "code": "005930", "exchange": "KOSPI"}],
        "etfs_kr": [], "stocks_us": [], "etfs_us": []})
    config.session.load_stock_config()

    printed = []
    monkeypatch.setattr(watchlist.utils, "show_menu", lambda *a, **k: "1")
    monkeypatch.setattr(watchlist.utils, "get_memo_codes", lambda: ["005930"])
    monkeypatch.setattr(watchlist.utils, "search_stock_in_list",
                        lambda lst, **k: (0, dict(lst[0])))
    monkeypatch.setattr(watchlist.utils, "print_breadcrumb", lambda *a, **k: None)
    monkeypatch.setattr(watchlist.Prompt, "ask", lambda *a, **k: "y")
    monkeypatch.setattr(watchlist.utils, "delete_all_stock_memos", lambda code: None)
    monkeypatch.setattr(config.console, "print",
                        lambda *a, **k: printed.append(str(a[0]) if a else ""))

    watchlist.delete_stock()

    body = "\n".join(printed)
    assert "관련 메모가 모두 삭제되었습니다" not in body, body
    assert "메모 DB 오류" in body
    # 종목 삭제 자체는 성공했으므로 그 안내는 그대로 나가야 한다.
    assert "삭제되었습니다" in body
