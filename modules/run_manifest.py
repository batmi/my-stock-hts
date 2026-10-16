"""실험 실행 기록 — 감사·백테스트 한 번이 '무엇으로' 돌았는지를 결과와 함께 남긴다.

[왜 · 2026-10-16] 이 저장소의 결론은 대부분 감사 도구의 표에서 나온다. 그런데 표만 남으면
 같은 표를 다시 만들 수 있는지 알 수 없다 — 코드가 바뀌었는지(커밋·수정 여부), 설정이 무엇
 이었는지, 일봉이 어느 소스에서 왔는지(KRX 폴백은 표에 안 보인다), 수급 데이터가 몇 종목이나
 빠졌는지, 비용 가정·씨드·의존성 버전, 백테스트가 재현하지 못한 실매매 조건이 무엇이었는지.
 실제로 계측기 결함·데이터 출처 차이로 결론을 뒤집은 일이 여러 번 있었다
 ([[audit-parallel-data-integrity]] [[audit-rerun-2026-08-19]]).

[무엇을 남기나] data/audit_runs/<시각>_<도구>.json 한 파일:
  tool·argv·시작/종료 시각·종료 상태, git(커밋·브랜치·수정 파일), 파이썬·플랫폼·핵심 패키지
  버전, 설정 전체 사본과 지문, 채점 규칙판, 거래비용·슬리피지, 인자에서 읽은 씨드,
  일봉·수급 출처 요약, 백테스트 미모델 조건, 표본 유니버스(씨드별), 표준출력 꼬리(결과 표).

[하지 않는 것] 운영 DB 를 열지 않는다(감사가 운영 파일을 건드리면 안 된다). 결과를 해석하지
 않는다. 최신 데이터로 다시 돌린 결과를 '원래 실험의 정확한 재현'으로 표시하지 않는다 —
 같은 커밋·같은 설정이어도 데이터 스냅샷이 다르면 다른 실험이다(출처 요약이 그 근거다).
"""
from __future__ import annotations

import atexit
import collections
import datetime as _dt
import hashlib
import json
import os
import platform
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
RUNS_DIR = os.path.join(ROOT, "data", "audit_runs")
STDOUT_TAIL_BYTES = 64 * 1024
PACKAGES = ("pandas", "numpy", "yfinance", "requests", "rich", "pydantic", "tvdatafeed")
SEED_FLAGS = ("--seed", "--seeds", "--pool-seeds", "--universe-seeds")

_STATE = {"active": False, "path": None, "started_at": None, "notes": {}, "universe": {},
          "tee": None, "error": None}


# ── 수집 도우미 (각자 실패해도 기록은 남는다 — 모르는 값은 None) ─────────────────────────
def git_info(root=ROOT):
    """커밋·브랜치는 .git 파일에서 직접 읽는다. 수정 여부는 git 명령(없으면 None)."""
    out = {"commit": None, "branch": None, "dirty": None, "dirty_files": None}
    gd = os.path.join(root, ".git")
    try:
        head = open(os.path.join(gd, "HEAD"), encoding="utf-8").read().strip()
        if head.startswith("ref:"):
            ref = head.split(" ", 1)[1].strip()
            out["branch"] = ref.rsplit("/", 1)[-1]
            p = os.path.join(gd, ref)
            if os.path.exists(p):
                out["commit"] = open(p, encoding="utf-8").read().strip()
            else:
                for line in open(os.path.join(gd, "packed-refs"), encoding="utf-8"):
                    if line.strip().endswith(ref):
                        out["commit"] = line.split()[0]
        else:
            out["commit"] = head
    except Exception:           # noqa: BLE001
        pass
    try:
        import subprocess
        r = subprocess.run(["git", "status", "--porcelain"], cwd=root, capture_output=True,
                           text=True, timeout=5)
        if r.returncode == 0:
            files = [l[3:] for l in r.stdout.splitlines() if l.strip()]
            out["dirty"] = bool(files)
            out["dirty_files"] = files[:50]
    except Exception:           # noqa: BLE001
        pass
    return out


def package_versions(names=PACKAGES):
    from importlib import metadata
    out = {}
    for n in names:
        try:
            out[n] = metadata.version(n)
        except Exception:       # noqa: BLE001
            out[n] = None
    return out


def config_snapshot():
    """설정 전체 사본·지문·프로필·비용 가정. 설정을 못 읽으면 None 들."""
    try:
        import config
        dump = config.capture_cycle_snapshot()
        blob = json.dumps(dump, sort_keys=True, default=str, ensure_ascii=False)
        costs = {k: getattr(config, k, None) for k in
                 ("BUY_FEE_RATE", "SELL_FEE_RATE", "SLIPPAGE_RATE",
                  "OVERSEAS_BUY_FEE_RATE", "OVERSEAS_SELL_FEE_RATE")}
        return {"fingerprint": hashlib.sha1(blob.encode("utf-8")).hexdigest()[:16],
                "profile": getattr(config, "active_profile", None), "costs": costs,
                "settings": dump}
    except Exception as e:      # noqa: BLE001
        return {"fingerprint": None, "error": f"{type(e).__name__}: {e}"}


def scoring_rules_version():
    try:
        from modules import score_model
        return score_model.SCORING_RULES_VERSION
    except Exception:           # noqa: BLE001
        return None


def seeds_from_argv(argv):
    out = {}
    for i, a in enumerate(argv):
        for flag in SEED_FLAGS:
            if a == flag and i + 1 < len(argv):
                out[flag] = argv[i + 1]
            elif a.startswith(flag + "="):
                out[flag] = a.split("=", 1)[1]
    return out


def data_sources():
    """이 실행에서 일봉·수급을 어디서 받았나(백테스트 모듈이 종목별로 쌓아 둔 표의 요약)."""
    out = {}
    #  이미 로드된 모듈만 읽는다 — 여기서 새로 import 하면 db_manager 가 따라 올라와 운영 DB 를
    #   연다(실행 기록이 운영 파일을 건드리면 안 된다). 도구가 백테스트를 안 썼다면 출처도 없다.
    backtest = sys.modules.get("modules.backtest")
    if backtest is None:
        return {"daily": {}, "smart_money": {}, "note": "백테스트 모듈을 쓰지 않은 실행"}
    try:
        daily = dict(getattr(backtest, "_DAILY_SOURCE", {}) or {})
        out["daily"] = dict(collections.Counter(str(v) for v in daily.values()))
        out["daily_failed"] = sorted(c for c, v in daily.items() if v is None)[:100]
        sm = dict(getattr(backtest, "_SMART_MONEY_SOURCE", {}) or {})
        out["smart_money"] = dict(collections.Counter(str(v) for v in sm.values()))
    except Exception as e:      # noqa: BLE001
        out["error"] = f"{type(e).__name__}: {e}"
    return out


def unmodeled_conditions():
    """켜져 있는데 백테스트가 재현하지 못하는 실매매 조건(진입·청산)."""
    pb = sys.modules.get("modules.portfolio_backtest")      # 위와 같은 이유로 새로 import 하지 않는다
    if pb is None:
        return {"entry": None, "sell": None, "note": "포트폴리오 백테스트를 쓰지 않은 실행"}
    try:
        return {"entry": pb.unmodeled_entry_features(), "sell": pb.unmodeled_sell_features()}
    except Exception as e:      # noqa: BLE001
        return {"error": f"{type(e).__name__}: {e}"}


# ── 표준출력 꼬리 ────────────────────────────────────────────────────────────────
class _Tee:
    def __init__(self, stream, limit=STDOUT_TAIL_BYTES):
        self._s, self._buf, self._n, self._limit = stream, collections.deque(), 0, limit

    def write(self, text):
        self._s.write(text)
        self._buf.append(text)
        self._n += len(text)
        while self._n > self._limit and len(self._buf) > 1:
            self._n -= len(self._buf.popleft())
        return len(text)

    def flush(self):
        self._s.flush()

    def tail(self):
        return "".join(self._buf)[-self._limit:]

    def __getattr__(self, name):
        return getattr(self._s, name)


# ── 공개 API ───────────────────────────────────────────────────────────────────
def note(key, value):
    """도구가 결과·기준선 등 남기고 싶은 값을 덧붙인다(JSON 직렬화 가능한 값)."""
    _STATE["notes"][str(key)] = value


def note_universe(seed, targets):
    """씨드별 표본 유니버스(코드 목록)."""
    try:
        _STATE["universe"][str(seed)] = [t[0] if isinstance(t, (tuple, list)) else t for t in targets]
    except Exception:           # noqa: BLE001
        pass


def build(argv=None, finished=True):
    argv = list(sys.argv if argv is None else argv)
    tee = _STATE.get("tee")
    return {
        "tool": os.path.basename(argv[0]) if argv else None,
        "argv": argv,
        "started_at": _STATE.get("started_at"),
        "finished_at": _dt.datetime.now().isoformat(timespec="seconds") if finished else None,
        "error": _STATE.get("error"),
        "git": git_info(),
        "python": sys.version.split()[0], "platform": platform.platform(),
        "machine": platform.machine(),
        "packages": package_versions(),
        "config": config_snapshot(),
        "scoring_rules_version": scoring_rules_version(),
        "seeds": seeds_from_argv(argv),
        "data_sources": data_sources(),
        "unmodeled": unmodeled_conditions(),
        "universe": _STATE.get("universe"),
        "notes": _STATE.get("notes"),
        "stdout_tail": tee.tail() if tee else None,
        "reproduction_note": ("같은 커밋·설정이어도 데이터 스냅샷(출처·날짜)이 다르면 다른 실험이다 — "
                              "data_sources 와 started_at 을 함께 볼 것"),
    }


def write(path=None):
    rec = build()
    if path is None:
        os.makedirs(RUNS_DIR, exist_ok=True)
        stamp = _dt.datetime.now().strftime("%Y%m%d_%H%M%S")
        path = os.path.join(RUNS_DIR, f"{stamp}_{(rec['tool'] or 'run').replace('.py', '')}.json")
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(rec, f, ensure_ascii=False, indent=1, default=str)
    os.replace(tmp, path)
    _STATE["path"] = path
    return path


def start(capture_stdout=True):
    """스크립트 실행 한 번의 기록을 켠다(종료 시 자동 저장). 두 번 불러도 한 번만 켠다."""
    if _STATE["active"]:
        return
    _STATE["active"] = True
    _STATE["started_at"] = _dt.datetime.now().isoformat(timespec="seconds")
    if capture_stdout and not isinstance(sys.stdout, _Tee):
        _STATE["tee"] = _Tee(sys.stdout)
        sys.stdout = _STATE["tee"]
    prev_hook = sys.excepthook

    def _hook(tp, val, tb):
        _STATE["error"] = f"{tp.__name__}: {val}"
        prev_hook(tp, val, tb)
    sys.excepthook = _hook

    def _finish():
        try:
            p = write()
            sys.__stdout__.write(f"\n[실행 기록] {os.path.relpath(p, ROOT)}\n")
        except Exception as e:  # noqa: BLE001  (기록 실패가 감사 결과를 가리면 안 된다)
            sys.__stderr__.write(f"[실행 기록] 저장 실패: {e}\n")
    atexit.register(_finish)


def auto_start_for_script(main_file):
    """감사 도구가 '스크립트로' 실행될 때만 켠다. import(테스트·다른 도구)에서는 켜지 않는다.

    HTS_NO_RUN_MANIFEST=1 이면 끈다.
    """
    if os.environ.get("HTS_NO_RUN_MANIFEST") == "1" or "pytest" in sys.modules:
        return False
    try:
        main = os.path.abspath(main_file or "")
    except Exception:           # noqa: BLE001
        return False
    if os.path.dirname(main) != os.path.join(ROOT, "tools"):
        return False
    start()
    return True
