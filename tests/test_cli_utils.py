import pytest
from unittest.mock import patch
from core import utils
import config

@patch('rich.prompt.Prompt.ask')
def test_select_stock_for_chart(mock_ask):
    """차트용 종목 선택 함수 테스트"""
    # 1(국내주식) -> 1(첫번째 종목)
    mock_ask.side_effect = ["1", "1"]
    config.session.stock_data = {"stocks_kr": [{"name": "삼성전자", "code": "005930"}]}
    
    code, name, is_overseas = utils.select_stock_for_chart()
    
    assert code == "005930"
    assert name == "삼성전자"
    assert is_overseas is False

@patch('rich.prompt.Prompt.ask')
@patch('api.get_stock_name_by_code', return_value="Apple")
def test_select_target_stock(mock_get_name, mock_ask):
    """주문용 종목 선택 함수 테스트"""
    # 2(미국) -> 1(첫번째 종목)
    mock_ask.side_effect = ["2", "1"]
    config.session.stock_data = {
        "stocks_kr": [], "etfs_kr": [],
        "stocks_us": [{"name": "Apple", "code": "AAPL"}],
        "etfs_us": []
    }
    
    code, name, is_overseas = utils.select_target_stock()
    
    assert code == "AAPL"
    assert name == "Apple"
    assert is_overseas is True