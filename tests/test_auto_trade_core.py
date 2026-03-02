import pytest
from unittest.mock import patch, MagicMock
from modules.auto_trade import OrderManager, OrderStatus, AutoTrader
import config

@pytest.fixture
def mock_trader():
    trader = MagicMock(spec=AutoTrader)
    trader.trade_history = []
    trader.trailing_stop_cache = {}
    trader._lock = MagicMock()
    trader._lock.__enter__ = MagicMock()
    trader._lock.__exit__ = MagicMock()
    return trader

@pytest.fixture
def order_manager(mock_trader):
    return OrderManager(mock_trader)

def test_order_manager_pending_logic(order_manager):
    """주문 상태 관리 로직 테스트"""
    code = "005930"
    odno = "12345"
    
    # Initial state
    assert not order_manager.is_pending(code)
    
    # Register manual order
    order_manager.register_manual_order(code, odno)
    assert order_manager.is_pending(code)
    assert order_manager.pending_orders[code][odno] == OrderStatus.ORDER_SENT
    
    # Update status to FILLED
    order_manager.update_order_status(code, odno, OrderStatus.FILLED)
    assert not order_manager.is_pending(code) # Should be removed

def test_send_order_success(order_manager):
    """주문 전송 성공 시나리오 테스트"""
    code = "005930"
    qty = 10
    price = 70000
    
    with patch('modules.auto_trade.api.place_order') as mock_place, \
         patch('modules.auto_trade.api.send_telegram_message') as mock_tg, \
         patch('modules.auto_trade.db_manager.db.insert_trade') as mock_db_insert, \
         patch('modules.auto_trade.db_manager.db.update_highest_price') as mock_db_update, \
         patch('modules.auto_trade.analysis.get_snapshot') as mock_snap, \
         patch('modules.auto_trade.ConclusionMonitor') as mock_monitor:
             
        mock_place.return_value = {'rt_cd': '0', 'output': {'ODNO': '99999'}}
        
        odno = order_manager.send_order(code, qty, "buy", price=price)
        
        assert odno == '99999'
        assert order_manager.is_pending(code)
        mock_place.assert_called_once()
        mock_db_insert.assert_called_once()
        mock_tg.assert_called_once()

def test_send_order_failure(order_manager):
    """주문 전송 실패 시나리오 테스트"""
    with patch('modules.auto_trade.api.place_order') as mock_place, \
         patch('modules.auto_trade.api.send_telegram_message'):
             
        mock_place.return_value = {'rt_cd': '1', 'msg1': 'Error', 'msg_cd': 'E123'}
        
        odno = order_manager.send_order("005930", 10, "buy", price=70000)
        
        assert odno is None