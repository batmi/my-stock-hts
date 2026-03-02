import pytest
from unittest.mock import patch, MagicMock
from modules import analysis
import config
import pandas as pd

@patch('modules.analysis.api.get_chart_data')
@patch('modules.analysis.indicators.calculate_indicators')
@patch('modules.analysis.classify_stock_state')
@patch('modules.analysis.calculate_score')
@patch('modules.analysis.api.get_realtime_vol_strength')
def test_analyze_stock_worker(mock_vol, mock_score, mock_classify, mock_calc, mock_get_chart):
    """단일 종목 분석 워커 테스트"""
    # Mock Data
    mock_df = pd.DataFrame({
        'close': [10000]*20,
        'high': [10500]*20,
        'low': [9500]*20,
        'open': [10000]*20,
        'volume': [1000]*20
    })
    mock_get_chart.return_value = mock_df
    mock_calc.return_value = {'ema_20': 10000, 'ema_60': 10000, 'ema_120': 10000, 'psar': 9000, 'rsi': 50, 'adx': 20, 'cci': 0}
    mock_classify.return_value = ("매수", "[red]", "조건 충족")
    mock_score.return_value = (9.0, [])
    mock_vol.return_value = 150.0 # 체결강도 Mock
    
    stock = {'code': '005930', 'name': 'Samsung'}
    params = {'WEIGHTS': config.SCORING_WEIGHTS}
    
    result = analysis._analyze_stock_worker(stock, params)
    
    assert result is not None
    assert result['code'] == '005930'
    assert result['score'] == 9.0
    assert result['state'] == "매수"