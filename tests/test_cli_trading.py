import pytest
from unittest.mock import patch, MagicMock
from modules import trading
import config

@patch('rich.prompt.Prompt.ask')
@patch('modules.trading.send_order')
@patch('modules.trading.modify_order')
def test_stock_order_menu(mock_modify, mock_send, mock_ask):
    """주문 관리 메뉴 테스트"""
    # 1(매수) 선택
    mock_ask.side_effect = ["1"]
    
    trading.stock_order_menu()
    
    mock_send.assert_called_once_with("buy")

@patch('rich.prompt.Prompt.ask')
@patch('modules.trading.api.place_order')
@patch('modules.trading.api.fetch_buyable_quantity', return_value=100)
@patch('modules.trading.api.get_current_price', return_value=50000)
def test_send_buy_order_flow(mock_price, mock_qty, mock_place, mock_ask):
    """매수 주문 흐름 테스트"""
    # 시나리오: 종목선택(5:직접) -> 코드(005930) -> 수량(10) -> 단가(0:시장가) -> 확인(y)
    # select_account는 테스트 환경(모의투자)에서 입력을 받지 않음
    mock_ask.side_effect = ["5", "005930", "10", "0", "y"]
    
    # API 응답 Mock
    mock_place.return_value = {'rt_cd': '0', 'output': {'ODNO': '12345'}}
    
    with patch('modules.trading.api.get_stock_name_by_code', return_value="삼성전자"):
        trading.send_order("buy")
        
    mock_place.assert_called()
    args, _ = mock_place.call_args
    assert args[1] == "buy" # action
    assert args[2] == "005930" # code
    assert args[3] == "10" # qty

@patch('rich.prompt.Prompt.ask')
@patch('modules.trading.api.revise_cancel_order')
@patch('modules.trading.show_open_orders')
def test_modify_order_flow(mock_show, mock_revise, mock_ask):
    """주문 정정 흐름 테스트"""
    # 미체결 내역 Mock
    mock_show.return_value = [{
        'odno': '12345', 'pdno': '005930', 'prdt_name': '삼성전자', 
        'rmn_qty': '10', '_origin': 'KR', 'sll_buy_dvsn_cd': '02'
    }]
    
    # 시나리오: 주문선택(1) -> 정정(1) -> 수량(5) -> 단가(55000) -> 확인(y)
    mock_ask.side_effect = ["1", "1", "5", "55000", "y"]
    
    mock_revise.return_value = {'rt_cd': '0', 'output': {'ODNO': '67890'}}
    
    trading.modify_order()
    
    mock_revise.assert_called()
    args, _ = mock_revise.call_args
    assert args[2] == "12345" # org_no
    assert args[4] == 5 # qty (정수형으로 전달됨)
    assert args[5] == "55000" # price