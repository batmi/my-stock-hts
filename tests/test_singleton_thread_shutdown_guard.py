"""conftest 가 초기화하는 싱글톤 중 **스레드를 띄우는 것**은 반드시 세워야 한다.

[왜 이 파일이 있나 · 2026-09-07]
같은 결함이 세 번 나왔다. 전부 모양이 같다 — 싱글톤을 격리 목록에 넣으면서
`X._instance = None` 한 줄만 쓰고, 그 인스턴스가 띄워 둔 스레드는 그대로 둔 것이다.

  · ConclusionMonitor  — 제한 해제 확인 스레드가 patch 원복 뒤에 깨어나 다음 테스트의
    mock 을 건드렸다(전체 3회 중 2회 간헐 실패, 매번 다른 테스트).
  · JournalSyncWorker · SystemScheduler — 참조만 끊어 xdist 워커가 통째로 크래시했다
    (3회 중 1회).
  · ReservedOrderMonitor — 2026-09-06 에 목록에 넣으면서 다시 같은 실수를 했다.
    10초마다 _check_orders() 를 부르는 스레드가 살아남아 예약 계열 17건을 한꺼번에
    깨뜨렸다(3회 중 2회. 파일 단독·같은 순서 직렬 실행은 통과 — 배분과 타이밍에 달렸다).

참조를 끊으면 **그 스레드를 세울 방법이 아예 사라진다**(stop 을 부를 인스턴스가 없다).
그래서 순서가 중요하다: 세우고, 기다리고, 그다음에 참조를 끊는다.

사람이 매번 기억할 일이 아니라서 센다. 검사 규칙: conftest 의 초기화 목록에 있는
클래스가 스레드를 띄우면(소스에 threading.Thread( 가 있으면), conftest 는 그 클래스의
`_instance` 를 **읽어서** 잡아 두는 문장을 함께 가져야 한다 — 그게 세 곳이 모두 쓰는
'세우고 나서 끊는' 모양이다.
"""
import ast
import inspect
import os

import pytest

CONFTEST = os.path.join(os.path.dirname(os.path.abspath(__file__)), "conftest.py")


def _conftest_tree():
    with open(CONFTEST, encoding="utf-8") as fh:
        return ast.parse(fh.read())


def _reset_fixture(tree):
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == "reset_all_singletons":
            return node
    raise AssertionError("conftest 에서 reset_all_singletons 를 찾지 못했다 — 검사기가 낡았다")


def _reset_aliases(fn):
    """`X._instance = None` 으로 초기화되는 클래스 별칭들."""
    out = set()
    for node in ast.walk(fn):
        if not isinstance(node, ast.Assign):
            continue
        for t in node.targets:
            if (isinstance(t, ast.Attribute) and t.attr == "_instance"
                    and isinstance(t.value, ast.Name)):
                out.add(t.value.id)
    return out


def _captured_aliases(fn):
    """초기화 그 자체가 아닌 자리에서 한 번이라도 **쓰이는** 클래스 별칭들.

    `_x = X._instance` 로 잡아 두는 모양도, `for cls in (A, B):` 로 도는 모양도 함께
    센다 — 세우는 방법은 하나가 아니어도 되지만, '초기화 한 줄만 있는' 것은 아니어야 한다.
    `X._instance = None` 안의 X 는 문법상 Load 지만 그건 초기화 그 자체이므로 뺀다
    (빼지 않으면 검사가 통째로 무의미해진다 — 실제로 놓쳤던 것이 정확히 그 모양이다).
    """
    reset_values = set()
    for node in ast.walk(fn):
        if isinstance(node, ast.Assign):
            for t in node.targets:
                if (isinstance(t, ast.Attribute) and t.attr == "_instance"
                        and isinstance(t.value, ast.Name)):
                    reset_values.add(id(t.value))

    out = set()
    for node in ast.walk(fn):
        if (isinstance(node, ast.Name) and isinstance(node.ctx, ast.Load)
                and id(node) not in reset_values):
            out.add(node.id)
    return out


def _owns_thread(alias):
    """그 클래스가 자기 스레드를 띄우는가."""
    import tests.conftest as conftest_mod

    cls = getattr(conftest_mod, alias, None)
    if cls is None or not inspect.isclass(cls):
        return False
    try:
        src = inspect.getsource(cls)
    except (OSError, TypeError):
        return False
    return "threading.Thread(" in src


def test_검사기가_실제로_목록을_읽는다():
    """가드 자기 검사 — 목록이 비면 아무것도 세지 않는 것과 같다."""
    fn = _reset_fixture(_conftest_tree())
    aliases = _reset_aliases(fn)
    assert len(aliases) >= 5, f"초기화 목록이 너무 적다 — 파싱이 깨졌다: {aliases}"
    assert any(_owns_thread(a) for a in aliases), "스레드를 띄우는 싱글톤을 하나도 못 찾았다"


def test_스레드를_띄우는_싱글톤은_참조를_끊기_전에_세운다():
    fn = _reset_fixture(_conftest_tree())
    reset = _reset_aliases(fn)
    captured = _captured_aliases(fn)

    missing = sorted(a for a in reset if _owns_thread(a) and a not in captured)

    assert not missing, (
        "conftest 가 이 싱글톤들의 참조만 끊고 스레드는 세우지 않는다: "
        + ", ".join(missing)
        + "\n참조를 끊으면 그 스레드를 세울 방법이 사라진다 — 살아남은 스레드가 "
          "다음 테스트의 mock 을 건드리거나 xdist 워커를 떨어뜨린다.")


@pytest.mark.parametrize("alias", ["_ReservedOrderMonitor", "_JournalSyncWorker",
                                   "_SystemScheduler", "ConclusionMonitor"])
def test_실제로_깨졌던_넷은_반드시_잡혀_있다(alias):
    """가드가 느슨해져도 이 넷만은 놓치지 않는다."""
    fn = _reset_fixture(_conftest_tree())
    assert alias in _captured_aliases(fn), f"{alias} 를 세우는 문장이 사라졌다"


def test_멈추라는_신호는_즉시_닿는다():
    """stop() 이 sleep 이 끝나기를 기다리면 '멈췄다'가 최대 10초 뒤의 일이 된다."""
    from modules.reserved_order_monitor import ReservedOrderMonitor

    m = ReservedOrderMonitor()
    m.start()
    assert m.monitor_thread.is_alive()

    m.stop()
    m.monitor_thread.join(timeout=2)
    assert not m.monitor_thread.is_alive(), \
        "stop() 뒤에도 감시 스레드가 살아 있다 — 다음 주기에 판정을 한 번 더 돌린다"
