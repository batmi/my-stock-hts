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


# ==========================================================
# 장중 지수 실시간 갱신 (2026-07-28 실측: 장중 전 지수가 '어제 값 + 0.00%'로 고착)
# ==========================================================

def test_index_price_tr_id_is_mapped():
    """지수 현재가 TR 매핑 누락 → call_api가 'TR_ID not found'로 즉시 실패하고
    당일 봉 실시간 오버레이·서킷브레이커 등락률이 통째로 죽는다(무증상 회귀)."""
    import constants
    entry = constants.TR_ID_CONFIG['domestic']['quotations']['index_price']
    assert entry['real'] and entry['sim']


def test_index_price_fills_prev_close(monkeypatch):
    """업종 현재가 TR은 전일 종가를 안 준다 → 현재가 - 전일대비로 채워야 한다."""
    api._MICRO_CACHE.clear()
    monkeypatch.setattr(api.config.session, 'is_toss', False, raising=False)
    monkeypatch.setattr(api, 'call_api', lambda *a, **k: {'rt_cd': '0', 'output': {
        'bstp_nmix_prpr': '6117.88', 'bstp_nmix_prdy_vrss': '-637.87', 'acml_vol': '189905'}})

    out = api.get_domestic_index_price('0001')['output']
    assert float(out['bstp_nmix_prdy_clpr']) == pytest.approx(6755.75, abs=0.01)


def test_intraday_overlay_updates_today_bar(monkeypatch):
    """장중: 캐시된 당일 봉이 실시간 지수(시/고/저/거래량 포함)로 갱신되어야 한다."""
    monkeypatch.setattr(api, '_chart_disk_get', lambda *a, **k: None)
    monkeypatch.setattr(api, '_chart_disk_set', lambda *a, **k: None)
    monkeypatch.setattr(api, 'market_today', lambda is_overseas=False: '20260728')
    monkeypatch.setattr(api, '_before_krx_regular_open', lambda: False)
    monkeypatch.setattr(api, '_krx_close_passed_at', lambda: None)
    monkeypatch.setattr(api, 'chart_overlay_enabled', lambda is_overseas=False: True)
    monkeypatch.setattr(api, 'get_domestic_index_price', lambda code: {'rt_cd': '0', 'output': {
        'bstp_nmix_prpr': '6117.88', 'bstp_nmix_prdy_clpr': '6755.75',
        'bstp_nmix_oprc': '6400.27', 'bstp_nmix_hgpr': '6413.57', 'bstp_nmix_lwpr': '6031.38',
        'acml_vol': '189905'}})
    api.clear_chart_cache()
    config.settings.CHART_CACHE_TTL_MINUTES = 180

    # 개장 직후 캐시된 당일 봉(= 전일 종가 복제)
    stale = pd.DataFrame([
        {'date': '20260727', 'open': 6806.27, 'high': 6806.27, 'low': 6557.39, 'close': 6755.75, 'volume': 275718.0},
        {'date': '20260728', 'open': 6755.75, 'high': 6755.75, 'low': 6755.75, 'close': 6755.75, 'volume': 0.0},
    ])
    fetch = MagicMock(return_value=stale)
    api._get_cached_chart('0001', False, True, fetch)          # 캐시 적재
    out = api._get_cached_chart('0001', False, True, fetch)    # 캐시 적중 + 오버레이

    assert float(out['close'].iloc[-1]) == pytest.approx(6117.88)   # 실시간 지수로 갱신
    assert float(out['low'].iloc[-1]) == pytest.approx(6031.38)     # 고저도 TR 실제값
    assert float(out['volume'].iloc[-1]) == pytest.approx(189905.0)
    api.clear_chart_cache()


def test_flat_today_bar_refetched_when_price_unavailable(monkeypatch):
    """오버레이 실패 + 당일 봉이 전일 종가 복제 → 캐시를 파기하고 재조회(하루 종일 0% 고착 차단)."""
    monkeypatch.setattr(api, '_chart_disk_get', lambda *a, **k: None)
    monkeypatch.setattr(api, '_chart_disk_set', lambda *a, **k: None)
    monkeypatch.setattr(api, 'market_today', lambda is_overseas=False: '20260728')
    monkeypatch.setattr(api, '_before_krx_regular_open', lambda: False)
    monkeypatch.setattr(api, '_krx_close_passed_at', lambda: None)
    monkeypatch.setattr(api, 'chart_overlay_enabled', lambda is_overseas=False: True)
    monkeypatch.setattr(api, 'get_domestic_index_price', lambda code: {'rt_cd': '9999'})
    api.clear_chart_cache()
    config.settings.CHART_CACHE_TTL_MINUTES = 180

    flat = pd.DataFrame([
        {'date': '20260727', 'open': 1, 'high': 1, 'low': 1, 'close': 6755.75, 'volume': 0.0},
        {'date': '20260728', 'open': 1, 'high': 1, 'low': 1, 'close': 6755.75, 'volume': 0.0},
    ])
    fresh = flat.copy()
    fresh.loc[1, 'close'] = 6117.88
    fetch = MagicMock(side_effect=[flat, fresh])

    api._get_cached_chart('0001', False, True, fetch)          # 캐시 적재(가짜 평봉)
    out = api._get_cached_chart('0001', False, True, fetch)    # 오버레이 실패 → 재조회

    assert fetch.call_count == 2
    assert float(out['close'].iloc[-1]) == pytest.approx(6117.88)
    api.clear_chart_cache()


@pytest.mark.real_index_chart
def test_preopen_source_placeholder_bar_dropped(monkeypatch):
    """장전 KIS 업종 일봉이 내려주는 '전일 종가 복제' 당일 행은 원본 단계에서 제거한다."""
    monkeypatch.setattr(api.config.session, 'is_toss', False, raising=False)
    monkeypatch.setattr(api, 'market_today', lambda is_overseas=False: '20260728')
    monkeypatch.setattr(api, '_before_krx_regular_open', lambda: True)
    monkeypatch.setattr(api, '_krx_close_passed_at', lambda: None)
    monkeypatch.setattr(api, 'chart_overlay_enabled', lambda is_overseas=False: False)
    api.clear_chart_cache()
    config.settings.CHART_CACHE_TTL_MINUTES = 180

    items = [
        {'stck_bsop_date': '20260728', 'bstp_nmix_prpr': '6755.75', 'bstp_nmix_oprc': '6755.75',
         'bstp_nmix_hgpr': '6755.75', 'bstp_nmix_lwpr': '6755.75', 'acml_vol': '0'},
        {'stck_bsop_date': '20260727', 'bstp_nmix_prpr': '6755.75', 'bstp_nmix_oprc': '6806.27',
         'bstp_nmix_hgpr': '6806.27', 'bstp_nmix_lwpr': '6557.39', 'acml_vol': '275718'},
        {'stck_bsop_date': '20260724', 'bstp_nmix_prpr': '6690.62', 'bstp_nmix_oprc': '7000.78',
         'bstp_nmix_hgpr': '7000.78', 'bstp_nmix_lwpr': '6650.41', 'acml_vol': '397655'},
    ]
    calls = {'n': 0}

    def fake_call_api(*a, **k):
        calls['n'] += 1
        return {'rt_cd': '0', 'output2': items if calls['n'] == 1 else []}

    monkeypatch.setattr(api, 'call_api', fake_call_api)
    out = api.get_domestic_index_chart('0001')

    assert list(out['date']) == ['20260724', '20260727']     # 장전 당일 placeholder 제거
    assert float(out['close'].iloc[-1]) != float(out['close'].iloc[-2])
    api.clear_chart_cache()
