"""멈춘 채 살아 있는 매매 루프를 누가 알아채는가.

[두 층의 사각지대] 프로세스 죽음은 두 층이 본다 — 스케줄러가 매매 스레드의 생존을,
프로세스 밖 감시자(tools/hts_watchdog.py)가 하트비트 파일을 본다. 그런데 **루프가 멈춘
채 스레드는 살아 있는 경우**는 둘 다 못 본다:
  · 멈춘 스레드도 `is_alive()` 는 참이다.
  · 예외가 안 나므로 `consecutive_errors` 는 0이다.
  · 하트비트를 찍는 것은 매매 스레드가 아니라 **스케줄러 스레드**다 — 계속 찍힌다.
그동안 손절·트레일링 감시는 통째로 멈춰 있다. 이 시스템에서 가장 값비싼 조용한 고장이다.

지연 자체는 종전에도 계산됐지만 `get_status_message()` 안에만 있어, 운영자가 상태 화면을
열어야 보였다.
"""
from datetime import datetime, timedelta

import pytest


@pytest.fixture
def trader():
    from modules import auto_trade
    t = auto_trade.AutoTrader()
    saved = (t.is_running, t.last_success_at, getattr(t, 'waiting_for_server', False),
             getattr(t, 'cycle_secs_history', None), getattr(t, 'last_cycle_secs', None))
    t.is_running = True
    t.waiting_for_server = False
    t.last_success_at = datetime.now()
    t.cycle_secs_history = [20.0] * 5
    t.last_cycle_secs = 20.0
    yield t
    (t.is_running, t.last_success_at, t.waiting_for_server,
     t.cycle_secs_history, t.last_cycle_secs) = saved


def _age(trader, seconds):
    trader.last_success_at = datetime.now() - timedelta(seconds=seconds)


def test_a_healthy_loop_is_not_stalled(trader):
    assert trader.loop_stall_seconds() < trader.loop_stall_threshold()


def test_a_frozen_loop_is_detected(trader):
    _age(trader, 3600)
    assert trader.loop_stall_seconds() > trader.loop_stall_threshold()


def test_a_stopped_trader_is_not_stalled(trader):
    """정지는 고장이 아니다 — 끄면 경보가 울리는 감시는 아무도 안 읽는다."""
    trader.is_running = False
    _age(trader, 3600)
    assert trader.loop_stall_seconds() is None


def test_waiting_for_the_broker_is_not_stalled(trader):
    """서버 장애 대기는 **의도된 멈춤**이다. 여기서 울리면 장애마다 가짜 경보가 겹친다."""
    trader.waiting_for_server = True
    _age(trader, 7200)
    assert trader.loop_stall_seconds() is None


def test_the_first_cycle_is_not_judged(trader):
    """기동 직후엔 완료 기록이 없다 — 없는 것을 정체로 세면 매 기동마다 울린다."""
    trader.last_success_at = None
    assert trader.loop_stall_seconds() is None


def test_the_threshold_scales_with_the_cycle_length(trader):
    """관심종목이 늘면 한 주기가 길어진다 — 고정값이면 정상인데도 울린다."""
    trader.cycle_secs_history = [20.0] * 5
    trader.last_cycle_secs = 20.0
    short = trader.loop_stall_threshold()
    trader.cycle_secs_history = [600.0] * 5
    trader.last_cycle_secs = 600.0
    assert trader.loop_stall_threshold() > short * 2


def test_the_threshold_has_a_floor(trader):
    """주기가 아주 짧아도 몇 초 지연으로 울리면 안 된다."""
    trader.cycle_secs_history = [0.1]
    trader.last_cycle_secs = 0.1
    assert trader.loop_stall_threshold() >= 300


def test_the_scheduler_raises_the_alarm(trader, monkeypatch):
    """계산만 하고 아무도 안 부르면 종전과 같다."""
    import modules.scheduler as sch

    sent = []
    s = sch.SystemScheduler.__new__(sch.SystemScheduler)
    s.trader = trader
    s.last_heartbeat_time = 0.0
    s._last_problem_msg = ""
    monkeypatch.setattr(sch.api, 'send_telegram_message', lambda m, *a, **k: sent.append(m))
    monkeypatch.setattr(sch.heartbeat, 'beat', lambda *a, **k: None)
    monkeypatch.setattr(s, '_heartbeat_context',
                        lambda: {"running": True, "mode": "2", "instance": "x", "holdings": 0},
                        raising=False)
    trader.thread = None
    trader.consecutive_errors = 0
    _age(trader, 7200)

    s._check_heartbeat()

    assert sent, "루프가 2시간째 멈췄는데 아무 알림도 없다"
    assert "한 주기를 끝내지 못했" in sent[0], sent[0]
    assert "손절" in sent[0], "무엇이 위험한지 말하지 않으면 경보가 아니다"


def test_the_scheduler_stays_quiet_when_healthy(trader, monkeypatch):
    import modules.scheduler as sch

    sent = []
    s = sch.SystemScheduler.__new__(sch.SystemScheduler)
    s.trader = trader
    s.last_heartbeat_time = 0.0
    s._last_problem_msg = ""
    monkeypatch.setattr(sch.api, 'send_telegram_message', lambda m, *a, **k: sent.append(m))
    monkeypatch.setattr(sch.heartbeat, 'beat', lambda *a, **k: None)
    monkeypatch.setattr(s, '_heartbeat_context',
                        lambda: {"running": True, "mode": "2", "instance": "x", "holdings": 0},
                        raising=False)
    trader.thread = None
    trader.consecutive_errors = 0

    s._check_heartbeat()
    assert not sent
