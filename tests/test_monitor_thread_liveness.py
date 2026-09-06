"""감시 스레드가 죽었을 때 그 사실이 드러나는가.

[왜 이 파일이 있는가]
 프로세스 사망은 밖의 cron 감시자가 본다(tools/hts_watchdog.py). 그러나 **프로세스는
 멀쩡한데 스레드만 죽는** 경우는 그 감시자에게 보이지 않는다. 종전에는 프로세스 안의
 점검도 self.trader.thread 하나만 봤고, 감시기 자신은 is_running 이 True 라 스스로
 '실행 중'이라 답했다 — 죽은 채로 영원히 살아 있는 상태가 어느 층에도 보이지 않았다.

 특히 체결 감시가 멈추면 매수 체결이 원장에 오르지 않고, 원장에 없는 종목은 손절·
 트레일링 감시 대상이 되지도 못한다. 조용히 꺼져도 되는 스레드가 아니다.
"""
import threading
import time

import pytest

from modules import auto_trade
import modules.auto_trade.conclusion as C
import modules.scheduler as sch


@pytest.fixture
def monitor(monkeypatch):
    """초기 지연 없이 즉시 도는 체결 감시기."""
    cm = auto_trade.ConclusionMonitor()
    cm.is_running = False
    cm.thread = None
    cm.loop_died_at = None
    cm.initialized = True
    cm.consecutive_errors = 0
    cm.active_until = 0
    cm.idle_interval = 1
    cm.active_interval = 1
    monkeypatch.setattr(C.config, 'CONCLUSION_CHECK_INTERVAL', 0, raising=False)
    monkeypatch.setattr(cm, '_check_conclusions', lambda initial=False: (False, False))
    yield cm
    cm.is_running = False
    try:
        cm.event.set()
    except Exception:
        pass
    if cm.thread is not None and cm.thread.ident is not None:
        cm.thread.join(timeout=2)
    cm.thread = None
    cm.loop_died_at = None


class _DeadEvent:
    """대기 자체가 터지는 이벤트 — 어떤 핸들러로도 못 막는 죽음을 만든다."""
    def wait(self, *a): raise RuntimeError("대기가 통째로 터졌다")
    def set(self): pass
    def clear(self): pass
    def is_set(self): return False


def _wait_until(pred, timeout=3.0, poke=None):
    """poke 를 주면 매 폴링마다 부른다 — 오류 후 백오프(event.wait(5))를 깨우는 용도."""
    end = time.time() + timeout
    while time.time() < end:
        if pred():
            return True
        if poke is not None:
            poke()
        time.sleep(0.02)
    return pred()


# ─────────────────────────── ① 루프의 보호 범위 ───────────────────────────

def test_휴장_판정이_던져도_체결_감시_스레드는_살아남는다(monitor, monkeypatch):
    """종전에는 안쪽 try 가 _check_conclusions 하나만 감쌌다.

    휴장 판정(_is_market_open)은 그 밖에 있어 맨몸이었다. 여기서 한 번 던지면 그것으로
    스레드가 끝났고, 그 뒤로 체결 확정은 영원히 멈춘다.
    """
    calls = []
    def boom():
        calls.append(1)
        raise RuntimeError("휴장일 조회가 던졌다")
    monkeypatch.setattr(C, 'is_system_market_open', boom)

    monitor.start()
    assert _wait_until(lambda: len(calls) >= 2, poke=monitor.event.set), \
        "한 번 던지고 루프가 끝났다"
    assert monitor.thread.is_alive(), "한 주기의 예외가 감시 스레드를 죽였다"


def test_한_주기가_통째로_실패하면_연속_에러로_센다(monitor, monkeypatch):
    """조용히 넘기면 '멈춘 채 살아 있는' 것과 같다 — Kill Switch 에 실려야 한다."""
    monkeypatch.setattr(C, 'is_system_market_open',
                        lambda: (_ for _ in ()).throw(RuntimeError("터짐")))
    monitor.start()
    assert _wait_until(lambda: monitor.consecutive_errors >= 2, poke=monitor.event.set), \
        "루프는 돌지만 실패를 세지 않으면 아무도 모른다"


# ─────────────────────────── ② 죽었을 때의 정직함 ───────────────────────────

@pytest.mark.filterwarnings("ignore::pytest.PytestUnhandledThreadExceptionWarning")
def test_죽은_체결_감시는_건강하다고_답하지_않는다(monitor, monkeypatch):
    """루프가 예외로 끝나면 **에러를 셀 주체가 사라져** 카운터가 0에 얼어붙는다.

    종전 is_healthy() 는 그 0을 보고 '건강하다'고 답했고, 자동매매의 Kill Switch 는
    그 답을 믿고 신규 주문을 계속 냈다 — 체결 확인이 안 되는 상태의 신규 주문이야말로
    그 스위치가 막으려던 것이다.
    """
    monkeypatch.setattr(C, 'is_system_market_open', lambda: True)
    monkeypatch.setattr(monitor, 'event', _DeadEvent())

    monitor.start()
    assert _wait_until(lambda: not monitor.thread.is_alive()), "이 테스트는 죽음을 전제한다"

    max_err = getattr(C.config, 'SYSTEM_MAX_CONSECUTIVE_ERRORS', 5)
    assert monitor.consecutive_errors < max_err, \
        "전제: 루프가 끝나면 셀 주체가 사라져 카운터가 한계에 닿지 못한다"
    assert monitor.is_running is True, \
        "'돌아야 하는데 죽었다'가 밖에서 보이는 유일한 근거다 — 내리면 안 된다"
    assert monitor.loop_died_at, "사망 사실을 남기지 않았다"
    assert monitor.is_healthy() is False, "죽은 감시기가 건강하다고 답한다"


def test_정상_종료는_사망으로_읽지_않는다(monitor, monkeypatch):
    """stop() 으로 내려온 것까지 사고로 읽으면 종료할 때마다 가짜 경보가 난다."""
    monkeypatch.setattr(C, 'is_system_market_open', lambda: False)
    monitor.start()
    assert _wait_until(lambda: monitor.thread.is_alive())
    monitor.is_running = False
    monitor.event.set()
    monitor.thread.join(timeout=3)
    assert not monitor.thread.is_alive()
    assert not monitor.loop_died_at, "정상 종료에 사망 표식이 찍혔다"


def test_스레드가_교체됐으면_옛_스레드의_퇴장은_정상이다(monitor):
    """_note_loop_exit 이 '내가 현역인가'를 보지 않으면 재기동이 자기 표식을 남긴다."""
    other = threading.Thread(target=lambda: None)
    monitor.is_running = True
    monitor.thread = other          # 현역은 다른 스레드다
    monitor._note_loop_exit(threading.current_thread())
    assert not monitor.loop_died_at


# ─────────────────────────── ③ 되살릴 수 있는가 ───────────────────────────

@pytest.mark.parametrize("factory, attr", [
    (lambda: auto_trade.ConclusionMonitor(), "thread"),
    (lambda: __import__("modules.reserved_order_monitor", fromlist=["x"]).ReservedOrderMonitor(),
     "monitor_thread"),
    (lambda: __import__("modules.journal_sync", fromlist=["x"]).JournalSyncWorker(), "thread"),
])
def test_죽은_스레드를_실행중으로_읽어_재기동을_거절하지_않는다(factory, attr):
    """세 감시기 모두 `if self.is_running: return` 하나로 막고 있었다.

    스레드가 죽어도 그 값은 True 로 남으므로, 되살리려는 start() 가 "이미 돌고 있다"며
    되돌아간다. 죽은 채로 영원히 '실행 중'인 상태다.
    """
    obj = factory()
    dead = threading.Thread(target=lambda: None)
    dead.start(); dead.join()
    obj.is_running = True
    setattr(obj, attr, dead)

    import inspect
    src = inspect.getsource(type(obj).start)
    assert "is_alive()" in src, \
        f"{type(obj).__name__}.start() 가 스레드에게 묻지 않는다 — 죽어도 되살릴 수 없다"


# ─────────────────────────── ④ 밖에서 보이는가 ───────────────────────────

def _bare_scheduler(monkeypatch, trader_thread=None):
    sent = []
    s = sch.SystemScheduler.__new__(sch.SystemScheduler)
    s.trader = type("T", (), {"is_running": False, "thread": trader_thread,
                              "consecutive_errors": 0,
                              "loop_stall_seconds": staticmethod(lambda: None),
                              "loop_stall_threshold": staticmethod(lambda: 999)})()
    s.last_heartbeat_time = 0.0
    s._last_problem_msg = ""
    monkeypatch.setattr(sch.api, 'send_telegram_message', lambda m, *a, **k: sent.append(m))
    monkeypatch.setattr(sch.heartbeat, 'beat', lambda *a, **k: None)
    monkeypatch.setattr(s, '_heartbeat_context',
                        lambda: {"running": False, "mode": "2", "instance": "x", "holdings": 0},
                        raising=False)
    return s, sent


def test_하트비트가_감시_스레드_사망을_알린다(monkeypatch):
    """종전 점검은 매매 스레드 하나만 봤다 — 나머지는 죽어도 소리가 나지 않았다."""
    s, sent = _bare_scheduler(monkeypatch)
    dead = threading.Thread(target=lambda: None)
    dead.start(); dead.join()
    fake = type("M", (), {"is_running": True, "thread": dead})()
    monkeypatch.setattr(s, '_monitor_threads',
                        lambda: [("체결 감시", "체결 확정이 멈춰 매수가 원장에 오르지 않습니다",
                                  fake, "thread")], raising=False)

    s._check_heartbeat()

    assert sent, "감시 스레드가 죽었는데 아무 알림도 없다"
    assert "체결 감시" in sent[0], sent[0]
    assert "원장" in sent[0], "무엇을 잃는지 말하지 않으면 경보가 아니다"


@pytest.mark.parametrize("state", [
    {"is_running": False, "alive": False},   # 아직 안 띄웠거나 stop() 으로 내려감
    {"is_running": True, "alive": True},     # 정상 가동
    {"is_running": True, "alive": None},     # 스레드 객체가 아직 없다
])
def test_정상_상태를_사망으로_읽지_않는다(monkeypatch, state):
    s, sent = _bare_scheduler(monkeypatch)
    if state["alive"] is None:
        th = None
    elif state["alive"]:
        stop = threading.Event()
        th = threading.Thread(target=stop.wait, daemon=True); th.start()
    else:
        th = threading.Thread(target=lambda: None); th.start(); th.join()
    fake = type("M", (), {"is_running": state["is_running"], "thread": th})()
    monkeypatch.setattr(s, '_monitor_threads',
                        lambda: [("체결 감시", "무언가", fake, "thread")], raising=False)
    try:
        s._check_heartbeat()
        assert not sent, f"가짜 경보: {sent}"
    finally:
        if state["alive"]:
            stop.set(); th.join(timeout=2)


def test_점검_목록이_실재하는_속성을_가리킨다():
    """[낡음 자체 점검] 이 목록이 조용히 무력화되는 길은 **이름이 바뀌는 것**이다.

    getattr 기본값이 False/None 이라, 속성 이름이 어긋나면 점검은 에러 없이 '항상 정상'
    이 된다 — 가드가 고장 나면 늘 초록이다. 실제 객체에 그 이름이 있는지 못 박는다.
    """
    s = sch.SystemScheduler.__new__(sch.SystemScheduler)
    specs = s._monitor_threads()
    assert len(specs) >= 3, f"점검 대상이 사라졌다: {[n for n, *_ in specs]}"
    for name, what_is_lost, obj, attr in specs:
        assert hasattr(obj, 'is_running'), f"{name}: is_running 이 없다"
        assert hasattr(obj, attr), f"{name}: '{attr}' 속성이 없다 — 점검이 항상 정상이라 답한다"
        assert what_is_lost.strip(), f"{name}: 무엇을 잃는지가 비어 있다"
