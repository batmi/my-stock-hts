"""KRX 정규장 기준 일봉 소스(pykrx/FDR) 및 토스 일봉 경로 연결 검증.

토스 캔들은 SOR 통합값이라 NXT 장전·장후 체결이 OHLC에 섞인다(ADX 왜곡 최대 9.45 실측).
국내 일봉은 pykrx(1순위)/FDR(폴백)로 받고, 당일 봉만 실시간 현재가로 채운다.
모든 테스트는 네트워크를 타지 않도록 소스 함수를 목으로 대체한다.
"""
import time
from unittest.mock import patch

import pandas as pd
import pytest

import config
from modules import krx_daily


def _pykrx_frame(n=200, start='2025-01-02'):
    """pykrx.get_market_ohlcv 형태(한글 컬럼 + DatetimeIndex)."""
    idx = pd.bdate_range(start, periods=n)
    return pd.DataFrame({
        '시가': [10000 + i for i in range(n)],
        '고가': [10100 + i for i in range(n)],
        '저가': [9900 + i for i in range(n)],
        '종가': [10050 + i for i in range(n)],
        '거래량': [1000 + i for i in range(n)],
    }, index=idx)


def _fdr_frame(n=200, start='2025-01-02'):
    """FinanceDataReader.DataReader 형태(영문 컬럼 + DatetimeIndex)."""
    idx = pd.bdate_range(start, periods=n)
    return pd.DataFrame({
        'Open': [20000 + i for i in range(n)],
        'High': [20100 + i for i in range(n)],
        'Low': [19900 + i for i in range(n)],
        'Close': [20050 + i for i in range(n)],
        'Volume': [2000 + i for i in range(n)],
    }, index=idx)


@pytest.fixture(autouse=True)
def clean_cache():
    """실제 import·네트워크를 타지 않도록 라이브러리 슬롯만 채우고 fetch는 목으로 대체한다."""
    saved = (krx_daily._import_done, krx_daily._pykrx, krx_daily._fdr)
    krx_daily._import_done = True
    krx_daily._pykrx = object()     # is_available() True 유지 (_fetch_* 는 테스트가 patch)
    krx_daily._fdr = object()
    krx_daily.clear_cache()
    yield
    krx_daily.clear_cache()
    krx_daily._import_done, krx_daily._pykrx, krx_daily._fdr = saved


# ---------------------------------------------------------
# 정규화
# ---------------------------------------------------------
def test_normalize_pykrx_korean_columns():
    df = krx_daily._normalize(_pykrx_frame(5), 'pykrx')
    assert list(df.columns) == ['date', 'open', 'high', 'low', 'close', 'volume']
    assert df['date'].iloc[0] == '20250102'          # KIS/토스 일봉과 동일한 YYYYMMDD 문자열
    assert df['date'].is_monotonic_increasing
    assert df['close'].iloc[0] == 10050


def test_normalize_fdr_english_columns():
    df = krx_daily._normalize(_fdr_frame(5), 'FDR')
    assert list(df.columns) == ['date', 'open', 'high', 'low', 'close', 'volume']
    assert df['close'].iloc[0] == 20050


def test_normalize_drops_zero_price_rows():
    """거래정지일의 0원 봉은 지표를 망가뜨리므로 제거한다."""
    raw = _pykrx_frame(5)
    raw.iloc[2] = 0
    df = krx_daily._normalize(raw, 'pykrx')
    assert len(df) == 4
    assert (df[['open', 'high', 'low', 'close']] > 0).all().all()


def test_normalize_missing_columns_returns_none():
    assert krx_daily._normalize(pd.DataFrame({'x': [1]}), 'bad') is None
    assert krx_daily._normalize(pd.DataFrame(), 'empty') is None
    assert krx_daily._normalize(None, 'none') is None


# ---------------------------------------------------------
# 소스 선택 / 폴백
# ---------------------------------------------------------
def test_pykrx_is_preferred():
    with patch.object(krx_daily, '_fetch_pykrx', return_value=krx_daily._normalize(_pykrx_frame(), 'pykrx')), \
         patch.object(krx_daily, '_fetch_fdr') as fdr_mock:
        df = krx_daily.get_daily('005930')
    assert df.attrs['source'] == 'pykrx'
    assert df['close'].iloc[0] == 10050
    fdr_mock.assert_not_called()        # 1순위가 성공하면 폴백은 호출되지 않는다


def test_falls_back_to_fdr_when_pykrx_raises():
    with patch.object(krx_daily, '_fetch_pykrx', side_effect=RuntimeError('KRX 차단')), \
         patch.object(krx_daily, '_fetch_fdr', return_value=krx_daily._normalize(_fdr_frame(), 'FDR')):
        df = krx_daily.get_daily('005930')
    assert df is not None
    assert df.attrs['source'] == 'FDR'
    assert df['close'].iloc[0] == 20050


def test_falls_back_to_fdr_when_pykrx_returns_empty():
    with patch.object(krx_daily, '_fetch_pykrx', return_value=None), \
         patch.object(krx_daily, '_fetch_fdr', return_value=krx_daily._normalize(_fdr_frame(), 'FDR')):
        assert krx_daily.get_daily('005930').attrs['source'] == 'FDR'


def test_both_sources_fail_returns_none():
    with patch.object(krx_daily, '_fetch_pykrx', side_effect=RuntimeError), \
         patch.object(krx_daily, '_fetch_fdr', side_effect=RuntimeError):
        assert krx_daily.get_daily('005930') is None


# ---------------------------------------------------------
# 코드 검증 / 캐시 / 실패 쿨다운
# ---------------------------------------------------------
@pytest.mark.parametrize("code", ['ABC', 'AAPL', '00593', '0059300', '', None])
def test_non_domestic_codes_rejected_without_network(code):
    with patch.object(krx_daily, '_fetch_pykrx') as m:
        assert krx_daily.get_daily(code) is None
    m.assert_not_called()


def test_cache_hit_skips_refetch():
    normalized = krx_daily._normalize(_pykrx_frame(), 'pykrx')
    with patch.object(krx_daily, '_fetch_pykrx', return_value=normalized) as m:
        krx_daily.get_daily('005930')
        krx_daily.get_daily('005930')
        krx_daily.get_daily('005930')
    assert m.call_count == 1


def test_cache_expires_after_ttl():
    normalized = krx_daily._normalize(_pykrx_frame(), 'pykrx')
    with patch.object(krx_daily, '_fetch_pykrx', return_value=normalized) as m:
        krx_daily.get_daily('005930')
        with krx_daily._CACHE_LOCK:      # 6시간 경과를 시뮬레이션
            krx_daily._CACHE['005930']['ts'] -= krx_daily._cache_ttl_sec() + 1
        krx_daily.get_daily('005930')
    assert m.call_count == 2


def test_cache_invalidated_on_day_change():
    normalized = krx_daily._normalize(_pykrx_frame(), 'pykrx')
    with patch.object(krx_daily, '_fetch_pykrx', return_value=normalized) as m:
        krx_daily.get_daily('005930')
        with krx_daily._CACHE_LOCK:
            krx_daily._CACHE['005930']['day'] = '19990101'
        krx_daily.get_daily('005930')
    assert m.call_count == 2


def test_use_cache_false_forces_refetch():
    normalized = krx_daily._normalize(_pykrx_frame(), 'pykrx')
    with patch.object(krx_daily, '_fetch_pykrx', return_value=normalized) as m:
        krx_daily.get_daily('005930')
        krx_daily.get_daily('005930', use_cache=False)
    assert m.call_count == 2


def test_failure_cooldown_prevents_retry_storm():
    """조회 실패 후 쿨다운 동안은 재시도하지 않는다(매 시세 조회마다 외부 소스를 때리지 않도록)."""
    with patch.object(krx_daily, '_fetch_pykrx', side_effect=RuntimeError), \
         patch.object(krx_daily, '_fetch_fdr', side_effect=RuntimeError) as m:
        assert krx_daily.get_daily('005930') is None
        assert krx_daily.get_daily('005930') is None
        assert krx_daily.get_daily('005930') is None
    assert m.call_count == 1

    with patch.object(krx_daily, '_fetch_pykrx', return_value=krx_daily._normalize(_pykrx_frame(), 'pykrx')):
        krx_daily._FAIL['005930'] = time.time() - krx_daily._FAIL_COOLDOWN_SEC - 1
        assert krx_daily.get_daily('005930') is not None    # 쿨다운 만료 후에는 재시도


def test_success_clears_previous_failure_mark():
    krx_daily._FAIL['005930'] = time.time() - krx_daily._FAIL_COOLDOWN_SEC - 1
    with patch.object(krx_daily, '_fetch_pykrx', return_value=krx_daily._normalize(_pykrx_frame(), 'pykrx')):
        krx_daily.get_daily('005930')
    assert '005930' not in krx_daily._FAIL


def test_cache_is_copied_not_shared():
    """캐시본을 그대로 넘기면 호출부의 수정(오버레이 등)이 캐시를 오염시킨다."""
    with patch.object(krx_daily, '_fetch_pykrx', return_value=krx_daily._normalize(_pykrx_frame(), 'pykrx')):
        a = krx_daily.get_daily('005930')
        a.loc[a.index[-1], 'close'] = 1
        b = krx_daily.get_daily('005930')
    assert b['close'].iloc[-1] != 1


# ---------------------------------------------------------
# api 연결부
# ---------------------------------------------------------
def test_api_krx_chart_rejects_short_history():
    """EMA120이 안 나오는 짧은 시계열은 채택하지 않고 토스 캔들에 맡긴다."""
    import api
    short = krx_daily._normalize(_pykrx_frame(50), 'pykrx')
    with patch.object(krx_daily, 'get_daily', return_value=short):
        assert api._krx_daily_chart('005930') is None


def test_api_krx_chart_returns_tail_250_with_source_tag():
    import api
    long_df = krx_daily._normalize(_pykrx_frame(400), 'pykrx')
    long_df.attrs['source'] = 'pykrx'
    with patch.object(krx_daily, 'get_daily', return_value=long_df), \
         patch.object(api, '_append_today_bar_from_price', side_effect=lambda df, code: df):
        out = api._krx_daily_chart('005930')
    assert len(out) == 250
    assert out.attrs['source'] == 'KRX/pykrx'


def test_api_krx_chart_ignores_source_errors():
    import api
    with patch.object(krx_daily, 'get_daily', side_effect=RuntimeError('boom')):
        assert api._krx_daily_chart('005930') is None


def test_today_bar_appended_when_missing():
    """pykrx·FDR은 장중 당일 값을 주지 않는다 → 현재가로 당일 봉을 만들어 붙인다."""
    import api
    df = krx_daily._normalize(_pykrx_frame(10), 'pykrx')
    price = {'rt_cd': '0', 'output': {'stck_prpr': '70000', 'stck_oprc': '69000',
                                      'stck_hgpr': '71000', 'stck_lwpr': '68000',
                                      'acml_vol': '12345'}}
    with patch.object(api, 'market_today', return_value='20991231'), \
         patch.object(api, 'chart_overlay_enabled', return_value=True), \
         patch.object(api, 'get_current_price_data', return_value=price):
        out = api._append_today_bar_from_price(df, '005930')
    assert len(out) == len(df) + 1
    last = out.iloc[-1]
    assert last['date'] == '20991231'
    assert (last['open'], last['high'], last['low'], last['close']) == (69000, 71000, 68000, 70000)


def test_today_bar_not_duplicated_when_present():
    import api
    df = krx_daily._normalize(_pykrx_frame(10), 'pykrx')
    today = df['date'].iloc[-1]
    with patch.object(api, 'market_today', return_value=today), \
         patch.object(api, 'get_current_price_data') as m:
        out = api._append_today_bar_from_price(df, '005930')
    assert len(out) == len(df)
    m.assert_not_called()


def test_today_bar_skipped_after_all_markets_close():
    """모든 장 종료 후엔 현재가가 마지막 NXT 체결가로 굳어 있어 당일 봉으로 쓸 수 없다."""
    import api
    df = krx_daily._normalize(_pykrx_frame(10), 'pykrx')
    with patch.object(api, 'market_today', return_value='20991231'), \
         patch.object(api, 'chart_overlay_enabled', return_value=False), \
         patch.object(api, 'get_current_price_data') as m:
        assert len(api._append_today_bar_from_price(df, '005930')) == len(df)
    m.assert_not_called()


def test_today_bar_skipped_when_price_unavailable():
    import api
    df = krx_daily._normalize(_pykrx_frame(10), 'pykrx')
    with patch.object(api, 'market_today', return_value='20991231'), \
         patch.object(api, 'chart_overlay_enabled', return_value=True), \
         patch.object(api, 'get_current_price_data', return_value={'rt_cd': '1'}):
        assert len(api._append_today_bar_from_price(df, '005930')) == len(df)


def test_today_bar_high_low_respect_current_price():
    """현재가가 API의 고가/저가 범위를 벗어나도 봉이 현재가를 포함하도록 넓힌다."""
    import api
    df = krx_daily._normalize(_pykrx_frame(10), 'pykrx')
    price = {'rt_cd': '0', 'output': {'stck_prpr': '80000', 'stck_oprc': '69000',
                                      'stck_hgpr': '71000', 'stck_lwpr': '68000'}}
    with patch.object(api, 'market_today', return_value='20991231'), \
         patch.object(api, 'chart_overlay_enabled', return_value=True), \
         patch.object(api, 'get_current_price_data', return_value=price):
        last = api._append_today_bar_from_price(df, '005930').iloc[-1]
    assert last['high'] == 80000
    assert last['low'] == 68000


# ---------------------------------------------------------
# 폴백 경고 (토스 캔들 = NXT 포함으로 계산됨을 사용자에게 알림)
# ---------------------------------------------------------
@pytest.fixture(autouse=True)
def clean_fallback():
    import api
    api.clear_krx_fallback()
    yield
    api.clear_krx_fallback()


def test_fallback_recorded_when_sources_fail():
    import api
    with patch.object(krx_daily, 'get_daily', return_value=None):
        assert api._krx_daily_chart('005930') is None
    assert '005930' in api.get_krx_fallback()


def test_fallback_recorded_when_history_too_short():
    """120봉 미만도 토스 캔들로 넘어가므로 경고 대상이다."""
    import api
    short = krx_daily._normalize(_pykrx_frame(50), 'pykrx')
    with patch.object(krx_daily, 'get_daily', return_value=short):
        assert api._krx_daily_chart('005930') is None
    assert '005930' in api.get_krx_fallback()
    assert '50봉' in api.get_krx_fallback()['005930']


def test_fallback_recorded_on_exception():
    import api
    with patch.object(krx_daily, 'get_daily', side_effect=RuntimeError('boom')):
        api._krx_daily_chart('005930')
    assert '005930' in api.get_krx_fallback()


def test_fallback_cleared_on_recovery():
    """다음 조회에서 KRX 소스가 살아나면 경고 목록에서 빠진다."""
    import api
    api.note_krx_fallback('005930', 'pykrx·FDR 모두 실패')
    good = krx_daily._normalize(_pykrx_frame(200), 'pykrx')
    with patch.object(krx_daily, 'get_daily', return_value=good), \
         patch.object(api, '_append_today_bar_from_price', side_effect=lambda df, code: df):
        assert api._krx_daily_chart('005930') is not None
    assert '005930' not in api.get_krx_fallback()


def test_warning_silent_when_no_fallback():
    import api
    import utils
    api.clear_krx_fallback()
    with patch.object(config.console, 'print') as p:
        utils.print_krx_fallback_warning()
    p.assert_not_called()


def test_warning_lists_affected_symbols_with_names():
    import api
    import utils
    api.note_krx_fallback('005930', 'pykrx·FDR 모두 실패')
    with patch.object(config.console, 'print') as p:
        utils.print_krx_fallback_warning({'005930': '삼성전자'})
    out = ' '.join(str(c.args[0]) for c in p.call_args_list if c.args)
    assert 'yellow' in out                 # 노란색으로 출력
    assert '삼성전자(005930)' in out
    assert 'NXT' in out                    # 왜 신뢰할 수 없는지 명시


def test_warning_truncates_long_symbol_list():
    import api
    import utils
    for i in range(12):
        api.note_krx_fallback(f'00000{i}', '실패')
    with patch.object(config.console, 'print') as p:
        utils.print_krx_fallback_warning()
    out = ' '.join(str(c.args[0]) for c in p.call_args_list if c.args)
    assert '외 4종목' in out               # 8개까지만 나열하고 나머지는 요약


def test_warning_survives_api_error():
    """경고 렌더링 실패가 결과 출력 자체를 막지 않는다."""
    import api
    import utils
    with patch.object(api, 'get_krx_fallback', side_effect=RuntimeError('boom')):
        utils.print_krx_fallback_warning()   # 예외가 새어나오지 않아야 한다


# ---------------------------------------------------------
# 토스 일봉 경로 우선순위
# ---------------------------------------------------------
def test_toss_daily_prefers_krx_for_domestic():
    import api
    krx_df = krx_daily._normalize(_pykrx_frame(200), 'pykrx')
    with patch.object(api, '_krx_daily_chart', return_value=krx_df) as krx_mock, \
         patch.object(api, '_toss_chart_data') as toss_mock:
        out = api._toss_daily_chart_with_tv_fallback('005930', is_overseas=False)
    assert out is krx_df
    krx_mock.assert_called_once()
    toss_mock.assert_not_called()       # KRX 성공 시 토스 캔들 페이지네이션을 아예 타지 않는다


def test_toss_daily_falls_back_to_toss_when_krx_fails():
    import api
    toss_df = krx_daily._normalize(_pykrx_frame(200), 'pykrx')
    with patch.object(api, '_krx_daily_chart', return_value=None), \
         patch.object(api, '_toss_chart_data', return_value=toss_df) as toss_mock:
        out = api._toss_daily_chart_with_tv_fallback('005930', is_overseas=False)
    assert out is toss_df
    toss_mock.assert_called_once()


def test_toss_cache_namespace_bumped_so_old_basis_is_not_reused():
    """일봉 '기준'이 바뀌면 캐시 네임스페이스를 올려야 한다.

    올리지 않으면 코드를 고쳐도 당일 디스크 캐시(구 기준 데이터)가 그대로 반환되어
    수정 전 값이 계속 보인다 — 실제로 T2를 유지한 채 배포해 겪은 회귀다.
    """
    import api
    config.session.is_toss = True
    try:
        key = api._chart_cache_key('005930', False, False)
        assert key.startswith('T3_'), f"토스 네임스페이스가 되돌아갔다: {key}"

        stale = krx_daily._normalize(_pykrx_frame(200), 'pykrx')
        stale.loc[stale.index[-1], 'close'] = 111111        # 구 기준(토스) 캐시를 흉내
        fresh = krx_daily._normalize(_pykrx_frame(200), 'pykrx')
        fresh.loc[fresh.index[-1], 'close'] = 222222        # 신 기준(KRX) 데이터

        with patch.object(api, '_CHART_CACHE', {'T2_005930_False_False': {
                'df': stale, 'timestamp': pd.Timestamp.now().to_pydatetime(),
                'date': pd.Timestamp.now().strftime('%Y-%m-%d')}}), \
             patch.object(api, '_chart_disk_get', return_value=None), \
             patch.object(api, '_chart_disk_set'), \
             patch.object(api, '_krx_daily_chart', return_value=fresh):
            out = api.get_chart_data('005930', is_overseas=False, period_type='daily')
    finally:
        config.session.is_toss = False

    assert out.iloc[-1]['close'] == 222222, "구 네임스페이스 캐시가 재사용됐다"


# ---------------------------------------------------------
# 기준가(전일 KRX 종가) 3순위 소스 — yfinance → pykrx/FDR 교체
# ---------------------------------------------------------
def test_base_price_source_prefers_krx_lib_over_yfinance():
    """yfinance는 특정일 공식 종가와 어긋나므로(237일 중 2~4일, 최대 1.59%) 후순위여야 한다."""
    import api
    df = krx_daily._normalize(_pykrx_frame(200), 'pykrx')
    ref = df['date'].iloc[-1]
    with patch.object(krx_daily, 'get_daily', return_value=df), \
         patch.object(api, '_toss_krx_close_put') as put:
        got = api._toss_krx_lib_close('005930', ref)
    assert got == float(df['close'].iloc[-1])
    # 검증값으로 저장돼 다음부터 2순위(신뢰 조회)에서 종료된다
    put.assert_called_once_with('005930', ref, got, source='krx')


def test_base_price_source_returns_none_for_missing_date():
    import api
    df = krx_daily._normalize(_pykrx_frame(200), 'pykrx')
    with patch.object(krx_daily, 'get_daily', return_value=df):
        assert api._toss_krx_lib_close('005930', '19990101') is None


def test_base_price_source_survives_library_failure():
    """KRX 소스가 죽어도 예외를 밖으로 내지 않고 None을 반환해 yfinance 폴백으로 넘어간다."""
    import api
    with patch.object(krx_daily, 'get_daily', side_effect=RuntimeError('boom')):
        assert api._toss_krx_lib_close('005930', '20260724') is None


def test_krx_source_is_trusted_for_stored_close():
    import api
    assert api._toss_krx_close_trusted('005930', 'krx') is True
    assert api._toss_krx_close_trusted('005930', 'yf') is True


def test_stored_close_never_regresses_to_less_accurate_source():
    """정확도 순위 krx > yf > cap — 낮은 출처가 높은 출처를 덮어쓰지 못한다."""
    import api
    rank = api._TOSS_CLOSE_SOURCE_RANK
    assert rank['krx'] > rank['yf'] > rank['cap']


def test_yfinance_stored_value_is_upgraded_by_krx():
    """이미 'yf'로 저장된 부정확한 값은 'krx'가 덮어써 자동 교정된다."""
    import api
    store = {}

    def fake_load():
        return store

    with patch.object(api, '_toss_krx_close_load_locked', side_effect=fake_load), \
         patch.object(api, '_toss_krx_close_path', return_value='/dev/null'), \
         patch('builtins.open', create=True), patch('os.replace'):
        api._toss_krx_close_put('005930', '20260724', 249100.0, source='yf')
        assert store['005930']['20260724']['s'] == 'yf'
        api._toss_krx_close_put('005930', '20260724', 249500.0, source='krx')
        assert store['005930']['20260724'] == {'c': 249500.0, 's': 'krx'}
        # 반대 방향(퇴행)은 막힌다
        api._toss_krx_close_put('005930', '20260724', 111111.0, source='yf')
        api._toss_krx_close_put('005930', '20260724', 222222.0, source='cap')
        assert store['005930']['20260724'] == {'c': 249500.0, 's': 'krx'}


def test_yfinance_numpy_warning_filter_outranks_yfinance_own_filter():
    """yfinance가 import 시 자기 경고를 강제 노출하므로, 우리 필터가 그보다 앞에 있어야 한다.

    yfinance/__init__.py:45
        warnings.filterwarnings('default', category=DeprecationWarning, module='^yfinance')
    는 파이썬 기본값(ignore::DeprecationWarning)을 뒤집는다. warnings 필터는 나중에 등록된
    것이 앞에 놓여 먼저 매칭되므로, config import 시점에만 걸면 yfinance 로드 후 무력화된다.
    → yfinance를 import하는 모듈들이 import 직후 재등록하는지(순서)를 검증한다.
    """
    import warnings

    msg = "The 'generic' unit for NumPy timedelta is deprecated, and will raise an error"

    def _emit_as_yfinance():
        """실제와 동일하게 'yfinance.utils' 모듈에서 발생한 경고로 만든다.

        warnings.warn()은 호출한 쪽(테스트 모듈) 기준으로 필터가 매칭되므로,
        yfinance 자체 필터(module='^yfinance')가 아예 적용되지 않아 재현이 안 된다.
        """
        warnings.warn_explicit(msg, DeprecationWarning,
                               'yfinance/utils.py', 667, module='yfinance.utils', registry={})

    # (1) config import 시점에만 건 경우 = yfinance 필터가 나중에 등록되어 앞을 차지 → 경고가 샌다
    with warnings.catch_warnings(record=True) as leaked:
        warnings.resetwarnings()
        config.silence_yfinance_numpy_warning()                       # 먼저
        warnings.filterwarnings('default', category=DeprecationWarning, module='^yfinance')
        _emit_as_yfinance()
    assert len(leaked) == 1, "이 순서에서는 경고가 새는 것이 정상 — 전제가 깨졌다면 재검토 필요"

    # (2) yfinance import '뒤'에 재등록한 경우 → 억제된다 (현재 구현)
    with warnings.catch_warnings(record=True) as blocked:
        warnings.resetwarnings()
        warnings.filterwarnings('default', category=DeprecationWarning, module='^yfinance')
        config.silence_yfinance_numpy_warning()                       # 나중
        _emit_as_yfinance()
    assert len(blocked) == 0, "yfinance 필터보다 뒤에 등록했는데도 억제되지 않았다"


def test_yfinance_importers_reregister_the_filter():
    """yfinance를 import하는 모듈은 import 직후 억제 필터를 재등록해야 한다."""
    import pathlib
    import re

    root = pathlib.Path(__file__).resolve().parent.parent
    targets = ['api.py', 'utils.py', 'modules/market.py',
               'modules/analysis.py', 'modules/manage/events.py']
    for rel in targets:
        src = (root / rel).read_text(encoding='utf-8')
        if not re.search(r'^\s*import yfinance as yf', src, re.M):
            continue
        assert 'silence_yfinance_numpy_warning()' in src, (
            f"{rel}: yfinance를 import하면서 config.silence_yfinance_numpy_warning() 재등록이 없다 "
            "— yfinance 자체 필터가 앞을 차지해 경고가 다시 출력된다")


def test_only_target_message_is_suppressed():
    """대상 메시지만 막고 다른 DeprecationWarning은 그대로 통과시킨다."""
    import warnings
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter('always')
        config.silence_yfinance_numpy_warning()
        warnings.warn("The 'generic' unit for NumPy timedelta is deprecated, and will raise",
                      DeprecationWarning)
        warnings.warn('unrelated deprecation', DeprecationWarning)
    assert [str(w.message) for w in caught] == ['unrelated deprecation']


def test_toss_daily_overseas_never_uses_krx():
    import api
    toss_df = krx_daily._normalize(_fdr_frame(200), 'FDR')
    with patch.object(api, '_krx_daily_chart') as krx_mock, \
         patch.object(api, '_toss_chart_data', return_value=toss_df):
        api._toss_daily_chart_with_tv_fallback('AAPL', is_overseas=True)
    krx_mock.assert_not_called()


# ---------------------------------------------------------
# 표 출력 경로의 입력 형태 (회귀: print_table의 data_list는 (이름, 코드) 튜플이다)
# ---------------------------------------------------------
def test_name_map_accepts_tuple_data_list():
    """analysis.print_table의 data_list는 dict가 아니라 (종목명, 종목코드) 튜플 리스트다.

    [회귀] dict로 가정해 d.get(...)을 쓰는 바람에 국내 종목 분석에서
    "테이블 출력 실패: 'tuple' object has no attribute 'get'"로 표가 통째로 안 나왔다.
    """
    from modules import analysis
    got = analysis._name_map_from([('삼성전자', '005930'), ('SK하이닉스', '000660')])
    assert got == {'005930': '삼성전자', '000660': 'SK하이닉스'}


def test_name_map_accepts_dict_and_list_shapes():
    from modules import analysis
    assert analysis._name_map_from([{'code': '005930', 'name': '삼성전자'}]) == {'005930': '삼성전자'}
    assert analysis._name_map_from([['카카오', '035720']]) == {'035720': '카카오'}


@pytest.mark.parametrize("bad", [None, [], [42], [None], [('짧음',)], ['문자열'], [()]])
def test_name_map_never_raises_on_bad_input(bad):
    """경고 문구용 부가 정보라 여기서 예외가 나면 표 자체가 죽는다 — 절대 던지지 않는다."""
    from modules import analysis
    assert isinstance(analysis._name_map_from(bad), dict)
