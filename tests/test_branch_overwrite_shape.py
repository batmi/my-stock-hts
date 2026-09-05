"""분기의 마지막 줄이 **분기 밖**에 있어 결과를 통째로 덮어쓰는 모양을 막는다.

[왜 이 파일이 있나 · 2026-09-05]
같은 오타 하나가 이 저장소에서 **세 번** 나왔다. 들여쓰기 한 칸이 빠져 if/elif 가
정해 놓은 값을 바로 다음 줄이 무조건 덮어쓴다:

    if is_paper:   mode = "가상투자"
    elif is_toss:  mode = "토스 실전"
    mode = "KIS 실전"          # ← 분기 밖. 무엇으로 떴든 이 값이 된다.

 · scheduler._heartbeat_context — 사망 알림이 무엇이 죽었든 '실전'이라고 했다 (2026-09-04)
 · trader 관제 화면의 운용 모드 표시 — 같은 형태가 쌍둥이 자리에 남아 있었다 (2026-09-05)
 · api.auth.call_api 토큰 만료 재시도 — 자동 토큰을 갱신한 **뒤 수동 토큰까지 강제
   재발급**했다. KIS 는 앱키당 1분에 한 번만 발급하고 발급 시 이전 토큰을 무효화하므로,
   멀쩡한 수동 토큰을 버리고 그 1분 예산까지 태운다 (2026-09-05)

셋 다 조용하다 — 예외도 안 나고 테스트도 안 깨진다. 값이 그냥 늘 같을 뿐이다.
사람이 리뷰로 잡기 어려운 형태이므로 구문으로 잡는다.
"""
import ast
import os

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

#  일부러 이렇게 쓴 자리. 사유 없이 넣지 말 것.
_ALLOWED = set()   # (파일, 대입 줄번호)


def _branch_assign_targets(node):
    """if/elif/else 각 갈래가 **마지막에** 대입하는 단일 이름 목록. 아니면 None."""
    names = []
    cur = node
    while True:
        if not cur.body:
            return None
        last = cur.body[-1]
        if not (isinstance(last, ast.Assign) and len(last.targets) == 1
                and isinstance(last.targets[0], ast.Name)):
            return None
        names.append(last.targets[0].id)

        if cur.orelse and len(cur.orelse) == 1 and isinstance(cur.orelse[0], ast.If):
            cur = cur.orelse[0]          # elif
            continue
        if cur.orelse:
            last = cur.orelse[-1]
            if not (isinstance(last, ast.Assign) and len(last.targets) == 1
                    and isinstance(last.targets[0], ast.Name)):
                return None
            names.append(last.targets[0].id)
        return names


def _scan_block(body, path, hits):
    for i, stmt in enumerate(body):
        if isinstance(stmt, ast.If) and i + 1 < len(body):
            names = _branch_assign_targets(stmt)
            nxt = body[i + 1]
            if (names and len(set(names)) == 1
                    and isinstance(nxt, ast.Assign) and len(nxt.targets) == 1
                    and isinstance(nxt.targets[0], ast.Name)
                    and nxt.targets[0].id == names[0]):
                #  덮어쓰는 값이 분기 결과를 참조하면 '누적'이라 정상이다
                #  (예: x = x + 1). 참조하지 않으면 분기가 통째로 버려진다.
                used = {n.id for n in ast.walk(nxt.value) if isinstance(n, ast.Name)}
                if names[0] not in used:
                    hits.append((path, stmt.lineno, nxt.lineno, names[0]))

        for field in ("body", "orelse", "finalbody"):
            sub = getattr(stmt, field, None)
            if isinstance(sub, list):
                _scan_block(sub, path, hits)
        for handler in getattr(stmt, "handlers", []) or []:
            _scan_block(handler.body, path, hits)


def _scan_file(path):
    """[중복 제거] _scan_block 은 중첩 블록을 스스로 파고들고 ast.walk 도 같은 함수 몸통에
    다시 닿는다 — 같은 자리가 두 번 잡힌다. 줄 번호로 한 번만 센다."""
    hits = []
    tree = ast.parse(open(path, encoding="utf-8").read())
    _scan_block(tree.body, path, hits)
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            _scan_block(node.body, path, hits)
    seen, out = set(), []
    for h in hits:
        key = (h[0], h[2])
        if key not in seen:
            seen.add(key)
            out.append(h)
    return out


def _source_files():
    for root, dirs, files in os.walk(ROOT):
        dirs[:] = [d for d in dirs
                   if d not in (".git", "__pycache__", ".venv", "data", "logs", "db",
                                "chart", "json", "backups")]
        for fn in files:
            if fn.endswith(".py"):
                yield os.path.join(root, fn)


def test_분기_결과를_다음_줄이_덮어쓰지_않는다():
    bad = []
    for path in _source_files():
        rel = os.path.relpath(path, ROOT)
        for _p, if_line, assign_line, name in _scan_file(path):
            if (rel, assign_line) in _ALLOWED:
                continue
            bad.append(f"{rel}:{assign_line} — {if_line}행 분기가 정한 '{name}' 를 "
                       f"바로 다음 줄이 무조건 덮어쓴다(들여쓰기 누락?)")
    assert not bad, "\n  " + "\n  ".join(bad)


def test_검사기가_그_모양을_실제로_잡는다(tmp_path):
    """0건을 훑고 초록인 상태를 막는다 — 합성 예제로 검사기를 건다."""
    sample = tmp_path / "sample.py"
    sample.write_text(
        "def f(a, b):\n"
        "    if a:\n"
        "        mode = 'x'\n"
        "    elif b:\n"
        "        mode = 'y'\n"
        "    mode = 'z'\n"
        "    return mode\n", encoding="utf-8")
    hits = _scan_file(str(sample))
    assert len(hits) == 1 and hits[0][3] == "mode", hits


def test_정상적인_누적_대입은_잡지_않는다(tmp_path):
    """x = x + 1 처럼 분기 결과를 쓰는 재대입은 정상이다."""
    sample = tmp_path / "ok.py"
    sample.write_text(
        "def f(a):\n"
        "    if a:\n"
        "        n = 1\n"
        "    else:\n"
        "        n = 2\n"
        "    n = n + 1\n"
        "    return n\n", encoding="utf-8")
    assert _scan_file(str(sample)) == []


def test_검사기가_저장소를_실제로_훑는다():
    files = list(_source_files())
    assert len(files) > 100, f"소스 파일을 {len(files)}개밖에 못 찾았다"
