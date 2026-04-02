import pytest
from unittest.mock import patch, MagicMock
from modules import market
import pandas as pd

@patch('modules.market.api.fetch_yfinance_data')
@patch('modules.market.yf.Tickers')
def test_show_market_indices_full_coverage(mock_tickers, mock_fetch):
    """시장 지수 조회 전체 커버리지 테스트"""
    # 1. Mock yfinance data (Daily & Intraday)
    dates = pd.date_range(start='2023-01-01', periods=10)
    
    # MultiIndex Mocking for fetch_yfinance_data (Ticker, Price)
    cols = pd.MultiIndex.from_product([['^KS11', '^KQ11', 'KRW=X'], ['Open', 'High', 'Low', 'Close', 'Volume']])
    data = pd.DataFrame(100, index=dates, columns=cols)
    mock_fetch.return_value = data
    
    # 2. Mock Tickers fast_info
    mock_ticker_obj = MagicMock()
    mock_ticker_obj.fast_info.last_price = 2500.0
    mock_ticker_obj.fast_info.regular_market_previous_close = 2490.0
    mock_ticker_obj.fast_info.year_high = 2600.0
    
    mock_tickers.return_value.tickers = {
        '^KS11': mock_ticker_obj,
        '^KQ11': mock_ticker_obj,
        'KRW=X': mock_ticker_obj
    }
    
    # 3. Run
    with patch('rich.prompt.Prompt.ask', side_effect=["8", "q"]):
        with patch('config.console.print') as mock_print:
            market.show_market_indices()
            assert mock_print.call_count > 0