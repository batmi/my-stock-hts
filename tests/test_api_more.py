import pytest
from unittest.mock import patch, MagicMock
import api
import config

@pytest.mark.real_balance_api  # HTTP 계층(session.request)을 직접 mock하므로 conftest의 잔고 차단을 끈다
@patch('api.session.request')
def test_get_overseas_balance(mock_request):
    """해외 주식 잔고 조회 테스트"""
    # 거래소별 응답 시뮬레이션 (NASD만 데이터 있음)
    empty_resp = MagicMock()
    empty_resp.json.return_value = {'rt_cd': '0', 'output1': []}
    
    data_resp = MagicMock()
    data_resp.json.return_value = {
        'rt_cd': '0',
        'output1': [{'ovrs_pdno': 'AAPL', 'ovrs_item_name': 'Apple', 'ovrs_cblc_qty': '10', 'pchs_avg_pric': '150.0', 'frcr_evlu_pfls_amt': '100.0', 'evlu_pfls_rt': '5.0', 'ovrs_now_pric': '160.0'}]
    }
    
    # 순서대로 응답 (NASD, NYSE, AMEX)
    mock_request.side_effect = [data_resp, empty_resp, empty_resp]
    
    holdings = api.get_overseas_balance()
    assert len(holdings) == 1
    assert holdings[0]['ovrs_pdno'] == 'AAPL'

@pytest.mark.real_balance_api  # HTTP 계층(session.request)을 직접 mock하므로 conftest의 계좌 조회 차단을 끈다
@patch('api.session.request')
def test_get_unfilled_orders(mock_request):
    """미체결 내역 조회 테스트"""
    # 국내
    mock_request.return_value.json.return_value = {
        'rt_cd': '0',
        'output': [{'odno': '12345', 'pdno': '005930', 'ord_qty': '10', 'rmn_qty': '5'}]
    }
    orders = api.get_unfilled_orders()
    assert len(orders) == 1
    assert orders[0]['odno'] == '12345'

@patch('api.session.request')
def test_revise_cancel_order(mock_request):
    """주문 정정/취소 테스트"""
    mock_request.return_value.json.return_value = {
        'rt_cd': '0',
        'output': {'ODNO': '67890'}
    }
    
    # 정정
    res = api.revise_cancel_order("domestic", "revise", "12345", "005930", 5, 60000, "01", "00")
    assert res['rt_cd'] == '0'
    
    # 취소
    res = api.revise_cancel_order("domestic", "cancel", "12345", "005930", 0, 0, "02", "00")
    assert res['rt_cd'] == '0'

@patch('api.session.request')
def test_get_deposit(mock_request):
    """예수금 조회 테스트"""
    mock_request.return_value.json.return_value = {
        'rt_cd': '0',
        'output': {'ord_psbl_cash': '1000000'}
    }
    res = api.get_deposit()
    assert res['rt_cd'] == '0'

@patch('api.session.request')
def test_check_server_health(mock_request):
    """서버 상태 점검 테스트"""
    mock_request.return_value.json.return_value = {'rt_cd': '0'}
    assert api.check_server_health() is True
    
    mock_request.return_value.json.return_value = {'rt_cd': '1'}
    assert api.check_server_health() is False