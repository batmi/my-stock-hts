import pytest
import threading
import time
from unittest.mock import patch, MagicMock
from modules.auto_trade import ConclusionMonitor

# We need to test the logic embedded in ConclusionMonitor._check_conclusions()
# We will mock the necessary dependencies.

@pytest.fixture
def mock_dependencies():
    with patch('modules.auto_trade.api.get_today_history') as mock_history, \
         patch('modules.auto_trade.api.get_overseas_today_history') as mock_overseas, \
         patch('modules.auto_trade.api.get_domestic_balance') as mock_domestic_bal, \
         patch('modules.auto_trade.api.get_overseas_balance') as mock_overseas_bal, \
         patch('modules.auto_trade.api.get_current_price_data') as mock_cp, \
         patch('modules.auto_trade.api.send_telegram_message') as mock_telegram, \
         patch('modules.auto_trade.db_manager.db') as mock_db, \
         patch('modules.auto_trade.config') as mock_config, \
         patch('modules.auto_trade.load_restricted_stocks') as mock_load, \
         patch('modules.auto_trade.save_restricted_stocks') as mock_save:
         
        # Basic config setup to bypass early returns
        mock_config.session.cano = "12345678"
        mock_config.session.acnt_prdt_cd = "01"
        mock_config.session.auto_cano = "12345678"
        mock_config.session.auto_acnt_prdt_cd = "01"
        mock_config.session.is_simulation = False
        mock_config.session.is_toss = False
        
        # We need _check_conclusions to process the items without exceptions
        mock_db.get_all_stock_strategies.return_value = []
        mock_db.check_trade_exists.return_value = False
        mock_db.get_trade_by_odno.return_value = None
        mock_db.get_reserved_order_by_odno.return_value = None
        mock_db.get_cancel_record_by_org_odno.return_value = None
        
        class MockApi:
            get_today_history = mock_history
            get_overseas_today_history = mock_overseas
            get_domestic_balance = mock_domestic_bal
            get_overseas_balance = mock_overseas_bal
            get_current_price_data = mock_cp
            send_telegram_message = mock_telegram
            
        yield MockApi, mock_db, mock_config, mock_load, mock_save

def test_manual_buy_adds_restriction_to_account(mock_dependencies):
    mock_api, mock_db, mock_config, mock_load, mock_save = mock_dependencies
    
    # Mock restricted stocks initial state
    mock_load.return_value = {}
    
    # Mock trade history API response to simulate a manual BUY order
    mock_api.get_today_history.return_value = {
        'rt_cd': '0',
        'output1': [{
            'odno': 'B001',
            'ord_qty': '10',
            'tot_ccld_qty': '10',
            'cncl_cfrm_qty': '0',
            'rmn_qty': '0',
            'avg_prvs': '50000',
            'sll_buy_dvsn_cd_name': '매수',
            'pdno': '005930',
            'prdt_name': '삼성전자'
        }]
    }
    mock_api.get_overseas_today_history.return_value = {'rt_cd': '0', 'output': []}
    mock_api.get_current_price_data.return_value = {'rt_cd': 'E'} # Ignore current price
    
    # Initialize and call the method
    monitor = ConclusionMonitor()
    monitor.initialized = True  # Prevent initial sync from skipping alert logic
    
    # Force _check_conclusions
    monitor._check_conclusions(initial=False)
    
    # Verify save_restricted_stocks was called with the new stock AND account key
    mock_save.assert_called_once()
    saved_data = mock_save.call_args[0][0]
    assert '005930' in saved_data
    assert saved_data['005930']['accounts']['12345678-01']['memo'] == "수동매매"
    assert saved_data['005930']['name'] == "삼성전자"

def test_manual_sell_removes_restriction_from_account(mock_dependencies):
    mock_api, mock_db, mock_config, mock_load, mock_save = mock_dependencies
    
    # Mock restricted stocks initial state (already restricted for this account)
    mock_load.return_value = {
        '005930': {
            'name': '삼성전자',
            'memo': '',
            'accounts': {'12345678-01': {'memo': '수동매매', 'type': '실전'}}
        }
    }
    
    # Mock trade history API response to simulate a manual SELL order
    mock_api.get_today_history.return_value = {
        'rt_cd': '0',
        'output1': [{
            'odno': 'S001',
            'ord_qty': '10',
            'tot_ccld_qty': '10',
            'cncl_cfrm_qty': '0',
            'rmn_qty': '0',
            'avg_prvs': '55000',
            'sll_buy_dvsn_cd_name': '매도',
            'pdno': '005930',
            'prdt_name': '삼성전자'
        }]
    }
    mock_api.get_overseas_today_history.return_value = {'rt_cd': '0', 'output': []}
    mock_api.get_current_price_data.return_value = {'rt_cd': 'E'}
    
    # Mock domestic balance to return 0 holdings for 005930
    mock_api.get_domestic_balance.return_value = ([], {}) # Empty holdings means qty = 0
    
    # We need to speed up the time.sleep(3) in the thread, so patch time.sleep
    with patch('modules.auto_trade.time.sleep', return_value=None):
        monitor = ConclusionMonitor()
        monitor.initialized = True
        
        # Reset cancel_status/order_status to avoid logic skips
        monitor.order_status = {}
        
        # Force _check_conclusions
        monitor._check_conclusions(initial=False)
        
        # The balance check and restriction removal happen in a daemon thread.
        time.sleep(0.1) 
        
        # Verify save_restricted_stocks was called and the stock was completely removed since it had no other accounts
        mock_save.assert_called_once()
        saved_data = mock_save.call_args[0][0]
        assert '005930' not in saved_data

def test_manual_sell_partial_keeps_restriction_in_account(mock_dependencies):
    mock_api, mock_db, mock_config, mock_load, mock_save = mock_dependencies
    
    # Mock restricted stocks initial state (already restricted)
    mock_load.return_value = {
        '005930': {
            'name': '삼성전자',
            'memo': '',
            'accounts': {'12345678-01': {'memo': '수동매매', 'type': '실전'}}
        }
    }
    
    # Mock trade history API response to simulate a manual SELL order
    mock_api.get_today_history.return_value = {
        'rt_cd': '0',
        'output1': [{
            'odno': 'S002',
            'ord_qty': '5',
            'tot_ccld_qty': '5',
            'cncl_cfrm_qty': '0',
            'rmn_qty': '0',
            'avg_prvs': '55000',
            'sll_buy_dvsn_cd_name': '매도',
            'pdno': '005930',
            'prdt_name': '삼성전자'
        }]
    }
    mock_api.get_overseas_today_history.return_value = {'rt_cd': '0', 'output': []}
    
    # Mock domestic balance to return 5 holdings for 005930 (Partial sell)
    mock_api.get_domestic_balance.return_value = ([{
        'pdno': '005930',
        'hldg_qty': '5'
    }], {})
    
    with patch('modules.auto_trade.time.sleep', return_value=None):
        monitor = ConclusionMonitor()
        monitor.initialized = True
        monitor.order_status = {}
        
        monitor._check_conclusions(initial=False)
        time.sleep(0.1)
        
        # Verify save_restricted_stocks was NOT called because qty != 0
        mock_save.assert_not_called()

def test_manual_sell_removes_only_account_restriction(mock_dependencies):
    mock_api, mock_db, mock_config, mock_load, mock_save = mock_dependencies
    
    # Mock restricted stocks initial state (Global memo + Account memo)
    mock_load.return_value = {
        '005930': {
            'name': '삼성전자',
            'memo': '급등주',
            'accounts': {'12345678-01': {'memo': '수동매매', 'type': '실전'}}
        }
    }
    
    # Mock trade history
    mock_api.get_today_history.return_value = {
        'rt_cd': '0',
        'output1': [{
            'odno': 'S003',
            'ord_qty': '10',
            'tot_ccld_qty': '10',
            'cncl_cfrm_qty': '0',
            'rmn_qty': '0',
            'avg_prvs': '55000',
            'sll_buy_dvsn_cd_name': '매도',
            'pdno': '005930',
            'prdt_name': '삼성전자'
        }]
    }
    mock_api.get_overseas_today_history.return_value = {'rt_cd': '0', 'output': []}
    
    # Mock domestic balance to return 0 holdings
    mock_api.get_domestic_balance.return_value = ([], {})
    
    with patch('modules.auto_trade.time.sleep', return_value=None):
        monitor = ConclusionMonitor()
        monitor.initialized = True
        monitor.order_status = {}
        
        monitor._check_conclusions(initial=False)
        time.sleep(0.1)
        
        # Verify save_restricted_stocks was called and the stock is still there but with the account removed
        mock_save.assert_called_once()
        saved_data = mock_save.call_args[0][0]
        assert '005930' in saved_data
        assert saved_data['005930']['memo'] == "급등주"
        assert '12345678-01' not in saved_data['005930'].get('accounts', {})

