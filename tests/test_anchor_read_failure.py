"""트레일링 앵커 조회 **실패**를 '기록 없음(0)'으로 접어 세션 캐시에 굳히는가.

[배경 · 2026-09-06 감사] get_highest_price 는 조회 실패와 '기록 없음'을 둘 다 None 으로
돌려줬다. 호출부(_cached_anchor)는 그것을 0.0 으로 접어 **메모리 캐시에 넣었다**.
그 뒤로는 캐시에 값이 있으므로 DB 가 회복돼도 다시 읽지 않는다.

앵커 0 은 트레일링 스탑 판정을 통째로 건너뛰게 한다 — 이 시스템의 주청산 수단이
그 종목에서 세션 내내 조용히 꺼진다([[unknown-vs-empty]] · [[trend-following-exit-policy]]).
"""
import sqlite3
from unittest.mock import patch

import pytest

from modules import db_manager
from modules.auto_trade import AutoTrader

CODE, PEAK = "005930", 90_000.0


def _db():
    return getattr(db_manager.db, "_real_db", db_manager.db)


@pytest.fixture
def trader():
    AutoTrader._instance = None
    t = AutoTrader()
    t.trailing_stop_cache = {}
    yield t


def test_a_failed_anchor_read_raises_instead_of_saying_no_record(monkeypatch):
    class _Broken:
        def cursor(self):
            raise sqlite3.OperationalError("database is locked")

    #  클래스에 건다 — 인스턴스에 걸면 복원 시 속성이 남아 뒤 테스트의 패치를 가린다.
    monkeypatch.setattr(type(_db()), "_get_conn", lambda self, *a, **k: _Broken())
    with pytest.raises(Exception):
        _db().get_highest_price(CODE)


def test_no_record_is_still_none():
    assert _db().get_highest_price("NO-SUCH-CODE") is None


def test_a_failed_read_is_not_frozen_into_the_session_cache(trader):
    """[핵심] 실패한 조회의 0.0 이 캐시에 남으면 DB 가 나아도 앵커가 0 으로 굳는다."""
    with patch('modules.auto_trade.db_manager.db.get_highest_price',
               side_effect=sqlite3.OperationalError("boom")):
        assert trader._cached_anchor(CODE) == 0.0

    assert CODE not in trader.trailing_stop_cache, \
        "실패로 얻은 0.0 이 캐시에 굳었다 — DB 가 회복돼도 다시 읽지 않는다"

    with patch('modules.auto_trade.db_manager.db.get_highest_price', return_value=PEAK):
        assert trader._cached_anchor(CODE) == PEAK, "회복된 뒤에도 다시 읽지 않았다"


def test_a_genuine_absence_is_still_cached_as_zero(trader):
    """대조군 — 진짜로 기록이 없으면 종전대로 0.0 을 캐시한다(매 주기 DB 를 치지 않는다)."""
    with patch('modules.auto_trade.db_manager.db.get_highest_price',
               return_value=None) as q:
        assert trader._cached_anchor(CODE) == 0.0
        assert trader._cached_anchor(CODE) == 0.0
        assert q.call_count == 1, "기록 없음까지 매 주기 다시 읽는다 — 파이3에 부담이다"


def test_a_known_anchor_is_returned_and_cached(trader):
    with patch('modules.auto_trade.db_manager.db.get_highest_price', return_value=PEAK):
        assert trader._cached_anchor(CODE) == PEAK
    assert trader.trailing_stop_cache[CODE] == PEAK
