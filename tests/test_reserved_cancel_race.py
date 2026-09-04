"""사용자 취소가 '이미 발동된' 예약 주문을 덮어쓰면 안 된다.

[배경 · 2026-09-04 감사] 예약 주문 취소는 '목록을 뽑아 보여주고 → 사람이 고르고 → 지운다'
는 흐름이라, 그 사이에 예약 감시 스레드가 같은 주문을 발동시킬 수 있다. 종전 두 경로
(메뉴 8-5 · 텔레그램 /reserves d)는 무조건 status='CANCELED' 로 덮었다. 그러면
**거래소에는 주문이 나갔는데 기록은 취소**가 된다 — 감시자는 그 주문을 더 보지 않고
(PENDING/PROCESSING 만 본다) 포지션이 관리 밖으로 떨어져 손절선이 붙을 자리를 잃는다.

같은 사고를 권리락 일괄 취소에서 이미 겪어 그쪽은 status='PENDING' 조건이 붙어 있다
(test_corporate_action: "발동이 끝난 주문의 이력까지 덮어썼다"). 사용자 취소 두 경로만
남아 있었다.
"""
import pytest

from modules import db_manager, trading
from modules.telegram_bot import TelegramCommander


@pytest.fixture
def db():
    return db_manager.db


def _add(db, code="005930", name="삼성전자"):
    db.insert_reserved_order(cano="12345678", acnt="01", market="domestic",
                             order_type="buy", code=code, name=name, qty=10,
                             order_price=70000, condition_type="PRICE_DOWN",
                             target_price=70000, target_time=None)
    return db.get_pending_reserved_orders()[-1]["id"]


def _status(db, oid):
    rows = db.execute_query("SELECT status FROM reserved_orders WHERE id=?", (oid,), fetch="one")
    return rows["status"] if rows else None


# ─────────────────────────────────────────────
# 1. DB 계약
# ─────────────────────────────────────────────

def test_pending_order_is_cancelled(db):
    oid = _add(db)
    assert db.cancel_reserved_order(oid, reason="사용자 수동 취소") is True
    assert _status(db, oid) == "CANCELED"


@pytest.mark.parametrize("state", ["PROCESSING", "TRIGGERED", "FAILED"])
def test_a_non_pending_order_is_left_alone(db, state):
    """[핵심] 발동이 시작된 주문을 덮으면 실주문과 기록이 갈라진다."""
    oid = _add(db)
    db.update_reserved_order_status(oid, state)

    assert db.cancel_reserved_order(oid) is False
    assert _status(db, oid) == state, "발동된 주문의 상태를 덮어썼다"


def test_unknown_id_is_reported_as_not_cancelled(db):
    assert db.cancel_reserved_order(999999) is False


def test_reason_is_recorded(db):
    oid = _add(db)
    db.cancel_reserved_order(oid, reason="사용자 수동 취소")
    row = db.execute_query("SELECT fail_reason FROM reserved_orders WHERE id=?",
                           (oid,), fetch="one")
    assert row["fail_reason"] == "사용자 수동 취소"


# ─────────────────────────────────────────────
# 2. 사용자 경로 두 곳이 그 결과를 알리는가
# ─────────────────────────────────────────────

def test_telegram_reports_when_the_order_already_fired(db, monkeypatch):
    """조용히 실패하면 '취소했다'고 믿은 채 주문이 살아 있다."""
    oid = _add(db)
    monkeypatch.setattr(db_manager.db, "get_pending_reserved_orders",
                        lambda: [{"id": oid, "order_type": "buy",
                                  "name": "삼성전자", "code": "005930"}])
    monkeypatch.setattr(db_manager.db, "cancel_reserved_order", lambda *a, **k: False)

    msg = TelegramCommander()._cmd_reserves(["d", str(oid)])
    assert "취소하지 못했습니다" in msg
    assert "예약 취소 완료" not in msg


def test_telegram_reports_success_normally(db, monkeypatch):
    oid = _add(db)
    monkeypatch.setattr(db_manager.db, "get_pending_reserved_orders",
                        lambda: [{"id": oid, "order_type": "buy",
                                  "name": "삼성전자", "code": "005930"}])
    monkeypatch.setattr(db_manager.db, "cancel_reserved_order", lambda *a, **k: True)

    assert "예약 취소 완료" in TelegramCommander()._cmd_reserves(["d", str(oid)])


def test_menu_path_does_not_announce_a_cancel_that_did_not_happen(monkeypatch):
    """취소 알림·거래기록은 실제로 취소됐을 때만 남아야 한다."""
    sent, logged = [], []
    orders = [{"id": 7, "code": "005930", "name": "삼성전자", "order_type": "buy",
               "qty": 10, "order_price": 70000, "condition_type": "PRICE_DOWN"}]

    monkeypatch.setattr(db_manager.db, "cancel_reserved_order", lambda *a, **k: False)
    monkeypatch.setattr(trading.api, "send_telegram_message", lambda *a, **k: sent.append(a))
    monkeypatch.setattr(db_manager.db, "insert_trade",
                        lambda *a, **k: logged.append(a))

    assert trading._cancel_reserved_orders(orders, ["7"]) == []
    assert sent == [] and logged == []


def test_menu_path_announces_a_real_cancel(monkeypatch):
    sent, logged = [], []
    orders = [{"id": 7, "code": "005930", "name": "삼성전자", "order_type": "buy",
               "qty": 10, "order_price": 70000, "condition_type": "PRICE_DOWN"}]

    monkeypatch.setattr(db_manager.db, "cancel_reserved_order", lambda *a, **k: True)
    monkeypatch.setattr(trading.api, "send_telegram_message", lambda *a, **k: sent.append(a))
    monkeypatch.setattr(db_manager.db, "insert_trade", lambda *a, **k: logged.append(a))

    assert trading._cancel_reserved_orders(orders, ["7"]) == ["7"]
    assert len(sent) == 1 and len(logged) == 1


def test_both_user_paths_use_the_conditional_cancel():
    """무조건 덮는 함수로 되돌아가면 같은 사고가 다시 난다."""
    import inspect

    for fn in (trading._cancel_reserved_orders, TelegramCommander._cmd_reserves):
        src = inspect.getsource(fn)
        assert "cancel_reserved_order" in src
        assert "'CANCELED'" not in src, f"{fn.__name__} 이 상태를 직접 덮는다"
