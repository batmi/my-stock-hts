import pytest
from unittest.mock import patch, MagicMock
from modules import analysis
import config

@patch('modules.analysis._get_master_stock_list')
@patch('modules.analysis._analyze_stock_worker')
@patch('rich.prompt.Prompt.ask')
def test_analyze_market_stocks(mock_ask, mock_worker, mock_master):
    """시장 전체 분석 함수 테스트"""
    # Setup
    mock_master.return_value = [{'code': '005930', 'name': 'Samsung'}]
    mock_worker.return_value = {
        'code': '005930', 'name': 'Samsung', 'price': 60000,
        'score': 9.0, 'state': '매수', 'state_color': '[red]', 'state_reason': 'Good',
        'rsi': 50, 'adx': 30, 'cci': 100, 'obv_trend': True, 'psar': 50000,
        'is_target': True, 'vol_strength': 150.0, 'w52_pos': 80.0
    }
    
    # User Input Mock (새로 분석 -> 설정 변경 안함 -> 상세 분석 메뉴에서 종료)
    mock_ask.side_effect = ['n', 'n', 'q'] 
    
    with patch('config.console.print'):
        analysis.analyze_market_stocks("KOSPI")
        
    mock_master.assert_called()
    mock_worker.assert_called()