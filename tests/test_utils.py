import pytest
from unittest.mock import patch, MagicMock
import utils
import config
import context

def test_get_tick_size_domestic():
    """국내 주식 호가 단위 테스트"""
    assert utils.get_tick_size(1000, False) == 1
    assert utils.get_tick_size(3000, False) == 5
    assert utils.get_tick_size(15000, False) == 10
    assert utils.get_tick_size(40000, False) == 50
    assert utils.get_tick_size(150000, False) == 100
    assert utils.get_tick_size(300000, False) == 500
    assert utils.get_tick_size(1000000, False) == 1000

def test_get_tick_size_overseas():
    """해외 주식 호가 단위 테스트"""
    assert utils.get_tick_size(150.50, True) == 0.01

def test_adjust_to_tick():
    """호가 단위에 맞춘 가격 보정 테스트"""
    # Domestic
    assert utils.adjust_to_tick(1234, False) == 1234 # 1 unit
    # 3456 / 5 = 691.2 -> 691 * 5 = 3455
    assert utils.adjust_to_tick(3456, False) == 3455 
    
    # Overseas
    assert utils.adjust_to_tick(150.123, True) == 150.12

def test_account_context():
    """계좌 컨텍스트 매니저 테스트"""
    # Setup
    config.session.is_simulation = False
    config.session.cano = "11111111"
    config.session.auto_cano = "22222222"
    context.trade_context.use_auto_account = False
    
    # Test entering auto account context
    with utils.AccountContext("22222222"):
        assert context.trade_context.use_auto_account is True
        
    # Test exit
    assert context.trade_context.use_auto_account is False
    
    # Test entering main account context
    context.trade_context.use_auto_account = True
    with utils.AccountContext("11111111"):
        assert context.trade_context.use_auto_account is False
        
    assert context.trade_context.use_auto_account is True