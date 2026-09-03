"""테스트가 만든 기록이 실제 매매일지 웹서버로 나가지 못하게 하는 방어선.

[2026-09-03 사고] 부분체결 회계 테스트가 만든 가짜 삼성전자 매도 2건이 사람이 보는
웹 매매일지에 실거래(isSimulated=false)로 기록됐다. 경로는 이랬다:

  테스트가 db_manager.db(운영 DB 싱글턴)로 insert_trade
    → 같은 트랜잭션에서 journal_outbox 에 적재
      → 같은 맥북에서 돌던 **실전(mode 2) 인스턴스의 워커**가 몇 초 뒤 집어서 전송

테스트 프로세스 안에 워커가 없어도 새어 나간다는 것이 이 사고의 핵심이다. 그래서
방어를 세 겹으로 두고, 각 겹을 여기서 따로 잰다. 한 겹이 무너져도 나머지가 잡는다.
"""
import os

import pytest

import config
from modules import db_manager, journal_sync
from tests.conftest import PRODUCTION_DB_PATH


# ── 1겹: 테스트는 운영 DB 를 만지지 못한다 ────────────────────────────────
def test_db_singleton_never_points_at_production():
    """운영 DB 에 한 행이라도 들어가면 성과 지표·자산 이력·매매일지가 함께 틀어진다."""
    assert os.path.abspath(db_manager.db.db_path) != PRODUCTION_DB_PATH


def test_inserted_trade_lands_in_temp_db_only():
    """가장 흔한 오염 경로(insert_trade)를 실제로 밟아 본다."""
    db_manager.db.insert_trade("매수", "000000", "격리검증", 1, 1.0, "TESTODNO0001",
                               order_status="접수")
    rows = db_manager.db.get_trades(limit=50) or []
    assert any(str(r.get('odno')) == "TESTODNO0001" for r in rows), \
        "임시 DB 에 기록되지 않았다면 어디로 갔는지부터 확인해야 한다"
    assert os.path.abspath(db_manager.db.db_path) != PRODUCTION_DB_PATH


# ── 2겹: 저널 호스트로 나가는 HTTP 는 막힌다 ──────────────────────────────
@pytest.mark.skipif(not getattr(config, 'JOURNAL_API_URL', ''),
                    reason="JOURNAL_API_URL 미설정 — 막을 호스트가 없다")
def test_journal_host_is_blocked_at_http_layer():
    import requests
    res = requests.post(f"{config.JOURNAL_API_URL.rstrip('/')}/api/v1/trades/batch",
                        json={}, timeout=5)
    # 200 이면 flush 가 '전송 완료' 도장을 찍어 진짜 체결이 큐에서 사라진다.
    assert res.status_code == 503, "차단 응답이 성공을 흉내 내면 안 된다"


# ── 3겹: 앞의 둘을 다 뚫어도 모듈이 스스로 닫는다 ─────────────────────────
def test_egress_guard_refuses_inside_pytest():
    assert journal_sync._egress_blocked(), \
        "pytest 세션에서는 매매일지 전송이 무조건 차단되어야 한다"


def test_request_returns_none_without_touching_network(monkeypatch):
    called = []
    import requests
    monkeypatch.setattr(requests, "request",
                        lambda *a, **k: called.append(a) or (_ for _ in ()).throw(
                            AssertionError("네트워크로 나갔다")))
    monkeypatch.setattr(journal_sync._tokens, "_token", "dummy", raising=False)
    monkeypatch.setattr(journal_sync._tokens, "_expires_at", 9e18, raising=False)

    assert journal_sync._request('POST', '/api/v1/trades/batch', json_body={}) is None
    assert not called


def test_token_fetch_refuses_inside_pytest(monkeypatch):
    monkeypatch.setattr(config, 'JOURNAL_API_URL', 'https://example.invalid', raising=False)
    monkeypatch.setattr(config, 'JOURNAL_API_KEY', 'skm_dummy', raising=False)
    journal_sync._tokens.invalidate()
    assert journal_sync._tokens.get() is None
