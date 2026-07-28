"""과거 일봉 수신(print_table 1단계)의 헛호출 제거 테스트.

증상: '데이터 수신' 프로그래스 바가 마지막 한두 종목에서 오래 멈춘다(국내·해외 모두).
이 단계는 캐시 미스일 때만 실제 다운로드를 하는데, 모의투자는 전역 2 TPS(SIM_TX_PER_SECOND)로
모든 호출이 직렬화되므로 종목당 헛호출 몇 번이 그대로 체감 지연이 된다.

제거한 낭비는 두 가지다.
 1) 거래소 탐색 — NASD/NYSE/AMEX는 NAS/NYS/AMS와 같은 거래소의 다른 표기인데 6개를 모두
    순회해, 거래소를 못 맞힌 종목마다 같은 곳을 두 번씩 물었다.
 2) 페이지네이션 — 더 받을 과거 봉이 없어 같은 구간이 반복 응답돼도 페이지 예산(10회)을
    끝까지 소진했다. 상장 이력이 짧은 종목·ETF에서 특히 심했다.

둘 다 '보내지 않아도 되는 요청'만 없앤 것으로, 조회 결과를 재사용하지 않으므로 시세·일봉의
신선도와는 무관하다.
"""
from unittest.mock import patch

import pytest

import api


# ==========================================================
# 거래소 코드 정규화
# ==========================================================

@pytest.mark.parametrize("raw, expect", [
    ("NASD", "NAS"), ("NYSE", "NYS"), ("AMEX", "AMS"),
    ("BAQ", "NAS"), ("BAY", "NYS"), ("BAA", "AMS"),   # 주간거래 코드 → 정규장 거래소
    ("NAS", "NAS"), ("nas", "NAS"),
    (None, None),
])
def test_excd_normalize(raw, expect):
    assert api.us_excd_normalize(raw) == expect


def test_probe_list_has_no_duplicate_venues():
    """같은 거래소를 두 번 묻지 않는다 — 최악 탐색 횟수가 6에서 3으로 준다."""
    assert api.us_excd_probe_list(None) == ["NAS", "NYS", "AMS"]
    assert len(api.us_excd_probe_list(None)) == 3


def test_probe_list_puts_cached_exchange_first():
    assert api.us_excd_probe_list("AMS")[0] == "AMS"
    assert sorted(api.us_excd_probe_list("AMS")) == ["AMS", "NAS", "NYS"]


def test_probe_list_normalizes_cached_alias():
    """캐시에 별칭(NASD)이나 주간거래 코드(BAQ)가 들어 있어도 중복이 생기지 않는다."""
    for cached in ("NASD", "BAQ"):
        got = api.us_excd_probe_list(cached)
        assert got == ["NAS", "NYS", "AMS"], f"{cached} -> {got}"


def test_excd_candidates_keeps_day_market_priority():
    """데이마켓 세션에는 주간 코드가 앞, 정규 코드가 뒤라는 기존 규칙을 유지한다."""
    with patch.object(api, 'us_day_market_session', lambda: "20260729"):
        got = api.us_excd_candidates("NYS")
    assert got[:3] == ["BAY", "BAQ", "BAA"]
    assert got[3:] == ["NYS", "NAS", "AMS"]


def test_excd_candidates_outside_day_session():
    with patch.object(api, 'us_day_market_session', lambda: None):
        assert api.us_excd_candidates(None) == ["NAS", "NYS", "AMS"]


# ==========================================================
# 페이지네이션 중단 조건
# ==========================================================

def _domestic_page(dates):
    return {'rt_cd': '0', 'output2': [
        {'stck_bsop_date': d, 'stck_clpr': '1000', 'stck_oprc': '1000',
         'stck_hgpr': '1000', 'stck_lwpr': '1000', 'acml_vol': '10'} for d in dates]}


def _fetch_domestic(pages):
    """call_api가 pages를 순서대로 돌려주게 하고 호출 횟수를 센다."""
    calls = []

    def _fake(url_path, market, category, action, params=None, **kw):
        calls.append(params)
        i = len(calls) - 1
        return pages[i] if i < len(pages) else pages[-1]

    with patch.object(api, 'call_api', side_effect=_fake), \
         patch.object(api, '_get_cached_chart', side_effect=lambda code, is_overseas, is_index, fetch_func, realtime_overlay=True: fetch_func()):
        df = api.get_chart_data("005930", is_overseas=False, period_type='daily', realtime=False)
    return df, calls


def test_domestic_stops_when_no_new_dates():
    """같은 구간이 반복 응답되면 즉시 멈춘다 (종전: 페이지 예산 10회 소진)."""
    same = _domestic_page(["20260720", "20260721"])
    df, calls = _fetch_domestic([same, same, same, same, same])
    assert len(calls) == 2, f"헛호출 {len(calls)}회"
    assert not df.empty


def test_domestic_stops_when_cursor_passes_lookback_start():
    """커서가 조회 시작일 이전으로 넘어가면 더 받을 것이 없다."""
    old = _domestic_page(["20200102", "20200103"])   # 조회 시작일(2년 전)보다 과거
    df, calls = _fetch_domestic([old, old, old])
    assert len(calls) == 1, f"헛호출 {len(calls)}회"


def test_domestic_still_paginates_while_dates_advance():
    """정상적으로 과거로 진행되는 동안에는 종전대로 계속 받는다(동작 축소 금지)."""
    pages = [
        _domestic_page([f"2026071{i}" for i in range(5, 9)]),
        _domestic_page([f"2026070{i}" for i in range(1, 5)]),
        {'rt_cd': '0', 'output2': []},               # 더 없음 → 종료
    ]
    df, calls = _fetch_domestic(pages)
    assert len(calls) == 3
    assert len(df) == 8


def _overseas_page(dates):
    return {'rt_cd': '0', 'output2': [
        {'xymd': d, 'clos': '10', 'open': '10', 'high': '10', 'low': '10', 'tvol': '5'}
        for d in dates]}


def _fetch_overseas(pages):
    calls = []

    def _fake(url_path, market, category, action, params=None, **kw):
        calls.append(params)
        i = len(calls) - 1
        return pages[i] if i < len(pages) else pages[-1]

    with patch.object(api, 'call_api', side_effect=_fake), \
         patch.object(api.config.session, 'exchange_cache', {"QQQ": "NAS"}), \
         patch.object(api.config.session, 'update_cache_and_save', lambda *a, **k: None), \
         patch.object(api, '_get_cached_chart', side_effect=lambda code, is_overseas, is_index, fetch_func, realtime_overlay=True: fetch_func()):
        df = api.get_chart_data("QQQ", is_overseas=True, period_type='daily', realtime=False)
    return df, calls


def test_overseas_stops_when_no_new_dates():
    same = _overseas_page(["20260720", "20260721"])
    df, calls = _fetch_overseas([same] * 5)
    assert len(calls) == 2, f"헛호출 {len(calls)}회"


def test_overseas_probes_at_most_three_exchanges():
    """모든 거래소가 빈 응답이어도 3회를 넘지 않는다 (종전 6회)."""
    empty = {'rt_cd': '0', 'output2': []}
    df, calls = _fetch_overseas([empty] * 10)
    assert len(calls) == 3, f"거래소 탐색 {len(calls)}회"
    assert [p['EXCD'] for p in calls] == ["NAS", "NYS", "AMS"]
    assert df.empty


# ==========================================================
# Rate Limit 재시도 대기
# ==========================================================
#  EGW00201은 서버 장애가 아니라 '초당 한도 초과'다. 다음 TPS 창(<1초)만 비면 풀리는데도
#  장애용 지수 백오프(1→2→4→8→16초)를 타면, 한 번 걸릴 때마다 최대 31초를 잠든다.
#  모의투자(2 TPS)에서 콜드 캐시로 대량 일봉을 받을 때 이 지연이 그대로 누적됐다.

_RATE_LIMIT = "Rate Limit Exceeded (EGW00201): 초당 거래건수 초과"
_SERVER_ERR = "KIS Server Intermittent Error (MCI): 게이트웨이 오류"


def _waits(reason, attempts=5):
    return [api._retry_wait_seconds(a, reason) for a in range(attempts)]


def test_rate_limit_retry_waits_are_short():
    waits = _waits(_RATE_LIMIT)
    # 지터(0.1~0.5)를 포함해도 회차당 상한 + 지터를 넘지 않는다
    assert max(waits) <= api.RATE_LIMIT_RETRY_WAIT_MAX + 0.5 + 1e-9, waits
    assert sum(waits) < 6.0, f"총 대기 {sum(waits):.1f}초"


def test_server_error_keeps_exponential_backoff():
    """진짜 서버 장애·연결 실패는 종전대로 지수 백오프를 유지한다(동작 축소 금지)."""
    waits = _waits(_SERVER_ERR)
    assert waits[4] > 16.0, waits            # 1→2→4→8→16 이 살아 있어야 한다
    assert sum(waits) > 31.0, f"총 대기 {sum(waits):.1f}초"


def test_rate_limit_backoff_is_much_cheaper_than_server_error():
    """모의투자(2 TPS) 대량 조회에서 한 종목이 잠드는 시간이 크게 줄어야 한다."""
    rl = sum(_waits(_RATE_LIMIT))
    srv = sum(_waits(_SERVER_ERR))
    assert srv / rl > 5, f"rate_limit={rl:.1f}s server={srv:.1f}s"


def test_rate_limit_wait_grows_but_is_capped():
    """회차가 늘어도 상한(RATE_LIMIT_RETRY_WAIT_MAX)을 넘지 않는다."""
    with patch.object(api.random, 'uniform', return_value=0.0):
        seq = [api._retry_wait_seconds(a, _RATE_LIMIT) for a in range(10)]
    assert seq[0] == pytest.approx(api.RATE_LIMIT_RETRY_WAIT)
    assert seq == sorted(seq)                                  # 단조 증가
    assert max(seq) == pytest.approx(api.RATE_LIMIT_RETRY_WAIT_MAX)


def test_unknown_reason_uses_exponential_backoff():
    """사유를 판별할 수 없으면 보수적으로 기존(지수) 대기를 쓴다."""
    with patch.object(api.random, 'uniform', return_value=0.0):
        assert api._retry_wait_seconds(3, "") == pytest.approx(8.0)
        assert api._retry_wait_seconds(3, None) == pytest.approx(8.0)


# ==========================================================
# Mode 3(토스) 해외 일봉 tvDatafeed 폴백
# ==========================================================
#  토스 캔들이 120봉 미만인 신규 상장 ETF는 매 조회마다 tvDatafeed 폴백을 탄다.
#  tvDatafeed 호출은 전역 락으로 직렬화되므로 헛도는 조회 하나가 다른 종목의 대기가 된다
#  ('데이터 수신' 단계가 마지막 한두 종목에서 오래 멈추는 원인).

import pandas as pd
from modules import analysis as _an


@pytest.fixture(autouse=True)
def _clear_tv_maps():
    _an._TVDATAFEED_EXCHANGE.clear()
    _an._TVDATAFEED_OVERSEAS_NEG_CACHE.clear()
    yield
    _an._TVDATAFEED_EXCHANGE.clear()
    _an._TVDATAFEED_OVERSEAS_NEG_CACHE.clear()


class _FakeTv:
    def __init__(self, hit_exchange=None, matches=None):
        self.hit = hit_exchange
        self.matches = matches or []
        self.hist_calls = []
        self.search_calls = 0

    def search_symbol(self, code):
        self.search_calls += 1
        return self.matches

    def get_hist(self, symbol, exchange, interval, n_bars):
        self.hist_calls.append(exchange)
        if exchange == self.hit:
            idx = pd.to_datetime(['2026-07-28'])
            idx.name = 'datetime'          # 실제 tvDatafeed와 동일한 인덱스 이름
            return pd.DataFrame({'open': [1.0], 'high': [1.0], 'low': [1.0],
                                 'close': [1.0], 'volume': [1.0]}, index=idx)
        return None


def _run_tv(tv, code="NASA"):
    with patch('modules.analysis._get_tvdatafeed', return_value=tv), \
         patch('modules.analysis.time.sleep'):
        return _an.fetch_overseas_daily_via_tvdatafeed(code)


def test_search_hit_skips_guessed_exchanges():
    """검색이 거래소를 찾아내면 NASDAQ/NYSE/AMEX 추측을 덧붙이지 않는다."""
    tv = _FakeTv(hit_exchange="AMEX", matches=[{'symbol': 'NASA', 'exchange': 'AMEX'}])
    assert _run_tv(tv) is not None
    assert tv.hist_calls == ["AMEX"]


def test_missing_symbol_bounded_attempts():
    """없는 종목이어도 조회 횟수가 제한된다 (종전 최대 6거래소 × 2회 = 12회)."""
    tv = _FakeTv(hit_exchange=None)          # 검색 결과 없음 → 추측 3개
    assert _run_tv(tv) is None
    # 첫 거래소만 2회, 나머지는 1회 → 4회
    assert len(tv.hist_calls) == 4, tv.hist_calls
    assert tv.hist_calls == ["NASDAQ", "NASDAQ", "NYSE", "AMEX"]


def test_exchange_is_remembered_for_next_call():
    """한 번 확인한 거래소는 기억해 재조회 때 검색·헛거래소 순회를 건너뛴다."""
    tv = _FakeTv(hit_exchange="AMEX")
    assert _run_tv(tv) is not None
    first = list(tv.hist_calls)
    assert _an._TVDATAFEED_EXCHANGE["NASA"] == "AMEX"

    tv2 = _FakeTv(hit_exchange="AMEX")
    assert _run_tv(tv2) is not None
    assert tv2.search_calls == 0, "기억된 거래소가 있으면 심볼 검색을 하지 않는다"
    assert tv2.hist_calls == ["AMEX"]
    assert len(tv2.hist_calls) < len(first)


def test_stale_remembered_exchange_is_forgotten():
    """기억한 거래소가 더는 맞지 않으면 잊고 다음 기회에 다시 찾는다."""
    _an._TVDATAFEED_EXCHANGE["NASA"] = "NYSE"
    tv = _FakeTv(hit_exchange=None)
    assert _run_tv(tv) is None
    assert "NASA" not in _an._TVDATAFEED_EXCHANGE


def test_tv_fallback_respects_time_budget():
    """조회 예산을 넘기면 남은 거래소를 포기한다 — 폴백은 보강이지 필수가 아니다.

    tvDatafeed 호출은 전역 락을 쥐고 돌기 때문에, 한 종목의 연결 타임아웃이 나머지 종목의
    대기로 그대로 번진다('데이터 수신' 단계가 마지막 한두 종목에서 멈추는 현상).
    """
    clock = {'t': 0.0}

    class _SlowTv(_FakeTv):
        def get_hist(self, symbol, exchange, interval, n_bars):
            clock['t'] += 10.0          # 거래소마다 10초씩 소모
            return super().get_hist(symbol, exchange, interval, n_bars)

    tv = _SlowTv(hit_exchange=None)
    with patch('modules.analysis._get_tvdatafeed', return_value=tv), \
         patch('modules.analysis.time.sleep'), \
         patch('modules.analysis.time.monotonic', side_effect=lambda: clock['t']):
        assert _an.fetch_overseas_daily_via_tvdatafeed("NASA") is None

    # 예산(12초) 안에서 가능한 만큼만 시도하고 나머지 거래소는 건너뛴다
    assert len(tv.hist_calls) < 4, tv.hist_calls
    assert clock['t'] <= _an.TVDATAFEED_FETCH_BUDGET_SEC + 10.0


def test_tv_fallback_budget_does_not_block_fast_success():
    """정상 응답이면 예산과 무관하게 종전대로 값을 돌려준다(동작 축소 금지)."""
    tv = _FakeTv(hit_exchange="NASDAQ")
    with patch('modules.analysis._get_tvdatafeed', return_value=tv), \
         patch('modules.analysis.time.sleep'):
        out = _an.fetch_overseas_daily_via_tvdatafeed("QQQ")
    assert out is not None and not out.empty
    assert tv.hist_calls == ["NASDAQ"]
