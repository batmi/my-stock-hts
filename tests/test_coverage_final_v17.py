import pytest
from unittest.mock import patch, MagicMock, ANY
import pandas as pd
import numpy as np
from datetime import datetime

import api
import config
import utils
from modules import analysis, auto_trade, market, theme_analysis, db_manager, manage, account, backtest
from modules.telegram_bot import TelegramCommander

@pytest.fixture(autouse=True)
def setup_env():
    """테스트 간 싱글톤 및 DB 상태 초기화"""
    TelegramCommander._instance = None
    auto_trade.AutoTrader._instance = None
    yield
    try:
        db_manager.db.close_connection()
    except: pass

# ==========================================================
# 1. modules/backtest.py 커버리지 (58% -> Target)
# ==========================================================
@patch('modules.backtest.api.get_investor_trend')
def test_backtest_append_smart_money_empty(mock_inv):
    """백테스트 스마트머니 병합 로직에서 수급 데이터가 없을 때의 방어 로직 커버리지"""
    # API 응답이 없거나 에러 발생 시
    mock_inv.return_value = []
    
    df = pd.DataFrame({'date': ['20230101', '20230102'], 'close': [100, 110]})
    
    with patch('config.console.print') as mock_print:
        res_df = backtest._append_smart_money_signal(df.copy(), "005930", is_overseas=False)
        
        assert 'smart_money' in res_df.columns
        assert res_df['smart_money'].iloc[0] == False
        assert any("데이터를 불러올 수 없어" in str(c) for c in mock_print.call_args_list)

@patch('modules.backtest.api.get_investor_trend')
def test_backtest_append_smart_money_exception(mock_inv):
    """백테스트 스마트머니 병합 중 예외 발생 시 로직 커버리지"""
    mock_inv.side_effect = Exception("API Error")
    df = pd.DataFrame({'date': ['20230101', '20230102'], 'close': [100, 110]})
    
    with patch('config.console.print') as mock_print:
        res_df = backtest._append_smart_money_signal(df.copy(), "005930", is_overseas=False)
        assert 'smart_money' in res_df.columns
        assert any("오류가 발생하여" in str(c) for c in mock_print.call_args_list)

@patch('modules.backtest.utils.validate_and_confirm_stock', return_value=True)
@patch('modules.backtest.api.get_stock_name_by_code', return_value="Samsung")
@patch('modules.backtest.get_backtest_data')
@patch('rich.prompt.Prompt.ask')
def test_run_backtest_optimization_grids(mock_ask, mock_get_data, mock_name, mock_val):
    """백테스팅 UI 중 '최적화 모드(RSI, 익절/손절, 가중치)' 전체 루프 커버리지"""
    # 더미 데이터 생성 (최소 60일 이상)
    dates = pd.date_range(start="2023-01-01", periods=100).strftime("%Y%m%d")
    df = pd.DataFrame({
        'date': dates, 'close': [10000]*100, 'open': [10000]*100, 
        'high': [10000]*100, 'low': [10000]*100, 'volume': [100]*100
    })
    mock_get_data.return_value = df
    
    # 시나리오 (run_backtest 대화 순서 그대로 — 프롬프트가 늘면 함께 갱신할 것):
    #   6(직접입력) -> 005930 -> n(프리셋) -> y(설정변경) ->
    #   [기간]100 -> [매수]8.0 -> [RSI]60 -> [익절]20.0 -> n(반익절X) -> [익절RSI]75 ->
    #   [매도점수]5.0 -> [TS발동]10.0 -> [TS콜백]3.0 -> [시간청산]10 -> n(ATR 미사용) -> [손절]-5.0 ->
    #   n(피라미딩 변경X) -> n(가중치 변경X) -> 1(모드=단일) -> n(AI진단X) -> q(메인복귀)
    mock_ask.side_effect = [
        "6", "005930", "n", "y", "100", "8.0", "60", "20.0", "n", "75", "5.0",
        "10.0", "3.0", "10", "n", "-5.0", "n", "n", "1", "n", "q"
    ]
    
    # 빠른 시뮬레이션을 위해 simulate_strategy 모킹 (내부의 방대한 연산 스킵)
    with patch('modules.backtest.simulate_strategy') as mock_sim:
        mock_sim.return_value = {
            "trades": [], "final_asset": 10000000, "total_return": 0, "mdd": 0,
            "win_trades": 0, "loss_trades": 0, "gross_profit": 0, "gross_loss": 0,
            "daily_assets": [], "max_score_observed": 0, "score_8_count": 0,
            "missed_caution_count": 0, "missed_danger_count": 0, "missed_trades": []
        }
        
        with patch('config.console.print'), patch('config.console.status') as mock_status:
            mock_status.return_value.__enter__.return_value = MagicMock()
            backtest.run_backtest()
            
        # 최적화 로직(점수별, RSI별, 익절/손절별, 가중치별)에 의해 수십 번의 simulate_strategy가 호출되어야 함
        assert mock_sim.call_count > 20

# ==========================================================
# 2. modules/telegram_bot.py 커버리지 (67% -> Target)
# ==========================================================
@patch('modules.telegram_bot.utils.get_stock_memos')
@patch('modules.telegram_bot.utils.delete_stock_memo_by_id')
@patch('modules.telegram_bot.utils.delete_all_stock_memos')
@patch('modules.telegram_bot.api.get_stock_name_by_code', return_value="Apple")
def test_telegram_memo_various_commands(mock_name, mock_del_all, mock_del_id, mock_get_memos):
    """텔레그램 /memo 명령어의 상세 파싱 및 예외 분기 커버리지"""
    cmd = TelegramCommander()
    
    # 1. /memo d [ID] (삭제 - 정상 ID)
    mock_del_id.return_value = True
    res1 = cmd._cmd_memo(["d", "5"])
    assert "삭제되었습니다" in res1
    
    # 2. /memo d [ID] (삭제 - 존재하지 않는 ID)
    mock_del_id.return_value = False
    res2 = cmd._cmd_memo(["d", "99"])
    assert "실패" in res2
    
    # 3. /memo d [종목명] (전체 삭제)
    config.session.stock_data = {"stocks_us": [{"code": "AAPL", "name": "Apple"}]}
    res3 = cmd._cmd_memo(["d", "Apple"])
    assert "모든 메모가 삭제" in res3
    mock_del_all.assert_called_with("AAPL")
    
    # 4. /memo (특정 종목 상세 조회 - 메모 존재)
    mock_get_memos.return_value = [{'id': 1, 'updated_at': '2023-01-01', 'memo': 'Test Memo'}]
    res4 = cmd._cmd_memo(["Apple"])
    assert "Test Memo" in res4

@patch('modules.telegram_bot.api.get_yf_fast_info')
@patch('modules.telegram_bot.analysis.get_domestic_index_data')
def test_telegram_market_status_error_handling(mock_dom, mock_fi):
    """/market 명령어 내 지수 조회 실패 및 예외 포맷팅 커버리지"""
    cmd = TelegramCommander()
    
    # 국내 지수 데이터 부족 처리
    mock_dom.return_value = pd.DataFrame()
    # 해외 지수 API 에러 발생
    mock_fi.side_effect = Exception("API Error")
    
    res = cmd._get_market_status(["국내 지수 (Domestic Indices)", "섹터 및 주요 지표 (Sectors & Key Indicators)"])
    
    assert "오류" in res or "데이터 조회 실패" in res

# ==========================================================
# 3. modules/auto_trade.py 커버리지 (69% -> Target)
# ==========================================================
@patch('modules.auto_trade.db_manager.db.get_trades')
@patch('modules.auto_trade.account.get_asset_status_data')
@patch('rich.prompt.Prompt.ask')
def test_autotrader_print_report_custom_days(mock_ask, mock_asset, mock_get_trades):
    """AutoTrader 트레이딩 평가 리포트 메뉴 (직접 입력 분기) 커버리지"""
    trader = auto_trade.AutoTrader()
    
    # 시나리오: 4(직접입력) -> 15(일)
    mock_ask.side_effect = ["4", "15"]
    
    # 더미 매매 기록
    mock_get_trades.return_value = [
        {'type': 'buy', 'code': '005930', 'name': 'Samsung', 'qty': 10, 'price': 60000, 'time': '2023-01-01 10:00:00', 'odno': '1', 'profit_rate': 0, 'profit_amt': 0, 'reason': '조건 만족'},
        {'type': 'sell', 'code': '005930', 'name': 'Samsung', 'qty': 10, 'price': 65000, 'time': '2023-01-10 10:00:00', 'odno': '2', 'profit_rate': 8.3, 'profit_amt': 50000, 'reason': '익절'}
    ]
    
    mock_asset.return_value = {'tot_asset': 10500000}
    
    with patch('config.console.print'):
        trader.print_report()
        
    # 15일 인자가 전달되었는지 확인
    mock_get_trades.assert_called()
    args, kwargs = mock_get_trades.call_args
    assert kwargs.get('start_date') is not None

def test_risk_manager_check_loss_limit_edge_case():
    """일일 손실 제한(Loss Limit) 정확히 임계점 도달 시 킬 스위치 발동 커버리지"""
    trader = auto_trade.AutoTrader()
    trader.initial_asset = 10_000_000
    rm = auto_trade.RiskManager(trader)
    
    config.SYSTEM_DAILY_LOSS_LIMIT = 10.0 # 10% 손실 제한
    current_total = 9_000_000 # 정확히 -10% 손실 (900만원)
    
    with patch.object(trader, 'stop') as mock_stop, \
         patch('modules.auto_trade.api.send_telegram_message') as mock_tg, \
         patch('config.console.print'):
             
        rm.check_loss_limit(current_total)
        
        # 임계값 이하라면 중단 로직이 트리거되어야 함
        mock_stop.assert_called_once_with(use_status=False)
        mock_tg.assert_called_once()

# ==========================================================
# 4. modules/analysis.py 커버리지 (72% -> Target)
# ==========================================================
@patch('rich.prompt.Prompt.ask')
def test_analysis_get_params_invalid_types(mock_ask):
    """분석 파라미터 사용자 입력 시 잘못된 문자열 예외 처리 커버리지"""
    # 시나리오: 
    # 1~4. 매수점수/RSI/체결강도/상승점수에 모두 문자열(invalid) 입력 -> 예외 발생 후 기본값 유지로 넘어감
    # 5~8. 가중치 설정에 10/10/0/0 입력 -> 합계 20이므로 루프 재시작
    # 9~12. 가중치 설정에 정상값 입력 -> 합계 10 통과
    # 13. 출력 필터 1 선택
    mock_ask.side_effect = ["abc", "xyz", "invalid", "invalid", "10", "10", "0", "0", "4.0", "2.5", "1.5", "2.0", "1"]
    
    with patch('config.console.print'):
        params = analysis.get_analysis_params()
        
        assert params is not None
        # 예외가 발생했어도 기본값이나 정상 입력값으로 복원되어 진행됨
        assert params['BUY_SCORE'] == config.ANALYSIS_THRESHOLDS["BUY_SCORE"]
        assert params['BUY_RSI_MAX'] == config.ANALYSIS_THRESHOLDS["BUY_RSI_MAX"]

@patch('modules.analysis._get_master_stock_list', return_value=[{'code': '005930', 'name': 'Samsung'}])
@patch('modules.analysis.api.get_current_price_data', return_value={'rt_cd': '1'})
@patch('modules.analysis.api.get_chart_data', return_value=pd.DataFrame())
def test_analysis_diagnose_group_stocks_data_fail(mock_chart, mock_cp, mock_master):
    """그룹 종목 분석 중 API 데이터 획득 모두 실패 시의 출력 포맷 커버리지"""
    # 국내 주식만 설정
    config.session.stock_data = {'stocks_kr': [{'code': '005930', 'name': 'Samsung'}], 'etfs_kr': []}
    
    with patch('config.console.print') as mock_print:
        analysis.diagnose_group_stocks("KOSPI")
        
        # 결과가 없다는 문구가 출력되어야 함
        assert any("해당 조건" in str(c) or "데이터를 불러올 수 없습니다" in str(c) for c in mock_print.call_args_list)

# ==========================================================
# 5. api.py & utils.py 커버리지 (74%, 69% -> Target)
# ==========================================================
@patch('api.call_api')
def test_api_get_intraday_domestic_loop_escape(mock_call):
    """국내 1분봉 페이징 순회 루프 탈출 조건 커버리지"""
    # KIS API 분봉 페이징 시뮬레이션
    # 1번째 응답: 데이터 있음, 2번째 응답: 데이터 없음(루프 종료)
    mock_call.side_effect = [
        {'rt_cd': '0', 'output2': [{'stck_bsop_date': '20230101', 'stck_cntg_hour': '150000', 'stck_prpr': '100', 'stck_oprc': '100', 'stck_hgpr': '100', 'stck_lwpr': '100', 'cntg_vol': '10'}]},
        {'rt_cd': '0', 'output2': []}
    ]
    
    df = api._get_intraday_chart_data("005930", is_overseas=False)
    assert not df.empty
    assert len(df) == 1
    assert 'date' in df.columns

@patch('api._get_intraday_yfinance')
@patch('api.call_api')
def test_api_get_intraday_domestic_premarket_empty(mock_call, mock_yf):
    """장 시작 전 등으로 KIS 당일분봉이 비면 빈 값을 반환(yfinance 폴백 없음)."""
    mock_call.return_value = {'rt_cd': '0', 'output2': []}  # 당일 데이터 없음

    df = api._get_intraday_chart_data("005930", is_overseas=False)

    assert df.empty
    mock_yf.assert_not_called()  # 국내 장전엔 yfinance로 폴백하지 않음(원복)

@patch('rich.prompt.Prompt.ask')
@patch('api.get_current_price_data', return_value={'rt_cd': '0', 'output': {'last': '150.50'}})
def test_utils_validate_confirm_stock_overseas(mock_cp_data, mock_ask):
    """해외 주식 선택 및 현재가 확인 로직 커버리지"""
    # 해외 주식은 달러($) 포맷팅이 적용됨
    mock_ask.return_value = "y"
    
    with patch('config.console.print'):
        res = utils.validate_and_confirm_stock("AAPL", "Apple", True, "진행?")
        
        assert res is True