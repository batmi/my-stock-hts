"""
상대강도(RS) 필터 테스트 — 소속 지수보다 약한 종목의 신규 매수 게이트 차단.

변경 사항:
  1. analysis.get_index_momentum — 지수의 최근 MOMENTUM_LOOKBACK(126일) 수익률(%).
     데이터 부족/조회 실패 시 None (호출부 fail-open).
  2. _analyze_candidate_worker — USE_RS_FILTER=True일 때 국내 종목의 126일
     수익률이 소속 지수(KOSPI/KOSDAQ) 이하이면 'rs_skip' 반환 (analyze_buy 미진행).
     종목 이력 부족·지수 조회 실패 시에는 통과 (데이터 장애로 매수 전면 중단 방지).

[주의] USE_RS_FILTER의 기본값은 False다 — 실증 검증에서 RS 게이트가 추세 초입 진입을
  막아 순손실로 확인되어 기본 OFF + 설정 숨김 처리되었다(config.py USE_RS_FILTER 주석).
  아래 게이트 테스트는 로직 자체가 살아있는지 확인하기 위해 fixture에서 명시적으로 켠다.
"""

import numpy as np
import pandas as pd
import pytest
from unittest.mock import patch

import config
from modules import analysis
from modules.auto_trade import AutoTrader

LOOKBACK = 126


def make_df(total_return_pct, length=200, start=100.0):
    """전체 기간에 걸쳐 지정 수익률(%)로 지수 성장하는 일봉 df 생성"""
    ratio = 1 + total_return_pct / 100.0
    closes = start * np.power(ratio, np.arange(length) / (length - 1))
    dates = pd.date_range(start='2023-01-01', periods=length).strftime('%Y%m%d')
    return pd.DataFrame({
        'date': dates, 'close': closes, 'open': closes,
        'high': closes * 1.01, 'low': closes * 0.99, 'volume': 1000.0,
    })


def expected_momentum(df, lookback=LOOKBACK):
    cur = float(df['close'].iloc[-1])
    past = float(df['close'].iloc[-(lookback + 1)])
    return (cur / past - 1) * 100


# ─────────────────────────────────────────────
# analysis.get_index_momentum
# ─────────────────────────────────────────────

class TestGetIndexMomentum:
    def test_computes_lookback_return(self, monkeypatch):
        idx_df = make_df(20.0, length=250)
        monkeypatch.setattr(analysis, 'get_domestic_index_data', lambda mt, force_refresh=False: idx_df)
        mom = analysis.get_index_momentum("KOSPI", lookback=LOOKBACK)
        assert mom == pytest.approx(expected_momentum(idx_df), rel=1e-6)

    def test_none_when_insufficient_rows(self, monkeypatch):
        idx_df = make_df(20.0, length=LOOKBACK)  # len <= lookback
        monkeypatch.setattr(analysis, 'get_domestic_index_data', lambda mt, force_refresh=False: idx_df)
        assert analysis.get_index_momentum("KOSPI", lookback=LOOKBACK) is None

    def test_none_when_fetch_fails(self, monkeypatch):
        monkeypatch.setattr(analysis, 'get_domestic_index_data',
                            lambda mt, force_refresh=False: (_ for _ in ()).throw(RuntimeError("network")))
        assert analysis.get_index_momentum("KOSPI") is None

    def test_none_when_empty(self, monkeypatch):
        monkeypatch.setattr(analysis, 'get_domestic_index_data', lambda mt, force_refresh=False: pd.DataFrame())
        assert analysis.get_index_momentum("KOSPI") is None


# ─────────────────────────────────────────────
# 매수 워커 RS 게이트
# ─────────────────────────────────────────────

@pytest.fixture(autouse=True)
def setup_teardown():
    """매 테스트마다 config·싱글톤 상태를 초기화하여 독립성을 보장합니다."""
    original_rs = getattr(config, 'USE_RS_FILTER', False)
    AutoTrader._instance = None
    config.USE_RS_FILTER = True

    patcher1 = patch('modules.auto_trade.api.get_current_price', return_value=0)
    patcher2 = patch('modules.auto_trade.api.get_order_book', return_value={'rt_cd': '0', 'output1': {'total_askp_rsqn': '100', 'total_bidp_rsqn': '100'}})
    patcher3 = patch('modules.auto_trade.api.is_nxt_tradeable', return_value=True)
    patcher1.start(); patcher2.start(); patcher3.start()

    yield

    patcher1.stop(); patcher2.stop(); patcher3.stop()
    config.USE_RS_FILTER = original_rs
    AutoTrader._instance = None


def run_worker(trader, mock_chart, df):
    mock_chart.return_value = df
    item = {'code': '005930', 'name': '삼성전자', 'group': 'stocks_kr'}
    return trader._analyze_candidate_worker(
        item, holding_codes=set(), rules_map={}, restricted_stocks={},
        market_regime_adj={'KOSPI': 0.0}, safe_delay=0, reentry_hurdles={},
        holdings_dfs={}, holding_groups_map={}
    )


@patch('modules.analysis.get_index_momentum', return_value=15.0)
@patch('modules.auto_trade.AutoTrader._get_stock_market_type', return_value='KOSPI')
@patch('modules.auto_trade.api.get_realtime_vol_strength', return_value=120.0)
@patch('modules.auto_trade.api.get_chart_data')
def test_rs_skip_when_weaker_than_index(mock_chart, mock_vol, mock_market, mock_idx):
    """1. 종목 126일 수익률 ≤ 지수 수익률이면 rs_skip으로 차단되는가?"""
    trader = AutoTrader()
    trader.is_running = True
    df = make_df(8.0)  # 전체 +8% → 126일 수익률 < 지수 +15%

    result = run_worker(trader, mock_chart, df)

    assert result is not None
    assert result['type'] == 'rs_skip'
    assert "지수 대비 약세" in result['log']


@patch('modules.analysis.get_index_momentum', return_value=5.0)
@patch('modules.auto_trade.AutoTrader._get_stock_market_type', return_value='KOSPI')
@patch('modules.auto_trade.api.get_realtime_vol_strength', return_value=120.0)
@patch('modules.auto_trade.api.get_chart_data')
def test_rs_pass_when_stronger_than_index(mock_chart, mock_vol, mock_market, mock_idx):
    """2. 지수보다 강한 종목은 게이트를 통과해 analyze_buy로 넘어가는가?"""
    trader = AutoTrader()
    trader.is_running = True
    df = make_df(40.0)  # 126일 수익률 > 지수 +5%

    with patch.object(trader.strategy, 'analyze_buy') as mock_analyze:
        mock_analyze.return_value = {'action': 'wait', 'state': '관망', 'score': 5.0, 'rsi': 50, 'adx': 20, 'cci': 0}
        result = run_worker(trader, mock_chart, df)
        assert result is None or result['type'] != 'rs_skip'
        mock_analyze.assert_called_once()


@patch('modules.analysis.get_index_momentum', return_value=None)
@patch('modules.auto_trade.AutoTrader._get_stock_market_type', return_value='KOSPI')
@patch('modules.auto_trade.api.get_realtime_vol_strength', return_value=120.0)
@patch('modules.auto_trade.api.get_chart_data')
def test_rs_fail_open_when_index_unavailable(mock_chart, mock_vol, mock_market, mock_idx):
    """3. 지수 조회 실패(None) 시 필터를 통과시키는가? (fail-open)"""
    trader = AutoTrader()
    trader.is_running = True
    df = make_df(2.0)  # 약세 종목이라도 지수 기준이 없으면 통과

    with patch.object(trader.strategy, 'analyze_buy') as mock_analyze:
        mock_analyze.return_value = {'action': 'wait', 'state': '관망', 'score': 5.0, 'rsi': 50, 'adx': 20, 'cci': 0}
        result = run_worker(trader, mock_chart, df)
        assert result is None or result['type'] != 'rs_skip'
        mock_analyze.assert_called_once()


@patch('modules.analysis.get_index_momentum', return_value=15.0)
@patch('modules.auto_trade.AutoTrader._get_stock_market_type', return_value='KOSPI')
@patch('modules.auto_trade.api.get_realtime_vol_strength', return_value=120.0)
@patch('modules.auto_trade.api.get_chart_data')
def test_rs_pass_insufficient_history(mock_chart, mock_vol, mock_market, mock_idx):
    """4. 종목 이력이 룩백에 못 미치면(신규 상장 등) 필터를 건너뛰는가?"""
    trader = AutoTrader()
    trader.is_running = True
    df = make_df(2.0, length=100)  # 100일 < 126일 룩백

    with patch.object(trader.strategy, 'analyze_buy') as mock_analyze:
        mock_analyze.return_value = {'action': 'wait', 'state': '관망', 'score': 5.0, 'rsi': 50, 'adx': 20, 'cci': 0}
        result = run_worker(trader, mock_chart, df)
        assert result is None or result['type'] != 'rs_skip'
        mock_analyze.assert_called_once()


@patch('modules.analysis.get_index_momentum', return_value=15.0)
@patch('modules.auto_trade.AutoTrader._get_stock_market_type', return_value='KOSPI')
@patch('modules.auto_trade.api.get_realtime_vol_strength', return_value=120.0)
@patch('modules.auto_trade.api.get_chart_data')
def test_rs_filter_disabled(mock_chart, mock_vol, mock_market, mock_idx):
    """5. USE_RS_FILTER=False면 약세 종목도 게이트에서 걸리지 않는가?"""
    config.USE_RS_FILTER = False
    trader = AutoTrader()
    trader.is_running = True
    df = make_df(2.0)

    with patch.object(trader.strategy, 'analyze_buy') as mock_analyze:
        mock_analyze.return_value = {'action': 'wait', 'state': '관망', 'score': 5.0, 'rsi': 50, 'adx': 20, 'cci': 0}
        result = run_worker(trader, mock_chart, df)
        assert result is None or result['type'] != 'rs_skip'
        mock_analyze.assert_called_once()
        mock_idx.assert_not_called()
