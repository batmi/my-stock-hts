"""연동 시작 시점의 보유 잔고를 서버에 심는가 (기초잔고).

연동 이전부터 들고 있던 종목은 서버에 매수 기록이 없다. 그러면 그 종목의 첫 매도가
'매수 기록 없음'으로 찍히고(needsReview) 보유 수량 집계가 음수로 내려간다.
서버에 `/api/v1/positions/opening` 이 있는 이유가 그것인데, HTS 에 부르는 쪽이 없었다.

**두 번 보내면 첫 문제보다 나쁘다** — 서버 멱등키에 날짜가 박혀 있어(OPENING:{env}:{날짜}:{종목})
다른 날 또 보내면 같은 종목의 기초잔고가 하나 더 생긴다. 없는 매수를 만드는 것이다.
그래서 여기 시험의 절반은 '한 번만 보내는가'에 쓴다.
"""
import pytest

import api
import config
from modules import db_manager, journal_sync


@pytest.fixture
def journal_on(monkeypatch):
    monkeypatch.setattr(config, 'JOURNAL_API_URL', 'https://example.invalid', raising=False)
    monkeypatch.setattr(config, 'JOURNAL_API_KEY', 'skm_dummy', raising=False)
    monkeypatch.setattr(config.settings, 'JOURNAL_SYNC_USE', True, raising=False)
    monkeypatch.setattr(config.session, 'is_paper', False, raising=False)
    monkeypatch.setattr(config.session, 'cano', '99887766', raising=False)
    monkeypatch.setattr(config.session, 'acnt_prdt_cd', '01', raising=False)
    db_manager.db.get_connection().execute("DELETE FROM journal_opening")
    db_manager.db.get_connection().commit()
    yield


class _Res:
    def __init__(self, status=201, payload=None):
        self.status_code = status
        self._payload = payload if payload is not None else {'inserted': 1}
        self.text = str(self._payload)

    def json(self):
        return self._payload


def _balance(*items):
    return {'rt_cd': '0', 'output1': list(items), 'output2': [{}]}


def _holding(code, qty=10, avg=70000.0, name='종목'):
    return {'pdno': code, 'prdt_name': name, 'hldg_qty': str(qty),
            'pchs_avg_pric': str(avg)}


@pytest.fixture
def stub_balances(monkeypatch):
    monkeypatch.setattr(api, 'get_overseas_balance', lambda *a, **k: {'rt_cd': '0', 'output1': []})


def test_holdings_without_local_buys_are_seeded(journal_on, stub_balances, monkeypatch):
    monkeypatch.setattr(api, 'get_domestic_balance',
                        lambda *a, **k: _balance(_holding('005930', 30, 70000.0, '삼성전자')))
    sent = []
    monkeypatch.setattr(journal_sync, '_request',
                        lambda m, p, **kw: sent.append((m, p, kw.get('json_body'))) or _Res())

    assert journal_sync.opening_once() == 1
    assert sent and sent[0][1] == '/api/v1/positions/opening'
    body = sent[0][2]
    assert [p['symbol'] for p in body['positions']] == ['005930']
    assert body['positions'][0]['volume'] == 30
    assert body['positions'][0]['avgPrice'] == 70000.0
    assert body['isSimulated'] is False


def test_holdings_with_local_buys_are_not_seeded(journal_on, stub_balances, monkeypatch):
    """로컬에 매수 기록이 있으면 그 매수가 서버에 닿는다 — 씨앗을 더하면 매수가 둘이 된다."""
    db_manager.db.insert_trade("매수(AUTO)", "102780", "KODEX", 5, 25500.0, "OPEN0001",
                               order_status="체결")
    monkeypatch.setattr(api, 'get_domestic_balance',
                        lambda *a, **k: _balance(_holding('102780', 5, 25500.0, 'KODEX')))
    sent = []
    monkeypatch.setattr(journal_sync, '_request',
                        lambda m, p, **kw: sent.append((m, p)) or _Res())

    assert journal_sync.opening_once() == 0
    assert not sent, "이미 매수 기록이 있는 종목에 기초잔고를 심었다"


def test_sent_only_once_per_account(journal_on, stub_balances, monkeypatch):
    """날짜가 바뀌면 멱등키도 바뀐다 — 두 번째 전송은 없는 매수를 만든다."""
    monkeypatch.setattr(api, 'get_domestic_balance',
                        lambda *a, **k: _balance(_holding('005930')))
    calls = []
    monkeypatch.setattr(journal_sync, '_request',
                        lambda m, p, **kw: calls.append(p) or _Res())

    journal_sync.opening_once()
    journal_sync.opening_once()
    journal_sync.opening_once()
    assert len(calls) == 1, f"기초잔고를 {len(calls)}번 보냈다"


def test_failed_send_is_retried_next_time(journal_on, stub_balances, monkeypatch):
    """전송 실패는 표시를 남기지 않는다 — 남기면 그 계좌는 영영 씨앗을 못 받는다."""
    monkeypatch.setattr(api, 'get_domestic_balance',
                        lambda *a, **k: _balance(_holding('005930')))
    calls = []

    def _fail(m, p, **kw):
        calls.append(p)
        return None

    monkeypatch.setattr(journal_sync, '_request', _fail)
    journal_sync.opening_once()
    journal_sync.opening_once()
    assert len(calls) == 2, "실패했는데 '보냄'으로 표시되어 재시도가 막혔다"


def test_balance_query_failure_does_not_mark_done(journal_on, stub_balances, monkeypatch):
    """조회 실패와 '보유 없음'을 구분해야 한다 — 섞으면 빈 계좌로 오인해 표시가 박힌다."""
    monkeypatch.setattr(api, 'get_domestic_balance',
                        lambda *a, **k: {'rt_cd': '1', 'msg1': '조회 실패'})
    monkeypatch.setattr(journal_sync, '_request',
                        lambda *a, **k: pytest.fail("조회 실패인데 전송했다"))
    assert journal_sync.opening_once() == 0

    # 표시가 박히지 않았으므로, 조회가 회복되면 그때 보낸다.
    monkeypatch.setattr(api, 'get_domestic_balance',
                        lambda *a, **k: _balance(_holding('005930')))
    sent = []
    monkeypatch.setattr(journal_sync, '_request',
                        lambda m, p, **kw: sent.append(p) or _Res())
    journal_sync.opening_once()
    assert sent == ['/api/v1/positions/opening']


def test_empty_account_is_marked_done(journal_on, stub_balances, monkeypatch):
    """보유가 없으면 심을 것도 없다 — 매 주기 잔고를 묻지 않도록 결론을 남긴다."""
    monkeypatch.setattr(api, 'get_domestic_balance', lambda *a, **k: _balance())
    asked = []
    monkeypatch.setattr(api, 'get_domestic_balance',
                        lambda *a, **k: asked.append(1) or _balance())
    monkeypatch.setattr(journal_sync, '_request',
                        lambda *a, **k: pytest.fail("보낼 것이 없는데 전송했다"))

    journal_sync.opening_once()
    journal_sync.opening_once()
    assert len(asked) == 1, "결론을 남기지 않아 매번 잔고를 다시 물었다"


def test_disabled_journal_sends_nothing(monkeypatch):
    monkeypatch.setattr(config.settings, 'JOURNAL_SYNC_USE', False, raising=False)
    monkeypatch.setattr(journal_sync, '_request',
                        lambda *a, **k: pytest.fail("연동이 꺼져 있는데 전송했다"))
    assert journal_sync.opening_once() == 0
