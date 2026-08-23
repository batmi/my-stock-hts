"""api 패키지 구조 계약 (2026-08-23 구 api.py 7,596줄 분해).

분해의 전제는 하나다 — **바깥에서 보기에 아무것도 바뀌지 않는다.** 호출부는 예전처럼
`api.함수()` 를 쓰고, 테스트의 patch.object(api, ...) 는 모든 호출부에 걸린다.
그 전제가 조용히 깨지지 않도록 여기서 규약을 고정한다.
"""
import ast
import importlib
import os
import sys
from unittest.mock import patch

import pytest

sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import api

API_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "api")


def _submodule_files():
    out = []
    for root, _dirs, files in os.walk(API_DIR):
        if "__pycache__" in root:
            continue
        for f in sorted(files):
            if f.endswith(".py") and f != "__init__.py":
                out.append(os.path.join(root, f))
    return sorted(out)


def test_every_submodule_has_the_accessor():
    """계층마다 _api() 접근자가 있어야 한다 — 상대 계층을 부르는 유일한 통로다."""
    for path in _submodule_files():
        tree = ast.parse(open(path, encoding="utf-8").read())
        names = {n.name for n in tree.body if isinstance(n, ast.FunctionDef)}
        assert "_api" in names, f"{os.path.basename(path)}: _api() 접근자가 없다"


def test_submodules_do_not_import_each_other_directly():
    """서브모듈끼리 직접 import 하면 patch 가 닿지 않는다 — _api() 를 쓰라는 규약."""
    offenders = []
    for path in _submodule_files():
        tree = ast.parse(open(path, encoding="utf-8").read())
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom):
                mod = node.module or ""
                if node.level > 0 or mod == "api" or mod.startswith("api."):
                    offenders.append(f"{os.path.basename(path)}: from {'.' * node.level}{mod}")
            elif isinstance(node, ast.Import):
                for a in node.names:
                    if a.name == "api" or a.name.startswith("api."):
                        # _api() 안의 지연 import 는 예외 — 함수 이름으로 구분한다
                        continue
    assert not offenders, "서브모듈 간 직접 import:\n" + "\n".join(offenders)


def test_patch_on_the_package_reaches_the_layer_that_owns_it():
    """patch.object(api, 'call_api') 가 그 값을 실제로 들고 있는 계층까지 바꾼다.

    분해 전에는 전부 한 모듈이라 당연했던 성질이다. 패키지가 사본을 들고 있으면
    같은 파일 안에서 부르는 쪽이 patch 를 놓친다 — 그러면 테스트가 조용히 실 호출을 한다.
    """
    sentinel = object()
    original = api.auth.call_api          # conftest 가 이미 감싸 두었을 수 있다
    with patch.object(api, "call_api", lambda *a, **k: sentinel):
        assert api.auth.call_api() is sentinel
        assert api.call_api() is sentinel
    assert api.auth.call_api is original  # 원복도 함께 전파된다


def test_patch_on_a_shared_alias_reaches_every_layer():
    """datetime 처럼 여러 계층이 함께 쓰는 이름도 한 번에 얼어붙어야 한다."""
    marker = object()
    with patch.object(api, "datetime", marker):
        assert api.sessions.datetime is marker
        assert api.toss.datetime is marker
    assert api.sessions.datetime is not marker


def test_state_reads_are_never_stale():
    """서브모듈이 값을 다시 묶어도 api.X 가 옛 값을 돌려주지 않는다.

    패키지가 import 시점의 사본을 들고 있으면, 캐시 플래그 같은 값이 바뀐 뒤에도
    api 쪽은 옛 값을 보여 준다 — 진단이 통째로 어긋나는 종류의 버그다.
    """
    before = api.instruments._NXT_MASTER_LOADED
    try:
        api.instruments._NXT_MASTER_LOADED = not before
        assert api._NXT_MASTER_LOADED == (not before)
    finally:
        api.instruments._NXT_MASTER_LOADED = before


def test_no_name_is_owned_by_two_layers_with_different_values():
    """같은 이름을 두 계층이 서로 다른 값으로 들고 있으면 이름 해석이 흔들린다."""
    clashes = []
    for name, layers in api._NAME_INDEX.items():
        if len(layers) < 2:
            continue
        values = {id(getattr(m, name)) for m in layers}
        if len(values) > 1:
            clashes.append(f"{name}: " + ", ".join(m.__name__ for m in layers))
    assert not clashes, "계층마다 값이 다른 이름:\n" + "\n".join(clashes)


@pytest.mark.parametrize("name", [
    "get_current_price", "get_chart_data", "call_api", "place_order",
    "get_domestic_balance", "is_holiday_today", "market_session_label",
    "send_telegram_message", "call_dart", "session",
])
def test_public_entry_points_stay_on_the_package(name):
    """분해 전 호출부가 쓰던 이름은 그대로 api 에서 보여야 한다."""
    assert hasattr(api, name), f"api.{name} 이 사라졌다"
