import pytest
from unittest.mock import patch, MagicMock
import sys
import os
import time

# 프로젝트 루트 경로 추가
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import api
import modules.market as market

def test_global_index_background_warming():
    """4번 기능: 글로벌 지수(yfinance) 백그라운드 예열(Warming) 동작 검증

    [주의] 이 예열 스레드는 데몬이고 지수 예열이 끝나면 관심종목 루프로 넘어가
      종목마다 get_chart_data + time.sleep(모의 1.0초)를 돈다. 종목 목록을 비우지 않으면
      실제 stock.json을 읽어 수십 초를 더 살아 있고, 그 사이 **다른 테스트가 패치해 둔
      api.get_chart_data 목에 호출을 얹는다**. 실제로 test_chart의
      assert_called_with(마지막 호출 검증)가 드물게 깨졌다. 종목 목록을 비우고 스레드가
      끝난 것까지 확인해, 검증 범위를 지수 예열로 한정한다.
    """
    import config

    # 기존 캐시 초기화
    market.clear_market_yf_cache()

    # 해외 지수 2개만 남긴다. 국내 지수는 tvDatafeed(웹소켓)를 타는데, requests 기준의
    # 테스트 격리 가드가 웹소켓은 막지 못해 스레드가 수십 초씩 살아남는다.
    with patch('api.fetch_yfinance_data') as mock_fetch, \
         patch('modules.market.ALL_INDICES', [("나스닥", "^IXIC"), ("S&P500", "^GSPC")]), \
         patch.object(config.session, 'stock_data', {}, create=True):
        import pandas as pd
        # 빈 데이터프레임 반환으로 모킹하여 실제 네트워크를 타지 않게 방어
        mock_fetch.return_value = pd.DataFrame()
        
        # 백그라운드 스레드 실행
        thread = api.prefetch_watchlists_async()
        
        # 워커 스레드가 완료될 때까지 대기
        thread.join(timeout=10.0)
        assert not thread.is_alive(), "예열 스레드가 끝나지 않았다 — 다른 테스트의 목을 오염시킨다"
        
        # 전체 지수 개수 / 청크사이즈(15) 번 만큼 fetch가 호출되었는지 검증 (1일봉, 5분봉 2번씩)
        assert mock_fetch.call_count >= 2

@patch('api.get_current_price_data')
@patch('api.get_investor_trend')
@patch('api.get_realtime_vol_strength')
def test_bulk_fetch_domestic_prices(mock_vol, mock_inv, mock_price):
    """2번 기능: 다중 종목 일괄 조회 API (국내) - ThreadPoolExecutor 동작 검증"""
    codes = ["005930", "000660", "373220"]
    api.prefetch_multiple_current_prices(codes, is_overseas=False)
    
    # 입력된 종목 수만큼 각 API가 정확히 1번씩 호출되었는지 검증 (병렬 처리 여부와 상관없이 총 횟수 확인)
    assert mock_price.call_count == len(codes)
    assert mock_inv.call_count == len(codes)
    assert mock_vol.call_count == len(codes)

@patch('api.get_yf_fast_info')
def test_bulk_fetch_overseas_prices(mock_fi):
    """2번 기능: 다중 종목 일괄 조회 API (해외) - get_yf_fast_info 병렬 호출 검증"""
    codes = ["AAPL", "TSLA", "NVDA"]
    api.prefetch_multiple_current_prices(codes, is_overseas=True)
    
    # 해외 종목의 경우 get_yf_fast_info가 각 종목마다 호출되었는지 검증
    assert mock_fi.call_count == len(codes)