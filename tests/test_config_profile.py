"""모드별 설정 프로필 — 관찰·모의·토스에서 바꾼 값이 실전으로 새지 않는가.

[고정하려는 것]
 1) 실전(mode 2)은 dynamic_config.json 만 읽고 쓴다.
 2) 그 외 모드는 기준 파일 위에 자기 프로필 파일을 얹어 읽고, **차이만** 그 파일에 쓴다.
 3) 관찰모드에서 안전장치를 꺼도 실전 기준 파일은 그대로다(이 구조의 존재 이유).
 4) 실전 기준 설정을 바꾸면 프로필 모드도 따라온다(프로필이 기준에서 굳지 않는다).
"""
import json
import os
import sys

import pytest

sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import config


@pytest.fixture
def json_dir(tmp_path, monkeypatch):
    """설정 파일 경로를 임시 폴더로 돌리고, 끝나면 실전 프로필로 되돌린다."""
    monkeypatch.setattr(config, "JSON_DIR", str(tmp_path))
    yield tmp_path
    config.set_config_profile(None)


def _write(path, data):
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f)


def test_mode_to_profile_mapping():
    assert config.profile_for_mode('2') is None      # 실전 = 기준 파일
    assert config.profile_for_mode('1') == 'sim'
    assert config.profile_for_mode('3') == 'toss'
    assert config.profile_for_mode('4') == 'paper'
    assert config.profile_for_mode('알수없음') is None  # 모르면 실전으로 본다(보수적)


def test_live_reads_base_only(json_dir):
    _write(json_dir / "dynamic_config.json", {"USE_MARKET_FILTER": True})
    _write(json_dir / "dynamic_config.paper.json", {"USE_MARKET_FILTER": False})

    config.set_config_profile(None)
    assert config.settings.USE_MARKET_FILTER is True
    assert config.profile_config_path().endswith("dynamic_config.json")


def test_profile_overlays_base(json_dir):
    _write(json_dir / "dynamic_config.json",
           {"USE_MARKET_FILTER": True, "SYSTEM_MAX_HOLDINGS": 4})
    _write(json_dir / "dynamic_config.paper.json", {"USE_MARKET_FILTER": False})

    config.set_config_profile('paper')
    assert config.settings.USE_MARKET_FILTER is False   # 프로필이 덮어쓴다
    assert config.settings.SYSTEM_MAX_HOLDINGS == 4     # 나머지는 기준을 따른다


def test_base_change_reaches_profile(json_dir):
    """프로필은 차이만 담는다 — 실전 기준이 바뀌면 프로필 모드도 그 값을 따라간다."""
    _write(json_dir / "dynamic_config.json", {"SYSTEM_MAX_HOLDINGS": 4})
    _write(json_dir / "dynamic_config.paper.json", {"USE_MARKET_FILTER": False})
    config.set_config_profile('paper')
    assert config.settings.SYSTEM_MAX_HOLDINGS == 4

    _write(json_dir / "dynamic_config.json", {"SYSTEM_MAX_HOLDINGS": 3})
    config.load_dynamic_config()
    assert config.settings.SYSTEM_MAX_HOLDINGS == 3
    assert config.settings.USE_MARKET_FILTER is False   # 프로필 차이는 유지


def test_profile_save_writes_diff_only_and_leaves_base(json_dir):
    """관찰모드에서 안전장치를 꺼도 실전 기준 파일은 손대지 않는다."""
    base = {"USE_MARKET_FILTER": True, "SYSTEM_MAX_HOLDINGS": 4,
            "ANALYSIS_THRESHOLDS": {"BUY_SCORE": 7.0}}
    _write(json_dir / "dynamic_config.json", base)
    config.set_config_profile('paper')

    current = {"USE_MARKET_FILTER": False, "SYSTEM_MAX_HOLDINGS": 4,
               "ANALYSIS_THRESHOLDS": {"BUY_SCORE": 5.0, "RISE_SCORE": 6.0}}
    written = config.save_dynamic_config(current)

    assert written.endswith("dynamic_config.paper.json")
    saved = json.load(open(written, encoding="utf-8"))
    assert saved == {"USE_MARKET_FILTER": False,
                     "ANALYSIS_THRESHOLDS": {"BUY_SCORE": 5.0}}   # 같은 값은 안 적는다
    # 실전 기준 파일은 글자 하나 바뀌지 않았다
    assert json.load(open(json_dir / "dynamic_config.json", encoding="utf-8")) == base


def test_live_save_writes_full_base(json_dir):
    config.set_config_profile(None)
    data = {"USE_MARKET_FILTER": False, "SYSTEM_MAX_HOLDINGS": 4}
    written = config.save_dynamic_config(data)
    assert written.endswith("dynamic_config.json")
    assert json.load(open(written, encoding="utf-8")) == data


def test_reset_all_in_profile_keeps_base(json_dir):
    """프로필에서 '전체 초기화'는 그 프로필만 지운다 — 실전 설정은 남는다."""
    _write(json_dir / "dynamic_config.json", {"SYSTEM_MAX_HOLDINGS": 3})
    _write(json_dir / "dynamic_config.paper.json", {"SYSTEM_MAX_HOLDINGS": 9})
    config.set_config_profile('paper')
    assert config.settings.SYSTEM_MAX_HOLDINGS == 9

    config.reset_all_settings()
    assert not os.path.exists(json_dir / "dynamic_config.paper.json")
    assert os.path.exists(json_dir / "dynamic_config.json")
    assert config.settings.SYSTEM_MAX_HOLDINGS == 3     # 기준 설정으로 복귀
