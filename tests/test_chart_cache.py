import pytest
import pandas as pd
from unittest.mock import patch, MagicMock
from datetime import datetime, timedelta
import sys
import os

# 프로젝트 루트 경로 추가
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import api
import config

@pytest.fixture(autouse=True)
def setup_teardown():
    """매 테스트마다 캐시를 강제 초기화하고 TTL을 설정합니다."""
    api.clear_chart_cache()
    config.settings.CHART_CACHE_TTL_MINUTES = 180
    yield
    api.clear_chart_cache()

def create_dummy_df(last_date_str, last_close=1000.0):
    """테스트용 2일 치 더미 일봉 데이터프레임 생성"""
    first_date_str = (datetime.strptime(last_date_str, "%Y%m%d") - timedelta(days=1)).strftime("%Y%m%d")
    return pd.DataFrame([
        {'date': first_date_str, 'open': 900, 'high': 1000, 'low': 800, 'close': 950, 'volume': 100},
        {'date': last_date_str, 'open': last_close, 'high': last_close+100, 'low': last_close-100, 'close': last_close, 'volume': 200},
    ])

@patch('api.get_current_price_data')
def test_initial_fetch_and_cache(mock_get_price):
    """1. 최초 요청 시 원본 데이터 조회 함수(fetch_func)가 호출되고 캐시에 저장되는가?"""
    today_str = datetime.now().strftime("%Y%m%d")
    mock_fetch_func = MagicMock(return_value=create_dummy_df(today_str))
    
    # 첫 번째 호출
    df = api._get_cached_chart('005930', False, False, mock_fetch_func)
    
    mock_fetch_func.assert_called_once() # 무거운 데이터 조회가 1회 발생해야 함
    assert not df.empty
    assert '005930_False_False' in api._CHART_CACHE # 캐시 딕셔너리에 저장되어야 함

@patch('api.get_current_price_data')
def test_cache_hit_and_stitch(mock_get_price):
    """2. 캐시가 존재할 때, 원본 조회 없이 실시간 현재가가 기존 과거 데이터 끝에 병합되는가?"""
    today_str = datetime.now().strftime("%Y%m%d")
    yesterday_str = (datetime.now() - timedelta(days=1)).strftime("%Y%m%d")
    
    # 1. 초기 캐시 세팅 (어제까지의 데이터만 있다고 가정)
    initial_df = create_dummy_df(yesterday_str, last_close=1000.0)
    mock_fetch_func = MagicMock(return_value=initial_df)
    api._get_cached_chart('005930', False, False, mock_fetch_func)
    
    # 2. 두 번째 호출 (캐시 히트 대기)
    # 현재가 API 모킹 (전일 종가 1000원, 오늘 현재가 1050원)
    mock_get_price.return_value = {
        'rt_cd': '0',
        'output': {
            'stck_prpr': '1050',
            'stck_oprc': '1010',
            'stck_hgpr': '1060',
            'stck_lwpr': '990',
            'acml_vol': '5000',
            'stck_prdy_clpr': '1000' # 어제 종가가 캐시(1000)와 일치함!
        }
    }
    
    mock_fetch_func.reset_mock()
    df_stitched = api._get_cached_chart('005930', False, False, mock_fetch_func)
    
    # 검증: 무거운 API 호출이 없어야 함
    mock_fetch_func.assert_not_called()
    
    # 검증: 데이터가 정상적으로 덧붙여졌는지 확인 (어제 데이터 2행 + 오늘 데이터 1행 = 총 3행)
    assert len(df_stitched) == 3
    assert df_stitched.iloc[-1]['date'] == today_str
    assert df_stitched.iloc[-1]['close'] == 1050.0 # 오늘 현재가가 종가로 반영됨

@patch('api.get_current_price_data')
def test_cache_invalidation_by_corporate_action(mock_get_price):
    """3. 액면분할 등으로 전일 종가가 달라졌을 때, 이를 감지하고 캐시를 파기하는가?"""
    yesterday_str = (datetime.now() - timedelta(days=1)).strftime("%Y%m%d")
    
    # 1. 초기 캐시 세팅 (어제 종가 10,000원)
    initial_df = create_dummy_df(yesterday_str, last_close=10000.0)
    mock_fetch_func = MagicMock(return_value=initial_df)
    api._get_cached_chart('005930', False, False, mock_fetch_func)
    
    # 2. 두 번째 호출 (액면분할 1/5 가정)
    mock_get_price.return_value = {
        'rt_cd': '0',
        'output': {
            'stck_prpr': '2100',
            'stck_oprc': '2000',
            'stck_hgpr': '2150',
            'stck_lwpr': '1950',
            'acml_vol': '50000',
            'stck_prdy_clpr': '2000' # API가 알려주는 수정된 어제 종가! (캐시의 10000과 다름)
        }
    }
    
    mock_fetch_func.reset_mock()
    # 파기 후 재조회 시 반환할 새로운 데이터프레임
    mock_fetch_func.return_value = create_dummy_df(yesterday_str, last_close=2000.0)
    
    df_new = api._get_cached_chart('005930', False, False, mock_fetch_func)
    
    # 검증: 캐시가 무효화되고 무거운 API(fetch_func)가 다시 호출되어야 함!
    mock_fetch_func.assert_called_once()
    assert df_new.iloc[-1]['close'] == 2000.0

def test_cache_ttl_expiration():
    """4. 지정된 TTL(180분)이 지나면 캐시가 만료되어 새로 데이터를 받아오는가?"""
    today_str = datetime.now().strftime("%Y%m%d")
    mock_fetch_func = MagicMock(return_value=create_dummy_df(today_str))
    
    # 1. 캐시 저장 (현재 시간 기준)
    api._get_cached_chart('005930', False, False, mock_fetch_func)
    assert mock_fetch_func.call_count == 1
    
    # 2. 캐시 타임스탬프를 과거(4시간 전)로 강제 조작
    with api._CHART_CACHE_LOCK:
        cached_item = api._CHART_CACHE['005930_False_False']
        cached_item['timestamp'] = datetime.now() - timedelta(hours=4)
        
    # 3. 다시 조회
    mock_fetch_func.reset_mock()
    
    # TTL 모킹을 위한 패치는 생략하고 실제 타임스탬프 조작으로 검증
    api._get_cached_chart('005930', False, False, mock_fetch_func)
    
    # 검증: 180분이 넘었으므로 캐시가 파기되고 원본 데이터 조회가 다시 일어나야 함
    mock_fetch_func.assert_called_once()

@patch('api.yf.Ticker')
def test_cache_hit_overseas_fast_info(mock_ticker):
    """5. 해외 주식/지수의 경우 yfinance fast_info를 이용해 실시간 캔들이 병합되는가?"""
    today_str = datetime.now().strftime("%Y%m%d")
    yesterday_str = (datetime.now() - timedelta(days=1)).strftime("%Y%m%d")
    
    # 1. 초기 캐시 세팅 (어제 종가 1000원)
    initial_df = create_dummy_df(yesterday_str, last_close=1000.0)
    mock_fetch_func = MagicMock(return_value=initial_df)
    api._get_cached_chart('NQ=F', is_overseas=True, is_index=True, fetch_func=mock_fetch_func)
    
    # 2. 두 번째 호출 (캐시 히트 및 fast_info 패치)
    mock_fast_info = MagicMock()
    mock_fast_info.last_price = 1050.0
    mock_fast_info.regular_market_previous_close = 1000.0
    mock_fast_info.last_volume = 5000
    mock_ticker.return_value.fast_info = mock_fast_info
    
    mock_fetch_func.reset_mock()
    df_stitched = api._get_cached_chart('NQ=F', is_overseas=True, is_index=True, fetch_func=mock_fetch_func)
    
    # 검증: 무거운 fetch_func는 불리지 않고, fast_info를 통해 오늘의 1050원 종가가 추가되어야 함
    mock_fetch_func.assert_not_called()
    assert len(df_stitched) == 3
    assert df_stitched.iloc[-1]['close'] == 1050.0

@patch('modules.market.api.fetch_yfinance_data')
def test_market_yf_cache_hit(mock_fetch):
    """6. market.py의 다중 티커 조회 전용 캐시(_MARKET_YF_CACHE)가 정상 작동하는가?"""
    import modules.market as market
    market.clear_market_yf_cache()
    
    today_str = datetime.now().strftime("%Y%m%d")
    d_df = create_dummy_df(today_str)
    # yfinance가 반환하는 원본 데이터 형식(첫 글자 대문자)에 맞춰 컬럼명 변환
    d_df.rename(columns={'date': 'Date', 'open': 'Open', 'high': 'High', 'low': 'Low', 'close': 'Close', 'volume': 'Volume'}, inplace=True)
    mock_fetch.return_value = d_df # 단일 티커 형태 반환 모킹
    
    # 1. 초기 시장 지수 조회 (나스닥 1개만 타겟)
    market._show_market_indices_core(["나스닥"])
    
    # 검증: daily와 intraday 조회를 위해 yfinance가 2번 호출되어야 함
    initial_call_count = mock_fetch.call_count
    assert initial_call_count >= 2
    assert "^IXIC" in market._MARKET_YF_CACHE
    
    # 2. 두 번째 조회 (캐시 히트)
    mock_fetch.reset_mock()
    market._show_market_indices_core(["나스닥"])
    
    # 검증: 메모리에 데이터가 있으므로 네트워크 다운로드 스레드가 전혀 실행되지 않아야 함
    assert mock_fetch.call_count == 0