import pytest
from unittest.mock import patch, MagicMock
from modules.telegram_bot import TelegramCommander
import config

@pytest.fixture
def commander():
    return TelegramCommander()

@patch('modules.telegram_bot.api.get_chart_data')
def test_cmd_market(mock_get_chart, commander):
    """시장 지수 조회 명령어 테스트"""
    # Mock DataFrame
    import pandas as pd
    df = pd.DataFrame({'close': [2500, 2510]})
    mock_get_chart.return_value = df
    
    res = commander._cmd_market([])
    assert "코스피" in res or "KOSPI" in res
    assert "2,510.00" in res

@patch('modules.telegram_bot.api.get_stock_name_by_code', return_value="삼성전자")
@patch('modules.telegram_bot.api.get_chart_data')
@patch('modules.telegram_bot.indicators.calculate_indicators')
@patch('modules.telegram_bot.analysis.classify_stock_state')
@patch('modules.telegram_bot.analysis.calculate_score')
def test_cmd_signal(mock_score, mock_classify, mock_calc, mock_get_chart, mock_name, commander):
    """종목 분석 명령어 테스트"""
    # config 데이터 초기화 (이름 충돌 방지)
    config.session.stock_data = {"stocks_kr": [], "etfs_kr": [], "stocks_us": [], "etfs_us": []}
    
    import pandas as pd
    mock_get_chart.return_value = pd.DataFrame({'close': [60000]*20, 'high': [61000]*20, 'low': [59000]*20})
    mock_calc.return_value = {'ema_20': 59000, 'ema_60': 58000, 'ema_120': 57000, 'psar': 58000, 'rsi': 60, 'adx': 30, 'cci': 100}
    mock_classify.return_value = ("매수", "[red]", "조건 충족")
    mock_score.return_value = (9.0, [])
    
    res = commander._cmd_signal(["005930"])
    assert "삼성전자" in res
    assert "9.0점" in res
    assert "매수" in res

def test_cmd_unknown(commander):
    """알 수 없는 명령어 테스트"""
    # TelegramCommander는 알 수 없는 명령어에 대해 반응하지 않거나 도움말을 줄 수 있음
    # 현재 구현상 핸들러 맵에 없으면 무시됨
    pass