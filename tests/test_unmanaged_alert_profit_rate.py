"""미관리 포지션 손절 경보가 '평가손익률 없음'에 조용해지지 않는가.

[왜 이 테스트인가] 이 경보는 시스템이 손절해 주지 않는 포지션(트레이딩 제한·ETF·미체결
주문 존재 등)의 **마지막 안전망**이다. 그런데 판정이 `float(item.get('evlu_pfls_rt') or 0.0)`
이었다. 필드가 없거나 빈 값이면 0%가 되고, 0%는 손절선(음수)보다 위라 '회복했다'로 읽혀
경보를 건너뛰는 것은 물론 24시간 스로틀까지 풀어 버린다.

증권사 어댑터는 일부 필드를 0/누락으로 준다 — 같은 이유로 pchs_amt 는 이미 수량×평단으로
복원하고 있다. 즉 잔고 데이터가 부실할수록 안전망이 조용해지는 구조였다.
"""
import pytest

from modules.auto_trade import engine as E


def test_uses_reported_rate_when_present():
    assert E.holding_profit_rate({"evlu_pfls_rt": "-8.3"}) == pytest.approx(-8.3)


def test_zero_is_a_real_value_not_missing():
    """실제로 본전인 포지션의 0%까지 '없음'으로 뭉개면 안 된다."""
    assert E.holding_profit_rate({"evlu_pfls_rt": "0"}) == 0.0


@pytest.mark.parametrize("raw", [None, "", "-"])
def test_missing_rate_is_derived_from_average_and_price(raw):
    """필드가 비어도 평단과 현재가가 있으면 직접 구한다 — 0%로 두면 손절 판정이 뒤집힌다."""
    item = {"evlu_pfls_rt": raw, "pchs_avg_pric": "100000", "prpr": "92000"}
    assert E.holding_profit_rate(item) == pytest.approx(-8.0)


@pytest.mark.parametrize("item", [
    {},
    {"pchs_avg_pric": "0", "prpr": "92000"},
    {"pchs_avg_pric": "100000", "prpr": "0"},
    {"pchs_avg_pric": "abc", "prpr": "92000"},
])
def test_unknown_returns_none_not_zero(item):
    """구할 수 없으면 모른다고 답한다 — 0을 돌려주면 손실 중인 포지션이 안전해 보인다."""
    assert E.holding_profit_rate(item) is None


def test_alert_holds_throttle_when_rate_is_unknown():
    """판정 불가일 때 스로틀을 풀면, 다음에 값이 잡혀도 이미 알린 것으로 오해될 수 있다."""
    import inspect
    from modules.auto_trade import AutoTrader
    src = inspect.getsource(AutoTrader._alert_unmanaged_stop)
    assert "holding_profit_rate" in src, "판정이 여전히 or 0.0 폴백을 쓴다"
    head = src.split("holding_profit_rate")[1][:400]
    assert "return" in head and "unmanaged_stop_notified.pop" not in head, \
        "판정 불가 경로에서 스로틀을 건드린다"


# ==========================================================
# [같은 규칙이 매도 경로 전체에 걸려 있는가] (2026-09-06)
# ==========================================================
#  holding_profit_rate 는 '미관리 경보' 하나를 위해 만들어졌는데, 같은 함정
#  (`float(item.get('evlu_pfls_rt') or 0.0)`)이 매도 경로에 세 곳 더 남아 있었다.
#   · 손절 보호(_cancel_pending_buy_on_stop_loss) — 0% > 손절선 → 보호가 통째로 건너뜀
#   · 마감 후 매도 신호 알림 — 알림 본문에 0.0% 를 적어 갭 전 판단을 그르침
#   · 매도 판정 본체 — `float(item['evlu_pfls_rt'])` 라 빈 값이면 ValueError 로
#     **그 종목의 손절·트레일링이 그 주기에 아예 돌지 않았다**

DECISION_FNS = (
    "_alert_unmanaged_stop",              # 시스템이 안 파는 포지션의 마지막 안전망
    "_cancel_pending_buy_on_stop_loss",   # 손절 중인 종목의 미체결 매수 취소
    "_alert_after_hours_sell",            # 마감 후 매도 신호 알림(갭 전 판단 근거)
    "_check_sell_conditions",             # 매도 판정 본체
)


def _fn_source(name):
    import ast
    import inspect

    from modules.auto_trade import trader as _trader

    tree = ast.parse(inspect.getsource(_trader))
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == name:
            return node
    raise AssertionError(f"{name} 을 찾지 못했다 — 검사기가 낡았다")


def test_판정_경로가_수익률을_직접_읽지_않는다():
    """한 곳만 되돌아가도 그 종목의 청산이 조용히 멈춘다.

    (표를 그리는 자리들은 대상이 아니다 — 그쪽은 값이 없으면 그 줄이 깨질 뿐
     판정을 바꾸지 않는다.)
    """
    import ast

    bad = []
    for name in DECISION_FNS:
        for node in ast.walk(_fn_source(name)):
            if isinstance(node, ast.Call) and ast.unparse(node).startswith("float("):
                if "evlu_pfls_rt" in ast.unparse(node):
                    bad.append((name, node.lineno, ast.unparse(node)))

    assert not bad, (
        "판정 경로가 수익률을 float() 로 직접 읽는다 — 빈 값이면 0% 로 둔갑하거나"
        f" 예외로 판정이 통째로 건너뛰어진다: {bad}")


def test_판정_함수가_실제로_배선돼_있다():
    """반대 방향 — 위 검사가 '아무도 수익률을 안 읽는다'로도 통과하면 안 된다."""
    import ast

    for name in DECISION_FNS:
        src = ast.unparse(_fn_source(name))
        assert "holding_profit_rate(item)" in src, (
            f"{name} 이 holding_profit_rate 를 쓰지 않는다")
