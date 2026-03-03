import pytest
from unittest.mock import patch, MagicMock
from modules import trading
import config

@patch('rich.prompt.Prompt.ask')
def test_select_account_main(mock_ask):
    """메인 계좌 선택 테스트"""
    config.session.is_simulation = False
    config.session.cano = "1111"
    config.session.acnt_prdt_cd = "01"
    config.session.auto_cano = "2222"
    
    # 선택 없이 기본값(1) 사용
    mock_ask.return_value = "1"
    
    cano, acnt, label = trading.select_account()
    assert cano == "1111"
    assert "실전" in label

@patch('rich.prompt.Prompt.ask')
def test_select_account_auto(mock_ask):
    """자동매매 계좌 선택 테스트"""
    config.session.is_simulation = False
    config.session.cano = "1111"
    config.session.auto_cano = "2222"
    config.session.auto_acnt_prdt_cd = "01"
    
    mock_ask.return_value = "2"
    
    cano, acnt, label = trading.select_account()
    assert cano == "2222"
    assert label == "자동투자"

@patch('rich.prompt.Prompt.ask')
@patch('modules.account.fetch_domestic_balance')
def test_select_stock_from_balance_domestic(mock_balance, mock_ask):
    """국내 잔고에서 매도 종목 선택 테스트"""
    # 1(국내) -> 1(첫번째 종목)
    mock_ask.side_effect = ["1", "1"]
    
    mock_balance.return_value = ([{
        'pdno': '005930', 'prdt_name': 'Samsung', 'hldg_qty': '10', 
        'pchs_avg_pric': '50000', 'prpr': '60000', 'evlu_amt': '600000', 
        'evlu_pfls_amt': '100000', 'evlu_pfls_rt': '20.0'
    }], None)
    
    with patch('config.console.print'): # 테이블 출력 억제
        code, name, is_ovs, excd, info = trading.select_stock_from_balance()
        
    assert code == "005930"
    assert name == "Samsung"
    assert is_ovs is False

@patch('rich.prompt.Prompt.ask')
@patch('modules.account.fetch_overseas_balance')
def test_select_stock_from_balance_overseas(mock_balance, mock_ask):
    """해외 잔고에서 매도 종목 선택 테스트"""
    # 2(해외) -> 1(첫번째 종목)
    mock_ask.side_effect = ["2", "1"]
    
    mock_balance.return_value = [{
        'ovrs_pdno': 'AAPL', 'ovrs_item_name': 'Apple', 'ovrs_cblc_qty': '5',
        'pchs_avg_pric': '150', 'frcr_evlu_pfls_amt': '50', 'evlu_pfls_rt': '6.6',
        'ovrs_now_pric': '160', '_exchange': 'NASD'
    }]
    
    with patch('config.console.print'):
        code, name, is_ovs, excd, info = trading.select_stock_from_balance()
        
    assert code == "AAPL"
    assert is_ovs is True
    assert excd == "NAS" # NASD -> NAS 매핑 확인

@patch('rich.prompt.Prompt.ask')
def test_order_menu_buy(mock_ask):
    """주문 메뉴 - 매수 선택 테스트"""
    mock_ask.return_value = "1"
    with patch('modules.trading.send_order') as mock_send:
        trading.order_menu()
        mock_send.assert_called_with("buy")

@patch('rich.prompt.Prompt.ask')
def test_order_menu_sell(mock_ask):
    """주문 메뉴 - 매도 선택 테스트"""
    mock_ask.return_value = "2"
    with patch('modules.trading.send_order') as mock_send:
        trading.order_menu()
        mock_send.assert_called_with("sell")