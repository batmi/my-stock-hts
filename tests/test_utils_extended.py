import pytest
from unittest.mock import patch, MagicMock
from core import utils
import config
from core import context
import api

def test_get_common_headers_simulation():
    """모의투자 헤더 생성 테스트"""
    config.session.is_simulation = True
    config.session.app_key = "sim_key"
    config.session.app_secret = "sim_secret"
    
    with patch('api.get_current_token', return_value="sim_token"):
        headers = utils.get_common_headers("TR123")
        
        assert headers["appKey"] == "sim_key"
        assert headers["appSecret"] == "sim_secret"
        assert headers["tr_id"] == "TR123"
        assert headers["authorization"] == "Bearer sim_token"

def test_get_common_headers_real():
    """실전투자 헤더 생성 테스트"""
    config.session.is_simulation = False
    config.session.real_app_key = "real_key"
    config.session.real_app_secret = "real_secret"
    context.trade_context.use_auto_account = False
    
    with patch('api.get_current_token', return_value="real_token"):
        headers = utils.get_common_headers("TR456")
        
        assert headers["appKey"] == "real_key"
        assert headers["appSecret"] == "real_secret"

def test_get_common_headers_auto():
    """자동매매 계좌 헤더 생성 테스트"""
    config.session.is_simulation = False
    config.session.auto_app_key = "auto_key"
    config.session.auto_app_secret = "auto_secret"
    context.trade_context.use_auto_account = True
    
    with patch('api.get_current_token', return_value="auto_token"):
        headers = utils.get_common_headers("TR789")
        
        assert headers["appKey"] == "auto_key"
        assert headers["appSecret"] == "auto_secret"

def test_get_tr_id_valid():
    """유효한 TR_ID 조회 테스트"""
    config.session.is_simulation = True
    with patch("core.utils.constants.TR_ID_CONFIG", {
        "domestic": {"trade": {"buy": {"sim": "VTTC0802U"}}}
    }):
        tr_id = utils.get_tr_id("domestic", "trade", "buy")
        assert tr_id == "VTTC0802U" 

def test_get_tr_id_invalid():
    """잘못된 경로로 TR_ID 조회 시 빈 문자열 반환 테스트"""
    tr_id = utils.get_tr_id("invalid", "path", "here")
    assert tr_id == ""

@patch("core.utils.yf.Ticker")
def test_get_exchange_rate_success(mock_ticker):
    """환율 조회 성공 테스트"""
    mock_instance = MagicMock()
    mock_instance.fast_info.last_price = 1300.50
    mock_ticker.return_value = mock_instance
    
    rate = utils.get_exchange_rate()
    assert rate == 1300.50