import pytest
from unittest.mock import patch, MagicMock
import main
import config

# [추가] time.sleep을 패치하여 테스트 속도 향상 (finally 블록의 대기 시간 제거)
@pytest.fixture(autouse=True)
def mock_sleep():
    with patch('time.sleep'):
        yield

# [추가] db_queue.shutdown 패치 (미초기화 상태에서의 블로킹 방지)
@pytest.fixture(autouse=True)
def mock_db_queue():
    with patch('modules.db_queue.shutdown'), patch('modules.db_queue.install_proxy'):
        yield

@patch('main.os._exit')
@patch('rich.prompt.Prompt.ask')
@patch('main.settings.system_config_menu')
@patch('main.market.show_market_indices')
@patch('main.analysis.show_stock_analysis')
@patch('main.trading.stock_order_menu')
@patch('main.auto_trade.system_trading_menu')
@patch('main.backtest.run_backtest')
@patch('main.theme_analysis.run_theme_analysis')
@patch('main.manage.manage_stock_menu')
@patch('main.account.asset_management_menu', return_value=False)
def test_main_menu_navigation(mock_asset, mock_manage, mock_theme, mock_backtest, mock_auto, mock_order, mock_analysis, mock_market, mock_settings, mock_ask, mock_exit):
    """메인 메뉴 네비게이션 테스트"""
    # 변경된 순서에 맞춰 메뉴 선택 후 종료 (0:설정, 1:지수, 2:시세, 4:백테스트, 5:자동매매, 6:트랜드, 7:종목관리, 8:주문, 9:자산)
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
    mock_backtest.assert_called()  # [4] 전략 백테스팅
    mock_auto.assert_called()      # [5] 시스템 트레이딩
    mock_theme.assert_called()     # [6] 종목 트랜드 분석
    mock_manage.assert_called()    # [7] 관심 종목 관리
    mock_order.assert_called()     # [8] 종목 주문 관리
    mock_asset.assert_called()     # [9] 자산 관리
    
    # os._exit(0) 호출 확인
    assert mock_exit.called

@patch('main.os._exit')
@patch('rich.prompt.Prompt.ask')
@patch('modules.chart.generate_visual_chart')
@patch('api.get_current_price_data')
def test_main_chart_menu(mock_cp, mock_chart, mock_ask, mock_exit):
    """메인 메뉴 -> 차트 분석 메뉴 테스트"""
    # 3번(차트) -> 6번(직접입력) -> 코드입력 -> 유효성확인(y) -> 1번(일봉) -> 1번(6개월)
    # 이후 부족분은 'q'를 자동 반환하여 흐름 변화에 무관하게 메뉴를 빠져나가게 한다
    # (StopIteration/무한루프 방지)
    _inputs = iter(["3", "6", "005930", "y", "1", "1"])
    mock_ask.side_effect = lambda *a, **k: next(_inputs, "q")
    mock_cp.return_value = {'rt_cd': '0', 'output': {'stck_prpr': '100'}}
    
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
                    
    # main.py에서 keyword args로 호출하므로 period_type/months 확인 (일봉 6개월)
    mock_chart.assert_called_with("005930", "삼성전자", False, period_type='daily', months=6)
    assert mock_exit.called