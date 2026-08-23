"""메뉴 0 '변수명 (Config Name)' 칸의 표기 통일.

종전에는 같은 설정이 화면마다 다른 이름으로 보였다(조회는 첨자 표기, 편집은 키만,
커스텀 조회는 점 표기). 게다가 조회 화면에는 한 칸에 키를 여러 개 묶어 파이썬으로는
성립하지 않는 표기가 있었다. 여기서 고정하는 것은 셋이다.

 1) 화면에 나오는 이름은 전부 점 표기(GROUP.KEY 또는 최상위 KEY)다.
 2) 그 이름이 실제 설정으로 해석된다 — json/dynamic_config.json 에서 그대로 찾을 수 있다.
 3) 키 이름은 그룹 간에 겹치지 않는다(겹치면 키만으로 그룹을 정할 수 없어 1)이 무너진다).
"""
import ast
import os
import re
import sys

sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import config
from modules import settings as settings_mod

SETTINGS_SRC = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                            "modules", "settings.py")


def _defaults():
    dump = getattr(config.GlobalSettings(), 'model_dump', None)
    return dump() if dump else config.GlobalSettings().dict()


def _resolves(name):
    """점 표기 이름이 실제 설정을 가리키는가."""
    data = _defaults()
    if '.' in name:
        group, key = name.split('.', 1)
        return isinstance(data.get(group), dict) and key in data[group]
    return name in data


def test_group_keys_do_not_collide():
    """키만으로 그룹이 정해진다는 전제 — 깨지면 표기 정규화가 엉뚱한 그룹을 붙인다."""
    owner = {}
    data = _defaults()
    for group in settings_mod._CONFIG_GROUPS:
        for key in data.get(group) or {}:
            assert key not in owner, f"'{key}' 가 {owner.get(key)} 와 {group} 양쪽에 있다"
            owner[key] = group
        # 최상위 스칼라와도 겹치면 안 된다
    for key in owner:
        assert not isinstance(data.get(key), (int, float, str, bool)) or key not in data, \
            f"'{key}' 가 그룹 키이면서 최상위 설정에도 있다"


def test_normalizer_accepts_all_three_legacy_forms():
    q = settings_mod.qualified_var_name
    assert q("ANALYSIS_THRESHOLDS['BUY_SCORE']") == "ANALYSIS_THRESHOLDS.BUY_SCORE"
    assert q("BUY_SCORE") == "ANALYSIS_THRESHOLDS.BUY_SCORE"
    assert q("ANALYSIS_THRESHOLDS.BUY_SCORE") == "ANALYSIS_THRESHOLDS.BUY_SCORE"
    assert q("SYSTEM_MAX_CONSECUTIVE_ERRORS") == "SYSTEM_MAX_CONSECUTIVE_ERRORS"


def test_multi_key_row_becomes_one_valid_name_per_line():
    """한 칸에 여러 키를 묶던 가짜 표기는 줄을 나눠 각각 실제 이름이 되어야 한다."""
    out = settings_mod.qualified_var_name("INDICATOR_PARAMS['RSI_PERIOD', 'RSI_SIGNAL']")
    lines = out.split("\n")
    assert lines == ["INDICATOR_PARAMS.RSI_PERIOD", "INDICATOR_PARAMS.RSI_SIGNAL"]
    assert all(_resolves(x) for x in lines)


def test_non_setting_text_is_untouched():
    """잠금 표시 같은 안내 문구를 설정 이름으로 오해해 건드리지 않는다."""
    text = "[dim](추세추종 검증값 — 조정 잠금)[/dim]"
    assert settings_mod.qualified_var_name(text) == text


def _row_var_names():
    tree = ast.parse(open(SETTINGS_SRC, encoding="utf-8").read())
    out = []
    for n in ast.walk(tree):
        if isinstance(n, ast.Call) and isinstance(n.func, ast.Name) and n.func.id == 'row':
            if len(n.args) >= 3 and isinstance(n.args[2], ast.Constant) \
                    and isinstance(n.args[2].value, str):
                out.append((n.lineno, n.args[2].value))
    return out


def _edit_item_names():
    tree = ast.parse(open(SETTINGS_SRC, encoding="utf-8").read())
    out = []
    for n in ast.walk(tree):
        if isinstance(n, ast.Dict):
            keys = [k.value for k in n.keys if isinstance(k, ast.Constant)]
            if 'name' in keys and 'desc' in keys:
                for k, v in zip(n.keys, n.values):
                    if isinstance(k, ast.Constant) and k.value == 'name' \
                            and isinstance(v, ast.Constant):
                        out.append((n.lineno, v.value))
    return out


def test_every_displayed_name_resolves_to_a_real_setting():
    """조회·편집 화면에 뜨는 이름은 모두 실제 설정을 가리킨다(오타·리네임 잔재 탐지)."""
    bad = []
    for lineno, raw in _row_var_names() + _edit_item_names():
        for name in settings_mod.qualified_var_name(raw).split("\n"):
            if not re.fullmatch(r"[A-Z_]+(\.[A-Z0-9_]+)?", name):
                continue                      # 설정 이름이 아닌 안내 문구
            if not _resolves(name):
                bad.append(f"L{lineno}: {raw!r} → {name}")
    assert not bad, "실제 설정으로 해석되지 않는 변수명:\n" + "\n".join(bad)


def test_no_screen_shows_subscript_notation():
    """정규화를 거치면 어떤 화면에도 첨자 표기가 남지 않는다."""
    for lineno, raw in _row_var_names() + _edit_item_names():
        shown = settings_mod.qualified_var_name(raw)
        assert '[' not in shown or not shown.split('[')[0].isupper(), \
            f"L{lineno}: 첨자 표기가 화면에 남는다 — {shown}"
