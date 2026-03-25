import pytest
import pandas as pd
from unittest.mock import patch
import config
from modules import analysis
from modules.auto_trade import DefaultStrategy

@pytest.fixture
def mock_thresholds():
    """테스트용 기본 임계값 설정 픽스처"""
    return {
        "BUY_SCORE": 7.5,
        "RISE_SCORE": 6.0,
        "BUY_RSI_MAX": 65.0,
        "SUPER_MOMENTUM_USE": True,
        "SUPER_MOMENTUM_SCORE": 8.5,
        "SUPER_MOMENTUM_W52_POS": 90.0,
        "SUPER_BUY_RSI_MAX": 75.0,
        "USE_MEAN_REVERSION": False
    }

@patch('modules.analysis.calculate_score')
def test_classify_stock_state_super_momentum_buy(mock_calc, mock_thresholds):
    """[매수 로직] 슈퍼 모멘텀 조건에 따른 RSI 완화 적용 테스트"""
    
    # 공통 입력값 (추세가 살아있는 완벽한 정배열 상태)
    base_args = {
        "price": 10000, "ema20": 9000, "ema60": 8000, "ema120": 7000, "sar": 8000,
        "prev_rsi": 55.0, "adx": 30, "cci": 50, "obv_trend": True, "thresholds": mock_thresholds
    }

    # 케이스 1: 점수 9.0, 고점 95% -> 슈퍼 모멘텀 발동. RSI 72.0 통과 (75 미만)
    mock_calc.return_value = (9.0, [])
    state, color, reason = analysis.classify_stock_state(**base_args, rsi=72.0, w52_pos=95.0)
    assert state == "강매수"
    assert "슈퍼 모멘텀 적용" in reason

    # 케이스 2: 점수 9.0, 고점 95% -> 슈퍼 모멘텀 발동. RSI 78.0 탈락 (75 초과)
    state, color, reason = analysis.classify_stock_state(**base_args, rsi=78.0, w52_pos=95.0)
    assert state == "상승" # 매수 탈락 후 상승으로 강등

    # 케이스 3: 일반 매수. 점수 9.0, 고점 95%. RSI 60.0 통과
    state, color, reason = analysis.classify_stock_state(**base_args, rsi=60.0, w52_pos=95.0)
    assert state == "강매수"
    assert "슈퍼 모멘텀 적용" in reason

    # 케이스 4: 점수 미달로 발동 불가. 점수 8.0, 고점 95%. RSI 72.0 탈락 (기본 65 초과)
    mock_calc.return_value = (8.0, [])
    state, color, reason = analysis.classify_stock_state(**base_args, rsi=72.0, w52_pos=95.0)
    assert state == "상승"
    assert "슈퍼 모멘텀 적용" not in reason

    # 케이스 5: 고점 미달로 발동 불가. 점수 9.0, 고점 80%. RSI 72.0 탈락 (기본 65 초과)
    mock_calc.return_value = (9.0, [])
    state, color, reason = analysis.classify_stock_state(**base_args, rsi=72.0, w52_pos=80.0)
    assert state == "상승"

@patch('modules.auto_trade.analysis.calculate_score')
@patch('modules.auto_trade.analysis.classify_stock_state')
@patch('modules.auto_trade.indicators.calculate_indicators')
def test_analyze_sell_super_momentum(mock_calc_ind, mock_classify, mock_calc_score, mock_thresholds):
    """[매도 로직] 슈퍼 모멘텀 유지 시 동적 과열 RSI 한도(85) 검증"""
    
    strategy = DefaultStrategy()
    
    # W52_POS 계산을 위한 더미 DataFrame
    # 52주 최저가 5000원, 최고가 10000원 가정
    df = pd.DataFrame({'high': [10000]*250, 'low': [5000]*250, 'close': [9000]*250})
    
    # 상태 및 계산값 Mocking
    mock_classify.return_value = ("강매수", "[bold magenta]", "매수 조건 충족 (슈퍼 모멘텀 적용)")
    base_ind = {'adx': 30, 'cci': 100, 'ema_20': 9000, 'ema_60': 8000, 'ema_120': 7000, 'psar': 8000}

    with patch.dict('config.SELL_STRATEGY', {
        "TAKE_PROFIT_RATE": 20.0,
        "STOP_LOSS_RATE": -7.0,
        "TAKE_PROFIT_RSI": 75.0,        # 일반 익절 RSI
        "SUPER_TAKE_PROFIT_RSI": 85.0,  # 슈퍼 모멘텀 익절 RSI
        "SELL_SCORE": 5.0,
        "TIME_STOP_USE": False
    }):
        
        # 케이스 1: 퀀트 점수 9.0, 현재가 9500원(w52 90%), RSI 80.0
        # 결과: 슈퍼 모멘텀 한도 85.0 이내이므로 '매도 보류(hold)' 되어야 함
        mock_calc_score.return_value = (9.0, [])
        mock_calc_ind.return_value = {**base_ind, 'rsi': 80.0}
        res1 = strategy.analyze_sell("005930", "Test", df, current_price=9500, buy_price=9000, profit_rate=5.5, thresholds=mock_thresholds)
        assert res1['action'] == 'hold'
        assert res1['reason'] == ''

        # 케이스 2: 퀀트 점수 9.0, 현재가 9500원(w52 90%), RSI 88.0
        # 결과: 슈퍼 모멘텀 한도 85.0을 초과하므로 '매도(sell)' 되어야 함
        mock_calc_ind.return_value = {**base_ind, 'rsi': 88.0}
        res2 = strategy.analyze_sell("005930", "Test", df, current_price=9500, buy_price=9000, profit_rate=5.5, thresholds=mock_thresholds)
        assert res2['action'] == 'sell'
        assert "슈퍼모멘텀" in res2['reason']
        assert "85.0" in res2['reason']

        # 케이스 3: [동적 회귀] 주가 하락/지표 훼손으로 점수가 8.0으로 하락, 현재가 9500원, RSI 80.0
        # 결과: 점수 미달로 슈퍼 모멘텀 해제. 기본 RSI 한도 75.0 초과이므로 즉시 '매도(sell)'
        mock_calc_score.return_value = (8.0, [])
        mock_calc_ind.return_value = {**base_ind, 'rsi': 80.0}
        res3 = strategy.analyze_sell("005930", "Test", df, current_price=9500, buy_price=9000, profit_rate=5.5, thresholds=mock_thresholds)
        assert res3['action'] == 'sell'
        assert "기준:75.0" in res3['reason']
        assert "슈퍼모멘텀" not in res3['reason']

        # 케이스 4: [동적 회귀] 점수 9.0 유지 중이나 현재가가 8500원으로 하락해 고점 근접률 70%(90% 미달), RSI 80.0
        # 결과: 고점 근접 조건 미달로 슈퍼 모멘텀 해제. 기본 RSI 한도 초과로 '매도(sell)'
        mock_calc_score.return_value = (9.0, [])
        res4 = strategy.analyze_sell("005930", "Test", df, current_price=8500, buy_price=9000, profit_rate=-5.5, thresholds=mock_thresholds)
        assert res4['action'] == 'sell'
        assert "기준:75.0" in res4['reason']