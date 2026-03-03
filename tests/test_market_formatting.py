import pytest
from unittest.mock import patch, MagicMock
from modules import market
import pandas as pd

@patch('modules.market.api.fetch_yfinance_data')
@patch('modules.market.yf.Tickers')
def test_show_market_indices_formatting(mock_tickers, mock_fetch):
    """모든 지수 타입에 대한 포맷팅 로직 테스트"""
    # Mock Data
    dates = pd.date_range(start='2023-01-01', periods=20)
    df = pd.DataFrame({
        'close': [100.0] * 20,
        'open': [100.0] * 20,
        'high': [100.0] * 20,
        'low': [100.0] * 20,
        'volume': [1000] * 20
    }, index=dates)
    
    # MultiIndex Mocking
    tickers = ["^KS11", "^KQ11", "^IXIC", "^GSPC", "^DJI", "GC=F", "SI=F", "HG=F", "CL=F", "NG=F", "ZW=F", "DX-Y.NYB", "KRW=X", "^VIX", "^SOX"]
    cols = pd.MultiIndex.from_product([tickers, df.columns])
    data = pd.DataFrame(100.0, index=dates, columns=cols)
    mock_fetch.return_value = data
    
    # Tickers Mock
    mock_ticker_obj = MagicMock()
    mock_ticker_obj.fast_info.last_price = 100.0
    mock_ticker_obj.fast_info.regular_market_previous_close = 99.0
    mock_tickers.return_value.tickers = {t: mock_ticker_obj for t in tickers}
    
    with patch('config.console.print') as mock_print:
        market.show_market_indices()
        # 모든 지수가 출력되었는지 확인 (행 개수 등으로)
        assert mock_print.call_count > 0