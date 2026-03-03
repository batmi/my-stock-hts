import pytest
from unittest.mock import patch, MagicMock
import api
import config
import requests

def test_throttled_session_logging():
    """ThrottledSession 로깅 및 에러 처리 테스트"""
    session = api.ThrottledSession()
    config.SCREEN_DEBUG_LEVEL = "DEBUG"
    
    with patch('requests.Session.request') as mock_request:
        # 1. Normal response
        mock_request.return_value = MagicMock(status_code=200, json=lambda: {'rt_cd': '0'})
        session.request('GET', 'http://test.com')
        
        # 2. Error response (500)
        mock_request.return_value = MagicMock(status_code=500, text="Server Error")
        try:
            session.request('GET', 'http://test.com', retries=0)
        except: pass
        
        # 3. API Error (rt_cd != 0)
        mock_request.return_value = MagicMock(status_code=200, json=lambda: {'rt_cd': '1', 'msg1': 'Error'})
        try:
            session.request('GET', 'http://test.com', retries=0)
        except: pass

    config.SCREEN_DEBUG_LEVEL = "OFF"