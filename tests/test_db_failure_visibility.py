"""DB 조회 실패가 '데이터 없음'으로 둔갑할 때, 최소한 흔적은 남는가.

[배경] db_manager 의 조회 함수 다수는 `except Exception: return None/[]/{}/False` 형태다.
호출부는 그 값을 '데이터 없음'으로 읽으므로 **DB 장애가 정상 상태로 보인다** — 게다가
로그가 한 줄도 남지 않았다(2026-09-03 전수 조사: 20곳). 위험한 조합이 여럿이다.

  · get_pending_reserved_orders() → []  예약 손절 감시 전체가 조용히 멈춘다
  · check_trade_exists() → False        체결 행을 한 번 더 INSERT 한다(이중 계상)
  · get_all_stock_strategies() → []     개별 룰이 사라지고 전역값으로 판정한다
  · get_all_trailing_stops() → {}       트레일링 앵커를 잃는다

반환 계약을 바꾸면 호출부 전체를 손봐야 하므로 **동작은 그대로 두고 흔적만 남긴다.**
'모름'과 '없음'을 가르는 것은 그다음 문제이고, 지금 급한 것은 보이지 않는다는 사실이다.
운영기가 라즈베리파이(SD 카드)라 I/O 오류·용량 부족이 남의 일이 아니다.

[2026-09-06] 그 '그다음 문제'를 하나씩 걷어내는 중이다. 호출부가 적고 대가가 큰 자리부터
 실패를 **올리도록** 바꾸고 있다. 여기서 흔적만 확인하는 목록(BROKEN)은 아직 종전 계약을
 지키는 자리들이고, 계약이 바뀐 자리는 아래 RAISING 에 옮겨 적는다 — 둘을 한 파일에 두는
 이유는, 어느 쪽이 남았는지가 한눈에 보여야 다음 감사가 이어지기 때문이다.
   · get_highest_price          → tests/test_anchor_read_failure.py
   · get_max_daily_asset        → tests/test_drawdown_hwm_outlier.py
   · get_cancel_record_by_org_odno → tests/test_cancel_origin_unknown.py
   · check_trade_exists          → tests/test_duplicate_fill_guard.py
   · get_position_ref            → tests/test_corporate_action.py
   · get_all_half_tp             → tests/test_half_tp_unknown.py
"""
import ast
import os

import config

import pytest

from modules import db_manager


@pytest.fixture(autouse=True)
def clear_throttle():
    db_manager._SWALLOW_LOGGED.clear()
    yield
    db_manager._SWALLOW_LOGGED.clear()


def _real_db():
    """큐 프록시가 걸려 있어도 실제 DBManager 를 잡는다."""
    d = db_manager.db
    return getattr(d, '_real_db', d)


BROKEN = [
    ("get_pending_reserved_orders", (), []),
    ("get_all_trailing_stops", (), {}),
    ("get_all_stock_strategies", (), []),
]

#  실패를 '없음'과 갈라 **올리도록** 계약이 바뀐 자리. 흔적은 여전히 남긴다.
RAISING = [
    ("get_highest_price", ("005930",)),
    ("check_trade_exists", ("0001", "체결")),
    ("get_position_ref", ("005930",)),
    ("get_all_half_tp", ()),
    ("get_cancel_record_by_org_odno", ("ODNO-1",)),
    ("get_max_daily_asset", ("2026-01-01", "ACC")),
]


@pytest.mark.parametrize("name,args,expected", BROKEN)
def test_failure_is_logged_and_return_value_is_unchanged(monkeypatch, caplog, name, args, expected):
    """실패해도 반환값은 종전 그대로이되, 경고 한 줄이 남아야 한다."""
    db = _real_db()
    monkeypatch.setattr(type(db), '_get_conn',
                        lambda self: (_ for _ in ()).throw(RuntimeError("디스크 오류")))

    with caplog.at_level("WARNING", logger="modules.db_manager"):
        got = getattr(db, name)(*args)

    assert got == expected, f"반환 계약이 바뀌었다: {got!r}"
    assert any(name in r.message for r in caplog.records), (
        f"{name} 실패가 로그에 남지 않았다 — DB 장애가 정상 상태로 보인다")
    assert any("디스크 오류" in r.message for r in caplog.records), "원인이 안 남았다"


@pytest.mark.parametrize("name,args", RAISING)
def test_a_changed_contract_raises_and_still_leaves_a_trace(monkeypatch, caplog, name, args):
    """계약이 바뀐 자리는 실패를 올린다 — 그래도 흔적은 남아야 원인을 찾는다."""
    db = _real_db()
    monkeypatch.setattr(type(db), '_get_conn',
                        lambda self: (_ for _ in ()).throw(RuntimeError("디스크 오류")))

    with caplog.at_level("WARNING", logger="modules.db_manager"):
        with pytest.raises(Exception):
            getattr(db, name)(*args)

    assert any(name in r.message for r in caplog.records), (
        f"{name} 실패가 로그에 남지 않았다")


def test_repeated_failure_is_throttled(monkeypatch, caplog):
    """같은 자리가 계속 실패해도 로그를 익사시키지 않는다.

    get_highest_price 는 주기마다 종목 수만큼 불린다. 매번 남기면 정작 읽어야 할
    다른 로그가 묻힌다. (이제 예외를 올리므로 호출부에서 받아 넘긴다 — 로그 스로틀은
    그대로 필요하다.)
    """
    db = _real_db()
    monkeypatch.setattr(type(db), '_get_conn',
                        lambda self: (_ for _ in ()).throw(RuntimeError("디스크 오류")))

    with caplog.at_level("WARNING", logger="modules.db_manager"):
        for _ in range(50):
            try:
                db.get_highest_price("005930")
            except Exception:
                pass

    hits = [r for r in caplog.records if "get_highest_price" in r.message]
    assert len(hits) == 1, f"스로틀이 동작하지 않는다: {len(hits)}건"


def test_throttle_is_per_call_site():
    """자리마다 따로 센다 — 한 곳이 시끄럽다고 다른 곳이 침묵하면 안 된다."""
    db_manager._SWALLOW_LOGGED.clear()
    db_manager._swallowed("aaa", RuntimeError("x"))
    db_manager._swallowed("bbb", RuntimeError("x"))
    assert set(db_manager._SWALLOW_LOGGED) == {"aaa", "bbb"}


def test_no_new_silent_swallow_appears():
    """조회 실패를 흔적 없이 삼키는 자리가 새로 생기면 알려준다.

    정리(close/__del__/backup) 경로는 조용해도 된다 — 실패해도 잃을 정보가 없다.
    """
    allowed = {"__del__", "close_connection", "close_all_connections", "backup"}
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    path = os.path.join(root, "modules", "db_manager.py")
    tree = ast.parse(open(path, encoding='utf-8').read())

    silent = []
    for fn in [n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef)]:
        if fn.name in allowed:
            continue
        for h in [n for n in ast.walk(fn) if isinstance(n, ast.ExceptHandler)]:
            if len(h.body) != 1 or not isinstance(h.body[0], ast.Return):
                continue
            v = h.body[0].value
            empty = (isinstance(v, (ast.List, ast.Dict, ast.Set, ast.Tuple))
                     and not getattr(v, 'elts', None) and not getattr(v, 'keys', None)) \
                    or (isinstance(v, ast.Constant) and v.value in (None, 0, False))
            if empty:
                silent.append(f"{fn.name}() L{h.lineno}")

    assert not silent, (
        "DB 실패를 흔적 없이 빈 값으로 바꾸는 자리가 있다 — _swallowed() 를 태울 것:\n  "
        + "\n  ".join(silent))


# ------------------------------------------------- 초기화·마이그레이션 실패
def test_init_failure_is_logged_and_counted(tmp_path, caplog, monkeypatch):
    """마이그레이션이 실패하면 로그와 쓰기 실패 카운터에 남아야 한다.

    [왜] 종전에는 console.print 한 줄이 전부였고 SCREEN_DEBUG_LEVEL=OFF면 그마저 없었다.
    운영은 헤드리스(라즈베리파이)라 보는 사람이 없다. 스키마가 반쯤 적용된 채 시스템이
    기동하면 이후 모든 조회가 '데이터 없음'으로 보이고 원인은 어디에도 없다.
    기동 자체는 막지 않는다 — 포지션을 든 채 손절이 멈추는 쪽이 더 나쁘다.
    """
    import sqlite3
    from modules.db_manager import DBManager

    def boom(*a, **k):
        raise sqlite3.OperationalError("disk I/O error")

    monkeypatch.setattr(config, "DB_FILE_PATH", str(tmp_path / "broken.db"), raising=False)
    monkeypatch.setattr(sqlite3, "connect", boom)

    with caplog.at_level("ERROR", logger="modules.db_manager"):
        mgr = DBManager()

    assert any("초기화" in r.message or "마이그레이션" in r.message
               for r in caplog.records), "초기화 실패가 로그에 남지 않았다"

    failures = mgr.get_write_failures()
    assert failures['count'] >= 1, "쓰기 실패 카운터에 잡히지 않았다 — 운영자에게 못 간다"
    assert "DB 초기화" in failures['last_op'], failures


def test_init_failure_does_not_abort_startup(tmp_path, monkeypatch):
    """실패해도 객체는 만들어진다 — 청산 감시가 멈추면 안 된다."""
    import sqlite3
    from modules.db_manager import DBManager

    monkeypatch.setattr(config, "DB_FILE_PATH", str(tmp_path / "broken2.db"), raising=False)
    monkeypatch.setattr(sqlite3, "connect",
                        lambda *a, **k: (_ for _ in ()).throw(sqlite3.OperationalError("boom")))
    mgr = DBManager()
    assert mgr is not None

