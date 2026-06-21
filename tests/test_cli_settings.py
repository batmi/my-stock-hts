import pytest
from unittest.mock import patch
from modules import settings
import config

@patch('rich.prompt.Prompt.ask')
@patch('modules.settings.modify_trading_cycle_settings')
def test_system_config_menu_cycle(mock_cycle, mock_ask):
    """시스템 설정 메뉴 - 환경 및 주기 설정 테스트"""
    # 메인메뉴 5 -> 서브메뉴 1 -> 메인메뉴 q
    mock_ask.side_effect = ["5", "1", "q"]
    settings.system_config_menu()
    mock_cycle.assert_called_once()

@patch('rich.prompt.Prompt.ask')
@patch('modules.settings.modify_analysis_thresholds')
def test_system_config_menu_analysis(mock_analysis, mock_ask):
    """시스템 설정 메뉴 - 분석 임계값 테스트"""
    # 메인메뉴 1 -> 서브메뉴 1 -> 메인메뉴 q
    mock_ask.side_effect = ["1", "1", "q"]
    settings.system_config_menu()
    mock_analysis.assert_called_once()

@patch('rich.prompt.Prompt.ask')
def test_modify_risk_portfolio_settings(mock_ask):
    """리스크/자산배분 설정 변경 테스트"""
    # 1번(투자비중) -> 0.3 -> q
    mock_ask.side_effect = ["1", "0.3", "q"]
    
    original_val = config.SYSTEM_INVEST_PER_STOCK
    try:
        settings.modify_risk_portfolio_settings()
        assert config.SYSTEM_INVEST_PER_STOCK == 0.3
    finally:
        config.SYSTEM_INVEST_PER_STOCK = original_val

@patch('rich.prompt.Prompt.ask')
def test_modify_scoring_weights(mock_ask):
    """스코어링 가중치 설정 변경 테스트"""
    # a(전체수정) -> TREND 2.0 -> MOMENTUM 2.0 -> STRENGTH 2.0 -> SYNERGY 3.0 -> MOMENTUM_PRICE 1.0 (합계 10.0) -> q
    mock_ask.side_effect = ["a", "2.0", "2.0", "2.0", "3.0", "1.0", "q"]

    original_weights = config.SCORING_WEIGHTS.copy()
    try:
        settings.modify_scoring_weights()
        assert config.SCORING_WEIGHTS["TREND"] == 2.0
        assert config.SCORING_WEIGHTS["MOMENTUM"] == 2.0
        assert config.SCORING_WEIGHTS["STRENGTH"] == 2.0
        assert config.SCORING_WEIGHTS["SYNERGY"] == 3.0
        assert config.SCORING_WEIGHTS["MOMENTUM_PRICE"] == 1.0
    finally:
        config.SCORING_WEIGHTS = original_weights