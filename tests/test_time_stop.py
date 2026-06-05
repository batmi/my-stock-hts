import pytest
import sys
import os

# 프로젝트 루트 경로를 시스템 패스에 추가하여 모듈 임포트가 가능하게 함
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from modules.auto_trade import DefaultStrategy
import config

@pytest.fixture
def strategy():
    """테스트용 전략 인스턴스 제공 픽스처"""
    return DefaultStrategy()

def test_time_stop_trigger(strategy):
    """1. 시간 청산 발동: 설정된 보유 기간을 초과하고 최소 기대 수익률에 미달한 경우"""
    config.SELL_STRATEGY["TIME_STOP_USE"] = True
    config.SELL_STRATEGY["TIME_STOP_DAYS"] = 10
    config.SELL_STRATEGY["TIME_STOP_MIN_PROFIT_RATE"] = 3.0
    thresholds = {"TAKE_PROFIT_RATE": 30.0, "STOP_LOSS_RATE": -7.0}
    
    buy_price = 10000
    current_price = 10100 # +1.0% 상승 (기대수익 3.0% 미달)
    profit_rate = 1.0
    
    result = strategy.analyze_sell(
        code="005930", name="삼성전자", df=None,
        current_price=current_price, buy_price=buy_price,
        profit_rate=profit_rate, thresholds=thresholds,
        already_half_sold=False, holding_days=11 # 10일 초과
    )
    
    assert result['action'] == 'sell'
    assert "시간청산" in result['reason']

def test_time_stop_profit_exceeded(strategy):
    """2. 기대 수익 달성: 보유 기간을 초과했지만 최소 기대 수익률을 달성한 경우 (보유 유지)"""
    config.SELL_STRATEGY["TIME_STOP_USE"] = True
    config.SELL_STRATEGY["TIME_STOP_DAYS"] = 10
    config.SELL_STRATEGY["TIME_STOP_MIN_PROFIT_RATE"] = 3.0
    thresholds = {"TAKE_PROFIT_RATE": 30.0, "STOP_LOSS_RATE": -7.0, "SELL_SCORE": 5.0}
    
    buy_price = 10000
    current_price = 10500 # +5.0% 상승 (기대수익 3.0% 초과)
    profit_rate = 5.0
    
    result = strategy.analyze_sell(
        code="005930", name="삼성전자", df=None,
        current_price=current_price, buy_price=buy_price,
        profit_rate=profit_rate, thresholds=thresholds,
        already_half_sold=False, holding_days=11 # 10일 초과
    )
    
    assert result['action'] == 'hold'

def test_time_stop_time_not_reached(strategy):
    """3. 기간 미달: 아직 설정된 보유 기간에 도달하지 않은 경우"""
    config.SELL_STRATEGY["TIME_STOP_USE"] = True
    config.SELL_STRATEGY["TIME_STOP_DAYS"] = 10
    config.SELL_STRATEGY["TIME_STOP_MIN_PROFIT_RATE"] = 3.0
    thresholds = {"TAKE_PROFIT_RATE": 30.0, "STOP_LOSS_RATE": -7.0, "SELL_SCORE": 5.0}
    
    result = strategy.analyze_sell(
        code="005930", name="삼성전자", df=None,
        current_price=10100, buy_price=10000,
        profit_rate=1.0, thresholds=thresholds,
        already_half_sold=False, holding_days=7 # 10일 미만
    )
    
    assert result['action'] == 'hold'

def test_time_stop_disabled(strategy):
    """4. 기능 OFF: 시간 청산 기능이 비활성화된 경우"""
    config.SELL_STRATEGY["TIME_STOP_USE"] = False
    config.SELL_STRATEGY["TIME_STOP_DAYS"] = 10
    config.SELL_STRATEGY["TIME_STOP_MIN_PROFIT_RATE"] = 3.0
    thresholds = {"TAKE_PROFIT_RATE": 30.0, "STOP_LOSS_RATE": -7.0, "SELL_SCORE": 5.0}
    
    result = strategy.analyze_sell(
        code="005930", name="삼성전자", df=None,
        current_price=10100, buy_price=10000,
        profit_rate=1.0, thresholds=thresholds,
        already_half_sold=False, holding_days=11 # 10일 초과
    )
    
    assert result['action'] == 'hold'