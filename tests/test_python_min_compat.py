"""최저 지원 파이썬 판(3.12)을 한 약속으로 지킨다 — 기동 가드·README 가 같은 판을 말하고, 모든 소스가 그 판에서 컴파일된다.

[2026-10-24] 최저 판 3.10 → 3.12. 3.10 은 2026-10 에 보안 지원이 끝났고 3.11 은 2027-10 에 끝난다. 운영 라즈베리파이는
3.13, 개발기는 3.14 다. 3.12 부터 f-string 안에 같은 따옴표·백슬래시·주석을 쓸 수 있어(PEP 701), 종전 이 파일
(test_python310_compat)이 지키던 '3.10/3.11 에서만 기동이 죽는 f-string'(65a47c1) 가드는 대상이 사라졌다.

남는 위험은 반대 방향이다 — 개발기(3.14)에서 쓴 3.13·3.14 전용 문법(괄호 없는 `except A, B:`, t-string 등)이 3.12 에서
SyntaxError 로 기동을 막는다. 테스트가 import 하지 않는 파일(tools/ 등)은 테스트로는 드러나지 않으므로 전수 컴파일한다.
  · 3.12 로 돌리면 지금 인터프리터가 곧 최저 판이다.
  · 개발기에서는 uv 에 최저 판이 깔려 있으면 그것으로 컴파일한다(없으면 skip — `uv python install 3.12`).
판정은 실제 인터프리터로 한다. `ast.parse(feature_version=...)` 같은 파서 흉내는 f-string 내부를 보지 않아
'3.10 문법 0건'으로 틀린 적이 있다(2026-09-20 감사, 09-26 정정).
"""
import os
import re
import shutil
import subprocess
import sys

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TARGETS = ("modules", "api", "core", "brokers", "tools", "tests", "main.py", "config.py")

MIN = (3, 12)
MIN_STR = f"{MIN[0]}.{MIN[1]}"


def _read(name):
    return open(os.path.join(ROOT, name), encoding="utf-8").read()


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


_COMPILE_ALL = (
    "import sys\n"
    "bad=[]\n"
    "for p in sys.argv[1:]:\n"
    "    try: compile(open(p,encoding='utf-8').read(), p, 'exec')\n"
    "    except SyntaxError as e: bad.append(f'{p}:{e.lineno} {e.msg}')\n"
    "print('\\n'.join(bad)); sys.exit(1 if bad else 0)\n"
)


def test_every_source_compiles_on_the_minimum_python():
    files = list(_py_files())
    assert len(files) > 300, "파일을 못 읽었다 — TARGETS 를 확인"
    if sys.version_info[:2] == MIN:
        py = sys.executable                     # 최저 판 인터프리터로 돌리는 중
    else:
        py = _uv_python(MIN_STR)
        if not py:
            pytest.skip(f"uv 의 Python {MIN_STR} 없음 — `uv python install {MIN_STR}` 뒤에 돈다")
    res = subprocess.run([py, "-c", _COMPILE_ALL, *files], capture_output=True, text=True, timeout=300)
    assert res.returncode == 0, f"Python {MIN_STR} 컴파일 실패:\n{res.stdout}{res.stderr[-500:]}"


def test_launchers_and_readme_state_the_same_minimum():
    """최저 판을 말하는 자리는 네 곳이다 — 한 곳만 올리면 기동 가드와 안내가 서로 다른 약속을 한다.
    run.bat 은 맥에서 실행해 볼 수 없어 문자열로 지킨다(2026-09-26: run.sh 에만 가드가 들어갔던 일)."""
    guard = f"sys.version_info >= ({MIN[0]}, {MIN[1]})"
    for name in ("run.sh", "run.bat"):
        text = _read(name)
        assert guard in text, f"{name}: {MIN_STR} 미만 차단 가드가 없다"
        assert f"Python {MIN_STR} 이상이 필요합니다" in text, f"{name}: 안내 문구의 판이 다르다"
        assert "[실패]" in text, f"{name}: pip 실패를 따로 표시하지 않는다"
        assert not re.search(r"sys\.version_info >= \(3, (?!%d\))\d+\)" % MIN[1], text), f"{name}: 다른 판의 가드가 남아 있다"
    assert f"Python {MIN_STR} 이상" in _read("README.md")
    assert f"Python {MIN_STR}+" in _read("README.en.md")
