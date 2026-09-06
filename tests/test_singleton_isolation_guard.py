"""프로세스 전역 싱글톤은 **하나도 빠짐없이** 테스트마다 초기화돼야 한다.

[왜 가드가 필요한가] conftest 의 초기화 목록은 손으로 관리한다. 그래서 두 번 뒤처졌다.
  · 2026-09-06 ReservedOrderMonitor — 예약 경로를 새로 태우는 테스트가 늘자
    전체 실행에서만 12건이 한꺼번에 깨졌다(파일 단독은 통과).
  · 2026-09-07 SystemScheduler · JournalSyncWorker — 한 테스트가 '실행 중인데 스레드는
    죽음'을 심어 두고 나가, 다른 파일의 하트비트 테스트 3건이 xdist 배분에 따라
    붙었다 떨어졌다 했다. 그리고 MarketHaltMonitor — VI 발동 집합과 폴링 쿨다운(20초)이
    그대로 넘어갔다.

 공통점은 **파일 단독으로는 늘 통과한다**는 것이다. 순서에 따라 답이 달라지는 스위트는
 계측기가 아니다. 새 싱글톤이 생기면 이 테스트가 그 자리에서 알린다.
"""
import ast
import pathlib
import re

import pytest

ROOT = pathlib.Path(__file__).resolve().parent.parent
CONFTEST = pathlib.Path(__file__).resolve().parent / "conftest.py"
SCAN_DIRS = ("modules", "core", "api")


def _singleton_classes():
    """제품 코드에서 `_instance = None` 을 클래스 몸통에 둔 클래스를 모은다.

    싱글톤 관용구가 그것이다(`__new__` 가 `cls._instance` 를 재사용한다).
    """
    found = []
    for d in SCAN_DIRS:
        for path in sorted((ROOT / d).rglob("*.py")):
            try:
                tree = ast.parse(path.read_text(encoding="utf-8"))
            except SyntaxError:                     # pragma: no cover
                continue
            for node in ast.walk(tree):
                if not isinstance(node, ast.ClassDef):
                    continue
                for stmt in node.body:
                    if (isinstance(stmt, ast.Assign)
                            and any(isinstance(t, ast.Name) and t.id == "_instance"
                                    for t in stmt.targets)
                            and isinstance(stmt.value, ast.Constant)
                            and stmt.value.value is None):
                        found.append((str(path.relative_to(ROOT)), node.name))
                        break
    return found


def test_탐지기_자체가_동작한다():
    """가드가 아무것도 못 찾으면 조용히 통과한다 — 그 상태를 먼저 막는다."""
    found = _singleton_classes()
    assert len(found) >= 5, f"싱글톤 탐지가 망가졌다: {found}"
    names = {n for _p, n in found}
    for expected in ("AutoTrader", "ConclusionMonitor", "MarketHaltMonitor"):
        assert expected in names, f"알려진 싱글톤 {expected} 을(를) 못 찾았다: {sorted(names)}"


def test_모든_싱글톤이_conftest_에서_초기화된다():
    text = CONFTEST.read_text(encoding="utf-8")
    #  `X._instance = None` 형태로 지우는 이름을 모은다. import 별칭(as _X)도 따라간다.
    cleared = set(re.findall(r"(\w+)\._instance\s*=\s*None", text))
    aliases = dict(re.findall(r"import\s+(\w+)\s+as\s+(\w+)", text))
    cleared |= {orig for orig, alias in aliases.items() if alias in cleared}

    missing = sorted({f"{path}::{name}" for path, name in _singleton_classes()
                      if name not in cleared})
    assert not missing, (
        "테스트마다 초기화되지 않는 싱글톤이 있다 — 앞 테스트의 상태가 뒤 테스트로 샌다"
        f"(파일 단독으로는 통과하고 전체 실행에서만 깨진다): {missing}\n"
        "tests/conftest.py 의 reset_all_singletons 에 `X._instance = None` 을 "
        "**앞뒤 두 곳 모두** 더할 것.")


def test_초기화는_테스트_앞뒤_모두에서_이뤄진다():
    """yield 앞에서만 지우면, 마지막 테스트가 남긴 상태가 다음 세션까지 간다."""
    text = CONFTEST.read_text(encoding="utf-8")
    body = text[text.index("def reset_all_singletons"):]
    body = body[:body.index("\ndef ")] if "\ndef " in body else body
    before, _, after = body.partition("\n    yield\n")
    assert after, "reset_all_singletons 에서 yield 를 찾지 못했다"
    b = set(re.findall(r"(\w+)\._instance\s*=\s*None", before))
    a = set(re.findall(r"(\w+)\._instance\s*=\s*None", after))
    assert not (b - a), f"테스트가 끝난 뒤에는 지우지 않는 싱글톤: {sorted(b - a)}"
