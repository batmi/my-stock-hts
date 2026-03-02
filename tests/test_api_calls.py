import pytest
from unittest.mock import patch, MagicMock
import pandas as pd
import api
import config
from datetime import datetime

@pytest.fixture
def mock_response():
    resp = MagicMock()
    resp.status_code = 200
    resp.json.return_value = {'rt_cd': '0', 'output': {}}
    return resp

@patch('api.session.request')
def test_get_current_price_domestic(mock_request, mock_response):
    """국내 주식 현재가 조회 테스트"""
    mock_response.json.return_value = {
        'rt_cd': '0',
        'output': {'stck_prpr': '50000'}
    }
    mock_request.return_value = mock_response
    
    price = api.get_current_price("005930", False)
    assert price == 50000

@patch('api.session.request')
def test_get_current_price_overseas(mock_request, mock_response):
    """해외 주식 현재가 조회 테스트"""
    mock_response.json.return_value = {
        'rt_cd': '0',
        'output': {'last': '150.50'}
    }
    mock_request.return_value = mock_response
    
    price = api.get_current_price("AAPL", True)
    assert price == 150.50

@patch('api.session.request')
def test_get_chart_data_domestic(mock_request, mock_response):
    """국내 차트 데이터 조회 테스트"""
    # 최근 날짜 사용 (데이터 필터링 방지)
    today = datetime.now().strftime("%Y%m%d")
    mock_response.json.return_value = {
        'rt_cd': '0',
        'output2': [
            {'stck_bsop_date': today, 'stck_clpr': '10000', 'stck_oprc': '10000', 'stck_hgpr': '10000', 'stck_lwpr': '10000', 'acml_vol': '1000'}
        ]
    }
    mock_request.return_value = mock_response
    
    df = api.get_chart_data("005930", False)
    assert isinstance(df, pd.DataFrame)
    assert not df.empty
    assert len(df) == 1
    assert df.iloc[0]['close'] == 10000.0

@patch('api.session.request')
def test_place_order(mock_request, mock_response):
    """주문 전송 테스트"""
    mock_response.json.return_value = {
        'rt_cd': '0',
        'output': {'ODNO': '12345'}
    }
    mock_request.return_value = mock_response
    
    res = api.place_order("domestic", "buy", "005930", 10, 50000, "00")
    assert res['rt_cd'] == '0'
    assert res['output']['ODNO'] == '12345'

@patch('api.session.request')
def test_get_domestic_balance(mock_request, mock_response):
    """국내 잔고 조회 테스트"""
    mock_response.json.return_value = {
        'rt_cd': '0',
        'output1': [{'pdno': '005930', 'hldg_qty': '10'}],
        'output2': [{'tot_evlu_amt': '1000000'}]
    }
    mock_request.return_value = mock_response
    
    holdings, summary = api.get_domestic_balance()
    assert len(holdings) == 1
    assert holdings[0]['pdno'] == '005930'
    assert summary[0]['tot_evlu_amt'] == '1000000'

@patch('api.session.request')
def test_api_error_handling(mock_request):
    """API 에러 처리 테스트"""
    # 500 에러 발생 시
    mock_resp = MagicMock()
    mock_resp.status_code = 500
    mock_resp.text = "Server Error"
    mock_request.return_value = mock_resp
    
    # call_api는 예외를 잡아서 에러 딕셔너리를 반환함
    res = api.call_api("test/url", "domestic", "test", "test", retries=1)
    assert res['rt_cd'] == '9999'
