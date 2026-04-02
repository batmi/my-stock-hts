import pytest
from unittest.mock import patch, MagicMock
import pandas as pd
import config
import context
from modules import theme_analysis, manage, trading
from modules.telegram_bot import TelegramCommander
from modules import db_manager

@pytest.fixture(autouse=True)
def setup_and_teardown():
    """테스트 간 상태 간섭 방지"""
    db_manager.db.close_connection()
    yield
    db_manager.db.close_connection()

# =====================================================================
# 1. theme_analysis.py (41% -> Target 70%+)
# =====================================================================

@patch('modules.theme_analysis.requests.get')
def test_fetch_naver_themes_and_detail(mock_get):
    """네이버 금융 테마 크롤링 및 주도주 파싱 로직 커버리지 확보"""
    # 1. 테마 목록 HTML 모킹 (euc-kr 인코딩)
    mock_theme_html = """
    <html><body>
        <table class="type_1 theme">
            <tr>
                <td class="col_type1"><a href="/sise/sise_group_detail.naver?type=theme&no=1">AI 반도체</a></td>
                <td class="col_type2 number">+5.50%</td>
                <td class="col_type3 number">+10.00%</td>
                <td>더미데이터</td>
            </tr>
        </table>
    </body></html>
    """.encode('cp949')
    
    # 2. 테마 상세(주도주) HTML 모킹
    mock_detail_html = """
    <html><body>
        <table class="type_5">
            <tr>
                <td class="name"><a href="/item/main.naver?code=005930">삼성전자</a></td>
                <td>설명</td>
                <td class="number">70,000</td>
                <td>1,000</td>
                <td>+1.50%</td>
            </tr>
            <tr>
                <td class="name"><a href="/item/main.naver?code=000660">SK하이닉스</a></td>
                <td>설명</td>
                <td class="number">150,000</td>
                <td>5,000</td>
                <td>+5.00%</td>
            </tr>
        </table>
    </body></html>
    """.encode('cp949')
    
    # requests.get이 호출 순서대로 다른 HTML을 반환하도록 설정
    mock_get.side_effect = [
        MagicMock(status_code=200, content=mock_theme_html, text=mock_theme_html.decode('cp949')),
        MagicMock(status_code=200, content=mock_detail_html, text=mock_detail_html.decode('cp949'))
    ]
    
    # 테마 목록 가져오기 테스트
    themes = theme_analysis.fetch_naver_themes()
    assert len(themes) == 1
    assert themes[0]['name'] == 'AI 반도체'
    assert themes[0]['rate'] == 5.50
    
    # 테마 상세(주도주) 가져오기 테스트
    theme_analysis._fetch_theme_detail(themes[0])
    assert "SK하이닉스(000660)" in themes[0]['leading'] # 등락률(5.0%)이 더 높은 하이닉스가 먼저 와야 함

@patch('modules.theme_analysis.fetch_naver_themes')
@patch('modules.theme_analysis._fetch_theme_detail')
def test_show_naver_themes_ui(mock_detail, mock_fetch):
    """네이버 테마 리스트 터미널 출력(UI) 분기 테스트"""
    mock_fetch.return_value = [
        {'name': '반도체', 'rate': 5.0, 'rate3': 10.0, 'link': '/link1', 'leading': '삼성전자'}
    ]
    
    with patch('config.console.print') as mock_print:
        theme_analysis._show_naver_themes()
        
    # 테이블이 정상적으로 생성되어 print 되었는지 확인
    assert mock_print.call_count > 0

@patch('rich.prompt.Prompt.ask', side_effect=["test prompt", "q"])
@patch('modules.theme_analysis.analyze_market_trends_with_gemini', return_value="Mocked AI Report")
def test_analyze_with_custom_prompt_ui(mock_analyze, mock_ask):
    """사용자 정의 프롬프트 분석 UI 흐름 테스트"""
    with patch('config.console.print') as mock_print:
        theme_analysis._analyze_with_custom_prompt_ui()
    
    mock_analyze.assert_called_with(custom_prompt="test prompt")
    assert mock_print.call_count > 0

# =====================================================================
# 2. manage.py (47% -> Target 70%+)
# =====================================================================

def test_view_watchlist():
    """관심 종목 리스트 전체 조회(태그 포함) 로직 커버리지"""
    # 설정 목 데이터 주입
    config.session.stock_data = {
        "stocks_kr": [{"code": "005930", "name": "삼성전자"}],
        "stocks_us": [{"code": "AAPL", "name": "Apple"}],
        "etfs_kr": [], "etfs_us": []
    }
    
    with patch('modules.auto_trade.load_restricted_stocks', return_value={"005930": {}}), \
         patch('modules.db_manager.db.get_all_stock_strategies', return_value=[{"code": "AAPL"}]), \
         patch('utils.get_memo_codes', return_value=["005930"]):
             
        with patch('config.console.print') as mock_print:
            manage.view_watchlist()
            
        # 테이블 출력 확인
        assert mock_print.call_count > 0

@patch('config.session.load_stock_config')
@patch('config.session.save_stock_config')
@patch('rich.prompt.Prompt.ask')
def test_reorder_stock_logic(mock_ask, mock_save, mock_load):
    """관심 종목 순서 변경 흐름 테스트"""
    config.session.stock_data = {
        "stocks_kr": [{"name": "A", "code": "1"}, {"name": "B", "code": "2"}, {"name": "C", "code": "3"}],
        "etfs_kr": [], "stocks_us": [], "etfs_us": []
    }
    
    # 1(국내주식) -> 3(C 종목 선택) -> 1(1번 위치로 이동)
    mock_ask.side_effect = ["1", "3", "1"]
    
    manage.reorder_stock()
        
    # 순서가 C, A, B 로 바뀌었는지 확인
    assert config.session.stock_data["stocks_kr"][0]["code"] == "3"
    assert config.session.stock_data["stocks_kr"][1]["code"] == "1"

@patch('config.console.input')
@patch('rich.prompt.Prompt.ask')
def test_add_new_stock_memo(mock_ask, mock_input):
    """새 종목 메모 추가 UI 및 로직 테스트"""
    # 5(직접입력) -> 005930 (종목코드)
    mock_ask.side_effect = ["5", "005930"]
    # 메모 내용 입력 -> 종료 커맨드 입력
    mock_input.side_effect = ["Buy target 60k", "Good company", ":q"]
    
    with patch('api.get_stock_name_by_code', return_value="삼성전자"):
        with patch('utils.add_stock_memo', return_value=True) as mock_add_memo:
            manage.add_new_stock_memo()
            
    mock_add_memo.assert_called_once_with("005930", "삼성전자", "Buy target 60k\nGood company")

# =====================================================================
# 3. trading.py (57% -> Target 80%+)
# =====================================================================

@patch('rich.prompt.Prompt.ask')
@patch('modules.account.fetch_overseas_balance')
def test_select_stock_from_balance_overseas(mock_ovs_balance, mock_ask):
    """해외 잔고에서 매도할 종목 선택 로직 커버리지"""
    # Mock 해외 잔고
    mock_ovs_balance.return_value = [
        {'ovrs_pdno': 'AAPL', 'ovrs_item_name': 'Apple', 'ovrs_cblc_qty': '10', 'pchs_avg_pric': '150.0', 'ovrs_now_pric': '160.0', 'frcr_evlu_pfls_amt': '100.0', 'evlu_pfls_rt': '6.6', '_exchange': 'NASD'}
    ]
    
    # 2(해외주식) -> 1(첫번째 종목)
    mock_ask.side_effect = ["2", "1"]
    
    with patch('config.console.print'):
        code, name, is_overseas, excd, info = trading.select_stock_from_balance("12345678", "01")
        
    assert code == "AAPL"
    assert is_overseas is True
    assert excd == "NAS" # NASD -> NAS 변환 확인
    assert info['qty'] == 10

# =====================================================================
# 4. telegram_bot.py (60% -> Target 80%+)
# =====================================================================

@patch('modules.theme_analysis.generate_portfolio_diagnosis')
@patch('api.get_domestic_balance')
@patch('api.get_deposit_balance')
def test_telegram_cmd_portfolio(mock_dep, mock_bal, mock_gen):
    """/portfolio 명령어 내부 백그라운드 스레드 실행 검증"""
    cmd = TelegramCommander()
    
    mock_bal.return_value = (
        [{'prdt_name': '삼성전자', 'evlu_amt': '500000', 'evlu_pfls_rt': '5.0', 'hldg_qty': '10'}], []
    )
    mock_dep.return_value = {'d2_deposit': 100000}
    mock_gen.return_value = "Mock Portfolio Diagnosis"
    
    with patch.object(cmd, '_send_reply') as mock_reply:
        # 직접 백그라운드 함수 호출하여 로직 커버리지 확보
        cmd._execute_portfolio_diagnosis()
        
        assert mock_reply.call_count > 0
        assert any("Mock Portfolio Diagnosis" in call.args[0] for call in mock_reply.call_args_list)

@patch('modules.theme_analysis.generate_stock_curation')
def test_telegram_cmd_curate(mock_gen):
    """/curate 명령어 내부 백그라운드 스레드 실행 검증"""
    cmd = TelegramCommander()
    mock_gen.return_value = "Mock Curation"
    
    with patch.object(cmd, '_send_reply') as mock_reply:
        cmd._execute_curate()
        
        assert mock_reply.call_count > 0
        assert any("Mock Curation" in call.args[0] for call in mock_reply.call_args_list)

def test_telegram_cmd_memo():
    """/memo 명령어 서브 분기 (추가/삭제/조회) 테스트"""
    cmd = TelegramCommander()
    
    # 1. 전체 조회 (데이터 없음)
    with patch('utils.get_all_stock_memos', return_value=[]):
        res = cmd._cmd_memo([])
        assert "없습니다" in res
        
    # 2. 개별 종목 메모 추가
    with patch('utils.add_stock_memo', return_value=True):
        with patch('api.get_stock_name_by_code', return_value="삼성전자"):
            res = cmd._cmd_memo(["a", "005930", "테스트", "메모입니다"])
            assert "추가되었습니다" in res
            
    # 3. ID로 삭제
    with patch('utils.delete_stock_memo_by_id', return_value=True):
        res = cmd._cmd_memo(["d", "5"])
        assert "삭제되었습니다" in res
        
    # 4. 종목 코드로 전체 삭제
    # 한글 종목명 해석을 위해 세션 데이터 주입
    config.session.stock_data = {"stocks_kr": [{"code": "005930", "name": "삼성전자"}]}
    with patch('utils.delete_all_stock_memos'):
        with patch('api.get_stock_name_by_code', return_value="삼성전자"):
            res = cmd._cmd_memo(["d", "삼성전자"])
            assert "모든 메모가 삭제" in res

@patch('modules.telegram_bot.api.get_yf_fast_info')
@patch('modules.telegram_bot.analysis.get_domestic_index_data')
def test_telegram_get_market_status_branches(mock_dom_idx, mock_yf_info):
    """/market 명령어에서 상승/하락/환율/금리/분류 등 다양한 분기 처리 검증"""
    cmd = TelegramCommander()
    
    # 국내 지수 모킹 (코스피: 강세장 조건 만족하도록 설정)
    df_kospi = pd.DataFrame({'close': [2500, 2600]})
    mock_dom_idx.return_value = df_kospi
    
    # 해외/환율/원자재 모킹 (yf fast_info)
    def mock_yf_side_effect(ticker):
        if ticker == "KRW=X": # 환율 (패닉 구간)
            return {'last_price': 1550.0, 'regular_market_previous_close': 1500.0}
        if ticker == "^TNX": # 미국채 10년물 (골디락스 구간)
            return {'last_price': 4.0, 'regular_market_previous_close': 3.9}
        if ticker == "CL=F": # 원유 (에너지 쇼크)
            return {'last_price': 130.0, 'regular_market_previous_close': 120.0}
        return {'last_price': 100.0, 'regular_market_previous_close': 100.0}
        
    mock_yf_info.side_effect = mock_yf_side_effect
    
    # 시장 국면 지표(ADX) 모킹
    with patch('indicators.calculate_indicators', return_value={'adx': 40.0}):
        res = cmd._get_market_status(["국내 지수 (Domestic Indices)", "금리 및 환율 (Rates & FX)", "원자재 (Commodities)"])
        
    assert "코스피" in res
    assert "1,550.00원" in res  # 환율 원 단위 포맷 확인
    assert "4.00%" in res       # 금리 % 단위 포맷 확인
    assert "130.00" in res      # 원유 가격 확인