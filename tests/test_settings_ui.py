import pytest
from unittest.mock import patch, MagicMock
from modules import settings
import config

@patch('rich.prompt.Prompt.ask')
def test_view_system_config(mock_ask):
    """시스템 설정 조회 UI 테스트"""
    mock_ask.return_value = 'q' # 바로 종료
    
    with patch('config.console.print') as mock_print:
        settings.view_system_config()
        # 테이블 출력 확인
        assert mock_print.call_count > 0

@patch('rich.prompt.Prompt.ask')
def test_modify_sell_strategy_ui(mock_ask):
    """매도 전략 수정 UI 테스트"""
    # 1번 선택 -> 값 입력 -> 종료
    mock_ask.side_effect = ["1", "50.0", "q"]
    
    original = config.SELL_STRATEGY["TAKE_PROFIT_RATE"]
    try:
        settings.modify_sell_strategy()
        assert config.SELL_STRATEGY["TAKE_PROFIT_RATE"] == 50.0
    finally:
        config.SELL_STRATEGY["TAKE_PROFIT_RATE"] = original

@patch('rich.prompt.Prompt.ask')
def test_modify_indicator_params_ui(mock_ask):
    """지표 설정 수정 UI 테스트"""
    # 1번 선택 -> 값 입력 -> 종료
    mock_ask.side_effect = ["1", "500", "q"]
    
    original = config.INDICATOR_PARAMS["CHART_LOOKBACK_DAYS"]
    try:
        settings.modify_indicator_params()
        assert config.INDICATOR_PARAMS["CHART_LOOKBACK_DAYS"] == 500
    finally:
        config.INDICATOR_PARAMS["CHART_LOOKBACK_DAYS"] = original