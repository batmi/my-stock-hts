import pytest
from datetime import datetime as _real_datetime
from unittest.mock import patch, MagicMock
import api
from modules import auto_trade


class FixedDatetime(_real_datetime):
    """now()만 고정하고 나머지(strptime 등)는 실제 datetime 동작을 유지하는 테스트용 대역.

    MagicMock으로 datetime 모듈 객체를 통째로 갈아끼우면 strftime 포맷과 무관하게
    같은 값이 나오고 strptime도 깨지므로, 서브클래스로 now()만 고정한다.
    """
    _FIXED = _real_datetime(2026, 1, 2, 16, 0, 0)  # NXT 운영 시간(16:00)

    @classmethod
    def now(cls, tz=None):
        return cls._FIXED

# --- NXT Master Tests ---
@patch('api.requests.get')
def test_load_nxt_master_success(mock_get):
    """NXT 마스터 파일 정상 로드 및 파싱 테스트"""
    # 캐시 및 플래그 초기화
    api._NXT_MASTER_LOADED = False
    api._NXT_TRADEABLE_CACHE.clear()
    
    mock_res = MagicMock()
    mock_res.status_code = 200
    # 정상적인 NXT 마스터 파일 응답 (파이프 구분자)
    mock_res.text = "005930|삼성전자|... \n000660|SK하이닉스|...\n"
    mock_get.return_value = mock_res
    
    api.load_nxt_master()
    
    assert api._NXT_MASTER_LOADED is True
    assert "005930" in api._NXT_TRADEABLE_CACHE
    assert "000660" in api._NXT_TRADEABLE_CACHE

@patch('api.requests.get')
def test_load_nxt_master_fail(mock_get):
    """NXT 마스터 파일 로드 실패 시 Fallback 동작 테스트"""
    api._NXT_MASTER_LOADED = False
    api._NXT_TRADEABLE_CACHE.clear()
    
    # API 통신 에러 발생 시뮬레이션
    mock_get.side_effect = Exception("API Connection Error")
    
    api.load_nxt_master()
    
    # 실패해도 무한 재시도를 막기 위해 로드 완료 플래그는 True로 변경되어야 함
    assert api._NXT_MASTER_LOADED is True
    # 캐시는 비어있어야 함
    assert len(api._NXT_TRADEABLE_CACHE) == 0

def test_is_nxt_tradeable():
    """NXT 거래 대상 종목 확인 로직 테스트"""
    api._NXT_MASTER_LOADED = True
    api._NXT_TRADEABLE_CACHE = {"005930"}
    
    # 1. 마스터 파일 캐시에 종목이 존재할 때
    assert api.is_nxt_tradeable("005930") is True
    # 2. 마스터 파일 캐시에 종목이 없을 때
    assert api.is_nxt_tradeable("000660") is False
    
    # 3. 마스터 파일 로드 실패로 캐시가 완전히 비어있는 경우 Fallback (모두 통과)
    api._NXT_TRADEABLE_CACHE.clear()
    assert api.is_nxt_tradeable("000660") is True

# [Fix] patch 대상은 modules.auto_trade가 아니라 실제 코드가 사는 modules.auto_trade.trader다.
#  (패키지 분해 후에도 옛 경로를 patch하고 있어 mock이 걸리지 않았고, 그 결과 이 테스트는
#   실행 시각이 실제로 NXT 시간대(15:30~20:00, 08:00~08:50)일 때만 통과하는 시간의존 테스트였다.)
@patch('modules.auto_trade.trader.datetime', FixedDatetime)
@patch('modules.auto_trade.api.is_nxt_tradeable', return_value=False)
def test_nxt_market_skip_logic(mock_nxt_tradeable):
    """NXT 장 시간대에 거래 불가 종목(ETF 등) 분석 스킵 테스트"""
    trader = auto_trade.AutoTrader()
    trader.is_running = True
    # 시장 필터(fail-closed)가 NXT 스킵보다 먼저 걸리지 않도록 정상 지수 상태를 세팅한다
    trader.buy_halted = False
    trader.market_index_status = {
        "KOSPI": {"is_healthy": True, "unknown": False, "current": 2500.0},
        "KOSDAQ": {"is_healthy": True, "unknown": False, "current": 800.0},
    }

    # 1. 매수 후보 분석 워커 스킵 테스트
    item = {'code': '000660', 'name': 'SK하이닉스', 'group': 'stocks_kr'}
    res = trader._analyze_candidate_worker(
        item=item,
        holding_codes=set(),
        rules_map={},
        restricted_stocks={},
        market_regime_adj={},
        safe_delay=0,
        reentry_hurdles={},
        holdings_dfs={},
        holding_groups_map={}
    )
    
    # NXT 거래 불가 종목이므로 스킵 로그 객체 반환 확인
    assert res is not None
    assert res.get('type') == 'log_only'
    assert 'NXT스킵' in res.get('log')
    
    # 2. 매도 후보 분석 로직 스킵 테스트
    holdings = [{'pdno': '000660', 'prdt_name': 'SK하이닉스', 'ord_psbl_qty': '10', 'evlu_pfls_rt': '5.0', 'prpr': '60000', 'pchs_avg_pric': '55000'}]
    with patch.object(trader, 'set_stock_state') as mock_set_state:
        trader._check_sell_conditions(holdings)
        # NXT 불가 종목으로 스킵되며, 상태가 None으로 세팅되었는지 확인
        mock_set_state.assert_called_with('000660', None)