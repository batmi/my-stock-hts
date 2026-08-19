"""지수 화면이 느린 지수 하나 때문에 통째로 멈추지 않는가.

[왜 이 테스트인가] 2026-08-19 토스 모드에서 '전체 지수'가 96%에서 정지한다는 신고가 있었다.
원인은 수집 단계가 `as_completed`를 **타임아웃 없이** 기다린 것이다. 토스 모드에서는
코스피200·코스닥150·미국채 4개 테너·HY OAS까지 7개가 tvDatafeed를 쓰고 그 호출은 전역
락으로 직렬화되므로, 소스가 흔들리면 마지막 몇 개가 줄을 서고 화면은 무한정 기다렸다.

지금은 시한을 넘긴 지수를 '수신 실패(N초 내 미응답)'로 표시하고 나머지 표를 그린다.
못 받은 것을 숨기지 않으면서 화면은 살아 있어야 한다 — 지수 fail-closed와 같은 원칙이다.
"""
import threading
import time

import pytest

import config
from modules import market


@pytest.fixture
def fast_deadline(monkeypatch):
    monkeypatch.setattr(config, "INDEX_FETCH_DEADLINE_SEC", 1, raising=False)


def _run_with_workers(monkeypatch, worker):
    """지수 두 개짜리 화면을 그린다. 반환은 실패로 표시된 지수 목록."""
    monkeypatch.setattr(market, "_process_index_worker", worker)
    return market._show_market_indices_core(target_indices=["코스피", "코스닥"])


def _ok(name, ticker, *_a, **_k):
    return {'status': 'failed', 'name': name, 'src': 'yfinance'}


def test_느린_지수_하나가_화면_전체를_잡아두지_않는다(fast_deadline, monkeypatch):
    started = threading.Event()

    def worker(name, ticker, *_a, **_k):
        if name == "코스닥":
            started.set()
            # 시한(1초)보다 오래. 인터프리터 종료 시 스레드풀이 join하므로(파이썬 3.9+)
            #  테스트 세션 전체를 늘리지 않을 만큼만 잔다.
            time.sleep(6)
        return {'status': 'failed', 'name': name, 'src': 'yfinance'}

    t0 = time.time()
    failed = _run_with_workers(monkeypatch, worker)
    elapsed = time.time() - t0

    assert started.is_set(), "느린 워커가 시작조차 안 했다 — 표본이 무효다"
    assert elapsed < 5, f"시한을 넘긴 워커를 계속 기다렸다 ({elapsed:.1f}s)"
    assert "코스닥" in failed, "미응답 지수가 실패 목록에 없다 — 조용히 사라지면 안 된다"
    assert "코스피" in failed, "정상 경로의 실패 표시가 사라졌다"


def test_시한_안에_끝나면_종전과_같다(monkeypatch):
    monkeypatch.setattr(config, "INDEX_FETCH_DEADLINE_SEC", 30, raising=False)
    failed = _run_with_workers(monkeypatch, _ok)
    assert sorted(failed) == ["코스닥", "코스피"]


def test_시한을_0으로_두면_기다린다(monkeypatch):
    """종전 동작(무한 대기)으로 되돌릴 수 있어야 한다 — 운영자가 끌 수 있는 손잡이."""
    monkeypatch.setattr(config, "INDEX_FETCH_DEADLINE_SEC", 0, raising=False)

    def worker(name, ticker, *_a, **_k):
        time.sleep(0.3)
        return {'status': 'failed', 'name': name, 'src': 'yfinance'}

    failed = _run_with_workers(monkeypatch, worker)
    assert sorted(failed) == ["코스닥", "코스피"]
