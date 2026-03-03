import pytest
from unittest.mock import patch, MagicMock
from modules.auto_trade import ConclusionMonitor, OrderManager, AutoTrader, OrderStatus
import config
from datetime import datetime

@pytest.fixture(autouse=True)
def reset_singleton():
    """싱글톤 인스턴스 초기화"""
    AutoTrader._instance = None
    ConclusionMonitor._instance = None
    yield

@pytest.fixture
def monitor():
    return ConclusionMonitor()

@patch('modules.auto_trade.api.get_today_history')
@patch('modules.auto_trade.db_manager.db')
@patch('modules.auto_trade.api.send_telegram_message')
def test_check_conclusions_filled(mock_tg, mock_db, mock_get_history, monitor):
    """체결 확인 로직 테스트"""
    # Mock API response (Filled)
    mock_get_history.return_value = {
        'rt_cd': '0',
        'output1': [{
            'odno': '12345', 'pdno': '005930', 'prdt_name': 'Samsung',
            'ord_qty': '10', 'tot_ccld_qty': '10', 'avg_prvs': '60000',
            'sll_buy_dvsn_cd_name': '매수'
        }]
    }
    
    # Mock DB
    mock_db.check_trade_exists.return_value = False # New trade
    
    # Run
    monitor._check_conclusions(initial=False)
    
    # Verify
    mock_db.insert_trade.assert_called()
    mock_tg.assert_called()
    assert "체결 알림" in mock_tg.call_args[0][0]

@patch('modules.auto_trade.api.get_unfilled_orders')
@patch('modules.auto_trade.api.revise_cancel_order')
def test_manage_unfilled_orders_cancel(mock_cancel, mock_get_unfilled):
    """오래된 미체결 주문 취소 테스트"""
    trader = AutoTrader()
    
    # Mock Unfilled Orders (Old)
    mock_get_unfilled.return_value = [{
        'odno': '12345', 'pdno': '005930', 'prdt_name': 'Samsung',
        'rmn_qty': '10', 'ord_tmd': '090000' # 09:00:00
    }]
    
    # Mock Cancel Response
    mock_cancel.return_value = {'rt_cd': '0'}
    
    # Set current time to 10:00:00 (1 hour later)
    class FakeDatetime(datetime):
        @classmethod
        def now(cls):
            return cls(2023, 1, 1, 10, 0, 0)

    with patch('modules.auto_trade.datetime', FakeDatetime):
        
        # Set threshold to 10 seconds
        config.UNFILLED_ORDER_CANCEL_SECONDS = 10
        
        trader.order_manager.manage_unfilled_orders()
        
    mock_cancel.assert_called()