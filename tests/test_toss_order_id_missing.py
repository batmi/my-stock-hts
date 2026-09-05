"""주문번호 없는 '성공'은 성공이 아니다.

[왜 이 파일이 있나 · 2026-09-05]
토스 주문 어댑터가 이렇게 생겼었다:

    odno = (r or {}).get('orderId', '')
    return {'rt_cd': '0', ..., 'output': {'ODNO': odno, ...}}

응답이 2xx 인데 본문을 못 읽거나(브로커 계층이 그때 None 을 돌려줬다) orderId 가 비면
**빈 주문번호가 '접수 성공'으로 올라간다.** 서버는 접수했는데 우리는 그 주문을 가리킬
수단이 없다는 뜻이고, 그 상태는 조용히 나쁘다:

  · 체결 대사(ConclusionMonitor)가 odno 로 찾으므로 그 체결을 영영 못 잡는다
  · 미체결 자동 취소도 못 찾는다
  · pending_orders 에 '' 로 남아 그 종목은 is_pending → **매도 워커에서 통째로 빠진다**
    = 손절·트레일링이 멈춘다

'모름'은 이 저장소에 이미 자리가 있다 — OrderOutcomeUnknown 을 올리면
api.orders._reconcile_unknown_order 가 당일 주문내역으로 대사한다([[order-timeout-no-resend]]).
재전송하지 않고 조회로 확인하는 그 경로가 정답이다.
"""
from unittest.mock import patch

import pytest

import api
import config
from api import toss as toss_adapter
from brokers import toss_api


@pytest.fixture(autouse=True)
def toss_mode(monkeypatch):
    monkeypatch.setattr(config.session, "is_toss", True, raising=False)
    monkeypatch.setattr(config.session, "is_paper", False, raising=False)


@pytest.mark.parametrize("response", [
    None,                       # 브로커 계층이 본문을 못 읽었을 때
    {},                         # result 는 왔는데 orderId 가 없다
    {"orderId": ""},            # 빈 문자열
    {"orderId": "   "},         # 공백만
])
def test_주문번호가_없으면_성공으로_올리지_않는다(response):
    with patch.object(toss_adapter.toss_api, "create_order", return_value=response):
        with pytest.raises(api.OrderOutcomeUnknown):
            toss_adapter._toss_place_order("domestic", "buy", "005930", 1, 70000, "00")


def test_정정_취소도_같다():
    with patch.object(toss_adapter.toss_api, "cancel_order", return_value={"orderId": ""}):
        with pytest.raises(api.OrderOutcomeUnknown):
            toss_adapter._toss_revise_cancel("domestic", "cancel", "ORD-1", "005930", 1, 0, "00")


def test_정상_주문번호는_그대로_성공이다():
    with patch.object(toss_adapter.toss_api, "create_order",
                      return_value={"orderId": "TOSS-123-456"}):
        res = toss_adapter._toss_place_order("domestic", "buy", "005930", 1, 70000, "00")
    assert res["rt_cd"] == "0"
    assert res["output"]["ODNO"] == "TOSS-123-456"


def test_주문_결과_불명은_대사_경로로_간다():
    """place_order 가 예외를 흘리지 않고 당일 주문내역 대사로 넘긴다."""
    with patch.object(toss_adapter.toss_api, "create_order", return_value={"orderId": ""}), \
         patch.object(api, "_reconcile_unknown_order",
                      return_value={"rt_cd": "1", "msg_cd": "ORDER_UNKNOWN",
                                    "msg1": "대사함", "output": {}}) as rec:
        res = api.place_order("domestic", "buy", "005930", 1, 70000, "00")
    assert rec.called, "결과 불명이 대사 경로로 가지 않았다"
    assert res["msg_cd"] == "ORDER_UNKNOWN"


# --------------------------------------------------------------------------
# 브로커 계층: 2xx 인데 본문을 해석 못 하는 경우
# --------------------------------------------------------------------------
class _Res:
    status_code = 200
    headers = {}
    text = "not json"

    def json(self):
        raise ValueError("no json")


def test_주문_요청은_해석불가_2xx를_모름으로_올린다(monkeypatch):
    monkeypatch.setattr(toss_api, "get_access_token", lambda *a, **k: "tok")
    monkeypatch.setattr(toss_api, "_throttle", lambda g: None)
    monkeypatch.setattr(toss_api.requests, "request", lambda *a, **k: _Res())
    with pytest.raises(toss_api.TossOrderOutcomeUnknown):
        toss_api._request("POST", "/orders", "order", json_body={},
                          account=False, idempotent=False)


def test_조회_요청은_종전대로_None(monkeypatch):
    """조회는 몇 번 다시 물어도 무해하다 — 여기까지 예외로 만들면 화면이 죽는다."""
    monkeypatch.setattr(toss_api, "get_access_token", lambda *a, **k: "tok")
    monkeypatch.setattr(toss_api, "_throttle", lambda g: None)
    monkeypatch.setattr(toss_api.requests, "get", lambda *a, **k: _Res())
    assert toss_api._request("GET", "/prices", "quote", account=False) is None
