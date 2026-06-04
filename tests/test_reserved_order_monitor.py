import pytest
import os
import sys
from unittest.mock import patch, MagicMock
from datetime import datetime, timedelta
import pandas as pd

sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from modules.reserved_order_monitor import ReservedOrderMonitor

@pytest.fixture
def monitor():
    """싱글톤 인스턴스 초기화용 피스처"""
    m = ReservedOrderMonitor()
    m.is_running = False
    m.chart_cache = {}
    return m

def test_monitor_start_stop(monitor):
    """모니터 스레드 시작 및 중지 테스트"""
    monitor.start()
    assert monitor.is_running is True
    monitor.stop()
    assert monitor.is_running is False

@patch('modules.reserved_order_monitor.db_manager.db.get_pending_reserved_orders')
@patch('modules.reserved_order_monitor.db_manager.db.update_reserved_order_status')
@patch('modules.reserved_order_monitor.api.send_telegram_message')
def test_check_orders_expire(mock_tg, mock_update, mock_get_orders, monitor):
    """기간 만료된 예약 주문 처리 테스트"""
    past_date = (datetime.now() - timedelta(days=1)).strftime("%Y%m%d")
    mock_get_orders.return_value = [
        {'id': 1, 'code': '005930', 'condition_type': 'LIMIT', 'target_price': 80000, 
         'order_type': 'sell', 'market': 'KR', 'name': '삼성전자', 'qty': 10, 'order_price': 0, 'expire_dt': past_date}
    ]
    
    monitor._check_orders()
    mock_update.assert_called_with(1, 'EXPIRED')
    mock_tg.assert_called_once()

@patch('modules.reserved_order_monitor.db_manager.db.get_pending_reserved_orders')
@patch('modules.reserved_order_monitor.api.get_current_price')
@patch('modules.reserved_order_monitor.ReservedOrderMonitor._execute_order')
def test_check_orders_limit_trigger(mock_execute, mock_get_price, mock_get_orders, monitor):
    """지정가 도달 조건(LIMIT) 트리거 테스트 (매도)"""
    mock_get_orders.return_value = [
        {'id': 2, 'code': '000660', 'condition_type': 'LIMIT', 'target_price': 150000, 
         'order_type': 'sell', 'market': 'KR', 'name': 'SK하이닉스', 'qty': 10, 'order_price': 150000, 'expire_dt': '20991231'}
    ]
    # 현재가가 목표가보다 높으므로 매도 조건 충족
    mock_get_price.return_value = 155000
    
    with patch('modules.reserved_order_monitor.datetime') as mock_dt:
        mock_dt.now.return_value.strftime.side_effect = lambda x: '1000' if x == '%H%M' else '20991231'
        monitor._check_orders()
        
    mock_execute.assert_called_once()
    assert "지정가 도달" in mock_execute.call_args[0][1]

@patch('modules.reserved_order_monitor.db_manager.db.get_pending_reserved_orders')
@patch('modules.reserved_order_monitor.ReservedOrderMonitor._execute_order')
def test_check_orders_time_trigger(mock_execute, mock_get_orders, monitor):
    """특정 시간(TIME) 도달 트리거 테스트"""
    mock_get_orders.return_value = [
        {'id': 3, 'code': '005930', 'condition_type': 'TIME', 'target_time': '1520', 
         'order_type': 'buy', 'market': 'KR', 'name': '삼성전자', 'qty': 10, 'order_price': 0, 'expire_dt': '20991231', 'target_price': 0.0}
    ]
    
    with patch('modules.reserved_order_monitor.datetime') as mock_dt:
        mock_dt.now.return_value.strftime.side_effect = lambda x: '1521' if x == '%H%M' else '20991231'
        monitor._check_orders()
        
    mock_execute.assert_called_once()
    assert "지정 시간" in mock_execute.call_args[0][1]

@patch('modules.reserved_order_monitor.db_manager.db.get_pending_reserved_orders')
@patch('modules.reserved_order_monitor.api.get_current_price')
@patch('modules.reserved_order_monitor.db_manager.db.update_reserved_order_lowest')
@patch('modules.reserved_order_monitor.ReservedOrderMonitor._execute_order')
def test_check_orders_trailing_buy(mock_execute, mock_update_low, mock_get_price, mock_get_orders, monitor):
    """트레일링 매수 조건 트리거 및 최저가 갱신 테스트"""
    mock_get_orders.return_value = [
        {'id': 4, 'code': 'AAPL', 'condition_type': 'TRAILING_BUY', 'target_price': 3.0, 'lowest_price': 100.0,
         'order_type': 'buy', 'market': 'US', 'name': 'Apple', 'qty': 5, 'order_price': 0, 'expire_dt': '20991231'}
    ]
    
    with patch('modules.reserved_order_monitor.datetime') as mock_dt:
        # 미국 주식은 08:00~16:00 사이에 감시를 생략하므로, 밤 10시(2200)로 모킹
        mock_dt.now.return_value.strftime.side_effect = lambda x: '2200' if x == '%H%M' else '20991231'
        
        # 1. 가격 하락 -> 최저가 갱신 트리거 안됨
        mock_get_price.return_value = 95.0
        monitor._check_orders()
        mock_update_low.assert_called_with(4, 95.0)
        mock_execute.assert_not_called()

        # 2. 가격 상승 (95.0 대비 3% 이상 반등) -> 98.0
        mock_get_orders.return_value[0]['lowest_price'] = 95.0
        mock_get_price.return_value = 98.0
        monitor._check_orders()
        mock_execute.assert_called_once()
        assert "반등 매수" in mock_execute.call_args[0][1]

@patch('modules.reserved_order_monitor.api.place_order')
@patch('modules.reserved_order_monitor.db_manager.db.update_reserved_order_status')
@patch('modules.reserved_order_monitor.api.send_telegram_message')
def test_execute_order_success(mock_tg, mock_update, mock_place, monitor):
    """_execute_order 성공 로직 검증"""
    order = {
        'id': 5, 'cano': '12345678', 'acnt': '01', 'code': '005930', 'market': 'KR', 'order_type': 'buy', 'name': '삼성전자', 'qty': 10, 'order_price': 80000
    }
    mock_place.return_value = {'rt_cd': '0', 'output': {'ODNO': '123456'}}
    
    monitor._execute_order(order, "테스트 사유")
    
    mock_place.assert_called_once_with('domestic', 'buy', '005930', 10, '80000', '00')
    mock_update.assert_any_call(5, 'TRIGGERED', '123456')
    mock_tg.assert_called_once()