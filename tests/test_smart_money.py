import pytest
import pandas as pd
from unittest.mock import patch, MagicMock
from datetime import datetime
import config
from modules import analysis
from modules import backtest

@pytest.fixture(autouse=True)
def clear_sm_cache():
    """테스트 간 캐시 간섭을 막기 위한 자동 초기화"""
    analysis.clear_smart_money_cache()
    yield
    analysis.clear_smart_money_cache()

# ==========================================================
# 1. analysis.check_smart_money_turnaround 테스트
# ==========================================================

def test_check_smart_money_overseas():
    """해외 주식은 수급 데이터를 제공하지 않으므로 즉시 False 반환"""
    flag, reason = analysis.check_smart_money_turnaround("AAPL", is_overseas=True)
    assert flag is False
    assert reason == ""

@patch('api.get_investor_trend')
def test_check_smart_money_no_data(mock_api):
    """API 응답이 비어있거나 데이터가 부족한 경우 예외 처리"""
    mock_api.return_value = []
    flag, reason = analysis.check_smart_money_turnaround("005930")
    assert flag is False
    
    mock_api.return_value = [{'frgn_ntby_qty': '100', 'orgn_ntby_qty': '100'}] # 3일치 미만
    flag, reason = analysis.check_smart_money_turnaround("005930")
    assert flag is False

@patch('api.get_investor_trend')
def test_check_smart_money_ssang_ssang(mock_api):
    """당일 쌍끌이 매수 포착 테스트"""
    mock_api.return_value = [
        {'frgn_ntby_qty': '100', 'orgn_ntby_qty': '200'}, # D-day (외인+, 기관+)
        {'frgn_ntby_qty': '-10', 'orgn_ntby_qty': '-10'},
        {'frgn_ntby_qty': '-10', 'orgn_ntby_qty': '-10'}
    ]
    flag, reason = analysis.check_smart_money_turnaround("005930")
    assert flag is True
    assert reason == "쌍끌이 매수"

@patch('api.get_investor_trend')
def test_check_smart_money_foreign_turnaround(mock_api):
    """외국인 단독 턴어라운드 (D-day 매수, D-1/D-2 연속 매도) 포착"""
    mock_api.return_value = [
        {'frgn_ntby_qty': '150', 'orgn_ntby_qty': '-50'},  # D-day (매수)
        {'frgn_ntby_qty': '-100', 'orgn_ntby_qty': '-10'}, # D-1 (매도)
        {'frgn_ntby_qty': '-200', 'orgn_ntby_qty': '-10'}, # D-2 (매도)
        {'frgn_ntby_qty': '300', 'orgn_ntby_qty': '-10'}   # D-3
    ]
    flag, reason = analysis.check_smart_money_turnaround("005930")
    assert flag is True
    assert reason == "외국인 턴어라운드"

@patch('api.get_investor_trend')
def test_check_smart_money_orgn_turnaround_yesterday(mock_api):
    """전일 기준 기관 턴어라운드 포착 (최근 2일 내 검사)"""
    mock_api.return_value = [
        {'frgn_ntby_qty': '-10', 'orgn_ntby_qty': '-50'},  # D-day (매도)
        {'frgn_ntby_qty': '-10', 'orgn_ntby_qty': '300'},  # D-1 (매수) -> 턴어라운드 발생일!
        {'frgn_ntby_qty': '-10', 'orgn_ntby_qty': '-100'}, # D-2 (매도)
        {'frgn_ntby_qty': '-10', 'orgn_ntby_qty': '-200'}  # D-3 (매도)
    ]
    flag, reason = analysis.check_smart_money_turnaround("005930")
    assert flag is True
    assert reason == "기관 턴어라운드"

@patch('api.get_investor_trend')
def test_check_smart_money_cache_hit(mock_api):
    """메모리 TTL 캐시 적용 여부 확인"""
    mock_api.return_value = [
        {'frgn_ntby_qty': '100', 'orgn_ntby_qty': '200'},
        {'frgn_ntby_qty': '-10', 'orgn_ntby_qty': '-10'},
        {'frgn_ntby_qty': '-10', 'orgn_ntby_qty': '-10'}
    ]
    # 첫 번째 호출 (API 사용)
    analysis.check_smart_money_turnaround("005930")
    assert mock_api.call_count == 1
    
    # 두 번째 호출 (캐시 사용으로 API 미호출)
    analysis.check_smart_money_turnaround("005930")
    assert mock_api.call_count == 1

# ==========================================================
# 2. analysis.calculate_score 테스트
# ==========================================================

def test_calculate_score_smart_money():
    """스마트머니 플래그에 따른 0.5점 가산점 정상 부여 확인"""
    test_weights = {"TREND": 4.0, "MOMENTUM": 2.5, "STRENGTH": 1.5, "SYNERGY": 2.0}
    # 동일한 조건에서 smart_money 여부만 변경
    score_no_sm, _ = analysis.calculate_score(1000, None, None, None, None, None, None, None, False, weights=test_weights, smart_money=False)
    score_sm, details = analysis.calculate_score(1000, None, None, None, None, None, None, None, False, weights=test_weights, smart_money=True)
    
    # 정확히 0.5점 차이 확인
    assert score_sm == round(score_no_sm + 0.5, 2)
    assert any("SM: 메이저 수급 턴어라운드" in d for d in details)

# ==========================================================
# 3. backtest._append_smart_money_signal 테스트
# ==========================================================

@patch('api.get_investor_trend')
def test_append_smart_money_signal_vectorized(mock_api):
    """백테스팅을 위한 판다스 벡터화 병합 및 조건 처리 확인"""
    # 차트 데이터 프레임 (시간순 정렬)
    df = pd.DataFrame({
        'date': ['20231001', '20231002', '20231003', '20231004', '20231005']
    })
    
    # API 응답 (최신순 역정렬)
    mock_api.return_value = [
        {'stck_bsop_date': '20231005', 'frgn_ntby_qty': '100', 'orgn_ntby_qty': '100'},  # D-Day: 쌍끌이 (True)
        {'stck_bsop_date': '20231004', 'frgn_ntby_qty': '100', 'orgn_ntby_qty': '-10'},  # D-1: 외인 매수 (D-2/D-3이 음수이므로 외인 턴어라운드 True)
        {'stck_bsop_date': '20231003', 'frgn_ntby_qty': '-10', 'orgn_ntby_qty': '-10'},  # D-2
        {'stck_bsop_date': '20231002', 'frgn_ntby_qty': '-10', 'orgn_ntby_qty': '-10'},  # D-3
        {'stck_bsop_date': '20231001', 'frgn_ntby_qty': '10',  'orgn_ntby_qty': '-10'},  # D-4: 쌍끌이 아님 & 과거 데이터 부족으로 턴어라운드 판별 불가 -> False
    ]
    
    with patch('config.console.print'): # 안내 문구 출력 숨김
        res_df = backtest._append_smart_money_signal(df.copy(), "005930", is_overseas=False)
    
    assert 'smart_money' in res_df.columns
    
    # 검증: 20231001 (이전 데이터 없으므로 False)
    assert res_df.loc[res_df['date'] == '20231001', 'smart_money'].iloc[0] == False
    
    # 검증: 20231004 (외인 턴어라운드 성공일)
    assert res_df.loc[res_df['date'] == '20231004', 'smart_money'].iloc[0] == True
    
    # 검증: 20231005 (당일 쌍끌이 + 어제 외인 턴어라운드 성공일 반영됨)
    assert res_df.loc[res_df['date'] == '20231005', 'smart_money'].iloc[0] == True