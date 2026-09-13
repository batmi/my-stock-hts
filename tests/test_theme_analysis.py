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
    """네이버 테마 목록 — JSON API 응답을 파싱한다 (2026-09-13 HTML→JSON 전환)"""
    payload = {"totalCount": 2, "page": 1, "pageSize": 100, "groups": [
        {"no": 1, "name": "2차전지", "totalCount": 10, "changeRate": "2.5", "riseCount": 7, "fallCount": 2, "steadyCount": 1},
        {"no": 2, "name": "반도체", "totalCount": 20, "changeRate": "-1.2", "riseCount": 5, "fallCount": 14, "steadyCount": 1},
    ]}
    with patch('requests.get') as mock_get:
        mock_get.return_value = MagicMock(status_code=200, json=lambda: payload)
        themes = theme_analysis.fetch_naver_themes()

        assert len(themes) == 2
        assert themes[0]['name'] == "2차전지"
        assert themes[0]['rate'] == 2.5
        assert themes[0]['rise'] == 7 and themes[0]['fall'] == 2 and themes[0]['total'] == 10
        assert themes[0]['no'] == 1
        assert themes[1]['name'] == "반도체"
        assert themes[1]['rate'] == -1.2
        assert 'rate3' not in themes[0]          # 3일 등락률은 API 에 없다 — 0 으로 지어내지 않는다


def test_fetch_naver_themes_pages_until_total():
    """페이지 상한(100)을 넘는 목록은 totalCount 까지 이어 받는다"""
    def page(n, count):
        return {"totalCount": 150, "groups": [
            {"no": i, "name": f"T{i}", "totalCount": 1, "changeRate": "0", "riseCount": 0, "fallCount": 0}
            for i in range((n - 1) * 100, (n - 1) * 100 + count)]}
    with patch('requests.get') as mock_get:
        mock_get.side_effect = [MagicMock(status_code=200, json=lambda: page(1, 100)),
                                MagicMock(status_code=200, json=lambda: page(2, 50))]
        themes = theme_analysis.fetch_naver_themes()
    assert len(themes) == 150 and mock_get.call_count == 2


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
    """테마 구성종목 — JSON API 로 받아 등락률 상위 2 를 주도주로 채운다"""
    payload = {"stocks": [
        {"itemCode": "005930", "stockName": "삼성전자", "fluctuationsRatio": "1.5"},
        {"itemCode": "000660", "stockName": "SK하이닉스", "fluctuationsRatio": "2.0"},
        {"itemCode": "000000", "stockName": "꼴찌", "fluctuationsRatio": "-3.0"},
    ]}
    theme = {'name': '반도체', 'no': 12}
    with patch('requests.get') as mock_get:
        mock_get.return_value = MagicMock(status_code=200, json=lambda: payload)
        theme_analysis._fetch_theme_detail(theme)

    # 등락률 순 정렬 (2.0% > 1.5%) · 상위 2 만
    assert theme['leading'] == "SK하이닉스(000660), 삼성전자(005930)"
    assert [s['code'] for s in theme['leading_stocks']] == ["000660", "005930"]


def test_fetch_theme_detail_without_no_is_dash():
    """테마 번호가 없으면(옛 항목) 조회하지 않고 '-' 로 둔다"""
    theme = {'name': '반도체'}
    with patch('requests.get') as mock_get:
        theme_analysis._fetch_theme_detail(theme)
    assert theme['leading'] == "-" and not mock_get.called

def test_analyze_chart_image_sends_sdk_part(tmp_path):
    """차트 이미지 입력이 신 SDK 가 받는 Part 로 전달되는지 검증.

    구 SDK 관행대로 {"mime_type": ..., "data": ...} dict 를 넘기면 요청 전에
    _GenerateContentParameters 검증에서 터졌다(2026-08-25). 실제 SDK 스키마에
    통과하는지까지 확인해 회귀를 막는다.
    """
    pytest.importorskip("google.genai")
    theme_analysis._ensure_genai()
    from google.genai import types as genai_types

    img = tmp_path / "chart.png"
    img.write_bytes(b"\x89PNG\r\n\x1a\n")

    original_key = config.GEMINI_API_KEY
    config.GEMINI_API_KEY = "TEST_KEY"
    with patch('modules.theme_analysis._gemini_stream') as mock_stream:
        mock_chunk = MagicMock()
        mock_chunk.text = "차트 분석 결과"
        mock_chunk.candidates = [MagicMock()]
        mock_stream.return_value = [mock_chunk]
        try:
            result = theme_analysis.analyze_chart_image_with_gemini(str(img), "삼성전자", "005930", "6개월")
        finally:
            config.GEMINI_API_KEY = original_key

    assert result == "차트 분석 결과"
    content = mock_stream.call_args[0][0]
    assert isinstance(content[0], str)
    assert isinstance(content[1], genai_types.Part)
    # SDK 요청 스키마가 그대로 받아들이는지(= 실제 호출이 통과하는지)까지 확인
    params = genai_types._GenerateContentParameters(model="x", contents=content)
    assert [type(c).__name__ for c in params.contents] == ["str", "Part"]
