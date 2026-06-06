import pytest
import sys
import os
import pandas as pd
from unittest.mock import patch

# 프로젝트 루트 경로를 시스템 패스에 추가하여 모듈 임포트가 가능하게 함
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from modules import analysis
from modules.auto_trade import DefaultStrategy
import config

def test_mr_state_classification():
    """1. 역추세 매수 상태 분류 테스트: 모든 조건(이격도, RSI 반등) 충족 시"""
    thresholds = {
        "USE_MEAN_REVERSION": True,
        "MR_RSI_MAX": 40.0,
        "MR_DISPARITY_MAX": 90.0
    }
    
    # 양봉 마감 여부 판별용 데이터프레임 전달 (종가 > 시가)
    df = pd.DataFrame({'close': [8500], 'open': [8400]})
    
    # 이격도 85% (8500 / 10000 = 85%), RSI 반등 (30 -> 35)
    state, _, _ = analysis.classify_stock_state(
        price=8500, ema20=10000, ema60=11000, ema120=12000,
        sar=9000, rsi=35.0, prev_rsi=30.0,
        adx=20, cci=-120, obv_trend=False, thresholds=thresholds, df=df
    )
    assert state == "역매수"

def test_mr_state_fail_rsi_rebound():
    """2. 역추세 매수 실패 테스트: RSI가 침체 구간이나 전일 대비 하락 중일 때"""
    thresholds = {
        "USE_MEAN_REVERSION": True,
        "MR_RSI_MAX": 40.0,
        "MR_DISPARITY_MAX": 90.0
    }
    
    # RSI 하락 중 (35 -> 30) - 바닥을 다지지 못했다고 판단
    state, _, _ = analysis.classify_stock_state(
        price=8500, ema20=10000, ema60=11000, ema120=12000,
        sar=9000, rsi=30.0, prev_rsi=35.0,
        adx=20, cci=-120, obv_trend=False, thresholds=thresholds
    )
    assert state != "역매수"

def test_mr_state_fail_disparity():
    """3. 역추세 매수 실패 테스트: 이격도 조건(충분한 낙폭) 미달 시"""
    thresholds = {
        "USE_MEAN_REVERSION": True,
        "MR_RSI_MAX": 40.0,
        "MR_DISPARITY_MAX": 90.0
    }
    
    # 이격도 95% (9500 / 10000 = 95%) > 기준(90%)
    state, _, _ = analysis.classify_stock_state(
        price=9500, ema20=10000, ema60=11000, ema120=12000,
        sar=9000, rsi=35.0, prev_rsi=30.0,
        adx=20, cci=-120, obv_trend=False, thresholds=thresholds
    )
    assert state != "역매수"

@patch('indicators.calculate_indicators')
@patch('modules.analysis.classify_stock_state')
@patch('modules.analysis.calculate_score')
def test_mr_grace_period_hold(mock_score, mock_classify, mock_ind):
    """4. 매도 방어(Grace Period) 테스트: 역추세 진입 후 5일 이내 점수 하락 시 방어 여부"""
    strategy = DefaultStrategy()
    df = pd.DataFrame({'close': [1000] * 20, 'high': [1050] * 20, 'low': [950] * 20}) # Dummy
    
    mock_ind.return_value = {'ema_20': 1000, 'ema_60': 1000, 'ema_120': 1000, 'psar': 1000, 'rsi': 50, 'adx': 20, 'cci': 0}
    mock_classify.return_value = ("관망", "", "점수하락")
    mock_score.return_value = (3.0, []) # 매도 기준(5.0) 미달
    
    thresholds = {"TAKE_PROFIT_RATE": 30.0, "STOP_LOSS_RATE": -10.0, "TAKE_PROFIT_RSI": 80, "SELL_SCORE": 5.0, "TIME_STOP_DAYS": 5}
    
    # 보유 3일차, 수익률 -2.0% -> 추세이탈 매도가 무시되고 HOLD 반환해야 함
    res = strategy.analyze_sell(
        code="000", name="Test", df=df, current_price=980, buy_price=1000,
        profit_rate=-2.0, thresholds=thresholds,
        holding_days=3, is_mr_holding=True
    )
    assert res['action'] == 'hold'

@patch('indicators.calculate_indicators')
@patch('modules.analysis.classify_stock_state')
@patch('modules.analysis.calculate_score')
def test_mr_grace_period_time_over(mock_score, mock_classify, mock_ind):
    """5. 매도 방어(Grace Period) 만료 테스트: 5일 초과 시 정상 손절(추세이탈) 여부"""
    strategy = DefaultStrategy()
    df = pd.DataFrame({'close': [1000] * 20, 'high': [1050] * 20, 'low': [950] * 20})
    
    mock_ind.return_value = {'ema_20': 1000, 'ema_60': 1000, 'ema_120': 1000, 'psar': 1000, 'rsi': 50, 'adx': 20, 'cci': 0}
    mock_classify.return_value = ("관망", "", "점수하락")
    mock_score.return_value = (3.0, [])
    
    thresholds = {"TAKE_PROFIT_RATE": 30.0, "STOP_LOSS_RATE": -10.0, "TAKE_PROFIT_RSI": 80, "SELL_SCORE": 5.0, "TIME_STOP_DAYS": 5}
    
    with patch.dict(config.SELL_STRATEGY, {"TIME_STOP_USE": False}):
        # 보유 6일차 (유예기간 만료) -> 점수 미달로 인해 SELL 반환해야 함
        res = strategy.analyze_sell(
            code="000", name="Test", df=df, current_price=980, buy_price=1000,
            profit_rate=-2.0, thresholds=thresholds,
            holding_days=6, is_mr_holding=True
        )
    assert res['action'] == 'sell'
    assert "추세이탈" in res['reason']