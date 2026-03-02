import pytest
from unittest.mock import patch, MagicMock
from modules.auto_trade import AutoTrader
import config

@pytest.fixture
def trader():
    return AutoTrader()

@patch('modules.auto_trade.api.get_domestic_balance')
@patch('modules.auto_trade.api.get_deposit_balance')
def test_get_total_estimated_asset(mock_deposit, mock_balance, trader):
    """총 추정 자산 계산 테스트"""
    mock_balance.return_value = ([], [{'scts_evlu_amt': '500000', 'dnca_tot_amt': '500000'}])
    # 모의투자 모드 가정
    config.session.is_simulation = True
    
    asset = trader._get_total_estimated_asset()
    # 500000 (주식) + 500000 (예수금) = 1000000
    # (모의투자는 dnca_tot_amt가 예수금으로 사용됨)
    assert asset == 1000000

def test_monitor_account_status(trader):
    """계좌 상태 모니터링 로깅 테스트"""
    holdings = [{'prdt_name': 'Samsung', 'pdno': '005930', 'hldg_qty': '10', 'pchs_avg_pric': '50000', 'prpr': '60000', 'evlu_amt': '600000', 'evlu_pfls_amt': '100000', 'evlu_pfls_rt': '20.0'}]
    summary = [{'scts_evlu_amt': '600000', 'dnca_tot_amt': '400000'}]
    deposit_res = {'d2_deposit': 400000, 'foreign_deposit': 0}
    
    with patch.object(trader, 'log') as mock_log:
        trader._monitor_account_status(holdings, summary, deposit_res)
        assert mock_log.called