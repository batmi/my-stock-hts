import pytest
from unittest.mock import patch, MagicMock
import pandas as pd
from datetime import datetime, timezone

import config
from modules import theme_analysis
from modules.telegram_bot import TelegramCommander

# ==========================================
# 1. theme_analysis.py 추가 커버리지
# ==========================================

def test_fetch_realtime_news_success():
    """구글 뉴스 RSS 크롤링 성공 파싱 테스트"""
    xml_content = """<?xml version="1.0" encoding="UTF-8"?>
    <rss><channel><item>
        <title>삼성전자 어닝 서프라이즈</title>
        <link>http://test.com/news1</link>
        <source>한국경제</source>
        <pubDate>Wed, 01 Nov 2023 10:00:00 GMT</pubDate>
    </item></channel></rss>"""
    
    with patch('requests.get') as mock_get:
        mock_get.return_value = MagicMock(status_code=200, text=xml_content)
        res = theme_analysis.fetch_realtime_news("삼성전자")
        
        assert "삼성전자 어닝 서프라이즈" in res
        assert "http://test.com/news1" in res
        assert "한국경제" in res

def test_fetch_realtime_news_fail():
    """구글 뉴스 RSS 크롤링 실패(404) 분기 테스트"""
    with patch('requests.get') as mock_get:
        mock_get.return_value = MagicMock(status_code=404, text="Not Found")
        res = theme_analysis.fetch_realtime_news("삼성전자")
        assert res == ""

@patch('modules.theme_analysis.genai.GenerativeModel')
def test_analyze_stock_with_gemini_api_error(mock_model):
    """종목 심층 진단 중 Rate Limit 에러 발생 시 처리 검증"""
    config.GEMINI_API_KEY = "test_key"
    # API 한도 초과 에러 모킹
    mock_model.return_value.generate_content.side_effect = Exception("429 RESOURCE_EXHAUSTED")
    
    res = theme_analysis.analyze_stock_with_gemini("005930", "삼성전자", "기술적 분석 텍스트")
    assert "호출 한도 초과" in res

@patch('modules.theme_analysis.genai.GenerativeModel')
def test_evaluate_backtest_with_gemini_modes(mock_model):
    """백테스트 평가 시 single 및 monte_carlo 분기 커버리지"""
    config.GEMINI_API_KEY = "test_key"
    mock_model.return_value.generate_content.return_value.text = "AI Backtest Evaluation"
    
    # 1. 단일 모드 (single)
    res_single = theme_analysis.evaluate_backtest_with_gemini("005930", "삼성전자", "test info", mode='single')
    assert "AI Backtest Evaluation" in res_single
    
    # 2. 몬테카를로 모드 (monte_carlo)
    res_mc = theme_analysis.evaluate_backtest_with_gemini("005930", "삼성전자", "test info", mode='monte_carlo')
    assert "AI Backtest Evaluation" in res_mc

@patch('modules.theme_analysis.genai.GenerativeModel')
def test_generate_trading_autopsy_success(mock_model):
    """매매 복기(Autopsy) AI 리포트 생성 검증"""
    config.GEMINI_API_KEY = "test_key"
    mock_model.return_value.generate_content.return_value.text = "AI Autopsy Report"
    
    res = theme_analysis.generate_trading_autopsy("005930", "삼성전자", "2023-11-01", 8, "익절", 5.0, 3)
    assert "AI Autopsy Report" in res

@patch('modules.theme_analysis.genai.GenerativeModel')
@patch('modules.theme_analysis._get_macro_context_str', return_value="Macro Context Info")
def test_generate_daily_closing_report_success(mock_macro, mock_model):
    """포트폴리오 리스크 진단 AI 생성 검증"""
    config.GEMINI_API_KEY = "test_key"
    mock_model.return_value.generate_content.return_value.text = "AI Portfolio Diagnosis"
    
    res = theme_analysis.generate_daily_closing_report("포트폴리오 정보")
    assert "AI Portfolio Diagnosis" in res

@patch('modules.theme_analysis.genai.GenerativeModel')
def test_generate_morning_briefing_success(mock_model):
    """장전 브리핑 AI 리포트 생성 검증"""
    config.GEMINI_API_KEY = "test_key"
    mock_model.return_value.generate_content.return_value.text = "AI Morning Briefing"
    
    res = theme_analysis.generate_morning_briefing("마감 데이터 요약")
    assert "AI Morning Briefing" in res

@patch('modules.theme_analysis.genai.GenerativeModel')
def test_generate_stock_curation_success(mock_model):
    """AI 종목 큐레이션 생성 검증"""
    config.GEMINI_API_KEY = "test_key"
    mock_model.return_value.generate_content.return_value.text = "AI Stock Curation"
    
    res = theme_analysis.generate_stock_curation()
    assert "AI Stock Curation" in res

@patch('modules.theme_analysis.fetch_realtime_news', return_value="")
def test_get_latest_news_with_gemini_empty_crawling(mock_fetch):
    """구글 뉴스 RSS가 빈 값을 반환했을 때 조기 종료 분기 검증"""
    config.GEMINI_API_KEY = "test_key"
    res = theme_analysis.get_latest_news_with_gemini("삼성전자")
    
    assert "실시간 뉴스 검색 결과가 없습니다" in res

@patch.dict('sys.modules', {'tradingview_screener': None})
def test_run_tradingview_screener_import_error():
    """트레이딩뷰 스크리너 라이브러리 미설치 시 에러 메시지 출력 검증"""
    with patch('config.console.print') as mock_print:
        res = theme_analysis._run_tradingview_screener()
        
        # 설치되지 않았다는 안내 메시지가 출력되었는지 확인
        assert any("설치되지 않았습니다" in str(call) for call in mock_print.call_args_list)
        assert res is None

@patch('utils.show_menu', return_value='q')
def test_run_theme_analysis_menu_quit(mock_menu):
    """테마 분석 메인 메뉴에서 q 입력 시 정상 종료 검증"""
    res = theme_analysis.run_theme_analysis()
    assert res is False


# ==========================================
# 2. telegram_bot.py 추가 커버리지
# ==========================================

@pytest.fixture
def commander():
    cmd = TelegramCommander()
    cmd.trader = MagicMock()
    cmd.trader.is_running = False
    return cmd

def test_cmd_analyze_no_chart_data(commander):
    """/analyze 명령어 실행 시 차트 데이터가 없을 경우 방어 로직 검증"""
    with patch('modules.telegram_bot.api.get_chart_data', return_value=None):
        with patch.object(commander, '_send_reply') as mock_reply:
            res = commander._cmd_analyze(['005930'])
            
            # 진행 중 메시지가 한 번 발송되었고
            mock_reply.assert_called_once()
            # 결과로는 차트 데이터 불러오기 실패 문자열을 반환해야 함
            assert "차트 데이터를 불러올 수 없어" in res

def test_cmd_scan_parsing_logic(commander):
    """/scan 명령어의 입력 인자 파싱 및 미설치 방어 검증"""
    with patch.object(commander, '_send_reply') as mock_reply, \
         patch.dict('sys.modules', {'tradingview_screener': None}):
             
        # 미국(u), 모멘텀(m) 조건 파싱
        commander._execute_scan(['u', 'm'])
        
        # 라이브러리가 모킹으로 없기 때문에 설치 안내 메시지가 발송되어야 함
        mock_reply.assert_called_once()
        assert "라이브러리가 설치되지 않았습니다" in mock_reply.call_args[0][0]

@patch('modules.telegram_bot.db_manager.db.get_trades', return_value=[])
@patch('modules.telegram_bot.db_manager.db.get_daily_asset', return_value=1000000)
@patch('modules.telegram_bot.account.get_asset_status_data')
@patch('modules.telegram_bot.analysis.get_domestic_index_data', return_value=pd.DataFrame())
def test_cmd_profit_periods_parsing(mock_index, mock_asset, mock_asset_db, mock_trades, commander):
    """/profit 명령어 기간별 인자(d/w/m) 파싱 분기 검증"""
    mock_asset.return_value = {'tot_asset': 1050000, 'sec_buy': 0, 'sec_pl': 0}
        
        # 반환값 모킹 (포매팅 시 TypeError 방지)
    commander.trader._refine_trade_records.return_value = []
    commander.trader._calculate_statistics.return_value = {
            'sell_trades_exist': True,
            'total_profit': 50000
    }
    
    # 주간 분기
    res_w = commander._cmd_profit(['w'])
    assert "주간 실현 손익" in res_w
    
    # 월간 분기
    res_m = commander._cmd_profit(['m'])
    assert "월간 실현 손익" in res_m
    
    # 숫자 입력 분기 (예: 최근 15일)
    res_num = commander._cmd_profit(['15'])
    assert "최근 15일" in res_num

def test_cmd_report_periods_parsing(commander):
    """/report 명령어 기간별 인자(d/w/m) 파싱 후 trader 호출 연동 검증"""
    commander.trader.get_performance_report.return_value = "Mock Report"
    
    # 월간(m -> 30) 파싱 검증
    res_m = commander._cmd_report(['m'])
    commander.trader.get_performance_report.assert_called_with(days=30)
    assert res_m == "Mock Report"
    
    # 주간(w -> 7) 파싱 검증
    res_w = commander._cmd_report(['w'])
    commander.trader.get_performance_report.assert_called_with(days=7)
    
    # 지정 일자 파싱 검증
    res_10 = commander._cmd_report(['10'])
    commander.trader.get_performance_report.assert_called_with(days=10)