"""config 의 그룹 딕셔너리에 **대입**하면 설정이 조용히 사라진다.

[구조] `config.ANALYSIS_THRESHOLDS` 는 config 모듈에 있는 변수가 아니다. 모듈 레벨
`__getattr__`(PEP 562)이 `config.settings.ANALYSIS_THRESHOLDS` 를 그대로 넘겨준다 —
그래서 메뉴가 제자리 수정(`config.ANALYSIS_THRESHOLDS['BUY_SCORE'] = 8`)한 값이
저장(`_save_dynamic_config` → `config.settings.model_dump()`)에 그대로 담긴다.

여기에 `config.ANALYSIS_THRESHOLDS = {...}` 로 대입하면 config 모듈 __dict__ 에 그
이름이 실제로 생겨 `__getattr__` 이 더는 불리지 않는다. 그 순간부터 모듈이 보는 값과
저장이 보는 값이 갈라지고, **둘 다 조용하다**.
"""
import pytest

import config


@pytest.fixture
def restore():
    saved = config.ANALYSIS_THRESHOLDS.copy()
    yield
    config.__dict__.pop("ANALYSIS_THRESHOLDS", None)
    config.ANALYSIS_THRESHOLDS.clear()
    config.ANALYSIS_THRESHOLDS.update(saved)


def test_the_module_name_and_the_settings_field_are_one_object():
    assert config.ANALYSIS_THRESHOLDS is config.settings.ANALYSIS_THRESHOLDS


def test_assigning_no_longer_splits_them_but_stays_banned_in_source(restore):
    """[2026-09-20] config.<그룹> 대입은 이제 settings 로 라우팅되어 **갈라지지 않는다**
    (config._ConfigModule). 그래도 소스 가드(대입 금지)는 유지한다 — 대입은 '임시 값을 저장
    대상에 쓰는' 행위라 되돌림 규약(제자리 복구)과 다르기 때문이다. 이 테스트는 옛 결함(대입이
    settings 와 갈라져 임시 값이 저장되던 것)이 재발하지 않음을 지킨다."""
    saved = config.ANALYSIS_THRESHOLDS.copy()
    config.ANALYSIS_THRESHOLDS["BUY_SCORE"] = 3.0      # 스크리닝이 임시로 낮춘 값
    config.ANALYSIS_THRESHOLDS = saved                 # 대입 — 이제 settings 자체가 saved 를 가리킨다

    assert config.ANALYSIS_THRESHOLDS is config.settings.ANALYSIS_THRESHOLDS
    assert config.settings.ANALYSIS_THRESHOLDS["BUY_SCORE"] == saved["BUY_SCORE"] != 3.0
    config.ANALYSIS_THRESHOLDS["BUY_SCORE"] = 8.0
    assert config.settings.model_dump()["ANALYSIS_THRESHOLDS"]["BUY_SCORE"] == 8.0
    assert "ANALYSIS_THRESHOLDS" not in vars(config)


def test_restoring_in_place_keeps_them_together(restore):
    """올바른 복구 — analysis.py 전체 종목 분석이 쓰는 방식."""
    saved = config.ANALYSIS_THRESHOLDS.copy()
    config.ANALYSIS_THRESHOLDS["BUY_SCORE"] = 3.0
    config.ANALYSIS_THRESHOLDS.clear()
    config.ANALYSIS_THRESHOLDS.update(saved)

    assert config.ANALYSIS_THRESHOLDS is config.settings.ANALYSIS_THRESHOLDS
    config.ANALYSIS_THRESHOLDS["BUY_SCORE"] = 8.0
    assert config.settings.model_dump()["ANALYSIS_THRESHOLDS"]["BUY_SCORE"] == 8.0


def test_no_source_file_assigns_to_a_config_group_dict():
    """새로 생기면 여기서 걸린다 — 증상이 '가끔 설정이 안 저장된다'라 추적이 어렵다."""
    import os
    import re

    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    groups = "|".join(config._CONFIG_GROUP_KEYS)
    pat = re.compile(rf"config\.({groups})\s*=[^=]")
    hits = []
    for base in ("modules", "core", "api", "tools"):
        for dirpath, _, files in os.walk(os.path.join(root, base)):
            if "__pycache__" in dirpath:
                continue
            for fn in files:
                if not fn.endswith(".py"):
                    continue
                p = os.path.join(dirpath, fn)
                for n, line in enumerate(open(p, encoding='utf-8').read().splitlines(), 1):
                    if line.strip().startswith("#"):
                        continue
                    if pat.search(line):
                        hits.append((os.path.relpath(p, root), n, line.strip()))
    assert not hits, f"그룹 딕셔너리에 대입한다 — .clear()/.update() 로 바꿔라: {hits}"
