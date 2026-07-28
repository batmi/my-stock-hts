"""단건 시세(get_yf_fast_info) 폴백 체인 테스트.

배경 1 — TV 스크리너 경로가 죽어 있었다.
  YF_TO_TV_EXACT 매핑 심볼(지수·선물·환율)은 Query().get_tickers()로 조회했는데, 설치된
  tradingview-screener 3.x에는 그 메서드가 없어(2.x API) AttributeError가 나고 바깥
  except가 삼켰다. 매핑 20개가 전부 무효인 채 yfinance 단일 소스로 동작했다.
  스크리너는 미국 '주식' 스캔 유니버스라 set_tickers로 고쳐도 지수·선물이 나오지 않는다
  (실측: CBOE:VIX·TVC:DXY·COMEX:GC1!·TVC:US10Y 모두 빈 응답) → tvDatafeed로 경로 이전.

배경 2 — 한 번의 순간 실패가 곧바로 '실시간 시세 지연' 경고가 됐다.
  지수 화면은 ~47개 심볼이 각자 fast_info를 호출하고 _YF_LOCK으로 직렬화된다. 야후의 순간
  스로틀 하나로 특정 지수만 확정 종가로 되돌아갔다(실측: DRG 2026-07-28).
  → 유예 시간 안에서는 직전 성공값을 재사용한다(stale-if-error).
"""
from unittest.mock import MagicMock, patch

import pandas as pd
import pytest

import api


@pytest.fixture(autouse=True)
def _clear_caches():
    api._MICRO_CACHE.clear()
    api._TVD_QUOTE_FAIL.clear()
    yield
    api._MICRO_CACHE.clear()
    api._TVD_QUOTE_FAIL.clear()


def _fi(last=150.0, prev=145.0):
    m = MagicMock()
    m.last_price = last
    m.regular_market_previous_close = prev
    m.last_volume = 1000
    m.year_high = 200.0
    return m


def _tv_df(closes, highs=None):
    return pd.DataFrame({'close': closes, 'high': highs or closes})


# ==========================================================
# 소스 선택 순서
# ==========================================================

def test_yfinance_success_skips_tvdatafeed():
    """평상시엔 yfinance로 끝난다 — tvDatafeed는 웹소켓 직렬화라 호출 자체를 아껴야 한다."""
    with patch.object(api.yf, 'Ticker') as mt, \
         patch.object(api, '_fast_info_via_tvdatafeed') as m_tv:
        mt.return_value.fast_info = _fi()
        got = api.get_yf_fast_info("^VIX")

    assert got['src'] == 'yf'
    assert got['last_price'] == 150.0
    m_tv.assert_not_called()


def test_tvdatafeed_fallback_when_yfinance_fails():
    """yfinance가 실패하면 TV 매핑이 있는 심볼은 tvDatafeed가 값을 메운다."""
    with patch.object(api.yf, 'Ticker', side_effect=Exception("boom")), \
         patch.object(api, '_fast_info_via_tvdatafeed',
                      return_value={'last_price': 18.8, 'regular_market_previous_close': 18.6,
                                    'last_volume': 0, 'year_high': None,
                                    'src': 'tvd', 'is_extended': False}) as m_tv:
        got = api.get_yf_fast_info("^VIX")

    m_tv.assert_called_once_with("CBOE:VIX")
    assert got['src'] == 'tvd'
    assert got['last_price'] == 18.8


def test_no_tv_mapping_skips_tvdatafeed():
    """매핑이 없는 티커(^DRG 등)는 tvDatafeed 대상이 아니다."""
    assert "^DRG" not in api.YF_TO_TV_EXACT
    with patch.object(api.yf, 'Ticker', side_effect=Exception("boom")), \
         patch.object(api, '_fast_info_via_tvdatafeed') as m_tv:
        got = api.get_yf_fast_info("^DRG")

    m_tv.assert_not_called()
    assert got is None


def test_screener_not_used_for_mapped_symbols():
    """회귀: 매핑 심볼을 스크리너로 조회하지 않는다(3.x에 get_tickers가 없어 항상 실패했다)."""
    with patch('tradingview_screener.Query') as mq, \
         patch.object(api.yf, 'Ticker') as mt:
        mt.return_value.fast_info = _fi()
        api.get_yf_fast_info("GC=F")
    mq.assert_not_called()


def test_screener_still_used_for_plain_stock():
    """미국 개별 주식은 종전대로 스크리너(get_scanner_data)를 먼저 쓴다 — 장외가 제공."""
    df = pd.DataFrame([{'close': 100.0, 'change_abs': 2.0, 'volume': 10,
                        'High.52Week': 120.0, 'premarket_close': None,
                        'postmarket_close': 101.0}])
    with patch('tradingview_screener.Query') as mq:
        chain = mq.return_value.set_markets.return_value.select.return_value \
                  .where.return_value.limit.return_value
        chain.get_scanner_data.return_value = (1, df)
        got = api.get_yf_fast_info("AAPL")

    assert got['src'] == 'tv'
    assert got['last_price'] == 101.0        # 애프터마켓가 우선
    assert got['is_extended'] is True


# ==========================================================
# 전일 종가 결측 처리
# ==========================================================

def test_partial_yfinance_prefers_complete_tv_quote():
    """전일 종가가 없으면 호출부가 실시간으로 인정하지 않는다 → 온전한 TV 값을 우선한다."""
    fi = _fi(prev=None)
    fi.previous_close = None
    with patch.object(api.yf, 'Ticker') as mt, \
         patch.object(api, '_fast_info_via_tvdatafeed',
                      return_value={'last_price': 18.8, 'regular_market_previous_close': 18.6,
                                    'last_volume': 0, 'year_high': None,
                                    'src': 'tvd', 'is_extended': False}):
        mt.return_value.fast_info = fi
        got = api.get_yf_fast_info("^VIX")

    assert got['src'] == 'tvd'


def test_partial_yfinance_returned_when_tv_also_fails():
    """TV까지 실패하면 반쪽 값이라도 종전처럼 돌려준다(동작 축소 금지)."""
    fi = _fi(prev=None)
    fi.previous_close = None
    with patch.object(api.yf, 'Ticker') as mt, \
         patch.object(api, '_fast_info_via_tvdatafeed', return_value=None):
        mt.return_value.fast_info = fi
        got = api.get_yf_fast_info("^VIX")

    assert got['src'] == 'yf'
    assert got['last_price'] == 150.0
    assert got['regular_market_previous_close'] is None


# ==========================================================
# stale-if-error
# ==========================================================

def test_stale_reuse_on_transient_failure():
    """직전에 성공했다면 순간 실패에 확정 종가로 되돌아가지 않는다."""
    with patch.object(api.yf, 'Ticker') as mt:
        mt.return_value.fast_info = _fi(last=1251.88, prev=1247.10)
        first = api.get_yf_fast_info("^DRG", ttl=60)
    assert first['last_price'] == 1251.88

    # 캐시 TTL을 지나 재조회 → 야후 실패
    with patch.object(api.yf, 'Ticker', side_effect=Exception("throttled")):
        got = api.get_yf_fast_info("^DRG", ttl=0)

    assert got is not None
    assert got['last_price'] == 1251.88
    assert got.get('is_stale') is True


def test_stale_not_reused_beyond_grace():
    """유예 시간을 넘긴 값은 재사용하지 않는다 — 오래된 값을 실시간으로 속이면 안 된다."""
    import time as _time
    with patch.object(api.yf, 'Ticker') as mt:
        mt.return_value.fast_info = _fi()
        api.get_yf_fast_info("^DRG")

    later = _time.time() + api._YF_FI_STALE_GRACE_SEC + 10
    with patch.object(api.yf, 'Ticker', side_effect=Exception("down")), \
         patch('time.time', return_value=later):
        assert api.get_yf_fast_info("^DRG") is None


# ==========================================================
# _fast_info_via_tvdatafeed
# ==========================================================

def test_tvdatafeed_quote_uses_last_two_closes():
    tv = MagicMock()
    tv.get_hist.return_value = _tv_df([100.0, 101.0, 102.0])
    with patch('modules.analysis._get_tvdatafeed', return_value=tv):
        got = api._fast_info_via_tvdatafeed("TVC:DXY")

    assert got['last_price'] == 102.0
    assert got['regular_market_previous_close'] == 101.0
    assert got['src'] == 'tvd'
    assert got['is_extended'] is False
    # 52주 고점이 아니므로 넘기지 않는다(호출부가 일봉 기준을 쓴다)
    assert got['year_high'] is None
    kwargs = tv.get_hist.call_args.kwargs
    assert kwargs['symbol'] == "DXY" and kwargs['exchange'] == "TVC"


def test_tvdatafeed_quote_requires_two_bars():
    tv = MagicMock()
    tv.get_hist.return_value = _tv_df([100.0])
    with patch('modules.analysis._get_tvdatafeed', return_value=tv):
        assert api._fast_info_via_tvdatafeed("TVC:DXY") is None


def test_tvdatafeed_quote_negative_cache_skips_retry():
    """실패한 심볼을 매 호출마다 재조회하면 웹소켓 재연결로 화면이 느려진다."""
    tv = MagicMock()
    tv.get_hist.return_value = None
    with patch('modules.analysis._get_tvdatafeed', return_value=tv):
        assert api._fast_info_via_tvdatafeed("TVC:DXY") is None
        assert api._fast_info_via_tvdatafeed("TVC:DXY") is None
    assert tv.get_hist.call_count == 1


def test_tvdatafeed_quote_rejects_bad_symbol():
    assert api._fast_info_via_tvdatafeed("DXY") is None      # 거래소 접두어 없음
    assert api._fast_info_via_tvdatafeed(None) is None


# ==========================================================
# 심볼 매핑
# ==========================================================

def test_brent_maps_to_ice_europe():
    """실측: NYMEX:BRN1!은 빈 응답, ICEEUR:BRN1!만 시세를 준다(2026-07-28)."""
    assert api.YF_TO_TV_EXACT["BZ=F"] == "ICEEUR:BRN1!"


def test_all_tv_symbols_have_exchange_prefix():
    for yf_code, tv_symbol in api.YF_TO_TV_EXACT.items():
        assert ':' in tv_symbol, f"{yf_code} 매핑에 거래소 접두어가 없다: {tv_symbol}"
