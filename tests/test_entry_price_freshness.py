"""매수 진입가는 실시간가여야 한다 — 조회 실패를 조용히 직전 종가로 대신하지 않는다.

2026-09-04 감사: `_analyze_candidate_worker` 는 실시간 현재가를 못 받으면(get_current_price
는 실패해도 예외가 아니라 **0** 을 돌려준다) 조용히 `df.iloc[-1]['close']`(직전 확정 종가)로
폴백했다. 그 값 하나가 아래 세 가지를 동시에 정한다:
  - 주문 지정가 (`current_price * (1 + SLIPPAGE_RATE)`)
  - ATR 손절폭 (`atr_stop_rate(atr, price)` — price 가 분모)
  - 포지션 수량 (`allocate_budget(current_price=...)`, `invest_amt / order_price`)
관심종목 44개·1년 실측으로 그 오차는 중앙값 2.0%·90분위 6.8%·최대 30.0%다.
"""
from unittest.mock import patch

import pytest

import config
from modules import auto_trade


@pytest.fixture
def trader():
    auto_trade.AutoTrader._instance = None
    t = auto_trade.AutoTrader()
    t.is_running = True
    yield t
    auto_trade.AutoTrader._instance = None


def _cand(**over):
    base = {'code': '005930', 'name': '삼성전자', 'price': 10000, 'score': 9.0,
            'rsi': 50, 'adx': 30, 'cci': 100, 'vol_strength': 120, 'atr': 200,
            'price_is_realtime': True}
    base.update(over)
    return base


@patch('modules.auto_trade.api.get_price_limits', return_value=(0, 0))
@patch('modules.auto_trade.api.place_order')
@patch('modules.auto_trade.api.fetch_buyable_quantity', return_value=10)
def test_stale_price_candidate_is_not_ordered(mock_qty, mock_place, mock_limits, trader):
    """실시간가를 못 받은 후보는 주문이 나가지 않는다."""
    mock_place.return_value = {'rt_cd': '0', 'output': {'ODNO': '1'}}
    trader._execute_buy_orders([_cand(price_is_realtime=False)], 1_000_000, 0.5, 0, 5)
    mock_place.assert_not_called()


@patch('modules.auto_trade.api.get_price_limits', return_value=(0, 0))
@patch('modules.auto_trade.api.place_order')
@patch('modules.auto_trade.api.fetch_buyable_quantity', return_value=10)
def test_stale_price_skip_is_logged(mock_qty, mock_place, mock_limits, trader):
    """조용히 건너뛰지 않는다 — 왜 안 샀는지 로그에 남는다."""
    seen = []
    with patch.object(trader, 'log', side_effect=lambda m: seen.append(m)):
        trader._execute_buy_orders([_cand(price_is_realtime=False)], 1_000_000, 0.5, 0, 5)
    assert any("실시간 현재가 조회 실패" in m for m in seen), seen


@patch('modules.auto_trade.api.get_price_limits', return_value=(0, 0))
@patch('modules.auto_trade.api.place_order')
@patch('modules.auto_trade.api.fetch_buyable_quantity', return_value=10)
def test_fresh_price_candidate_still_orders(mock_qty, mock_place, mock_limits, trader):
    """정상 후보는 종전과 같이 주문된다(게이트가 과잉 차단하지 않는다)."""
    mock_place.return_value = {'rt_cd': '0', 'output': {'ODNO': '1'}}
    trader._execute_buy_orders([_cand()], 1_000_000, 0.5, 0, 5)
    mock_place.assert_called_once()


@patch('modules.auto_trade.api.get_price_limits', return_value=(0, 0))
@patch('modules.auto_trade.api.place_order')
@patch('modules.auto_trade.api.fetch_buyable_quantity', return_value=10)
def test_missing_flag_defaults_to_orderable(mock_qty, mock_place, mock_limits, trader):
    """플래그가 없는 후보(옛 경로·수동 호출)는 종전 동작을 유지한다."""
    mock_place.return_value = {'rt_cd': '0', 'output': {'ODNO': '1'}}
    cand = _cand()
    cand.pop('price_is_realtime')
    trader._execute_buy_orders([cand], 1_000_000, 0.5, 0, 5)
    mock_place.assert_called_once()


@patch('modules.auto_trade.api.get_price_limits', return_value=(0, 0))
@patch('modules.auto_trade.api.place_order')
@patch('modules.auto_trade.api.fetch_buyable_quantity', return_value=10)
def test_stale_candidate_does_not_consume_heat_budget(mock_qty, mock_place, mock_limits, trader):
    """보류된 후보가 포트폴리오 리스크 예산을 잡아먹으면 뒤의 정상 후보가 막힌다."""
    mock_place.return_value = {'rt_cd': '0', 'output': {'ODNO': '1'}}
    trader.portfolio_heat_amt = 0.0
    before = trader.portfolio_heat_amt
    trader._execute_buy_orders([_cand(price_is_realtime=False)], 1_000_000, 0.5, 0, 5)
    assert trader.portfolio_heat_amt == before


def test_analyzer_marks_the_price_source():
    """분석 워커가 실시간/직전종가를 구분해 후보에 실어 보낸다 — 소스로 고정한다."""
    import inspect

    src = inspect.getsource(auto_trade.AutoTrader._analyze_candidate_worker)
    assert "price_is_realtime = bool(realtime_price and realtime_price > 0)" in src
    assert "'price_is_realtime': price_is_realtime," in src
    #  폴백 자체는 남는다(점수·표시는 확정 종가 기준이라 그대로 계산해야 한다).
    assert "float(df.iloc[-1]['close'])" in src


def test_execute_gate_precedes_sizing():
    """게이트는 예산·수량 계산보다 **앞**에 있어야 한다 — 뒤에 두면 이미 어긋난 값으로 계산된다."""
    import inspect

    src = inspect.getsource(auto_trade.AutoTrader._execute_buy_orders)
    gate = src.index("price_is_realtime")
    assert gate < src.index("allocate_budget")
    assert gate < src.index("adjust_to_tick")
