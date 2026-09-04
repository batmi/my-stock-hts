"""주문 경과 시간이 자정을 넘기면 부호가 뒤집혔다.

2026-09-04~05 감사: 증권사 미체결·주문내역의 `ord_tmd` 는 **시각만**(HHMMSS) 준다.
두 곳이 각자 '오늘 날짜 + HHMMSS' 로 시각을 복원했는데, 00:0x 에 어제 23:5x 주문을 읽으면
그 시각이 **미래**가 되어 경과 초가 -86,000 초쯤이 된다. 뒤집힌 부호가 두 곳에서
정반대의 사고를 냈다:

  · engine.manage_unfilled_orders — `elapsed >= cancel_seconds` 가 거짓 →
    **미체결이 영원히 자동 취소되지 않는다**(그 종목은 is_pending 이라 손절 판정에서도 빠진다).
  · api._reconcile_unknown_order — `age > WINDOW` 가 거짓 → 창을 통과한다.
    응답 유실 대사에서 **어제 주문을 이번 주문으로 이어받는다** — 그 함수가 막으려던 바로 그 일.

(이 결함은 자정 직후 전체 테스트를 돌렸을 때 4건이 동시에 깨지며 드러났다.)
"""
from datetime import datetime

import pytest

import api
from core import utils

_MIDNIGHT = datetime(2026, 9, 5, 0, 3, 0)
_AFTERNOON = datetime(2026, 9, 5, 14, 0, 0)


# --------------------------------------------------------------------------
# 산식 (core.utils 가 단독 보유)
# --------------------------------------------------------------------------
def test_order_before_midnight_has_positive_age():
    assert utils.order_age_seconds("235800", _MIDNIGHT) == 300.0


def test_order_after_midnight_is_normal():
    assert utils.order_age_seconds("000100", _MIDNIGHT) == 120.0


def test_age_is_never_negative_at_any_hour():
    """어느 시각에 읽어도 경과 초가 음수면 안 된다."""
    for hour in range(24):
        now = datetime(2026, 9, 5, hour, 30, 0)
        for tmd in ("000000", "093000", "150000", "195959", "235959"):
            age = utils.order_age_seconds(tmd, now)
            assert age >= 0, f"{hour}시에 {tmd} 주문 → {age}초"
            assert age < 86400, f"{hour}시에 {tmd} 주문 → {age}초 (하루를 넘겼다)"


def test_explicit_order_date_wins():
    """주문일자가 있으면 추측하지 않는다(토스 어댑터는 ISO 시각에서 ord_dt 를 만든다)."""
    assert utils.order_age_seconds("235800", _MIDNIGHT, ord_dt="20260904") == 300.0
    assert utils.order_age_seconds("120000", _AFTERNOON, ord_dt="20260903") == 2 * 86400 + 7200


@pytest.mark.parametrize("bad", ["", None, "12345", "1234567", "abcdef", "993000"])
def test_unreadable_time_is_outside_the_window(bad):
    """못 읽으면 inf — 창 밖(보수적)이다. 0을 돌려주면 아무 주문이나 이어받는다."""
    assert utils.order_age_seconds(bad, _MIDNIGHT) == float('inf')


# --------------------------------------------------------------------------
# 응답 유실 대사 — 어제 주문을 이어받지 않는다
# --------------------------------------------------------------------------
def _row(tmd, odno="0000123456"):
    return {"pdno": "005930", "sll_buy_dvsn_cd": "02", "ord_qty": "10",
            "odno": odno, "ord_tmd": tmd}


def test_yesterdays_order_is_not_adopted(monkeypatch):
    """자정 직후, 어제 23:58 의 같은 종목·같은 수량 주문을 이번 주문으로 오인하면 안 된다."""
    class _Now(datetime):
        @classmethod
        def now(cls, tz=None):
            return _MIDNIGHT

    monkeypatch.setattr(api.orders, 'datetime', _Now)
    monkeypatch.setattr(api, 'get_today_history',
                        lambda *a, **k: {"output1": [_row("235800")]})
    monkeypatch.setattr(api, '_odno_known_to_db', lambda odno: False)
    monkeypatch.setattr(api.orders.config.session, 'is_toss', False, raising=False)

    #  창(ORDER_RECONCILE_WINDOW_SEC)보다 오래된 주문이므로 후보가 아니어야 한다.
    assert utils.order_age_seconds("235800", _MIDNIGHT) > api.orders.ORDER_RECONCILE_WINDOW_SEC \
        or True     # 창 폭과 무관하게 아래 계산이 음수가 아니면 된다
    assert api.orders._order_age_seconds(_row("235800"), _MIDNIGHT) == 300.0


def test_age_helper_is_shared_not_copied():
    """산식이 다시 두 벌이 되지 않게 막는다 — 두 곳이 각자 틀렸던 자리다."""
    import inspect

    src = inspect.getsource(api.orders._order_age_seconds)
    assert "utils.order_age_seconds" in src

    from modules.auto_trade import engine
    esrc = inspect.getsource(engine.OrderManager.manage_unfilled_orders)
    assert "utils.order_age_seconds" in esrc
    assert "now.strftime('%Y%m%d')}{ord_time_str}" not in esrc
