import pytest
from unittest.mock import patch, MagicMock, ANY
import pandas as pd
import api
import config
from modules import analysis, auto_trade, market, account, db_manager
import json
import os
from datetime import datetime

@pytest.fixture(autouse=True)
def cleanup_db_connection():
    yield
    db_manager.db.close_connection()

# --- API Coverage ---
@patch('api.call_api')
def test_get_chart_data_overseas_pagination(mock_call):
    """해외 차트 데이터 페이징 및 거래소 순회 테스트"""
    # 1. NASD 실패 -> 2. NYSE 성공 (데이터 2페이지)
    
    def side_effect(url, market, category, action, params=None, **kwargs):
        if params and params.get('EXCD') == 'NAS':
            return {'rt_cd': '0', 'output2': []}
        if params and params.get('EXCD') == 'NYS':
            # Pagination simulation based on some condition (e.g. call count or params)
            today = datetime.now().strftime("%Y%m%d")
            return {
                'rt_cd': '0', 
                'output2': [{'xymd': today, 'clos': '100', 'open': '100', 'high': '110', 'low': '90', 'tovol': '1000'}]
            }
        return {'rt_cd': '1'}

    mock_call.side_effect = side_effect
    
    df = api.get_chart_data("AAPL", is_overseas=True)
    assert not df.empty
    assert len(df) == 1

@patch('api.call_api')
def test_get_deposit_balance_simulation_fallback(mock_call):
    """모의투자 예수금 조회 Fallback 테스트"""
    
    # get_domestic_balance (balance) -> Fail or Empty
    # get_deposit (deposit) -> Success
    
    def side_effect(url, *args, **kwargs):
        if "inquire-balance" in url:
            return {'rt_cd': '0', 'output1': [], 'output2': []} # Empty balance
        if "inquire-psbl-order" in url:
            return {'rt_cd': '0', 'output': {'ord_psbl_cash': '5000000'}}
        return {'rt_cd': '1'}
    
    mock_call.side_effect = side_effect
    
    res = api.get_deposit_balance("12345678", "01")
    assert res['deposit'] == 5000000

# --- Analysis Coverage ---
def test_classify_stock_state_edge_cases():
    """상태 분류 엣지 케이스 테스트"""
    # 1. 데이터 부족
    state, _, _ = analysis.classify_stock_state(None, None, None, None, None, None, None, None, None, None)
    assert state == "-"
    
    # 2. 위험 (RSI 초과매도)
    state, _, _ = analysis.classify_stock_state(10000, 10000, 10000, 10000, 9000, 10, None, 20, 0, True)
    assert state == "매도"
    
    # 3. 주의 (MACD 데드크로스)
    state, _, _ = analysis.classify_stock_state(10000, 10000, 10000, 10000, 9000, 50, None, 20, 0, True, macd=10, macd_signal=20)
    assert state == "주의"

@patch('modules.analysis._get_db_connection')
def test_analysis_db_operations(mock_conn):
    """분석 결과 DB 저장/로드 테스트"""
    mock_cursor = MagicMock()
    mock_conn.return_value.__enter__.return_value.cursor.return_value = mock_cursor
    
    # Save
    analysis._save_analysis_result("KOSPI", [{"code": "005930"}], {})
    mock_cursor.execute.assert_called()
    
    # Load
    mock_cursor.fetchone.return_value = ('2023-01-01', '{}', '[{"code": "005930"}]')
    res = analysis._load_analysis_result("KOSPI")
    assert res['data'][0]['code'] == "005930"

# --- AutoTrade Coverage ---
@patch('modules.auto_trade.api.get_today_history')
@patch('modules.auto_trade.db_manager.db')
def test_conclusion_monitor_sell_logic(mock_db, mock_api):
    """체결 모니터 매도 로직 테스트"""
    monitor = auto_trade.ConclusionMonitor()
    
    # Mock API: 매도 체결
    mock_api.return_value = {
        'rt_cd': '0',
        'output1': [{
            'odno': '999', 'pdno': '005930', 'prdt_name': 'Samsung',
            'tot_ccld_qty': '10', 'avg_prvs': '70000', 'sll_buy_dvsn_cd_name': '매도'
        }]
    }
    
    # Mock DB: 원 주문 정보 (매수 정보가 있어야 수익률 계산됨)
    mock_db.get_trade_by_odno.return_value = {
        'type': '매도', 'profit_amt': 10000, 'profit_rate': 5.0, 'strategy_score': 8.0
    }
    mock_db.check_trade_exists.return_value = False
    
    monitor._check_conclusions(initial=False)
    
    # DB insert verify
    args, kwargs = mock_db.insert_trade.call_args
    assert args[0] == '매도' # type
    # profit_amt 확인 (위치 인자 또는 키워드 인자)
    p_amt = kwargs.get('profit_amt') if 'profit_amt' in kwargs else (args[9] if len(args) > 9 else None)
    assert p_amt == 10000

@patch('modules.auto_trade.api.get_domestic_balance')
@patch('modules.auto_trade.api.get_deposit_balance')
@patch('modules.auto_trade.api.get_current_price_data')
def test_autotrader_check_sell_individual_rule(mock_cp, mock_deposit, mock_balance):
    """개별 룰이 적용된 매도 조건 체크 테스트"""
    trader = auto_trade.AutoTrader()
    trader.is_running = True
    
    # Holdings
    holdings = [{
        'pdno': '005930', 'prdt_name': 'Samsung', 'ord_psbl_qty': '10',
        'evlu_pfls_rt': '5.0', 'prpr': '60000', 'pchs_avg_pric': '57000', 'evlu_pfls_amt': '30000'
    }]
    
    # Individual Rule (익절 3%로 설정 -> 현재 5%이므로 매도해야 함)
    rule = {
        'code': '005930', 'name': 'Samsung', 'take_profit': 3.0, 
        'stop_loss': -5.0, 'take_profit_rsi': 80, 'sell_score': 5.0,
        'ts_activation': 10.0, 'ts_callback': 3.0, 'buy_score': 8.0, 'buy_rsi': 60
    }
    
    with patch('modules.auto_trade.db_manager.db.get_all_stock_strategies', return_value=[rule]), \
         patch('modules.auto_trade.api.get_chart_data') as mock_chart, \
         patch('modules.auto_trade.api.fetch_sellable_quantity', return_value=10), \
         patch('modules.auto_trade.OrderManager.send_order') as mock_send, \
         patch.object(trader.order_manager, 'is_pending', return_value=False): # Pending 상태 강제 해제
        
        # Chart data for analysis
        mock_chart.return_value = pd.DataFrame({'close': [60000]*20, 'high': [60000]*20, 'low': [60000]*20, 'open': [60000]*20, 'volume': [1000]*20})
        
        trader._check_sell_conditions(holdings, is_market_open=True)
        
        mock_send.assert_called()
        args, kwargs = mock_send.call_args
        reason = kwargs.get('reason') if 'reason' in kwargs else (args[6] if len(args) > 6 else "")
        assert "익절" in reason

# --- DB Manager Coverage ---
def test_db_manager_vacuum():
    """VACUUM 실행 테스트"""
    with patch('sqlite3.connect') as mock_connect:
        db_manager.db.run_vacuum()
        mock_connect.return_value.execute.assert_called_with("VACUUM;")

def test_db_cleanup_old_data():
    """오래된 데이터 삭제 테스트"""
    with patch('sqlite3.connect') as mock_connect:
        mock_cursor = MagicMock()
        mock_connect.return_value.cursor.return_value = mock_cursor
        db_manager.db.local.conn = mock_connect.return_value
        
        db_manager.db.cleanup_old_data(30)
        mock_cursor.execute.assert_called()
        assert "DELETE FROM trades" in mock_cursor.execute.call_args[0][0]