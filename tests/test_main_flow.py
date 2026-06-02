import pytest
from unittest.mock import patch, MagicMock
import main
import sys

@patch('main.auto_trade.AutoTrader')
@patch('main.telegram_bot.TelegramCommander')
@patch('main.auto_trade.ConclusionMonitor')
@patch('main.preflight_check', return_value=True)
@patch('main.api.check_and_refresh_token_if_expired')
@patch('main.config.session.load_stock_config')
@patch('main.config.session.initialize')
@patch('main.Prompt.ask')
@patch('main.os._exit')
@patch('main.time.sleep')
@patch('modules.db_queue.install_proxy')
def test_main_auto_mode(mock_install_proxy, mock_sleep, mock_exit, mock_ask, mock_init, mock_load, mock_check_token, mock_preflight, mock_monitor, mock_bot, mock_trader):
    """메인 함수 자동 시작 모드 테스트"""
    test_args = ['main.py', '--mode', '1', '--auto']
    
    # Prompt.ask가 호출되면 'q'를 반환하여 루프 종료 유도
    mock_ask.return_value = 'q'

    # view_log_file은 단순히 리턴하도록 설정 (블로킹 방지)
    mock_trader.return_value.view_log_file.return_value = None

    with patch.object(sys, 'argv', test_args):
        main.main()
                
    mock_init.assert_called_with(mode='1')
    mock_trader.return_value.start.assert_called_with(interactive=False)
    mock_exit.assert_called_with(0)

@patch('main.Prompt.ask')
@patch('main.os._exit')
def test_main_menu_exit(mock_exit, mock_ask):
    """메인 메뉴 종료 테스트"""
    mock_ask.return_value = 'q'
    
    # 의존성 모킹
    with patch('main.config.session'), \
         patch('main.api'), \
         patch('main.auto_trade'), \
         patch('main.telegram_bot'), \
         patch('modules.db_queue.install_proxy'):
        
        with patch.object(sys, 'argv', ['main.py']):
             main.main()
    
    mock_exit.assert_called_with(0)