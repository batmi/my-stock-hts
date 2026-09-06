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
    """if/elif/else 각 갈래가 **모두** 대입하는 단일 이름들. 갈래가 하나라도 안 대입하면 빈 집합.

    [넓힘 2026-09-06] 종전에는 각 갈래의 **마지막 문장**이 대입이어야만 인정했다. 그
     조건 때문에 trader._run_loop 의 계좌 표시(4번째 사례)를 놓쳤다 — 한 갈래가 값을
     정한 **뒤에** 표시용 계좌번호를 더 만지느라 마지막 문장이 if 였다:

        if is_paper:
            acc_type = "가상투자"
            ...
            if _vc: display_cano = ...     # ← 마지막 문장이 대입이 아니다
        elif is_toss:
            acc_type = "토스증권"
        acc_type = "한투증권(자동)"        # ← 분기 밖. 늘 이 값이 된다.

     '마지막 문장'이 아니라 '그 갈래가 정하는 이름'을 본다. 모든 갈래가 정하는 이름만
     후보로 삼으므로, 한쪽에서만 쓰는 임시 변수는 걸리지 않는다.
    """
    per_branch = []
    cur = node
    while True:
        if not cur.body:
            return set()
        per_branch.append(_assigned_names(cur.body))

        if cur.orelse and len(cur.orelse) == 1 and isinstance(cur.orelse[0], ast.If):
            cur = cur.orelse[0]          # elif
            continue
        if cur.orelse:
            per_branch.append(_assigned_names(cur.orelse))
        break

    #  갈래가 하나뿐(= elif·else 가 없는 맨 if)이면 뒤따르는 대입은 '조건부로 손보고
    #  기본값으로 되돌리기' 같은 정상 관용구일 수 있다. 실제 사고는 전부 if/elif 였다.
    if len(per_branch) < 2:
        return set()

    common = set(per_branch[0])
    for names in per_branch[1:]:
        common &= names
    return common


def _assigned_names(body):
    """그 블록이 **직접** 대입하는 단일 이름들 (중첩 블록 안은 세지 않는다).

    중첩까지 세면 `if 조건: x = ...` 처럼 조건부로만 정하는 이름이 섞여, 뒤따르는
    대입이 정당한 기본값 채우기인 경우까지 잡는다.
    """
    #  갈래가 return·raise·continue·break 로 끝나면 뒤따르는 대입은 그 갈래에 닿지
    #  않는다 — 덮어쓰기가 아니라 '다른 경로의 시작'이다.
    if body and isinstance(body[-1], (ast.Return, ast.Raise, ast.Continue, ast.Break)):
        return set()
    out = set()
    for stmt in body:
        if (isinstance(stmt, ast.Assign) and len(stmt.targets) == 1
                and isinstance(stmt.targets[0], ast.Name)):
            out.add(stmt.targets[0].id)
    return out


def _scan_block(body, path, hits):
    for i, stmt in enumerate(body):
        if isinstance(stmt, ast.If) and i + 1 < len(body):
            names = _branch_assign_targets(stmt)
            nxt = body[i + 1]
            if (names
                    and isinstance(nxt, ast.Assign) and len(nxt.targets) == 1
                    and isinstance(nxt.targets[0], ast.Name)
                    and nxt.targets[0].id in names):
                #  덮어쓰는 값이 분기 결과를 참조하면 '누적'이라 정상이다
                #  (예: x = x + 1). 참조하지 않으면 분기가 통째로 버려진다.
                target = nxt.targets[0].id
                used = {n.id for n in ast.walk(nxt.value) if isinstance(n, ast.Name)}
                if target not in used:
                    hits.append((path, stmt.lineno, nxt.lineno, target))

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


def test_갈래가_값을_정한_뒤_더_손봐도_잡는다(tmp_path):
    """[넓힘 자체 점검] 4·5번째 사례의 모양 — 마지막 문장이 대입이 아니다.

    종전 검사기는 '각 갈래의 **마지막 문장**이 대입'일 때만 인정해서, 값을 정한 뒤
    표시용 값을 더 만지는 갈래를 통째로 놓쳤다. 그 구멍으로 trader._run_loop 의 계좌
    표시와 utils.print_breadcrumb 의 메뉴 헤더가 살아남았다.
    """
    sample = tmp_path / "late.py"
    sample.write_text(
        "def f(a, b, c):\n"
        "    if a:\n"
        "        mode = 'x'\n"
        "        if c:\n"
        "            label = 'c'\n"
        "    elif b:\n"
        "        mode = 'y'\n"
        "    mode = 'z'\n"
        "    return mode\n", encoding="utf-8")
    hits = _scan_file(str(sample))
    assert len(hits) == 1 and hits[0][3] == "mode", hits


def test_맨_if_뒤의_대입은_잡지_않는다(tmp_path):
    """elif·else 가 없으면 '조건부로 손보고 기본 경로로 이어가기'가 정상 관용구다
    (예: api.quotes.price 의 지수 조회 → 실패 시 현재가 폴백)."""
    sample = tmp_path / "bare.py"
    sample.write_text(
        "def f(a):\n"
        "    if a:\n"
        "        data = 1\n"
        "        if data:\n"
        "            return data\n"
        "    data = 2\n"
        "    return data\n", encoding="utf-8")
    assert _scan_file(str(sample)) == []


def test_return_으로_끝나는_갈래는_잡지_않는다(tmp_path):
    """그 갈래는 뒤 문장에 닿지 않는다 — 덮어쓰기가 아니라 다른 경로의 시작이다."""
    sample = tmp_path / "ret.py"
    sample.write_text(
        "def f(a, b):\n"
        "    if a:\n"
        "        dead = 1\n"
        "        return dead\n"
        "    else:\n"
        "        dead = 2\n"
        "        return dead\n"
        "    dead = 0\n"
        "    return dead\n", encoding="utf-8")
    assert _scan_file(str(sample)) == []
