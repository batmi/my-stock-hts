"""run.sh 의 '설치돼 있는가' 판정은 import 를 실행하지 않는다.

[배경 · 2026-09-17] pykrx 는 패키지 import 시점에 KRX 로그인을 시도한다(KRX_ID/KRX_PW).
KRX 가 에러 페이지(HTML)를 주면 그 JSON 파싱 예외로 `import pykrx` 자체가 죽는다.
종전 run.sh 는 `python -c "import pykrx"` 의 실패를 "설치되어 있지 않다"로 읽어 pip 를
다시 돌리고(이미 만족), 그래도 import 가 안 되니 기동을 **중단**했다 — 앱은 pykrx 없이
FDR 로 폴백해 도는데 런처가 먼저 포기한 것이다. '설치 안 됨'과 '설치됐지만 import 가
죽음'은 다른 문제다.
"""
import os
import subprocess
import sys
import textwrap

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
RUN_SH = os.path.join(ROOT, "run.sh")


def _is_installed(name, extra_path):
    """run.sh 의 _is_installed 함수만 떼어 같은 셸에서 실행한다."""
    script = textwrap.dedent(f"""
        PYTHON_PATH="{sys.executable}"
        eval "$(sed -n '/^_is_installed() {{/,/^}}/p' "{RUN_SH}")"
        _is_installed "{name}"
    """)
    env = dict(os.environ, PYTHONPATH=extra_path + os.pathsep + os.environ.get("PYTHONPATH", ""))
    return subprocess.run(["bash", "-c", script], env=env, capture_output=True).returncode == 0


@pytest.fixture
def fake_site(tmp_path):
    """import 하면 예외를 내는 가짜 패키지 하나 + 정상 패키지 하나."""
    broken = tmp_path / "broken_on_import"
    broken.mkdir()
    (broken / "__init__.py").write_text("raise ValueError('login page was HTML')\n")
    fine = tmp_path / "fine_pkg"
    fine.mkdir()
    (fine / "__init__.py").write_text("X = 1\n")
    return str(tmp_path)


def test_a_package_whose_import_raises_still_counts_as_installed(fake_site):
    assert _is_installed("broken_on_import", fake_site), \
        "import 시점 예외를 '미설치'로 읽으면 pip 를 헛돌리고 기동을 막는다"


def test_a_normal_package_is_installed(fake_site):
    assert _is_installed("fine_pkg", fake_site)


def test_a_missing_package_is_missing(fake_site):
    assert not _is_installed("surely_not_installed_pkg_xyz", fake_site)


def test_run_sh_no_longer_probes_by_executing_the_import():
    src = open(RUN_SH, encoding="utf-8").read()
    assert '-c "import $IMPORT_NAME"' not in src, \
        "설치 판정에 import 실행이 다시 들어왔다 — _is_installed(find_spec) 를 쓸 것"
