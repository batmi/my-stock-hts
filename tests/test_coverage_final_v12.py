import pytest
from unittest.mock import patch, MagicMock
import pandas as pd
import config
from core import utils
import api
from core import context
from modules import market, trading, account, manage, db_manager
from modules.telegram_bot import TelegramCommander

@pytest.fixture(autouse=True)
def setup_and_teardown():
    yield
    try:
        db_manager.db.close_connection()
    except: pass

# ==========================================
# 1. utils.py UI 및 메모 유틸리티 테스트 (58% -> Target 80%)
# ==========================================
@patch('core.utils.os.system')
def test_utils_clear_screen(mock_sys):
    with patch.object(config, 'CLEAR_SCREEN_ON_MENU', True):
        context.USER_ACTION_BREADCRUMB = ["Test"]
        utils.clear_screen()
        mock_sys.assert_called()

@patch('builtins.input')
def test_utils_pause(mock_input):
    with patch.object(config, 'CLEAR_SCREEN_ON_MENU', True):
        mock_input.return_value = ""
        utils.pause()
        mock_input.assert_called()

@patch('config.console.print')
def test_utils_print_breadcrumb(mock_print):
    context.USER_ACTION_BREADCRUMB = ["Test1", "Test2"]
    utils.print_breadcrumb()
    assert mock_print.call_count > 0

@patch('rich.prompt.Prompt.ask')
def test_utils_show_menu(mock_ask):
    mock_ask.return_value = "1"
    res = utils.show_menu("Test Menu", [("1", "Opt1", "Opt1 En")])
    assert res == "1"

def test_utils_memo_db():
    """메모 관리 DB 로직 통합 테스트"""
    with patch('core.utils.sqlite3.connect') as mock_connect:
        mock_cursor = MagicMock()
        # utils의 메모 DB 함수는 `with closing(sqlite3.connect()) as conn, conn:` 패턴이라
        # conn = sqlite3.connect() 반환값(mock_connect.return_value)이다. (closing.__enter__가 원본 반환)
        mock_conn = mock_connect.return_value
        mock_conn.cursor.return_value = mock_cursor
        
        # get_stock_memos
        mock_cursor.fetchall.return_value = [{'id': '1', 'name': '삼성전자', 'memo': '메모내용', 'updated_at': '2023-01-01'}]
        res = utils.get_stock_memos('005930')
        assert len(res) == 1
        
        # add_stock_memo
        mock_cursor.fetchone.return_value = [1]
        assert utils.add_stock_memo('005930', '삼성전자', '새로운 메모') is True
        
        # delete_stock_memo_by_id
        assert utils.delete_stock_memo_by_id(1) is True
        
        # delete_all_stock_memos
        assert utils.delete_all_stock_memos('005930') is True
        
        # get_memo_codes
        mock_cursor.fetchall.return_value = [('005930',)]
        codes = utils.get_memo_codes()
        assert '005930' in codes

# ==========================================
# 2. market.py 테스트 보완 (56% -> Target 80%)
# ==========================================
@patch('modules.market.api.fetch_yfinance_data')
@patch('modules.market.api.get_domestic_index_chart')
@patch('modules.market.yf.Tickers')
def test_market_all_groups_render(mock_tickers, mock_dom, mock_yf):
    """시장 지수 전 그룹(암호화폐, 환율 등) 렌더링 테스트"""
    # 국내 데이터
    mock_dom.return_value = pd.DataFrame({
        'close': [100.0]*20, 'open': [100.0]*20, 'high': [110.0]*20, 'low': [90.0]*20, 'volume': [1000.0]*20,
        'date': pd.date_range('2023-01-01', periods=20)
    })
    
    # 해외/기타 데이터 (MultiIndex)
    dates = pd.date_range('2023-01-01', periods=20)
    tickers = ['BTC-USD', 'ETH-USD', '^GSPC', 'KRW=X', 'CL=F', '^TNX']
    cols = pd.MultiIndex.from_product([tickers, ['Close', 'Open', 'High', 'Low', 'Volume']])
    mock_yf.return_value = pd.DataFrame(100.0, index=dates, columns=cols)
    
    # fast_info
    mock_ticker = MagicMock()
    mock_ticker.fast_info.last_price = 100.0
    mock_ticker.fast_info.regular_market_previous_close = 90.0
    mock_tickers.return_value.tickers = {t: mock_ticker for t in tickers}

    with patch('config.console.print') as mock_print:
        market._show_market_indices_core(["비트코인", "이더리움", "달러환율", "WTI 원유", "^GSPC"])
        assert mock_print.call_count > 0

# ==========================================
# 3. trading.py 테스트 보완 (57% -> Target 80%)
# ==========================================
@patch('rich.prompt.Prompt.ask')
@patch('modules.trading.api.place_order')
@patch('modules.trading.select_stock_from_balance')
@patch('modules.trading.api.fetch_sellable_quantity')
@patch('modules.trading.api.get_current_price')
def test_trading_send_order_sell_market(mock_price, mock_qty, mock_select, mock_place, mock_ask):
    """매도 주문 (시장가, 잔고에서 선택) 흐름 테스트"""
    # 수량(10) -> 0(시장가) -> y(확인)
    mock_ask.side_effect = ["10", "0", "y"]
    mock_select.return_value = ("005930", "삼성전자", False, "KRX", {'qty': 10})
    mock_qty.return_value = 10
    mock_price.return_value = 60000
    mock_place.return_value = {'rt_cd': '0', 'output': {'ODNO': '12345'}}
    
    with patch('config.console.print'):
        trading.send_order('sell')
        
    mock_place.assert_called()

# ==========================================
# 4. telegram_bot.py 직접 매매 테스트 (63% -> Target 80%)
# ==========================================
def test_telegram_cmd_scan():
    """/scan 명령어 서브 분기 (안전 호출) 테스트

    [Fix] bot_executor.submit을 반드시 mock해야 한다. _cmd_scan은 실제 스캔(_execute_scan)을
    스레드 풀에 던지고 즉시 반환하는데, 그 작업은 with 블록이 끝난 뒤(=_send_reply 패치가
    풀린 뒤) 실행된다. 그러면 진짜 TradingView 스크리너를 네트워크로 조회하고 그 결과를
    운영자 텔레그램으로 실제 발송한다(실측: 테스트 1회 실행마다 시장 스캔 결과가 수신됨).
    이 테스트의 검증 대상은 '명령이 실행기로 넘어가는가'이므로 submit만 확인하면 충분하다.
    """
    bot = TelegramCommander()
    if hasattr(bot, '_cmd_scan'):
        with patch('modules.telegram_bot.bot_executor.submit') as mock_submit, \
             patch.object(bot, '_send_reply') as mock_reply:
            bot._cmd_scan(["KOSPI"])
            mock_submit.assert_called_once()      # 실제 스캔은 실행되지 않는다
            mock_reply.assert_called_once()       # 대기 안내만 즉시 회신