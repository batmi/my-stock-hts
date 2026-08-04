"""관심종목 리스트의 단축 명령 줄과 /news_ 단축어.

리스트에서 종목을 보고 바로 뉴스로 넘어갈 수 있어야 한다. 순서는 운영자가 실제로
쓰는 흐름(신호 → 차트 → 상세분석 → 뉴스)을 따른다.
"""
from unittest.mock import MagicMock, patch

import pytest

import config
from modules.telegram_bot import TelegramCommander


@pytest.fixture
def commander():
    TelegramCommander._instance = None
    cmd = TelegramCommander()
    cmd.trader = MagicMock()
    return cmd


@pytest.fixture
def watchlist(monkeypatch):
    """국내주식 2종목만 등록된 관심목록."""
    monkeypatch.setitem(config.session.stock_data, 'stocks_kr',
                        [{'code': '005930', 'name': '삼성전자'},
                         {'code': '000660', 'name': 'SK하이닉스'}])
    monkeypatch.setitem(config.session.stock_data, 'etfs_kr', [])


def _monitoring_msg(commander):
    with patch('modules.telegram_bot.auto_trade.get_restricted_stocks', return_value={}), \
         patch('modules.telegram_bot.db_manager.db.get_all_stock_strategies', return_value=[]), \
         patch('modules.telegram_bot.context.get_stock_state', return_value=None):
        return commander._get_monitoring_list()


def test_watchlist_includes_news_link(commander, watchlist):
    """각 종목 줄에 /news_<코드> 가 포함된다."""
    msg = _monitoring_msg(commander)
    assert "/news_005930" in msg
    assert "/news_000660" in msg


def test_watchlist_command_order(commander, watchlist):
    """순서는 signal → chart → analyze → news 다."""
    msg = _monitoring_msg(commander)
    assert "/signal_005930  /chart_005930  /analyze_005930  /news_005930" in msg


def test_watchlist_code_stays_parenthesized(commander, watchlist):
    """코드는 괄호 형태를 유지해야 한다 — 트레이딩뷰 링크가 이 패턴에 붙는다.

    telegram_notify 가 발송 직전 '(005930)' 정규식을 <a href=...> 로 바꾼다.
    괄호를 없애면 리스트에서 링크가 통째로 사라진다.
    """
    msg = _monitoring_msg(commander)
    assert "(005930)" in msg


@patch('api.send_telegram_message')
def test_news_shortcut_command(mock_send, commander):
    """/news_005930 이 /news 005930 으로 해석된다."""
    config.TELEGRAM_CHAT_ID = "12345"
    with patch.object(commander, '_cmd_news') as mock_news, \
         patch.dict(commander.command_handlers, {'/news': mock_news}), \
         patch.object(commander, '_send_reply'), \
         patch('modules.telegram_bot.bot_executor.submit', side_effect=lambda f, *a: f(*a)):
        commander._handle_message({'chat': {'id': 12345}, 'text': '/news_005930'})

    mock_news.assert_called_with(['005930'])


@patch('api.send_telegram_message')
def test_plain_news_command_still_works(mock_send, commander):
    """대조군 — 기존 '/news 삼성전자' 형태가 깨지지 않는다."""
    config.TELEGRAM_CHAT_ID = "12345"
    with patch.object(commander, '_cmd_news') as mock_news, \
         patch.dict(commander.command_handlers, {'/news': mock_news}), \
         patch.object(commander, '_send_reply'), \
         patch('modules.telegram_bot.bot_executor.submit', side_effect=lambda f, *a: f(*a)):
        commander._handle_message({'chat': {'id': 12345}, 'text': '/news 삼성전자'})

    mock_news.assert_called_with(['삼성전자'])
