import pytest
from unittest.mock import patch, MagicMock
from modules.telegram_bot import TelegramCommander
import config

@pytest.fixture
def commander():
    return TelegramCommander()

def test_handle_message_valid(commander):
    """유효한 명령어 처리 테스트"""
    msg = {"text": "/status", "chat": {"id": config.TELEGRAM_CHAT_ID}}
    
    with patch.object(commander, '_send_reply') as mock_reply:
        # [수정] _cmd_status 대신 내부에서 호출하는 trader.get_status_message를 Mocking
        with patch.object(commander.trader, 'get_status_message', return_value="System OK"):
            commander._handle_message(msg)
            mock_reply.assert_any_call("System OK")

def test_handle_message_invalid_chat_id(commander):
    """잘못된 Chat ID 무시 테스트"""
    config.TELEGRAM_CHAT_ID = "99999"  # 다른 테스트로부터의 상태 오염 방지
    msg = {"text": "/status", "chat": {"id": "12345"}} # config와 다름
    
    with patch.object(commander, '_send_reply') as mock_reply:
        commander._handle_message(msg)
        assert not mock_reply.called

def test_handle_message_unknown_command(commander):
    """알 수 없는 명령어 안내 메시지 발송 테스트"""
    msg = {"text": "/unknown", "chat": {"id": config.TELEGRAM_CHAT_ID}}
    
    with patch.object(commander, '_send_reply') as mock_reply:
        commander._handle_message(msg)
        mock_reply.assert_called_once()
        assert "지원하지 않는 명령어" in mock_reply.call_args[0][0]

@patch('modules.telegram_bot.api.send_telegram_message')
def test_send_reply(mock_send, commander):
    """답장 전송 테스트"""
    commander._send_reply("Test Reply")
    mock_send.assert_called_once()
    args, kwargs = mock_send.call_args
    assert args[0] == "Test Reply"
    assert "reply_markup" in kwargs