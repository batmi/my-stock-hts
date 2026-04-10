import pytest
from unittest.mock import patch, MagicMock
import pandas as pd
import numpy as np
from datetime import datetime, timedelta
import threading

import config
import context
import api
from modules import market, auto_trade, analysis, account, settings, theme_analysis, db_manager

@pytest.fixture(autouse=True)
def cleanup_db_connection():
    yield
    try:
        db_manager.db.close_connection()
    except: pass

# ==============================================================================
# 1. modules/auto_trade.py - 리스크 관리 및 미체결 주문 처리 엣지 케이스
# ==============================================================================
@patch('modules.auto_trade.api.send_telegram_message')
def test_risk_manager_emergency_stop(mock_tg):
    """일일 손실 한도 도달 시 비상 정지(Kill Switch) 작동 커버리지"""
    trader = auto_trade.AutoTrader()
    trader.initial_asset = 10000000
    trader.is_running = True
    
    config.SYSTEM_DAILY_LOSS_LIMIT = 10.0
    
    with patch.object(trader, 'stop') as mock_stop:
        # 20% 손실(8백만원) 발생 시뮬레이션
        trader.risk_manager.check_loss_limit(8000000)
        
        mock_stop.assert_called_once_with(use_status=False)
        mock_tg.assert_called()
        assert "비상 정지" in mock_tg.call_args[0][0]

@patch('modules.auto_trade.api.get_unfilled_orders', return_value=[])
@patch('modules.auto_trade.api.revise_cancel_order', return_value={'rt_cd': '0', 'output': {'ODNO': 'CANC123'}})
def test_order_manager_simulation_force_cancel(mock_cancel, mock_unfilled):
    """모의투자 시 API 누락으로 인해 DB 시간 기준으로 강제 취소되는 로직 커버리지"""
    config.session.is_simulation = True
    trader = auto_trade.AutoTrader()
    # 주문이 아직 진행 중인 상태 모킹
    trader.order_manager.pending_orders = {"005930": {"12345": auto_trade.OrderStatus.ORDER_SENT}}
    
    # 주문 발생 시간을 현재 시간보다 과거(취소 기준 시간 120초 초과)로 설정
    old_time = (datetime.now() - timedelta(seconds=150)).strftime("%Y-%m-%d %H:%M:%S")
    mock_trade = {'name': '삼성전자', 'qty': '10', 'price': '60000', 'type': 'buy', 'time': old_time}
    
    with patch('modules.db_manager.db.get_trade_by_odno', return_value=mock_trade), \
         patch('modules.db_manager.db.insert_trade'):
        trader.order_manager.manage_unfilled_orders()
        
        mock_cancel.assert_called_once()

# ==============================================================================
# 2. modules/analysis.py - AI 분석 예외 및 입력 중단 흐름
# ==============================================================================
@patch('rich.prompt.Prompt.ask', side_effect=["5", "invalid_code"])
def test_diagnose_stock_invalid_input(mock_ask):
    """직접입력에서 유효하지 않은 종목코드 검색 시 거부 커버리지"""
    config.session.stock_data = {"stocks_kr": [], "etfs_kr": [], "stocks_us": [], "etfs_us": []}
    with patch('config.console.print') as mock_print:
        res = analysis.diagnose_stock()
        assert res is False
        assert any("유효하지 않은" in str(call) for call in mock_print.call_args_list)

@patch('modules.analysis.api.get_chart_data')
@patch('rich.prompt.Prompt.ask', side_effect=["y", "y"]) # 차트분석(y) -> AI분석(y)
@patch('modules.theme_analysis.analyze_stock_with_gemini', return_value="AI Report Mock")
def test_diagnose_stock_with_ai(mock_gemini, mock_ask, mock_chart):
    """정상적인 종목 분석 후 AI 심층 진단까지 도달하는지 확인"""
    dates = pd.date_range('2023-01-01', periods=20)
    df = pd.DataFrame({
        'date': dates.strftime("%Y%m%d"),
        'close': [1000.0]*20, 'open': [1000.0]*20, 'high': [1000.0]*20, 'low': [1000.0]*20, 'volume': [1000]*20
    }, index=dates)
    mock_chart.return_value = df
    
    with patch('config.console.print'):
        analysis.diagnose_stock("005930", "삼성전자", False)
        assert mock_gemini.called

# ==============================================================================
# 3. modules/market.py - 데이터 파싱 에러
# ==============================================================================
@patch('modules.market.api.get_yf_fast_info', return_value=None)
def test_process_index_worker_yf_error(mock_fast_info):
    """yfinance 빈 데이터가 들어올 때의 Error Status 반환 검증"""
    res = market._process_index_worker("S&P500", "^GSPC", pd.DataFrame(), pd.DataFrame())
    assert res['status'] == 'failed'

# ==============================================================================
# 4. modules/settings.py - Preset 엣지 및 초기화 취소
# ==============================================================================
def test_apply_strategy_preset_invalid():
    """잘못된 프리셋 입력에 대한 방어 로직"""
    res = settings.apply_strategy_preset("invalid")
    assert "알 수 없는 프리셋" in res

@patch('rich.prompt.Prompt.ask', return_value="n")
def test_reset_to_default_cancel(mock_ask):
    """초기화 여부 묻는 프롬프트에서 취소 선택"""
    assert settings.reset_to_default(interactive=True) is False

# ==============================================================================
# 5. modules/theme_analysis.py & modules/db_manager.py - 인프라 예외
# ==============================================================================
@patch('modules.theme_analysis.genai.GenerativeModel')
def test_generate_portfolio_diagnosis_rate_limit(mock_model):
    """포트폴리오 분석 중 Gemini Rate Limit 등 API 예외 발생 시 반환 문자열 커버리지"""
    mock_model.return_value.generate_content.side_effect = Exception("429 Quota Exceeded")
    config.GEMINI_API_KEY = "dummy_key"
    res = theme_analysis.generate_portfolio_diagnosis("Mock Portfolio")
    assert "Rate Limit" in res

@patch('modules.db_manager.sqlite3.connect', side_effect=Exception("Vacuum Lock Error"))
def test_db_manager_vacuum_exception(mock_connect):
    """DB 최적화(VACUUM) 중 SQLite Lock 발생 시 조용히 넘기기 커버리지"""
    config.SCREEN_DEBUG_LEVEL = "DEBUG"
    with patch('config.console.print') as mock_print:
        db_manager.db.run_vacuum()
        assert any("VACUUM Error" in str(c) for c in mock_print.call_args_list)
    config.SCREEN_DEBUG_LEVEL = "OFF"