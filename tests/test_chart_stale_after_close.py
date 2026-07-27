"""장 마감 후 '전일 종가가 오늘 값으로 보이던' 캐시 staleness 회귀 테스트.

증상(2026-07-27(월) 22:40 실측, mode 1/2/3 공통):
    삼성전자 249,500 -20,500(-7.59%)  ← 7/24(금) 확정 종가·등락
    실제 7/27 종가는 254,000 (+1.80%). EMA·RSI·CCI·52주 위치도 하루 밀렸다.

원인: 디스크 차트 캐시(data/chart_cache.db)가 달력일(trade_date)만 검사하고 저장 시각을
보지 않았다. 자정 직후(00:54)에 저장된 '어제까지의 일봉'이 그날 밤까지 재사용됐고,
복원할 때 메모리 캐시 timestamp를 now로 다시 찍어 6시간 TTL도 만료되지 않았다.
장중에는 실시간 오버레이가 당일 봉을 채워 가려지지만, 모든 장이 끝난 뒤(20:00~)에는
오버레이가 꺼져 마지막 봉(=직전 거래일)이 그대로 '현재가'로 표시된다.
"""
import os
import sys
from datetime import datetime, timedelta
from unittest.mock import MagicMock, patch

import numpy as np
import pandas as pd
import pytest

sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import api
import config
from modules import analysis, krx_daily

TRADING_DAY = '20260727'        # 월요일(거래일)
PREV_DAY = '20260724'           # 직전 거래일(금요일)


def _at(hh, mm):
    return datetime(2026, 7, 27, hh, mm)


@pytest.fixture
def trading_day(monkeypatch):
    """오늘을 거래일(2026-07-27 월)로 고정한다."""
    monkeypatch.setattr(api, 'market_today', lambda is_overseas=False: TRADING_DAY)


def _freeze_now(monkeypatch, when):
    """api 모듈이 보는 시계(datetime.now / time.time)를 고정한다(다른 모듈 영향 없음)."""
    import time as _time

    class _DT(datetime):
        @classmethod
        def now(cls, tz=None):
            return when

    class _Clock:
        """api 모듈 전용 time 프록시 — time()만 고정하고 나머지는 원본에 위임."""
        def __getattr__(self, name):
            return getattr(_time, name)

        def time(self):
            return when.timestamp()

    monkeypatch.setattr(api, 'datetime', _DT)
    monkeypatch.setattr(api, 'time', _Clock())


# ---------------------------------------------------------
# 1. 마감 기준선 판정
# ---------------------------------------------------------
def test_close_line_none_before_settlement(monkeypatch, trading_day):
    """마감(+확정 여유) 전에는 기준선이 없다 — 캐시를 파기할 이유가 없다."""
    _freeze_now(monkeypatch, _at(15, 39))
    assert api._krx_close_passed_at() is None


def test_close_line_set_after_settlement(monkeypatch, trading_day):
    """마감 후에는 그 시각이 기준선이 된다."""
    _freeze_now(monkeypatch, _at(22, 40))
    assert api._krx_close_passed_at() == _at(15, 40)


def test_close_line_none_on_holiday(monkeypatch):
    """휴장일은 새로 마감된 세션이 없으므로 기준선 없음(불필요한 재조회 방지)."""
    monkeypatch.setattr(api, 'market_today', lambda is_overseas=False: PREV_DAY)
    _freeze_now(monkeypatch, _at(22, 40))
    assert api._krx_close_passed_at() is None


# ---------------------------------------------------------
# 2. 디스크 캐시 — 마감 전에 저장된 항목은 마감 후 재사용 금지
# ---------------------------------------------------------
@pytest.fixture
def disk_db(monkeypatch, tmp_path):
    monkeypatch.setattr(api, '_chart_disk_path', lambda: str(tmp_path / 'chart_cache.db'))
    monkeypatch.setattr(config, 'CHART_DISK_CACHE', True, raising=False)
    monkeypatch.setattr(config, 'CHART_CACHE_TTL_MINUTES', 360, raising=False)
    return tmp_path


def _put_disk(cache_key, last_date, saved_at, today_str):
    """saved_at 시각에 저장된 것으로 디스크 캐시에 넣는다."""
    df = pd.DataFrame([{'date': last_date, 'open': 1, 'high': 1, 'low': 1, 'close': 1, 'volume': 1}])
    api._chart_disk_set(cache_key, df, today_str)
    import sqlite3
    from contextlib import closing
    with closing(sqlite3.connect(api._chart_disk_path())) as conn, conn:
        conn.execute("UPDATE chart_cache SET ts=? WHERE cache_key=?",
                     (saved_at.timestamp(), cache_key))


def test_disk_cache_rejects_entry_saved_before_close(monkeypatch, trading_day, disk_db):
    """자정 직후(00:54) 저장된 '어제까지의 일봉'은 마감 후 재사용되지 않는다(실측 재현)."""
    today_str = '2026-07-27'
    _put_disk('K_005930_False_False', PREV_DAY, _at(0, 54), today_str)

    _freeze_now(monkeypatch, _at(22, 40))
    assert api._chart_disk_get('K_005930_False_False', today_str) is None


def test_disk_cache_keeps_entry_saved_after_close(monkeypatch, trading_day, disk_db):
    """마감 후에 저장된 항목(당일 확정 종가 포함)은 그대로 재사용한다."""
    today_str = '2026-07-27'
    _put_disk('K_005930_False_False', TRADING_DAY, _at(20, 57), today_str)

    _freeze_now(monkeypatch, _at(22, 40))
    df = api._chart_disk_get('K_005930_False_False', today_str)
    assert df is not None and str(df.iloc[-1]['date']) == TRADING_DAY


def test_disk_cache_honors_ttl(monkeypatch, trading_day, disk_db):
    """TTL(6시간)이 지난 항목은 같은 달력일이라도 재사용하지 않는다.
    (종전엔 복원 시 메모리 timestamp를 now로 다시 찍어 TTL이 영영 만료되지 않았다)"""
    today_str = '2026-07-27'
    _put_disk('K_005930_False_False', TRADING_DAY, _at(16, 0), today_str)

    _freeze_now(monkeypatch, _at(23, 30))      # 저장 후 7.5시간
    assert api._chart_disk_get('K_005930_False_False', today_str) is None


def test_disk_cache_overseas_ignores_krx_close(monkeypatch, trading_day, disk_db):
    """해외 일봉에는 KRX 마감 기준선을 적용하지 않는다(TTL만 적용).

    같은 항목(15:00 저장 / 20:00 조회 = TTL 이내, KRX 마감 이전)을 두 기준으로 조회해
    국내만 거부되는지 확인한다.
    """
    today_str = '2026-07-27'
    _put_disk('K_AAPL_True_False', TRADING_DAY, _at(15, 0), today_str)
    _freeze_now(monkeypatch, _at(20, 0))

    assert api._chart_disk_get('K_AAPL_True_False', today_str, is_overseas=True) is not None
    assert api._chart_disk_get('K_AAPL_True_False', today_str, is_overseas=False) is None


# ---------------------------------------------------------
# 3. 메모리 캐시 — 마감 전 캐시는 TTL 이내라도 파기하고 재조회
# ---------------------------------------------------------
def test_memory_cache_purged_after_close(monkeypatch, trading_day):
    """마감 전에 만들어진 메모리 캐시는 TTL(6시간) 안이라도 파기 후 재조회한다."""
    monkeypatch.setattr(api, '_chart_disk_get', lambda *a, **k: None)
    monkeypatch.setattr(api, '_chart_disk_set', lambda *a, **k: None)
    monkeypatch.setattr(config, 'CHART_CACHE_TTL_MINUTES', 360, raising=False)
    monkeypatch.setattr(api, 'chart_overlay_enabled', lambda is_overseas=False: False)
    key = api._chart_cache_key('005930', False, False)
    stale = pd.DataFrame([{'date': PREV_DAY, 'open': 1, 'high': 1, 'low': 1, 'close': 249500, 'volume': 1}])
    fresh = pd.DataFrame([{'date': TRADING_DAY, 'open': 1, 'high': 1, 'low': 1, 'close': 254000, 'volume': 1}])

    # 15:00 저장 / 19:30 조회 = TTL(6시간) 이내 — 기존 TTL이 아니라 마감 기준선으로 파기돼야 한다
    with api._CHART_CACHE_LOCK:
        api._CHART_CACHE[key] = {'df': stale, 'timestamp': _at(15, 0), 'date': '2026-07-27'}

    _freeze_now(monkeypatch, _at(19, 30))
    fetch = MagicMock(return_value=fresh)
    try:
        out = api._get_cached_chart('005930', False, False, fetch)
    finally:
        with api._CHART_CACHE_LOCK:
            api._CHART_CACHE.pop(key, None)

    fetch.assert_called_once()
    assert out.iloc[-1]['close'] == 254000


def test_memory_cache_kept_before_close(monkeypatch, trading_day):
    """마감 전(장중)에는 종전대로 캐시를 유지한다 — 재조회 폭주 방지."""
    monkeypatch.setattr(api, '_chart_disk_get', lambda *a, **k: None)
    monkeypatch.setattr(api, '_chart_disk_set', lambda *a, **k: None)
    monkeypatch.setattr(config, 'CHART_CACHE_TTL_MINUTES', 360, raising=False)
    monkeypatch.setattr(api, 'chart_overlay_enabled', lambda is_overseas=False: False)
    key = api._chart_cache_key('005930', False, False)
    cached = pd.DataFrame([{'date': PREV_DAY, 'open': 1, 'high': 1, 'low': 1, 'close': 249500, 'volume': 1}])

    with api._CHART_CACHE_LOCK:
        api._CHART_CACHE[key] = {'df': cached, 'timestamp': _at(9, 30), 'date': '2026-07-27'}

    _freeze_now(monkeypatch, _at(13, 0))
    fetch = MagicMock()
    try:
        api._get_cached_chart('005930', False, False, fetch)
    finally:
        with api._CHART_CACHE_LOCK:
            api._CHART_CACHE.pop(key, None)

    fetch.assert_not_called()


# ---------------------------------------------------------
# 4. 토스 모드(KRX 일봉 캐시)도 같은 기준으로 만료
# ---------------------------------------------------------
def test_krx_daily_cache_expires_at_close(monkeypatch):
    """krx_daily(pykrx/FDR) 캐시도 마감 전 저장분은 마감 후 재조회한다."""
    import time as _time
    monkeypatch.setattr(krx_daily, 'is_available', lambda: True)
    monkeypatch.setattr(krx_daily, '_session_settled_ts', lambda: _at(15, 40).timestamp())
    df = pd.DataFrame({'date': [PREV_DAY], 'open': [1.0], 'high': [1.0],
                       'low': [1.0], 'close': [1.0], 'volume': [1.0]})
    krx_daily.clear_cache()
    with krx_daily._CACHE_LOCK:
        krx_daily._CACHE['005930'] = {'df': df, 'ts': _at(10, 0).timestamp(),
                                      'day': datetime.now().strftime('%Y%m%d'), 'lookback': 400}

    fresh = pd.DataFrame({'date': [TRADING_DAY], 'open': [1.0], 'high': [1.0],
                          'low': [1.0], 'close': [2.0], 'volume': [1.0]})
    monkeypatch.setattr(krx_daily, '_fetch_pykrx', lambda c, s, e: fresh)
    monkeypatch.setattr(krx_daily, '_fetch_fdr', lambda c, s, e: None)

    try:
        out = krx_daily.get_daily('005930')
    finally:
        krx_daily.clear_cache()

    assert str(out.iloc[-1]['date']) == TRADING_DAY, "마감 전 캐시가 재사용됐다"


# ---------------------------------------------------------
# 5. 표시 경로 방어 — 하루 밀린 차트를 '현재가'로 쓰지 않는다
# ---------------------------------------------------------
def _stale_chart_df(periods=260):
    """마지막 봉이 직전 거래일(PREV_DAY)에서 멈춘 차트."""
    end = pd.Timestamp(PREV_DAY)
    dates = pd.date_range(end=end, periods=periods).strftime('%Y%m%d')
    close = np.linspace(200000.0, 249500.0, periods)
    return pd.DataFrame({
        'date': dates, 'open': close * 0.995, 'high': close * 1.01,
        'low': close * 0.99, 'close': close,
        'volume': np.full(periods, 200000.0),
    })


def test_table_row_ignores_stale_chart_close(monkeypatch):
    """마지막 봉이 하루 밀린 차트는 확정 종가로 쓰지 않고 실시간 시세로 표시한다."""
    monkeypatch.setattr(analysis.api, 'chart_overlay_enabled', lambda is_overseas=False: False)
    monkeypatch.setattr(analysis.api, 'chart_overlay_price', lambda p, o=False: 0.0)
    monkeypatch.setattr(analysis.utils, 'market_today', lambda is_overseas=False: TRADING_DAY)

    bundle = {
        'curr_data': {'rt_cd': '0', 'output': {
            'stck_prpr': '254000', 'stck_sdpr': '249500',
            'prdy_vrss': '4500', 'prdy_ctrt': '1.80', 'rprs_mrkt_kor_name': '코스피',
        }},
        'chart_df': _stale_chart_df(), 'inv_list': None,
        'rt_strength': 110.0, 'ask_bid_ratio': None, 'detail': None,
    }

    with patch('modules.analysis.check_smart_money_turnaround', return_value=(False, "")):
        result = analysis._analyze_table_row(
            ('삼성전자', '005930'), '국내 주식 기술적 분석', False, False,
            set(), {}, {}, set(), set(), bundle
        )

    assert result is not None
    row = result[0]
    assert '254,000' in row[3], f"현재가가 하루 밀린 차트 종가로 표시됐다: {row[3]}"
    assert '+1.80%' in row[4], f"등락률이 직전 거래일 값으로 표시됐다: {row[4]}"
