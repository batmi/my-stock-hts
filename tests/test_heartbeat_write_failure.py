"""감시 장치 자신이 고장 나면 그 사실이 보여야 한다.

[사고 시나리오] 라즈베리파이의 SD 카드가 가득 찬다. heartbeat.beat() 의 파일 쓰기가
 실패한다. 종전에는 그것을 debug 로만 남기고 아무것도 돌려주지 않았다. 그러면:
   · 운영 로그(기본 FILE_DEBUG_LEVEL=INFO)에는 한 줄도 안 남는다.
   · 밖의 감시자(cron)는 약속 시각이 지나는 순간 **살아 있는 프로세스를 사망으로**
     판정하고 알린다. 실측: 5회 연속 실패 → 400초 뒤 판정 dead.
   · 운영자는 접속해서 프로세스가 멀쩡한 것을 보고 '감시자가 이상하다'고 결론짓는다.
 거짓 사망 경보는 안전한 방향이지만, 반복되면 정작 진짜 한 건을 흘려보낸다.
 프로세스는 아직 살아 있으므로 텔레그램은 나간다 — 그때 진짜 원인을 말해 준다.
"""
import logging
import os

import pytest

from modules import heartbeat as hb
from modules import scheduler


@pytest.fixture
def hb_path(tmp_path):
    p = str(tmp_path / "logs" / "heartbeat.real.json")
    hb._BEAT_FAILURES.clear()
    yield p
    hb._BEAT_FAILURES.clear()


def _break_write(monkeypatch):
    def boom(path, payload):
        raise OSError(28, "No space left on device")
    monkeypatch.setattr(hb, '_atomic_write', boom)


def test_도장_실패는_성공여부로_돌아온다(hb_path, monkeypatch):
    assert hb.beat(interval_sec=60, mode="실전", path=hb_path) is True
    assert os.path.exists(hb_path)
    _break_write(monkeypatch)
    assert hb.beat(interval_sec=60, mode="실전", path=hb_path) is False


def test_도장_실패는_운영_로그에_남는다(hb_path, monkeypatch, caplog):
    _break_write(monkeypatch)
    with caplog.at_level(logging.DEBUG, logger="modules.heartbeat"):
        hb.beat(interval_sec=60, mode="실전", path=hb_path)
    warns = [r for r in caplog.records if r.levelno >= logging.WARNING]
    assert warns, "debug 로만 남으면 기본 설정에서는 파일에도 안 남는다"
    assert "사망으로 판정" in warns[0].message, "결과를 말하지 않으면 경고가 아니다"


def test_연속_실패_횟수를_센다(hb_path, monkeypatch):
    _break_write(monkeypatch)
    for i in range(1, 4):
        hb.beat(interval_sec=60, mode="실전", path=hb_path)
        assert hb.beat_failure_streak(path=hb_path) == i
    monkeypatch.undo()
    hb.beat(interval_sec=60, mode="실전", path=hb_path)
    assert hb.beat_failure_streak(path=hb_path) == 0, "성공했는데 실패 기록이 남았다"


def test_종료_표식_실패도_드러난다(hb_path, monkeypatch, caplog):
    _break_write(monkeypatch)
    with caplog.at_level(logging.DEBUG, logger="modules.heartbeat"):
        assert hb.stopped(reason="테스트", mode="실전", path=hb_path) is False
    warns = [r for r in caplog.records if r.levelno >= logging.WARNING]
    assert warns and "사망으로 알릴 수 있습니다" in warns[0].message


# ------------------------------------------------- 프로세스 안 하트비트 점검
def _sched(monkeypatch, beat_ok, streak):
    s = object.__new__(scheduler.SystemScheduler)   # 싱글톤을 건드리지 않는다
    s.trader = type("T", (), {"is_running": False, "thread": None, "consecutive_errors": 0,
                              "loop_stall_seconds": staticmethod(lambda: None),
                              "loop_stall_threshold": staticmethod(lambda: 999)})()
    s.last_heartbeat_time = 0.0
    s._last_problem_msg = ""
    sent = []
    monkeypatch.setattr(scheduler.api, 'send_telegram_message', lambda m, *a, **k: sent.append(m))
    monkeypatch.setattr(scheduler.heartbeat, 'beat', lambda **k: beat_ok)
    monkeypatch.setattr(scheduler.heartbeat, 'beat_failure_streak', lambda **k: streak)
    monkeypatch.setattr(s, '_heartbeat_context',
                        lambda: {"running": False, "mode": "실전", "instance": "x", "holdings": 0},
                        raising=False)
    monkeypatch.setattr(s, '_dead_monitor_threads', lambda: [], raising=False)
    return s, sent


def test_연속_실패가_이어지면_프로세스_안에서_알린다(monkeypatch):
    s, sent = _sched(monkeypatch, beat_ok=False,
                     streak=scheduler.SystemScheduler.BEAT_FAIL_ALERT_STREAK)
    s._check_heartbeat()
    assert sent, "감시 장치가 고장 났는데 아무 말도 없다"
    assert "생존 신호" in sent[0] and "거짓" in sent[0], \
        f"곧 올 사망 알림이 거짓임을 말해야 한다: {sent[0]}"


def test_한두_번의_실패로는_울리지_않는다(monkeypatch):
    s, sent = _sched(monkeypatch, beat_ok=False,
                     streak=scheduler.SystemScheduler.BEAT_FAIL_ALERT_STREAK - 1)
    s._check_heartbeat()
    assert not sent, "순간 IO 지연마다 울리면 그 경보는 곧 무시된다"


def test_도장이_정상이면_조용하다(monkeypatch):
    s, sent = _sched(monkeypatch, beat_ok=True, streak=0)
    s._check_heartbeat()
    assert not sent


# ------------------------------------------------- 알림을 삼키는 함정
def test_알리지_않는_판정은_표식을_남기지_않는다(tmp_path, monkeypatch):
    """판정만 해 보는 실행이 사망 건을 '이미 알린 것'으로 굳히면 진짜 감시자가 침묵한다."""
    base = str(tmp_path / "logs" / "heartbeat.json")
    monkeypatch.setattr(hb, 'ALERT_STATE_PATH', str(tmp_path / "logs" / "alert.json"))
    p = hb.path_for("실전", base)
    hb.beat(interval_sec=1, mode="실전", path=p)

    import time
    later = time.time() + 600
    state, sent = hb.check_and_notify(now=later, path=p, notify=False)
    assert state == "dead" and sent is False
    assert hb._load_alert_state() == {}, "알리지도 않고 '알렸다'로 굳혔다"

    calls = []
    monkeypatch.setattr(hb, '_send_telegram', lambda t: (calls.append(t), (True, ""))[1])
    state, sent = hb.check_and_notify(now=later, path=p, notify=True)
    assert state == "dead" and sent is True and calls, "진짜 감시자가 침묵했다"
