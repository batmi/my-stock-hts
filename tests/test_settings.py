import pytest
import json
import os
from unittest.mock import patch
import config

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
    original_max_holdings = config.SYSTEM_MAX_HOLDINGS
    original_buy_score = config.ANALYSIS_THRESHOLDS["BUY_SCORE"]
    
    # config.JSON_DIR을 임시 경로로 모킹하여 테스트
    with patch("config.JSON_DIR", str(temp_config_dir)):
        try:
            # 3. 로드 함수 실행
            config.load_dynamic_config()
            
            # 4. 검증
            assert config.SYSTEM_MAX_HOLDINGS == 99
            assert config.ANALYSIS_THRESHOLDS["BUY_SCORE"] == 9.9
            
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
        
    original_sl = config.SELL_STRATEGY["STOP_LOSS_RATE"]
    original_tp = config.SELL_STRATEGY["TAKE_PROFIT_RATE"]
    
    with patch("config.JSON_DIR", str(temp_config_dir)):
        try:
            config.load_dynamic_config()
            
            # 변경된 값 확인
            assert config.SELL_STRATEGY["STOP_LOSS_RATE"] == -50.0
            # 변경하지 않은 값은 그대로 유지되어야 함
            assert config.SELL_STRATEGY["TAKE_PROFIT_RATE"] == original_tp
            
        finally:
            config.SELL_STRATEGY["STOP_LOSS_RATE"] = original_sl

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