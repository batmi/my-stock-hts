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

def test_half_tp_trigger(strategy):
    """1. 반익절 발동: 수익률이 목표 익절의 절반 도달 시 50% 매도 신호 발생"""
    config.SELL_STRATEGY["HALF_TAKE_PROFIT_USE"] = True
    thresholds = {"TAKE_PROFIT_RATE": 30.0, "STOP_LOSS_RATE": -7.0}
    
    buy_price = 10000
    current_price = 11500 # +15% 상승 (목표치 30%의 절반)
    profit_rate = 15.0
    
    result = strategy.analyze_sell(
        code="005930", name="삼성전자", df=None,
        current_price=current_price, buy_price=buy_price,
        profit_rate=profit_rate, ts_msg="", thresholds=thresholds,
        already_half_sold=False # 아직 반익절 하지 않은 상태
    )
    
    assert result['action'] == 'sell'
    assert result['sell_ratio'] == 0.5
    assert "반익절" in result['reason']

def test_half_tp_already_sold(strategy):
    """2. 중복 방지: 이미 반익절이 나간 종목은 추가로 반익절(50%) 되지 않고 관망(Hold)"""
    config.SELL_STRATEGY["HALF_TAKE_PROFIT_USE"] = True
    thresholds = {"TAKE_PROFIT_RATE": 30.0, "STOP_LOSS_RATE": -7.0}
    
    buy_price = 10000
    current_price = 11600 # +16% 상승 (15% 초과)
    profit_rate = 16.0
    
    result = strategy.analyze_sell(
        code="005930", name="삼성전자", df=None,
        current_price=current_price, buy_price=buy_price,
        profit_rate=profit_rate, ts_msg="", thresholds=thresholds,
        already_half_sold=True # DB/메모리에 이미 반익절 한 것으로 기록된 상태
    )
    
    assert result['action'] == 'hold'

def test_half_tp_disabled(strategy):
    """3. 설정 OFF: 반익절 기능이 꺼져있을 때는 목표치 절반에 도달해도 매도하지 않음"""
    config.SELL_STRATEGY["HALF_TAKE_PROFIT_USE"] = False
    thresholds = {"TAKE_PROFIT_RATE": 30.0, "STOP_LOSS_RATE": -7.0}
    
    result = strategy.analyze_sell(
        code="005930", name="삼성전자", df=None,
        current_price=11500, buy_price=10000,
        profit_rate=15.0, ts_msg="", thresholds=thresholds,
        already_half_sold=False
    )
    
    assert result['action'] == 'hold'

def test_full_tp_after_half_tp(strategy):
    """4. 최종 익절 연계: 반익절을 완료한 후 최종 목표 수익률에 도달하면 남은 물량 전량 매도"""
    config.SELL_STRATEGY["HALF_TAKE_PROFIT_USE"] = True
    thresholds = {"TAKE_PROFIT_RATE": 30.0, "STOP_LOSS_RATE": -7.0}
    
    result = strategy.analyze_sell(
        code="005930", name="삼성전자", df=None,
        current_price=13000, buy_price=10000,
        profit_rate=30.0, ts_msg="", thresholds=thresholds,
        already_half_sold=True # 이미 절반을 판 상태
    )
    
    assert result['action'] == 'sell'
    assert result['sell_ratio'] == 1.0 # 남은 물량 전량 매도
    assert "익절" in result['reason']
    assert "반익절" not in result['reason']