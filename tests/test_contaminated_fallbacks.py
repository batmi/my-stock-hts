"""오염된 값으로 메우느니 비운다 — 2026-10-05 폴백 정리 회귀.

① 국내 지수 yfinance 최후 폴백 제거(test_toss_api 의 폴백 체인 테스트가 지킨다)
② 국내 백테스트 yfinance·차트API 폴백 제거(test_backtest_data_source 가 지킨다)
③ 토스 등락률 기준가: 검증 출처가 없으면 비운다 — 화면은 0% 가 아니라 '-', 알림은 현재가만
④ 토스 캔들(NXT 혼입) 일봉 종목은 신규 매수·피라미딩만 막고 청산은 막지 않는다
⑤ 지수 조회 실패 시 직전 데이터는 2거래일 이내만 대신 쓴다
"""
import inspect
from unittest.mock import patch

import pandas as pd
import pytest

import api
import config
from modules import analysis
from modules.auto_trade import conclusion, trader


# ---------------------------------------------------------------- ③ 기준가를 모를 때

def test_adapter_leaves_rate_fields_empty_without_verified_base():
    config.session.is_toss = True
    try:
        with patch("brokers.toss_api.get_price",
                   return_value={"symbol": "005930", "lastPrice": "72000", "currency": "KRW"}), \
             patch.object(api, "_toss_capture_krx_close"), \
             patch.object(api, "_toss_capture_break_close"), \
             patch.object(api, "_toss_base_price", return_value=None):
            out = api.get_current_price_data("005930", False)['output']
    finally:
        config.session.is_toss = False
    assert out['stck_prpr'] == '72000'
    assert 'prdy_ctrt' not in out and 'stck_sdpr' not in out


@pytest.mark.parametrize("output,expected", [
    ({'prdy_ctrt': '1.234'}, " (🔺 +1.23%)"),
    ({'prdy_ctrt': '-0.5'}, " (🔻 -0.50%)"),
    ({'prdy_ctrt': '0'}, " (➖ +0.00%)"),
    ({}, ""),                       # 모르면 꼬리를 붙이지 않는다(현재가 줄은 남는다)
    ({'prdy_ctrt': ''}, ""),
    (None, ""),
])
def test_notice_rate_suffix(output, expected):
    assert conclusion._rate_suffix(output) == expected


# ---------------------------------------------------------------- ④ 진입만 막는다

def test_contaminated_reason_only_in_toss_mode():
    with patch.object(api, "get_krx_fallback", return_value={"005930": "Open API·FDR 모두 실패"}):
        config.session.is_toss = True
        try:
            assert trader.contaminated_chart_reason("005930") == "Open API·FDR 모두 실패"
            assert trader.contaminated_chart_reason("000660") is None
        finally:
            config.session.is_toss = False
        # KIS 모드 일봉은 KRX 정규장 기준이라 이 폴백 자체가 없다
        assert trader.contaminated_chart_reason("005930") is None


def test_unknown_fallback_state_blocks_rather_than_opens():
    config.session.is_toss = True
    try:
        with patch.object(api, "get_krx_fallback", side_effect=RuntimeError("boom")):
            assert trader.contaminated_chart_reason("005930")
    finally:
        config.session.is_toss = False


def test_both_buy_paths_share_the_guard_and_sell_paths_do_not():
    """신규 매수·피라미딩은 같은 가드를 가져야 하고([[seed-spend-guard-parity]]),
    청산 경로에는 들어가면 안 된다 — 장애 중 손절·트레일링이 멈추는 것이 더 위험하다."""
    src_buy = inspect.getsource(trader.AutoTrader._execute_buy_orders)
    src_pyr = inspect.getsource(trader.AutoTrader._try_pyramid_buy)
    assert "contaminated_chart_reason(" in src_buy
    assert "contaminated_chart_reason(" in src_pyr
    module_src = inspect.getsource(trader)
    assert module_src.count("contaminated_chart_reason(") == 3, \
        "정의 + 매수 + 피라미딩 세 곳 밖(예: 청산)에 쓰이면 안 된다"


# ---------------------------------------------------------------- ⑤ 직전 지수 데이터의 기한

def _idx(last_date):
    return pd.DataFrame({"date": [last_date], "open": 1.0, "high": 1.0, "low": 1.0,
                         "close": 1.0, "volume": 0.0})


@pytest.mark.parametrize("last,lag", [
    ("20261002", 0),        # 금 = 시장 기준일
    ("20261001", 1),        # 목
    ("20260930", 2),        # 수
    ("20260929", 3),        # 화 — 기한(2) 초과
])
def test_lag_counts_trading_days(last, lag):
    with patch.object(analysis, "_current_market_day", return_value="20261002"), \
         patch.object(api, "is_holiday_on", side_effect=lambda d: pd.Timestamp(d).weekday() >= 5):
        assert analysis._index_lag_trading_days(_idx(last)) == lag


def test_lag_skips_weekend_and_holidays():
    """금요일 데이터 → 다음 주 화요일(월요일 휴장)이면 1거래일 묵음이다."""
    holidays = {"20261005"}
    with patch.object(analysis, "_current_market_day", return_value="20261006"), \
         patch.object(api, "is_holiday_on",
                      side_effect=lambda d: d in holidays or pd.Timestamp(d).weekday() >= 5):
        assert analysis._index_lag_trading_days(_idx("20261002")) == 1


def _locked_fetch(stale_last, monkeypatch):
    monkeypatch.setattr(analysis, "_index_cache_enabled", lambda: True)
    monkeypatch.setattr(analysis, "_lookup_index_cache", lambda m: ("expired", _idx(stale_last)))
    monkeypatch.setattr(analysis, "_fetch_domestic_index_data", lambda m: None)
    monkeypatch.setattr(analysis, "_store_index_cache", lambda m, d: None)
    monkeypatch.setattr(analysis, "_current_market_day", lambda: "20261002")
    monkeypatch.setattr(api, "is_holiday_on", lambda d: pd.Timestamp(d).weekday() >= 5)
    return analysis._fetch_index_locked("KOSPI", False)


def test_recent_stale_index_is_still_served(monkeypatch):
    out = _locked_fetch("20261001", monkeypatch)
    assert out is not None and str(out['date'].iloc[-1]) == "20261001"


def test_old_stale_index_is_not_served(monkeypatch, caplog):
    with caplog.at_level("WARNING"):
        out = _locked_fetch("20260925", monkeypatch)
    assert out is None, "며칠 묵은 지수로 국면·시장 필터를 돌리면 안 된다"
    assert any("쓰지 않는다" in r.getMessage() for r in caplog.records)
