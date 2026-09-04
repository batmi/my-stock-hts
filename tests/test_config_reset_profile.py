"""커스텀 설정 되돌리기 — 프로필 모드에서도 실제로 되돌아가는가.

2026-09-04 실사용 로그: 토스 모드에서 '초기화' 를 눌러도 값이 그대로인데 화면은
'성공적으로 초기화되었습니다' 라고 했다. 커스텀 목록은 클래스 기본값과 비교해 뽑으므로
기준 파일에서 물려받은 값도 목록에 오르는데, 되돌리기는 프로필 파일의 키만 지웠고
프로필 파일이 없으면 조용히 돌아갔다.
"""
import json
import os

import pytest

import config
from core import jsonio


@pytest.fixture
def cfg(tmp_path, monkeypatch):
    monkeypatch.setattr(config, 'JSON_DIR', str(tmp_path))
    base = {"INDICATOR_PARAMS": {"BOX_PERIOD": 40}, "ENABLE_TELEGRAM": False}
    (tmp_path / "dynamic_config.json").write_text(json.dumps(base), encoding='utf-8')
    yield tmp_path
    config.set_config_profile(None)


def _default(key_path):
    d = getattr(config.GlobalSettings(), 'model_dump', config.GlobalSettings().dict)()
    parent, _, child = key_path.partition('.')
    return d[parent][child] if child else d[parent]


def test_profile_reset_actually_restores_default(cfg):
    """프로필 파일이 아예 없어도 기준 파일에서 물려받은 값이 되돌아간다."""
    config.set_config_profile('toss')
    assert config.INDICATOR_PARAMS['BOX_PERIOD'] == 40

    done = config.reset_custom_settings(['INDICATOR_PARAMS.BOX_PERIOD'])

    assert done == ['INDICATOR_PARAMS.BOX_PERIOD']
    assert config.INDICATOR_PARAMS['BOX_PERIOD'] == _default('INDICATOR_PARAMS.BOX_PERIOD')
    assert 'INDICATOR_PARAMS.BOX_PERIOD' not in config.get_custom_settings()


def test_profile_reset_does_not_touch_base_file(cfg):
    """관찰·토스에서 되돌린 값이 실전 기준 파일을 바꾸면 안 된다."""
    config.set_config_profile('toss')
    config.reset_custom_settings(['INDICATOR_PARAMS.BOX_PERIOD', 'ENABLE_TELEGRAM'])

    base = json.loads((cfg / "dynamic_config.json").read_text(encoding='utf-8'))
    assert base['INDICATOR_PARAMS']['BOX_PERIOD'] == 40
    assert base['ENABLE_TELEGRAM'] is False


def test_profile_reset_writes_explicit_override(cfg):
    """되돌리기는 프로필 파일에 '이 모드에서는 기본값' 을 명시한다(편집과 대칭)."""
    config.set_config_profile('toss')
    config.reset_custom_settings(['ENABLE_TELEGRAM'])

    overlay = json.loads((cfg / "dynamic_config.toss.json").read_text(encoding='utf-8'))
    assert overlay['ENABLE_TELEGRAM'] == _default('ENABLE_TELEGRAM')


def test_profile_reset_removes_own_override_too(cfg):
    """프로필 자신의 덮어쓰기도 함께 걷어낸다(기준값으로만 되돌아가면 안 된다)."""
    (cfg / "dynamic_config.toss.json").write_text(
        json.dumps({"INDICATOR_PARAMS": {"BOX_PERIOD": 55}}), encoding='utf-8')
    config.set_config_profile('toss')
    assert config.INDICATOR_PARAMS['BOX_PERIOD'] == 55

    config.reset_custom_settings(['INDICATOR_PARAMS.BOX_PERIOD'])
    assert config.INDICATOR_PARAMS['BOX_PERIOD'] == _default('INDICATOR_PARAMS.BOX_PERIOD')


def test_live_reset_deletes_from_base_file(cfg):
    """실전(프로필 없음)은 기준 파일에서 키를 지운다 — 명시 덮어쓰기를 남기지 않는다."""
    config.set_config_profile(None)
    done = config.reset_custom_settings(['INDICATOR_PARAMS.BOX_PERIOD'])

    assert done == ['INDICATOR_PARAMS.BOX_PERIOD']
    base = json.loads((cfg / "dynamic_config.json").read_text(encoding='utf-8'))
    assert 'BOX_PERIOD' not in base.get('INDICATOR_PARAMS', {})
    assert config.INDICATOR_PARAMS['BOX_PERIOD'] == _default('INDICATOR_PARAMS.BOX_PERIOD')


def test_returns_only_what_was_reset(cfg):
    """클래스에 없는 키는 되돌린 목록에 오르지 않는다 — 화면이 거짓말하지 않게."""
    config.set_config_profile('toss')
    done = config.reset_custom_settings(['ENABLE_TELEGRAM', 'NO_SUCH_KEY_XYZ'])
    assert done == ['ENABLE_TELEGRAM']


def test_corrupt_profile_file_is_preserved_not_overwritten(cfg):
    """손상 파일은 jsonio 가 옆으로 치워 원본을 지킨다 — 되돌리기가 그것을 덮지 않는다."""
    (cfg / "dynamic_config.toss.json").write_text("{ not json", encoding='utf-8')
    config.set_config_profile('toss')          # 여기서 격리된다

    config.reset_custom_settings(['ENABLE_TELEGRAM'])

    kept = list(cfg.glob("dynamic_config.toss.json.corrupt.*"))
    assert len(kept) == 1
    assert kept[0].read_text(encoding='utf-8') == "{ not json"


def test_unquarantinable_corrupt_file_aborts(cfg, monkeypatch):
    """격리에 실패해 손상 파일이 그대로 남아 있으면 아무것도 되돌리지 않는다.

    빈 딕셔너리로 이어가 저장하면 그 파일이 사용자의 설정 원본을 덮어 없앤다.
    """
    config.set_config_profile('toss')
    path = cfg / "dynamic_config.toss.json"
    path.write_text("{ not json", encoding='utf-8')
    monkeypatch.setattr(jsonio, '_quarantine', lambda p: None)

    assert config.reset_custom_settings(['ENABLE_TELEGRAM']) == []
    assert path.read_text(encoding='utf-8') == "{ not json"


def test_reset_all_listed_items_clears_the_list(cfg):
    """화면에 뜬 항목을 전부 고르면(전체: 0) 목록이 비어야 한다."""
    config.set_config_profile('toss')
    keys = list(config.get_custom_settings().keys())
    assert keys
    config.reset_custom_settings(keys)
    assert config.get_custom_settings() == {}
