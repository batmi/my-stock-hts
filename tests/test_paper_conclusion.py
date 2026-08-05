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
from modules import db_manager, paper_broker
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

    assert inserted[0][0][4] == 70000.0  # insert_trade(type, code, name, qty, price, ...)


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


def test_get_fill_by_odno(paper):
    """주문번호 대사는 해당 주문의 체결만 정확히 집어낸다."""
    res = api.place_order("domestic", "buy", "005930", 3, 70000, "00")
    odno = res['output']['ODNO']
    fill = paper.get_fill_by_odno(odno)
    assert fill and fill['qty'] == 3 and fill['price'] == 70000 and fill['odno'] == odno
    assert paper.get_fill_by_odno("P_UNKNOWN") is None
    assert paper.get_fill_by_odno(None) is None
