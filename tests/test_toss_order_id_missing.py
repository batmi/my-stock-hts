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


# ---------------------------------------------------------------------------
# 주문번호 자리에 '지점 코드'가 들어가던 길 (2026-09-05)
#
# KIS 주문 응답의 KRX_FWDG_ORD_ORGNO 는 한국거래소 전송 주문 **조직(지점) 번호**다.
# 주문번호가 아니고, 같은 지점의 모든 주문이 같은 값을 갖는다. 호출부들이
# `output.ODNO or output.KRX_FWDG_ORD_ORGNO` 로 폴백하고 있었다.
#
# trades.odno 는 체결 대사의 유일 키다(db.get_trade_by_odno 는 그 값으로 접수 행을
# 찾아 손절률·점수·사유·실현손익을 체결 행에 상속한다). 지점 코드가 들어가면
#   · 그 주문의 접수 행을 못 찾거나(손절률 0.0 으로 리셋), 더 나쁘게는
#   · 같은 값을 가진 **다른 종목의 접수 행**을 물어 온다(ORDER BY id DESC LIMIT 1).
import ast
import os

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_ODNO_SITES = ("modules/trading.py", "modules/reserved_order_monitor.py",
               "modules/auto_trade/engine.py", "api/orders.py")


def test_지점코드를_주문번호_폴백으로_쓰지_않는다():
    bad = []
    for rel in _ODNO_SITES:
        path = os.path.join(_ROOT, rel)
        for i, line in enumerate(open(path, encoding="utf-8"), 1):
            if line.lstrip().startswith("#"):
                continue
            if "KRX_FWDG_ORD_ORGNO" not in line:
                continue
            # 요청 본문에 빈 값으로 넣거나, 응답 형태를 흉내 내는 것은 정상이다.
            if '"KRX_FWDG_ORD_ORGNO": ""' in line or "'KRX_FWDG_ORD_ORGNO': ''" in line:
                continue
            bad.append(f"{rel}:{i} {line.strip()}")
    assert not bad, (
        "KRX_FWDG_ORD_ORGNO 를 주문번호처럼 쓰고 있다 — 그것은 지점 코드다.\n  "
        + "\n  ".join(bad))


def test_정정_취소_응답도_주문번호를_요구한다():
    """정정은 새 주문번호를 채번한다 — 없으면 성공으로 보지 않는다."""
    from api import orders as orders_mod

    ok = {'rt_cd': '0', 'output': {'ODNO': '0000123456'}}
    assert orders_mod._require_odno_rc(ok, 'modify', '005930', '111') is ok

    for res in ({'rt_cd': '0', 'output': {'ODNO': ''}},
                {'rt_cd': '0', 'output': {}},
                {'rt_cd': '0', 'output': {'ODNO': '   ', 'KRX_FWDG_ORD_ORGNO': '00950'}}):
        got = orders_mod._require_odno_rc(res, 'cancel', '005930', '111')
        assert got['rt_cd'] == '1', f"주문번호 없는 성공이 통과했다: {res}"
        assert got['msg_cd'] == 'ORDER_UNKNOWN'
        # 대사 경로로 가지 않는다(재전송 위험이 없고, 다음 주기가 미체결을 다시 본다).
        assert not (got.get('output') or {}).get('ODNO')

    # 실패 응답은 손대지 않는다.
    fail = {'rt_cd': '1', 'msg1': '거부', 'output': {}}
    assert orders_mod._require_odno_rc(fail, 'cancel', '005930', '111') is fail


def test_불변식이_정정_취소_경로에_실제로_걸려_있다():
    """헬퍼만 있고 아무도 안 부르는 상태를 막는다."""
    src = open(os.path.join(_ROOT, "api/orders.py"), encoding="utf-8").read()
    tree = ast.parse(src)
    fn = next(n for n in tree.body
              if isinstance(n, ast.FunctionDef) and n.name == "revise_cancel_order")
    calls = [n for n in ast.walk(fn)
             if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)
             and n.func.id == "_require_odno_rc"]
    assert len(calls) >= 2, (
        f"revise_cancel_order 의 KIS 반환 경로 일부가 불변식을 지나지 않는다({len(calls)}곳)")
