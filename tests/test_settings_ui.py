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
    """매도 전략 수정 UI 테스트

    반추세성 청산 설정(고정 익절 등)은 메뉴에서 숨겨져 1번 항목이 TS 발동 수익률이다.
    """
    # 1번 선택 -> 값 입력 -> 종료
    mock_ask.side_effect = ["1", "50.0", "q"]

    original = config.SELL_STRATEGY["TRAILING_STOP_ACTIVATION_RATE"]
    try:
        settings.modify_sell_strategy()
        assert config.SELL_STRATEGY["TRAILING_STOP_ACTIVATION_RATE"] == 50.0
    finally:
        config.SELL_STRATEGY["TRAILING_STOP_ACTIVATION_RATE"] = original


def test_anti_trend_keys_hidden_from_menus():
    """추세추종 보호: 반추세성 청산 설정이 설정/프리셋 편집 목록에 노출되지 않아야 한다."""
    sell_names = {it["name"] for it in settings._sell_strategy_items()}
    entry_names = {it["name"] for it in settings._entry_strategy_items()}
    assert not (settings.ANTI_TREND_HIDDEN_KEYS & sell_names)
    assert not (settings.ANTI_TREND_HIDDEN_KEYS & entry_names)
    # 내부 설정 키 자체는 유지되어야 한다 (로직/백테스트 호환)
    for key in ["TAKE_PROFIT_RATE", "HALF_TAKE_PROFIT_USE", "TAKE_PROFIT_RSI",
                "SUPER_TAKE_PROFIT_RSI", "DEFENSIVE_HALF_SELL_USE", "MR_GRACE_LOSS_RATE"]:
        assert key in config.SELL_STRATEGY
    for key in ["USE_MEAN_REVERSION", "MR_RSI_MAX", "MR_DISPARITY_MAX", "MR_VOL_STRENGTH"]:
        assert key in config.ANALYSIS_THRESHOLDS

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