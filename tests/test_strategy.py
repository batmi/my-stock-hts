from modules import analysis
import config
import pytest
from unittest.mock import patch

# 1. 강력 매수 시나리오 (Perfect Bull)
def test_score_perfect_bull_market():
    """
    모든 지표가 상승을 가리킬 때 점수가 8.0 이상 나오는지 테스트
    조건: 정배열, MACD 골든크로스, RSI 강세, ADX 상승
    """
    # 가상 입력 데이터
    price = 10000
    ema20 = 9500
    ema60 = 9000
    ema120 = 8500   # 정배열
    sar = 9000      # 주가 > SAR (상승)
    rsi = 60        # 강세 구간
    adx = 25        # 추세 강함
    cci = 150       # 과매수권 진입 시도
    obv_trend = True
    macd = 50
    macd_signal = 40 # MACD > Signal (골든크로스 상태)
    
    score, _ = analysis.calculate_score(
        price, ema20, ema60, ema120, sar, rsi, adx, cci, obv_trend, macd, macd_signal
    )
    
    # print(f"Calculated Score: {score}")
    assert score >= 8.0, f"완벽한 상승장 조건에서는 8.0점 이상이어야 합니다. (현재: {score})"

# 2. 하락장 시나리오 (Bear Market)
def test_score_bear_market():
    """
    하락장일 때 점수가 5.0 미만 나오는지 테스트
    조건: 역배열, MACD 데드크로스, RSI 약세
    """
    price = 8000
    ema20 = 8500
    ema60 = 9000
    ema120 = 9500   # 역배열
    sar = 8500      # 주가 < SAR (하락)
    rsi = 30        # 약세
    adx = 20
    cci = -150
    obv_trend = False
    macd = -50
    macd_signal = -40 # MACD < Signal (데드크로스 상태)
    
    score, _ = analysis.calculate_score(
        price, ema20, ema60, ema120, sar, rsi, adx, cci, obv_trend, macd, macd_signal
    )
    
    assert score < 5.0, f"하락장 조건에서는 5.0점 미만이어야 합니다. (현재: {score})"

# 3. 과열 필터링 테스트 (RSI Overbought)
def test_buy_filter_rsi_overbought():
    """
    점수가 높아도 RSI가 과열(예: 80)이면 매수 상태가 아니어야 함
    """
    # 점수는 높게 나오도록 설정
    price = 10000; ema20 = 9000; ema60 = 8000; ema120 = 7000
    sar = 9000; adx = 30; cci = 200; obv_trend = True; macd = 100; macd_signal = 50
    
    rsi = 80; prev_rsi = 75 # RSI 과열
    
    state, _, _ = analysis.classify_stock_state(
        price, ema20, ema60, ema120, sar, rsi, prev_rsi, adx, cci, obv_trend, macd, macd_signal
    )
    
    # 매수(Buy)가 아니라 주의(Caution)가 나와야 함 (RSI 과열)
    assert state != "매수", f"RSI가 80일 때는 매수 신호가 나오면 안 됩니다. (현재 상태: {state})"
    assert "과열" in _ or "주의" in state, "과열 상태가 감지되어야 합니다."

# 4. 횡보장 시나리오 (Sideways)
def test_score_sideways_market():
    """
    횡보장일 때 점수가 매수 기준(8.0) 미만인지 테스트
    조건: 이평선 혼조, ADX 낮음
    """
    price = 10000
    ema20 = 10100; ema60 = 9900; ema120 = 10050 # 혼조세
    sar = 9800
    rsi = 50        # 중립
    adx = 10        # 추세 없음 (횡보)
    cci = 0
    obv_trend = False
    macd = 5; macd_signal = 0
    
    score, _ = analysis.calculate_score(
        price, ema20, ema60, ema120, sar, rsi, adx, cci, obv_trend, macd, macd_signal
    )
    
    assert score < 8.0, f"횡보장에서는 매수 기준 점수(8.0) 미만이어야 합니다. (현재: {score})"

# 5. 시장 국면 판단 테스트 (Market Regime)
def test_market_regime_bull(sample_uptrend_df):
    """시장 국면 판단: 강세장(Bull) 테스트"""
    # api.get_domestic_index_chart가 상승장 데이터를 반환하도록 모킹
    with patch('modules.analysis.api.get_domestic_index_chart') as mock_get:
        mock_get.return_value = sample_uptrend_df
        
        # KOSPI 강세장 가정
        regime, score_adj = analysis.get_market_regime("KOSPI")
        
        # 생성된 가상 데이터의 ADX 강도에 따라 Bull 또는 Sideways가 나올 수 있음
        # 하지만 하락장(Bear)은 아니어야 함
        assert regime != "Bear"
        if regime == "Bull":
            assert score_adj < 0 # 매수 기준 완화

def test_market_regime_bear(sample_downtrend_df):
    """시장 국면 판단: 약세장(Bear) 테스트"""
    with patch('modules.analysis.api.get_domestic_index_chart') as mock_get:
        mock_get.return_value = sample_downtrend_df
        
        regime, score_adj = analysis.get_market_regime("KOSPI")
        
        assert regime == "Bear"
        assert score_adj > 0 # 매수 기준 강화

def test_market_filtering_logic(sample_downtrend_df):
    """시장 필터링 로직 검증 (이평선 이탈 여부)"""
    # AutoTrader에서 사용하는 필터링 로직 시뮬레이션
    # 조건: 지수 < 20일 이평선이면 is_healthy = False
    ma_period = 20
    
    df = sample_downtrend_df
    ma_val = df['close'].rolling(window=ma_period).mean().iloc[-1]
    current_price = df['close'].iloc[-1]
    
    # 하락장 데이터이므로 현재가가 이평선보다 낮아야 함
    is_healthy = current_price >= ma_val
    
    assert not is_healthy, "하락장에서는 시장 필터링(is_healthy=False)이 작동해야 합니다."