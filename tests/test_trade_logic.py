from modules.auto_trade import DefaultStrategy
import config
import pytest
from unittest.mock import patch

@pytest.fixture
def strategy():
    return DefaultStrategy()

def test_take_profit(strategy):
    """익절 테스트: 수익률이 목표치(+30%) 도달 시 매도 신호 발생"""
    # 설정: 익절 30%
    thresholds = {"TAKE_PROFIT_RATE": 30.0, "STOP_LOSS_RATE": -7.0}
    config.SELL_STRATEGY["HALF_TAKE_PROFIT_USE"] = False
    
    buy_price = 10000
    current_price = 13500 # +35% 상승
    profit_rate = 35.0
    
    result = strategy.analyze_sell(
        code="005930", name="삼성전자", df=None, 
        current_price=current_price, buy_price=buy_price, 
        profit_rate=profit_rate, thresholds=thresholds, highest_price=10000
    )
    
    assert result['action'] == 'sell'
    assert "익절" in result['reason']

def test_half_take_profit(strategy):
    """반익절 테스트: 목표 익절의 절반 도달 시 50% 매도 신호 발생"""
    thresholds = {"TAKE_PROFIT_RATE": 30.0, "STOP_LOSS_RATE": -7.0}
    config.SELL_STRATEGY["HALF_TAKE_PROFIT_USE"] = True
    
    buy_price = 10000
    current_price = 11600 # +16% 상승 (15% 초과)
    profit_rate = 16.0
    
    result = strategy.analyze_sell(
        code="005930", name="삼성전자", df=None, 
        current_price=current_price, buy_price=buy_price, 
        profit_rate=profit_rate, thresholds=thresholds, already_half_sold=False, highest_price=11600
    )
    assert result['action'] == 'sell'
    assert result['sell_ratio'] == 0.5
    assert "반익절" in result['reason']

def test_stop_loss(strategy):
    """손절 테스트: 손실률이 한계치(-7%) 도달 시 매도 신호 발생"""
    # 설정: 손절 -7%
    thresholds = {"TAKE_PROFIT_RATE": 30.0, "STOP_LOSS_RATE": -7.0}
    
    buy_price = 10000
    current_price = 9200 # -8% 하락
    profit_rate = -8.0
    
    result = strategy.analyze_sell(
        code="005930", name="삼성전자", df=None, 
        current_price=current_price, buy_price=buy_price, 
        profit_rate=profit_rate, thresholds=thresholds, highest_price=10000
    )
    
    assert result['action'] == 'sell'
    assert "손절" in result['reason']

def test_trailing_stop(strategy):
    """트레일링 스탑 테스트: 조건 달성 시 매도"""
    # 반익절 로직이 먼저 트리거되는 것을 방지하기 위해 끄기
    config.SELL_STRATEGY["HALF_TAKE_PROFIT_USE"] = False
    config.SELL_STRATEGY["USE_ATR_STOP"] = False # 고정 비율 사용 테스트
    
    result = strategy.analyze_sell(
        code="005930", name="삼성전자", df=None, 
        current_price=11500, buy_price=10000, 
        profit_rate=15.0, highest_price=12500 # 25% 상승 후 11500(-8%) 하락
    )
    
    assert result['action'] == 'sell'
    assert "트레일링스탑" in result['reason']

def test_hold_condition(strategy):
    """보유 테스트: 익절/손절 조건 미달 시 보유(Hold) 유지"""
    thresholds = {"TAKE_PROFIT_RATE": 30.0, "STOP_LOSS_RATE": -7.0, "SELL_SCORE": 5.0}
    
    buy_price = 10000
    current_price = 10500 # +5% (아직 익절 아님)
    profit_rate = 5.0
    
    # 점수도 양호하다고 가정 (Mocking 필요하지만 df=None이면 점수 계산 스킵됨)
    # 여기서는 df=None으로 두어 기술적 지표에 의한 매도가 발생하지 않도록 함
    
    result = strategy.analyze_sell(
        code="005930", name="삼성전자", df=None, 
        current_price=current_price, buy_price=buy_price, 
        profit_rate=profit_rate, thresholds=thresholds, highest_price=10500
    )
    
    assert result['action'] == 'hold'

def test_rsi_overbought_sell(strategy, sample_uptrend_df):
    """RSI 과열 시 매도 신호 발생 테스트"""
    # 상승장 데이터이므로 RSI가 높을 것으로 예상됨
    # 테스트를 위해 과열 기준을 낮게 설정 (예: 30)
    thresholds = {"TAKE_PROFIT_RSI": 30, "TAKE_PROFIT_RATE": 100.0} # 수익률 익절은 배제
    
    buy_price = 10000
    current_price = 10000 # 수익률 0%
    profit_rate = 0.0
    
    result = strategy.analyze_sell(
        code="005930", name="삼성전자", df=sample_uptrend_df, 
        current_price=current_price, buy_price=buy_price, 
        profit_rate=profit_rate, thresholds=thresholds, highest_price=10000
    )
    
    assert result['action'] == 'sell'
    assert "RSI과열" in result['reason']

def test_trend_broken_sell(strategy, sample_downtrend_df):
    """추세 이탈(점수 하락) 시 매도 신호 발생 테스트"""
    # 하락장 데이터 사용 -> 점수가 낮게 나옴
    # 매도 기준 점수를 높게 설정하여 확실하게 매도 유도
    thresholds = {"SELL_SCORE": 9.0, "STOP_LOSS_RATE": -20.0} 
    
    buy_price = 10000
    current_price = 9500 # -5% (손절 -20%에는 도달 안 함)
    profit_rate = -5.0
    
    result = strategy.analyze_sell(
        code="005930", name="삼성전자", df=sample_downtrend_df, 
        current_price=current_price, buy_price=buy_price, 
        profit_rate=profit_rate, thresholds=thresholds, highest_price=10000
    )
    
    assert result['action'] == 'sell'
    # "추세이탈" 또는 "매도진입" 또는 "점수하락" 등의 키워드가 포함되어야 함
    assert any(x in result['reason'] for x in ["추세", "매도", "점수"])

def test_atr_stop_loss_logic(strategy):
    """ATR 기반 동적 손절률 적용 테스트"""
    # ATR 계산에 의해 -4.5%가 손절 라인으로 설정되었다고 가정 (thresholds로 전달됨)
    dynamic_sl_rate = -4.5
    thresholds = {"STOP_LOSS_RATE": dynamic_sl_rate}
    
    buy_price = 10000
    current_price = 9500 # -5% 하락
    profit_rate = -5.0
    
    # -5% 손실은 -4.5% 손절 라인을 건드렸으므로 매도해야 함
    result = strategy.analyze_sell(
        code="005930", name="삼성전자", df=None, 
        current_price=current_price, buy_price=buy_price, 
        profit_rate=profit_rate, thresholds=thresholds, highest_price=10200
    )
    
    assert result['action'] == 'sell'
    assert "손절" in result['reason']
    
    # 비교군: 손실이 -4.0%라면 매도하지 않아야 함
    current_price_hold = 9600 # -4%
    profit_rate_hold = -4.0
    
    result_hold = strategy.analyze_sell(
        code="005930", name="삼성전자", df=None, 
        current_price=current_price_hold, buy_price=buy_price, 
        profit_rate=profit_rate_hold, thresholds=thresholds, highest_price=10000
    )
    
    # df=None이므로 기술적 지표에 의한 매도는 발생하지 않음
    assert result_hold['action'] == 'hold'

def test_break_even_stop(strategy):
    """본전 청산(Break-Even Stop) 테스트: 최고 수익률 달성 후 가격 하락 시 본전(+0.5%) 청산 방어"""
    thresholds = {
        "BREAK_EVEN_PROFIT_RATE": 7.0,
        "BREAK_EVEN_STOP_RATE": 0.5,
        "STOP_LOSS_RATE": -7.0,
        "USE_ATR_STOP": False
    }
    
    buy_price = 10000
    highest_price = 10800 # +8.0% (본전청산 발동 조건 7.0% 만족)
    current_price = 10040 # +0.4% (본전 청산선 0.5% 하향 이탈)
    profit_rate = 0.4
    
    result = strategy.analyze_sell(
        code="005930", name="삼성전자", df=None, 
        current_price=current_price, buy_price=buy_price, 
        profit_rate=profit_rate, thresholds=thresholds, highest_price=highest_price
    )
    
    assert result['action'] == 'sell'
    assert "본전청산" in result['reason']

@patch('modules.auto_trade.indicators.calculate_indicators')
@patch('modules.auto_trade.analysis.classify_stock_state')
@patch('modules.auto_trade.analysis.calculate_score')
def test_dynamic_atr_trailing_stop(mock_calc_score, mock_classify, mock_calc_ind, strategy):
    """ATR 기반 동적 트레일링 스탑 테스트 (휩쏘 방어 검증)"""
    import pandas as pd
    df = pd.DataFrame({'close': [11000], 'high': [11000], 'low': [11000]})
    
    # 종목의 현재 ATR이 300원이라고 가정
    mock_calc_ind.return_value = {
        'atr': 300, 'rsi': 50, 'adx': 20, 'cci': 0, 'psar': 9000,
        'ema_20': 10000, 'ema_60': 9000, 'ema_120': 8000,
        'obv_trend': True, 'macd': 1, 'macd_signal': 0,
        'plus_di': 25, 'minus_di': 15, 'ema_5': 10500,
        'prev_cci': 0, 'vol_spike': False, 'macd_hist': 1, 'prev_macd_hist': 0
    }
    mock_classify.return_value = ("상승", "", "")
    mock_calc_score.return_value = (7.0, [])
    
    thresholds = {
        "ts_activation": 10.0,
        "ts_callback": 2.0, # 고정 비율로는 고점 대비 2% 하락 시 매도
        "USE_ATR_STOP": True,
        "ATR_STOP_MULTIPLIER": 2.0
    }
    buy_price = 10000
    highest_price = 11500 # +15% 수익 도달 (트레일링 발동)
    
    # 시나리오 1: 11,500원에서 11,200원으로 하락 (-2.6% 하락)
    # 고정 비율 2.0%라면 매도되어야 하지만, 변동성(ATR) 동적 허용치(300*2=600원, 약 5.21%) 덕분에 홀딩해야 함
    result_hold = strategy.analyze_sell(
        code="005930", name="삼성전자", df=df, 
        current_price=11200, buy_price=buy_price, 
        profit_rate=12.0, thresholds=thresholds, highest_price=highest_price
    )
    assert result_hold['action'] == 'hold'
    
    # 시나리오 2: 11,500원에서 10,800원으로 하락 (-6.08% 하락)
    # 동적 ATR 허용치 5.21%마저 초과 이탈하였으므로 확실하게 트레일링 스탑(매도)을 실행해야 함
    result_sell = strategy.analyze_sell(
        code="005930", name="삼성전자", df=df, 
        current_price=10800, buy_price=buy_price, 
        profit_rate=8.0, thresholds=thresholds, highest_price=highest_price
    )
    assert result_sell['action'] == 'sell'
    assert "트레일링스탑" in result_sell['reason']

def test_defensive_half_sell(strategy):
    """방어적 반매도 (하락 반전 신호) 테스트"""
    import pandas as pd
    df = pd.DataFrame({'close': [10000], 'high': [10500], 'low': [9900]})
    
    # 현재가(10000)가 5일선(10500) 및 SAR(10100)보다 낮음 (하락 반전)
    mock_ind = {
        'psar': 10100, 'ema_5': 10500, 'rsi': 50, 'adx': 20, 'cci': 0,
        'ema_20': 9500, 'ema_60': 9000, 'ema_120': 8500,
        'macd': 1, 'macd_signal': 0, 'obv_trend': True,
        'plus_di': 25, 'minus_di': 15, 'prev_cci': 0, 'vol_spike': False, 'macd_hist': 1, 'prev_macd_hist': 0
    }
    thresholds = {"TAKE_PROFIT_RATE": 30.0, "STOP_LOSS_RATE": -10.0}
    
    with patch('modules.auto_trade.indicators.calculate_indicators', return_value=mock_ind), \
         patch('modules.auto_trade.analysis.classify_stock_state', return_value=("관망", "", "")), \
         patch('modules.auto_trade.analysis.calculate_score', return_value=(6.0, [])):
        
        # 방어적 반매도는 최소 수익권(기본 3.0%) 이상일 때 발동해야 하므로 profit_rate를 4.1%로 설정
        res = strategy.analyze_sell("005930", "Test", df, current_price=10000, buy_price=9600, profit_rate=4.1, thresholds=thresholds)
        
        assert res['action'] == 'sell'
        assert res['sell_ratio'] == 0.5
        assert "방어적 반매도" in res['reason']