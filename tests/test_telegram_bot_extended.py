import pytest
from unittest.mock import patch, MagicMock
from modules.telegram_bot import TelegramCommander
import config

@pytest.fixture
def commander(monkeypatch):
    # conftest 가 TELEGRAM_CHAT_ID 를 ""로 비워 둔다(실제 전송 방지). 명령 수신은
    # 2026-08-10부터 fail-closed 라 빈 값이면 아무 명령도 받지 않으므로, 수신 경로를
    # 시험하는 여기서는 발신자를 명시한다.
    monkeypatch.setattr(config, "TELEGRAM_CHAT_ID", "987654321", raising=False)
    return TelegramCommander()

def test_handle_message_valid(commander):
    """유효한 명령어 처리 테스트"""
    msg = {"text": "/status", "chat": {"id": config.TELEGRAM_CHAT_ID}}
    
    mock_handler = MagicMock()
    
    # 백그라운드 스레드 풀 실행을 가로채어 동기적으로 즉시 실행하도록 변경
    def fake_submit(self, fn, *args, **kwargs):
        fn(*args, **kwargs)
        return MagicMock()
        
    with patch.dict(commander.command_handlers, {'/status': mock_handler}):
        with patch('concurrent.futures.ThreadPoolExecutor.submit', new=fake_submit):
            commander._handle_message(msg)
            mock_handler.assert_called_once()

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


def test_no_chat_id_means_no_command_is_accepted(commander):
    """[Fix 2026-08-10] Chat ID가 비면 인증이 통째로 사라졌다 — fail-closed 로 뒤집었다.

    종전 조건은 `if config.TELEGRAM_CHAT_ID and chat_id != ...` 였다. 환경변수가 없으면
    앞 항이 거짓이라 검사 자체가 건너뛰어져, 봇 토큰만 아는 제3자가 /stop·/config·
    /addrestrict 를 보낼 수 있었다. config 기본값이 ""(config.py)이므로 기계를 옮기며
    변수 하나를 빠뜨리면 조용히 인증이 꺼진 채 실계좌 봇이 도는 상태가 된다.
    """
    saved = config.TELEGRAM_CHAT_ID
    config.TELEGRAM_CHAT_ID = ""
    try:
        mock_handler = MagicMock()
        with patch.dict(commander.command_handlers, {'/stop': mock_handler}):
            with patch.object(commander, '_send_reply') as mock_reply:
                commander._handle_message({"text": "/stop", "chat": {"id": "12345"}})
        assert not mock_handler.called, "수신자를 모르는데 명령을 실행했다"
        assert not mock_reply.called
    finally:
        config.TELEGRAM_CHAT_ID = saved


def test_missing_chat_id_is_announced_at_startup():
    """명령이 전부 무시되는 상태를 모르면 '봇이 죽었다'로 오해한다."""
    import inspect
    from modules import telegram_bot
    src = inspect.getsource(telegram_bot.TelegramCommander.start)
    assert "TELEGRAM_CHAT_ID" in src, "미설정 경고가 기동 경로에 없다"
