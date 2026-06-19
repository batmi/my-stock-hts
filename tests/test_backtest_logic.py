import pytest
import pandas as pd
from unittest.mock import patch, MagicMock
from modules import backtest
import config

@pytest.fixture
def sample_df():
    """백테스트용 가상 데이터프레임 생성"""
    data = {
        'date': ['20231001', '20231002', '20231003', '20231004', '20231005'],
        'close': [50000, 51000, 49000, 52000, 53000],
        'open': [49500, 50500, 50000, 49500, 52500],
        'high': [50500, 51500, 50500, 52500, 53500],
        'low': [49000, 50000, 48500, 49000, 52000],
        'EMA20': [49000, 49500, 49600, 49800, 50000],
        'EMA60': [48000, 48100, 48200, 48300, 48400],
        'EMA120': [47000, 47100, 47200, 47300, 47400],
        'SAR': [48000, 48500, 49000, 48000, 48500],
        'RSI': [55.0, 65.0, 45.0, 75.0, 85.0],
        'ADX': [25.0, 26.0, 27.0, 30.0, 35.0],
        'CCI': [100, 150, -50, 200, 250],
        'OBV': [1000, 2000, 1500, 3000, 4000],
        'OBV_MA': [1000, 1200, 1300, 1500, 1800],
        'ATR': [1000, 1000, 1000, 1000, 1000],
    }
    return pd.DataFrame(data)

@patch('modules.analysis.classify_stock_state', return_value=("상승", [], "테스트사유"))
@patch('modules.analysis.calculate_score', return_value=(8.5, {}))
def test_calculate_daily_status(mock_calc, mock_class, sample_df):
    """일별 상태 계산이 analysis 모듈을 잘 호출하는지 검증"""
    row = sample_df.iloc[1]
    prev_row = sample_df.iloc[0]
    
    thresholds = {"WEIGHTS": {}}
    raw_score, sell_check_score, can_buy_state, state, reason = backtest.calculate_daily_status(row, prev_row, thresholds)
    
    assert raw_score == 8.5
    assert sell_check_score == 8.5
    assert can_buy_state is True
    assert state == "상승"
    mock_class.assert_called_once()
    mock_calc.assert_called_once()


@patch('modules.backtest.calculate_daily_status')
def test_simulate_strategy(mock_calc_status, sample_df):
    """시뮬레이션 로직 검증"""
    # 5개의 행에 대해 모두 매수 조건을 만족한다고 가정
    # 첫번째 날 매수, 마지막 날 매도하도록 상태 모킹
    # raw_score, sell_check_score, can_buy_state, state, reason
    mock_calc_status.side_effect = [
        (9.0, 9.0, True, "매수", "강세"), # 1일차
        (9.0, 9.0, True, "매수", "강세"), # 2일차
        (9.0, 9.0, True, "매수", "강세"), # 3일차
        (9.0, 9.0, True, "매수", "강세"), # 4일차
        (0.0, 0.0, False, "매도", "추세이탈") # 5일차: 매도 신호
    ]
    
    # 설정 초기화 방어
    config.SELL_STRATEGY["STOP_LOSS_RATE"] = -10.0
    config.SELL_STRATEGY["TAKE_PROFIT_RATE"] = 10.0
    config.SELL_STRATEGY["TAKE_PROFIT_RSI"] = 80.0
    config.SELL_STRATEGY["SELL_SCORE"] = 4.0
    
    res = backtest.simulate_strategy(
        sim_df=sample_df,
        prev_row_init=None,
        initial_capital=1000000,
        buy_score_limit=8.0,
        buy_rsi_limit=70.0,
        is_overseas=False
    )
    
    assert res is not None
    assert "trades" in res
    assert len(res["trades"]) > 0
    # 최소 매수 1회, 매도 1회가 있는지 검증
    types = [t['type'] for t in res["trades"]]
    assert any(t.startswith("매수") for t in types)
    assert any(t.startswith("매도") for t in types)

@patch('api.get_investor_trend', return_value=[
    {'stck_bsop_date': '20231001', 'frgn_ntby_qty': '100', 'orgn_ntby_qty': '100'},
    {'stck_bsop_date': '20231002', 'frgn_ntby_qty': '200', 'orgn_ntby_qty': '200'},
])
def test_append_smart_money_signal(mock_investor, sample_df):
    """수급 데이터 병합이 정상적으로 동작하는지 검증"""
    # 국내 주식 테스트
    df_res = backtest._append_smart_money_signal(sample_df.copy(), "005930", is_overseas=False)
    assert "smart_money" in df_res.columns
    # 해외 주식 테스트 (수급 무시)
    df_res2 = backtest._append_smart_money_signal(sample_df.copy(), "AAPL", is_overseas=True)
    assert bool(df_res2["smart_money"].iloc[0]) is False
