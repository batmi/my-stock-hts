"""DB 큐의 시한 초과는 '실패'가 아니라 '모른다'다.

[왜] DBProxy 는 결과를 OP_TIMEOUT_SEC 초 기다리다 포기하는데, **작업을 취소하지는
 않는다** — 큐에 그대로 남아 곧 실행된다. 그런데 종전 예외는
 `Exception("DB Method 'insert_trade' Timeout")` 이었다. 읽는 사람도 잡는 코드도
 그것을 '기록되지 않았다'로 읽는다.
 실측(시한 0.2초 · 작업 1초): 호출부는 예외를 받았고, 1.5초 뒤 원장에는 그 행이 들어가
 있었다. 보상 조치(수동 재입력·재삽입)는 **중복 원장**을 만들고, 원장의 중복은 손익·
 평단·트레일링 기준을 통째로 흔든다.

 이 시스템은 주문 응답 유실에 대해 같은 규칙을 이미 세워 뒀다 — 재전송하지 말고 대사하라
 ([[order-timeout-no-resend]], api/http.py 의 '결과 불명'). 한 층 아래에서 그 규칙이
 깨져 있었다.
"""
import logging
import time

import pytest

from modules import db_queue


class SlowDB:
    """한 건에 1초 걸리는 가짜 DB — 시한(0.2초)을 반드시 넘긴다."""

    def __init__(self):
        self.written = []

    def insert_trade(self, row):
        time.sleep(1.0)
        self.written.append(row)
        return "ROWID-1"

    def quick(self):
        return "ok"


@pytest.fixture
def proxy(monkeypatch):
    monkeypatch.setattr(db_queue, 'OP_TIMEOUT_SEC', 0.2)
    real = SlowDB()
    p = db_queue.DBProxy(real)
    yield p, real
    p.stop(timeout=3)


def test_시한_초과는_결과_불명_예외다(proxy):
    p, _real = proxy
    with pytest.raises(db_queue.DBOperationUnknown) as ei:
        p.insert_trade({"code": "005930"})
    msg = str(ei.value)
    assert "결과 불명" in msg
    assert "중복" in msg, "다시 넣지 말라는 말이 없으면 호출부가 재삽입한다"


def test_시한을_넘긴_작업은_취소되지_않고_반영된다(proxy):
    """'실패'로 답하면 안 되는 이유 그 자체 — 작업은 살아 있다."""
    p, real = proxy
    with pytest.raises(db_queue.DBOperationUnknown):
        p.insert_trade({"code": "005930"})
    assert real.written == []
    time.sleep(1.5)
    assert real.written == [{"code": "005930"}], "취소된 줄 알았는데 반영되지 않았다"


def test_지각_완료는_로그로_남는다(proxy, caplog):
    """호출부는 이미 돌아갔다 — 반영됐다는 사실이 어디엔가는 남아야 한다."""
    p, real = proxy
    with caplog.at_level(logging.WARNING, logger="modules.db_queue"):
        with pytest.raises(db_queue.DBOperationUnknown):
            p.insert_trade({"code": "005930"})
        time.sleep(1.5)
    late = [r.message for r in caplog.records if "시한" in r.message and "완료" in r.message]
    assert late, f"지각 완료 경고가 없다: {[r.message for r in caplog.records]}"
    assert "반영됐습니다" in late[0]


def test_execute_custom_도_같은_예외를_쓴다(proxy):
    p, _real = proxy
    with pytest.raises(db_queue.DBOperationUnknown):
        p.execute_custom(lambda: time.sleep(1.0))


def test_시한_안에_끝나면_평소대로_돌아간다(proxy):
    p, _real = proxy
    assert p.quick() == "ok"
