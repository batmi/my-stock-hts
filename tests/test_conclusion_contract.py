"""체결 감시가 반환 규약을 지키는가.

[사고 2026-08-05] 관찰 모드에서 체결 대사를 건너뛰도록 조기 반환을 넣었는데 맨 return
(=None)이었다. 호출부는 `is_rate_limited, has_error = self._check_conclusions(...)`로
언패킹하므로 매 주기마다 다음이 찍혔다:

    [ERROR] 체결 감시 중 오류 발생: cannot unpack non-iterable NoneType object

게다가 이 예외는 consecutive_errors를 올려 킬 스위치까지 건드린다 — 체결 감시가 죽은
채로 에러만 쌓인다. 함수가 길고(500줄+) 반환 지점이 흩어져 있어 눈으로는 놓치기 쉽다.

여기서는 '어떻게 동작하는가'가 아니라 **반환 규약 자체**를 못박는다.
"""
import ast
import inspect

import pytest
from unittest.mock import patch

import config
from modules.auto_trade import conclusion


def _direct_returns(fn_node):
    """중첩 함수(_check_and_remove_restriction 등)의 return은 제외하고 자신의 것만 모은다.

    ast.walk는 중첩 함수까지 내려가므로 그대로 쓰면 남의 return을 이 함수 것으로 오인한다.
    """
    out = []

    def visit(node):
        for child in ast.iter_child_nodes(node):
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)):
                continue                       # 중첩 함수는 자기 규약을 따른다
            if isinstance(child, ast.Return):
                out.append(child)
            visit(child)

    visit(fn_node)
    return out


def _find_func(name):
    tree = ast.parse(inspect.getsource(conclusion))
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == name:
            return node
    pytest.fail(f"{name}을 찾지 못했다")


def test_every_return_yields_a_pair():
    """[핵심] 모든 반환 지점이 (rate_limited, has_error) 2-튜플이어야 한다."""
    rets = _direct_returns(_find_func("_check_conclusions"))
    assert rets, "반환문을 하나도 찾지 못했다 — 검사가 헐거워졌다"
    bad = [r.lineno for r in rets
           if not (isinstance(r.value, ast.Tuple) and len(r.value.elts) == 2)]
    assert not bad, (
        f"{bad}번 줄의 반환이 2-튜플이 아니다 — 호출부 언패킹이 깨진다 "
        f"(cannot unpack non-iterable NoneType object)")


def test_paper_mode_returns_a_pair_not_none():
    """[재발 방지] 관찰 모드 조기 반환이 실제로 언패킹 가능한지 실행으로 확인한다.

    정적 검사만 두면 조기 반환을 다른 형태로 다시 넣었을 때 놓칠 수 있다.
    """
    monitor = conclusion.ConclusionMonitor()
    with patch.object(config.session, 'is_paper', True):
        result = monitor._check_conclusions(initial=False)

    assert result is not None, "관찰 모드에서 None을 반환한다"
    rate_limited, has_error = result           # 호출부와 같은 방식으로 언패킹
    assert rate_limited is False and has_error is False, (
        f"건너뛴 것을 에러로 보고하면 킬 스위치가 오작동한다: {result}")


def test_paper_mode_does_not_query_the_broker():
    """관찰 모드에서는 증권사 체결 조회 자체가 나가지 않아야 한다.

    가상 주문은 즉시 전량 체결이라 대사할 대상이 없고, 계좌번호도 'PAPER'다.
    """
    monitor = conclusion.ConclusionMonitor()
    with patch.object(config.session, 'is_paper', True), \
         patch('modules.auto_trade.conclusion.api.get_today_history') as hist:
        monitor._check_conclusions(initial=False)
    assert not hist.called, "관찰 모드인데 증권사 체결 내역을 조회했다"


def test_real_mode_still_runs():
    """대조군 — 실계좌에서는 그대로 돌아야 한다(가드가 상시면 체결 감시가 죽는다)."""
    monitor = conclusion.ConclusionMonitor()
    with patch.object(config.session, 'is_paper', False), \
         patch.object(config.session, 'cano', '12345678'), \
         patch.object(config.session, 'acnt_prdt_cd', '01'), \
         patch('modules.auto_trade.conclusion.api.get_today_history',
               return_value={'rt_cd': '0', 'output1': [], 'output2': {}}) as hist, \
         patch('modules.auto_trade.conclusion.db_manager.db.get_all_stock_strategies',
               return_value=[]):
        result = monitor._check_conclusions(initial=True)

    assert hist.called, "실계좌인데 체결 조회를 건너뛰었다"
    assert result is not None and len(result) == 2
