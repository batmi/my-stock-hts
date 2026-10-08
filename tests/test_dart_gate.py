"""DART 호출 관문 — 속도 제한·차단기 (2026-10-07).

[왜] 4스레드 무간격 일괄 조회가 opendart.fss.or.kr 의 IP 차단을 불렀고, 막힌 동안 같은 공유기 뒤의
 DART 기능(6-5~6-8·텔레그램 공시 알림)이 함께 죽었다. 모든 DART HTTP 는 _dart_get 하나를 지나며
 (1) 프로세스 전체 토큰 버킷(몰림 허용·지속량 제한)을 지키고 (2) 연결이 연달아 끊기거나 한도(020)를 넘으면 더 보내지 않는다.
"""
import threading
import time
from unittest.mock import MagicMock

import pytest
import requests

import config
from modules import dart_api


@pytest.fixture
def key(monkeypatch):
    monkeypatch.setattr(config, "DART_API_KEY", "dummy", raising=False)


def _ok(payload=None):
    res = MagicMock()
    res.json.return_value = payload or {"status": "000", "induty_code": "264"}
    return res


def _burst_threads(n):
    ts = [threading.Thread(target=dart_api.call_dart, args=("company.json", {"corp_code": "1"}))
          for _ in range(n)]
    for t in ts:
        t.start()
    for t in ts:
        t.join()


def test_burst_goes_out_without_waiting(monkeypatch, key):
    """메뉴 6처럼 몰아 부르는 호출은 버킷 안에서는 기다리지 않는다(2026-10-08 체감 지연 수정)."""
    monkeypatch.setattr(dart_api, "DART_BURST", 20)
    monkeypatch.setattr(dart_api, "DART_REFILL_PER_SEC", 1.0)
    sent = []
    monkeypatch.setattr(dart_api, "_send",
                        lambda *a, **k: sent.append(time.monotonic()) or _ok())
    t0 = time.monotonic()
    _burst_threads(8)
    assert len(sent) == 8 and time.monotonic() - t0 < 0.5


def test_sustained_rate_is_capped_across_threads(monkeypatch, key):
    """버킷이 비면 스레드가 몇 개든 합산 초당 DART_REFILL_PER_SEC 건으로 묶인다."""
    monkeypatch.setattr(dart_api, "DART_BURST", 2)
    monkeypatch.setattr(dart_api, "DART_REFILL_PER_SEC", 20.0)
    sent = []
    monkeypatch.setattr(dart_api, "_send",
                        lambda *a, **k: sent.append(time.monotonic()) or _ok())
    _burst_threads(8)
    sent.sort()
    assert len(sent) == 8
    # 첫 2건은 버킷에서 바로, 나머지 6건은 0.05초 간격 → 마지막은 시작 뒤 약 0.3초
    assert sent[-1] - sent[0] >= 0.25, sent


def test_repeated_connection_errors_open_breaker(monkeypatch, key):
    calls = []

    def boom(*a, **k):
        calls.append(1)
        raise requests.exceptions.ConnectionError("Connection reset by peer")

    monkeypatch.setattr(dart_api, "_send", boom)
    for _ in range(dart_api.DART_BLOCK_AFTER_CONN_ERRORS):
        with pytest.raises(dart_api.DartQueryError):
            dart_api.call_dart("company.json", {"corp_code": "1"})
    assert dart_api.dart_blocked_reason() is not None
    with pytest.raises(dart_api.DartBlockedError):
        dart_api.call_dart("company.json", {"corp_code": "1"})
    assert len(calls) == dart_api.DART_BLOCK_AFTER_CONN_ERRORS     # 막힌 뒤엔 보내지 않는다


def test_success_resets_connection_error_count(monkeypatch, key):
    seq = iter([requests.exceptions.ConnectionError("x"), requests.exceptions.ConnectionError("x"),
                None, requests.exceptions.ConnectionError("x"), requests.exceptions.ConnectionError("x")])

    def flaky(*a, **k):
        e = next(seq)
        if e:
            raise e
        return _ok()

    monkeypatch.setattr(dart_api, "_send", flaky)
    for _ in range(5):
        try:
            dart_api.call_dart("company.json", {"corp_code": "1"})
        except dart_api.DartQueryError:
            pass
    assert dart_api.dart_blocked_reason() is None     # 연달아 3번이 아니면 열지 않는다


def test_quota_exceeded_blocks_until_midnight(monkeypatch, key):
    monkeypatch.setattr(dart_api, "_send",
                        lambda *a, **k: _ok({"status": "020", "message": "요청 제한을 초과하였습니다."}))
    with pytest.raises(dart_api.DartQueryError):
        dart_api.call_dart("company.json", {"corp_code": "1"})
    assert "020" in (dart_api.dart_blocked_reason() or "")
    assert dart_api._dart_blocked_until == pytest.approx(dart_api._next_midnight_ts())
    with pytest.raises(dart_api.DartBlockedError):
        dart_api.call_dart("list.json", {"corp_code": "1"})


def test_breaker_expires(monkeypatch, key):
    monkeypatch.setattr(dart_api, "_dart_blocked_until", time.time() - 1)
    monkeypatch.setattr(dart_api, "_send", lambda *a, **k: _ok())
    assert dart_api.call_dart("company.json", {"corp_code": "1"})["induty_code"] == "264"


def test_document_and_corpcode_go_through_gate(monkeypatch, key):
    """공시 원문·기업코드 ZIP 도 같은 관문을 지난다 — 차단기가 열려 있으면 보내지 않는다."""
    monkeypatch.setattr(dart_api, "_dart_blocked_until", time.time() + 600)
    sent = []
    monkeypatch.setattr(dart_api, "_send", lambda *a, **k: sent.append(a) or _ok())
    assert dart_api.get_dart_document_text("20261007000001") is None
    monkeypatch.setattr(dart_api, "_dart_corp_map_cache", None)
    monkeypatch.setattr(config, "JSON_DIR", "/nonexistent-dir-for-test", raising=False)
    with pytest.raises(dart_api.DartQueryError):
        dart_api.get_dart_corp_map(force_refresh=True)
    assert sent == []


def test_queued_calls_do_not_send_after_breaker_opens(monkeypatch, key):
    """여러 스레드가 줄을 선 사이 차단기가 열리면, 아직 안 나간 호출은 보내지 않는다."""
    monkeypatch.setattr(dart_api, "DART_BURST", 0)
    monkeypatch.setattr(dart_api, "DART_REFILL_PER_SEC", 20.0)
    sent = []

    def boom(*a, **k):
        sent.append(1)
        raise requests.exceptions.ConnectionError("reset")

    monkeypatch.setattr(dart_api, "_send", boom)
    ts = [threading.Thread(target=lambda: pytest.raises(dart_api.DartQueryError, dart_api.call_dart,
                                                        "company.json", {"corp_code": "1"}))
          for _ in range(8)]
    for t in ts:
        t.start()
    for t in ts:
        t.join()
    assert len(sent) == dart_api.DART_BLOCK_AFTER_CONN_ERRORS


def test_read_timeout_is_retried_once(monkeypatch, key):
    """서버 꼬리 지연(2026-10-08 실측 최대 39초)은 한 번만 다시 보낸다 — 차단 신호로 세지 않는다."""
    seq = iter([requests.exceptions.ReadTimeout("slow"), None])
    sent = []

    def slow_then_ok(url, params=None, timeout=None):
        sent.append(timeout)
        e = next(seq)
        if e:
            raise e
        return _ok()

    monkeypatch.setattr(dart_api, "_send", slow_then_ok)
    assert dart_api.call_dart("company.json", {"corp_code": "1"})["induty_code"] == "264"
    assert len(sent) == 2
    assert sent[0] == (dart_api.DART_CONNECT_TIMEOUT_SEC, dart_api.DART_READ_TIMEOUT_SEC)
    assert dart_api._dart_conn_errors == 0


def test_second_read_timeout_fails_without_breaker(monkeypatch, key):
    sent = []

    def always_slow(*a, **k):
        sent.append(1)
        raise requests.exceptions.ReadTimeout("slow")

    monkeypatch.setattr(dart_api, "_send", always_slow)
    for _ in range(3):
        with pytest.raises(dart_api.DartQueryError):
            dart_api.call_dart("company.json", {"corp_code": "1"})
    assert len(sent) == 6                       # 호출마다 정확히 한 번만 재시도
    assert dart_api.dart_blocked_reason() is None


def test_connections_are_reused(monkeypatch):
    """연결 풀은 프로세스에 하나 — 매번 새 TLS 를 맺으면 중앙 응답이 3배 늘었다(0.06→0.18초)."""
    monkeypatch.setattr(dart_api, "_dart_session", None)
    made = []

    class FakeSession:
        def __init__(self):
            made.append(self)

        def mount(self, *a, **k):
            pass

        def get(self, url, params=None, timeout=None):
            return _ok()

    monkeypatch.setattr(dart_api.requests, "Session", FakeSession)
    for _ in range(3):
        dart_api._send("https://opendart.fss.or.kr/api/x", params={}, timeout=(1, 1))
    assert len(made) == 1
