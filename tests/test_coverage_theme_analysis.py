import pytest
from unittest.mock import patch, MagicMock
from modules import theme_analysis
import config
from datetime import datetime, timezone

@patch('requests.get')
def test_fetch_realtime_news(mock_get):
    """구글 뉴스 RSS 크롤링 정상/에러 커버리지"""
    # RSS 정상 응답 모킹
    xml_data = """<?xml version="1.0" encoding="UTF-8"?>
    <rss version="2.0">
        <channel>
            <item>
                <title>삼성전자 어닝 서프라이즈</title>
                <link>http://news.google.com/test</link>
                <source>한국경제</source>
                <pubDate>Wed, 01 Nov 2023 10:00:00 GMT</pubDate>
            </item>
            <item>
                <title>두번째 뉴스</title>
                <link>http://news.google.com/test2</link>
            </item>
        </channel>
    </rss>"""
    mock_get.return_value = MagicMock(status_code=200, text=xml_data)
    
    res = theme_analysis.fetch_realtime_news("삼성전자")
    assert "삼성전자 어닝 서프라이즈" in res
    assert "한국경제" in res
    assert "두번째 뉴스" in res
    
    # HTTP 에러 응답 모킹
    mock_get.return_value = MagicMock(status_code=500, text="Error")
    res_fail = theme_analysis.fetch_realtime_news("삼성전자")
    assert res_fail == ""

@patch('modules.analysis.get_us_treasury_spot_data')
@patch('modules.analysis.get_domestic_index_data')
@patch('api.get_yf_fast_info')
def test_get_macro_context_str(mock_fast_info, mock_dom_idx, mock_treasury):
    """매크로 지표 컨텍스트 데이터 수집 파이프라인 테스트"""
    import pandas as pd
    # 국내 지수 모킹
    mock_dom_idx.return_value = pd.DataFrame({'close': [2500, 2600]})
    # 미국채 현물(TVC:USxxY) 모킹 — 실 tvDatafeed 네트워크 호출 차단.
    # 5년물은 현물 실패로 두어 아래 yfinance(^FVX)+선물 프록시(선물적용) 분기를 검증한다.
    def treasury_side_effect(symbol, n_bars=300):
        if symbol == "US05Y":
            return None
        return pd.DataFrame({'close': [4.1, 4.15]})
    mock_treasury.side_effect = treasury_side_effect
    
    # 해외 지수 모킹 (미국채 선물 프록시 로직 포함)
    def fast_info_side_effect(ticker):
        if ticker == "^FVX": # 미국채 5년물
            return {'last_price': 4.0, 'regular_market_previous_close': 3.9, 'year_high': 5.0}
        if ticker == "ZF=F": # 5년물 선물
            return {'last_price': 100.0, 'regular_market_previous_close': 99.0}
        return {'last_price': 100.0, 'regular_market_previous_close': 90.0, 'year_high': 110.0}
    
    mock_fast_info.side_effect = fast_info_side_effect
    
    # 시간 조작으로 선물 프록시 연산 분기 발동 (utc_hour < 13)
    with patch('modules.theme_analysis.datetime') as mock_dt:
        mock_dt.now.return_value = datetime(2023, 1, 1, 10, 0, tzinfo=timezone.utc)
        res = theme_analysis._get_macro_context_str()
        
    assert "코스피" in res
    assert "미국채 5년물 금리" in res
    assert "선물적용" in res

@patch('modules.theme_analysis.genai.GenerativeModel')
def test_ask_gemini(mock_model):
    """자유 질문 API 핸들러 테스트"""
    config.GEMINI_API_KEY = "test_key"
    mock_model.return_value.generate_content.return_value = MagicMock(text="AI 답변입니다.")
    
    res = theme_analysis.ask_gemini("주식이 뭐야?")
    assert "AI 답변입니다." in res

@patch('modules.theme_analysis.fetch_realtime_news')
@patch('modules.theme_analysis.genai.GenerativeModel')
def test_get_latest_news_with_gemini(mock_model, mock_news):
    """뉴스 요약 API 핸들러 테스트"""
    config.GEMINI_API_KEY = "test_key"
    mock_news.return_value = "뉴스 리스트"
    mock_model.return_value.generate_content.return_value = MagicMock(text="요약된 뉴스입니다.")
    
    res = theme_analysis.get_latest_news_with_gemini("삼성전자")
    assert "요약된 뉴스입니다." in res
    
    # 뉴스 데이터가 없을 경우
    mock_news.return_value = ""
    res_empty = theme_analysis.get_latest_news_with_gemini("없는주식")
    assert "결과가 없습니다" in res_empty