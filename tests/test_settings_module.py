import pytest
from unittest.mock import patch
from modules import settings
import config

@patch('rich.prompt.Prompt.ask')
def test_modify_analysis_thresholds(mock_ask):
    """분석 임계값 설정 변경 테스트"""
    # 1번 항목 선택 -> 값 변경(9.0) -> 종료(q)
    mock_ask.side_effect = ["1", "9.0", "q"]
    
    original_val = config.ANALYSIS_THRESHOLDS["BUY_SCORE"]
    
    try:
        settings.modify_analysis_thresholds()
        assert config.ANALYSIS_THRESHOLDS["BUY_SCORE"] == 9.0
    finally:
        config.ANALYSIS_THRESHOLDS["BUY_SCORE"] = original_val

@patch('rich.prompt.Prompt.ask')
def test_modify_analysis_thresholds_new_items(mock_ask):
    """새로 추가된 분석 임계값 설정 변경 테스트 (비대칭성, 역추세 RSI)"""
    # 8번 항목(비대칭성) -> 1.8 -> 12번 항목(역추세 RSI) -> 35.0 -> 종료(q)
    mock_ask.side_effect = ["8", "1.8", "12", "35.0", "q"]
    
    orig_ratio = config.ANALYSIS_THRESHOLDS.get("BUY_ASK_BID_RATIO", 1.2)
    orig_mr_rsi = config.ANALYSIS_THRESHOLDS.get("MR_RSI_MAX", 40.0)
    
    try:
        settings.modify_analysis_thresholds()
        assert config.ANALYSIS_THRESHOLDS["BUY_ASK_BID_RATIO"] == 1.8
        assert config.ANALYSIS_THRESHOLDS["MR_RSI_MAX"] == 35.0
    finally:
        config.ANALYSIS_THRESHOLDS["BUY_ASK_BID_RATIO"] = orig_ratio
        config.ANALYSIS_THRESHOLDS["MR_RSI_MAX"] = orig_mr_rsi

@patch('rich.prompt.Prompt.ask')
def test_modify_risk_portfolio_settings_new_items(mock_ask):
    """새로 추가된 리스크 설정 변경 테스트 (일일 손실 제한)"""
    # 항목 번호는 목록에서 조회한다 — 숨김 처리(ANTI_TREND/BACKTESTED_HIDDEN_KEYS)로
    # 항목이 늘거나 줄어도 테스트가 깨지지 않도록 하드코딩하지 않는다.
    names = [it["name"] for it in settings._risk_portfolio_items()]
    idx = names.index("SYSTEM_DAILY_LOSS_LIMIT") + 1
    mock_ask.side_effect = [str(idx), "15.0", "q"]

    orig_loss_limit = getattr(config.settings, 'SYSTEM_DAILY_LOSS_LIMIT', 10.0)
    try:
        settings.modify_risk_portfolio_settings()
        assert getattr(config.settings, 'SYSTEM_DAILY_LOSS_LIMIT') == 15.0
    finally:
        setattr(config.settings, 'SYSTEM_DAILY_LOSS_LIMIT', orig_loss_limit)

@patch('rich.prompt.Prompt.ask')
def test_modify_telegram_settings(mock_ask):
    """텔레그램 설정 변경 테스트"""
    # 1번 항목(사용여부) 선택 -> 변경(y) -> 종료(q)
    mock_ask.side_effect = ["1", "y", "q"]
    
    original_val = getattr(config.settings, 'ENABLE_TELEGRAM', True)
    config.settings.ENABLE_TELEGRAM = True # 초기값 True로 고정
    
    try:
        settings.modify_telegram_settings()
        assert getattr(config.settings, 'ENABLE_TELEGRAM', True) is False
    finally:
        config.settings.ENABLE_TELEGRAM = original_val