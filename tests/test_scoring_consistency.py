import pytest
from unittest.mock import patch, MagicMock
import pandas as pd
import numpy as np
from datetime import datetime

# Import all necessary modules
import config
from modules import analysis, backtest, trading, reserved_order_monitor, theme_analysis, manage, db_manager

# Fixture to reset DB connection
@pytest.fixture(autouse=True)
def cleanup_db_connection():
    yield
    try:
        db_manager.db.close_connection()
    except: pass

# Test 1: analysis.py - calculate_score fallback logic
def test_calculate_score_fallback_logic():
    """점수 계산 시, 세부 지표가 누락되어도 df만으로 자체 재계산하는지 검증"""
    # Create a DataFrame with enough data for fallback calculations
    df = pd.DataFrame({
        'close': np.random.rand(30) * 100 + 1000,
        'high': np.random.rand(30) * 110 + 1000,
        'low': np.random.rand(30) * 90 + 1000,
        'open': np.random.rand(30) * 100 + 1000,
        'volume': np.random.rand(30) * 10000
    })
    # Call with only df, no ind or other specific indicators
    score, details = analysis.calculate_score(df=df)
    # Just check if it runs without error and returns a float
    assert isinstance(score, float)
    assert score >= 0.0

# Test 2: analysis.py - classify_stock_state with is_yangbong
def test_classify_stock_state_with_is_yangbong():
    """역추세 매수 조건에서 '양봉 마감' 파라미터가 정상 작동하는지 검증"""
    thresholds = {
        "USE_MEAN_REVERSION": True,
        "MR_RSI_MAX": 40.0,
        "MR_DISPARITY_MAX": 90.0
    }
    # Common args
    common_args = {
        "price": 8500, "ema20": 10000, "ema60": 11000, "ema120": 12000,
        "sar": 9000, "rsi": 35.0, "prev_rsi": 30.0, "adx": 20, "cci": -120,
        "obv_trend": False, "thresholds": thresholds
    }
    
    # Case 1: Yangbong -> 역매수
    state_yang, _, _ = analysis.classify_stock_state(**common_args, is_yangbong=True)
    assert state_yang == "역매수"
    
    # Case 2: Not Yangbong -> Not 역매수
    state_eum, _, _ = analysis.classify_stock_state(**common_args, is_yangbong=False)
    assert state_eum != "역매수"

# Test 3: backtest.py - calculate_daily_status passing is_yangbong
def test_backtest_calculate_daily_status_yangbong_pass():
    """백테스팅 중 '양봉' 여부가 상태 판별 함수로 정확히 전달되는지 검증"""
    # Mock row data for a "yangbong" candle
    row = pd.Series({'close': 100, 'open': 90, 'EMA20': 100, 'EMA60': 100, 'EMA120': 100, 'RSI': 35, 'SAR': 90, 'ADX': 20, 'CCI': -120, 'OBV': 1000, 'OBV_MA': 900})
    prev_row = pd.Series({'RSI': 30})
    
    # Patch the downstream call to verify the parameter is passed
    with patch('modules.backtest.analysis.classify_stock_state') as mock_classify:
        mock_classify.return_value = ("역매수", "", "")
        backtest.calculate_daily_status(row, prev_row, thresholds={"USE_MEAN_REVERSION": True, "MR_RSI_MAX": 40.0, "MR_DISPARITY_MAX": 100.0})
        
        mock_classify.assert_called()
        _, kwargs = mock_classify.call_args
        assert bool(kwargs.get('is_yangbong')) is True

# Test 4: trading.py - send_order score calculation
@patch('modules.trading.api.place_order')
@patch('modules.trading.api.get_current_price_data', return_value={'rt_cd': '0', 'output': {'stck_prpr': '10000'}})
@patch('modules.trading.api.get_current_price', return_value=10000)
@patch('modules.trading.api.get_chart_data')
@patch('modules.trading.db_manager.db.get_stock_strategy')
@patch('modules.trading.analysis.check_smart_money_turnaround', return_value=(True, "Test"))
def test_trading_send_order_score_params(mock_sm, mock_db_rule, mock_chart, mock_price, mock_cp_data, mock_place):
    """수동 주문 시, 개별 룰 가중치 및 스마트머니 수급이 점수 계산에 반영되는지 검증"""
    # [수정] send_order의 복잡한 UI 흐름 전체를 모킹
    # 매수종목선택(5) -> 종목코드(005930) -> 진행확인(y) -> 수량(10) -> 단가(0) -> 최종확인(y)
    mock_ask_side_effect = ["5", "005930", "y", "10", "0", "y"]
        
    # Mock dependencies
    mock_chart.return_value = pd.DataFrame({'close': [10000]*20, 'high': [10000]*20, 'low': [10000]*20, 'open': [10000]*20, 'volume': [1000]*20})
    mock_place.return_value = {'rt_cd': '0', 'output': {'ODNO': '12345'}}
    
    # Case 1: No custom rule
    mock_db_rule.return_value = None
    with patch('modules.trading.analysis.calculate_score') as mock_calc_score, \
         patch('rich.prompt.Prompt.ask', side_effect=mock_ask_side_effect), \
         patch('api.get_stock_name_by_code', return_value="삼성전자"):
        trading.send_order('buy')
    
    mock_calc_score.assert_called()
    _, kwargs = mock_calc_score.call_args
    assert kwargs.get('weights') == config.SCORING_WEIGHTS
    assert kwargs.get('smart_money') is True

    # Case 2: With custom rule
    custom_weights = {"TREND": 5.0, "MOMENTUM": 2.0, "STRENGTH": 1.0, "SYNERGY": 2.0}
    mock_db_rule.return_value = {'weights': custom_weights}
    with patch('modules.trading.analysis.calculate_score') as mock_calc_score, \
         patch('rich.prompt.Prompt.ask', side_effect=mock_ask_side_effect), \
         patch('api.get_stock_name_by_code', return_value="삼성전자"):
        trading.send_order('buy')
        
        mock_calc_score.assert_called()
        _, kwargs = mock_calc_score.call_args
        assert kwargs.get('weights') == custom_weights

# Test 5: reserved_order_monitor.py - _check_orders score calculation
@patch('modules.reserved_order_monitor.db_manager.db.get_pending_reserved_orders')
@patch('modules.reserved_order_monitor.api.get_current_price', return_value=10000)
@patch('modules.reserved_order_monitor.api.get_chart_data')
@patch('modules.reserved_order_monitor.analysis.check_smart_money_turnaround', return_value=(True, "Test"))
def test_reserved_order_monitor_score_params(mock_sm, mock_chart, mock_price, mock_get_orders):
    """예약 주문 감시 중, 점수 계산 시 스마트머니 수급이 반영되는지 검증"""
    monitor = reserved_order_monitor.ReservedOrderMonitor()
    
    mock_get_orders.return_value = [
        {"id": 1, "code": "005930", "name": "삼성전자", "condition_type": "SCORE_UP", "target_price": 7.5, "order_type": "buy", "market": "KR"}
    ]
    mock_chart.return_value = pd.DataFrame({'close': [10000]*20, 'high': [10000]*20, 'low': [10000]*20, 'open': [10000]*20, 'volume': [1000]*20})
    
    with patch('modules.reserved_order_monitor.analysis.calculate_score') as mock_calc_score, \
         patch('modules.reserved_order_monitor.indicators.calculate_indicators', return_value={'rsi': 50}), \
         patch('modules.reserved_order_monitor.datetime') as mock_dt:
        mock_calc_score.return_value = (8.0, [])
        # 장 중(12:00) 시간으로 강제 고정하여 감시 스킵 방지
        mock_dt.now.return_value.strftime.side_effect = lambda x: '1200' if x == '%H%M' else '209912311200' if x == '%Y%m%d%H%M' else '20991231'
        with patch.object(monitor, '_execute_order'):
            monitor._check_orders()
            
        mock_calc_score.assert_called()
        _, kwargs = mock_calc_score.call_args
        assert kwargs.get('smart_money') is True

# Test 6: theme_analysis.py - _analyze_stock_ui score calculation
@patch('api.get_current_price_data', return_value={'rt_cd': '0', 'output': {'stck_prpr': '10000'}})
@patch('api.get_chart_data')
@patch('modules.analysis.get_market_regime', return_value=("Bull", -0.5))
@patch('modules.db_manager.DBManager.get_stock_strategy', return_value=None)
def test_theme_analysis_ui_score_params(mock_db_rule, mock_regime, mock_chart, mock_cp_data):
    """AI 심층 분석 UI에서 시장 국면 보정값이 점수 계산에 반영되는지 검증"""
    mock_chart.return_value = pd.DataFrame({'close': [10000]*20, 'high': [10000]*20, 'low': [10000]*20, 'open': [10000]*20, 'volume': [1000]*20})
    
    with patch('rich.prompt.Prompt.ask', side_effect=["5", "005930", "y", "n"]), \
         patch('modules.analysis.classify_stock_state') as mock_classify, \
         patch('api.get_stock_name_by_code', return_value="삼성전자"):
        
        theme_analysis._analyze_stock_ui()
        
        mock_classify.assert_called()
        _, kwargs = mock_classify.call_args
        # 시장 보정값이 적용된 thresholds가 전달되었는지 확인
        assert 'thresholds' in kwargs
        assert kwargs['thresholds']['BUY_SCORE'] == config.ANALYSIS_THRESHOLDS['BUY_SCORE'] - 0.5

# Test 7: manage.py - _print_table_worker
@patch('modules.analysis.api.get_current_price_data', return_value={'rt_cd': '0', 'output': {'stck_prpr': '10000', 'prdy_ctrt': '0', 'prdy_vrss': '0'}})
@patch('modules.analysis.api.get_chart_data')
@patch('modules.analysis.check_smart_money_turnaround', return_value=(True, "Test"))
def test_manage_print_table_worker_params(mock_sm, mock_chart, mock_cp_data):
    """전체 종목 테이블 출력 시, 스마트머니 수급이 상태 판별에 반영되는지 검증"""
    mock_chart.return_value = pd.DataFrame({'close': [10000]*20, 'high': [10000]*20, 'low': [10000]*20, 'open': [10000]*20, 'volume': [1000]*20})
    
    with patch('modules.analysis.classify_stock_state') as mock_classify:
        analysis._print_table_worker(("삼성전자", "005930"), "Test", False, False, {}, {}, {}, set(), set())
        
        mock_classify.assert_called()
        _, kwargs = mock_classify.call_args
        assert kwargs.get('smart_money') is True