"""테스트가 DB 연결을 **인스턴스에** 패치하지 못하게 막는다.

[왜 · 2026-09-06] `monkeypatch.setattr(db, "_get_conn", ...)` 처럼 인스턴스에 걸면,
되돌릴 때 원래의 **바인드 메서드가 인스턴스 속성으로 남는다**(원래는 클래스에만 있었다).
그 뒤로는 다른 테스트가 `monkeypatch.setattr(type(db), "_get_conn", ...)` 로 클래스를
패치해도 인스턴스 속성이 그것을 가려, **DB 장애를 흉내 내는 테스트가 장애 없이 통과한다.**

실제로 그렇게 됐다: test_db_failure_visibility 의 5개 케이스와
test_restart_decision_identity 의 '조회 실패는 -1' 계약이 전체 실행에서만 조용히 깨졌고,
파일 단독으로는 통과했다. 안전장치를 시험하는 테스트가 아무것도 시험하지 않는 상태가
가장 나쁜 실패다 — 초록불이 거짓말을 한다.

그래서 규칙은 하나다: **_get_conn 은 클래스에 패치한다.**
"""
import ast
import pathlib

import pytest


TESTS_DIR = pathlib.Path(__file__).parent
TARGETS = {"_get_conn", "_all_conns"}


def _is_type_call(node):
    """type(x) 또는 클래스 자체를 가리키는 표현인가."""
    if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == "type":
        return True
    name = node.id if isinstance(node, ast.Name) else (
        node.attr if isinstance(node, ast.Attribute) else "")
    return name.endswith("Manager")


def _is_shared_db(node):
    """이 표현이 **모듈 전역으로 공유되는** DB 객체를 가리키는가.

    잔재가 문제가 되는 것은 여러 테스트가 함께 쓰는 객체일 때뿐이다. 테스트가 그 안에서
    새로 만든 DBManager(`DBManager()` · `DBManager.__new__(...)`)는 그 테스트와 함께
    사라지므로 잔재가 남아도 아무도 보지 못한다.
    """
    src = ast.unparse(node)
    if src.endswith(".db") or ".db." in src:          # db_manager.db · dbm.db
        return True
    return src in ("_db()", "_real_db()")             # 이 스위트의 관용 헬퍼


def _offenders():
    """monkeypatch.setattr 로 **공유 DB 인스턴스**의 연결을 갈아끼우는 자리.

    patch.object 는 원래 인스턴스에 없던 속성이면 되돌릴 때 **지우므로** 안전하다
    (실측 확인). 문제는 monkeypatch.setattr 뿐이다 — 그쪽은 되돌릴 때 setattr 로
    되돌려 놓아 인스턴스 속성이 남는다.
    """
    out = []
    for path in sorted(TESTS_DIR.glob("test_*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"), str(path))
        for node in ast.walk(tree):
            if not (isinstance(node, ast.Call)
                    and isinstance(node.func, ast.Attribute)
                    and node.func.attr == "setattr"
                    and len(node.args) >= 2):
                continue
            target, attr = node.args[0], node.args[1]
            if not (isinstance(attr, ast.Constant) and attr.value in TARGETS):
                continue
            if _is_type_call(target) or not _is_shared_db(target):
                continue
            out.append(f"{path.name}:{node.lineno}  {ast.unparse(node)[:90]}")
    return out


def test_db_connection_is_patched_on_the_class_not_the_instance():
    offenders = _offenders()
    assert not offenders, (
        "DB 연결을 인스턴스에 패치하는 자리가 있다. 되돌려도 인스턴스 속성이 남아\n"
        "뒤이은 테스트의 **클래스 패치를 가린다** — 장애를 흉내 내는 테스트가\n"
        "장애 없이 통과해 초록불이 거짓말을 한다.\n"
        "  고치는 법: monkeypatch.setattr(type(db), '_get_conn', lambda self, *a, **k: ...)\n\n"
        + "\n".join(f"  · {o}" for o in offenders))


def test_the_detector_actually_detects(tmp_path):
    """가드가 고장 나면 늘 초록이다 — 탐지기 자체를 시험한다."""
    bad = tmp_path / "test_bad.py"
    bad.write_text("def t(monkeypatch):\n"
                   "    monkeypatch.setattr(db_manager.db, '_get_conn', lambda: None)\n",
                   encoding="utf-8")
    tree = ast.parse(bad.read_text(encoding="utf-8"))
    hits = [n for n in ast.walk(tree)
            if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
            and n.func.attr == "setattr" and len(n.args) >= 2
            and isinstance(n.args[1], ast.Constant) and n.args[1].value == "_get_conn"
            and not _is_type_call(n.args[0]) and _is_shared_db(n.args[0])]
    assert hits, "탐지기가 명백한 위반을 놓친다"


def test_the_detector_allows_the_correct_form():
    src = "monkeypatch.setattr(type(db), '_get_conn', lambda self: None)"
    node = ast.parse(src).body[0].value
    assert _is_type_call(node.args[0]), "올바른 형태를 위반으로 잡는다"


def test_the_detector_leaves_local_managers_alone():
    """테스트가 자기 안에서 만든 매니저는 잔재가 남아도 아무도 보지 못한다."""
    node = ast.parse("monkeypatch.setattr(mgr, '_get_conn', f)").body[0].value
    assert not _is_shared_db(node.args[0])


def test_patch_object_really_removes_the_attribute():
    """탐지기가 patch.object 를 봐주는 근거를 실제로 확인한다."""
    from unittest.mock import patch as _patch

    class _C:
        def m(self):
            return "real"

    o = _C()
    with _patch.object(o, "m", return_value="fake"):
        pass
    assert "m" not in o.__dict__, "patch.object 가 인스턴스 속성을 남긴다 — 탐지기를 넓혀야 한다"


def test_monkeypatch_really_leaves_the_attribute(monkeypatch):
    """반대쪽 근거 — monkeypatch 는 남긴다. 이 성질이 바뀌면 이 가드는 불필요해진다."""
    class _C:
        def m(self):
            return "real"

    o = _C()
    monkeypatch.setattr(o, "m", lambda: "fake")
    monkeypatch.undo()
    assert "m" in o.__dict__, "monkeypatch 가 더 이상 잔재를 남기지 않는다 — 가드를 재검토하라"
