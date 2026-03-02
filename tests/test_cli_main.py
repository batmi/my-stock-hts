import pytest
from unittest.mock import patch, MagicMock
import main
import config

@patch('main.os._exit')
@patch('main.Prompt.ask')
@patch('main.settings.system_config_menu')
@patch('main.market.show_market_indices')
@patch('main.analysis.show_stock_analysis')
@patch('main.trading.stock_order_menu')
@patch('main.auto_trade.system_trading_menu')
@patch('main.backtest.run_backtest')
@patch('main.theme_analysis.run_theme_analysis')
@patch('main.manage.manage_stock_menu')
@patch('main.account.asset_management_menu')
def test_main_menu_navigation(mock_asset, mock_manage, mock_theme, mock_backtest, mock_auto, mock_order, mock_analysis, mock_market, mock_settings, mock_ask, mock_exit):
    """메인 메뉴 네비게이션 테스트"""
    # 순서대로 메뉴 선택 후 종료 (0 -> 1 -> ... -> q)
    mock_ask.side_effect = ["0", "1", "2", "4", "5", "6", "7", "8", "9", "q"]
    
    # 무한 루프 방지를 위해 KeyboardInterrupt 발생 시뮬레이션 대신 side_effect로 종료 유도
    # main() 함수는 while True 루프를 돌므로, q 입력 시 break 되도록 설계됨
    
    # main.py의 main() 함수 실행 (인자 없이)
    with patch('sys.argv', ['main.py']):
        with patch('config.session.initialize'): # 초기화 로직 스킵
            with patch('config.session.load_stock_config'):
                with patch('api.get_access_token'):
                    with patch('api.get_real_access_token'):
                        with patch('modules.auto_trade.ConclusionMonitor.start'):
                            with patch('modules.telegram_bot.TelegramCommander.start'):
                                try:
                                    main.main()
                                except SystemExit:
                                    pass
    
    # 각 메뉴 함수가 호출되었는지 검증
    mock_settings.assert_called()
    mock_market.assert_called()
    mock_analysis.assert_called()
    mock_order.assert_called()
    mock_auto.assert_called()
    mock_backtest.assert_called()
    mock_theme.assert_called()
    mock_manage.assert_called()
    mock_asset.assert_called()
    
    # os._exit(0) 호출 확인
    assert mock_exit.called

@patch('main.os._exit')
@patch('main.Prompt.ask')
@patch('modules.chart.generate_visual_chart')
def test_main_chart_menu(mock_chart, mock_ask, mock_exit):
    """메인 메뉴 -> 차트 분석 메뉴 테스트"""
    # 3번(차트) -> 6번(직접입력) -> 코드입력 -> 종료
    mock_ask.side_effect = ["3", "6", "005930", "q"]
    
    with patch('sys.argv', ['main.py']):
        with patch('config.session.initialize'), \
             patch('config.session.load_stock_config'), \
             patch('api.get_access_token'), \
             patch('api.get_real_access_token'), \
             patch('modules.auto_trade.ConclusionMonitor.start'), \
             patch('modules.telegram_bot.TelegramCommander.start'), \
             patch('api.get_stock_name_by_code', return_value="삼성전자"):
                try:
                    main.main()
                except SystemExit:
                    pass
                    
    mock_chart.assert_called_with("005930", "삼성전자", False)
    assert mock_exit.called