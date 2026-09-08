"""모듈을 **읽기만 한** 프로세스가 운영 원장을 고치지 못하게 한다.

[왜 · 2026-09-08 감사] `modules/db_manager.py` 는 import 되는 것만으로
`atexit.register(db.run_vacuum)` 을 걸었다. 그런데 그 등록은 **본체에서는 한 번도
돌지 않았다** — main.py 는 `os._exit(0)` 으로 끝나 atexit 를 건너뛰고, 종료 정리는
이미 종료 절차 4/4 에서 run_vacuum() 을 직접 부른다.

정작 도는 것은 반대쪽이었다. 이 모듈을 import 하기만 한 프로세스는 정상 종료하므로
atexit 가 돈다 — `tools/audit_*.py` 전부, 일회성 스크립트, `python -c` 한 줄.
run_vacuum 은 읽기가 아니라 trades·signal_ledger 에서 보존기간 밖 행을 DELETE 하고
VACUUM 으로 파일을 다시 쓰는 일이다. 즉 **분석 도구를 돌릴 때마다 운영 원장이 깎였다.**
신호 원장은 감사 증거인데([[signal-ledger-observability]]) 그 보존 시계가 매매와
아무 상관 없는 실행으로 전진했다.

규칙: **import 는 읽기다.** 모듈을 불러오는 것만으로 원장을 지우는 종료 훅을 걸지
않는다. 지우는 일은 의도한 종료 경로에서 명시적으로 부른다.
"""
import json
import pathlib
import subprocess
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent

#  import 시점에 atexit 로 걸려도 되는 것. 원장을 고치지 않는 것만 허용하며,
#  추가할 때는 '무엇을 하는가'를 반드시 함께 적는다.
_ALLOWED_HINTS = ()


_PROBE = r"""
import atexit, json, sys, tempfile, os
sys.path.insert(0, {root!r})

# 운영 DB 를 건드리지 않도록 임시 경로로 돌린 뒤 import 한다
# (DBManager.__init__ 이 _init_db() 로 파일을 연다).
import config
config.DB_FILE_PATH = os.path.join(tempfile.mkdtemp(), "probe.db")

recorded = []
_orig = atexit.register


def _spy(fn, *a, **k):
    recorded.append(getattr(fn, "__qualname__", None) or repr(fn))
    return _orig(fn, *a, **k)


atexit.register = _spy
import modules.db_manager  # noqa: F401,E402
atexit.register = _orig

print("PROBE" + json.dumps(recorded))
"""


def _atexit_hooks_on_import():
    """db_manager 를 import 할 때 걸리는 atexit 훅 이름 목록."""
    res = subprocess.run(
        [sys.executable, "-c", _PROBE.format(root=str(ROOT))],
        cwd=str(ROOT), capture_output=True, text=True, timeout=120,
    )
    assert res.returncode == 0, f"프로브 실행 실패:\n{res.stdout}\n{res.stderr}"
    for line in res.stdout.splitlines():
        if line.startswith("PROBE"):
            return json.loads(line[len("PROBE"):])
    raise AssertionError(f"프로브가 결과를 내지 않았습니다:\n{res.stdout}\n{res.stderr}")


def test_importing_db_manager_registers_no_db_mutating_atexit_hook():
    hooks = _atexit_hooks_on_import()
    bad = [h for h in hooks
           if "DBManager" in h and not any(a in h for a in _ALLOWED_HINTS)]
    assert not bad, (
        f"db_manager 를 import 하는 것만으로 DB 를 고치는 종료 훅이 걸렸습니다: {bad}\n"
        f"import 는 읽기다 — 원장을 지우는 일은 의도한 종료 경로에서 직접 부를 것 "
        f"(main.py 의 종료 절차 4/4 가 run_vacuum() 을 부른다)."
    )


def test_db_manager_source_has_no_atexit_registration():
    """소스에서도 막는다 — 훅 이름이 DBManager 밖(모듈 함수·람다)으로 나가도 잡히게."""
    src = (ROOT / "modules" / "db_manager.py").read_text(encoding="utf-8")
    offending = [ln.strip() for ln in src.splitlines()
                 if "atexit.register" in ln and not ln.lstrip().startswith("#")]
    assert not offending, (
        f"db_manager 에 atexit 등록이 되살아났습니다: {offending}\n"
        f"main.py 는 os._exit(0) 으로 끝나 이 등록은 본체에서 돌지 않고, "
        f"모듈을 import 한 도구·스크립트에서만 돕니다."
    )
