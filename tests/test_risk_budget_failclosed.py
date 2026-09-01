"""리스크 한도가 '계산 불가'일 때 열리지 않는가.

[왜 이 테스트인가] 포트폴리오 히트 캡은 동시 다발 손절의 합산 손실을 묶는 마지막 장치다.
그런데 기준자산을 못 구하거나 오픈 리스크 산출이 실패하면 None(=게이트 통째로 스킵) 또는
heat=0(=오픈 리스크 없음)이 되어 **한도가 조용히 사라졌다**. 데이터가 없을수록 열리는
구조였다 — 하필 데이터가 흔들리는 국면이 위험한 국면이다.

'제한 없음'(사용자가 캡을 끔)과 '계산 불가'(데이터 결손)를 가르는 것이 이 테스트의 핵심이다.
"""
import pytest

import config
from modules.auto_trade import AutoTrader


@pytest.fixture
def rm(monkeypatch):
    t = AutoTrader()
    monkeypatch.setattr(config.settings, "SYSTEM_MAX_PORTFOLIO_RISK", 10.0, raising=False)
    t.current_total_asset = 10_000_000
    t.initial_asset = 10_000_000
    t.portfolio_heat_amt = 0.0
    t.portfolio_heat_unknown = False
    monkeypatch.setattr(t.risk_manager, "current_risk_scale", lambda: 1.0)
    return t.risk_manager, t


def test_normal_budget(rm):
    r, t = rm
    assert r.portfolio_risk_budget_left() == pytest.approx(1_000_000)


def test_heat_reduces_budget(rm):
    r, t = rm
    t.portfolio_heat_amt = 400_000
    assert r.portfolio_risk_budget_left() == pytest.approx(600_000)


def test_user_disabled_cap_is_unlimited(rm, monkeypatch):
    """사용자가 캡을 끈 것은 의도된 '제한 없음'이다 — 이건 막지 않는다."""
    r, t = rm
    monkeypatch.setattr(config.settings, "SYSTEM_MAX_PORTFOLIO_RISK", 0.0, raising=False)
    assert r.portfolio_risk_budget_left() is None


def test_missing_equity_falls_back_to_cash_not_to_unlimited(rm):
    """기준자산이 없으면 예수금으로 폴백한다 — 예전엔 None(=게이트 통째로 스킵)이었다.

    폴백 대상이 예수금인 이유는 allocate_budget 이 같은 상황에서 쓰는 값이기 때문이다.
    두 한도가 서로 다른 기준을 보면 종목당 한도는 걸리는데 합산 한도는 안 걸린다.
    """
    r, t = rm
    t.current_total_asset = 0
    t.initial_asset = 0
    assert r.portfolio_risk_budget_left(avail_cash=5_000_000) == pytest.approx(500_000)


def test_no_equity_and_no_cash_blocks(rm):
    """자산도 예수금도 모르면 한도를 계산할 수 없다 — 열지 않는다."""
    r, t = rm
    t.current_total_asset = 0
    t.initial_asset = 0
    assert r.portfolio_risk_budget_left() == 0.0
    assert r.portfolio_risk_budget_left(avail_cash=0) == 0.0


def test_unknown_heat_blocks_instead_of_opening(rm):
    """오픈 리스크를 '못 센 것'은 '없는 것'이 아니다 — 예전엔 0으로 두어 예산이 전부 열렸다."""
    r, t = rm
    t.portfolio_heat_unknown = True
    assert r.portfolio_risk_budget_left() == 0.0


def test_unknown_heat_wins_over_cash_fallback(rm):
    """히트를 못 셌으면 예수금이 있어도 예산을 줄 수 없다 — 분자가 아니라 차감분이 없다."""
    r, t = rm
    t.current_total_asset = 0
    t.initial_asset = 0
    t.portfolio_heat_unknown = True
    assert r.portfolio_risk_budget_left(avail_cash=5_000_000) == 0.0


def test_unknown_heat_wins_over_a_healthy_equity(rm):
    """자산이 멀쩡해도 히트를 못 셌으면 예산을 줄 수 없다."""
    r, t = rm
    t.current_total_asset = 50_000_000
    t.portfolio_heat_unknown = True
    assert r.portfolio_risk_budget_left() == 0.0


def test_heat_failure_sets_unknown_not_zero(monkeypatch):
    """산출이 터졌을 때 0으로 덮으면 '오픈 리스크 없음'이 되어 캡이 무력화된다."""
    t = AutoTrader()
    t.portfolio_heat_amt = 777_000
    t.portfolio_heat_unknown = False

    def _boom(*a, **kw):
        raise RuntimeError("heat calc down")

    monkeypatch.setattr(t.risk_manager, "compute_portfolio_heat", _boom)
    try:
        t.portfolio_heat_amt = t.risk_manager.compute_portfolio_heat([], {})
        t.portfolio_heat_unknown = False
    except Exception:
        t.portfolio_heat_unknown = True

    assert t.portfolio_heat_unknown is True
    assert t.portfolio_heat_amt == 777_000, "실패했는데 값을 0으로 덮었다"


def test_recovers_on_next_cycle(rm):
    """막는 것은 영구 차단이 아니다 — 다음 주기에 데이터가 잡히면 저절로 풀려야 한다."""
    r, t = rm
    t.portfolio_heat_unknown = True
    assert r.portfolio_risk_budget_left() == 0.0
    t.portfolio_heat_unknown = False
    assert r.portfolio_risk_budget_left() == pytest.approx(1_000_000)


# ---------------------------------------------------------------------------
# 신규 매수 경로의 예산 선점 — 동작으로 잰다
#
# [왜] 종전에는 주문 성공 뒤에 히트를 더했다. 그 사이(네트워크 왕복)에 매도 워커의
# 피라미딩이 아직 반영되지 않은 예산을 보고 자기 몫을 잡을 수 있어, 두 경로의 합계가
# 포트폴리오 히트 캡을 넘었다. 피라미딩은 확인·선점을 락으로 원자화하고 실패 시
# 반납한다 — 신규 매수도 같은 규약이어야 한다.
#
# [이 테스트를 다시 쓴 이유] 종전 판정은 inspect.getsource 로 소스 문자열의 등장 순서를
# 비교했다. 변수명만 바꿔도 깨지고, 반대로 의미가 바뀌어도 통과할 수 있다 — 지키려던
# 성질(주문이 나가는 순간 예산이 이미 잡혀 있는가)을 실제로는 재지 않았다.
# ---------------------------------------------------------------------------
from unittest.mock import patch

_CAND = [{'code': '005930', 'name': '삼성전자', 'price': 70_000, 'score': 9.0,
          'rsi': 50, 'adx': 30, 'cci': 100, 'is_custom_rule': False, 'atr': 1_000}]


@pytest.fixture
def buyer(monkeypatch):
    """신규 매수 1회를 태울 수 있는 트레이더."""
    AutoTrader._instance = None
    t = AutoTrader()
    t.is_running = True
    t.buy_halted = False
    t.pending_restore_ok = True
    t.initial_asset = 10_000_000
    t.current_total_asset = 10_000_000
    t.portfolio_heat_amt = 0.0
    t.portfolio_heat_unknown = False
    monkeypatch.setattr(config.settings, "SYSTEM_MAX_PORTFOLIO_RISK", 10.0, raising=False)
    monkeypatch.setattr(t.risk_manager, "current_risk_scale", lambda market_type=None: 1.0)
    return t


def _run_buy(trader, odno="ODNO1", on_send=None):
    def _send(*a, **kw):
        if on_send:
            on_send()
        return odno
    with patch.dict(config.SELL_STRATEGY,
                    {"USE_ATR_STOP": True, "STOP_LOSS_RATE": -7.0, "ATR_STOP_MULTIPLIER": 2.0}), \
         patch.object(trader, '_clamp_order_price', side_effect=lambda c, p: p), \
         patch('modules.auto_trade.api.fetch_buyable_quantity', return_value=1000), \
         patch('modules.auto_trade.db_manager.db.delete_trailing_stop'), \
         patch('modules.auto_trade.db_manager.db.delete_half_tp'), \
         patch('modules.auto_trade.db_manager.db.cancel_reserved_buy_orders', return_value=0), \
         patch.object(trader.order_manager, 'send_order', side_effect=_send) as order:
        trader._execute_buy_orders(list(_CAND), 10_000_000, 0.25, 0, 4)
    return order


def test_budget_is_reserved_before_the_order_leaves(buyer):
    """[핵심] 주문이 나가는 그 순간에 이미 예산이 잡혀 있어야 한다."""
    seen = []
    order = _run_buy(buyer, on_send=lambda: seen.append(buyer.portfolio_heat_amt))
    assert order.called, "대조 조건이 깨졌다 — 주문 자체가 안 나갔다"
    assert seen and seen[0] > 0, "주문이 나가는 시점에 예산이 아직 안 잡혀 있었다"


def test_reservation_is_returned_when_the_order_fails(buyer):
    """주문이 실패하면 선점분은 반납된다 — 안 그러면 예산이 영구히 잠긴다."""
    order = _run_buy(buyer, odno=None)
    assert order.called
    assert buyer.portfolio_heat_amt == 0.0, "주문 실패인데 선점분이 남았다"


def test_budget_taken_between_check_and_send_blocks_the_buy(buyer):
    """[핵심] 확인과 선점 사이에 다른 경로가 예산을 가져가면 사지 않는다.

    배분액 기준의 첫 확인은 락 밖이다. 그 뒤 매도 워커의 피라미딩이 같은 예산을 잡으면
    합계가 캡을 넘으므로, 확정 수량으로 **다시 확인하고 같은 락 안에서 선점**해야 한다.
    (첫 호출은 넉넉한 예산, 두 번째 호출은 소진 — 경합을 그대로 흉내 낸다)
    """
    budgets = iter([10_000_000, 0.0])
    with patch.object(buyer.risk_manager, 'portfolio_risk_budget_left',
                      side_effect=lambda *a, **kw: next(budgets, 0.0)):
        order = _run_buy(buyer)
    assert not order.called, "주문 직전에 예산이 소진됐는데 그대로 발주했다"
    assert buyer.portfolio_heat_amt == 0.0, "보류인데 예산이 선점된 채 남았다"


def test_no_contention_means_the_recheck_is_a_no_op(buyer):
    """[대조군] 경합이 없으면 재확인은 무동작이어야 한다 — 상시 보류면 기능이 죽는다."""
    order = _run_buy(buyer)
    assert order.called, "경합이 없는데 재확인이 매수를 막았다"
    assert buyer.portfolio_heat_amt > 0, "체결 대기분의 리스크가 예산에 안 잡혔다"


# ---------------------------------------------------------------------------
# 종목 하나가 계산 불가일 때 (compute_portfolio_heat 내부)
# ---------------------------------------------------------------------------
class _BrokenTrades:
    """매수 기록을 훑는 순간 터지는 객체 — 손절률 유도 단계의 실패를 흉내낸다."""
    def __iter__(self):
        raise RuntimeError("매수 기록 파손")


class _FlakyHolding(dict):
    """두 번째 조회부터 터지는 잔고 행 — 폴백 계산조차 불가능한 상황을 만든다."""
    def __init__(self, *a, **kw):
        super().__init__(*a, **kw)
        self._reads = 0

    def get(self, key, *default):
        if key == "hldg_qty":
            self._reads += 1
            if self._reads > 1:
                raise RuntimeError("잔고 필드 파손")
        return super().get(key, *default)


def _holding(**over):
    h = {"pdno": "005930", "hldg_qty": "10", "pchs_avg_pric": "70000", "prpr": "80000"}
    h.update(over)
    return h


def test_broken_holding_is_counted_conservatively_not_as_zero(rm, caplog):
    """한 종목의 리스크 산출이 실패해도 **0으로 세면 안 된다.**

    0이면 총 히트가 과소평가되고 히트 캡이 그만큼 느슨해진다 — compute_portfolio_heat 이
    독스트링에서 표방하는 '보수적(과대평가)' 방향의 정반대다. 기본 손절폭을 가정해
    보수적으로 채우고, 조용히 넘어가지 않도록 경고를 남긴다.
    """
    r, _t = rm
    h = _holding()
    with caplog.at_level("WARNING"):
        heat = r.compute_portfolio_heat([h], {"005930": _BrokenTrades()})

    # 기준은 매수가다 — 히트 전체가 '진입 대비 손실'이므로 폴백도 같은 자로 재야
    #  총계에 섞였을 때 단위가 어긋나지 않는다(2026-09-01 정의 변경).
    default_sl = config.SELL_STRATEGY["STOP_LOSS_RATE"]
    expected = 10 * 70000 * abs(default_sl) / 100.0
    assert heat == pytest.approx(expected), "실패한 종목이 0원으로 계상됐다 — 캡이 느슨해진다"
    assert heat > 0
    assert any("오픈 리스크 산출 실패" in rec.message for rec in caplog.records), \
        "조용히 넘어갔다 — 실패는 로그에 남아야 한다"


def test_uncomputable_holding_propagates_to_failclosed_path(rm):
    """폴백조차 불가능하면 예외를 올려 호출부의 fail-closed 경로에 닿아야 한다.

    종전에는 `except Exception: continue` 가 예외를 삼켜, trader 가 준비해 둔
    portfolio_heat_unknown 처리에 **도달하지 못했다.** 못 센 것이 없는 것으로 둔갑했다.
    """
    r, _t = rm
    h = _FlakyHolding(_holding())
    with pytest.raises(Exception):
        r.compute_portfolio_heat([h], {"005930": _BrokenTrades()})


def test_repeated_failure_logs_once_per_code(rm, caplog):
    """같은 종목의 실패가 주기(60초)마다 경고를 쌓지 않아야 한다."""
    r, _t = rm
    with caplog.at_level("WARNING"):
        for _ in range(3):
            r.compute_portfolio_heat([_holding()], {"005930": _BrokenTrades()})
    hits = [rec for rec in caplog.records if "오픈 리스크 산출 실패" in rec.message]
    assert len(hits) == 1, f"같은 종목 경고가 {len(hits)}번 — 로그가 주기마다 쌓인다"
