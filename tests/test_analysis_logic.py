import pytest
from unittest.mock import patch, MagicMock
from modules import analysis
import pandas as pd
import config

@pytest.fixture
def mock_df():
    return pd.DataFrame({
        'date': pd.date_range(start='2023-01-01', periods=100),
        'close': [10000] * 100,
        'high': [10500] * 100,
        'low': [9500] * 100,
        'open': [10000] * 100,
        'volume': [1000] * 100
    })

@patch('modules.analysis.api.get_chart_data')
def test_diagnose_stock_no_data(mock_get_chart):
    """데이터가 없을 때 분석 함수 처리 테스트"""
    mock_get_chart.return_value = pd.DataFrame()
    
    with patch('config.console.print') as mock_print:
        analysis.diagnose_stock("005930", "Samsung", False)
        # 에러 메시지 출력 확인
        # print 호출 인자 중 하나에 "불러올 수 없습니다"가 포함되어 있는지 확인
        found = False
        for call in mock_print.call_args_list:
            if "불러올 수 없습니다" in str(call):
                found = True
                break
        assert found

@patch('modules.analysis.api.get_chart_data')
@patch('modules.analysis.indicators.calculate_indicators')
@patch('rich.prompt.Prompt.ask', return_value='n')
def test_diagnose_stock_indicators(mock_ask, mock_calc, mock_get_chart, mock_df):
    """지표 계산 후 출력 테스트"""
    mock_get_chart.return_value = mock_df
    mock_calc.return_value = {
        'ema_5': 10000, 'ema_20': 9000, 'ema_60': 8000, 'ema_120': 7000,
        'rsi': 60, 'adx': 30, 'cci': 100, 'obv': 1000, 'obv_trend': True,
        'psar': 9000, 'macd': 50, 'macd_signal': 40, 'macd_hist': 10,
        'atr': 100
    }
    
    with patch('config.console.print') as mock_print:
        analysis.diagnose_stock("005930", "Samsung", False)
        # 테이블 출력 확인 (호출 횟수로 간접 확인)
        assert mock_print.call_count > 5