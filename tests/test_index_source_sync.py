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

def test_KIS실전전용_지수는_토스_모의모드_목록에서_빠진다():
    config.session.is_toss = False
    config.session.is_simulation = False
    assert [n for n, _ in market.selectable_indices()] == [n for n, _ in market.ALL_INDICES]

    for mode_attr in ('is_toss', 'is_simulation'):
        config.session.is_toss = False
        config.session.is_simulation = False
        setattr(config.session, mode_attr, True)
        names = [n for n, _ in market.selectable_indices()]
        assert "V코스피200" not in names
        assert "코스피200선물" not in names
        assert "코스피200" in names  # 대체 소스가 있는 지수는 남는다


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
