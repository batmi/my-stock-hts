# tests/test_coverage_final_v8.py
import pytest
from unittest.mock import patch, MagicMock, ANY
import modules.auto_trade as auto_trade
import config
import pandas as pd
from modules import db_manager

@pytest.fixture(autouse=True)
def cleanup_db_connection():
    yield
    db_manager.db.close_connection()

# --- UI Tests for AutoTrade ---

@patch('rich.prompt.Prompt.ask')
@patch('modules.auto_trade._view_stock_rules')
def test_manage_stock_rules_view(mock_view, mock_ask):
    """룰 관리 메뉴 - 조회"""
    mock_ask.side_effect = ["1", "q"] # 1: 조회 -> q: 종료
    auto_trade.manage_stock_rules()
    mock_view.assert_called_once()

@patch('rich.prompt.Prompt.ask')
@patch('modules.auto_trade._set_stock_rules')
def test_manage_stock_rules_set(mock_set, mock_ask):
    """룰 관리 메뉴 - 설정"""
    mock_ask.side_effect = ["2", "q"]
    auto_trade.manage_stock_rules()
    mock_set.assert_called_once()

@patch('modules.auto_trade.db_manager.db.get_all_stock_strategies')
def test_view_stock_rules_empty(mock_get_rules):
    """룰 조회 - 데이터 없음"""
    mock_get_rules.return_value = []
    with patch('config.console.print') as mock_print:
        auto_trade._view_stock_rules()
        assert any("없습니다" in str(c) for c in mock_print.call_args_list)

@patch('modules.auto_trade.db_manager.db.get_all_stock_strategies')
def test_view_stock_rules_exist(mock_get_rules):
    """룰 조회 - 데이터 있음"""
    mock_get_rules.return_value = [{
        'code': '005930', 'name': 'Samsung', 'buy_score': 8.0, 'buy_rsi': 60,
        'sell_score': 5.0, 'take_profit_rsi': 75, 'take_profit': 10, 'stop_loss': -5,
        'ts_activation': 5, 'ts_callback': 2, 'updated_at': '2023-01-01', 'memo': 'Test',
        'weights': '{"TREND": 4.0, "MOMENTUM": 2.5, "STRENGTH": 1.5, "SYNERGY": 2.0}'
    }]
    with patch('config.console.print') as mock_print:
        auto_trade._view_stock_rules()
        # 테이블 출력 확인
        assert mock_print.call_count > 0

@patch('modules.auto_trade._select_stock_for_rules')
@patch('modules.auto_trade._input_and_save_rule')
def test_set_stock_rules(mock_input, mock_select):
    """룰 설정 흐름"""
    mock_select.return_value = ("005930", "Samsung", False)
    auto_trade._set_stock_rules()
    mock_input.assert_called_with("005930", "Samsung")

@patch('modules.auto_trade.db_manager.db.get_all_stock_strategies')
@patch('rich.prompt.Prompt.ask')
@patch('modules.auto_trade._input_and_save_rule')
def test_modify_stock_rules(mock_input, mock_ask, mock_get_rules):
    """룰 수정 흐름"""
    mock_get_rules.return_value = [{'code': '005930', 'name': 'Samsung', 'buy_score': 8.0, 'buy_rsi': 60, 'sell_score': 5.0, 'take_profit_rsi': 75, 'take_profit': 10, 'stop_loss': -5, 'ts_activation': 5, 'ts_callback': 2, 'updated_at': '2023-01-01'}]
    mock_ask.side_effect = ["1"] # 1번 선택
    
    auto_trade._modify_stock_rules()
    mock_input.assert_called_with("005930", "Samsung")

@patch('modules.auto_trade.db_manager.db.get_all_stock_strategies')
@patch('rich.prompt.Prompt.ask')
@patch('modules.auto_trade.db_manager.db.delete_stock_strategy')
@patch('modules.auto_trade._view_stock_rules')
def test_delete_stock_rules(mock_view, mock_delete, mock_ask, mock_get_rules):
    """룰 삭제 흐름"""
    mock_get_rules.return_value = [{'code': '005930', 'name': 'Samsung', 'buy_score': 8.0, 'buy_rsi': 60, 'sell_score': 5.0, 'take_profit_rsi': 75, 'take_profit': 10, 'stop_loss': -5, 'ts_activation': 5, 'ts_callback': 2, 'updated_at': '2023-01-01'}]
    # 번호선택(1) -> 확인(y)
    mock_ask.side_effect = ["1", "y"]
    
    auto_trade._delete_stock_rules()
    mock_delete.assert_called_with("005930")
    mock_view.assert_called_once()

@patch('modules.auto_trade.api.get_current_price', return_value=60000)
@patch('modules.auto_trade.db_manager.db.get_stock_strategy', return_value=None)
@patch('rich.prompt.Prompt.ask')
@patch('modules.auto_trade.db_manager.db.save_stock_strategy')
@patch('modules.auto_trade._save_rule_weights')
def test_input_and_save_rule_new(mock_save_weights, mock_save, mock_ask, mock_get_strat, mock_price):
    """새로운 룰 입력 및 저장"""
    mock_ask.side_effect = [
        "8.5", "60", "100", "10.0", "y", "75", "5.0", "10.0", "3.0", "10",
        "20", "n", "-5.0", "4.0", "2.5", "1.5", "2.0", "Test Memo"
    ]
    
    auto_trade._input_and_save_rule("005930", "Samsung")
    
    mock_save.assert_called()
    mock_save_weights.assert_called()
    args, _ = mock_save.call_args
    assert args[2]['buy_score'] == 8.5
    assert args[2]['memo'] == "Test Memo"

@patch('modules.auto_trade.load_restricted_stocks')
@patch('modules.auto_trade.api.get_chart_data')
def test_view_restricted_stocks(mock_chart, mock_load):
    """제한 종목 조회 테스트"""
    mock_load.return_value = {'005930': {'name': 'Samsung', 'memo': 'Test', 'date': '2023-01-01'}}
    mock_chart.return_value = pd.DataFrame({'close': [60000]*20, 'high': [60000]*20, 'low': [60000]*20, 'open': [60000]*20, 'volume': [1000]*20})
    
    with patch('config.console.print') as mock_print:
        auto_trade._view_restricted_stocks()
        assert mock_print.call_count > 0

@patch('modules.auto_trade._select_stock_for_rules')
@patch('modules.auto_trade.load_restricted_stocks', return_value={})
@patch('modules.auto_trade.save_restricted_stocks')
@patch('rich.prompt.Prompt.ask')
def test_add_restricted_stock(mock_ask, mock_save, mock_load, mock_select):
    """제한 종목 추가 테스트"""
    mock_select.return_value = ("005930", "Samsung", False)
    mock_ask.return_value = "Memo"
    
    auto_trade._add_restricted_stock()
    
    mock_save.assert_called()
    args, _ = mock_save.call_args
    assert "005930" in args[0]
    assert args[0]["005930"]["memo"] == "Memo"

@patch('modules.auto_trade.load_restricted_stocks')
@patch('modules.auto_trade.save_restricted_stocks')
@patch('rich.prompt.Prompt.ask')
@patch('modules.auto_trade.api.get_chart_data', return_value=None) # 차트 조회 실패 시에도 동작 확인
def test_remove_restricted_stock(mock_chart, mock_ask, mock_save, mock_load):
    """제한 종목 해제 테스트"""
    mock_load.return_value = {'005930': {'name': 'Samsung', 'memo': 'Test'}}
    mock_ask.return_value = "1" # 1번 선택
    
    auto_trade._remove_restricted_stock()
    
    mock_save.assert_called()
    args, _ = mock_save.call_args
    assert "005930" not in args[0]

@patch('rich.prompt.Prompt.ask')
@patch('modules.auto_trade._view_restricted_stocks')
def test_manage_restricted_stocks_menu(mock_view, mock_ask):
    """제한 종목 관리 메뉴 테스트"""
    mock_ask.side_effect = ["1", "q"]
    auto_trade.manage_restricted_stocks_menu()
    mock_view.assert_called_once()

@patch('rich.prompt.Prompt.ask')
@patch('modules.auto_trade.AutoTrader')
def test_system_trading_menu(mock_trader_cls, mock_ask):
    """시스템 트레이딩 메뉴 테스트"""
    mock_trader = mock_trader_cls.return_value
    mock_trader.is_running = False
    
    # 1(Start) 후 종료(q) -> 2(Stop) 후 종료(q) -> 즉시 종료(q)
    mock_ask.side_effect = ["1", "q", "2", "q", "q"]
    
    # 1. Start 실행
    auto_trade.system_trading_menu()
    mock_trader.start.assert_called()
    
    # 2. Stop 실행
    auto_trade.system_trading_menu()
    mock_trader.stop.assert_called()
    
    # 3. 종료 (q)
    auto_trade.system_trading_menu()

# --- Logic/DB Tests ---

@patch('sqlite3.connect')
def test_save_rule_weights(mock_connect):
    """가중치 DB 저장 테스트"""
    mock_conn = MagicMock()
    mock_cursor = MagicMock()
    mock_connect.return_value.__enter__.return_value = mock_conn
    mock_conn.cursor.return_value = mock_cursor
    
    auto_trade._save_rule_weights("005930", {"TREND": 5.0})
    
    mock_cursor.execute.assert_called()
    assert "UPDATE stock_strategies" in mock_cursor.execute.call_args[0][0]

@patch('modules.auto_trade.api.get_today_history')
def test_check_conclusions_rate_limit(mock_api):
    """체결 확인 Rate Limit 감지 테스트"""
    monitor = auto_trade.ConclusionMonitor()
    
    # Rate Limit 응답
    mock_api.return_value = {'rt_cd': '1', 'msg_cd': 'EGW00201'}
    
    is_limited, has_error = monitor._check_conclusions()
    assert is_limited is True

@patch('modules.auto_trade.api.get_overseas_today_history')
@patch('modules.auto_trade.api.get_today_history')
def test_check_conclusions_api_error(mock_api, mock_ovs_api):
    """체결 확인 API 에러 테스트"""
    monitor = auto_trade.ConclusionMonitor()
    
    # General Error
    mock_api.return_value = {'rt_cd': '1', 'msg_cd': 'E999'}
    mock_ovs_api.return_value = {'rt_cd': '1', 'msg_cd': 'E999'}
    
    is_limited, has_error = monitor._check_conclusions()
    assert has_error is True

@patch('modules.auto_trade.utils.validate_and_confirm_stock', return_value=True)
def test_select_stock_for_rules_input(mock_val):
    """종목 선택 헬퍼 - 직접 입력 테스트"""
    with patch('rich.prompt.Prompt.ask') as mock_ask:
        # 5(직접입력) -> 005930
        mock_ask.side_effect = ["5", "005930"]
        
        with patch('modules.auto_trade.api.get_stock_name_by_code', return_value="Samsung"):
            code, name, is_ovs = auto_trade._select_stock_for_rules()
            
            assert code == "005930"
            assert name == "Samsung"
            assert is_ovs is False

def test_select_stock_for_rules_list():
    """종목 선택 헬퍼 - 리스트 선택 테스트"""
    config.session.stock_data = {"stocks_kr": [{"code": "005930", "name": "Samsung"}]}
    
    with patch('rich.prompt.Prompt.ask') as mock_ask:
        # 1(국내주식) -> 1(첫번째)
        mock_ask.side_effect = ["1", "1"]
        
        code, name, is_ovs = auto_trade._select_stock_for_rules()
        
        assert code == "005930"
        assert name == "Samsung"
        assert is_ovs is False