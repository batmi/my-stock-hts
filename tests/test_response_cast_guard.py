"""증권사 응답·DB 행의 숫자 필드를 **맨 float()/int()** 로 캐스팅하지 않는다.

[왜 이 파일이 있나 · 2026-09-06]
 `float(item.get('avg_prvs', 0))` 는 방어처럼 보이지만 방어가 아니다. dict.get 의
 기본값은 **키가 없을 때만** 쓰인다 — 증권사는 키를 주고 값을 **빈 문자열**로 채우고,
 SQLite 는 키를 주고 None 을 담는다. 실측:

     float('') → ValueError: could not convert string to float: ''
     {'avg_prvs': None}.get('avg_prvs', 0) → None → float(None) → TypeError

 대가가 크다. 체결 대사 루프에서 이 한 줄이 던지면 그 계좌의 **다른 종목 체결까지**
 통째로 사라진다(실측):

     _check_conclusions 결과   : has_error=True
     깨진 건(005930) 기록됐나  : False
     멀쩡한 건(000660) 기록됐나: False   ← 남의 종목이다

 같은 응답의 **수량은 전부 api.safe_int 로 받으면서 단가만** 맨 float() 인 자리가
 여러 곳이었다 — 한 줄씩 보면 눈에 띄지 않는다. 그래서 구문으로 잡는다.
"""
import ast
import os

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

#  증권사 응답·DB 행을 담는 관용 변수명. 여기 담긴 값은 '' 또는 None 일 수 있다.
_RESPONSE_NAMES = {
    "item", "out", "out1", "out2", "output", "summary", "cp_data", "res", "res_json",
    "holding", "order", "db_order", "trade", "origin_trade", "detail", "fill",
}

#  일부러 맨 캐스팅을 쓰는 자리. **사유 없이 넣지 말 것.**
_ALLOWED = {
    ("api/indices.py", "get_k200_futures_quote"):
        "실패를 0 으로 만들면 안 되는 자리다 — 예외가 곧 '모른다'이고, 바깥에서 None 을 "
        "돌려준다([[unknown-vs-empty]]). safe_float 로 바꾸면 조회 실패가 0원 시세가 된다.",
}


def _casts(path):
    """[(함수명, 줄번호, 소스), ...] — 응답성 변수에 대한 맨 캐스팅."""
    src = open(path, encoding="utf-8").read()
    try:
        tree = ast.parse(src)
    except SyntaxError:
        return []
    lines = src.split("\n")
    owner_of = {}
    for fn in ast.walk(tree):
        if isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
            for n in ast.walk(fn):
                owner_of[id(n)] = fn.name

    out = []
    for n in ast.walk(tree):
        if not (isinstance(n, ast.Call) and getattr(n.func, "id", None) in ("int", "float")):
            continue
        if not (n.args and isinstance(n.args[0], ast.Call)
                and isinstance(n.args[0].func, ast.Attribute)
                and n.args[0].func.attr == "get"):
            continue
        owner = n.args[0].func.value
        name = getattr(owner, "id", None)
        if name is None and isinstance(owner, ast.Subscript):
            name = getattr(owner.value, "id", None)
        if name in _RESPONSE_NAMES:
            out.append((owner_of.get(id(n), "<module>"), n.lineno,
                        lines[n.lineno - 1].strip()[:90]))
    return out


def _source_files():
    for root, dirs, files in os.walk(ROOT):
        dirs[:] = [d for d in dirs
                   if d not in (".git", "__pycache__", ".venv", "data", "logs", "db",
                                "chart", "json", "backups", "tests", "tools")]
        for fn in files:
            if fn.endswith(".py"):
                yield os.path.join(root, fn)


def test_응답_필드를_맨_캐스팅하지_않는다():
    bad = []
    for path in _source_files():
        rel = os.path.relpath(path, ROOT).replace(os.sep, "/")
        for fname, line, src in _casts(path):
            if (rel, fname) in _ALLOWED:
                continue
            bad.append(f"{rel}:{line} ({fname}) — {src}\n"
                       f"      → api.safe_float(x.get(k), default=...) / api.safe_int(x.get(k))")
    assert not bad, ("증권사 응답·DB 행을 맨 캐스팅하는 자리가 있다. dict.get 의 기본값은 "
                     "키가 **없을 때만** 쓰인다.\n  " + "\n  ".join(bad))


def test_예외_목록은_실재하는_자리만_가리킨다():
    """[낡음 자체 점검] 고쳐 놓고 예외만 남으면 그 예외가 다음 사고를 덮는다."""
    stale = []
    for (rel, fname), reason in _ALLOWED.items():
        path = os.path.join(ROOT, rel)
        assert os.path.exists(path), f"{rel} 이 없다 — 예외 목록이 낡았다"
        assert reason.strip(), f"{rel}::{fname} 예외에 사유가 없다"
        if fname not in {f for f, _, _ in _casts(path)}:
            stale.append(f"{rel}::{fname} — 이미 안전하게 고쳤다. 예외를 지워라")
    assert not stale, "\n  " + "\n  ".join(stale)


def test_검사기가_그_모양을_실제로_잡는다(tmp_path):
    """0건을 훑고 초록인 상태를 막는다."""
    s = tmp_path / "s.py"
    s.write_text(
        "def f(item, cfg):\n"
        "    a = float(item.get('avg_prvs', 0))\n"          # 잡아야 한다
        "    b = api.safe_float(item.get('x'), default=0)\n"  # 정상
        "    c = float(cfg.get('WHIPSAW_LO', 0.4))\n"       # 우리 설정 — 대상 아님
        "    return a, b, c\n", encoding="utf-8")
    hits = _casts(str(s))
    assert [(h[0], h[1]) for h in hits] == [("f", 2)], hits


def test_아랫첨자로_받은_응답도_잡는다(tmp_path):
    """cp_data['output'].get(...) 처럼 한 단계 들어간 형태가 실제 사고 지점이었다."""
    s = tmp_path / "t.py"
    s.write_text(
        "def g(cp_data):\n"
        "    return float(cp_data['output'].get('last', 0))\n", encoding="utf-8")
    assert [(h[0], h[1]) for h in _casts(str(s))] == [("g", 2)]


def test_검사기가_저장소를_실제로_훑는다():
    #  tests·tools 를 뺀 본체 기준. 크게 줄면 훑는 범위가 무너진 것이다.
    files = list(_source_files())
    assert len(files) > 60, f"소스 파일을 {len(files)}개밖에 못 찾았다"
