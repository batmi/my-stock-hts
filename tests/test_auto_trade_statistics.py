import pytest
from modules.auto_trade import AutoTrader
from datetime import datetime, timedelta
from unittest.mock import patch

@pytest.fixture
def trader():
    return AutoTrader()

def test_calculate_statistics_empty(trader):
    """빈 거래 내역 통계 계산 테스트"""
    stats = trader._calculate_statistics([])
    assert stats['total_trades'] == 0
    assert stats['win_rate'] == 0.0
    assert stats['total_profit'] == 0

@patch('modules.auto_trade.db_manager.db.get_trades')
def test_calculate_statistics_mixed(mock_get_trades, trader):
    """매수/매도 혼합 내역 통계 계산 테스트"""
    mock_get_trades.return_value = []
    records = [
        {'type': 'buy', 'code': '005930', 'time': '2023-01-01 10:00:00', 'price': 10000, 'qty': 10},
        {'type': 'sell', 'code': '005930', 'time': '2023-01-02 10:00:00', 'price': 11000, 'qty': 10, 'profit_amt': 10000, 'profit_rate': 10.0, 'reason': '익절'},
        {'type': 'buy', 'code': '000660', 'time': '2023-01-03 10:00:00', 'price': 20000, 'qty': 5},
        {'type': 'sell', 'code': '000660', 'time': '2023-01-03 11:00:00', 'price': 19000, 'qty': 5, 'profit_amt': -5000, 'profit_rate': -5.0, 'reason': '손절'}
    ]
    
    stats = trader._calculate_statistics(records)
    
    assert stats['total_trades'] == 4
    assert stats['buy_count'] == 2
    assert stats['sell_count'] == 2
    assert stats['win_trades'] == 1
    assert stats['loss_trades'] == 1
    assert stats['total_profit'] == 5000
    assert stats['avg_profit_rate'] == 2.5
    assert stats['win_rate'] == 50.0
    assert stats['sell_trades_exist'] is True
    assert stats['best_trade']['code'] == '005930'
    assert stats['worst_trade']['code'] == '000660'
    assert stats['sell_reasons']['익절'] == 1
    assert stats['sell_reasons']['손절'] == 1
    
    # 보유 기간 확인 (1일 vs 1시간 -> 평균 약 12.5시간)
    assert "시간" in stats['avg_holding_str'] or "분" in stats['avg_holding_str']

@patch('modules.auto_trade.db_manager.db.get_trades')
def test_calculate_statistics_holding_time(mock_get_trades, trader):
    """평균 보유 기간 계산 테스트"""
    mock_get_trades.return_value = []
    t1 = datetime(2023, 1, 1, 10, 0, 0)
    t2 = t1 + timedelta(minutes=1)
    
    records = [
        {'type': 'buy', 'code': 'A', 'time': t1.strftime("%Y-%m-%d %H:%M:%S"), 'price': 100, 'qty': 1},
        {'type': 'sell', 'code': 'A', 'time': t2.strftime("%Y-%m-%d %H:%M:%S"), 'price': 110, 'qty': 1, 'profit_amt': 10, 'profit_rate': 10.0}
    ]
    
    stats = trader._calculate_statistics(records)
    assert stats['avg_holding_str'] == "1분 0초"