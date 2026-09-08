"""실패한 백업이 **'오늘의 백업'으로 굳지** 못하게 한다.

[왜 · 2026-09-08 감사] `DBManager.backup` 은 맨 앞에서 `os.path.exists(dest)` 를
'오늘 것은 이미 떴다'로 읽었다. 그런데 `sqlite3.connect(dest)` 가 파일을 **먼저**
만든다 — `src.backup(dst)` 이 도중에 실패하면(디스크 가득·잠금·전원 차단) 비거나
반쪽인 파일이 그 이름으로 남고, 같은 날 다시 부르면 그것을 성공한 백업으로 읽고
되돌아간다. 백업이 아닌 것이 백업 자리를 차지한 채 회전까지 살아남는다.

거기에 회전은 `.db` 로 끝나는 것만 지워서 `-wal`·`-shm` 이 영영 남았다. 용량 문제
이기도 하지만 더 나쁜 것은 같은 이름이 다시 생겼을 때 SQLite 가 낡은 WAL 을 새 DB 에
붙여 복구를 시도한다는 점이다.

이 파일은 운영 DB 를 절대 건드리지 않는다(tmp_path 위에 자기 DB 를 만든다).
"""
import os
import sqlite3

import pytest

from modules.db_manager import DBManager


@pytest.fixture
def db(tmp_path, monkeypatch):
    """운영 경로와 무관한 임시 DB 를 가진 DBManager."""
    import config
    path = tmp_path / "trade_history.db"
    monkeypatch.setattr(config, "DB_FILE_PATH", str(path))
    inst = DBManager()
    assert os.path.abspath(inst.db_path) == os.path.abspath(str(path))
    return inst


def _backup_dir(db):
    return os.path.join(os.path.dirname(os.path.abspath(db.db_path)), "backups")


def test_backup_creates_file(db):
    dest = db.backup()
    assert dest and os.path.exists(dest)
    # 진짜 열리는 DB 여야 한다(빈 파일이 아니다)
    con = sqlite3.connect(dest)
    assert con.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    con.close()


class _FailingBackupConn:
    """`.backup()` 만 실패하는 연결 대역. sqlite3.Connection 은 불변이라 감싼다."""

    def __init__(self, real):
        self._real = real

    def backup(self, *a, **kw):
        raise sqlite3.OperationalError("disk I/O error")

    def close(self):
        self._real.close()

    def __getattr__(self, name):
        return getattr(self._real, name)


@pytest.fixture
def break_backup(monkeypatch):
    """backup() 호출만 실패시킨다. 되돌리면 진짜 백업이 다시 된다."""
    import modules.db_manager as dbm
    real_connect = sqlite3.connect
    state = {"on": True}

    def _connect(path, *a, **kw):
        con = real_connect(path, *a, **kw)
        # backup() 은 **원본 연결**에서 불린다(src.backup(dst)).
        return _FailingBackupConn(con) if state["on"] else con

    monkeypatch.setattr(dbm.sqlite3, "connect", _connect)
    return state


def test_failed_backup_leaves_no_file_to_mistake_for_success(db, break_backup):
    """백업 도중 실패하면 그 이름의 파일이 남지 않는다."""
    assert db.backup() is None

    bdir = _backup_dir(db)
    leftovers = os.listdir(bdir) if os.path.isdir(bdir) else []
    assert not leftovers, f"실패했는데 잔해가 남았다: {leftovers}"


def test_retry_after_failure_makes_a_real_backup(db, break_backup):
    """같은 날 실패 뒤 다시 부르면 **진짜로 다시 뜬다**(옛 동작은 반쪽을 성공으로 읽었다)."""
    assert db.backup() is None

    break_backup["on"] = False          # 장애 해소
    dest = db.backup()
    assert dest and os.path.exists(dest)
    con = sqlite3.connect(dest)
    assert con.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    con.close()


def test_rotation_removes_wal_and_shm_sidecars(db):
    """회전이 `.db` 만 지우고 `-wal`·`-shm` 을 남기지 않는다.

    회전은 **새 백업을 뜨는 날에만** 돈다(오늘 것이 이미 있으면 앞에서 되돌아간다).
    그래서 옛 파일을 먼저 깔아 두고 그날의 첫 백업을 부른다.
    """
    bdir = _backup_dir(db)
    os.makedirs(bdir, exist_ok=True)
    base = os.path.splitext(os.path.basename(db.db_path))[0]

    # 지워져야 할 옛 백업을 사이드카까지 만들어 둔다
    old = os.path.join(bdir, f"{base}_20200101.db")
    for p in (old, old + "-wal", old + "-shm"):
        with open(p, "w") as f:
            f.write("")

    assert db.backup(keep=1)
    for p in (old, old + "-wal", old + "-shm"):
        assert not os.path.exists(p), f"회전이 {os.path.basename(p)} 을 남겼다"


def test_todays_completed_backup_is_reused(db):
    """완성된 오늘 백업은 다시 뜨지 않는다(종전 최적화는 유지)."""
    first = db.backup()
    mtime = os.path.getmtime(first)
    second = db.backup()
    assert second == first and os.path.getmtime(second) == mtime
