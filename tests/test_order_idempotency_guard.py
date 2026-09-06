"""주문은 재전송되면 안 된다 — 그 규칙이 새 경로에서 조용히 풀리지 않게 못 박는다.

[왜 구문으로 잡나 · 2026-09-06]
 두 계층 모두 '상태를 바꾸는 요청'을 **따로 표시**해서 재시도에서 제외한다.

   · KIS  : api.http._STATE_CHANGING_URL_HINTS 에 URL 조각이 들어 있어야 한다.
   · 토스 : brokers.toss_api._request(..., idempotent=False) 를 넘겨야 한다.

 둘 다 **기본값이 '재전송해도 되는 요청'** 이다. 그래서 주문 엔드포인트가 하나 늘거나
 URL 이 바뀌면, 아무도 에러를 보지 못한 채 그 경로만 재전송 가능 상태가 된다. 응답을
 못 받은 주문을 다시 보내면 포지션이 하나 더 생기고, 그건 되돌릴 수 없다
 ([[order-timeout-no-resend]]).
"""
import ast
import inspect
import re

import api.http as http
import brokers.toss_api as toss_api
import core.constants as constants


def _all_urls(obj, out=None):
    out = [] if out is None else out
    if isinstance(obj, str):
        out.append(obj)
    elif isinstance(obj, dict):
        for v in obj.values():
            _all_urls(v, out)
    elif isinstance(obj, (list, tuple)):
        for v in obj:
            _all_urls(v, out)
    return out


#  '주문 계열'로 볼 URL 조각. 조회(inquire-)는 몇 번 다시 보내도 무해하다.
_ORDER_URL_RE = re.compile(r"/trading/(order|order-cash|order-credit|order-rvsecncl|order-resv)\b")


def test_KIS_주문_URL은_전부_비재시도_목록에_있다():
    urls = [u for u in _all_urls(constants.API_URLS) if _ORDER_URL_RE.search(u)]
    assert urls, "constants 에서 주문 URL 을 하나도 못 찾았다 — 검사기가 낡았다"
    missed = [u for u in urls
              if not any(h in u for h in http._STATE_CHANGING_URL_HINTS)]
    assert not missed, (
        "재시도에서 제외되지 않는 주문 URL 이 있다 — 응답 유실 시 재전송되어 "
        f"포지션이 하나 더 생긴다:\n  " + "\n  ".join(missed))


def test_조회_URL까지_비재시도로_묶지_않는다():
    """반대 방향도 지킨다 — 조회가 재시도를 잃으면 일시 장애가 곧 판정 실패가 된다."""
    inquiries = [u for u in _all_urls(constants.API_URLS) if "inquire-" in u]
    assert inquiries, "조회 URL 을 못 찾았다 — 검사기가 낡았다"
    wrongly = [u for u in inquiries
               if any(h in u for h in http._STATE_CHANGING_URL_HINTS)]
    assert not wrongly, f"조회가 비재시도로 묶였다: {wrongly}"


def test_상태변경_판정은_GET을_제외한다():
    """같은 URL이라도 GET 은 조회다 — 판정이 메서드를 무시하면 조회가 재시도를 잃는다."""
    order_url = "/uapi/domestic-stock/v1/trading/order-cash"
    assert http._is_state_changing("POST", order_url) is True
    assert http._is_state_changing("GET", order_url) is False


def test_응답을_못_받은_것과_연결_실패를_가른다():
    """ConnectTimeout 은 주문이 나갈 수 없었으므로 재전송해도 안전하다.
    ReadTimeout·전송 중 끊김은 이미 체결됐을 수 있다 — 여기서 가르지 못하면
    '재전송 금지'가 통째로 무의미해진다."""
    import requests as rq
    assert http._is_response_unknown(rq.exceptions.ConnectTimeout()) is False
    for exc in (rq.exceptions.ReadTimeout(), rq.exceptions.ConnectionError(),
                rq.exceptions.ChunkedEncodingError()):
        assert http._is_response_unknown(exc) is True, type(exc).__name__


def test_토스_주문_경로는_전부_idempotent_False_다():
    """`_request` 의 기본값이 idempotent=True 다 — 안 넘기면 재전송된다."""
    src = inspect.getsource(toss_api)
    tree = ast.parse(src)

    bad = []
    for node in ast.walk(tree):
        if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
                and node.func.id == "_request"):
            continue
        method = node.args[0].value if (node.args and isinstance(node.args[0], ast.Constant)) else None
        if method is None or str(method).upper() == "GET":
            continue
        kw = {k.arg: k.value for k in node.keywords}
        flag = kw.get("idempotent")
        ok = isinstance(flag, ast.Constant) and flag.value is False
        if not ok:
            bad.append(f"{method} 요청(줄 {node.lineno})이 idempotent=False 를 넘기지 않는다")
    assert not bad, "\n  " + "\n  ".join(bad)


def test_토스_주문_경로를_실제로_찾는다():
    """0건을 훑고 초록인 상태를 막는다 — 주문 함수가 사라지면 위 검사는 늘 통과한다."""
    src = inspect.getsource(toss_api)
    tree = ast.parse(src)
    posts = [n for n in ast.walk(tree)
             if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)
             and n.func.id == "_request" and n.args
             and isinstance(n.args[0], ast.Constant)
             and str(n.args[0].value).upper() != "GET"]
    assert len(posts) >= 3, f"토스 주문/정정/취소 경로를 {len(posts)}개밖에 못 찾았다"


def test_검사기가_빠진_주문_URL을_실제로_잡는다():
    """탐지기 자체 시험 — 가드가 고장 나면 늘 초록이다."""
    fake = {"OVERSEAS": {"TRADING": {"ORDER_NEW": "/uapi/overseas-stock/v2/trading/order-new"}}}
    urls = [u for u in _all_urls(fake) if "/trading/order" in u]
    assert urls == ["/uapi/overseas-stock/v2/trading/order-new"]
    assert any(h in urls[0] for h in http._STATE_CHANGING_URL_HINTS), \
        "이 합성 URL 은 'trading/order' 힌트에 걸려야 한다"

    unmatched = "/uapi/overseas-stock/v2/exec/submit"
    assert not any(h in unmatched for h in http._STATE_CHANGING_URL_HINTS), \
        "힌트 목록이 아무 URL 이나 잡는다 — 검사가 무의미해진다"
