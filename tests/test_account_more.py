import pytest
from unittest.mock import patch
from modules import account

import api

@patch('api.get_domestic_balance')
def test_fetch_domestic_balance(mock_get):
    """국내 잔고 조회 래퍼 테스트"""
    mock_get.return_value = ([{'pdno': '005930', 'hldg_qty': '10'}], [{'tot_evlu_amt': '1000000'}])
    
    holdings, summary = account.fetch_domestic_balance()
    assert len(holdings) == 1
    assert summary['tot_evlu_amt'] == '1000000'

@patch('api.get_overseas_balance')
def test_fetch_overseas_balance(mock_get):
    """해외 잔고 조회 래퍼 테스트"""
    mock_get.return_value = [{'ovrs_pdno': 'AAPL', 'ovrs_cblc_qty': '5'}]
    
    holdings = account.fetch_overseas_balance()
    assert len(holdings) == 1
    assert holdings[0]['ovrs_pdno'] == 'AAPL'