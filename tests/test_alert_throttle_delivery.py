"""경보 스로틀은 **전달을 확인한 뒤에** 찍는다 — 남은 두 자리.

[왜] send_telegram_message 는 기본이 비동기라 실패해도 예외를 던지지 않는다. 보내기
 **전에** 스로틀을 찍으면 네트워크가 끊겨 있어도 '보냈다'로 굳고, 그 문제가 지속되는
 내내 다시 보내지 않는다(alert_delivered 를 만든 이유, 2026-09-04).

 이번에 남아 있던 두 곳:
   · 하트비트 이상 경보 — 스로틀이 msg 자체다. 실측: 4주기 동안 전송 시도 1회.
     내용은 '매매 스레드가 죽었다 / 루프가 멈췄다 / 감시 스레드가 종료됐다 /
     생존 신호를 못 쓴다' 다. 놓치면 그 상태를 영영 모른다.
   · DB 쓰기 실패 경보 — 쿨다운을 먼저 찍었다. 내용이 '디스크가 차서 트레일링 최고가가
     저장되지 않는다' 라, 놓치면 재기동 후 청산선이 어긋난다.
"""
import time

import pytest

from modules import scheduler
from modules.auto_trade import AutoTrader


# ─────────────────────────────── 하트비트 이상 경보
def _sched(monkeypatch, delivered):
    sent = []
    s = object.__new__(scheduler.SystemScheduler)
    s.trader = type("T", (), {"is_running": True, "thread": None, "consecutive_errors": 99,
                              "loop_stall_seconds": staticmethod(lambda: None),
                              "loop_stall_threshold": staticmethod(lambda: 999)})()
    s.last_heartbeat_time = 0.0
    s._last_problem_msg = ""
    monkeypatch.setattr(scheduler, 'alert_delivered',
                        lambda m, urgent=False: (sent.append(m), delivered)[1])
    monkeypatch.setattr(scheduler.heartbeat, 'beat', lambda **k: True)
    monkeypatch.setattr(scheduler.heartbeat, 'beat_failure_streak', lambda **k: 0)
    monkeypatch.setattr(s, '_heartbeat_context',
                        lambda: {"running": True, "mode": "실전", "instance": "x", "holdings": 0},
                        raising=False)
    monkeypatch.setattr(s, '_dead_monitor_threads', lambda: [], raising=False)
    return s, sent


def _turns(s, n=4):
    for _ in range(n):
        s.last_heartbeat_time = 0.0     # 매 주기 점검 창을 연다
        s._check_heartbeat()


def test_하트비트_경보를_못_보내면_다음_주기에_다시_보낸다(monkeypatch):
    s, sent = _sched(monkeypatch, delivered=False)
    _turns(s, 4)
    assert len(sent) == 4, f"못 닿았는데 침묵했다(시도 {len(sent)}회)"
    assert s._last_problem_msg == "", "전달 못 했는데 스로틀이 찍혔다"


def test_전달되면_같은_문제로_다시_울리지_않는다(monkeypatch):
    s, sent = _sched(monkeypatch, delivered=True)
    _turns(s, 4)
    assert len(sent) == 1, "같은 문제로 매 주기 울리면 그 경보는 곧 무시된다"
    assert s._last_problem_msg


def test_문제가_풀리면_스로틀이_비워진다(monkeypatch):
    s, sent = _sched(monkeypatch, delivered=True)
    _turns(s, 1)
    s.trader.consecutive_errors = 0
    _turns(s, 1)
    assert s._last_problem_msg == ""


# ─────────────────────────────── DB 쓰기 실패 경보
@pytest.fixture
def trader():
    AutoTrader._instance = None
    t = AutoTrader()
    t._db_write_fail_seen = 0
    t._db_write_fail_alerted_at = 0.0
    yield t
    AutoTrader._instance = None


def _db_alert(monkeypatch, trader, delivered, count=3):
    from modules.auto_trade import trader as tr

    sent = []
    monkeypatch.setattr(tr.db_manager.db, 'get_write_failures',
                        lambda: {'count': count, 'last_op': 'update_trade',
                                 'last_error': 'disk I/O error'}, raising=False)
    monkeypatch.setattr(tr.db_manager.db, 'disk_free_mb', lambda: 3.0, raising=False)
    monkeypatch.setattr(tr._pkg(), 'alert_delivered',
                        lambda m, urgent=False: (sent.append(m), delivered)[1], raising=False)
    monkeypatch.setattr(type(trader), 'log', lambda self, *a, **k: None, raising=False)
    trader._check_db_write_failures()
    return sent


def test_DB_쓰기_실패_경보를_못_보내면_쿨다운을_찍지_않는다(trader, monkeypatch):
    sent = _db_alert(monkeypatch, trader, delivered=False)
    assert sent, "경보 시도조차 없었다"
    assert trader._db_write_fail_alerted_at == 0.0, "못 닿았는데 쿨다운이 찍혔다"


def test_전달되면_쿨다운을_찍는다(trader, monkeypatch):
    _db_alert(monkeypatch, trader, delivered=True)
    assert trader._db_write_fail_alerted_at > 0
