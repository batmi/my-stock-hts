import pytest
from unittest.mock import patch, MagicMock
import api
import config
from core import context

@patch('api.get_auto_access_token', return_value="auto_token")
@patch('api.get_real_access_token')
@patch('api.send_telegram_message')
def test_check_and_refresh_token_expired(mock_tg, mock_real, mock_auto):
    """토큰 만료 감지 및 갱신 테스트"""
    context.TOKEN_EXPIRED = True
    context.LAST_TOKEN_REFRESH_ATTEMPT = 0   # 쿨타임(60초)에 걸려 조기 반환되지 않게

    mock_real.return_value = "refreshed_token"

    api.check_and_refresh_token_if_expired()

    assert context.TOKEN_EXPIRED is False
    mock_real.assert_called_with(force_refresh=True)

@patch('api._token_session.post')
def test_get_real_access_token_success(mock_post):
    """실전투자 토큰 발급 성공 테스트"""
    mock_post.return_value.status_code = 200
    mock_post.return_value.json.return_value = {
        'access_token': 'real_token',
        'access_token_token_expired': '2025-01-01 12:00:00'
    }
    
    config.session.real_app_key = "real_key"
    config.session.real_app_secret = "real_secret"
    
    token = api._get_real_access_token_internal(force_refresh=True)
    assert token == 'real_token'