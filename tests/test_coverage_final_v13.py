import pytest
from unittest.mock import patch, MagicMock
import config
import context
from modules import manage, theme_analysis, trading, db_manager
import utils

@pytest.fixture(autouse=True)
def setup_and_teardown():
    yield
    try:
        db_manager.db.close_connection()
    except: pass

# ==========================================
# 1. manage.py 추가 커버리지 (60% -> Target 80%)
# ==========================================
@patch('rich.prompt.Prompt.ask')
def test_manage_delete_stock_with_memo(mock_ask):
    """관심 종목 삭제 시, 연관된 메모도 함께 삭제하는 흐름 테스트"""
    # 1. 데이터 준비
    config.session.stock_data = {"stocks_kr": [{"code": "005930", "name": "삼성전자"}]}
    
    # 2. Mock 설정
    # 그룹선택(1) -> 종목선택(1) -> 삭제확인(y) -> 메모삭제확인(y)
    mock_ask.side_effect = ["1", "1", "y", "y"]
    
    with patch('utils.get_memo_codes', return_value=["005930"]), \
         patch('utils.delete_all_stock_memos') as mock_delete_memo, \
         patch('config.session.save_stock_config'), \
         patch('config.session.load_stock_config'): # [수정] 원본 파일 로드 차단
        
        # 3. 실행
        manage.delete_stock()
        
    # 4. 검증
    assert not config.session.stock_data["stocks_kr"] # 종목이 삭제되었는가?
    mock_delete_memo.assert_called_once_with("005930") # 메모 삭제 함수가 호출되었는가?

@patch('rich.prompt.Prompt.ask')
def test_manage_stock_memos_by_mode_flow(mock_ask):
    """메모 관리 메뉴의 전체적인 흐름 (조회->삭제->추가) 테스트"""
    # 1. 데이터 준비
    mock_memos = [{'id': 1, 'code': '005930', 'name': '삼성전자', 'memo': 'Test', 'updated_at': '2023-01-01'}]
    
    # 2. Mock 설정
    # 상세조회(1) -> 뒤로가기(b) -> 삭제모드(d) -> 삭제대상(1) -> 확인(y) -> 추가모드(a) -> ...
    mock_ask.side_effect = [
        "1", "b", # 1. 상세 조회 후 뒤로가기
        "d",       # 2. 삭제 모드 진입
        "1", "y", # 3. 1번 메모 삭제
        "a",       # 4. 추가 모드 진입
        "5", "000660", # 5. 직접 입력으로 하이닉스 선택
        "q"        # 6. 메모 입력 취소
    ]
    
    with patch('utils.get_all_stock_memos', side_effect=[mock_memos, mock_memos, []]), \
         patch('utils.get_stock_memos', return_value=mock_memos), \
         patch('utils.delete_stock_memo_by_id', return_value=True), \
         patch('api.get_stock_name_by_code', return_value="SK하이닉스"):
        
        # 3. 실행
        manage.manage_stock_memos_by_mode('view')

# ==========================================
# 2. theme_analysis.py 추가 커버리지 (41% -> Target 70%)
# ==========================================
def test_evaluate_market_indicator_all_cases():
    """매크로 지표 평가 함수 모든 분기 테스트"""
    assert "시스템 위기" in theme_analysis.evaluate_market_indicator("미국채 10년물 금리", 5.5)
    assert "골디락스" in theme_analysis.evaluate_market_indicator("WTI 원유", 70)
    assert "패닉 구간" in theme_analysis.evaluate_market_indicator("달러환율", 1550)
    assert "안정" in theme_analysis.evaluate_market_indicator("VIX (변동성)", 15)
    assert "신고가 랠리" in theme_analysis.evaluate_market_indicator("SOX (반도체)", 100, yh_rate=-3.0)
    assert "침체/약세장" in theme_analysis.evaluate_market_indicator("기타지수", 100, yh_rate=-25.0)

@patch('modules.theme_analysis.genai.GenerativeModel')
def test_gemini_api_error_handling(mock_model):
    """Gemini API 호출 시 발생하는 다양한 예외 처리 로직 테스트"""
    # 1. Rate Limit (429)
    mock_model.return_value.generate_content.side_effect = Exception("429 RESOURCE_EXHAUSTED")
    with patch('config.console.print') as mock_print:
        res = theme_analysis.analyze_market_trends_with_gemini()
        assert res is None # [수정] 해당 함수는 예외 시 None을 반환함
        assert any("호출 한도 초과" in str(c) for c in mock_print.call_args_list)
    
    # 2. 모델명 오류 (404)
    mock_model.return_value.generate_content.side_effect = Exception("404 NOT_FOUND")
    res = theme_analysis.analyze_stock_with_gemini("005930", "삼성", "")
    assert "모델을 찾을 수 없습니다" in res
    
    # 3. 타임아웃
    mock_model.return_value.generate_content.side_effect = Exception("TimeoutError")
    res = theme_analysis.evaluate_backtest_with_gemini("005930", "삼성", "")
    assert "응답 대기 시간 초과" in res

# ==========================================
# 3. trading.py 추가 커버리지 (57% -> Target 70%)
# ==========================================
@patch('rich.prompt.Prompt.ask')
@patch('modules.trading.api.place_order')
@patch('modules.trading.api.fetch_buyable_quantity', return_value=100)
@patch('modules.trading.api.get_current_price', return_value=60000)
@patch('modules.trading.api.get_current_price_data')
@patch('modules.trading.api.get_stock_name_by_code', return_value="삼성전자")
@patch('modules.trading.api.get_chart_data', return_value=None)
@patch('modules.trading.show_open_orders')
@patch('modules.trading.api.send_telegram_message')
def test_trading_send_order_buy_direct(mock_tg, mock_show, mock_chart, mock_name, mock_cp_data, mock_price, mock_qty, mock_place, mock_ask):
    """매수 주문 (직접 입력) 흐름 테스트"""
    # 유효성 검사를 통과하기 위한 현재가 상세 응답 모킹
    mock_cp_data.return_value = {'rt_cd': '0', 'output': {'stck_prpr': '60000'}}
    
    # 실제 send_order('buy') 내부 메뉴 흐름에 맞춘 정확한 입력 시나리오
    # 5(직접입력) -> 005930(코드) -> y(진행확인) -> 10(수량) -> 60000(단가) -> y(최종확인)
    mock_ask.side_effect = ["5", "005930", "y", "10", "60000", "y"]
    mock_place.return_value = {'rt_cd': '0', 'output': {'ODNO': '12345'}}
    
    config.session.is_simulation = True # 불필요한 계좌 선택 메뉴 우회
    config.session.cano = "12345678"
    config.session.acnt_prdt_cd = "01"
    config.session.auto_cano = ""
    
    with patch('config.console.print'):
        trading.send_order('buy')
        
    mock_place.assert_called()
    args, kwargs = mock_place.call_args
    # [수정] api.place_order(market, action, code, qty, price, ord_dvsn) 는 위치 인자를 사용함
    assert args[2] == '005930'
    assert str(args[4]) == '60000'