"""NXT 주문 구간은 시세 구간과 다르다 — 그 차이를 고정한다.

[왜 두 경계가 다른가] 시세는 '지금 화면에 뿌릴 값이 NXT 체결가인가'를 묻고, 주문은
'지금 낸 주문이 NXT 로 나가는가'를 묻는다. NXT 프리마켓은 08:00~08:50 이고 08:50~09:00
은 KRX 시가 단일가에 맞춰 쉰다. 그 10분 동안 화면은 여전히 마지막 NXT 체결가를 보여야
하지만(_nxt_quote_phase 의 'active'), 주문은 KRX 동시호가로 들어가므로 **시장가가 정상
접수된다**. 시세 경계를 주문에 쓰면 그 10분의 시장가가 지정가로 바뀌어, 동시호가에서
체결되지 않을 수 있다.
"""
from datetime import datetime

import pytest

import api
from modules import trading


def _at(hm):
    return datetime(2026, 9, 4, int(hm[:2]), int(hm[2:]))


@pytest.mark.parametrize("hm, expected", [
    ("0759", False),   # 개장 전
    ("0800", True),    # NXT 프리마켓 시작
    ("0849", True),
    ("0850", True),    # 경계는 포함 — 종전 구현과 같게 둔다
    ("0851", False),   # NXT 휴식 · KRX 시가 단일가 접수
    ("0859", False),
    ("0900", False),   # KRX 정규장
    ("1200", False),
    ("1529", False),
    ("1530", True),    # NXT 애프터마켓
    ("1900", True),
    ("2000", True),
    ("2001", False),
])
def test_the_order_window_is_nxt_hours_not_quote_hours(hm, expected):
    assert api.nxt_order_window(_at(hm)) is expected


def test_the_ten_minutes_before_the_open_differ_from_the_quote_phase():
    """08:50~09:00 — 시세는 여전히 NXT, 주문은 이미 NXT 가 아니다."""
    assert api.nxt_order_window(_at("0855")) is False
    # 시세 경계(domestic_session_phase)는 같은 시각을 nxt_pre 로 본다. 둘이 같아지면
    # 어느 한쪽이 다른 쪽 용도로 잘못 쓰였다는 뜻이다.
    lo, hi = api.NXT_ORDER_WINDOWS[0]
    assert (lo, hi) == ("0800", "0850")


@pytest.mark.parametrize("path", [
    "modules/trading.py",
    "modules/reserved_order_monitor.py",
    "modules/auto_trade/trader.py",
])
def test_no_one_keeps_a_private_copy_of_the_window(path):
    """같은 구간이 네 벌 복사돼 있었다. 한 벌만 고치면 화면마다 다르게 동작한다."""
    src = open(path, encoding='utf-8').read()
    code = "\n".join(l.split("  #")[0] for l in src.splitlines()
                     if not l.strip().startswith("#"))
    assert '"0850"' not in code, f"{path}: NXT 구간을 직접 들고 있다"


# ----------------------------------------------------------------------------
# TIME 예약이 장 밖에서 발동하던 것
# ----------------------------------------------------------------------------

def _monitor():
    from modules.reserved_order_monitor import ReservedOrderMonitor
    ReservedOrderMonitor._instance = None
    return ReservedOrderMonitor()


TIME_ORDER = {
    'id': 7, 'cano': '12345678', 'acnt': '01', 'code': '005930', 'name': '삼성전자',
    'market': 'KR', 'order_type': 'sell', 'qty': 1, 'order_price': 0,
    'condition_type': 'TIME', 'target_price': 0.0, 'target_time': '1400',
    'expire_dt': '20991231', 'composite_json': None,
}


def _run_time_order(monkeypatch, session_open):
    """지정 시각(14:00)이 이미 지난 뒤 감시 주기가 한 번 도는 상황."""
    import modules.reserved_order_monitor as rom

    executed = []
    m = _monitor()
    monkeypatch.setattr(rom.db_manager.db, 'get_pending_reserved_orders',
                        lambda: [dict(TIME_ORDER)], raising=False)
    monkeypatch.setattr(rom.api, 'domestic_trading_session_open', lambda: session_open)
    monkeypatch.setattr(rom.api, 'get_current_price', lambda *a, **k: 70000.0)
    monkeypatch.setattr(rom._at_common(), 'is_single_price_break', lambda *a, **k: False)
    monkeypatch.setattr(rom.ReservedOrderMonitor, '_execute_order',
                        lambda self, o, r: executed.append((o['id'], r)))
    m._check_orders()
    return executed


def test_a_time_order_does_not_fire_after_the_market_has_closed(monkeypatch):
    """낮에 꺼져 있다가 밤에 켜면, 14:00 예약이 그 순간 발주돼 거부되고 FAILED 로 굳었다.

    예약은 대개 손절·익절이라 한 번 소진되면 보호가 사라진다. 장이 닫혀 있으면 발동하지
    않는다 — 다음 거래 시간에 다시 본다.
    """
    assert _run_time_order(monkeypatch, session_open=False) == []


def test_a_time_order_still_fires_while_the_market_is_open(monkeypatch):
    """게이트가 TIME 을 통째로 막아 버리면 안 된다."""
    fired = _run_time_order(monkeypatch, session_open=True)
    assert [oid for oid, _ in fired] == [7], fired
    assert "지정 시간" in fired[0][1]
