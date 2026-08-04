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

def test_reset_matches_the_class_defaults(setup_temp_config):
    """[불변식] '전체 초기화'가 되돌리는 값 = GlobalSettings 클래스 기본값.

    reset_all_settings()는 딕셔너리 참조 오염을 막으려고 기본값을 **하드코딩으로 한 번 더**
    적는다(config.py의 [동기화] 주석). 두 벌이라 한쪽만 고치면 조용히 어긋나고, 그때
    '초기화'는 기본값이 아니라 **옛 값**으로 되돌리는 버튼이 된다.

    [실제 사고 2026-08-05] DD_SCALE_1/2가 2026-08-04 실증으로 0.75/0.5 → 0.90/0.80으로
    바뀌었는데 초기화 경로에만 구 값이 남아, 초기화하면 기각된 설정으로 되돌아갔다.
    주석으로만 요구하던 불변식을 여기서 강제한다.
    """
    cls_defaults = config.GlobalSettings()
    for key in ("ANALYSIS_THRESHOLDS", "SELL_STRATEGY", "INDICATOR_PARAMS",
                "SCORING_WEIGHTS", "MARKET_REGIME_PARAMS", "RISK_SCALING_PARAMS"):
        expected = getattr(cls_defaults, key)
        actual = getattr(config.settings, key)
        assert isinstance(expected, dict), f"{key}가 딕셔너리가 아니다"
        diff = {k: (expected.get(k, "<없음>"), actual.get(k, "<없음>"))
                for k in set(expected) | set(actual)
                if expected.get(k, "<없음>") != actual.get(k, "<없음>")}
        assert not diff, (
            f"{key}: 초기화 결과가 클래스 기본값과 다르다 "
            f"(항목별 (클래스, 초기화후)): {diff}")


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