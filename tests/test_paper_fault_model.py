"""가상투자 체결 시험 모델(config.PAPER_FAULT_MODEL) — 장애·상태 전이 검증 수단.

현실 시장 재현이 아니다. 지키는 것:
 ① 끄면 종전과 같다(즉시 전량 체결, 미체결 없음, 취소 대상 없음)
 ② 켜면 거절·지연·부분체결·취소·경합이 **시드로 재현**된다(주문·현금·보유가 같다)
 ③ 미체결 매수는 주문가능금액을, 미체결 매도는 주문가능수량을 묶는다
 ④ 재시작해도 미체결이 이어지고, 체결 감시는 종결된 주문만 주문번호당 한 번 반영한다
"""
import os
import tempfile
from datetime import datetime, timedelta

import pytest

import api
import config
from modules import db_manager, paper_broker
from core import trading_cost

T0 = datetime(2026, 10, 16, 10, 0, 0)


class _Clock:
    def __init__(self):
        self.t = T0

    def __call__(self):
        return self.t

    def add(self, sec):
        self.t = self.t + timedelta(seconds=sec)


def _open_paper(monkeypatch, path):
    monkeypatch.setattr(config, 'PAPER_DB_FILE_PATH', path, raising=False)
    monkeypatch.setattr(config, 'PAPER_SEED_CAPITAL', 5_000_000, raising=False)
    monkeypatch.setattr(config.session, 'is_paper', True, raising=False)
    db_manager.db.switch_path(path)
    paper_broker.init_tables()


@pytest.fixture
def paper(monkeypatch):
    path = os.path.join(tempfile.mkdtemp(), "paper_fault.db")
    original = db_manager.db.db_path
    _open_paper(monkeypatch, path)
    monkeypatch.setattr(paper_broker, '_current_price',
                        lambda code, fallback=0.0: {'005930': 70000}.get(code, fallback or 0))
    clock = _Clock()
    monkeypatch.setattr(paper_broker, '_now', clock)
    yield clock, path
    db_manager.db.close_all_connections()
    db_manager.db.switch_path(original)


def _faults(monkeypatch, **kw):
    cfg = {"ENABLED": True, "SEED": 7, "REJECT_PROB": 0.0, "DELAY_PROB": 0.0, "DELAY_SECONDS": 30,
           "PARTIAL_PROB": 0.0, "PARTIAL_RATIO": 0.5, "REMAINDER_FILL_SECONDS": 0, "CANCEL_RACE_PROB": 0.0}
    cfg.update(kw)
    monkeypatch.setattr(config, "PAPER_FAULT_MODEL", cfg, raising=False)


def _buy(qty=10, price=70000):
    return api.place_order("domestic", "buy", "005930", qty, price, "00")


def test_disabled_model_is_the_old_immediate_fill(paper):
    assert not paper_broker.fault_model_enabled()
    res = _buy()
    assert res['rt_cd'] == '0'
    assert paper_broker.get_positions()[0]['qty'] == 10
    assert api.get_unfilled_orders() == []
    assert paper_broker.get_order_state(res['output']['ODNO']) is None
    c = api.revise_cancel_order("domestic", "cancel", res['output']['ODNO'], "005930", 10, "0", "02", "00")
    assert c['rt_cd'] == '1' and "즉시 체결" in c['msg1']
    dep = paper_broker.get_deposit_balance()
    assert dep['order_possible'] == dep['deposit']


def test_reject(paper, monkeypatch):
    _faults(monkeypatch, REJECT_PROB=1.0)
    res = _buy()
    assert res['rt_cd'] == '1' and "시험 모델" in res['msg1']
    assert paper_broker.get_positions() == [] and paper_broker.get_cash() == 5_000_000


def test_delayed_fill_reserves_cash_until_it_fills(paper, monkeypatch):
    clock, _ = paper
    _faults(monkeypatch, DELAY_PROB=1.0, DELAY_SECONDS=30)
    res = _buy()
    odno = res['output']['ODNO']
    assert res['rt_cd'] == '0'
    opened = api.get_unfilled_orders()
    assert len(opened) == 1 and opened[0]['rmn_qty'] == '10' and opened[0]['sll_buy_dvsn_cd'] == '02'
    dep = paper_broker.get_deposit_balance()
    px = paper_broker.fill_price(70000, 'buy')
    reserved = px * 10 + trading_cost.buy_fee(px * 10)
    assert dep['deposit'] == 5_000_000 and dep['order_possible'] == int(5_000_000 - int(reserved))
    assert paper_broker.get_fill_by_odno(odno) is None          # 아직 체결 전
    clock.add(31)
    fill = paper_broker.get_fill_by_odno(odno)
    assert fill and fill['qty'] == 10 and not fill['partial']
    assert fill['time'] == (T0 + timedelta(seconds=30)).strftime('%Y-%m-%d %H:%M:%S')   # 예정 시각
    assert api.get_unfilled_orders() == []
    assert paper_broker.get_cash() == pytest.approx(5_000_000 - reserved)


def test_partial_fill_then_cancel_settles_once_with_filled_qty(paper, monkeypatch):
    _faults(monkeypatch, PARTIAL_PROB=1.0, PARTIAL_RATIO=0.3)
    odno = _buy()['output']['ODNO']
    st = paper_broker.get_order_state(odno)
    assert (st['status'], st['filled_qty']) == ("open", 3)
    assert paper_broker.get_positions()[0]['qty'] == 3
    assert paper_broker.get_fill_by_odno(odno) is None          # 종결 전에는 넘기지 않는다
    c = api.revise_cancel_order("domestic", "cancel", odno, "005930", 7, "0", "02", "00")
    assert c['rt_cd'] == '0'
    fill = paper_broker.get_fill_by_odno(odno)
    assert fill['qty'] == 3 and fill['partial'] and fill['status'] == "cancelled"
    assert paper_broker.get_deposit_balance()['order_possible'] == int(paper_broker.get_cash())
    # 재기동 복구용 목록에서도 주문번호당 한 건이다
    settled = [f for f in paper_broker.get_settled_fills() if f['odno'] == odno]
    assert len(settled) == 1 and settled[0]['qty'] == 3


def test_cancel_race_fills_remainder_and_cancel_fails(paper, monkeypatch):
    _faults(monkeypatch, PARTIAL_PROB=1.0, CANCEL_RACE_PROB=1.0)
    odno = _buy()['output']['ODNO']
    c = paper_broker.cancel_order(odno)
    assert c['rt_cd'] == '1' and c['msg_cd'] == "PAPER_RACE"
    assert paper_broker.get_order_state(odno)['status'] == "filled"
    assert paper_broker.get_positions()[0]['qty'] == 10
    assert paper_broker.get_fill_by_odno(odno)['qty'] == 10


def test_open_sell_locks_orderable_quantity(paper, monkeypatch):
    _buy()                                                       # 즉시 체결로 10주 확보(모델 꺼짐)
    _faults(monkeypatch, DELAY_PROB=1.0)
    assert api.place_order("domestic", "sell", "005930", 10, 71000, "00")['rt_cd'] == '0'
    out1, _ = paper_broker.get_domestic_balance()
    assert out1[0]['hldg_qty'] == '10' and out1[0]['ord_psbl_qty'] == '0'
    again = api.place_order("domestic", "sell", "005930", 1, 71000, "00")
    assert again['rt_cd'] == '1' and "주문가능수량" in again['msg1']


def _scenario(monkeypatch, path):
    """같은 시드·같은 주문 순서 — 결과(주문 상태·현금·보유)를 돌려준다."""
    _open_paper(monkeypatch, path)
    clock = _Clock()
    monkeypatch.setattr(paper_broker, '_now', clock)
    _faults(monkeypatch, SEED=20261016, REJECT_PROB=0.2, DELAY_PROB=0.4, PARTIAL_PROB=0.4,
            REMAINDER_FILL_SECONDS=90, CANCEL_RACE_PROB=0.3)
    states = []
    for i in range(16):
        side = "buy" if i % 3 else "sell"
        res = api.place_order("domestic", side, "005930", 4, 70000, "00")
        states.append(res['rt_cd'])
        odno = (res.get('output') or {}).get('ODNO')
        clock.add(20)
        if odno and i % 4 == 0:
            states.append(paper_broker.cancel_order(odno)['msg_cd'])
    clock.add(600)
    api.get_unfilled_orders()                      # 조회가 시간이 된 체결을 진행시킨다
    orders = db_manager.db.execute_query(
        "SELECT side, qty, filled_qty, status, next_stage FROM paper_orders ORDER BY created_at, rowid",
        fetch='all')
    result = (states, [tuple(o) for o in orders], round(paper_broker.get_cash(), 4),
              [(p['code'], p['qty']) for p in paper_broker.get_positions()])
    db_manager.db.close_all_connections()
    return result


def test_same_seed_reproduces_orders_cash_and_holdings(paper, monkeypatch):
    _, _ = paper
    a = _scenario(monkeypatch, os.path.join(tempfile.mkdtemp(), "a.db"))
    b = _scenario(monkeypatch, os.path.join(tempfile.mkdtemp(), "b.db"))
    assert a == b
    # 이 시드가 실제로 밟은 분기 — 하나라도 빠지면 '같다'가 공짜로 통과한다
    assert "1" in a[0] and "0" in a[0] and "PAPER_RACE" in a[0], a[0]     # 거절·접수·취소 경합
    assert any(0 < o[2] < o[1] or o[4] > 1 for o in a[1]), a[1]           # 분할 체결
    assert all(o[3] != "open" for o in a[1]), a[1]                        # 600초 뒤 모두 정산


def test_restart_keeps_open_orders_and_continues_the_seed_sequence(paper, monkeypatch):
    clock, path = paper
    _faults(monkeypatch, PARTIAL_PROB=1.0, REMAINDER_FILL_SECONDS=60)
    odno = _buy()['output']['ODNO']
    seq_before = int(paper_broker._get_state("fault_seq"))
    # 재시작: 연결을 모두 닫고 같은 파일을 다시 연다(모듈 메모리 상태와 무관해야 한다)
    db_manager.db.close_all_connections()
    _open_paper(monkeypatch, path)
    opened = api.get_unfilled_orders()
    assert [o['odno'] for o in opened] == [odno] and opened[0]['rmn_qty'] == '5'
    clock.add(61)
    assert paper_broker.get_fill_by_odno(odno)['qty'] == 10
    _buy(qty=2)
    assert int(paper_broker._get_state("fault_seq")) == seq_before + 1


def test_conclusion_closes_zero_fill_cancelled_orders(paper, monkeypatch):
    """체결 0으로 종결된 시험 주문은 주문 상태기계에서 정리된다(고아로 남지 않는다)."""
    import modules.auto_trade as at
    from modules.auto_trade.common import OrderStatus
    from modules.auto_trade.conclusion import ConclusionMonitor

    _faults(monkeypatch, DELAY_PROB=1.0)
    odno = _buy()['output']['ODNO']
    assert paper_broker.cancel_order(odno)['rt_cd'] == '0'

    updates = []

    class _OM:
        _lock = __import__("threading").RLock()
        pending_orders = {"005930": {odno: OrderStatus.ORDER_SENT}}

        def update_order_status(self, code, o, status):
            updates.append((code, o, status))

    class _Trader:
        order_manager = _OM()

    monkeypatch.setattr(at, "AutoTrader", lambda: _Trader())
    mon = ConclusionMonitor.__new__(ConclusionMonitor)
    mon.paper_backfill_done = True
    mon._check_paper_conclusions()
    assert updates == [("005930", odno, OrderStatus.CANCELED)]


# ---------------------------------------------------------------------------
# 통합 — 실제 OrderManager·ConclusionMonitor 를 상태 있는 가상 브로커에 붙인다
# ---------------------------------------------------------------------------
@pytest.fixture
def live_trader(paper, monkeypatch):
    from modules.auto_trade import AutoTrader
    clock, _ = paper
    clock.t = datetime.now() - timedelta(minutes=10)      # 미체결 타임아웃(120초)을 넘긴 주문
    monkeypatch.setattr(api, "send_telegram_message", lambda *a, **k: True)
    AutoTrader._instance = None
    t = AutoTrader()
    t.is_running = True
    monkeypatch.setattr(t, "is_market_open", lambda: True)
    yield t
    AutoTrader._instance = None


def _place_tracked(trader):
    from modules.auto_trade.common import OrderStatus
    res = _buy()
    odno = res['output']['ODNO']
    db_manager.db.insert_trade("매수(AUTO)", "005930", "삼성전자", 10, 70000, odno,
                               reason="시험", order_status="접수")
    trader.order_manager.pending_orders = {"005930": {odno: OrderStatus.ORDER_SENT}}
    return odno


def _run_conclusion(trader):
    from modules.auto_trade.conclusion import ConclusionMonitor
    mon = ConclusionMonitor.__new__(ConclusionMonitor)
    mon.paper_backfill_done = True
    mon.processed_sim_fills = set()
    mon._lock = __import__("threading").RLock()
    mon._check_paper_conclusions()


def test_timeout_cancel_then_conclusion_releases_the_symbol(live_trader, monkeypatch):
    _faults(monkeypatch, DELAY_PROB=1.0, DELAY_SECONDS=100_000)       # 체결되지 않는 지연 주문
    odno = _place_tracked(live_trader)
    assert live_trader.order_manager.is_pending("005930")
    live_trader.order_manager.manage_unfilled_orders()                # 실제 미체결 관리 코드
    assert paper_broker.get_order_state(odno)['status'] == "cancelled"
    _run_conclusion(live_trader)
    assert not live_trader.order_manager.is_pending("005930"), "취소된 주문이 종목을 계속 묶고 있다"
    assert paper_broker.get_positions() == [] and paper_broker.get_cash() == 5_000_000


def test_cancel_race_is_recorded_as_failure_and_the_fill_lands_once(live_trader, monkeypatch):
    _faults(monkeypatch, PARTIAL_PROB=1.0, CANCEL_RACE_PROB=1.0)
    odno = _place_tracked(live_trader)
    live_trader.order_manager.manage_unfilled_orders()
    assert live_trader.order_manager.cancel_failures.get(str(odno)) == 1   # 취소는 실패로 센다
    assert paper_broker.get_order_state(odno)['status'] == "filled"
    _run_conclusion(live_trader)
    _run_conclusion(live_trader)                                          # 두 번 돌아도 한 번만
    assert not live_trader.order_manager.is_pending("005930")
    fills = [r for r in db_manager.db.get_trades(code="005930", limit=20)
             if "체결" in str(r.get("order_status") or "")]
    assert len(fills) == 1 and int(fills[0]["qty"]) == 10
