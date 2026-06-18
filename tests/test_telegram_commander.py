import pytest
from unittest.mock import patch, MagicMock
from modules.telegram_bot import TelegramCommander
from modules.auto_trade import AutoTrader
import config

@pytest.fixture
def commander():
    config.ENABLE_TELEGRAM = True
    config.TELEGRAM_BOT_TOKEN = "TEST_TOKEN"
    # 테스트 간 상태 오염 방지를 위한 싱글톤 인스턴스 강제 초기화
    TelegramCommander._instance = None
    AutoTrader._instance = None
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

@patch('modules.auto_trade.db_manager.db.get_trades')
def test_cmd_report(mock_get_trades, commander):
    """리포트 명령어 테스트"""
    # Mock DB response
    mock_get_trades.return_value = [
            {'type': 'buy', 'code': '005930', 'name': 'Samsung', 'qty': 10, 'price': 60000, 'time': '2023-01-01 10:00:00', 'odno': '1', 'profit_rate': 0, 'profit_amt': 0, 'reason': '매수', 'order_status': '체결'},
            {'type': 'sell', 'code': '005930', 'name': 'Samsung', 'qty': 10, 'price': 61000, 'time': '2023-01-01 11:00:00', 'odno': '2', 'profit_amt': 10000, 'profit_rate': 1.6, 'reason': '익절', 'order_status': '체결'}
    ]
    
    # /report d (일간)
    res = commander._cmd_report(['d'])
    assert "시스템 트레이딩 성과 리포트" in res
    assert "총 실현 손익: +10,000원" in res
    
    # 데이터 없음
    mock_get_trades.return_value = []
    res_empty = commander._cmd_report([])
    assert "매매 기록이 없습니다" in res_empty
