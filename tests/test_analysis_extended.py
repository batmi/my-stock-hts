import pytest
from unittest.mock import patch, MagicMock
from modules import analysis
import config
import pandas as pd

@pytest.fixture
def mock_chart_df():
    # 분석용 더미 데이터프레임 생성
    df = pd.DataFrame({
        'date': pd.date_range(start='2023-01-01', periods=100),
        'close': [10000] * 100,
        'high': [10500] * 100,
        'low': [9500] * 100,
        'open': [10000] * 100,
        'volume': [1000] * 100
    })
    return df

@patch('modules.analysis.api.get_chart_data')
@patch('modules.analysis.api.get_realtime_vol_strength')
@patch('modules.analysis.indicators.calculate_indicators')
def test_diagnose_stock(mock_calc, mock_vol, mock_get_chart, mock_chart_df):
    """개별 종목 진단 함수 테스트"""
    # Mock 설정
    mock_get_chart.return_value = mock_chart_df
    mock_vol.return_value = 150.0
    
    # 지표 계산 결과 Mock
    mock_calc.return_value = {
        'ema_5': 10000, 'ema_20': 9000, 'ema_60': 8000, 'ema_120': 7000,
        'rsi': 60, 'adx': 30, 'cci': 100, 'obv': 1000, 'obv_trend': True,
        'psar': 9000, 'macd': 50, 'macd_signal': 40, 'macd_hist': 10,
        'atr': 100
    }
    
    # 콘솔 출력 억제 및 실행 확인
    with patch('config.console.print'), patch('config.console.status') as mock_status:
        mock_status.return_value.__enter__.return_value = MagicMock()
        analysis.diagnose_stock("005930", "Samsung", False)
        
    mock_get_chart.assert_called()
    mock_vol.assert_called()

@patch('modules.analysis._get_master_stock_list')
@patch('modules.analysis.api.get_chart_data')
@patch('modules.analysis.api.get_realtime_vol_strength')
def test_analyze_market_stocks(mock_vol, mock_get_chart, mock_master, mock_chart_df):
    """시장 전체 분석 함수 테스트"""
    # Mock 설정
    mock_master.return_value = [{'code': '005930', 'name': 'Samsung'}]
    mock_get_chart.return_value = mock_chart_df
    mock_vol.return_value = 150.0
    
    # 사용자 입력(Prompt)을 'n'(설정 변경 안함)으로 Mocking
    with patch('rich.prompt.Prompt.ask', return_value='n'):
        with patch('config.console.print'), patch('config.console.status') as mock_status:
            mock_status.return_value.__enter__.return_value = MagicMock()
            analysis.analyze_market_stocks("KOSPI")
            
    mock_master.assert_called_with("KOSPI")