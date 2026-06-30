import pytest
from unittest.mock import patch, MagicMock
import pandas as pd
import numpy as np
from datetime import datetime, timedelta, timezone
import os
import queue
import threading
import requests

import config
import context
import api
from modules import market, auto_trade, analysis, account, settings, telegram_bot, backtest, theme_analysis, db_manager, trading

@pytest.fixture(autouse=True)
def cleanup_db_connection():
    yield
    try:
        db_manager.db.close_connection()
    except: pass

# ==============================================================================
# 1. modules/market.py 커버리지 (데이터 갭 패치, 엣지 케이스 지수 색상 렌더링)
# ==============================================================================

@patch('modules.market.api.get_yf_fast_info', return_value=None) # fast_info 무시하고 과거 데이터 로직 타게 함
def test_market_process_index_worker_gap_patching(mock_fast_info):
    """주말/휴장일로 인한 데이터 갭 발생 시 분봉 데이터를 활용한 패치(Patch) 로직 커버리지"""
    # 평일(월요일)로 기준일 고정하여 주말 테스트 시 is_gap 조건이 False가 되는 문제를 완벽 방지
    real_now = datetime(2024, 4, 15, 12, 0, 0)
    past_date = real_now - timedelta(days=10)
    
    df_daily = pd.DataFrame({
        'close': [100.0]*60,
        'open': [100.0]*60, 'high': [100.0]*60, 'low': [100.0]*60, 'volume': [1000]*60
    }, index=pd.date_range(end=past_date, periods=60))
    
    # 분봉에는 충분히 최근의 데이터가 있음
    df_intra = pd.DataFrame({
        'close': [105.0, 106.0, 107.0]
    }, index=pd.DatetimeIndex([real_now - timedelta(days=1), real_now, real_now + timedelta(days=1)]))
    
    # S&P500 (해외 지수여야 패치 로직이 돎)
    res = market._process_index_worker("S&P500", "^GSPC", df_daily, df_intra)
    
    assert res['status'] == 'success'
    # 분봉 데이터로 패치되었으므로 patched_name이 기록되어야 함
    assert res.get('patched_name') == "S&P500"

@patch('modules.market.api.fetch_yfinance_data', return_value=pd.DataFrame())
@patch('modules.market.api.get_yf_fast_info', return_value=None)
def test_market_process_index_worker_adaptive_colors(mock_fast_info, mock_fetch):
    """시장 지수별 특수 조건(미국채 10년, SOX, 달러인덱스 등) 색상 렌더링 분기 커버리지"""
    # fast_info 실패 시 분봉(5m)을 단건 지연조회하므로, 네트워크 격리를 위해 빈 분봉을 반환하도록 모킹
    df_base = pd.DataFrame({
        'close': [100.0]*20, 'open': [100.0]*20, 'high': [100.0]*20, 'low': [100.0]*20, 'volume': [1000]*20
    }, index=pd.date_range('2023-01-01', periods=20))
    
    # 미국채 10년물 금리 (5.20 이상 -> magenta)
    df_10y = df_base.copy()
    df_10y.iloc[-1, df_10y.columns.get_loc('close')] = 5.30
    res1 = market._process_index_worker("미국채 10년물 금리", "^TNX", df_10y, pd.DataFrame())
    assert "magenta" in res1['row_data'][0]
    
    # SOX 반도체 (high_52_rate 적용) - 52주 고점 대비 -25% 이하 -> blue
    df_sox = df_base.copy()
    df_sox.iloc[-1, df_sox.columns.get_loc('close')] = 70.0 # 고점 100 대비 -30%
    res2 = market._process_index_worker("SOX (반도체)", "^SOX", df_sox, pd.DataFrame())
    assert "blue" in res2['row_data'][0]
    
    # 달러인덱스 (120 이상 -> magenta)
    df_dx = df_base.copy()
    df_dx.iloc[-1, df_dx.columns.get_loc('close')] = 125.0
    res3 = market._process_index_worker("달러인덱스", "DX-Y.NYB", df_dx, pd.DataFrame())
    assert "magenta" in res3['row_data'][0]

# ==============================================================================
# 2. modules/auto_trade.py 커버리지 (예외 블록, Kill Switch, Heartbeat)
# ==============================================================================

@patch('modules.auto_trade.api.get_domestic_balance')
def test_autotrader_run_loop_exception_kill_switch(mock_bal):
    """자동매매 루프 내 API 치명적 오류 연속 발생 시 대기 모드 전환 로직"""
    trader = auto_trade.AutoTrader()
    trader.is_running = True
    trader.thread = threading.current_thread()
    
    # 루프 안에서 Exception 발생 유도
    mock_bal.side_effect = Exception("Fatal Network Error")
    
    # MAX_ERRORS 임계치를 2로 낮춰서 빠른 테스트 진행
    config.SYSTEM_MAX_CONSECUTIVE_ERRORS = 2
    trader.consecutive_errors = 1 # 1회 남음
    
    with patch.object(trader, 'is_market_open', return_value=True), \
         patch.object(trader, '_wait_for_server_recovery') as mock_recovery, \
         patch('modules.auto_trade.api.send_telegram_message') as mock_tg, \
         patch('time.sleep', side_effect=InterruptedError): # 다음 루프 방지용
             
        try:
            trader._run_loop()
        except InterruptedError:
            pass
            
        # 에러가 2회 누적되어 서버 복구 대기 모드로 빠졌는지 확인
        mock_recovery.assert_called_once()
        mock_tg.assert_called()
        assert any("시스템 긴급 대기" in args[0][0] for args in mock_tg.call_args_list)

@patch('modules.auto_trade.api.get_today_history', return_value={'rt_cd': '1'})
@patch('modules.auto_trade.api.get_overseas_today_history', return_value={'rt_cd': '1'})
def test_conclusion_monitor_error_handling(mock_ovs, mock_dom):
    """체결 감시자 내부 API 에러 시 consecutive_errors 증가 및 예외 방어 커버리지"""
    monitor = auto_trade.ConclusionMonitor()
    monitor.initialized = True
    monitor.consecutive_errors = 0
    
    # API 조회 실패를 유도하여 has_error = True 반환
    is_limited, has_error = monitor._check_conclusions()
    
    assert has_error is True
    
    # run_loop 시뮬레이션
    monitor.is_running = True
    monitor.thread = threading.current_thread()
    
    with patch.object(monitor, '_is_market_open', return_value=True):
        with patch('time.sleep'):
            with patch.object(monitor, '_check_conclusions', side_effect=Exception("Simulated Crash")):
                with patch.object(monitor.event, 'wait', side_effect=InterruptedError):
                    try:
                        monitor._run_loop()
                    except InterruptedError:
                        pass
    
    assert monitor.consecutive_errors == 1

# ==============================================================================
# 3. modules/analysis.py 커버리지 (파일 에러, 그룹 필터링 엣지)
# ==============================================================================

@patch('pandas.ExcelWriter')
def test_save_all_market_analysis_permission_error(mock_writer):
    """전체 종목 분석 엑셀 저장 중 권한/파일 열림 에러 시 예외 처리"""
    # 엑셀 파일이 열려있어서 PermissionError가 나는 상황 모킹
    mock_writer.side_effect = PermissionError("Permission denied")
    
    mock_item = {
        'code': '005930', 'name': 'Samsung', 'price': 100, 'score': 5.0, 'state': '매수',
        'state_reason': '', 'state_color': '[red]', 'rsi': 50, 'adx': 50, 'plus_di': 35, 'minus_di': 15, 'cci': 50, 'psar': 50, 'macd': 1,
        'macd_signal': 0, 'obv_trend': True, 'vol_strength': 100, 'w52_pos': 50, 'is_custom_rule': False,
        'is_target': True
    }

    with patch('modules.analysis._get_master_stock_list', return_value=[{'code': '005930', 'name': 'Samsung'}]), \
         patch('modules.analysis._analyze_stock_worker', return_value=mock_item), \
         patch('rich.prompt.Prompt.ask', return_value='y'), \
         patch('config.console.print') as mock_print:
             
        analysis.save_all_market_analysis()
        
        # 오류 발생 메시지가 정상적으로 콘솔에 출력되었는지 확인
        assert any("오류 발생" in str(c) for c in mock_print.call_args_list)

@patch('modules.analysis._load_analysis_result', return_value=None)
def test_analyze_market_stocks_empty_results(mock_load):
    """시장 전체 분석 후 결과가 비어있을 때 방어 로직"""
    with patch('modules.analysis._get_master_stock_list', return_value=[]), \
         patch('rich.prompt.Prompt.ask', return_value='n'), \
         patch('config.console.print') as mock_print:
             
        analysis.analyze_market_stocks("KOSPI")
        
        assert any("전체 종목 수: 0개" in str(c) for c in mock_print.call_args_list)
        assert any("조건을 만족하는 종목이 없습니다" in str(c) for c in mock_print.call_args_list)

# ==============================================================================
# 4. modules/account.py 커버리지 (DB 예외)
# ==============================================================================

@patch('modules.account.api.get_today_history')
@patch('modules.account.api.get_overseas_today_history')
@patch('modules.account.db_manager.db.check_trade_exists', return_value=False)
@patch('modules.account.db_manager.db.insert_trade')
def test_sync_today_trades_insert_exception(mock_insert, mock_check, mock_ovs, mock_dom):
    """체결 내역 동기화 중 DB Insert 에러가 발생해도 루프가 죽지 않는지 테스트"""
    mock_dom.return_value = {
        'rt_cd': '0',
        'output1': [{'odno': '1', 'avg_prvs': '50000', 'tot_ccld_qty': '10', 'sll_buy_dvsn_cd': '02', 'pdno': '005930'}]
    }
    mock_ovs.return_value = {'rt_cd': '1'}
    
    # Insert 중 강제 예외 발생
    mock_insert.side_effect = Exception("DB Insert Failed")
    
    # 예외가 발생해도 함수가 튕기지 않고 처리 건수(0)를 반환해야 함
    count = account.sync_today_trades()
    assert count == 0

# ==============================================================================
# 5. modules/backtest.py 커버리지 (결측치 데이터 핸들링)
# ==============================================================================

def test_simulate_strategy_nan_price_handling():
    """시뮬레이터 내 가격 데이터에 NaN이나 0이 포함된 경우 방어 로직"""
    dates = pd.date_range("2023-01-01", periods=5)
    df = pd.DataFrame({
        'date': dates.strftime("%Y%m%d"),
        'close': [10000, np.nan, 0, -1000, 10500], # 불량 데이터 주입
        'high': [10000]*5, 'low': [10000]*5, 'open': [10000]*5, 'volume': [100]*5,
        'EMA20': [10000]*5, 'EMA60': [10000]*5, 'EMA120': [10000]*5,
        'SAR': [9000]*5, 'RSI': [50]*5, 'ADX': [20]*5, 'PLUS_DI': [25]*5, 'MINUS_DI': [15]*5, 'CCI': [0]*5,
        'OBV': [1000]*5, 'OBV_MA': [1000]*5, 'ATR': [100]*5, 'MACD': [0]*5, 'MACD_Signal': [0]*5
    })
    
    # 예외 없이 시뮬레이션이 완료되어야 함
    res = backtest.simulate_strategy(df, df.iloc[0], 10000000, 8.0, 70, False)
    assert res['final_asset'] == 10000000

# ==============================================================================
# 6. modules/settings.py 커버리지 (사용자 입력 검증 엣지 케이스)
# ==============================================================================

@patch('rich.prompt.Prompt.ask')
def test_edit_config_table_invalid_inputs(mock_ask):
    """설정 테이블에서 float, int 타입에 문자를 넣었을 때의 예외 처리"""
    # 시나리오: 1번(float) 항목 선택 -> "abc" 입력(오류 발생, 메뉴로 튕김) 
    # -> 다시 1번 선택 -> "5.0" 정상 입력 -> "q"로 빠져나오기
    mock_ask.side_effect = ["1", "abc", "1", "5.0", "q"]
    
    test_config = {"VAL": 1.0}
    items = [{
        "desc": "Test", "help": "", "name": "VAL", "type": "float",
        "get": lambda: test_config["VAL"], "set": lambda v: test_config.update({"VAL": v})
    }]
    
    with patch('config.console.print') as mock_print:
        settings._edit_config_table("Test", items)
        
        # 잘못된 입력 메시지 출력 확인
        assert any("잘못된 입력입니다" in str(c) for c in mock_print.call_args_list)
        # 결국 5.0으로 세팅 성공해야 함
        assert test_config["VAL"] == 5.0

# ==============================================================================
# 7. modules/theme_analysis.py 커버리지 (네이버 크롤링 예외)
# ==============================================================================

@patch('requests.get')
def test_fetch_naver_themes_timeout(mock_get):
    """네이버 테마 크롤링 시 TimeOut/Connection Error 발생 시 방어 로직"""
    mock_get.side_effect = requests.exceptions.Timeout("Request Timeout")
    
    themes = theme_analysis.fetch_naver_themes()
    assert themes == []

# ==============================================================================
# 8. modules/trading.py 커버리지 (계좌 선택 취소)
# ==============================================================================

@patch('modules.trading.utils.show_menu', return_value='q')
def test_select_account_cancel(mock_menu):
    """주문 시 계좌 선택 화면에서 취소(q)를 눌렀을 때의 반환 커버리지"""
    config.session.is_simulation = False
    config.session.auto_cano = "1234"
    
    cano, acnt, label = trading.select_account()
    assert cano is False
    assert acnt is False
    assert label is False