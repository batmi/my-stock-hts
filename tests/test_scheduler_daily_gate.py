"""하루 한 번짜리 알림은 '일이 끝난 것'을 확인한 뒤에 날짜를 찍는다.

[왜] 스케줄러의 세 알림(캘린더·휴장 안내·장전 브리핑)은 창에 들어서자마자
 `last_*_date = 오늘`을 찍고 그 뒤에 조회·발송을 했다. 그래서 조회가 던지거나 텔레그램이
 끊겨 있어도 하루가 이미 소비돼, 남은 창 동안 다시 시도하지 않았다.

 캘린더는 특히 나쁘다 — events.check_and_alert_calendar 는 2026-09-04 에 '전달을 확인한
 뒤에만 표시한다, 실패하면 다음 기회에 다시 시도'로 고쳐 뒀는데, **그 다음 기회를 이
 게이트가 없애고 있었다**(실측: 워커가 실패해도 같은 날 재시도 0회). D-1 알림은 다음 날
 보내 봐야 소용이 없다.

 반대 방향도 지켜야 한다 — '보낼 것이 없는 날'(주말·정상 개장·일정 없음)까지 재시도로
 남기면 무거운 수집이 창 내내 반복된다(파이 1GB). 0(없음)과 -1(전달 실패)을 가른 이유다.
"""
from datetime import datetime, timedelta

import pytest

import config
from modules import scheduler


@pytest.fixture
def sched(monkeypatch):
    """싱글톤을 건드리지 않고 메서드만 시험한다(AutoTrader 생성 회피).

    SystemScheduler.__new__ 는 싱글톤이라 그대로 부르면 **테스트끼리 같은 객체를 공유**해
    재시도 타이머가 새어 나간다. object.__new__ 로 매번 새 껍데기를 만든다.
    """
    s = object.__new__(scheduler.SystemScheduler)
    s.last_calendar_alert_date = None
    s.last_holiday_notified_date = None
    s.last_briefing_date = None
    return s


#  벽시계에 기대지 않는다 — 발송 창은 하루 몇 시간뿐이라 그대로 두면 테스트가
#  '그 시각에만' 도는 물건이 된다(이미 겪은 자리다: [[test-suite-open-delay-clock]]).
FIXED = datetime(2026, 9, 8, 8, 45)          # 2026-09-08 = 화요일 08:45
FIXED_DAY = FIXED.strftime("%Y-%m-%d")


@pytest.fixture
def frozen(monkeypatch):
    real_dt = datetime

    class _DT(real_dt):
        @classmethod
        def now(cls, tz=None):
            return FIXED

        @classmethod
        def today(cls):
            return FIXED

    monkeypatch.setattr(scheduler, 'datetime', _DT)
    return _DT


# ---------------------------------------------------------------- 캘린더
class _InlineThread:
    """스레드를 즉시 실행으로 바꿔 결과를 그 자리에서 본다."""
    def __init__(self, target=None, **kw):
        self.target = target

    def start(self):
        self.target()


@pytest.fixture
def calendar_window(monkeypatch, sched, frozen):
    monkeypatch.setattr(config.settings, 'AUTO_CALENDAR_ALERT_TIME', "0845",
                        raising=False)
    monkeypatch.setattr(scheduler.threading, 'Thread', _InlineThread)
    return sched


def _patch_calendar(monkeypatch, result):
    calls = []

    def fake(*a, **k):
        calls.append(1)
        if isinstance(result, Exception):
            raise result
        return result

    import modules.manage.events as ev
    monkeypatch.setattr(ev, 'check_and_alert_calendar', fake)
    return calls


def test_캘린더_전달_실패는_오늘을_소비하지_않는다(calendar_window, monkeypatch):
    s = calendar_window
    calls = _patch_calendar(monkeypatch, -1)          # 보내려 했으나 전달 미확인
    s._check_calendar_alerts()
    assert len(calls) == 1
    assert s.last_calendar_alert_date is None, "실패했는데 오늘을 다 썼다"

    # 재시도 간격이 지나면 같은 날 다시 건다.
    s._last_calendar_attempt = FIXED - timedelta(seconds=s.CALENDAR_RETRY_SEC + 1)
    s._check_calendar_alerts()
    assert len(calls) == 2


def test_캘린더_보낼것이_없으면_오늘은_끝이다(calendar_window, monkeypatch):
    s = calendar_window
    calls = _patch_calendar(monkeypatch, 0)          # 보낼 일정이 없었다
    s._check_calendar_alerts()
    assert s.last_calendar_alert_date == FIXED_DAY

    s._last_calendar_attempt = FIXED - timedelta(seconds=s.CALENDAR_RETRY_SEC + 1)
    s._check_calendar_alerts()
    assert len(calls) == 1, "무거운 수집을 창 내내 반복하면 안 된다"


def test_캘린더_워커_예외도_오늘을_소비하지_않는다(calendar_window, monkeypatch):
    s = calendar_window
    _patch_calendar(monkeypatch, RuntimeError("DART 조회 실패"))
    s._check_calendar_alerts()
    assert s.last_calendar_alert_date is None


def test_캘린더_재시도_간격_안에는_다시_걸지_않는다(calendar_window, monkeypatch):
    s = calendar_window
    calls = _patch_calendar(monkeypatch, -1)
    s._check_calendar_alerts()
    s._check_calendar_alerts()
    s._check_calendar_alerts()
    assert len(calls) == 1


# ---------------------------------------------------------------- 휴장 안내
def test_휴장안내는_전달을_확인한_뒤_오늘을_찍는다(sched, monkeypatch, frozen):
    monkeypatch.setattr(scheduler.api, 'is_holiday_today', lambda: True)
    monkeypatch.setattr(scheduler.api, 'is_us_holiday_today', lambda: False)
    monkeypatch.setattr(scheduler.api, 'get_holiday_name', lambda *a, **k: "추석")

    delivered = []
    monkeypatch.setattr(scheduler, 'alert_delivered',
                        lambda msg, urgent=False: (delivered.append(msg), False)[1])
    sched._check_holiday_notification()
    assert delivered, "발송 시도조차 없었다"
    assert sched.last_holiday_notified_date is None, "전달 미확인인데 오늘을 다 썼다"

    monkeypatch.setattr(scheduler, 'alert_delivered', lambda msg, urgent=False: True)
    sched._last_holiday_attempt = FIXED - timedelta(seconds=sched.NOTICE_RETRY_SEC + 1)
    sched._check_holiday_notification()
    assert sched.last_holiday_notified_date == FIXED_DAY


def test_휴장여부_조회_실패도_오늘을_소비하지_않는다(sched, monkeypatch, frozen):
    def boom():
        raise RuntimeError("휴장 달력 조회 실패")

    monkeypatch.setattr(scheduler.api, 'is_holiday_today', boom)
    sched._check_holiday_notification()
    assert sched.last_holiday_notified_date is None


def test_정상_개장일은_보낼것이_없으니_오늘을_찍는다(sched, monkeypatch, frozen):
    monkeypatch.setattr(scheduler.api, 'is_holiday_today', lambda: False)
    monkeypatch.setattr(scheduler.api, 'is_us_holiday_today', lambda: False)
    monkeypatch.setattr(scheduler.api, 'get_holiday_name', lambda *a, **k: None)
    calls = []
    monkeypatch.setattr(scheduler, 'alert_delivered',
                        lambda msg, urgent=False: (calls.append(msg), True)[1])
    sched._check_holiday_notification()
    assert not calls
    assert sched.last_holiday_notified_date == FIXED_DAY


# ---------------------------------------------------------------- 장전 브리핑
def test_브리핑_예고_전달_실패는_오늘을_소비하지_않는다(sched, monkeypatch, frozen):
    monkeypatch.setattr(config, 'AUTO_MORNING_BRIEFING_TIME', "0830", raising=False)
    started = []
    monkeypatch.setattr(scheduler.threading, 'Thread',
                        lambda **k: type('T', (), {'start': lambda self: started.append(1)})())
    monkeypatch.setattr(scheduler, 'alert_delivered', lambda msg, urgent=False: False)
    sched._check_morning_briefing()
    assert sched.last_briefing_date is None
    assert not started, "예고도 못 보냈는데 무거운 브리핑을 돌렸다"

    monkeypatch.setattr(scheduler, 'alert_delivered', lambda msg, urgent=False: True)
    sched._last_briefing_attempt = FIXED - timedelta(seconds=sched.NOTICE_RETRY_SEC + 1)
    sched._check_morning_briefing()
    assert sched.last_briefing_date == FIXED_DAY
    assert started
