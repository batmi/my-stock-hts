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


# ══════════════════════════════════════════════════════════════════════
# 서버까지 닿는가 — 큐를 고치는 것만으로는 부족하다
#
# 웹서버(stock-memo)의 배치 엔드포인트는 같은 brokerExecutionId 를 duplicate 로
# 건너뛰기만 하고 **값을 덮지 않는다**(trading_api/entries._insert_trade).
# 클라이언트는 duplicate 를 성공으로 세므로, 정정분을 다시 POST 하면 '전송 완료'
# 도장만 찍히고 서버 값은 옛것 그대로 남는다 — 조용히 갈라진다.
# 갱신 경로는 PATCH 하나뿐이다.
# ══════════════════════════════════════════════════════════════════════

class _Res:
    def __init__(self, status=200, payload=None):
        self.status_code = status
        self._payload = payload or {}
        self.text = json.dumps(self._payload)
        self.headers = {}

    def json(self):
        return self._payload


def _sent_row(db, odno, remote_id="9001"):
    """이미 서버에 들어간 체결 하나를 만든다."""
    db.insert_trade("sell(AUTO)", CODE, "삼성전자", 30, 77000.0, odno,
                    order_status="체결", profit_amt=204601, profit_rate=9.74)
    conn = db.get_connection()
    conn.execute("UPDATE journal_outbox SET synced_at='2026-09-03 16:00:00', remote_id=? "
                 "WHERE exec_id LIKE ?", (remote_id, f"%{odno}%"))
    conn.commit()


def test_correction_is_sent_as_patch_not_post(journal_on, monkeypatch):
    db = db_manager.db
    odno = "0000778001"
    _sent_row(db, odno, remote_id="9001")

    calls = []

    def _fake(method, path, **kw):
        calls.append((method, path, kw.get('json_body')))
        return _Res(200, {'id': '9001'})

    monkeypatch.setattr(journal_sync, '_request', _fake)

    db.update_trade(odno, qty=100, profit_amt=681999, where_status="체결")
    journal_sync.flush_once()

    patches = [c for c in calls if c[0] == 'PATCH']
    assert patches, f"정정이 PATCH 로 나가지 않았다 (호출: {[(c[0], c[1]) for c in calls]})"
    assert patches[0][1] == '/api/v1/trades/9001'
    assert patches[0][2]['realizedPnl'] == 681999
    assert patches[0][2]['volume'] == 100
    assert not [c for c in calls if c[0] == 'POST' and 'batch' in c[1]], \
        "정정분이 배치로도 나갔다 — 서버는 그것을 duplicate 로 버린다"

    row = [r for r in _outbox(db) if odno in r['exec_id']][0]
    assert row['synced_at'] is not None, "PATCH 성공 후에도 대기로 남아 있다"


def test_patch_404_falls_back_to_new_registration(journal_on, monkeypatch):
    """서버에서 지워진 기록은 PATCH 로 영영 못 고친다 — 신규 등록으로 되돌린다."""
    db = db_manager.db
    odno = "0000778002"
    _sent_row(db, odno, remote_id="9002")

    monkeypatch.setattr(journal_sync, '_request',
                        lambda method, path, **kw: _Res(404, {'error': 'NOT_FOUND'}))
    db.update_trade(odno, profit_amt=681999, where_status="체결")
    journal_sync.flush_once()

    row = [r for r in _outbox(db) if odno in r['exec_id']][0]
    assert row['needs_patch'] in (0, None), "PATCH 표시가 남아 영영 재시도만 반복한다"
    assert row['remote_id'] is None
    assert row['synced_at'] is None, "신규 등록 대상으로 대기열에 남아야 한다"


def test_unsent_correction_still_goes_out_as_batch(journal_on, monkeypatch):
    """아직 안 보낸 건의 정정은 종전대로 POST 다 — PATCH 할 대상이 없다."""
    db = db_manager.db
    odno = "0000778003"
    db.insert_trade("sell(AUTO)", CODE, "삼성전자", 30, 77000.0, odno,
                    order_status="체결", profit_amt=100, profit_rate=1.0)

    calls = []

    def _fake(method, path, **kw):
        calls.append((method, path))
        return _Res(200, {'results': [{'index': 0, 'status': 'created', 'id': '9003',
                                       'brokerExecutionId': None}]})

    monkeypatch.setattr(journal_sync, '_request', _fake)
    db.update_trade(odno, profit_amt=999, where_status="체결")
    journal_sync.flush_once()

    assert not [c for c in calls if c[0] == 'PATCH']
    assert [c for c in calls if c[0] == 'POST']


def test_patchable_fields_match_server_contract():
    """서버가 고칠 수 있는 필드와 우리가 보내는 필드가 갈리면 정정이 조용히 유실된다."""
    server = {'price', 'volume', 'status', 'confidence', 'realizedPnl',
              'realizedPnlRate', 'fee', 'tax', 'strategyScore', 'stopLossRate',
              'memo', 'name'}
    assert set(journal_sync._PATCHABLE_FIELDS) <= server, \
        "서버가 받지 않는 필드를 PATCH 로 보내면 조용히 무시된다"
    # 우리가 실제로 정정하는 값(수량·손익)은 반드시 포함돼야 한다.
    for must in ('volume', 'price', 'realizedPnl', 'realizedPnlRate'):
        assert must in journal_sync._PATCHABLE_FIELDS
