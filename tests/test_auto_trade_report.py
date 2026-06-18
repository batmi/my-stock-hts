import pytest
from unittest.mock import patch, MagicMock
from modules.auto_trade import AutoTrader
import config

@pytest.fixture
def trader():
    return AutoTrader()

@patch('modules.auto_trade.db_manager.db.get_trades')
def test_get_performance_report_no_data(mock_get_trades, trader):
    """데이터가 없을 때 성과 리포트 테스트"""
    mock_get_trades.return_value = []
    report = trader.get_performance_report()
    assert "매매 기록이 없습니다" in report

@patch('modules.auto_trade.db_manager.db.get_trades')
def test_get_performance_report_with_data(mock_get_trades, trader):
    """데이터가 있을 때 성과 리포트 테스트"""
    mock_get_trades.return_value = [
                {'type': 'buy', 'code': '005930', 'name': 'Samsung', 'qty': 10, 'price': 60000, 'time': '2023-01-01 10:00:00', 'odno': '1', 'profit_rate': 0, 'profit_amt': 0, 'reason': '매수', 'order_status': '체결'},
                {'type': 'sell', 'code': '005930', 'name': 'Samsung', 'qty': 10, 'price': 61000, 'time': '2023-01-01 11:00:00', 'odno': '2', 'profit_amt': 10000, 'profit_rate': 1.6, 'reason': '익절', 'order_status': '체결'}
    ]
    report = trader.get_performance_report()
    assert "총 실현 손익: +10,000원" in report
    assert "승률: 100.0%" in report

@patch('modules.auto_trade.api.get_domestic_balance')
@patch('modules.auto_trade.db_manager.db.get_trades')
@patch('rich.prompt.Prompt.ask')
def test_print_report_ui(mock_ask, mock_get_trades, mock_balance, trader):
    """리포트 출력 UI 테스트"""
    mock_balance.return_value = ([], [])
    # 1(일간) 선택
    mock_ask.return_value = "1"
    mock_get_trades.return_value = [
        {'type': 'buy', 'code': '005930', 'name': 'Samsung', 'qty': 10, 'price': 60000, 'time': '2023-01-01 10:00:00', 'odno': '1', 'profit_rate': 0, 'profit_amt': 0, 'reason': '매수', 'order_status': '체결'},
        {'type': 'sell', 'code': '005930', 'name': 'Samsung', 'qty': 10, 'price': 61000, 'time': '2023-01-01 11:00:00', 'odno': '2', 'profit_amt': 10000, 'profit_rate': 1.6, 'reason': '익절', 'order_status': '체결'}
    ]
    
    with patch('config.console.print') as mock_print:
        trader.print_report()
        assert mock_print.call_count > 0

@patch('modules.auto_trade.db_manager.db.get_trades')
@patch('rich.prompt.Prompt.ask')
def test_print_report_ui_custom_days(mock_ask, mock_get_trades, trader):
    """리포트 출력 UI (기간 직접 입력) 테스트"""
    # 4(직접입력) -> 10(일)
    mock_ask.side_effect = ["4", "10"]
    mock_get_trades.return_value = []
    
    with patch('config.console.print') as mock_print:
        trader.print_report()
        assert any("매매 기록이 없습니다" in str(c) for c in mock_print.call_args_list)