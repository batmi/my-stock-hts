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
        profit_rate=profit_rate, thresholds=thresholds,
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
        profit_rate=profit_rate, thresholds=thresholds,
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
        profit_rate=15.0, thresholds=thresholds,
        already_half_sold=False
    )
    
    assert result['action'] == 'hold'

def test_full_tp_after_half_tp(strategy):
    """4. 최종 익절 연계: 반익절 후에는 목표 도달만으로 전량매도하지 않고 'Let profit run'으로
    트레일링/수익보존에 위임한다. 목표가(+30%)를 돌파했다가 수익보존선(목표-3%=+27%) 아래로
    하락하면 남은 물량을 전량 매도한다."""
    config.SELL_STRATEGY["HALF_TAKE_PROFIT_USE"] = True
    thresholds = {"TAKE_PROFIT_RATE": 30.0, "STOP_LOSS_RATE": -7.0}

    # 고점에서 목표(+30%)를 돌파(13000) 후 현재 +26%로 하락 → 수익보존선(+27%) 이탈
    result = strategy.analyze_sell(
        code="005930", name="삼성전자", df=None,
        current_price=12600, buy_price=10000,
        profit_rate=26.0, thresholds=thresholds,
        already_half_sold=True, # 이미 절반을 판 상태
        highest_price=13000     # 목표가 돌파 후 하락
    )

    assert result['action'] == 'sell'
    assert result['sell_ratio'] == 1.0 # 남은 물량 전량 매도
    assert "수익보존" in result['reason']
    assert "반익절" not in result['reason']