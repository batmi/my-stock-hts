"""[카오스] 판정이 불가능한 장애 상황에서 안전 쪽으로 닫히는가.

판정 로직이 옳은 것과, 판정을 못 하는 상황에서 안전하게 멈추는 것은 별개다. 실매매의
사고는 대부분 후자에서 난다. 여기서는 지수 조회 경로에 장애를 **실제로 주입해** 다음을
확인한다. 코드를 읽고 맞다고 판단하는 것과 주입해서 확인하는 것은 다르다.

  · 지수를 못 읽으면 신규 매수는 보류되고(fail-closed) 그 사실이 unknown 플래그로 남는가
  · 정상 데이터에서는 반대로 제대로 열리는가 (테스트가 무조건 통과하지 않도록 하는 대조군)
  · 판단 불가 상태에서 매수 후보 스캔이 실제로 종목을 걸러내는가
  · 피라미딩 증액도 같은 기준으로 막히는가
  · 매도·손절은 시장 판정과 무관하게 살아 있는가

기준 문서: trader._update_market_indices_status / _analyze_candidate_worker 주석
('모르겠으면 아무것도 하지 마라').
"""
import numpy as np
import pandas as pd
import pytest
from unittest.mock import MagicMock, patch

import config
from modules.auto_trade import AutoTrader


@pytest.fixture
def trader():
    t = AutoTrader()
    t.is_running = True
    t.market_index_status = {}
    t.market_status_notified = {}
    return t


def _good_index_df(n=400):
    """정상 지수 일봉 — 상승 추세라 시장 필터가 열려야 한다."""
    close = np.linspace(2000.0, 3000.0, n)
    return pd.DataFrame({
        'date': pd.date_range('2024-01-01', periods=n).strftime('%Y%m%d'),
        'open': close, 'high': close * 1.01, 'low': close * 0.99,
        'close': close, 'volume': np.full(n, 1000),
    })


def _boom(*_a, **_k):
    raise RuntimeError("index feed down")


# ---------------------------------------------------------------------------
# 1. 지수 조회 장애 주입 → 판단 불가로 닫히는가
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("failure,label", [
    (lambda *_a, **_k: None, "응답 없음(None)"),
    (lambda *_a, **_k: pd.DataFrame(), "빈 응답"),
    (lambda *_a, **_k: _good_index_df(10), "데이터 부족(SMA 미충족)"),
    (_boom, "예외 발생"),
])
@patch('modules.auto_trade.api.send_telegram_message')
def test_index_failure_closes_buy_gate(mock_tg, trader, failure, label):
    with patch('modules.auto_trade.analysis.get_domestic_index_data', side_effect=failure):
        trader._update_market_indices_status(notify=False)

    for market in ("KOSPI", "KOSDAQ"):
        stat = trader.market_index_status.get(market)
        assert isinstance(stat, dict), f"{label}: 상태가 기록되지 않았다"
        assert stat.get('is_healthy') is False, f"{label}: 매수 게이트가 열려 있다"
        assert stat.get('unknown') is True, f"{label}: 판단 불가 표시가 없다"


@patch('modules.auto_trade.api.send_telegram_message')
def test_healthy_index_opens_gate(mock_tg, trader):
    """대조군 — 정상 데이터에서는 열려야 한다. (위 테스트가 무조건 통과하지 않게 하는 장치)"""
    with patch('modules.auto_trade.analysis.get_domestic_index_data',
               side_effect=lambda *_a, **_k: _good_index_df()):
        trader._update_market_indices_status(notify=False)

    for market in ("KOSPI", "KOSDAQ"):
        stat = trader.market_index_status.get(market)
        assert stat.get('is_healthy') is True, "정상 지수인데 매수가 막혔다"
        assert stat.get('unknown') is False


# ---------------------------------------------------------------------------
# 2. 판단 불가 상태에서 매수 후보 스캔이 실제로 걸러내는가
# ---------------------------------------------------------------------------
def _worker(trader, code="005930", name="삼성전자"):
    return trader._analyze_candidate_worker(
        {'code': code, 'name': name}, holding_codes=[], rules_map={},
        restricted_stocks={}, market_regime_adj=0.0, safe_delay=0,
        reentry_hurdles={}, holdings_dfs={}, holding_groups_map={})


@pytest.mark.parametrize("status,label", [
    ({}, "상태 캐시 없음(첫 주기 전·조회 실패)"),
    ({"KOSPI": {"is_healthy": False, "unknown": True, "current": 0}}, "판단 불가"),
    ({"KOSPI": {"is_healthy": False, "unknown": False, "current": 2500}}, "약세 확정"),
])
def test_candidate_scan_skips_when_market_not_healthy(trader, status, label):
    trader.market_index_status = status
    with patch.object(config, 'USE_MARKET_FILTER', True), \
         patch.object(trader, '_get_stock_market_type', return_value="KOSPI"), \
         patch.object(trader, 'set_stock_state'):
        res = _worker(trader)
    assert res is not None and res.get('type') == 'market_skip', \
        f"{label}: 시장 판정이 닫혔는데 매수 후보 분석이 진행됐다"


def test_candidate_scan_proceeds_when_healthy(trader):
    """대조군 — 시장이 정상이면 게이트에서 멈추지 않고 다음 단계로 넘어가야 한다."""
    trader.market_index_status = {"KOSPI": {"is_healthy": True, "unknown": False, "current": 2500}}
    with patch.object(config, 'USE_MARKET_FILTER', True), \
         patch.object(trader, '_get_stock_market_type', return_value="KOSPI"), \
         patch.object(trader, 'set_stock_state'), \
         patch('modules.auto_trade.api.get_chart_data', return_value=None):
        res = _worker(trader)
    assert res is None or res.get('type') != 'market_skip', \
        "정상 시장인데 시장 필터에서 막혔다"


# ---------------------------------------------------------------------------
# 3. 피라미딩 증액도 같은 기준으로 막히는가
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("status,label", [
    ({}, "상태 캐시 없음"),
    ({"KOSPI": {"is_healthy": False, "unknown": True, "current": 0}}, "판단 불가"),
])
@patch('modules.auto_trade.api.send_telegram_message')
def test_pyramiding_blocked_when_market_unknown(mock_tg, trader, status, label):
    trader.market_index_status = status
    strategy = MagicMock()
    strategy.analyze_pyramid.return_value = (True, "증액 조건 충족")
    trader.strategy = strategy
    order_manager = MagicMock()
    order_manager.is_pending.return_value = False
    trader.order_manager = order_manager

    trader.buy_halted = False
    with patch.object(config, 'USE_MARKET_FILTER', True), \
         patch.dict(config.ANALYSIS_THRESHOLDS, {"PYRAMIDING_REQUIRE_HEALTHY_MARKET": True}), \
         patch.object(trader, '_get_stock_market_type', return_value="KOSPI"), \
         patch('modules.auto_trade.api.fetch_buyable_quantity') as mock_buyable:
        trader._try_pyramid_buy(
            code="005930", name="삼성전자", held_qty=10, current_price=70000,
            profit_rate=12.0, result={'state': '매수', 'score': 8.0},
            last_buy=None, is_market_open=True)

    strategy.analyze_pyramid.assert_called_once()   # 게이트 전까지는 진행됐음을 확인
    mock_buyable.assert_not_called(), f"{label}: 판단 불가인데 매수가능수량을 조회했다"
    assert all('place_order' not in str(c) for c in order_manager.method_calls), \
        f"{label}: 판단 불가인데 증액 주문이 나갔다"


# ---------------------------------------------------------------------------
# 4. 매도·손절은 시장 판정과 무관하게 살아 있는가
# ---------------------------------------------------------------------------
@patch('modules.auto_trade.api.send_telegram_message')
def test_sell_path_untouched_by_index_failure(mock_tg, trader):
    """지수를 못 읽어도 청산 판단은 계속돼야 한다 — 못 사는 것과 못 파는 것은 위험이 다르다."""
    with patch('modules.auto_trade.analysis.get_domestic_index_data', side_effect=_boom):
        trader._update_market_indices_status(notify=False)

    strategy = MagicMock()
    strategy.analyze_sell.return_value = {'action': 'sell', 'reason': '손절'}
    trader.strategy = strategy

    df = _good_index_df(300)
    with patch('modules.auto_trade.api.get_chart_data', return_value=df):
        res = trader.strategy.analyze_sell(
            "005930", "삼성전자", df, 70000, 80000, 10, -12.5)

    assert res is not None and res['action'] == 'sell', \
        "지수 장애가 매도 판단까지 막았다 — 손절이 죽으면 fail-closed가 아니라 fail-deadly다"
