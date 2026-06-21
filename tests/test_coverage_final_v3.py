import pytest
from unittest.mock import patch, MagicMock, ANY
import api
import config
from modules import analysis, auto_trade, market, account, db_manager
import pandas as pd
import utils
import os

# --- API ---
def test_get_telegram_footer():
    """텔레그램 푸터 생성 테스트"""
    config.TELEGRAM_BOT_TOKEN = "TEST"
    config.TELEGRAM_INSTANCE_NAME = "HTS"
    config.session.cano = "12345678"
    config.session.is_simulation = True
    
    footer = api._get_telegram_footer()
    assert "[HTS | 모의 12345678]" in footer

@patch('requests.post')
def test_send_telegram_photo_retry(mock_post):
    """사진 전송 재시도 테스트"""
    config.TELEGRAM_BOT_TOKEN = "TEST"
    config.TELEGRAM_CHAT_ID = "123"
    
    # 1, 2회 실패 -> 3회 성공
    mock_post.side_effect = [
        Exception("Fail 1"),
        MagicMock(status_code=500),
        MagicMock(status_code=200)
    ]
    
    with patch('builtins.open', MagicMock()):
        with patch('os.path.exists', return_value=True):
            with patch('time.sleep'):
                ret = api.send_telegram_photo("test.png")
                assert ret is True
                assert mock_post.call_count == 3

# --- Analysis ---
def test_calculate_score_weights():
    """가중치 적용 점수 계산 테스트"""
    # 기본 점수 계산
    score_def, _ = analysis.calculate_score(10000, 9000, 8000, 7000, 9000, 60, 30, 100, True, 50, 40)
    
    # 가중치 변경 (추세 비중 0 → 추세 점수가 모두 제거되어 총점이 낮아져야 함)
    # 다른 팩터는 기본값 유지하여 '추세 비중' 변화 효과만 검증 (보정 상쇄로 인한 우연한 동점 방지)
    weights = {"TREND": 0.0, "MOMENTUM": 2.5, "STRENGTH": 1.5, "SYNERGY": 2.0}
    score_custom, _ = analysis.calculate_score(10000, 9000, 8000, 7000, 9000, 60, 30, 100, True, 50, 40, weights=weights)

    assert score_custom < score_def

@patch('modules.analysis._load_analysis_result')
@patch('rich.prompt.Prompt.ask')
def test_analyze_market_stocks_cache(mock_ask, mock_load):
    """시장 분석 캐시 사용 테스트"""
    mock_load.return_value = {
        'updated_at': '2023-01-01',
        'params': {},
        'data': [{'code': '005930', 'name': 'Samsung', 'score': 9.0, 'rsi': 50, 'state': '매수', 'state_color': '[red]', 'state_reason': '', 'adx': 20, 'cci': 100, 'is_target': True, 'price': 60000}]
    }
    # [수정] 무한 루프 방지: 'y'(캐시사용) -> 'q'(상세보기 종료)
    mock_ask.side_effect = ['y', 'q']
    
    with patch('config.console.print') as mock_print:
        analysis.analyze_market_stocks("KOSPI")
        assert mock_print.call_count > 0

# --- AutoTrade ---
def test_refine_trade_records_priority():
    """거래 내역 정제 우선순위 테스트"""
    trader = auto_trade.AutoTrader()
    records = [
        {'odno': '1', 'reason': '체결 확인', 'time': '2023-01-01 10:00:00', 'code': '005930', 'type': 'buy'},
        {'odno': '1', 'reason': '전략 매수', 'time': '2023-01-01 10:00:00', 'code': '005930', 'type': 'buy'}
    ]
    refined = trader._refine_trade_records(records)
    assert len(refined) == 1
    assert refined[0]['reason'] == '전략 매수'

@patch('modules.auto_trade.api.get_domestic_balance')
@patch('modules.auto_trade.api.get_deposit_balance')
def test_get_total_estimated_asset_fallback(mock_deposit, mock_balance):
    """자산 계산 Fallback 테스트"""
    trader = auto_trade.AutoTrader()
    # 잔고 조회 실패 (API 모듈은 실패 시 None, None을 반환함)
    mock_balance.return_value = (None, None)
    # 예수금 조회 성공
    mock_deposit.return_value = {'deposit': 1000000, 'foreign_deposit': 0, 'd2_deposit': 1000000}
    
    config.session.is_simulation = False
    
    asset = trader._get_total_estimated_asset()
    assert asset == 1000000

# --- Utils ---
def test_get_tick_size_edge():
    """호가 단위 경계값 테스트"""
    assert utils.get_tick_size(1999, False) == 1
    assert utils.get_tick_size(2000, False) == 5
    assert utils.get_tick_size(4999, False) == 5
    assert utils.get_tick_size(5000, False) == 10