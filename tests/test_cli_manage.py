import pytest
from unittest.mock import patch, MagicMock
from modules import manage
import config
from modules import db_manager

@pytest.fixture(autouse=True)
def cleanup_db_connection():
    yield
    db_manager.db.close_connection()

@patch('rich.prompt.Prompt.ask')
@patch('modules.manage.get_current_price')
@patch('modules.manage.delete_stock')
def test_manage_stock_menu(mock_delete, mock_add, mock_ask):
    """종목 관리 메뉴 테스트"""
    # 1(추가) 선택
    mock_ask.side_effect = ["1"]
    
    manage.manage_stock_menu()
    
    mock_add.assert_called_once()

@patch('config.session.load_stock_config')
@patch('rich.prompt.Prompt.ask')
@patch('modules.manage.api.get_current_price_data')
def test_get_current_price_add_flow(mock_api, mock_ask, mock_load_config):
    """종목 검색 및 추가 흐름 테스트"""
    # 시나리오: 코드입력(005930) -> 추가확인(y) -> 이름확인(엔터) -> 그룹선택(1:국내주식) -> 위치지정(엔터) -> (여분)
    mock_ask.side_effect = ["005930", "y", "", "1", "", ""]
    
    # API Mock
    mock_api.return_value = {
        'rt_cd': '0', 
        'output': {'stck_prpr': '60000', 'rprs_mrkt_kor_name': 'KOSPI'}
    }
    
    # 세션 데이터 초기화
    config.session.stock_data = {"stocks_kr": [], "etfs_kr": [], "stocks_us": [], "etfs_us": []}
    
    with patch('modules.manage.api.get_stock_name_by_code', return_value="삼성전자"):
        with patch('config.session.save_stock_config'): # 파일 저장 방지
            manage.get_current_price(mode='add')
            
    # 데이터 추가 확인
    assert len(config.session.stock_data['stocks_kr']) == 1
    assert config.session.stock_data['stocks_kr'][0]['code'] == "005930"

@patch('config.session.load_stock_config')
@patch('rich.prompt.Prompt.ask')
def test_delete_stock_flow(mock_ask, mock_load_config):
    """종목 삭제 흐름 테스트"""
    # 초기 데이터 설정
    config.session.stock_data = {
        "stocks_kr": [{"name": "삼성전자", "code": "005930"}],
        "etfs_kr": [], "stocks_us": [], "etfs_us": []
    }
    
    # 시나리오: 그룹선택(1) -> 번호선택(1) -> 확인(y) -> 메모삭제(n)
    mock_ask.side_effect = ["1", "1", "y", "n"]
    
    with patch('config.session.save_stock_config'):
        manage.delete_stock()
        
    # 데이터 삭제 확인
    assert len(config.session.stock_data['stocks_kr']) == 0