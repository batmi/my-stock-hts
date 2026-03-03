import pytest
from unittest.mock import patch, MagicMock
from modules import analysis
import config

@patch('rich.prompt.Prompt.ask')
@patch('modules.analysis.diagnose_stock')
def test_show_stock_analysis_individual(mock_diagnose, mock_ask):
    """개별 종목 분석 메뉴 테스트"""
    # 6(개별분석) -> 종료
    mock_ask.side_effect = ["6", "q"]
    analysis.show_stock_analysis()
    mock_diagnose.assert_called_once()

@patch('rich.prompt.Prompt.ask')
@patch('modules.analysis.analyze_market_stocks')
def test_show_stock_analysis_market(mock_analyze, mock_ask):
    """전체 종목 분석 메뉴 테스트"""
    # 7(전체분석) -> 1(KOSPI)
    mock_ask.side_effect = ["7", "1"]
    analysis.show_stock_analysis()
    mock_analyze.assert_called_with("KOSPI")

@patch('rich.prompt.Prompt.ask')
@patch('modules.analysis.save_all_market_analysis')
def test_show_stock_analysis_save(mock_save, mock_ask):
    """전체 분석 저장 메뉴 테스트"""
    # 7(전체분석) -> 3(저장)
    mock_ask.side_effect = ["7", "3"]
    analysis.show_stock_analysis()
    mock_save.assert_called_once()

@patch('rich.prompt.Prompt.ask')
@patch('modules.analysis.print_table')
def test_show_stock_analysis_group(mock_print, mock_ask):
    """그룹별 분석 메뉴 테스트"""
    # 1(국내주식) -> q
    mock_ask.side_effect = ["1", "q"]
    config.session.stock_data = {"stocks_kr": [{"code": "005930", "name": "Samsung"}]}
    
    analysis.show_stock_analysis()
    mock_print.assert_called()

@patch('rich.prompt.Prompt.ask')
def test_get_analysis_params_defaults(mock_ask):
    """분석 파라미터 입력 (기본값 사용) 테스트"""
    # Enter(기본값) 연타 -> 가중치는 float 변환을 위해 명시적 값 전달 -> 1(매수필터)
    # 순서: 매수점수, RSI, 체결강도, 상승점수, 가중치(T, M, S, Syn), 필터
    mock_ask.side_effect = ["", "", "", "", "4.0", "2.5", "1.5", "2.0", "1"]
    
    params = analysis.get_analysis_params()
    assert params is not None
    assert params['OUTPUT_FILTER'] == 'BUY'

@patch('rich.prompt.Prompt.ask')
def test_get_analysis_params_custom(mock_ask):
    """분석 파라미터 입력 (사용자 값) 테스트"""
    # 점수(9.0) -> RSI(60) -> 체결(120) -> 상승(7.0) -> T(5) -> M(3) -> S(1) -> Syn(1) -> 2(상승필터)
    mock_ask.side_effect = ["9.0", "60", "120", "7.0", "5", "3", "1", "1", "2"]
    
    params = analysis.get_analysis_params()
    assert params['BUY_SCORE'] == 9.0
    assert params['BUY_RSI_MAX'] == 60
    assert params['BUY_VOL_STRENGTH'] == 120.0
    assert params['WEIGHTS']['TREND'] == 5.0
    assert params['OUTPUT_FILTER'] == 'RISE'