import pytest
from unittest.mock import patch, MagicMock
from modules.auto_trade import AutoTrader, ConclusionMonitor
import config
import time

@pytest.fixture
def trader():
    t = AutoTrader()
    t.is_running = False
    return t

@patch('modules.auto_trade.api.send_telegram_message')
@patch('modules.auto_trade.api.get_domestic_balance')
@patch('modules.auto_trade.api.get_deposit_balance')
@patch('threading.Thread')
def test_autotrader_start_stop(mock_thread_cls, mock_deposit, mock_balance, mock_telegram, trader):
    """AutoTrader 시작 및 중지 로직 테스트"""
    # Mock 설정
    mock_balance.return_value = ([], [{'scts_evlu_amt': '1000000'}])
    mock_deposit.return_value = {'d2_deposit': 1000000, 'foreign_deposit': 0}
    
    # Thread Mock (실제 스레드 생성 방지 및 join 호출 검증용)
    mock_thread_instance = MagicMock()
    mock_thread_cls.return_value = mock_thread_instance
    
    # Start
    trader.start(interactive=False)
    assert trader.is_running is True
    assert trader.initial_asset > 0
    mock_thread_instance.start.assert_called_once()
    
    # Stop
    trader.stop(use_status=False)
    assert trader.is_running is False
    mock_thread_instance.join.assert_called()

@patch('modules.auto_trade.api.get_domestic_balance')
@patch('modules.auto_trade.api.get_deposit_balance')
def test_print_status(mock_deposit, mock_balance, trader):
    """상태 출력 함수 테스트"""
    mock_balance.return_value = (
        [{'prdt_name': 'Samsung', 'pdno': '005930', 'hldg_qty': '10', 'pchs_avg_pric': '50000', 'prpr': '60000', 'evlu_amt': '600000', 'evlu_pfls_amt': '100000', 'evlu_pfls_rt': '20.0'}],
        [{'scts_evlu_amt': '600000', 'dnca_tot_amt': '400000'}]
    )
    mock_deposit.return_value = {'d2_deposit': 400000, 'foreign_deposit': 0}
    
    trader.initial_asset = 1000000
    
    with patch('config.console.print') as mock_print:
        trader.print_status()
        # 호출 여부만 확인 (내용 검증은 복잡함)
        assert mock_print.called

def test_conclusion_monitor_loop():
    """체결 감시자 루프 테스트"""
    monitor = ConclusionMonitor()
    monitor.is_running = True
    
    # _check_conclusions를 Mocking하여 루프가 한 번 돌고 종료되도록 유도
    with patch.object(monitor, '_check_conclusions', return_value=(False, False)) as mock_check:
        # _is_market_open을 True로 고정하여 대기 로직 회피
        with patch.object(monitor, '_is_market_open', return_value=True):
            # time.sleep (초기 지연) 모킹
            with patch('time.sleep'):
                # event.wait를 모킹하여 루프 탈출 유도 (첫 번째 호출은 정상, 두 번째 호출에서 예외 발생)
                with patch.object(monitor.event, 'wait', side_effect=[None, Exception("Stop Loop")]):
                    try:
                        monitor._run_loop()
                    except Exception:
                        pass
                    
                    assert mock_check.called