import pytest
from unittest.mock import patch, MagicMock, call
from modules import theme_analysis
import config
from modules import db_manager

@pytest.fixture(autouse=True)
def mock_db_connection():
    """DB 연결 모킹하여 ResourceWarning 방지"""
    with patch('modules.db_manager.DBManager._get_conn') as mock_conn:
        yield mock_conn

@patch('modules.theme_analysis.fetch_naver_themes')
@patch('modules.theme_analysis._fetch_theme_detail')
def test_show_naver_themes(mock_detail, mock_fetch):
    """네이버 테마 순위 출력 테스트"""
    mock_fetch.return_value = [
        {'name': '2차전지', 'rate': 5.0, 'rate3': 10.0, 'link': '/link'}
    ]
    
    with patch('config.console.print') as mock_print:
        with patch('config.console.status') as mock_status:
            mock_status.return_value.__enter__.return_value = MagicMock()
            theme_analysis._show_naver_themes()
            
    # 테이블 출력 확인
    assert mock_print.call_count > 0

@patch('modules.theme_analysis._load_theme_analysis')
@patch('rich.prompt.Prompt.ask')
def test_analyze_with_gemini_ui_cached(mock_ask, mock_load):
    """Gemini 분석 UI (캐시 사용) 테스트"""
    mock_load.return_value = {'updated_at': '2023-01-01', 'data': 'Cached Result'}
    mock_ask.return_value = '1' # Use cache
    
    with patch('config.console.print') as mock_print:
        theme_analysis._analyze_with_gemini_ui()
        
    # 패널 출력 확인
    assert mock_print.call_count > 0

@patch('modules.theme_analysis.analyze_market_trends_with_gemini')
@patch('rich.prompt.Prompt.ask')
def test_analyze_with_custom_prompt_ui(mock_ask, mock_analyze):
    """사용자 정의 프롬프트 분석 UI 테스트"""
    mock_ask.return_value = "Custom Prompt"
    mock_analyze.return_value = "AI Response"
    
    with patch('config.console.print') as mock_print:
        theme_analysis._analyze_with_custom_prompt_ui()
        
    assert mock_print.call_count > 0