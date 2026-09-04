"""메뉴에서 바꾼 설정은 빠짐없이, 그리고 **그 모드의 파일에만** 저장돼야 한다.

[배경 · 2026-09-04 감사]
 1. 저장할 키를 손으로 50개 나열해 두고, 읽는 쪽(config._apply_config_data)은 파일에 있는
    키를 가리지 않고 전부 적용했다. 한쪽만 늘어나면 설정이 조용히 사라진다 —
    SYSTEM_INCLUDE_ETF·USE_WEBSOCKET 은 메뉴에서 바꿀 수 있는데 목록에 없어서, '저장
    되었습니다'를 보고 재시작하면 되돌아가 있었다. 저장은 파일을 통째로 덮으므로
    dynamic_config.json 에 손으로 적어 둔 코드 다이얼(MARKET_HALT_VI_INTERVAL 등 10개)도
    다음 저장 한 번에 지워졌다.
 2. check_and_update_active_preset 이 **모드와 무관하게** json/dynamic_config.json(실전
    기준 파일)을 직접 열어 다시 썼다. 기동 때마다(main.py) 그리고 저장 첫머리에 불리므로,
    토스·관찰 모드로 띄우기만 해도 실전 설정 파일이 다시 쓰였다 — 모드별 프로필로 갈라
    둔 취지가 이 한 줄로 새고 있었다.
"""
import pytest

import config
from modules import settings


@pytest.fixture
def capture(monkeypatch):
    """저장 페이로드만 가로챈다 — 실제 파일은 건드리지 않는다."""
    seen = {}
    monkeypatch.setattr(config, "save_dynamic_config",
                        lambda d: (seen.update(payload=dict(d)), "/tmp/fake.json")[1])
    monkeypatch.setattr(settings.console, "print", lambda *a, **k: None)
    return seen


def _fields():
    return set(getattr(config.settings, 'model_dump', config.settings.dict)().keys())


# ─────────────────────────────────────────────
# 1. 빠지는 설정이 없는가
# ─────────────────────────────────────────────

def test_every_setting_field_is_saved(capture):
    """[핵심] GlobalSettings 필드가 곧 '저장 가능한 설정'의 정의다.

    새 설정을 추가하고 저장 목록에 넣는 것을 잊으면 이 테스트가 잡는다 — 종전에는
    잊어도 아무도 몰랐고, 사용자만 '저장했는데 안 됐다'를 겪었다.
    """
    settings._save_dynamic_config()
    missing = _fields() - set(capture["payload"])
    assert not missing, f"저장되지 않는 설정: {sorted(missing)}"


@pytest.mark.parametrize("key,value", [
    ("SYSTEM_INCLUDE_ETF", True),      # 메뉴에서 바꿀 수 있는데 저장 안 되던 키
    ("USE_WEBSOCKET", False),
    ("MARKET_HALT_VI_INTERVAL", 77),   # 손으로 적어 두면 다음 저장에 지워지던 키
    ("MAX_POSITION_OVERSHOOT", 1.5),
])
def test_previously_dropped_keys_now_survive(capture, monkeypatch, key, value):
    monkeypatch.setattr(config.settings, key, value)
    settings._save_dynamic_config()
    assert capture["payload"][key] == value


def test_in_place_group_edits_are_picked_up(capture, monkeypatch):
    """메뉴는 config.ANALYSIS_THRESHOLDS 를 제자리 수정한다 — 그 값이 담겨야 한다."""
    monkeypatch.setitem(config.ANALYSIS_THRESHOLDS, "BUY_SCORE", 7.7)
    settings._save_dynamic_config()
    assert capture["payload"]["ANALYSIS_THRESHOLDS"]["BUY_SCORE"] == 7.7


def test_no_credentials_are_written(capture):
    """저장 대상을 모델 전체로 넓혔으니, 그 안에 자격증명이 없다는 것을 고정해 둔다.
    (API 키류는 ~/.htsrc 환경변수로만 들어오며 GlobalSettings 에 없다)"""
    import re

    settings._save_dynamic_config()
    bad = [k for k in capture["payload"]
           if re.search(r"KEY|SECRET|TOKEN|PASSWORD|APPKEY|CANO", k)]
    assert not bad, f"설정 파일에 자격증명이 섞인다: {bad}"


# ─────────────────────────────────────────────
# 2. 모드 경계를 넘지 않는가
# ─────────────────────────────────────────────

def test_preset_sync_does_not_write_any_file(monkeypatch):
    """[핵심] 프리셋 동기화는 메모리만 만진다 — 기동만 해도 실전 파일이 다시 쓰이던 자리."""
    from core import jsonio

    monkeypatch.setattr(jsonio, "save_json",
                        lambda *a, **k: pytest.fail("프리셋 동기화가 파일에 썼다"))
    settings.check_and_update_active_preset()


def test_preset_sync_still_updates_memory(monkeypatch):
    monkeypatch.setattr(config.settings, "ACTIVE_PRESET", "bull")
    result = settings.check_and_update_active_preset()
    assert config.settings.ACTIVE_PRESET == result


def test_saving_in_a_profile_mode_never_touches_the_base_file(capture, monkeypatch, tmp_path):
    """토스·관찰 모드의 저장이 실전 기준 파일로 새면 안 된다(모드별 프로필의 전부)."""
    from core import jsonio

    base = config.base_config_path()
    monkeypatch.setattr(config, "active_profile", "toss")

    def _guard(path, data):
        assert str(path) != str(base), f"프로필 모드인데 실전 파일에 썼다: {path}"
        return True

    monkeypatch.setattr(jsonio, "save_json", _guard)
    settings._save_dynamic_config()
    assert capture["payload"], "저장 자체는 일어나야 한다"


def test_active_preset_rides_the_normal_save_path(capture):
    """직접 쓰기를 없앤 대신, 저장 경로가 ACTIVE_PRESET 을 실어 나르는지 확인한다."""
    settings._save_dynamic_config()
    assert "ACTIVE_PRESET" in capture["payload"]


# ─────────────────────────────────────────────
# 3. 초기화 안내가 실제 대상과 맞는가
# ─────────────────────────────────────────────

def test_reset_message_names_the_file_it_actually_deleted(monkeypatch):
    """reset_all_settings 는 현재 프로필 파일만 지우는데 안내는 늘 실전 파일을 찍었다."""
    lines = []
    monkeypatch.setattr(config, "active_profile", "toss")
    monkeypatch.setattr(config, "reset_all_settings", lambda: None)
    monkeypatch.setattr(settings.console, "print", lambda *a, **k: lines.append(str(a[0]) if a else ""))
    monkeypatch.setattr(settings.Prompt, "ask", lambda *a, **k: "y")
    monkeypatch.setattr(config, "ENABLE_TELEGRAM", False, raising=False)

    settings.reset_to_default(interactive=True)
    text = "\n".join(lines)
    assert config.profile_config_path() in text
    assert "실전 설정(dynamic_config.json)은 그대로입니다" in text
