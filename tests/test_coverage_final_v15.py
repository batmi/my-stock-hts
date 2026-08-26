import pytest
from unittest.mock import patch, MagicMock, ANY
import pandas as pd
import numpy as np
from datetime import datetime

import api
import config
from core import utils
from modules import analysis, auto_trade, market, theme_analysis, db_manager, manage, account
from modules.telegram_bot import TelegramCommander

@pytest.fixture(autouse=True)
def cleanup_db_connection():
    yield
    try:
        db_manager.db.close_connection()
    except: pass

@pytest.fixture(autouse=True)
def reset_singletons():
    """싱글톤 객체 오염 방지"""
    TelegramCommander._instance = None
    auto_trade.AutoTrader._instance = None
    yield

# ==========================================================
# 1. api.py 커버리지 보완
# ==========================================================
@pytest.mark.real_yfinance
@patch('api.yf.download')
@patch('api.clear_yfinance_cache')
@patch('time.sleep')
def test_fetch_yfinance_data_db_lock_retry(mock_sleep, mock_clear, mock_dl):
    """yfinance DB Lock 발생 시 캐시 삭제 및 재시도 로직 커버리지"""
    # 첫 번째: DB 락 에러, 두 번째: 정상 데이터 반환
    mock_dl.side_effect = [Exception("database is locked"), pd.DataFrame({'close': [100]})]
    
    df = api.fetch_yfinance_data("AAPL")
    
    assert not df.empty
    assert mock_dl.call_count == 2
    assert mock_clear.call_count == 1

@patch.object(api.session, 'get')
@patch('api.get_real_access_token', return_value="NEW_TOKEN")
def test_call_api_token_refresh_and_retry(mock_token, mock_get):
    """call_api 내에서 토큰 만료 에러 감지 후 갱신 및 재요청 커버리지"""
    
    # 첫 번째: 토큰 만료 예외, 두 번째: 성공
    mock_get.side_effect = [
        Exception("Token Expired"), 
        MagicMock(status_code=200, json=lambda: {'rt_cd': '0', 'output': {}})
    ]
    
    with patch('api.get_current_token', return_value="OLD_TOKEN"):
        # 유효한 URL과 카테고리(inquiry/balance)를 넘겨서 정상 로직을 타게 함
        res = api.call_api("test_url", "domestic", "inquiry", "balance", method="GET")
        
        assert res['rt_cd'] == '0'
        assert mock_get.call_count == 2
        mock_token.assert_called_once()

# ==========================================================
# 2. modules/telegram_bot.py 커버리지 보완
# ==========================================================
def test_telegram_cmd_market_invalid_groups():
    """/market 명령어에서 유효하지 않은 그룹 키만 입력되었을 때 예외 반환 커버리지"""
    cmd = TelegramCommander()
    res = cmd._cmd_market(["z", "x"]) # 존재하지 않는 그룹
    assert "잘못된 그룹 키" in res
    
@patch('modules.auto_trade.db_manager.db.get_trades')
def test_telegram_cmd_history_no_data(mock_trades):
    """/history 명령어 조회 데이터가 없을 때 및 파라미터 없을 때(전체) 분기 커버리지"""
    cmd = TelegramCommander()
    mock_trades.return_value = []
    
    # 파라미터 없이 조회 (기본값)
    res = cmd._cmd_history([])
    assert "거래 내역이 없습니다" in res

@patch('modules.auto_trade.api.get_domestic_balance')
def test_telegram_cmd_holdings_empty(mock_bal):
    """/holdings 명령어 보유 잔고가 없을 때 출력 포맷 커버리지"""
    cmd = TelegramCommander()
    # 잔고가 0인 데이터 모킹
    mock_bal.return_value = ([{'prdt_name': 'Samsung', 'hldg_qty': '0'}], [])
    
    res = cmd._cmd_holdings([])
    assert "없음" in res

@patch('modules.auto_trade.api.get_deposit_balance', return_value=None)
def test_telegram_cmd_balance_fail(mock_dep):
    """/balance 명령어 자산 조회 실패 예외 커버리지"""
    cmd = TelegramCommander()
    with patch('modules.telegram_bot.account.get_asset_status_data', return_value=None):
        res = cmd._cmd_balance([])
        assert "조회 실패" in res or "오류" in res

# ==========================================================
# 3. modules/auto_trade.py 커버리지 보완
# ==========================================================
@patch('modules.auto_trade.api.get_current_price', return_value=50000)
def test_simulation_fill_market_price(mock_cp):
    """모의투자 잔고 기반 체결 - 시장가(0) 매도 주문 체결 시 현재가 대체 로직 커버리지"""
    monitor = auto_trade.ConclusionMonitor()
    
    trade = {'type': '매도', 'price': 0, 'qty': 10, 'name': 'Samsung'}
    
    with patch('modules.auto_trade.db_manager.db.insert_trade') as mock_insert, \
         patch('modules.auto_trade.api.send_telegram_message'):
             
        monitor._handle_simulation_fill(MagicMock(), trade, "12345", "005930", 10, "잔고 확인")
        
        mock_insert.assert_called_once()
        args, kwargs = mock_insert.call_args
        assert args[4] == 50000 # price가 현재가(50000)로 대체되었는지 확인

@patch('modules.auto_trade.api.get_domestic_balance')
def test_log_current_holdings_empty(mock_bal):
    """현재 보유 종목 로깅 시 잔고가 없을 때 분기 커버리지"""
    trader = auto_trade.AutoTrader()
    mock_bal.return_value = ([], [])
    
    with patch.object(trader, 'log') as mock_log:
        trader.log_current_holdings()
        assert any("보유 종목 없음" in str(c) for c in mock_log.call_args_list)

@patch('modules.auto_trade.analysis.get_domestic_index_data')
def test_update_market_indices_status_data_lack(mock_get_index):
    """시장 국면 업데이트 시 데이터가 부족할 때(ma_period 미만) 방어 로직 커버리지"""
    trader = auto_trade.AutoTrader()
    # 10개만 반환 (기본 MA 20 미만)
    mock_get_index.return_value = pd.DataFrame({'close': [100]*10})
    
    with patch('modules.auto_trade.api.send_telegram_message'):
        trader._update_market_indices_status()

    # [추세추종] 데이터 부족 = '시장 방향 판단 불가' → 신규 매수는 fail-closed로 보류한다.
    #  ("대체 무슨 일이 벌어지고 있는지 모르겠다면, 아무것도 하지 마라")
    assert trader.market_index_status["KOSPI"]["is_healthy"] is False
    assert trader.market_index_status["KOSPI"]["unknown"] is True
    assert trader.market_index_status["KOSPI"]["current"] == 0

# ==========================================================
# 4. modules/theme_analysis.py 커버리지 보완
# ==========================================================
@patch('api.get_current_price_data', return_value={'rt_cd': '0', 'output': {'stck_prpr': '60000'}})
@patch('modules.analysis.print_table')
@patch('rich.prompt.Prompt.ask')
@patch('api.get_chart_data')
@patch('modules.theme_analysis.analyze_stock_with_gemini')
def test_analyze_stock_ui_direct_input(mock_gemini, mock_chart, mock_ask, mock_print_table, mock_cp):
    """개별 종목 AI 심층 진단 UI - 직접 입력 및 차트 분석 연동 커버리지"""
    # 5(직접입력) -> 005930 -> y(진행)
    mock_ask.side_effect = ["5", "005930", "y"]
    
    mock_chart.return_value = pd.DataFrame({
        'close': [60000]*20, 'high': [60000]*20, 'low': [60000]*20, 'open': [60000]*20, 'volume': [100]*20
    })
    mock_gemini.return_value = "Mock AI Report"
    
    with patch('api.get_stock_name_by_code', return_value="삼성전자"), \
         patch('modules.analysis.classify_stock_state', return_value=("매수", "[red]", "이유")), \
         patch('modules.analysis.calculate_score', return_value=(9.0, [])), \
         patch('core.indicators.calculate_indicators', return_value={'ema_20': 100, 'ema_60': 100, 'ema_120': 100, 'psar': 90, 'rsi': 50, 'adx': 20, 'cci': 0}), \
         patch('config.console.print'):
             
        theme_analysis._analyze_stock_ui()
        
        mock_gemini.assert_called_once()

# ==========================================================
# 5. modules/manage.py 커버리지 보완
# ==========================================================
@patch('rich.prompt.Prompt.ask')
def test_manage_specific_stock_memos_invalid_idx(mock_ask):
    """메모 관리 상세 메뉴에서 잘못된 번호 입력 후 루프 탈출 커버리지"""
    # 잘못된 번호(99) 입력 -> 뒤로가기(b)
    mock_ask.side_effect = ["99", "b"]
    
    memos = [{'id': 1, 'memo': 'Test', 'updated_at': '2023-01-01'}]
    with patch('modules.manage.utils.get_stock_memos', return_value=memos), \
         patch('config.console.print'):
        
        res = manage._manage_specific_stock_memos("005930", "삼성전자", mode="view")
        assert res == "quit_to_menu"

# ==========================================================
# 6. modules/market.py 커버리지 보완
# ==========================================================
@patch('modules.market.utils.pause') # 무한 대기 방지
@patch('modules.market.api.fetch_yfinance_data')
@patch('api.get_yf_fast_info', return_value=None) # 실제 네트워크 통신 방지 (지연 원인)
@patch('modules.analysis.get_domestic_index_data', return_value=pd.DataFrame())
def test_show_market_indices_keyboard_interrupt(mock_dom, mock_fi, mock_fetch, mock_pause):
    """시장 지수 수집 중 KeyboardInterrupt 발생 시 예외 전파 커버리지"""
    # 데이터를 비워두어 재시도 여부를 묻는 Prompt.ask가 호출되도록 유도
    mock_fetch.return_value = pd.DataFrame()
    
    with patch('rich.prompt.Prompt.ask', side_effect=["9", KeyboardInterrupt(), "q"]): # 전체조회 후 재시도 프롬프트에서 Ctrl+C, 이후 메인메뉴 종료
        with patch('config.console.print') as mock_print:
            market.show_market_indices()
            
            # 취소 메시지 출력 확인
            assert any("취소" in str(c) for c in mock_print.call_args_list)

# ==========================================================
# 7. modules/account.py 커버리지 보완
# ==========================================================
@patch('modules.account.api.get_today_profit_summary')
@patch('modules.account.db_manager.db.get_trades')
def test_get_asset_status_data_db_fallback(mock_trades, mock_profit):
    """기간별 손익 조회 누락 시 DB에서 계산하는 Fallback 로직 커버리지"""
    # API는 0을 반환
    mock_profit.return_value = {'rt_cd': '0', 'output2': [{'thdt_buy_amt': '0', 'thdt_sll_amt': '0', 'rlzt_pfls': '0'}]}
    
    # DB에는 체결된 수익 기록이 존재
    mock_trades.return_value = [
        {'type': '매도', 'price': 10000, 'qty': 10, 'profit_amt': 5000, 'reason': '체결 확인 (정상)'}
    ]
    
    with patch('modules.account.api.get_domestic_balance', return_value=([], [])), \
         patch('modules.account.api.get_deposit_balance', return_value={'d2_deposit': 1000000, 'deposit': 1000000, 'foreign_deposit': 0, 'withdraw': 1000000}), \
         patch('modules.account.fetch_today_history', return_value={'buy_total': 0, 'sell_total': 0}):
             
        res = account.get_asset_status_data("1234", "01")
        
        # DB에서 계산된 값(5000)이 fallback되어 반영되었는지 확인
        assert res['realized_pl'] == 5000
        assert res['sell_today'] == 100000
