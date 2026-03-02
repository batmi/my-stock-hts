import pytest
import pandas as pd
import numpy as np
from unittest.mock import patch, MagicMock
from modules import backtest
import config

@pytest.fixture
def sample_backtest_df():
    # 백테스팅 모듈이 기대하는 컬럼을 포함한 데이터프레임 생성
    dates = pd.date_range(start="2023-01-01", periods=50)
    df = pd.DataFrame({
        'date': dates.strftime("%Y%m%d"),
        'close': np.linspace(10000, 12000, 50),
        'open': np.linspace(10000, 12000, 50),
        'high': np.linspace(10100, 12100, 50),
        'low': np.linspace(9900, 11900, 50),
        'volume': np.random.randint(1000, 5000, 50),
        'EMA20': np.linspace(9000, 11000, 50),
        'EMA60': np.linspace(8000, 10000, 50),
        'EMA120': np.linspace(7000, 9000, 50),
        'SAR': np.linspace(9500, 11500, 50),
        'RSI': np.random.uniform(30, 70, 50),
        'ADX': np.random.uniform(10, 40, 50),
        'CCI': np.random.uniform(-100, 100, 50),
        'OBV': np.linspace(1000, 5000, 50),
        'OBV_MA': np.linspace(1000, 4800, 50),
        'ATR': np.random.uniform(100, 200, 50),
        'MACD': np.random.uniform(-10, 10, 50),
        'MACD_Signal': np.random.uniform(-10, 10, 50)
    })
    return df

def test_calculate_daily_status(sample_backtest_df):
    """일별 상태 계산 로직 테스트"""
    row = sample_backtest_df.iloc[-1]
    prev_row = sample_backtest_df.iloc[-2]
    
    # 기본 임계값 사용
    thresholds = {
        "BUY_SCORE": 8.0,
        "BUY_RSI_MAX": 70,
        "RISE_SCORE": 6.0,
        "WEIGHTS": config.SCORING_WEIGHTS
    }
    
    raw_score, sell_score, can_buy, state, reason = backtest.calculate_daily_status(row, prev_row, thresholds)
    
    assert isinstance(raw_score, float)
    assert isinstance(sell_score, float)
    assert isinstance(can_buy, bool)
    assert isinstance(state, str)

def test_simulate_strategy(sample_backtest_df):
    """전략 시뮬레이션 실행 테스트"""
    initial_capital = 10_000_000
    buy_score = 5.0 # 매수가 발생하도록 낮은 점수 설정
    buy_rsi = 80
    
    # Config Mocking
    with patch.dict(config.SELL_STRATEGY, {"STOP_LOSS_RATE": -5.0, "TAKE_PROFIT_RATE": 10.0}):
        res = backtest.simulate_strategy(
            sample_backtest_df, 
            prev_row_init=sample_backtest_df.iloc[0],
            initial_capital=initial_capital,
            buy_score_limit=buy_score,
            buy_rsi_limit=buy_rsi,
            is_overseas=False
        )
    
    assert "total_return" in res
    assert "trades" in res
    assert "final_asset" in res
    # 상승장 데이터이므로 수익이 나거나 최소한 자산이 유지되어야 함
    assert res['final_asset'] >= initial_capital * 0.9

@patch('modules.backtest.api.fetch_yfinance_data')
def test_get_backtest_data_yfinance(mock_fetch):
    """yfinance 데이터 조회 테스트"""
    mock_df = pd.DataFrame({
        'Date': pd.date_range(start="2023-01-01", periods=10),
        'Close': [100] * 10,
        'Open': [100] * 10,
        'High': [100] * 10,
        'Low': [100] * 10,
        'Volume': [1000] * 10
    })
    mock_fetch.return_value = mock_df
    
    df = backtest.get_backtest_data("AAPL", True, 10)
    assert not df.empty
    assert 'close' in df.columns
    assert 'date' in df.columns

