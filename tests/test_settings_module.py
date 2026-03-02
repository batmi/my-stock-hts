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
def test_modify_telegram_settings(mock_ask):
    """텔레그램 설정 변경 테스트"""
    # 1번 항목(사용여부) 선택 -> 변경(n) -> 종료(q)
    mock_ask.side_effect = ["1", "n", "q"]
    
    original_val = config.ENABLE_TELEGRAM
    config.ENABLE_TELEGRAM = True # 초기값 True로 고정
    
    try:
        settings.modify_telegram_settings()
        assert config.ENABLE_TELEGRAM is False
    finally:
        config.ENABLE_TELEGRAM = original_val