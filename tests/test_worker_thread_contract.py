"""워커 스레드가 지켜야 할 두 계약 — 계좌 컨텍스트 전파와 실패의 가시성.

[배경] 이 저장소가 반복해서 밟은 결함은 '선언은 있는데 한 곳만 지킨다'이다.
계좌 컨텍스트 전파(core.utils.inherit_account_context)는 2026-08 매도 워커에서만
적용됐고, 같은 조건인 매수 후보 워커(at_cand)·그 안의 I/O 풀(cand_io)·보유분석
풀(at_engine)은 빠져 있었다. 빠진 쪽은 시세 조회가 **수동 계좌 앱키**로 나간다
(core.utils.get_common_headers).

실패의 가시성도 같은 모양이었다. 매도 워커는 _sell_worker_guarded 로 감싸 예외를
로그·경보로 남기는데, 매수 후보 워커는 `except Exception: return None` 이라 종목이
흔적 없이 사라졌다 — 로그도, 신호 원장 행도 남지 않아 게이트 감사의 분모가 조용히
줄어든다.
"""
import threading

import pytest

from modules.auto_trade import trader as trader_mod
from modules.auto_trade import engine as engine_mod


# --------------------------------------------------------- 실패의 가시성
class _Recorder:
    """_analyze_candidate_worker 가 요구하는 최소한의 AutoTrader."""

    def __init__(self):
        self.logs = []
        self.is_running = True

    def log(self, msg, *a, **k):
        self.logs.append(str(msg))


def test_buy_candidate_worker_logs_instead_of_vanishing(monkeypatch):
    """후보 하나가 던져도 조용히 사라지지 않는다(매도측과 같은 형태로 남긴다)."""
    rec = _Recorder()
    # 'name' 키가 없어 워커 본문 첫 줄에서 KeyError 가 난다.
    item = {'code': '005930'}

    got = trader_mod.AutoTrader._analyze_candidate_worker(
        rec, item, set(), {}, set(), {}, 0, {}, {}, {})

    assert got is None, "실패한 후보는 후보 목록에 들어가면 안 된다"
    assert rec.logs, "예외가 조용히 삼켜졌다 — 로그가 한 줄도 남지 않았다"
    joined = "\n".join(rec.logs)
    assert "분석실패" in joined and "005930" in joined, joined
    assert "KeyError" in joined, f"예외 종류를 남겨야 원인을 좁힌다: {joined}"


# --------------------------------------------------- 계좌 컨텍스트 전파
#
# [왜 AST 전수 검사인가 · 2026-09-05]
#  종전 이 절은 "이 문자열이 소스에 있는가"를 풀마다 손으로 나열했다. 그래서 **목록에
#  없는 풀은 아무도 안 봤다** — 실제로 기동 초기화 풀(at_init: 잔고·예수금·미체결 복원)과
#  상태 조회 풀(at_status: 잔고·예수금)이 빠진 채로 남아 있었다. 나열은 시간이 지나면
#  낡는다. 규칙 자체를 검사한다:
#
#      AccountContext 블록 **안에서** 스레드를 띄우면 그 대상은 반드시 감싸져 있어야 한다.
#
#  use_auto_account 는 threading.local 이라 상속되지 않는다. 풀리면 그 요청은 수동
#  앱키·수동 토큰으로 나가고(core.utils.get_common_headers · api.auth.get_current_token)
#  TPS 도 수동 버킷에서 깎인다(api.http._real_bucket_key).
import ast
import os

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

#  감싸진 것으로 인정하는 표현. 제출 스레드에서 미리 만든 별칭도 포함한다.
_WRAPPED_HINTS = ("inherit_account_context", "_task(", "_io(")

#  일부러 감싸지 않는 자리. 사유 없이 넣지 말 것.
_ALLOWED = {
    # (파일, 호출 대상)
}


def _spawns_inside_account_context(path):
    """AccountContext 블록 안에서 띄우는 (줄번호, 종류, 대상표현) 목록."""
    hits = []

    class _V(ast.NodeVisitor):
        def __init__(self):
            self.depth = 0

        def visit_With(self, node):
            inside = any(
                isinstance(i.context_expr, ast.Call)
                and getattr(getattr(i.context_expr, "func", None), "attr", "") == "AccountContext"
                for i in node.items)
            if inside:
                self.depth += 1
            self.generic_visit(node)
            if inside:
                self.depth -= 1

        def visit_Call(self, node):
            if self.depth > 0:
                f = node.func
                if isinstance(f, ast.Attribute) and f.attr == "submit" and node.args:
                    hits.append((node.lineno, "submit", ast.unparse(node.args[0])))
                elif isinstance(f, (ast.Name, ast.Attribute)) and ast.unparse(f).endswith("Thread"):
                    tgt = next((k.value for k in node.keywords if k.arg == "target"), None)
                    if tgt is not None:
                        hits.append((node.lineno, "Thread", ast.unparse(tgt)))
            self.generic_visit(node)

    _V().visit(ast.parse(open(path, encoding="utf-8").read()))
    return hits


def _source_files():
    for root, dirs, files in os.walk(ROOT):
        dirs[:] = [d for d in dirs
                   if d not in (".git", "__pycache__", ".venv", "data", "logs", "db",
                                "tests", "tools", "chart", "json", "backups")]
        for fn in files:
            if fn.endswith(".py"):
                yield os.path.join(root, fn)


def test_계좌_컨텍스트_안에서_띄우는_스레드는_전부_감싼다():
    """[핵심] 나열이 아니라 규칙으로 지킨다 — 새 풀이 생겨도 자동으로 걸린다."""
    bad = []
    for path in _source_files():
        rel = os.path.relpath(path, ROOT)
        for lineno, kind, target in _spawns_inside_account_context(path):
            if (rel, target) in _ALLOWED:
                continue
            if not any(h in target for h in _WRAPPED_HINTS):
                bad.append(f"{rel}:{lineno} {kind} → {target}")
    assert not bad, (
        "AccountContext 안에서 감싸지 않은 채 스레드를 띄운다 — 워커는 수동 앱키로 나간다.\n"
        "  utils.inherit_account_context(fn) 로 **제출 스레드에서** 감싸세요:\n  "
        + "\n  ".join(bad))


def test_검사기가_실제로_무언가를_보고_있다():
    """0건을 훑고 초록인 상태를 막는다."""
    found = [(os.path.relpath(p, ROOT), h)
             for p in _source_files() for h in _spawns_inside_account_context(p)]
    assert len(found) >= 5, f"AccountContext 안의 스레드 생성을 {len(found)}건밖에 못 찾았다"
    assert any("trader.py" in rel for rel, _ in found)


def test_먼저_고쳐진_풀들이_되돌아가지_않았다():
    """AccountContext 밖에서 만들어지는 풀은 위 규칙에 안 걸리므로 따로 못박는다.

    매도(at_sell)·후보(at_cand)·그 안의 I/O 풀(cand_io)·보유분석(at_engine)은
    2026-08~09 에 각각 고쳐졌다. 이들은 컨텍스트를 함수 인자·상위 프레임에서 받으므로
    구문만으로는 위 검사에 잡히지 않는다.
    """
    trader_src = open(trader_mod.__file__.replace(".pyc", ".py"), encoding="utf-8").read()
    engine_src = open(engine_mod.__file__.replace(".pyc", ".py"), encoding="utf-8").read()
    for needle in ("_sell_task = utils.inherit_account_context(_sell_worker_guarded)",
                   "_cand_task = utils.inherit_account_context(self._analyze_candidate_worker)",
                   "ex.submit(_io(api.get_chart_data)"):
        assert needle in trader_src, f"되돌아갔다: {needle}"
    assert "_task = utils.inherit_account_context(_worker)" in engine_src
    assert "executor.submit(self._analyze_candidate_worker" not in trader_src
    assert "executor.map(_worker, entries)" not in engine_src


def test_래퍼가_실제로_값을_옮긴다():
    """구조 검사만으로는 래퍼가 고장 난 것을 못 잡는다 — 동작도 한 번 건다."""
    import concurrent.futures

    from core import context, utils

    seen = {}

    def _peek():
        seen["flag"] = getattr(context.trade_context, "use_auto_account", False)

    prev = getattr(context.trade_context, "use_auto_account", False)
    try:
        context.trade_context.use_auto_account = True
        task = utils.inherit_account_context(_peek)
        with concurrent.futures.ThreadPoolExecutor(max_workers=1) as ex:
            ex.submit(task).result()
        assert seen["flag"] is True, "워커에 계좌 컨텍스트가 전달되지 않았다"

        seen.clear()
        with concurrent.futures.ThreadPoolExecutor(max_workers=1) as ex:
            ex.submit(_peek).result()
        assert seen["flag"] is False, (
            "감싸지 않은 워커가 컨텍스트를 물려받았다 — 이 테스트의 전제가 무너졌다")
    finally:
        context.trade_context.use_auto_account = prev
