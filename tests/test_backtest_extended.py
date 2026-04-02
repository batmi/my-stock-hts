import pytest
from unittest.mock import patch, MagicMock
from modules import backtest
from modules import chart # [추가] chart 모듈 임포트
import pandas as pd
import config

@pytest.fixture
def sample_backtest_df():
    dates = pd.date_range(start="2023-01-01", periods=100)
    df = pd.DataFrame({
        'date': dates.strftime("%Y%m%d"),
        'close': [10000] * 100,
        'open': [10000] * 100,
        'high': [10100] * 100,
        'low': [9900] * 100,
        'volume': [1000] * 100,
        'EMA20': [10000] * 100,
        'EMA60': [10000] * 100,
        'EMA120': [10000] * 100,
        'SAR': [9000] * 100,
        'RSI': [50] * 100,
        'ADX': [20] * 100,
        'CCI': [0] * 100,
        'OBV': [1000] * 100,
        'OBV_MA': [1000] * 100,
        'ATR': [100] * 100,
        'MACD': [0] * 100,
        'MACD_Signal': [0] * 100
    })
    return df

@patch('rich.prompt.Prompt.ask', return_value='n')
@patch('config.console.print')
def test_run_monte_carlo_simulation(mock_print, mock_ask, sample_backtest_df):
    """몬테카를로 시뮬레이션 실행 테스트"""
    backtest.run_monte_carlo_simulation(
        sample_backtest_df, 
        sample_backtest_df.iloc[0], 
        10000000, 
        8.0, 70, False, 
        -7.0, 30.0, 75, 5.0, 10.0, 3.0,
        5, True, 2.0, False, name="TestStock", code="005930", days=100
    )
    
    # 결과 출력 확인
    assert mock_print.call_count > 0
    # 히스토그램 생성 확인 (파일 저장)
    with patch('modules.chart.plt.savefig') as mock_savefig:
        chart.generate_monte_carlo_histogram([0.1, 0.2, -0.1], "Test", "005930", open_file=False) # [수정] chart 모듈 함수 호출
        assert mock_savefig.called

@patch('rich.prompt.Prompt.ask')
@patch('modules.backtest.get_backtest_data')
def test_run_backtest_menu(mock_get_data, mock_ask, sample_backtest_df):
    """백테스팅 메뉴 실행 테스트"""
    # 6(직접입력) -> 코드(005930) -> 설정변경(n) -> 모드(1:단일) -> AI진단(n) -> 메인화면(q)
    mock_ask.side_effect = ["6", "005930", "n", "1", "n", "q"]
    
    mock_get_data.return_value = sample_backtest_df
    
    with patch('modules.backtest.api.get_stock_name_by_code', return_value="삼성전자"), \
         patch('modules.backtest.utils.validate_and_confirm_stock', return_value=True):
         with patch('config.console.print'):
             backtest.run_backtest()
            
    mock_get_data.assert_called()