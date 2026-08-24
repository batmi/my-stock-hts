import pytest
from unittest.mock import patch
from core import utils
import config
from core import context

def test_get_common_headers_auto_account():
    """자동매매 계좌 컨텍스트에서 헤더 생성 테스트"""
    # Setup
    config.session.is_simulation = False
    config.session.auto_app_key = "auto_key"
    config.session.auto_app_secret = "auto_secret"
    context.trade_context.use_auto_account = True
    
    with patch('api.get_current_token', return_value="auto_token"):
        headers = utils.get_common_headers("TEST_TR")
        assert headers['appKey'] == "auto_key"
        assert headers['appSecret'] == "auto_secret"

@patch('core.utils.yf.Ticker')
def test_get_exchange_rate_failure(mock_ticker):
    """환율 조회 실패 시 기본값 반환 테스트"""
    mock_ticker.side_effect = Exception("YFinance API Error")
    
    # 테스트를 위해 기본 환율 임시 변경
    original_rate = config.DEFAULT_EXCHANGE_RATE
    config.DEFAULT_EXCHANGE_RATE = 1500.0
    
    rate = utils.get_exchange_rate()
    assert rate == 1500.0
    
    # 원래 값으로 복구
    config.DEFAULT_EXCHANGE_RATE = original_rate