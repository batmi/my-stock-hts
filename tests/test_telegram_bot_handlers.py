import pytest
from unittest.mock import patch, MagicMock
from modules.telegram_bot import TelegramCommander
import config

@pytest.fixture
def commander():
    return TelegramCommander()

def test_cmd_stocks(commander):
    """관심 종목 리스트 조회 명령어 테스트"""
    config.session.stock_data = {
        "stocks_kr": [{"name": "Samsung", "code": "005930"}],
        "etfs_kr": [], "stocks_us": [], "etfs_us": []
    }
    res = commander._cmd_stocks([])
    assert "Samsung" in res
    assert "005930" in res

def test_cmd_config(commander):
    """설정 조회 명령어 테스트"""
    res = commander._cmd_config([])
    assert "매매 전략 설정" in res
    assert "매수 조건" in res

@patch('modules.telegram_bot.db_manager.db.get_trades')
def test_cmd_history(mock_get_trades, commander):
    """체결 내역 조회 명령어 테스트"""
    mock_get_trades.return_value = [
        {'time': '2023-01-01 10:00:00', 'type': 'buy', 'name': 'Samsung', 'code': '005930', 'qty': 10, 'price': 50000, 'odno': '1', 'order_status': '체결'}
    ]
    res = commander._cmd_history(['5'])
    assert "거래 내역" in res
    assert "Samsung" in res

def test_cmd_log(commander):
    """로그 조회 명령어 테스트"""
    commander.trader.logs = ["[INFO] Test Log 1", "[INFO] Test Log 2"]
    res = commander._cmd_log(['2'])
    assert "Test Log 1" in res
    assert "Test Log 2" in res

@patch('modules.telegram_bot.account.get_asset_status_data')
@patch('modules.telegram_bot.api.get_deposit_balance')
def test_cmd_balance(mock_deposit, mock_get_asset, commander):
    """자산 현황 조회 명령어 테스트"""
    mock_get_asset.return_value = {
        'tot_asset': 1000000, 'dep_dom': 500000, 'dep_ovs': 0, 'withdraw': 500000,
        'sec_buy': 500000, 'sec_eval': 550000, 'sec_pl': 50000,
        'buy_today': 0, 'sell_today': 0, 'total_cost': 0, 'realized_pl': 0,
        'd1_dep': 500000, 'd2_dep': 500000
    }
    mock_deposit.return_value = {'order_possible': 1000000}
    res = commander._cmd_balance([])
    assert "1,000,000원" in res
    assert "500,000원" in res

@patch('modules.telegram_bot.api.get_domestic_balance')
def test_cmd_holdings(mock_balance, commander):
    """보유 종목 조회 명령어 테스트"""
    mock_balance.return_value = (
        [{'prdt_name': 'Samsung', 'pdno': '005930', 'hldg_qty': '10', 'prpr': '60000', 'pchs_avg_pric': '50000', 'evlu_amt': '600000', 'evlu_pfls_amt': '100000', 'evlu_pfls_rt': '20.0'}],
        [{'tot_evlu_amt': '600000', 'evlu_pfls_smtl_amt': '100000', 'pchs_amt_smtl': '500000'}]
    )
    res = commander._cmd_holdings([])
    assert "Samsung" in res
    assert "20.00%" in res

@patch('modules.telegram_bot.db_manager.db.get_trades')
def test_cmd_profit(mock_get_trades, commander):
    """실현 손익 조회 명령어 테스트"""
    mock_get_trades.return_value = [
        {'type': 'sell', 'name': 'Samsung', 'code': '005930', 'profit_amt': 10000, 'profit_rate': 10.0}
    ]
    res = commander._cmd_profit(['d'])
    assert "실현 손익" in res
    assert "+10,000원" in res

@patch('modules.telegram_bot.auto_trade.load_restricted_stocks')
def test_cmd_restricted(mock_load, commander):
    """제한 종목 조회 명령어 테스트"""
    mock_load.return_value = {'005930': {'name': 'Samsung', 'memo': 'Test'}}
    res = commander._cmd_restricted([])
    assert "Samsung" in res
    assert "Test" in res