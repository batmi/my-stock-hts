"""표본 씨드가 규약(3개)에 못 미치면 감사 도구가 **말을 하는가**.

[왜] audit-seed-robustness 규약은 '경계선 결과는 씨드 3개, 채택 직전이면 5개로 재확인'
 이다. 그런데 2026-08-23 시점에 감사 도구 28개의 기본 씨드가 **1개**였고, 그중 6개는
 씨드를 바꿀 손잡이조차 없었다(`random.Random(20260804)` 이 소스에 박혀 있었다).
 규약은 문서에만 있고 도구는 아무 말도 하지 않으니, 한 번 돌린 표를 그대로 결론으로
 적기 쉬운 구조였다. 실제로 2026-08-17 슬롯 축에서 같은 설정이 씨드만 바꾸자
 58% → 43% 로 뒤집힌 적이 있다.

[고른 해법] 기본 씨드 수를 3으로 올리면 모든 감사의 실행 시간이 그대로 3배가 된다.
 올리는 대신 **잊지 않게** 한다 — 3개 미만으로 돌면 경고 한 줄을 찍는다. 판정을 막지는
 않는다(탐색 단계의 1씨드 실행은 정당하다).

[이 테스트가 지키는 것]
 ① seed_notice 자체의 경계(3 미만만 경고).
 ② 종목 표본을 무작위로 뽑는 감사 도구는 전부 seed_notice 를 부른다 — 새 도구가
    조용히 빠지면 잡는다.
 ③ 씨드 손잡이가 없는 도구가 다시 생기지 않는다(random.Random 에 리터럴 금지).
"""
import ast
import glob
import os

import pytest

from tools.audit_common import seed_notice

TOOLS = sorted(glob.glob(os.path.join(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__))), "tools", "audit_*.py")))


def _tree(path):
    with open(path, encoding="utf-8") as fp:
        return ast.parse(fp.read(), path)


def _samples_randomly(tree):
    """종목 표본을 난수로 뽑는 도구인가 — random.Random(...) 을 만드는지로 본다."""
    for node in ast.walk(tree):
        if (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                and node.func.attr == "Random"):
            return True
    return False


def _calls_seed_notice(tree):
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and getattr(node.func, "id", None) == "seed_notice":
            return True
    return False


@pytest.mark.parametrize("n,expected", [(0, True), (1, True), (2, True),
                                        (3, False), (5, False), (10, False)])
def test_seed_notice_boundary(n, expected):
    """3개가 경계다. 3개 이상이면 아무 말도 하지 않는다."""
    said = []
    assert seed_notice(n, emit=said.append) is expected
    assert bool(said) is expected


def test_seed_notice_message_names_the_flag_and_an_example():
    """경고는 '어떻게 다시 돌리는가'까지 말해야 쓸모가 있다."""
    said = []
    seed_notice(1, example="--seeds 20260816,7,101", emit=said.append)
    assert len(said) == 1
    msg = said[0]
    assert "--seeds 20260816,7,101" in msg
    assert "audit-seed-robustness" in msg


def test_every_sampling_tool_warns_on_thin_seeds():
    """무작위 표본을 쓰는 감사 도구는 전부 seed_notice 를 부른다."""
    missing = []
    for path in TOOLS:
        tree = _tree(path)
        if _samples_randomly(tree) and not _calls_seed_notice(tree):
            missing.append(os.path.basename(path))
    assert not missing, (
        "무작위 표본을 쓰면서 씨드 경고가 없는 도구:\n  " + "\n  ".join(missing)
        + "\n→ args = ap.parse_args() 바로 뒤에 audit_common.seed_notice(...) 를 부를 것"
    )


def test_no_hardcoded_sampling_seed():
    """씨드는 CLI 인자로 바꿀 수 있어야 한다 — 소스에 박으면 재확인 자체가 불가능하다."""
    hardcoded = []
    for path in TOOLS:
        for node in ast.walk(_tree(path)):
            if (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                    and node.func.attr == "Random" and node.args
                    and isinstance(node.args[0], ast.Constant)
                    and isinstance(node.args[0].value, int)):
                hardcoded.append(f"{os.path.basename(path)}:{node.lineno}"
                                 f" random.Random({node.args[0].value})")
    assert not hardcoded, (
        "표본 씨드가 소스에 박혀 있다(바꿔 돌릴 수 없다):\n  " + "\n  ".join(hardcoded)
        + '\n→ ap.add_argument("--seed", type=int, default=<그 값>) 을 두고 args.seed 를 쓸 것'
    )


def test_seed_notice_is_placed_right_after_parse_args():
    """경고는 데이터 준비(수 분) **전에** 나와야 멈춰 세울 수 있다."""
    late = []
    for path in TOOLS:
        tree = _tree(path)
        if not _calls_seed_notice(tree):
            continue
        for fn in ast.walk(tree):
            if not isinstance(fn, ast.FunctionDef):
                continue
            body = fn.body
            for i, stmt in enumerate(body):
                if (isinstance(stmt, ast.Assign) and "parse_args" in ast.dump(stmt)):
                    nxt = body[i + 1] if i + 1 < len(body) else None
                    if not (isinstance(nxt, ast.Expr)
                            and getattr(getattr(nxt.value, "func", None), "id", None)
                            == "seed_notice"):
                        late.append(f"{os.path.basename(path)}:{stmt.lineno}")
    assert not late, ("parse_args 바로 다음 줄이 seed_notice 가 아니다:\n  "
                      + "\n  ".join(late))
