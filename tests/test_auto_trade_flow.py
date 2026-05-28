import pytest
from unittest.mock import patch, MagicMock
from modules.auto_trade import AutoTrader
import config
import pandas as pd

@pytest.fixture
def trader():
    t = AutoTrader()
    t.order_manager.pending_orders.clear() # 상태 초기화
    t.consecutive_errors = 0
    return t

@pytest.fixture
def mock_chart_df():
    df = pd.DataFrame({
        'date': pd.date_range(start='2023-01-01', periods=100),
        'close': [10000] * 100,
        'high': [10500] * 100,
        'low': [9500] * 100,
        'open': [10000] * 100,
        'volume': [1000] * 100
    })
    return df

@patch('modules.auto_trade.load_restricted_stocks', return_value={})
@patch('api.get_chart_data')
@patch('api.get_realtime_vol_strength')
@patch('api.fetch_buyable_quantity')
@patch('api.place_order')
@patch('indicators.calculate_indicators')
@patch('modules.auto_trade.db_manager.db.get_all_stock_strategies', return_value=[])
def test_check_buy_conditions(mock_get_rules, mock_calc, mock_place, mock_qty, mock_vol, mock_get_chart, mock_restricted, trader, mock_chart_df):
    """매수 조건 점검 및 주문 실행 테스트"""
    # Setup
    trader.is_running = True
    config.session.stock_data = {"stocks_kr": [{"code": "005930", "name": "Samsung"}]}
    
    # Mocks
    mock_get_chart.return_value = mock_chart_df
    mock_vol.return_value = 150.0
    mock_qty.return_value = 10
    mock_place.return_value = {'rt_cd': '0', 'output': {'ODNO': '12345'}}
    
    # 테스트 간섭 방지를 위해 명시적 임계값 고정
    config.ANALYSIS_THRESHOLDS["BUY_SCORE"] = 7.0
    config.ANALYSIS_THRESHOLDS["BUY_RSI_MAX"] = 65.0

    # 매수 신호가 나오도록 지표 설정
    mock_calc.return_value = {
        'ema_5': 10000, 'ema_20': 9000, 'ema_60': 8000, 'ema_120': 7000,
        'rsi': 60, 'adx': 30, 'cci': 100, 'obv': 1000, 'obv_trend': True,
        'psar': 9000, 'macd': 50, 'macd_signal': 40, 'macd_hist': 10,
        'atr': 100,
        'plus_di': 30, 'minus_di': 10 # 추가 점수 확보용 DMI
    }
    
    # 예수금 Mock
    deposit_res = {'d2_deposit': 1000000}
    
    # 실행
    with patch('modules.auto_trade.db_manager.db.insert_trade'), \
         patch('modules.auto_trade.api.send_telegram_message'):
        trader._check_buy_conditions([], deposit_res, is_market_open=True)
        
    # 주문 함수 호출 확인
    mock_place.assert_called()
    args, _ = mock_place.call_args
    assert args[2] == "005930" # code
    assert args[1] == "buy" # type

@patch('modules.auto_trade.load_restricted_stocks', return_value={})
@patch('api.get_chart_data')
@patch('api.place_order')
@patch('indicators.calculate_indicators')
@patch('api.fetch_sellable_quantity', return_value=10)
@patch('modules.auto_trade.db_manager.db.get_all_stock_strategies', return_value=[])
def test_check_sell_conditions(mock_get_rules, mock_sell_qty, mock_calc, mock_place, mock_get_chart, mock_restricted, trader, mock_chart_df):
    """매도 조건 점검 및 주문 실행 테스트"""
    # Setup
    mock_sell_qty.return_value = 10  # 명시적 반환값 설정
    trader.is_running = True
    holdings = [{
        'pdno': '005930', 'prdt_name': 'Samsung', 'ord_psbl_qty': '10',
        'evlu_pfls_rt': '5.0', 'prpr': '10000', 'pchs_avg_pric': '9500',
        'evlu_pfls_amt': '5000'
    }]
    
    # Mocks
    mock_get_chart.return_value = mock_chart_df
    mock_place.return_value = {'rt_cd': '0', 'output': {'ODNO': '12345'}}
    
    # 테스트 간섭 방지를 위해 명시적 임계값 고정 (다른 테스트가 값을 바꿔도 영향받지 않도록 방어)
    config.SELL_STRATEGY["TAKE_PROFIT_RSI"] = 75.0
    config.SELL_STRATEGY["SUPER_TAKE_PROFIT_RSI"] = 85.0

    # 매도 신호(RSI 과열)가 나오도록 지표 설정
    mock_calc.return_value = {
        'ema_5': 10000, 'ema_20': 9000, 'ema_60': 8000, 'ema_120': 7000,
        'rsi': 90, # Overbought (상향된 기준치를 무조건 초과하도록 90으로 설정)
        'adx': 30, 'cci': 100, 'obv': 1000, 'obv_trend': True,
        'psar': 9000, 'macd': 50, 'macd_signal': 40, 'macd_hist': 10,
        'atr': 100,
        'plus_di': 30, 'minus_di': 10
    }
    
    # 실행
    with patch('modules.auto_trade.db_manager.db.insert_trade'), \
         patch('modules.auto_trade.api.send_telegram_message'), \
         patch('modules.auto_trade.db_manager.db.get_highest_price', return_value=0), \
         patch('modules.auto_trade.db_manager.db.update_highest_price'), \
         patch('modules.auto_trade.db_manager.db.delete_trailing_stop'):
        trader._check_sell_conditions(holdings, is_market_open=True)
        
    # 주문 함수 호출 확인
    mock_get_chart.assert_called()
    mock_calc.assert_called()
    mock_sell_qty.assert_called()
    mock_place.assert_called()
    
    args, _ = mock_place.call_args
    assert args[2] == "005930" # code
    assert args[1] == "sell" # type