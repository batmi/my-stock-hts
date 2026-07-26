import pytest
import threading
from unittest.mock import patch, MagicMock
from modules.auto_trade import AutoTrader, ConclusionMonitor
import config
import time

@pytest.fixture
def trader():
    # 싱글톤 인스턴스의 상태를 매 테스트마다 초기화
    t = AutoTrader()
    t.is_running = True
    t.thread = threading.current_thread()
    t.consecutive_errors = 0
    t.logs.clear()
    t.initial_holdings = None
    return t

@patch('modules.auto_trade.api.get_domestic_balance')
@patch('modules.auto_trade.api.get_deposit_balance')
@patch('modules.auto_trade.api.send_telegram_message')
def test_run_loop_market_closed(mock_tg, mock_deposit, mock_balance, trader):
    """장 마감 시 루프 동작 테스트"""
    # 장 마감 상태 모킹 (휴장일이면 "휴장일" 문구로 갈라지므로 거래일로 고정)
    with patch('api.is_holiday_today', return_value=False), \
         patch.object(trader, 'is_market_open', return_value=False):
        # 한 번만 실행하고 종료되도록 설정
        with patch('time.sleep', side_effect=InterruptedError):
            try:
                trader._run_loop()
            except InterruptedError:
                pass
            
    # 장 마감 로그 확인
    assert any("장 마감" in log for log in trader.logs)

@patch('modules.auto_trade.api.get_domestic_balance')
@patch('modules.auto_trade.api.get_deposit_balance')
@patch.object(AutoTrader, '_update_market_indices_status')
def test_run_loop_exception_handling(mock_update_indices, mock_deposit, mock_balance, trader):
    """루프 내 예외 발생 시 처리 테스트"""
    # 예외 발생 설정
    mock_balance.side_effect = Exception("API Error")
    
    with patch.object(trader, 'is_market_open', return_value=True):
        # time.sleep 호출 시 예외를 발생시켜 루프를 한 번만 실행하고 탈출
        with patch('time.sleep', side_effect=InterruptedError):
            try:
                trader._run_loop()
            except InterruptedError:
                pass
                
    # 에러 카운트 증가 확인
    assert trader.consecutive_errors > 0
    assert any("에러 발생" in log for log in trader.logs)

def test_conclusion_monitor_error_handling():
    """체결 감시자 예외 처리 테스트"""
    monitor = ConclusionMonitor()
    monitor.is_running = True
    monitor.thread = threading.current_thread()
    
    with patch.object(monitor, '_check_conclusions', side_effect=Exception("Monitor Error")):
        with patch.object(monitor, '_is_market_open', return_value=True):
            with patch('time.sleep'):
                with patch.object(monitor.event, 'wait', side_effect=[None, Exception("Stop")]):
                    try:
                        monitor._run_loop()
                    except Exception:
                        pass
                        
    assert monitor.consecutive_errors > 0