import pytest
from unittest.mock import patch, MagicMock
import pandas as pd
from modules import auto_trade, analysis, market
import api
import config

# --- AutoTrade ---
def test_analyze_candidates_restricted():
    """트레이딩 제한 종목 필터링 테스트"""
    trader = auto_trade.AutoTrader()
    trader.is_running = True
    
    targets = [{'code': '005930', 'name': 'Samsung'}]
    
    with patch('modules.auto_trade.load_restricted_stocks', return_value={'005930': {}}), \
         patch('time.sleep'):
        candidates = trader._analyze_candidates(targets, set(), {}, {}, {})
        assert len(candidates) == 0

def test_execute_buy_orders_low_cash():
    """예수금 부족 시 매수 중단 테스트"""
    trader = auto_trade.AutoTrader()
    trader.is_running = True
    
    candidates = [{'code': '005930', 'name': 'Samsung', 'price': 50000, 'score': 9.0}]
    
    with patch.object(trader, 'log') as mock_log:
        trader._execute_buy_orders(candidates, 500, 0.5, 0, 5) # Cash 500 < 1000
        assert any("예수금 부족" in str(c) for c in mock_log.call_args_list)

@patch('modules.auto_trade.analysis.get_domestic_index_data')
def test_update_market_indices_status_exception(mock_analysis_get):
    """지수 상태 업데이트 예외 처리 테스트"""
    trader = auto_trade.AutoTrader()
    mock_analysis_get.side_effect = Exception("API Error")
    
    with patch.object(trader, 'log') as mock_log:
        trader._update_market_indices_status()
        assert any("지수 조회 실패" in str(c) or "지수 데이터 부족/조회 실패" in str(c) for c in mock_log.call_args_list)

# --- Analysis ---
@patch('modules.analysis.api.get_chart_data')
def test_analyze_stock_worker_exception(mock_get):
    """분석 워커 예외 처리 테스트"""
    mock_get.side_effect = Exception("Chart Error")
    res = analysis._analyze_stock_worker({'code': '005930', 'name': 'Samsung'})
    assert res is not None
    assert 'error' in res

# --- Market ---
@patch('modules.market.api.fetch_yfinance_data')
def test_show_market_indices_no_data(mock_fetch):
    """데이터 없음 처리 테스트"""
    mock_fetch.return_value = pd.DataFrame()
    
    # [수정] 사용자 입력을 모킹 (메뉴 선택 '8', 재시도 'n', 메인화면 'q')
    with patch('rich.prompt.Prompt.ask', side_effect=["8", "n", "n", "q"]):
        with patch('config.console.print') as mock_print:
            market.show_market_indices()
            assert mock_print.call_count > 0

# --- API ---
def test_get_chart_data_index_code():
    """지수 코드 처리 테스트"""
    with patch('api.fetch_yfinance_data') as mock_fetch:
        mock_fetch.return_value = pd.DataFrame({'Close': [100], 'Date': [pd.Timestamp.now()]})
        df = api.get_chart_data('^KS11')
        assert not df.empty