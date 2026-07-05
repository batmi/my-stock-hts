import pytest
import sys
import os
from unittest.mock import MagicMock
from datetime import datetime

# 프로젝트 루트 경로 추가
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import config
from modules.auto_trade import AutoTrader
import api

@pytest.fixture
def trader():
    return AutoTrader()

def test_market_open_regular_times(trader, monkeypatch):
    """1. 정상적인 거래 시간(정규장, 프리마켓, 애프터마켓) 인식 테스트"""
    monkeypatch.setattr(api, 'is_holiday_today', lambda: False)
    
    # 오전 8시 30분 (NXT 프리마켓)
    monkeypatch.setattr('modules.auto_trade.common.datetime', MagicMock(now=lambda: datetime(2026, 6, 11, 8, 30)))
    assert trader.is_market_open() is True
    
    # 오전 10시 (KRX 정규장)
    monkeypatch.setattr('modules.auto_trade.common.datetime', MagicMock(now=lambda: datetime(2026, 6, 11, 10, 0)))
    assert trader.is_market_open() is True
    
    # 오후 4시 (NXT 애프터마켓)
    monkeypatch.setattr('modules.auto_trade.common.datetime', MagicMock(now=lambda: datetime(2026, 6, 11, 16, 0)))
    assert trader.is_market_open() is True

def test_market_pause_times(trader, monkeypatch):
    """2. NXT 장 휴게시간 (단일가 동기화 시간) 차단 테스트"""
    monkeypatch.setattr(api, 'is_holiday_today', lambda: False)
    
    # 08:55 (오전 휴게시간)
    monkeypatch.setattr('modules.auto_trade.common.datetime', MagicMock(now=lambda: datetime(2026, 6, 11, 8, 55)))
    assert trader.is_market_open() is False
    
    # 15:28 (오후 휴게시간)
    monkeypatch.setattr('modules.auto_trade.common.datetime', MagicMock(now=lambda: datetime(2026, 6, 11, 15, 28)))
    assert trader.is_market_open() is False

def test_sor_order_routing_real(monkeypatch):
    """3. 실전 투자 시 SOR(최적주문집행) 거래소 코드가 올바르게 포함되는지 테스트"""
    config.session.is_simulation = False
    
    def mock_call_api(url_path, market, category, action, data=None, method="GET", timeout=None, retries=None, tr_id=None):
        assert data is not None
        assert data.get("EXCG_ID_DVSN_CD") == "SOR" # SOR 라우팅 확인
        return {'rt_cd': '0', 'output': {'ODNO': '12345'}}
        
    monkeypatch.setattr(api, 'call_api', mock_call_api)
    
    api.place_order("domestic", "buy", "005930", 1, 50000, "00")