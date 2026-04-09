import pytest
from unittest.mock import patch, MagicMock
import time
import os

import api
import config
import context
import main
from modules import auto_trade, telegram_bot, trading, account

@pytest.fixture(autouse=True)
def setup_test_env():
    """테스트 환경 초기화"""
    config.TELEGRAM_BOT_TOKEN = "TEST_TOKEN"
    config.TELEGRAM_CHAT_ID = "TEST_ID"
    yield

# ==========================================================
# 1. api.py 커버리지 부스트
# ==========================================================
@patch('api.requests.post')
def test_send_telegram_message_chunking(mock_post):
    """4000자가 넘는 긴 텔레그램 메시지가 청크 단위로 나뉘어 전송되는지 테스트"""
    mock_post.return_value.status_code = 200
    
    # 4000자가 넘는 긴 메시지 생성
    long_msg = "A" * 4005
    api.send_telegram_message(long_msg)
    
    # 청크 분할로 인해 최소 2번 이상 post 요청이 발생해야 함
    assert mock_post.call_count >= 2

@patch('api.get_access_token')
def test_check_and_refresh_token_cooldown(mock_get_token):
    """토큰 갱신 시 60초 쿨타임이 적용되어 중복 API 호출을 방지하는지 테스트"""
    context.TOKEN_EXPIRED = True
    # 방금 전(0.1초 전)에 갱신을 시도했다고 세팅
    context.LAST_TOKEN_REFRESH_ATTEMPT = time.time()
    
    api.check_and_refresh_token_if_expired()
    
    # 쿨타임 때문에 실제 토큰 발급 함수가 호출되지 않아야 함
    mock_get_token.assert_not_called()

@patch('os.path.exists', return_value=True)
@patch('os.listdir', return_value=['cache.sqlite', 'other.txt'])
@patch('os.remove')
def test_clear_yfinance_cache(mock_remove, mock_listdir, mock_exists):
    """yfinance SQLite 캐시 파일 정리 로직 테스트"""
    api.clear_yfinance_cache()
    # .sqlite 확장자를 가진 파일만 삭제 시도해야 함
    mock_remove.assert_called()
    args, _ = mock_remove.call_args
    assert 'cache.sqlite' in args[0]

# ==========================================================
# 2. main.py 커버리지 부스트
# ==========================================================
@patch('main.api.get_access_token', return_value=None)
@patch('main.api.get_real_access_token', return_value=None)
def test_preflight_check_fail_token(mock_real, mock_sim):
    """사전 점검 시 API 토큰 발급에 실패했을 때 False 반환 테스트"""
    config.session.app_key = "VALID_KEY"
    config.session.app_secret = "VALID_SECRET"
    
    with patch('config.console.print'):
        res = main.preflight_check()
        
    assert res is False

def test_flush_input_no_crash():
    """입력 버퍼 플러시 함수가 예외 없이 동작하는지 테스트 (운영체제 무관)"""
    try:
        main.flush_input()
    except Exception as e:
        pytest.fail(f"flush_input() raised an exception: {e}")

# ==========================================================
# 3. telegram_bot.py 커버리지 부스트
# ==========================================================
@patch('modules.db_manager.db.get_trades', return_value=[])
def test_telegram_cmd_stats_empty(mock_get_trades):
    """매매 기록이 없을 때 /stats 명령어 예외 처리 테스트"""
    cmd = telegram_bot.TelegramCommander()
    res = cmd._cmd_stats([])
    assert "기록이 없습니다" in res

@patch('modules.telegram_bot.api.get_yf_fast_info', return_value=None)
@patch('modules.telegram_bot.theme_analysis.generate_morning_briefing', return_value="Mock Briefing")
def test_telegram_execute_briefing(mock_gen, mock_yf):
    """장전 브리핑 내부 실행 워커 테스트"""
    cmd = telegram_bot.TelegramCommander()
    with patch.object(cmd, '_send_reply') as mock_reply:
        cmd._execute_briefing()
        mock_reply.assert_called_with("Mock Briefing")

# ==========================================================
# 4. auto_trade.py & trading.py 커버리지 부스트
# ==========================================================
@patch('modules.auto_trade.api.get_domestic_balance', return_value=(None, None))
@patch('modules.auto_trade.api.get_deposit_balance', return_value=None)
def test_autotrader_initialize_fail(mock_deposit, mock_balance):
    """자동매매 초기화 시 API 응답이 없어 자산 조회에 실패하는 예외 발생 테스트"""
    trader = auto_trade.AutoTrader()
    trader.initialized = False
    with pytest.raises(Exception, match="자산/예수금 조회 실패"):
        trader.initialize()

@patch('modules.trading.utils.show_menu', return_value="1")
@patch('modules.trading.account.fetch_domestic_balance', return_value=([], None))
def test_trading_select_stock_empty(mock_fetch, mock_menu):
    """매도할 보유 종목이 없을 때의 예외 처리 분기 테스트"""
    res = trading.select_stock_from_balance()
    assert res == (None, None, False, None, None)