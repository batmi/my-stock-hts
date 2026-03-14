from modules.auto_trade import DefaultStrategy
import config
import pytest

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
        profit_rate=profit_rate, ts_msg="", thresholds=thresholds
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
        profit_rate=profit_rate, ts_msg="", thresholds=thresholds, already_half_sold=False
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
        profit_rate=profit_rate, ts_msg="", thresholds=thresholds
    )
    
    assert result['action'] == 'sell'
    assert "손절" in result['reason']

def test_trailing_stop(strategy):
    """트레일링 스탑 테스트: 외부에서 감지된 TS 메시지가 있을 경우 매도"""
    # 설정 무관 (ts_msg가 있으면 매도)
    ts_msg = "트레일링스탑 (최고가:12000원, 하락률:-3.5%)"
    
    result = strategy.analyze_sell(
        code="005930", name="삼성전자", df=None, 
        current_price=11500, buy_price=10000, 
        profit_rate=15.0, ts_msg=ts_msg
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
        profit_rate=profit_rate, ts_msg="", thresholds=thresholds
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
        profit_rate=profit_rate, ts_msg="", thresholds=thresholds
    )
    
    assert result['action'] == 'sell'
    assert "RSI 과열" in result['reason']

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
        profit_rate=profit_rate, ts_msg="", thresholds=thresholds
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
        profit_rate=profit_rate, ts_msg="", thresholds=thresholds
    )
    
    assert result['action'] == 'sell'
    assert "손절" in result['reason']
    
    # 비교군: 손실이 -4.0%라면 매도하지 않아야 함
    current_price_hold = 9600 # -4%
    profit_rate_hold = -4.0
    
    result_hold = strategy.analyze_sell(
        code="005930", name="삼성전자", df=None, 
        current_price=current_price_hold, buy_price=buy_price, 
        profit_rate=profit_rate_hold, ts_msg="", thresholds=thresholds
    )
    
    # df=None이므로 기술적 지표에 의한 매도는 발생하지 않음
    assert result_hold['action'] == 'hold'