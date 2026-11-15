"""신호 원장이 '왜 안 샀나'에 답하는가 — 계좌 상태 차단의 배선.

[이 파일의 근거는 추정이 아니라 실제 운용 기록이다]
db/paper_trading.db 의 2026-08-27·28 을 보면 대한항공이 285~287주기 내내 `passed`인데
매수는 0건이다. 슬롯이 4/4로 꽉 차 있었기 때문인데, **원장 어디에도 그 사실이 없다.**
사유는 로그에만 남았고, 로그 파싱은 이 원장을 만든 이유 자체(차단율을 1.3%→75%로
뒤집어 읽은 적이 있다)라 되돌아갈 수 없다.

그 상태의 원장을 읽으면 "신호가 287번 섰다"만 보인다. 슬롯이나 시드를 늘리면 그만큼
더 샀을 것처럼 읽혀, 노출 확대 판단의 근거가 통째로 부풀 수 있다.

여기서 고정하는 것은 **배선**이다 — 계좌 상태 사유가 판정 지점에서 원장까지 실제로
도달하는가. 카운터 자체의 셈법은 tests/test_signal_ledger.py 가 맡는다.
"""
from unittest.mock import MagicMock, patch

import numpy as np
import pandas as pd
import pytest

import config
from modules.auto_trade import AutoTrader


@pytest.fixture
def trader():
    t = AutoTrader()
    t.is_running = True
    t.market_index_status = {"KOSPI": {"is_healthy": True, "unknown": False, "current": 2500}}
    t.market_status_notified = {}
    return t


def _chart(n=300):
    close = np.linspace(50_000.0, 90_000.0, n)
    return pd.DataFrame({
        'date': pd.date_range('2024-01-01', periods=n).strftime('%Y%m%d'),
        'open': close, 'high': close * 1.01, 'low': close * 0.99,
        'close': close, 'volume': np.full(n, 500_000.0),
    })


def _worker(trader, buy_block=None, state="매수", action="buy"):
    """분석 워커를 한 종목에 대해 돌리고 원장 행을 돌려준다."""
    result = {'score': 8.0, 'state': state, 'state_reason': '', 'action': action,
              'rsi': 60.0, 'adx': 30.0, 'cci': 100.0, 'atr': 1000.0,
              'vol_strength': 150.0, 'ask_bid_ratio': 2.0, 'w52_pos': 0.8,
              'trend_quality': 50.0, 'vol_reject_reason': None,
              'macd': 1.0, 'macd_signal': 0.5, 'obv_trend': True, 'smart_money': True}
    with patch.object(config, 'USE_MARKET_FILTER', False), \
         patch.object(trader, '_get_stock_market_type', return_value="KOSPI"), \
         patch.object(trader, 'set_stock_state'), \
         patch('modules.auto_trade.api.get_chart_data', return_value=_chart()), \
         patch('modules.auto_trade.api.get_current_price', return_value=90_000), \
         patch('modules.auto_trade.api.get_realtime_vol_strength', return_value=150.0), \
         patch('modules.auto_trade.api.get_ask_bid_ratio', return_value=2.0), \
         patch.object(trader.strategy, 'analyze_buy', return_value=result):
        res = trader._analyze_candidate_worker(
            {'code': '003490', 'name': '대한항공'}, holding_codes=[], rules_map={},
            restricted_stocks={}, market_regime_adj={}, safe_delay=0,
            reentry_hurdles={}, holdings_dfs={}, holding_groups_map={},
            buy_block=buy_block)
    return (res or {}).get('ledger')


def test_a_full_slot_reaches_the_ledger(trader):
    """[핵심] 슬롯 만석으로 못 산 사실이 원장까지 간다 — 2026-08-27 대한항공의 상황."""
    row = _worker(trader, buy_block='slot')
    assert row is not None, "매수 상태인데 원장 행이 만들어지지 않았다"
    assert row['outcome'] == 'passed', "게이트 판정은 그대로 남아야 한다"
    assert row.get('blocked_by') == 'slot'


def test_insufficient_cash_reaches_the_ledger(trader):
    row = _worker(trader, buy_block='cash')
    assert row.get('blocked_by') == 'cash'


def test_an_unblocked_cycle_carries_no_account_reason(trader):
    """대조군 — 살 수 있었던 주기에 차단 사유가 붙으면 기회비용이 부풀어 오른다."""
    row = _worker(trader, buy_block=None)
    assert row['outcome'] == 'passed'
    assert 'blocked_by' not in row


def test_a_gated_signal_is_not_blamed_on_the_account(trader):
    """게이트가 이미 막은 신호는 계좌 탓이 아니다(슬롯을 늘려도 안 샀을 종목)."""
    row = _worker(trader, buy_block='slot', action='hold')
    assert row is not None and row['outcome'] != 'passed'
    assert 'blocked_by' not in row


def test_a_non_signal_writes_no_row_at_all(trader):
    """매수 상태가 아니었던 종목은 원장에 들어가지 않는다(차단율 분모 보호)."""
    assert _worker(trader, buy_block='slot', state='관망', action='hold') is None


def test_the_block_reason_survives_the_pipeline_to_the_database(trader):
    """[배선 전체] 워커가 만든 사유가 record_signal_ledger 호출까지 살아서 간다."""
    from modules import db_manager
    rows = [{'code': '003490', 'name': '대한항공', 'outcome': 'passed',
             'score': 8.0, 'state': '매수', 'vol': 200.0, 'abr': 1.5,
             'blocked_by': 'slot'}]
    with patch.object(db_manager.db, 'record_signal_ledger') as rec:
        db_manager.db.record_signal_ledger('20260827', rows)
    assert rec.call_args[0][1][0]['blocked_by'] == 'slot'


# ───────────────────── 사유를 정하는 지점 ─────────────────────
#  위 테스트들은 사유가 '전달되는가'를 본다. 여기서는 **어떤 사유가 붙는가**를 본다 —
#  슬롯과 예수금이 뒤바뀌어도 위 테스트는 전부 통과하기 때문이다.

def _run_buy_cycle(trader, holdings, cash):
    """_check_buy_conditions 를 한 바퀴 돌리고 워커에 넘어간 buy_block 을 돌려준다."""
    seen = {}

    def _capture(*a, **kw):
        seen['buy_block'] = kw.get('buy_block')
        return []

    stocks = [{'code': '003490', 'name': '대한항공'}]
    with patch.object(config.session, 'stock_data', {'stocks_kr': stocks}), \
         patch.object(config.settings, 'SYSTEM_MAX_HOLDINGS', 4), \
         patch.object(trader, '_analyze_candidates', side_effect=_capture), \
         patch.object(trader, 'log'), \
         patch('modules.auto_trade.buying.entry_open_delay_remaining', return_value=0), \
         patch('modules.auto_trade.db_manager.db.get_trades', return_value=[]), \
         patch('modules.auto_trade.db_manager.db.get_latest_buy_trades', return_value={}), \
         patch('modules.auto_trade.db_manager.db.get_all_stock_strategies', return_value=[]):
        trader.buy_halted = False
        trader.pending_restore_ok = True
        trader._check_buy_conditions(holdings, {'d2_deposit': cash}, rules_map={})
    return seen.get('buy_block', '(호출 안 됨)')


def _hold(code, name):
    return {'pdno': code, 'prdt_name': name, 'hldg_qty': '10'}


def test_a_full_portfolio_is_reported_as_a_slot_block(trader):
    """[핵심] 2026-08-27 상황 그대로 — 4/4 만석에서 신호가 서면 사유는 'slot'이다."""
    full = [_hold('161890', '한국콜마'), _hold('017670', 'SK텔레콤'),
            _hold('006400', '삼성SDI'), _hold('006360', 'GS건설')]
    assert _run_buy_cycle(trader, full, cash=5_000_000) == 'slot'


def test_an_empty_wallet_is_reported_as_a_cash_block(trader):
    """슬롯은 비었는데 예수금이 없다 — 슬롯을 늘려도 못 산다. 사유가 갈려야 한다."""
    assert _run_buy_cycle(trader, [_hold('161890', '한국콜마')], cash=500) == 'cash'


def test_a_healthy_account_reports_no_block(trader):
    assert _run_buy_cycle(trader, [_hold('161890', '한국콜마')], cash=5_000_000) is None


# ───────────────────── 주문 단계에서 접은 후보 (2026-11-15) ─────────────────────
#  위 사유들은 후보 분석 **앞에서** 이미 아는 계좌 상태다(만석·빈 지갑). 여기는 그 뒤다 — 게이트를
#  통과해 후보가 됐고 슬롯·예수금도 있었는데, 주문을 짜다가 접은 경우. 원장은 그 전에 이미 '통과'로
#  쓰였으므로, 접은 사유를 따로 얹지 않으면 '통과했는데 매수 0'만 남는다.
#  실제 기록: 2026-10-23 가상투자 삼성바이오로직스 — 통과 4주기·차단 0·매수 0
#  (로그: "1주 1,478,000원이 배분액의 2.3배(상한 1.3배)").

ATR_STOP = {"USE_ATR_STOP": True, "STOP_LOSS_RATE": -7.0, "ATR_STOP_MULTIPLIER": 2.0}


def _cand(code, name, price=70_000, score=9.0):
    return {'code': code, 'name': name, 'price': price, 'score': score, 'rsi': 50, 'adx': 30,
            'cci': 100, 'is_custom_rule': False, 'atr': price * 0.02}


def _execute(trader, cands, *, cash=10_000_000, held=0, slots=4, alloc=None, budget_left=None,
             heat_unknown=False):
    """주문 집행부를 한 번 돌리고 (주문 Mock, 원장에 얹은 사유 dict | None) 을 돌려준다."""
    from modules import db_manager
    trader.buy_halted = False
    trader.pending_restore_ok = True
    trader.initial_asset = 10_000_000
    trader.current_total_asset = 10_000_000
    trader.portfolio_heat_amt = 0.0
    trader.portfolio_heat_unknown = heat_unknown
    patches = [
        patch.dict(config.SELL_STRATEGY, ATR_STOP),
        patch.object(trader, '_clamp_order_price', side_effect=lambda c, p: p),
        patch.object(trader, 'log'),
        patch('modules.auto_trade.api.fetch_buyable_quantity', return_value=1000),
        patch('modules.auto_trade.db_manager.db.delete_trailing_stop', return_value=True),
        patch('modules.auto_trade.db_manager.db.delete_half_tp', return_value=True),
        patch('modules.auto_trade.db_manager.db.cancel_reserved_buy_orders', return_value=0),
    ]
    if alloc is not None:
        patches.append(patch.object(trader.risk_manager, 'allocate_budget', return_value=alloc))
    if budget_left is not None:
        patches.append(patch.object(trader.risk_manager, 'portfolio_risk_budget_left', return_value=budget_left))
    for p in patches:
        p.start()
    try:
        with patch.object(trader.order_manager, 'send_order', return_value="ODNO1") as order, \
             patch.object(db_manager.db, 'add_signal_ledger_account_blocks') as rec:
            trader._execute_buy_orders(cands, cash, 0.25, held, slots)
    finally:
        for p in reversed(patches):
            p.stop()
    return order, (rec.call_args[0][1] if rec.called else None)


def test_a_share_too_expensive_for_the_allocation_reaches_the_ledger(trader):
    """[핵심] 10-23 삼성바이오로직스 — 1주 값이 배분액 상한을 넘어 접은 사실이 원장까지 간다."""
    order, blocks = _execute(trader, [_cand('207940', '삼성바이오로직스', price=1_478_000)], alloc=640_000)
    assert not order.called
    assert blocks == {'207940': 'size'}


def test_a_heat_cap_hold_reaches_the_ledger(trader):
    """총 리스크 한도에 남은 예산이 1주에도 못 미쳐 접은 경우."""
    order, blocks = _execute(trader, [_cand('005930', '삼성전자')], budget_left=100)
    assert not order.called
    assert blocks == {'005930': 'heat'}


def test_a_heat_hold_from_missing_data_is_not_blamed_on_the_cap(trader):
    """한도에 '닿은' 것과 한도를 '계산 못 한' 것은 다르다 — 뒤쪽은 데이터 결손이라 계좌 사유로 세지 않는다."""
    order, blocks = _execute(trader, [_cand('005930', '삼성전자')], budget_left=100, heat_unknown=True)
    assert not order.called
    assert blocks is None


def test_a_candidate_outranked_for_the_last_slot_is_a_slot_block(trader):
    """같은 주기의 앞 순위가 마지막 슬롯을 쓰면, 뒤 순위는 '슬롯'에 막힌 것이다(순위에서 밀린 후보)."""
    cands = [_cand('005930', '삼성전자', score=9.5), _cand('000660', 'SK하이닉스'), _cand('035420', 'NAVER')]
    order, blocks = _execute(trader, cands, held=3, slots=4)
    assert order.call_count == 1 and order.call_args[0][0] == '005930'
    assert blocks == {'000660': 'slot', '035420': 'slot'}


def test_running_out_of_cash_mid_cycle_is_a_cash_block(trader):
    order, blocks = _execute(trader, [_cand('005930', '삼성전자'), _cand('000660', 'SK하이닉스')], cash=900)
    assert not order.called
    assert blocks == {'005930': 'cash', '000660': 'cash'}


def test_an_executed_buy_writes_no_block(trader):
    """대조군 — 산 주기에 사유가 붙으면 '못 산 주기'가 부풀고, 쓰기도 주기마다 한 번씩 는다."""
    order, blocks = _execute(trader, [_cand('005930', '삼성전자')])
    assert order.called
    assert blocks is None


def test_a_ledger_failure_does_not_stop_the_next_cycle(trader):
    """계측 실패는 삼킨다 — 주문 집행부가 예외로 끝나면 주 루프가 그 주기를 오류로 센다."""
    from modules import db_manager
    with patch.object(db_manager.db, 'add_signal_ledger_account_blocks', side_effect=RuntimeError("disk full")), \
         patch.object(trader, 'log'), \
         patch.object(trader.risk_manager, 'allocate_budget', return_value=640_000), \
         patch.dict(config.SELL_STRATEGY, ATR_STOP), \
         patch.object(trader, '_clamp_order_price', side_effect=lambda c, p: p), \
         patch.object(trader.order_manager, 'send_order', return_value="ODNO1"):
        trader.is_running = True
        trader._execute_buy_orders([_cand('207940', '삼성바이오로직스', price=1_478_000)], 10_000_000, 0.25, 0, 4)
