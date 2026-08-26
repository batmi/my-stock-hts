"""지수 소스 선정 로직 동기화 회귀 테스트.

[왜 묻는가] 지수 화면·개별 지수 분석(메인 1)은 국내 지수를 모드별 소스 체인
(KIS/토스/tvDatafeed/yfinance)으로, 미국채 현물·HY OAS를 tvDatafeed 전용 소스로 조회한다.
그런데 차트 분석(메인 3-5)은 지수 목록의 yfinance 티커를 그대로 넘겨,
  - 코스피200·코스닥150이 모드별 소스를 타지 못하고(표와 차트 값이 어긋남)
  - 자리표시자 티커(^VKOSPI·^K200FUT·^US02Y·^HYOAS)는 조회 자체가 실패했다.
소스 선정을 market.resolve_index_source / api.index_source_kind 한 곳으로 모으고,
양쪽 진입점이 같은 규칙을 쓰는지 검증한다.
"""
import os
import sys
import pandas as pd
import pytest
from unittest.mock import patch

sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import api
import config
from modules import analysis, market


def _src_df(periods=120):
    """지수 전용 소스(analysis 계열)의 반환 스키마 — date는 datetime."""
    return pd.DataFrame({
        'date': pd.date_range(end=pd.Timestamp.today().normalize(), periods=periods),
        'open': range(100, 100 + periods),
        'high': range(101, 101 + periods),
        'low': range(99, 99 + periods),
        'close': range(100, 100 + periods),
        'volume': [0] * periods,
    })


# ---------------------------------------------------------------- 소스 선정 규칙

@pytest.mark.parametrize("name,ticker,expected_code", [
    ("코스피", "^KS11", "KOSPI"),
    ("코스피200", "^KS200", "KOSPI200"),
    ("코스닥", "^KQ11", "KOSDAQ"),
    ("코스닥150", "^KQ150", "KOSDAQ150"),
    ("V코스피200", "^VKOSPI", "VKOSPI"),
])
def test_국내지수는_내부코드로_치환된다(name, ticker, expected_code):
    code, is_overseas = market.resolve_index_source(name, ticker)
    assert code == expected_code
    assert is_overseas is False
    assert api.index_source_kind(code) == 'domestic'


def test_코스피200선물은_표시세션에_맞는_KIS코드로_치환된다():
    with patch.object(market, '_k200_night_session', return_value=False):
        assert market.resolve_index_source("코스피200선물", "^K200FUT") == ("K200FUT_F", False)
    with patch.object(market, '_k200_night_session', return_value=True):
        assert market.resolve_index_source("코스피200선물", "^K200FUT") == ("K200FUT_CM", False)


def test_해외지수는_목록티커를_그대로_쓴다():
    assert market.resolve_index_source("나스닥", "^IXIC") == ("^IXIC", True)
    assert api.index_source_kind("^IXIC") is None


def test_미국채_현물과_HYOAS는_전용소스로_판정된다():
    # 이름이 아니라 티커로 넘어와도 지수 화면과 같은 소스를 골라야 한다.
    for ticker in config.US_TREASURY_SPOT_TICKERS:
        assert api.index_source_kind(ticker) == 'tv_spot'
    assert api.index_source_kind("^HYOAS") == 'fred'


def test_미국채_티커맵은_지수목록과_정합적이다():
    """config의 티커맵과 지수 목록(이름→티커)이 어긋나면 차트만 다른 만기를 그린다."""
    for name, symbol in config.US_TREASURY_SPOT_SYMBOLS.items():
        ticker = market.INDICES_MAP[name]
        assert config.US_TREASURY_SPOT_TICKERS.get(ticker) == symbol


# ---------------------------------------------------------------- 목록 노출 정책

def test_KIS실전전용_지수는_토스모드_목록에서_빠진다():
    """토스는 대체 소스가 없다 — 조회 못 하는 행을 보여 주지 않기 위해서다."""
    config.session.is_toss = False
    assert [n for n, _ in market.selectable_indices()] == [n for n, _ in market.ALL_INDICES]

    config.session.is_toss = True
    try:
        names = [n for n, _ in market.selectable_indices()]
        assert "V코스피200" not in names
        assert "코스피200선물" not in names
        assert "코스피200" in names  # 대체 소스가 있는 지수는 남는다
    finally:
        config.session.is_toss = False


def test_V코스피200은_KRX로_대신하지_않는다():
    """KRX 는 확정 봉만 준다 — 변동성지수를 묵은 값으로 보는 것은 안 보는 것보다 낫지 않다.

    2026-08-25~08-26 사이 잠깐 모드 3 에 열려 있었다. 값 자체는 KIS 0503 과 일치하지만
    장중에는 직전 확정치라, 하필 변동성이 튀는 순간에 화면만 조용하다.
    """
    config.session.is_toss = True
    try:
        assert "V코스피200" in market.blocked_kis_only_indices()
        assert "V코스피200" not in [n for n, _ in market.selectable_indices()]
    finally:
        config.session.is_toss = False
    # 모드 2(실전)에서는 KIS 0503 실시간이라 그대로 보인다
    assert "V코스피200" in [n for n, _ in market.selectable_indices()]


def test_코스피200선물은_토스모드에서_빠진다():
    """KRX도 확정 봉만 주는데 선물은 세션이 하루를 덮어 장중 내내 최대 하루 묵는다.
    야간장 01:47 실측 KIS 1,034.85 vs KRX 확정 1,074.55 — 부정확한 값은 보여 주지 않는다."""
    config.session.is_toss = True
    try:
        assert "코스피200선물" not in [n for n, _ in market.selectable_indices()]
    finally:
        config.session.is_toss = False
    # 모드 2(실전)에서는 그대로 보인다
    assert "코스피200선물" in [n for n, _ in market.selectable_indices()]


def test_토스모드_V코스피200_조회는_KRX로_폴백하지_않는다():
    """목록에서 빠졌더라도 조회 경로가 열려 있으면 묵은 값이 다른 화면으로 샌다."""
    config.session.is_toss = True
    try:
        with patch.object(analysis, '_fetch_index_via_krx') as krx:
            assert analysis._fetch_domestic_index_data("VKOSPI") is None
        krx.assert_not_called()
    finally:
        config.session.is_toss = False


def test_텔레그램도_같은_제외목록을_쓴다():
    """이름을 따로 적으면 화면과 텔레그램이 서로 다른 지수 집합을 보게 된다."""
    import inspect
    from modules import telegram_bot
    src = inspect.getsource(telegram_bot.TelegramCommander._get_market_status)
    assert 'blocked_kis_only_indices' in src
    assert '"V코스피200"' not in src


def test_토스모드_선물조회는_KRX로_폴백하지_않는다():
    """폴백해 봐야 묵은 값이라 오해만 부른다 — None 이어야 한다."""
    config.session.is_toss = True
    try:
        with patch.object(analysis, '_fetch_index_via_krx') as krx:
            assert analysis._fetch_domestic_index_data("K200FUT_CM") is None
        krx.assert_not_called()
    finally:
        config.session.is_toss = False


def test_목록과_워커의_판정이_같은_함수를_쓴다():
    """목록에는 있는데 워커가 건너뛰면 화면에 빈 행이 남는다 — 두 곳이 갈라지면 안 된다."""
    config.session.is_toss = True
    try:
        with patch.object(market, 'blocked_kis_only_indices', return_value=()) as gate, \
             patch.object(analysis, 'get_domestic_index_data', return_value=_src_df()), \
             patch.object(market, 'is_market_open_for_index', return_value=False):
            res = market._process_index_worker("V코스피200", "^VKOSPI",
                                               pd.DataFrame(), pd.DataFrame())
        assert res['status'] != 'skipped'
        assert gate.called
    finally:
        config.session.is_toss = False


# ---------------------------------------------------------------- 차트 데이터 조회

def test_국내지수_차트는_지수화면과_같은_소스를_탄다():
    src = _src_df()
    with patch.object(analysis, 'get_domestic_index_data', return_value=src) as m:
        df = api.get_chart_data("KOSPI200", is_overseas=False, period_type='daily')
    m.assert_called_once_with("KOSPI200")
    assert not df.empty
    assert list(df.columns) == ['date', 'open', 'high', 'low', 'close', 'volume']
    assert isinstance(df['date'].iloc[-1], str) and len(df['date'].iloc[-1]) == 8
    # 공유 캐시 객체를 그대로 변형하면 지수 화면이 깨진다 → 원본 보존 확인
    assert isinstance(src['date'].iloc[0], pd.Timestamp)


def test_국내지수_주봉은_일봉_리샘플링으로_제공된다():
    with patch.object(analysis, 'get_domestic_index_data', return_value=_src_df()):
        w = api.get_chart_data("KOSPI200", is_overseas=False, period_type='weekly')
    assert not w.empty
    assert len(w) < 120  # 일봉보다 적은 주봉 수
    assert list(w.columns) == ['date', 'open', 'high', 'low', 'close', 'volume']


@pytest.mark.parametrize("period_type", ['hourly', 'intraday'])
def test_지수전용소스는_시봉_분봉을_조회하지_않는다(period_type):
    """국내 지수 코드가 KIS 종목 차트 TR로 흘러가면 엉뚱한 조회가 나간다."""
    with patch.object(api, '_get_intraday_chart_data') as mi, \
         patch.object(api, '_get_hourly_chart_data') as mh:
        df = api.get_chart_data("KOSPI200", is_overseas=False, period_type=period_type)
    assert df.empty
    mi.assert_not_called()
    mh.assert_not_called()


def test_미국채_2년물_차트는_tvDatafeed_현물을_쓴다():
    with patch.object(analysis, 'get_us_treasury_spot_data', return_value=_src_df()) as m:
        df = api.get_chart_data("^US02Y", is_overseas=True, period_type='daily')
    m.assert_called_once_with("US02Y")
    assert not df.empty


def test_HYOAS_차트는_FRED를_쓴다():
    with patch.object(analysis, 'get_fred_data', return_value=_src_df()) as m:
        df = api.get_chart_data("^HYOAS", is_overseas=True, period_type='daily')
    m.assert_called_once_with(config.FRED_INDEX_TICKERS["^HYOAS"])
    assert not df.empty


def test_야후_티커로_들어와도_국내지수_소스를_탄다():
    """^KS200/^KQ150 하위호환 경로도 같은 헬퍼를 거쳐야 한다."""
    with patch.object(analysis, 'get_domestic_index_data', return_value=_src_df()) as m:
        api.get_chart_data("^KS200", is_overseas=True, period_type='daily')
    m.assert_called_once_with("KOSPI200")


# ---------------------------------------------------------------- KRX 공식 이력 병합
#  2026-08-25: 코스피200·코스닥150은 여태 tvDatafeed 단일 소스였다(야후·FDR에 티커가 없다).
#  KRX 확정 봉을 지표의 뼈대로 깔고, KRX가 아직 갖지 않은 날짜만 실시간 소스에서 가져온다.

def _krx_df(dates, close=100.0, volume=1000.0):
    return pd.DataFrame({'date': list(dates),
                         'open': [close] * len(dates), 'high': [close + 1] * len(dates),
                         'low': [close - 1] * len(dates), 'close': [close] * len(dates),
                         'volume': [volume] * len(dates)})


def test_병합은_KRX_이력에_실시간_최신봉만_얹는다():
    hist = _krx_df(['20260819', '20260820', '20260821'], close=100.0, volume=555.0)
    hist.attrs['source'] = 'KRX'
    live = _krx_df(['20260820', '20260821', '20260824'], close=999.0, volume=0.0)
    out = analysis._merge_index_history(hist, live)

    assert out['date'].tolist() == ['20260819', '20260820', '20260821', '20260824']
    # 겹치는 구간은 KRX가 이긴다(거래량 0인 tvDatafeed 값이 덮지 않는다)
    assert out.loc[out['date'] == '20260821', 'close'].iloc[0] == 100.0
    assert out.loc[out['date'] == '20260821', 'volume'].iloc[0] == 555.0
    # KRX가 아직 없는 당일 봉만 실시간 소스에서 온다
    assert out.loc[out['date'] == '20260824', 'close'].iloc[0] == 999.0
    assert out.attrs['source'] == 'KRX'


def test_병합은_날짜타입이_달라도_동작한다():
    """소스마다 date 타입이 다르다 — KIS/토스는 문자열, tvDatafeed는 datetime."""
    hist = _krx_df(['20260820', '20260821'])
    live = _krx_df(pd.to_datetime(['2026-08-21', '2026-08-24']), close=777.0)
    out = analysis._merge_index_history(hist, live)
    assert out['date'].tolist() == ['20260820', '20260821', '20260824']


def test_실시간_소스가_통째로_죽어도_KRX로_살아남는다():
    """코스닥150은 야후·FDR에 티커 자체가 없어 종전엔 tvDatafeed 실패 = 복구 불가였다."""
    hist = _krx_df(['20260820', '20260821'])
    assert analysis._merge_index_history(hist, None) is hist
    assert len(analysis._merge_index_history(hist, pd.DataFrame())) == 2


def test_KRX가_없으면_종전_체인_결과를_그대로_쓴다():
    live = _krx_df(['20260820', '20260821'])
    assert analysis._merge_index_history(None, live) is live


def test_VKOSPI는_병합대상이_아니다():
    """KRX는 V코스피200을 종가만 준다 — KIS의 OHLC를 덮으면 지표가 나빠진다."""
    assert "VKOSPI" not in analysis._KRX_INDEX_MERGE_TYPES
    assert "KOSPI200" in analysis._KRX_INDEX_MERGE_TYPES
    assert "KOSDAQ150" in analysis._KRX_INDEX_MERGE_TYPES


def test_선물은_KIS와_병합하지_않는다():
    """근월물 선택이 소스마다 갈릴 수 있다 — 다른 계약을 이어붙이면 가짜 갭이 생긴다."""
    assert not any(t.startswith("K200FUT") for t in analysis._KRX_INDEX_MERGE_TYPES)


def test_지수화면_목록도_같은_게이트를_쓴다():
    """게이트가 세 곳(목록·화면·워커)에 있다 — 갈라지면 서로 다른 지수 집합을 본다."""
    config.session.is_toss = True
    try:
        with patch.object(market, 'blocked_kis_only_indices',
                          wraps=market.blocked_kis_only_indices) as gate:
            assert market._show_market_indices_core(target_indices=["V코스피200"]) == []
            assert gate.called
    finally:
        config.session.is_toss = False


# ---------------------------------------------------------------- 개장 표시
#  2026-08-26: 확정 표시(=)는 제거했다. KRX 확정 봉이 장중에 실시간인 척하는 것을 표시자로
#  알리려던 장치인데, 그런 값을 아예 안 보여 주기로 하면서 붙을 자리가 사라졌다.

def test_개장중_지수에는_개장표시가_붙는다():
    """실시간으로 움직이는 지수만 표시자를 받는다 — 하단 범례가 이 값을 설명한다."""
    df = _src_df()
    config.session.is_toss = False          # 앞 테스트가 남긴 모드 상태를 지운다(워커 스킵 방지)
    with patch.object(market, 'is_market_open_for_index', return_value=True), \
         patch.object(analysis, 'get_domestic_index_data', return_value=df):
        res = market._process_index_worker("코스피200", "^KS200", pd.DataFrame(), pd.DataFrame())
    assert res['status'] == 'success'
    assert market.INDEX_OPEN_MARK in res['row_data'][0]


def test_장이_닫혀_있으면_표시자가_없다():
    df = _src_df()
    config.session.is_toss = False
    with patch.object(market, 'is_market_open_for_index', return_value=False), \
         patch.object(analysis, 'get_domestic_index_data', return_value=df):
        res = market._process_index_worker("코스피200", "^KS200", pd.DataFrame(), pd.DataFrame())
    assert res['status'] == 'success'
    assert market.INDEX_OPEN_MARK not in res['row_data'][0]


def test_확정표시_장치는_남아있지_않다():
    """묵은 값은 표시로 덮는 게 아니라 목록에서 뺀다 — 표시자만 남기면 다시 붙일 이유부터 찾게 된다."""
    for gone in ('INDEX_SETTLED_MARK', '_is_krx_settled_value', '_KRX_SETTLED_ONLY_INDICES'):
        assert not hasattr(market, gone), gone
