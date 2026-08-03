import pytest
import pandas as pd
from unittest.mock import patch, MagicMock
from modules import backtest
import config

@pytest.fixture
def sample_df():
    """백테스트용 가상 데이터프레임 생성"""
    data = {
        'date': ['20231001', '20231002', '20231003', '20231004', '20231005'],
        'close': [50000, 51000, 49000, 52000, 53000],
        'open': [49500, 50500, 50000, 49500, 52500],
        'high': [50500, 51500, 50500, 52500, 53500],
        'low': [49000, 50000, 48500, 49000, 52000],
        'EMA20': [49000, 49500, 49600, 49800, 50000],
        'EMA60': [48000, 48100, 48200, 48300, 48400],
        'EMA120': [47000, 47100, 47200, 47300, 47400],
        'SAR': [48000, 48500, 49000, 48000, 48500],
        'RSI': [55.0, 65.0, 45.0, 75.0, 85.0],
        'ADX': [25.0, 26.0, 27.0, 30.0, 35.0],
        'CCI': [100, 150, -50, 200, 250],
        'OBV': [1000, 2000, 1500, 3000, 4000],
        'OBV_MA': [1000, 1200, 1300, 1500, 1800],
        'ATR': [1000, 1000, 1000, 1000, 1000],
    }
    return pd.DataFrame(data)

@patch('modules.analysis.classify_stock_state', return_value=("상승", [], "테스트사유"))
@patch('modules.analysis.calculate_score', return_value=(8.5, {}))
def test_calculate_daily_status(mock_calc, mock_class, sample_df):
    """일별 상태 계산이 analysis 모듈을 잘 호출하는지 검증"""
    row = sample_df.iloc[1]
    prev_row = sample_df.iloc[0]
    
    thresholds = {"WEIGHTS": {}}
    raw_score, sell_check_score, can_buy_state, state, reason = backtest.calculate_daily_status(row, prev_row, thresholds)
    
    assert raw_score == 8.5
    assert sell_check_score == 8.5
    assert can_buy_state is True
    assert state == "상승"
    mock_class.assert_called_once()
    mock_calc.assert_called_once()


@patch('modules.backtest.calculate_daily_status')
def test_simulate_strategy(mock_calc_status, sample_df):
    """시뮬레이션 로직 검증"""
    # 5개의 행에 대해 모두 매수 조건을 만족한다고 가정
    # 첫번째 날 매수, 마지막 날 매도하도록 상태 모킹
    # raw_score, sell_check_score, can_buy_state, state, reason
    mock_calc_status.side_effect = [
        (9.0, 9.0, True, "매수", "강세"), # 1일차
        (9.0, 9.0, True, "매수", "강세"), # 2일차
        (9.0, 9.0, True, "매수", "강세"), # 3일차
        (9.0, 9.0, True, "매수", "강세"), # 4일차
        (0.0, 0.0, False, "매도", "추세이탈") # 5일차: 매도 신호
    ]
    
    # 설정 초기화 방어 (patch.dict로 테스트 종료 시 원복하여 타 테스트 오염 방지)
    with patch.dict(config.SELL_STRATEGY, {
        "STOP_LOSS_RATE": -10.0,
        "TAKE_PROFIT_RATE": 10.0,
        "TAKE_PROFIT_RSI": 80.0,
        "SELL_SCORE": 4.0,
    }):
        res = backtest.simulate_strategy(
            sim_df=sample_df,
            prev_row_init=None,
            initial_capital=1000000,
            buy_score_limit=8.0,
            buy_rsi_limit=70.0,
            is_overseas=False
        )
    
    assert res is not None
    assert "trades" in res
    assert len(res["trades"]) > 0
    # 최소 매수 1회, 매도 1회가 있는지 검증
    types = [t['type'] for t in res["trades"]]
    assert any(t.startswith("매수") for t in types)
    assert any(t.startswith("매도") for t in types)

@patch('api.get_investor_trend', return_value=[
    {'stck_bsop_date': '20231001', 'frgn_ntby_qty': '100', 'orgn_ntby_qty': '100'},
    {'stck_bsop_date': '20231002', 'frgn_ntby_qty': '200', 'orgn_ntby_qty': '200'},
])
def test_append_smart_money_signal(mock_investor, sample_df):
    """수급 데이터 병합이 정상적으로 동작하는지 검증"""
    # 국내 주식 테스트
    df_res = backtest._append_smart_money_signal(sample_df.copy(), "005930", is_overseas=False)
    assert "smart_money" in df_res.columns
    # 해외 주식 테스트 (수급 무시)
    df_res2 = backtest._append_smart_money_signal(sample_df.copy(), "AAPL", is_overseas=True)
    assert bool(df_res2["smart_money"].iloc[0]) is False


# =========================================================
# [동기화] 실매매-백테스트 로직 패리티 테스트 (피라미딩/수익보존/시간청산 유예)
# =========================================================

def _make_bt_df(closes, dates=None, highs=None):
    """패리티 테스트용 최소 컬럼 데이터프레임 생성"""
    n = len(closes)
    if dates is None:
        dates = [(pd.Timestamp('2023-10-01') + pd.Timedelta(days=i)).strftime('%Y%m%d') for i in range(n)]
    if highs is None:
        highs = [c + 500 for c in closes]
    return pd.DataFrame({
        'date': dates, 'close': closes, 'open': closes, 'high': highs,
        'low': [c - 500 for c in closes],
        'RSI': [55.0] * n, 'ADX': [25.0] * n, 'CCI': [100] * n,
        'OBV': [1000] * n, 'OBV_MA': [900] * n, 'ATR': [1000] * n,
        'SAR': [c - 1000 for c in closes],
    })


_PYR_ON = {"PYRAMIDING_USE": True, "PYRAMIDING_PROFIT_TRIGGER": 10.0, "PYRAMIDING_RATIO": 0.5, "PYRAMIDING_MAX_COUNT": 1}
_PYR_OFF = {"PYRAMIDING_USE": False}
_TF_SELL = {"TAKE_PROFIT_RATE": 0.0, "HALF_TAKE_PROFIT_USE": False, "DEFENSIVE_HALF_SELL_USE": False,
            "TAKE_PROFIT_RSI": 0.0, "SELL_SCORE": 5.0, "STOP_LOSS_RATE": -7.0}


@patch('modules.backtest.calculate_daily_status')
def test_backtest_pyramiding_triggers(mock_status):
    """수익 +10% & 매수신호 유지 시 백테스트에서도 피라미딩 증액이 발생해야 함"""
    df = _make_bt_df([50000, 56000, 57000, 57500, 58000])
    mock_status.side_effect = [
        (9.0, 9.0, True, "매수", "강세"),   # 1일차: 신규 매수
        (9.0, 9.0, True, "매수", "강세"),   # 2일차: +11.8% & 매수 유지 → 피라미딩
        (7.0, 7.0, False, "상승", ""),
        (7.0, 7.0, False, "상승", ""),
        (7.0, 7.0, False, "상승", ""),
    ]
    with patch.dict(config.ANALYSIS_THRESHOLDS, _PYR_ON), patch.dict(config.SELL_STRATEGY, _TF_SELL):
        res = backtest.simulate_strategy(
            sim_df=df, prev_row_init=None, initial_capital=10_000_000,
            buy_score_limit=8.0, buy_rsi_limit=70.0, is_overseas=False
        )
    types = [t['type'] for t in res['trades']]
    assert any(t == "매수(일반)" for t in types)
    pyramid_trades = [t for t in types if t.startswith("매수(피라미딩")]
    assert len(pyramid_trades) == 1, f"피라미딩 1회 발생해야 함: {types}"
    assert "매수(피라미딩 1차)" in pyramid_trades


@patch('modules.backtest.calculate_daily_status')
def test_backtest_pyramiding_blocked_below_trigger(mock_status):
    """수익률이 트리거 미달이면 매수신호가 유지돼도 증액하지 않아야 함"""
    df = _make_bt_df([50000, 50500, 50700, 50900, 51000])
    mock_status.side_effect = [(9.0, 9.0, True, "매수", "강세")] * 5
    with patch.dict(config.ANALYSIS_THRESHOLDS, _PYR_ON), patch.dict(config.SELL_STRATEGY, _TF_SELL):
        res = backtest.simulate_strategy(
            sim_df=df, prev_row_init=None, initial_capital=10_000_000,
            buy_score_limit=8.0, buy_rsi_limit=70.0, is_overseas=False
        )
    types = [t['type'] for t in res['trades']]
    assert not any(t.startswith("매수(피라미딩") for t in types)


@patch('modules.backtest.calculate_daily_status')
def test_backtest_profit_lockin_after_half_tp(mock_status):
    """익절/반익절 활성 시: 반익절 → 목표 돌파(천장 해제) → 목표-3% 반납 시 수익보존 전량 매도 (실매매 동일)"""
    df = _make_bt_df([50000, 63000, 76000, 73000])
    mock_status.side_effect = [
        (9.0, 9.0, True, "매수", "강세"),   # 매수
        (7.0, 7.0, False, "상승", ""),      # +25.7% → 반익절
        (7.0, 7.0, False, "상승", ""),      # +51.7% → 천장 해제 (Let profit run)
        (7.0, 7.0, False, "상승", ""),      # +45.7% (목표-3% 이하) → 수익보존
    ]
    sell_opts = {**_TF_SELL, "TAKE_PROFIT_RATE": 50.0, "HALF_TAKE_PROFIT_USE": True}
    with patch.dict(config.ANALYSIS_THRESHOLDS, _PYR_OFF), patch.dict(config.SELL_STRATEGY, sell_opts):
        res = backtest.simulate_strategy(
            sim_df=df, prev_row_init=None, initial_capital=10_000_000,
            buy_score_limit=8.0, buy_rsi_limit=70.0, is_overseas=False
        )
    types = [t['type'] for t in res['trades']]
    assert "매도(반익절)" in types
    assert "매도(수익보존)" in types
    assert "매도(익절)" not in types  # 천장 해제로 고정 익절은 발생하지 않아야 함


@patch('modules.backtest.calculate_daily_status')
def test_backtest_time_stop_momentum_grace(mock_status):
    """시간청산 유예: 상태 유지 + 최근 5일 고점 >= 10일 고점(상방 모멘텀)일 때만 유예 (실매매 동일)"""
    n = 11
    dates = [(pd.Timestamp('2023-01-01') + pd.Timedelta(days=3 * i)).strftime('%Y%m%d') for i in range(n)]
    closes = [50000] + [48500] * (n - 1)  # 매수 후 약 -3% 손실 정체

    # Case A: 고점 유지 (roll5 == roll10) → 유예되어 시간청산 없음
    df_hold = _make_bt_df(closes, dates=dates, highs=[50500] * n)
    mock_status.side_effect = [(9.0, 9.0, True, "매수", "강세")] + [(7.0, 7.0, False, "매수", "")] * (n - 1)
    with patch.dict(config.ANALYSIS_THRESHOLDS, _PYR_OFF), patch.dict(config.SELL_STRATEGY, {**_TF_SELL, "TIME_STOP_USE": True, "TIME_STOP_DAYS": 20, "TIME_STOP_MIN_PROFIT_RATE": 0.0, "USE_ATR_STOP": False}):
        res = backtest.simulate_strategy(
            sim_df=df_hold, prev_row_init=None, initial_capital=10_000_000,
            buy_score_limit=8.0, buy_rsi_limit=70.0, is_overseas=False
        )
    assert not any("시간청산" in t['type'] for t in res['trades']), "상방 모멘텀 유지 시 유예되어야 함"

    # Case B: 고점이 계속 낮아짐 (roll5 < roll10) → 상태가 '매수'여도 시간청산 발동
    df_sell = _make_bt_df(closes, dates=dates, highs=[52000 - 300 * i for i in range(n)])
    mock_status.side_effect = [(9.0, 9.0, True, "매수", "강세")] + [(7.0, 7.0, False, "매수", "")] * (n - 1)
    with patch.dict(config.ANALYSIS_THRESHOLDS, _PYR_OFF), patch.dict(config.SELL_STRATEGY, {**_TF_SELL, "TIME_STOP_USE": True, "TIME_STOP_DAYS": 20, "TIME_STOP_MIN_PROFIT_RATE": 0.0, "USE_ATR_STOP": False}):
        res = backtest.simulate_strategy(
            sim_df=df_sell, prev_row_init=None, initial_capital=10_000_000,
            buy_score_limit=8.0, buy_rsi_limit=70.0, is_overseas=False
        )
    assert any("시간청산" in t['type'] for t in res['trades']), "상방 모멘텀 상실 시 시간청산되어야 함"


# =========================================================
# [동기화] 시장 필터(USE_MARKET_FILTER) 백테스트 모델링 테스트
# =========================================================

@patch('modules.backtest.calculate_daily_status')
def test_backtest_market_filter_blocks_new_entry(mock_status, monkeypatch):
    """시장 필터 차단일에는 점수 충족해도 신규 진입하지 않고, 해제일부터 진입한다."""
    df = _make_bt_df([50000, 51000, 52000, 52500, 53000])
    mock_status.side_effect = [(9.0, 9.0, True, "매수", "강세")] * 5
    dates = list(df['date'])

    # 1~2일차를 지수 약세(차단일)로 지정
    monkeypatch.setattr(backtest, '_MARKET_FILTER_STATE',
                        {"dates": {dates[0], dates[1]}, "desc": "KOSPI < SMA60", "key": None})

    with patch.dict(config.ANALYSIS_THRESHOLDS, _PYR_OFF), patch.dict(config.SELL_STRATEGY, _TF_SELL):
        res = backtest.simulate_strategy(
            sim_df=df, prev_row_init=None, initial_capital=10_000_000,
            buy_score_limit=8.0, buy_rsi_limit=70.0, is_overseas=False
        )

    buys = [t for t in res['trades'] if t['type'].startswith("매수")]
    assert buys, "차단 해제일(3일차)부터는 진입해야 함"
    assert buys[0]['date'] == dates[2]
    assert res.get('missed_market_filter_count', 0) == 2
    mf_missed = [m for m in res.get('missed_trades', []) if '시장 필터' in m.get('reason', '')]
    assert len(mf_missed) == 2


@patch('modules.backtest.calculate_daily_status')
def test_backtest_market_filter_absent_no_effect(mock_status, monkeypatch):
    """차단일 집합이 준비되지 않으면(필터 OFF·직접 호출) 기존과 동일하게 1일차 진입."""
    df = _make_bt_df([50000, 51000, 52000, 52500, 53000])
    mock_status.side_effect = [(9.0, 9.0, True, "매수", "강세")] * 5
    monkeypatch.setattr(backtest, '_MARKET_FILTER_STATE', {"dates": None, "desc": "", "key": None})

    with patch.dict(config.ANALYSIS_THRESHOLDS, _PYR_OFF), patch.dict(config.SELL_STRATEGY, _TF_SELL):
        res = backtest.simulate_strategy(
            sim_df=df, prev_row_init=None, initial_capital=10_000_000,
            buy_score_limit=8.0, buy_rsi_limit=70.0, is_overseas=False
        )

    buys = [t for t in res['trades'] if t['type'].startswith("매수")]
    assert buys and buys[0]['date'] == df['date'].iloc[0]
    assert res.get('missed_market_filter_count', 0) == 0


def test_prepare_market_filter_uses_settings(monkeypatch):
    """prepare_market_filter가 설정값(USE_MARKET_FILTER/MARKET_FILTER_MA)을 그대로 읽어
    '지수 종가 < SMA(설정 MA)' 날짜 집합을 만드는지 검증."""
    monkeypatch.setattr(config, 'USE_MARKET_FILTER', True, raising=False)
    monkeypatch.setattr(config, 'MARKET_FILTER_MA', 3, raising=False)
    monkeypatch.setattr(config, 'MARKET_FILTER_BAND', 0.0, raising=False)  # 단순 이탈 판정
    monkeypatch.setattr(backtest, '_MARKET_FILTER_STATE', {"dates": None, "desc": "", "key": None})

    idx = pd.DataFrame(
        {'Close': [100.0, 100.0, 100.0, 100.0, 50.0]},
        index=pd.date_range('2023-10-01', periods=5, name='Date'),
    )
    with patch('api.fetch_yfinance_data', return_value=idx):
        result = backtest.prepare_market_filter('005930', is_overseas=False, days=30)

    assert result is not None
    cnt, desc = result
    assert 'SMA3' in desc and 'KOSPI' in desc
    assert cnt == 1  # 마지막 날(50 < SMA3)만 차단
    assert '20231005' in backtest._MARKET_FILTER_STATE['dates']


def test_prepare_market_filter_band_hysteresis(monkeypatch):
    """[이탈 확인 밴드] 밴드 안의 되돌림은 상태를 바꾸지 못한다.

    지수가 SMA를 -밴드% 넘게 이탈하면 차단되고, 그 뒤 SMA를 살짝 웃도는 정도(밴드 안)로
    회복해도 차단이 유지된다 — 이것이 '3일짜리 눌림에 매수중단' 휩소를 막는 장치다.
    """
    monkeypatch.setattr(config, 'USE_MARKET_FILTER', True, raising=False)
    monkeypatch.setattr(config, 'MARKET_FILTER_MA', 3, raising=False)
    monkeypatch.setattr(config, 'MARKET_FILTER_BAND', 2.0, raising=False)
    monkeypatch.setattr(backtest, '_MARKET_FILTER_STATE', {"dates": None, "desc": "", "key": None})

    # 100 유지 → 90(이탈, 차단) → 96(SMA 92.0보다 높지만 +2% 밴드 93.84 미만 → 차단 유지)
    idx = pd.DataFrame(
        {'Close': [100.0, 100.0, 100.0, 90.0, 96.0]},
        index=pd.date_range('2023-10-01', periods=5, name='Date'),
    )
    with patch('api.fetch_yfinance_data', return_value=idx):
        cnt, desc = backtest.prepare_market_filter('005930', is_overseas=False, days=30)

    blocked = backtest._MARKET_FILTER_STATE['dates']
    assert '-2%' in desc
    assert '20231004' in blocked          # 이탈일
    assert '20231005' in blocked          # 밴드 안 되돌림 → 차단 유지(히스테리시스)
    assert cnt == 2


def test_market_filter_band_zero_matches_legacy():
    """밴드 0%면 종전 판정('지수 종가 < SMA')과 완전히 동일해야 한다(하위 호환)."""
    import indicators
    import numpy as np

    rng = np.random.default_rng(11)
    close = pd.Series(1000 * np.cumprod(1 + rng.normal(0.0002, 0.013, 400)))
    blocked = indicators.get_market_filter_blocked(close, 60, 0.0)
    legacy = (close < close.rolling(60).mean()).fillna(False)
    assert (blocked.values == legacy.values).all()
    # 워밍업(SMA 미산출) 구간은 차단하지 않는다
    assert not blocked[:59].any()


def test_prepare_market_filter_off(monkeypatch):
    """USE_MARKET_FILTER=False면 차단일을 만들지 않는다(백테스트 미적용)."""
    monkeypatch.setattr(config, 'USE_MARKET_FILTER', False, raising=False)
    monkeypatch.setattr(backtest, '_MARKET_FILTER_STATE', {"dates": None, "desc": "", "key": None})

    with patch('api.fetch_yfinance_data') as mock_fetch:
        assert backtest.prepare_market_filter('005930', is_overseas=False, days=30) is None
    mock_fetch.assert_not_called()
    assert backtest._MARKET_FILTER_STATE['dates'] is None
