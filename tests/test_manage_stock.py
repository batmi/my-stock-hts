import pytest
import json
import os
from unittest.mock import patch, mock_open
import config
from session import SessionManager
from modules import db_manager

@pytest.fixture(autouse=True)
def cleanup_db_connection():
    yield
    db_manager.db.close_connection()

@pytest.fixture
def session_manager():
    return SessionManager()

def test_load_stock_config_file_not_exists(session_manager):
    """설정 파일이 없을 때 빈 설정 로드"""
    with patch("os.path.exists", return_value=False):
        session_manager.load_stock_config()
        assert session_manager.stock_data == {"stocks_kr": [], "etfs_kr": [], "stocks_us": [], "etfs_us": []}

def test_load_stock_config_success(session_manager):
    """설정 파일 로드 성공 테스트"""
    mock_data = {
        "stocks_kr": [{"name": "삼성전자", "code": "005930"}],
        "etfs_kr": [],
        "stocks_us": [],
        "etfs_us": []
    }
    json_str = json.dumps(mock_data)
    
    with patch("os.path.exists", return_value=True):
        with patch("builtins.open", mock_open(read_data=json_str)):
            session_manager.load_stock_config()
            assert len(session_manager.stock_data["stocks_kr"]) == 1
            assert session_manager.stock_data["stocks_kr"][0]["code"] == "005930"

def test_save_stock_config(session_manager):
    """설정 파일 저장 테스트"""
    data = {"stocks_kr": [{"name": "테스트", "code": "123456"}]}
    
    with patch("builtins.open", mock_open()) as mock_file:
        session_manager.save_stock_config(data)
        
        mock_file.assert_called_with(config.STOCK_DATA_FILE, 'w', encoding='utf-8')
        handle = mock_file()
        # json.dump가 write를 호출했는지 확인
        assert handle.write.called

def test_add_stock_logic(session_manager):
    """종목 추가 로직 시뮬레이션"""
    # 초기 상태
    session_manager.stock_data = {"stocks_kr": [], "etfs_kr": [], "stocks_us": [], "etfs_us": []}
    
    # 종목 추가
    new_stock = {"name": "SK하이닉스", "code": "000660"}
    session_manager.stock_data["stocks_kr"].append(new_stock)
    
    # 저장 호출 검증
    with patch.object(session_manager, 'save_stock_config') as mock_save:
        session_manager.save_stock_config(session_manager.stock_data)
        mock_save.assert_called_once()
        args, _ = mock_save.call_args
        assert args[0]["stocks_kr"][0]["code"] == "000660"

def test_remove_stock_logic(session_manager):
    """종목 삭제 로직 시뮬레이션"""
    # 초기 상태
    session_manager.stock_data = {
        "stocks_kr": [{"name": "삼성전자", "code": "005930"}, {"name": "SK하이닉스", "code": "000660"}],
        "etfs_kr": [], "stocks_us": [], "etfs_us": []
    }
    
    # 삭제 대상: 삼성전자
    target_code = "005930"
    session_manager.stock_data["stocks_kr"] = [s for s in session_manager.stock_data["stocks_kr"] if s["code"] != target_code]
    
    assert len(session_manager.stock_data["stocks_kr"]) == 1
    assert session_manager.stock_data["stocks_kr"][0]["code"] == "000660"

def test_update_exchange_cache(session_manager):
    """거래소 정보 캐시 업데이트 및 저장 테스트"""
    session_manager.stock_data = {
        "stocks_us": [{"name": "Apple", "code": "AAPL"}]
    }
    
    with patch.object(session_manager, 'save_stock_config') as mock_save:
        session_manager.update_cache_and_save("AAPL", "NAS")
        
        assert session_manager.exchange_cache["AAPL"] == "NAS"
        assert session_manager.stock_data["stocks_us"][0]["exchange"] == "NAS"
        mock_save.assert_called_once()