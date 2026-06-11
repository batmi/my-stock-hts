import pytest
from unittest.mock import patch, MagicMock
import api
from modules import auto_trade

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

@patch('modules.auto_trade.datetime')
@patch('modules.auto_trade.api.is_nxt_tradeable', return_value=False)
def test_nxt_market_skip_logic(mock_nxt_tradeable, mock_datetime):
    """NXT 장 시간대에 거래 불가 종목(ETF 등) 분석 스킵 테스트"""
    # 현재 시간을 NXT 장 운영 시간인 16:00 (1600)으로 모킹
    mock_now = MagicMock()
    mock_now.strftime.return_value = "1600"
    mock_datetime.now.return_value = mock_now
    
    trader = auto_trade.AutoTrader()
    trader.is_running = True
    
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