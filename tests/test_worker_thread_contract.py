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
def _submits_are_wrapped(path, needles):
    src = open(path, encoding='utf-8').read()
    code = "\n".join(l for l in src.split("\n") if not l.strip().startswith("#"))
    return code, [n for n in needles if n not in code]


def test_candidate_pool_submits_through_the_context_wrapper():
    """at_cand 풀과 그 안의 cand_io 풀 모두 래퍼를 지난다."""
    code, missing = _submits_are_wrapped(
        trader_mod.__file__.replace('.pyc', '.py'),
        ["_cand_task = utils.inherit_account_context(self._analyze_candidate_worker)",
         "executor.submit(_cand_task, item,",
         "ex.submit(_io(api.get_chart_data)",
         "ex.submit(_io(api.get_realtime_vol_strength)"])
    assert not missing, f"계좌 컨텍스트 전파가 풀린 제출 지점이 있다: {missing}"
    assert "executor.submit(self._analyze_candidate_worker" not in code, (
        "감싸지 않은 원본 제출이 되살아났다")


def test_holding_analysis_pool_submits_through_the_context_wrapper():
    """analyze_holdings(at_engine)도 마찬가지 — 화면과 시스템의 판정이 갈리면 안 된다."""
    code, missing = _submits_are_wrapped(
        engine_mod.__file__.replace('.pyc', '.py'),
        ["_task = utils.inherit_account_context(_worker)",
         "executor.map(_task, entries)"])
    assert not missing, f"계좌 컨텍스트 전파가 풀린 제출 지점이 있다: {missing}"
    assert "executor.map(_worker, entries)" not in code, (
        "감싸지 않은 원본 제출이 되살아났다")


def test_sell_pool_still_wrapped():
    """먼저 고쳐진 매도 워커가 되돌아가지 않았는지 함께 지킨다."""
    code, missing = _submits_are_wrapped(
        trader_mod.__file__.replace('.pyc', '.py'),
        ["_sell_task = utils.inherit_account_context(_sell_worker_guarded)"])
    assert not missing, missing
