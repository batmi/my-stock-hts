import pytest
from unittest.mock import patch, MagicMock
from modules import market
import pandas as pd
import config

@patch('modules.market.api.fetch_yfinance_data')
@patch('modules.market.api.get_domestic_index_chart')
@patch('modules.market.api.get_chart_data')
def test_show_market_indices(mock_get_chart, mock_get_index, mock_fetch_yf):
    """시장 지수 조회 함수 테스트"""
    # Mock Data
    mock_df = pd.DataFrame({
        'date': pd.date_range(start='2023-01-01', periods=100),
        'close': [2500] * 100,
        'open': [2500] * 100,
        'high': [2550] * 100,
        'low': [2450] * 100,
        'volume': [10000] * 100
    })
    
    mock_fetch_yf.return_value = mock_df
    mock_get_index.return_value = mock_df
    mock_get_chart.return_value = mock_df
    
    # yfinance Tickers Mock
    with patch('modules.market.yf.Tickers') as mock_tickers:
        mock_ticker = MagicMock()
        mock_ticker.fast_info.last_price = 2500.0
        mock_ticker.fast_info.regular_market_previous_close = 2490.0
        mock_tickers.return_value.tickers = {'^KS11': mock_ticker}
        
        with patch('rich.prompt.Prompt.ask', side_effect=["8", "q"]):
            with patch('config.console.print') as mock_print:
                market.show_market_indices()
                # 테이블 출력 확인 (호출 횟수로 간접 확인)
                assert mock_print.call_count > 0