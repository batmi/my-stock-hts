"""서버 장애 대기/복구 알림은 **짝이 맞아야** 한다.

[왜] 진입 알림은 쿨타임(10분)과 `_wait_alert_sent` 를 **보내기 전에** 찍었다.
 send_telegram_message 는 비동기라 실패가 예외로 오지 않으므로:
   · 못 닿아도 10분간 침묵한다(그동안 자동매매는 멈춰 있다).
   · 더 나쁘게는 `_wait_alert_sent` 가 True 로 남아, 진입 알림은 못 갔는데
     **복구 알림만** 나간다 — 운영자는 있지도 않았던 장애의 복구를 통보받는다.
 복구 알림도 같다: 못 닿았는데 짝을 풀면 그 통보는 영영 없다.
"""
import time

import pytest

from modules.auto_trade import AutoTrader
from modules.auto_trade import trader as tr


@pytest.fixture
def trader():
    AutoTrader._instance = None
    t = AutoTrader()
    t._wait_alert_sent = False
    t.last_wait_alert_time = 0
    t.last_emergency_alert_time = 0
    yield t
    AutoTrader._instance = None


def _run_recovery(trader, monkeypatch):
    """서버가 곧바로 정상으로 확인되는 한 바퀴만 돌린다."""
    monkeypatch.setattr(tr.api, 'check_server_health', lambda *a, **k: True, raising=False)
    monkeypatch.setattr(tr.time, 'sleep', lambda *_a: None, raising=False)
    monkeypatch.setattr(type(trader), 'log', lambda self, *a, **k: None, raising=False)
    monkeypatch.setattr(tr._pkg(), 'ConclusionMonitor',
                        lambda: type("C", (), {"consecutive_errors": 0})(), raising=False)
    trader.is_running = True
    trader._wait_for_server_recovery()


def _patch_alert(monkeypatch, delivered):
    sent = []
    monkeypatch.setattr(tr._pkg(), 'alert_delivered',
                        lambda m, urgent=False: (sent.append(m), delivered)[1], raising=False)
    return sent


def test_복구_알림을_못_보내면_짝을_풀지_않는다(trader, monkeypatch):
    trader._wait_alert_sent = True
    sent = _patch_alert(monkeypatch, delivered=False)

    _run_recovery(trader, monkeypatch)

    assert sent, "복구 알림 시도조차 없었다"
    assert trader._wait_alert_sent is True, "못 보냈는데 짝을 풀었다 — 통보가 영영 사라진다"


def test_복구_알림이_전달되면_짝을_푼다(trader, monkeypatch):
    trader._wait_alert_sent = True
    _patch_alert(monkeypatch, delivered=True)
    _run_recovery(trader, monkeypatch)
    assert trader._wait_alert_sent is False


def test_진입_알림_경로가_전달_확인을_거친다():
    """소스 검사 — 이 경로는 서버 장애 상태라야 도달해 런타임 재현이 비싸다."""
    import ast
    import inspect

    src = inspect.getsource(AutoTrader._run_loop)
    tree = ast.parse(src.lstrip() if src.startswith(' ') else src)
    #  _wait_alert_sent = True 가 alert_delivered 검사 **안**에 있어야 한다.
    guarded = False
    for node in ast.walk(tree):
        if not isinstance(node, ast.If):
            continue
        calls = [c for c in ast.walk(node.test)
                 if isinstance(c, ast.Call)
                 and ((isinstance(c.func, ast.Attribute) and c.func.attr == 'alert_delivered')
                      or (isinstance(c.func, ast.Name) and c.func.id == 'alert_delivered'))]
        if not calls:
            continue
        for stmt in ast.walk(node):
            if (isinstance(stmt, ast.Assign)
                    and any(isinstance(t, ast.Attribute) and t.attr == '_wait_alert_sent'
                            for t in stmt.targets)):
                guarded = True
    assert guarded, "_wait_alert_sent 가 전달 확인 밖에서 찍힌다"
