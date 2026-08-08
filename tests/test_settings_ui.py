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

    청산 체계는 대부분 봉인돼 있어 이 메뉴에 남는 편집 항목은 시간 청산 사용뿐이다
    (TS 발동 방식은 잠금, TS 발동 수익률은 breakeven에서 폴백 전용이라 함께 숨김).
    """
    # 1번 선택 -> 값 입력 -> 종료 ('n'은 '현재 유지'라 토글되지 않는다)
    mock_ask.side_effect = ["1", "false", "q"]

    original = config.SELL_STRATEGY["TIME_STOP_USE"]
    try:
        settings.modify_sell_strategy()
        assert config.SELL_STRATEGY["TIME_STOP_USE"] is False
    finally:
        config.SELL_STRATEGY["TIME_STOP_USE"] = original


def test_ts_activation_rate_range_rule_still_guards():
    """TS 발동률 50%는 트레일링 스탑을 사실상 비활성화한다 — 주청산 수단이 사라진다.

    breakeven에서 이 항목은 메뉴에서 숨겨졌지만, fixed로 되돌리면 다시 편집 대상이
    되므로 범위 규칙 자체는 살아 있어야 한다.
    """
    assert settings._range_error("TRAILING_STOP_ACTIVATION_RATE", 50.0)
    assert settings._range_error("TRAILING_STOP_ACTIVATION_RATE", 20.0) is None


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