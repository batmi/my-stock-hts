"""체결 인지 지연 계측(fill_latency)의 계약.

[왜 · 2026-09-13] 웹소켓의 값어치는 백테스트로 못 잰다 — 이득이 지연에 있다. 그래서
 체결 감시가 체결을 알아챌 때마다 (WS가 먼저 알린 시각, 인지 시각, 깨운 주체, 폴링 상한)을
 한 행에 남긴다. 계측이 틀리면 두 방향으로 아프다:
   · 'WS가 안 알렸다'가 0초 지연으로 둔갑하면 웹소켓이 완벽해 보인다
   · 계측이 예외를 던지면 체결 기록 자체가 멈춘다 — 손절선이 붙을 근거가 사라진다
 그래서 NULL 규약과 '계측은 매매를 막지 않는다'를 여기서 고정한다.
"""
import os
import sys
import time

import pytest

sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from brokers import realtime as rt


@pytest.fixture(autouse=True)
def _clean_seen():
    with rt._EXEC_SEEN_LOCK:
        rt._EXEC_SEEN.clear()
    yield
    with rt._EXEC_SEEN_LOCK:
        rt._EXEC_SEEN.clear()


# ---------------------------------------------------------------- 피드 쪽
def test_첫_이벤트_시각만_남는다():
    """같은 주문의 PENDING → PARTIAL_FILL → FILL 중 **첫** 시각이 지연의 기준이다."""
    rt._note_exec_event({'odno': 'A1', 'event': 'PENDING'})
    t0 = rt.exec_event_first_seen('A1')
    time.sleep(0.01)
    rt._note_exec_event({'odno': 'A1', 'event': 'FILL'})
    assert rt.exec_event_first_seen('A1') == t0


def test_WS가_알린_적_없는_주문은_None이다():
    """0 이나 now 를 돌려주면 '못 알림'이 '즉시 알림'으로 둔갑한다([[unknown-vs-empty]])."""
    assert rt.exec_event_first_seen('NEVER') is None


def test_주문번호_없는_통보는_기록하지_않는다():
    rt._note_exec_event({'event': 'RECONNECT', 'resync': True})
    rt._note_exec_event({'odno': None})
    with rt._EXEC_SEEN_LOCK:
        assert rt._EXEC_SEEN == {}


def test_날짜가_바뀌면_옛_항목을_버린다():
    """주문번호는 당일 채번이라 날짜를 넘기면 유일하지 않다([[odno-daily-reset]])."""
    with rt._EXEC_SEEN_LOCK:
        rt._EXEC_SEEN['OLD'] = (time.time() - 86400, '19990101')
    rt._note_exec_event({'odno': 'NEW'})
    assert rt.exec_event_first_seen('OLD') is None
    assert rt.exec_event_first_seen('NEW') is not None


def test_두_피드_모두_콜백_전에_기록한다():
    """KIS·토스 피드의 _invoke_exec_callbacks 가 같은 기록부를 거친다."""
    for feed in (rt.KisRealtimeFeed(), rt.TossWsFeed()):
        with rt._EXEC_SEEN_LOCK:
            rt._EXEC_SEEN.clear()
        seen_at_callback = []
        feed.register_exec_callback(
            lambda n: seen_at_callback.append(rt.exec_event_first_seen(n['odno'])))
        feed._invoke_exec_callbacks({'odno': 'X9', 'rejected': False, 'is_fill': True})
        assert seen_at_callback and seen_at_callback[0] is not None, type(feed).__name__


# ---------------------------------------------------------------- 감시기 쪽
def _monitor_stub():
    """ConclusionMonitor 를 만들지 않고 _record_fill_latency 만 떼어 쓴다."""
    from modules.auto_trade import conclusion as c

    class _M:
        _cycle_woke_by_ws = True
        _cycle_poll_interval = 300.0
    m = _M()
    m._record_fill_latency = c.ConclusionMonitor._record_fill_latency.__get__(m)
    return m, c


def test_인지_행에_WS_시각과_깨운_주체가_실린다(monkeypatch):
    m, c = _monitor_stub()
    rt._note_exec_event({'odno': 'T1'})
    got = {}
    monkeypatch.setattr(c.db_manager.db, "record_fill_latency",
                        lambda *a: got.update(zip(
                            ("date", "odno", "broker", "code", "side",
                             "ws_event_at", "recognized_at", "woke_by_ws", "poll_interval"), a)))
    monkeypatch.setattr(c.config.session, "is_toss", True, raising=False)
    m._record_fill_latency('T1', '005930', '매수', '20260913')
    assert got['broker'] == 'toss' and got['odno'] == 'T1'
    assert got['ws_event_at'] is not None
    assert got['recognized_at'] >= got['ws_event_at']
    assert got['woke_by_ws'] is True and got['poll_interval'] == 300.0


def test_WS가_안_알린_체결은_NULL로_남는다(monkeypatch):
    m, c = _monitor_stub()
    got = {}
    monkeypatch.setattr(c.db_manager.db, "record_fill_latency",
                        lambda *a: got.update(ws_event_at=a[5]))
    m._record_fill_latency('SILENT', '000660', '매도', '20260913')
    assert got['ws_event_at'] is None


def test_계측_실패가_체결_기록을_막지_않는다(monkeypatch):
    """DB가 죽어도, 피드가 없어도 예외가 바깥으로 나가면 안 된다."""
    m, c = _monitor_stub()
    monkeypatch.setattr(c.db_manager.db, "record_fill_latency",
                        lambda *a: (_ for _ in ()).throw(RuntimeError("db down")))
    m._record_fill_latency('T2', '005930', '매수', '20260913')     # 예외 없이 끝나야 한다


# ---------------------------------------------------------------- DB 쪽
@pytest.fixture
def tmp_db(tmp_path):
    import config
    from modules.db_manager import DBManager
    original = config.DB_FILE_PATH
    config.DB_FILE_PATH = str(tmp_path / "latency.sqlite")
    manager = DBManager()
    yield manager
    if getattr(getattr(manager, "local", None), "conn", None):
        manager.local.conn.close()
    config.DB_FILE_PATH = original


def test_같은_주문은_첫_인지만_남는다(tmp_db):
    db = tmp_db
    now = time.time()
    db.record_fill_latency('20260913', 'O1', 'toss', '005930', '매수', now - 1.5, now, True, 5.0)
    db.record_fill_latency('20260913', 'O1', 'toss', '005930', '매수', None, now + 60, False, 300.0)
    rows = db.get_fill_latency(broker='toss')
    assert rows is not None and len(rows) == 1
    assert abs(rows[0]['recognized_at'] - now) < 1e-6
    assert rows[0]['ws_event_at'] is not None and rows[0]['woke_by_ws'] == 1


def test_조회_실패는_빈_목록이_아니라_None이다(monkeypatch):
    from modules import db_manager
    db = getattr(db_manager.db, "_real_db", db_manager.db)
    # 클래스에 패치한다 — 인스턴스에 패치하면 monkeypatch 가 되돌릴 때 바운드 메서드를 인스턴스
    #  속성으로 남겨, 뒤 테스트의 type(db)._get_conn 패치가 가려진다(test_half_tp_unknown 오염).
    monkeypatch.setattr(type(db), "_get_conn", lambda self: (_ for _ in ()).throw(RuntimeError("boom")))
    assert db.get_fill_latency() is None
