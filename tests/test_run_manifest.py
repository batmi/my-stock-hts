"""실험 실행 기록(modules/run_manifest) — 감사 한 번이 무엇으로 돌았는지 결과와 함께 남는다."""
import json
import os
import subprocess
import sys
import tempfile

from modules import run_manifest as rm

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def test_build_records_what_reproduction_needs():
    rec = rm.build(argv=["tools/audit_x.py", "--seed", "7", "--pool-seeds=1,2,3"])
    for key in ("git", "packages", "config", "scoring_rules_version", "seeds", "data_sources",
                "unmodeled", "python", "platform"):
        assert key in rec, key
    assert rec["seeds"] == {"--seed": "7", "--pool-seeds": "1,2,3"}
    assert rec["config"]["fingerprint"] and "SELL_STRATEGY" in rec["config"]["settings"]
    assert set(rec["config"]["costs"]) >= {"BUY_FEE_RATE", "SELL_FEE_RATE", "SLIPPAGE_RATE"}
    assert rec["git"]["commit"] and len(rec["git"]["commit"]) >= 7
    assert {"entry", "sell"} <= set(rec["unmodeled"])
    json.dumps(rec, default=str)


def test_manifest_never_imports_db_touching_modules():
    """실행 기록은 이미 로드된 모듈만 읽는다 — 스스로 db_manager 를 끌어올리면 운영 DB 를 연다."""
    code = (f"import sys; sys.path.insert(0, {ROOT!r})\n"
            "from modules import run_manifest as r\n"
            "r.build(argv=['tools/audit_x.py'])\n"
            "print('DB' if 'modules.db_manager' in sys.modules else 'CLEAN')\n")
    p = subprocess.run([sys.executable, "-c", code], cwd=ROOT, capture_output=True, text=True, timeout=120)
    assert p.stdout.strip().endswith("CLEAN"), (p.stdout[-300:], p.stderr[-800:])


def test_data_source_summary_shows_fallbacks(monkeypatch):
    from modules import backtest
    monkeypatch.setattr(backtest, "_DAILY_SOURCE",
                        {"005930": "KRX/OPENAPI", "000660": "yfinance", "123456": None})
    ds = rm.data_sources()
    assert ds["daily"] == {"KRX/OPENAPI": 1, "yfinance": 1, "None": 1}
    assert ds["daily_failed"] == ["123456"]


def test_config_fingerprint_changes_with_settings():
    import config
    a = rm.config_snapshot()["fingerprint"]
    keep = config.SELL_STRATEGY["SELL_SCORE"]
    try:
        config.SELL_STRATEGY["SELL_SCORE"] = keep + 0.5
        assert rm.config_snapshot()["fingerprint"] != a
    finally:
        config.SELL_STRATEGY["SELL_SCORE"] = keep


def test_tee_keeps_a_bounded_tail():
    class _Sink:
        def __init__(self):
            self.out = []

        def write(self, t):
            self.out.append(t)

        def flush(self):
            pass
    sink = _Sink()
    t = rm._Tee(sink, limit=10)
    for i in range(20):
        t.write(f"{i},")
    assert "".join(sink.out).startswith("0,1,")                 # 원래 출력은 그대로 나간다
    assert len(t.tail()) <= 10 and t.tail().endswith("19,")


def test_not_started_on_import_or_outside_tools(monkeypatch):
    assert rm.auto_start_for_script(os.path.join(ROOT, "tools", "audit_x.py")) is False   # pytest 안
    monkeypatch.setenv("HTS_NO_RUN_MANIFEST", "1")
    assert rm.auto_start_for_script(os.path.join(ROOT, "tools", "audit_x.py")) is False


def test_script_run_writes_one_record_with_the_result_tail():
    """별도 프로세스에서 '감사 도구가 스크립트로 실행된' 상황을 만든다(임시 디렉터리에 저장)."""
    out_dir = tempfile.mkdtemp()
    code = (
        "import sys, __main__\n"
        f"sys.path.insert(0, {ROOT!r})\n"
        f"__main__.__file__ = {os.path.join(ROOT, 'tools', 'audit_fake.py')!r}\n"
        "sys.argv = ['tools/audit_fake.py', '--seed', '42']\n"
        "from modules import run_manifest as r\n"
        f"r.RUNS_DIR = {out_dir!r}\n"
        "assert r.auto_start_for_script(__main__.__file__)\n"
        "r.note('result', {'cagr': 12.3})\n"
        "print('결과 표: 수익 +12.3%')\n"
    )
    env = dict(os.environ)
    env.pop("HTS_NO_RUN_MANIFEST", None)
    env.pop("PYTEST_CURRENT_TEST", None)
    p = subprocess.run([sys.executable, "-c", code], cwd=ROOT, capture_output=True, text=True,
                       timeout=120, env=env)
    assert p.returncode == 0, p.stderr[-2000:]
    files = [f for f in os.listdir(out_dir) if f.endswith(".json")]
    assert len(files) == 1, (files, p.stdout[-500:])
    rec = json.load(open(os.path.join(out_dir, files[0]), encoding="utf-8"))
    assert rec["tool"] == "audit_fake.py" and rec["seeds"] == {"--seed": "42"}
    assert "결과 표: 수익 +12.3%" in rec["stdout_tail"]
    assert rec["notes"] == {"result": {"cagr": 12.3}}
    assert rec["finished_at"] and rec["error"] is None
