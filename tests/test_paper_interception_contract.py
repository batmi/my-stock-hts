"""관찰(가상투자) 모드 가로채기 커버리지 계약.

[왜 필요한가] 가상투자는 api 층에서 계좌·주문 호출을 가로채 가상 포트폴리오로
대체한다. 그런데 가로채기가 **함수마다 따로** 붙어 있어, 계좌를 건드리는 함수가
새로 생기거나 기존 함수가 고쳐질 때 조용히 빠질 수 있다. 빠지면 CANO='PAPER'로
실계좌 API를 때려 INVALID_CHECK_ACNO(rt_cd=2)가 돌아오고, 호출부는 그것을
'0' 또는 '실패'로 읽는다.

실제로 두 건이 그렇게 새고 있었다(2026-08-05 라즈베리파이 로그로 발견):
  · fetch_sellable_quantity → 0 → 트레이더가 '팔 수 없는 상태'로 읽어 **매도 전면 중단**
    (손절·트레일링·점수매도가 전부 죽어 청산 검증 자체가 성립하지 않았다)
  · fetch_buyable_quantity  → 0 → 피라미딩은 폴백이 없어 **증액이 영구 보류**

한 번 훑고 끝내면 같은 실수가 반복되므로, '계좌 파라미터를 보내는 함수는 관찰 모드
가드를 갖는다'를 소스 수준 계약으로 고정한다.
"""
import ast
import os

import pytest

#  api 는 2026-08-23 부터 패키지다(구 api.py 분해). 계좌를 건드리는 함수가 어느 계층으로
#  옮겨가도 계약이 따라가도록 파일 하나가 아니라 패키지 전체를 훑는다.
API_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "api")


def _api_source_files():
    out = []
    for root, _dirs, files in os.walk(API_DIR):
        for f in files:
            if f.endswith(".py"):
                out.append(os.path.join(root, f))
    return sorted(out)

# 가드가 없어도 되는 함수. 이유를 반드시 함께 적는다.
EXEMPT = {
    # 계좌 파라미터를 '고르는' 헬퍼일 뿐 스스로 호출하지 않는다. 관찰 모드에서는
    # 이 함수가 고른 값이 실제 요청으로 나가기 전에 각 함수의 가드가 먼저 막는다.
    "_prepare_account_params",
    #  [2026-09-05] place_order 의 실제 브로커 경로. 공개 함수 place_order 가 **가장 바깥**
    #  에서 _paper_active 가드를 통과시킨 뒤에만 불린다(private, 호출부는 그 한 곳뿐).
    #  가드를 여기에도 복사하면 사본이 둘이 되어 한쪽만 고쳐지는 길이 열린다 —
    #  이 저장소가 반복해서 밟은 형태다. 대신 아래 test_place_order_guard_is_outermost 가
    #  '공개 함수 최상단'이라는 위치 자체를 못박는다.
    "_place_order_impl",
}


def _account_touching_functions():
    """CANO 파라미터를 실제로 만들어 보내는 최상위 함수 목록."""
    out = []
    for path in _api_source_files():
        tree = ast.parse(open(path, encoding="utf-8").read())
        for node in tree.body:
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            src = ast.dump(node)
            touches = ("'CANO'" in src or '"CANO"' in src
                       or "_prepare_account_params" in src)
            if touches:
                out.append(node)
    return out


def test_account_calls_are_intercepted_in_paper_mode():
    """계좌 파라미터를 보내는 api 함수는 모두 관찰 모드 가드를 갖는다."""
    missing = []
    for node in _account_touching_functions():
        if node.name in EXEMPT:
            continue
        if "_paper_active" not in ast.dump(node):
            missing.append(node.name)

    assert not missing, (
        "관찰 모드 가드(_paper_active)가 없는 계좌 호출 함수:\n  "
        + "\n  ".join(missing)
        + "\n\n가상투자에서 이 함수들은 CANO='PAPER'로 실계좌를 조회해 실패한다."
        " paper_broker의 가상 상태로 답하도록 최상단에 가드를 넣거나,"
        " 호출될 수 없는 경로임을 확인했다면 사유와 함께 EXEMPT에 등록할 것."
    )


def test_exempt_list_stays_small():
    """면제 목록이 조용히 불어나지 않게 한다(가드를 넣는 대신 면제로 도망가는 것 방지)."""
    assert len(EXEMPT) <= 2, "면제 함수가 늘었다 — 정말 가드가 불가능한지 다시 볼 것"


@pytest.mark.parametrize("fname", [
    "fetch_buyable_quantity", "fetch_sellable_quantity",
    "fetch_overseas_buyable_quantity", "fetch_overseas_sellable_quantity",
])
def test_guard_is_at_function_top(fname):
    """가드는 함수 **최상단**에 있어야 의미가 있다.

    아래 어떤 분기(토스·계좌 선택·TR 분기)보다 먼저 걸리지 않으면, 그 분기가
    실계좌 파라미터를 만들어 요청을 보낸 뒤에야 가드를 만나게 된다.
    """
    node = next(n for n in _account_touching_functions() if n.name == fname)
    body = [s for s in node.body if not (isinstance(s, ast.Expr)
                                         and isinstance(s.value, ast.Constant))]
    assert body and "_paper_active" in ast.dump(body[0]), \
        f"{fname}: 관찰 모드 가드가 첫 문장이 아니다"


def test_place_order_guard_is_outermost():
    """관찰 모드 가드는 place_order 의 **첫 분기**여야 한다.

    [왜 위치까지 보나 · 2026-09-05] 주문 응답의 불변식 검사(_require_odno)를 붙이면서
     place_order 를 얇은 래퍼로 바꿨는데, 그때 가드가 내부 구현으로 내려갔다. 그러면
     가상투자에서도 결과 불명 대사 경로(_reconcile_unknown_order)가 열리는데 그쪽은
     **실계좌 당일 주문내역을 조회한다.** 가드는 반드시 바깥이어야 한다.
    """
    import inspect

    import api

    fn = ast.parse(inspect.getsource(api.place_order)).body[0]
    stmts = [n for n in fn.body
             if not (isinstance(n, ast.Expr) and isinstance(n.value, ast.Constant))]
    assert stmts, "place_order 본문이 비었다"
    first = stmts[0]
    assert isinstance(first, ast.If) and "_paper_active" in ast.dump(first.test), (
        "관찰 모드 가드가 place_order 의 첫 분기가 아니다 — 그 앞에 실계좌 경로가 열린다:\n"
        + ast.unparse(first)[:200])


def test_place_order_never_reports_success_without_an_order_number():
    """rt_cd='0' 인데 ODNO 가 비면 '성공'으로 내보내지 않는다.

    추적 키가 '' 가 되면 체결 대사도 미체결 자동 취소도 그 주문을 못 찾고, 그 종목은
    is_pending 인 채로 매도 워커에서 빠져 손절·트레일링이 멈춘다.
    """
    from unittest.mock import patch

    import api
    import config

    with patch.object(api, "_paper_active", return_value=False), \
         patch.object(config.session, "is_toss", False), \
         patch.object(api, "_place_order_impl",
                      return_value={"rt_cd": "0", "msg1": "ok", "output": {"ODNO": ""}}), \
         patch.object(api, "_reconcile_unknown_order",
                      return_value={"rt_cd": "1", "msg_cd": "ORDER_UNKNOWN",
                                    "msg1": "대사함", "output": {}}) as rec:
        res = api.place_order("domestic", "buy", "005930", 1, 70000, "00")

    assert rec.called, "주문번호 없는 성공이 그대로 통과했다"
    assert res["msg_cd"] == "ORDER_UNKNOWN"
