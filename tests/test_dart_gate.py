"""DART 호출 관문 — 속도 제한·차단기 (2026-10-07).

[왜] 4스레드 무간격 일괄 조회가 opendart.fss.or.kr 의 IP 차단을 불렀고, 막힌 동안 같은 공유기 뒤의
 DART 기능(6-5~6-8·텔레그램 공시 알림)이 함께 죽었다. 모든 DART HTTP 는 _dart_get 하나를 지나며
 (1) 프로세스 전체에서 간격을 지키고 (2) 연결이 연달아 끊기거나 한도(020)를 넘으면 더 보내지 않는다.
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


def test_calls_are_spaced_across_threads(monkeypatch, key):
    monkeypatch.setattr(dart_api, "DART_MIN_INTERVAL_SEC", 0.05)
    sent = []
    monkeypatch.setattr(dart_api.requests, "get",
                        lambda *a, **k: sent.append(time.monotonic()) or _ok())
    ts = [threading.Thread(target=dart_api.call_dart, args=("company.json", {"corp_code": "1"}))
          for _ in range(4)]
    for t in ts:
        t.start()
    for t in ts:
        t.join()
    sent.sort()
    assert len(sent) == 4
    assert all(b - a >= 0.045 for a, b in zip(sent, sent[1:])), sent   # 스레드가 늘어도 합산 간격 유지


def test_repeated_connection_errors_open_breaker(monkeypatch, key):
    calls = []

    def boom(*a, **k):
        calls.append(1)
        raise requests.exceptions.ConnectionError("Connection reset by peer")

    monkeypatch.setattr(dart_api.requests, "get", boom)
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

    monkeypatch.setattr(dart_api.requests, "get", flaky)
    for _ in range(5):
        try:
            dart_api.call_dart("company.json", {"corp_code": "1"})
        except dart_api.DartQueryError:
            pass
    assert dart_api.dart_blocked_reason() is None     # 연달아 3번이 아니면 열지 않는다


def test_quota_exceeded_blocks_until_midnight(monkeypatch, key):
    monkeypatch.setattr(dart_api.requests, "get",
                        lambda *a, **k: _ok({"status": "020", "message": "요청 제한을 초과하였습니다."}))
    with pytest.raises(dart_api.DartQueryError):
        dart_api.call_dart("company.json", {"corp_code": "1"})
    assert "020" in (dart_api.dart_blocked_reason() or "")
    assert dart_api._dart_blocked_until == pytest.approx(dart_api._next_midnight_ts())
    with pytest.raises(dart_api.DartBlockedError):
        dart_api.call_dart("list.json", {"corp_code": "1"})


def test_breaker_expires(monkeypatch, key):
    monkeypatch.setattr(dart_api, "_dart_blocked_until", time.time() - 1)
    monkeypatch.setattr(dart_api.requests, "get", lambda *a, **k: _ok())
    assert dart_api.call_dart("company.json", {"corp_code": "1"})["induty_code"] == "264"


def test_document_and_corpcode_go_through_gate(monkeypatch, key):
    """공시 원문·기업코드 ZIP 도 같은 관문을 지난다 — 차단기가 열려 있으면 보내지 않는다."""
    monkeypatch.setattr(dart_api, "_dart_blocked_until", time.time() + 600)
    sent = []
    monkeypatch.setattr(dart_api.requests, "get", lambda *a, **k: sent.append(a) or _ok())
    assert dart_api.get_dart_document_text("20261007000001") is None
    monkeypatch.setattr(dart_api, "_dart_corp_map_cache", None)
    monkeypatch.setattr(config, "JSON_DIR", "/nonexistent-dir-for-test", raising=False)
    with pytest.raises(dart_api.DartQueryError):
        dart_api.get_dart_corp_map(force_refresh=True)
    assert sent == []


def test_queued_calls_do_not_send_after_breaker_opens(monkeypatch, key):
    """여러 스레드가 줄을 선 사이 차단기가 열리면, 아직 안 나간 호출은 보내지 않는다."""
    monkeypatch.setattr(dart_api, "DART_MIN_INTERVAL_SEC", 0.05)
    sent = []

    def boom(*a, **k):
        sent.append(1)
        raise requests.exceptions.ConnectionError("reset")

    monkeypatch.setattr(dart_api.requests, "get", boom)
    ts = [threading.Thread(target=lambda: pytest.raises(dart_api.DartQueryError, dart_api.call_dart,
                                                        "company.json", {"corp_code": "1"}))
          for _ in range(8)]
    for t in ts:
        t.start()
    for t in ts:
        t.join()
    assert len(sent) == dart_api.DART_BLOCK_AFTER_CONN_ERRORS
