"""관심종목 저장이 실패했는데 '되었습니다'라고 말하지 않는가 (감사 2026-09-06, 배치 54).

[무엇이 걸려 있는가] 관심종목은 자동매매의 **진입 유니버스**다. 운영자가 어떤 종목을
빼는 것은 "이 종목은 더 사지 마라"는 지시이고, 넣는 것은 그 반대다.

종전에는 save_stock_config 가 실패를 화면에 한 줄 찍고 **반환값이 없었다**. 호출부
여섯 곳은 그 결과와 무관하게 초록색 성공 문구를 이어 붙였다. 게다가 저장 직후의
load_stock_config() 가 파일을 다시 읽어 옛 목록을 되살리므로, 지운 종목은 **그대로
매매 대상에 남는다**. 탐색 메뉴는 한술 더 떠 "다음 감시 주기부터 반영됩니다"라고
약속한다 — 지킬 수 없는 약속이다.

운영기는 램 1GB·SD 카드 라즈베리파이라 쓰기 실패가 실재한다([[deployment-raspberry-pi]]).
"""
from unittest.mock import patch

import pytest

import config
from core import jsonio


@pytest.fixture
def stock_file(tmp_path, monkeypatch):
    path = tmp_path / "stock.json"
    monkeypatch.setattr(config, 'STOCK_DATA_FILE', str(path), raising=False)
    data = {"stocks_kr": [{"name": "삼성전자", "code": "005930", "exchange": "KOSPI"},
                          {"name": "SK하이닉스", "code": "000660", "exchange": "KOSPI"}],
            "etfs_kr": [], "stocks_us": [], "etfs_us": []}
    jsonio.save_json(str(path), data)
    config.session.load_stock_config()
    yield path


def test_저장_성공은_True_실패는_False다(stock_file, monkeypatch):
    """호출부가 결과를 알 수 있어야 정직한 안내가 가능하다."""
    data = dict(config.session.stock_data)
    assert config.session.save_stock_config(data) is True

    monkeypatch.setattr(jsonio, 'save_json', lambda *a, **k: False)
    with patch.object(config.console, 'print'):
        assert config.session.save_stock_config(data) is False


def test_삭제_저장이_실패하면_삭제되었다고_말하지_않는다(stock_file, monkeypatch):
    """저장에 실패하면 그 종목은 목록에 그대로 남아 계속 매매 대상이다."""
    from modules.manage import watchlist

    printed = []
    monkeypatch.setattr(jsonio, 'save_json', lambda *a, **k: False)
    monkeypatch.setattr(watchlist.utils, 'show_menu', lambda *a, **k: "1")
    monkeypatch.setattr(watchlist.utils, 'get_memo_codes', lambda: [])
    monkeypatch.setattr(watchlist.utils, 'search_stock_in_list',
                        lambda lst, **k: (0, dict(lst[0])))
    monkeypatch.setattr(watchlist.utils, 'print_breadcrumb', lambda *a, **k: None)
    monkeypatch.setattr(watchlist.Prompt, 'ask', lambda *a, **k: "y")
    monkeypatch.setattr(config.console, 'print', lambda *a, **k: printed.append(str(a[0]) if a else ""))

    watchlist.delete_stock()

    body = "\n".join(printed)
    assert "삭제되었습니다" not in body, f"저장 실패인데 삭제됐다고 알렸다:\n{body}"
    assert "삭제하지 못했습니다" in body

    # 그리고 실제로 남아 있다.
    codes = [i['code'] for i in config.session.stock_data['stocks_kr']]
    assert '005930' in codes


def test_삭제_저장이_성공하면_정상_안내한다(stock_file, monkeypatch):
    """정직해지느라 정상 경로를 막으면 안 된다."""
    from modules.manage import watchlist

    printed = []
    monkeypatch.setattr(watchlist.utils, 'show_menu', lambda *a, **k: "1")
    monkeypatch.setattr(watchlist.utils, 'get_memo_codes', lambda: [])
    monkeypatch.setattr(watchlist.utils, 'search_stock_in_list',
                        lambda lst, **k: (0, dict(lst[0])))
    monkeypatch.setattr(watchlist.utils, 'print_breadcrumb', lambda *a, **k: None)
    monkeypatch.setattr(watchlist.Prompt, 'ask', lambda *a, **k: "y")
    monkeypatch.setattr(config.console, 'print', lambda *a, **k: printed.append(str(a[0]) if a else ""))

    watchlist.delete_stock()

    assert "삭제되었습니다" in "\n".join(printed)
    codes = [i['code'] for i in config.session.stock_data['stocks_kr']]
    assert '005930' not in codes


def test_탐색_추가는_저장을_확인한_뒤에만_성공으로_본다(stock_file, monkeypatch):
    """이 화면의 성공 문구는 '자동매매가 돌고 있다면 다음 감시 주기부터 반영됩니다'이다.
    저장이 실패했으면 지킬 수 없는 약속이므로 성공으로 보고해서는 안 된다."""
    from modules.manage import discover

    printed = []
    monkeypatch.setattr(jsonio, 'save_json', lambda *a, **k: False)
    fake_console = type('C', (), {'print': lambda self, *a, **k: printed.append(str(a[0]) if a else "")})()

    cands = [{"code": "035420", "name": "NAVER", "exchange": "KOSPI"}]
    assert discover._commit_additions(cands, console=fake_console) is False

    body = "\n".join(printed)
    assert "저장하지 못했습니다" in body
    # 그리고 실제로 추가되지 않았다(load_stock_config 가 파일을 다시 읽었다).
    codes = [i['code'] for i in config.session.stock_data['stocks_kr']]
    assert '035420' not in codes


def test_탐색_추가_저장이_성공하면_실제로_들어간다(stock_file, monkeypatch):
    from modules.manage import discover

    printed = []
    fake_console = type('C', (), {'print': lambda self, *a, **k: printed.append(str(a[0]) if a else "")})()
    cands = [{"code": "035420", "name": "NAVER", "exchange": "KOSPI"}]
    assert discover._commit_additions(cands, console=fake_console) is True

    codes = [i['code'] for i in config.session.stock_data['stocks_kr']]
    assert '035420' in codes
