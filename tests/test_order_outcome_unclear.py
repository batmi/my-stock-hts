"""'접수 여부를 모른다'를 '실패'로 접지 않는다.

[왜 · 2026-09-08] 전송 계층은 응답 유실을 이미 세 갈래로 나눠 준다
 (api/orders.py 의 _reconcile_unknown_order):
   · ORDER_RECOVERED  — 조회로 접수를 확인했다(주문번호까지 이어받는다)
   · ORDER_NOT_PLACED — 조회로 미접수를 확인했다
   · ORDER_UNKNOWN    — 대사 조회가 실패했거나 후보가 둘 이상이라 단정할 수 없다
 그런데 OrderManager.send_order 가 뒤의 둘을 **None 하나로 접어** 돌려줬다. 실측:

     미접수(대사로 확인)  → send_order 반환값 = None
     모름(대사 조회 실패)  → send_order 반환값 = None

 호출부의 `if not odno` 는 그 둘을 구별할 수 없고, 그래서:
   · 신규매수·피라미딩이 히트 캡 **선점분을 반납**한다. 살아 있을지 모르는 주문의
     리스크가 장부에서 사라지고 같은 주기의 다음 후보가 그 예산을 다시 쓴다.
     히트 캡은 브로커가 막아 주지 않는 우리 쪽 장부라 이것을 막을 것이 없다.
   · 운용자에게 "🚫 [매수 실패]" 라고 알린다. 사실이 아니다 — 그 말을 믿고 수동으로
     다시 사면 같은 종목에 두 번째 포지션이 생긴다. 대사 로직이 막으려던 바로 그것이다.
   · 매도는 방향이 반대다. 접수됐는데 '안 됐다'로 두면 다음 주기가 같은 수량을 다시 판다.
"""
import threading
from unittest.mock import MagicMock, patch

import pytest

from modules.auto_trade import engine

NOT_PLACED = {'rt_cd': '1', 'msg_cd': 'ORDER_NOT_PLACED',
              'msg1': '주문 미접수(응답 유실 후 대사 확인)', 'output': {}}
UNKNOWN = {'rt_cd': '1', 'msg_cd': 'ORDER_UNKNOWN',
           'msg1': '주문 결과 불명(응답 유실)', 'output': {}}
PLAIN_FAIL = {'rt_cd': '1', 'msg_cd': 'APBK0013',
              'msg1': '주문가능금액이 부족합니다', 'output': {}}


def _manager():
    om = object.__new__(engine.OrderManager)
    om._lock = threading.RLock()
    om.pending_orders = {}
    om.trader = MagicMock()
    om.trader._lock = threading.RLock()
    om.trader.log = lambda *a, **k: None
    om._alert_order_fail = MagicMock()
    return om


def _send(response, sent, om=None):
    om = om or _manager()
    with patch.object(engine.api, 'place_order', return_value=response), \
         patch.object(engine.api, 'send_telegram_message',
                      side_effect=lambda m: sent.append(m)), \
         patch.object(engine.utils, 'AccountContext', MagicMock()), \
         patch.object(engine.utils, 'system_trading_account', lambda: ("1", "01")):
        return om.send_order("005930", 10, "buy", name="삼성전자", price=70000)


# ---------------------------------------------------------------------------
# send_order 가 '모름'을 전달하는가
# ---------------------------------------------------------------------------

def test_an_unknown_outcome_is_raised_not_returned_as_none():
    with pytest.raises(engine.OrderOutcomeUnclear):
        _send(UNKNOWN, [])


def test_a_confirmed_non_placement_is_still_a_plain_none():
    """대사로 '미접수'를 확인한 것은 실패가 맞다 — 예외로 만들면 안 된다."""
    assert _send(NOT_PLACED, []) is None


def test_an_ordinary_rejection_is_still_a_plain_none():
    assert _send(PLAIN_FAIL, []) is None


# ---------------------------------------------------------------------------
# 운용자에게 뭐라고 말하는가
# ---------------------------------------------------------------------------

def test_the_operator_is_not_told_the_order_failed():
    sent = []
    with pytest.raises(engine.OrderOutcomeUnclear):
        _send(UNKNOWN, sent)
    body = "\n".join(sent)
    assert "실패" not in body, f"'모름'을 '실패'라고 알렸다: {body}"
    assert "결과 불명" in body
    assert "다시 주문하지 마십시오" in body, body


def test_an_ordinary_failure_is_still_called_a_failure():
    sent = []
    om = _manager()
    _send(PLAIN_FAIL, sent, om)
    assert om._alert_order_fail.called, "보통 실패는 종전대로 실패 알림을 탄다"
    body = om._alert_order_fail.call_args[0][3]
    assert "실패" in body


def test_the_unclear_alert_is_never_suppressed_as_a_repeat():
    """반복 억제(_alert_order_fail)에 걸리면 두 번째부터 침묵한다 — 이 알림은 눌러선 안 된다."""
    sent = []
    om = _manager()
    with pytest.raises(engine.OrderOutcomeUnclear):
        _send(UNKNOWN, sent, om)
    assert not om._alert_order_fail.called


def test_the_pre_registration_is_cleaned_up_even_when_unclear():
    """임시 선점 ID 가 남으면 그 종목의 다음 주문이 '이미 주문 중'으로 막힌다."""
    om = _manager()
    with pytest.raises(engine.OrderOutcomeUnclear):
        _send(UNKNOWN, [], om)
    assert not om.pending_orders.get("005930"), om.pending_orders


# ---------------------------------------------------------------------------
# 호출부 세 곳이 그 값을 실제로 다루는가 (구조로 고정한다)
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("marker", [
    "self.order_manager.send_order(cand['code']",      # 신규 매수
    "self.order_manager.send_order(code, add_qty",     # 피라미딩
    "self.order_manager.send_order(code, target_sell_qty",  # 매도
])
def test_every_send_order_call_site_handles_the_unclear_outcome(marker):
    """호출부가 하나라도 이 예외를 안 잡으면, 그 경로는 워커의 포괄 except 로 흘러가
    '판정을 받지 못했습니다' 같은 **사실과 다른** 경보를 띄운다."""
    src = open("modules/auto_trade/trader.py", encoding="utf-8").read()
    i = src.index(marker)
    window = src[max(0, i - 400):i + 900]
    assert "OrderOutcomeUnclear" in window, \
        f"{marker[:45]}… 호출부가 '모름'을 처리하지 않는다"


def _unclear_handlers():
    """trader.py 의 `except ... OrderOutcomeUnclear` 처리 블록들을 구문으로 뽑는다.

    소스를 글자로 잘라 보지 않는다 — 처음에 그렇게 짰다가 잘못된 구간을 봤다.
    """
    import ast

    tree = ast.parse(open("modules/auto_trade/trader.py", encoding="utf-8").read())
    out = []
    for n in ast.walk(tree):
        if not isinstance(n, ast.ExceptHandler) or n.type is None:
            continue
        if "OrderOutcomeUnclear" not in ast.unparse(n.type):
            continue
        out.append("\n".join(ast.unparse(b) for b in n.body))
    return out


def test_there_are_exactly_three_unclear_handlers():
    """신규매수·피라미딩·매도. 늘거나 줄면 이 파일의 다른 검사들이 낡은 것이다."""
    assert len(_unclear_handlers()) == 3, _unclear_handlers()


def test_no_unclear_handler_gives_back_the_reserved_heat():
    """선점분 반납은 '미접수'가 **확인됐을 때만** 한다.

    반납하면 살아 있을지 모르는 포지션의 리스크가 장부에서 사라지고, 같은 주기의 다음
    후보가 그 예산을 다시 쓴다. 히트 캡은 브로커가 막아 주지 않는 우리 쪽 장부다.
    """
    for body in _unclear_handlers():
        assert "portfolio_heat_amt" not in body, \
            f"'모름' 처리에서 선점분을 건드린다:\n{body}"


def test_no_unclear_handler_resends_the_order():
    """재전송은 이 모든 장치가 막으려던 것이다."""
    for body in _unclear_handlers():
        assert "send_order" not in body, f"'모름' 처리에서 다시 주문한다:\n{body}"


def test_every_unclear_handler_stops_that_path():
    """계속 진행하면 접수가 확인되지 않은 주문 위에 후속 처리가 얹힌다."""
    for body in _unclear_handlers():
        assert any(k in body for k in ("break", "return", "continue")), \
            f"'모름' 처리가 흐름을 멈추지 않는다:\n{body}"


def test_the_sell_path_does_not_fall_through_to_the_success_block():
    """접수가 확인되지 않은 매도로 거래기록·예약취소·앵커정리를 하면 안 된다."""
    src = open("modules/auto_trade/trader.py", encoding="utf-8").read()
    i = src.index("매도 결과 불명:")
    tail = src[i:i + 400]
    assert "return" in tail.split("\n\n")[0], "매도 워커가 성공 블록으로 흘러간다"
