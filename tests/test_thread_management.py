import pytest
from unittest.mock import patch, MagicMock
import threading
import sys
import os

# 프로젝트 루트 경로를 sys.path에 추가하여 모듈 import 가능하게 함
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from modules.telegram_bot import TelegramCommander
from modules.auto_trade import AutoTrader, ConclusionMonitor

def test_telegram_cmd_stop_avoids_ui_deadlock():
    """텔레그램 /stop 명령어가 UI 렌더링(progress) 없이 백그라운드에서 안전하게 호출되는지 테스트"""
    bot = TelegramCommander()
    bot.trader = MagicMock()
    bot.trader.is_running = True

    response = bot._cmd_stop([])

    bot.trader.stop.assert_called_once_with(use_status=False)
    assert "중단 요청을 처리했습니다" in response

@patch('time.sleep', return_value=None)
def test_telegram_cmd_restart_avoids_ui_deadlock(mock_sleep):
    """텔레그램 /restart 명령어가 UI 렌더링 없이 안전하게 호출되는지 테스트"""
    bot = TelegramCommander()
    bot.trader = MagicMock()
    bot.trader.is_running = True

    response = bot._cmd_restart([])

    bot.trader.stop.assert_called_once_with(use_status=False)
    bot.trader.start.assert_called_once_with(interactive=False)
    assert "재시작했습니다" in response

def test_autotrader_zombie_killer():
    """AutoTrader의 스레드가 덮어씌워졌을 때 예전 스레드가 스스로 종료하는지(Zombie 방지) 테스트"""
    trader = AutoTrader()
    trader.is_running = True
    
    dummy_thread_1 = MagicMock()
    dummy_thread_2 = MagicMock()
    
    # 현재 실행 중인 스레드를 dummy_thread_1로 모킹
    with patch('threading.current_thread', return_value=dummy_thread_1):
        # 누군가 start()를 연타하여 최신 스레드가 dummy_thread_2로 덮어씌워진 상황 시뮬레이션
        trader.thread = dummy_thread_2
        
        # _run_loop 내부 while 조건: self.is_running and self.thread is my_thread
        # self.thread(dummy_thread_2) is not my_thread(dummy_thread_1) 이므로
        # 무한 루프에 진입하지 않고 즉시 리턴(종료)되어야 테스트 통과
        with patch('core.utils.AccountContext'):
            trader._run_loop() 

def test_conclusion_monitor_zombie_killer():
    """ConclusionMonitor의 스레드가 덮어씌워졌을 때 예전 스레드가 스스로 종료하는지 테스트"""
    monitor = ConclusionMonitor()
    monitor.is_running = True
    
    dummy_thread_1 = MagicMock()
    dummy_thread_2 = MagicMock()
    
    with patch('threading.current_thread', return_value=dummy_thread_1):
        monitor.thread = dummy_thread_2
        with patch('time.sleep'):
            # 무한 루프에 빠지지 않고 바로 리턴되면 성공
            monitor._run_loop()