"""매매일지 웹서버 연동(journal_sync) 테스트.

핵심 계약을 회귀로 고정한다.
  - 체결만 큐에 쌓이고, 접수·취소·모의투자는 기본적으로 제외된다
  - 멱등키에 계좌·일자가 들어간다 (odno 는 영업일마다 재사용되므로)
  - 큐 적재는 거래 기록 저장과 같은 트랜잭션이라 '기록만 남고 큐엔 없는' 틈이 없다
  - 전송 실패·서버 장애가 매매 로직으로 새어 나오지 않고, 재시도로 복구된다
"""
import json
import re
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
    from core import context
    monkeypatch.setattr(db_manager_module, 'db', manager)
    monkeypatch.setattr(context.trade_context, 'use_auto_account', False, raising=False)

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
    assert payload['memo'] == '<p>추세 진입</p>'


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


# ── 시스템/비시스템 분류 ──────────────────────────────────────────────
#
# HTS 는 자기 계좌에서 일어난 체결을 전부 보고한다 — 토스 앱이나 증권사 HTS 에서
# 사람이 직접 낸 주문까지 포함해서다. 예전엔 그 전부에 tradeClass='시스템'을 못 박아
# 보내 자동매매 성과와 수동 매매가 한 덩어리가 됐다.

def test_auto_order_is_marked_system(db):
    """AutoTrader 가 낸 주문만 '시스템'으로 확정해 보낸다."""
    _fill(db, type_str='매수(AUTO)')
    payload = json.loads(_outbox(db)[0]['payload'])
    assert payload['isSystem'] is True
    assert payload['tradeClass'] == '시스템'


@pytest.mark.parametrize('type_str', ['매도(수동)', '매수(예약)', '매수(외부)'])
def test_non_auto_order_is_not_system_and_sends_no_class(db, type_str):
    """예약·수동·외부는 사람이 낸 주문이다 — 분류를 아예 싣지 않는다.

    분류를 비워 보내야 서버가 같은 종목의 직전 분류(장기투자 등)를 물려받는다.
    '시스템'을 실어 보내면 그 폴백이 영원히 동작하지 않는다.
    """
    _fill(db, type_str=type_str)
    payload = json.loads(_outbox(db)[0]['payload'])
    assert payload['isSystem'] is False
    assert 'tradeClass' not in payload


def test_unknown_origin_omits_is_system(db):
    """출처를 모르면 단정하지 않는다 — isSystem 자체를 보내지 않는다.

    False 로 눕히면 '사람이 냈다'고 확정한 셈이 되어, 서버의 분류 상속 폴백이
    '모름'과 '사람이 냄'을 구분하지 못한다.
    """
    _fill(db, type_str='매수')
    payload = json.loads(_outbox(db)[0]['payload'])
    assert 'isSystem' not in payload
    assert 'tradeClass' not in payload


def test_is_system_helper_maps_every_origin():
    assert journal_sync._is_system('매수(AUTO)') is True
    assert journal_sync._is_system('매수(예약)') is False
    assert journal_sync._is_system('매도(수동)') is False
    assert journal_sync._is_system('매수(외부)') is False
    assert journal_sync._is_system('매수') is None
    assert journal_sync._is_system(None) is None


# ── 봇 인스턴스 식별 ──────────────────────────────────────────────────

def _set_mode(monkeypatch, *, toss=False, simulation=False, cano='68029263', auto=''):
    monkeypatch.setattr(config.session, 'is_toss', toss, raising=False)
    monkeypatch.setattr(config.session, 'is_simulation', simulation)
    monkeypatch.setattr(config.session, 'cano', cano)
    monkeypatch.setattr(config.session, 'auto_cano', auto, raising=False)


def test_bot_id_defaults_to_source(db, monkeypatch):
    monkeypatch.setattr(config, 'JOURNAL_SOURCE', 'raspi', raising=False)
    monkeypatch.setattr(config, 'JOURNAL_BOT_ID', '', raising=False)
    _set_mode(monkeypatch)
    assert journal_sync._bot_id() == 'raspi:real:68029263'


def test_bot_id_override_is_used_as_prefix(db, monkeypatch):
    monkeypatch.setattr(config, 'JOURNAL_SOURCE', 'raspi', raising=False)
    monkeypatch.setattr(config, 'JOURNAL_BOT_ID', 'pi3b', raising=False)
    _set_mode(monkeypatch, simulation=True, cano='50196591')
    assert journal_sync._bot_id() == 'pi3b:sim:50196591'


def test_bot_id_differs_per_mode_on_one_machine(db, monkeypatch):
    """모드는 `--mode` CLI 인자라 환경변수로는 구분되지 않는다.

    같은 기기에서 ~/.htsrc 하나로 세 모드를 돌리면 JOURNAL_BOT_ID·JOURNAL_SOURCE 가
    셋 다 같은 값이 된다. 환경변수만 식별자로 쓰면 세 인스턴스가 서버의 같은 칸을
    덮어써서, 웹 목록에 자기 자리가 없는 봇이 생긴다
    (실측 2026-08-03: 모의·실전·토스 3대를 돌렸는데 2대만 표시됨).
    """
    monkeypatch.setattr(config, 'JOURNAL_SOURCE', 'raspi', raising=False)
    monkeypatch.setattr(config, 'JOURNAL_BOT_ID', '', raising=False)

    _set_mode(monkeypatch, simulation=True, cano='50196591')
    sim = journal_sync._bot_id()
    _set_mode(monkeypatch, cano='68029263', auto='44048158')
    real = journal_sync._bot_id()
    _set_mode(monkeypatch, toss=True, cano='189-01-501685')
    toss = journal_sync._bot_id()

    assert len({sim, real, toss}) == 3


def test_bot_id_stays_within_length_limit_without_losing_the_discriminator(db, monkeypatch):
    """상한을 뒤에서 자르면 구분자가 날아가 충돌이 되살아난다 — 접두어 쪽을 깎는다."""
    monkeypatch.setattr(config, 'JOURNAL_BOT_ID', 'x' * 200, raising=False)
    _set_mode(monkeypatch, cano='68029263')
    bot_id = journal_sync._bot_id()
    assert len(bot_id) <= 64
    assert bot_id.endswith(':real:68029263')


def test_bot_label_shows_the_auto_trading_account_too(db, monkeypatch):
    """한투 실전은 거래 계좌와 자동매매 계좌가 다르다 — 둘 다 보여야 한다."""
    monkeypatch.setattr(config, 'JOURNAL_BOT_LABEL', '', raising=False)
    _set_mode(monkeypatch, cano='68029263', auto='44048158')
    monkeypatch.setattr(config.session, 'auto_acnt_prdt_cd', '01', raising=False)
    label = journal_sync._bot_label()
    assert '68029263-01' in label and '44048158-01' in label


def test_bot_label_keeps_the_account_product_code(db, monkeypatch):
    """상품코드(-01)는 별도 필드라, 붙이지 않으면 화면 계좌번호가 잘려 보인다."""
    monkeypatch.setattr(config, 'JOURNAL_BOT_LABEL', '', raising=False)
    _set_mode(monkeypatch, simulation=True, cano='50196591', auto='50196591')
    assert journal_sync._bot_label() == '모의 50196591-01'


def test_bot_label_omits_auto_account_when_it_is_the_same(db, monkeypatch):
    """모의·토스는 단일 계좌라 같은 번호를 두 번 적을 이유가 없다."""
    monkeypatch.setattr(config, 'JOURNAL_BOT_LABEL', '', raising=False)
    _set_mode(monkeypatch, toss=True, cano='189-01-501685', auto='189-01-501685')
    monkeypatch.setattr(config.session, 'acnt_prdt_cd', '')
    monkeypatch.setattr(config.session, 'auto_acnt_prdt_cd', '', raising=False)
    assert journal_sync._bot_label() == '토스 189-01-501685'


def test_ping_carries_bot_identity(db, monkeypatch):
    """botId 가 빠지면 서버가 사용자당 한 칸에 상태를 겹쳐 쓴다 — 여러 대를 돌릴 때
    실전봇의 죽음이 모의봇 Ping 에 가려지고, 재동기화도 엉뚱한 봇이 채간다."""
    monkeypatch.setattr(config, 'JOURNAL_BOT_ID', 'raspi', raising=False)
    monkeypatch.setattr(config, 'JOURNAL_BOT_LABEL', '', raising=False)
    _set_mode(monkeypatch, cano='68029263')
    calls = _patch_transport(monkeypatch, _FakeResponse(200, {'status': 'success'}))

    assert journal_sync.ping('running') is True
    body = calls[0][2]
    assert body['botId'] == 'raspi:real:68029263'
    assert body['label']          # 표시명이 비면 웹 목록에서 어느 봇인지 알 수 없다


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


# ── 로그 레벨 ─────────────────────────────────────────────────────────

@pytest.mark.parametrize('file_level,expect_info,expect_debug', [
    ('WARNING', True, False),   # 기본 설정 — 연동 로그는 그래도 남아야 한다
    ('ERROR', True, False),
    ('DEBUG', True, True),      # 더 자세히 보려는 설정은 존중한다
])
def test_journal_logs_at_info_regardless_of_file_level(
        monkeypatch, tmp_path, file_level, expect_info, expect_debug):
    """외부로 나가는 전송은 사후 추적이 되어야 하므로 항상 INFO 이상을 남긴다."""
    import logging

    monkeypatch.setattr(config, 'LOG_DIR', str(tmp_path))
    monkeypatch.setattr(config.settings, 'FILE_DEBUG_LEVEL', file_level)
    config.setup_logging()

    journal_logger = logging.getLogger('modules.journal_sync')
    assert journal_logger.isEnabledFor(logging.INFO) is expect_info
    assert journal_logger.isEnabledFor(logging.DEBUG) is expect_debug


def test_other_modules_keep_configured_level(monkeypatch, tmp_path):
    """연동 로거만 낮추고 나머지는 FILE_DEBUG_LEVEL 을 그대로 따라야 한다."""
    import logging

    monkeypatch.setattr(config, 'LOG_DIR', str(tmp_path))
    monkeypatch.setattr(config.settings, 'FILE_DEBUG_LEVEL', 'WARNING')
    config.setup_logging()

    assert logging.getLogger('modules.trading').isEnabledFor(logging.INFO) is False


def test_queued_fill_is_logged_at_info(db, caplog):
    """어떤 체결이 언제 큐에 들어갔는지 로그만으로 추적할 수 있어야 한다."""
    import logging

    with caplog.at_level(logging.INFO, logger='modules.journal_sync'):
        _fill(db)

    messages = [r.message for r in caplog.records]
    assert any('대기열 적재' in m and '005930' in m for m in messages)


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

    def fake_request(method, path, *, json_body=None, **kwargs):
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


# ── 상태 Ping / 종료 통지 ─────────────────────────────────────────────

def test_ping_interval_matches_three_strike_detection():
    """웹 대시보드가 3회 누락으로 장애를 판정하므로 주기가 늘어나면 감지도 늦어진다."""
    assert journal_sync._PING_INTERVAL_SEC == 10
    assert journal_sync._TICK_INTERVAL_SEC == journal_sync._PING_INTERVAL_SEC


def test_ping_sends_status_and_resets_fail_streak(db, monkeypatch):
    calls = _patch_transport(monkeypatch, _FakeResponse(200, {'status': 'success'}))
    journal_sync._ping_fail_streak = 5

    assert journal_sync.ping('running') is True
    assert calls[0][0] == 'POST' and calls[0][1] == '/api/v1/bot/status'
    assert calls[0][2]['status'] == 'running'
    assert journal_sync._ping_fail_streak == 0


def test_ping_failure_accumulates_streak(db, monkeypatch):
    """3회 연속 실패가 곧 웹 표시등의 '통신단절' 판정 시점이다."""
    _patch_transport(monkeypatch, None)
    journal_sync._ping_fail_streak = 0

    for _ in range(3):
        assert journal_sync.ping('running') is False
    assert journal_sync._ping_fail_streak == 3


def test_notify_shutdown_sends_stopped(db, monkeypatch):
    """HTS 종료 시 stopped 를 보내야 웹 표시등이 즉시 '정지됨'으로 바뀐다."""
    calls = _patch_transport(monkeypatch, _FakeResponse(200, {'status': 'success'}))

    assert journal_sync.notify_shutdown() is True
    assert calls[-1][1] == '/api/v1/bot/status'
    assert calls[-1][2]['status'] == 'stopped'


def test_notify_shutdown_works_while_toggle_already_off(db, monkeypatch):
    """메뉴로 연동을 끄면 토글이 먼저 False 가 된다 — 그래도 종료 통지는 나가야 한다."""
    calls = _patch_transport(monkeypatch, _FakeResponse(200, {'status': 'success'}))
    monkeypatch.setattr(config.settings, 'JOURNAL_SYNC_USE', False)

    assert journal_sync.notify_shutdown() is True
    assert calls[-1][2]['status'] == 'stopped'


def test_notify_shutdown_without_credentials_is_noop(monkeypatch):
    monkeypatch.setattr(config, 'JOURNAL_API_URL', '', raising=False)
    monkeypatch.setattr(config, 'JOURNAL_API_KEY', '', raising=False)
    assert journal_sync.notify_shutdown() is False


def test_notify_shutdown_never_raises_on_transport_error(db, monkeypatch):
    """종료 경로에서 예외가 올라오면 프로그램 종료 절차 자체가 깨진다."""
    def boom(*a, **kw):
        raise RuntimeError('network down')
    monkeypatch.setattr(journal_sync, '_request', boom)

    assert journal_sync.notify_shutdown() is False


def test_worker_stop_notifies_stopped(db, monkeypatch):
    """워커를 멈출 때 종료 통지가 함께 나가는지 (main.py 종료 경로의 계약)."""
    sent = []
    monkeypatch.setattr(journal_sync, '_notify_shutdown',
                        lambda status='stopped', message=None: sent.append(status))

    worker = journal_sync.JournalSyncWorker()
    worker.is_running = True
    worker.thread = None
    worker.stop()

    assert sent == ['stopped']


def test_worker_stop_can_skip_notification(db, monkeypatch):
    sent = []
    monkeypatch.setattr(journal_sync, '_notify_shutdown',
                        lambda status='stopped', message=None: sent.append(status))

    worker = journal_sync.JournalSyncWorker()
    worker.is_running = True
    worker.thread = None
    worker.stop(notify=None)

    assert sent == []


# ── 메모 구성 (진입/청산 근거 + 실현손익) ────────────────────────────

def _submit(db, **kw):
    """원 주문('접수') 기록. 진입/청산 근거는 여기에만 남는다."""
    args = dict(type_str='매수(AUTO)', code='005930', name='삼성전자', qty=10,
                price=71000, odno='0000012345', order_status='접수',
                custom_time='2026-08-01 09:29:00')
    args.update(kw)
    db.insert_trade(**args)


def _memo(db, index=0):
    return json.loads(_outbox(db)[index]['payload'])['memo']


def _memo_lines(db, index=0):
    """웹 카드에 표시될 메모를 줄 단위로 돌려준다.

    memo 는 `<p>` 문단으로 나가는데(서버가 HTML 그대로 카드에 그리므로 개행문자로는
    줄이 나뉘지 않는다), 검증은 표시될 줄 목록으로 하는 편이 의도가 드러난다.
    """
    return re.findall(r'<p>(.*?)</p>', _memo(db, index), re.S)


def test_memo_is_split_into_lines_for_the_web_card(db):
    """한 줄로 붙여 보내면 카드에서 통째로 흘러 눈으로 훑을 수 없다."""
    _submit(db, reason='조건 만족(슈퍼모멘텀) [당일 재진입(기존 100.1% 경신)] '
                       '[점수:9.5, RSI:70.4, 체결강도:102.7%] '
                       '[ATR:1,129/변동성:54.4%] [ATR손절:-7%]')
    _fill(db, reason='체결 확인 (잔고 입고 확인)')

    assert _memo_lines(db) == [
        '조건 만족(슈퍼모멘텀)',
        '[당일 재진입(기존 100.1% 경신)]',
        '[점수:9.5, RSI:70.4, 체결강도:102.7%]',
        '[ATR:1,129/변동성:54.4%]',
        '[ATR손절:-7%]',
        '· 체결 확인 (잔고 입고 확인)',
    ]


def test_memo_carries_the_reason_from_the_submission(db):
    """왜 샀는지는 '접수' 행에만 있다 — 체결 행만 보내면 근거가 통째로 사라진다."""
    _submit(db, reason='[추세매수] 조건 만족 [점수:8.5, RSI:61.6]')
    _fill(db, reason='체결 확인 (잔고 입고 확인)')

    assert _memo_lines(db) == ['[추세매수] 조건 만족', '[점수:8.5, RSI:61.6]',
                               '· 체결 확인 (잔고 입고 확인)']


def test_sell_memo_includes_realized_pnl(db):
    """구조화 필드로도 보내지만 웹 카드 본문엔 안 나온다 — 메모에도 적는다."""
    _submit(db, type_str='매도(AUTO)', odno='0000003867',
            reason='반익절(10.3%)')
    _fill(db, type_str='매도(AUTO)', odno='0000003867',
          reason='체결 확인 (잔고 0 확인)', profit_amt=88000, profit_rate=10.34)

    assert _memo_lines(db) == ['반익절(10.3%)', '· 체결 확인 (잔고 0 확인)',
                               '· 손익: +88,000원 (+10.34%)']


def test_memo_escapes_html_so_a_stray_bracket_cannot_break_the_card(db):
    """memo 는 카드에 HTML 그대로 들어간다 — 이스케이프하지 않으면 화면이 깨진다."""
    _submit(db, reason='<b>조건</b> & 만족')
    _fill(db, reason='체결 확인')

    assert '&lt;b&gt;조건&lt;/b&gt; &amp; 만족' in _memo(db)
    assert '<b>' not in _memo(db)


def test_buy_memo_has_no_pnl_section(db):
    """매수에는 실현손익이 없다 — 0원이라고 적으면 오히려 오해를 부른다."""
    _submit(db, reason='[추세매수] 조건 만족')
    _fill(db, reason='체결 확인 (잔고 입고 확인)')

    assert '손익' not in _memo(db)


def test_memo_ignores_the_same_order_number_from_another_day(db):
    """odno 는 영업일마다 재사용된다 — 날짜로 좁히지 않으면 남의 근거가 따라붙는다."""
    _submit(db, code='017670', name='SK텔레콤',
            custom_time='2026-06-30 10:00:00', reason='[다른날] 붙으면 안 되는 근거')
    _fill(db, reason='체결 확인 (잔고 입고 확인)')

    assert _memo_lines(db) == ['체결 확인 (잔고 입고 확인)']


def test_memo_falls_back_when_there_is_no_submission_row(db):
    """외부(앱/HTS) 주문은 접수 기록이 없다 — 확인 문구만 남기고 넘어간다.

    앞이 비었는데 '·' 를 붙이면 점만 덩그러니 남는다.
    """
    _fill(db, type_str='현금매수(외부)', reason='체결 확인 (앱/HTS 외부 주문)')

    assert _memo_lines(db) == ['체결 확인 (앱/HTS 외부 주문)']


def test_memo_follows_the_original_order_when_amended(db):
    """정정 행의 사유는 '사용자 정정'뿐 — 원주문까지 한 단계 거슬러 올라간다."""
    _submit(db, odno='0000011111', reason='[추세매수] 조건 만족 [점수:8.0]')
    _fill(db, odno='0000022222', org_odno='0000011111',
          reason='체결 확인 (사용자 정정)')

    assert _memo_lines(db)[0] == '[추세매수] 조건 만족'


def test_memo_does_not_repeat_an_identical_reason(db):
    """수동 주문은 접수·체결 사유가 같을 수 있다 — 같은 문장을 두 번 적지 않는다."""
    _submit(db, type_str='매수(수동)', reason='사용자 수동 주문')
    _fill(db, type_str='매수(수동)', reason='사용자 수동 주문')

    assert _memo_lines(db) == ['사용자 수동 주문']


def test_overseas_pnl_is_labelled_with_its_own_currency(db):
    """해외 체결 손익에 '원'을 붙이면 금액을 완전히 잘못 읽게 된다."""
    _submit(db, type_str='매도(AUTO)', code='AAPL', name='Apple', odno='0000009999',
            reason='[추세이탈] 매도진입')
    _fill(db, type_str='매도(AUTO)', code='AAPL', name='Apple', odno='0000009999',
          reason='체결 확인', profit_amt=-12.34, profit_rate=-3.5)

    assert '손익: -12.34 USD (-3.50%)' in _memo(db)


def test_memo_is_truncated_below_the_server_limit(db):
    """5000자를 넘기면 서버가 거절한다 — 메모가 길어서 체결을 잃으면 안 된다."""
    _submit(db, reason='가' * 6000)
    _fill(db, reason='체결 확인')

    assert len(_memo(db)) <= 4900


def test_entry_reason_lookup_failure_never_breaks_recording(db, monkeypatch):
    """근거 조회는 부가 기능이다 — 실패해도 거래 기록·큐 적재를 막으면 안 된다."""
    def boom(cursor, trade):
        raise sqlite3.OperationalError('boom')

    monkeypatch.setattr(journal_sync, '_lookup_entry_reason', boom)
    _submit(db, reason='[추세매수] 조건 만족')
    _fill(db, reason='체결 확인 (잔고 입고 확인)')

    assert len(_outbox(db)) == 1
    assert _memo_lines(db) == ['체결 확인 (잔고 입고 확인)']


def test_backfill_also_recovers_the_entry_reason(db, monkeypatch):
    """백필로 뒤늦게 회수한 건도 근거가 붙어야 한다."""
    monkeypatch.setattr(config.settings, 'JOURNAL_SYNC_USE', False)
    _submit(db, reason='[추세매수] 조건 만족 [점수:8.5]', custom_time=_recent(35))
    _fill(db, reason='체결 확인 (잔고 입고 확인)', custom_time=_recent(30))

    monkeypatch.setattr(config.settings, 'JOURNAL_SYNC_USE', True)
    _patch_backfill_transport(monkeypatch)
    assert journal_sync.backfill_once() == (1, 1)

    assert _memo_lines(db)[:2] == ['[추세매수] 조건 만족', '[점수:8.5]']


# ── 재동기화 (웹에서 지운 기록 복구) ─────────────────────────────────

def _mark_synced(db):
    """이미 서버로 보내진 상태로 만든다."""
    conn = db._get_conn()
    conn.execute("UPDATE journal_outbox SET synced_at = ?, remote_id = '1'",
                 (_recent(5),))
    conn.commit()


def test_resync_resends_records_already_marked_as_sent(db, monkeypatch):
    """운용자가 웹에서 지운 기록은 outbox 에 '전송 완료'로 남아 백필로는 못 잡는다."""
    _fill(db, custom_time=_recent(30))
    _mark_synced(db)
    assert journal_sync.pending_count() == 0

    _patch_backfill_transport(monkeypatch)
    assert journal_sync.backfill_once() == (0, 1)      # 백필로는 회수되지 않는다
    assert journal_sync.pending_count() == 0

    queued, scanned = journal_sync.resync_once(date_from=_recent(60)[:10])

    assert (queued, scanned) == (1, 1)
    assert journal_sync.pending_count() == 1           # 다시 보낼 대기 상태
    row = _outbox(db)[0]
    assert row['synced_at'] is None and row['attempts'] == 0


def test_resync_honours_the_requested_period(db):
    """기간 밖의 체결까지 되보내면 운용자가 지정한 범위의 의미가 없어진다."""
    _fill(db, odno='0000000001', custom_time='2026-05-01 09:30:00')   # 범위 밖
    _fill(db, odno='0000000002', custom_time='2026-07-15 09:30:00')   # 범위 안
    _mark_synced(db)

    queued, scanned = journal_sync.resync_once(date_from='2026-07-01',
                                               date_to='2026-07-31')

    assert (queued, scanned) == (1, 1)
    rows = {r['exec_id']: r for r in _outbox(db)}
    resent = [k for k, r in rows.items() if r['synced_at'] is None]
    assert len(resent) == 1 and '20260715' in resent[0]


def test_resync_end_date_covers_the_whole_day(db):
    """종료일을 2026-07-31 로 주면 그날 장중 체결이 빠지면 안 된다."""
    _fill(db, custom_time='2026-07-31 15:20:00')
    _mark_synced(db)

    assert journal_sync.resync_once(date_from='2026-07-01',
                                    date_to='2026-07-31') == (1, 1)


def test_resync_leaves_dead_lettered_rows_alone(db):
    """서버가 반복 거절한 건은 '지운 기록 복구'와 무관하다 — 되살리지 않는다."""
    _fill(db, custom_time=_recent(30))
    conn = db._get_conn()
    conn.execute("UPDATE journal_outbox SET synced_at = NULL, dead_at = ?, reject_count = 5",
                 (_recent(5),))
    conn.commit()

    assert journal_sync.resync_once(date_from=_recent(60)[:10]) == (0, 1)
    assert journal_sync.dead_count() == 1
    assert journal_sync.pending_count() == 0


def test_resync_marks_rows_as_backlog(db):
    """재동기화분이 실시간 체결보다 먼저 나가면 일지 반영이 몇 분씩 밀린다."""
    _fill(db, odno='0000000001', custom_time=_recent(30))
    _mark_synced(db)
    journal_sync.resync_once(date_from=_recent(60)[:10])

    # 재동기화 뒤에 실시간 체결이 하나 발생
    _fill(db, odno='0000000002', custom_time=_recent(1))

    backlog = {r['exec_id']: r['is_backlog'] for r in _outbox(db)}
    assert sorted(backlog.values()) == [0, 1]

    # 전송 순서: 실시간 체결이 먼저다
    first = journal_sync._fetch_pending()[0]
    assert backlog[first['exec_id']] == 0


def test_resync_is_noop_when_disabled(db, monkeypatch):
    monkeypatch.setattr(config.settings, 'JOURNAL_SYNC_USE', False)
    assert journal_sync.resync_once(date_from='2026-01-01') == (0, 0)


# ── 서버 지시(Ping 응답) 처리 ────────────────────────────────────────

def _ping_response(monkeypatch, body):
    """Ping 응답만 갈아끼우고, 보낸 요청 본문을 기록한다."""
    monkeypatch.setattr(journal_sync._tokens, '_token', 'tok')
    monkeypatch.setattr(journal_sync._tokens, '_expires_at', 1e18)
    monkeypatch.setattr(journal_sync, '_handled_command_id', None)
    monkeypatch.setattr(journal_sync, '_pending_ack', None)
    sent = []

    def fake_request(method, path, *, json_body=None, params=None, **kwargs):
        sent.append((path, json_body))
        if path == '/api/v1/bot/status':
            return _FakeResponse(200, body)
        return _FakeResponse(200, {'lastExecutedAt': None, 'count': 0})

    monkeypatch.setattr(journal_sync, '_request', fake_request)
    return sent


def test_ping_runs_resync_command_from_server(db, monkeypatch):
    """웹에서 누른 재동기화가 Ping 응답을 타고 봇까지 도달해야 한다."""
    _fill(db, custom_time=_recent(30))
    _mark_synced(db)
    sent = _ping_response(monkeypatch, {
        'status': 'success', 'command': 'resync', 'commandId': 7,
        'commandParams': {'from': _recent(60)[:10], 'to': None}})

    assert journal_sync.ping('running') is True
    assert journal_sync.pending_count() == 1          # 재동기화가 실제로 돌았다

    # 결과는 다음 Ping 에 실려 서버로 보고된다
    journal_sync.ping('running')
    ack = sent[-1][1]['commandAck']
    assert ack['id'] == 7 and ack['result'] == 'queued' and ack['count'] == 1


def test_same_command_is_not_run_twice(db, monkeypatch):
    """서버는 ack 받을 때까지 같은 명령을 계속 준다 — 10초마다 재실행하면 안 된다."""
    _fill(db, custom_time=_recent(30))
    _mark_synced(db)
    _ping_response(monkeypatch, {
        'status': 'success', 'command': 'resync', 'commandId': 7,
        'commandParams': {'from': _recent(60)[:10]}})

    calls = []
    real = journal_sync.resync_once
    monkeypatch.setattr(journal_sync, 'resync_once',
                        lambda *a, **kw: (calls.append(1), real(*a, **kw))[1])

    for _ in range(5):
        journal_sync.ping('running')

    assert len(calls) == 1


def test_pause_command_is_refused(db, monkeypatch):
    """웹서버가 매매봇을 멈추게 하는 지시는 구현하지 않는다."""
    _ping_response(monkeypatch, {
        'status': 'success', 'command': 'pause', 'commandId': 9})

    ran = []
    monkeypatch.setattr(journal_sync, 'resync_once',
                        lambda *a, **kw: (ran.append(1), (0, 0))[1])

    assert journal_sync.ping('running') is True
    assert ran == []
    assert journal_sync._pending_ack is None      # ack 도 보내지 않는다(미구현이므로)


def test_command_without_id_is_refused(db, monkeypatch):
    """commandId 가 없으면 중복 실행을 막을 수 없다 — 실행하지 않는다."""
    _ping_response(monkeypatch, {'status': 'success', 'command': 'resync'})

    ran = []
    monkeypatch.setattr(journal_sync, 'resync_once',
                        lambda *a, **kw: (ran.append(1), (0, 0))[1])

    journal_sync.ping('running')
    assert ran == []


def test_ping_without_command_is_unchanged(db, monkeypatch):
    """평소 Ping 은 예전과 똑같이 동작해야 한다."""
    sent = _ping_response(monkeypatch, {'status': 'success', 'command': 'none'})

    assert journal_sync.ping('running') is True
    assert 'commandAck' not in sent[0][1]


def test_resync_failure_is_reported_back(db, monkeypatch):
    """봇이 조용히 실패하면 운용자는 영영 복구된 줄 안다."""
    _ping_response(monkeypatch, {
        'status': 'success', 'command': 'resync', 'commandId': 3,
        'commandParams': {'from': '2026-01-01'}})

    def boom(*a, **kw):
        raise sqlite3.OperationalError('disk I/O error')

    monkeypatch.setattr(journal_sync, 'resync_once', boom)
    journal_sync.ping('running')

    assert journal_sync._pending_ack['result'] == 'failed'
    assert 'disk I/O error' in journal_sync._pending_ack['message']


# ── dead-letter (전송 포기) ───────────────────────────────────────────

def _clear_backoff(db):
    """백오프를 무시하고 곧바로 다음 전송을 시도하게 만든다."""
    conn = db._get_conn()
    conn.execute("UPDATE journal_outbox SET last_attempt_at = NULL")
    conn.commit()


def test_transport_failure_never_dead_letters(db, monkeypatch):
    """웹서버가 오래 죽어 있어도 대기열을 버리면 안 된다.

    통신 실패까지 포기 횟수로 세면 주말 내내 서버가 내려간 것만으로 그 사이의
    체결이 통째로 폐기된다. 이 시스템에서 가장 피해야 할 사고다.
    """
    _fill(db)
    _patch_transport(monkeypatch, None)

    for _ in range(journal_sync._MAX_REJECTS * 3):
        _clear_backoff(db)
        journal_sync.flush_once()

    row = _outbox(db)[0]
    assert row['dead_at'] is None
    assert row['reject_count'] == 0
    assert journal_sync.pending_count() == 1


def test_repeated_server_rejection_is_dead_lettered(db, monkeypatch):
    """서버가 사유를 붙여 거절하는 건은 재시도해도 결과가 같다 — 큐에서 뺀다."""
    _fill(db)
    _patch_transport(monkeypatch, _FakeResponse(200, {
        'inserted': 0, 'skipped': 0, 'failed': 1,
        'results': [{'index': 0, 'status': 'failed',
                     'errorCode': 'VALIDATION_ERROR', 'error': '가격이 필요합니다'}]}))

    for _ in range(journal_sync._MAX_REJECTS):
        _clear_backoff(db)
        journal_sync.flush_once()

    row = _outbox(db)[0]
    assert row['dead_at'] is not None
    assert row['reject_count'] == journal_sync._MAX_REJECTS
    assert journal_sync.pending_count() == 0      # 더는 배치 앞자리를 잡지 않는다
    assert journal_sync.dead_count() == 1
    assert 'VALIDATION_ERROR' in row['last_error']


def test_dead_letter_frees_the_queue_behind_it(db, monkeypatch):
    """독약 한 건이 뒤에 쌓인 정상 건까지 막지 않아야 한다."""
    _fill(db, odno='0000000001')          # 서버가 영구 거절할 건
    _fill(db, odno='0000000002')          # 정상 건

    poison = _outbox(db)[0]['exec_id']

    def respond(method, path, *, json_body=None, **kwargs):
        results = []
        for i, item in enumerate(json_body['trades']):
            if item['brokerExecutionId'] == poison:
                results.append({'index': i, 'status': 'failed',
                                'brokerExecutionId': item['brokerExecutionId'],
                                'errorCode': 'VALIDATION_ERROR', 'error': '거절'})
            else:
                results.append({'index': i, 'status': 'created', 'id': 'ok',
                                'brokerExecutionId': item['brokerExecutionId']})
        return _FakeResponse(201, {'inserted': 1, 'results': results})

    monkeypatch.setattr(journal_sync._tokens, '_token', 'tok')
    monkeypatch.setattr(journal_sync._tokens, '_expires_at', 1e18)
    monkeypatch.setattr(journal_sync, '_request', respond)

    journal_sync.flush_once()
    assert journal_sync.pending_count() == 1      # 정상 건은 이미 나갔다

    for _ in range(journal_sync._MAX_REJECTS):
        _clear_backoff(db)
        journal_sync.flush_once()

    assert journal_sync.pending_count() == 0
    assert journal_sync.dead_count() == 1


# ── 배치 전체 거절 시 분할 재시도 ─────────────────────────────────────

def test_whole_batch_rejection_is_bisected_to_the_offender(db, monkeypatch):
    """묶음 전체가 4xx 로 튕기면 반씩 쪼개 진범만 남기고 나머지는 보낸다."""
    for i in range(4):
        _fill(db, odno=f'000000000{i}')
    poison = _outbox(db)[1]['exec_id']

    def respond(method, path, *, json_body=None, **kwargs):
        trades = json_body['trades']
        if any(t['brokerExecutionId'] == poison for t in trades):
            # 서버가 항목별 결과 없이 요청 전체를 거절하는 상황
            return _FakeResponse(400, {'error': 'BAD_REQUEST'})
        return _FakeResponse(201, {
            'inserted': len(trades),
            'results': [{'index': i, 'status': 'created', 'id': str(i),
                         'brokerExecutionId': t['brokerExecutionId']}
                        for i, t in enumerate(trades)]})

    monkeypatch.setattr(journal_sync._tokens, '_token', 'tok')
    monkeypatch.setattr(journal_sync._tokens, '_expires_at', 1e18)
    monkeypatch.setattr(journal_sync, '_request', respond)

    ok, fail = journal_sync.flush_once()

    assert (ok, fail) == (3, 1)               # 진범 1건만 실패
    rows = {r['exec_id']: r for r in _outbox(db)}
    assert rows[poison]['reject_count'] == 1  # 분할로 특정됐으니 거절로 센다
    assert all(r['synced_at'] for k, r in rows.items() if k != poison)


def test_server_error_is_not_bisected(db, monkeypatch):
    """5xx 는 이 건의 문제가 아니다 — 쪼개지 말고 통째로 다시 보낸다."""
    for i in range(4):
        _fill(db, odno=f'000000000{i}')
    calls = _patch_transport(monkeypatch, _FakeResponse(503, {'error': 'down'}))

    ok, fail = journal_sync.flush_once()

    assert (ok, fail) == (0, 4)
    assert len(calls) == 1                    # 분할 재시도를 하지 않았다
    assert all(r['reject_count'] == 0 for r in _outbox(db))


# ── 응답 매핑 ─────────────────────────────────────────────────────────

def test_results_are_matched_by_exec_id_not_position(db, monkeypatch):
    """순서가 어긋난 응답을 위치로 믿으면 엉뚱한 행이 전송 완료로 표시된다."""
    _fill(db, odno='0000000001')
    _fill(db, odno='0000000002')
    first, second = [r['exec_id'] for r in _outbox(db)]

    # 서버가 순서를 뒤집어 돌려준 상황 — 두 번째 건만 실패다.
    _patch_transport(monkeypatch, _FakeResponse(201, {
        'inserted': 1, 'results': [
            {'index': 1, 'status': 'failed', 'brokerExecutionId': second,
             'errorCode': 'VALIDATION_ERROR', 'error': '거절'},
            {'index': 0, 'status': 'created', 'id': '11', 'brokerExecutionId': first},
        ]}))

    assert journal_sync.flush_once() == (1, 1)
    rows = {r['exec_id']: r for r in _outbox(db)}
    assert rows[first]['remote_id'] == '11'
    assert rows[first]['synced_at'] is not None
    assert rows[second]['synced_at'] is None


def test_mismatched_index_without_exec_id_is_not_trusted(db, monkeypatch):
    """멱등키도 없고 index 도 어긋나면 짝을 지을 수 없다 — 재전송에 맡긴다."""
    _fill(db)
    _patch_transport(monkeypatch, _FakeResponse(201, {
        'inserted': 1, 'results': [{'index': 7, 'status': 'created', 'id': '99'}]}))

    assert journal_sync.flush_once() == (0, 1)
    assert journal_sync.pending_count() == 1
    assert _outbox(db)[0]['reject_count'] == 0   # 서버 거절이 아니므로 세지 않는다


# ── 대기열 정리 (retention) ───────────────────────────────────────────

def test_purge_removes_only_old_synced_rows(db, monkeypatch):
    """라파 SD카드 보호. 미전송·dead-letter 행은 건드리면 안 된다."""
    for i in range(3):
        _fill(db, odno=f'000000000{i}')
    conn = db._get_conn()
    conn.execute("UPDATE journal_outbox SET synced_at = '2020-01-01 00:00:00' WHERE id = 1")
    conn.execute("UPDATE journal_outbox SET synced_at = datetime('now') WHERE id = 2")
    conn.execute("UPDATE journal_outbox SET dead_at = '2020-01-01 00:00:00' WHERE id = 3")
    conn.commit()

    assert journal_sync.purge_synced(days=30) == 1

    remaining = {r['id'] for r in _outbox(db)}
    assert remaining == {2, 3}   # 최근 전송분과 dead-letter 는 남는다


def test_retention_outlives_the_backfill_window():
    """보존 기간이 백필 스캔 범위보다 짧으면 이미 보낸 건을 매번 다시 주워 담는다."""
    assert journal_sync._RETENTION_DAYS > journal_sync._BACKFILL_LOOKBACK_DAYS


# ── 백필 (큐에 들어가지 못한 체결 회수) ───────────────────────────────

def _recent(minutes_ago):
    from datetime import datetime, timedelta
    return (datetime.now(journal_sync.KST)
            - timedelta(minutes=minutes_ago)).strftime('%Y-%m-%d %H:%M:%S')


def _patch_backfill_transport(monkeypatch, last_sync=None):
    """last-sync 조회만 응답하고 배치 전송은 하지 않는 전송 계층."""
    monkeypatch.setattr(journal_sync._tokens, '_token', 'tok')
    monkeypatch.setattr(journal_sync._tokens, '_expires_at', 1e18)
    calls = []

    def fake_request(method, path, *, json_body=None, params=None, **kwargs):
        calls.append((method, path, params))
        if path == '/api/v1/trades/last-sync':
            return _FakeResponse(200, last_sync if last_sync is not None else
                                 {'lastExecutedAt': None, 'lastBrokerExecutionId': None,
                                  'count': 0})
        return _FakeResponse(201, {'inserted': 0, 'results': []})

    monkeypatch.setattr(journal_sync, '_request', fake_request)
    return calls


def test_backfill_recovers_fills_made_while_integration_was_off(db, monkeypatch):
    """연동이 꺼져 있던 동안의 체결은 큐에 없다 — 큐 재시도로는 영영 복구되지 않는다."""
    monkeypatch.setattr(config.settings, 'JOURNAL_SYNC_USE', False)
    _fill(db, custom_time=_recent(30))
    assert _outbox(db) == []          # 적재 자체가 일어나지 않았다

    monkeypatch.setattr(config.settings, 'JOURNAL_SYNC_USE', True)
    _patch_backfill_transport(monkeypatch)

    assert journal_sync.backfill_once() == (1, 1)
    assert journal_sync.pending_count() == 1
    assert _outbox(db)[0]['payload']


def test_backfill_asks_the_server_with_source_scope(db, monkeypatch):
    """source 를 빼면 웹에서 손으로 넣은 기록까지 섞여 구간이 통째로 건너뛰어진다."""
    calls = _patch_backfill_transport(monkeypatch)
    journal_sync.backfill_once()

    method, path, params = calls[0]
    assert (method, path) == ('GET', '/api/v1/trades/last-sync')
    assert params['source'] == 'my-stock-hts'
    assert params['isSimulated'] == 'false'


def test_backfill_does_not_duplicate_rows_already_queued(db, monkeypatch):
    """정상 경로로 이미 큐에 있는 건을 다시 넣으면 안 된다."""
    _fill(db, custom_time=_recent(30))
    assert len(_outbox(db)) == 1

    _patch_backfill_transport(monkeypatch)
    assert journal_sync.backfill_once() == (0, 1)   # 스캔은 했지만 회수 0건
    assert len(_outbox(db)) == 1


def test_backfill_converts_server_utc_to_local_kst(db, monkeypatch):
    """서버는 UTC, 로컬 trades.time 은 KST — 변환을 빠뜨리면 9시간이 어긋난다."""
    from datetime import datetime, timedelta, timezone
    utc_point = datetime.now(timezone.utc) - timedelta(hours=2)

    since = journal_sync._backfill_since(
        {'lastExecutedAt': utc_point.strftime('%Y-%m-%dT%H:%M:%S%z')})
    expected = (utc_point.astimezone(journal_sync.KST)
                - timedelta(minutes=journal_sync._BACKFILL_OVERLAP_MIN))

    assert since == expected.strftime('%Y-%m-%d %H:%M:%S')


def test_backfill_falls_back_to_lookback_when_server_is_empty(db):
    """서버에 기록이 없으면 기본 조회 범위로 거슬러 올라간다."""
    from datetime import datetime, timedelta
    since = journal_sync._backfill_since({'lastExecutedAt': None})
    expected = (datetime.now(journal_sync.KST)
                - timedelta(days=journal_sync._BACKFILL_LOOKBACK_DAYS))

    assert since[:10] == expected.strftime('%Y-%m-%d')


def test_backfill_ignores_non_fill_rows(db, monkeypatch):
    """접수·취소는 체결이 아니다 — 백필도 전송 대상 판정을 똑같이 따른다."""
    monkeypatch.setattr(config.settings, 'JOURNAL_SYNC_USE', False)
    _fill(db, order_status='접수', custom_time=_recent(30))
    _fill(db, order_status='취소', odno='0000000099', custom_time=_recent(30))

    monkeypatch.setattr(config.settings, 'JOURNAL_SYNC_USE', True)
    _patch_backfill_transport(monkeypatch)

    assert journal_sync.backfill_once() == (0, 0)
    assert _outbox(db) == []


def test_backfill_is_noop_when_disabled(db, monkeypatch):
    monkeypatch.setattr(config.settings, 'JOURNAL_SYNC_USE', False)
    calls = _patch_backfill_transport(monkeypatch)

    assert journal_sync.backfill_once() == (0, 0)
    assert calls == []                 # 서버를 건드리지도 않는다


def test_backfill_gives_up_quietly_without_read_scope(db, monkeypatch):
    """trades:read 없는 키면 백필은 못 하지만 전송은 계속돼야 한다."""
    monkeypatch.setattr(journal_sync._tokens, '_token', 'tok')
    monkeypatch.setattr(journal_sync._tokens, '_expires_at', 1e18)
    monkeypatch.setattr(journal_sync, '_request',
                        lambda *a, **kw: _FakeResponse(403, {'error': 'forbidden'}))

    # 조회에 실패하면 기본 범위로라도 스캔한다 — 예외로 워커를 끊지 않는다.
    assert journal_sync.backfill_once() == (0, 0)


# ── 서버와의 계약 고정 ────────────────────────────────────────────────
#  아래 두 본문은 stock-memo 의 실제 /api/v1/bot/status 응답을 그대로 옮긴 것이다.
#  양쪽 저장소가 따로 움직이므로, 필드 이름이 어긋나면 재동기화 버튼이 조용히
#  죽는다(봇이 명령을 못 알아듣고 아무 일도 하지 않는다). 그 순간을 여기서 잡는다.

_SERVER_PING_IDLE = {
    "command": "none",
    "nextPingSeconds": 10,
    "status": "success",
    "updatedAt": "2026-08-02T21:59:51+09:00",
}

_SERVER_PING_WITH_RESYNC = {
    "command": "resync",
    "commandId": 1,
    "commandParams": {"from": "2026-05-04", "to": None},
    "nextPingSeconds": 10,
    "status": "success",
    "updatedAt": "2026-08-02T21:59:51+09:00",
}


def test_real_server_idle_response_asks_for_nothing(db):
    assert journal_sync._handle_command(_SERVER_PING_IDLE) is None


def test_real_server_resync_response_is_understood(db, monkeypatch):
    """서버가 실제로 내려보내는 본문을 봇이 해석하지 못하면 버튼이 죽는다."""
    seen = {}
    monkeypatch.setattr(journal_sync, 'resync_once',
                        lambda f=None, t=None: (seen.update(f=f, t=t), (3, 9))[1])

    ack = journal_sync._handle_command(_SERVER_PING_WITH_RESYNC)

    assert seen == {'f': '2026-05-04', 't': None}      # 기간이 그대로 전달된다
    assert ack['id'] == 1
    assert ack['result'] == 'queued'
    assert ack['count'] == 3


def test_ack_shape_matches_what_the_server_stores(db, monkeypatch):
    """서버는 ack 에서 id/result/count/message 를 읽는다 — 이름이 어긋나면 안 된다."""
    monkeypatch.setattr(journal_sync, 'resync_once', lambda f=None, t=None: (2, 5))
    ack = journal_sync._handle_command(_SERVER_PING_WITH_RESYNC)

    assert set(ack) == {'id', 'result', 'count', 'message'}
    assert isinstance(ack['id'], int) and isinstance(ack['count'], int)
    assert ack['result'] in ('queued', 'skipped', 'failed')


def test_resync_rebuilds_the_payload(db):
    """큐에는 적재 시점의 JSON 이 통째로 들어 있다.

    그대로 다시 보내면 그동안 고친 표현·필드가 반영되지 않는다. 실제로 메모 형식을
    바꾼 뒤 재동기화를 걸었는데 옛 문구가 그대로 나가는 일이 있었다.
    """
    _submit(db, reason='[추세매수] 조건 만족', custom_time=_recent(35))
    _fill(db, reason='체결 확인', custom_time=_recent(30))
    _mark_synced(db)

    # 큐에 저장된 페이로드를 옛 형식으로 되돌려 놓는다
    conn = db._get_conn()
    stale = json.loads(_outbox(db)[0]['payload'])
    stale['memo'] = '옛 형식 한 줄짜리 메모'
    conn.execute("UPDATE journal_outbox SET payload = ?",
                 (json.dumps(stale, ensure_ascii=False),))
    conn.commit()

    journal_sync.resync_once(date_from=_recent(60)[:10])

    assert _memo_lines(db) == ['[추세매수] 조건 만족', '· 체결 확인']


def test_resync_does_not_revive_dead_letter_via_upsert(db):
    """페이로드를 덮어쓰는 경로에서도 dead-letter 는 그대로 둬야 한다."""
    _fill(db, custom_time=_recent(30))
    conn = db._get_conn()
    conn.execute("UPDATE journal_outbox SET synced_at = NULL, dead_at = ?, reject_count = 5",
                 (_recent(5),))
    conn.commit()

    assert journal_sync.resync_once(date_from=_recent(60)[:10]) == (0, 1)
    assert journal_sync.dead_count() == 1
    assert journal_sync.pending_count() == 0
