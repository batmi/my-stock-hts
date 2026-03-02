import pytest
from unittest.mock import patch, MagicMock
from modules.telegram_bot import TelegramCommander
import config

@pytest.fixture
def commander():
    config.ENABLE_TELEGRAM = True
    config.TELEGRAM_BOT_TOKEN = "TEST_TOKEN"
    return TelegramCommander()

def test_cmd_help(commander):
    """도움말 명령어 테스트"""
    response = commander._cmd_help([])
    assert "도움말" in response
    assert "/start" in response

@patch('modules.telegram_bot.AutoTrader')
def test_cmd_status(mock_trader_cls, commander):
    """상태 조회 명령어 테스트"""
    mock_trader = mock_trader_cls.return_value
    mock_trader.get_status_message.return_value = "System Running"
    commander.trader = mock_trader  # Mock 객체 주입
    
    response = commander._cmd_status([])
    assert response == "System Running"

@patch('modules.telegram_bot.AutoTrader')
def test_cmd_start_stop(mock_trader_cls, commander):
    """시작/중지 명령어 테스트"""
    mock_trader = mock_trader_cls.return_value
    commander.trader = mock_trader  # Mock 객체 주입
    
    # Start
    mock_trader.is_running = False
    response = commander._cmd_start([])
    assert "시작했습니다" in response
    mock_trader.start.assert_called_once()
    
    # Stop
    mock_trader.is_running = True
    response = commander._cmd_stop([])
    assert "중단 요청" in response
    mock_trader.stop.assert_called_once()

@patch('modules.telegram_bot.db_manager.db.get_all_stock_strategies')
def test_cmd_rules(mock_get_rules, commander):
    """룰 조회 명령어 테스트"""
    mock_get_rules.return_value = [
        {
            'code': '005930', 'name': '삼성전자', 'buy_score': 8.0, 'buy_rsi': 60, 
            'sell_score': 5.0, 'take_profit': 10, 'stop_loss': -5, 
            'ts_activation': 5, 'ts_callback': 2, 'take_profit_rsi': 75, 'buy_vol_strength': 100
        }
    ]
    
    response = commander._cmd_rules([])
    assert "삼성전자" in response
    assert "005930" in response
    assert "매수: 8.0점" in response
