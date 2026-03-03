import pytest
from unittest.mock import patch, MagicMock
import api
import config

@patch('api.session.get')
def test_get_stock_name_by_code_domestic(mock_get):
    """국내 종목명 크롤링 테스트"""
    # 네이버 금융 응답 Mock
    mock_resp = MagicMock()
    mock_resp.text = '<meta property="og:title" content="삼성전자 : 네이버 금융">'
    mock_get.return_value = mock_resp
    
    name = api.get_stock_name_by_code("005930", False)
    assert name == "삼성전자"

@patch('api.yf.Ticker')
def test_get_stock_name_by_code_overseas(mock_ticker):
    """해외 종목명 조회 테스트 (yfinance)"""
    mock_instance = MagicMock()
    mock_instance.info = {'longName': 'Apple Inc.'}
    mock_ticker.return_value = mock_instance
    
    name = api.get_stock_name_by_code("AAPL", True)
    assert name == "Apple Inc."

@patch('api.call_api')
def test_get_domestic_index_chart(mock_call):
    """국내 지수 차트 조회 테스트"""
    mock_call.return_value = {
        'rt_cd': '0',
        'output2': [
            {'stck_bsop_date': '20230101', 'bstp_nmix_prpr': '2500.00', 'bstp_nmix_oprc': '2480.00', 'bstp_nmix_hgpr': '2550.00', 'bstp_nmix_lwpr': '2450.00', 'acml_vol': '100000'}
        ]
    }
    
    df = api.get_domestic_index_chart("0001")
    assert not df.empty
    assert df.iloc[0]['close'] == 2500.00

@patch('api.call_api')
def test_fetch_overseas_detail_price(mock_call):
    """해외 주식 상세 현재가 조회 테스트"""
    mock_call.return_value = {
        'rt_cd': '0',
        'output': {'last': '150.00', 'h52p': '200.00'}
    }
    
    data = api.fetch_overseas_detail_price("AAPL", "NAS")
    assert data['last'] == '150.00'

@patch('api.call_api')
def test_fetch_buyable_quantity(mock_call):
    """매수 가능 수량 조회 테스트"""
    mock_call.return_value = {
        'rt_cd': '0',
        'output': {'ord_psbl_qty': '100', 'ord_psbl_cash': '5000000'}
    }
    
    qty = api.fetch_buyable_quantity("005930", 50000)
    assert qty == 100