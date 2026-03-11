import pytest
from unittest.mock import patch, MagicMock
from modules import market
import pandas as pd
import config
from modules import db_manager

@patch('modules.market.api.fetch_yfinance_data')
@patch('modules.market.api.get_domestic_index_chart')
@patch('modules.market.api.get_chart_data')
def test_show_market_indices_exceptions(mock_get_chart, mock_get_index, mock_fetch_yf):
    """시장 지수 조회 중 예외 발생 시 처리 테스트"""
    # API 호출에서 예외 발생 설정
    mock_fetch_yf.side_effect = Exception("YFinance Error")
    mock_get_index.side_effect = Exception("KIS API Error")
    mock_get_chart.side_effect = Exception("Chart Data Error")
    
    with patch('rich.prompt.Prompt.ask', return_value="8"):
        with patch('config.console.print') as mock_print:
            market.show_market_indices()

    db_manager.db._get_conn().close() # Close db connection to resolve warnings.
    assert mock_print.call_count > 0

    #with patch('modules.api.get_domestic_index_chart') as mock_get_index, \
    #    patch('modules.api.fetch_yfinance_data') as mock_fetch_yf:
    #    mock_fetch_yf.side_effect = Exception("YFinance Error")
    #    mock_get_index.side_effect = Exception("KIS API Error")

    #    with patch('config.console.print') as mock_print:
    #        market.show_market_indices()
        # 에러가 발생해도 테이블은 출력되어야 함 (Error 행 포함)
    assert mock_print.call_count > 0

@patch('modules.market.api.fetch_yfinance_data')
def test_show_market_indices_empty_data(mock_fetch_yf):
    """데이터가 비어있을 때 처리 테스트"""
    mock_fetch_yf.return_value = pd.DataFrame()
    
    with patch('rich.prompt.Prompt.ask', return_value="8"):
        with patch('config.console.print') as mock_print:
            market.show_market_indices()
            assert mock_print.call_count > 0