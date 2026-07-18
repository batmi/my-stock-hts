import pytest
from unittest.mock import patch, MagicMock
from modules import analysis
import pandas as pd
import config

@pytest.fixture
def mock_df():
    return pd.DataFrame({
        'date': pd.date_range(start='2023-01-01', periods=100),
        'close': [10000] * 100,
        'high': [10500] * 100,
        'low': [9500] * 100,
        'open': [10000] * 100,
        'volume': [1000] * 100
    })

@patch('modules.analysis.api.get_chart_data')
def test_diagnose_stock_no_data(mock_get_chart):
    """데이터가 없을 때 분석 함수 처리 테스트"""
    mock_get_chart.return_value = pd.DataFrame()
    
    with patch('config.console.print') as mock_print:
        analysis.diagnose_stock("005930", "Samsung", False)
        # 에러 메시지 출력 확인
        # print 호출 인자 중 하나에 "불러올 수 없습니다"가 포함되어 있는지 확인
        found = False
        for call in mock_print.call_args_list:
            if "불러올 수 없습니다" in str(call):
                found = True
                break
        assert found

@patch('modules.analysis.api.get_chart_data')
@patch('modules.analysis.indicators.calculate_indicators')
@patch('rich.prompt.Prompt.ask', return_value='n')
def test_diagnose_stock_indicators(mock_ask, mock_calc, mock_get_chart, mock_df):
    """지표 계산 후 출력 테스트"""
    mock_get_chart.return_value = mock_df
    mock_calc.return_value = {
        'ema_5': 10000, 'ema_20': 9000, 'ema_60': 8000, 'ema_120': 7000,
        'rsi': 60, 'adx': 30, 'plus_di': 35, 'minus_di': 15,
        'cci': 100, 'obv': 1000, 'obv_trend': True,
        'psar': 9000, 'macd': 50, 'macd_signal': 40, 'macd_hist': 10,
        'atr': 100
    }
    
    with patch('config.console.print') as mock_print:
        analysis.diagnose_stock("005930", "Samsung", False)
        # 테이블 출력 확인 (호출 횟수로 간접 확인)
        assert mock_print.call_count > 5

def _make_daily_df(dates_closes):
    return pd.DataFrame({
        'date': pd.to_datetime([d for d, _ in dates_closes]),
        'close': [c for _, c in dates_closes],
    })


def test_prelisting_last_regular_change_weekday_preopen():
    """거래일 장전(오늘 봉 없음): 마지막 봉=전일 → 전전일은 -2."""
    df = _make_daily_df([('2026-07-15', 280000.0), ('2026-07-16', 282000.0), ('2026-07-17', 285000.0)])
    frozen = MagicMock()
    frozen.now.return_value.strftime.return_value = '20260720'  # 월요일(달력 오늘) > 마지막 봉(금)
    with patch('modules.analysis.datetime', frozen):
        res = analysis._prelisting_last_regular_change(df, 285000.0)
    # 전전일 = -2(282000): 전일(285000) vs 전전일 → +3000 (+1.06%)
    assert res == (3000, pytest.approx(3000 / 282000 * 100))


def test_prelisting_last_regular_change_weekend_uses_calendar_today():
    """주말: market_today는 금요일을 반환하지만 달력 오늘 기준으로 -2(목요일 종가)를 써야 한다."""
    df = _make_daily_df([('2026-07-15', 280000.0), ('2026-07-16', 282000.0), ('2026-07-17', 285000.0)])
    frozen = MagicMock()
    frozen.now.return_value.strftime.return_value = '20260718'  # 토요일(달력 오늘)
    with patch('modules.analysis.datetime', frozen), \
         patch('modules.analysis.utils.market_today', return_value='20260717'):
        res = analysis._prelisting_last_regular_change(df, 285000.0)
    # 마지막 봉(금 285000) < 오늘(토) → 전전일 = -2(목 282000). -3(수 280000)이면 회귀.
    assert res == (3000, pytest.approx(3000 / 282000 * 100))


def test_prelisting_last_regular_change_today_bar_exists():
    """장전 placeholder로 오늘 봉이 이미 있으면 전전일은 -3."""
    df = _make_daily_df([('2026-07-15', 280000.0), ('2026-07-16', 282000.0), ('2026-07-17', 285000.0)])
    frozen = MagicMock()
    frozen.now.return_value.strftime.return_value = '20260717'  # 마지막 봉 날짜 == 오늘
    with patch('modules.analysis.datetime', frozen):
        res = analysis._prelisting_last_regular_change(df, 282000.0)
    # 전전일 = -3(280000): 전일(282000) vs 전전일 → +2000 (+0.71%)
    assert res == (2000, pytest.approx(2000 / 280000 * 100))
