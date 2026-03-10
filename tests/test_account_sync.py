import pytest
from unittest.mock import patch, MagicMock
from modules import account
import api
import config

@patch('api.get_today_history')
@patch('modules.account.db_manager.db')
def test_sync_today_trades(mock_db, mock_get_history):
    """금일 체결 내역 동기화 테스트"""
    # Setup mock API response
    mock_get_history.return_value = {
        'rt_cd': '0',
        'output1': [
            {
                'odno': '1001', 'avg_prvs': '70000', 'tot_ccld_qty': '10', 
                'pdno': '005930', 'prdt_name': 'Samsung', 
                'sll_buy_dvsn_cd': '02', # Buy
                'ord_dt': '20230101', 'ord_tmd': '120000'
            }
        ]
    }
    
    # Setup mock DB
    mock_db.check_trade_exists.return_value = False # New trade
    mock_db.get_trade_by_odno.return_value = None
    
    # Config setup
    config.session.cano = "12345678"
    config.session.acnt_prdt_cd = "01"
    config.session.is_simulation = True
    
    count = account.sync_today_trades()
    
    assert count == 1
    mock_db.insert_trade.assert_called_once()

    # [수정] 체결 확인 로직 변경으로 원본 주문의 가격을 업데이트하지 않음.
    # mock_db.update_trade.assert_called_once_with('1001', price=70000.0)