"""정정된 체결이 매매일지 대기열에도 반영되는가.

outbox 의 payload 는 적재 시점 스냅샷이다. 부분체결이 여러 폴링 주기에 걸치면 수량·
평균단가가 나중에 확정되고 실현손익이 다시 계산되는데, 그 정정이 큐에 반영되지 않으면
**로컬 DB 와 웹 매매일지의 손익이 갈린다**(실측 2026-09-03: 로컬 681,999 / 전송 204,601).
웹 매매일지는 사람이 성과를 판단하는 화면이므로 갈리면 안 된다.
"""
import json

import pytest

import config
from modules import db_manager, journal_sync

ODNO = "0000777001"
CODE = "005930"


@pytest.fixture
def journal_on(monkeypatch, tmp_path):
    """연동을 켠 채로 임시 DB 를 쓴다. (네트워크는 3겹 방어가 막는다)"""
    monkeypatch.setattr(config, 'JOURNAL_API_URL', 'https://example.invalid', raising=False)
    monkeypatch.setattr(config, 'JOURNAL_API_KEY', 'skm_dummy', raising=False)
    monkeypatch.setattr(config.settings, 'JOURNAL_SYNC_USE', True, raising=False)
    monkeypatch.setattr(config.session, 'is_paper', False, raising=False)
    assert journal_sync.is_enabled()
    yield


def _outbox(db):
    cur = db.get_connection().cursor()
    return [dict(r) for r in cur.execute(
        "SELECT * FROM journal_outbox ORDER BY id DESC").fetchall()]


def test_corrected_fill_updates_queued_payload(journal_on):
    db = db_manager.db
    db.insert_trade("sell(AUTO)", CODE, "삼성전자", 30, 77000.0, ODNO,
                    order_status="체결", profit_amt=204601, profit_rate=9.74)

    before = [r for r in _outbox(db) if ODNO in r['exec_id']]
    assert before, "체결이 대기열에 적재되어야 한다"
    assert json.loads(before[0]['payload'])['volume'] == 30

    # 뒤늦게 전량 체결이 확인되어 수량·손익이 정정된다.
    db.update_trade(ODNO, qty=100, price=77000.0, profit_amt=681999,
                    profit_rate=9.74, where_status="체결")

    after = [r for r in _outbox(db) if ODNO in r['exec_id']]
    assert len(after) == len(before), "정정은 새 행이 아니라 같은 행을 덮어야 한다"
    payload = json.loads(after[0]['payload'])
    assert payload['volume'] == 100, "정정된 수량이 대기열에 반영되지 않았다"
    assert '681,999' in json.dumps(payload, ensure_ascii=False) or \
           payload.get('profitAmount') == 681999, \
           f"정정된 손익이 대기열에 반영되지 않았다: {payload}"


def test_correction_resets_sent_row_for_resend(journal_on):
    """이미 보낸 건이어도 정정되면 다시 보내야 한다 — 서버 값이 옛 값으로 남는다."""
    db = db_manager.db
    odno = "0000777002"
    db.insert_trade("sell(AUTO)", CODE, "삼성전자", 30, 77000.0, odno,
                    order_status="체결", profit_amt=100, profit_rate=1.0)

    conn = db.get_connection()
    conn.execute("UPDATE journal_outbox SET synced_at='2026-09-03 16:00:00', remote_id='r1' "
                 "WHERE exec_id LIKE ?", (f"%{odno}%",))
    conn.commit()

    db.update_trade(odno, profit_amt=999, where_status="체결")

    row = [r for r in _outbox(db) if odno in r['exec_id']][0]
    assert row['synced_at'] is None, "정정분이 전송 대기로 되돌아가지 않았다"


def test_dead_lettered_row_is_not_revived(journal_on):
    """전송을 포기한 행은 정정이 있어도 되살리지 않는다 — 배치 앞자리를 다시 막는다."""
    db = db_manager.db
    odno = "0000777003"
    db.insert_trade("sell(AUTO)", CODE, "삼성전자", 30, 77000.0, odno,
                    order_status="체결", profit_amt=100, profit_rate=1.0)

    conn = db.get_connection()
    conn.execute("UPDATE journal_outbox SET dead_at='2026-09-03 16:00:00' "
                 "WHERE exec_id LIKE ?", (f"%{odno}%",))
    conn.commit()

    db.update_trade(odno, profit_amt=999, where_status="체결")

    row = [r for r in _outbox(db) if odno in r['exec_id']][0]
    assert row['dead_at'] is not None
    assert json.loads(row['payload'])['volume'] == 30


def test_received_order_row_is_not_queued(journal_on):
    """'접수' 행의 단가 갱신은 대기열을 건드리면 안 된다 (체결이 아니다)."""
    db = db_manager.db
    odno = "0000777004"
    db.insert_trade("sell(AUTO)", CODE, "삼성전자", 30, 0.0, odno, order_status="접수")
    assert not [r for r in _outbox(db) if odno in r['exec_id']]

    db.update_trade(odno, price=77000.0)
    assert not [r for r in _outbox(db) if odno in r['exec_id']], \
        "접수 행이 매매일지 대기열에 실렸다"
