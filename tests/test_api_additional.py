import pytest
from unittest.mock import patch, MagicMock
import api
import config
import pandas as pd
import os

@pytest.fixture(autouse=True)
def clear_micro_cache():
    """테스트 간 마이크로 캐시 오염을 방지하기 위한 초기화"""
    with api._MICRO_CACHE_LOCK:
        api._MICRO_CACHE.clear()
    yield
    with api._MICRO_CACHE_LOCK:
        api._MICRO_CACHE.clear()

@patch('api.call_api')
def test_get_investor_trend(mock_call):
    """투자자 동향 조회 테스트"""
    mock_call.return_value = {
        'rt_cd': '0',
        'output': [{'prsn_ntby_qty': '100', 'frgn_ntby_qty': '-50'}]
    }
    
    result = api.get_investor_trend("005930")
    assert len(result) == 1
    assert result[0]['prsn_ntby_qty'] == '100'

@patch('api.call_api')
def test_fetch_domestic_period_price(mock_call):
    """국내 기간별 시세 조회 테스트"""
    mock_call.return_value = {
        'rt_cd': '0',
        'output2': [{'stck_bsop_date': '20230101', 'stck_clpr': '60000'}]
    }
    
    result = api.fetch_domestic_period_price("005930")
    assert len(result) == 1
    assert result[0]['stck_clpr'] == '60000'

@patch('api.call_api')
def test_fetch_overseas_period_price(mock_call):
    """해외 기간별 시세 조회 테스트"""
    mock_call.return_value = {
        'rt_cd': '0',
        'output2': [{'xymd': '20230101', 'clos': '150.00', 'high': '155.00', 'low': '145.00', 'tovol': '1000'}]
    }
    
    df = api.fetch_overseas_period_price("AAPL", "NAS")
    assert not df.empty
    assert df.iloc[0]['close'] == 150.00

@patch('api.call_api')
def test_find_best_exchange_code(mock_call):
    """최적 거래소 찾기 테스트"""
    # 첫 번째 호출(NAS)에서 성공 가정
    mock_call.return_value = {
        'rt_cd': '0',
        'output': {'last': '150.00'}
    }
    
    excd = api.find_best_exchange_code("AAPL")
    assert excd == "NAS"

@patch('api.call_api')
def test_get_realtime_vol_strength_success(mock_call):
    """실시간 체결강도 조회 성공 테스트"""
    mock_call.return_value = {
        'rt_cd': '0',
        'output': [{'tday_rltv': '120.50'}]
    }
    
    vol = api.get_realtime_vol_strength("005930")
    assert vol == 120.50

@patch('api.call_api')
def test_get_realtime_vol_strength_failure(mock_call):
    """실시간 체결강도 조회 실패 테스트"""
    mock_call.return_value = {'rt_cd': '1'}
    
    vol = api.get_realtime_vol_strength("005930")
    assert vol is None

@patch('api.call_api')
def test_fetch_sellable_quantity(mock_call):
    """매도 가능 수량 조회 테스트"""
    mock_call.return_value = {
        'rt_cd': '0',
        'output1': [{'pdno': '005930', 'ord_psbl_qty': '10'}]
    }
    
    qty = api.fetch_sellable_quantity("005930")
    assert qty == 10

@patch('api.call_api')
def test_fetch_overseas_buyable_quantity(mock_call):
    """해외 매수 가능 수량 조회 테스트"""
    mock_call.return_value = {
        'rt_cd': '0',
        'output': {'ovrs_ord_psbl_qty': '5'}
    }
    
    qty = api.fetch_overseas_buyable_quantity("AAPL", 150.0, "NAS")
    assert qty == 5

@patch('api.call_api')
def test_fetch_overseas_sellable_quantity(mock_call):
    """해외 매도 가능 수량 조회 테스트"""
    mock_call.return_value = {
        'rt_cd': '0',
        'output1': [{'ovrs_pdno': 'AAPL', 'ord_psbl_qty': '5'}]
    }
    
    qty = api.fetch_overseas_sellable_quantity("AAPL", "NAS")
    assert qty == 5

@patch('api.call_api')
def test_get_overseas_open_orders(mock_call):
    """해외 미체결 내역 조회 테스트"""
    mock_call.return_value = {
        'rt_cd': '0',
        'output': [{'odno': '123', 'ovrs_pdno': 'AAPL'}]
    }
    
    orders = api.get_overseas_open_orders()
    assert len(orders) > 0 
    assert orders[0]['odno'] == '123'

@patch('os.path.exists', return_value=True)
@patch('os.listdir', return_value=['test.sqlite'])
@patch('os.remove')
def test_clear_yfinance_cache(mock_remove, mock_listdir, mock_exists):
    """yfinance 캐시 정리 테스트"""
    api.clear_yfinance_cache()
    mock_remove.assert_called()

@patch('api.call_api')
def test_get_ask_bid_ratio_rest_fallback(mock_call):
    """WS 미커버 시 REST 호가 out1 총잔량으로 비율 계산(폴백)"""
    mock_call.return_value = {
        'rt_cd': '0',
        'output1': {'total_askp_rsqn': '3000', 'total_bidp_rsqn': '1500'},
    }
    config.session.is_toss = False
    ratio = api.get_ask_bid_ratio("005930", is_overseas=False)
    assert ratio == pytest.approx(2.0)


@patch('api.call_api')
def test_get_ask_bid_ratio_ws_first_skips_rest(mock_call):
    """WS에 신선한 호가 총잔량이 있으면 REST(call_api) 없이 즉시 계산"""
    import realtime

    class _StubFeed:
        def get_orderbook(self, code, max_age=3.0):
            return {'total_ask': 2000, 'total_bid': 1000}

    config.session.is_toss = False
    with patch.object(realtime, 'get_feed', return_value=_StubFeed()):
        ratio = api.get_ask_bid_ratio("005930", is_overseas=False)
    assert ratio == pytest.approx(2.0)
    mock_call.assert_not_called()


@patch('api.call_api')
def test_get_ask_bid_ratio_only_ask_returns_sentinel(mock_call):
    """매수 잔량 0·매도만 존재 시 99.9 반환"""
    mock_call.return_value = {
        'rt_cd': '0',
        'output1': {'total_askp_rsqn': '5000', 'total_bidp_rsqn': '0'},
    }
    config.session.is_toss = False
    ratio = api.get_ask_bid_ratio("005930", is_overseas=False)
    assert ratio == pytest.approx(99.9)
