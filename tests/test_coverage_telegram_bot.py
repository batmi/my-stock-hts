import pytest
from unittest.mock import patch, MagicMock
from modules.telegram_bot import TelegramCommander
import config
from datetime import datetime

@pytest.fixture
def commander():
    cmd = TelegramCommander()
    cmd.trader = MagicMock()
    return cmd

def test_handle_message_branches(commander):
    """명령어 파싱 및 단축어 처리 커버리지"""
    config.TELEGRAM_CHAT_ID = "12345"
    
    with patch.object(commander, '_cmd_signal') as mock_signal, \
         patch.object(commander, '_cmd_chart') as mock_chart, \
         patch.dict(commander.command_handlers, {'/signal': mock_signal, '/chart': mock_chart}), \
         patch.object(commander, '_send_reply'):
             
        # 단축 명령어 테스트
        msg1 = {'chat': {'id': 12345}, 'text': '/signal_005930'}
        commander._handle_message(msg1)
        mock_signal.assert_called_with(['005930'])
        
        msg2 = {'chat': {'id': 12345}, 'text': '/chart_AAPL'}
        commander._handle_message(msg2)
        mock_chart.assert_called_with(['aapl'])
        
        # 권한 없는 채팅 ID (수행되지 않아야 함)
        msg3 = {'chat': {'id': 99999}, 'text': '/status'}
        commander._handle_message(msg3)

@patch('modules.theme_analysis.ask_gemini', return_value="AI Answer")
def test_cmd_ask_branch(mock_ask, commander):
    """/ask 명령어 분기 테스트"""
    with patch.object(commander, '_send_reply'):
        res = commander._cmd_ask(['What', 'is', 'AI?'])
        assert "AI Answer" in res
        
        # 인자 누락
        res_empty = commander._cmd_ask([])
        assert "질문을 입력해주세요" in res_empty

def test_cmd_news_branch(commander):
    """/news 명령어 백그라운드 호출 테스트"""
    with patch.object(commander, '_send_reply') as mock_reply, \
         patch('modules.telegram_bot.threading.Thread') as mock_thread:
        
        res = commander._cmd_news(['삼성전자'])
        assert res is None
        mock_reply.assert_called_once()
        mock_thread.assert_called_once()
        
        # 인자 누락
        assert "사용법" in commander._cmd_news([])

@patch('modules.telegram_bot.datetime')
@patch('modules.telegram_bot.threading.Thread')
def test_check_morning_briefing_scheduler(mock_thread, mock_dt, commander):
    """장전 브리핑 스케줄러 시간 조건 테스트"""
    commander.last_briefing_date = None
    config.AUTO_MORNING_BRIEFING_USE = True
    config.AUTO_MORNING_BRIEFING_TIME = "0830"
    
    # 08:40 시뮬레이션 (수요일)
    mock_now = datetime(2023, 11, 1, 8, 40)
    mock_dt.now.return_value = mock_now
    mock_dt.strptime = datetime.strptime
    mock_dt.combine = datetime.combine
    mock_dt.today.return_value = mock_now.date()
    
    commander._check_morning_briefing()
    assert commander.last_briefing_date == "2023-11-01"
    mock_thread.assert_called_once()

def test_cmd_market_invalid_key(commander):
    """/market 명령어 오입력 테스트"""
    res = commander._cmd_market(["z"]) # 존재하지 않는 키
    assert "잘못된 그룹 키" in res