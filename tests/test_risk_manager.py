import pytest
import math
from modules.auto_trade import RiskManager
from unittest.mock import patch
import config

# Mock Trader 클래스 (RiskManager 초기화용)
class MockTrader:
    def __init__(self, initial_asset=10_000_000):
        self.initial_asset = initial_asset
    def log(self, msg):
        pass # 테스트 중 로그 출력 생략
    def stop(self):
        pass # Mock stop method

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

def test_volatility_targeting_low_vol_capped_at_base(risk_manager):
    """[집중 캡] 저변동성 확대 스케일은 기초 비중(base)을 초과하지 못하고 클램프된다.

    변동성 관리 강조 fix: 종목당 명목 상한(SYSTEM_INVEST_PER_STOCK)을 넘는 몰빵을 방지하기 위해,
    변동성 타겟팅의 확대(scale>1) 결과가 base_amt를 넘지 않도록 최종 클램프한다.
    (예전에는 base×scale이 그대로 반영되어 한 종목이 자본의 최대 50%까지 커질 수 있었다.)"""
    config.USE_VOLATILITY_TARGETING = True
    config.TARGET_VOLATILITY = 0.20
    config.VOLATILITY_SCALING_MAX = 2.0

    avail_cash = 10_000_000
    invest_ratio = 0.1 # 기본 투자금(base) 100만원

    # 상황: ATR이 낮음 -> 연환산 변동성 약 10% -> Scale ≈ 2.0 (확대)
    current_price = 10000
    atr = 63

    invest_amt = risk_manager.allocate_budget(
        avail_cash, invest_ratio, stop_loss_rate=None, atr=atr, current_price=current_price
    )

    # 확대 스케일(2.0)에도 불구하고 base(100만원)로 클램프됨
    assert invest_amt == 1_000_000

def test_risk_per_trade_limit(risk_manager):
    """리스크 기반 포지션 사이징 테스트 (손절폭이 클 때 비중 축소)"""
    config.SYSTEM_RISK_PER_TRADE = 1.0 # 1회 매매 시 자산의 1% 리스크만 허용
    config.USE_VOLATILITY_TARGETING = False # 변동성 타겟팅 끄고 테스트
    # 갭 버퍼는 별도 테스트에서 검증하므로 여기선 1.0으로 격리 (순수 리스크 수식 검증)
    config.RISK_SCALING_PARAMS = dict(config.RISK_SCALING_PARAMS, GAP_RISK_BUFFER=1.0)

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


def test_gap_risk_buffer_reduces_size(risk_manager):
    """[갭 버퍼] 손절폭에 GAP_RISK_BUFFER를 곱해 리스크 기반 투자금을 보수적으로 축소"""
    config.SYSTEM_RISK_PER_TRADE = 1.0
    config.USE_VOLATILITY_TARGETING = False
    config.RISK_SCALING_PARAMS = dict(config.RISK_SCALING_PARAMS, GAP_RISK_BUFFER=1.25)

    # 허용액 10만원, 손절 -20% → 유효 손절폭 = 0.20 × 1.25 = 0.25
    # 투자금 = 100,000 / 0.25 = 400,000원 (버퍼 미적용 시 500,000원)
    invest_amt = risk_manager.allocate_budget(
        10_000_000, 0.5, stop_loss_rate=-20.0, atr=None, current_price=None
    )
    assert invest_amt == 400_000
    config.RISK_SCALING_PARAMS = dict(config.RISK_SCALING_PARAMS, GAP_RISK_BUFFER=1.2)


def test_risk_scale_shrinks_size(risk_manager):
    """[리스크 스케일링] risk_scale(<1)이 리스크 허용액을 배수만큼 축소"""
    config.SYSTEM_RISK_PER_TRADE = 1.0
    config.USE_VOLATILITY_TARGETING = False
    config.RISK_SCALING_PARAMS = dict(config.RISK_SCALING_PARAMS, GAP_RISK_BUFFER=1.0)
    risk_manager.trader.risk_scale = 0.5  # 약세/드로다운 축소 가정

    # 허용액 = 1000만 × 1% × 0.5 = 5만원, 손절 -20% → 5만/0.2 = 25만원
    invest_amt = risk_manager.allocate_budget(
        10_000_000, 0.5, stop_loss_rate=-20.0, atr=None, current_price=None
    )
    assert invest_amt == 250_000
    risk_manager.trader.risk_scale = 1.0

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
    # 변동성 5% -> Scale 4.0 -> Max 1.5 적용, 다시 base(100만원)로 클램프됨
    # (변동성 관리 fix: 확대는 종목당 명목 상한 base를 넘을 수 없음)
    atr_low = 31.5 # ~5% vol
    invest_amt_max = risk_manager.allocate_budget(
        avail_cash, invest_ratio, stop_loss_rate=None, atr=atr_low, current_price=current_price
    )
    assert invest_amt_max == 1_000_000 # min(100만 * 1.5, base 100만) = 100만
    
    # 2. 극도로 높은 변동성 (Scale < 0.5 상황)
    # 변동성 80% -> Scale 0.25 -> Min 0.5 적용되어야 함
    atr_high = 504 # ~80% vol
    invest_amt_min = risk_manager.allocate_budget(
        avail_cash, invest_ratio, stop_loss_rate=None, atr=atr_high, current_price=current_price
    )
    assert invest_amt_min == 500_000 # 100만 * 0.5

@patch('modules.auto_trade.api.send_telegram_message')
def test_check_loss_limit_triggered(mock_tg, risk_manager):
    """손실 한도 초과 시 정지 테스트"""
    risk_manager.trader.initial_asset = 10_000_000
    config.SYSTEM_DAILY_LOSS_LIMIT = 5.0 # 5% limit
    
    # Current asset: 9,000,000 (-10% loss)
    current_total = 9_000_000
    
    with patch.object(risk_manager.trader, 'stop') as mock_stop:
        risk_manager.check_loss_limit(current_total)
        
        mock_stop.assert_called_once()
        mock_tg.assert_called_once()
        assert "비상 정지" in mock_tg.call_args[0][0]

def test_check_loss_limit_safe(risk_manager):
    """손실 한도 이내일 때 테스트"""
    risk_manager.trader.initial_asset = 10_000_000
    config.SYSTEM_DAILY_LOSS_LIMIT = 5.0

    # Current asset: 9,600,000 (-4% loss)
    current_total = 9_600_000

    with patch.object(risk_manager.trader, 'stop') as mock_stop:
        risk_manager.check_loss_limit(current_total)
        mock_stop.assert_not_called()


# =========================================================
# [추가] 포트폴리오 히트(총 오픈 리스크) 계산·예산 테스트
# =========================================================
import threading

class MockHeatTrader(MockTrader):
    """compute_portfolio_heat가 참조하는 트레일링 캐시/락 포함 Mock"""
    def __init__(self, initial_asset=10_000_000):
        super().__init__(initial_asset)
        self._lock = threading.RLock()
        self.trailing_stop_cache = {}
        self.portfolio_heat_amt = 0.0


def _holding(code, qty, buy, cur):
    return {'pdno': code, 'hldg_qty': str(qty), 'pchs_avg_pric': str(buy), 'prpr': str(cur)}


def test_portfolio_heat_basic(monkeypatch):
    """오픈 리스크 = 수량 × (현재가 - 손절선). 손절률은 매수기록 수량가중 평균 사용"""
    trader = MockHeatTrader()
    rm = RiskManager(trader)
    monkeypatch.setitem(config.SELL_STRATEGY, 'USE_ATR_STOP', True)
    monkeypatch.setitem(config.SELL_STRATEGY, 'TRAILING_STOP_ACTIVATION_RATE', 10.0)
    monkeypatch.setitem(config.SELL_STRATEGY, 'TRAILING_STOP_CALLBACK_RATE', 5.0)
    trader.trailing_stop_cache['005930'] = 10000.0  # 최고가=매수가 → BEP/TS 미발동

    heat = rm.compute_portfolio_heat(
        [_holding('005930', 10, 10000, 10000)],
        {'005930': [{'qty': 10, 'stop_loss_rate': -5.0}]},
    )
    # 손절선 = 10000×0.95=9500 → 리스크 = 10주 × 500원
    assert heat == pytest.approx(10 * 500)


def test_portfolio_heat_bep_and_ts_uplift(monkeypatch):
    """BEP/트레일링 발동 이력이 있으면 손절선이 상향되어 오픈 리스크가 줄어든다"""
    trader = MockHeatTrader()
    rm = RiskManager(trader)
    monkeypatch.setitem(config.SELL_STRATEGY, 'USE_ATR_STOP', True)
    monkeypatch.setitem(config.SELL_STRATEGY, 'TRAILING_STOP_ACTIVATION_RATE', 10.0)
    monkeypatch.setitem(config.SELL_STRATEGY, 'TRAILING_STOP_CALLBACK_RATE', 5.0)

    # BEP: 최고가 +6% ≥ 손절폭 5%(ATR 동기화) → 손절선 본전(매수가) 상향
    trader.trailing_stop_cache['000001'] = 10600.0
    heat = rm.compute_portfolio_heat(
        [_holding('000001', 10, 10000, 10300)],
        {'000001': [{'qty': 10, 'stop_loss_rate': -5.0}]},
    )
    assert heat == pytest.approx(10 * 300)

    # TS: 최고가 +20% ≥ 발동 10% → 손절선 = 12000×(1-5%)=11400
    trader.trailing_stop_cache['000002'] = 12000.0
    heat2 = rm.compute_portfolio_heat(
        [_holding('000002', 10, 10000, 11500)],
        {'000002': [{'qty': 10, 'stop_loss_rate': -5.0}]},
    )
    assert heat2 == pytest.approx(10 * 100)


def test_portfolio_heat_locked_position_zero_risk(monkeypatch):
    """손절선이 현재가 위(이익 잠김)면 해당 포지션 리스크는 0"""
    trader = MockHeatTrader()
    rm = RiskManager(trader)
    monkeypatch.setitem(config.SELL_STRATEGY, 'USE_ATR_STOP', True)
    monkeypatch.setitem(config.SELL_STRATEGY, 'TRAILING_STOP_ACTIVATION_RATE', 10.0)
    monkeypatch.setitem(config.SELL_STRATEGY, 'TRAILING_STOP_CALLBACK_RATE', 5.0)

    # 최고가 15000 → TS선 14250 > 현재가 14000 → 리스크 0 (음수 아님)
    trader.trailing_stop_cache['000003'] = 15000.0
    heat = rm.compute_portfolio_heat(
        [_holding('000003', 10, 10000, 14000)],
        {'000003': [{'qty': 10, 'stop_loss_rate': -5.0}]},
    )
    assert heat == 0.0


def test_portfolio_risk_budget_left(monkeypatch):
    """히트 캡까지 남은 예산 계산 및 캡 미사용(0) 시 None"""
    trader = MockHeatTrader(10_000_000)
    rm = RiskManager(trader)

    monkeypatch.setattr(config, 'SYSTEM_MAX_PORTFOLIO_RISK', 10.0, raising=False)
    trader.portfolio_heat_amt = 400_000
    assert rm.portfolio_risk_budget_left() == pytest.approx(600_000)

    monkeypatch.setattr(config, 'SYSTEM_MAX_PORTFOLIO_RISK', 0.0, raising=False)
    assert rm.portfolio_risk_budget_left() is None


def test_portfolio_risk_budget_uses_current_asset(monkeypatch):
    """[히트 캡 기준자산] 현재 평가자산이 있으면 그 값을 기준으로 예산 산출 (보수적)"""
    trader = MockHeatTrader(10_000_000)
    rm = RiskManager(trader)
    monkeypatch.setattr(config, 'SYSTEM_MAX_PORTFOLIO_RISK', 10.0, raising=False)

    # 장중 손실로 현재자산이 800만원으로 감소 → 예산도 함께 축소
    trader.current_total_asset = 8_000_000
    trader.portfolio_heat_amt = 0
    assert rm.portfolio_risk_budget_left() == pytest.approx(800_000)  # 800만 × 10%


def test_portfolio_risk_budget_scaled_by_risk_scale(monkeypatch):
    """[리스크 스케일링] risk_scale(<1)이 히트 캡 예산을 배수만큼 축소"""
    trader = MockHeatTrader(10_000_000)
    rm = RiskManager(trader)
    monkeypatch.setattr(config, 'SYSTEM_MAX_PORTFOLIO_RISK', 10.0, raising=False)
    trader.portfolio_heat_amt = 0
    trader.risk_scale = 0.5  # 약세/드로다운 축소

    # 실효 캡 = 10% × 0.5 = 5% → 1000만 × 5% = 50만원
    assert rm.portfolio_risk_budget_left() == pytest.approx(500_000)
    assert rm.effective_portfolio_cap() == pytest.approx(5.0)
    trader.risk_scale = 1.0