import pytest
import pandas as pd
import numpy as np
from unittest.mock import patch, MagicMock
import sys
import os

# 프로젝트 루트 경로 추가
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import config
from modules.auto_trade import AutoTrader

@pytest.fixture(autouse=True)
def setup_teardown():
    """매 테스트마다 config 설정을 강제 초기화하여 독립성을 보장합니다."""
    original_use = getattr(config, 'USE_CORRELATION_FILTER', True)
    original_threshold = getattr(config, 'CORRELATION_THRESHOLD', 0.7)
    
    # 싱글톤 상태 초기화 (이전 테스트의 미체결 주문 등 상태 누수 방지)
    AutoTrader._instance = None
    
    # 기본 설정으로 테스트 세팅
    config.USE_CORRELATION_FILTER = True
    config.CORRELATION_THRESHOLD = 0.7
    
    yield
    
    # 테스트 종료 후 원상 복구
    config.USE_CORRELATION_FILTER = original_use
    config.CORRELATION_THRESHOLD = original_threshold
    AutoTrader._instance = None


def create_mock_df(pattern_type='same', length=40):
    """수학적으로 통제된 변화율(상관계수)을 가진 가상 일봉 차트 데이터를 생성합니다."""
    dates = pd.date_range(start='2023-01-01', periods=length).strftime('%Y%m%d')
    
    # 기본 변화율 패턴 생성 (사인파 형태로 오르락 내리락)
    base_pct = np.sin(np.linspace(0, 10, length)) * 0.05
    
    if pattern_type == 'same':
        pct_changes = base_pct # 상관계수 1.0 기대
    elif pattern_type == 'opposite':
        pct_changes = -base_pct # 상관계수 -1.0 기대
    else:
        pct_changes = np.random.normal(0, 0.02, length) # 상관계수 0.0 기대 (랜덤)
        
    pct_changes[0] = 0
    prices = 1000 * np.cumprod(1 + pct_changes)
    
    return pd.DataFrame({
        'date': dates, 
        'close': prices,
        'open': prices,
        'high': prices * 1.01,
        'low': prices * 0.99,
        'volume': 1000
    })

@patch('modules.auto_trade.AutoTrader._get_stock_market_type', return_value='KOSPI')
@patch('modules.auto_trade.api.get_realtime_vol_strength', return_value=120.0)
@patch('modules.auto_trade.api.get_chart_data')
def test_correlation_skip_high_correlation(mock_chart, mock_vol, mock_market):
    """1. 상관계수가 임계값(0.7) 이상일 때 매수가 정상적으로 보류(Skip)되는가?"""
    trader = AutoTrader()
    
    # 후보 종목과 보유 종목이 완벽히 동일한 패턴으로 움직임 (상관계수 1.0)
    df_cand = create_mock_df('same', 40)
    df_hold = create_mock_df('same', 40)
    
    mock_chart.return_value = df_cand
    holdings_dfs = {'000660': {'name': 'SK하이닉스', 'df': df_hold}}
    item = {'code': '005930', 'name': '삼성전자'}
    
    result = trader._analyze_candidate_worker(
        item, holding_codes={'000660'}, rules_map={}, restricted_stocks={}, 
        market_regime_adj={'KOSPI': 0.0}, safe_delay=0, reentry_hurdles={}, holdings_dfs=holdings_dfs
    )
    
    # 검증: 상관관계 스킵 타입으로 반환되어야 함
    assert result is not None
    assert result['type'] == 'correlation_skip'
    assert "높은 상관관계" in result['log']
    assert ">= 0.7" in result['log']

@patch('modules.auto_trade.AutoTrader._get_stock_market_type', return_value='KOSPI')
@patch('modules.auto_trade.api.get_realtime_vol_strength', return_value=120.0)
@patch('modules.auto_trade.api.get_chart_data')
def test_correlation_pass_low_correlation(mock_chart, mock_vol, mock_market):
    """2. 상관계수가 임계값 미만(정반대 또는 랜덤)일 때 필터링을 통과하는가?"""
    trader = AutoTrader()
    
    # 후보 종목은 오르고, 보유 종목은 내리는 정반대 패턴 (상관계수 -1.0)
    df_cand = create_mock_df('same', 40)
    df_hold = create_mock_df('opposite', 40)
    
    mock_chart.return_value = df_cand
    holdings_dfs = {'000660': {'name': 'SK하이닉스', 'df': df_hold}}
    item = {'code': '005930', 'name': '삼성전자'}
    
    # 필터를 통과하여 실제 분석(strategy.analyze_buy)으로 넘어가는지 확인하기 위해 모킹
    with patch.object(trader.strategy, 'analyze_buy') as mock_analyze:
        mock_analyze.return_value = {'action': 'wait', 'state': '관망', 'score': 5.0, 'rsi': 50, 'adx': 20, 'cci': 0}
        
        result = trader._analyze_candidate_worker(
            item, {'000660'}, {}, {}, {'KOSPI': 0.0}, 0, {}, holdings_dfs
        )
        
        # 검증: 필터링을 통과했으므로 correlation_skip이 아니어야 함
        assert result['type'] != 'correlation_skip'
        mock_analyze.assert_called_once()

@patch('modules.auto_trade.AutoTrader._get_stock_market_type', return_value='KOSPI')
@patch('modules.auto_trade.api.get_realtime_vol_strength', return_value=120.0)
@patch('modules.auto_trade.api.get_chart_data')
def test_correlation_pass_insufficient_data(mock_chart, mock_vol, mock_market):
    """3. 비교할 수 있는 거래일이 30일 이하일 경우 (신규 상장 등) 스킵하지 않고 통과하는가?"""
    trader = AutoTrader()
    
    # 동일한 패턴이지만 데이터 길이가 20일밖에 안 됨 (len <= 30)
    df_short = create_mock_df('same', 20)
    
    mock_chart.return_value = df_short
    holdings_dfs = {'000660': {'name': 'SK하이닉스', 'df': df_short}}
    item = {'code': '005930', 'name': '삼성전자'}
    
    with patch.object(trader.strategy, 'analyze_buy') as mock_analyze:
        mock_analyze.return_value = {'action': 'wait', 'state': '관망', 'score': 5.0, 'rsi': 50, 'adx': 20, 'cci': 0}
        
        result = trader._analyze_candidate_worker(
            item, {'000660'}, {}, {}, {'KOSPI': 0.0}, 0, {}, holdings_dfs
        )
        
        assert result['type'] != 'correlation_skip'
        mock_analyze.assert_called_once()

@patch('modules.auto_trade.AutoTrader._get_stock_market_type', return_value='KOSPI')
@patch('modules.auto_trade.api.get_realtime_vol_strength', return_value=120.0)
@patch('modules.auto_trade.api.get_chart_data')
def test_correlation_pass_when_disabled(mock_chart, mock_vol, mock_market):
    """4. config 설정에서 기능을 껐을 때(False), 상관계수가 높아도 필터를 통과하는가?"""
    config.USE_CORRELATION_FILTER = False # 기능 강제 종료
    trader = AutoTrader()
    
    df_same = create_mock_df('same', 40)
    mock_chart.return_value = df_same
    holdings_dfs = {'000660': {'name': 'SK하이닉스', 'df': df_same}}
    item = {'code': '005930', 'name': '삼성전자'}
    
    with patch.object(trader.strategy, 'analyze_buy') as mock_analyze:
        mock_analyze.return_value = {'action': 'wait', 'state': '관망', 'score': 5.0, 'rsi': 50, 'adx': 20, 'cci': 0}
        result = trader._analyze_candidate_worker(
            item, {'000660'}, {}, {}, {'KOSPI': 0.0}, 0, {}, holdings_dfs
        )
        assert result['type'] != 'correlation_skip'

@patch('modules.auto_trade.AutoTrader._analyze_candidate_worker')
@patch('modules.auto_trade.api.prefetch_multiple_current_prices')
def test_analyze_candidates_logging(mock_prefetch, mock_worker):
    """5. 다중 종목 분석 시, 스킵된 종목들이 정상적으로 로깅 및 집계되는가?"""
    trader = AutoTrader()
    trader.is_running = True # [추가] 분석 루프가 중간에 종료(break)되지 않도록 상태 변경
    
    # 워커 함수가 첫 번째 종목은 스킵, 두 번째 종목은 통과시켰다고 모킹
    mock_worker.side_effect = [
        {'type': 'correlation_skip', 'name': '삼성전자', 'log': '상관관계 스킵됨'},
        {'type': 'candidate', 'name': 'SK하이닉스', 'data': {'code':'000660', 'name':'SK하이닉스', 'score':8.0, 'rsi': 50}, 'log': '후보선정됨'}
    ]
    
    targets = [{'code': '005930', 'name': '삼성전자'}, {'code': '000660', 'name': 'SK하이닉스'}]
    
    with patch.object(trader, 'log') as mock_log:
        candidates = trader._analyze_candidates(targets, {'005380'}, {}, {}, {'005380': '현대차'})
        
        # 통과한 종목은 1개 뿐이어야 함
        assert len(candidates) == 1
        assert candidates[0]['code'] == '000660'
        
        # 종합 집계 로그가 정상적으로 남았는지 검증
        log_calls = [call.args[0] for call in mock_log.call_args_list]
        assert any("보유 종목과 유사 테마로 매수 보류 (1종목): 삼성전자" in log for log in log_calls)