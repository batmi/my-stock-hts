"""자산 리스크 관리(손실컷·방어모드) 핵심 로직 테스트.

OrderManager.check_loss_limit 은 '돈을 지키는' 마지막 안전장치이므로
한도초과 방어모드 / 한도내 유지 / 비활성 / 비정상 급감 스킵 / 시작자산 0 분기를 검증한다.

[추세추종] 한도 초과 시 시스템을 통째로 정지(stop)하면 매도 감시까지 꺼져 보유 포지션이
손절선 아래로 방치되므로, '신규 매수만 중단(halt_buys)'하고 청산은 계속 돌리도록 바뀌었다.
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
    trader.halt_buys.return_value = True  # 최초 발동 (중복 아님)
    return RiskManager(trader), trader


@patch('modules.auto_trade.api.send_telegram_message')
def test_loss_limit_triggers_buy_halt_not_stop(mock_tg):
    """일일 손실 한도 초과 시 신규 매수만 중단하고 시스템은 정지하지 않는다."""
    config.SYSTEM_DAILY_LOSS_LIMIT = 5.0
    om, trader = _om(1_000_000)

    om.check_loss_limit(900_000)  # -10% (한도 -5% 초과)

    trader.halt_buys.assert_called_once()
    # 청산(손절·트레일링 스탑) 감시가 계속되어야 하므로 시스템 정지는 호출되지 않는다
    trader.stop.assert_not_called()

    # 알림은 halt_buys에 notify_msg로 위임된다 (중복 발송 방지)
    _, kwargs = trader.halt_buys.call_args
    assert "방어 모드" in kwargs['notify_msg']
    assert "손절" in kwargs['notify_msg']


@patch('modules.auto_trade.api.send_telegram_message')
def test_loss_limit_within_limit_no_halt(mock_tg):
    """손실이 한도 이내면 매수 중단도 정지도 하지 않는다."""
    config.SYSTEM_DAILY_LOSS_LIMIT = 5.0
    om, trader = _om(1_000_000)

    om.check_loss_limit(980_000)  # -2%

    trader.halt_buys.assert_not_called()
    trader.stop.assert_not_called()
    mock_tg.assert_not_called()


@patch('modules.auto_trade.api.send_telegram_message')
def test_loss_limit_disabled(mock_tg):
    """손실 한도가 0(미설정)이면 어떤 손실에도 정지하지 않는다."""
    config.SYSTEM_DAILY_LOSS_LIMIT = 0.0
    om, trader = _om(1_000_000)

    om.check_loss_limit(500_000)  # -50%

    trader.halt_buys.assert_not_called()
    trader.stop.assert_not_called()


@patch('modules.auto_trade.api.send_telegram_message')
def test_loss_limit_abnormal_drop_skipped(mock_tg):
    """시작자산의 50% 미만으로 급감하면 API 오류 의심으로 손실 체크를 건너뛴다."""
    config.SYSTEM_DAILY_LOSS_LIMIT = 5.0
    om, trader = _om(1_000_000)

    om.check_loss_limit(400_000)  # < 50% → 가짜 비상정지 방지

    trader.halt_buys.assert_not_called()
    trader.stop.assert_not_called()


@patch('modules.auto_trade.api.send_telegram_message')
def test_loss_limit_zero_initial_asset(mock_tg):
    """시작 자산이 0(미초기화)이면 비율 계산 불가이므로 정지하지 않는다."""
    config.SYSTEM_DAILY_LOSS_LIMIT = 5.0
    om, trader = _om(0)

    om.check_loss_limit(900_000)

    trader.halt_buys.assert_not_called()
    trader.stop.assert_not_called()


@patch('modules.auto_trade.api.send_telegram_message')
def test_loss_limit_exact_threshold_triggers(mock_tg):
    """손실률이 한도와 정확히 같아도(<=) 방어 모드로 진입한다(경계값)."""
    config.SYSTEM_DAILY_LOSS_LIMIT = 5.0
    om, trader = _om(1_000_000)

    om.check_loss_limit(950_000)  # 정확히 -5%

    trader.halt_buys.assert_called_once()
    trader.stop.assert_not_called()


@patch('modules.auto_trade.api.send_telegram_message')
def test_loss_limit_repeat_does_not_realert(mock_tg):
    """같은 날 재차 한도를 넘어도 halt_buys가 False를 돌려주면 재알림하지 않는다."""
    config.SYSTEM_DAILY_LOSS_LIMIT = 5.0
    om, trader = _om(1_000_000)
    trader.halt_buys.return_value = False  # 이미 발동 중

    om.check_loss_limit(900_000)

    trader.halt_buys.assert_called_once()
    trader.stop.assert_not_called()
    # 중복 콘솔/로그 출력 없이 조용히 넘어간다
    trader.log.assert_not_called()


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
    config.RISK_SCALING_PARAMS = dict(config.RISK_SCALING_PARAMS, GAP_RISK_BUFFER=1.0)  # 갭 버퍼 격리
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


def test_allocate_budget_volatility_scaling_down():
    """변동성 타겟팅 활성 시 고변동성 종목은 비중을 축소한다 (scale<1)."""
    config.SYSTEM_RISK_PER_TRADE = 0
    config.USE_VOLATILITY_TARGETING = True
    config.TARGET_VOLATILITY = 0.20
    config.VOLATILITY_SCALING_MAX = 2.0
    config.VOLATILITY_SCALING_MIN = 0.4
    om, trader = _om(10_000_000)

    # atr/price=0.03 → 연환산 0.03*sqrt(252)≈0.476, scale=0.20/0.476≈0.42 (고변동성 → 축소)
    annual_vol = 0.03 * math.sqrt(252)
    expected_scale = max(0.4, min(2.0, 0.20 / annual_vol))
    amt = om.allocate_budget(50_000_000, 0.1, atr=300, current_price=10000)
    assert amt == int(1_000_000 * expected_scale)


def test_allocate_budget_volatility_scaleup_capped_at_base():
    """[집중 캡] 저변동성 확대 스케일은 기초 비중(base)을 초과하지 못한다."""
    config.SYSTEM_RISK_PER_TRADE = 0
    config.USE_VOLATILITY_TARGETING = True
    config.TARGET_VOLATILITY = 0.20
    config.VOLATILITY_SCALING_MAX = 2.0
    config.VOLATILITY_SCALING_MIN = 0.4
    om, trader = _om(10_000_000)

    # 저변동성 → scale≈1.26(>1)이지만 base(100만)로 클램프
    amt = om.allocate_budget(50_000_000, 0.1, atr=100, current_price=10000)
    assert amt == 1_000_000
