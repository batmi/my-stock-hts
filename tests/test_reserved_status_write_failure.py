"""예약 주문 발주 **뒤** 상태 기록이 실패하면 주문번호가 유실되는가.

[배경 · 2026-09-06 감사] db_manager 의 writer 27개 중 update_reserved_order_status 만
**잠금 재시도도 예외 처리도 없었다.** 이 함수는 발주 직후 'TRIGGERED' 와 주문번호(odno)를
적는 자리다. 여기서 던지면 예외가 감시 루프(_monitor_loop)까지 올라가고, 그 결과
  · 주문은 거래소에 살아 있는데
  · 예약 상태는 PROCESSING 에 갇혀 다시는 조회되지 않고 (감시는 PENDING 만 본다)
  · **주문번호가 어디에도 남지 않는다** — 체결 대사도 미체결 자동 취소도 odno 로 찾는다
는 상태가 된다([[order-id-invariant]]).

바로 위 코드의 '좀비 방지' 주석이 막으려던 것이 정확히 이 상태인데, 그 방어는 발주
**전** 구간에만 걸려 있었다.
"""
import sqlite3
from unittest.mock import MagicMock, patch

import pytest

from modules import db_manager, reserved_order_monitor


def _db():
    return getattr(db_manager.db, "_real_db", db_manager.db)


class _Broken:
    def cursor(self):
        raise sqlite3.OperationalError("database is locked")

    def commit(self):  # pragma: no cover
        pass


# ── DB 계층 ──────────────────────────────────────────

def test_the_status_write_reports_failure_instead_of_raising(monkeypatch):
    monkeypatch.setattr(db_manager.time, "sleep", lambda s: None)
    monkeypatch.setattr(type(_db()), "_get_conn", lambda self, *a, **k: _Broken())
    assert _db().update_reserved_order_status(1, 'TRIGGERED', 'O1') is False


def test_a_successful_status_write_reports_true():
    db = _db()
    oid = db.insert_reserved_order("12345678", "01", "KR", "buy", "005930", "삼성전자",
                                   10, 70000, "LIMIT", 70000, None)
    assert db.update_reserved_order_status(oid, 'TRIGGERED', 'O1') is True


def test_a_locked_db_is_retried_before_giving_up(monkeypatch):
    calls = []

    class _Locked:
        def cursor(self):
            calls.append(1)
            raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(db_manager.time, "sleep", lambda s: None)
    monkeypatch.setattr(type(_db()), "_get_conn", lambda self, *a, **k: _Locked())
    assert _db().update_reserved_order_status(1, 'TRIGGERED', 'O1') is False
    assert len(calls) == 5, f"다른 writer 와 재시도 횟수가 다르다: {len(calls)}"


# ── 발주 뒤 — 주문번호가 사람에게 닿는가 ─────────────

def _order_row():
    return {'id': 7, 'cano': '12345678', 'acnt': '01', 'market': 'KR',
            'order_type': 'buy', 'code': '005930', 'name': '삼성전자',
            'qty': 10, 'order_price': 70000, 'condition_type': 'LIMIT',
            'target_price': 70000, 'status': 'PENDING'}


def _execute(status_ok):
    mon = reserved_order_monitor.ReservedOrderMonitor()
    with patch('modules.reserved_order_monitor.db_manager.db.update_reserved_order_status',
               return_value=status_ok) as upd, \
         patch('modules.reserved_order_monitor.api.place_order',
               return_value={'rt_cd': '0', 'output': {'ODNO': 'ODNO-9'}}), \
         patch('modules.reserved_order_monitor.api.send_telegram_message'), \
         patch('modules.reserved_order_monitor.db_manager.db.insert_trade'), \
         patch('modules.reserved_order_monitor.analysis.get_snapshot', return_value=None), \
         patch('modules.reserved_order_monitor.db_manager.db.cancel_other_reserved_orders',
               return_value=[]), \
         patch('modules.reserved_order_monitor.alert_delivered',
               return_value=True) as alert:
        mon._execute_order(_order_row(), "지정가 도달")
    return upd, alert


def test_a_lost_status_write_does_not_kill_the_monitor():
    """예외가 감시 루프까지 올라가면 그 주기의 나머지 예약도 함께 죽는다."""
    _execute(status_ok=False)          # 예외가 나오면 이 호출 자체가 실패한다


def test_a_lost_status_write_reaches_the_operator_with_the_order_number():
    """[핵심] 시스템이 추적하지 못하는 주문은 사람이 대신 찾아야 한다."""
    _upd, alert = _execute(status_ok=False)
    assert alert.called, "주문번호가 유실됐는데 아무에게도 알리지 않았다"
    body = alert.call_args[0][0]
    assert "ODNO-9" in body, f"경보에 주문번호가 없다: {body}"


def test_a_normal_execution_does_not_alert():
    """대조군 — 정상 경로에서 경보가 나가면 사람이 경보를 무시하게 된다."""
    _upd, alert = _execute(status_ok=True)
    assert not alert.called
