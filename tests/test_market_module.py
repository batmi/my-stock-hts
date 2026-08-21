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


# ── 개장 판정의 '세션 귀속일' (is_market_open_for_index) ──
#  야간선물은 18:00~익일 05:00, 미국 정규장은 KST 22:30~익일 05:00으로 자정을 넘긴다.
#  두 세션 모두 '오늘(KST)' 날짜로 휴장을 판정하면 새벽 구간에서 결과가 뒤집힌다.
def _fake_now(monkeypatch, kst_str):
    """modules.market 안의 datetime.now()만 고정한다 (다른 모듈은 실제 시각 유지)."""
    from datetime import datetime as real_dt

    fixed = real_dt.strptime(kst_str, "%Y-%m-%d %H:%M")

    class _DT(real_dt):
        @classmethod
        def now(cls, tz=None):
            return fixed if tz is None else real_dt.now(tz)

    monkeypatch.setattr(market, 'datetime', _DT)
    return fixed


def test_k200_night_futures_open_after_midnight(monkeypatch):
    """토요일 새벽(=금요일 야간장)에 야간선물이 개장으로 잡히는가?"""
    _fake_now(monkeypatch, "2026-08-22 00:15")   # 토요일 00:15 = 금요일(08-21) 야간장
    seen = []

    def _holiday_on(d):
        seen.append(d)
        return False                              # 금요일(20260821)은 거래일

    monkeypatch.setattr(market.api, 'is_holiday_on', _holiday_on)
    assert market.is_market_open_for_index("코스피200선물") is True
    assert seen == ["20260821"]                   # '오늘(토)'이 아니라 '전날(금)'로 물어야 한다


def test_k200_night_futures_closed_when_prev_day_holiday(monkeypatch):
    """월요일 새벽(전날=일요일)은 야간장 자체가 없으므로 휴장인가?"""
    _fake_now(monkeypatch, "2026-08-24 03:00")   # 월요일 03:00 → 전날은 일요일
    monkeypatch.setattr(market.api, 'is_holiday_on',
                        lambda d: d == "20260823")   # 일요일만 휴장
    assert market.is_market_open_for_index("코스피200선물") is False


def test_us_regular_open_judged_by_eastern_date(monkeypatch):
    """KST로는 토요일인 시각이라도 미국 동부가 금요일 정규장이면 개장인가?"""
    from datetime import datetime as real_dt
    _fake_now(monkeypatch, "2026-08-22 00:15")           # KST 토요일 00:15
    monkeypatch.setattr(market.api, 'now_us_eastern',
                        lambda: real_dt(2026, 8, 21, 11, 15))   # ET 금요일 11:15
    seen = []

    def _us_holiday_on(d):
        seen.append(d)
        return False

    monkeypatch.setattr(market.api, 'is_us_holiday_on', _us_holiday_on)
    assert market.is_market_open_for_index("나스닥") is True
    assert seen == ["20260821"]                          # 동부 날짜로 물어야 한다


def test_open_sessions_tag_lists_only_open_groups(monkeypatch):
    """제목 표기는 열려 있는 시장 그룹만, 표에 실린 순서대로 나열하는가?"""
    monkeypatch.setattr(market, 'is_market_open_for_index',
                        lambda n: n in ("나스닥", "S&P500 선물", "비트코인"))
    tag = market.open_sessions_tag(["코스피", "나스닥 선물", "나스닥", "S&P500 선물", "비트코인"])
    assert "개장 중 · 미국 정규장, 해외 선물·원자재·금리·FX, 암호화폐" in tag
    assert "KRX 정규장" not in tag


def test_open_sessions_tag_when_everything_closed(monkeypatch):
    """전부 닫혀 있으면 '개장 중' 대신 휴장 문구를 내는가?"""
    monkeypatch.setattr(market, 'is_market_open_for_index', lambda n: False)
    assert "실시간 개장 중인 시장 없음" in market.open_sessions_tag(["코스피", "나스닥"])


def test_europe_open_judged_by_local_time(monkeypatch):
    """KST로는 토요일 00:15여도 유럽 현지가 금요일 장중이면 개장인가?"""
    from datetime import datetime as real_dt
    _fake_now(monkeypatch, "2026-08-22 00:15")                       # KST 토요일 00:15
    monkeypatch.setattr(market.api, 'now_europe_london',
                        lambda: real_dt(2026, 8, 21, 16, 15))        # 런던 금 16:15 (마감 16:30 전)
    monkeypatch.setattr(market.api, 'now_europe_central',
                        lambda: real_dt(2026, 8, 21, 17, 15))        # 프랑크푸르트 금 17:15
    assert market.is_market_open_for_index("UK - FTSE 100") is True
    assert market.is_market_open_for_index("Germany - DAX 40") is True


def test_europe_closed_after_local_bell(monkeypatch):
    """현지 마감(런던 16:30 / 중부유럽 17:30)을 넘기면 닫히는가?"""
    from datetime import datetime as real_dt
    _fake_now(monkeypatch, "2026-08-22 00:35")
    monkeypatch.setattr(market.api, 'now_europe_london',
                        lambda: real_dt(2026, 8, 21, 16, 35))
    monkeypatch.setattr(market.api, 'now_europe_central',
                        lambda: real_dt(2026, 8, 21, 17, 35))
    assert market.is_market_open_for_index("UK - FTSE 100") is False
    assert market.is_market_open_for_index("Europe - STOXX 50") is False


def test_europe_closed_on_sunday_evening(monkeypatch):
    """월요일 새벽(=유럽 일요일 저녁, 세션 없음)이 개장으로 새지 않는가?"""
    from datetime import datetime as real_dt
    _fake_now(monkeypatch, "2026-08-24 01:00")                       # KST 월요일 01:00
    monkeypatch.setattr(market.api, 'now_europe_london',
                        lambda: real_dt(2026, 8, 23, 17, 0))         # 런던 일요일 17:00
    monkeypatch.setattr(market.api, 'now_europe_central',
                        lambda: real_dt(2026, 8, 23, 18, 0))
    assert market.is_market_open_for_index("UK - FTSE 100") is False
    assert market.is_market_open_for_index("France - CAC 40") is False


def test_europe_dst_transition(monkeypatch):
    """유럽 서머타임 전환(3월/10월 마지막 일요일 01:00 UTC)을 옳게 가르는가?"""
    from datetime import datetime as real_dt, timezone as real_tz
    import api as api_mod

    def _at(utc_str):
        fixed = real_dt.strptime(utc_str, "%Y-%m-%d %H:%M").replace(tzinfo=real_tz.utc)

        class _DT(real_dt):
            @classmethod
            def now(cls, tz=None):
                return fixed if tz is not None else fixed.replace(tzinfo=None)

        monkeypatch.setattr(api_mod, 'datetime', _DT)

    _at("2026-08-21 15:00")            # 서머타임 구간
    assert api_mod.now_europe_london().hour == 16      # BST = UTC+1
    assert api_mod.now_europe_central().hour == 17     # CEST = UTC+2

    _at("2026-01-15 15:00")            # 표준시 구간
    assert api_mod.now_europe_london().hour == 15      # GMT = UTC+0
    assert api_mod.now_europe_central().hour == 16     # CET = UTC+1

    # 2026년 전환일: 3월 29일 / 10월 25일 (각 01:00 UTC)
    _at("2026-03-29 00:59")
    assert api_mod.now_europe_london().hour == 0
    _at("2026-03-29 01:00")
    assert api_mod.now_europe_london().hour == 2
    _at("2026-10-25 00:59")
    assert api_mod.now_europe_london().hour == 1
    _at("2026-10-25 01:00")
    assert api_mod.now_europe_london().hour == 1
