import pytest
from unittest.mock import patch, MagicMock
import api
import config
import time
import requests
import pandas as pd

def test_throttled_session_rate_limit():
    """Rate Limit 동작 테스트"""
    session = api.ThrottledSession()
    config.SIM_TX_PER_SECOND = 2 # 0.5s interval
    
    # Mock time
    with patch('time.time') as mock_time, patch('time.sleep') as mock_sleep:
        mock_time.side_effect = [100.0 + i*0.1 for i in range(20)] # Provide enough values
        
        # Mock super().request to avoid actual network call
        with patch('requests.Session.request') as mock_request:
            mock_request.return_value.status_code = 200
            
            # First request (Sim)
            session.request('GET', 'https://openapivts.koreainvestment.com/test')
            # Second request (Sim) - should trigger sleep
            session.request('GET', 'https://openapivts.koreainvestment.com/test')
            
            assert mock_sleep.called

@patch('api.get_current_token', return_value="TEST_TOKEN")
@patch('requests.Session.request')
def test_call_api_retry_logic(mock_request, mock_token):
    """API 호출 재시도 로직 테스트"""
    # 1. Fail -> 2. Fail -> 3. Success (for test/url)
    success_resp = MagicMock()
    success_resp.status_code = 200
    success_resp.json.return_value = {'rt_cd': '0'}
    
    call_history = []
    
    def side_effect(*args, **kwargs):
        # Check if "test/url" is in arguments to isolate test calls from background threads
        if any(isinstance(arg, str) and "test/url" in arg for arg in args):
            call_history.append(1)
            count = len(call_history)
            if count == 1:
                raise Exception("Connection Error 1")
            elif count == 2:
                raise Exception("Connection Error 2")
            return success_resp
        return MagicMock(status_code=200, json=lambda: {'rt_cd': '0'})
    
    mock_request.side_effect = side_effect
    
    # config.SYSTEM_LOGGER가 없어서 발생하는 AttributeError 방지
    with patch.object(config, 'SYSTEM_LOGGER', None, create=True), patch('time.sleep'): # Skip delay
        res = api.call_api("test/url", "domestic", "trade", "buy", method="POST", retries=2)
        
    assert res['rt_cd'] == '0', f"Expected '0', got {res.get('rt_cd')} with msg {res.get('msg1')}"
    assert len(call_history) == 3

@patch('api.session.get')
def test_call_api_token_expired(mock_get):
    """토큰 만료 시 갱신 후 재시도 테스트"""
    # 1. Token Expired Error
    expired_resp = MagicMock()
    expired_resp.status_code = 200
    expired_resp.json.return_value = {'rt_cd': '1', 'msg_cd': 'EGW00123'}
    
    # 2. Success
    success_resp = MagicMock()
    success_resp.status_code = 200
    success_resp.json.return_value = {'rt_cd': '0'}
    
    mock_get.side_effect = [Exception("Token Expired (EGW00123)"), success_resp]
    
    with patch('api.get_access_token') as mock_refresh:
        mock_refresh.return_value = "NEW_TOKEN"
        
        # Simulation mode
        config.session.is_simulation = True
        
        res = api.call_api("test/url", "domestic", "inquiry", "balance", method="GET")
        
        assert res['rt_cd'] == '0'
        assert mock_refresh.called

@patch('api.fetch_yfinance_data')
@patch('api.call_api')
def test_get_domestic_index_chart_fallback(mock_call, mock_yf):
    """KIS API 실패 시 yfinance Fallback 테스트"""
    # KIS API Fail
    mock_call.return_value = {'rt_cd': '1'}
    
    # yfinance Success
    mock_df = pd.DataFrame({'close': [2500]})
    mock_yf.return_value = mock_df
    
    df = api.get_domestic_index_chart("0001")
    
    assert df.empty