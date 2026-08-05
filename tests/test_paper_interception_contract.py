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

API_PATH = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "api.py")

# 가드가 없어도 되는 함수. 이유를 반드시 함께 적는다.
EXEMPT = {
    # 계좌 파라미터를 '고르는' 헬퍼일 뿐 스스로 호출하지 않는다. 관찰 모드에서는
    # 이 함수가 고른 값이 실제 요청으로 나가기 전에 각 함수의 가드가 먼저 막는다.
    "_prepare_account_params",
}


def _account_touching_functions():
    """CANO 파라미터를 실제로 만들어 보내는 최상위 함수 목록."""
    tree = ast.parse(open(API_PATH, encoding="utf-8").read())
    out = []
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
