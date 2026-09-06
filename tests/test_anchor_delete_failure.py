"""신규 진입 때 옛 앵커 삭제가 **실패하면** 어떻게 되는가.

[배경 · 2026-09-06 감사] 같은 행(trailing_stops)을 **쓰는** update_highest_price 는
성공 여부를 돌려주고 그 사유까지 적어 뒀다 — "실패해도 호출부는 인메모리 캐시를 갱신하고
넘어간다. 그래서 그 세션 동안은 정상으로 보이고, **재기동해야 소실이 드러난다**".
그런데 같은 행을 **지우는** delete_trailing_stop 은 예외를 통째로 삼키고 아무것도
돌려주지 않았다. 대칭이 깨진 자리다.

신규 매수 직전에 이 삭제가 도는 이유는 하나다 — **새 포지션이 이전 포지션의 고점을
물려받지 않게** 하려는 것이다(trader 매수 경로). 삭제가 조용히 실패하면
  · 그 세션은 멀쩡하다(메모리 캐시는 pop 되므로)
  · **재기동하면** get_all_trailing_stops 가 옛 고점을 싣고 온다
  · update_highest_price 는 단조 증가라 그 값이 내려오지 않는다
  · 샹들리에 TS 가 '고점 대비 폭락'으로 읽어 **즉시 시장가 청산**을 때린다
같은 사고를 권리 조정 쪽에서 이미 문서화해 뒀다(액면분할로 앵커만 남는 경우).
"""
import sqlite3

import pytest

from modules import db_manager
from modules.auto_trade.engine import compute_trailing_stop

CODE = "005930"
OLD_PEAK = 130_000.0        # 지난 포지션의 고점
NEW_BUY = 50_000.0          # 한참 뒤 훨씬 싼 값에 다시 들어갔다
NEW_NOW = 51_000.0          # 진입 직후, 아직 아무 일도 없다


def _db():
    return getattr(db_manager.db, "_real_db", db_manager.db)


@pytest.fixture(autouse=True)
def _clean():
    _db().delete_trailing_stop(CODE)
    yield
    _db().delete_trailing_stop(CODE)


class _Broken:
    def cursor(self):
        raise sqlite3.OperationalError("disk I/O error")

    def commit(self):  # pragma: no cover
        pass


# ── 계약 ────────────────────────────────────────────

def test_a_failed_delete_reports_false(monkeypatch):
    _db().update_highest_price(CODE, OLD_PEAK)
    monkeypatch.setattr(type(_db()), "_get_conn", lambda self, *a, **k: _Broken())
    assert _db().delete_trailing_stop(CODE) is False


def test_a_successful_delete_reports_true():
    _db().update_highest_price(CODE, OLD_PEAK)
    assert _db().delete_trailing_stop(CODE) is True


def test_deleting_a_row_that_is_not_there_is_still_success():
    """없는 것을 지우라는 요청은 실패가 아니다 — 호출부가 헛경보를 내면 안 된다."""
    assert _db().delete_trailing_stop("NO-SUCH-CODE") is True


def test_a_failed_delete_is_logged(monkeypatch, caplog):
    import logging
    _db().update_highest_price(CODE, OLD_PEAK)
    monkeypatch.setattr(type(_db()), "_get_conn", lambda self, *a, **k: _Broken())
    with caplog.at_level(logging.ERROR, logger="modules.db_manager"):
        _db().delete_trailing_stop(CODE)
    assert any("앵커" in r.getMessage() or "trailing" in r.getMessage().lower()
               for r in caplog.records), [r.getMessage() for r in caplog.records]


# ── 무엇이 걸려 있나 ────────────────────────────────

def test_a_stale_anchor_would_liquidate_the_new_position_at_once():
    """삭제가 실패한 상태를 재기동으로 재현한다 — 새 포지션이 즉시 청산 판정을 받는다."""
    _db().update_highest_price(CODE, OLD_PEAK)          # 지난 포지션의 고점이 남아 있다

    stale = _db().get_all_trailing_stops().get(CODE)    # 재기동 시 캐시에 실린다
    assert stale == pytest.approx(OLD_PEAK)

    ts = compute_trailing_stop(stale, NEW_BUY, NEW_NOW, ind={'atr': 1_500.0})
    assert ts and ts['triggered'], (
        "옛 앵커가 남았는데 청산이 발동하지 않는다 — 이 시나리오가 재현되지 않는다")

    #  앵커가 제대로 지워졌다면 새 포지션은 아직 아무 판정도 받지 않는다.
    assert _db().delete_trailing_stop(CODE) is True
    fresh = _db().get_all_trailing_stops().get(CODE)
    assert fresh is None
