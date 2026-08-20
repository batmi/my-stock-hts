"""가상투자(mode 4) 체결 확정 대사 검증.

[배경] 가상 주문은 증권사에 나가지 않으므로 미체결 목록에도, KIS 체결내역 API에도
잡히지 않는다. 종전에는 체결 감시가 관찰 모드에서 통째로 조기 반환해, 주문이
paper_broker 원장에서는 이미 체결됐는데도 트레이더 상태기계는 ORDER_SENT에
멈춰 있었다. 그 결과 ① 거래 히스토리가 '접수'로 끝나고 ② is_pending이 True로
굳어 해당 종목이 손절 판정에서 빠지며 ③ 고아 주문 경보가 떴다
(2026-08-05 실측: 018260·035420).
"""
import os
import tempfile
import threading
from unittest.mock import patch

import pytest

import api
import config
import modules.auto_trade as auto_trade
from modules import db_manager, paper_broker, trading_cost
from modules.auto_trade.common import OrderStatus


class _FakeOrderManager:
    def __init__(self):
        self.pending_orders = {}
        self._lock = threading.RLock()


class _FakeTrader:
    """체결 대사가 건드리는 표면만 흉내낸다(AutoTrader 싱글턴 기동 회피)."""

    def __init__(self):
        self.order_manager = _FakeOrderManager()
        self.updates = []

    def update_order_status(self, code, odno, status):
        self.updates.append((code, odno, status))


@pytest.fixture
def paper(monkeypatch):
    tmpdir = tempfile.mkdtemp()
    path = os.path.join(tmpdir, "paper_conclusion.db")
    original_path = db_manager.db.db_path
    monkeypatch.setattr(config, 'PAPER_DB_FILE_PATH', path, raising=False)
    monkeypatch.setattr(config, 'PAPER_SEED_CAPITAL', 5_000_000, raising=False)
    monkeypatch.setattr(config.session, 'is_paper', True, raising=False)
    db_manager.db.switch_path(path)
    paper_broker.init_tables()
    monkeypatch.setattr(paper_broker, '_current_price',
                        lambda code, fallback=0.0: {'005930': 70000}.get(code, fallback or 0))
    yield paper_broker
    db_manager.db.close_all_connections()
    db_manager.db.switch_path(original_path)


@pytest.fixture
def monitor(monkeypatch):
    """알림·부가 조회를 차단한 체결 감시 인스턴스."""
    m = auto_trade.ConclusionMonitor()
    m.processed_sim_fills = set()
    m.paper_backfill_done = False
    monkeypatch.setattr('modules.auto_trade.conclusion.is_system_odno', lambda odno: True)
    monkeypatch.setattr(api, 'get_current_price_data',
                        lambda *a, **k: {'rt_cd': '1', 'output': {}})
    return m


def test_paper_fill_closes_pending_order(paper, monitor, monkeypatch):
    """원장에 체결이 있으면 대기 주문이 FILLED로 닫히고 '체결' 이력이 남는다."""
    res = api.place_order("domestic", "buy", "005930", 2, 70000, "00")
    odno = res['output']['ODNO']

    trader = _FakeTrader()
    trader.order_manager.pending_orders['005930'] = {odno: OrderStatus.ORDER_SENT}
    monkeypatch.setattr(auto_trade, 'AutoTrader', lambda: trader)
    monkeypatch.setattr(db_manager.db, 'get_trade_by_odno',
                        lambda o: {'type': 'buy(AUTO)', 'name': '삼성전자', 'qty': 2,
                                   'price': 70000, 'reason': '조건 만족'})
    monkeypatch.setattr(db_manager.db, 'check_trade_exists', lambda *a, **k: False)

    inserted = []
    monkeypatch.setattr(db_manager.db, 'insert_trade',
                        lambda *a, **k: inserted.append((a, k)))
    sent = []
    monkeypatch.setattr(api, 'send_telegram_message', lambda msg, **k: sent.append(msg))

    assert monitor._check_conclusions() == (False, False)

    assert trader.updates == [('005930', odno, OrderStatus.FILLED)]
    assert inserted, "체결 이력이 기록되지 않았다"
    # 원장 대사는 확정 체결이다 — '(추정)' 라벨이 붙으면 안 된다.
    assert inserted[0][1]['order_status'] == "체결"
    assert sent and "체결" in sent[0] and "추정" not in sent[0]


def test_paper_fill_uses_ledger_price(paper, monitor, monkeypatch):
    """체결가는 주문 지정가가 아니라 원장의 실제 체결가를 쓴다(시장가 주문 대비)."""
    res = api.place_order("domestic", "buy", "005930", 1, 0, "01")  # 시장가 → 70,000 체결
    odno = res['output']['ODNO']

    trader = _FakeTrader()
    trader.order_manager.pending_orders['005930'] = {odno: OrderStatus.ORDER_SENT}
    monkeypatch.setattr(auto_trade, 'AutoTrader', lambda: trader)
    monkeypatch.setattr(db_manager.db, 'get_trade_by_odno',
                        lambda o: {'type': 'buy(AUTO)', 'name': '삼성전자', 'qty': 1,
                                   'price': 0, 'reason': '조건 만족'})
    monkeypatch.setattr(db_manager.db, 'check_trade_exists', lambda *a, **k: False)
    inserted = []
    monkeypatch.setattr(db_manager.db, 'insert_trade',
                        lambda *a, **k: inserted.append((a, k)))
    monkeypatch.setattr(api, 'send_telegram_message', lambda msg, **k: None)

    monitor._check_conclusions()

    # insert_trade(type, code, name, qty, price, ...) — 체결가에는 슬리피지가 붙는다(2026-08-10).
    # 요점은 '원장에 적힌 체결가를 그대로 쓴다'이지 주문가와 같다는 것이 아니다.
    assert inserted[0][0][4] == pytest.approx(trading_cost.apply_slippage(70000, 'buy'))


def test_paper_fill_updates_order_row_price(paper, monitor, monkeypatch):
    """접수 행의 단가도 체결가로 갱신된다 — 실체결 경로와 같은 규약.

    [배경] 성과 리포트(9-4)는 접수·체결 두 행을 (거래일, odno)로 병합하는데, 체결가를
     채택하는 것은 접수 단가가 0(시장가)일 때뿐이다. 지정가 주문은 접수 단가가 그대로
     남아, 관찰모드 리포트의 단가·매매금액이 주문가로 굳었다(가상 브로커는 슬리피지를
     얹어 체결하므로 지정가 주문도 항상 어긋난다). 실체결 경로는 update_trade로 이미
     갱신하고 있었다 — 관찰모드만 빠져 있었던 파리티 결함이다.
    """
    res = api.place_order("domestic", "buy", "005930", 1, 70000, "00")  # 지정가
    odno = res['output']['ODNO']
    fill_price = trading_cost.apply_slippage(70000, 'buy')
    assert fill_price != 70000, "슬리피지가 0이면 이 테스트가 아무것도 지키지 못한다"

    trader = _FakeTrader()
    trader.order_manager.pending_orders['005930'] = {odno: OrderStatus.ORDER_SENT}
    monkeypatch.setattr(auto_trade, 'AutoTrader', lambda: trader)
    monkeypatch.setattr(db_manager.db, 'get_trade_by_odno',
                        lambda o: {'type': 'buy(AUTO)', 'name': '삼성전자', 'qty': 1,
                                   'price': 70000, 'reason': '조건 만족'})
    monkeypatch.setattr(db_manager.db, 'check_trade_exists', lambda *a, **k: False)
    monkeypatch.setattr(db_manager.db, 'insert_trade', lambda *a, **k: None)
    monkeypatch.setattr(api, 'send_telegram_message', lambda msg, **k: None)

    updates = []
    monkeypatch.setattr(db_manager.db, 'update_trade',
                        lambda o, **k: updates.append((o, k)))

    monitor._check_conclusions()

    assert updates, "접수 행의 단가가 갱신되지 않았다"
    assert updates[0][0] == odno
    assert updates[0][1]['price'] == pytest.approx(fill_price)


def test_paper_pending_without_ledger_stays_open(paper, monitor, monkeypatch):
    """원장에 없는 주문은 건드리지 않는다(주문 기록 큐 지연 시 오탐 방지)."""
    trader = _FakeTrader()
    trader.order_manager.pending_orders['005930'] = {"P_NOT_FILLED": OrderStatus.ORDER_SENT}
    monkeypatch.setattr(auto_trade, 'AutoTrader', lambda: trader)
    monkeypatch.setattr(api, 'send_telegram_message', lambda msg, **k: None)

    monitor._check_conclusions()

    assert trader.updates == []


def test_paper_restart_backfills_today_ledger(paper, monitor, monkeypatch):
    """재기동으로 pending이 비어도 당일 원장의 체결은 1회 복구된다."""
    res = api.place_order("domestic", "buy", "005930", 2, 70000, "00")
    odno = res['output']['ODNO']

    trader = _FakeTrader()  # pending 비어 있음 = 재기동 직후
    monkeypatch.setattr(auto_trade, 'AutoTrader', lambda: trader)
    monkeypatch.setattr(db_manager.db, 'get_trade_by_odno',
                        lambda o: {'type': 'buy(AUTO)', 'name': '삼성전자', 'qty': 2,
                                   'price': 70000, 'reason': '조건 만족'})
    monkeypatch.setattr(db_manager.db, 'check_trade_exists', lambda *a, **k: False)
    inserted = []
    monkeypatch.setattr(db_manager.db, 'insert_trade',
                        lambda *a, **k: inserted.append((a, k)))
    monkeypatch.setattr(api, 'send_telegram_message', lambda msg, **k: None)

    monitor._check_conclusions()
    assert len(inserted) == 1 and inserted[0][0][5] == odno

    # 2회차부터는 원장을 다시 훑지 않는다(매 주기 DB 스캔 방지).
    monitor.processed_sim_fills = set()
    monitor._check_conclusions()
    assert len(inserted) == 1


def _run_backfill_with_trade_type(paper, monitor, monkeypatch, trade_type):
    """재기동 직후(_SYSTEM_ODNOS 비어 있음) 원장 백필을 한 번 돌리고 제한 등록을 수집한다."""
    api.place_order("domestic", "buy", "005930", 2, 70000, "00")

    trader = _FakeTrader()  # pending 비어 있음 = 재기동 직후
    monkeypatch.setattr(auto_trade, 'AutoTrader', lambda: trader)
    monkeypatch.setattr(db_manager.db, 'get_trade_by_odno',
                        lambda o: {'type': trade_type, 'name': '삼성전자', 'qty': 2,
                                   'price': 70000, 'reason': '조건 만족'})
    monkeypatch.setattr(db_manager.db, 'check_trade_exists', lambda *a, **k: False)
    monkeypatch.setattr(db_manager.db, 'insert_trade', lambda *a, **k: None)
    monkeypatch.setattr(api, 'send_telegram_message', lambda msg, **k: None)
    # 재기동하면 시스템 ODNO 메모리 세트는 비어 있다 — 그 상태를 그대로 재현한다.
    monkeypatch.setattr('modules.auto_trade.common.is_system_odno', lambda odno: False)
    monkeypatch.setattr('modules.auto_trade.conclusion.is_system_odno', lambda odno: False)

    restricted = []
    monkeypatch.setattr('modules.auto_trade.conclusion.add_restricted_stock',
                        lambda code, name, memo, **k: restricted.append((code, memo)))

    monitor._check_conclusions()
    return restricted


def test_system_buy_is_not_restricted_after_restart(paper, monitor, monkeypatch):
    """[회귀 방지] 자동매매가 산 종목은 재기동 후 백필에서도 제한 종목이 되지 않는다.

    _SYSTEM_ODNOS는 프로세스 메모리라 재기동하면 비고, 가상투자는 그때 당일 원장을
    다시 훑는다. ODNO만으로 판정하면 자동매매가 자기 보유 종목을 '수동매매'로 제한해
    이후 매수를 통째로 스킵한다(관측: 삼성SDS·NAVER).
    """
    assert _run_backfill_with_trade_type(paper, monitor, monkeypatch, 'buy(AUTO)') == []


def test_manual_buy_is_still_restricted_after_restart(paper, monitor, monkeypatch):
    """대조군 — 사용자가 낸 수동 매수는 재기동 후에도 제한 종목으로 등록된다."""
    assert _run_backfill_with_trade_type(
        paper, monitor, monkeypatch, '매수(수동)') == [('005930', '수동매매')]


def test_reset_frees_paper_account_restrictions(paper, monkeypatch, tmp_path):
    """초기화는 가상 계좌 제한만 풀고 실계좌·전체 계좌 제한은 보존한다.

    restricted_stocks.json은 실계좌와 한 파일을 공유하므로 통째로 비우면 실계좌의
    수동매매 보호가 사라진다.
    """
    from modules.auto_trade import common as at_common

    monkeypatch.setattr(at_common, 'RESTRICTED_FILE', str(tmp_path / "restricted.json"))
    for attr, val in (('cano', 'PAPER'), ('acnt_prdt_cd', ''),
                      ('auto_cano', 'PAPER'), ('auto_acnt_prdt_cd', '')):
        monkeypatch.setattr(config.session, attr, val, raising=False)
    monkeypatch.setattr(config.session, 'is_simulation', False, raising=False)
    monkeypatch.setattr(config.session, 'is_toss', False, raising=False)

    auto_trade.add_restricted_stock('018260', '삼성SDS', '수동매매', cano='PAPER', acnt='')
    auto_trade.add_restricted_stock('005930', '삼성전자', '수동매매', cano='44048158', acnt='01')
    auto_trade.add_restricted_stock('042660', '한화오션', '수동매매')  # 전 계좌 공통

    paper.reset(1_000_000)

    data = auto_trade.load_restricted_stocks()
    assert '018260' not in data, "가상 계좌 제한이 남았다"
    assert 'PAPER-' not in str(data)
    assert '44048158-01' in data['005930']['accounts'], "실계좌 제한을 지웠다"
    assert data['042660']['memo'] == '수동매매', "전 계좌 공통 제한을 지웠다"


def test_reset_clears_only_paper_daily_baseline(paper, monkeypatch, tmp_path):
    """초기화는 가상 계좌의 일일 기준선만 지우고 실계좌 기준선은 보존한다.

    daily_asset_state.json은 실계좌와 한 파일을 공유한다. 통째로 지우면 실전
    인스턴스가 같은 날 재기동할 때 당일 손실 기준을 잃는다.
    """
    import jsonio
    from modules.auto_trade import common as at_common

    state = tmp_path / "daily_asset_state.json"
    # 실제 관찰 모드 세션과 동일하게 둔다(cano=PAPER, 상품코드 없음).
    for attr, val in (('cano', 'PAPER'), ('acnt_prdt_cd', ''),
                      ('auto_cano', 'PAPER'), ('auto_acnt_prdt_cd', '')):
        monkeypatch.setattr(config.session, attr, val, raising=False)
    monkeypatch.setattr(at_common, 'DAILY_STATE_FILE', str(state))
    jsonio.save_json(str(state), {"date": "2026-08-05",
                                  "accounts": {"PAPER-": 5_000_000, "44048158-01": 10027}})

    paper.reset(1_000_000)

    left = (jsonio.load_json(str(state), default={}) or {}).get("accounts", {})
    assert "PAPER-" not in left, "가상 계좌 기준선이 남았다"
    assert left.get("44048158-01") == 10027, "실계좌 기준선을 지웠다"
    assert paper.get_seed() == 1_000_000


def test_get_fill_by_odno(paper):
    """주문번호 대사는 해당 주문의 체결만 정확히 집어낸다."""
    res = api.place_order("domestic", "buy", "005930", 3, 70000, "00")
    odno = res['output']['ODNO']
    fill = paper.get_fill_by_odno(odno)
    assert fill and fill['qty'] == 3 and fill['odno'] == odno
    assert fill['price'] == pytest.approx(trading_cost.apply_slippage(70000, 'buy'))
    assert paper.get_fill_by_odno("P_UNKNOWN") is None
    assert paper.get_fill_by_odno(None) is None
