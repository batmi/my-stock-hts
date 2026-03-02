import pytest
from unittest.mock import patch, MagicMock
from modules.auto_trade import AutoTrader
import config

@pytest.fixture
def trader():
    t = AutoTrader()
    t.is_running = True
    return t

@patch('modules.auto_trade.api.fetch_buyable_quantity')
@patch('modules.auto_trade.api.place_order')
def test_execute_buy_orders(mock_place, mock_qty, trader):
    """매수 주문 실행 로직 테스트"""
    # Setup
    candidates = [{
        'code': '005930', 'name': 'Samsung', 'price': 50000, 
        'score': 9.0, 'rsi': 50, 'adx': 30, 'cci': 100,
        'is_custom_rule': False
    }]
    avail_cash = 1000000
    invest_ratio = 0.5
    current_holdings = 0
    max_holdings = 5
    
    # Mocks
    mock_qty.return_value = 10
    mock_place.return_value = {'rt_cd': '0', 'output': {'ODNO': '12345'}}
    
    # 실행
    with patch('modules.auto_trade.db_manager.db.insert_trade'), \
         patch('modules.auto_trade.api.send_telegram_message'):
        trader._execute_buy_orders(candidates, avail_cash, invest_ratio, current_holdings, max_holdings)
        
    mock_place.assert_called()

@patch('modules.auto_trade.api.get_domestic_index_chart')
def test_update_market_indices_status(mock_get_index, trader):
    """시장 지수 상태 업데이트 테스트"""
    # Mock DataFrame
    import pandas as pd
    df = pd.DataFrame({'close': [2500] * 100})
    mock_get_index.return_value = df
    
    trader._update_market_indices_status()
    
    assert "KOSPI" in trader.market_index_status
    assert bool(trader.market_index_status["KOSPI"]["is_healthy"]) is True