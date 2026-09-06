"""'실패=None' 계약을 `or []` 한 조각으로 무력화하지 못하게 한다 (AST 가드).

[이 가드가 잡는 것] api 계층에는 실패를 None 으로, '없음'을 빈 값으로 돌려주는 함수가
여럿 있다 — 그 구분이 안전 게이트의 근거다([[unknown-vs-empty]]). 그런데 호출부에서

    rows = api.get_domestic_open_orders(cano, acnt) or []

한 줄이면 그 구분이 조용히 사라진다. 실패가 '없음'이 되고, 그 위에 선 판정은
아무 흔적 없이 뒤집힌다. 실제로 이 관용구 때문에 세 곳이 무력화돼 있었다:
  · 토스 자산 보정(미체결) → 가짜 입출금 감지 (2026-09-06)
  · 주문 대사(당일 주문내역·토스 미체결) → '미접수' 단정 = 재전송 금지 규칙 붕괴
  · 매매일지 기초잔고(해외) → 해외 보유가 한 번도 실리지 않음

[왜 목록을 손으로 관리하지 않는가] 함수가 늘 때마다 사람이 목록을 갱신해야 하면
반드시 빠진다. 여기서는 api/ 를 AST 로 훑어 '실패=None' 함수를 **스스로 찾아낸다**.

[예외를 두는 법] 정말로 폴백이 옳은 자리라면 아래 _ALLOWED 에 사유와 함께 적는다.
비워 두는 것이 기본이다 — 예외가 늘면 이 가드는 의미를 잃는다.
"""
import ast
import os

import pytest


# (파일, 함수명) — 폴백이 의도적으로 옳은 자리. 사유를 반드시 함께 적는다.
_ALLOWED: dict[tuple[str, str], str] = {}


def _none_returning_api_functions():
    """api/ 에서 '실패 → None, 성공 → 값' 인 공개 함수 이름을 모은다."""
    names = set()
    for root, _, files in os.walk('api'):
        for f in sorted(files):
            if not f.endswith('.py'):
                continue
            path = os.path.join(root, f)
            with open(path, encoding='utf-8') as fh:
                tree = ast.parse(fh.read(), path)
            for node in ast.walk(tree):
                if not isinstance(node, ast.FunctionDef) or node.name.startswith('_'):
                    continue
                rets = [n for n in ast.walk(node) if isinstance(n, ast.Return)]
                bare_none = any(isinstance(r.value, ast.Constant) and r.value.value is None
                                for r in rets)
                other = any(r.value is not None
                            and not (isinstance(r.value, ast.Constant) and r.value.value is None)
                            for r in rets)
                if bare_none and other:
                    names.add(node.name)
    return names


def _production_files():
    for root, dirs, files in os.walk('.'):
        dirs[:] = [d for d in dirs
                   if d not in ('.venv', '.git', 'tests', 'tools', '__pycache__', 'docs')]
        for f in sorted(files):
            if f.endswith('.py'):
                yield os.path.join(root, f)


class _Collapses(ast.NodeVisitor):
    """`<api호출>(...) or <상수/빈컨테이너>` 를 찾는다 — 실패를 '없음'으로 접는 관용구."""

    _EMPTYISH = (ast.List, ast.Dict, ast.Tuple, ast.Set)

    def __init__(self, targets):
        self.targets = targets
        self.hits = []

    def visit_BoolOp(self, node):
        if isinstance(node.op, ast.Or) and len(node.values) >= 2:
            left = node.values[0]
            name = None
            if isinstance(left, ast.Call):
                fn = left.func
                name = fn.attr if isinstance(fn, ast.Attribute) else (
                    fn.id if isinstance(fn, ast.Name) else None)
            if name in self.targets:
                rest = node.values[1]
                is_emptyish = (isinstance(rest, self._EMPTYISH)
                               or (isinstance(rest, ast.Constant)
                                   and rest.value in (None, 0, '', False)))
                if is_emptyish:
                    self.hits.append((name, node.lineno))
        self.generic_visit(node)


def test_실패가_None인_api호출을_or_빈값으로_접지_않는다():
    targets = _none_returning_api_functions()
    assert len(targets) >= 15, (
        f"'실패=None' 함수를 {len(targets)}개밖에 못 찾았다 — 탐지기가 고장 났다")

    offenders = []
    for path in _production_files():
        if path.startswith('./api/'):
            continue                      # api 내부의 자기 호출은 계약 정의 쪽이다
        with open(path, encoding='utf-8') as fh:
            src = fh.read()
        try:
            tree = ast.parse(src, path)
        except SyntaxError:
            continue
        v = _Collapses(targets)
        v.visit(tree)
        lines = src.splitlines()
        for name, lineno in v.hits:
            if (path, name) in _ALLOWED:
                continue
            offenders.append(f"{path}:{lineno}  {name}(...) or ...\n"
                             f"      {lines[lineno - 1].strip()[:110]}")

    assert not offenders, (
        "조회 실패(None)를 빈 값으로 접는 자리가 있다 — 그 위의 '없음' 판정이 조용히 뒤집힌다.\n"
        "실패를 실패로 다루거나, 정말 폴백이 옳다면 _ALLOWED 에 사유와 함께 적어라.\n"
        + "\n".join(offenders))
