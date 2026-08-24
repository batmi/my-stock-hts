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
    
    real_db = getattr(db_manager.db, '_real_db', db_manager.db)
    # [추가] 전역 DBManager 인스턴스의 경로 업데이트 및 연결 재설정
    original_manager_path = real_db.db_path
    real_db.db_path = str(db_file)
    
    # 기존 연결 닫기 (현재 스레드)
    if hasattr(real_db, 'local') and hasattr(real_db.local, 'conn') and real_db.local.conn:
        real_db.local.conn.close()
        real_db.local.conn = None
    
    # 새 DB 초기화
    real_db._init_db()
    
    yield db_file
    
    # [추가] 복구
    if hasattr(real_db, 'local') and hasattr(real_db.local, 'conn') and real_db.local.conn:
        real_db.local.conn.close()
        real_db.local.conn = None
    
    real_db.db_path = original_manager_path
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

def test_analyze_market_trends_success():
    """Gemini API 호출 성공 테스트

    [주의] 게이트를 **테스트 안에서** 건다. 예전에는
        @pytest.mark.skipif(getattr(theme_analysis, 'genai', None) is None, ...)
    였는데, theme_analysis.genai 는 _ensure_genai() 가 채우는 지연 로드 변수이고
    그 호출은 conftest 의 세션 fixture 가 한다 — **fixture 는 수집(collection) 이후에
    돈다.** 그래서 마커가 평가되는 시점의 genai 는 언제나 None 이었고, 패키지가 깔려
    있어도 이 테스트는 한 번도 실행되지 않았다(스위트의 유일한 skip 이 이것이었다).
    아래 _run_fallback_scenario 계열이 쓰는 방식과 같게 맞춘다.
    """
    pytest.importorskip("google.genai")
    theme_analysis._ensure_genai()

    # API 키 설정 (테스트용)
    original_key = config.GEMINI_API_KEY
    config.GEMINI_API_KEY = "TEST_KEY"

    with patch('modules.theme_analysis._gemini_stream') as mock_stream:
        # 신 SDK 는 청크 제너레이터를 준다 — 조각 하나짜리 스트림으로 흉내 낸다.
        mock_chunk = MagicMock()
        mock_candidate = MagicMock()
        mock_part = MagicMock()

        # 응답 구조 모킹 (candidates[0].content.parts 존재 여부 확인용)
        mock_chunk.text = "시장 분석 결과입니다."
        mock_chunk.candidates = [mock_candidate]
        mock_candidate.content.parts = [mock_part]

        mock_stream.return_value = [mock_chunk]

        try:
            result = theme_analysis.analyze_market_trends_with_gemini()

            assert result == "시장 분석 결과입니다."
            mock_stream.assert_called_once()
        finally:
            config.GEMINI_API_KEY = original_key

def test_gemini_generate_503_fallback():
    """기본 모델 503(서버 과부하) 시 폴백 모델로 자동 전환 테스트"""
    pytest.importorskip("google.genai")
    theme_analysis._ensure_genai()
    with patch('modules.theme_analysis._gemini_stream') as mock_stream:
        _run_fallback_scenario(mock_stream, "503 This model is currently experiencing high demand. Please try again later.")
        # _gemini_stream(content, model_name, gen_cfg) — 두 번째 호출의 모델이 폴백이어야 한다
        assert mock_stream.call_args_list[1][0][1] == config.GEMINI_FALLBACK_MODEL


def test_gemini_generate_429_fallback():
    """기본 모델 429(한도 초과) 시 폴백 모델로 자동 전환 테스트"""
    pytest.importorskip("google.genai")
    theme_analysis._ensure_genai()
    with patch('modules.theme_analysis._gemini_stream') as mock_stream:
        _run_fallback_scenario(mock_stream, "429 RESOURCE_EXHAUSTED: Quota exceeded")


def _run_fallback_scenario(mock_stream, error_message):
    """기본 모델이 error_message로 실패하면 폴백 모델 응답이 반환되는지 검증"""
    mock_chunk = MagicMock()
    mock_chunk.text = "폴백 모델 분석 결과"
    mock_stream.side_effect = [Exception(error_message), [mock_chunk]]

    result = theme_analysis._gemini_generate("테스트 프롬프트", {"temperature": 0.2}, 5.0)

    # 신 SDK 경로는 청크를 모아 만든 _StreamedResponse 를 돌려준다
    assert result.text == "폴백 모델 분석 결과"
    assert mock_stream.call_count == 2


def test_analyze_market_trends_no_api_key():
    """API 키가 없을 때 None 반환 테스트"""
    original_key = config.GEMINI_API_KEY
    config.GEMINI_API_KEY = ""
    
    result = theme_analysis.analyze_market_trends_with_gemini()
    
    assert result is None
    
    config.GEMINI_API_KEY = original_key

def test_fetch_theme_detail_success():
    """테마 상세 페이지 크롤링 테스트"""
    html = """
    <html>
    <table class="type_5">
        <tr>
            <td><a href="/item/main.naver?code=005930">삼성전자</a></td>
            <td>설명</td>
            <td>가격</td>
            <td>대비</td>
            <td>+1.5%</td>
        </tr>
        <tr>
            <td><a href="/item/main.naver?code=000660">SK하이닉스</a></td>
            <td>설명</td>
            <td>가격</td>
            <td>대비</td>
            <td>+2.0%</td>
        </tr>
    </table>
    </html>
    """
    theme = {'name': '반도체', 'link': '/theme/detail'}
    with patch('requests.get') as mock_get:
        mock_resp = MagicMock()
        mock_resp.content = html.encode('cp949')
        mock_get.return_value = mock_resp
        
        theme_analysis._fetch_theme_detail(theme)
        
        assert 'leading' in theme
        # 등락률 순 정렬 (2.0% > 1.5%)
        assert 'SK하이닉스' in theme['leading']
        assert '삼성전자' in theme['leading']