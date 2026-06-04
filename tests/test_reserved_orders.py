import pytest
import os
import sys
from datetime import datetime
from unittest.mock import patch, MagicMock

# 프로젝트 루트 경로를 sys.path에 추가하여 모듈 임포트 가능하도록 설정
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import config
from modules.db_manager import DBManager
from modules import trading
from modules.telegram_bot import TelegramCommander

# -------------------------------------------------------------------
# 1. DBManager 예약 주문 처리 테스트
# -------------------------------------------------------------------
@pytest.fixture
def test_db():
    """테스트용 독립된 SQLite DB 인스턴스 생성"""
    old_db_path = config.DB_FILE_PATH
    test_db_path = "test_reserved_orders.db"
    config.DB_FILE_PATH = test_db_path
    
    db = DBManager()
    yield db
    
    db.close_connection()
    if os.path.exists(test_db_path):
        os.remove(test_db_path)
    config.DB_FILE_PATH = old_db_path

def test_db_reserved_orders_lifecycle(test_db):
    """예약 주문의 등록, 조회, 상태 업데이트, 취소 라이프사이클 테스트"""
    # 1. 예약 주문 등록 (매수)
    test_db.insert_reserved_order(
        "12345678", "01", "KR", "buy", "005930", "삼성전자", 
        10, 79000.0, "LIMIT", 79000.0, "", "20991231"
    )
    
    # 2. 대기 중인 주문 조회
    pending = test_db.get_pending_reserved_orders()
    assert len(pending) == 1
    order = pending[0]
    assert order["code"] == "005930"
    assert order["order_type"] == "buy"
    assert order["status"] == "PENDING"
    
    # 3. 상태 업데이트 (취소)
    test_db.update_reserved_order_status(order["id"], "CANCELED")
    assert len(test_db.get_pending_reserved_orders()) == 0
    
    # 4. 일괄 취소 기능 테스트 (매수)
    test_db.insert_reserved_order("12345678", "01", "KR", "buy", "000660", "SK하이닉스", 10, 150000.0, "LIMIT", 149000.0, "", "20991231")
    test_db.cancel_reserved_buy_orders("12345678", "01", "000660")
    assert len(test_db.get_pending_reserved_orders()) == 0

    # 5. 일괄 취소 기능 테스트 (매도)
    test_db.insert_reserved_order("12345678", "01", "KR", "sell", "000660", "SK하이닉스", 10, 160000.0, "LIMIT", 161000.0, "", "20991231")
    test_db.cancel_reserved_sell_orders("12345678", "01", "000660")
    assert len(test_db.get_pending_reserved_orders()) == 0

def test_db_reserved_orders_update_tracking_prices(test_db):
    """트레일링 매수/매도를 위한 최저점/최고점 업데이트 테스트"""
    test_db.insert_reserved_order("12345678", "01", "KR", "buy", "005930", "삼성전자", 10, 79000.0, "TRAILING_BUY", 3.0, "", "20991231")
    order_id = test_db.get_pending_reserved_orders()[0]["id"]
    
    test_db.update_reserved_order_lowest(order_id, 68000.0)
    test_db.update_reserved_order_highest(order_id, 75000.0)
    
    conn = test_db._get_conn()
    row = conn.execute("SELECT lowest_price, highest_price FROM reserved_orders WHERE id=?", (order_id,)).fetchone()
    assert row["lowest_price"] == 68000.0
    assert row["highest_price"] == 75000.0

def test_db_reserved_orders_update_with_odno_and_fail_reason(test_db):
    """주문번호(odno) 및 실패 사유(fail_reason)를 포함한 상태 업데이트 테스트"""
    test_db.insert_reserved_order("12345678", "01", "KR", "buy", "005930", "삼성전자", 10, 79000.0, "LIMIT", 79000.0, "", "20991231")
    order_id1 = test_db.get_pending_reserved_orders()[0]["id"]
    test_db.update_reserved_order_status(order_id1, "FILLED", odno="987654321")
    
    test_db.insert_reserved_order("12345678", "01", "KR", "buy", "000660", "SK하이닉스", 10, 150000.0, "LIMIT", 149000.0, "", "20991231")
    order_id2 = test_db.get_pending_reserved_orders()[0]["id"]
    test_db.update_reserved_order_status(order_id2, "FAILED", fail_reason="잔고 부족")
    
    conn = test_db._get_conn()
    assert conn.execute("SELECT odno FROM reserved_orders WHERE id=?", (order_id1,)).fetchone()["odno"] == "987654321"
    assert conn.execute("SELECT fail_reason FROM reserved_orders WHERE id=?", (order_id2,)).fetchone()["fail_reason"] == "잔고 부족"


# -------------------------------------------------------------------
# 2. Trading 모듈 예약 주문 UI 로직 테스트
# -------------------------------------------------------------------
@patch('modules.trading.Prompt.ask')
@patch('modules.trading.utils.show_menu')
@patch('modules.trading.select_account')
@patch('modules.trading.utils.select_target_stock')
@patch('modules.trading.api.get_current_price')
@patch('modules.trading.api.send_telegram_message')
@patch('modules.trading.db_manager.db.insert_reserved_order')
def test_register_reserved_order_buy_limit(mock_insert, mock_tg, mock_get_price, mock_select_stock, mock_select_account, mock_show_menu, mock_ask):
    """지정가 조건의 예약 매수 등록 흐름 테스트"""
    mock_select_account.return_value = ("12345678", "01", "실전투자")
    
    # show_menu: [1] 예약 매수 -> [3] 지정가 도달 (LIMIT)
    mock_show_menu.side_effect = ["1", "3"] 
    mock_select_stock.return_value = ("005930", "삼성전자", False)
    mock_get_price.return_value = 80000.0
    
    # Prompt 입력값 순서:
    # 1. 목표가: '79000'
    # 2. 주문단가: '' (목표가와 동일하게 자동 설정)
    # 3. 주문수량: '10'
    # 4. 유효기간: '4' (무기한)
    # 5. 최종확인: 'y'
    mock_ask.side_effect = ["79000", "", "10", "4", "y"]
    
    result = trading.register_reserved_order()
    
    assert result is None # 에러 없이 정상 반환
    mock_insert.assert_called_once()
    kwargs = mock_insert.call_args[1]
    assert kwargs['cano'] == "12345678"
    assert kwargs['order_type'] == "buy"
    assert kwargs['code'] == "005930"
    assert kwargs['qty'] == 10
    assert kwargs['condition_type'] == "LIMIT"
    assert kwargs['target_price'] == 79000.0
    assert kwargs['order_price'] == 79000.0
    assert kwargs['expire_dt'] == "20991231"
    mock_tg.assert_called_once()

@patch('modules.trading.Prompt.ask')
@patch('modules.trading.utils.show_menu')
@patch('modules.trading.select_account')
@patch('modules.trading.select_stock_from_balance')
@patch('modules.trading.api.get_current_price')
@patch('modules.trading.db_manager.db.insert_reserved_order')
def test_register_reserved_order_sell_time(mock_insert, mock_get_price, mock_select_bal, mock_select_account, mock_show_menu, mock_ask):
    """시간 조건 및 시장가 예약 매도 등록 흐름 테스트 (퍼센트 및 M 입력)"""
    mock_select_account.return_value = ("12345678", "01", "실전투자")
    
    # show_menu: [2] 예약 매도 -> [4] 특정 시간 (TIME)
    mock_show_menu.side_effect = ["2", "4"] 
    mock_select_bal.return_value = ("AAPL", "Apple", True, "NAS", {"qty": 50, "buy_price": 150.0})
    mock_get_price.return_value = 160.0
    
    # Prompt 입력값 순서:
    # 1. 시간 조건: '1' (당일 장 마감 무렵 15:20)
    # 2. 주문단가: 'm' (발동 시 시장가)
    # 3. 주문수량: '25'
    # 4. 유효기간: '1' (당일)
    # 5. 최종확인: 'y'
    mock_ask.side_effect = ["1", "m", "25", "1", "y"]
    
    trading.register_reserved_order()
    
    mock_insert.assert_called_once()
    kwargs = mock_insert.call_args[1]
    assert kwargs['order_type'] == "sell"
    assert kwargs['code'] == "AAPL"
    assert kwargs['qty'] == 25
    assert kwargs['condition_type'] == "TIME"
    assert kwargs['target_time'].endswith("1520")
    assert kwargs['order_price'] == 0.0 # 시장가 처리는 0.0

@patch('modules.trading.Prompt.ask')
@patch('modules.trading.db_manager.db.get_pending_reserved_orders')
@patch('modules.trading.db_manager.db.update_reserved_order_status')
def test_manage_reserved_orders(mock_update, mock_get_orders, mock_ask):
    """예약 주문 관리 및 삭제 처리 테스트"""
    mock_get_orders.side_effect = [
        [
            {
                "id": 1, "cano": "12345678", "acnt": "01", "market": "KR", 
                "order_type": "buy", "code": "005930", "name": "삼성전자",
                "qty": 10, "order_price": 79000.0, "condition_type": "LIMIT",
                "target_price": 79000.0, "target_time": "", "expire_dt": "20991231"
            }
        ],
        []
    ]
    # 취소할 ID 입력, 두 번째 루프에서는 빈 리스트를 반환하여 종료됨
    mock_ask.side_effect = ["1", "q"]
    
    with patch('modules.trading.api.send_telegram_message') as mock_tg:
        trading.manage_reserved_orders()
        mock_update.assert_called_once_with(1, 'CANCELED')
        mock_tg.assert_called_once()
        assert "ID: 1" in mock_tg.call_args[0][0]

@patch('modules.trading.Prompt.ask')
@patch('modules.trading.utils.show_menu')
@patch('modules.trading.select_account')
@patch('modules.trading.utils.select_target_stock')
@patch('modules.trading.api.get_current_price')
@patch('modules.trading.db_manager.db.insert_reserved_order')
def test_register_reserved_order_score_up(mock_insert, mock_get_price, mock_select_stock, mock_select_account, mock_show_menu, mock_ask):
    """퀀트 점수(SCORE_UP) 조건의 예약 매수 등록 흐름 테스트"""
    mock_select_account.return_value = ("12345678", "01", "실전투자")
    mock_show_menu.side_effect = ["1", "5"] # 1: 예약 매수, 5: 퀀트 점수(SCORE)
    mock_select_stock.return_value = ("005930", "삼성전자", False)
    mock_get_price.return_value = 80000.0
    # Prompt 입력: 목표점수(7.5), 발동방향(1:이상돌파), 주문단가(0:현재가), 주문수량(10), 유효기간(4:무기한), 최종확인(y)
    mock_ask.side_effect = ["7.5", "1", "0", "10", "4", "y"]
    
    with patch('modules.trading.api.send_telegram_message'):
        trading.register_reserved_order()
        
    mock_insert.assert_called_once()
    assert mock_insert.call_args[1]['condition_type'] == "SCORE_UP"
    assert mock_insert.call_args[1]['target_price'] == 7.5

@patch('modules.trading.Prompt.ask')
@patch('modules.trading.utils.show_menu')
@patch('modules.trading.select_account')
@patch('modules.trading.utils.select_target_stock')
@patch('modules.trading.api.get_current_price')
@patch('modules.trading.db_manager.db.insert_reserved_order')
def test_register_reserved_order_trailing_buy(mock_insert, mock_get_price, mock_select_stock, mock_select_account, mock_show_menu, mock_ask):
    """퍼센트(%) 단가 입력을 포함한 트레일링 매수 등록 흐름 테스트"""
    mock_select_account.return_value = ("12345678", "01", "실전투자")
    mock_show_menu.side_effect = ["1", "6"] # 1: 예약 매수, 6: 트레일링 매수
    mock_select_stock.return_value = ("005930", "삼성전자", False)
    mock_get_price.return_value = 80000.0
    # Prompt 입력: 반등폭(3.0%), 주문단가(-1%), 주문수량(10), 유효기간(1:당일), 최종확인(y)
    mock_ask.side_effect = ["3.0", "-1%", "10", "1", "y"]
    
    with patch('modules.trading.api.send_telegram_message'):
        trading.register_reserved_order()
        
    mock_insert.assert_called_once()
    assert mock_insert.call_args[1]['condition_type'] == "TRAILING_BUY"
    assert mock_insert.call_args[1]['target_price'] == 3.0
    assert mock_insert.call_args[1]['order_price'] == 79200.0 # 80000원 기준 -1% 적용

@patch('modules.trading.Prompt.ask')
@patch('modules.trading.utils.show_menu')
@patch('modules.trading.select_account')
@patch('modules.trading.utils.select_target_stock')
@patch('modules.trading.api.get_current_price')
def test_register_reserved_order_cancel_by_user(mock_get_price, mock_select_stock, mock_select_account, mock_show_menu, mock_ask):
    """사용자가 예약 매매 도중 취소(q)를 입력했을 때의 처리 확인"""
    mock_select_account.return_value = ("12345678", "01", "실전투자")
    mock_show_menu.side_effect = ["1", "3"] # 1: 예약 매수, 3: 지정가 도달
    mock_select_stock.return_value = ("005930", "삼성전자", False)
    mock_get_price.return_value = 80000.0
    # 목표가 입력 단계에서 'q' 입력
    mock_ask.side_effect = ["q"]
    
    # 중단 시 False 반환
    result = trading.register_reserved_order()
    assert result is False

@patch('modules.trading.Prompt.ask')
@patch('modules.trading.utils.show_menu')
@patch('modules.trading.select_account')
@patch('modules.trading.utils.select_target_stock')
@patch('modules.trading.api.get_current_price')
@patch('modules.trading.db_manager.db.insert_reserved_order')
def test_register_reserved_order_rsi_down(mock_insert, mock_get_price, mock_select_stock, mock_select_account, mock_show_menu, mock_ask):
    """RSI(RSI_DOWN) 조건의 예약 매수 등록 흐름 테스트"""
    mock_select_account.return_value = ("12345678", "01", "실전투자")
    mock_show_menu.side_effect = ["1", "8"] # 1: 예약 매수, 8: RSI
    mock_select_stock.return_value = ("005930", "삼성전자", False)
    mock_get_price.return_value = 80000.0
    # Prompt 입력: 목표RSI(30), 발동방향(2:이하하락), 주문단가(0:시장가), 주문수량(10), 유효기간(4:무기한), 최종확인(y)
    mock_ask.side_effect = ["30", "2", "0", "10", "4", "y"]
    
    with patch('modules.trading.api.send_telegram_message'):
        trading.register_reserved_order()
        
    mock_insert.assert_called_once()
    assert mock_insert.call_args[1]['condition_type'] == "RSI_DOWN"
    assert mock_insert.call_args[1]['target_price'] == 30.0

@patch('modules.trading.Prompt.ask')
@patch('modules.trading.utils.show_menu')
@patch('modules.trading.select_account')
@patch('modules.trading.select_stock_from_balance')
@patch('modules.trading.api.get_current_price')
@patch('modules.trading.db_manager.db.insert_reserved_order')
def test_register_reserved_order_ema_up(mock_insert, mock_get_price, mock_select_bal, mock_select_account, mock_show_menu, mock_ask):
    """EMA(EMA_UP) 조건의 예약 매도 등록 흐름 테스트"""
    mock_select_account.return_value = ("12345678", "01", "실전투자")
    mock_show_menu.side_effect = ["2", "9"] # 2: 예약 매도, 9: EMA
    mock_select_bal.return_value = ("005930", "삼성전자", False, None, {"qty": 100, "buy_price": 75000.0})
    mock_get_price.return_value = 80000.0
    # Prompt 입력: 목표EMA(60), 발동방향(1:상향돌파), 주문단가(0:시장가), 주문수량(50), 유효기간(1:당일), 최종확인(y)
    mock_ask.side_effect = ["60", "1", "0", "50", "1", "y"]
    
    with patch('modules.trading.api.send_telegram_message'):
        trading.register_reserved_order()
        
    mock_insert.assert_called_once()
    assert mock_insert.call_args[1]['condition_type'] == "EMA_UP"
    assert mock_insert.call_args[1]['target_price'] == 60.0

# -------------------------------------------------------------------
# 4. ReservedOrderMonitor 발동 로직 테스트
# -------------------------------------------------------------------
from modules.reserved_order_monitor import ReservedOrderMonitor
import pandas as pd

@patch('modules.reserved_order_monitor.db_manager.db.get_pending_reserved_orders')
@patch('modules.reserved_order_monitor.api.get_current_price')
@patch('modules.reserved_order_monitor.api.get_chart_data')
@patch('modules.reserved_order_monitor.indicators.calculate_indicators')
@patch('modules.reserved_order_monitor.ReservedOrderMonitor._execute_order')
def test_monitor_trigger_ema_rsi_score(mock_execute, mock_calc, mock_chart, mock_get_price, mock_get_orders):
    """EMA, RSI, SCORE 기반 예약 매매 발동 모니터링 테스트"""
    monitor = ReservedOrderMonitor()
    
    mock_get_orders.return_value = [
        {"id": 1, "code": "005930", "name": "삼성전자", "condition_type": "EMA_UP", "target_price": 60, "order_type": "buy", "market": "KR", "expire_dt": "20991231"},
        {"id": 2, "code": "000660", "name": "SK하이닉스", "condition_type": "RSI_DOWN", "target_price": 30, "order_type": "buy", "market": "KR", "expire_dt": "20991231"},
        {"id": 3, "code": "035420", "name": "NAVER", "condition_type": "SCORE_UP", "target_price": 7.5, "order_type": "buy", "market": "KR", "expire_dt": "20991231"},
    ]
    
    mock_get_price.side_effect = lambda code, is_ovs: {"005930": 85000.0, "000660": 150000.0, "035420": 200000.0}.get(code, 0.0)
    mock_chart.return_value = pd.DataFrame({'close': [100.0, 200.0]})
    mock_calc.return_value = {'ema_60': 80000.0, 'rsi': 25.0}
    
    with patch('modules.analysis.calculate_score', return_value=(8.0, {})):
        with patch('modules.reserved_order_monitor.datetime') as mock_dt:
            mock_dt.now.return_value.strftime.side_effect = lambda fmt: "1200" if fmt == "%H%M" else "20240101"
            monitor._check_orders()
        
    assert mock_execute.call_count == 3

@patch('modules.reserved_order_monitor.db_manager.db.get_pending_reserved_orders')
@patch('modules.reserved_order_monitor.api.get_current_price')
@patch('modules.reserved_order_monitor.ReservedOrderMonitor._execute_order')
def test_monitor_trigger_price_based(mock_execute, mock_get_price, mock_get_orders):
    """가격(목표가/트레일링) 기반 예약 매매 발동 모니터링 테스트"""
    monitor = ReservedOrderMonitor()
    
    mock_get_orders.return_value = [
        {"id": 4, "code": "005930", "name": "삼성전자", "condition_type": "STOP", "target_price": 70000, "order_type": "sell", "market": "KR", "expire_dt": "20991231"},
        {"id": 5, "code": "000660", "name": "SK하이닉스", "condition_type": "BREAKOUT", "target_price": 160000, "order_type": "buy", "market": "KR", "expire_dt": "20991231"},
        {"id": 6, "code": "035420", "name": "NAVER", "condition_type": "TRAILING_SELL", "target_price": 5.0, "order_type": "sell", "market": "KR", "expire_dt": "20991231", "highest_price": 200000.0},
    ]
    
    mock_get_price.side_effect = lambda code, is_ovs: {"005930": 69000.0, "000660": 165000.0, "035420": 185000.0}.get(code, 0.0)
    
    with patch('modules.reserved_order_monitor.datetime') as mock_dt:
        mock_dt.now.return_value.strftime.side_effect = lambda fmt: "1200" if fmt == "%H%M" else "20240101"
        monitor._check_orders()
        
    assert mock_execute.call_count == 3

@patch('modules.reserved_order_monitor.db_manager.db.get_pending_reserved_orders')
@patch('modules.reserved_order_monitor.ReservedOrderMonitor._execute_order')
def test_monitor_trigger_time_condition(mock_execute, mock_get_orders):
    """TIME 조건 발동 모니터링 테스트"""
    monitor = ReservedOrderMonitor()
    mock_get_orders.return_value = [
        {"id": 1, "code": "005930", "name": "삼성전자", "condition_type": "TIME", "target_time": "1520", "order_type": "buy", "market": "KR", "expire_dt": "20991231", "target_price": 0.0},
        {"id": 2, "code": "000660", "name": "SK하이닉스", "condition_type": "TIME", "target_time": "202401010900", "order_type": "sell", "market": "KR", "expire_dt": "20991231", "target_price": 0.0},
    ]
    
    with patch('modules.reserved_order_monitor.datetime') as mock_dt:
        # 첫 번째 1520 도달, 두 번째 202401010900 도달
        mock_dt.now.return_value.strftime.side_effect = lambda fmt: "1525" if fmt == "%H%M" else ("202401010905" if fmt == "%Y%m%d%H%M" else "20240101")
        monitor._check_orders()
        
    assert mock_execute.call_count == 2

@patch('modules.reserved_order_monitor.db_manager.db.get_pending_reserved_orders')
@patch('modules.reserved_order_monitor.api.get_current_price')
@patch('modules.reserved_order_monitor.ReservedOrderMonitor._execute_order')
def test_monitor_trigger_limit_condition(mock_execute, mock_get_price, mock_get_orders):
    """LIMIT (지정가) 매수/매도 발동 모니터링 테스트"""
    monitor = ReservedOrderMonitor()
    mock_get_orders.return_value = [
        {"id": 1, "code": "005930", "name": "삼성전자", "condition_type": "LIMIT", "target_price": 80000, "order_type": "buy", "market": "KR", "expire_dt": "20991231"},
        {"id": 2, "code": "000660", "name": "SK하이닉스", "condition_type": "LIMIT", "target_price": 150000, "order_type": "sell", "market": "KR", "expire_dt": "20991231"},
    ]
    
    # 매수는 목표가 80000 이하여야 하므로 79000에서 발동
    # 매도는 목표가 150000 이상이어야 하므로 151000에서 발동
    mock_get_price.side_effect = lambda code, is_ovs: {"005930": 79000.0, "000660": 151000.0}.get(code, 0.0)
    
    with patch('modules.reserved_order_monitor.datetime') as mock_dt:
        mock_dt.now.return_value.strftime.side_effect = lambda fmt: "1200" if fmt == "%H%M" else "20240101"
        monitor._check_orders()
        
    assert mock_execute.call_count == 2

@patch('modules.reserved_order_monitor.db_manager.db.update_reserved_order_lowest')
@patch('modules.reserved_order_monitor.db_manager.db.get_pending_reserved_orders')
@patch('modules.reserved_order_monitor.api.get_current_price')
@patch('modules.reserved_order_monitor.ReservedOrderMonitor._execute_order')
def test_monitor_trigger_trailing_buy(mock_execute, mock_get_price, mock_get_orders, mock_update_lowest):
    """TRAILING_BUY 조건 발동 모니터링 테스트"""
    monitor = ReservedOrderMonitor()
    mock_get_orders.return_value = [
        {"id": 1, "code": "005930", "name": "삼성전자", "condition_type": "TRAILING_BUY", "target_price": 3.0, "order_type": "buy", "market": "KR", "expire_dt": "20991231", "lowest_price": 70000.0},
    ]
    
    # 목표 반등률: 3.0%
    # lowest_price가 70000.0일 때, 3% 상승하면 72100.0
    mock_get_price.side_effect = lambda code, is_ovs: {"005930": 72500.0}.get(code, 0.0)
    
    with patch('modules.reserved_order_monitor.datetime') as mock_dt:
        mock_dt.now.return_value.strftime.side_effect = lambda fmt: "1200" if fmt == "%H%M" else "20240101"
        monitor._check_orders()
        
    assert mock_execute.call_count == 1

@patch('modules.reserved_order_monitor.db_manager.db.get_pending_reserved_orders')
@patch('modules.reserved_order_monitor.api.get_current_price')
@patch('modules.reserved_order_monitor.api.get_chart_data')
@patch('modules.reserved_order_monitor.indicators.calculate_indicators')
@patch('modules.reserved_order_monitor.ReservedOrderMonitor._execute_order')
def test_monitor_trigger_down_conditions(mock_execute, mock_calc, mock_chart, mock_get_price, mock_get_orders):
    """SCORE_DOWN, RSI_UP, EMA_DOWN 발동 모니터링 테스트"""
    monitor = ReservedOrderMonitor()
    
    mock_get_orders.return_value = [
        {"id": 1, "code": "005930", "name": "삼성전자", "condition_type": "EMA_DOWN", "target_price": 20, "order_type": "sell", "market": "KR", "expire_dt": "20991231"},
        {"id": 2, "code": "000660", "name": "SK하이닉스", "condition_type": "RSI_UP", "target_price": 70, "order_type": "sell", "market": "KR", "expire_dt": "20991231"},
        {"id": 3, "code": "035420", "name": "NAVER", "condition_type": "SCORE_DOWN", "target_price": 4.0, "order_type": "sell", "market": "KR", "expire_dt": "20991231"},
    ]
    
    # EMA_DOWN: ema_20보다 현재가가 낮아야 함
    # RSI_UP: rsi가 70 이상이어야 함
    # SCORE_DOWN: score가 4.0 이하여야 함
    mock_get_price.side_effect = lambda code, is_ovs: {"005930": 79000.0, "000660": 150000.0, "035420": 200000.0}.get(code, 0.0)
    mock_chart.return_value = pd.DataFrame({'close': [100.0, 200.0]})
    mock_calc.return_value = {'ema_20': 80000.0, 'rsi': 75.0}
    
    with patch('modules.analysis.calculate_score', return_value=(3.5, {})):
        with patch('modules.reserved_order_monitor.datetime') as mock_dt:
            mock_dt.now.return_value.strftime.side_effect = lambda fmt: "1200" if fmt == "%H%M" else "20240101"
            monitor._check_orders()
        
    assert mock_execute.call_count == 3


# -------------------------------------------------------------------
# 3. 텔레그램 봇 예약 주문 명령어 테스트
# -------------------------------------------------------------------
@patch('modules.telegram_bot.db_manager.db.get_pending_reserved_orders')
def test_telegram_cmd_reserves_summary(mock_get_orders):
    """/reserves 명령어 - 대기 주문 요약 문자열 확인"""
    commander = TelegramCommander()
    mock_get_orders.return_value = [
        {
            "id": 100, "cano": "12345678", "acnt": "01", "market": "KR", 
            "order_type": "buy", "code": "005930", "name": "삼성전자",
            "qty": 10, "order_price": 79000.0, "condition_type": "LIMIT",
            "target_price": 79000.0, "target_time": "", "expire_dt": "20991231"
        }
    ]
    result = commander._cmd_reserves([])
    assert "삼성전자(005930)" in result
    assert "🔴 매수 | 10주 @ 79,000원" in result
    assert "ID: 100" in result

@patch('modules.telegram_bot.db_manager.db.get_pending_reserved_orders')
@patch('modules.telegram_bot.db_manager.db.update_reserved_order_status')
def test_telegram_cmd_reserves_delete(mock_update, mock_get_orders):
    """/reserves d [ID] 명령어 - 주문 삭제 처리 확인"""
    commander = TelegramCommander()
    mock_get_orders.return_value = [{"id": 100, "order_type": "buy", "name": "삼성전자", "code": "005930"}]
    result = commander._cmd_reserves(["d", "100"])
    mock_update.assert_called_once_with(100, 'CANCELED')
    assert "예약 취소 완료" in result

@patch('modules.telegram_bot.db_manager.db.get_pending_reserved_orders')
@patch('modules.telegram_bot.db_manager.db.update_reserved_order_status')
def test_telegram_cmd_reserves_delete_all(mock_update, mock_get_orders):
    """/reserves d 0 명령어 - 일괄 주문 취소(All) 확인"""
    commander = TelegramCommander()
    mock_get_orders.return_value = [
        {"id": 1, "order_type": "buy", "name": "삼성전자", "code": "005930"},
        {"id": 2, "order_type": "sell", "name": "SK하이닉스", "code": "000660"}
    ]
    result = commander._cmd_reserves(["d", "0"])
    assert mock_update.call_count == 2
    mock_update.assert_any_call(1, 'CANCELED')
    mock_update.assert_any_call(2, 'CANCELED')
    assert "예약 취소 완료" in result

def test_telegram_cmd_reserves_invalid():
    """/reserves 명령어 - 잘못된 하위 명령어 입력 시 안내 메시지 확인"""
    commander = TelegramCommander()
    result = commander._cmd_reserves(["x", "100"])
    assert "알 수 없는 명령어" in result

def test_telegram_cmd_reserves_delete_no_args():
    """/reserves d 명령어 - 취소할 ID를 명시하지 않았을 때 안내 메시지 확인"""
    commander = TelegramCommander()
    result = commander._cmd_reserves(["d"])
    assert "사용법:" in result
    assert "예약주문ID" in result