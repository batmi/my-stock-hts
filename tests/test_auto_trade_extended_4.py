import pytest
from unittest.mock import patch, MagicMock
import pandas as pd
from modules.auto_trade import AutoTrader
import config

@pytest.fixture
def trader():
    t = AutoTrader()
    t.is_running = True
    return t

@patch('modules.auto_trade.api.fetch_sellable_quantity')
@patch('modules.auto_trade.api.get_chart_data')
@patch('modules.auto_trade.DefaultStrategy.analyze_sell')
@patch('modules.auto_trade.api.send_telegram_message')
@patch('modules.auto_trade.db_manager.db.insert_trade')
@patch('modules.auto_trade.load_restricted_stocks', return_value={})
@patch('modules.auto_trade.db_manager.db.get_highest_price', return_value=0)
@patch('modules.auto_trade.db_manager.db.update_highest_price')
@patch('modules.auto_trade.db_manager.db.delete_trailing_stop')
def test_check_sell_conditions_sell_signal(mock_db_del, mock_db_update, mock_db_get, mock_restricted, mock_db, mock_tg, mock_analyze, mock_chart, mock_qty, trader):
    """매도 신호 발생 시 주문 실행 테스트"""
    # Setup
    holdings = [{
        'pdno': '005930', 'prdt_name': 'Samsung', 'ord_psbl_qty': '10',
        'evlu_pfls_rt': '5.0', 'prpr': '60000', 'pchs_avg_pric': '55000',
        'evlu_pfls_amt': '50000'
    }]
    
    # Mocks
    mock_qty.return_value = 10
    mock_chart.return_value = pd.DataFrame({'close': [60000], 'high': [60000], 'low': [60000], 'open': [60000], 'volume': [1000]})
    mock_analyze.return_value = {
        'action': 'sell', 'reason': '익절', 'score': 4.0, 'state': '매도',
        'ind': {'rsi': 80, 'adx': 30, 'cci': 100}
    }
    
    with patch.object(trader.order_manager, 'is_pending', return_value=False):
        with patch.object(trader.order_manager, 'send_order', return_value='12345') as mock_send:
            trader._check_sell_conditions(holdings, is_market_open=True)
            
            mock_send.assert_called_once()
            args, _ = mock_send.call_args
            assert args[0] == '005930' # code
            assert args[2] == 'sell'   # type

@patch('modules.auto_trade.load_restricted_stocks', return_value={})
@patch('modules.auto_trade.api.get_chart_data')
@patch('modules.auto_trade.api.get_realtime_vol_strength')
@patch('modules.auto_trade.DefaultStrategy.analyze_buy')
def test_analyze_candidates(mock_analyze, mock_vol, mock_chart, mock_restricted, trader):
    """매수 후보 분석 로직 테스트"""
    # Setup
    targets = [{'code': '005930', 'name': 'Samsung'}]
    holding_codes = set()
    rules_map = {}
    
    # Mocks
    mock_vol.return_value = 150.0
    mock_chart.return_value = pd.DataFrame({'close': [10000]}) # df가 비어있지 않도록
    mock_analyze.return_value = {
        'action': 'buy', 'score': 9.0, 'state': '매수',
        'rsi': 50, 'adx': 30, 'cci': 100, 'vol_strength': 150.0
    }
    
    # 시장 필터링 비활성화 및 Pending 상태 False 설정
    config.USE_MARKET_FILTER = False
    with patch.object(trader.order_manager, 'is_pending', return_value=False):
        candidates = trader._analyze_candidates(targets, holding_codes, rules_map, {}, {}, {})
        
        assert len(candidates) == 1
        assert candidates[0]['code'] == '005930'
        assert candidates[0]['score'] == 9.0