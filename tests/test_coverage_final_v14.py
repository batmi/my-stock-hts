import pytest
from unittest.mock import patch, MagicMock, ANY
import pandas as pd
import numpy as np
from datetime import datetime

import api
import config
from core import utils
from core import context
from modules import analysis, auto_trade, market, theme_analysis, db_manager
from modules.telegram_bot import TelegramCommander

@pytest.fixture(autouse=True)
def cleanup_db_connection():
    yield
    try:
        db_manager.db.close_connection()
    except: pass

# ==========================================================
# 1. api.py 커버리지 (메시지 분할 전송, 마크다운 링크 파싱)
# ==========================================================
@patch('api.requests.post')
def test_api_send_telegram_chunking_and_markdown(mock_post):
    """4000자 초과 메시지 자동 분할 및 마크다운 링크 변환 커버리지"""
    config.TELEGRAM_BOT_TOKEN = "TEST"
    config.TELEGRAM_CHAT_ID = "TEST"
    # HTTP 200 성공 응답 모킹
    mock_post.return_value = MagicMock(status_code=200)
    
    # 마크다운 링크가 포함된 4500자 길이의 더미 메시지 생성
    long_msg = "이것은 긴 메시지입니다. [구글링크](https://google.com)\n" * 150
    
    # 실제 전송 함수 호출
    api.send_telegram_message(long_msg, reply_markup={"keyboard": []}, sync=True)
    
    # 4000자 제한으로 인해 최소 2번 이상 post가 호출되어야 함
    assert mock_post.call_count >= 2
    
    # 전송된 데이터 중 마크다운이 HTML a 태그로 잘 변환되었는지 검증
    args, kwargs = mock_post.call_args_list[0]
    sent_text = kwargs['data']['text']
    assert '<a href="https://google.com">구글링크</a>' in sent_text

# ==========================================================
# 2. modules/telegram_bot.py 커버리지
# ==========================================================
@patch('modules.auto_trade.api.get_domestic_balance')
@patch('modules.auto_trade.api.get_deposit_balance')
def test_autotrader_status_market_filter_skip(mock_dep, mock_bal):
    """상태 요약 메시지에서 하락장 필터링 보류 카운트 커버리지"""
    trader = auto_trade.AutoTrader()
    config.USE_MARKET_FILTER = True
    trader.skipped_by_market_filter_count = {"KOSPI": 5, "KOSDAQ": 0}
    
    mock_bal.return_value = ([], [{'scts_evlu_amt': '0'}])
    mock_dep.return_value = {'d2_deposit': 1000000}
    
    res = trader.get_status_message()
    assert "KOSPI 5종목" in res

@patch('modules.telegram_bot.api.get_chart_data', return_value=pd.DataFrame())
def test_telegram_cmd_chart_empty_data(mock_chart):
    """/chart 명령어 차트 데이터가 없을 때 예외 커버리지"""
    cmd = TelegramCommander()
    with patch.object(cmd, '_send_reply') as mock_reply:
        cmd._cmd_chart(['d', '005930'])
        assert any("실패" in str(call) or "오류" in str(call) for call in mock_reply.call_args_list)

# ==========================================================
# 3. modules/theme_analysis.py 커버리지
# ==========================================================
@patch('modules.theme_analysis.genai.GenerativeModel')
def test_theme_analysis_gemini_400_error(mock_model):
    """Gemini API Tools 400 에러(Search Grounding 미지원) 커버리지"""
    mock_model.return_value.generate_content.side_effect = Exception("400 INVALID_ARGUMENT: tools")
    
    with patch('config.console.print') as mock_print:
        res = theme_analysis.analyze_stock_with_gemini("005930", "삼성전자", "기술적 정보")
        assert "Google Search 도구 사용 불가" in res
        
        res2 = theme_analysis.analyze_market_trends_with_gemini()
        assert res2 is None
        assert any("Google Search 도구 사용 불가" in str(c) for c in mock_print.call_args_list)

@patch('modules.theme_analysis.analyze_market_trends_with_gemini', return_value="새로운 AI 분석")
@patch('modules.theme_analysis._load_theme_analysis', return_value=None)
@patch('modules.theme_analysis._save_theme_analysis')
def test_analyze_with_gemini_ui_no_cache(mock_save, mock_load, mock_analyze):
    """캐시가 없을 때 Gemini 분석을 새로 수행하고 저장하는 로직 커버리지"""
    with patch('config.console.print'):
        theme_analysis._analyze_with_gemini_ui()
        mock_analyze.assert_called_once()
        mock_save.assert_called_once_with("새로운 AI 분석")

@patch('modules.theme_analysis.Prompt.ask')
def test_run_tradingview_screener_market_choice(mock_ask):
    """TradingView 스크리너 미국 주식(2) 선택 로직 커버리지"""
    # 2(미국) -> 1(눌림목) -> n(상세진단 안함)
    mock_ask.side_effect = ["2", "1", "n"]
    
    # tradingview_screener 모킹
    mock_query = MagicMock()
    mock_query.set_markets.return_value = mock_query
    mock_query.select.return_value = mock_query
    mock_query.where.return_value = mock_query
    mock_query.order_by.return_value = mock_query
    mock_query.limit.return_value = mock_query
    
    df_mock = pd.DataFrame({
        'name': ['AAPL'], 'description': ['Apple'], 'close': [150], 'change': [1.5],
        'volume': [10000], 'RSI': [40], 'SMA20': [140]
    })
    mock_query.get_scanner_data.return_value = (1, df_mock)
    
    # tradingview_screener 전체를 MagicMock으로 대체하면 Column(...) > 1e11 비교가 TypeError를
    # 내므로, 유동성 필터 함수를 mock하여 실제 Column 비교를 우회한다.
    with patch.dict('sys.modules', {'tradingview_screener': MagicMock(Query=MagicMock(return_value=mock_query))}), \
         patch('modules.theme_analysis.screener_liquidity_filters', return_value=([], "테스트필터")), \
         patch('config.console.print'):
        theme_analysis._run_tradingview_screener()
        mock_query.set_markets.assert_called_with('america')

# ==========================================================
# 4. utils.py 커버리지 (페이징 UI 루프)
# ==========================================================
@patch('rich.prompt.Prompt.ask')
def test_utils_search_stock_in_list_pagination(mock_ask):
    """종목 리스트가 15개가 넘어갈 때 발생하는 페이징(n/p) 로직 커버리지"""
    # 20개의 더미 종목 생성
    dummy_list = [{'code': f"{i:06d}", 'name': f"종목{i}"} for i in range(1, 21)]
    
    # 동작 시나리오: n(다음페이지) -> p(이전페이지) -> 1(첫번째 항목 선택)
    mock_ask.side_effect = ["n", "p", "1"]
    
    with patch('config.console.print'):
        idx, item = utils.search_stock_in_list(dummy_list)
        assert idx == 0
        assert item['name'] == "종목1"

# ==========================================================
# 5. modules/market.py 커버리지 (지수 상태별 색상 렌더링)
# ==========================================================
@patch('modules.market.api.fetch_yfinance_data', return_value=pd.DataFrame())
@patch('modules.market.api.get_yf_fast_info', return_value=None)
def test_market_index_specific_rendering(mock_fast_info, mock_fetch):
    """특정 지수(VIX, WTI 원유 등)의 조건별 색상 렌더링 분기 커버리지"""
    # fast_info 실패 시 분봉(5m)을 단건 지연조회하므로, 네트워크 격리를 위해 빈 분봉을 반환하도록 모킹
    df = pd.DataFrame({
        'close': [45.0] * 20, 'open': [45.0]*20, 'high': [45.0]*20, 'low': [45.0]*20, 'volume': [100]*20,
        'date': pd.date_range('2023-01-01', periods=20)
    })
    df.set_index('date', inplace=True)
    
    # 1. VIX (변동성) - 45.0이면 주황색 경계 상태
    res1 = market._process_index_worker("VIX (변동성)", "^VIX", df, pd.DataFrame())
    assert res1['status'] == 'success'
    assert "magenta" in res1['row_data'][0]
    
    # 2. WTI 원유 - 45.0이면 파란색 심각한 수요 파괴 상태
    res2 = market._process_index_worker("WTI 원유", "CL=F", df, pd.DataFrame())
    assert "blue" in res2['row_data'][0]

# ==========================================================
# 6. modules/analysis.py 커버리지
# ==========================================================
@patch('rich.prompt.Prompt.ask')
def test_analysis_get_params_weight_validation(mock_ask):
    """분석 파라미터 입력 시 가중치 합계 예외 처리 무한루프 커버리지"""
    # 시나리오: 조건변경(y) -> (매수설정들 엔터 패스) -> 가중치변경루프
    # 가중치 오입력(합계 20) -> 정상입력(4/2.5/1.5/2.0) -> 출력필터(1)
    mock_ask.side_effect = [
        "y",  # 변경하시겠습니까
        "", "", "", "",  # 매수 조건 패스
        "10", "10", "0", "0",  # 합계 20점 (경고 발생 및 루프 재시작)
        "4.0", "2.5", "1.5", "2.0",  # 합계 10점 (통과)
        "1" # 필터
    ]
    
    with patch('config.console.print') as mock_print:
        params = analysis.get_analysis_params()
        assert params['WEIGHTS']['TREND'] == 4.0
        # 경고 메시지가 출력되었는지 확인
        assert any("가중치 합계가" in str(c) for c in mock_print.call_args_list)

@patch('pandas.ExcelWriter')
@patch('modules.analysis.api.get_current_price_data')
def test_analysis_save_excel_formatting(mock_cp, mock_writer):
    """엑셀 저장 시 openpyxl 서식(글자색상 등) 적용 로직 커버리지"""
    # _analyze_stock_worker 대신 가짜 분석 결과 주입을 위해 _analyze_stock_worker 모킹
    with patch('modules.analysis._analyze_stock_worker') as mock_worker, \
         patch('modules.analysis._get_master_stock_list', return_value=[{'code':'005930', 'name':'삼성'}]), \
         patch('rich.prompt.Prompt.ask', return_value='y'):
             
        # 다양한 상태를 가진 더미 응답 생성 (색상 포맷팅 분기 타격)
        mock_worker.side_effect = [
            {'code': '005930', 'name': '삼성', 'price': 100, 'w52_pos': 50, 'score': 9.0, 'state': '매수', 'state_reason': '', 'rsi': 50, 'adx': 30, 'cci': 100, 'psar': 90, 'obv_trend': True, 'is_target': True, 'vol_strength': 100},
            {'code': '000660', 'name': '하닉', 'price': 100, 'w52_pos': 50, 'score': 7.0, 'state': '상승', 'state_reason': '', 'rsi': 50, 'adx': 30, 'cci': 100, 'psar': 90, 'obv_trend': True, 'is_target': True, 'vol_strength': 100},
            {'code': '373220', 'name': '엔솔', 'price': 100, 'w52_pos': 50, 'score': 4.0, 'state': '매도', 'state_reason': '', 'rsi': 50, 'adx': 30, 'cci': 100, 'psar': 90, 'obv_trend': True, 'is_target': True, 'vol_strength': 100},
        ]
        # 삼성, 하닉, 엔솔 3번 호출되도록 master_list 확장
        with patch('modules.analysis._get_master_stock_list', return_value=[
            {'code':'005930', 'name':'삼성'}, {'code':'000660', 'name':'하닉'}, {'code':'373220', 'name':'엔솔'}
        ]):
            mock_cp.return_value = {'rt_cd': '0', 'output': {'bstp_kor_isnm': 'IT'}}
            
            # openpyxl 객체 모킹
            mock_ws = MagicMock()
            mock_writer.return_value.__enter__.return_value.sheets = {'KOSPI': mock_ws, 'KOSDAQ': mock_ws}
            mock_ws.max_row = 4
            
            # 에러 방지를 위해 간단히 헤더 모킹
            cell_mock = MagicMock()
            cell_mock.value = "상태"
            mock_ws.__getitem__.return_value = [cell_mock] * 15 # header row
            mock_ws.cell.return_value = MagicMock(value="매수")
            
            with patch('config.console.print'):
                analysis.save_all_market_analysis()
        
        assert mock_writer.call_count > 0