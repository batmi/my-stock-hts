import pytest
from unittest.mock import patch, MagicMock
from modules import trading
import config

@patch('rich.prompt.Prompt.ask')
def test_send_order_cancel(mock_ask):
    """주문 전송 취소 테스트"""
    # 계좌 선택(1) -> 종목 선택(취소 q)
    mock_ask.side_effect = ["1", "q"]
    
    with patch('config.console.print') as mock_print:
        trading.send_order("buy")

@patch('rich.prompt.Prompt.ask')
@patch('modules.trading.api.place_order')
def test_send_order_api_fail(mock_place, mock_ask):
    """주문 전송 API 실패 테스트"""
    # 계좌(1) -> 직접입력(5) -> 코드(005930) -> 수량(1) -> 단가(0) -> 확인(y)
    mock_ask.side_effect = ["5", "005930", "1", "0", "y"]
    
    mock_place.return_value = {'rt_cd': '1', 'msg1': '주문 전송 실패'}
    
    with patch('modules.trading.api.get_stock_name_by_code', return_value="삼성전자"):
        with patch('modules.trading.api.get_current_price', return_value=50000):
            with patch('modules.trading.api.fetch_buyable_quantity', return_value=10):
                with patch('config.console.print') as mock_print:
                    trading.send_order("buy")
                    assert any("주문 실패" in str(c) for c in mock_print.call_args_list)

@patch('rich.prompt.Prompt.ask')
def test_modify_order_no_orders(mock_ask):
    """미체결 내역 없을 때 정정 시도 테스트"""
    with patch('modules.trading.show_open_orders', return_value=[]):
        with patch('config.console.print') as mock_print:
            trading.modify_order()