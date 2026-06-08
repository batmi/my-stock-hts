import pytest
import os
import sys
from unittest.mock import patch

import config
from modules import settings
import main

@pytest.fixture
def setup_temp_config(tmp_path):
    """테스트 실행 전/후 환경을 고립시키기 위한 픽스처"""
    orig_json_dir = config.JSON_DIR
    config.JSON_DIR = str(tmp_path)
    config.reset_all_settings()
    yield
    config.JSON_DIR = orig_json_dir
    config.reset_all_settings()

def test_custom_settings_detection(setup_temp_config):
    """커스텀 설정 변경 감지 기능 검증"""
    # 1. 초기 상태에는 커스텀 설정이 없어야 함
    assert len(config.get_custom_settings()) == 0
    
    # 2. 단일 및 딕셔너리 내부 값 변경
    config.settings.ENABLE_TELEGRAM = False
    config.settings.SYSTEM_INVEST_PER_STOCK = 0.5
    config.ANALYSIS_THRESHOLDS["BUY_SCORE"] = 8.0
    
    # 3. 커스텀 설정 감지 결과 확인
    customs = config.get_custom_settings()
    assert "ENABLE_TELEGRAM" in customs
    assert customs["ENABLE_TELEGRAM"]["current"] is False
    assert customs["ENABLE_TELEGRAM"]["default"] is True
    
    assert "SYSTEM_INVEST_PER_STOCK" in customs
    assert customs["SYSTEM_INVEST_PER_STOCK"]["current"] == 0.5
    
    assert "ANALYSIS_THRESHOLDS.BUY_SCORE" in customs
    assert customs["ANALYSIS_THRESHOLDS.BUY_SCORE"]["current"] == 8.0

def test_reset_custom_settings(setup_temp_config):
    """특정 커스텀 설정(부분 초기화) 복원 기능 검증"""
    # 설정 변경 후 동적 설정 파일(JSON) 저장 시뮬레이션
    config.settings.ENABLE_TELEGRAM = False
    config.settings.SYSTEM_INVEST_PER_STOCK = 0.5
    settings._save_dynamic_config()
    
    # ENABLE_TELEGRAM 설정만 선택하여 초기화
    config.reset_custom_settings(["ENABLE_TELEGRAM"])
    
    # ENABLE_TELEGRAM은 기본값(True)으로, INVEST_PER_STOCK은 유지(0.5)되어야 함
    assert config.settings.ENABLE_TELEGRAM is True
    assert config.settings.SYSTEM_INVEST_PER_STOCK == 0.5

def test_reset_all_settings_clears_ghost_vars(setup_temp_config):
    """전체 초기화 시 모듈 레벨의 껍데기 변수 소거 및 딕셔너리 참조 복원 검증"""
    current_module = sys.modules['config']
    current_module.__dict__['ENABLE_TELEGRAM'] = False
    current_module.__dict__['TELEGRAM_INSTANCE_NAME'] = "MBA"
    
    config.reset_all_settings()
    
    # 모듈 레벨의 잘못 할당된 속성(ghost attribute)이 제거되었는지 확인
    assert "ENABLE_TELEGRAM" not in current_module.__dict__
    # 중앙 객체(config.settings)의 진짜 기본값이 살아있는지 확인
    assert config.settings.ENABLE_TELEGRAM is True
    assert config.settings.TELEGRAM_INSTANCE_NAME == "HTS"

def test_dynamic_preset_and_emoji_evaluation(setup_temp_config):
    """동적 전략 프리셋 일치 여부 평가 및 이모티콘 상태 매핑 검증"""
    # 기본 설정 상태일 때
    assert settings.check_and_update_active_preset() == "default"
    
    # 커스텀 변경이 발생했을 때
    config.ANALYSIS_THRESHOLDS["BUY_SCORE"] = 9.9
    assert settings.check_and_update_active_preset() == "custom"
    assert main._get_preset_emoji() == "⚪"