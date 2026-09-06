"""토스 매도가능수량: 응답이 와도 값이 없으면 '0주'가 아니라 '모름'이다.

[왜] _toss_sellable_qty 는 "조회 실패는 None(=알 수 없음)"이라 적어 두고 그 규칙을
 **예외에만** 걸어 뒀다. 응답은 200 인데 sellableQuantity 가 빠져 있거나 null 이거나
 이름이 바뀌면 `_toss_int(None)` 이 0 으로 접어 다시 '팔 수 없다'가 된다.
 호출부(trader._sell_worker)는 0 을 '팔 수 없는 상태'로 읽어 **매도를 중단**한다 —
 일시적 스키마 변화가 손절을 거르는 결과가 된다.

 가정이 아니다 — 토스 국내 잔고에서 ord_psbl_qty 가 사라져 손절·트레일링이 전면 정지한
 적이 있다([[toss-balance-sell-gate]]). 그때와 같은 모양이 한 필드 아래에 남아 있었다.
 실측: 필드 누락·null·이름 변경·숫자 아닌 값 넷 다 0(매도 중단)으로 읽혔다.
 [[unknown-vs-empty]]
"""
from unittest.mock import patch

import pytest

from api import toss as T

UNKNOWN = [
    ("필드 누락", {}),
    ("값이 null", {"sellableQuantity": None}),
    ("이름이 바뀜", {"sellable_quantity": 8}),
    ("숫자가 아님", {"sellableQuantity": "N/A"}),
    ("응답이 None", None),
    ("dict 가 아님", []),
]


@pytest.mark.parametrize("label,payload", UNKNOWN)
def test_값을_못_읽으면_모름이다(label, payload):
    with patch("brokers.toss_api.get_sellable_quantity", return_value=payload):
        got = T._toss_sellable_qty("005930")
    assert got is None, f"{label}: 0(매도 중단)으로 읽힌다 — 손절이 거른다"


def test_응답이_실제로_0이면_0이다():
    """'모름'을 넓히다가 진짜 0을 놓치면 반대 방향으로 틀린다(없는 주식을 팔려 든다)."""
    with patch("brokers.toss_api.get_sellable_quantity", return_value={"sellableQuantity": 0}):
        assert T._toss_sellable_qty("005930") == 0
    with patch("brokers.toss_api.get_sellable_quantity", return_value={"sellableQuantity": "0"}):
        assert T._toss_sellable_qty("005930") == 0


def test_정상_값은_그대로_돌려준다():
    with patch("brokers.toss_api.get_sellable_quantity", return_value={"sellableQuantity": "8"}):
        assert T._toss_sellable_qty("005930") == 8


def test_조회_예외는_종전대로_모름이다():
    import brokers.toss_api as toss_api

    with patch("brokers.toss_api.get_sellable_quantity",
               side_effect=toss_api.TossApiError("E001", "boom")):
        assert T._toss_sellable_qty("005930") is None
