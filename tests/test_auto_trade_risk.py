"""자산 리스크 관리(손실컷·비상정지) 핵심 로직 테스트.

OrderManager.check_loss_limit 은 '돈을 지키는' 마지막 안전장치이므로
한도초과 정지 / 한도내 유지 / 비활성 / 비정상 급감 스킵 / 시작자산 0 분기를 검증한다.
"""
import pytest
from unittest.mock import patch, MagicMock

from modules.auto_trade import RiskManager
import config


def _om(initial_asset=1_000_000):
    """trader를 mock한 RiskManager 생성 헬퍼."""
    trader = MagicMock()
    trader.initial_asset = initial_asset
    trader.last_emergency_alert_time = 0
    return RiskManager(trader), trader


@patch('modules.auto_trade.api.send_telegram_message')
def test_loss_limit_triggers_emergency_stop(mock_tg):
    """일일 손실 한도 초과 시 시스템을 정지(stop)하고 알림을 보낸다."""
    config.SYSTEM_DAILY_LOSS_LIMIT = 5.0
    om, trader = _om(1_000_000)

    om.check_loss_limit(900_000)  # -10% (한도 -5% 초과)

    trader.stop.assert_called_once_with(use_status=False)
    mock_tg.assert_called_once()


@patch('modules.auto_trade.api.send_telegram_message')
def test_loss_limit_within_limit_no_stop(mock_tg):
    """손실이 한도 이내면 정지하지 않는다."""
    config.SYSTEM_DAILY_LOSS_LIMIT = 5.0
    om, trader = _om(1_000_000)

    om.check_loss_limit(980_000)  # -2%

    trader.stop.assert_not_called()
    mock_tg.assert_not_called()


@patch('modules.auto_trade.api.send_telegram_message')
def test_loss_limit_disabled(mock_tg):
    """손실 한도가 0(미설정)이면 어떤 손실에도 정지하지 않는다."""
    config.SYSTEM_DAILY_LOSS_LIMIT = 0.0
    om, trader = _om(1_000_000)

    om.check_loss_limit(500_000)  # -50%

    trader.stop.assert_not_called()


@patch('modules.auto_trade.api.send_telegram_message')
def test_loss_limit_abnormal_drop_skipped(mock_tg):
    """시작자산의 50% 미만으로 급감하면 API 오류 의심으로 손실 체크를 건너뛴다."""
    config.SYSTEM_DAILY_LOSS_LIMIT = 5.0
    om, trader = _om(1_000_000)

    om.check_loss_limit(400_000)  # < 50% → 가짜 비상정지 방지

    trader.stop.assert_not_called()


@patch('modules.auto_trade.api.send_telegram_message')
def test_loss_limit_zero_initial_asset(mock_tg):
    """시작 자산이 0(미초기화)이면 비율 계산 불가이므로 정지하지 않는다."""
    config.SYSTEM_DAILY_LOSS_LIMIT = 5.0
    om, trader = _om(0)

    om.check_loss_limit(900_000)

    trader.stop.assert_not_called()


@patch('modules.auto_trade.api.send_telegram_message')
def test_loss_limit_exact_threshold_triggers(mock_tg):
    """손실률이 한도와 정확히 같아도(<=) 정지한다(경계값)."""
    config.SYSTEM_DAILY_LOSS_LIMIT = 5.0
    om, trader = _om(1_000_000)

    om.check_loss_limit(950_000)  # 정확히 -5%

    trader.stop.assert_called_once_with(use_status=False)


# ==========================================================
# RiskManager.allocate_budget 포지션 사이징
# ==========================================================
import math


def test_allocate_budget_base_ratio():
    """리스크/변동성 비활성 시 기초 비중(initial_asset * ratio)만 적용한다."""
    config.SYSTEM_RISK_PER_TRADE = 0
    config.USE_VOLATILITY_TARGETING = False
    om, trader = _om(10_000_000)

    amt = om.allocate_budget(avail_cash=5_000_000, invest_ratio=0.1)
    assert amt == 1_000_000  # 10,000,000 * 0.1


def test_allocate_budget_capped_by_cash():
    """목표 금액이 예수금을 초과하면 예수금으로 제한한다."""
    config.SYSTEM_RISK_PER_TRADE = 0
    config.USE_VOLATILITY_TARGETING = False
    om, trader = _om(10_000_000)

    amt = om.allocate_budget(avail_cash=500_000, invest_ratio=0.1)  # 목표 1M > 예수금 500k
    assert amt == 500_000


def test_allocate_budget_risk_based_reduction():
    """손절폭이 넓으면 리스크 한도(SYSTEM_RISK_PER_TRADE)에 맞춰 비중을 축소한다."""
    config.SYSTEM_RISK_PER_TRADE = 1.0  # 거래당 최대 손실 1%
    config.USE_VOLATILITY_TARGETING = False
    om, trader = _om(10_000_000)

    # max_loss=10M*1%=100k, 손절 -20% → risk_amt=100k/0.2=500k < 기초 1M → 500k
    amt = om.allocate_budget(5_000_000, 0.1, stop_loss_rate=-20.0)
    assert amt == 500_000


def test_allocate_budget_zero_initial_uses_cash():
    """시작 자산이 0이면 예수금 기준으로 비중을 계산한다."""
    config.SYSTEM_RISK_PER_TRADE = 0
    config.USE_VOLATILITY_TARGETING = False
    om, trader = _om(0)

    amt = om.allocate_budget(2_000_000, 0.1)  # 0이면 avail_cash * ratio
    assert amt == 200_000


def test_allocate_budget_volatility_scaling():
    """변동성 타겟팅 활성 시 종목 변동성에 따라 비중을 스케일한다."""
    config.SYSTEM_RISK_PER_TRADE = 0
    config.USE_VOLATILITY_TARGETING = True
    config.TARGET_VOLATILITY = 0.30
    config.VOLATILITY_SCALING_MAX = 2.0
    config.VOLATILITY_SCALING_MIN = 0.5
    om, trader = _om(10_000_000)

    # atr/price=0.01 → 연환산 0.01*sqrt(252)≈0.1587, scale=0.30/0.1587≈1.89 (저변동성 → 비중 확대)
    annual_vol = 0.01 * math.sqrt(252)
    expected_scale = max(0.5, min(2.0, 0.30 / annual_vol))
    amt = om.allocate_budget(50_000_000, 0.1, atr=100, current_price=10000)
    assert amt == int(1_000_000 * expected_scale)
