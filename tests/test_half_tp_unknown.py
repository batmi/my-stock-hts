"""반익절 이력을 **못 읽은 것**과 '아무도 반익절 안 했다'를 가르는가.

[배경 · 2026-09-06 감사] get_all_half_tp 는 조회 실패를 빈 집합으로 돌려줬다. db_manager
안의 주석이 그 위험을 이미 적어 뒀는데도(`빈 집합은 '아무도 반익절 안 했다'로 읽혀 이미
반익절한 종목을 또 판다`) 반환값은 그대로였다 — 위험을 적어만 두고 고치지 않은 자리다.

게다가 그 빈 집합은 기동 시 **메모리 캐시로 굳었다**(trader.half_tp_cache). 세션 내내
다시 읽지 않으므로, 기동 순간의 DB 한 번 실패가 하루치 판정을 바꾼다.

방향은 대가로 정한다:
  · '안 했다'로 틀리면 → 이미 반쪽 판 포지션을 **또 판다**(실제 돈이 나간다)
  · '했다'로 틀리면   → 이번 반익절을 걸렀을 뿐, 청산 체인(TS·손절)은 그대로 흐른다
그래서 모르면 '했다'로 답한다.

바로 두 줄 아래에 같은 규약이 이미 있었다: `portfolio_heat_unknown  # '0(없음)'과 '못 셈'을 가른다`.
"""
import sqlite3
from unittest.mock import patch

import pytest

from modules import db_manager
from modules.auto_trade import AutoTrader

CODE = "005930"


def _db():
    return getattr(db_manager.db, "_real_db", db_manager.db)


@pytest.fixture
def trader():
    AutoTrader._instance = None
    t = AutoTrader()
    t.half_tp_cache = set()
    t.half_tp_cache_loaded = False
    yield t


# ── DB 계층 ──────────────────────────────────────────

def test_a_failed_read_raises_instead_of_an_empty_set(monkeypatch):
    class _Broken:
        def cursor(self):
            raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(type(_db()), "_get_conn", lambda self, *a, **k: _Broken())
    with pytest.raises(Exception):
        _db().get_all_half_tp()


def test_an_empty_table_is_still_an_empty_set():
    assert _db().get_all_half_tp() == set()


# ── 판정 ────────────────────────────────────────────

def test_unknown_is_treated_as_already_half_sold(trader):
    """[핵심] 못 읽었으면 '했다'로 답한다 — 중복 매도를 만들지 않는다."""
    with patch('modules.auto_trade.db_manager.db.get_all_half_tp',
               side_effect=sqlite3.OperationalError("boom")):
        assert trader._already_half_sold(CODE) is True


def test_the_failure_is_not_frozen_into_the_session(trader):
    """기동 때 못 읽었어도 판정 시점에 다시 읽는다 — 하루가 통째로 어긋나지 않는다."""
    with patch('modules.auto_trade.db_manager.db.get_all_half_tp',
               side_effect=sqlite3.OperationalError("boom")):
        assert trader._already_half_sold(CODE) is True

    with patch('modules.auto_trade.db_manager.db.get_all_half_tp', return_value=set()):
        assert trader._already_half_sold(CODE) is False, "회복됐는데 다시 읽지 않았다"


def test_a_loaded_cache_is_not_reread_every_time(trader):
    """적재에 성공했으면 매 판정마다 DB 를 치지 않는다(파이3)."""
    with patch('modules.auto_trade.db_manager.db.get_all_half_tp',
               return_value={CODE}) as q:
        assert trader._already_half_sold(CODE) is True
        assert trader._already_half_sold(CODE) is True
        assert q.call_count == 1


def test_a_known_absence_is_reported_as_not_sold(trader):
    trader.half_tp_cache = set()
    trader.half_tp_cache_loaded = True
    assert trader._already_half_sold(CODE) is False


def test_a_known_record_is_reported_as_sold(trader):
    trader.half_tp_cache = {CODE}
    trader.half_tp_cache_loaded = True
    assert trader._already_half_sold(CODE) is True
