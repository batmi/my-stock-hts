"""함수 안에서 **어디에도 묶이지 않은 이름**을 읽는 자리를 막는다.

[왜 이 파일이 있나 · 2026-09-07]
탐색 메뉴(7-5)에서 실제로 이렇게 죽고 있었다. 2026-09-06 에 저장 실패 처리를
_commit_additions 로 떼어내면서, 성공 문구를 찍는 두 줄이 원래 자리에 남았다.
그 줄이 쓰는 `added` 는 떼어낸 함수의 지역 변수다 — 그때부터 성공 경로는
NameError 로 죽었다(실측: name 'added' is not defined).

이 결함의 성질이 나쁘다:
 · **성공 경로에만** 있다. 실패·취소 경로는 멀쩡하니 오류 처리 테스트를 아무리 써도
   지나가지 않는다.
 · 단위 테스트는 떼어낸 함수를 직접 부르므로 그 이음매를 건너뛴다. 실제로 기존
   테스트 두 개가 _commit_additions 를 직접 부르며 통과하고 있었다.
 · 저장이 끝난 **뒤에** 죽는다. 종목은 이미 들어갔는데 사람은 실패로 보고 다시 돌린다.
 · 파이썬은 실행하기 전까지 아무 말도 하지 않는다. 자주 안 도는 메뉴라면 몇 달을 산다.

리팩터가 흔한 코드베이스에서 사람이 매번 눈으로 찾을 수 있는 종류가 아니다. 세어서 막는다.
가드 자신이 조용해지지 않게 자기 검사(아래 test_가드가_실제로_잡는가)를 함께 둔다.

[검사 범위] 저장소의 모든 파이썬 파일. 함수 안에서 읽는 이름 중, 그 함수·감싼 함수
어디에도 대입/인자/import/전역/내장이 없는 것만 본다 — 지역인지 전역인지 헷갈릴 여지가
없는 것만 잡으므로 오탐이 없다.

tools/ 도 뺄 수 없다: 같은 결함이 audit_tq_band_structure.py 절 [3] 에도 있었고
(`range(k)` 인데 그 함수에 k 가 없다), 하필 **긴 백테스트가 다 끝난 뒤에** 죽는다.
앞 절들은 이미 찍힌 뒤라 출력만 보면 정상 종료처럼 보인다.
tests/ 도 넣는다 — 테스트 안의 NameError 는 '검사하지 않는 검사'가 된다.

데코레이터·인자 기본값·타입 주석은 함수가 아니라 감싼 스코프에서 평가되므로 검사에서
뺀다(그러지 않으면 @property 짝인 @x.setter 가 전부 걸린다 — 실측 5건).
"""
import ast
import builtins
import os

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SKIP_DIRS = {"__pycache__", ".git", ".venv", "backup", "json", "data"}
BUILTINS = set(dir(builtins)) | {"__file__", "__name__", "__doc__", "__package__", "__spec__"}


def _production_files():
    for d, dirnames, filenames in os.walk(ROOT):
        dirnames[:] = [x for x in dirnames if x not in SKIP_DIRS and not x.startswith(".")]
        for f in sorted(filenames):
            if f.endswith(".py"):
                yield os.path.join(d, f)


def _bound_names(node):
    """이 스코프가 직접 묶는 이름(중첩 함수·클래스 안쪽은 세지 않는다)."""
    names = set()
    if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)):
        a = node.args
        for arg in (*a.posonlyargs, *a.args, *a.kwonlyargs):
            names.add(arg.arg)
        if a.vararg:
            names.add(a.vararg.arg)
        if a.kwarg:
            names.add(a.kwarg.arg)

    def walk(n, top=False):
        for child in ast.iter_child_nodes(n):
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                names.add(child.name)          # 이름만 묶고 본문은 자기 스코프다
                continue
            if isinstance(child, ast.Lambda):
                continue
            if isinstance(child, ast.Name) and isinstance(child.ctx, (ast.Store, ast.Del)):
                names.add(child.id)
            elif isinstance(child, (ast.Import, ast.ImportFrom)):
                for al in child.names:
                    names.add((al.asname or al.name).split(".")[0])
            elif isinstance(child, (ast.Global, ast.Nonlocal)):
                names.update(child.names)
            elif isinstance(child, ast.ExceptHandler) and child.name:
                names.add(child.name)
            elif isinstance(child, (ast.comprehension,)):
                for t in ast.walk(child.target):
                    if isinstance(t, ast.Name):
                        names.add(t.id)
            elif isinstance(child, ast.MatchAs) and child.name:
                names.add(child.name)
            elif isinstance(child, ast.MatchStar) and child.name:
                names.add(child.name)
            elif isinstance(child, ast.MatchMapping) and child.rest:
                names.add(child.rest)
            walk(child)

    walk(node, top=True)
    return names


def _loaded_names(nodes):
    """이 스코프에서 직접 읽는 (이름, 줄번호) — 중첩 스코프 안쪽은 뺀다.

    **본문 문장만** 받는다. 데코레이터·인자 기본값·타입 주석은 함수 자신이 아니라
    감싼 스코프에서 평가되므로(메서드라면 클래스 본문) 여기서 세면 오탐이 난다 —
    실제로 `@x.setter` 다섯 개가 그렇게 잡혔다.
    """
    out = []

    def walk(n):
        for child in ast.iter_child_nodes(n):
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef,
                                  ast.ClassDef, ast.Lambda)):
                continue
            if isinstance(child, ast.Name) and isinstance(child.ctx, ast.Load):
                out.append((child.id, child.lineno))
            walk(child)

    for n in nodes:
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef, ast.Lambda)):
            continue
        if isinstance(n, ast.Name) and isinstance(n.ctx, ast.Load):
            out.append((n.id, n.lineno))
        walk(n)
    return out


def _offenders(source, path):
    tree = ast.parse(source)
    module_names = _bound_names(tree)
    hits = []

    def visit(node, enclosing):
        scope = _bound_names(node)
        visible = enclosing | scope
        for name, lineno in _loaded_names(node.body):
            if name not in visible and name not in module_names and name not in BUILTINS:
                hits.append((path, lineno, name))
        for child in node.body:
            _descend(child, visible)

    def _descend(node, visible):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            visit(node, visible)
            return
        if isinstance(node, ast.ClassDef):
            # 클래스 본문은 메서드에서 보이지 않는다 — 감싼 스코프만 물려준다.
            for child in ast.iter_child_nodes(node):
                _descend(child, visible)
            return
        for child in ast.iter_child_nodes(node):
            _descend(child, visible)

    for child in ast.iter_child_nodes(tree):
        _descend(child, module_names)
    return hits


def test_묶이지_않은_이름을_읽는_자리가_없다():
    found = []
    for path in _production_files():
        with open(path, encoding="utf-8") as fh:
            source = fh.read()
        try:
            found += _offenders(source, os.path.relpath(path, ROOT))
        except SyntaxError:
            continue

    assert not found, "함수 안에서 어디에도 묶이지 않은 이름을 읽는다 " \
                      "(실행되면 NameError):\n" + "\n".join(
                          f"  {p}:{ln}  {n}" for p, ln, n in found)


# --------------------------------------------------------------------------
# 가드 자기 검사 — 세는 장치가 조용해지면 세지 않는 것과 같다.
# --------------------------------------------------------------------------
REAL_BUG = '''
def _commit(chosen):
    added = 0
    for c in chosen:
        added += 1
    return True

def discover():
    if not _commit([]):
        return False
    print(f"{added}종목을 추가했습니다")
'''

CLEAN = '''
import os
COUNT = 3

def outer(a, *args, **kw):
    b = a + COUNT
    def inner():
        return b + a
    try:
        [x for x in range(b)]
    except ValueError as e:
        return str(e)
    with open(os.devnull) as fh:
        return fh, inner, args, kw

class C:
    attr = 1
    def m(self, v=None):
        return self.attr + (v or COUNT)
'''


def test_가드가_실제로_잡는가():
    """실제로 있었던 결함 그대로."""
    hits = _offenders(REAL_BUG, "x.py")
    assert [n for _, _, n in hits] == ["added"]


@pytest.mark.parametrize("src", [CLEAN])
def test_정상_코드를_잡지_않는다(src):
    """인자·중첩함수·컴프리헨션·except as·with as·클래스 속성에 오탐이 없어야 한다."""
    assert _offenders(src, "x.py") == []
