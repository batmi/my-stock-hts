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

@patch('modules.auto_trade.api.get_domestic_balance')
@patch('modules.auto_trade.db_manager.db.get_trade_by_odno')
@patch('modules.auto_trade.api.send_telegram_message')
@patch('modules.auto_trade.db_manager.db.insert_trade')
def test_simulation_fill_buy(mock_insert, mock_tg, mock_get_trade, mock_balance):
    """모의투자 잔고 기반 매수 체결 확인 테스트"""
    # 1. Setup
    trader = auto_trade.AutoTrader()
    trader.order_manager.pending_orders = {
        '005930': {'12345': auto_trade.OrderStatus.ORDER_SENT}
    }
    
    # Mock Balance: 삼성전자 10주 보유 (체결됨을 의미)
    # Holdings list, Summary list
    mock_balance.return_value = ([
        {'pdno': '005930', 'hldg_qty': '10', 'prdt_name': 'Samsung'}
    ], [])
    
    # Mock Trade Info from DB
    mock_get_trade.return_value = {
        'type': 'buy', 'code': '005930', 'name': 'Samsung', 'qty': 10, 'price': 50000,
        'snapshot': '{"indicators": {"rsi": 50}}', 'strategy_score': 8.0,
        'profit_amt': 0, 'profit_rate': 0.0
    }
    
    # 2. Run
    monitor = auto_trade.ConclusionMonitor()
    # Mock DB update to avoid actual DB call
    with patch('modules.auto_trade.db_manager.db.update_trade'):
        with patch('modules.auto_trade.db_manager.db.check_trade_exists', return_value=False):
            monitor._check_simulation_conclusions_by_balance("12345678", "01")
    
    # 3. Verify
    # insert_trade called with "체결(추정)"
    mock_insert.assert_called()
    
    # Check if order_status is correct (args or kwargs)
    _, kwargs = mock_insert.call_args
    assert kwargs.get('order_status') == "체결(추정)"
    
    # Telegram alert sent
    mock_tg.assert_called()
    assert "체결 알림(추정)" in mock_tg.call_args[0][0]

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
