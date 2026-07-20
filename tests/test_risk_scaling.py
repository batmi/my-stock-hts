"""리스크 한도 동적 스케일링 테스트 (약세 국면·계좌 드로다운 연동)

[추세추종 2원칙] "자본대비 리스크에 한도를 둬야 한다" 구현부(_update_risk_scale,
_get_account_drawdown_pct)와 피라미딩 시장 필터 게이트를 검증한다.
"""
import pytest
from unittest.mock import patch, MagicMock
from modules.auto_trade import AutoTrader
import config


@pytest.fixture
def trader():
    t = AutoTrader()
    # 테스트 격리: 상태 초기화
    t.initial_asset = 10_000_000
    t.current_total_asset = 10_000_000
    t.risk_scale = 1.0
    t.risk_scale_reason = ""
    t._hwm_cache = 0.0
    t._hwm_cache_date = None
    t.market_index_status = {}
    return t


@pytest.fixture(autouse=True)
def restore_risk_params():
    orig = dict(config.RISK_SCALING_PARAMS)
    yield
    config.RISK_SCALING_PARAMS = orig


# =========================================================
# _update_risk_scale — 시장 국면 연동
# =========================================================
def test_risk_scale_bear_regime(trader):
    """약세(Bear) 국면이면 리스크 배수가 BEAR_RISK_SCALE로 축소된다."""
    config.RISK_SCALING_PARAMS = dict(config.RISK_SCALING_PARAMS,
                                      USE_REGIME_RISK_SCALING=True, BEAR_RISK_SCALE=0.75,
                                      USE_DRAWDOWN_RISK_SCALING=False)
    with patch('modules.auto_trade.trader.analysis.get_market_regime', return_value=("Bear", 0.5)):
        trader._update_risk_scale()
    assert trader.risk_scale == pytest.approx(0.75)


def test_risk_scale_bull_regime_no_change(trader):
    """강세/횡보 국면이면 국면에 의한 축소는 없다(1.0 유지)."""
    config.RISK_SCALING_PARAMS = dict(config.RISK_SCALING_PARAMS,
                                      USE_REGIME_RISK_SCALING=True, BEAR_RISK_SCALE=0.75,
                                      USE_DRAWDOWN_RISK_SCALING=False)
    with patch('modules.auto_trade.trader.analysis.get_market_regime', return_value=("Bull", -0.5)):
        trader._update_risk_scale()
    assert trader.risk_scale == pytest.approx(1.0)


# =========================================================
# _update_risk_scale — 드로다운 연동
# =========================================================
def _patch_regime_neutral():
    return patch('modules.auto_trade.trader.analysis.get_market_regime', return_value=("Sideways", 0.0))


def test_risk_scale_drawdown_level1(trader):
    """드로다운 1단계(5%~10%) 진입 시 DD_SCALE_1 적용."""
    config.RISK_SCALING_PARAMS = dict(config.RISK_SCALING_PARAMS,
                                      USE_REGIME_RISK_SCALING=False, USE_DRAWDOWN_RISK_SCALING=True,
                                      DD_LEVEL_1=5.0, DD_SCALE_1=0.75, DD_LEVEL_2=10.0, DD_SCALE_2=0.5)
    # HWM 1000만, 현재 930만 → DD 7% → 1단계
    trader.current_total_asset = 9_300_000
    with _patch_regime_neutral(), \
         patch('modules.auto_trade.trader.db_manager.db.get_max_daily_asset', return_value=10_000_000):
        trader._update_risk_scale()
    assert trader.risk_scale == pytest.approx(0.75)


def test_risk_scale_drawdown_level2(trader):
    """드로다운 2단계(≥10%) 진입 시 DD_SCALE_2 적용."""
    config.RISK_SCALING_PARAMS = dict(config.RISK_SCALING_PARAMS,
                                      USE_REGIME_RISK_SCALING=False, USE_DRAWDOWN_RISK_SCALING=True,
                                      DD_LEVEL_1=5.0, DD_SCALE_1=0.75, DD_LEVEL_2=10.0, DD_SCALE_2=0.5)
    # HWM 1000만, 현재 880만 → DD 12% → 2단계
    trader.current_total_asset = 8_800_000
    with _patch_regime_neutral(), \
         patch('modules.auto_trade.trader.db_manager.db.get_max_daily_asset', return_value=10_000_000):
        trader._update_risk_scale()
    assert trader.risk_scale == pytest.approx(0.5)


def test_risk_scale_regime_and_drawdown_compound(trader):
    """약세 국면 × 드로다운 배수는 곱으로 결합된다."""
    config.RISK_SCALING_PARAMS = dict(config.RISK_SCALING_PARAMS,
                                      USE_REGIME_RISK_SCALING=True, BEAR_RISK_SCALE=0.75,
                                      USE_DRAWDOWN_RISK_SCALING=True,
                                      DD_LEVEL_1=5.0, DD_SCALE_1=0.75, DD_LEVEL_2=10.0, DD_SCALE_2=0.5)
    trader.current_total_asset = 8_800_000  # DD 12% → 0.5
    with patch('modules.auto_trade.trader.analysis.get_market_regime', return_value=("Bear", 0.5)), \
         patch('modules.auto_trade.trader.db_manager.db.get_max_daily_asset', return_value=10_000_000):
        trader._update_risk_scale()
    assert trader.risk_scale == pytest.approx(0.75 * 0.5)


def test_risk_scale_no_drawdown(trader):
    """고점 근처(드로다운 < 1단계)면 축소 없음."""
    config.RISK_SCALING_PARAMS = dict(config.RISK_SCALING_PARAMS,
                                      USE_REGIME_RISK_SCALING=False, USE_DRAWDOWN_RISK_SCALING=True,
                                      DD_LEVEL_1=5.0, DD_SCALE_1=0.75)
    trader.current_total_asset = 9_800_000  # DD 2%
    with _patch_regime_neutral(), \
         patch('modules.auto_trade.trader.db_manager.db.get_max_daily_asset', return_value=10_000_000):
        trader._update_risk_scale()
    assert trader.risk_scale == pytest.approx(1.0)


def test_drawdown_pct_uses_initial_asset_as_hwm_floor(trader):
    """HWM은 DB 고점과 당일 시작자산 중 큰 값. DB가 낮아도 시작자산이 바닥선."""
    config.RISK_SCALING_PARAMS = dict(config.RISK_SCALING_PARAMS, DD_LOOKBACK_DAYS=90)
    trader.initial_asset = 10_000_000
    trader.current_total_asset = 9_000_000
    trader._hwm_cache_date = None
    with patch('modules.auto_trade.trader.db_manager.db.get_max_daily_asset', return_value=8_000_000):
        dd = trader._get_account_drawdown_pct()
    # HWM = max(DB 800만, 시작 1000만) = 1000만 → DD = (1000-900)/1000 = 10%
    assert dd == pytest.approx(10.0)


# =========================================================
# 피라미딩 시장 필터 게이트
# =========================================================
def test_pyramid_blocked_in_bear_market(trader):
    """시장 필터 '보류'(지수<SMA) 시장의 종목은 피라미딩 증액을 보류한다."""
    config.USE_MARKET_FILTER = True
    with patch.dict(config.ANALYSIS_THRESHOLDS, {"PYRAMIDING_REQUIRE_HEALTHY_MARKET": True}):
        trader.market_index_status = {"KOSPI": {"is_healthy": False, "current": 2000}}
        trader.strategy = MagicMock()
        trader.strategy.analyze_pyramid.return_value = (True, "피라미딩 1차")
        # 국내 KOSPI 종목으로 판별되도록 패치
        with patch.object(trader, '_get_stock_market_type', return_value="KOSPI"), \
             patch.object(trader, 'log') as mock_log:
            result = {'state': '매수', 'score': 8.0, 'ind': {}}
            trader._try_pyramid_buy('005930', '삼성전자', 10, 60000, 12.0,
                                    result, last_buy=None, is_market_open=True, rule=None)
            # 증액 주문 경로로 진입하지 않고 보류 로그만 남아야 함
            assert any("약세" in str(c) for c in mock_log.call_args_list)
