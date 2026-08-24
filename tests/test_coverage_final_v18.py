import pytest
from unittest.mock import patch, MagicMock
from datetime import datetime, timedelta
import pandas as pd

import config
from core import context
from core import session
from core import utils
import api
from modules import account, telegram_bot, theme_analysis, db_manager

@pytest.fixture(autouse=True)
def setup_env():
    yield
    try:
        db_manager.db.close_connection()
    except: pass

# ==========================================================
# 1. session.py 커버리지 (환경 변수 적용 및 토큰 로직)
# ==========================================================
def test_session_initialize_with_auto_account(monkeypatch):
    monkeypatch.setenv("AUTO_ACC_NUM", "87654321-01")
    monkeypatch.setenv("AUTO_APP_KEY", "auto_key")
    monkeypatch.setenv("AUTO_APP_SECRET", "auto_secret")
    
    s = session.SessionManager()
    with patch('rich.prompt.Prompt.ask', return_value='2'), \
         patch('config.console.print'):
        s.initialize()
        
    assert s.auto_cano == "87654321"
    assert s.auto_acnt_prdt_cd == "01"

def test_session_token_validity():
    s = session.SessionManager()
    # 1. 만료일시가 과거
    info1 = {'access_token': 'tok1', 'token_expired': '2020-01-01 12:00:00'}
    assert s._check_token_validity(info1) is False
    
    # 2. 만료일시가 미래
    future_dt = (datetime.now() + timedelta(hours=1)).strftime("%Y-%m-%d %H:%M:%S")
    info2 = {'access_token': 'tok2', 'token_expired': future_dt}
    assert s._check_token_validity(info2) is True

def test_session_is_token_recently_issued():
    s = session.SessionManager()
    # 발행일시가 10초 전
    recent_dt = (datetime.now() - timedelta(seconds=10)).strftime("%Y-%m-%d %H:%M:%S")
    
    with patch.object(s, '_load_token_cache', return_value={'TEST': {'issued_at': recent_dt}}):
        assert s.is_token_recently_issued('TEST', 60) is True
        assert s.is_token_recently_issued('TEST', 5) is False

# ==========================================================
# 2. utils.py 커버리지 (예외 및 분기 처리)
# ==========================================================
@pytest.fixture
def restore_account_context():
    """[격리] 이 파일은 config.session과 trade_context를 직접 건드린다.

    use_auto_account(threading.local)와 is_simulation을 되돌리지 않고 끝내면,
    같은 워커에서 뒤에 도는 테스트의 insert_trade가 자동계좌로 기록된다
    (db_manager.insert_trade가 이 플래그를 보고 acc_no를 정한다).
    실제로 test_journal_sync의 멱등키 계좌가 뒤바뀌는 것이 관측됐다.
    """
    saved = (getattr(context.trade_context, 'use_auto_account', False),
             config.session.is_simulation,
             getattr(config.session, 'auto_cano', ''),
             getattr(config.session, 'auto_acnt_prdt_cd', ''))
    yield
    (context.trade_context.use_auto_account,
     config.session.is_simulation,
     config.session.auto_cano,
     config.session.auto_acnt_prdt_cd) = saved


@patch('api.get_current_token', return_value="test_token")
def test_get_common_headers(mock_token, restore_account_context):
    # 1. 실전 & Auto 계좌 사용
    config.session.is_simulation = False
    context.trade_context.use_auto_account = True
    config.session.auto_app_key = "auto_k"
    config.session.auto_app_secret = "auto_s"
    
    h1 = utils.get_common_headers("TR123")
    assert h1['appKey'] == "auto_k"
    
    # 2. 실전 & Main 계좌 사용
    context.trade_context.use_auto_account = False
    config.session.real_app_key = "real_k"
    config.session.real_app_secret = "real_s"
    
    h2 = utils.get_common_headers("TR123")
    assert h2['appKey'] == "real_k"

@patch.dict('sys.modules', {'tradingview_screener': None})
@patch('core.utils.yf.Ticker')
def test_get_exchange_rate_fallback(mock_ticker):
    """TradingView 없을 때 yfinance fallback 및 에러 무시 테스트"""
    # yfinance 정상
    mock_ticker.return_value.fast_info.last_price = 1350.5
    rate = utils.get_exchange_rate()
    assert rate == 1350.5
    
    # yfinance 예외
    mock_ticker.return_value.fast_info.last_price = None # AttributeError 유발
    mock_ticker.side_effect = Exception("YF Error")
    
    with patch('config.SCREEN_DEBUG_LEVEL', 'DEBUG'), patch('config.console.print'):
        rate2 = utils.get_exchange_rate()
        assert rate2 == config.DEFAULT_EXCHANGE_RATE

@patch('core.utils.sqlite3.connect', side_effect=Exception("DB Error"))
def test_utils_memo_db_errors(mock_connect):
    """메모 DB 함수 예외 처리 커버리지"""
    assert utils.get_stock_memos("005930") == []
    assert utils.get_all_stock_memos() == []
    assert utils.add_stock_memo("005930", "삼성", "메모") is False
    assert utils.update_stock_memo(1, "수정") is False

# ==========================================================
# 3. api.py 커버리지 (통신/파서 예외 분기)
# ==========================================================
def test_tls_adapter_init_poolmanager():
    """urllib3 버전에 따른 TLSAdapter 분기 커버리지"""
    from api import TLSAdapter
    adapter = TLSAdapter()
    
    with patch('api.urllib3.__version__', '1.26.0'):
        adapter.init_poolmanager(10, 10)
        assert hasattr(adapter, 'poolmanager')
        
    with patch('api.urllib3.__version__', '2.0.0'):
        adapter.init_poolmanager(10, 10)
        assert hasattr(adapter, 'poolmanager')

def test_get_telegram_footer_auto(restore_account_context):
    config.session.is_simulation = False
    context.trade_context.use_auto_account = True
    config.session.auto_cano = "9999"
    config.TELEGRAM_INSTANCE_NAME = "TEST"
    config.TELEGRAM_BOT_TOKEN = "test_token"  # footer는 토큰이 있어야 생성됨(테스트 자립성 확보)

    footer = api._get_telegram_footer()
    assert "자동 9999" in footer

# ==========================================================
# 4. telegram_bot.py 커버리지 (에러 모니터링 및 상태 조회 실패)
# ==========================================================

@patch('modules.telegram_bot.db_manager.db.get_trades', return_value=[])
def test_cmd_stats_empty(mock_trades):
    cmd = telegram_bot.TelegramCommander()
    res = cmd._cmd_stats([])
    assert "매매 기록이 없습니다" in res
    
    # 종목 검색 실패 분기
    # 매매 기록이 비어있으면 검색 전에 종료되므로 가짜 데이터를 하나 넣어줌
    mock_trades.return_value = [{'type': 'buy', 'code': '005930', 'name': 'Samsung', 'qty': 10, 'price': 100, 'time': '2023-01-01', 'odno': '1', 'order_status': '체결'}]
    
    # [추가] 앞선 조회로 인해 메모리에 빈 리스트가 캐싱되어 있으므로 캐시 강제 초기화
    cmd._trade_cache.clear()

    with patch.object(cmd, '_resolve_stock', return_value=(None, None, False)):
        res2 = cmd._cmd_stats(["없는종목"])
        assert "찾을 수 없습니다" in res2