import pytest
from unittest.mock import patch, MagicMock
from modules import telegram_bot
from modules.auto_trade import AutoTrader

@pytest.fixture
def commander():
    # TelegramCommander는 싱글톤이므로 초기화 플래그를 우회하여 강제 초기화
    telegram_bot.TelegramCommander._instance = None
    cmd = telegram_bot.TelegramCommander()
    cmd.trader = MagicMock(spec=AutoTrader)
    return cmd

def test_cmd_help(commander):
    """도움말 명령어 텍스트 검증"""
    res = commander._cmd_help([])
    assert "시스템 트레이딩 봇 도움말" in res
    assert "/start" in res
    assert "/help" in res

def test_cmd_start_already_running(commander):
    """이미 실행 중일 때 start 호출 시"""
    commander.trader.is_running = True
    res = commander._cmd_start([])
    assert "이미 시스템 트레이딩이 실행 중" in res
    commander.trader.start.assert_not_called()

def test_cmd_start_success(commander):
    """실행 중이 아닐 때 start 호출 시"""
    commander.trader.is_running = False
    res = commander._cmd_start([])
    assert "시스템 트레이딩을 시작" in res
    commander.trader.start.assert_called_once_with(interactive=False)

def test_cmd_stop_not_running(commander):
    """실행 중이 아닐 때 stop 호출 시"""
    commander.trader.is_running = False
    res = commander._cmd_stop([])
    assert "실행 중인 시스템 트레이딩이 없습니다" in res
    commander.trader.stop.assert_not_called()

def test_cmd_stop_success(commander):
    """실행 중일 때 stop 호출 시"""
    commander.trader.is_running = True
    res = commander._cmd_stop([])
    assert "중단 요청을 처리" in res
    commander.trader.stop.assert_called_once_with(use_status=False)

def test_cmd_restart(commander):
    """restart 명령어 호출 검증"""
    commander.trader.is_running = True
    res = commander._cmd_restart([])
    assert "중단 완료" in res
    assert "재시작" in res
    commander.trader.stop.assert_called_once()
    commander.trader.start.assert_called_once()

def test_cmd_report_args(commander):
    """report 명령어의 기간 인자 파싱 검증"""
    commander.trader.get_performance_report.return_value = "report_data"
    
    # 인자 없을 때 (당일)
    res = commander._cmd_report([])
    assert res == "report_data"
    commander.trader.get_performance_report.assert_called_with(days=0)
    
    # 주간
    commander._cmd_report(["w"])
    commander.trader.get_performance_report.assert_called_with(days=7)
    
    # 월간
    commander._cmd_report(["m"])
    commander.trader.get_performance_report.assert_called_with(days=30)
    
    # 임의 숫자
    commander._cmd_report(["10"])
    commander.trader.get_performance_report.assert_called_with(days=10)

def test_cmd_calendar_default_days(commander):
    """/calendar 는 기본 30일 캘린더 메시지를 돌려준다"""
    from modules.manage import events as calendar_events
    with patch.object(commander, '_send_reply'), \
         patch.object(calendar_events, 'build_telegram_message', return_value="📅 캘린더") as mock_build:
        res = commander._cmd_calendar([])
    mock_build.assert_called_once_with(days=30)
    assert res == "📅 캘린더"

def test_cmd_calendar_clamps_days(commander):
    """/calendar 인자는 1~180일로 제한된다"""
    from modules.manage import events as calendar_events
    with patch.object(commander, '_send_reply'), \
         patch.object(calendar_events, 'build_telegram_message', return_value="ok") as mock_build:
        commander._cmd_calendar(["365"])
    mock_build.assert_called_once_with(days=180)

def test_cmd_calendar_handles_failure(commander):
    """조회 실패 시 예외를 삼키고 안내 문구를 돌려준다"""
    from modules.manage import events as calendar_events
    with patch.object(commander, '_send_reply'), \
         patch.object(calendar_events, 'build_telegram_message', side_effect=RuntimeError("boom")):
        res = commander._cmd_calendar([])
    assert "오류가 발생했습니다" in res

def test_calendar_command_registered(commander):
    """/calendar 가 명령어 핸들러와 도움말에 모두 등록돼 있다"""
    assert "/calendar" in commander.command_handlers
    assert "/calendar" in commander._cmd_help([])
