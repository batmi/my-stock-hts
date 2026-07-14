"""토스증권 클라이언트(toss_api) 단위 테스트.

네트워크를 타지 않도록 requests 및 토큰 캐시를 모킹한다.
"""
import json
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
        with patch("toss_api.get_candles", return_value=res), \
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
    """KRX 마감가 저장소를 테스트용으로 초기화(디스크 재로드 차단)."""
    import api
    api._toss_krx_close_store = entries if entries is not None else {}


def test_toss_base_price_falls_back_to_prev_nxt_candle():
    """[폴백] 저장된 KRX 마감가가 없으면 전일 NXT 종가(일봉 직전 캔들)로 계산한다(역산·yfinance 없음)."""
    import api
    _reset_krx_store({})  # 저장분 없음
    _inject_daily_chart("TSTA", PAST_CHART)  # 마지막 캔들 20260713 < 오늘 → 그 종가
    try:
        with patch("toss_api.get_price_limit") as m_pl:
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
             patch.object(api, "_toss_capture_krx_close"):  # 마감가 캡처(분봉 조회) 격리
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
        with patch("api.get_current_price_data", return_value=curr), \
             patch("api.get_chart_data", return_value=df.copy()), \
             patch("api.get_investor_trend", return_value=[]), \
             patch("api.get_order_book", return_value=ob), \
             patch("api.get_realtime_vol_strength", return_value=None), \
             patch("modules.analysis.check_smart_money_turnaround", return_value=(False, "")):
            result = analysis._print_table_worker(
                ("삼성전자", "005930"), "국내 주식 기술적 분석",
                False, False, set(), {}, {}, set(), set())
    finally:
        config.session.is_toss = False

    rate_str = result[0][4]  # 등락폭 (등락률) [매도비] 셀
    assert "2.00" in rate_str     # 매도잔량비 숫자만 표시
    assert "배" not in rate_str    # '배' 단위 제거
    assert "[0%]" not in rate_str  # 체결강도(강도) 형식 아님


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


def test_overseas_detail_na_for_toss():
    import api
    config.session.is_toss = True
    try:
        assert api.fetch_overseas_detail_price("AAPL", "NAS") is None
    finally:
        config.session.is_toss = False


def test_rate_limit_group_rps_and_cooldown():
    """그룹별 RPS 설정 및 429 쿨다운 동작."""
    assert toss_api._group_rps("MARKET_DATA_CHART") == 10  # 토스 최대치(10 TPS)
    assert toss_api._group_rps("MARKET_DATA") == 10
    assert toss_api._group_rps("ORDER") == 10
    assert toss_api._group_rps("AUTH") == 2  # 토큰 발급은 보수적 유지
    # 미정의 그룹은 기본값(config.TOSS_TX_PER_SECOND)
    import config as _cfg
    assert toss_api._group_rps("UNKNOWN_GROUP") == max(_cfg.TOSS_TX_PER_SECOND, 1)

    toss_api._group_cooldown.clear()
    toss_api._note_rate_limited("MARKET_DATA", 2)
    assert toss_api._group_cooldown["MARKET_DATA"] > time.time()


def test_rate_limit_smooths_calls_within_window():
    """그룹 호출이 최소 간격으로 분산되어 즉시 폭주하지 않는다."""
    g = "MARKET_DATA_CHART"  # rps=10 → 최소 간격 0.1s
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
def test_index_chart_toss_uses_yfinance_not_kis():
    """토스 모드: 국내 지수 차트가 KIS 대신 yfinance(get_chart_data)로 라우팅."""
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
             patch("api.call_api") as mock_call:
            df = api.get_domestic_index_chart("0001")  # KOSPI
    finally:
        config.session.is_toss = False
    assert captured["ticker"] == "^KS11"
    assert captured["is_overseas"] is True
    assert not df.empty
    mock_call.assert_not_called()  # KIS 미호출


def test_index_price_toss_uses_fast_info():
    """토스 모드: 국내 지수 현재가가 KIS 대신 yfinance fast_info → KIS 형태 반환."""
    import api
    config.session.is_toss = True
    try:
        with patch("api.get_yf_fast_info",
                   return_value={"last_price": 850.5, "regular_market_previous_close": 845.0}), \
             patch("api.call_api") as mock_call:
            res = api.get_domestic_index_price("1001")  # KOSDAQ
    finally:
        config.session.is_toss = False
    assert res["rt_cd"] == "0"
    assert res["output"]["bstp_nmix_prpr"] == "850.5"
    assert res["output"]["bstp_nmix_prdy_clpr"] == "845.0"
    mock_call.assert_not_called()


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


def test_kosdaq150_skipped_in_toss():
    """토스 모드: 코스닥150은 KIS/yfinance 모두 미사용 → 데이터 없음(None/empty)."""
    import api
    import modules.analysis as analysis
    config.session.is_toss = True
    try:
        # 토스 모드면 KIS(get_domestic_index_chart)도, yfinance(get_chart_data)도 호출되면 안 됨
        with patch("api.get_domestic_index_chart") as mock_kis, \
             patch("api.get_chart_data") as mock_yf:
            df = analysis.get_domestic_index_data("KOSDAQ150", force_refresh=True)
    finally:
        config.session.is_toss = False
    assert df is None or df.empty
    mock_kis.assert_not_called()   # KIS 미호출
    mock_yf.assert_not_called()    # yfinance(^KQ150)도 미호출 (스킵)


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
