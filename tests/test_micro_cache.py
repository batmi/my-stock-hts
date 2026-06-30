import pytest
from unittest.mock import patch, MagicMock
import time
import sys
import os

# 프로젝트 루트 경로 추가
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import api

@pytest.fixture(autouse=True)
def setup_teardown():
    """매 테스트마다 마이크로 캐시를 강제 초기화하여 독립성을 보장합니다."""
    with api._MICRO_CACHE_LOCK:
        api._MICRO_CACHE.clear()
    yield
    with api._MICRO_CACHE_LOCK:
        api._MICRO_CACHE.clear()

def test_micro_cache_hit_and_miss():
    """1. 마이크로 캐시의 TTL 적중(Hit) 및 만료(Miss) 기본 동작 검증"""
    test_key = "test_key"
    test_data = {"price": 1000}
    
    # 캐시 저장
    api._set_micro_cache(test_key, test_data)
    
    # TTL 이내 조회 (Hit)
    assert api._get_micro_cache(test_key, ttl=3.0) == test_data
    
    # 시간을 강제로 TTL 이후로 조작하여 조회 (Miss)
    with patch('time.time', return_value=time.time() + 5.0):
        assert api._get_micro_cache(test_key, ttl=3.0) is None

@patch('api.call_api')
def test_get_current_price_data_uses_micro_cache(mock_call_api):
    """2. get_current_price_data API가 마이크로 캐시를 정상적으로 활용하는지 검증"""
    # API 응답 모킹
    mock_call_api.return_value = {"rt_cd": "0", "output": {"stck_prpr": "50000"}}

    # NXT(대체거래소) 보조 호출까지 검증하므로 실전 모드를 가정한다.
    # (모의투자 환경에서는 NXT 조회를 스킵해 호출이 1회로 줄어 환경 의존이 생기므로 명시 고정)
    with patch.object(api.config.session, 'is_simulation', False):
        # 첫 번째 호출: 캐시가 없으므로 call_api가 호출되어야 함 (정규장 1회 + NXT장 1회 = 총 2회)
        res1 = api.get_current_price_data("005930", is_overseas=False)
        assert res1["output"]["stck_prpr"] == "50000"
        assert mock_call_api.call_count == 2

        # 두 번째 호출: 캐시가 존재하므로 call_api가 호출되지 않아야 함 (중복 방지 성공)
        res2 = api.get_current_price_data("005930", is_overseas=False)
        assert res2["output"]["stck_prpr"] == "50000"
        assert mock_call_api.call_count == 2  # 호출 횟수가 증가하지 않음!

        # 시간 경과 시뮬레이션 (TTL 만료 시 재호출 여부 확인)
        with patch('time.time', return_value=time.time() + 65.0): # 기본 TTL 60초 초과
            res3 = api.get_current_price_data("005930", is_overseas=False)
            assert mock_call_api.call_count == 4  # 캐시 만료로 다시 API가 호출되어야 함 (총 4회)

@patch('tradingview_screener.Query')
@patch('api.yf.Ticker')
def test_get_yf_fast_info_uses_micro_cache(mock_ticker, mock_query):
    """3. get_yf_fast_info 함수가 yfinance 통신을 캐싱하여 중복을 방지하는지 검증"""
    mock_fast_info = MagicMock()
    mock_fast_info.last_price = 150.0
    mock_fast_info.regular_market_previous_close = 145.0
    mock_ticker.return_value.fast_info = mock_fast_info
    
    # TV Screener 강제 실패 유도 (yfinance Fallback 정상 작동 확인용)
    mock_query.side_effect = Exception("Force Fallback")

    # 첫 호출
    info1 = api.get_yf_fast_info("AAPL")
    assert info1["last_price"] == 150.0
    assert mock_ticker.call_count == 1
    
    # 두 번째 호출 (캐시 적중)
    info2 = api.get_yf_fast_info("AAPL")
    assert info2["last_price"] == 150.0
    assert mock_ticker.call_count == 1 # 추가 객체 생성 및 통신이 없어야 함