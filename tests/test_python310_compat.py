"""README 가 약속하는 'Python 3.10 이상'을 지킨다 — 3.12 전용 f-string 문법(PEP 701) 가드.

[왜 · 2026-09-26] modules/settings.py 에 따옴표를 재사용한 중첩 f-string 이 있어 3.10/3.11 에서 **기동 자체가
불가**했다(65a47c1 에서 수정). 09-20 감사는 `ast.parse(feature_version=(3, 10))` 로 '3.10 문법 0건'이라
판정했는데, feature_version 은 **f-string 내부 문법을 검사하지 않는다** — 그 도구로는 잡을 수 없었다.

개발기는 3.14 라 컴파일로는 드러나지 않는다. 3.12+ 토크나이저가 f-string 을 FSTRING_START/…/END 로
쪼개 주므로, 그 토큰 위에서 3.12 이전에는 문법 오류였던 세 모양을 찾는다:
  ① 치환 필드 안에서 바깥 f-string 과 **같은 따옴표**의 문자열(f-string 포함)
  ② 치환 필드 안 문자열의 **백슬래시**
  ③ 치환 필드 안의 **주석**
uv 로 3.10/3.11 이 깔려 있으면 실제 인터프리터로도 전수 컴파일한다(없으면 그 테스트만 skip).
"""
import io
import os
import re
import shutil
import subprocess
import sys
import tokenize

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TARGETS = ("modules", "api", "core", "brokers", "tools", "tests", "main.py", "config.py")

pytestmark = pytest.mark.skipif(sys.version_info < (3, 12),
                                reason="FSTRING_* 토큰은 3.12+ 토크나이저에만 있다(구 버전에선 컴파일이 곧 검사)")


def _py_files():
    for t in TARGETS:
        p = os.path.join(ROOT, t)
        if os.path.isfile(p):
            yield p
            continue
        for d, dirs, files in os.walk(p):
            dirs[:] = [x for x in dirs if x != "__pycache__"]
            for f in files:
                if f.endswith(".py"):
                    yield os.path.join(d, f)


def _quote(tok_string):
    body = re.sub(r"^[A-Za-z]*", "", tok_string)
    return body[:3] if body[:3] in ('"""', "'''") else body[:1]


def pep701_violations(source):
    """[(행, 사유)] — 3.12 미만에서 SyntaxError 인 f-string 모양."""
    out = []
    stack = []          # [ [quote, brace_depth], ... ]
    toks = tokenize.generate_tokens(io.StringIO(source).readline)
    for tok in toks:
        inside = bool(stack) and stack[-1][1] > 0
        if tok.type == tokenize.FSTRING_START:
            q = _quote(tok.string)
            if inside and q == stack[-1][0]:
                out.append((tok.start[0], f"중첩 f-string 이 바깥과 같은 따옴표({q})"))
            stack.append([q, 0])
        elif tok.type == tokenize.FSTRING_END:
            if stack:
                stack.pop()
        elif tok.type == tokenize.OP and stack and tok.string in ("{", "}"):
            stack[-1][1] += 1 if tok.string == "{" else -1
        elif tok.type == tokenize.STRING and inside:
            if _quote(tok.string) == stack[-1][0]:
                out.append((tok.start[0], f"치환 필드 안 문자열이 바깥과 같은 따옴표({_quote(tok.string)})"))
            if "\\" in tok.string:
                out.append((tok.start[0], "치환 필드 안 문자열에 백슬래시"))
        elif tok.type == tokenize.COMMENT and inside:
            out.append((tok.start[0], "치환 필드 안 주석"))
    return out


def test_no_pep701_only_fstrings_anywhere():
    hits = []
    n = 0
    for p in _py_files():
        n += 1
        src = open(p, encoding="utf-8").read()
        for line, why in pep701_violations(src):
            hits.append(f"{os.path.relpath(p, ROOT)}:{line} {why}")
    assert n > 300, "파일을 못 읽었다 — TARGETS 를 확인"
    assert not hits, "3.10/3.11 에서 SyntaxError 가 나는 f-string:\n" + "\n".join(hits)


@pytest.mark.parametrize("src, expect", [
    # 65a47c1 이 고친 실제 모양(바깥 " 안에 f"...")
    ('x = f"a {\'b\' + f"{1:g}" + \'c\'}"\n', True),
    ('x = f"{d["k"]}"\n', True),                       # 같은 따옴표 아랫첨자
    ("x = f'{\"\\n\".join(a)}'\n", True),              # 필드 안 백슬래시
    ('x = f"""{a  # 주석\n}"""\n', True),              # 필드 안 주석
    ('x = f"{d[\'k\']}"\n', False),                    # 다른 따옴표 — 3.10 에서도 된다
    ('x = f"{a:>{w}}"\n', False),                      # 중첩 포맷 스펙
    ('x = f"{{literal}}" + "\\n"\n', False),           # 이스케이프된 중괄호·필드 밖 백슬래시
])
def test_the_detector_actually_detects(src, expect):
    assert bool(pep701_violations(src)) is expect


def _uv_python(ver):
    uv = shutil.which("uv")
    if not uv:
        return None
    try:
        res = subprocess.run([uv, "python", "find", ver], capture_output=True, text=True, timeout=20)
    except Exception:      # noqa: BLE001
        return None
    path = res.stdout.strip().splitlines()[-1] if res.returncode == 0 and res.stdout.strip() else None
    return path if path and os.path.exists(path) else None


@pytest.mark.parametrize("ver", ["3.10", "3.11"])
def test_real_old_interpreter_compiles_everything(ver):
    """uv 로 설치된 실제 구 버전이 있으면 전수 컴파일 — 위 탐지기가 모르는 문법까지 잡는다."""
    py = _uv_python(ver)
    if not py:
        pytest.skip(f"uv 의 Python {ver} 없음")
    code = (
        "import os,sys\n"
        "bad=[]\n"
        "for p in sys.argv[1:]:\n"
        "    try: compile(open(p,encoding='utf-8').read(), p, 'exec')\n"
        "    except SyntaxError as e: bad.append(f'{p}:{e.lineno} {e.msg}')\n"
        "print('\\n'.join(bad)); sys.exit(1 if bad else 0)\n"
    )
    res = subprocess.run([py, "-c", code, *_py_files()], capture_output=True, text=True, timeout=300)
    assert res.returncode == 0, f"Python {ver} 컴파일 실패:\n{res.stdout}{res.stderr[-500:]}"


def test_both_launchers_refuse_python_below_310_and_report_pip_failure():
    """run.sh 와 run.bat 은 같은 기동 가드를 가진다 — 3.10 미만 차단, pip 실패를 '완료'로 적지 않기.
    (2026-09-26: run.sh 에만 들어가고 run.bat 은 빠져 있었다. run.bat 은 맥에서 실행해 볼 수 없어 문자열로 지킨다.)"""
    for name in ("run.sh", "run.bat"):
        text = open(os.path.join(ROOT, name), encoding="utf-8").read()
        assert "sys.version_info >= (3, 10)" in text, f"{name}: 3.10 미만 차단 가드가 없다"
        assert "[실패]" in text, f"{name}: pip 실패를 따로 표시하지 않는다"
    for readme in ("README.md", "README.en.md"):
        assert "3.10" in open(os.path.join(ROOT, readme), encoding="utf-8").read(), f"{readme}: 최소 버전 안내가 없다"
