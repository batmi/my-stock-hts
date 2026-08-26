import pytest
from unittest.mock import patch, MagicMock
from modules import account
import config
import api


@patch('modules.account.fetch_overseas_balance') # Fix: Corrected the module path
@patch('api.get_domestic_balance') # Fix: Corrected the module path
@patch('api.get_deposit_balance')
def test_get_asset_status_data(mock_deposit, mock_balance, mock_overseas):
    """자산 현황 데이터 집계 테스트"""
    # Mock Balance (보유 종목 및 평가금)
    try:
        mock_balance.return_value = ( # Fixed: Added parenthesis
                [{'pdno': '005930', 'prdt_name': 'Samsung', 'hldg_qty': '10', 'pchs_amt': '500000', 'pchs_avg_pric': '50000', 'evlu_amt': '600000', 'evlu_pfls_amt': '100000', 'evlu_pfls_rt': '20.0'}],
            [{'scts_evlu_amt': '600000', 'tot_evlu_amt': '1600000', 'dnca_tot_amt': '1000000'}]
        )
    
        # Mock Deposit (예수금)
        mock_deposit.return_value = {'deposit': 1000000, 'foreign_deposit': 0, 'd2_deposit': 1000000, 'withdraw': 1000000}
    
        # Mock Overseas Balance
        mock_overseas.return_value = []
    
        # Mock config
    
        data = account.get_asset_status_data("12345678", "01")
    
        assert data['sec_eval'] == 600000
        assert data['dep_dom'] == 1000000
        # 총 자산 = 예수금 + 주식평가금

        assert data['tot_asset'] == 1600000
        assert data['sec_pl'] == 100000
    except Exception as e:
        print(f"Error in test_get_asset_status_data: {e}")
        assert False

@patch('api.get_today_profit_summary') # Fix: Corrected the module path
def test_fetch_today_profit_summary(mock_api):
    """금일 수익 현황 조회 테스트"""
    try:
        mock_api.return_value =  { # Fixed: Added parenthesis
            'rt_cd': '0',
            'output2': [{'thdt_buy_amt': '1000', 'thdt_sll_amt': '2000', 'rlzt_pfls': '500'}]
        }

        summary = account.fetch_today_profit_summary()
        assert summary['buy_amt'] == 1000
        assert summary['sell_amt'] == 2000
        assert summary['realized_pl'] == 500
    except Exception as e:
        print(f"Error in test_fetch_today_profit_summary: {e}")
        assert False
 
