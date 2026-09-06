"""미래 주문이라는 것은 없다 — 음수 경과 시간은 값이 아니라 '모른다'다.

[왜] order_age_seconds 는 2026-09-04 에 자정 넘김을 고쳤는데, 그 방어가 **날짜를
 추측하는 경로에만** 걸렸다. ord_dt 를 받은 쪽은 그대로 빼서 음수를 돌려준다.
 실측(now=2026-09-07 09:05):
     ord_dt 로 미래 시각(09:10) → age = -300.0
     ord_dt 가 내일             → age = -86,100.0

 부호가 뒤집힌 값은 두 곳에서 정반대의 사고를 낸다(그 함수의 독스트링이 적어 둔 그것이다):
   · engine.manage_unfilled_orders — `elapsed >= cancel_seconds` 가 거짓이 되어
     **미체결이 영원히 자동 취소되지 않는다**(그 종목은 is_pending 이라 손절 판정에서도 빠진다).
   · api._reconcile_unknown_order — `age > WINDOW` 가 거짓이 되어 창을 통과한다.
     응답 유실 대사에서 **어제 주문을 이번 주문으로 이어받는다**.

 증권사 서버 시각이 앞서거나 ord_dt 가 하루 앞이면 바로 그 상태다. 운영기는 RTC 없는
 라즈베리파이라 NTP 동기 전 로컬 시계가 뒤처지는 것이 실재한다([[deployment-raspberry-pi]]).
 [[order-age-midnight]] · [[unknown-vs-empty]]
"""
from datetime import datetime

import pytest

from core import utils

NOW = datetime(2026, 9, 7, 9, 5, 0)
INF = float('inf')


def _age(tmd, ord_dt=None):
    return utils.order_age_seconds(tmd, now=NOW, ord_dt=ord_dt)


def test_정상_경과는_그대로다():
    assert _age("090000", "20260907") == pytest.approx(300.0)


def test_ord_dt_없이_자정을_넘긴_주문은_전날로_본다():
    """종전 수정이 덮던 경로 — 그대로 살아 있어야 한다."""
    assert _age("235900") == pytest.approx(32760.0)


@pytest.mark.parametrize("label,tmd,dt", [
    ("증권사 시각이 앞선다", "091000", "20260907"),
    ("ord_dt 가 내일", "090000", "20260908"),
])
def test_미래_주문은_모름이다(label, tmd, dt):
    assert _age(tmd, dt) == INF, f"{label}: 음수가 그대로 나가 두 사고를 낸다"


def test_읽을_수_없는_시각은_종전대로_모름이다():
    assert _age("") == INF
    assert _age("12345") == INF
    assert _age("abcdef") == INF


def test_모름은_두_호출부_모두에서_보수적이다():
    """inf 를 고른 이유 — 미체결이면 취소하고, 대사면 후보에서 뺀다."""
    age = _age("091000", "20260907")
    assert age >= 60, "미체결 자동취소 창(cancel_seconds)을 넘어야 취소된다"
    assert age > 180, "응답 유실 대사 창(ORDER_RECONCILE_WINDOW_SEC) 밖이어야 안전하다"


def test_시계_어긋남은_로그로_드러난다(caplog):
    import logging
    with caplog.at_level(logging.WARNING, logger="core.utils"):
        _age("091000", "20260907")
    assert any("미래" in r.message and "NTP" in r.message for r in caplog.records), \
        f"조용히 넘어갔다: {[r.message for r in caplog.records]}"
