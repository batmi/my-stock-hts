"""매매일지 대기열의 두 가지 조용한 유실 경로.

① 서버 기록의 주소(remote_id)를 잃으면 이후 손익 정정이 영영 반영되지 않는다.
   서버는 같은 brokerExecutionId 를 duplicate 로 **건너뛰기만** 하고 덮어쓰지 않으므로
   (stock-memo: trading_api/entries._insert_trade), 갱신 경로는 PATCH 하나뿐이다.
   PATCH 는 remote_id 가 있어야 보낼 수 있다.

② 백오프 식이 파이썬(_backoff_seconds)과 SQL(_BACKOFF_SQL) 두 벌로 존재한다.
   갈라지면 죽은 서버에 매 순회 재요청을 때리거나(백오프 무력화), 멀쩡한 행이
   과하게 묶인다. 주석의 "같아야 한다"는 선언은 시간이 지나면 거짓이 된다.
"""
import sqlite3

import pytest

from modules import journal_sync as js


SCHEMA = """CREATE TABLE journal_outbox(
 id INTEGER PRIMARY KEY AUTOINCREMENT, exec_id TEXT UNIQUE, payload TEXT,
 created_at TEXT, attempts INTEGER DEFAULT 0, last_attempt_at TEXT, last_error TEXT,
 synced_at TEXT, remote_id TEXT, dead_at TEXT, reject_count INTEGER DEFAULT 0,
 is_backlog INTEGER DEFAULT 0, needs_patch INTEGER DEFAULT 0)"""

# enqueue(resend=True) 의 upsert — 정정분을 되살리며 PATCH/POST 갈림길을 정한다.
RESEND_SQL = """INSERT INTO journal_outbox(exec_id,payload,created_at,is_backlog)
 VALUES(?,?,?,0)
 ON CONFLICT(exec_id) DO UPDATE SET payload=excluded.payload, synced_at=NULL, attempts=0,
   needs_patch=CASE WHEN journal_outbox.remote_id IS NOT NULL THEN 1 ELSE 0 END,
   last_attempt_at=NULL, last_error=NULL WHERE dead_at IS NULL"""


@pytest.fixture
def db(monkeypatch):
    con = sqlite3.connect(":memory:")
    con.row_factory = sqlite3.Row
    con.execute(SCHEMA)
    con.execute("INSERT INTO journal_outbox(exec_id,payload,created_at) VALUES('E1','{}','t')")
    con.commit()

    class _FakeDB:
        lock = __import__('threading').RLock()

        def _get_conn(self):
            return con

    import modules.db_manager as dbm
    monkeypatch.setattr(dbm, 'db', _FakeDB())
    return con


def _row(con):
    return con.execute("SELECT * FROM journal_outbox WHERE exec_id='E1'").fetchone()


def _resend(con):
    con.execute(RESEND_SQL, ('E1', '{"pnl":1}', 't2'))
    con.commit()


# ─────────── ① remote_id ───────────

def test_a_duplicate_without_an_id_does_not_erase_the_address(db):
    """서버가 id 를 생략한 duplicate 응답을 보내도 주소를 잃지 않아야 한다."""
    js._mark_result({1: (True, 'R7', None, False)}, 't1')       # 최초 전송 — id 받음
    assert _row(db)['remote_id'] == 'R7'

    js._mark_result({1: (True, None, None, False)}, 't2')       # 재전송 — id 없는 duplicate
    assert _row(db)['remote_id'] == 'R7', "서버 기록의 주소를 지웠다"


def test_losing_the_address_would_send_the_correction_the_wrong_way(db):
    """주소를 잃으면 어떤 일이 벌어지는지 — 이 테스트가 위 보호의 이유다."""
    db.execute("UPDATE journal_outbox SET synced_at='t1', remote_id=NULL")
    db.commit()
    _resend(db)
    assert _row(db)['needs_patch'] == 0, (
        "주소가 없으면 POST 경로로 간다 — 서버는 duplicate 로 건너뛰고 정정이 사라진다")


def test_with_the_address_kept_the_correction_goes_out_as_a_patch(db):
    """대조군 — 정상 흐름."""
    js._mark_result({1: (True, 'R7', None, False)}, 't1')
    js._mark_result({1: (True, None, None, False)}, 't2')
    _resend(db)
    row = _row(db)
    assert row['needs_patch'] == 1 and row['remote_id'] == 'R7'
    assert row['synced_at'] is None, "정정분이 전송 대기로 돌아오지 않았다"


def test_a_real_id_still_overwrites_an_older_one(db):
    """COALESCE 가 갱신까지 막으면 안 된다."""
    js._mark_result({1: (True, 'R7', None, False)}, 't1')
    js._mark_result({1: (True, 'R9', None, False)}, 't2')
    assert _row(db)['remote_id'] == 'R9'


# ─────────── ② 백오프 ───────────

@pytest.mark.parametrize("attempts", [0, 1, 2, 3, 5, 6, 7, 12, 100])
def test_the_sql_and_python_backoff_agree(attempts):
    con = sqlite3.connect(":memory:")
    con.execute(SCHEMA)
    con.execute("INSERT INTO journal_outbox(exec_id,attempts) VALUES('X',?)", (attempts,))
    got = con.execute(f"SELECT {js._BACKOFF_SQL} FROM journal_outbox").fetchone()[0]
    assert got == js._backoff_seconds(attempts)


def test_a_null_attempts_does_not_strand_the_row():
    """식이 NULL 이 되면 그 행은 두 조회 어디에도 안 잡혀 미전송인 채로 사라진다."""
    con = sqlite3.connect(":memory:")
    con.execute(SCHEMA)
    con.execute("INSERT INTO journal_outbox(exec_id,attempts) VALUES('X',NULL)")
    got = con.execute(f"SELECT {js._BACKOFF_SQL} FROM journal_outbox").fetchone()[0]
    assert got == js._backoff_seconds(0)


def test_the_backoff_is_capped_at_an_hour():
    assert js._backoff_seconds(99) == 3600
    assert js._backoff_seconds(0) == 60
