"""설정 기본값의 단일 소스 보장.

종전에는 기본값이 세 곳(GlobalSettings 클래스 · reset_all_settings 의 하드코딩 딕셔너리
· json/dynamic_config.json)에 있었고, 한쪽만 고쳐지는 사고가 실제로 두 번 났다
(2026-08-05 DD_SCALE 구 값 부활, 2026-08-09 동적 ATR 캡 8키 누락).
하드코딩은 제거했고, 여기서 그 제거가 성립하는 전제를 고정한다.
"""
import json
import os
import sys

import pytest

sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import config

GROUPS = ["ANALYSIS_THRESHOLDS", "SELL_STRATEGY", "INDICATOR_PARAMS",
          "SCORING_WEIGHTS", "MARKET_REGIME_PARAMS", "RISK_SCALING_PARAMS"]


def test_mutable_defaults_are_isolated_per_instance():
    """하드코딩을 지울 수 있었던 근거 — 새 인스턴스는 이전 인스턴스의 제자리 수정을 물려받지 않는다.

    이 성질이 깨지면(pydantic 동작 변경 등) reset_all_settings 가 오염된 값을
    '기본값'이라며 되살리게 되므로, 여기서 먼저 실패해야 한다.
    """
    a = config.GlobalSettings()
    a.ANALYSIS_THRESHOLDS["BUY_SCORE"] = 99.0
    a.SELL_STRATEGY["TIME_STOP_DAYS"] = 999

    fresh = config.GlobalSettings()
    assert fresh.ANALYSIS_THRESHOLDS["BUY_SCORE"] != 99.0
    assert fresh.SELL_STRATEGY["TIME_STOP_DAYS"] != 999


def test_reset_all_settings_restores_class_defaults(tmp_path, monkeypatch):
    """'전체 초기화'는 클래스 기본값과 정확히 같아야 한다(어떤 키도 빠지지 않는다)."""
    monkeypatch.setattr(config, "JSON_DIR", str(tmp_path))
    config.set_config_profile(None)
    try:
        config.settings.ANALYSIS_THRESHOLDS["BUY_SCORE"] = 1.0
        config.settings.SYSTEM_MAX_HOLDINGS = 99

        config.reset_all_settings()

        defaults = config.GlobalSettings()
        for group in GROUPS:
            assert getattr(config.settings, group) == getattr(defaults, group), group
        assert config.settings.SYSTEM_MAX_HOLDINGS == defaults.SYSTEM_MAX_HOLDINGS
    finally:
        config.set_config_profile(None)


def test_no_hardcoded_default_dicts_in_reset():
    """초기화 경로가 기본값을 다시 타이핑하지 않는지 — 세 번째 사본이 되살아나는 것을 막는다."""
    src = open(config.__file__, encoding="utf-8").read()
    body = src[src.index("def reset_all_settings"):]
    body = body[:body.index("\n# ")]
    for group in GROUPS:
        assert f"settings.{group} = {{" not in body, (
            f"{group} 기본값이 reset_all_settings 에 다시 하드코딩됐다 — "
            f"GlobalSettings 클래스 기본값만 진실이어야 한다")


def test_saved_config_has_no_unknown_keys():
    """운영 설정 파일이 클래스에 없는 키를 들고 있지 않은지(리네임·삭제 후 잔재 탐지)."""
    path = os.path.join(os.path.dirname(config.__file__), "json", "dynamic_config.json")
    if not os.path.exists(path):
        pytest.skip("운영 설정 파일 없음")
    data = json.load(open(path, encoding="utf-8"))
    defaults = getattr(config.GlobalSettings(), "model_dump", config.GlobalSettings().dict)()

    unknown_top = [k for k in data if k not in defaults]
    assert not unknown_top, f"클래스에 없는 최상위 키: {unknown_top}"

    for group in GROUPS:
        if group not in data:
            continue
        unknown = [k for k in data[group] if k not in defaults[group]]
        assert not unknown, f"{group} 에 클래스에 없는 키: {unknown}"
