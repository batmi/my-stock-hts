"""같은 주문번호가 다른 날에 다시 나왔을 때 무슨 일이 생기는가.

주문번호는 당일 채번이라 매일 0부터 다시 올라간다([[odno-daily-reset]]). 번호 공간이
작아 몇 달을 돌리면 충돌은 예외가 아니라 **정상**이다. 여기서 못 박는 것은 두 가지다 —
옛 행을 오늘 것으로 **읽지 않는다**, 그리고 무엇보다 **덮어쓰지 않는다**.
"""
from datetime import datetime, timedelta

import pytest

from modules import db_manager

ODNO = "0000123"
CODE = "005930"


@pytest.fixture
def old_row():
    """두 달 전, 같은 번호로 낸 '매도' 접수 행."""
    db = db_manager.db
    day = (datetime.now() - timedelta(days=60)).strftime('%Y-%m-%d')
    db.insert_trade("매도", CODE, "삼성전자", 10, 70000, ODNO,
                    profit_amt=-50000, profit_rate=-6.6, score=9,
                    stop_loss_rate=-3.0, order_status="접수",
                    custom_time=f"{day} 10:00:00")
    yield day
    conn = db._get_conn()
    conn.cursor().execute("DELETE FROM trades WHERE odno=?", (ODNO,))
    conn.commit()


def _row():
    conn = db_manager.db._get_conn()
    r = conn.cursor().execute(
        "SELECT time, type, price, qty, profit_amt, order_status "
        "FROM trades WHERE odno=?", (ODNO,)).fetchone()
    return dict(r) if r else None


def test_오늘_체결이_두_달_전_주문을_원_주문으로_읽지_않는다(old_row):
    """호출부는 이 행에서 type·profit_amt·score·stop_loss_rate 를 물려준다 —
    오늘 낸 매수가 두 달 전 매도의 손익을 달고 원장에 남는다."""
    today = datetime.now().strftime('%Y-%m-%d')
    assert db_manager.db.get_trade_by_odno(ODNO, on_date=today) is None, \
        "오늘 것이 아닌데 오늘 조회에 잡힌다"
    # 대조군 — 그 날짜로 물으면 당연히 찾힌다(행을 못 찾게 만든 것이 아니다).
    found = db_manager.db.get_trade_by_odno(ODNO, on_date=old_row)
    assert found and found['type'] == "매도"


def test_오늘_체결_갱신이_두_달_전_행을_덮어쓰지_않는다(old_row):
    """읽기의 오판은 다음 주기가 바로잡지만 이 덮어쓰기는 되돌릴 수 없다."""
    db_manager.db.update_trade(ODNO, price=99999, qty=3, profit_amt=0,
                               profit_rate=0.0, order_status="체결")
    after = _row()
    assert after['price'] == '70000', f"옛 단가가 덮였다: {after}"
    assert after['qty'] == '10', f"옛 수량이 덮였다: {after}"
    assert after['profit_amt'] == -50000, \
        f"그 날의 실현손익이 사라졌다 — 성과 지표와 입출금 판정이 함께 어긋난다: {after}"
    assert after['order_status'] == "접수", f"옛 주문 상태가 덮였다: {after}"


def test_날짜를_명시하면_그_날의_행은_고칠_수_있다(old_row):
    """좁히기만 하고 못 고치게 만든 것이 아니다 — 과거 행을 손보려면 날짜를 대면 된다."""
    db_manager.db.update_trade(ODNO, price=88888, on_date=old_row)
    assert _row()['price'] == '88888'


def test_취소_이력도_날짜로_가른다(old_row):
    """옛 취소 이력이 오늘 취소의 짝으로 잡히면 호출부는 '우리가 낸 취소'로 읽어
    **외부 취소 알림을 삼킨다** — 누가 휴대폰으로 우리 주문을 취소해도 끝내 모른다."""
    db = db_manager.db
    day = (datetime.now() - timedelta(days=60)).strftime('%Y-%m-%d')
    db.insert_trade("매도", CODE, "삼성전자", 10, 70000, "9990001",
                    org_odno=ODNO, order_status="취소", reason="수동 취소",
                    custom_time=f"{day} 11:00:00")
    try:
        today = datetime.now().strftime('%Y-%m-%d')
        assert db.get_cancel_record_by_org_odno(ODNO, on_date=today) is None
        assert db.get_cancel_record_by_org_odno(ODNO, on_date=day) is not None
    finally:
        conn = db._get_conn()
        conn.cursor().execute("DELETE FROM trades WHERE odno=?", ("9990001",))
        conn.commit()


def test_예약_주문은_발동한_날로_가른다():
    """예약 행은 발동 뒤에도 남는다. created_at 으로는 가를 수 없다 — 지난주에 걸어 둔
    예약이 오늘 발동할 수 있어 생성일과 발동일이 다르다."""
    db = db_manager.db
    db.insert_reserved_order("12345678", "01", "KR", "buy", CODE, "삼성전자",
                             1, 70000, "PRICE_ABOVE", 70000, None)
    #  insert_reserved_order 는 id 를 돌려주지 않는다(별개 사안). 방금 넣은 행을 집는다.
    rid = db._get_conn().cursor().execute(
        "SELECT id FROM reserved_orders ORDER BY id DESC LIMIT 1").fetchone()[0]
    try:
        assert db.update_reserved_order_status(rid, "TRIGGERED", "0000555")
        today = datetime.now().strftime('%Y-%m-%d')
        assert db.get_reserved_order_by_odno("0000555", on_date=today), \
            "오늘 발동한 예약을 오늘 날짜로 못 찾는다"
        other = (datetime.now() - timedelta(days=3)).strftime('%Y-%m-%d')
        assert db.get_reserved_order_by_odno("0000555", on_date=other) is None, \
            "발동일이 다른데 잡힌다 — 다음 달 같은 번호의 체결이 이 예약으로 라벨링된다"
    finally:
        conn = db._get_conn()
        conn.cursor().execute("DELETE FROM reserved_orders WHERE id=?", (rid,))
        conn.commit()
