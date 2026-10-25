import pytest
from unittest.mock import patch, MagicMock
from modules.auto_trade import AutoTrader
import config

@pytest.fixture
def trader():
    return AutoTrader()

def test_monitor_account_status(trader):
    """계좌 상태 모니터링 로깅 테스트"""
    holdings = [{'prdt_name': 'Samsung', 'pdno': '005930', 'hldg_qty': '10', 'pchs_avg_pric': '50000', 'prpr': '60000', 'evlu_amt': '600000', 'evlu_pfls_amt': '100000', 'evlu_pfls_rt': '20.0'}]
    summary = [{'scts_evlu_amt': '600000', 'dnca_tot_amt': '400000'}]
    deposit_res = {'d2_deposit': 400000, 'foreign_deposit': 0}
    
    with patch.object(trader, 'log') as mock_log:
        trader._monitor_account_status(holdings, summary, deposit_res)
        assert mock_log.called