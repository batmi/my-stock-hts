import pytest
from unittest.mock import patch, MagicMock
from modules import analysis
import config
import pandas as pd

@pytest.fixture
def mock_chart_df():
    df = pd.DataFrame({
        'date': pd.date_range(start='2023-01-01', periods=100),
        'close': [10000] * 100,
        'high': [10500] * 100,
        'low': [9500] * 100,
        'open': [10000] * 100,
        'volume': [1000] * 100
    })
    return df

@patch('modules.analysis._get_master_stock_list')
@patch('modules.analysis.api.get_chart_data')
@patch('modules.analysis.api.get_realtime_vol_strength')
@patch('modules.analysis.api.get_current_price_data')
def test_diagnose_group_stocks(mock_cp, mock_vol, mock_get_chart, mock_master, mock_chart_df):
    """그룹 종목 진단 테스트"""
    # Mock 설정
    config.session.stock_data = {"stocks_kr": [{"code": "005930", "name": "Samsung"}]}
    mock_get_chart.return_value = mock_chart_df
    mock_vol.return_value = 150.0
    mock_cp.return_value = {'rt_cd': '0', 'output': {'rprs_mrkt_kor_name': 'KOSPI'}}
    
    # 콘솔 출력 억제
    with patch('config.console.print'), patch('modules.analysis.Progress'):
        analysis.diagnose_group_stocks(market_filter="KOSPI")
        
    mock_get_chart.assert_called()

@patch('modules.analysis._get_master_stock_list')
@patch('modules.analysis.api.get_chart_data')
@patch('modules.analysis.api.get_current_price_data')
@patch('rich.prompt.Prompt.ask')
def test_save_all_market_analysis(mock_ask, mock_cp, mock_get_chart, mock_master, mock_chart_df):
    """전체 시장 분석 저장 테스트"""
    # Mock 설정
    mock_ask.return_value = "y"
    mock_master.return_value = [{'code': '005930', 'name': 'Samsung'}]
    mock_get_chart.return_value = mock_chart_df
    mock_cp.return_value = {'rt_cd': '0', 'output': {'bstp_kor_isnm': 'Electrical'}}
    
    # 엑셀 저장 모킹
    with patch('pandas.DataFrame.to_excel'), patch('pandas.ExcelWriter'):
        with patch('config.console.print'), patch('modules.analysis.Progress'):
            analysis.save_all_market_analysis()
            
    mock_master.assert_called()