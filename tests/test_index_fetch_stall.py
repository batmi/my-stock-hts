"""지수 화면이 느린 지수 하나 때문에 통째로 멈추지 않는가.

[왜 이 테스트인가] 2026-08-19 토스 모드에서 '전체 지수'가 96%에서 정지한다는 신고가 있었다.
원인은 수집 단계가 `as_completed`를 **타임아웃 없이** 기다린 것이다. 토스 모드에서는
코스피200·코스닥150·미국채 4개 테너·HY OAS까지 7개가 tvDatafeed를 쓰고 그 호출은 전역
락으로 직렬화되므로, 소스가 흔들리면 마지막 몇 개가 줄을 서고 화면은 무한정 기다렸다.

[한 번 잘못 고쳤다 — 그 함정을 여기서 고정한다] 처음에는 전체 수집에 시한을 걸었다.
그러자 느리지만 **진행 중인** 실행에서도 꼬리가 늘 잘려, 줄의 맨 뒤인 코스피200·코스닥150이
매번 '수신 실패'로 찍혔다(사용자 신고). 재야 하는 것은 총 소요가 아니라 **'한 건도 끝나지
않은 채 흐른 시간'** 이다. 진행이 있는 한 기다리고, 정말 멈췄을 때만 남은 것을 실패로 밝힌다.
"""
import threading
import time

import pytest

import config
from modules import market


@pytest.fixture
def fast_stall(monkeypatch):
    monkeypatch.setattr(config, "INDEX_FETCH_STALL_SEC", 1, raising=False)


def _run_with_workers(monkeypatch, worker):
    """지수 두 개짜리 화면을 그린다. 반환은 실패로 표시된 지수 목록."""
    monkeypatch.setattr(market, "_process_index_worker", worker)
    return market._show_market_indices_core(target_indices=["코스피", "코스닥"])


def _ok(name, ticker, *_a, **_k):
    return {'status': 'failed', 'name': name, 'src': 'yfinance'}


def test_멈춘_지수_하나가_화면_전체를_잡아두지_않는다(fast_stall, monkeypatch):
    started = threading.Event()

    def worker(name, ticker, *_a, **_k):
        if name == "코스닥":
            started.set()
            # 무진행 허용치(1초)보다 오래 — 이 워커만 멈춘 상황이다. 인터프리터 종료 시
            #  스레드풀이 join하므로(파이썬 3.9+) 세션을 늘리지 않을 만큼만 잔다.
            time.sleep(6)
        return {'status': 'failed', 'name': name, 'src': 'yfinance'}

    t0 = time.time()
    failed = _run_with_workers(monkeypatch, worker)
    elapsed = time.time() - t0

    assert started.is_set(), "느린 워커가 시작조차 안 했다 — 표본이 무효다"
    assert elapsed < 5, f"시한을 넘긴 워커를 계속 기다렸다 ({elapsed:.1f}s)"
    assert "코스닥" in failed, "미응답 지수가 실패 목록에 없다 — 조용히 사라지면 안 된다"
    assert "코스피" in failed, "정상 경로의 실패 표시가 사라졌다"


def test_진행이_있으면_느려도_기다린다(monkeypatch):
    """[핵심] 총 소요가 허용치를 넘어도, 한 건씩 끝나고 있으면 잘라내지 않는다.

    전체 시한 방식이었다면 여기서 뒤쪽 지수가 실패로 찍힌다 — 실제로 그렇게 깨졌다.
    """
    monkeypatch.setattr(config, "INDEX_FETCH_STALL_SEC", 1, raising=False)
    order = []

    def worker(name, ticker, *_a, **_k):
        # 하나당 0.7초 — 무진행 허용치(1초)보다 짧지만 둘을 합치면 넘긴다.
        time.sleep(0.7)
        order.append(name)
        return {'status': 'failed', 'name': name, 'src': 'yfinance'}

    t0 = time.time()
    failed = _run_with_workers(monkeypatch, worker)
    assert time.time() - t0 > 0.7, "워커가 실제로 시간을 쓰지 않았다 — 표본이 무효다"
    assert len(order) == 2, f"진행 중인 워커가 잘렸다 (완료 {order})"
    assert sorted(failed) == ["코스닥", "코스피"]


def test_허용치_안에_끝나면_종전과_같다(monkeypatch):
    monkeypatch.setattr(config, "INDEX_FETCH_STALL_SEC", 30, raising=False)
    failed = _run_with_workers(monkeypatch, _ok)
    assert sorted(failed) == ["코스닥", "코스피"]


def test_0으로_두면_무한정_기다린다(monkeypatch):
    """종전 동작(무한 대기)으로 되돌릴 수 있어야 한다 — 운영자가 끌 수 있는 손잡이."""
    monkeypatch.setattr(config, "INDEX_FETCH_STALL_SEC", 0, raising=False)

    def worker(name, ticker, *_a, **_k):
        time.sleep(0.3)
        return {'status': 'failed', 'name': name, 'src': 'yfinance'}

    failed = _run_with_workers(monkeypatch, worker)
    assert sorted(failed) == ["코스닥", "코스피"]
