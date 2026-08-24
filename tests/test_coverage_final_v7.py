# tests/test_coverage_final_v7.py
import pytest
from unittest.mock import patch, MagicMock, ANY
import pandas as pd
import numpy as np
from modules import auto_trade, analysis, backtest, account, market, db_manager
import api
import config
from core import utils
from core import context
from datetime import datetime

@pytest.fixture(autouse=True)
def cleanup_db_connection():
    yield
    db_manager.db.close_connection()

# --- modules/auto_trade.py ---

@patch('modules.auto_trade.api.send_telegram_message')
def test_autotrader_stop_force(mock_tg):
    """AutoTrader 강제 종료 테스트 (스레드 alive 상태)"""
    trader = auto_trade.AutoTrader()
    trader.is_running = True
    # Mock thread to be alive
    mock_thread = MagicMock()
    mock_thread.is_alive.return_value = True
    trader.thread = mock_thread
    
    with patch('config.console.print'):
        trader.stop(use_status=False)
    
    assert trader.is_running is False
    mock_thread.join.assert_called()

def test_risk_manager_allocate_budget_zero_asset():
    """초기 자산 0일 때 예산 할당 테스트"""
    trader = auto_trade.AutoTrader()
    trader.initial_asset = 0
    rm = auto_trade.RiskManager(trader)
    
    # 가용 현금 100만원, 비중 10% -> 10만원 할당
    amt = rm.allocate_budget(1000000, 0.1)
    assert amt == 100000

@patch('modules.auto_trade.api.get_domestic_balance')
def test_get_holdings_message_exception(mock_balance):
    """보유 종목 메시지 생성 중 예외 처리"""
    trader = auto_trade.AutoTrader()
    mock_balance.side_effect = Exception("API Error")
    
    msg = trader._get_holdings_message("12345678")
    assert "조회 실패" in msg

@patch('modules.auto_trade.api.fetch_sellable_quantity')
@patch('modules.auto_trade.api.get_chart_data')
@patch('modules.auto_trade.DefaultStrategy.analyze_sell')
@patch('modules.auto_trade.api.send_telegram_message')
@patch('modules.auto_trade.db_manager.db.insert_trade')
@patch('modules.auto_trade.db_manager.db.update_highest_price')
@patch('modules.auto_trade.db_manager.db.get_highest_price')
@patch('modules.auto_trade.db_manager.db.delete_trailing_stop')
def test_check_sell_conditions_trailing_stop(mock_del, mock_get_high, mock_update_high, mock_insert, mock_tg, mock_analyze, mock_chart, mock_qty):
    """트레일링 스탑 발동 테스트"""
    trader = auto_trade.AutoTrader()
    trader.is_running = True
    
    holdings = [{
        'pdno': '005930', 'prdt_name': 'Samsung', 'ord_psbl_qty': '10',
        'evlu_pfls_rt': '15.0', 'prpr': '11000', 'pchs_avg_pric': '10000',
        'evlu_pfls_amt': '10000'
    }]
    
    mock_qty.return_value = 10
    mock_chart.return_value = pd.DataFrame({'close': [11000], 'open': [11000], 'high': [11500], 'low': [10500]})
    
    # 최고가 12000원 (20% 수익) -> 현재 11000원 (10% 수익) -> 고점 대비 약 8.3% 하락
    # 설정: 발동 10%, 콜백 3% -> 매도 조건 충족
    mock_get_high.return_value = 12000
    
    # Strategy returns sell due to TS
    mock_analyze.return_value = {
        'action': 'sell', 'reason': '트레일링스탑', 'score': 5.0, 'state': '보유', 'ind': {}
    }
    
    with patch.object(trader.order_manager, 'is_pending', return_value=False):
        with patch.object(trader.order_manager, 'send_order', return_value='12345') as mock_send:
            trader._check_sell_conditions(holdings, is_market_open=True)
            
            mock_send.assert_called()
            args, kwargs = mock_send.call_args
            assert "트레일링스탑" in kwargs.get('reason', '')

# --- modules/analysis.py ---

def test_calculate_score_missing_indicators():
    """지표 누락 시 점수 계산 테스트"""
    # All None
    score, details = analysis.calculate_score(10000, None, None, None, None, None, None, None, False)
    assert score == 0
    
    # Partial None (Only Price and EMA20)
    # Price(10000) > EMA20(9000) -> Trend Score +0.5
    score, details = analysis.calculate_score(10000, 9000, None, None, None, None, None, None, False, weights={"TREND": 4.0, "MOMENTUM": 2.5, "STRENGTH": 1.5, "SYNERGY": 2.0})
    assert score == 0.5

def test_classify_stock_state_neutral():
    """관망 상태 분류 테스트"""
    # 점수가 낮고(5.0 미만 가정), 위험/주의 조건이 아님
    # Config 오염 방지를 위해 임계값 강제 설정 (RISE_SCORE=6.0)
    with patch.dict(config.ANALYSIS_THRESHOLDS, {"BUY_SCORE": 8.0, "RISE_SCORE": 6.0, "BUY_RSI_MAX": 65}):
        state, color, reason = analysis.classify_stock_state(
            10000, 10000, 10000, 10000, 9000, 50, 50, 20, 0, False, macd=0, macd_signal=0
        )
        assert state == "관망"

# --- modules/backtest.py ---

def test_calculate_daily_status_sell_condition():
    """백테스트 일별 상태 계산 - 매도 조건"""
    row = pd.Series({
        'close': 9000, 'open': 9000, 'EMA20': 10000, 'EMA60': 11000, 'EMA120': 12000,
        'SAR': 13000, 'RSI': 30, 'ADX': 20, 'CCI': -100, 'OBV': 1000, 'OBV_MA': 1000,
        'MACD': -10, 'MACD_Signal': 0
    })
    prev_row = pd.Series({'RSI': 35})
    
    raw, sell, can_buy, state, reason = backtest.calculate_daily_status(row, prev_row)
    assert state == "매도"
    assert sell == 0 # 매도 상태면 sell_check_score는 0

# --- api.py ---

@patch('api.fetch_yfinance_data')
def test_get_chart_data_index_logic(mock_fetch):
    """지수 차트 데이터 조회 로직 테스트"""
    # Mock response
    df = pd.DataFrame({
        'Date': pd.date_range(end=datetime.now(), periods=10),
        'Close': [100]*10, 'Open': [100]*10, 'High': [100]*10, 'Low': [100]*10, 'Volume': [100]*10
    })
    mock_fetch.return_value = df
    
    res = api.get_chart_data('^KS11')
    assert not res.empty
    assert 'close' in res.columns

# --- utils.py ---

def test_get_tick_size_boundary():
    """호가 단위 경계값 테스트"""
    assert utils.get_tick_size(1990, False) == 1
    assert utils.get_tick_size(2000, False) == 5
    assert utils.get_tick_size(4990, False) == 5
    assert utils.get_tick_size(5000, False) == 10
    assert utils.get_tick_size(9990, False) == 10
    assert utils.get_tick_size(10000, False) == 10
    assert utils.get_tick_size(49950, False) == 50
    assert utils.get_tick_size(50000, False) == 100
    assert utils.get_tick_size(99900, False) == 100
    assert utils.get_tick_size(100000, False) == 100
    assert utils.get_tick_size(499500, False) == 500
    assert utils.get_tick_size(500000, False) == 1000