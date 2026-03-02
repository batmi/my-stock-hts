import pytest
from unittest.mock import patch, MagicMock
from modules import account
import config

@patch('rich.prompt.Prompt.ask')
@patch('modules.account.get_deposit_balance')
def test_asset_management_menu_deposit(mock_deposit, mock_ask):
    """자산 관리 메뉴 - 자산 조회 테스트"""
    mock_ask.return_value = "1"
    account.asset_management_menu()
    mock_deposit.assert_called_once()

@patch('rich.prompt.Prompt.ask')
@patch('modules.account.get_account_balance')
def test_asset_management_menu_balance(mock_balance, mock_ask):
    """자산 관리 메뉴 - 보유 잔고 테스트"""
    mock_ask.return_value = "2"
    account.asset_management_menu()
    mock_balance.assert_called_once()

@patch('rich.prompt.Prompt.ask')
@patch('modules.account.view_trade_history')
def test_asset_management_menu_history(mock_history, mock_ask):
    """자산 관리 메뉴 - 거래 내역 테스트"""
    mock_ask.return_value = "3"
    account.asset_management_menu()
    mock_history.assert_called_once()

@patch('modules.account.api.get_domestic_balance')
@patch('modules.account.api.get_overseas_balance')
@patch('modules.account.api.get_deposit_balance')
def test_display_asset_status(mock_deposit, mock_ovs_balance, mock_dom_balance):
    """자산 현황 출력 함수 테스트"""
    mock_dom_balance.return_value = (
        [{'pdno': '005930', 'hldg_qty': '10', 'pchs_amt': '500000', 'evlu_amt': '600000', 'evlu_pfls_amt': '100000'}],
        [{'scts_evlu_amt': '600000', 'dnca_tot_amt': '1000000', 'prvs_rcdl_excc_amt': '1000000'}]
    )
    mock_ovs_balance.return_value = []
    mock_deposit.return_value = {'deposit': 1000000, 'foreign_deposit': 0, 'd2_deposit': 1000000}
    
    with patch('config.console.print'):
        # _display_asset_status는 private이지만 테스트를 위해 직접 호출
        account._display_asset_status(config.session.cano, config.session.acnt_prdt_cd)
        
    mock_dom_balance.assert_called()
    mock_ovs_balance.assert_called()
    mock_deposit.assert_called()