"""매매일지 웹서버 연동(journal_sync) 테스트.

핵심 계약을 회귀로 고정한다.
  - 체결만 큐에 쌓이고, 접수·취소·모의투자는 기본적으로 제외된다
  - 멱등키에 계좌·일자가 들어간다 (odno 는 영업일마다 재사용되므로)
  - 큐 적재는 거래 기록 저장과 같은 트랜잭션이라 '기록만 남고 큐엔 없는' 틈이 없다
  - 전송 실패·서버 장애가 매매 로직으로 새어 나오지 않고, 재시도로 복구된다
"""
import json
import sqlite3

import pytest

import config
from modules import journal_sync
from modules.db_manager import DBManager


@pytest.fixture
def db(tmp_path, monkeypatch):
    """임시 DB + 연동 활성 상태의 DBManager."""
    monkeypatch.setattr(config, 'DB_FILE_PATH', str(tmp_path / 'journal_test.db'))
    monkeypatch.setattr(config, 'JOURNAL_API_URL', 'http://journal.test', raising=False)
    monkeypatch.setattr(config, 'JOURNAL_API_KEY', 'skm_test', raising=False)
    monkeypatch.setattr(config, 'JOURNAL_SOURCE', 'my-stock-hts', raising=False)
    monkeypatch.setattr(config, 'JOURNAL_SYNC_SIMULATION', False, raising=False)
    monkeypatch.setattr(config.settings, 'JOURNAL_SYNC_USE', True)
    monkeypatch.setattr(config.session, 'is_simulation', False)
    monkeypatch.setattr(config.session, 'cano', '12345678')
    monkeypatch.setattr(config.session, 'acnt_prdt_cd', '01')

    manager = DBManager()
    # journal_sync 는 전역 db 핸들을 쓰므로 같은 인스턴스를 바라보게 한다.
    from modules import db_manager as db_manager_module
    monkeypatch.setattr(db_manager_module, 'db', manager)

    yield manager

    manager.close_all_connections() if hasattr(manager, 'close_all_connections') else None


def _outbox(db):
    conn = db._get_conn()
    return [dict(r) for r in conn.execute(
        "SELECT * FROM journal_outbox ORDER BY id").fetchall()]


def _fill(db, **kw):
    args = dict(type_str='매수(AUTO)', code='005930', name='삼성전자', qty=10,
                price=71000, odno='0000012345', order_status='체결',
                custom_time='2026-08-01 09:30:00')
    args.update(kw)
    db.insert_trade(**args)


# ── 큐 적재 대상 ──────────────────────────────────────────────────────

def test_fill_is_queued(db):
    _fill(db)
    rows = _outbox(db)
    assert len(rows) == 1
    payload = json.loads(rows[0]['payload'])
    assert payload['symbol'] == '005930'
    assert payload['side'] == 'BUY'
    assert payload['volume'] == 10
    assert payload['orderOrigin'] == 'AUTO'
    assert payload['isSimulated'] is False


@pytest.mark.parametrize('status', ['접수', '취소', '취소(추정)', '체결/취소(추정)'])
def test_non_fill_statuses_are_not_queued(db, status):
    """접수는 아직 체결이 아니고, 취소는 매매 기록이 아니다."""
    _fill(db, order_status=status)
    assert _outbox(db) == []


def test_estimated_fill_is_marked(db):
    """잔고 대조로 '추정'한 체결은 확정 체결과 구분해서 보내야 나중에 정정할 수 있다."""
    _fill(db, order_status='체결(추정)')
    payload = json.loads(_outbox(db)[0]['payload'])
    assert payload['confidence'] == 'ESTIMATED'


def test_simulation_is_excluded_by_default(db, monkeypatch):
    monkeypatch.setattr(config.session, 'is_simulation', True)
    _fill(db)
    assert _outbox(db) == []


def test_simulation_is_queued_when_opted_in(db, monkeypatch):
    monkeypatch.setattr(config, 'JOURNAL_SYNC_SIMULATION', True, raising=False)
    monkeypatch.setattr(config.session, 'is_simulation', True)
    _fill(db)
    payload = json.loads(_outbox(db)[0]['payload'])
    assert payload['isSimulated'] is True
    assert payload['brokerExecutionId'].startswith('SIM:')


def test_disabled_integration_queues_nothing(db, monkeypatch):
    monkeypatch.setattr(config, 'JOURNAL_API_URL', '', raising=False)
    _fill(db)
    assert _outbox(db) == []
    # 연동이 꺼져도 거래 기록 자체는 정상 저장되어야 한다.
    assert len(db.get_trades()) == 1


def test_menu_toggle_off_queues_nothing(db, monkeypatch):
    """환경변수가 다 있어도 메뉴 0 스위치가 꺼져 있으면 아무것도 하지 않는다."""
    monkeypatch.setattr(config.settings, 'JOURNAL_SYNC_USE', False)
    assert journal_sync.is_enabled() is False
    _fill(db)
    assert _outbox(db) == []
    assert len(db.get_trades()) == 1


def test_toggle_default_is_off():
    """기본값은 OFF — 설정하지 않은 사용자에게 외부 전송이 켜져 있으면 안 된다."""
    from config import GlobalSettings
    assert GlobalSettings().JOURNAL_SYNC_USE is False


def test_toggle_persisted_in_dynamic_config(monkeypatch):
    """재시작 후에도 유지되도록 dynamic_config 저장 대상에 포함되어야 한다."""
    from modules import settings as settings_module

    monkeypatch.setattr(config.settings, 'JOURNAL_SYNC_USE', True)
    saved = {}
    monkeypatch.setattr(settings_module.jsonio, 'save_json',
                        lambda path, data: saved.update(data) or True)
    settings_module._save_dynamic_config()
    assert saved['JOURNAL_SYNC_USE'] is True


def test_toggle_appears_in_settings_menu():
    """메뉴 0 → 5-3(데이터·통신)에서 편집할 수 있어야 한다."""
    from modules import settings as settings_module

    item = next(i for i in settings_module._trading_cycle_items()
                if i['name'] == 'JOURNAL_SYNC_USE')
    assert item['type'] == 'bool'
    assert item['section'] == '5-3. 데이터·통신'


def test_toggle_off_warns_and_stops_worker(monkeypatch):
    """끄면 워커도 즉시 멈춰야 한다 (재시작을 기다리게 하지 않는다)."""
    from modules import settings as settings_module

    stopped = []
    monkeypatch.setattr(journal_sync, 'stop', lambda: stopped.append(True))
    monkeypatch.setattr(config.settings, 'JOURNAL_SYNC_USE', True)

    settings_module._set_journal_sync_use(False)
    assert config.settings.JOURNAL_SYNC_USE is False
    assert stopped == [True]


def test_toggle_on_without_credentials_does_not_start(monkeypatch):
    """환경변수가 없으면 켜도 워커를 띄우지 않고 무엇이 빠졌는지 알린다."""
    from modules import settings as settings_module

    started = []
    monkeypatch.setattr(journal_sync, 'start', lambda: started.append(True))
    monkeypatch.setattr(config, 'JOURNAL_API_URL', '', raising=False)
    monkeypatch.setattr(config, 'JOURNAL_API_KEY', '', raising=False)
    monkeypatch.setattr(config.settings, 'JOURNAL_SYNC_USE', False)

    settings_module._set_journal_sync_use(True)
    assert config.settings.JOURNAL_SYNC_USE is True   # 설정 자체는 저장된다
    assert started == []                              # 그러나 워커는 뜨지 않는다


def test_unparseable_type_is_not_queued(db):
    """'확인요망' 처럼 매수/매도로 해석되지 않는 기록은 일지로 보내지 않는다."""
    _fill(db, type_str='확인요망')
    assert _outbox(db) == []


# ── 멱등키 ────────────────────────────────────────────────────────────

def test_execution_id_includes_account_and_date(db):
    """odno 는 영업일마다 재사용된다 — 계좌·일자가 없으면 다른 날 체결이 중복 처리된다."""
    _fill(db)
    exec_id = _outbox(db)[0]['exec_id']
    assert exec_id == 'REAL:1234567801:20260801:0000012345:F'


def test_same_fill_recorded_twice_queues_once(db):
    _fill(db)
    _fill(db)
    assert len(_outbox(db)) == 1


def test_same_odno_on_different_days_is_distinct(db):
    """같은 주문번호라도 날짜가 다르면 별개의 체결이다."""
    _fill(db, custom_time='2026-08-01 09:30:00')
    _fill(db, custom_time='2026-08-03 09:30:00')
    assert len({r['exec_id'] for r in _outbox(db)}) == 2


def test_estimated_and_confirmed_are_distinct_entries(db):
    _fill(db, order_status='체결(추정)')
    _fill(db, order_status='체결')
    ids = [r['exec_id'] for r in _outbox(db)]
    assert ids[0].endswith(':E') and ids[1].endswith(':F')


# ── 페이로드 내용 ─────────────────────────────────────────────────────

def test_sell_carries_realized_pnl(db):
    """봇이 이미 계산해 둔 실현손익을 넘겨야 서버 통계가 정확해진다."""
    _fill(db, type_str='매도(AUTO)', profit_amt=40000, profit_rate=5.63,
          odno='0000012399')
    payload = json.loads(_outbox(db)[0]['payload'])
    assert payload['side'] == 'SELL'
    assert payload['realizedPnl'] == 40000
    assert payload['realizedPnlRate'] == 5.63


def test_strategy_fields_are_carried(db):
    _fill(db, score=7.2, stop_loss_rate=-7.0, reason='추세 진입')
    payload = json.loads(_outbox(db)[0]['payload'])
    assert payload['strategyScore'] == 7.2
    assert payload['stopLossRate'] == -7.0
    assert payload['memo'] == '추세 진입'


def test_executed_at_carries_kst_offset(db):
    """오프셋 없이 보내면 서버가 해외 체결의 거래일을 잘못 귀속시킨다."""
    _fill(db)
    payload = json.loads(_outbox(db)[0]['payload'])
    assert payload['executedAt'] == '2026-08-01T09:30:00+0900'


def test_overseas_exchange_resolved_from_universe(db, monkeypatch):
    """서버가 이 값으로 현지 거래일을 계산하므로 해외는 거래소 코드가 중요하다."""
    monkeypatch.setattr(config.session, 'stock_data',
                        {'stocks_us': [{'code': 'AAPL', 'exchange': 'NAS'}]})
    _fill(db, code='AAPL', name='Apple', odno='0000099001')
    payload = json.loads(_outbox(db)[0]['payload'])
    assert payload['currency'] == 'USD'
    assert payload['exchange'] == 'NAS'


def test_domestic_marked_as_krx(db):
    _fill(db)
    payload = json.loads(_outbox(db)[0]['payload'])
    assert payload['currency'] == 'KRW'
    assert payload['exchange'] == 'KRX'


@pytest.mark.parametrize('type_str,expected', [
    ('매수(AUTO)', 'AUTO'),
    ('매도(수동)', 'MANUAL'),
    ('매수(예약)', 'RESERVED'),
    ('매수(외부)', 'EXTERNAL'),
])
def test_order_origin_extracted(db, type_str, expected):
    _fill(db, type_str=type_str)
    assert json.loads(_outbox(db)[0]['payload'])['orderOrigin'] == expected


# ── 장애 격리 ─────────────────────────────────────────────────────────

def test_queue_failure_never_breaks_trade_recording(db, monkeypatch):
    """일지 전송은 부가 기능이다 — 여기서 터져도 거래 기록은 반드시 남아야 한다."""
    def boom(*args, **kwargs):
        raise RuntimeError('큐 적재 폭발')
    monkeypatch.setattr(journal_sync, 'enqueue', boom)

    _fill(db)
    assert len(db.get_trades()) == 1


def test_flush_is_noop_when_disabled(db, monkeypatch):
    monkeypatch.setattr(config, 'JOURNAL_API_KEY', '', raising=False)
    assert journal_sync.flush_once() == (0, 0)


# ── 전송 결과 반영 ────────────────────────────────────────────────────

class _FakeResponse:
    def __init__(self, status_code, body=None, headers=None):
        self.status_code = status_code
        self._body = body or {}
        self.headers = headers or {}
        self.text = json.dumps(self._body)

    def json(self):
        return self._body


def _patch_transport(monkeypatch, response):
    """토큰 발급을 건너뛰고 배치 요청 응답만 갈아끼운다."""
    monkeypatch.setattr(journal_sync._tokens, '_token', 'tok')
    monkeypatch.setattr(journal_sync._tokens, '_expires_at', 1e18)
    calls = []

    def fake_request(method, path, *, json_body=None, params=None, retry_on_401=True):
        calls.append((method, path, json_body))
        return response

    monkeypatch.setattr(journal_sync, '_request', fake_request)
    return calls


def test_successful_flush_marks_rows_synced(db, monkeypatch):
    _fill(db)
    calls = _patch_transport(monkeypatch, _FakeResponse(201, {
        'inserted': 1, 'skipped': 0, 'failed': 0,
        'results': [{'index': 0, 'status': 'created', 'id': '42'}]}))

    assert journal_sync.flush_once() == (1, 0)
    assert journal_sync.pending_count() == 0
    assert _outbox(db)[0]['remote_id'] == '42'
    assert calls[0][1] == '/api/v1/trades/batch'


def test_duplicate_response_counts_as_synced(db, monkeypatch):
    """서버가 이미 갖고 있는 기록은 성공으로 처리해야 큐가 영원히 안 비지 않는다."""
    _fill(db)
    _patch_transport(monkeypatch, _FakeResponse(200, {
        'inserted': 0, 'skipped': 1, 'failed': 0,
        'results': [{'index': 0, 'status': 'duplicate', 'id': '7'}]}))

    assert journal_sync.flush_once() == (1, 0)
    assert journal_sync.pending_count() == 0


def test_server_down_keeps_rows_for_retry(db, monkeypatch):
    """네트워크가 끊겨도 큐가 남아 복구 후 자동 재전송된다."""
    _fill(db)
    _patch_transport(monkeypatch, None)

    assert journal_sync.flush_once() == (0, 1)
    assert journal_sync.pending_count() == 1
    row = _outbox(db)[0]
    assert row['attempts'] == 1 and row['synced_at'] is None


def test_item_level_failure_is_retried(db, monkeypatch):
    _fill(db)
    _patch_transport(monkeypatch, _FakeResponse(200, {
        'inserted': 0, 'skipped': 0, 'failed': 1,
        'results': [{'index': 0, 'status': 'failed',
                     'errorCode': 'INVALID_FIELD', 'error': '가격 오류'}]}))

    assert journal_sync.flush_once() == (0, 1)
    assert 'INVALID_FIELD' in _outbox(db)[0]['last_error']


def test_rate_limited_response_defers_without_data_loss(db, monkeypatch):
    _fill(db)
    _patch_transport(monkeypatch, _FakeResponse(429, {}, {'Retry-After': '30'}))

    assert journal_sync.flush_once() == (0, 1)
    assert journal_sync.pending_count() == 1


def test_backoff_defers_immediate_retry(db, monkeypatch):
    """실패 직후 곧바로 다시 보내면 죽은 서버에 헛된 요청만 쌓인다."""
    _fill(db)
    _patch_transport(monkeypatch, None)
    journal_sync.flush_once()

    # 백오프 대기 중이므로 두 번째 호출은 아무것도 집어오지 않는다.
    assert journal_sync._fetch_pending() == []


def test_missing_result_item_is_retried(db, monkeypatch):
    """응답에 결과가 빠지면 성공 여부를 알 수 없다 — 멱등하니 다시 보내는 쪽이 안전하다."""
    _fill(db)
    _patch_transport(monkeypatch, _FakeResponse(201, {'inserted': 1, 'results': []}))

    assert journal_sync.flush_once() == (0, 1)
    assert journal_sync.pending_count() == 1
