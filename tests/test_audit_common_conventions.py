"""감사 도구가 공통 규약을 각자 다시 쓰고 있지 않은가.

[왜 검사가 필요한가] `tools/audit_common.py` 는 '무엇이 청산인가'와 '기간을 어떻게
나누는가'를 한 곳에서 정하려고 만들어졌고, 그 독스트링은 사본을 모았다고 **선언**한다.
그런데 선언은 시간이 지나면 거짓이 된다 — 2026-09-04 실측으로 청산 판정 사본 2벌,
구간 분할 사본 3벌이 남아 있었다. 규칙이 갈리면 도구 간 비교가 조용히 성립하지 않고,
그 위에서 채택·기각 결정이 내려진다.

사본은 지금 값이 같더라도 위험하다. 시뮬레이터에 청산 사유가 하나 늘거나 경계 규칙이
바뀔 때 **따라오지 않는 도구만 남는다**.
"""
import os
import re

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TOOLS = os.path.join(ROOT, "tools")


def _audit_sources():
    for fn in sorted(os.listdir(TOOLS)):
        if not (fn.startswith("audit_") and fn.endswith(".py")) or fn == "audit_common.py":
            continue
        path = os.path.join(TOOLS, fn)
        yield fn, open(path, encoding='utf-8').read()


def _code_lines(src):
    """주석·독스트링 줄을 뺀 실행 코드. 사본을 없앤 경위를 주석에 적을 수 있어야 한다."""
    import ast
    doc = set()
    try:
        tree = ast.parse(src)
    except SyntaxError:
        return []
    for node in ast.walk(tree):
        if not isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        body = getattr(node, "body", None)
        if body and isinstance(body[0], ast.Expr) and isinstance(body[0].value, ast.Constant) \
                and isinstance(body[0].value.value, str):
            doc.update(range(body[0].lineno, (body[0].end_lineno or body[0].lineno) + 1))
    return [(n, l) for n, l in enumerate(src.splitlines(), 1)
            if n not in doc and not l.strip().startswith("#")]


# 자기 손으로 청산 어휘를 판정하는 형태. audit_common.is_exit / exits 를 써야 한다.
_PRIVATE_EXIT = re.compile(r'reason\s*!=\s*["\']매수["\']')
# 자기 손으로 구간 경계를 만드는 형태. audit_common.windows 를 써야 한다.
_PRIVATE_WINDOW = re.compile(r'len\(dates\)\s*//\s*|dates\[\s*i\s*\*\s*(step|size)\s*:')


# 규칙이 **일부러** 다른 도구. 새 사본이 여기 슬쩍 들어오지 않도록 사유를 적어 둔다.
_WINDOW_EXEMPT = {
    # 3분할 고정 + '한 조각이 120거래일 미만이면 분할하지 않는다' 가드 + 날짜 라벨.
    # k 파라미터 분할과는 다른 규칙이라 공통 helper 로 표현되지 않는다.
    "audit_market_bear_release.py",
    # 분할 방식 자체가 이 도구의 **측정 대상**이다(A안 독립 실행 vs B안 연속 분할).
    # 경계를 인덱스 쌍으로 들고 두 방식을 같은 표본에 적용해야 한다.
    "audit_subwindow_method.py",
}


@pytest.mark.parametrize("pattern, helper, exempt", [
    (_PRIVATE_EXIT, "audit_common.is_exit / exits", frozenset()),
    (_PRIVATE_WINDOW, "audit_common.windows", frozenset(_WINDOW_EXEMPT)),
])
def test_no_tool_keeps_a_private_copy(pattern, helper, exempt):
    hits = [(fn, n, line.strip())
            for fn, src in _audit_sources() if fn not in exempt
            for n, line in _code_lines(src) if pattern.search(line)]
    assert not hits, f"{helper} 를 쓰지 않고 직접 판정한다: {hits}"


def test_the_exemption_list_does_not_outlive_its_files():
    """면제 목록이 사라진 파일을 가리키면, 다음 사람이 그 이름으로 새 사본을 만들 수 있다."""
    missing = [fn for fn in _WINDOW_EXEMPT
               if not os.path.exists(os.path.join(TOOLS, fn))]
    assert not missing, missing


def test_the_shared_window_split_matches_what_the_copies_did():
    """사본을 걷어내면서 경계가 바뀌면 과거 감사 수치와 비교할 수 없게 된다."""
    from tools.audit_common import windows

    def old(dates, k):
        k = max(1, k)
        step = max(1, len(dates) // k)
        return [list(dates[i * step:(i + 1) * step if i < k - 1 else len(dates)])
                for i in range(k)]

    for n in (50, 251, 500, 750, 1095):
        dates = list(range(n))
        for k in (1, 2, 3, 4, 5, 6, 8):
            got = [list(d) for _, d in windows(dates, k, whole=True)[1:]]
            assert got == old(dates, k), (n, k)


def test_a_split_finer_than_the_data_does_not_collapse_into_one_window():
    """가드가 없던 사본은 k > 거래일 수 이면 마지막 구간에 **전 기간**을 담았다 —
    '구간별로 쟀다'고 적힌 표가 실은 전체를 한 번 잰 것이 된다."""
    from tools.audit_common import windows

    got = [list(d) for _, d in windows(list(range(3)), 5)]
    assert not any(len(d) == 3 for d in got), got


def test_the_exit_vocabulary_comes_from_the_simulator():
    """감사 어휘가 시뮬레이터와 갈리면 승률·PF 분모가 서로 다른 집합이 된다."""
    from modules.portfolio_backtest import EXIT_REASONS
    from tools.audit_common import SELL_REASONS, is_exit

    assert SELL_REASONS is EXIT_REASONS
    assert all(is_exit(r) for r in EXIT_REASONS)
    assert not is_exit("매수") and not is_exit("피라미딩2차")
