"""손절 후 되사기 루프와 고아 주문 누수를 막는다.

[관측 2026-08-05 · 가상투자] 손절 -0.1% 개별 룰을 건 NAVER가 매 주기 이렇게 돌았다.
  15:06:58 매도 229,500원(손절) → 15:07:08 매수 230,500원  ← 10초 뒤 1,000원 비싸게
  15:08:11 매도 229,500원(손절) → 15:08:21 매수 231,000원
매도는 즉시 체결을 위해 현재가 아래, 매수는 위로 지정가를 내므로 왕복 스프레드만큼
실현 손실만 쌓였다. 기존 재진입 허들(직전 매수의 체결강도 경신)은 재진입마다 값이
갱신되어(103.1% → 127.3% → 127.5%) 스스로 세운 기준을 스스로 넘었다.

이어서 손절 체결 뒤 주문이 ORDER_SENT로 남아(고아 주문) 그 종목이 매수·매도 판정에서
통째로 빠졌다. 가상투자는 즉시 전량 체결 모델이라 '살아 있는 주문'이 존재하지 않으므로,
체결 기록으로 확인한 뒤 정리해야 한다.
"""
import threading

import pytest

import config
from modules.auto_trade.common import OrderStatus
from modules.auto_trade.engine import OrderManager
from modules.auto_trade.trader import AutoTrader


# ==========================================================
# 손절가 위 재진입 차단
# ==========================================================
def test_collect_stop_exit_prices_picks_latest_stop_loss():
    trades = [
        {'time': '2026-08-05 15:06:58', 'type': '매도(AUTO)', 'code': '035420',
         'price': '229500', 'reason': '손절(-0.2%)'},
        {'time': '2026-08-05 15:08:11', 'type': '매도(AUTO)', 'code': '035420',
         'price': '229000', 'reason': '손절(-0.2%)'},
    ]
    out = AutoTrader._collect_stop_exit_prices(trades)
    assert out == {'035420': 229000.0}, "같은 날 여러 손절이면 가장 최근 값이 기준이다"


def test_profit_exits_are_not_blocked():
    """익절·트레일링 청산은 대상이 아니다 — 상승 추세의 정상 재진입까지 막으면 안 된다."""
    trades = [
        {'time': '2026-08-05 10:00:00', 'type': '매도(AUTO)', 'code': '005930',
         'price': '80000', 'reason': '익절(7.0%)'},
        {'time': '2026-08-05 11:00:00', 'type': '매도(AUTO)', 'code': '000660',
         'price': '90000', 'reason': '트레일링스탑(+12.0% → +8.0%)'},
        {'time': '2026-08-05 12:00:00', 'type': '매도(AUTO)', 'code': '012330',
         'price': '70000', 'reason': '시간청산(20일경과, 상방모멘텀 상실)'},
    ]
    assert AutoTrader._collect_stop_exit_prices(trades) == {}


def test_break_even_exit_is_treated_as_stop():
    """본전청산도 손실 회피 청산이므로 같은 취급."""
    trades = [{'time': '2026-08-05 13:00:00', 'type': '매도(AUTO)', 'code': '035420',
               'price': '231000', 'reason': '본전청산(0.4%)'}]
    assert AutoTrader._collect_stop_exit_prices(trades) == {'035420': 231000.0}


def test_buy_side_trades_are_ignored():
    trades = [{'time': '2026-08-05 09:00:00', 'type': '매수(AUTO)', 'code': '035420',
               'price': '230500', 'reason': '손절(-0.2%)'}]
    assert AutoTrader._collect_stop_exit_prices(trades) == {}


def test_malformed_rows_do_not_break_collection():
    """가격이 비었거나 형식이 깨진 기록이 있어도 나머지는 살아남는다."""
    trades = [
        {'time': '2026-08-05 10:00:00', 'type': '매도', 'code': 'A', 'price': '', 'reason': '손절(-1.0%)'},
        {'time': '2026-08-05 10:01:00', 'type': '매도', 'code': 'B', 'price': 'N/A', 'reason': '손절(-1.0%)'},
        {'time': '2026-08-05 10:02:00', 'type': '매도', 'code': 'C', 'price': '1,500', 'reason': '손절(-1.0%)'},
    ]
    assert AutoTrader._collect_stop_exit_prices(trades) == {'C': 1500.0}


def test_gate_default_is_on():
    """휩소 방어는 기본 ON — 끄려면 명시적으로 꺼야 한다."""
    assert getattr(config, 'REENTRY_BLOCK_ABOVE_STOP_PRICE', True) is True


# ==========================================================
# 가상투자 고아 주문 복구
# ==========================================================
class _FakeTrader:
    def __init__(self):
        self.logs = []
        self.half_tp_cache = set()
        self.trailing_stop_cache = {}
        self._lock = threading.RLock()

    def log(self, msg):
        self.logs.append(msg)

    def log_current_holdings(self):
        pass


@pytest.fixture
def om():
    m = OrderManager.__new__(OrderManager)
    m._lock = threading.RLock()
    m.pending_orders = {'035420': {'P123': OrderStatus.ORDER_SENT}}
    m.sell_pre_qty = {}
    m.sell_cleanup_odnos = {}
    m.cancel_failures = {}
    m.orphan_alerted = set()
    m.order_fail_alerted = {}
    m.orders_sent_count = 0
    m.trader = _FakeTrader()
    return m


def test_paper_orphan_with_fill_is_resolved(om, monkeypatch):
    """[핵심] 체결 기록이 있으면 정리한다 — 방치하면 그 종목이 판정에서 통째로 빠진다."""
    from modules import paper_broker
    monkeypatch.setattr(paper_broker, 'is_active', lambda: True)
    monkeypatch.setattr(paper_broker, 'get_fill_by_odno',
                        lambda odno: {'name': 'NAVER', 'type': '매도', 'qty': 4, 'price': 229000.0})
    # 체결 확정은 '1.5초 뒤 잔고 재출력' 데몬 스레드를 띄운다. 테스트에서는 띄우지 않는다 —
    # 그 스레드가 살아 있는 동안 time.sleep을 전역 패치하는 다른 테스트와 얽혀 병렬 실행이
    # 불안정해진다(여기서 검증할 대상도 아니다).
    monkeypatch.setattr(threading, 'Thread', lambda *a, **k: type(
        'NoThread', (), {'start': lambda self: None, 'daemon': True})())

    assert om._resolve_paper_orphan('035420', 'P123') is True
    assert om.is_pending('035420') is False, "고아 주문이 정리되지 않아 판정이 계속 막힌다"
    assert any("가상체결 복구" in m for m in om.trader.logs)


def test_paper_orphan_without_fill_is_left_alone(om, monkeypatch):
    """체결 기록이 없으면 추측하지 않는다 — 기존 경보 경로로 넘긴다."""
    from modules import paper_broker
    monkeypatch.setattr(paper_broker, 'is_active', lambda: True)
    monkeypatch.setattr(paper_broker, 'get_fill_by_odno', lambda odno: None)

    assert om._resolve_paper_orphan('035420', 'P123') is False
    assert om.is_pending('035420') is True


def test_real_account_orphan_is_never_auto_resolved(om, monkeypatch):
    """[안전] 실계좌는 자동 정리하지 않는다 — 살아 있는 주문을 풀면 주문끼리 싸운다."""
    from modules import paper_broker
    monkeypatch.setattr(paper_broker, 'is_active', lambda: False)

    def _boom(odno):
        raise AssertionError("실계좌에서 체결 기록을 조회해서는 안 된다")

    monkeypatch.setattr(paper_broker, 'get_fill_by_odno', _boom)

    assert om._resolve_paper_orphan('035420', 'P123') is False
    assert om.is_pending('035420') is True


# ==========================================================
# 매수 후보 스킵 가시성
# ==========================================================
def test_buy_candidate_skips_are_logged():
    """[회귀 방지] 매수 후보에서 조용히 빠지는 경로가 없어야 한다.

    대기 주문·차트 없음으로 빠지면 그 종목은 [분석] 줄도 [분석스킵] 줄도 없이 사라져,
    왜 후보에서 빠졌는지 운영자가 알 수 없다(2026-08-05 NAVER).
    """
    import os
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    src = open(os.path.join(root, "modules/auto_trade/trader.py"), encoding="utf-8").read()

    start = src.find("def _analyze_candidate_worker")
    end = src.find("def _analyze_candidates", start)
    assert start > 0 and end > start
    worker = src[start:end]

    for marker in ("진행 중인 주문 존재", "차트 데이터 없음"):
        assert marker in worker, f"매수 후보 스킵 로그가 사라졌다: {marker}"


# ==========================================================
# 가상투자 계좌 라벨
# ==========================================================
def test_paper_mode_has_its_own_account_label():
    """[회귀 방지] mode 4가 라벨 분기에서 실전으로 떨어지지 않는다.

    is_toss → else 순서로만 분기하면 가상투자가 '한투증권(자동)'으로
    표시된다. 실전 시세를 쓸 뿐 계좌는 가상이라, 실전 자동매매 계좌로 읽히면 위험하다
    (같은 누락이 텔레그램 꼬리말에서도 있었다).
    """
    import os
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    src = open(os.path.join(root, "modules/auto_trade/trader.py"), encoding="utf-8").read()

    idx = src.find('acc_type = "한투증권(자동)"')
    assert idx > 0, "운용 계좌 라벨 분기를 찾지 못했다"
    block = src[max(0, idx - 1600):idx]
    assert "is_paper" in block, "가상투자 분기가 없어 실전 라벨로 떨어진다"
    assert '"가상투자"' in block, "라벨은 trading.py와 같은 '가상투자'로 맞춘다"


def test_paper_label_matches_manual_trading_menu():
    """수동 거래 메뉴와 같은 문구를 쓴다 — 화면마다 다른 이름으로 부르지 않는다."""
    import os
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    manual = open(os.path.join(root, "modules/trading.py"), encoding="utf-8").read()
    assert 'acc_label = "가상투자"' in manual
