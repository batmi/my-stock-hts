"""하위 계층 패키지의 구조 계약 (2026-08-24 루트 평면 파일 정리).

루트에 흩어져 있던 모듈을 두 패키지로 모았다.

    core/      '무엇을 매매하는가'를 모르는 코드 — 날짜·포맷·캐시·JSON IO·스레드 상태·지표 수식
    brokers/   증권사가 정한 규격을 그대로 말하는 원시 클라이언트 — KIS WebSocket, 토스 REST

둘 다 **아래쪽 계층**이라는 것이 존재 이유다. 그 성질은 저절로 유지되지 않는다 — 누군가
편의로 `import api` 한 줄을 최상단에 얹으면, 이 패키지를 여는 것만으로 상위 계층이 딸려 오고
방향이 뒤집힌다. 그래서 두 가지를 여기서 고정한다.

  1) **import 순서와 무관하게 단독으로 import 된다.** 이전 session.py 는 config 와 서로를
     최상단에서 물어, `import session` 을 먼저 실행하면 ImportError 로 죽었다(config 가 항상
     먼저 로드되는 실행 경로에서만 우연히 살아 있었다). 도구 스크립트 하나가 순서를 바꾸면
     드러나는 지뢰였다.
  2) **모듈 최상단에서 상위 계층(api/modules)을 import 하지 않는다.** 함수 안 지연 import 는
     허용한다 — 호출 시점에는 순환이 성립하지 않고, 무엇보다 import 비용을 지지 않는다.
"""
import ast
import os
import subprocess
import sys

import pytest

sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# 상위 계층 — 아래쪽 패키지가 최상단에서 부르면 안 되는 것들
UPPER_LAYERS = ("api", "modules")


def _modules_of(pkg):
    d = os.path.join(ROOT, pkg)
    return sorted(f[:-3] for f in os.listdir(d)
                  if f.endswith(".py") and f != "__init__.py")


TARGETS = [(pkg, name) for pkg in ("core", "brokers") for name in _modules_of(pkg)]
IDS = [f"{pkg}.{name}" for pkg, name in TARGETS]


@pytest.mark.parametrize("pkg,name", TARGETS, ids=IDS)
def test_module_imports_standalone(pkg, name):
    """<패키지>.<모듈> 을 **가장 먼저** import 해도 살아 있어야 한다 (별도 프로세스로 확인)."""
    res = subprocess.run(
        [sys.executable, "-c", f"from {pkg} import {name}"],
        cwd=ROOT, capture_output=True, text=True, timeout=120,
    )
    assert res.returncode == 0, (
        f"{pkg}.{name} 단독 import 실패 — 상위 계층과 순환한다:\n{res.stderr[-1500:]}"
    )


@pytest.mark.parametrize("pkg,name", TARGETS, ids=IDS)
def test_module_does_not_import_upper_layers_at_top_level(pkg, name):
    """최상단 import 에 api/modules 가 없어야 한다. (함수 안 지연 import 는 허용)"""
    path = os.path.join(ROOT, pkg, f"{name}.py")
    with open(path, encoding="utf-8") as fp:
        tree = ast.parse(fp.read())

    offenders = []
    for node in tree.body:                     # 최상단만 본다 — 함수 내부는 순회하지 않는다
        if isinstance(node, ast.Import):
            offenders += [a.name for a in node.names
                          if a.name.split(".")[0] in UPPER_LAYERS]
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            if node.module.split(".")[0] in UPPER_LAYERS:
                offenders.append(node.module)

    assert not offenders, (
        f"{pkg}/{name}.py 최상단이 상위 계층을 import 한다: {offenders} "
        "— 함수 안으로 내려 지연 import 하라(아래쪽 계층이라는 성질이 깨진다)"
    )


def test_brokers_names_do_not_join_the_api_namespace():
    """brokers 는 api 패키지의 평탄화 대상이 아니다 — 이름이 부딪히기 때문이다.

    api 는 서브모듈 이름을 전부 `api.X` 로 올리고 쓰기를 모든 서브모듈로 전파한다. brokers 를
    그 안에 넣었다면 `get_investor_trend` 가 **KIS 수급과 토스 수급** 둘을 가리켜, patch 가 엉뚱한
    클라이언트를 덮었을 것이다. 실제로 충돌하는 이름이 있다는 사실 자체를 고정해 둔다 —
    누군가 brokers 를 api 계층으로 등록하려 하면 이 테스트가 이유를 알려 준다.
    """
    import api
    from brokers import toss_api

    clashes = {n for n in vars(toss_api)
               if not n.startswith("__") and n in api._NAME_INDEX}
    assert "get_investor_trend" in clashes, (
        "토스와 KIS 의 수급 조회 함수 이름이 더는 겹치지 않는다 — 분리 근거가 바뀌었으니 "
        "brokers/__init__.py 의 설명을 다시 쓰라"
    )
    assert api._NAME_INDEX["get_investor_trend"][0].__name__ == "api.quotes.price", (
        "api.get_investor_trend 가 KIS(api.quotes.price) 가 아닌 다른 계층으로 해석된다"
    )
