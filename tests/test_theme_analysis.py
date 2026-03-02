import pytest
from unittest.mock import patch, MagicMock
import os
import sys
import sqlite3
import json

# 프로젝트 루트 경로 추가
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from modules import theme_analysis
import config
from modules import db_manager # [추가]

@pytest.fixture
def temp_db(tmp_path):
    """테스트용 임시 DB 생성 및 설정"""
    db_file = tmp_path / "test_theme.db"
    original_db_path = config.DB_FILE_PATH
    config.DB_FILE_PATH = str(db_file)
    
    # [추가] 전역 DBManager 인스턴스의 경로 업데이트 및 연결 재설정
    original_manager_path = db_manager.db.db_path
    db_manager.db.db_path = str(db_file)
    
    # 기존 연결 닫기 (현재 스레드)
    if hasattr(db_manager.db.local, 'conn') and db_manager.db.local.conn:
        db_manager.db.local.conn.close()
        del db_manager.db.local.conn
    
    # 새 DB 초기화
    db_manager.db._init_db()
    
    yield db_file
    
    # [추가] 복구
    if hasattr(db_manager.db.local, 'conn') and db_manager.db.local.conn:
        db_manager.db.local.conn.close()
        del db_manager.db.local.conn
    
    db_manager.db.db_path = original_manager_path
    config.DB_FILE_PATH = original_db_path

def test_db_operations(temp_db):
    """테마 분석 결과 DB 저장 및 로드 테스트"""
    test_data = "테스트 분석 결과입니다."
    
    # 1. 저장
    theme_analysis._save_theme_analysis(test_data)
    
    # 2. 로드
    result = theme_analysis._load_theme_analysis()
    
    assert result is not None
    assert result['data'] == test_data
    assert 'updated_at' in result

def test_fetch_naver_themes_success():
    """네이버 금융 테마 크롤링 성공 테스트"""
    # 가짜 HTML 응답 생성 (CP949 인코딩 필요)
    html_content = """
    <html>
    <body>
    <table class="type_1">
        <tr>
            <td class="col_type1"><a href="/sise/sise_group_detail.naver?type=theme&no=1">2차전지</a></td>
            <td class="col_type1">2.5%</td>
            <td class="col_type1">5.0%</td>
            <td>...</td>
        </tr>
        <tr>
            <td class="col_type1"><a href="/sise/sise_group_detail.naver?type=theme&no=2">반도체</a></td>
            <td class="col_type1">-1.2%</td>
            <td class="col_type1">0.5%</td>
            <td>...</td>
        </tr>
    </table>
    </body>
    </html>
    """
    
    with patch('requests.get') as mock_get:
        mock_response = MagicMock()
        mock_response.content = html_content.encode('cp949')
        mock_get.return_value = mock_response
        
        themes = theme_analysis.fetch_naver_themes()
        
        assert len(themes) == 2
        assert themes[0]['name'] == "2차전지"
        assert themes[0]['rate'] == 2.5
        assert themes[1]['name'] == "반도체"
        assert themes[1]['rate'] == -1.2

def test_fetch_naver_themes_failure():
    """크롤링 실패 시 빈 리스트 반환 테스트"""
    with patch('requests.get') as mock_get:
        mock_get.side_effect = Exception("Network Error")
        
        themes = theme_analysis.fetch_naver_themes()
        
        assert themes == []

@patch('modules.theme_analysis.genai.Client')
def test_analyze_market_trends_success(mock_client_cls):
    """Gemini API 호출 성공 테스트"""
    # API 키 설정 (테스트용)
    original_key = config.GEMINI_API_KEY
    config.GEMINI_API_KEY = "TEST_KEY"
    
    # Mock Client 및 Response 설정
    mock_client = mock_client_cls.return_value
    mock_response = MagicMock()
    mock_candidate = MagicMock()
    mock_part = MagicMock()
    
    # 응답 구조 모킹 (candidates[0].content.parts 존재 여부 확인용)
    mock_response.text = "시장 분석 결과입니다."
    mock_response.candidates = [mock_candidate]
    mock_candidate.content.parts = [mock_part]
    
    mock_client.models.generate_content.return_value = mock_response
    
    result = theme_analysis.analyze_market_trends_with_gemini()
    
    assert result == "시장 분석 결과입니다."
    mock_client.models.generate_content.assert_called_once()
    
    config.GEMINI_API_KEY = original_key

def test_analyze_market_trends_no_api_key():
    """API 키가 없을 때 None 반환 테스트"""
    original_key = config.GEMINI_API_KEY
    config.GEMINI_API_KEY = ""
    
    result = theme_analysis.analyze_market_trends_with_gemini()
    
    assert result is None
    
    config.GEMINI_API_KEY = original_key