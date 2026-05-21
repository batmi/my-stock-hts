import pytest
from unittest.mock import patch, MagicMock
from datetime import datetime, timedelta
import config
from modules import theme_analysis
from modules.auto_trade import ConclusionMonitor
from modules.telegram_bot import TelegramCommander

@pytest.fixture
def mock_genai():
    """Gemini API 호출을 방지하기 위한 Mock 픽스처"""
    with patch('modules.theme_analysis.genai') as mock:
        # 테스트 중 API 키 검증 통과를 위해 임시 값 설정
        config.GEMINI_API_KEY = "test_dummy_key"
        yield mock

# ==========================================================
# 1. AI 매매 복기 (Trading Autopsy) 테스트
# ==========================================================

def test_generate_trading_autopsy_success(mock_genai):
    """매매 복기 프롬프트 생성 및 응답 정상 처리 테스트"""
    mock_model = MagicMock()
    mock_genai.GenerativeModel.return_value = mock_model
    
    mock_response = MagicMock()
    mock_response.text = "🤖 **수석 전략가 분석**:\n테스트 분석 결과입니다.\n\n💡 **조언**:\n테스트 조언입니다."
    mock_model.generate_content.return_value = mock_response

    res = theme_analysis.generate_trading_autopsy("005930", "삼성전자", "2023-10-01 10:00:00", 8.5, "익절", 20.0, 5)
    
    assert res is not None
    assert "테스트 분석 결과" in res
    mock_model.generate_content.assert_called_once()
    
    # 전달된 프롬프트에 주요 파라미터가 포함되었는지 확인
    prompt_args = mock_model.generate_content.call_args[0][0]
    assert "삼성전자(005930)" in prompt_args
    assert "8.5점" in prompt_args
    assert "+20.00%" in prompt_args
    assert "익절" in prompt_args

def test_generate_trading_autopsy_no_api_key():
    """API 키가 없을 때의 예외 처리 테스트"""
    config.GEMINI_API_KEY = ""
    res = theme_analysis.generate_trading_autopsy("005930", "삼성전자", "2023-10-01", 8.0, "익절", 10.0, 5)
    assert "Gemini" in res

def test_generate_trading_autopsy_api_exception(mock_genai):
    """API 호출 중 에러 발생 시의 예외 처리 테스트"""
    mock_model = MagicMock()
    mock_genai.GenerativeModel.return_value = mock_model
    mock_model.generate_content.side_effect = Exception("API Timeout")
    
    res = theme_analysis.generate_trading_autopsy("005930", "삼성전자", "2023-10-01", 8.0, "익절", 10.0, 5)
    assert "Gemini" in res

@patch('modules.theme_analysis.generate_trading_autopsy')
@patch('modules.db_manager.db.get_latest_buy_trade')
@patch('api.send_telegram_message')
def test_conclusion_monitor_send_trading_autopsy(mock_send_tg, mock_get_buy, mock_generate_autopsy):
    """ConclusionMonitor 스레드에서 매도 체결 시 복기 메시지 발송 래퍼 로직 테스트"""
    monitor = ConclusionMonitor()
    
    # DB 매수 기록 모킹
    mock_get_buy.return_value = {'time': '2023-10-01 10:00:00', 'score': 8.0}
    # AI 복기 결과 모킹
    mock_generate_autopsy.return_value = "Mock Autopsy Report"
    
    sell_trade = {'reason': '익절(20%)', 'profit_rate': 20.0}
    monitor._send_trading_autopsy("005930", "삼성전자", sell_trade)
    
    mock_send_tg.assert_called_once()
    assert "📝 [AI 매매 복기 리포트] 삼성전자(005930)" in mock_send_tg.call_args[0][0]
    assert "Mock Autopsy Report" in mock_send_tg.call_args[0][0]

# ==========================================================
# 2. AI 포트폴리오 진단 (Portfolio Diagnosis) 테스트
# ==========================================================

@patch('modules.theme_analysis._get_macro_context_str')
def test_generate_portfolio_diagnosis_success(mock_macro, mock_genai):
    """포트폴리오 진단 프롬프트 생성 및 응답 정상 처리 테스트"""
    mock_macro.return_value = "[시스템 제공 실시간 핵심 매크로 지표]\n- 코스피: 2600.00"
    mock_model = MagicMock()
    mock_genai.GenerativeModel.return_value = mock_model
    
    mock_response = MagicMock()
    mock_response.text = "📊 **섹터/테마 편중도 요약**: 반도체 편중\n💡 **리밸런싱 제안**: 헷지 필요"
    mock_model.generate_content.return_value = mock_response

    portfolio_str = "총 자산: 10,000,000원\n- 삼성전자: 비중 100%"
    res = theme_analysis.generate_portfolio_diagnosis(portfolio_str)
    
    assert res is not None
    assert "반도체 편중" in res
    mock_model.generate_content.assert_called_once()
    
    prompt_args = mock_model.generate_content.call_args[0][0]
    assert "삼성전자: 비중 100%" in prompt_args
    assert "코스피: 2600.00" in prompt_args

# ==========================================================
# 3. AI 관심 종목 큐레이션 (Stock Curation) 테스트
# ==========================================================

@patch('modules.theme_analysis._get_macro_context_str')
def test_generate_stock_curation_success(mock_macro, mock_genai):
    """관심 종목 큐레이션 프롬프트 생성 및 응답 정상 처리 테스트"""
    mock_macro.return_value = "[시스템 제공 실시간 핵심 매크로 지표]"
    mock_model = MagicMock()
    mock_genai.GenerativeModel.return_value = mock_model
    
    mock_response = MagicMock()
    mock_response.text = "🎯 [AI 관심 종목 큐레이션]\n\n📊 1. AI 반도체 장비\n• 한미반도체(042700) - HBM 수혜"
    mock_model.generate_content.return_value = mock_response

    res = theme_analysis.generate_stock_curation()
    
    assert res is not None
    assert "한미반도체(042700)" in res
    mock_model.generate_content.assert_called_once()
    
    # 전달된 프롬프트에 주요 파라미터가 포함되었는지 확인
    prompt_args = mock_model.generate_content.call_args[0][0]
    assert "[시스템 제공 실시간 핵심 매크로 지표]" in prompt_args
    assert "핵심 테마 2~3가지" in prompt_args

@patch('modules.telegram_bot.threading.Thread')
@patch('modules.telegram_bot.TelegramCommander._send_reply')
def test_cmd_curate_trigger(mock_reply, mock_thread):
    """/curate 명령어 입력 시 비동기 스레드 트리거 테스트"""
    cmd = TelegramCommander()
    cmd._cmd_curate([])
    
    mock_reply.assert_called_once()
    assert "주도주를 발굴 중" in mock_reply.call_args[0][0]
    mock_thread.assert_called_once()

@patch('modules.theme_analysis.generate_stock_curation')
@patch('modules.telegram_bot.TelegramCommander._send_reply')
def test_execute_curate_success(mock_reply, mock_generate):
    """큐레이션 결과가 있을 때 텔레그램 메시지 발송 확인 테스트"""
    cmd = TelegramCommander()
    mock_generate.return_value = "Mock Curation Report"
    
    cmd._execute_curate()
    
    mock_generate.assert_called_once()
    mock_reply.assert_called_once()
    assert "Mock Curation Report" in mock_reply.call_args[0][0]
    assert "터미널 HTS 메뉴" in mock_reply.call_args[0][0]