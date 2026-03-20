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
    """4번 기능: 글로벌 지수(yfinance) 백그라운드 예열(Warming) 동작 검증"""
    # 기존 캐시 초기화
    market.clear_market_yf_cache()
    
    with patch('api.fetch_yfinance_data') as mock_fetch:
        import pandas as pd
        # 빈 데이터프레임 반환으로 모킹하여 실제 네트워크를 타지 않게 방어
        mock_fetch.return_value = pd.DataFrame()
        
        # 백그라운드 스레드 실행
        thread = api.prefetch_watchlists_async()
        
        # 워커 스레드가 완료될 때까지 대기
        thread.join(timeout=5.0)
        
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

@patch('api.yf.Tickers')
def test_bulk_fetch_overseas_prices(mock_tickers):
    """2번 기능: 다중 종목 일괄 조회 API (해외) - yfinance Tickers 동작 검증"""
    mock_tickers_obj = MagicMock()
    mock_tickers.return_value = mock_tickers_obj
    
    codes = ["AAPL", "TSLA", "NVDA"]
    api.prefetch_multiple_current_prices(codes, is_overseas=True)
    
    # 해외 종목의 경우 Tickers 객체 생성(bulk fetch)이 1회 호출되었는지 검증
    assert mock_tickers.called