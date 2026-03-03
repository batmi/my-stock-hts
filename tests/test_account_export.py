import pytest
from unittest.mock import patch, MagicMock, call
from modules import account
import pandas as pd
import os

@patch('modules.account.db_manager.db.get_trades')
@patch('pandas.DataFrame.to_excel')
@patch('pandas.ExcelWriter')
def test_export_trade_history_to_excel(mock_writer, mock_to_excel, mock_get_trades):
    """거래 내역 엑셀 저장 테스트"""
    # Mock DB data
    mock_get_trades.return_value = [
        {'time': '2023-01-01 10:00:00', 'account': '12345678-01', 'is_sim': 1, 'odno': '1', 'type': 'buy', 'name': 'Samsung', 'code': '005930', 'qty': 10, 'price': 60000}
    ]
    
    with patch('config.console.print') as mock_print:
        with patch('rich.prompt.Prompt.ask', return_value='y'): 
             account.export_trade_history_to_excel()
             
    mock_get_trades.assert_called()
    mock_writer.assert_called()
    # 성공 메시지 출력 확인
    assert any("성공적으로 저장되었습니다" in str(c) for c in mock_print.call_args_list)

@patch('modules.account.db_manager.db.get_trades')
def test_export_trade_history_no_data(mock_get_trades):
    """데이터 없을 때 저장 시도 테스트"""
    mock_get_trades.return_value = []
    
    with patch('config.console.print') as mock_print:
        account.export_trade_history_to_excel()
        
    assert any("저장할 거래 내역이 없습니다" in str(c) for c in mock_print.call_args_list)