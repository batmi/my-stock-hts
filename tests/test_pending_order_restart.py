"""재기동 후에도 '이미 걸린 주문'이 보이는가 — 중복 주문 방지.

pending_orders는 메모리에만 산다. 재기동하면 비고, is_pending(code)가 False가 되는
순간 그 종목은 시스템 눈에 '주문 없음'으로 보인다. 그런데 주문은 거래소에 살아 있다.

  · 매수: 후보 필터를 그대로 통과해 **두 번째 매수 주문**이 나간다. 잔고에도 안 잡히므로
    보유 종목 수 게이트도 못 막는다. 둘 다 체결되면 의도한 리스크의 2배가 된다.
  · 매도: 이미 매도 주문이 걸려 매도가능수량이 0인데 거래정지로 오인해 경보를 낸다.

운영 환경이 라즈베리파이3(1GB)라 OOM 재기동이 드물지 않다 — 세션 내 한정 위험이 아니다.

[복구 경로 두 개]
  ① initialize() — 시작 시 거래소 미체결을 조회해 메모리 추적에 되살린다.
  ② manage_unfilled_orders() — 매 주기 같은 복구를 한다(①이 실패해도 자가 치유).
②는 매수·매도 검사 **뒤에** 돌기 때문에 ①이 없으면 첫 주기가 그대로 노출된다.
"""
import pytest
from unittest.mock import patch

import config
from modules.auto_trade import AutoTrader
from modules.auto_trade.common import OrderStatus

CODE = "005930"
NAME = "삼성전자"
ODNO = "0000123456"


@pytest.fixture
def trader():
    AutoTrader._instance = None
    t = AutoTrader()
    t.is_running = True
    t.order_manager.pending_orders.clear()
    t.pending_restore_ok = True
    yield t
    t.order_manager.pending_orders.clear()


def _open_order(code=CODE, odno=ODNO, qty=100):
    return {'odno': odno, 'pdno': code, 'prdt_name': NAME, 'rmn_qty': str(qty),
            'ord_tmd': '090500', 'ord_unpr': '70000', 'sll_buy_dvsn_cd': '02'}


def _known_trade():
    """재기동 전에 시스템이 직접 낸 주문 — DB에 기록이 남아 있다."""
    return {'type': '매수', 'name': NAME, 'qty': 100, 'price': 70000,
            'time': '2026-08-04 09:05:00', 'order_status': '접수'}


# ─────────────────────── ① 시작 시 복구 ───────────────────────

def test_restore_puts_a_live_exchange_order_back_under_tracking(trader):
    """[핵심] 거래소에 살아 있는 미체결 주문은 재기동 후 다시 pending으로 잡혀야 한다."""
    assert not trader.order_manager.is_pending(CODE), "전제: 재기동 직후 추적은 비어 있다"

    with patch('modules.auto_trade.api.get_domestic_open_orders',
               return_value=[_open_order()]):
        ok = trader.order_manager.restore_pending_orders("12345678", "01")

    assert ok is True
    assert trader.order_manager.is_pending(CODE), \
        "거래소에 주문이 살아 있는데 '주문 없음'으로 보인다 — 중복 주문이 나간다"


def test_restore_ignores_fully_filled_rows(trader):
    """잔량 0은 이미 끝난 주문이다 — 되살리면 그 종목의 매도 판정이 통째로 멈춘다."""
    with patch('modules.auto_trade.api.get_domestic_open_orders',
               return_value=[_open_order(qty=0)]):
        trader.order_manager.restore_pending_orders()
    assert not trader.order_manager.is_pending(CODE), "잔량 0 주문을 추적에 올렸다"


def test_restore_reports_failure_instead_of_pretending_there_is_nothing(trader):
    """조회 실패와 '미체결 없음'은 다르다 — 실패를 성공으로 삼키면 안 된다."""
    with patch('modules.auto_trade.api.get_domestic_open_orders', return_value=None):
        assert trader.order_manager.restore_pending_orders() is False
    with patch('modules.auto_trade.api.get_domestic_open_orders',
               side_effect=OSError("network")):
        assert trader.order_manager.restore_pending_orders() is False
    with patch('modules.auto_trade.api.get_domestic_open_orders', return_value=[]):
        assert trader.order_manager.restore_pending_orders() is True, \
            "빈 목록은 '미체결 없음'이라는 정상 응답이다"


def test_restore_does_not_downgrade_a_progressed_order(trader):
    """이미 진행된 주문(부분체결 등)의 상태를 ORDER_SENT로 되돌리면 안 된다.

    로컬 폴백 취소가 ORDER_SENT만 보므로, 되돌리면 진행 중인 주문이 취소 대상이 된다.
    """
    trader.order_manager.register_manual_order(CODE, ODNO)
    with trader.order_manager._lock:
        trader.order_manager.pending_orders[CODE][ODNO] = OrderStatus.PARTIAL_FILLED

    with patch('modules.auto_trade.api.get_domestic_open_orders',
               return_value=[_open_order()]):
        trader.order_manager.restore_pending_orders()

    assert trader.order_manager.pending_orders[CODE][ODNO] == OrderStatus.PARTIAL_FILLED, \
        "부분체결 주문이 ORDER_SENT로 되돌아갔다 — 폴백이 이걸 취소한다"


def test_initialize_actually_runs_the_restore(trader):
    """[배선] 복구 함수가 있어도 initialize()가 부르지 않으면 첫 주기가 그대로 노출된다.

    manage_unfilled_orders는 매수·매도 검사 **뒤에** 돌기 때문에, 시작 시점 복구가
    빠지면 첫 주기에 중복 주문이 나간다. 함수 단위 테스트만으로는 이 공백이 안 잡힌다.
    """
    trader.initialized = False
    summary = [{'dnca_tot_amt': '1000000', 'prvs_rcdl_excc_amt': '1000000',
                'tot_evlu_amt': '1000000'}]

    with patch.object(config.session, 'is_simulation', True), \
         patch('modules.auto_trade.api.get_domestic_balance', return_value=([], summary)), \
         patch('modules.auto_trade.db_manager.db.get_all_trailing_stops', return_value={}), \
         patch('modules.auto_trade.db_manager.db.get_all_half_tp', return_value=set()), \
         patch('modules.auto_trade.trader.load_daily_initial_asset', return_value=1_000_000), \
         patch.object(trader.order_manager, 'restore_pending_orders',
                      return_value=True) as restore:
        assert trader.initialize() is True
    assert restore.called, "initialize()가 재기동 복구를 부르지 않는다 — 첫 주기가 노출된다"
    assert trader.pending_restore_ok is True


def test_initialize_holds_buys_when_the_restore_fails(trader):
    """복구 조회가 실패했으면 그 사실이 매수 게이트까지 전달돼야 한다."""
    trader.initialized = False
    summary = [{'dnca_tot_amt': '1000000', 'prvs_rcdl_excc_amt': '1000000',
                'tot_evlu_amt': '1000000'}]

    with patch.object(config.session, 'is_simulation', True), \
         patch('modules.auto_trade.api.get_domestic_balance', return_value=([], summary)), \
         patch('modules.auto_trade.db_manager.db.get_all_trailing_stops', return_value={}), \
         patch('modules.auto_trade.db_manager.db.get_all_half_tp', return_value=set()), \
         patch('modules.auto_trade.trader.load_daily_initial_asset', return_value=1_000_000), \
         patch.object(trader.order_manager, 'restore_pending_orders', return_value=False):
        trader.initialize()
    assert trader.pending_restore_ok is False, \
        "조회 실패가 매수 게이트에 전달되지 않는다 — 모르는 채로 매수한다"


# ─────────────────── ② 매 주기 자가 치유 ───────────────────

def test_cycle_sweep_retracks_our_own_order_not_just_external_ones(trader):
    """[핵심 회귀] DB에 기록이 있는 '자기 주문'도 다시 추적에 올라야 한다.

    종전에는 추적 등록이 '외부 주문'(DB에 기록이 없는 주문) 분기 안에만 있었다.
    재기동 후 자기가 낸 주문은 DB에 있으므로 그 분기를 타지 않아, 매 주기 조회에
    잡히면서도 영원히 pending에 오르지 않았다.
    """
    assert not trader.order_manager.is_pending(CODE), "전제: 추적은 비어 있다"

    with patch.object(config.session, 'is_simulation', False), \
         patch.object(trader, 'is_market_open', return_value=True), \
         patch('modules.auto_trade.api.get_unfilled_orders', return_value=[_open_order()]), \
         patch('modules.auto_trade.db_manager.db.get_trade_by_odno', return_value=_known_trade()), \
         patch('modules.auto_trade.db_manager.db.insert_trade') as ins, \
         patch('modules.auto_trade.api.send_telegram_message') as tg, \
         patch('modules.auto_trade.api.revise_cancel_order',
               return_value={'rt_cd': '0', 'output': {'ODNO': 'C1'}}):
        trader.order_manager.manage_unfilled_orders()
        # 자기 주문을 '외부 주문'으로 오인해 DB에 다시 넣거나 알림을 쏘면 안 된다.
        assert not any("외부" in str(c) for c in ins.call_args_list), "자기 주문을 외부로 기록했다"
        assert not any("외부접수" in str(c) for c in tg.call_args_list), "자기 주문을 외부로 알렸다"

    assert trader.order_manager.is_pending(CODE), \
        "DB에 기록이 있다는 이유로 추적 복원을 건너뛰었다"


def test_cycle_sweep_clears_the_buy_hold_once_it_can_see_orders(trader):
    """복구 조회가 한 번이라도 성공하면 매수 보류가 자동으로 풀려야 한다."""
    trader.pending_restore_ok = False
    with patch.object(config.session, 'is_simulation', False), \
         patch.object(trader, 'is_market_open', return_value=True), \
         patch('modules.auto_trade.api.get_unfilled_orders', return_value=[]), \
         patch('modules.auto_trade.api.send_telegram_message'):
        trader.order_manager.manage_unfilled_orders()
    assert trader.pending_restore_ok is True, "운영자 개입 없이는 매수가 안 풀린다"


# ─────────────────── ③ 모르면 사지 않는다 ───────────────────

def test_buys_are_held_while_the_open_order_state_is_unknown(trader):
    """복구 조회 실패 상태에서는 신규 매수를 내지 않는다.

    모르는 채로 주문을 더 내는 것이 가장 나쁘다 — 이미 걸린 주문 위에 겹쳐 쌓인다.
    """
    trader.pending_restore_ok = False
    # 게이트 말고는 어디서도 멈추지 않도록 전제를 다 채운다. 종목 목록이 비어 있으면
    # 게이트를 지워도 `if not targets: return`에 걸려 통과해 버린다(자기참조 테스트).
    with patch.object(config.session, 'stock_data',
                      {'stocks_kr': [{'code': CODE, 'name': NAME}]}), \
         patch('modules.auto_trade.db_manager.db.get_trades', return_value=[]), \
         patch('modules.auto_trade.db_manager.db.get_latest_buy_trades', return_value={}), \
         patch('modules.auto_trade.db_manager.db.get_all_stock_strategies', return_value=[]), \
         patch.object(trader, '_analyze_candidates', return_value=[]) as analyze, \
         patch.object(trader, '_execute_buy_orders') as execute:
        trader._check_buy_conditions([], {'d2_deposit': 10_000_000, 'deposit': 10_000_000})
    assert not analyze.called and not execute.called, "미체결 현황을 모르는데 매수했다"


def test_buys_proceed_once_the_state_is_known(trader):
    """대조군 — 복구가 됐으면 매수 경로는 평소대로 흘러야 한다(보류가 상시가 되면 안 된다)."""
    trader.pending_restore_ok = True
    with patch.object(config.session, 'stock_data',
                      {'stocks_kr': [{'code': CODE, 'name': NAME}]}), \
         patch.object(trader, '_analyze_candidates', return_value=[]) as analyze, \
         patch('modules.auto_trade.db_manager.db.get_trades', return_value=[]), \
         patch('modules.auto_trade.db_manager.db.get_latest_buy_trades', return_value={}), \
         patch('modules.auto_trade.db_manager.db.get_all_stock_strategies', return_value=[]):
        trader._check_buy_conditions([], {'d2_deposit': 10_000_000, 'deposit': 10_000_000})
    assert analyze.called, "정상 상태인데 매수 경로가 막혔다 — 보류 조건이 너무 넓다"


def test_a_restored_order_keeps_the_candidate_out(trader):
    """복원된 pending은 실제로 매수 후보를 걸러내야 한다(복원이 무의미해지면 안 된다)."""
    with patch('modules.auto_trade.api.get_domestic_open_orders',
               return_value=[_open_order()]):
        trader.order_manager.restore_pending_orders()

    item = {'code': CODE, 'name': NAME, 'group': 'stocks_kr'}
    with patch.object(trader, 'is_market_open', return_value=True):
        result = trader._analyze_candidate_worker(
            item, holding_codes=set(), rules_map={}, restricted_stocks=set(),
            market_regime_adj=0, safe_delay=0, reentry_hurdles={},
            holdings_dfs={}, holding_groups_map={})
    assert result is None, "이미 주문이 걸린 종목이 매수 후보로 올라왔다"
