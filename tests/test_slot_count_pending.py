"""슬롯 상한은 '잔고 + 미체결'로 센다 — 미체결이 같은 자리를 두 번 열어 주지 않게.

[왜 · 2026-09-08] SYSTEM_MAX_HOLDINGS 판정이 **잔고만** 봤다. 직전 주기에 낸 매수 주문이
 아직 체결되지 않았으면 그 종목은 잔고에 없다 — 같은 슬롯이 다음 주기에 다시 비어 보인다.

     주기 N   : 보유 2 / 상한 4 → 두 자리가 비어 A·B 매수 (미체결)
     주기 N+1 : 보유 여전히 2 → **또 두 자리가 비어** C·D 매수
     넷이 다 체결되면 6종목. 상한은 4다.

 A·B 자신은 is_pending 으로 후보에서 빠지지만 그것은 **같은 종목**의 중복만 막는다.
 슬롯은 '몇 자리를 썼나'의 문제라 종목이 달라도 넘친다.
 미체결 자동 취소는 UNFILLED_ORDER_CANCEL_SECONDS(기본 120초) 뒤이고 감시 주기는 그보다
 짧으므로 이 창은 상시 열려 있다(취소가 연속 실패하는 경로도 따로 있다).

 슬롯 4는 실증으로 정해진 정책이다([[seed-slot-sizing]]) — 넘치면 검증한 적 없는 노출로
 돈다. 히트 캡도 같은 이유로 과소평가되는데, 그쪽은 이미 '알려진 한계'로 적혀 있었다.
 다만 그 주석이 폭을 '그 한 주문분'이라고 적은 것은 틀렸다(아래 마지막 테스트 참조).
"""
import threading
from unittest.mock import MagicMock

import pytest

from modules.auto_trade.trader import AutoTrader


def _trader(pending):
    t = object.__new__(AutoTrader)
    t.log = lambda *a, **k: None
    om = MagicMock()
    om._lock = threading.RLock()
    om.pending_orders = pending
    t.order_manager = om
    return t


HELD = {"005930", "000660"}


def test_a_pending_order_on_a_new_code_takes_a_slot():
    t = _trader({"035420": {"odno1": "ORDER_SENT"}})
    assert t.occupied_slot_codes(HELD) == HELD | {"035420"}


def test_two_pending_orders_fill_the_remaining_two_slots():
    """이것이 결함의 핵심 — 종전에는 이 상황에서 두 자리가 또 비어 보였다."""
    t = _trader({"035420": {"o1": "ORDER_SENT"}, "051910": {"o2": "ORDER_SENT"}})
    assert len(t.occupied_slot_codes(HELD)) == 4, t.occupied_slot_codes(HELD)


def test_an_empty_pending_entry_does_not_take_a_slot():
    """빈 dict 는 '대기 없음'이다 — 키만 남은 종결 주문이 슬롯을 영구히 묶으면 안 된다.

    (is_pending 이 같은 이유로 bool(값)을 본다)
    """
    t = _trader({"035420": {}})
    assert t.occupied_slot_codes(HELD) == HELD


def test_a_pending_order_on_a_held_code_is_not_double_counted():
    """보유 종목의 미체결 매도는 이미 잔고에 있다 — 합집합이라 중복되지 않는다."""
    t = _trader({"005930": {"o1": "ORDER_SENT"}})
    assert t.occupied_slot_codes(HELD) == HELD


def test_no_holdings_and_no_pending_is_zero():
    assert _trader({}).occupied_slot_codes(set()) == set()


def test_counting_failure_falls_back_to_holdings_and_says_so():
    """못 세면 잔고만으로 두되 조용히 넘어가지 않는다."""
    logged = []
    t = _trader({})
    t.log = lambda m, *a, **k: logged.append(m)

    class _Boom:
        _lock = threading.RLock()

        @property
        def pending_orders(self):
            raise RuntimeError("주문 상태를 읽을 수 없음")

    t.order_manager = _Boom()
    assert t.occupied_slot_codes(HELD) == HELD
    assert any("미체결" in m for m in logged), logged


# ---------------------------------------------------------------------------
# 두 자리(게이트·실행부)가 같은 셈을 쓰는가
# ---------------------------------------------------------------------------

def test_the_gate_and_the_executor_use_the_same_count():
    """게이트만 고치고 실행부를 두면, 게이트는 막는데 실행부는 자리가 있다고 믿는다."""
    src = open("modules/auto_trade/trader.py", encoding="utf-8").read()
    assert "len(slot_codes) >= max_holdings" in src, "슬롯 게이트가 잔고만 센다"
    assert "len(self.occupied_slot_codes(holding_codes)), max_holdings" in src, \
        "_execute_buy_orders 에 넘기는 수가 잔고 기준이다"
    assert "if len(holding_codes) >= max_holdings" not in src, \
        "옛 잔고 기준 게이트가 남아 있다"


def test_the_heat_comment_no_longer_claims_a_single_order_bound():
    """히트 과소평가 폭을 '그 한 주문분'이라고 적은 것은 사실이 아니었다.

    한 주기에 빈 슬롯 수만큼 주문이 나가므로 폭은 그 합이다. 주석이 실제보다 작은
    상한을 말하면, 읽는 사람이 그 값을 믿고 캡을 설계한다.
    """
    src = open("modules/auto_trade/trader.py", encoding="utf-8").read()
    assert "과소평가 폭은 그 한 주문분이라" not in src
    assert "한 주기에\n        #   나간 주문분" in src or "한 주기에" in src


# ---------------------------------------------------------------------------
# 상관 필터도 같은 셈을 쓴다
# ---------------------------------------------------------------------------

def test_the_correlation_filter_compares_against_pending_orders_too():
    """백테스트는 매수가 즉시 포지션이 되어 다음 판정의 held 에 들어간다
    (portfolio_backtest 의 entry_gate(day, code, tuple(positions))).
    실매매에서만 체결 전까지 보이지 않으면, 이 필터를 검증한 모델보다 실매매가
    더 허용적이 된다 — 미체결 종목과 같은 테마를 그 사이에 살 수 있다.
    """
    src = open("modules/auto_trade/trader.py", encoding="utf-8").read()
    assert "corr_ref_codes = self.occupied_slot_codes(holding_codes)" in src, \
        "상관 비교 대상이 잔고 기준이다"
    assert "if use_corr_filter and holding_codes:" not in src, \
        "옛 잔고 기준 분기가 남아 있다"


def test_the_correlation_scope_between_candidates_is_left_alone():
    """후보끼리 비교하지 않는 것은 실매매·백테스트가 같은 설계 범위다 — 건드리지 않는다.

    (양쪽 모두 한 번에 만든 후보 목록을 같은 스냅샷으로 판정한다. 그 범위 안에서
     0.7 이 검증됐다 — 범위를 바꾸는 것은 감사가 아니라 전략 제안이다.)
    """
    src = open("modules/portfolio_backtest.py", encoding="utf-8").read()
    assert "entry_gate(day, code, tuple(positions))" in src, \
        "백테스트의 게이트 호출 형태가 바뀌었다 — 이 파일의 패리티 근거를 다시 확인할 것"
