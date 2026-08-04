"""HY OAS(FRED) 조회가 tvDatafeed 간헐 실패를 견디는가.

[왜 묻는가] HY OAS는 tvDatafeed(FRED:BAMLH0A0HYM2)에서 받는다. 그런데 익명 웹소켓은
웜 인스턴스에서도 ~1/3 확률로 빈 응답을 주고 실패가 버스트로 몰린다
(_fetch_index_via_tvdatafeed 주석). 같은 소스를 쓰는 국내 지수는 4회, 국채 현물은
최대 6회 재시도 + 성공값 폴백을 두었는데 **이 경로만 1회 시도에 캐시도 폴백도 없었다.**

그 결과 tvDatafeed 호출이 많은 토스/가상투자 모드(mode 3·4 — 코스피200·코스닥150이
tvDatafeed 1순위라 같은 전역 락에 8회가 더 붙는다)에서 HY OAS만 상시 실패로 보였다.
mode 2(KIS)는 그 둘을 KIS에서 받으므로 경합이 적어 1회 시도로도 대개 성공했다.

게다가 실패하면 yfinance 분기로 흘러가 ^HYOAS(야후 미제공)를 조회했고, 안내 문구가
'yfinance 서버 장애'로 잘못 나왔다. 국채 현물은 같은 이유로 이미 early return 하는데
이 경로만 빠져 있었다.
"""
import time
from datetime import datetime, timedelta

import pandas as pd
import pytest
from unittest.mock import patch, MagicMock

from modules import analysis, market

SYMBOL = "BAMLH0A0HYM2"


def _hist(last=3.5):
    """tv.get_hist가 돌려주는 모양의 df — 인덱스 이름이 'datetime'이어야 한다."""
    idx = pd.date_range("2026-01-01", periods=5, freq="D", name="datetime")
    return pd.DataFrame({'open': [last] * 5, 'high': [last + 0.1] * 5,
                         'low': [last - 0.1] * 5, 'close': [last] * 5,
                         'volume': [0] * 5}, index=idx)


@pytest.fixture
def clean_fred():
    analysis._FRED_CACHE.clear()
    yield
    analysis._FRED_CACHE.clear()


def _tv(*responses):
    """get_hist가 responses를 차례로 돌려주는 가짜 tvDatafeed."""
    tv = MagicMock()
    tv.get_hist.side_effect = list(responses)
    return tv


# ───────────────────────── 재시도 ─────────────────────────

def test_empty_response_is_retried(clean_fred):
    """[핵심] 빈 응답 한 번에 포기하면 안 된다 — 1회 시도의 실패율이 ~1/3이다."""
    tv = _tv(pd.DataFrame(), pd.DataFrame(), _hist(4.2))
    with patch.object(analysis, '_get_tvdatafeed', return_value=tv), \
         patch.object(time, 'sleep'):
        out = analysis.get_fred_data(SYMBOL)

    assert tv.get_hist.call_count == 3, "빈 응답을 받고 재시도하지 않았다"
    assert out is not None and float(out['close'].iloc[-1]) == 4.2


def test_exception_is_retried_too(clean_fred):
    """예외(웹소켓 끊김)도 빈 응답과 같이 재시도 대상이다."""
    tv = _tv(OSError("socket closed"), _hist(3.9))
    with patch.object(analysis, '_get_tvdatafeed', return_value=tv), \
         patch.object(time, 'sleep'):
        out = analysis.get_fred_data(SYMBOL)

    assert tv.get_hist.call_count == 2
    assert out is not None


def test_retries_are_bounded(clean_fred):
    """무한 재시도는 지수 화면을 통째로 붙잡는다 — 상한이 있어야 한다."""
    tv = _tv(*[pd.DataFrame()] * 10)
    with patch.object(analysis, '_get_tvdatafeed', return_value=tv), \
         patch.object(time, 'sleep'):
        out = analysis.get_fred_data(SYMBOL)

    assert tv.get_hist.call_count == 4, f"시도 횟수가 {tv.get_hist.call_count}회"
    assert out is None


def test_backoff_is_applied_between_attempts(clean_fred):
    """재시도가 페이싱 없이 붙으면 같은 버스트에 그대로 다시 걸린다."""
    tv = _tv(pd.DataFrame(), pd.DataFrame(), _hist())
    with patch.object(analysis, '_get_tvdatafeed', return_value=tv), \
         patch.object(time, 'sleep') as slept:
        analysis.get_fred_data(SYMBOL)

    assert slept.call_count == 2, "재시도 사이에 대기가 없다"
    assert slept.call_args_list[1][0][0] > slept.call_args_list[0][0][0], "백오프가 점증하지 않는다"


# ───────────────────────── 캐시 ─────────────────────────

def test_success_is_cached(clean_fred):
    """[락 경합] 성공값을 캐시해야 매 렌더마다 전역 락을 다시 잡지 않는다.

    이 경합이 곧 다른 tvDatafeed 소비자(코스피200·코스닥150·국채)의 실패율이다.
    """
    tv = _tv(_hist(4.0))
    with patch.object(analysis, '_get_tvdatafeed', return_value=tv):
        first = analysis.get_fred_data(SYMBOL)
        second = analysis.get_fred_data(SYMBOL)

    assert tv.get_hist.call_count == 1, "캐시가 있는데도 tvDatafeed를 다시 호출했다"
    assert float(second['close'].iloc[-1]) == float(first['close'].iloc[-1])


def test_failure_falls_back_to_the_last_good_value(clean_fred):
    """[핵심] 실패 시 직전 성공값을 쓴다 — 간헐 실패로 '-'가 뜨는 것을 막는다."""
    with patch.object(analysis, '_get_tvdatafeed', return_value=_tv(_hist(4.4))):
        analysis.get_fred_data(SYMBOL)

    # TTL을 만료시켜 재조회를 유도하고, 그 재조회가 전부 실패하게 한다
    analysis._FRED_CACHE[SYMBOL]["time"] = datetime.now() - timedelta(seconds=analysis._FRED_TTL_SEC + 1)
    with patch.object(analysis, '_get_tvdatafeed', return_value=_tv(*[pd.DataFrame()] * 4)), \
         patch.object(time, 'sleep'):
        out = analysis.get_fred_data(SYMBOL)

    assert out is not None, "직전 성공값이 있는데 None을 돌려줬다"
    assert float(out['close'].iloc[-1]) == 4.4


def test_repeated_failures_are_negative_cached(clean_fred):
    """연속 실패 시 매 렌더마다 4회씩 때리면 전역 락이 그만큼 더 막힌다."""
    with patch.object(analysis, '_get_tvdatafeed', return_value=_tv(*[pd.DataFrame()] * 4)), \
         patch.object(time, 'sleep'):
        analysis.get_fred_data(SYMBOL)

    tv2 = _tv(*[pd.DataFrame()] * 4)
    with patch.object(analysis, '_get_tvdatafeed', return_value=tv2), \
         patch.object(time, 'sleep'):
        analysis.get_fred_data(SYMBOL)

    assert tv2.get_hist.call_count == 0, "음성 캐시 구간인데 다시 조회했다"


def test_manual_retry_clears_the_negative_cache(clean_fred):
    """사용자가 재시도(y)를 골랐는데 음성 캐시가 남아 있으면 무의미하다."""
    with patch.object(analysis, '_get_tvdatafeed', return_value=_tv(*[pd.DataFrame()] * 4)), \
         patch.object(time, 'sleep'):
        analysis.get_fred_data(SYMBOL)

    analysis.reset_fred_failures()

    tv2 = _tv(_hist(5.1))
    with patch.object(analysis, '_get_tvdatafeed', return_value=tv2):
        out = analysis.get_fred_data(SYMBOL)

    assert tv2.get_hist.call_count == 1, "해제 후에도 음성 캐시가 재조회를 막았다"
    assert float(out['close'].iloc[-1]) == 5.1


def test_missing_tvdatafeed_returns_the_cache_not_none(clean_fred):
    """tvDatafeed 미설치·초기화 실패에도 직전 값이 있으면 그걸 쓴다."""
    with patch.object(analysis, '_get_tvdatafeed', return_value=_tv(_hist(3.3))):
        analysis.get_fred_data(SYMBOL)
    analysis._FRED_CACHE[SYMBOL]["time"] = datetime.now() - timedelta(seconds=analysis._FRED_TTL_SEC + 1)

    with patch.object(analysis, '_get_tvdatafeed', return_value=None):
        out = analysis.get_fred_data(SYMBOL)

    assert out is not None and float(out['close'].iloc[-1]) == 3.3


# ───────────────── 실패 소스 표기 (yfinance 오인) ─────────────────

def test_hy_oas_failure_is_attributed_to_tradingview():
    """[핵심] 야후는 ^HYOAS를 제공하지 않는다 — 'yfinance 장애'는 오진이다.

    종전에는 FRED 실패 후 yfinance 분기로 흘러가 실패 소스가 yfinance로 안내됐다.
    운영자가 엉뚱한 곳을 보게 만드는 메시지다(국채 현물은 이미 고쳐져 있었다).
    """
    with patch.object(market.analysis, 'get_fred_data', return_value=None), \
         patch.object(market.api, 'get_yf_fast_info') as yf:
        res = market._process_index_worker("HY OAS (신용위험)", "^HYOAS",
                                           pd.DataFrame(), pd.DataFrame())

    assert res.get('status') == 'failed'
    assert res.get('src') == 'TradingView', f"실패 소스를 잘못 안내한다: {res.get('src')}"
    assert not yf.called, "야후가 제공하지 않는 티커로 조회를 시도했다"


def test_hy_oas_success_still_renders():
    """대조군 — 정상 수신 시에는 실패로 빠지지 않아야 한다(보류가 상시면 기능이 죽는다)."""
    df = pd.DataFrame({'date': pd.date_range("2026-01-01", periods=5, freq="D"),
                       'open': [3.5] * 5, 'high': [3.6] * 5, 'low': [3.4] * 5,
                       'close': [3.5, 3.5, 3.5, 3.5, 3.7], 'volume': [0] * 5})
    with patch.object(market.analysis, 'get_fred_data', return_value=df):
        res = market._process_index_worker("HY OAS (신용위험)", "^HYOAS",
                                           pd.DataFrame(), pd.DataFrame())

    assert res.get('status') != 'failed', f"정상 데이터인데 실패 처리됐다: {res}"
