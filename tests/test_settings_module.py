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
    """분석 임계값 설정 변경 테스트 (매도잔량 비율 기준)

    항목 번호는 목록에서 조회한다 — 숨김 처리(ANTI_TREND/BACKTESTED/INDICATOR_STANDARD
    _HIDDEN_KEYS)로 항목이 늘거나 줄어도 깨지지 않게 하드코딩하지 않는다.
    (역추세 RSI(MR_RSI_MAX)는 추세추종 보호로 영구 숨김되어 편집 대상이 아니므로 제외)
    """
    names = [it["name"] for it in settings._entry_strategy_items()]
    idx = names.index("BUY_ASK_BID_RATIO") + 1
    mock_ask.side_effect = [str(idx), "1.8", "q"]

    orig_ratio = config.ANALYSIS_THRESHOLDS.get("BUY_ASK_BID_RATIO", 1.2)

    try:
        settings.modify_analysis_thresholds()
        assert config.ANALYSIS_THRESHOLDS["BUY_ASK_BID_RATIO"] == 1.8
        assert "MR_RSI_MAX" not in names, "역추세 설정은 편집 목록에 노출되면 안 된다"
    finally:
        config.ANALYSIS_THRESHOLDS["BUY_ASK_BID_RATIO"] = orig_ratio

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