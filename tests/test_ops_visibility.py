"""운영이 **묻지 않아도** 알아야 하는 둘 — 감시자의 부재와 메모리 고갈.

[왜 · 2026-09-08 감사]
  ① 사망 감시자(`tools/hts_watchdog.py`, cron)는 평상시에 아무것도 쓰지 않았다.
     cron 이 멈추거나 crontab 이 지워지거나 `. $HOME/.htsrc` 가 실패해 **감시자가 한 번도
     돌지 않는 상태**가, 파일상 '모든 것이 정상'과 완전히 같았다. 프로세스 사망 감지
     체계 전체가 조용히 부재할 수 있었다([[process-death-watchdog]]).
  ② 가용 메모리 문턱(120/60MB)은 `get_health_message()` 안에만 있어 **사람이 /health 를
     쳐야** 보였다. OOM 은 분석이 몰리는 순간에 나고 그때 화면을 보는 사람은 없다
     (헤드리스). 결국 메모리는 늘 사후에만 알려졌다 — 죽고 나서 감시자가 통보하는
     순서다([[deployment-raspberry-pi]]).
"""
import time

import pytest

import config
from modules import heartbeat


# ── ① 감시자 자신의 생존 ────────────────────────────────────────────
@pytest.fixture
def alert_state(tmp_path, monkeypatch):
    monkeypatch.setattr(heartbeat, "ALERT_STATE_PATH", str(tmp_path / "alert.json"))
    return tmp_path


def test_watchdog_absence_is_distinguishable_from_healthy(alert_state):
    """한 번도 돈 적 없음 = 'unknown'. '정상'으로 읽히면 안 된다."""
    state, detail = heartbeat.watchdog_status()
    assert state == "unknown"
    assert "cron" in detail


def test_watchdog_stamps_every_run(alert_state):
    heartbeat.note_watchdog_check()
    state, _ = heartbeat.watchdog_status()
    assert state == "ok"
    assert heartbeat.watchdog_last_check() is not None


def test_watchdog_gone_stale_is_reported(alert_state):
    heartbeat.note_watchdog_check(now=time.time() - 3600)
    state, detail = heartbeat.watchdog_status(stale_after=1800)
    assert state == "stale"
    assert "분째" in detail


def test_watchdog_stamp_survives_alert_bookkeeping(alert_state):
    """사망 알림 기록이 감시자 도장을 지우지 않는다(같은 파일을 쓴다)."""
    heartbeat.note_watchdog_check()
    alert = heartbeat._load_alert_state()
    alert = heartbeat._put_alert(alert, "logs/heartbeat.json",
                                 {"notified_ts": 1.0, "notified_at": 2.0, "delivered": True})
    heartbeat._save_alert_state(alert)
    assert heartbeat.watchdog_last_check() is not None, "알림 기록이 감시자 도장을 날렸다"


# ── ② 메모리 경보가 능동적으로 나간다 ──────────────────────────────
class _Trader:
    """_warn_if_memory_low 만 떼어 세운다(AutoTrader 는 싱글턴·부작용이 크다)."""

    def __init__(self, avail):
        from modules.auto_trade.trader import AutoTrader
        self._avail = avail
        self.sent = []
        self.logged = []
        self._warn_if_memory_low = AutoTrader._warn_if_memory_low.__get__(self)
        self._health_memory = lambda: (500.0, self._avail, 700.0)

    def log(self, msg):
        self.logged.append(msg)


@pytest.fixture
def capture_alerts(monkeypatch):
    sent = []

    class _Pkg:
        @staticmethod
        def alert_delivered(msg, urgent=False):
            sent.append((msg, urgent))
            return True

    monkeypatch.setattr("modules.auto_trade.trader._pkg", lambda: _Pkg)
    return sent


def test_healthy_memory_sends_nothing(capture_alerts):
    t = _Trader(avail=800.0)
    t._warn_if_memory_low()
    assert not capture_alerts


def test_low_memory_alerts_before_the_process_dies(capture_alerts):
    t = _Trader(avail=float(config.SYSTEM_MEMORY_WARN_MB) - 1)
    t._warn_if_memory_low()
    assert len(capture_alerts) == 1, "문턱을 넘었는데 아무도 안 알렸다"
    msg, urgent = capture_alerts[0]
    assert "메모리 경고" in msg and urgent is False


def test_critical_memory_is_urgent(capture_alerts):
    t = _Trader(avail=float(config.SYSTEM_MEMORY_RISK_MB) - 1)
    t._warn_if_memory_low()
    msg, urgent = capture_alerts[0]
    assert "메모리 위험" in msg and urgent is True


def test_same_level_is_not_repeated_every_cycle(capture_alerts):
    t = _Trader(avail=float(config.SYSTEM_MEMORY_WARN_MB) - 1)
    for _ in range(5):
        t._warn_if_memory_low()
    assert len(capture_alerts) == 1, "주기마다 보내면 경보가 소음이 된다"


def test_worsening_to_critical_alerts_again(capture_alerts):
    t = _Trader(avail=float(config.SYSTEM_MEMORY_WARN_MB) - 1)
    t._warn_if_memory_low()
    t._avail = float(config.SYSTEM_MEMORY_RISK_MB) - 1
    t._warn_if_memory_low()
    assert len(capture_alerts) == 2, "경고 뒤 위험으로 나빠진 것은 새 소식이다"


def test_unmeasurable_memory_does_not_alert(capture_alerts):
    """못 재는 것(비리눅스 등)은 경보하지 않는다 — 0을 '고갈'로 읽지 않는다."""
    t = _Trader(avail=0.0)
    t._warn_if_memory_low()
    assert not capture_alerts


def test_memory_check_is_actually_wired_into_the_cycle():
    """**배선**을 따로 건다 — 산식만 검사하면 호출부가 빠져도 초록이다.

    (2026-09-08 물기 검사에서 실제로 그랬다: `_record_cycle_duration` 에서 호출을
     지워도 위 테스트 11건이 전부 통과했다. 능동 경보의 값어치는 '주기마다 스스로
     도는 것'에 있으므로, 그 연결이 곧 기능이다.)
    """
    from modules.auto_trade.trader import AutoTrader

    class _Cycle:
        def __init__(self):
            self.checked = 0
            self.cycle_secs_history = []
            self._record_cycle_duration = AutoTrader._record_cycle_duration.__get__(self)

        def _warn_if_memory_low(self):
            self.checked += 1

        def log(self, msg):
            pass

    c = _Cycle()
    c._record_cycle_duration(12.3, log=False)
    assert c.checked == 1, "주기가 끝났는데 메모리를 보지 않았다"


def test_undelivered_alert_is_not_marked_as_sent(monkeypatch):
    """전달 실패를 '보냈다'로 굳히면 그날은 영영 조용하다."""
    class _Pkg:
        @staticmethod
        def alert_delivered(msg, urgent=False):
            return False

    monkeypatch.setattr("modules.auto_trade.trader._pkg", lambda: _Pkg)
    t = _Trader(avail=float(config.SYSTEM_MEMORY_RISK_MB) - 1)
    t._warn_if_memory_low()
    assert getattr(t, "_mem_alert_key", None) is None
