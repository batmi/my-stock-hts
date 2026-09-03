"""수급 컬럼 ↔ OBV 컬럼 전환은 모드에 상관없이 같은 뜻이어야 한다.

[배경 · 2026-09-04 실측] 컬럼 선택은 '수급 최신 행의 값이 0인가'라는 간접 판정이었고,
그 판정이 모드마다 정반대로 굴렀다.
 · KIS(mode 1·2) — 장이 서면 당일 행이 0으로 생겨 OBV 로 전환된다.
 · 토스(mode 3)  — 수급 API 가 **전일 확정치만** 주고 당일 행이 없다(다음날 06:50 갱신.
   09-04 08:43 조회에 최신 레코드가 09-03). 그래서 판정이 늘 참이라 온종일 수급이
   남았고, 그 값은 전일 것이었다.
같은 화면이 모드에 따라 다른 것을 뜻하면 안 되므로 KIS 동작에 맞춘다.
"""
import pytest

import api
import config
from modules import analysis


@pytest.fixture
def toss(monkeypatch):
    monkeypatch.setattr(config.session, "is_toss", True, raising=False)


@pytest.fixture
def kis(monkeypatch):
    monkeypatch.setattr(config.session, "is_toss", False, raising=False)


@pytest.mark.parametrize("phase,suppressed", [
    ("krx", True),          # 정규장 — 수급은 아직 확정되지 않았다
    ("nxt_pre", False),     # 개장 전 — 전일 확정 수급을 보여준다
    ("nxt_after", False),
    ("closed", False),
    ("holiday", False),
])
def test_toss_suppresses_investor_column_during_regular_session(toss, monkeypatch, phase, suppressed):
    monkeypatch.setattr(api, "domestic_session_phase", lambda: phase)
    assert analysis._toss_investor_suppressed() is suppressed


@pytest.mark.parametrize("phase", ["krx", "nxt_pre", "closed"])
def test_kis_modes_are_untouched(kis, monkeypatch, phase):
    """KIS 는 종전대로 데이터가 정한다 — 이 게이트는 토스 전용이다."""
    monkeypatch.setattr(api, "domestic_session_phase", lambda: phase)
    assert analysis._toss_investor_suppressed() is False


def test_session_probe_failure_keeps_previous_behaviour(toss, monkeypatch):
    """세션 판정이 깨져도 표가 사라지면 안 된다 — 종전 동작(수급 허용)으로 둔다."""
    def _boom():
        raise RuntimeError("세션 판정 불가")

    monkeypatch.setattr(api, "domestic_session_phase", _boom)
    assert analysis._toss_investor_suppressed() is False


def test_gate_is_wired_into_print_table():
    """게이트가 실제로 컬럼 선택 지점에 걸려 있는지 — 함수만 있고 안 쓰면 소용없다."""
    import inspect

    src = inspect.getsource(analysis.print_table)
    head = src[:src.index("_probe_investor_data")]
    assert "_toss_investor_suppressed()" in head, "수급 조회 전에 게이트가 걸려 있어야 한다"
