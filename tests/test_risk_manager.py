import pytest
import math
from modules.auto_trade import RiskManager
import config

# Mock Trader 클래스 (RiskManager 초기화용)
class MockTrader:
    def __init__(self, initial_asset=10_000_000):
        self.initial_asset = initial_asset
    def log(self, msg):
        pass # 테스트 중 로그 출력 생략

@pytest.fixture
def risk_manager():
    trader = MockTrader()
    return RiskManager(trader)

def test_volatility_targeting_high_vol(risk_manager):
    """변동성이 높을 때 투자 비중 축소 테스트"""
    # 설정: 목표 변동성 20%, 최대 2배, 최소 0.3배
    config.USE_VOLATILITY_TARGETING = True
    config.TARGET_VOLATILITY = 0.20
    config.VOLATILITY_SCALING_MAX = 2.0
    config.VOLATILITY_SCALING_MIN = 0.3
    
    avail_cash = 10_000_000
    invest_ratio = 0.1 # 기본 투자금 100만원 (자산의 10%)
    
    # 상황: ATR이 높음 -> 연환산 변동성 약 40% 가정
    # Annual Vol = (ATR / Price) * sqrt(252)
    # 0.4 ~= (ATR / 10000) * 15.87
    # ATR ~= 252
    current_price = 10000
    atr = 252 
    
    # 예상 Scale = 목표(0.2) / 현재(0.4) = 0.5
    # 투자금 = 1,000,000 * 0.5 = 500,000원
    
    invest_amt = risk_manager.allocate_budget(
        avail_cash, invest_ratio, stop_loss_rate=None, atr=atr, current_price=current_price
    )
    
    # 계산 오차 고려하여 범위 확인
    assert 490_000 <= invest_amt <= 510_000

def test_volatility_targeting_low_vol(risk_manager):
    """변동성이 낮을 때 투자 비중 확대 테스트"""
    config.USE_VOLATILITY_TARGETING = True
    config.TARGET_VOLATILITY = 0.20
    config.VOLATILITY_SCALING_MAX = 2.0
    
    avail_cash = 10_000_000
    invest_ratio = 0.1 # 기본 투자금 100만원
    
    # 상황: ATR이 낮음 -> 연환산 변동성 약 10% 가정
    # 0.1 ~= (ATR / 10000) * 15.87
    # ATR ~= 63
    current_price = 10000
    atr = 63
    
    # 예상 Scale = 목표(0.2) / 현재(0.1) = 2.0
    # 투자금 = 1,000,000 * 2.0 = 2,000,000원
    
    invest_amt = risk_manager.allocate_budget(
        avail_cash, invest_ratio, stop_loss_rate=None, atr=atr, current_price=current_price
    )
    
    assert 1_900_000 <= invest_amt <= 2_100_000

def test_risk_per_trade_limit(risk_manager):
    """리스크 기반 포지션 사이징 테스트 (손절폭이 클 때 비중 축소)"""
    config.SYSTEM_RISK_PER_TRADE = 1.0 # 1회 매매 시 자산의 1% 리스크만 허용
    config.USE_VOLATILITY_TARGETING = False # 변동성 타겟팅 끄고 테스트
    
    # 자산 1000만원 -> 1% 리스크 허용액 = 10만원
    
    avail_cash = 10_000_000
    invest_ratio = 0.5 # 기본 비중 50% (500만원) 시도
    
    # Case 1: 손절폭이 20%로 매우 큰 경우
    # 500만원 투자 시 손실액 = 100만원 -> 허용액(10만원) 초과
    # 적정 투자금 = 허용액(10만원) / 손절률(0.2) = 50만원
    invest_amt = risk_manager.allocate_budget(
        avail_cash, invest_ratio, stop_loss_rate=-20.0, atr=None, current_price=None
    )
    
    assert invest_amt == 500_000
    
    # Case 2: 손절폭이 1%로 작은 경우
    # 500만원 투자 시 손실액 = 5만원 -> 허용액(10만원) 이내
    # 따라서 기본 비중(500만원) 그대로 투자
    invest_amt_safe = risk_manager.allocate_budget(
        avail_cash, invest_ratio, stop_loss_rate=-1.0, atr=None, current_price=None
    )
    
    assert invest_amt_safe == 5_000_000

def test_volatility_targeting_scaling_limits(risk_manager):
    """변동성 타겟팅 스케일링 제한(Max/Min) 테스트"""
    config.USE_VOLATILITY_TARGETING = True
    config.TARGET_VOLATILITY = 0.20
    config.VOLATILITY_SCALING_MAX = 1.5 # 최대 1.5배로 제한
    config.VOLATILITY_SCALING_MIN = 0.5 # 최소 0.5배로 제한
    
    avail_cash = 10_000_000
    invest_ratio = 0.1 # 100만원
    current_price = 10000
    
    # 1. 극도로 낮은 변동성 (Scale > 1.5 상황)
    # 변동성 5% -> Scale 4.0 -> Max 1.5 적용되어야 함
    atr_low = 31.5 # ~5% vol
    invest_amt_max = risk_manager.allocate_budget(
        avail_cash, invest_ratio, stop_loss_rate=None, atr=atr_low, current_price=current_price
    )
    assert invest_amt_max == 1_500_000 # 100만 * 1.5
    
    # 2. 극도로 높은 변동성 (Scale < 0.5 상황)
    # 변동성 80% -> Scale 0.25 -> Min 0.5 적용되어야 함
    atr_high = 504 # ~80% vol
    invest_amt_min = risk_manager.allocate_budget(
        avail_cash, invest_ratio, stop_loss_rate=None, atr=atr_high, current_price=current_price
    )
    assert invest_amt_min == 500_000 # 100만 * 0.5