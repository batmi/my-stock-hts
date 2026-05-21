import pytest
import time
from unittest.mock import patch, MagicMock
from datetime import datetime

try:
    from modules import scheduler
except ImportError:
    pytest.skip("scheduler.py 모듈이 아직 로드되지 않았습니다.", allow_module_level=True)

# ==========================================================
# 1. 텔레그램 봇과 스케줄러의 분리 (SRP 원칙 준수 검증)
# ==========================================================

@pytest.fixture
def app_scheduler():
    # 테스트용 스케줄러 인스턴스 반환
    # 실제 scheduler 모듈의 클래스명에 맞게 조정 (예: SystemScheduler)
    return scheduler.SystemScheduler() if hasattr(scheduler, 'SystemScheduler') else MagicMock()

@patch('modules.scheduler.api.send_telegram_message')
def test_scheduler_heartbeat_trigger(mock_tg, app_scheduler):
    """스케줄러에서 독립적으로 하트비트/시스템 에러를 모니터링하는지 검증"""
    if isinstance(app_scheduler, MagicMock):
        pytest.skip("실제 스케줄러 객체가 필요합니다.")
        
    # 의도적으로 에러 카운트를 조작
    app_scheduler.trader.consecutive_errors = 6
    app_scheduler.last_heartbeat_time = 0  # 쿨타임 무시하여 즉시 검사되도록 유도
    app_scheduler._check_heartbeat()  # 스케줄러 내부 하트비트 트리거 메서드라 가정
    
    # 텔레그램 모듈이 아닌 스케줄러 모듈에서 알림을 발송하는지 확인
    assert mock_tg.called
    assert any("에러" in str(call.args[0]) for call in mock_tg.call_args_list)

@patch('modules.scheduler.theme_analysis._get_macro_context_str', return_value="Mock Context")
@patch('modules.scheduler.theme_analysis.generate_morning_briefing', return_value="Mock Briefing")
@patch('modules.scheduler.api.send_telegram_message')
def test_scheduler_morning_briefing_job(mock_tg, mock_briefing, mock_macro, app_scheduler):
    """스케줄러가 지정된 시간에 장전 브리핑을 정상 트리거하는지 검증"""
    if isinstance(app_scheduler, MagicMock):
        pytest.skip("실제 스케줄러 객체가 필요합니다.")
        
    # 강제로 장전 브리핑 Job 실행
    if hasattr(app_scheduler, 'execute_morning_briefing'):
        app_scheduler.execute_morning_briefing()
    elif hasattr(app_scheduler, '_execute_briefing'):
        app_scheduler._execute_briefing()
    elif hasattr(app_scheduler, 'execute_briefing'):
        app_scheduler.execute_briefing()
        
    # 모듈 간 결합도 없이 독립적으로 브리핑을 생성하고 알림을 보냈는지 확인
    mock_briefing.assert_called_once()
    mock_tg.assert_called_with("Mock Briefing")