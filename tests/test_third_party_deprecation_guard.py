"""서드파티 DeprecationWarning 출력 가드 — 전역 필터가 다른 스레드에서 깨져도 화면에 찍히지 않는다.

[2026-10-03] 차트 AI 분석 중 google/genai/types.py 의 DeprecationWarning 이 main.py 의 ignore 필터를
뚫고 화면에 찍혔다. pandas.pandas_dtype 이 `catch_warnings()` 안에서 simplefilter("always",
DeprecationWarning) 를 거는데, GIL 빌드의 catch_warnings 는 프로세스 전역이라 두 스레드가 엇갈리면
'always' 가 영구히 남는다. 필터는 지킬 수 없으므로 출력 단계(config.install_third_party_deprecation_guard)에서 거른다.
"""
import os
import threading
import warnings

import pytest

import config

SITE = os.path.join(os.sep, "x", "lib", "python3.14", "site-packages", "google", "genai", "types.py")


@pytest.fixture
def guarded(monkeypatch):
    shown = []
    monkeypatch.setattr(warnings, "showwarning", lambda *a, **k: shown.append(a))
    config.install_third_party_deprecation_guard()
    return shown


def test_third_party_deprecation_is_not_shown(guarded):
    warnings.showwarning("x", DeprecationWarning, SITE, 42)
    warnings.showwarning("x", PendingDeprecationWarning, SITE, 42)
    assert guarded == []


def test_our_own_and_other_categories_still_show(guarded):
    warnings.showwarning("ours", DeprecationWarning, os.path.join("my-stock-hts", "modules", "x.py"), 1)
    warnings.showwarning("lib", UserWarning, SITE, 1)
    assert [a[0] for a in guarded] == ["ours", "lib"]


def test_install_is_idempotent(guarded):
    first = warnings.showwarning
    config.install_third_party_deprecation_guard()
    assert warnings.showwarning is first


def test_interleaved_catch_warnings_leaves_always_filter_behind():
    """가드가 필요한 이유 자체를 고정한다 — pandas 와 같은 모양의 두 스레드가 엇갈리면 'always' 가 남는다."""
    with warnings.catch_warnings():        # 이 테스트가 남기는 필터를 스위트 밖으로 흘리지 않는다
        a_in, b_in, a_out = threading.Event(), threading.Event(), threading.Event()

        def a():
            with warnings.catch_warnings():
                warnings.simplefilter("always", DeprecationWarning)
                a_in.set()
                b_in.wait(5)
            a_out.set()

        def b():
            a_in.wait(5)
            with warnings.catch_warnings():
                warnings.simplefilter("always", DeprecationWarning)
                b_in.set()
                a_out.wait(5)
        ts = [threading.Thread(target=a), threading.Thread(target=b)]
        for t in ts:
            t.start()
        for t in ts:
            t.join(5)
        assert warnings.filters[0][:3] == ("always", None, DeprecationWarning)


def test_main_installs_the_guard_right_after_config():
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    src = open(os.path.join(root, "main.py"), encoding="utf-8").read()
    i_cfg = src.index("\nimport config\n")
    i_guard = src.index("config.install_third_party_deprecation_guard()")
    i_api = src.index("\nimport api\n")
    assert i_cfg < i_guard < i_api
