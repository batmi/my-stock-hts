import pytest
from unittest.mock import patch, MagicMock
import api
import config
import os

@pytest.fixture
def mock_env():
    # 테스트용 환경변수 설정 백업 및 가짜 값 설정
    original_token = config.TELEGRAM_BOT_TOKEN
    original_chat_id = config.TELEGRAM_CHAT_ID
    
    config.TELEGRAM_BOT_TOKEN = "TEST_TOKEN_12345"
    config.TELEGRAM_CHAT_ID = "987654321"
    
    yield
    
    # 복구
    config.TELEGRAM_BOT_TOKEN = original_token
    config.TELEGRAM_CHAT_ID = original_chat_id

def test_send_telegram_message_success(mock_env):
    """텔레그램 메시지 전송 성공 테스트"""
    with patch('requests.post') as mock_post:
        # Mock 응답 설정 (성공 200)
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_post.return_value = mock_response
        
        msg = "테스트 메시지입니다."
        api.send_telegram_message(msg, sync=True)
        
        # requests.post가 호출되었는지 확인
        assert mock_post.called
        args, kwargs = mock_post.call_args
        
        # URL 및 데이터 확인
        expected_url = f"https://api.telegram.org/bot{config.TELEGRAM_BOT_TOKEN}/sendMessage"
        assert args[0] == expected_url
        assert kwargs['data']['chat_id'] == config.TELEGRAM_CHAT_ID
        assert msg in kwargs['data']['text']

def test_send_telegram_message_retry_logic(mock_env):
    """전송 실패 시 재시도 로직 테스트"""
    with patch('requests.post') as mock_post:
        # 첫 번째, 두 번째는 실패(500), 세 번째 성공(200) 시나리오
        fail_response = MagicMock()
        fail_response.status_code = 500
        fail_response.text = "Internal Server Error"
        
        success_response = MagicMock()
        success_response.status_code = 200
        
        mock_post.side_effect = [fail_response, fail_response, success_response]
        
        # time.sleep을 모킹하여 테스트 대기 시간 제거
        with patch('time.sleep'):
            api.send_telegram_message("재시도 테스트", sync=True)
            
        # 총 3번 호출되었는지 확인 (2번 실패 후 3번째 성공)
        assert mock_post.call_count == 3

def test_send_telegram_message_no_token():
    """토큰이 설정되지 않았을 때 전송 시도하지 않음"""
    # 토큰 제거
    original_token = config.TELEGRAM_BOT_TOKEN
    config.TELEGRAM_BOT_TOKEN = ""
    
    with patch('requests.post') as mock_post:
        api.send_telegram_message("토큰 없음", sync=True)
        # 호출되지 않아야 함
        assert not mock_post.called
        
    # 복구
    config.TELEGRAM_BOT_TOKEN = original_token

def test_send_telegram_photo_success(mock_env, tmp_path):
    """사진 전송 성공 테스트"""
    # 임시 이미지 파일 생성
    img_file = tmp_path / "test_chart.png"
    img_file.write_bytes(b"fake image data")
    
    with patch('requests.post') as mock_post:
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_post.return_value = mock_response
        
        ret = api.send_telegram_photo(str(img_file), caption="차트")
        
        assert ret is True
        assert mock_post.called
        
        # 파일이 전송되었는지 확인 (files 인자)
        _, kwargs = mock_post.call_args
        assert 'files' in kwargs
        assert 'photo' in kwargs['files']
        assert kwargs['data']['caption'].startswith("차트")

def test_send_telegram_photo_file_not_found(mock_env):
    """존재하지 않는 파일 전송 시도 시 실패 처리"""
    with patch('requests.post') as mock_post:
        # 존재하지 않는 파일 경로
        ret = api.send_telegram_photo("non_existent_file.png")
        
        assert ret is False
        assert not mock_post.called