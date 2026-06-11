import pytest
import sys
import os
from unittest.mock import MagicMock
from datetime import datetime

# 프로젝트 루트 경로 추가
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from modules.reserved_order_monitor import ReservedOrderMonitor
import api
from modules import db_manager

@pytest.fixture
def monitor():
    return ReservedOrderMonitor()

@pytest.fixture
def mock_db_and_api(monkeypatch):
    """의존성 모킹 통합 픽스처"""
    monkeypatch.setattr(db_manager.db, 'update_reserved_order_status', lambda *args, **kwargs: None)
    monkeypatch.setattr(db_manager.db, 'insert_trade', lambda *args, **kwargs: None)
    monkeypatch.setattr(db_manager.db, 'cancel_other_reserved_orders', lambda *args, **kwargs: [])
    monkeypatch.setattr(api, 'send_telegram_message', lambda *args, **kwargs: None)

def test_nxt_aftermarket_order_conversion(monitor, mock_db_and_api, monkeypatch):
    """1. 애프터마켓(15:30~20:00) 예약 시장가(0) 주문이 지정가(00)로 자동 변환되는지 테스트"""
    
    # 시간 모킹 (오후 4시 = NXT 애프터마켓)
    monkeypatch.setattr('modules.reserved_order_monitor.datetime', MagicMock(now=lambda: datetime(2026, 6, 11, 16, 0)))
    monkeypatch.setattr(api, 'get_current_price', lambda code, is_overseas: 50000)
    
    def mock_place_order(market, action, code, qty, price, ord_dvsn, exchange_code=None):
        assert ord_dvsn == "00"  # 지정가로 강제 변환 확인
        assert price == "50000"  # 현재가로 채워졌는지 확인
        return {'rt_cd': '0', 'output': {'ODNO': '12345'}}
        
    monkeypatch.setattr(api, 'place_order', mock_place_order)
    
    order = {'id': 1, 'cano': '1234', 'acnt': '01', 'market': 'KR', 'order_type': 'buy', 'code': '005930', 'name': '삼성전자', 'qty': 1, 'order_price': 0, 'condition_type': 'LIMIT'}
    monitor._execute_order(order, "테스트 발동")

def test_nxt_premarket_order_conversion(monitor, mock_db_and_api, monkeypatch):
    """2. 프리마켓(08:00~08:50) 예약 시장가(0) 주문이 지정가(00)로 자동 변환되는지 테스트"""
    
    # 시간 모킹 (오전 8시 30분 = NXT 프리마켓)
    monkeypatch.setattr('modules.reserved_order_monitor.datetime', MagicMock(now=lambda: datetime(2026, 6, 11, 8, 30)))
    monkeypatch.setattr(api, 'get_current_price', lambda code, is_overseas: 60000)
    
    def mock_place_order(market, action, code, qty, price, ord_dvsn, exchange_code=None):
        assert ord_dvsn == "00"
        assert price == "60000"
        return {'rt_cd': '0', 'output': {'ODNO': '12345'}}
        
    monkeypatch.setattr(api, 'place_order', mock_place_order)
    
    order = {'id': 2, 'cano': '1234', 'acnt': '01', 'market': 'KR', 'order_type': 'buy', 'code': '005930', 'name': '삼성전자', 'qty': 1, 'order_price': 0, 'condition_type': 'LIMIT'}
    monitor._execute_order(order, "테스트 발동")

def test_regular_market_order_no_conversion(monitor, mock_db_and_api, monkeypatch):
    """3. 정규장 시간(09:00~15:20)에는 시장가(01) 및 0원으로 정상 전송되는지 (간섭 여부) 테스트"""
    
    # 시간 모킹 (오전 10시 = 정규장)
    monkeypatch.setattr('modules.reserved_order_monitor.datetime', MagicMock(now=lambda: datetime(2026, 6, 11, 10, 0)))
    
    def mock_place_order(market, action, code, qty, price, ord_dvsn, exchange_code=None):
        assert ord_dvsn == "01"  # 시장가 원형 보존
        assert price == "0"
        return {'rt_cd': '0', 'output': {'ODNO': '12345'}}
        
    monkeypatch.setattr(api, 'place_order', mock_place_order)
    
    order = {'id': 3, 'cano': '1234', 'acnt': '01', 'market': 'KR', 'order_type': 'buy', 'code': '005930', 'name': '삼성전자', 'qty': 1, 'order_price': 0, 'condition_type': 'LIMIT'}
    monitor._execute_order(order, "테스트 발동")