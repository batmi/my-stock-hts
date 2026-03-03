import pytest
from unittest.mock import patch, MagicMock
from modules import account
import config

@patch('modules.account.db_manager.db.get_trades')
@patch('modules.account.sync_today_trades')
@patch('rich.prompt.Prompt.ask')
def test_view_trade_history_all(mock_ask, mock_sync, mock_get_trades):
    """거래 내역 조회 (전체) 테스트"""
    # 1(전체) -> q(종료)
    mock_ask.side_effect = ["1", "q"]
    
    mock_get_trades.return_value = [
        {'time': '2023-01-01 10:00:00', 'type': 'buy', 'name': 'Samsung', 'code': '005930', 'qty': 10, 'price': 60000, 'odno': '1', 'is_sim': 1, 'account': '1234-01'}
    ]
    
    with patch('config.console.print') as mock_print:
        account.view_trade_history()
        # 테이블 출력 확인
        assert mock_print.call_count > 0

@patch('modules.account.db_manager.db.get_trades')
@patch('modules.account.sync_today_trades')
@patch('rich.prompt.Prompt.ask')
def test_view_trade_history_search(mock_ask, mock_sync, mock_get_trades):
    """거래 내역 검색 테스트"""
    # 3(검색) -> 005930 -> q
    mock_ask.side_effect = ["3", "005930", "q"]
    
    mock_get_trades.return_value = []
    
    with patch('config.console.print') as mock_print:
        account.view_trade_history()
        assert any("검색된 거래 내역이 없습니다" in str(c) for c in mock_print.call_args_list)

@patch('modules.account.export_trade_history_to_excel')
@patch('modules.account.sync_today_trades')
@patch('rich.prompt.Prompt.ask')
def test_view_trade_history_export(mock_ask, mock_sync, mock_export):
    """거래 내역 엑셀 저장 메뉴 테스트"""
    # 4(저장)
    mock_ask.return_value = "4"
    
    account.view_trade_history()
    mock_export.assert_called_once()