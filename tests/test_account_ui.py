import pytest
from unittest.mock import patch, MagicMock
from modules import account
import config

import api

@patch('api.get_domestic_balance')
def test_display_balance_details_domestic(mock_balance):
    """국내 잔고 상세 출력 테스트"""
    mock_balance.return_value = (
        [{'prdt_name': 'Samsung', 'pdno': '005930', 'hldg_qty': '10', 'pchs_avg_pric': '50000', 'prpr': '60000', 'evlu_amt': '600000', 'evlu_pfls_amt': '100000', 'evlu_pfls_rt': '20.0'}],
        [{'scts_evlu_amt': '600000', 'tot_evlu_amt': '1000000', 'evlu_pfls_smtl_amt': '100000'}]
    )

    with patch('config.console.print') as mock_print:
        account._display_balance_details("12345678", "01")
        # 테이블 출력 확인
        assert mock_print.call_count > 0

@patch('api.get_overseas_balance')
@patch('api.get_domestic_balance')
def test_display_balance_details_overseas(mock_dom, mock_ovs):
    """해외 잔고 상세 출력 테스트"""
    mock_dom.return_value = ([], [])
    mock_ovs.return_value = [
        {'ovrs_item_name': 'Apple', 'ovrs_pdno': 'AAPL', 'ovrs_cblc_qty': '10', 'pchs_avg_pric': '150.0', 'ovrs_now_pric': '160.0', 'frcr_evlu_pfls_amt': '100.0', 'evlu_pfls_rt': '6.6', '_exchange': 'NASD'}
    ]
    
    with patch('config.console.print') as mock_print:
        account._display_balance_details("12345678", "01")
        assert mock_print.call_count > 0

@patch('modules.account._display_balance_details')
def test_get_account_balance_ui(mock_display):
    """계좌 잔고 조회 메뉴 테스트"""
    config.session.cano = "12345678"
    config.session.acnt_prdt_cd = "01"
    config.session.auto_cano = "12345678"
    config.session.auto_acnt_prdt_cd = "01"

    with patch('config.console.print'):
        account.get_account_balance()
        mock_display.assert_called_with("12345678", "01")
