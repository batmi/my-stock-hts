"""국내 지수 공유 캐시의 '오래된 값 고착' 방지 회귀 테스트.

운영 중 확인된 두 증상을 재현·차단한다.
  1) 24시간 이상 구동 시 다음날 지수가 어제 값 그대로 + 등락률 0%
  2) 몇 시간 텀을 두고 조회하면 이전 값이 보이고, 곧바로 재조회해야 갱신
"""
import sys
import os
import time
import pandas as pd
import pytest
from unittest.mock import patch, MagicMock
from datetime import datetime, timedelta

sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import api
import config
from modules import analysis


@pytest.fixture(autouse=True)
def clean_index_cache():
    analysis._INDEX_DATA_CACHE.clear()
    analysis._INDEX_REFRESH_INFLIGHT.clear()
    yield
    analysis._INDEX_DATA_CACHE.clear()
    analysis._INDEX_REFRESH_INFLIGHT.clear()


def _df():
    return pd.DataFrame([
        {'date': '20260723', 'open': 1, 'high': 1, 'low': 1, 'close': 3000.0, 'volume': 0},
        {'date': '20260724', 'open': 1, 'high': 1, 'low': 1, 'close': 3100.0, 'volume': 0},
    ])


def _put(day, age_sec):
    analysis._INDEX_DATA_CACHE['KOSPI'] = {
        'df': _df(), 'time': time.time() - age_sec, 'fail_time': 0, 'day': day,
    }


def test_fresh_within_ttl(monkeypatch):
    monkeypatch.setattr(analysis, '_current_market_day', lambda: '20260724')
    _put('20260724', 10)
    assert analysis._lookup_index_cache('KOSPI')[0] == 'fresh'


def test_stale_serves_old_value_only_briefly(monkeypatch):
    """TTL 경과 직후는 stale(즉시 반환 + 백그라운드 갱신)이 유지되어야 한다."""
    monkeypatch.setattr(analysis, '_current_market_day', lambda: '20260724')
    _put('20260724', analysis._INDEX_DATA_CACHE_TTL + 10)
    assert analysis._lookup_index_cache('KOSPI')[0] == 'stale'


def test_long_idle_forces_sync_refetch(monkeypatch):
    """[증상 2] 몇 시간 방치 후 조회: stale 서빙 금지 → 동기 재조회(expired)."""
    monkeypatch.setattr(analysis, '_current_market_day', lambda: '20260724')
    _put('20260724', analysis._INDEX_DATA_MAX_STALE + 60)
    status, df = analysis._lookup_index_cache('KOSPI')
    assert status == 'expired'
    assert df is not None and not df.empty   # 재조회 실패 시 폴백용으로 함께 반환


def test_next_day_forces_sync_refetch(monkeypatch):
    """[증상 1] 날짜(거래일)가 바뀌면 TTL 이내라도 옛 데이터를 서빙하지 않는다."""
    monkeypatch.setattr(analysis, '_current_market_day', lambda: '20260725')
    _put('20260724', 10)
    assert analysis._lookup_index_cache('KOSPI')[0] == 'expired'


def test_expired_entry_triggers_real_fetch(monkeypatch):
    """expired 상태에서 get_domestic_index_data가 실제 조회를 수행하고 새 값을 돌려주는가."""
    monkeypatch.setattr(analysis, '_index_cache_enabled', lambda: True)
    monkeypatch.setattr(analysis, '_current_market_day', lambda: '20260725')
    _put('20260724', 10)

    fresh = _df()
    fresh.loc[len(fresh)] = {'date': '20260725', 'open': 1, 'high': 1, 'low': 1,
                             'close': 3200.0, 'volume': 0}
    fetcher = MagicMock(return_value=fresh)
    monkeypatch.setattr(analysis, '_fetch_domestic_index_data', fetcher)

    out = analysis.get_domestic_index_data('KOSPI')
    fetcher.assert_called_once()
    assert float(out['close'].iloc[-1]) == 3200.0
    assert analysis._INDEX_DATA_CACHE['KOSPI']['day'] == '20260725'


def test_refresh_inflight_released_when_thread_start_fails(monkeypatch):
    """스레드 기동 실패 시 inflight 표시가 남으면 이후 갱신이 영구히 막힌다 → 반드시 해제."""
    monkeypatch.setattr(analysis, '_index_cache_enabled', lambda: True)

    def boom(*a, **k):
        raise RuntimeError("can't start new thread")

    monkeypatch.setattr(analysis.threading, 'Thread', boom)
    analysis._trigger_async_refresh('KOSPI')
    assert 'KOSPI' not in analysis._INDEX_REFRESH_INFLIGHT


def test_stuck_refresh_allows_retrigger(monkeypatch):
    """워커가 멈춰 inflight가 남아도 stuck 시간이 지나면 재기동을 허용한다."""
    monkeypatch.setattr(analysis, '_index_cache_enabled', lambda: True)
    analysis._INDEX_REFRESH_INFLIGHT['KOSPI'] = time.time() - (analysis._INDEX_REFRESH_STUCK_SEC + 5)

    started = []
    monkeypatch.setattr(analysis.threading, 'Thread',
                        lambda *a, **k: MagicMock(start=lambda: started.append(1)))
    analysis._trigger_async_refresh('KOSPI')
    assert started == [1]


@patch('api.get_domestic_index_price')
def test_preopen_index_does_not_append_flat_bar(mock_price, monkeypatch):
    """[증상 1의 0%] 장전(09:00 전) 국내 지수: 현재가=전일 종가로 가짜 당일 봉을 만들지 않는다."""
    monkeypatch.setattr(api, '_chart_disk_get', lambda *a, **k: None)
    monkeypatch.setattr(api, '_chart_disk_set', lambda *a, **k: None)
    monkeypatch.setattr(api, '_before_krx_regular_open', lambda: True)
    monkeypatch.setattr(api, 'market_today', lambda is_overseas=False: '20260725')
    api.clear_chart_cache()
    config.settings.CHART_CACHE_TTL_MINUTES = 180

    # 장전 KIS 지수 현재가 = 전일 종가
    mock_price.return_value = {'rt_cd': '0', 'output': {
        'bstp_nmix_prpr': '3100.0', 'bstp_nmix_prdy_clpr': '3100.0'}}

    fetch = MagicMock(return_value=_df())   # 마지막 봉 = 20260724 종가 3100
    api._get_cached_chart('0001', False, True, fetch)      # 최초 조회(캐시 적재)
    out = api._get_cached_chart('0001', False, True, fetch)  # 캐시 적중 + 오버레이

    assert list(out['date']) == ['20260723', '20260724']    # 오늘자 가짜 봉 없음
    assert float(out['close'].iloc[-1]) != float(out['close'].iloc[-2])  # 등락률 0% 고착 없음
    api.clear_chart_cache()
