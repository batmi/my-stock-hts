"""예약 감시가 **한 건의 오류로 그 주기 전체를 잃는가**, 그리고 취소 실패를 '없음'으로 읽는가.

[배경 · 2026-09-06 감사]

1) _check_orders 의 주문 루프에는 주문 단위 격리가 없었다. 루프 **첫 줄**이
   `float(order.get('target_price', 0.0))` 인데 target_price 는 NULL 이 가능한 컬럼이다
   (TIME·COMPOSITE 조건은 목표가가 없다). dict.get 의 기본값은 **키가 없을 때만** 쓰이고
   SQLite 는 키를 주고 None 을 담으므로, 그런 행이 하나 있으면 TypeError 가 난다. 실측:

       DB 에서 읽은 target_price : None
       dict.get 기본값이 쓰이나  : None
       float() 결과             : TypeError

   바깥 감시 루프(_monitor_loop)는 로그 한 줄을 남기고 다음 주기로 넘어가지만, 그 행은
   그대로 남아 **같은 자리에서 또 깨진다.** 예약은 대개 손절·익절이라, 다른 종목들의
   보호 공백이 무기한 이어진다.

2) cancel_reserved_buy_orders 는 실패를 0 으로 돌려줬다. 이 함수의 존재 이유가
   '신규 매수 시 중복 진입 방지'인데, 실패가 '취소할 예약이 없었다'로 읽히면 남아 있는
   예약 매수가 나중에 그대로 발동해 **같은 종목에 두 번째 포지션**이 생긴다.
"""
import sqlite3
from unittest.mock import patch

import pytest

from modules import db_manager, reserved_order_monitor


def _db():
    return getattr(db_manager.db, "_real_db", db_manager.db)


# ── ① 한 건의 오류가 나머지를 죽이는가 ────────────────

def _row(oid, code, name, target_price, condition="LIMIT"):
    return {'id': oid, 'cano': '1', 'acnt': '01', 'market': 'KR',
            'order_type': 'sell', 'code': code, 'name': name, 'qty': 10,
            'order_price': 0.0, 'condition_type': condition,
            'target_price': target_price, 'target_time': '', 'status': 'PENDING',
            'expire_dt': '20991231', 'lowest_price': 0.0, 'highest_price': 0.0,
            'composite_json': None}


def test_a_broken_row_does_not_skip_the_other_reserved_orders():
    """[핵심] target_price 가 NULL 인 행 하나가 뒤따르는 손절 예약을 삼키면 안 된다."""
    mon = reserved_order_monitor.ReservedOrderMonitor()
    orders = [_row(1, "000660", "SK하이닉스", None),        # 깨지는 행
              _row(2, "005930", "삼성전자", 70_000.0, "STOP")]  # 발동해야 하는 손절

    with patch('modules.reserved_order_monitor.db_manager.db.get_pending_reserved_orders',
               return_value=orders), \
         patch('modules.reserved_order_monitor.api.get_current_price', return_value=60_000.0), \
         patch('modules.reserved_order_monitor.api.domestic_trading_session_open',
               return_value=True), \
         patch('modules.reserved_order_monitor.api.get_chart_data', return_value=None), \
         patch.object(mon, '_execute_order') as ex:
        mon._check_orders()

    fired = [c.args[0]['id'] for c in ex.call_args_list]
    assert 2 in fired, f"깨진 행 하나 때문에 손절 예약이 점검되지 않았다 (발동: {fired})"


def test_a_null_target_price_is_read_as_zero_not_an_error():
    """TIME 조건은 목표가가 없다 — 그 자체는 오류가 아니다."""
    mon = reserved_order_monitor.ReservedOrderMonitor()
    orders = [_row(1, "005930", "삼성전자", None, "TIME")]
    orders[0]['target_time'] = "0000"

    with patch('modules.reserved_order_monitor.db_manager.db.get_pending_reserved_orders',
               return_value=orders), \
         patch('modules.reserved_order_monitor.api.domestic_trading_session_open',
               return_value=True), \
         patch('modules.reserved_order_monitor.api.get_chart_data', return_value=None), \
         patch.object(mon, '_execute_order') as ex:
        mon._check_orders()

    assert ex.called, "목표가가 없는 TIME 예약이 발동하지 못했다"


# ── ② 취소 실패 ≠ 취소할 것이 없음 ────────────────────

def test_a_failed_bulk_cancel_reports_none_not_zero(monkeypatch):
    class _Broken:
        def cursor(self):
            raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(db_manager.time, "sleep", lambda s: None)
    monkeypatch.setattr(type(_db()), "_get_conn", lambda self, *a, **k: _Broken())
    assert _db().cancel_reserved_buy_orders("1", "01", "005930") is None


def test_no_pending_reservation_is_still_zero():
    assert _db().cancel_reserved_buy_orders("1", "01", "NO-SUCH-CODE") == 0


def test_a_real_cancel_reports_the_count():
    db = _db()
    db.insert_reserved_order("C1", "01", "KR", "buy", "005930", "삼성전자",
                             10, 70000, "LIMIT", 70000, "", None, None)
    assert db.cancel_reserved_buy_orders("C1", "01", "005930") == 1
