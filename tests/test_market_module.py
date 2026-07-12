import pytest
from unittest.mock import patch, MagicMock
from modules import market
import pandas as pd
import config

@patch('modules.market.api.fetch_yfinance_data')
@patch('modules.market.api.get_domestic_index_chart')
@patch('modules.market.api.get_chart_data')
def test_show_market_indices(mock_get_chart, mock_get_index, mock_fetch_yf):
    """시장 지수 조회 함수 테스트"""
    # Mock Data
    mock_df = pd.DataFrame({
        'date': pd.date_range(start='2023-01-01', periods=100),
        'close': [2500] * 100,
        'open': [2500] * 100,
        'high': [2550] * 100,
        'low': [2450] * 100,
        'volume': [10000] * 100
    })
    
    mock_fetch_yf.return_value = mock_df
    mock_get_index.return_value = mock_df
    mock_get_chart.return_value = mock_df
    
    # yfinance Tickers Mock
    with patch('modules.market.yf.Tickers') as mock_tickers:
        mock_ticker = MagicMock()
        mock_ticker.fast_info.last_price = 2500.0
        mock_ticker.fast_info.regular_market_previous_close = 2490.0
        mock_tickers.return_value.tickers = {'^KS11': mock_ticker}
        
        with patch('rich.prompt.Prompt.ask', side_effect=["8", "q"]):
            with patch('config.console.print') as mock_print:
                market.show_market_indices()
                # 테이블 출력 확인 (호출 횟수로 간접 확인)
                assert mock_print.call_count > 0

# ── 선물/원자재 휴장 시 등락률 0% 방지 (_daily_prev_close_idx) ──
def _futures_daily_df(last_dt_str, closes=(100.0, 110.0)):
    """마지막 봉 날짜를 지정한 2봉짜리 일봉 DF (DatetimeIndex)"""
    from datetime import datetime as dt, timedelta as td
    last = dt.strptime(last_dt_str, "%Y%m%d")
    return pd.DataFrame({'close': list(closes)},
                        index=pd.DatetimeIndex([last - td(days=1), last]))


def test_futures_prev_idx_weekend_stale_price(monkeypatch):
    """휴장(현재가==마지막 봉 종가)이면 -2를 골라 마지막 세션 등락이 표시되는가?"""
    from datetime import datetime, timezone, timedelta
    monkeypatch.setattr(market, '_us_futures_closed_now', lambda: False)  # 가격 동일성만으로 판정
    yday = (datetime.now(timezone.utc) - timedelta(days=2)).strftime("%Y%m%d")
    df = _futures_daily_df(yday, closes=(100.0, 110.0))
    # 현재가가 마지막 봉 종가와 동일 → 시장 정지 상태로 판단 → -2 (전전 봉과 비교)
    assert market._daily_prev_close_idx(df, last_price=110.0, is_futures=True) == -2
    # 현재가가 다르면(장중 오버나이트) 기존 로직 유지 → -1
    assert market._daily_prev_close_idx(df, last_price=111.5, is_futures=True) == -1


def test_futures_prev_idx_weekend_window(monkeypatch):
    """CME 주말 휴장 시간대면 가격이 달라도(피드 오차) -2를 고르는가?"""
    from datetime import datetime, timezone, timedelta
    monkeypatch.setattr(market, '_us_futures_closed_now', lambda: True)
    yday = (datetime.now(timezone.utc) - timedelta(days=2)).strftime("%Y%m%d")
    df = _futures_daily_df(yday)
    assert market._daily_prev_close_idx(df, last_price=110.05, is_futures=True) == -2


def test_futures_prev_idx_today_candle_exists():
    """오늘(UTC) 봉이 이미 있으면 항상 -2 (기존 동작 유지)"""
    from datetime import datetime, timezone
    today = datetime.now(timezone.utc).strftime("%Y%m%d")
    df = _futures_daily_df(today)
    assert market._daily_prev_close_idx(df, last_price=115.0, is_futures=True) == -2


def test_crypto_prev_idx_unaffected(monkeypatch):
    """암호화폐(24/7)는 휴장 보정 없이 기존 로직(-1) 유지"""
    from datetime import datetime, timezone, timedelta
    monkeypatch.setattr(market, '_us_futures_closed_now', lambda: True)
    yday = (datetime.now(timezone.utc) - timedelta(days=2)).strftime("%Y%m%d")
    df = _futures_daily_df(yday)
    assert market._daily_prev_close_idx(df, last_price=110.0, is_futures=False) == -1
