"""토스증권 클라이언트(toss_api) 단위 테스트.

네트워크를 타지 않도록 requests 및 토큰 캐시를 모킹한다.
"""
import json
import time
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
        with patch("toss_api.get_candles", return_value=res):
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
        with patch("toss_api.get_candles", side_effect=fake_candles):
            df = api.get_chart_data("005930", period_type='daily')
    finally:
        config.session.is_toss = False

    # 2페이지(>=260) 확보 후 중단, tail(250) 적용
    assert calls["n"] == 2
    assert len(df) == 250


def test_chart_data_adapter_hourly_unsupported():
    import api
    config.session.is_toss = True
    try:
        df = api.get_chart_data("005930", period_type='hourly')
    finally:
        config.session.is_toss = False
    assert df.empty


def test_current_price_data_adapter():
    import api
    config.session.is_toss = True
    try:
        with patch("toss_api.get_price", return_value={"symbol": "005930", "lastPrice": "72000", "currency": "KRW"}):
            res = api.get_current_price_data("005930", False)
            price = api.get_current_price("005930", False)
    finally:
        config.session.is_toss = False
    assert res['rt_cd'] == '0'
    assert res['output']['stck_prpr'] == '72000'
    assert price == 72000


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
    assert toss_api._group_rps("MARKET_DATA_CHART") == 6
    assert toss_api._group_rps("AUTH") == 2
    # 미정의 그룹은 기본값(config.TOSS_TX_PER_SECOND)
    import config as _cfg
    assert toss_api._group_rps("UNKNOWN_GROUP") == max(_cfg.TOSS_TX_PER_SECOND, 1)

    toss_api._group_cooldown.clear()
    toss_api._note_rate_limited("MARKET_DATA", 2)
    assert toss_api._group_cooldown["MARKET_DATA"] > time.time()


def test_rate_limit_smooths_calls_within_window():
    """그룹 호출이 최소 간격으로 분산되어 즉시 폭주하지 않는다."""
    g = "MARKET_DATA_CHART"  # rps=6 → 최소 간격 ≈ 0.167s
    toss_api._group_hist[g].clear()
    toss_api._group_cooldown.clear()

    t0 = time.time()
    for _ in range(6):
        toss_api._throttle(g)
    elapsed = time.time() - t0
    # 즉시(0초)가 아니라 분산되어야 함 (5구간 × 0.167 ≈ 0.83s)
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
    """정정(전량, req_qty=0) → 수량 미지정/가격만 정정."""
    import api
    config.session.is_toss = True
    captured = {}

    def fake_modify(order_id, order_type="LIMIT", quantity=None, price=None, **kw):
        captured.update(order_id=order_id, quantity=quantity, price=price)
        return {"orderId": "MOD"}

    try:
        with patch("toss_api.modify_order", side_effect=fake_modify):
            res = api.revise_cancel_order("domestic", "revise", "KR1", "005930", 0, 71000, "01", "00")
    finally:
        config.session.is_toss = False
    assert res["rt_cd"] == "0"
    assert res["output"]["ODNO"] == "MOD"
    assert captured["order_id"] == "KR1"
    assert captured["quantity"] is None   # 0 → 전량(미지정)
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
