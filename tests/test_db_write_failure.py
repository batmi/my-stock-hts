"""DB 쓰기 실패가 조용히 사라지지 않는가.

[왜 조용한가] 종전에는 실패해도 console.print 한 줄이 전부였고, 그마저
SCREEN_DEBUG_LEVEL=OFF면 안 나왔다. 반환값이 없어 호출부는 실패를 알 수 없었고,
logger 를 쓰지 않아 로그 파일에도 안 남았다. 헤드리스 라즈베리파이에는 콘솔을 보는
사람이 없다 — 세 겹으로 안 보였다.

[왜 위험한가] 실패해도 호출부는 인메모리 캐시(trailing_stop_cache)를 갱신하고 넘어간다.
그래서 **그 세션 동안은 정상으로 보이고, 재기동해야 소실이 드러난다.** 그 시점엔
트레일링 최고가가 옛 값이라 청산선이 통째로 어긋나 있다.

발생 조건이 운영 환경과 겹친다 — SD카드 가득 참(database or disk is full), I/O 오류.
"""
import os
import sqlite3
import time

import pytest
from unittest.mock import patch

from modules import db_manager
from modules.auto_trade import AutoTrader


@pytest.fixture
def readonly_db(tmp_path):
    """쓰기가 **실제로** 실패하는 DB. 예외를 흉내 내지 않고 파일 권한으로 막는다.

    모킹으로 만든 실패는 '어느 예외 타입을 잡는가'를 검증하지 못한다.
    """
    path = tmp_path / "ro.db"
    conn = sqlite3.connect(str(path))
    conn.execute("CREATE TABLE trailing_stops (code TEXT PRIMARY KEY, highest_price REAL, "
                 "update_time TEXT, ref_avg_price REAL DEFAULT 0, ref_pchs_amt REAL DEFAULT 0)")
    conn.execute("CREATE TABLE trades (time TEXT, type TEXT, code TEXT, name TEXT, qty TEXT, "
                 "price TEXT, odno TEXT, org_odno TEXT, account TEXT, is_sim INT, snapshot TEXT, "
                 "profit_amt INT, profit_rate REAL, reason TEXT, strategy_score REAL, "
                 "order_status TEXT, stop_loss_rate REAL)")
    conn.commit()
    conn.close()
    os.chmod(str(path), 0o444)
    os.chmod(str(tmp_path), 0o555)          # -wal/-journal 생성도 막아야 쓰기가 실패한다

    saved = db_manager.db.db_path
    db_manager.db.close_all_connections()
    db_manager.db.db_path = str(path)
    db_manager.db.reset_write_failures()
    try:
        yield path
    finally:
        os.chmod(str(tmp_path), 0o755)
        os.chmod(str(path), 0o644)
        db_manager.db.close_all_connections()
        db_manager.db.db_path = saved
        db_manager.db.reset_write_failures()


def test_the_readonly_fixture_actually_blocks_writes(readonly_db):
    """전제 확인 — 쓰기가 정말 실패하는가. 아니면 아래 테스트가 전부 무의미하다."""
    with pytest.raises(sqlite3.Error):
        conn = sqlite3.connect(str(readonly_db))
        conn.execute("INSERT INTO trailing_stops (code, highest_price) VALUES ('X', 1)")
        conn.commit()


def test_trailing_high_write_failure_is_reported(readonly_db):
    """[핵심] 최고가 저장 실패가 반환값과 카운터에 남아야 한다."""
    ok = db_manager.db.update_highest_price("005930", 70000.0)
    assert ok is False, "쓰기가 실패했는데 성공으로 보고했다 — 호출부가 알 방법이 없다"

    h = db_manager.db.get_write_failures()
    assert h['count'] == 1 and "트레일링" in h['last_op']
    assert "005930" in h['recent'][-1][2], "어느 종목이 유실됐는지 안 남는다"


def test_write_failure_goes_to_the_log_file(readonly_db, caplog):
    """logger 로 남아야 한다 — 헤드리스 운영에는 콘솔을 보는 사람이 없다."""
    with caplog.at_level("ERROR", logger="modules.db_manager"):
        db_manager.db.update_highest_price("005930", 70000.0)
    assert any("쓰기 실패" in r.message for r in caplog.records), \
        "로그 파일에 안 남는다 — 사후 추적이 불가능하다"


def test_position_ref_failure_is_reported(readonly_db):
    """권리조정 기준값은 종전에 실패 경로에 출력이 아예 없었다."""
    assert db_manager.db.update_position_ref("005930", 70000.0, 700000.0) is False
    assert db_manager.db.get_write_failures()['count'] == 1


def test_trade_record_failure_is_reported(readonly_db):
    assert db_manager.db.insert_trade("매수", "005930", "삼성전자", 10, 70000, "ODNO1") is False
    h = db_manager.db.get_write_failures()
    assert h['count'] == 1 and "거래" in h['last_op']


def test_successful_writes_are_not_counted_as_failures(tmp_path):
    """대조군 — 정상 쓰기가 실패로 잡히면 경보가 상시가 된다."""
    path = tmp_path / "ok.db"
    saved = db_manager.db.db_path
    db_manager.db.close_all_connections()
    db_manager.db.db_path = str(path)
    try:
        db_manager.db._init_db()
        db_manager.db.reset_write_failures()
        assert db_manager.db.update_highest_price("005930", 70000.0) is True
        assert db_manager.db.get_write_failures()['count'] == 0
    finally:
        db_manager.db.close_all_connections()
        db_manager.db.db_path = saved


def test_failure_history_is_bounded(readonly_db):
    """유실 이력이 무한히 자라면 안 된다(1GB 라즈베리파이)."""
    for i in range(db_manager.db.WRITE_FAILURE_KEEP + 10):
        db_manager.db.update_highest_price(f"{i:06d}", 100.0)
    h = db_manager.db.get_write_failures()
    assert len(h['recent']) == db_manager.db.WRITE_FAILURE_KEEP
    assert h['count'] == db_manager.db.WRITE_FAILURE_KEEP + 10, "누적 건수까지 잘리면 안 된다"


# ───────────────────────── 알림·표시 배선 ─────────────────────────

@pytest.fixture
def trader():
    AutoTrader._instance = None
    t = AutoTrader()
    t._db_write_fail_seen = 0
    t._db_write_fail_alerted_at = 0.0
    yield t


def test_new_write_failures_raise_an_alert(trader):
    """[배선] 집계만 하고 안 알리면 재기동 전까지 아무도 모른다."""
    with patch.object(db_manager.db, 'get_write_failures',
                      return_value={'count': 3, 'last_op': '트레일링 최고가',
                                    'last_error': 'disk I/O error', 'last_at': time.time(),
                                    'recent': []}), \
         patch.object(db_manager.db, 'disk_free_mb', return_value=12.0), \
         patch('modules.auto_trade.api.send_telegram_message') as tg:
        trader._check_db_write_failures()
    assert tg.called and "DB 쓰기 실패" in str(tg.call_args), "쓰기 실패를 알리지 않았다"


def test_repeat_failures_do_not_spam(trader):
    """디스크가 차면 매 주기 실패한다 — 도배되면 정작 중요한 경보가 묻힌다."""
    state = {'count': 1}
    with patch.object(db_manager.db, 'get_write_failures',
                      side_effect=lambda: {'count': state['count'], 'last_op': 'x',
                                           'last_error': 'e', 'last_at': 0, 'recent': []}), \
         patch.object(db_manager.db, 'disk_free_mb', return_value=12.0), \
         patch('modules.auto_trade.api.send_telegram_message') as tg:
        for _ in range(5):
            state['count'] += 1
            trader._check_db_write_failures()
    assert tg.call_count == 1, f"같은 원인으로 {tg.call_count}건 알렸다"


def test_no_alert_when_nothing_failed(trader):
    """대조군 — 실패가 없으면 조용해야 한다."""
    with patch.object(db_manager.db, 'get_write_failures',
                      return_value={'count': 0, 'last_op': '', 'last_error': '',
                                    'last_at': None, 'recent': []}), \
         patch('modules.auto_trade.api.send_telegram_message') as tg:
        trader._check_db_write_failures()
    assert not tg.called


def test_low_disk_warns_but_never_blocks_trading(trader):
    """[핵심 방침] 디스크 부족으로 매매를 막지 않는다.

    손상과 다르다 — 지금 읽는 값은 옳다. 여기서 멈추면 보유 포지션의 손절 감시까지
    함께 멈춰서, 기록을 지키려다 돈을 잃는다.
    """
    with patch.object(db_manager.db, 'check_integrity', return_value=(True, "ok")), \
         patch.object(db_manager.db, 'backup', return_value="/tmp/b.db"), \
         patch.object(db_manager.db, 'disk_free_mb', return_value=5.0), \
         patch('modules.auto_trade.api._is_screen_output_allowed', return_value=False), \
         patch('modules.auto_trade.api.send_telegram_message') as tg:
        assert trader._check_db_health() is True, "디스크 부족이 매매를 막았다"
    assert tg.called and "디스크" in str(tg.call_args), "조용히 넘어갔다"


def test_ample_disk_stays_quiet(trader):
    with patch.object(db_manager.db, 'check_integrity', return_value=(True, "ok")), \
         patch.object(db_manager.db, 'backup', return_value="/tmp/b.db"), \
         patch.object(db_manager.db, 'disk_free_mb', return_value=50_000.0), \
         patch('modules.auto_trade.api.send_telegram_message') as tg:
        assert trader._check_db_health() is True
    assert not tg.called, "여유가 충분한데 경고했다"


def test_health_panel_shows_storage_trouble(trader):
    """[배선] 상태창에 드러나야 한다."""
    with patch.object(db_manager.db, 'disk_free_mb', return_value=50_000.0), \
         patch.object(db_manager.db, 'get_write_failures',
                      return_value={'count': 0, 'last_op': '', 'last_error': '',
                                    'last_at': None, 'recent': []}):
        assert "쓰기 실패" not in trader._health_storage_text()

    with patch.object(db_manager.db, 'disk_free_mb', return_value=12.0), \
         patch.object(db_manager.db, 'get_write_failures',
                      return_value={'count': 7, 'last_op': '트레일링 최고가',
                                    'last_error': 'disk I/O error', 'last_at': 0, 'recent': []}):
        text = trader._health_storage_text()
    assert "쓰기 실패 7건" in text and "12MB" in text, f"상태창이 고장을 안 보여준다: {text}"
