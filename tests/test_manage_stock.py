import pytest
import json
import os
from unittest.mock import patch, mock_open
import config
from core.session import SessionManager
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

def test_save_stock_config(session_manager, tmp_path, monkeypatch):
    """설정 파일 저장 테스트.

    [2026-09-04] 저장은 core/jsonio.save_json 을 타며 **원자적**이다(임시 파일에 쓰고
    os.replace). 그래서 '최종 경로를 'w' 로 연다'는 종전 검증은 더 이상 성립하지 않는다
    — 그 동작이야말로 쓰다 만 관심종목 파일을 남기던 원인이라 일부러 없앴다.
    구현 대신 결과를 본다: 파일에 그대로 들어갔고, 임시 파일이 남지 않는다.
    """
    target = tmp_path / "stock.json"
    monkeypatch.setattr(config, "STOCK_DATA_FILE", str(target))
    data = {"stocks_kr": [{"name": "테스트", "code": "123456"}]}

    session_manager.save_stock_config(data)

    assert json.loads(target.read_text(encoding="utf-8")) == data
    assert session_manager.stock_data == data
    assert [p.name for p in tmp_path.iterdir() if p.name.endswith(".tmp")] == []

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