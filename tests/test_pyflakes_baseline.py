"""pyflakes 기준선 — 미정의 이름·모듈 가림·재정의 계열은 0건이어야 한다.

[왜 · 2026-09-20] 린터 축 감사에서 pyflakes 를 처음 전수 실행했다. 미정의 이름은 0건이었고
(test_unbound_name_guard 가 지켜 온 결과), 유일한 실질 항목은 modules/backtest.register_market_types
의 루프 변수 `market` 이 모듈 `market` 을 가리던 것(무해했으나 정정). 그 기준선을 여기서 고정한다.

미사용 import 는 **일부러 검사하지 않는다** — 테스트가 `patch('modules.analysis.api...')` 처럼
모듈에 걸린 이름을 patch 대상으로 쓰므로, '안 쓰는' import 를 걷어내면 테스트 격리가 깨진다.
"""
import io
import os

import pytest

pyflakes_api = pytest.importorskip("pyflakes.api")
from pyflakes import reporter as _reporter  # noqa: E402

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TARGETS = ("modules", "api", "core", "brokers", "main.py", "config.py")
# 결함으로 보는 메시지 조각 — 위생 항목(unused import 등)은 넣지 않는다.
DEFECT_MARKERS = (
    "undefined name",
    "referenced before assignment",
    "shadowed by loop variable",
    "duplicate argument",
)


def _run_pyflakes():
    out, err = io.StringIO(), io.StringIO()
    rep = _reporter.Reporter(out, err)
    for t in TARGETS:
        path = os.path.join(ROOT, t)
        if os.path.isdir(path):
            for dirpath, dirs, files in os.walk(path):
                dirs[:] = [d for d in dirs if d != "__pycache__"]
                for fn in files:
                    if fn.endswith(".py"):
                        pyflakes_api.checkPath(os.path.join(dirpath, fn), rep)
        else:
            pyflakes_api.checkPath(path, rep)
    return out.getvalue().splitlines(), err.getvalue()


def test_no_defect_class_findings():
    lines, err = _run_pyflakes()
    assert not err.strip(), f"pyflakes 가 파일을 못 읽었다: {err[:300]}"
    assert len(lines) > 50, "pyflakes 가 아무것도 안 봤다 — 경로가 바뀌었으면 TARGETS 를 고쳐라"
    hits = [l for l in lines if any(m in l for m in DEFECT_MARKERS)]
    assert not hits, "pyflakes 결함 계열:\n" + "\n".join(hits)


def test_the_marker_list_actually_matches_pyflakes_wording(tmp_path):
    """검사기가 실제로 잡는가 — 미정의 이름과 루프 변수 가림을 심어 본다."""
    bad = tmp_path / "bad.py"
    bad.write_text("import os\nfor os in range(3):\n    pass\nprint(nope)\n", encoding="utf-8")
    out = io.StringIO()
    pyflakes_api.checkPath(str(bad), _reporter.Reporter(out, io.StringIO()))
    text = out.getvalue()
    assert "undefined name" in text and "shadowed by loop variable" in text
