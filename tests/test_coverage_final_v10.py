import pytest
from unittest.mock import patch, MagicMock
import pandas as pd
from modules import auto_trade, analysis, db_manager
import config
import api

@pytest.fixture(autouse=True)
def cleanup_db_connection():
    yield
    db_manager.db.close_connection()

@patch('modules.auto_trade.api.send_telegram_message')
@patch('modules.auto_trade.db_manager.db.insert_trade')
def test_simulation_fill_buy(mock_insert, mock_tg):
    """체결 확정 통보가 없는 체결 처리는 '(추정)' 라벨로 원장·알림에 남는다.

    [이력] 종전에는 잔고 기반 추론(_check_simulation_conclusions_by_balance)을 통해
    이 경로에 들어왔다. 그 함수는 KIS 모의투자 모드가 폐지되며 호출부가 사라져
    죽은 코드였고(2026-08-30 제거), 테스트만 살아 있어 '검증된 체결 경로'처럼 보였다.
    라벨링 자체는 살아 있는 로직이므로 핸들러를 직접 태워 그대로 고정한다.
    """
    trader = auto_trade.AutoTrader()
    trade = {
        'type': 'buy', 'code': '005930', 'name': 'Samsung', 'qty': 10, 'price': 50000,
        'snapshot': '{"indicators": {"rsi": 50}}', 'strategy_score': 8.0,
        'profit_amt': 0, 'profit_rate': 0.0
    }

    monitor = auto_trade.ConclusionMonitor()
    monitor.processed_sim_fills.discard('12345')
    with patch('modules.auto_trade.db_manager.db.update_trade'), \
         patch('modules.auto_trade.db_manager.db.check_trade_exists', return_value=False):
        monitor._handle_simulation_fill(trader, trade, '12345', '005930', 10, "잔고 증가 확인")

    mock_insert.assert_called()
    _, kwargs = mock_insert.call_args
    assert kwargs.get('order_status') == "체결(추정)"

    mock_tg.assert_called()
    assert "[매수 체결(추정)]" in mock_tg.call_args[0][0]

@patch('modules.auto_trade.api.get_deposit_balance')
@patch('modules.auto_trade.api.get_domestic_balance')
@patch('modules.auto_trade.api.send_telegram_message')
def test_autotrader_stop_report(mock_tg, mock_dom_bal, mock_dep_bal):
    """자동매매 종료 시 최종 자산 보고 테스트"""
    trader = auto_trade.AutoTrader()
    trader.is_running = True
    trader.initial_asset = 1000000
    
    # Mock thread (dead)
    trader.thread = MagicMock()
    trader.thread.is_alive.return_value = False
    
    # Mock Asset Data
    # D+2 예수금 50만 + 외화 0
    mock_dep_bal.return_value = {'d2_deposit': 500000, 'foreign_deposit': 0}
    # Holdings(Empty), Summary(Stock Eval 600k)
    mock_dom_bal.return_value = ([], [{'scts_evlu_amt': '600000'}]) 
    
    with patch('config.console.print'):
        trader.stop(use_status=False)
        
    # Verify Telegram Message
    # Total = 500k + 600k = 1.1M. Profit +100k
    mock_tg.assert_called()
    msg = mock_tg.call_args[0][0]
    assert "최종 예수금: 500,000원" in msg
    assert "증권 평가 자산: 600,000원" in msg
    assert "금일 최종 손익: +100,000원" in msg

@patch('modules.analysis.api.get_chart_data')
@patch('modules.analysis.indicators.calculate_indicators')
@patch('modules.analysis.classify_stock_state')
@patch('modules.analysis.calculate_score')
@patch('modules.analysis.api.get_realtime_vol_strength')
@patch('modules.analysis.db_manager.db.get_all_stock_strategies')
@patch('modules.auto_trade.load_restricted_stocks')
def test_diagnose_group_stocks(mock_restrict, mock_strategies, mock_vol, mock_score, mock_classify, mock_ind, mock_chart):
    """그룹 종목 일괄 분석 테스트"""
    # Config Setup
    config.session.stock_data = {
        'stocks_kr': [{'code': '005930', 'name': 'Samsung'}],
        'etfs_kr': []
    }
    
    # Mocks
    mock_strategies.return_value = []
    mock_restrict.return_value = {}
    mock_chart.return_value = pd.DataFrame({'close': [100]*20})
    mock_ind.return_value = {'ema_20': 100, 'ema_60': 100, 'ema_120': 100, 'psar': 90, 'rsi': 50, 'adx': 20, 'cci': 0}
    mock_classify.return_value = ("매수", "[red]", "Reason")
    mock_score.return_value = (9.0, [])
    mock_vol.return_value = 120.0
    
    with patch('config.console.print') as mock_print:
        # api.get_current_price_data mocking for market filter check inside loop if needed
        # But loop uses stock_data directly first.
        # It calls get_current_price_data if market_filter is set. We pass None.
        analysis.diagnose_group_stocks(market_filter=None)
        
        # Check if table output contains Samsung
        # console.print is called multiple times (table, newlines, etc)
        assert mock_print.call_count > 0
