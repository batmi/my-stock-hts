"""권리 조정(액면분할·무상증자) 감지 시 예약 주문 일괄 취소 — 커버리지 측정(2026-09-20)에서
한 번도 실행되지 않던 경로를 고정한다. 조정 전 가격의 예약이 남으면 곧바로 오발동한다."""
from unittest.mock import patch

import pytest

from modules import db_manager, reserved_order_monitor as rom


@pytest.fixture
def db():
    return db_manager.db


def _add(db, code, order_type="buy", status=None):
    db.insert_reserved_order(cano="12345678", acnt="01", market="domestic",
                             order_type=order_type, code=code, name=f"N{code}", qty=10,
                             order_price=70000, condition_type="PRICE_DOWN",
                             target_price=70000, target_time=None)
    oid = db.get_pending_reserved_orders()[-1]["id"]
    if status:
        db.execute_query("UPDATE reserved_orders SET status=? WHERE id=?", (status, oid))
    return oid


def test_대기_예약만_전부_취소하고_취소된_목록을_돌려준다(db):
    code = "900001"
    a = _add(db, code, "buy")
    b = _add(db, code, "sell")
    done = _add(db, code, "buy", status="FILLED")
    other = _add(db, "900002")
    rows = db.cancel_reserved_orders_on_corp_action(code, "액면분할")
    assert sorted(r["id"] for r in rows) == sorted([a, b])
    st = lambda i: db.execute_query("SELECT status, fail_reason FROM reserved_orders WHERE id=?", (i,), fetch="one")
    assert st(a)["status"] == "CANCELED" and st(a)["fail_reason"] == "액면분할"
    assert st(done)["status"] == "FILLED" and st(other)["status"] == "PENDING"
    assert db.cancel_reserved_orders_on_corp_action(code, "x") == []      # 두 번째는 취소할 것이 없다


def test_감시기는_취소_내역을_기록하고_알리며_실패는_긴급_경보다(db, monkeypatch):
    mon = rom.ReservedOrderMonitor.__new__(rom.ReservedOrderMonitor)
    sent = []
    monkeypatch.setattr(rom, "alert_delivered", lambda msg, **k: sent.append((msg, k)) or True)
    inserted = []
    monkeypatch.setattr(db, "insert_trade", lambda *a, **k: inserted.append((a, k)) or True)
    _add(db, "900003", "sell")
    mon._cancel_on_corp_action("900003", 0.02, "액면분할 1:50")
    assert len(inserted) == 1 and inserted[0][0][0] == "매도취소(예약)"
    assert sent and "권리 조정" in sent[-1][0] and "다시 설정" in sent[-1][0]
    # DB 실패(None)는 '취소할 것 없음'과 다르다 — 긴급 경보
    sent.clear()
    monkeypatch.setattr(db, "cancel_reserved_orders_on_corp_action", lambda *a, **k: None)
    mon._cancel_on_corp_action("900003", 0.02, "액면분할 1:50")
    assert sent and sent[-1][1].get("urgent") is True and "취소 실패" in sent[-1][0]


def test_매수_사유문의_체결강도_표기와_재진입_허들_정규식은_같은_계약이다():
    """trader._execute_buy_orders 가 적는 '체결강도:{v:.1f}%' 를 _check_buy_conditions 가
    r'체결강도:\\s*([0-9.]+)%' 로 되읽는다 — 어느 한쪽만 바뀌면 허들이 조용히 사라진다."""
    import re
    vol_val = f"{127.34:.1f}%"
    reason = f"매수 [점수:8.5, RSI:55.0, 체결강도:{vol_val}]"
    m = re.search(r'체결강도:\s*([0-9.]+)%', reason)
    assert m and float(m.group(1)) == 127.3
    src = open("modules/auto_trade/trader.py", encoding="utf-8").read()
    assert "체결강도:{vol_val}]" in src and "r'체결강도:\\s*([0-9.]+)%'" in src
