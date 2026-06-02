import pytest
import os
import sys
from unittest.mock import patch, MagicMock
from datetime import datetime, time

sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import config
from modules.scheduler import SystemScheduler

@pytest.fixture
def scheduler():
    """스케줄러 싱글톤 초기화용 피스처"""
    s = SystemScheduler()
    s.is_running = False
    s.last_holiday_notified_date = None
    s.last_briefing_date = None
    return s

def test_scheduler_start_stop(scheduler):
    """스케줄러 스레드 시작 및 중지 테스트"""
    config.ENABLE_TELEGRAM = True
    scheduler.start()
    assert scheduler.is_running is True
    scheduler.stop()
    assert scheduler.is_running is False

@patch('modules.scheduler.api.is_holiday_today')
@patch('modules.scheduler.api.is_us_holiday_today')
@patch('modules.scheduler.api.get_holiday_name')
@patch('modules.scheduler.api.send_telegram_message')
def test_check_holiday_notification_both_closed(mock_tg, mock_name, mock_us_holiday, mock_kr_holiday, scheduler):
    """국내 및 미국 시장 모두 휴장 시 알림 테스트"""
    mock_kr_holiday.return_value = True
    mock_us_holiday.return_value = True
    mock_name.side_effect = ["어린이날", "독립기념일"]
    
    with patch('modules.scheduler.datetime') as mock_dt:
        now = datetime.now()
        # 시간을 알림 전송 타겟 시간(08:30~09:30)으로 강제 모킹
        mock_dt.now.return_value = datetime.combine(now.date(), time(8, 45))
        mock_dt.today.return_value = now
        mock_dt.strptime = datetime.strptime
        mock_dt.combine = datetime.combine
        
        scheduler._check_holiday_notification()
        
    mock_tg.assert_called_once()
    assert "모두 휴장" in mock_tg.call_args[0][0]
    assert scheduler.last_holiday_notified_date == mock_dt.now.return_value.strftime("%Y-%m-%d")

@patch('modules.scheduler.theme_analysis.generate_morning_briefing')
@patch('modules.scheduler.theme_analysis._get_macro_context_str')
@patch('modules.scheduler.api.send_telegram_message')
def test_execute_briefing(mock_tg, mock_macro, mock_generate, scheduler):
    """장전 브리핑 전송 로직 검증"""
    mock_macro.return_value = "Macro Context Data"
    mock_generate.return_value = "Morning Briefing Content"
    
    scheduler.execute_briefing()
    
    mock_macro.assert_called_once()
    mock_generate.assert_called_once_with("Macro Context Data")
    mock_tg.assert_called_once_with("Morning Briefing Content")

@patch('modules.scheduler.theme_analysis.generate_daily_closing_report')
@patch('modules.scheduler.api.get_domestic_balance')
@patch('modules.scheduler.api.get_deposit_balance')
@patch('modules.scheduler.api.send_telegram_message')
def test_execute_daily_closing_report(mock_tg, mock_dep, mock_bal, mock_generate, scheduler):
    """장 마감 종합 브리핑 전송 로직 검증"""
    # 모의 잔고 반환 설정
    mock_bal.return_value = (
        [{'prdt_name': '삼성전자', 'evlu_amt': '1000000', 'evlu_pfls_rt': '5.5', 'hldg_qty': '10'}], 
        []
    )
    mock_dep.return_value = {'d2_deposit': 5000000}
    mock_generate.return_value = "Closing Report Content"
    
    scheduler.execute_daily_closing_report()
    
    mock_bal.assert_called_once()
    mock_dep.assert_called_once()
    mock_generate.assert_called_once()
    
    # 프롬프트 포트폴리오 텍스트가 올바르게 생성되어 전달되었는지 검증
    called_args = mock_generate.call_args[0][0]
    assert "삼성전자" in called_args
    assert "총 자산: 6,000,000원" in called_args
    
    mock_tg.assert_called_once()
    assert "Closing Report Content" in mock_tg.call_args[0][0]