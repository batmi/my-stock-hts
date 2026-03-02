import pytest
from unittest.mock import patch, MagicMock
from modules.auto_trade import AutoTrader
import config

@pytest.fixture
def trader():
    t = AutoTrader()
    t.logs = [] # 로그 초기화
    return t

def test_refine_trade_records(trader):
    """거래 내역 중복 제거 및 정제 테스트"""
    records = [
        {'odno': '1', 'reason': '체결 확인', 'time': '2023-01-01 10:00:00', 'code': '005930', 'type': 'buy'},
        {'odno': '1', 'reason': '전략 매수', 'time': '2023-01-01 10:00:00', 'code': '005930', 'type': 'buy'}, # 우선순위 높음
        {'odno': '2', 'reason': '체결 확인', 'time': '2023-01-01 11:00:00', 'code': '000660', 'type': 'sell'},
        {'odno': None, 'reason': '수동', 'time': '2023-01-01 12:00:00', 'code': '005930', 'type': 'buy'} # ODNO 없음
    ]
    
    refined = trader._refine_trade_records(records)
    
    assert len(refined) == 3
    # ODNO 1번은 '전략 매수' 사유가 남아야 함
    r1 = next(r for r in refined if r.get('odno') == '1')
    assert r1['reason'] == '전략 매수'

def test_get_recent_logs(trader):
    """최근 로그 반환 테스트"""
    for i in range(20):
        trader.log(f"Log {i}")
        
    logs = trader.get_recent_logs(count=5)
    assert "Log 19" in logs
    assert "Log 15" in logs
    assert "Log 14" not in logs # 5개만 가져오므로