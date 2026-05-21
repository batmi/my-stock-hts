import pytest
from unittest.mock import patch, MagicMock, ANY
import pandas as pd
import api
import config
from modules import analysis, auto_trade, market, account
from datetime import datetime

# --- api.py coverage ---
@patch('api.fetch_yfinance_data')
def test_get_chart_data_index(mock_fetch):
    """지수(Index) 차트 데이터 조회 테스트"""
    # 최근 날짜로 설정하여 필터링 통과 보장
    mock_df = pd.DataFrame({
        'Date': pd.date_range(end=datetime.now(), periods=10),
        'Close': [100] * 10, 'High': [110] * 10, 'Low': [90] * 10, 'Open': [100] * 10, 'Volume': [1000] * 10
    })
    mock_fetch.return_value = mock_df
    df = api.get_chart_data('^KS11')
    assert not df.empty
    assert 'close' in df.columns

@patch('api.session.get')
def test_get_stock_name_by_code_fail(mock_get):
    """종목명 조회 실패 시 코드 반환 테스트"""
    mock_get.side_effect = Exception("Network Error")
    name = api.get_stock_name_by_code("123456", False)
    assert name == "123456"

@patch.dict('sys.modules', {'tradingview_screener': None})
@patch('api.yf.Ticker')
def test_get_stock_name_by_code_overseas_fail(mock_ticker):
    """해외 종목명 조회 실패 테스트"""
    mock_ticker.side_effect = Exception("YF Error")
    name = api.get_stock_name_by_code("AAPL", True)
    assert name == "AAPL"

@patch('time.sleep')
@patch('requests.post')
def test_send_telegram_message_fail(mock_post, mock_sleep):
    """텔레그램 전송 최종 실패 테스트"""
    config.TELEGRAM_BOT_TOKEN = "TEST"
    config.TELEGRAM_CHAT_ID = "TEST"
    mock_post.side_effect = Exception("Connection Error")
    api.send_telegram_message("test", sync=True)
    assert mock_post.call_count == 3

# --- modules/analysis.py coverage ---
def test_calculate_score_partial_data():
    """일부 지표 누락 시 점수 계산 테스트"""
    score, details = analysis.calculate_score(10000, None, None, None, None, None, None, None, False)
    assert score == 0
    assert len(details) == 0

def test_classify_stock_state_insufficient():
    """데이터 부족 시 상태 분류 테스트"""
    state, color, reason = analysis.classify_stock_state(None, None, None, None, None, None, None, None, None, None)
    assert state == "-"
    assert "데이터 부족" in reason

# --- modules/auto_trade.py coverage ---
def test_risk_manager_simple_allocation():
    """단순 자산 배분 (변동성 타겟팅 미사용) 테스트"""
    trader = MagicMock()
    trader.initial_asset = 10_000_000
    rm = auto_trade.RiskManager(trader)
    with patch('config.USE_VOLATILITY_TARGETING', False):
        amt = rm.allocate_budget(5_000_000, 0.1)
        assert amt == 1_000_000

def test_order_manager_unknown_update():
    """알 수 없는 주문 상태 업데이트 무시 테스트"""
    trader = MagicMock()
    om = auto_trade.OrderManager(trader)
    om.update_order_status("005930", "99999", "FILLED") # Should not raise error

@patch('sqlite3.connect')
def test_ensure_db_weights_column(mock_connect):
    """DB 컬럼 추가 로직 테스트"""
    mock_conn = MagicMock()
    mock_cursor = MagicMock()
    mock_connect.return_value = mock_conn
    mock_conn.cursor.return_value = mock_cursor
    
    mock_cursor.fetchone.return_value = ('stock_strategies',) # Table exists
    mock_cursor.fetchall.return_value = [('cid', 'code', 'type', 0, None, 1)] # Column missing
    
    auto_trade._ensure_db_weights_column()
    assert any("ALTER TABLE" in str(call) for call in mock_cursor.execute.call_args_list)

# --- modules/market.py coverage ---
@patch('modules.market.api.fetch_yfinance_data')
def test_show_market_indices_partial_fail(mock_fetch):
    """일부 지수 데이터 조회 실패 시에도 동작 확인"""
    mock_fetch.side_effect = [pd.DataFrame(), pd.DataFrame()]
    # [수정] 사용자 입력을 모킹 (메뉴 선택 '8', 재시도 'n', 메인화면 'q')
    with patch('rich.prompt.Prompt.ask', side_effect=["8", "n", "n", "q"]):
        with patch('config.console.print') as mock_print:
            market.show_market_indices()
            assert mock_print.call_count > 0

# --- modules/account.py ---
@patch('api.get_today_profit_summary')
def test_fetch_today_profit_summary_exception(mock_api):
    """수익 현황 조회 예외 처리 테스트"""
    mock_api.side_effect = Exception("API Error")
    summary = account.fetch_today_profit_summary()
    assert summary['realized_pl'] == 0

@patch('api.get_today_history')
def test_fetch_today_history_exception(mock_api):
    """체결 내역 조회 예외 처리 테스트"""
    mock_api.side_effect = Exception("API Error")
    summary = account.fetch_today_history()
    assert summary['buy_total'] == 0