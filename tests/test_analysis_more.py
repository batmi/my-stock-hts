import pytest
from modules import analysis
import config

def test_classify_stock_state_danger():
    """위험 상태 분류 테스트"""
    # 이평선 완전 이탈
    state, color, reason = analysis.classify_stock_state(
        price=9000, ema20=10000, ema60=11000, ema120=12000, 
        sar=13000, rsi=30, prev_rsi=None, adx=20, cci=-100, obv_trend=False
    )
    assert state == "매도"
    assert "이평선 완전 이탈" in reason

def test_classify_stock_state_caution():
    """주의 상태 분류 테스트"""
    # RSI 과열
    state, color, reason = analysis.classify_stock_state(
        price=10000, ema20=9500, ema60=9000, ema120=8500, 
        sar=9000, rsi=85, prev_rsi=None, adx=20, cci=100, obv_trend=True
    )
    assert state == "주의"
    assert "RSI 과열" in reason

def test_calculate_score_details():
    """스코어링 상세 로직 테스트"""
    # 모든 조건 충족 (만점)
    score, details = analysis.calculate_score(
        price=10000, ema20=9000, ema60=8000, ema120=7000, 
        sar=9000, rsi=60, adx=30, cci=150, obv_trend=True, 
        macd=50, macd_signal=40,
        ema_5=9500, macd_hist=10, prev_macd_hist=5,
        prev_cci=0, vol_spike=True, plus_di=30, minus_di=10
    )
    assert score >= 8.5
    assert len(details) > 5

    # 일부 조건 미충족
    score, details = analysis.calculate_score(
        price=10000, ema20=11000, ema60=12000, ema120=13000, # 역배열
        sar=11000, rsi=40, adx=10, cci=-50, obv_trend=False,
        macd=-10, macd_signal=0
    )
    assert score < 3.0