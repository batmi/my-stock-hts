import pytest
from unittest.mock import patch, MagicMock
from modules import manage
import config

@patch('rich.prompt.Prompt.ask')
def test_delete_stock_cancel(mock_ask):
    """종목 삭제 취소 테스트"""
    # 그룹 선택(1) -> 번호 선택(1) -> 삭제 확인(n)
    mock_ask.side_effect = ["1", "1", "n"]
    
    config.session.stock_data = {
        "stocks_kr": [{"name": "삼성전자", "code": "005930"}],
        "etfs_kr": [], "stocks_us": [], "etfs_us": []
    }
    
    with patch('config.console.print') as mock_print:
        manage.delete_stock()
        # 삭제되지 않았음을 확인
        assert len(config.session.stock_data["stocks_kr"]) == 1

@patch('rich.prompt.Prompt.ask')
def test_delete_stock_invalid_input(mock_ask):
    """종목 삭제 잘못된 입력 테스트"""
    # 그룹 선택(1) -> 범위 밖 입력(99, 검색어로 처리되어 결과 없음) -> 종료(q)
    mock_ask.side_effect = ["1", "99", "q"]

    config.session.stock_data = {
        "stocks_kr": [{"name": "삼성전자", "code": "005930"}],
        "etfs_kr": [], "stocks_us": [], "etfs_us": []
    }

    with patch('config.console.print') as mock_print:
        manage.delete_stock()
        # 목록 범위를 벗어난 숫자는 검색어로 처리되어 '검색 결과가 없습니다' 안내가 출력된다
        assert any("검색 결과가 없습니다" in str(c) for c in mock_print.call_args_list)

@patch('rich.prompt.Prompt.ask')
def test_get_current_price_invalid_code(mock_ask):
    """존재하지 않는 종목 코드 입력 테스트"""
    # 코드 입력(000000) -> 종료(q)
    mock_ask.side_effect = ["000000", "q"]
    
    with patch('modules.manage.api.get_current_price_data') as mock_api:
        mock_api.return_value = {'rt_cd': '1', 'msg1': '존재하지 않는 종목'}
        
        with patch('config.console.print') as mock_print:
            manage.get_current_price()
            assert any("조회 실패" in str(c) for c in mock_print.call_args_list)