"""스케줄러는 봇 명령 수신과 무관하게 돌고, 유지보수 작업(하트비트·원장 백업)은 항상 한다.

[왜 · 2026-10-17] 스케줄러는 ENABLE_TELEGRAM 이 꺼지면 start() 에서 돌아갔다. `--no-bot`(파이와
 같은 봇 토큰의 폴링 충돌 회피)이 그 값을 내리므로, 맥북 실전 인스턴스는 하트비트·수능일 재수집이
 멈췄고 원장 백업은 자동매매가 돌 때만 떠서 09-15 에 멈췄다(원장은 10-16 까지 쓰임).
 반대로 알림성 작업(휴장 안내·브리핑·공시·일정·시장 정지)은 봇을 켠 인스턴스만 보내야
 두 기기가 같은 안내를 두 번 보내지 않는다.
"""
import threading
from datetime import datetime
from unittest.mock import MagicMock

import pytest

import config
from modules import scheduler


@pytest.fixture
def sched():
    """싱글톤을 건드리지 않는 새 껍데기(AutoTrader 생성 회피)."""
    s = object.__new__(scheduler.SystemScheduler)
    s.is_running = False
    s.thread = None
    s._wake = threading.Event()
    s.last_backup_date = None
    s.last_backup_attempt = 0.0
    return s


FIXED = datetime(2026, 10, 17, 9, 0)


@pytest.fixture
def frozen(monkeypatch):
    real_dt = datetime

    class _DT(real_dt):
        @classmethod
        def now(cls, tz=None):
            return FIXED

    monkeypatch.setattr(scheduler, 'datetime', _DT)


# ---------------------------------------------------------------- 기동

def test_텔레그램이_꺼져도_스케줄러는_뜬다(monkeypatch, sched):
    monkeypatch.setattr(config, "ENABLE_TELEGRAM", False, raising=False)
    hold = threading.Event()
    sched._run_loop = lambda: hold.wait(5)
    sched.start()
    try:
        assert sched.thread is not None and sched.thread.is_alive(), \
            "ENABLE_TELEGRAM=False 에서 start() 가 되돌아갔다 — --no-bot 인스턴스의 하트비트·백업이 멈춘다"
    finally:
        hold.set()
        sched.thread.join(timeout=2)


NOTIFY = ("_check_holiday_notification", "_check_morning_briefing", "_check_disclosure_alerts",
          "_check_calendar_alerts", "_check_market_halt")
MAINT = ("_check_heartbeat", "_check_db_backup", "_check_holidays_package", "_check_csat_date")


def _one_round(monkeypatch, sched, telegram_on):
    monkeypatch.setattr(config, "ENABLE_TELEGRAM", telegram_on, raising=False)
    monkeypatch.setattr(config, "AUTO_MORNING_BRIEFING_USE", True, raising=False)
    monkeypatch.setattr(config.settings, "AUTO_DISCLOSURE_ALERT_USE", True, raising=False)
    monkeypatch.setattr(config.settings, "AUTO_CALENDAR_ALERT_USE", True, raising=False)
    monkeypatch.setattr(config, "MARKET_HALT_ALERT_USE", True, raising=False)
    calls = {n: MagicMock() for n in NOTIFY + MAINT}
    for n, m in calls.items():
        setattr(sched, n, m)
    sched.is_running = True
    sched._wake.set()               # 한 바퀴만 돌고 빠진다
    sched._run_loop()
    return calls


def test_no_bot_은_알림성_작업만_끄고_유지보수는_돈다(monkeypatch, sched):
    calls = _one_round(monkeypatch, sched, telegram_on=False)
    for n in NOTIFY:
        assert not calls[n].called, f"{n} 이 --no-bot 인스턴스에서 돌았다 — 두 기기가 같은 안내를 두 번 보낸다"
    for n in MAINT:
        assert calls[n].called, f"{n} 이 --no-bot 인스턴스에서 멈췄다"


def test_봇을_켜면_전부_돈다(monkeypatch, sched):
    calls = _one_round(monkeypatch, sched, telegram_on=True)
    for n in NOTIFY + MAINT:
        assert calls[n].called, f"{n} 이 돌지 않았다"


# ---------------------------------------------------------------- 원장 일일 백업

@pytest.fixture
def fake_db(monkeypatch):
    from modules import db_manager
    db = MagicMock()
    monkeypatch.setattr(db_manager, "db", db)
    return db


def test_백업은_하루_한_번(frozen, sched, fake_db):
    fake_db.backup.return_value = "/x/trade_history_20261017.db"
    sched._check_db_backup()
    sched._check_db_backup()
    assert fake_db.backup.call_count == 1
    assert sched.last_backup_date == "2026-10-17"


def test_백업_실패는_간격을_두고_재시도하고_알림은_하루_한_번(monkeypatch, frozen, sched, fake_db):
    sent = []
    monkeypatch.setattr(scheduler, "alert_delivered", lambda m, *a, **k: sent.append(m) or True)
    clock = [1_000_000.0]
    monkeypatch.setattr(scheduler.time, "time", lambda: clock[0])
    fake_db.backup.return_value = None

    sched._check_db_backup()
    assert fake_db.backup.call_count == 1 and sched.last_backup_date is None
    sched._check_db_backup()                          # 재시도 간격 전 — 다시 부르지 않는다
    assert fake_db.backup.call_count == 1

    clock[0] += sched.BACKUP_RETRY_SEC + 1
    sched._check_db_backup()
    assert fake_db.backup.call_count == 2
    assert len(sent) == 1, "같은 날 실패 알림이 두 번 나갔다"

    fake_db.backup.return_value = "/x/ok.db"
    clock[0] += sched.BACKUP_RETRY_SEC + 1
    sched._check_db_backup()
    assert sched.last_backup_date == "2026-10-17"


def test_실제_DB_로_사본이_생긴다():
    """가짜가 아닌 DBManager.backup 경로로 — 백업 디렉터리에 오늘 사본이 생긴다."""
    import os
    from modules import db_manager
    repo_db = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "db")
    assert not os.path.abspath(db_manager.db.db_path).startswith(repo_db), "운영 DB 를 보고 있다 — 격리 실패"
    path = db_manager.db.backup()
    assert path and path.endswith(datetime.now().strftime("%Y%m%d") + ".db")
