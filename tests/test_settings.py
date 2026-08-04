import pytest
import json
import os
from unittest.mock import patch, mock_open, MagicMock
import config
from modules import settings

@pytest.fixture
def temp_config_dir(tmp_path):
    """테스트용 임시 설정 디렉토리 생성"""
    config_dir = tmp_path / "json"
    config_dir.mkdir()
    return config_dir

def test_load_dynamic_config_updates_values(temp_config_dir):
    """동적 설정 파일(dynamic_config.json) 로드 시 값이 업데이트되는지 테스트"""
    config_file = temp_config_dir / "dynamic_config.json"
    
    # 1. 임시 설정 파일 작성 (변경할 값)
    new_settings = {
        "SYSTEM_MAX_HOLDINGS": 99,
        "ANALYSIS_THRESHOLDS": {
            "BUY_SCORE": 9.9
        }
    }
    with open(config_file, 'w', encoding='utf-8') as f:
        json.dump(new_settings, f)
    
    # 2. 초기값 백업
    original_max_holdings = config.settings.SYSTEM_MAX_HOLDINGS
    original_buy_score = config.settings.ANALYSIS_THRESHOLDS["BUY_SCORE"]
    
    # config.JSON_DIR을 임시 경로로 모킹하여 테스트
    with patch("config.JSON_DIR", str(temp_config_dir)):
        try:
            # 3. 로드 함수 실행
            config.load_dynamic_config()
            
            # 4. 검증
            assert config.settings.SYSTEM_MAX_HOLDINGS == 99
            assert config.settings.ANALYSIS_THRESHOLDS["BUY_SCORE"] == 9.9
            
        finally:
            # 5. 복구 (다른 테스트에 영향 주지 않도록)
            config.SYSTEM_MAX_HOLDINGS = original_max_holdings
            config.ANALYSIS_THRESHOLDS["BUY_SCORE"] = original_buy_score

def test_load_dynamic_config_partial_update(temp_config_dir):
    """일부 설정만 변경했을 때 다른 설정은 유지되는지 테스트 (Dictionary Update)"""
    config_file = temp_config_dir / "dynamic_config.json"
    
    # SELL_STRATEGY의 일부(손절률)만 변경
    new_settings = {
        "SELL_STRATEGY": {
            "STOP_LOSS_RATE": -50.0
        }
    }
    with open(config_file, 'w', encoding='utf-8') as f:
        json.dump(new_settings, f)
        
    original_sl = config.settings.SELL_STRATEGY["STOP_LOSS_RATE"]
    original_tp = config.settings.SELL_STRATEGY["TAKE_PROFIT_RATE"]
    
    with patch("config.JSON_DIR", str(temp_config_dir)):
        try:
            config.load_dynamic_config()
            
            # 변경된 값 확인
            assert config.settings.SELL_STRATEGY["STOP_LOSS_RATE"] == -50.0
            # 변경하지 않은 값은 그대로 유지되어야 함
            assert config.settings.SELL_STRATEGY["TAKE_PROFIT_RATE"] == original_tp
            
        finally:
            config.settings.SELL_STRATEGY["STOP_LOSS_RATE"] = original_sl

def test_load_dynamic_config_file_not_found():
    """설정 파일이 없을 때 에러 없이 넘어가는지 테스트"""
    # 존재하지 않는 경로로 모킹
    with patch("config.JSON_DIR", "/non/existent/path"):
        try:
            config.load_dynamic_config()
        except Exception as e:
            pytest.fail(f"설정 파일이 없을 때 예외가 발생했습니다: {e}")

def test_load_dynamic_config_invalid_json(temp_config_dir):
    """잘못된 JSON 파일이 있을 때 예외 처리 테스트"""
    config_file = temp_config_dir / "dynamic_config.json"
    
    # 유효하지 않은 JSON 작성
    with open(config_file, 'w', encoding='utf-8') as f:
        f.write("{invalid_json_format}")
        
    with patch("config.JSON_DIR", str(temp_config_dir)):
        # 예외가 발생하더라도 프로그램이 죽지 않고 로그만 찍히는지 확인 (함수 내 try-except)
        try:
            config.load_dynamic_config()
        except Exception as e:
            pytest.fail(f"잘못된 JSON 파일 처리 중 예외가 전파되었습니다: {e}")

def test_save_dynamic_config():
    """_save_dynamic_config 함수가 json 데이터를 파일에 정상적으로 쓰는지 검증"""
    m_open = mock_open()
    with patch('builtins.open', m_open), \
         patch('modules.settings.console.print'), \
         patch('modules.settings.check_and_update_active_preset'):
         
         # [주의] config.ANALYSIS_THRESHOLDS 는 프로세스 전역이라 반드시 되돌려야 한다.
         #  복구하지 않으면 같은 프로세스의 뒤 테스트가 BUY_SCORE=99.9 를 보고 매수 0건이 되어
         #  엉뚱한 곳(포트폴리오 백테스트 3건)이 실패한다 — 실제로 그렇게 새고 있었다.
         orig_preset = config.settings.ACTIVE_PRESET
         orig_score = config.ANALYSIS_THRESHOLDS["BUY_SCORE"]
         try:
             config.settings.ACTIVE_PRESET = "test_preset"
             config.ANALYSIS_THRESHOLDS["BUY_SCORE"] = 99.9

             settings._save_dynamic_config()

             m_open.assert_called_once()
             # 쓰여진 데이터 검증
             handle = m_open()
             written_data = "".join([call[0][0] for call in handle.write.call_args_list])
             data = json.loads(written_data)
             assert data["ACTIVE_PRESET"] == "test_preset"
             assert data["ANALYSIS_THRESHOLDS"]["BUY_SCORE"] == 99.9
         finally:
             config.settings.ACTIVE_PRESET = orig_preset
             config.ANALYSIS_THRESHOLDS["BUY_SCORE"] = orig_score

@patch('modules.settings.console.print')
def test_view_system_config(mock_print):
    """현재 시스템 설정 조회 함수가 예외 없이 동작하는지 검증"""
    # 단순 조회 기능이므로 에러가 안나는지만 검증
    res = settings.view_system_config()
    assert res is True
    mock_print.assert_called()

@patch('modules.settings.Prompt.ask', side_effect=["1", "88.8", "q"])
@patch('modules.settings._save_dynamic_config')
def test_edit_config_table_value_change(mock_save, mock_ask):
    """_edit_config_table 내부에서 설정값이 변경되는 로직 검증"""
    test_val = {"TARGET": 10.0}
    
    items = [
        {"desc": "테스트", "help": "도움말", "name": "TARGET", "type": "float",
         "get": lambda: test_val["TARGET"], "set": lambda v: test_val.update({"TARGET": v})}
    ]
    
    action_taken = settings._edit_config_table("테스트 타이틀", items, check_preset=False)
    
    assert action_taken is True
    assert test_val["TARGET"] == 88.8
    mock_save.assert_called_once()

@patch('modules.settings.Prompt.ask', side_effect=["q"])
def test_edit_config_table_cancel(mock_ask):
    """_edit_config_table 메뉴 진입 직후 종료 로직 검증"""
    items = [
        {"desc": "테스트", "help": "도움말", "name": "TARGET", "type": "float",
         "get": lambda: 10.0, "set": lambda v: None}
    ]
    action_taken = settings._edit_config_table("테스트", items, check_preset=False)
    assert action_taken is False