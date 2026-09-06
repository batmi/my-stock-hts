"""시세 소스가 '값이 아닌 것'을 성공으로 돌려주지 않는가.

[왜 이 파일이 있나 · 2026-09-06]
 get_yf_fast_info 는 세 갈래(TV 스크리너 → yfinance → tvDatafeed)를 차례로 탄다. 그런데
 **가운데 둘만** 값을 검사했다:

   · yfinance    : `if last_price is None or pd.isna(last_price): raise`
   · tvDatafeed  : `if not (last_price > 0): raise`
   · TV 스크리너 : (없음)

 대가가 캐시 때문에 커진다. NaN 이 한 번 굳으면 60초 동안 그것이 답이고, 그 뒤로도
 stale-if-error 유예(900초) 안에서는 **'직전 성공값'** 이라는 이름으로 계속 나온다.
 NaN 은 비교가 전부 거짓이라 `price > 0` 류의 검사를 조용히 통과하는 대신, 등락률과
 역산 계산을 NaN 으로 물들인다.
"""
import sys
import types

import numpy as np
import pandas as pd
import pytest

import api.yf_quotes as yq


@pytest.fixture
def fresh_cache(monkeypatch):
    monkeypatch.setattr(yq, "_MICRO_CACHE",
                        type(yq._MICRO_CACHE)(max_size=100), raising=False)
    yield


def _install_screener(row):
    """tradingview_screener 를 한 행짜리 가짜로 갈아끼운다."""
    mod = types.ModuleType("tradingview_screener")

    class _Col:
        def __init__(self, n): pass
        def __eq__(self, o): return self

    class _Q:
        def set_markets(self, *a): return self
        def select(self, *a): return self
        def where(self, *a): return self
        def limit(self, *a): return self
        def get_scanner_data(self): return None, pd.DataFrame([row])

    mod.Query = _Q
    mod.Column = _Col
    sys.modules["tradingview_screener"] = mod


@pytest.fixture(autouse=True)
def _cleanup_screener():
    yield
    sys.modules.pop("tradingview_screener", None)


_NAN_ROW = {'close': np.nan, 'change_abs': np.nan, 'volume': np.nan,
            'High.52Week': np.nan, 'premarket_close': np.nan, 'postmarket_close': np.nan}


def test_NaN_시세를_성공으로_돌려주지_않는다(fresh_cache):
    _install_screener(_NAN_ROW)
    assert yq.get_yf_fast_info("AAPL", ttl=60.0) is None, \
        "NaN 을 시세로 돌려준다 — 등락률·역산이 NaN 으로 물든다"


def test_NaN_시세를_캐시에_굳히지_않는다(fresh_cache):
    """굳으면 stale-if-error 유예(900초) 동안 '직전 성공값'으로 계속 나온다."""
    _install_screener(_NAN_ROW)
    yq.get_yf_fast_info("AAPL", ttl=60.0)
    assert yq._get_micro_cache("yf_fi_AAPL", ttl=yq._YF_FI_STALE_GRACE_SEC) is None


@pytest.mark.parametrize("close", [0, -1, None])
def test_0_이하도_시세가_아니다(fresh_cache, close):
    _install_screener(dict(_NAN_ROW, close=close, change_abs=0))
    assert yq.get_yf_fast_info("AAPL", ttl=60.0) is None


def test_정상값은_종전대로_돌려준다(fresh_cache):
    """대조군 — 검사가 정상 경로를 막지 않는다."""
    _install_screener({'close': 190.0, 'change_abs': 2.0, 'volume': 1000,
                       'High.52Week': 220.0, 'premarket_close': np.nan,
                       'postmarket_close': np.nan})
    d = yq.get_yf_fast_info("AAPL", ttl=60.0)
    assert d is not None and d['last_price'] == 190.0
    assert d['regular_market_previous_close'] == 188.0
    assert d['src'] == 'tv'


def test_거래량_52주고점의_NaN도_흘리지_않는다(fresh_cache):
    """시세만큼 치명적이진 않지만, NaN 이 흐르면 포맷·비교가 조용히 무너진다."""
    _install_screener({'close': 190.0, 'change_abs': 2.0, 'volume': np.nan,
                       'High.52Week': np.nan, 'premarket_close': np.nan,
                       'postmarket_close': np.nan})
    d = yq.get_yf_fast_info("AAPL", ttl=60.0)
    assert d['last_volume'] == 0
    assert d['year_high'] is None


# ─────────────────────────────────────────────────────────────────────────────
#  테스트 격리 — 차단 목록이 '막는다'고 말하는 것이 실제로 막히는가
# ─────────────────────────────────────────────────────────────────────────────

def test_yfinance_는_테스트에서_실제로_막힌다(fresh_cache):
    """conftest 의 차단 목록에는 finance.yahoo.com 이 처음부터 있었다.

    그런데 차단 수단이 `requests.Session.request` 패치라, **자체 HTTP 클라이언트를 쓰는
    yfinance 는 그것을 통째로 비켜 갔다.** 실측: 테스트 안에서 진짜 AAPL 시세(319.97)가
    돌아왔다. 목록이 막는다고 말하는데 실제로는 안 막히는 상태가 가장 나쁘다 —
    '네트워크를 안 탄다'는 전제 위에 쓴 다른 테스트들이 조용히 거짓이 된다.
    """
    import yfinance as yf
    with pytest.raises(Exception) as e1:
        yf.Ticker("AAPL")
    assert "테스트 격리" in str(e1.value)

    with pytest.raises(Exception) as e2:
        yf.download("AAPL")
    assert "테스트 격리" in str(e2.value)


def test_시세_조회는_막힌_상태에서_None_을_돌려준다(fresh_cache):
    """세 소스가 모두 막히면 '모른다'여야 한다 — 지어내지 않는다."""
    sys.modules.pop("tradingview_screener", None)
    assert yq.get_yf_fast_info("ZZZZ_NOT_A_TICKER", ttl=60.0) is None
