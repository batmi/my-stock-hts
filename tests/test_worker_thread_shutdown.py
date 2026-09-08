"""작업 스레드는 **멈추라는 말이 닿아야** 멈춘다.

[왜 이 파일이 있나 · 2026-09-07]
2026-09-06/07 에 세 번 고친 격리 결함(참조만 끊고 스레드는 안 세운다)을 가드로 막고
나서, 그 가드가 닿지 않는 자리를 실제로 재 봤다. 전체 실행 한 번에 **655개 테스트가
작업 스레드를 남긴 채** 끝나고 있었다. 네 갈래였고, 셋은 '세운다고 믿었지만 안 세워지는'
자리였다.

 · SystemScheduler — stop() 이 is_running 만 내렸고 루프는 time.sleep(10) 안에서 잤다.
   '멈춰라'와 '실제로 멈춤' 사이가 최대 10초다. 게다가 start() 가 is_running 만 봐서
   **죽은 스레드도 '실행 중'** 이었다 — 되살리려는 start() 가 그대로 되돌아간다
   (실측). 이 스레드가 죽으면 휴장 알림·장전 브리핑·공시·캘린더·시장정지 감시·
   하트비트가 한꺼번에 멈춘다. 같은 수정이 ConclusionMonitor·ReservedOrderMonitor 에는
   2026-09-06 에 들어갔고 여기만 남아 있었다.

 · 예열 워커(CacheWarmer·OverviewWarmer) — 멈출 방법이 **아예 없었다**. 모듈 수준
   스레드라 싱글톤 목록에도 안 걸린다. main() 을 태우는 테스트 하나가 띄우면 세션
   끝까지 15초마다 외부 조회를 낸다(실측 358건).

 · 제한 해제 확인(RestrictionCheck) — 종료 이벤트를 기다리게 해 뒀는데, start() 가
   그 이벤트를 clear() 한다. stop() 이 신호를 켠 뒤 그 스레드가 **깨어나기 전에**
   누군가 start() 하면 신호가 지워지고, 멈추라는 말을 들은 적이 없는 것처럼 계속
   돌며 잔고를 조회한다(실측 193건 · 예약 계열 17건이 그 잔고 mock 때문에 깨졌다).
   지울 수 있는 신호로는 이 경합을 못 막는다 — 되돌릴 수 없는 세대 번호로 가른다.

이 파일을 쓰다가 같은 실수를 한 번 더 했다: 예열 워커를 stop 직후 clear 하는 reset 을
함께 불렀더니, 자고 있던 워커가 깨어나기 전에 신호가 지워져 **살아남은 워머가 358개에서
745개로 오히려 늘었다.** 신호는 켠 채로 두고 다음에 띄우는 쪽이 내린다.
"""
import threading
import time

import pytest

import config


# --------------------------------------------------------------------------
# SystemScheduler
# --------------------------------------------------------------------------
def _bare_scheduler():
    from modules.scheduler import SystemScheduler

    s = object.__new__(SystemScheduler)
    s.is_running = False
    s.thread = None
    s._wake = threading.Event()
    return s


@pytest.mark.filterwarnings("ignore::pytest.PytestUnhandledThreadExceptionWarning")
def test_죽은_스케줄러_스레드는_다시_띄울_수_있다(monkeypatch):
    monkeypatch.setattr(config, "ENABLE_TELEGRAM", True, raising=False)
    s = _bare_scheduler()

    def _die():
        raise RuntimeError("루프가 예외로 죽었다")

    s._run_loop = _die
    s.start()
    s.thread.join(timeout=2)
    assert not s.thread.is_alive()
    assert s.is_running is True, "죽었는데도 스스로 '실행 중'이라 말한다(그게 전제다)"

    revived = threading.Event()
    s._run_loop = lambda: revived.wait(5)
    s.start()

    assert s.thread.is_alive(), "start() 가 is_running 만 보고 되돌아갔다"
    revived.set()


def test_돌고_있는_스케줄러를_두_번_띄우지_않는다(monkeypatch):
    monkeypatch.setattr(config, "ENABLE_TELEGRAM", True, raising=False)
    s = _bare_scheduler()
    hold = threading.Event()
    s._run_loop = lambda: hold.wait(5)

    s.start()
    first = s.thread
    s.start()

    assert s.thread is first, "이미 도는 스레드가 있는데 하나 더 띄웠다"
    hold.set()


def test_멈추라는_신호가_주기_대기를_뚫는다(monkeypatch):
    """10초 주기를 기다리게 두면 종료도 테스트도 그만큼 늘어진다."""
    monkeypatch.setattr(config, "ENABLE_TELEGRAM", True, raising=False)
    s = _bare_scheduler()
    rounds = []

    def _loop():
        while s.is_running:
            rounds.append(1)
            if s._wake.wait(10):
                break

    s._run_loop = _loop
    s.start()
    time.sleep(0.05)

    started = time.time()
    s.is_running = False
    s._wake.set()
    s.thread.join(timeout=2)

    assert not s.thread.is_alive()
    assert time.time() - started < 1.0, "주기 대기가 끝나기를 기다렸다"


# --------------------------------------------------------------------------
# 예열 워커
# --------------------------------------------------------------------------
def test_예열_종료_신호는_스레드가_볼_때까지_남는다():
    """stop 직후 clear 하면 자고 있던 워커는 신호를 못 본다 — 실제로 그렇게 늘었다."""
    from api import chart_cache

    chart_cache._WARM_STOP.clear()
    seen = []

    def worker():
        while True:
            seen.append(1)
            if chart_cache._WARM_STOP.wait(5):
                return

    t = threading.Thread(target=worker, daemon=True, name="CacheWarmer")
    t.start()
    chart_cache._register_warm_thread(t)

    chart_cache.stop_background_warmers(timeout=2)

    assert not t.is_alive(), "종료 신호를 주고도 워커가 살아 있다"
    assert chart_cache._WARM_STOP.is_set(), \
        "신호를 지웠다 — 아직 자고 있는 워커는 멈추라는 말을 영영 못 듣는다"


def test_다시_띄우는_쪽이_옛_신호를_내린다():
    """신호를 켠 채로 두는 대가 — 새 워머가 즉시 죽으면 안 된다."""
    from api import chart_cache

    chart_cache.stop_background_warmers(timeout=0.1)
    assert chart_cache._WARM_STOP.is_set()

    chart_cache._WARM_STOP.clear()          # start_* 가 하는 일
    assert not chart_cache._WARM_STOP.is_set()


# --------------------------------------------------------------------------
# 보조 스레드 세대
# --------------------------------------------------------------------------
def test_세대가_바뀌면_지울_수_없는_퇴역_명령이_된다():
    """shutdown 이벤트는 start() 가 지운다 — 세대 번호는 되돌릴 수 없다."""
    from modules.auto_trade import conclusion

    epoch = conclusion.current_aux_epoch()
    assert conclusion._bump_aux_epoch() == epoch + 1
    assert conclusion.current_aux_epoch() != epoch


def _restriction_monitor(monkeypatch, calls):
    """제한 해제 보조 스레드의 **실제 몸통**을 부를 수 있는 감시기."""
    from modules.auto_trade import conclusion

    m = object.__new__(conclusion.ConclusionMonitor)
    m.shutdown = threading.Event()
    monkeypatch.setattr(conclusion, "_pkg", lambda: type("P", (), {
        "current_holding_qty": staticmethod(
            lambda *a, **k: (calls.append(1), None)[1])})())   # None = 조회 실패 → 계속 재시도
    return conclusion, m


def test_퇴역한_보조_스레드는_잔고를_다시_묻지_않는다(monkeypatch):
    """이 스레드가 다음 테스트의 잔고 mock 을 건드려 예약 계열 17건이 깨졌다.

    shutdown 신호를 켠 **뒤에 지우는** 것이 실제로 벌어지는 일이다(다음 테스트의
    start() 가 clear 한다). 세대 번호가 없으면 그 스레드는 다섯 바퀴를 끝까지 돈다.
    """
    calls = []
    conclusion, m = _restriction_monitor(monkeypatch, calls)
    epoch = conclusion.current_aux_epoch()

    #  실제 사고에서는 이 스레드가 종료 신호를 **한 번도 보지 못한다** — stop() 이 켠
    #   신호를 다음 테스트의 start() 가 깨어나기 전에 지우기 때문이다. 그 상황을 그대로
    #   만든다: 신호는 영영 서지 않고, 세대만 바뀐다.
    monkeypatch.setattr(m.shutdown, "wait", lambda _t=None: False)
    conclusion._bump_aux_epoch()

    m._release_restriction_when_flat("005930", "12345678", "01", False, epoch)

    assert calls == [], \
        f"잔고를 {len(calls)}번 물었다 — 퇴역한 세대인데 계속 돌았다"


def test_퇴역_명령이_없으면_정상적으로_다섯_번_확인한다(monkeypatch):
    """정직해지느라 정상 동작을 막으면 안 된다 — 재시도는 잔고 반영 지연 대비다."""
    calls = []
    conclusion, m = _restriction_monitor(monkeypatch, calls)
    monkeypatch.setattr(conclusion.ConclusionMonitor, "shutdown", m.shutdown, raising=False)
    m.shutdown = threading.Event()

    orig_wait = m.shutdown.wait
    monkeypatch.setattr(m.shutdown, "wait", lambda _t=None: False)

    m._release_restriction_when_flat("005930", "12345678", "01", False,
                                     conclusion.current_aux_epoch())

    assert len(calls) == 5, f"재시도가 {len(calls)}번에서 끊겼다"
