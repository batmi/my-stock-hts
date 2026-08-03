"""토스증권 클라이언트(toss_api) 단위 테스트.

네트워크를 타지 않도록 requests 및 토큰 캐시를 모킹한다.
"""
import json
import os
import tempfile
import time
import pytest
from unittest.mock import patch, MagicMock

import config
import toss_api


class FakeResponse:
    def __init__(self, status_code, body=None, text="", headers=None):
        self.status_code = status_code
        self._body = body
        self.text = text or (json.dumps(body) if body is not None else "")
        self.headers = headers or {}

    def json(self):
        if self._body is None:
            raise ValueError("no json")
        return self._body


def test_get_access_token_success():
    captured = {}

    def fake_set_token(key, token, expired):
        captured["key"] = key
        captured["token"] = token
        captured["expired"] = expired

    config.session.toss_app_key = "c_test"
    config.session.toss_app_secret = "s_test"

    with patch.object(config.session, "get_valid_token", return_value=None), \
         patch.object(config.session, "set_token", side_effect=fake_set_token), \
         patch("toss_api.requests.post",
               return_value=FakeResponse(200, {"access_token": "TOK123", "token_type": "Bearer", "expires_in": 86400})) as mp:
        token = toss_api.get_access_token(force_refresh=True)

    assert token == "TOK123"
    assert captured["key"] == "TOSS"
    # form-urlencoded 본문으로 전송되는지 확인
    _, kwargs = mp.call_args
    assert kwargs["data"]["grant_type"] == "client_credentials"
    assert kwargs["data"]["client_id"] == "c_test"


def test_get_access_token_uses_cache():
    with patch.object(config.session, "get_valid_token", return_value="CACHED"), \
         patch("toss_api.requests.post") as mp:
        token = toss_api.get_access_token(force_refresh=False)
    assert token == "CACHED"
    mp.assert_not_called()


def test_request_retries_once_after_401_invalid_token():
    """서버가 캐시 만료 전에 토큰을 폐기(401)하면 강제 재발급 후 같은 요청을 재시도한다."""
    config.session.toss_account_seq = 1
    err = {"error": {"code": "invalid-token", "message": "유효하지 않은 토큰입니다."}}
    ok = {"result": [{"symbol": "005930", "lastPrice": "72000"}]}
    responses = [FakeResponse(401, err), FakeResponse(200, ok)]
    issued = []

    def fake_token(force_refresh=False, stale_token=None):
        issued.append((force_refresh, stale_token))
        return "NEW" if force_refresh else "DEAD"

    with patch("toss_api.get_access_token", side_effect=fake_token), \
         patch("toss_api.requests.get", side_effect=responses) as mg:
        result = toss_api.get_price("005930")

    assert result["lastPrice"] == "72000"          # 재시도로 복구
    assert issued[1] == (True, "DEAD")             # 죽은 토큰을 넘겨 강제 재발급
    assert mg.call_args_list[1][1]["headers"]["Authorization"] == "Bearer NEW"


def test_request_401_refresh_only_once():
    """재발급 후에도 401이면 더 발급하지 않고 예외로 끝낸다(무한 재발급 방지)."""
    config.session.toss_account_seq = 1
    err = {"error": {"code": "invalid-token", "message": "유효하지 않은 토큰입니다."}}
    calls = {"n": 0}

    def fake_token(force_refresh=False, stale_token=None):
        calls["n"] += 1
        return f"TOK{calls['n']}"

    with patch("toss_api.get_access_token", side_effect=fake_token), \
         patch("toss_api.requests.get", return_value=FakeResponse(401, err)):
        try:
            toss_api.get_price("005930")
            assert False, "TossApiError가 발생해야 함"
        except toss_api.TossApiError as e:
            assert e.status == 401
    assert calls["n"] == 2  # 최초 1회 + 강제 재발급 1회


def test_get_access_token_skips_refresh_when_peer_already_renewed():
    """동시 401에서 다른 스레드가 이미 갱신했으면 재발급하지 않고 새 토큰을 그대로 쓴다."""
    with patch.object(config.session, "get_valid_token", return_value="FRESH"), \
         patch("toss_api.requests.post") as mp:
        token = toss_api.get_access_token(force_refresh=True, stale_token="DEAD")
    assert token == "FRESH"
    mp.assert_not_called()


def test_concurrent_401_issues_token_only_once():
    """[폭주 방지] 여러 스레드가 동시에 401을 맞아도 실제 발급은 1회여야 한다.

    [2026-08-04] 운영 로그에 401 warning이 10줄 연달아 찍혀 토큰 폭주가 의심됐다.
    실제로는 '1회 발급 + 9회 재사용'이며(경고는 재시도한 스레드 수만큼 남는다),
    저마다 재발급하면 서로의 토큰을 무효화해 무한 401 루프가 된다. 그 경계를 고정한다.
    """
    import threading

    stale = "STALE"
    issued = []
    lock = threading.Lock()
    store = {"tok": stale}

    def fake_post(url, **kw):
        with lock:
            issued.append(1)
            tok = f"NEW-{len(issued)}"
        return FakeResponse(200, {"access_token": tok, "expires_in": 3600})

    config.session.toss_app_key = "K"
    config.session.toss_app_secret = "S"
    results = []

    with patch("toss_api.requests.post", side_effect=fake_post), \
         patch.object(config.session, "get_valid_token", side_effect=lambda _p: store["tok"]), \
         patch.object(config.session, "set_token",
                      side_effect=lambda _p, t, _e: store.__setitem__("tok", t)):
        def worker():
            results.append(toss_api.get_access_token(force_refresh=True, stale_token=stale))

        threads = [threading.Thread(target=worker) for _ in range(10)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

    assert len(issued) == 1, f"토큰이 {len(issued)}회 발급됐다 — 서로 무효화하는 폭주"
    assert len(set(results)) == 1, "스레드마다 다른 토큰을 받았다"
    assert results[0] != stale


def test_resolve_account_seq_matches_acc_num():
    config.session.toss_account_seq = None
    config.session.toss_acc_num = "12345678901"
    config.session.cano = ""
    accounts = [
        {"accountNo": "99999999999", "accountSeq": 7, "accountType": "BROKERAGE"},
        {"accountNo": "12345678901", "accountSeq": 3, "accountType": "BROKERAGE"},
    ]
    with patch("toss_api.get_accounts", return_value=accounts):
        seq = toss_api.resolve_account_seq(force=True)
    assert seq == 3
    assert config.session.toss_account_seq == 3


def test_resolve_account_seq_fallback_first():
    config.session.toss_account_seq = None
    config.session.toss_acc_num = ""
    config.session.cano = ""
    accounts = [{"accountNo": "55555555555", "accountSeq": 1, "accountType": "BROKERAGE"}]
    with patch("toss_api.get_accounts", return_value=accounts):
        seq = toss_api.resolve_account_seq(force=True)
    assert seq == 1


def test_get_price_returns_first_row():
    rows = [{"symbol": "005930", "lastPrice": "72000", "currency": "KRW"}]
    with patch("toss_api._request", return_value=rows) as mp:
        result = toss_api.get_price("005930")
    assert result["lastPrice"] == "72000"
    # prices 엔드포인트로 라우팅되는지 확인
    args, kwargs = mp.call_args
    assert "/api/v1/prices" in args[1]


def test_request_error_envelope_raises():
    config.session.toss_account_seq = 1
    err_body = {"error": {"requestId": "R1", "code": "stock-not-found", "message": "없음"}}
    with patch("toss_api.get_access_token", return_value="TOK"), \
         patch("toss_api.requests.get", return_value=FakeResponse(404, err_body)):
        try:
            toss_api.get_orderbook("000000")
            assert False, "TossApiError가 발생해야 함"
        except toss_api.TossApiError as e:
            assert e.code == "stock-not-found"
            assert e.status == 404


def test_domestic_balance_adapter_kr_shape():
    """토스 holdings → KIS get_domestic_balance(output1/output2) 변환 검증."""
    import api
    holdings = {
        "items": [
            {"symbol": "005930", "name": "삼성전자", "marketCountry": "KR", "currency": "KRW",
             "quantity": "10", "lastPrice": "72000", "averagePurchasePrice": "65000",
             "marketValue": {"amount": "720000"},
             "profitLoss": {"amount": "70000", "rate": "0.1077"}},
            {"symbol": "AAPL", "name": "Apple", "marketCountry": "US", "currency": "USD",
             "quantity": "5", "lastPrice": "180", "averagePurchasePrice": "150",
             "marketValue": {"amount": "900"}, "profitLoss": {"amount": "150", "rate": "0.2"}},
        ]
    }
    config.session.is_toss = True
    try:
        with patch("toss_api.get_holdings", return_value=holdings), \
             patch("toss_api.get_buying_power", return_value={"currency": "KRW", "cashBuyingPower": "5000000"}):
            output1, output2 = api.get_domestic_balance()
    finally:
        config.session.is_toss = False

    # KR 종목만 output1 에 포함
    assert len(output1) == 1
    row = output1[0]
    assert row["pdno"] == "005930"
    assert row["hldg_qty"] == "10"
    assert row["evlu_amt"] == "720000"
    assert row["evlu_pfls_rt"] == "10.77"  # 0.1077 → 10.77%
    # 예수금/요약
    summary = output2[0]
    assert summary["dnca_tot_amt"] == "5000000"
    assert summary["scts_evlu_amt"] == "720000"


def test_overseas_balance_adapter_us_only():
    import api
    holdings = {
        "items": [
            {"symbol": "005930", "name": "삼성전자", "marketCountry": "KR",
             "quantity": "10", "averagePurchasePrice": "65000",
             "marketValue": {"amount": "720000"}, "profitLoss": {"amount": "70000", "rate": "0.1"}},
            {"symbol": "AAPL", "name": "Apple", "marketCountry": "US", "market": "NASDAQ",
             "quantity": "5", "lastPrice": "180", "averagePurchasePrice": "150",
             "profitLoss": {"amount": "150", "rate": "0.2"}},
        ]
    }
    config.session.is_toss = True
    try:
        with patch("toss_api.get_holdings", return_value=holdings):
            out = api.get_overseas_balance()
    finally:
        config.session.is_toss = False
    assert len(out) == 1
    assert out[0]["ovrs_pdno"] == "AAPL"
    assert out[0]["ord_psbl_qty"] == "5.0"
    assert out[0]["evlu_pfls_rt"] == "20.0"


def test_deposit_balance_adapter():
    import api
    config.session.is_toss = True
    try:
        with patch("toss_api.get_buying_power", return_value={"currency": "KRW", "cashBuyingPower": "1234567"}):
            res = api.get_deposit_balance()
    finally:
        config.session.is_toss = False
    assert res["deposit"] == 1234567
    assert res["order_possible"] == 1234567
    assert res["foreign_deposit"] == 0


def test_chart_data_adapter_daily():
    import api
    res = {
        "candles": [
            {"timestamp": "2026-03-25T09:00:00+09:00", "openPrice": "71600", "highPrice": "72300",
             "lowPrice": "71500", "closePrice": "72000", "volume": "3521000", "currency": "KRW"},
            {"timestamp": "2026-03-24T09:00:00+09:00", "openPrice": "71200", "highPrice": "71800",
             "lowPrice": "71000", "closePrice": "71600", "volume": "2984000", "currency": "KRW"},
        ],
        "nextBefore": None,
    }
    config.session.is_toss = True
    try:
        # 국내 일봉은 KRX 소스(pykrx/FDR)가 1순위다. 이 테스트는 '토스 캔들 어댑터'를 검증하므로
        # KRX 경로를 비활성화해 폴백을 강제한다(네트워크 격리 목적도 겸함).
        with patch("api._krx_daily_chart", return_value=None), \
             patch("toss_api.get_candles", return_value=res), \
             patch("toss_api.get_price_limit",
                   side_effect=toss_api.TossApiError("network-error", "mock")):  # 기준가 보정 경로 차단(네트워크 격리)
            df = api.get_chart_data("005930", is_overseas=False, period_type='daily')
    finally:
        config.session.is_toss = False
    assert list(df.columns) == ['date', 'open', 'high', 'low', 'close', 'volume']
    # 오름차순 정렬: 마지막 행이 최신(03-25)
    assert df.iloc[-1]['date'] == '20260325'
    assert df.iloc[-1]['close'] == 72000.0


def test_chart_data_adapter_daily_paginates_to_250():
    """일봉은 nextBefore 커서로 250개 이상 모으는지 검증 (52주/EMA 정확도)."""
    import api

    from datetime import datetime as _dt, timedelta as _td
    _base = _dt(2026, 6, 1)

    def make_page(start_idx):
        candles = []
        for i in range(start_idx, start_idx + 200):
            # i가 클수록 과거 날짜(고유)
            ts = (_base - _td(days=i)).strftime("%Y-%m-%dT09:00:00+09:00")
            candles.append({
                "timestamp": ts,
                "openPrice": "100", "highPrice": "110", "lowPrice": "90",
                "closePrice": str(100 + i), "volume": "1000",
            })
        return candles

    pages = [
        {"candles": make_page(0), "nextBefore": "P2"},
        {"candles": make_page(200), "nextBefore": "P3"},
    ]
    calls = {"n": 0}

    def fake_candles(code, interval="1d", count=100, before=None, adjusted=True):
        idx = calls["n"]
        calls["n"] += 1
        return pages[idx] if idx < len(pages) else {"candles": [], "nextBefore": None}

    config.session.is_toss = True
    try:
        # 어댑터의 페이징만 검증한다(get_chart_data는 일봉 캐시 레이어를 덧대므로 직접 호출).
        with patch("toss_api.get_candles", side_effect=fake_candles), \
             patch("toss_api.get_price_limit",
                   side_effect=toss_api.TossApiError("network-error", "mock")):  # 기준가 보정 경로 차단(네트워크 격리)
            df = api._toss_chart_data("005930", period_type='daily', is_overseas=False)
    finally:
        config.session.is_toss = False

    # 2페이지(>=260) 확보 후 중단, tail(250) 적용
    assert calls["n"] == 2
    assert len(df) == 250


def _inject_daily_chart(code, rows):
    """기준가 판별용 일봉을 메모리 차트 캐시에 주입한다. rows: [{'date','close'}, ...] (과거→최신)"""
    import api
    import pandas as pd
    from datetime import datetime as _dt
    df = pd.DataFrame(rows)
    api._CHART_CACHE[api._chart_cache_key(code, False, False)] = {
        'df': df, 'timestamp': _dt.now(), 'date': _dt.now().strftime('%Y-%m-%d')}


def _pop_daily_chart(code):
    import api
    api._CHART_CACHE.pop(api._chart_cache_key(code, False, False), None)


# 과거 거래일 일봉(항상 오늘보다 과거) → ref_date = 마지막 캔들일(20260713)
PAST_CHART = [{'date': '20260710', 'close': 999.0}, {'date': '20260713', 'close': 285000.0}]


def _reset_krx_store(entries=None):
    """KRX 마감가 저장소 + 랭킹 basePrice 캐시를 테스트용으로 초기화(네트워크/디스크 차단).

    랭킹 캐시를 '오늘자 빈 맵'으로 세팅해 _toss_ranking_base가 네트워크 없이 None(1순위 미스)을
    반환하게 한다 → 2/3순위 경로를 격리 검증할 수 있다.
    """
    import api
    from datetime import datetime as _dt
    api._toss_krx_close_store = entries if entries is not None else {}
    api._toss_rank_base_map = {}
    api._toss_rank_base_day = _dt.now().strftime('%Y%m%d')


def _set_rank_base(mp):
    """랭킹 basePrice 맵(1순위)을 오늘자로 주입한다."""
    import api
    from datetime import datetime as _dt
    api._toss_rank_base_map = dict(mp)
    api._toss_rank_base_day = _dt.now().strftime('%Y%m%d')


def test_toss_base_price_uses_ranking_base_first():
    """[우선순위 1] 랭킹 basePrice가 있으면 저장분·NXT 캔들을 보지 않고 그 값을 쓴다(단락 평가)."""
    import api
    _reset_krx_store({"TSTR": {"20260713": 284000.0}})  # 저장분(2순위)도 있지만
    _set_rank_base({"TSTR": 286000.0})                  # 랭킹(1순위)이 이김
    _inject_daily_chart("TSTR", PAST_CHART)             # NXT 캔들(3순위)=285000
    config.session.is_toss = True
    try:
        assert api._toss_base_price("TSTR") == 286000.0  # 랭킹 basePrice
    finally:
        config.session.is_toss = False
        _pop_daily_chart("TSTR")


def test_toss_ranking_base_builds_map_and_caches():
    """랭킹 basePrice 맵: 거래대금+거래량 각 100을 하루 1회 적재하고 재호출 시 캐시 사용."""
    import api
    api._toss_rank_base_map = None
    api._toss_rank_base_day = None
    resp = {"rankings": [
        {"symbol": "005930", "price": {"lastPrice": "72000", "basePrice": "71600"}},
        {"symbol": "000660", "price": {"lastPrice": "180000", "basePrice": "178000"}},
    ]}
    config.session.is_toss = True
    try:
        with patch("toss_api.get_rankings", return_value=resp) as m:
            assert api._toss_ranking_base("005930") == 71600.0
            assert api._toss_ranking_base("000660") == 178000.0
            assert api._toss_ranking_base("999999") is None  # 랭킹 밖 → None(하위순위로)
            assert m.call_count == 2  # 최초 1회 적재(대금+거래량), 이후 캐시
    finally:
        config.session.is_toss = False
        api._toss_rank_base_map = None
        api._toss_rank_base_day = None


def test_toss_base_price_falls_back_to_prev_nxt_candle():
    """[최종 폴백] 저장분·yfinance가 모두 없으면 전일 NXT 종가(일봉 직전 캔들)로 계산한다(역산 없음)."""
    import api
    _reset_krx_store({})  # 저장분 없음
    _inject_daily_chart("TSTA", PAST_CHART)  # 마지막 캔들 20260713 < 오늘 → 그 종가
    try:
        with patch("toss_api.get_price_limit") as m_pl, \
             patch.object(api, "_toss_krx_lib_close", return_value=None), \
             patch.object(api, "_toss_yf_krx_close", return_value=None):  # 3순위(KRX·yfinance) 미스
            assert api._toss_base_price("TSTA") == 285000.0  # NXT 캔들 종가
            m_pl.assert_not_called()  # 역산 없음
    finally:
        _pop_daily_chart("TSTA")


def test_toss_base_price_uses_stored_krx_close_first():
    """[우선순위 1] ref_date에 저장된 KRX 정규장 마감가가 있으면 NXT 폴백보다 그 값을 쓴다(HTS 일치)."""
    import api
    _reset_krx_store({"TSTB": {"20260713": 284000.0}})  # 캡처된 KRX 마감가
    _inject_daily_chart("TSTB", PAST_CHART)  # ref_date=20260713, NXT 캔들=285000
    try:
        assert api._toss_base_price("TSTB") == 284000.0  # 저장분 우선(285000 NXT 아님)
    finally:
        _pop_daily_chart("TSTB")


def test_toss_base_price_ref_date_is_prev_when_today_candle_exists():
    """일봉 마지막 캔들이 '오늘'이면(마감 후 오늘 봉 형성) 직전 캔들 종가가 전일 기준가다(폴백)."""
    import api
    from datetime import datetime as _dt
    _reset_krx_store({})
    today = _dt.now().strftime('%Y%m%d')
    _inject_daily_chart("TSTC", [{'date': '20260713', 'close': 285000.0},
                                 {'date': today, 'close': 262500.0}])
    try:
        with patch.object(api, "_toss_krx_lib_close", return_value=None), \
             patch.object(api, "_toss_yf_krx_close", return_value=None):  # 3순위 미스 → NXT 폴백
            assert api._toss_base_price("TSTC") == 285000.0  # 오늘 봉(262500)이 아니라 직전(285000)
    finally:
        _pop_daily_chart("TSTC")


def test_toss_base_price_none_when_insufficient_candles():
    """캔들이 2개 미만이면 기준가를 만들지 않는다(등락률 필드 생략)."""
    import api
    _reset_krx_store({})
    _inject_daily_chart("TSTD", [{'date': '20260713', 'close': 285000.0}])
    try:
        assert api._toss_base_price("TSTD") is None
    finally:
        _pop_daily_chart("TSTD")


def test_toss_capture_krx_close_stores_1530_bar_once(tmp_path):
    """마감 후 정규장 분봉의 마지막(15:30) 봉 종가를 오늘 KRX 마감가로 1회 저장(재조회 안 함)."""
    import api
    import pandas as pd
    from datetime import datetime as _dt
    _reset_krx_store({})
    today = _dt.now().strftime('%Y%m%d')
    now = _dt.now()
    intraday = pd.DataFrame([  # _toss_chart_data 분봉(정규장 필터) 결과 형태
        {'date': now.replace(hour=15, minute=29), 'open': 0, 'high': 0, 'low': 0, 'close': 283000.0, 'volume': 0},
        {'date': now.replace(hour=15, minute=30), 'open': 0, 'high': 0, 'low': 0, 'close': 284000.0, 'volume': 0},
    ])
    config.session.is_toss = True
    try:
        with patch.object(api, "_toss_after_krx_close", return_value=True), \
             patch.object(api, "_toss_krx_close_path", return_value=str(tmp_path / "krx.json")), \
             patch.object(api, "_toss_chart_data", return_value=intraday) as m_chart:
            api._toss_capture_krx_close("TSTE")
            assert api._toss_krx_close_get("TSTE", today) == 284000.0  # 15:30 봉 종가
            api._toss_capture_krx_close("TSTE")  # 이미 저장됨 → 분봉 재조회 없음
            assert m_chart.call_count == 1
    finally:
        config.session.is_toss = False


def test_toss_capture_krx_close_skips_before_close():
    """마감(15:35) 전에는 캡처하지 않는다(장중 분봉 마지막≠KRX 마감가)."""
    import api
    _reset_krx_store({})
    config.session.is_toss = True
    try:
        with patch.object(api, "_toss_after_krx_close", return_value=False), \
             patch.object(api, "_toss_chart_data") as m_chart:
            api._toss_capture_krx_close("TSTF")
            m_chart.assert_not_called()
    finally:
        config.session.is_toss = False


def _daily_candles(rows):
    """[(days_ago, o, h, l, c), ...] → 토스 캔들 응답(최신 우선)."""
    from datetime import datetime as _dt, timedelta as _td
    today = _dt.now()
    return {"candles": [
        {"timestamp": (today - _td(days=d)).strftime("%Y-%m-%dT09:00:00+09:00"),
         "openPrice": str(o), "highPrice": str(h), "lowPrice": str(l),
         "closePrice": str(c), "volume": "1000"}
        for d, o, h, l, c in rows
    ], "nextBefore": None}


def test_daily_sanitize_drops_isolated_low_outlier():
    """이웃 봉에서 고립된 저가(NXT 프리마켓 하한가 체결)를 제거한다(KT 2025-09-03 실측 형태)."""
    import api
    # 가운데 봉: 시가·저가만 36,500(이웃 저가 51,400 대비 -29%), 종가는 52,500으로 정상
    res = _daily_candles([(1, 52300, 53000, 52100, 52700),
                          (2, 36500, 53700, 36500, 52500),
                          (3, 53200, 53200, 51400, 52200)])
    with patch("toss_api.get_candles", return_value=res):
        df = api._toss_chart_data("030200", period_type='daily', is_overseas=False)
    bar = df.iloc[-2]
    assert float(bar['low']) == 51400.0     # 이웃 저가 수준으로 복원(가짜 하한가 제거)
    assert float(bar['open']) == 51400.0    # 같은 이상치였던 시가도 함께 교정
    assert float(bar['high']) == 53700.0    # 고가는 손대지 않는다
    assert float(bar['close']) == 52500.0   # 종가는 어떤 경우에도 무보정


def test_daily_sanitize_keeps_real_limit_down_day():
    """진짜 하한가 마감일(종가도 저가까지 내려감)은 원본이 보존된다."""
    import api
    res = _daily_candles([(1, 30100, 30500, 30000, 30200),
                          (2, 30050, 30050, 30050, 30050),   # -29.95% 하한가 마감
                          (3, 43000, 43500, 42800, 42900)])
    with patch("toss_api.get_candles", return_value=res):
        df = api._toss_chart_data("950160", period_type='daily', is_overseas=False)
    bar = df.iloc[-2]
    assert float(bar['low']) == 30050.0 and float(bar['open']) == 30050.0


def test_daily_sanitize_keeps_normal_spike_high():
    """장중 급등 후 밀린 정상 봉(고가 +29%)은 건드리지 않는다 — 고가는 판별 축이 아니다."""
    import api
    res = _daily_candles([(1, 96000, 99000, 95000, 97000),
                          (2, 78000, 129000, 77000, 98000),  # 전일 종가 100,000 대비 고가 +29%
                          (3, 99000, 101000, 96000, 100000)])
    with patch("toss_api.get_candles", return_value=res):
        df = api._toss_chart_data("079550", period_type='daily', is_overseas=False)
    bar = df.iloc[-2]
    assert float(bar['high']) == 129000.0   # 진짜 급등 고가 보존
    assert float(bar['low']) == 77000.0     # 이웃 대비 고립도 -19% → 임계(20%) 미만이라 유지


def test_daily_sanitize_skips_overseas():
    """해외는 가격제한폭이 없어 판정이 성립하지 않으므로 보정하지 않는다."""
    import api
    res = _daily_candles([(1, 52.3, 53.0, 52.1, 52.7),
                          (2, 36.5, 53.7, 36.5, 52.5),
                          (3, 53.2, 53.2, 51.4, 52.2)])
    with patch("toss_api.get_candles", return_value=res):
        df = api._toss_chart_data("TSLA", period_type='daily', is_overseas=True)
    assert float(df.iloc[-2]['low']) == 36.5


def test_chart_data_adapter_daily_keeps_nxt_close():
    """국내 일봉: 직전 거래일 봉 종가를 NXT 연장(~20:00) 종가 '그대로' 둔다(KRX 역산 보정 폐기)."""
    import api
    from datetime import datetime as _dt, timedelta as _td
    today = _dt.now()
    ts_today = today.strftime("%Y-%m-%dT09:00:00+09:00")
    ts_prev = (today - _td(days=3)).strftime("%Y-%m-%dT09:00:00+09:00")
    res = {"candles": [
        {"timestamp": ts_today, "openPrice": "285000", "highPrice": "292500",
         "lowPrice": "262000", "closePrice": "262500", "volume": "100"},
        {"timestamp": ts_prev, "openPrice": "285000", "highPrice": "298000",
         "lowPrice": "282000", "closePrice": "286500", "volume": "100"},  # NXT 20:00 연장 종가
    ], "nextBefore": None}
    with patch("toss_api.get_candles", return_value=res), \
         patch.object(api, "market_today", return_value=today.strftime("%Y%m%d")):
        df = api._toss_chart_data("005930", period_type='daily', is_overseas=False)
    assert float(df.iloc[-2]['close']) == 286500.0  # NXT 종가 그대로(더 이상 285000으로 보정 안 함)
    assert float(df.iloc[-1]['close']) == 262500.0  # 당일 봉도 그대로


def test_chart_data_adapter_intraday_session_window():
    """분봉은 nextBefore로 페이징하여 최신 거래일의 09:00~15:30(정규장)만 표시(KIS와 동일)."""
    import api

    from datetime import datetime as _dt, timedelta as _td
    _base = _dt(2026, 6, 23, 15, 30)  # 정규장 마감 시각 기준 과거로 분 단위 캔들 생성

    def make_page(start_idx):
        candles = []
        for i in range(start_idx, start_idx + 200):
            ts = (_base - _td(minutes=i)).strftime("%Y-%m-%dT%H:%M:00+09:00")
            candles.append({
                "timestamp": ts,
                "openPrice": "100", "highPrice": "110", "lowPrice": "90",
                "closePrice": str(100 + i), "volume": "1000",
            })
        return candles

    # 0~389분 전(=당일 09:01~15:30) + 그 이전(장전/전일, 윈도우 밖)
    pages = [
        {"candles": make_page(0), "nextBefore": "P2"},
        {"candles": make_page(200), "nextBefore": "P3"},
        {"candles": make_page(400), "nextBefore": "P4"},
    ]
    calls = {"n": 0}

    def fake_candles(code, interval="1d", count=100, before=None, adjusted=True):
        assert interval == "1m"
        idx = calls["n"]
        calls["n"] += 1
        return pages[idx] if idx < len(pages) else {"candles": [], "nextBefore": None}

    config.session.is_toss = True
    try:
        with patch("toss_api.get_candles", side_effect=fake_candles):
            df = api.get_chart_data("005930", period_type='intraday')
    finally:
        config.session.is_toss = False

    # 모든 행이 당일 09:00~15:30 구간 내
    assert (df['date'].dt.hour >= 9).all()
    assert ((df['date'].dt.hour < 15) | ((df['date'].dt.hour == 15) & (df['date'].dt.minute <= 30))).all()
    assert df['date'].dt.normalize().nunique() == 1  # 단일 거래일
    # 경계 확인 (15:30 포함, 09:00 이전 제외)
    assert df.iloc[-1]['date'].strftime("%H:%M") == "15:30"
    assert df['date'].dt.hour.min() == 9


def test_chart_data_adapter_intraday_paginates_when_nextbefore_missing():
    """분봉에서 nextBefore가 없어도 가장 오래된 timestamp를 before 커서로 폴백해 09:00까지 확보."""
    import api

    from datetime import datetime as _dt, timedelta as _td, timezone as _tz
    _kst = _tz(_td(hours=9))
    _base = _dt(2026, 6, 23, 15, 30, tzinfo=_kst)

    def page_before(before):
        # before(가장 오래된 timestamp ISO) 이전 200개를 생성. before=None이면 최신부터.
        if before is None:
            start = 0
        else:
            bt = _dt.fromisoformat(before)
            start = int((_base - bt).total_seconds() // 60) + 1
        candles = []
        for i in range(start, start + 200):
            ts = (_base - _td(minutes=i)).strftime("%Y-%m-%dT%H:%M:00+09:00")
            candles.append({
                "timestamp": ts,
                "openPrice": "100", "highPrice": "110", "lowPrice": "90",
                "closePrice": str(100 + i), "volume": "1000",
            })
        # nextBefore는 항상 None → 폴백(oldest timestamp) 경로를 강제
        return {"candles": candles, "nextBefore": None}

    calls = {"n": 0}

    def fake_candles(code, interval="1d", count=100, before=None, adjusted=True):
        assert interval == "1m"
        calls["n"] += 1
        return page_before(before)

    config.session.is_toss = True
    try:
        with patch("toss_api.get_candles", side_effect=fake_candles):
            df = api.get_chart_data("005930", period_type='intraday')
    finally:
        config.session.is_toss = False

    # nextBefore가 None이어도 2페이지 이상 페이징하여 09:00을 확보해야 한다
    assert calls["n"] >= 2
    assert df['date'].dt.hour.min() == 9
    assert df.iloc[-1]['date'].strftime("%H:%M") == "15:30"


def test_chart_data_adapter_intraday_today_session_only():
    """KIS 당일분봉과 동일: 당일(최근 거래일) 정규장(09:00~15:30)만 표시.
    시간외(NXT)·전일 캔들은 제외. 장전이면 당일 정규장 데이터가 없어 빈 값(→ 장전 안내)."""
    import api

    def C(ts):
        return {"timestamp": ts, "openPrice": "100", "highPrice": "110",
                "lowPrice": "90", "closePrice": "105", "volume": "1000"}

    # 당일(23일): 장전 NXT(08:30) + 정규장(09:00/12:00/15:30) + 시간외 NXT(18:00)
    # 전일(22일): 정규장(15:00) → 당일이 아니므로 제외
    one_page = {
        "candles": [
            C("2026-06-23T18:00:00+09:00"),   # 당일 시간외(NXT) → 제외
            C("2026-06-23T15:30:00+09:00"),
            C("2026-06-23T12:00:00+09:00"),
            C("2026-06-23T09:00:00+09:00"),
            C("2026-06-23T08:30:00+09:00"),   # 당일 장전(NXT) → 제외
            C("2026-06-22T15:00:00+09:00"),   # 전일 → 제외
        ],
        "nextBefore": None,
    }

    def fake_candles(code, interval="1d", count=100, before=None, adjusted=True):
        return one_page if before is None else {"candles": [], "nextBefore": None}

    config.session.is_toss = True
    try:
        with patch("toss_api.get_candles", side_effect=fake_candles):
            df = api.get_chart_data("005930", period_type='intraday')
    finally:
        config.session.is_toss = False

    # 당일(23일) 정규장 09:00~15:30만 (08:30/18:00/전일 제외)
    assert df['date'].dt.normalize().nunique() == 1
    assert df['date'].dt.day.unique().tolist() == [23]
    assert df.iloc[0]['date'].strftime("%H:%M") == "09:00"
    assert df.iloc[-1]['date'].strftime("%H:%M") == "15:30"
    assert len(df) == 3  # 09:00, 12:00, 15:30


def test_chart_data_adapter_intraday_premarket_returns_empty():
    """장전 조회: 당일(최근 거래일)에 정규장 데이터가 없으면 빈 값을 반환(전일로 폴백하지 않음)."""
    import api

    def C(ts):
        return {"timestamp": ts, "openPrice": "100", "highPrice": "110",
                "lowPrice": "90", "closePrice": "105", "volume": "1000"}

    # 당일(23일) 장전 NXT만 존재 + 전일(22일) 정규장 → 당일 정규장이 없으므로 빈 값
    one_page = {
        "candles": [
            C("2026-06-23T08:22:00+09:00"),
            C("2026-06-23T08:00:00+09:00"),
            C("2026-06-22T15:30:00+09:00"),
            C("2026-06-22T09:00:00+09:00"),
        ],
        "nextBefore": None,
    }

    def fake_candles(code, interval="1d", count=100, before=None, adjusted=True):
        return one_page if before is None else {"candles": [], "nextBefore": None}

    config.session.is_toss = True
    try:
        with patch("toss_api.get_candles", side_effect=fake_candles):
            df = api.get_chart_data("005930", period_type='intraday')
    finally:
        config.session.is_toss = False

    assert df.empty  # 당일 정규장 없음 → 빈 값(호출부에서 장전 안내)


def test_chart_data_adapter_hourly_unsupported():
    import api
    config.session.is_toss = True
    try:
        df = api.get_chart_data("005930", period_type='hourly')
    finally:
        config.session.is_toss = False
    assert df.empty


def test_chart_data_adapter_intraday_date_is_timestamp():
    """분봉 date는 KIS와 동일하게 Timestamp여야 차트 X축 라벨('MM-DD HH:MM')이 동일 출력된다."""
    import api
    import pandas as pd
    res = {
        "candles": [
            {"timestamp": "2026-06-23T09:31:00+09:00", "openPrice": "72000", "highPrice": "72100",
             "lowPrice": "71900", "closePrice": "72050", "volume": "12000", "currency": "KRW"},
            {"timestamp": "2026-06-23T09:30:00+09:00", "openPrice": "71900", "highPrice": "72000",
             "lowPrice": "71800", "closePrice": "71950", "volume": "15000", "currency": "KRW"},
        ],
        "nextBefore": None,
    }
    config.session.is_toss = True
    try:
        with patch("toss_api.get_candles", return_value=res):
            df = api.get_chart_data("005930", is_overseas=False, period_type='intraday')
    finally:
        config.session.is_toss = False
    assert list(df.columns) == ['date', 'open', 'high', 'low', 'close', 'volume']
    # date는 문자열이 아닌 datetime64(Timestamp) → strftime 경로로 KIS와 동일 라벨
    assert pd.api.types.is_datetime64_any_dtype(df['date'])
    # 오름차순: 마지막 행이 최신(09:31), tz는 제거된 naive KST
    last = df.iloc[-1]['date']
    assert last.strftime("%m-%d %H:%M") == "06-23 09:31"
    assert last.tzinfo is None


def test_current_price_data_adapter():
    import api
    config.session.is_toss = True
    _reset_krx_store({})  # 저장된 KRX 마감가 없음 → NXT 폴백 경로
    # 전일 NXT 종가(=일봉 직전 캔들 종가) 71600 → 등락률 기준가
    _inject_daily_chart("005930", [{'date': '20260710', 'close': 71000.0},
                                   {'date': '20260713', 'close': 71600.0}])
    try:
        with patch("toss_api.get_price",
                   return_value={"symbol": "005930", "lastPrice": "72000", "currency": "KRW"}), \
             patch.object(api, "_toss_capture_krx_close"), \
             patch.object(api, "_toss_krx_lib_close", return_value=None), \
             patch.object(api, "_toss_yf_krx_close", return_value=None):  # 캡처·KRX·yfinance 격리
            res = api.get_current_price_data("005930", False)
            price = api.get_current_price("005930", False)
    finally:
        config.session.is_toss = False
        _pop_daily_chart("005930")
    assert res['rt_cd'] == '0'
    assert res['output']['stck_prpr'] == '72000'
    assert price == 72000
    # [추가] 국내는 전일 NXT 종가(=일봉 직전 캔들 종가) 기준으로 KIS 호환 전일대비 필드를 채운다
    assert res['output']['stck_sdpr'] == '71600'
    assert res['output']['prdy_vrss'] == '400'
    assert res['output']['prdy_ctrt'] == '0.56'


def test_current_price_data_keeps_last_session_rate_before_nxt_open():
    """NXT 미지원 종목: 다음날 NXT 개장(08:00) 전엔 현재가=기준가여도 직전 정규장 등락률 유지."""
    import api
    config.session.is_toss = True
    _reset_krx_store({})
    _set_rank_base({"005930": 285000.0})  # 기준가(전일 종가) = 현재가와 동일 → 0% 상황
    # 일봉: 전전일(280000) → 전일(285000). NXT 개장 전 오늘 캔들 없음.
    _inject_daily_chart("005930", [{'date': '20260713', 'close': 280000.0},
                                   {'date': '20260714', 'close': 285000.0}])
    try:
        with patch("toss_api.get_price",
                   return_value={"symbol": "005930", "lastPrice": "285000", "currency": "KRW"}), \
             patch.object(api, "_toss_capture_krx_close"), \
             patch.object(api, "_toss_before_nxt_open", return_value=True):
            res = api.get_current_price_data("005930", False)
    finally:
        config.session.is_toss = False
        _pop_daily_chart("005930")
    # 기준가를 전전일(280000)로 대체 → 직전 정규장 최종 등락률(전일 vs 전전일) 유지
    assert res['output']['stck_prpr'] == '285000'
    assert res['output']['stck_sdpr'] == '280000'
    assert res['output']['prdy_vrss'] == '5000'
    assert res['output']['prdy_ctrt'] == '1.79'


def test_current_price_data_zero_rate_after_nxt_open():
    """NXT 개장(08:00) 후엔 대체하지 않는다: 미지원 종목은 현재가=기준가 → 0% 노출."""
    import api
    config.session.is_toss = True
    _reset_krx_store({})
    _set_rank_base({"005930": 285000.0})
    _inject_daily_chart("005930", [{'date': '20260713', 'close': 280000.0},
                                   {'date': '20260714', 'close': 285000.0}])
    try:
        with patch("toss_api.get_price",
                   return_value={"symbol": "005930", "lastPrice": "285000", "currency": "KRW"}), \
             patch.object(api, "_toss_capture_krx_close"), \
             patch.object(api, "_toss_before_nxt_open", return_value=False):
            res = api.get_current_price_data("005930", False)
    finally:
        config.session.is_toss = False
        _pop_daily_chart("005930")
    assert res['output']['stck_sdpr'] == '285000'
    assert res['output']['prdy_ctrt'] == '0.0'


def test_toss_before_nxt_open_boundary():
    """08:00 경계: 08:00 직전 True, 08:00·09:00 False (거래일 가정)."""
    import api
    from datetime import datetime as _dt

    class _FrozenDT(_dt):
        _now = None
        @classmethod
        def now(cls, tz=None):
            return cls._now

    today = _dt.now().strftime('%Y%m%d')
    cases = {'0759': True, '0800': False, '0830': False, '0900': False}
    for hhmm, expected in cases.items():
        _FrozenDT._now = _dt.strptime(today + hhmm, '%Y%m%d%H%M')
        with patch.object(api, 'datetime', _FrozenDT), \
             patch.object(api, 'market_today', return_value=today):
            assert api._toss_before_nxt_open() is expected, hhmm


def test_toss_before_nxt_open_holiday_all_day():
    """휴장일(주말·공휴일, market_today != 오늘): 시각과 무관하게 항상 True — 다음 NXT 개장까지 유지."""
    import api
    from datetime import datetime as _dt

    class _FrozenDT(_dt):
        _now = None
        @classmethod
        def now(cls, tz=None):
            return cls._now

    # 2026-07-18(토): 직전 거래일은 2026-07-17(금)
    for hhmm in ('0700', '0800', '1200', '2300'):
        _FrozenDT._now = _dt.strptime('20260718' + hhmm, '%Y%m%d%H%M')
        with patch.object(api, 'datetime', _FrozenDT), \
             patch.object(api, 'market_today', return_value='20260717'):
            assert api._toss_before_nxt_open() is True, hhmm


def test_order_book_adapter_totals():
    import api
    ob = {
        "currency": "KRW",
        "asks": [{"price": "72300", "volume": "1200"}, {"price": "72200", "volume": "3400"}],
        "bids": [{"price": "72000", "volume": "5200"}, {"price": "71900", "volume": "4100"}],
    }
    config.session.is_toss = True
    try:
        with patch("toss_api.get_orderbook", return_value=ob):
            res = api.get_order_book("005930", False)
    finally:
        config.session.is_toss = False
    assert res['rt_cd'] == '0'
    out1 = res['output1']
    assert out1['askp1'] == '72300'
    assert out1['bidp1'] == '72000'
    assert out1['total_askp_rsqn'] == '4600'   # 1200+3400
    assert out1['total_bidp_rsqn'] == '9300'   # 5200+4100


def test_investor_and_vol_strength_na_for_toss():
    import api
    config.session.is_toss = True
    try:
        assert api.get_investor_trend("005930") == []
        assert api.get_realtime_vol_strength("005930") is None
        # 외국인 소진율(외인률)도 토스에선 KIS로 누수되지 않고 빈 값
        assert api.get_daily_foreign_rate("005930") == []
    finally:
        config.session.is_toss = False


def test_print_table_worker_toss_enriches_change_and_52w():
    """토스 모드: 일괄 분석 표에서 등락/52주가 차트로 보강되는지 검증."""
    import pandas as pd
    from modules import analysis
    import api

    n = 250
    closes = [50000 + i * 100 for i in range(n)]  # 오름차순(마지막이 최고가권)
    df = pd.DataFrame({
        'date': [f"2025{(i % 12) + 1:02d}{(i % 28) + 1:02d}" for i in range(n)],
        'open': [float(c) for c in closes],
        'high': [c * 1.01 for c in closes],
        'low': [c * 0.99 for c in closes],
        'close': [float(c) for c in closes],
        'volume': [1000.0 + i for i in range(n)],
    })
    # 실제 토스 일봉은 장중/장후 마지막 봉이 '당일'이다 → 현재가가 당일 봉을 덮어쓰고
    # 등락은 직전 봉(전일) 대비로 계산된다. (마지막 봉이 과거면 프리마켓 보정이 당일봉을 새로 추가)
    import utils
    df.loc[df.index[-1], 'date'] = utils.market_today(False)
    curr = {'rt_cd': '0', 'output': {'stck_prpr': str(closes[-1])}}

    config.session.is_toss = True
    try:
        with patch("api.get_current_price_data", return_value=curr), \
             patch("api.get_chart_data", return_value=df.copy()), \
             patch("api.get_investor_trend", return_value=[]), \
             patch("modules.analysis.check_smart_money_turnaround", return_value=(False, "")):
            result = analysis._print_table_worker(
                ("삼성전자", "005930"), "국내 주식 기술적 분석",
                False, True, set(), {}, {}, set(), set())
    finally:
        config.session.is_toss = False

    row = result[0]
    rate_str = row[4]   # 등락폭 (등락률)
    w52_str = row[5]    # 52주 위치
    assert "+0 (+0.00%)" not in rate_str   # 등락이 0이 아니라 차트로 계산됨
    assert "%" in w52_str and "dim]-" not in w52_str  # 52주 위치 표시됨


def test_print_table_worker_toss_shows_ask_bid_ratio():
    """토스 모드: 일괄 분석 표에서 체결강도([0%]) 대신 매도잔량비(N.NN배)를 표시한다."""
    import pandas as pd
    from modules import analysis

    n = 250
    closes = [50000 + i * 100 for i in range(n)]
    df = pd.DataFrame({
        'date': [f"2025{(i % 12) + 1:02d}{(i % 28) + 1:02d}" for i in range(n)],
        'open': [float(c) for c in closes],
        'high': [c * 1.01 for c in closes],
        'low': [c * 0.99 for c in closes],
        'close': [float(c) for c in closes],
        'volume': [1000.0 + i for i in range(n)],
    })
    curr = {'rt_cd': '0', 'output': {'stck_prpr': str(closes[-1])}}
    ob = {'rt_cd': '0', 'output1': {'total_askp_rsqn': '2000', 'total_bidp_rsqn': '1000'}}  # 2.00배

    config.session.is_toss = True
    try:
        # [추가] 매도잔량비는 NXT 운영시간(08:00~20:00) 게이트가 적용되므로 장중 시각으로 고정
        from datetime import datetime as real_dt
        with patch("api.get_current_price_data", return_value=curr), \
             patch("api.get_chart_data", return_value=df.copy()), \
             patch("api.get_investor_trend", return_value=[]), \
             patch("api.get_order_book", return_value=ob), \
             patch("api.get_realtime_vol_strength", return_value=None), \
             patch("api.is_holiday_today", return_value=False), \
             patch("api.datetime") as mock_api_dt, \
             patch("modules.analysis.check_smart_money_turnaround", return_value=(False, "")):
            mock_api_dt.now.return_value = real_dt(2026, 7, 17, 10, 0)
            result = analysis._print_table_worker(
                ("삼성전자", "005930"), "국내 주식 기술적 분석",
                False, False, set(), {}, {}, set(), set())
    finally:
        config.session.is_toss = False

    rate_str = result[0][4]  # 등락폭 (등락률) [매도비] 셀
    assert "2.00" in rate_str     # 매도잔량비 숫자만 표시
    assert "배" not in rate_str    # '배' 단위 제거
    assert "[0%]" not in rate_str  # 체결강도(강도) 형식 아님


def test_print_table_worker_toss_hides_ask_bid_outside_nxt_hours():
    """토스 모드: NXT 운영시간(08:00~20:00) 밖에는 매도비 표기를 셀에서 생략한다."""
    import pandas as pd
    from datetime import datetime as real_dt
    from modules import analysis

    n = 250
    closes = [50000 + i * 100 for i in range(n)]
    df = pd.DataFrame({
        'date': [f"2025{(i % 12) + 1:02d}{(i % 28) + 1:02d}" for i in range(n)],
        'open': [float(c) for c in closes],
        'high': [c * 1.01 for c in closes],
        'low': [c * 0.99 for c in closes],
        'close': [float(c) for c in closes],
        'volume': [1000.0 + i for i in range(n)],
    })
    curr = {'rt_cd': '0', 'output': {'stck_prpr': str(closes[-1])}}
    ob = {'rt_cd': '0', 'output1': {'total_askp_rsqn': '2000', 'total_bidp_rsqn': '1000'}}

    config.session.is_toss = True
    try:
        with patch("api.get_current_price_data", return_value=curr), \
             patch("api.get_chart_data", return_value=df.copy()), \
             patch("api.get_investor_trend", return_value=[]), \
             patch("api.get_order_book", return_value=ob), \
             patch("api.get_realtime_vol_strength", return_value=None), \
             patch("api.is_holiday_today", return_value=False), \
             patch("api.datetime") as mock_api_dt, \
             patch("modules.analysis.check_smart_money_turnaround", return_value=(False, "")):
            mock_api_dt.now.return_value = real_dt(2026, 7, 17, 22, 30)  # NXT 마감 후 야간
            result = analysis._print_table_worker(
                ("삼성전자", "005930"), "국내 주식 기술적 분석",
                False, False, set(), {}, {}, set(), set())
    finally:
        config.session.is_toss = False

    rate_str = result[0][4]  # 등락폭 (등락률) 셀 — 매도비 접미사 자체가 없어야 함
    assert "2.00" not in rate_str
    assert "[-]" not in rate_str


def test_print_table_worker_toss_us_52w_from_chart_perpbr_na():
    """토스 단독: 미국 종목 52주는 차트로 유지, PER/PBR은 N/A."""
    import pandas as pd
    from modules import analysis

    n = 250
    closes = [100.0 + i * 0.5 for i in range(n)]  # 오름차순
    df = pd.DataFrame({
        'date': [f"2025{(i % 12) + 1:02d}{(i % 28) + 1:02d}" for i in range(n)],
        'open': closes, 'high': [c * 1.01 for c in closes], 'low': [c * 0.99 for c in closes],
        'close': closes, 'volume': [1000.0 + i for i in range(n)],
    })
    curr = {'rt_cd': '0', 'output': {'last': str(closes[-1])}}

    config.session.is_toss = True
    try:
        with patch("api.get_current_price_data", return_value=curr), \
             patch("api.get_chart_data", return_value=df.copy()), \
             patch("api.fetch_overseas_detail_price", return_value=None), \
             patch("modules.analysis.check_smart_money_turnaround", return_value=(False, "")):
            result = analysis._print_table_worker(
                ("Apple Inc.", "AAPL"), "미국 주식 기술적 분석",
                True, False, set(), {}, {}, set(), set())
    finally:
        config.session.is_toss = False

    row = result[0]
    assert "%" in row[5] and "dim]-" not in row[5]   # 52주 위치 차트로 표시됨
    # PER/PBR(마지막 두 컬럼)은 토스 미제공 → N/A
    assert row[-1] == "[dim]-[/dim]" and row[-2] == "[dim]-[/dim]"


def test_overseas_detail_via_tv_for_toss():
    """토스 모드: 해외 PER/PBR/상장주수는 TradingView 스캐너로 조회한다(KIS 미호출)."""
    import api
    from unittest.mock import patch
    config.session.is_toss = True
    api._MICRO_CACHE.clear()
    try:
        with patch.object(api, '_tv_overseas_fundamentals',
                          return_value={'perx': '39.27', 'pbrx': '44.71'}) as m_tv, \
             patch.object(api, 'call_api') as m_kis:
            res = api.fetch_overseas_detail_price("AAPL", "NAS")
        assert res == {'perx': '39.27', 'pbrx': '44.71'}
        m_tv.assert_called_once_with("AAPL")
        m_kis.assert_not_called()  # 토스 모드에서 KIS DETAIL TR 미호출
    finally:
        config.session.is_toss = False
        api._MICRO_CACHE.clear()


def test_rate_limit_group_rps_and_cooldown():
    """그룹별 RPS 설정 및 429 쿨다운 동작."""
    # 2026-07-19 tools/toss_tps_probe.py 실측값 (서버 X-RateLimit-Limit 헤더 기준)
    assert toss_api._group_rps("MARKET_DATA_CHART") == 5   # 실측 5
    assert toss_api._group_rps("MARKET_DATA") == 10        # 실측 10
    assert toss_api._group_rps("STOCK") == 5               # 실측 5
    assert toss_api._group_rps("MARKET_INFO") == 3         # 실측 3
    assert toss_api._group_rps("RANKING") == 5             # 실측 5
    assert toss_api._group_rps("ORDER") == 10              # 미실측 — 종전 값 유지
    assert toss_api._group_rps("AUTH") == 2  # 토큰 발급은 보수적 유지
    # 미정의 그룹은 기본값(config.TOSS_TX_PER_SECOND)
    import config as _cfg
    assert toss_api._group_rps("UNKNOWN_GROUP") == max(_cfg.TOSS_TX_PER_SECOND, 1)

    toss_api._group_cooldown.clear()
    toss_api._note_rate_limited("MARKET_DATA", 2)
    assert toss_api._group_cooldown["MARKET_DATA"] > time.time()


def test_rate_limit_smooths_calls_within_window():
    """그룹 호출이 최소 간격으로 분산되어 즉시 폭주하지 않는다."""
    g = "MARKET_DATA"  # rps=10 → 최소 간격 0.1s
    toss_api._group_hist[g].clear()
    toss_api._group_cooldown.clear()

    t0 = time.time()
    for _ in range(6):
        toss_api._throttle(g)
    elapsed = time.time() - t0
    # 즉시(0초)가 아니라 분산되어야 함 (5구간 × 0.1 ≈ 0.5s)
    assert elapsed >= 0.4
    assert len(toss_api._group_hist[g]) == 6


def test_create_order_builds_body():
    config.session.toss_account_seq = 1
    captured = {}

    def fake_request(method, path, group, params=None, json_body=None, account=True, retries=2):
        captured["method"] = method
        captured["path"] = path
        captured["body"] = json_body
        return {"orderId": "OID", "clientOrderId": None}

    with patch("toss_api._request", side_effect=fake_request):
        res = toss_api.create_order("005930", "BUY", order_type="LIMIT", quantity=10, price=70000)

    assert res["orderId"] == "OID"
    assert captured["method"] == "POST"
    assert captured["body"]["symbol"] == "005930"
    assert captured["body"]["quantity"] == "10"
    assert captured["body"]["price"] == "70000"


# =========================================================================
# 메뉴 8: 주문/미체결/정정·취소 어댑터
# =========================================================================
def _orders_envelope():
    """토스 OPEN 주문 응답(국내 1건 + 해외 1건)."""
    return {
        "orders": [
            {"orderId": "KR1", "symbol": "005930", "side": "BUY", "orderType": "LIMIT",
             "status": "PENDING", "price": "70000", "quantity": "10", "currency": "KRW",
             "orderedAt": "2026-03-29T09:30:00+09:00",
             "execution": {"filledQuantity": "3"}},
            {"orderId": "US1", "symbol": "AAPL", "side": "SELL", "orderType": "LIMIT",
             "status": "PENDING", "price": "185.5", "quantity": "5", "currency": "USD",
             "orderedAt": "2026-03-29T22:00:00+09:00",
             "execution": {"filledQuantity": "0"}},
        ],
        "nextCursor": None, "hasNext": False,
    }


def test_open_orders_domestic_adapter():
    import api
    config.session.is_toss = True
    try:
        with patch("toss_api.get_orders", return_value=_orders_envelope()), \
             patch("toss_api.get_stocks", return_value=[{"symbol": "005930", "name": "삼성전자"}]):
            out = api.get_domestic_open_orders()
    finally:
        config.session.is_toss = False
    assert len(out) == 1  # KRW 종목만
    o = out[0]
    assert o["odno"] == "KR1"
    assert o["pdno"] == "005930"
    assert o["prdt_name"] == "삼성전자"
    assert o["sll_buy_dvsn_cd"] == "02"  # BUY
    assert o["ord_qty"] == "10"
    assert o["rmn_qty"] == "7"           # 10 - 3 체결
    assert o["ord_unpr"] == "70000"
    assert o["ord_tmd"] == "093000"


def test_open_orders_overseas_adapter():
    import api
    config.session.is_toss = True
    try:
        with patch("toss_api.get_orders", return_value=_orders_envelope()), \
             patch("toss_api.get_stocks", return_value=[{"symbol": "AAPL", "name": "Apple"}]):
            out = api.get_overseas_open_orders()
    finally:
        config.session.is_toss = False
    assert len(out) == 1  # USD 종목만
    o = out[0]
    assert o["odno"] == "US1"
    assert o["pdno"] == "AAPL"
    assert o["sll_buy_dvsn_cd"] == "01"  # SELL
    assert o["ft_ord_qty"] == "5"
    assert o["nccs_qty"] == "5"
    assert float(o["ft_ord_unpr3"]) == 185.5


def test_place_order_limit_adapter():
    import api
    config.session.is_toss = True
    captured = {}

    def fake_create(symbol, side, order_type="LIMIT", quantity=None, price=None, **kw):
        captured.update(symbol=symbol, side=side, order_type=order_type,
                        quantity=quantity, price=price)
        return {"orderId": "NEWID"}

    try:
        with patch("toss_api.create_order", side_effect=fake_create):
            res = api.place_order("domestic", "buy", "005930", 10, 70000, "00")
    finally:
        config.session.is_toss = False
    assert res["rt_cd"] == "0"
    assert res["output"]["ODNO"] == "NEWID"
    assert captured["side"] == "BUY"
    assert captured["order_type"] == "LIMIT"
    assert captured["price"] == 70000


def test_place_order_market_adapter():
    """국내 시장가(ord_dvsn='01') → 토스 MARKET, price 미전달."""
    import api
    config.session.is_toss = True
    captured = {}

    def fake_create(symbol, side, order_type="LIMIT", quantity=None, price=None, **kw):
        captured.update(order_type=order_type, price=price)
        return {"orderId": "MKT"}

    try:
        with patch("toss_api.create_order", side_effect=fake_create):
            res = api.place_order("domestic", "sell", "005930", 3, 0, "01")
    finally:
        config.session.is_toss = False
    assert res["rt_cd"] == "0"
    assert captured["order_type"] == "MARKET"
    assert captured["price"] is None


def test_place_order_error_adapter():
    import api
    config.session.is_toss = True
    try:
        with patch("toss_api.create_order",
                   side_effect=toss_api.TossApiError("INSUFFICIENT_CASH", "잔액부족", status=400)):
            res = api.place_order("domestic", "buy", "005930", 10, 70000, "00")
    finally:
        config.session.is_toss = False
    assert res["rt_cd"] == "1"
    assert "잔액부족" in res["msg1"]
    assert res["output"] == {}


def test_cancel_order_adapter():
    import api
    config.session.is_toss = True
    captured = {}

    def fake_cancel(oid):
        captured["oid"] = oid
        return {"orderId": "CXL"}

    try:
        with patch("toss_api.cancel_order", side_effect=fake_cancel):
            res = api.revise_cancel_order("domestic", "cancel", "KR1", "005930", 0, "0", "02", "00")
    finally:
        config.session.is_toss = False
    assert res["rt_cd"] == "0"
    assert res["output"]["ODNO"] == "CXL"
    assert captured["oid"] == "KR1"


def test_modify_order_adapter():
    """정정(전량, req_qty=0) → 토스는 수량 필수이므로 미체결 잔량을 조회해 명시."""
    import api
    config.session.is_toss = True
    captured = {}

    def fake_modify(order_id, order_type="LIMIT", quantity=None, price=None, **kw):
        captured.update(order_id=order_id, quantity=quantity, price=price)
        return {"orderId": "MOD"}

    # 전량(0) 요청 → get_order로 잔량(총 5 - 체결 2 = 3) 조회하여 수량 명시
    order_detail = {"quantity": "5", "execution": {"filledQuantity": "2"}}

    try:
        with patch("toss_api.modify_order", side_effect=fake_modify), \
             patch("toss_api.get_order", return_value=order_detail):
            res = api.revise_cancel_order("domestic", "revise", "KR1", "005930", 0, 71000, "01", "00")
    finally:
        config.session.is_toss = False
    assert res["rt_cd"] == "0"
    assert res["output"]["ODNO"] == "MOD"
    assert captured["order_id"] == "KR1"
    assert captured["quantity"] == 3   # 0 → 미체결 잔량(3)으로 명시
    assert captured["price"] == 71000


def test_modify_order_adapter_explicit_qty():
    """정정에 수량을 명시(2)하면 잔량 조회 없이 그대로 전달."""
    import api
    config.session.is_toss = True
    captured = {}

    def fake_modify(order_id, order_type="LIMIT", quantity=None, price=None, **kw):
        captured.update(quantity=quantity, price=price)
        return {"orderId": "MOD"}

    try:
        with patch("toss_api.modify_order", side_effect=fake_modify):
            res = api.revise_cancel_order("domestic", "revise", "KR1", "005930", 2, 71000, "01", "00")
    finally:
        config.session.is_toss = False
    assert res["rt_cd"] == "0"
    assert captured["quantity"] == 2
    assert captured["price"] == 71000


def test_buyable_quantity_adapter():
    import api
    config.session.is_toss = True
    try:
        with patch("toss_api.get_buying_power",
                   return_value={"currency": "KRW", "cashBuyingPower": "1000000"}):
            qty = api.fetch_buyable_quantity("005930", 70000)
    finally:
        config.session.is_toss = False
    assert qty == 14  # 1,000,000 / 70,000


def test_sellable_quantity_adapter():
    import api
    config.session.is_toss = True
    try:
        with patch("toss_api.get_sellable_quantity", return_value={"sellableQuantity": "8"}):
            qty = api.fetch_sellable_quantity("005930")
    finally:
        config.session.is_toss = False
    assert qty == 8


# =========================================================================
# 메뉴 1(시장 지수): 토스 모드는 KIS 미사용, yfinance 폴백
# =========================================================================
@pytest.mark.real_index_chart  # get_domestic_index_chart 자체 로직 검증 → 지수 조회 자동 mock 비활성화
def test_index_chart_toss_uses_market_indicator_for_kospi():
    """토스 모드: 코스피 지수 차트는 토스 시장지표 API(1.2.4)로 조회한다(KIS·yfinance 미사용)."""
    import api
    res = {"candles": [
        {"timestamp": "2026-03-25T09:00:00+09:00", "openPrice": "2798.32", "highPrice": "2820.15",
         "lowPrice": "2790.10", "closePrice": "2812.45", "volume": "542000000"},
        {"timestamp": "2026-03-24T09:00:00+09:00", "openPrice": "2785.60", "highPrice": "2801.22",
         "lowPrice": "2779.85", "closePrice": "2798.10", "volume": "498000000"},
    ], "nextBefore": None}

    api.clear_chart_cache()
    config.session.is_toss = True
    try:
        with patch("toss_api.get_market_indicator_candles", return_value=res) as mock_ind, \
             patch("api.get_chart_data") as mock_yf, \
             patch("api.call_api") as mock_call:
            df = api.get_domestic_index_chart("0001")  # KOSPI
    finally:
        config.session.is_toss = False
        api.clear_chart_cache()

    assert mock_ind.call_args.args[0] == "KOSPI"
    assert mock_ind.call_args.kwargs["interval"] == "1d"
    assert list(df.columns) == ['date', 'open', 'high', 'low', 'close', 'volume']
    assert df.iloc[-1]['date'] == '20260325'          # 오름차순 정렬
    assert df.iloc[-1]['close'] == 2812.45
    mock_yf.assert_not_called()   # yfinance 미사용
    mock_call.assert_not_called() # KIS 미호출


@pytest.mark.real_index_chart
def test_index_chart_toss_kospi200_still_uses_yfinance():
    """토스 모드: 코스피200은 토스 심볼 카탈로그에 없어 종전 yfinance 경로를 유지한다."""
    import api
    import pandas as pd
    captured = {}

    def fake_chart(code, is_overseas=False, period_type='daily'):
        captured["ticker"] = code
        captured["is_overseas"] = is_overseas
        return pd.DataFrame({"date": ["20260101"], "close": [2500.0]})

    config.session.is_toss = True
    try:
        with patch("api.get_chart_data", side_effect=fake_chart), \
             patch("toss_api.get_market_indicator_candles") as mock_ind, \
             patch("api.call_api") as mock_call:
            df = api.get_domestic_index_chart("2001")  # KOSPI200
    finally:
        config.session.is_toss = False
    assert captured["ticker"] == "^KS200"
    assert captured["is_overseas"] is True
    assert not df.empty
    mock_ind.assert_not_called()   # 토스 시장지표 미지원 심볼
    mock_call.assert_not_called()  # KIS 미호출


def test_toss_index_chart_paginates_with_next_before():
    """토스 시장지표 일봉도 nextBefore 커서로 250봉 이상 확보한다(EMA120·52주 정확도)."""
    import api
    from datetime import datetime as _dt, timedelta as _td
    _base = _dt(2026, 6, 1)

    def make_page(start_idx):
        return [{
            "timestamp": (_base - _td(days=i)).strftime("%Y-%m-%dT09:00:00+09:00"),
            "openPrice": "2700", "highPrice": "2750", "lowPrice": "2650",
            "closePrice": str(2700 + i), "volume": "1000",
        } for i in range(start_idx, start_idx + 200)]

    pages = [{"candles": make_page(0), "nextBefore": "P2"},
             {"candles": make_page(200), "nextBefore": "P3"}]
    calls = {"n": 0}

    def fake_candles(symbol, interval="1d", count=200, before=None):
        idx = calls["n"]
        calls["n"] += 1
        return pages[idx] if idx < len(pages) else {"candles": [], "nextBefore": None}

    with patch("toss_api.get_market_indicator_candles", side_effect=fake_candles):
        df = api._toss_index_chart_data("KOSPI")

    assert calls["n"] == 2      # 2페이지(>=260) 확보 후 중단
    assert len(df) == 250       # tail(250)
    assert df['date'].is_monotonic_increasing


@pytest.mark.real_index_chart
def test_index_chart_toss_overlays_today_with_market_indicator_price(monkeypatch):
    """토스 지수도 KIS와 동일하게 당일 봉을 실시간 지수(시장지표 현재가)로 갱신한다."""
    import api
    res = {"candles": [
        {"timestamp": "2026-03-24T09:00:00+09:00", "openPrice": "2780", "highPrice": "2800",
         "lowPrice": "2770", "closePrice": "2798.10", "volume": "498000000"},
    ], "nextBefore": None}

    monkeypatch.setattr(api, "market_today", lambda is_overseas=False: "20260325")
    monkeypatch.setattr(api, "_before_krx_regular_open", lambda: False)  # 장중
    # 장중 시나리오이므로 지표 오버레이 게이트(KRX 정규장 전용)를 통과시킨다.
    #  [2026-07-28] 게이트가 domestic_trading_session_open(08:00~20:00) → _nxt_quote_phase=='skip'
    #  (KRX 정규장)으로 바뀌어, 종전 패치로는 실행 시각에 따라 오버레이가 차단됐다.
    monkeypatch.setattr(api, "_nxt_quote_phase", lambda: 'skip')
    api.clear_chart_cache()
    api._MICRO_CACHE.clear()
    config.session.is_toss = True
    try:
        with patch("toss_api.get_market_indicator_candles", return_value=res), \
             patch("toss_api.get_market_indicator_price",
                   return_value={"symbol": "KOSPI", "lastPrice": "2812.45"}):
            api.get_domestic_index_chart("0001")          # 캐시 적재
            df = api.get_domestic_index_chart("0001")     # 캐시 적중 + 오버레이
    finally:
        config.session.is_toss = False
        api.clear_chart_cache()
        api._MICRO_CACHE.clear()

    assert list(df['date']) == ['20260324', '20260325']   # 당일 봉 추가
    assert float(df.iloc[-1]['close']) == 2812.45         # 실시간 지수로 갱신


def test_toss_index_chart_empty_on_api_error():
    """토스 시장지표 오류 시 빈 DF(상위 폴백 체인으로 넘어감)."""
    import api
    with patch("toss_api.get_market_indicator_candles",
               side_effect=toss_api.TossApiError("internal-error", "mock", status=500)):
        df = api._toss_index_chart_data("KOSDAQ")
    assert df.empty


def test_index_price_toss_uses_market_indicator():
    """토스 모드: 국내 지수 현재가는 토스 시장지표 API로 조회해 KIS 형태로 반환한다.

    전일 종가는 미제공 → '0'으로 둬 상위(_get_cached_chart)의 수정주가 검증을 건너뛰게 한다.
    """
    import api
    api._MICRO_CACHE.clear()
    config.session.is_toss = True
    try:
        with patch("toss_api.get_market_indicator_price",
                   return_value={"symbol": "KOSDAQ", "lastPrice": "845.32"}) as mock_ind, \
             patch("api.get_yf_fast_info") as mock_yf, \
             patch("api.call_api") as mock_call:
            res = api.get_domestic_index_price("1001")  # KOSDAQ
    finally:
        config.session.is_toss = False
        api._MICRO_CACHE.clear()
    assert mock_ind.call_args.args[0] == "KOSDAQ"
    assert res["rt_cd"] == "0"
    assert res["output"]["bstp_nmix_prpr"] == "845.32"
    assert res["output"]["bstp_nmix_prdy_clpr"] == "0"
    mock_yf.assert_not_called()
    mock_call.assert_not_called()


def test_index_price_toss_falls_back_to_fast_info():
    """토스 시장지표 실패 시 종전 yfinance fast_info 경로로 폴백한다."""
    import api
    api._MICRO_CACHE.clear()
    config.session.is_toss = True
    try:
        with patch("toss_api.get_market_indicator_price",
                   side_effect=toss_api.TossApiError("internal-error", "mock", status=500)), \
             patch("api.get_yf_fast_info",
                   return_value={"last_price": 850.5, "regular_market_previous_close": 845.0}), \
             patch("api.call_api") as mock_call:
            res = api.get_domestic_index_price("1001")
    finally:
        config.session.is_toss = False
        api._MICRO_CACHE.clear()
    assert res["rt_cd"] == "0"
    assert res["output"]["bstp_nmix_prpr"] == "850.5"
    assert res["output"]["bstp_nmix_prdy_clpr"] == "845.0"
    mock_call.assert_not_called()


def _reset_nxt_cache():
    import api
    api._toss_nxt_map = {}
    api._toss_nxt_miss = {}
    api._toss_nxt_day = None


def _reset_cal_cache():
    import api
    api._toss_kr_cal_map = {}
    api._toss_kr_cal_day = None
    api._toss_kr_cal_fail = 0.0


def test_krx_only_uses_nxt_supported_field():
    """NXT 미거래(nxtSupported=false) 종목은 토스 체결이 KRX 단독 → 신뢰 대상."""
    import api
    _reset_nxt_cache()
    config.session.is_toss = True
    try:
        rows = [{"symbol": "069500", "koreanMarketDetail": {"nxtSupported": False}}]
        with patch("toss_api.get_stocks", return_value=rows) as mock_st:
            assert api._toss_krx_only("069500") is True
            assert api._toss_krx_only("069500") is True   # 캐시 적중
        mock_st.assert_called_once()                      # 종목당 1회만 조회

        rows2 = [{"symbol": "005930", "koreanMarketDetail": {"nxtSupported": True}}]
        with patch("toss_api.get_stocks", return_value=rows2):
            assert api._toss_krx_only("005930") is False  # NXT 병행 체결 → 신뢰 불가
    finally:
        config.session.is_toss = False
        _reset_nxt_cache()


def test_krx_only_falls_back_to_etf_heuristic(monkeypatch):
    """stocks 조회 실패 시 종전 ETF/ETN 휴리스틱으로 폴백하고, 쿨다운 동안 재조회하지 않는다."""
    import api
    _reset_nxt_cache()
    monkeypatch.setattr(api, "is_domestic_etf_etn", lambda code, name="": True)
    config.session.is_toss = True
    try:
        with patch("toss_api.get_stocks",
                   side_effect=toss_api.TossApiError("internal-error", "mock", status=500)) as mock_st:
            assert api._toss_krx_only("069500") is True   # 휴리스틱 폴백
            assert api._toss_krx_only("069500") is True
        mock_st.assert_called_once()                      # 실패 쿨다운으로 재조회 억제
    finally:
        config.session.is_toss = False
        _reset_nxt_cache()


def test_krx_close_trusted_by_nxt_support():
    """분봉 캡처값('cap') 신뢰 판정이 nxtSupported를 따른다."""
    import api
    _reset_nxt_cache()
    config.session.is_toss = True
    try:
        with patch("toss_api.get_stocks",
                   return_value=[{"symbol": "069500", "koreanMarketDetail": {"nxtSupported": False}}]):
            assert api._toss_krx_close_trusted("069500", "cap") is True
        _reset_nxt_cache()
        with patch("toss_api.get_stocks",
                   return_value=[{"symbol": "005930", "koreanMarketDetail": {"nxtSupported": True}}]):
            assert api._toss_krx_close_trusted("005930", "cap") is False
            assert api._toss_krx_close_trusted("005930", "yf") is True   # 일봉 검증값은 항상 신뢰
    finally:
        config.session.is_toss = False
        _reset_nxt_cache()


def _cal_payload(start="09:00:00", end="15:30:00", date="2026-03-25"):
    return {
        "today": {"date": date, "integrated": {
            "regularMarket": {"startTime": f"{date}T{start}+09:00",
                              "endTime": f"{date}T{end}+09:00"}}},
        "previousBusinessDay": {"date": "2026-03-24", "integrated": {
            "regularMarket": {"startTime": "2026-03-24T09:00:00+09:00",
                              "endTime": "2026-03-24T15:30:00+09:00"}}},
        "nextBusinessDay": {"date": "2026-03-26", "integrated": None},
    }


def test_krx_regular_bounds_from_calendar():
    """정규장 경계를 market-calendar에서 읽고, 캘린더 호출은 하루 1회로 캐시한다."""
    import api
    _reset_cal_cache()
    config.session.is_toss = True
    try:
        with patch("toss_api.get_market_calendar",
                   return_value=_cal_payload(start="10:00:00", end="16:00:00")) as mock_cal:
            assert api._toss_krx_regular_bounds("20260325") == ((10, 0), (16, 0))  # 지연·연장 개장
            assert api._toss_krx_regular_bounds("20260324") == ((9, 0), (15, 30))  # 전 영업일도 함께 캐시
        mock_cal.assert_called_once()
        # 휴장(integrated=null)·미수록 날짜는 기본값
        assert api._toss_krx_regular_bounds("20260326") == ((9, 0), (15, 30))
    finally:
        config.session.is_toss = False
        _reset_cal_cache()


def test_krx_regular_bounds_default_on_failure():
    """캘린더 조회 실패 시 기본값(09:00~15:30)을 쓰고 쿨다운 동안 재조회하지 않는다."""
    import api
    _reset_cal_cache()
    config.session.is_toss = True
    try:
        with patch("toss_api.get_market_calendar",
                   side_effect=toss_api.TossApiError("internal-error", "mock", status=500)) as mock_cal:
            assert api._toss_krx_regular_bounds() == ((9, 0), (15, 30))
            assert api._toss_krx_regular_bounds() == ((9, 0), (15, 30))
        mock_cal.assert_called_once()
    finally:
        config.session.is_toss = False
        _reset_cal_cache()


def test_intraday_filter_follows_calendar_bounds():
    """분봉 정규장 필터가 캘린더 경계를 따른다(단축장이면 그 시각까지만 남는다)."""
    import api
    _reset_cal_cache()
    candles = [{
        "timestamp": f"2026-03-25T{h:02d}:{m:02d}:00+09:00",
        "openPrice": "100", "highPrice": "100", "lowPrice": "100",
        "closePrice": "100", "volume": "10",
    } for h, m in [(8, 30), (9, 0), (10, 0), (13, 0), (14, 0), (15, 30), (16, 0)]]

    config.session.is_toss = True
    try:
        # 단축장(09:00~14:00) → 14:00 초과 봉 제외, NXT 프리(08:30)·애프터(15:30/16:00) 제거
        with patch("toss_api.get_market_calendar",
                   return_value=_cal_payload(end="14:00:00")), \
             patch("toss_api.get_candles", return_value={"candles": candles, "nextBefore": None}):
            df = api._toss_chart_data("005930", period_type='intraday', is_overseas=False)
        assert [t.strftime('%H:%M') for t in df['date']] == ['09:00', '10:00', '13:00', '14:00']
    finally:
        config.session.is_toss = False
        _reset_cal_cache()


def test_market_indicator_endpoints_paths_and_groups():
    """시장지표 엔드포인트 경로·레이트리밋 그룹이 스펙(1.2.4)과 일치하는가."""
    seen = []

    def fake_request(method, path, group, params=None, json_body=None, account=True, retries=2):
        seen.append((path, group, params, account))
        return [] if path.endswith("/prices") else {}

    with patch("toss_api._request", side_effect=fake_request):
        toss_api.get_market_indicator_prices(["KOSPI", "KOSDAQ"])
        toss_api.get_market_indicator_candles("KOSPI", interval="1d", count=200)
        toss_api.get_market_indicator_investor_trading("KOSPI", interval="1d", count=5)

    assert seen[0][0] == "/api/v1/market-indicators/prices"
    assert seen[0][1] == "MARKET_INDICATOR"
    assert seen[0][2]["symbols"] == "KOSPI,KOSDAQ"
    assert seen[1][0] == "/api/v1/market-indicators/KOSPI/candles"
    assert seen[1][1] == "MARKET_INDICATOR_CHART"
    assert seen[2][0] == "/api/v1/market-indicators/KOSPI/investor-trading"
    assert all(s[3] is False for s in seen)   # 계좌 헤더 불필요(토큰만으로 호출)


def test_index_price_toss_unknown_code():
    import api
    config.session.is_toss = True
    try:
        res = api.get_domestic_index_price("9999")
    finally:
        config.session.is_toss = False
    assert res["rt_cd"] == "9999"


def _buy_df():
    import pandas as pd
    rows = []
    base = 10000
    for i in range(30):
        p = base + i * 100
        rows.append({'date': f'2026040{i % 9 + 1}', 'open': p, 'high': p + 50,
                     'low': p - 50, 'close': p, 'volume': 1000})
    return pd.DataFrame(rows)


def _strategy_buy_patches(state="매수"):
    return [
        patch("modules.auto_trade.indicators.calculate_indicators", return_value={
            'rsi': 55.0, 'adx': 25.0, 'cci': 50.0, 'atr': 100.0, 'psar': 9000.0,
            'macd': 1.0, 'macd_signal': 0.5, 'obv_trend': True}),
        patch("modules.auto_trade.analysis.classify_stock_state", return_value=(state, None, "사유")),
        patch("modules.auto_trade.analysis.calculate_score", return_value=(8.5, None)),
        patch("modules.auto_trade.analysis.check_smart_money_turnaround", return_value=(False, "")),
    ]


def test_toss_buy_gate_uses_ask_bid_ratio():
    """토스: 체결강도 None → 매도잔량비(ask_bid_ratio) 게이트로 매수 판정."""
    import contextlib
    from modules.auto_trade import DefaultStrategy
    strat = DefaultStrategy()
    df = _buy_df()
    th = {"BUY_VOL_STRENGTH": 100.0, "BUY_ASK_BID_RATIO": 1.0, "AUTO_ADJUST_ASK_BID_RATIO": True}
    config.session.is_toss = True
    try:
        with contextlib.ExitStack() as es:
            for p in _strategy_buy_patches("매수"):
                es.enter_context(p)
            r_ok = strat.analyze_buy("005930", "삼성", df, 13000, vol_strength=None, thresholds=th, ask_bid_ratio=1.5)
            r_no = strat.analyze_buy("005930", "삼성", df, 13000, vol_strength=None, thresholds=th, ask_bid_ratio=0.5)
            r_none = strat.analyze_buy("005930", "삼성", df, 13000, vol_strength=None, thresholds=th, ask_bid_ratio=None)
    finally:
        config.session.is_toss = False
    assert r_ok['action'] == 'buy'        # 매도잔량비 1.5 >= 1.0 → 통과
    assert r_no['action'] == 'wait'        # 0.5 < 1.0 → 거부
    assert '매도비' in r_no['vol_reject_reason']
    assert '체결강도대체' not in r_no['vol_reject_reason']
    assert r_none['action'] == 'buy'       # 호가 없음 → 상태 게이트만으로 진입(거래중단 방지)


def test_non_toss_buy_gate_unchanged():
    """비토스: 기존 체결강도 게이트 동작 유지(회귀 방지)."""
    import contextlib
    from modules.auto_trade import DefaultStrategy
    strat = DefaultStrategy()
    df = _buy_df()
    th = {"BUY_VOL_STRENGTH": 100.0, "BUY_ASK_BID_RATIO": 1.0, "AUTO_ADJUST_ASK_BID_RATIO": True}
    config.session.is_toss = False
    with contextlib.ExitStack() as es:
        for p in _strategy_buy_patches("매수"):
            es.enter_context(p)
        r_low = strat.analyze_buy("005930", "삼성", df, 13000, vol_strength=50.0, thresholds=th, ask_bid_ratio=2.0)
        r_ok = strat.analyze_buy("005930", "삼성", df, 13000, vol_strength=150.0, thresholds=th, ask_bid_ratio=2.0)
    assert r_low['action'] == 'wait'       # 체결강도 50 < 100 → 거부
    assert '체결:' in r_low['vol_reject_reason']
    assert r_ok['action'] == 'buy'


def test_toss_mode_syncs_auto_account():
    """토스 모드: 시스템 트레이딩 계좌(auto_cano)가 거래 계좌(cano)와 동기화된다."""
    import os
    from session import SessionManager
    sm = SessionManager()
    with patch.dict(os.environ, {"TOSS_ACC_NUM": "18901501685",
                                 "TOSS_APP_KEY": "k", "TOSS_APP_SECRET": "s"}):
        sm.initialize(mode='3')
    try:
        assert sm.is_toss is True
        assert sm.cano == "18901501685"
        # auto_cano == cano 여야 auto_cano 기반 자동매매 분기가 토스 계좌를 가리킨다
        assert sm.auto_cano == sm.cano
        assert sm.auto_acnt_prdt_cd == sm.acnt_prdt_cd
    finally:
        sm.is_toss = False


def _closed_orders_today():
    """오늘 날짜의 CLOSED 주문(국내 체결 1 + 해외 체결 1 + 국내 취소 1)."""
    import api
    from datetime import datetime
    today = datetime.now().strftime("%Y-%m-%d")
    return {
        "orders": [
            {"orderId": "F_KR", "symbol": "005930", "side": "BUY", "status": "FILLED",
             "price": "70000", "quantity": "10", "currency": "KRW",
             "orderedAt": f"{today}T09:30:00+09:00",
             "execution": {"filledQuantity": "10", "averageFilledPrice": "69950",
                           "filledAt": f"{today}T09:31:05+09:00"}},
            {"orderId": "F_US", "symbol": "AAPL", "side": "SELL", "status": "FILLED",
             "price": "185.5", "quantity": "5", "currency": "USD",
             "orderedAt": f"{today}T22:30:00+09:00",
             "execution": {"filledQuantity": "5", "averageFilledPrice": "185.6",
                           "filledAt": f"{today}T22:31:00+09:00"}},
            {"orderId": "C_KR", "symbol": "000660", "side": "BUY", "status": "CANCELED",
             "price": "150000", "quantity": "4", "currency": "KRW",
             "orderedAt": f"{today}T10:00:00+09:00", "canceledAt": f"{today}T10:05:00+09:00",
             "execution": {"filledQuantity": "1", "averageFilledPrice": "150000",
                           "filledAt": f"{today}T10:01:00+09:00"}},
        ],
        "hasNext": False, "nextCursor": None,
    }


def test_today_history_domestic_adapter():
    """토스 CLOSED → KIS get_today_history(output1) 변환(국내만, 체결/취소 필드)."""
    import api
    config.session.is_toss = True
    try:
        api._MICRO_CACHE.clear()
        with patch("toss_api.get_orders", return_value=_closed_orders_today()), \
             patch("toss_api.get_stocks", return_value=[
                 {"symbol": "005930", "name": "삼성전자"},
                 {"symbol": "000660", "name": "SK하이닉스"}]):
            res = api.get_today_history()
    finally:
        config.session.is_toss = False
    out1 = res["output1"]
    assert res["rt_cd"] == "0"
    assert len(out1) == 2  # KRW 2건(해외 제외)
    filled = next(o for o in out1 if o["odno"] == "F_KR")
    assert filled["tot_ccld_qty"] == "10"
    assert filled["ord_qty"] == "10"
    assert filled["avg_prvs"] == "69950.0"
    assert filled["sll_buy_dvsn_cd"] == "02"
    assert filled["cncl_cfrm_qty"] == "0"
    assert "ft_ord_qty" not in filled  # 국내 항목엔 해외 필드 없어야 함(판별 오류 방지)
    canceled = next(o for o in out1 if o["odno"] == "C_KR")
    assert canceled["tot_ccld_qty"] == "1"        # 부분 체결
    assert canceled["cncl_cfrm_qty"] == "3"        # 4 - 1 취소


def test_today_history_overseas_adapter():
    import api
    config.session.is_toss = True
    try:
        api._MICRO_CACHE.clear()
        with patch("toss_api.get_orders", return_value=_closed_orders_today()), \
             patch("toss_api.get_stocks", return_value=[{"symbol": "AAPL", "name": "Apple"}]):
            res = api.get_overseas_today_history()
    finally:
        config.session.is_toss = False
    out = res["output"]
    assert len(out) == 1  # USD 1건
    o = out[0]
    assert o["odno"] == "F_US"
    assert o["ft_ccld_qty"] == "5"
    assert float(o["ft_ccld_unpr3"]) == 185.6
    assert o["sll_buy_dvsn_cd"] == "01"  # SELL


def test_today_history_filters_other_days():
    """오늘이 아닌 체결은 제외된다."""
    import api
    config.session.is_toss = True
    env = {"orders": [
        {"orderId": "OLD", "symbol": "005930", "side": "BUY", "status": "FILLED",
         "price": "70000", "quantity": "10", "currency": "KRW",
         "orderedAt": "2020-01-02T09:30:00+09:00",
         "execution": {"filledQuantity": "10", "filledAt": "2020-01-02T09:31:00+09:00"}},
    ], "hasNext": False}
    try:
        api._MICRO_CACHE.clear()
        with patch("toss_api.get_orders", return_value=env), \
             patch("toss_api.get_stocks", return_value=[]):
            res = api.get_today_history()
    finally:
        config.session.is_toss = False
    assert res["output1"] == []


def test_kosdaq150_no_data_in_toss():
    """토스 모드: 코스닥150은 tvDatafeed 실패 시 yfinance(^KQ150) 폴백까지 시도하나
    ^KQ150은 실측 데이터가 없어 빈 응답 → 최종 '-'(None/empty). KIS는 미사용."""
    import api
    import modules.analysis as analysis
    import pandas as pd
    config.session.is_toss = True
    try:
        # tvDatafeed가 데이터를 못 주는 상황을 결정적으로 재현(라이브 웹소켓 flaky 제거).
        # 폴백 체인상 yfinance(^KQ150)까지 내려가지만 빈 응답이라 데이터 없음.
        with patch("modules.analysis._fetch_index_via_tvdatafeed", return_value=None), \
             patch("api.get_domestic_index_chart") as mock_kis, \
             patch("api.get_chart_data", return_value=pd.DataFrame()) as mock_yf:
            df = analysis.get_domestic_index_data("KOSDAQ150", force_refresh=True)
    finally:
        config.session.is_toss = False
    assert df is None or df.empty
    mock_kis.assert_not_called()             # 토스는 KIS 미호출
    assert mock_yf.call_args.args[0] == "^KQ150"  # 최후 폴백으로 yfinance(^KQ150) 시도됨


def test_kospi_kosdaq_use_toss_market_indicator_first():
    """토스 모드: 코스피/코스닥은 토스 시장지표가 1순위 → 성공 시 tvDatafeed/yfinance 미호출."""
    import modules.analysis as analysis
    import pandas as pd
    config.session.is_toss = True
    fake = pd.DataFrame({
        'date': [f'2026{(i // 28) + 1:02d}{(i % 28) + 1:02d}' for i in range(130)],
        'open': [2700.0] * 130, 'high': [2700.0] * 130, 'low': [2700.0] * 130,
        'close': [2700.0 + i for i in range(130)], 'volume': [1000] * 130,
    })
    try:
        with patch("api.get_domestic_index_chart", return_value=fake) as mock_idx, \
             patch("modules.analysis._fetch_index_via_tvdatafeed") as mock_tv, \
             patch("api.get_chart_data") as mock_yf:
            for mtype in ("KOSPI", "KOSDAQ"):
                df = analysis.get_domestic_index_data(mtype, force_refresh=True)
                assert df is not None and not df.empty
                assert df.attrs.get('source') == 'TOSS'
        assert {c.args[0] for c in mock_idx.call_args_list} == {"0001", "1001"}
        mock_tv.assert_not_called()   # 토스 성공 → tvDatafeed 미호출
        mock_yf.assert_not_called()   # yfinance 미호출
    finally:
        config.session.is_toss = False


def test_kospi_falls_back_to_tvdatafeed_then_yfinance_in_toss():
    """토스 모드 폴백 체인: 시장지표 실패 → tvDatafeed → yfinance(^KS11)."""
    import modules.analysis as analysis
    import pandas as pd
    config.session.is_toss = True
    tv_df = pd.DataFrame({
        'date': pd.date_range('2026-01-01', periods=130),
        'open': [7000.0] * 130, 'high': [7000.0] * 130, 'low': [7000.0] * 130,
        'close': [7000.0 + i for i in range(130)], 'volume': [0] * 130,
    })
    tv_df.attrs['source'] = 'TVDATAFEED'
    yf_df = pd.DataFrame({
        'date': pd.date_range('2026-01-01', periods=130),
        'open': [3000.0] * 130, 'high': [3000.0] * 130, 'low': [3000.0] * 130,
        'close': [3000.0] * 130, 'volume': [100] * 130,
    })
    try:
        # (a) 시장지표 실패 → tvDatafeed 성공: yfinance 미호출
        with patch("api.get_domestic_index_chart", return_value=pd.DataFrame()) as mock_idx, \
             patch("modules.analysis._fetch_index_via_tvdatafeed", return_value=tv_df) as mock_tv, \
             patch("api.get_chart_data", return_value=yf_df) as mock_yf:
            df = analysis.get_domestic_index_data("KOSPI", force_refresh=True)
            assert df is not None and df.attrs.get('source') == 'TVDATAFEED'
            mock_idx.assert_called()
            mock_tv.assert_called_once()
            mock_yf.assert_not_called()

        # (b) 시장지표·tvDatafeed 모두 실패 → yfinance(^KS11) 폴백
        with patch("api.get_domestic_index_chart", return_value=pd.DataFrame()), \
             patch("modules.analysis._fetch_index_via_tvdatafeed", return_value=None) as mock_tv2, \
             patch("api.get_chart_data", return_value=yf_df) as mock_yf2:
            df2 = analysis.get_domestic_index_data("KOSPI", force_refresh=True)
            assert df2 is not None and df2.attrs.get('source') == 'YFINANCE'
            mock_tv2.assert_called_once()
            assert mock_yf2.call_args.args[0] == "^KS11"
    finally:
        config.session.is_toss = False


def test_merge_index_volume_from_yfinance_fills_volume():
    """tvDatafeed 지수(volume=0)에 yfinance 거래량을 날짜 매칭으로 채운다(가격은 유지)."""
    import modules.analysis as analysis
    import pandas as pd
    tv = pd.DataFrame({
        'date': pd.to_datetime(['2026-07-13', '2026-07-14', '2026-07-15']),
        'open': [7000.0] * 3, 'high': [7000.0] * 3, 'low': [7000.0] * 3,
        'close': [7000.0, 7100.0, 7200.0], 'volume': [0, 0, 0],
    })
    yf = pd.DataFrame({
        'date': ['20260713', '20260714', '20260715'],
        'open': [0.0] * 3, 'high': [0.0] * 3, 'low': [0.0] * 3,
        'close': [0.0] * 3, 'volume': [100, 200, 300],
    })
    with patch("api.get_chart_data", return_value=yf) as mock_yf:
        out = analysis._merge_index_volume_from_yfinance(tv, "^KS11")
    assert list(out['volume']) == [100.0, 200.0, 300.0]   # 거래량 채워짐
    assert list(out['close']) == [7000.0, 7100.0, 7200.0]  # 가격은 tvDatafeed 유지
    assert mock_yf.call_args.args[0] == "^KS11"


def test_merge_index_volume_noop_when_yfinance_empty():
    """yfinance가 빈 응답(^KQ150 등)이면 거래량 0을 그대로 둔다."""
    import modules.analysis as analysis
    import pandas as pd
    tv = pd.DataFrame({
        'date': pd.to_datetime(['2026-07-14', '2026-07-15']),
        'open': [1.0] * 2, 'high': [1.0] * 2, 'low': [1.0] * 2,
        'close': [1.0, 2.0], 'volume': [0, 0],
    })
    with patch("api.get_chart_data", return_value=pd.DataFrame()):
        out = analysis._merge_index_volume_from_yfinance(tv, "^KQ150")
    assert list(out['volume']) == [0, 0]


def test_kis_mode_index_fallback_chain_kis_then_tvdatafeed_then_yfinance():
    """모드 1/2(KIS): KIS 실패 시 1차 tvDatafeed, 그 실패 시 2차 yfinance 순으로 폴백."""
    import modules.analysis as analysis
    import pandas as pd
    config.session.is_toss = False  # KIS 모드

    tv_df = pd.DataFrame({
        'date': pd.date_range('2026-01-01', periods=130),
        'open': [7000.0] * 130, 'high': [7000.0] * 130, 'low': [7000.0] * 130,
        'close': [7000.0 + i for i in range(130)], 'volume': [0] * 130,
    })
    tv_df.attrs['source'] = 'TVDATAFEED'
    yf_df = pd.DataFrame({
        'date': pd.date_range('2026-01-01', periods=130),
        'open': [3000.0] * 130, 'high': [3000.0] * 130, 'low': [3000.0] * 130,
        'close': [3000.0] * 130, 'volume': [100] * 130,
    })

    # KIS는 항상 빈 응답(실패)으로 두고, tvDatafeed 성공/실패에 따른 분기를 검증
    with patch("api.get_domestic_index_chart", return_value=pd.DataFrame()) as mock_kis:
        # (a) KIS 실패 → tvDatafeed 성공: yfinance는 호출되지 않는다
        with patch("modules.analysis._fetch_index_via_tvdatafeed", return_value=tv_df) as mock_tv, \
             patch("api.get_chart_data", return_value=yf_df) as mock_yf:
            df = analysis.get_domestic_index_data("KOSPI", force_refresh=True)
            assert df is not None and df.attrs.get('source') == 'TVDATAFEED'
            mock_kis.assert_called()      # KIS 1순위 시도
            mock_tv.assert_called_once()  # 1차 폴백 tvDatafeed
            mock_yf.assert_not_called()   # tvDatafeed 성공 → yfinance 미호출

        # (b) KIS 실패 → tvDatafeed 실패 → yfinance 폴백
        with patch("modules.analysis._fetch_index_via_tvdatafeed", return_value=None) as mock_tv2, \
             patch("api.get_chart_data", return_value=yf_df) as mock_yf2:
            df2 = analysis.get_domestic_index_data("KOSPI", force_refresh=True)
            assert df2 is not None and df2.attrs.get('source') == 'YFINANCE'
            mock_tv2.assert_called_once()
            assert mock_yf2.call_args.args[0] == "^KS11"


def test_format_order_no_toss_last_10():
    """토스 모드: 긴 주문번호는 뒤 10자리만. 비토스(KIS): 그대로."""
    import utils
    long_odno = "mJerK-dVKoU-sVb1D84BERbn9k-APR21uQi9Jj3JFBl70_mMjmGvqAANmrmhQdxQ9XgMjhiEsudGuoGZZUGf4g"

    config.session.is_toss = True
    try:
        assert utils.format_order_no(long_odno) == long_odno[-10:]
        assert len(utils.format_order_no(long_odno)) == 10
        assert utils.format_order_no(None) == ""
    finally:
        config.session.is_toss = False

    # 비토스(KIS)는 절단하지 않고 그대로
    assert utils.format_order_no("0001234567") == "0001234567"
    assert utils.format_order_no(long_odno) == long_odno


# ---------------------------------------------------------------------------
# 마감 후 ETF 현재가 = KRX 정규장 종가 고정 (mode 2/HTS 정합)
# ---------------------------------------------------------------------------
#  ETF는 NXT 연장거래 대상이 아니라 15:30 이후 체결은 전부 KRX 시간외단일가(16:00~18:00)다.
#  KIS 경로(mode 1/2)는 시간외단일가를 별도 TR로만 제공해 반영하지 않으므로 정규장 종가를
#  보여주는데, 토스 lastPrice는 시간외 체결을 그대로 반영해 두 모드가 어긋났다.
#  (실측 2026-07-22 16:07 KODEX 코스닥150: KIS·HTS 12,525 / 토스 12,530)
_KRX_CLOSE, _AFTER_HOURS, _BASE = 12525.0, 12530.0, 12650.0


def _toss_price_output(after_close, code, krx_close=_KRX_CLOSE, last=_AFTER_HOURS):
    import api
    from unittest.mock import patch
    with patch.object(api.toss_api, "get_price", return_value={"lastPrice": last}), \
         patch.object(api, "_toss_capture_krx_close"), \
         patch.object(api, "_toss_after_krx_close", return_value=after_close), \
         patch.object(api, "_toss_krx_close_get", return_value=krx_close), \
         patch.object(api, "_toss_base_price", return_value=_BASE), \
         patch.object(api, "_toss_before_nxt_open", return_value=False):
        return api._toss_current_price_data(code, is_overseas=False)["output"]


def _setup_etf_watchlist():
    import api
    config.session.stock_data = {
        "stocks_kr": [{"code": "005930", "name": "삼성전자"}],
        "etfs_kr": [{"code": "229200", "name": "KODEX 코스닥150"}],
        "stocks_us": [], "etfs_us": [],
    }
    api._ETF_ETN_CACHE.clear()


def test_toss_etf_after_close_uses_krx_regular_close():
    """마감 후 ETF는 시간외 체결가가 아니라 KRX 정규장 종가를 보여준다."""
    config.session.is_toss = True
    _setup_etf_watchlist()
    try:
        o = _toss_price_output(after_close=True, code="229200")
        assert int(o["stck_prpr"]) == 12525          # 시간외 12,530이 아님
        assert o["prdy_vrss"] == "-125"              # 기준가 12,650 대비
        assert float(o["prdy_ctrt"]) == -0.99
    finally:
        config.session.is_toss = False


def test_toss_etf_during_session_keeps_live_price():
    """장중에는 그대로 실시간 체결가를 쓴다(고정은 마감 후에만)."""
    config.session.is_toss = True
    _setup_etf_watchlist()
    try:
        o = _toss_price_output(after_close=False, code="229200")
        assert int(o["stck_prpr"]) == 12530
    finally:
        config.session.is_toss = False


def test_toss_stock_after_close_keeps_nxt_price():
    """일반 주식은 마감 후에도 NXT 연장가를 유지한다.

    mode 1/2도 장후(15:30~20:00)에 NXT 연장가를 노출하므로(get_multi_current_prices_nxt),
    여기서 정규장 종가로 고정하면 오히려 모드 간 값이 어긋난다.
    """
    config.session.is_toss = True
    _setup_etf_watchlist()
    try:
        o = _toss_price_output(after_close=True, code="005930")
        assert int(o["stck_prpr"]) == 12530          # NXT 연장가 유지
    finally:
        config.session.is_toss = False


def test_toss_etf_after_close_without_captured_close_falls_back():
    """KRX 마감가 캡처에 실패했으면 기존 동작(실시간가)으로 폴백한다."""
    config.session.is_toss = True
    _setup_etf_watchlist()
    try:
        o = _toss_price_output(after_close=True, code="229200", krx_close=None)
        assert int(o["stck_prpr"]) == 12530
    finally:
        config.session.is_toss = False


# ---------------------------------------------------------------------------
# 기준가 3순위: yfinance 일봉(KRX 정규장 종가) — 캡처 공백 소급 보정
# ---------------------------------------------------------------------------
#  캡처(2순위)는 '그날 15:35 이후 프로그램이 mode 3로 떠 있어야' 저장되므로, 하루라도 안 띄우면
#  그날 KRX 종가가 영구히 비어 기준가가 NXT 종가로 폴백된다.
#  (실측 2026-07-22 한미약품: KIS 기준가 372,500[KRX] vs 토스 371,500[NXT] → -1.21% vs -0.94%)
def _yf_frame(ticker, date_str, close):
    import pandas as pd
    idx = pd.to_datetime([date_str])
    return pd.DataFrame({"Close": [close]}, index=idx)


def test_toss_base_price_uses_yfinance_when_capture_missing():
    """[우선순위 3] 저장분이 없으면 yfinance 일봉(KRX 종가)을 쓰고 NXT 폴백으로 내려가지 않는다."""
    import api
    _reset_krx_store({})
    _inject_daily_chart("TSTY", PAST_CHART)  # ref_date=20260713, NXT 캔들=285000
    try:
        with patch.object(api, "fetch_yfinance_data",
                          return_value=_yf_frame("TSTY.KS", "2026-07-13", 284000.0)), \
             patch.object(api, "_toss_krx_close_put") as m_put:
            assert api._toss_base_price("TSTY") == 284000.0   # NXT 285000이 아님
            # 검증값('yf')으로 저장 → 다음부터 2순위(신뢰 조회)에서 종료
            m_put.assert_called_once_with("TSTY", "20260713", 284000.0, source="yf")
    finally:
        _pop_daily_chart("TSTY")


def test_toss_base_price_verified_store_beats_yfinance():
    """검증값('yf')이 저장돼 있으면 yfinance를 다시 호출하지 않는다(네트워크 절약)."""
    import api
    _reset_krx_store({"TSTZ": {"20260713": {"c": 284000.0, "s": "yf"}}})
    _inject_daily_chart("TSTZ", PAST_CHART)
    try:
        with patch.object(api, "fetch_yfinance_data") as m_yf:
            assert api._toss_base_price("TSTZ") == 284000.0
            m_yf.assert_not_called()
    finally:
        _pop_daily_chart("TSTZ")


# --- 분봉 캡처값 신뢰 범위 -------------------------------------------------
#  NXT(넥스트레이드)가 정규장 시간대에 KRX와 병행 체결되고 토스 분봉이 두 거래소를 섞어
#  주므로, 주식의 캡처값은 KRX 종가가 아니다(실측 2026-07-16: 주식 0/10, ETF 15/15).
#  캡처값을 믿으면 yfinance가 영원히 호출되지 않아 오차가 고정된다 — 그 회귀를 막는다.

def test_toss_base_price_distrusts_stock_capture_and_uses_yfinance():
    """[회귀] 주식의 캡처값(구형식)은 신뢰하지 않고 yfinance로 교정해야 한다."""
    import api
    _reset_krx_store({"TSTC": {"20260713": 283000.0}})   # 구형식 = 캡처값(오염)
    _inject_daily_chart("TSTC", PAST_CHART)
    try:
        with patch.object(api, "is_domestic_etf_etn", return_value=False), \
             patch.object(api, "fetch_yfinance_data",
                          return_value=_yf_frame("TSTC.KS", "2026-07-13", 284000.0)):
            assert api._toss_base_price("TSTC") == 284000.0   # 오염된 283000이 아님
    finally:
        _pop_daily_chart("TSTC")


def test_toss_base_price_trusts_etf_capture():
    """ETF는 NXT 미거래라 캡처값이 곧 KRX 종가 → yfinance를 호출하지 않는다."""
    import api
    _reset_krx_store({"TSTE": {"20260713": 283000.0}})
    _inject_daily_chart("TSTE", PAST_CHART)
    try:
        with patch.object(api, "is_domestic_etf_etn", return_value=True), \
             patch.object(api, "fetch_yfinance_data") as m_yf:
            assert api._toss_base_price("TSTE") == 283000.0
            m_yf.assert_not_called()
    finally:
        _pop_daily_chart("TSTE")


def test_toss_base_price_falls_back_to_capture_before_nxt():
    """yfinance가 실패하면 캡처값을 쓰고, NXT 종가까지 내려가지 않는다."""
    import api
    _reset_krx_store({"TSTF": {"20260713": 283000.0}})
    _inject_daily_chart("TSTF", PAST_CHART)   # NXT 캔들 = 285000
    api._toss_yf_base_miss.clear()
    try:
        with patch.object(api, "is_domestic_etf_etn", return_value=False), \
             patch.object(api, "fetch_yfinance_data", return_value=None):
            assert api._toss_base_price("TSTF") == 283000.0   # NXT 285000이 아님
    finally:
        _pop_daily_chart("TSTF")
        api._toss_yf_base_miss.clear()


def test_toss_krx_close_put_does_not_downgrade_verified_value():
    """검증값('yf')을 캡처값('cap')이 덮어써 오염으로 되돌리면 안 된다."""
    import api
    _reset_krx_store({})
    with patch.object(api, "_toss_krx_close_path",
                      return_value=os.path.join(tempfile.mkdtemp(), "k.json")):
        api._toss_krx_close_put("TSTG", "20260713", 284000.0, source="yf")
        api._toss_krx_close_put("TSTG", "20260713", 283000.0, source="cap")
        assert api._toss_krx_close_get("TSTG", "20260713") == 284000.0
        assert api._toss_krx_close_get("TSTG", "20260713", trusted_only=True) == 284000.0


def test_toss_krx_close_get_reads_legacy_and_tagged_formats():
    """구형식(실수)과 신형식(dict) 저장값을 모두 읽어야 한다(하위 호환)."""
    import api
    _reset_krx_store({"TSTH": {"20260713": 283000.0},
                      "TSTI": {"20260713": {"c": 284000.0, "s": "yf"}}})
    assert api._toss_krx_close_get("TSTH", "20260713") == 283000.0
    assert api._toss_krx_close_get("TSTI", "20260713") == 284000.0
    with patch.object(api, "is_domestic_etf_etn", return_value=False):
        assert api._toss_krx_close_get("TSTH", "20260713", trusted_only=True) is None
        assert api._toss_krx_close_get("TSTI", "20260713", trusted_only=True) == 284000.0


def test_toss_yf_krx_close_negative_cache_blocks_refetch():
    """조회 실패는 쿨다운으로 묶어 시세 갱신마다 yfinance를 두드리지 않는다."""
    import api
    api._toss_yf_base_miss.clear()
    with patch.object(api, "fetch_yfinance_data", return_value=None) as m_yf:
        assert api._toss_yf_krx_close("TSTQ", "20260713") is None
        assert api._toss_yf_krx_close("TSTQ", "20260713") is None
        assert m_yf.call_count == 2   # 1회차만 .KS/.KQ 두 번 시도, 2회차는 쿨다운으로 미호출
    api._toss_yf_base_miss.clear()


def test_toss_yf_krx_close_ignores_other_dates():
    """요청한 거래일과 다른 날짜 봉만 오면 값을 만들지 않는다(잘못된 기준가 방지)."""
    import api
    api._toss_yf_base_miss.clear()
    with patch.object(api, "fetch_yfinance_data",
                      return_value=_yf_frame("X.KS", "2026-07-10", 999.0)):
        assert api._toss_yf_krx_close("TSTW", "20260713") is None
    api._toss_yf_base_miss.clear()
