"""토스 모드도 응답이 유실된 주문을 재전송하지 않는가 — 중복 주문 방지.

[배경 · 2026-09-04 감사] 재전송 금지는 2026-08-10에 KIS 경로에만 들어갔다
(api/http.py 의 OrderOutcomeUnknown, api/orders.py 의 대사). 토스는 place_order 첫 줄에서
갈라져 그 계층을 지나지 않고, 브로커 계층(brokers/toss_api._request)은 메서드를 가리지 않고
네트워크 오류·5xx 를 재시도했다(retries=2 → 최대 3회 전송). 즉 mode 3 에서는 응답만
유실돼도 같은 주문이 두 번, 세 번 나갈 수 있었다.

포지션이 두 배가 되면 손절폭·변동성 한도·포트폴리오 히트 캡이 한꺼번에 무의미해진다.
손실 자체보다 통제 수단이 조용히 사라지는 것이 문제다(KIS 쪽 같은 계약의 사유와 동일).

여기서 고정하는 계약:
  1. 요청을 보낸 뒤 응답을 못 받으면(ReadTimeout·전송 중 끊김) 재전송하지 않는다
  2. 5xx 도 마찬가지다 — 서버가 접수한 뒤 죽었을 수 있다
  3. 연결조차 못 한 ConnectTimeout, 서버가 실행 없이 거절한 429 는 그대로 재시도한다
  4. 조회(GET)는 종전대로 재시도한다 — 여기까지 막으면 장애 내성만 떨어진다
  5. 유실된 주문은 재전송이 아니라 **조회로 대사**한다. 토스 당일 체결이력은 CLOSED 만
     주므로 미체결까지 합쳐야 KIS 와 같은 범위가 된다
"""
import json
from datetime import datetime

import pytest
import requests

import api
import config
from brokers import toss_api


class FakeResponse:
    def __init__(self, status_code, body=None, headers=None):
        self.status_code = status_code
        self._body = body
        self.text = json.dumps(body) if body is not None else ""
        self.headers = headers or {}

    def json(self):
        if self._body is None:
            raise ValueError("no json")
        return self._body


@pytest.fixture
def toss_wire(monkeypatch):
    """토큰·스로틀·계좌 헤더를 걷어내고 전송 횟수만 재는 배선."""
    monkeypatch.setattr(toss_api, "get_access_token", lambda *a, **k: "TOK")
    monkeypatch.setattr(toss_api, "_throttle", lambda group: None)
    monkeypatch.setattr(toss_api.time, "sleep", lambda s: None)
    monkeypatch.setattr(config.session, "toss_account_seq", 1, raising=False)
    sent = []

    def install(outcome):
        def _fake(method, url, **kw):
            sent.append(method)
            if isinstance(outcome, Exception):
                raise outcome
            return outcome
        monkeypatch.setattr(toss_api.requests, "request", _fake)
        monkeypatch.setattr(toss_api.requests, "get",
                            lambda url, **kw: _fake("GET", url, **kw))
        return sent

    return install


# ─────────────────────────────────────────────
# 1. 브로커 계층 — 주문 POST가 몇 번 나가는가
# ─────────────────────────────────────────────

@pytest.mark.parametrize("outcome", [
    requests.exceptions.ReadTimeout("timeout"),
    requests.exceptions.ConnectionError("전송 중 끊김"),
    requests.exceptions.ChunkedEncodingError("본문 끊김"),
])
def test_lost_order_response_is_not_resent(toss_wire, outcome):
    """[핵심] 이 테스트가 이번 수정의 전부다 — 주문 POST가 두 번 나가면 안 된다."""
    sent = toss_wire(outcome)
    with pytest.raises(toss_api.TossOrderOutcomeUnknown):
        toss_api.create_order(symbol="005930", side="BUY", quantity=10, price=70000)
    assert len(sent) == 1, f"주문이 {len(sent)}번 전송됐다 — 중복 주문이 나간다"


def test_server_error_on_an_order_is_unknown_not_retried(toss_wire):
    """5xx 는 서버가 이미 접수한 뒤 죽었을 수 있다 — '실패'로 단정해 다시 보내면 안 된다."""
    sent = toss_wire(FakeResponse(503, {"error": {"code": "server-error", "message": "busy"}}))
    with pytest.raises(toss_api.TossOrderOutcomeUnknown):
        toss_api.create_order(symbol="005930", side="BUY", quantity=10, price=70000)
    assert len(sent) == 1


def test_connect_timeout_is_still_retried(toss_wire):
    """연결 자체가 안 됐으면 주문이 나갔을 수 없다 — 재시도해야 장애 내성이 산다."""
    sent = toss_wire(requests.exceptions.ConnectTimeout("connect"))
    with pytest.raises(toss_api.TossApiError) as e:
        toss_api.create_order(symbol="005930", side="BUY", quantity=10, price=70000)
    assert not isinstance(e.value, toss_api.TossOrderOutcomeUnknown)
    assert len(sent) > 1


def test_rate_limit_on_an_order_is_still_retried(toss_wire):
    """429 는 서버가 실행하지 않고 거절한 것이다 — 결과를 안다."""
    sent = toss_wire(FakeResponse(429, {"error": {"code": "rate-limit", "message": "slow"}},
                                  headers={"Retry-After": "0"}))
    with pytest.raises(toss_api.TossApiError) as e:
        toss_api.create_order(symbol="005930", side="BUY", quantity=10, price=70000)
    assert not isinstance(e.value, toss_api.TossOrderOutcomeUnknown)
    assert len(sent) > 1


def test_a_read_timeout_on_a_query_is_still_retried(toss_wire):
    sent = toss_wire(requests.exceptions.ReadTimeout("timeout"))
    with pytest.raises(toss_api.TossApiError):
        toss_api.get_price("005930")
    assert len(sent) > 1, "조회 재시도가 사라졌다"


@pytest.mark.parametrize("call", [
    lambda: toss_api.modify_order("O1", quantity=5, price=1000),
    lambda: toss_api.cancel_order("O1"),
])
def test_modify_and_cancel_are_not_resent_either(toss_wire, call):
    sent = toss_wire(requests.exceptions.ReadTimeout("timeout"))
    with pytest.raises(toss_api.TossOrderOutcomeUnknown):
        call()
    assert len(sent) == 1


# ─────────────────────────────────────────────
# 2. 어댑터 — KIS 와 같은 예외로 올라오는가
# ─────────────────────────────────────────────

def _unknown(*a, **k):
    """브로커 계층이 던지는 토스 전용 예외."""
    raise toss_api.TossOrderOutcomeUnknown("order-outcome-unknown", "ReadTimeout")


def _unknown_shared(*a, **k):
    """어댑터가 옮겨 던진 공용 예외 — place_order 가 대사로 받아야 하는 신호."""
    raise api.OrderOutcomeUnknown("ReadTimeout")


def test_adapter_raises_the_shared_exception(monkeypatch):
    """토스 전용 예외로 남으면 place_order 가 못 알아보고 '실패'로 흘려보낸다."""
    monkeypatch.setattr(toss_api, "create_order", _unknown)
    with pytest.raises(api.OrderOutcomeUnknown):
        api._toss_place_order("domestic", "buy", "005930", 10, 70000, "00")


def test_adapter_still_reports_ordinary_failures_as_failures(monkeypatch):
    """서버가 거부한 주문은 '모름'이 아니다 — 종전대로 rt_cd=1 이어야 한다."""
    def _reject(*a, **k):
        raise toss_api.TossApiError("insufficient-cash", "예수금 부족", status=400)

    monkeypatch.setattr(toss_api, "create_order", _reject)
    res = api._toss_place_order("domestic", "buy", "005930", 10, 70000, "00")
    assert res["rt_cd"] == "1" and res["msg_cd"] == "insufficient-cash"


# ─────────────────────────────────────────────
# 3. 대사 — 재전송 대신 조회로 확인하는가
# ─────────────────────────────────────────────

@pytest.fixture
def toss_mode(monkeypatch):
    monkeypatch.setattr(config.session, "is_toss", True, raising=False)
    monkeypatch.setattr(api, "_paper_active", lambda: False)
    monkeypatch.setattr(api, "_odno_known_to_db", lambda odno: False)
    monkeypatch.setattr(api, "_toss_place_order", _unknown_shared)


def _open_row(code="005930", side="02", qty=10, odno="TOSS-1"):
    return {"odno": odno, "pdno": code, "sll_buy_dvsn_cd": side, "ord_qty": str(qty),
            "rmn_qty": str(qty), "ord_tmd": datetime.now().strftime("%H%M%S")}


def test_an_unfilled_order_that_landed_is_adopted_not_resent(toss_mode, monkeypatch):
    """접수됐지만 아직 미체결인 주문 — 토스 체결이력에는 없고 미체결 목록에만 있다."""
    monkeypatch.setattr(api, "get_today_history", lambda *a, **k: {"rt_cd": "0", "output1": []})
    monkeypatch.setattr(api, "_toss_open_orders", lambda market: [_open_row()])

    res = api.place_order("domestic", "buy", "005930", 10, 70000, "00")
    assert res["rt_cd"] == "0" and res["msg_cd"] == "ORDER_RECOVERED"
    assert res["output"]["ODNO"] == "TOSS-1"


def test_an_order_that_never_landed_is_reported_as_not_placed(toss_mode, monkeypatch):
    monkeypatch.setattr(api, "get_today_history", lambda *a, **k: {"rt_cd": "0", "output1": []})
    monkeypatch.setattr(api, "_toss_open_orders", lambda market: [])

    res = api.place_order("domestic", "buy", "005930", 10, 70000, "00")
    assert res["rt_cd"] == "1" and res["msg_cd"] == "ORDER_NOT_PLACED"


def test_reconcile_does_not_double_count_the_same_order(monkeypatch):
    """체결이력과 미체결에 같은 주문번호가 겹쳐도 후보가 둘로 늘면 안 된다
    (후보 2건은 '단정 불가'로 처리돼 운용자 호출까지 간다)."""
    monkeypatch.setattr(config.session, "is_toss", True, raising=False)
    monkeypatch.setattr(api, "get_today_history", lambda *a, **k: {"rt_cd": "0", "output1": [_open_row()]})
    monkeypatch.setattr(api, "_toss_open_orders", lambda market: [_open_row()])

    rows = api.orders._reconcile_rows()
    assert len([r for r in rows if r["odno"] == "TOSS-1"]) == 1


def test_kis_mode_does_not_call_the_toss_listing(monkeypatch):
    """게이트가 모드에 걸려 있는지 — KIS 에서 토스 조회를 부르면 예외가 난다."""
    monkeypatch.setattr(config.session, "is_toss", False, raising=False)
    monkeypatch.setattr(api, "get_today_history", lambda *a, **k: {"rt_cd": "0", "output1": [_open_row()]})
    monkeypatch.setattr(api, "_toss_open_orders",
                        lambda market: pytest.fail("KIS 모드에서 토스 미체결을 조회했다"))

    assert len(api.orders._reconcile_rows()) == 1


def test_toss_modify_cancel_is_left_to_the_next_cycle(monkeypatch):
    """정정·취소는 결과가 애매해도 손해가 누적되지 않는다 — 실패로 돌려 다음 주기에 맡긴다."""
    monkeypatch.setattr(config.session, "is_toss", True, raising=False)
    monkeypatch.setattr(api, "_paper_active", lambda: False)
    monkeypatch.setattr(api, "_toss_revise_cancel", _unknown_shared)

    res = api.revise_cancel_order("domestic", "cancel", "TOSS-1", "005930", 10, 0, "02", "00")
    assert res["rt_cd"] == "1" and res["msg_cd"] == "ORDER_UNKNOWN"
