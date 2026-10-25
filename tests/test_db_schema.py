"""DB 스키마 번호(PRAGMA user_version)와 스키마 기록 — C3(2026-10-25).

[왜] db_manager._init_db 는 '열이 없으면 더한다'로 스키마를 맞춘다. 매 기동 돌아 어떤 세대의 파일이든 메우는 것은
 장점이지만(실측 10-25: 맥 실전·파이 두 DB 모두 새 DB 와 같은 열), 번호가 없어
  ① 어느 파일이 어느 코드의 스키마인지, 코드를 되돌려 새 스키마 파일을 옛 코드로 열었는지 알 수 없었고,
  ② CREATE TABLE 에만 열을 넣고 '없으면 더한다' 목록에 빠뜨리면 기존 파일에는 그 열이 영영 생기지 않는데(그 열을
     읽는 조회가 계속 실패한다) 새 DB 로만 도는 테스트는 그것을 보지 못한다.
 여기서 잰다.
  · 새 DB 의 스키마가 기록(fixtures/db_schema.json)과 같다 — 바꿨으면 SCHEMA_VERSION 을 올리고 기록을 다시 만든다.
  · 실제 옛 DB 의 스키마(fixtures/db_schema_history/*.sql)로 만든 파일을 지금 코드로 열면 새 DB 와 같은 열이 된다.
    번호를 올려 기록을 다시 만들 때 직전 세대의 DDL 이 이 폴더에 더해진다 — 세대마다 올라오는 길을 계속 잰다.
  · 코드의 INSERT·UPDATE 가 쓰는 열이 스키마에 모두 있다.
  · 번호: 새 DB·옛 DB 는 SCHEMA_VERSION 을 받고, 더 큰 번호는 낮추지 않고 경고하며, 열 추가가 실패하면 올리지 않는다.
기록 갱신: DB_SCHEMA_UPDATE=1 python -m pytest tests/test_db_schema.py -o addopts=
"""
import ast
import glob
import json
import logging
import os
import re
import sqlite3

import pytest

import config
from modules import db_manager

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
FIXTURE = os.path.join(ROOT, "tests", "fixtures", "db_schema.json")
HISTORY = os.path.join(ROOT, "tests", "fixtures", "db_schema_history")


def _schema(path):
    """테이블별 열(이름·형·NOT NULL·기본값·PK 순번)과 인덱스 이름. 열 순서는 파일에 붙은 순서다."""
    c = sqlite3.connect(path)
    try:
        tables = {}
        for (t,) in c.execute("SELECT name FROM sqlite_master WHERE type='table' "
                              "AND name NOT LIKE 'sqlite_%' ORDER BY name").fetchall():
            tables[t] = [list(r[1:]) for r in c.execute(f"PRAGMA table_info({t})").fetchall()]
        indexes = sorted(r[0] for r in c.execute(
            "SELECT name FROM sqlite_master WHERE type='index' AND sql IS NOT NULL").fetchall())
        return {"tables": tables, "indexes": indexes}
    finally:
        c.close()


def _ddl(path):
    c = sqlite3.connect(path)
    try:
        return [r[0].strip() for r in c.execute(
            "SELECT sql FROM sqlite_master WHERE sql IS NOT NULL AND name NOT LIKE 'sqlite_%' "
            "ORDER BY CASE type WHEN 'table' THEN 0 ELSE 1 END, name").fetchall()]
    finally:
        c.close()


def _user_version(path):
    c = sqlite3.connect(path)
    try:
        return c.execute("PRAGMA user_version").fetchone()[0]
    finally:
        c.close()


def _open(path, monkeypatch):
    """그 경로로 DB 관리자를 새로 만든다(생성자가 _init_db 를 돈다) — 운용 기동과 같은 길."""
    monkeypatch.setattr(config, "DB_FILE_PATH", path)
    m = db_manager.DBManager()
    m.close_all_connections()
    return m


@pytest.fixture
def fresh_db(tmp_path, monkeypatch):
    path = str(tmp_path / "fresh.db")
    _open(path, monkeypatch)
    return path


def test_새_DB_스키마가_기록과_같다(fresh_db):
    got = _schema(fresh_db)
    ver = db_manager.SCHEMA_VERSION
    if os.environ.get("DB_SCHEMA_UPDATE") == "1":
        if os.path.exists(FIXTURE):
            with open(FIXTURE, encoding="utf-8") as f:
                prev = json.load(f)
            #  세대가 바뀌면 직전 세대의 DDL 을 이력에 남긴다 — 그 세대 파일이 지금 코드로 올라오는지 계속 잰다.
            if prev.get("version") != ver and prev.get("ddl"):
                out = os.path.join(HISTORY, f"v{prev['version']}.sql")
                with open(out, "w", encoding="utf-8") as f:
                    f.write(f"-- 스키마 세대 {prev['version']} 의 새 DB DDL(직전 tests/fixtures/db_schema.json 에서).\n")
                    f.write("".join(s + ";\n\n" for s in prev["ddl"]))
        with open(FIXTURE, "w", encoding="utf-8") as f:
            json.dump({"version": ver, **got, "ddl": _ddl(fresh_db)}, f, ensure_ascii=False, indent=1)
            f.write("\n")
        pytest.skip("스키마 기록을 다시 만들었다")

    with open(FIXTURE, encoding="utf-8") as f:
        rec = json.load(f)
    if got != {"tables": rec["tables"], "indexes": rec["indexes"]}:
        changed = sorted(set(got["tables"]) ^ set(rec["tables"])) + [
            t for t in got["tables"] if t in rec["tables"] and got["tables"][t] != rec["tables"][t]]
        if got["indexes"] != rec["indexes"]:
            changed.append("인덱스")
        if rec["version"] == ver:
            pytest.fail(f"스키마가 바뀌었는데 db_manager.SCHEMA_VERSION 이 {ver} 그대로다 — 번호를 올리고 기록을 다시 "
                        f"만든다(DB_SCHEMA_UPDATE=1). 바뀐 곳: {changed}")
        pytest.fail(f"기록이 옛 세대({rec['version']})다 — DB_SCHEMA_UPDATE=1 로 다시 만든다. 바뀐 곳: {changed}")
    assert rec["version"] == ver, (
        f"SCHEMA_VERSION 만 {ver} 로 바뀌고 기록은 {rec['version']} 이다 — DB_SCHEMA_UPDATE=1 로 다시 만든다")


def test_새_DB_는_현재_번호를_받는다(fresh_db):
    assert _user_version(fresh_db) == db_manager.SCHEMA_VERSION


HISTORY_FILES = sorted(glob.glob(os.path.join(HISTORY, "*.sql")))


def test_옛_스키마_표본이_있다():
    assert len(HISTORY_FILES) >= 2, "실제 옛 DB 의 DDL 표본이 사라졌다 — 아래 '올라오는 길' 검사가 아무것도 재지 않는다"


@pytest.mark.parametrize("ddl_file", HISTORY_FILES, ids=os.path.basename)
def test_옛_DB_는_지금_코드로_열면_새_DB_와_같은_열이_된다(ddl_file, fresh_db, tmp_path, monkeypatch):
    old = str(tmp_path / "old.db")
    c = sqlite3.connect(old)
    with open(ddl_file, encoding="utf-8") as f:
        c.executescript(f.read())
    c.close()
    assert _user_version(old) == 0

    _open(old, monkeypatch)

    want, got = _schema(fresh_db), _schema(old)
    missing_tables = sorted(set(want["tables"]) - set(got["tables"]))
    assert not missing_tables, f"옛 DB 에 생기지 않은 테이블: {missing_tables}"
    missing = {t: sorted({c[0] for c in cols} - {c[0] for c in got["tables"][t]})
               for t, cols in want["tables"].items()}
    missing = {t: v for t, v in missing.items() if v}
    assert not missing, (f"옛 DB 에 생기지 않은 열 {missing} — CREATE TABLE 에만 넣고 "
                         f"_init_db 의 _add_missing_columns 목록에 빠뜨렸다(기존 파일에는 영영 안 생긴다)")
    assert set(want["indexes"]) <= set(got["indexes"]), "옛 DB 에 생기지 않은 인덱스"
    assert _user_version(old) == db_manager.SCHEMA_VERSION


def test_더_새_번호의_파일은_낮추지_않고_경고한다(fresh_db, monkeypatch, caplog):
    c = sqlite3.connect(fresh_db)
    c.execute(f"PRAGMA user_version = {db_manager.SCHEMA_VERSION + 3}")
    c.commit()
    c.close()
    with caplog.at_level(logging.WARNING, logger="modules.db_manager"):
        _open(fresh_db, monkeypatch)
    assert _user_version(fresh_db) == db_manager.SCHEMA_VERSION + 3, "더 새 코드의 번호를 낮췄다"
    assert any("더 새 코드의 스키마" in r.getMessage() for r in caplog.records), "코드를 되돌린 사실이 로그에 없다"


def test_열_추가가_실패하면_번호를_올리지_않고_운용자_창구로_올린다(tmp_path, monkeypatch):
    old = str(tmp_path / "old.db")
    c = sqlite3.connect(old)
    with open(HISTORY_FILES[0], encoding="utf-8") as f:
        c.executescript(f.read())
    c.close()

    real = db_manager.DBManager._add_missing_columns

    def _fail_trades(self, cursor, table, columns):
        failed = real(self, cursor, table, columns)
        return failed + (["trades.cost_amt"] if table == "trades" else [])
    monkeypatch.setattr(db_manager.DBManager, "_add_missing_columns", _fail_trades)

    m = _open(old, monkeypatch)
    assert _user_version(old) == 0, "덜 갖춘 파일에 현재 번호를 붙였다 — 다음 기동이 다시 메운다는 표시가 사라진다"
    wf = m.get_write_failures()
    assert wf["count"] and wf["last_op"] == "DB 초기화" and "trades.cost_amt" in str(wf["last_error"])


def test_열_추가_실패는_목록으로_돌려준다(tmp_path, monkeypatch):
    """ALTER 가 실제로 실패하는 경우(뷰에는 열을 더할 수 없다) — 예외로 초기화 전체를 멈추지 않는다."""
    m = _open(str(tmp_path / "a.db"), monkeypatch)
    conn = sqlite3.connect(m.db_path)
    cur = conn.cursor()
    cur.execute("CREATE VIEW v_trades AS SELECT id FROM trades")
    try:
        assert m._add_missing_columns(cur, "v_trades", (("x", "TEXT"),)) == ["v_trades.x"]
        assert m._add_missing_columns(cur, "trades", {"cost_amt": "REAL"}) == [], "이미 있는 열을 다시 더하려 했다"
    finally:
        conn.close()


# ── 코드가 쓰는 열은 스키마에 있다 ────────────────────────────────────────────
_INSERT = re.compile(r"\bINSERT\s+(?:OR\s+\w+\s+)?INTO\s+(\w+)\s*\(([^)]*)\)", re.I | re.S)
_UPDATE = re.compile(r"\bUPDATE\s+(\w+)\s+SET\s+(.+?)(?:\bWHERE\b|$)", re.I | re.S)


def _sql_strings():
    """modules·core·api·brokers 의 문자열 상수. f-string 은 값 자리를 '{}' 로 둔다(그 열은 검사하지 않는다)."""
    for base in ("modules", "core", "api", "brokers"):
        for path in glob.glob(os.path.join(ROOT, base, "**", "*.py"), recursive=True):
            with open(path, encoding="utf-8") as f:
                tree = ast.parse(f.read(), path)
            for node in ast.walk(tree):
                if isinstance(node, ast.Constant) and isinstance(node.value, str):
                    yield os.path.relpath(path, ROOT), node.value
                elif isinstance(node, ast.JoinedStr):
                    yield os.path.relpath(path, ROOT), "".join(
                        str(v.value) if isinstance(v, ast.Constant) else "{}" for v in node.values)


def test_INSERT_UPDATE_가_쓰는_열이_스키마에_있다(tmp_path, monkeypatch):
    path = str(tmp_path / "s.db")
    _open(path, monkeypatch)
    #  가상 계좌 표는 같은 파일에 paper_broker 가 만든다 — 같은 길로 얹는다.
    from modules import paper_broker
    monkeypatch.setattr(db_manager.db, "db_path", path)
    db_manager.db.close_all_connections()
    paper_broker.init_tables()
    db_manager.db.close_all_connections()
    cols = {t: {c[0] for c in v} for t, v in _schema(path)["tables"].items()}

    checked, bad = 0, []
    for where, s in _sql_strings():
        for t, body in _INSERT.findall(s):
            if t in cols:
                checked += 1
                bad += [f"{where}: {t}.{c}" for c in (x.strip() for x in body.split(","))
                        if c and "{" not in c and c not in cols[t]]
        for t, body in _UPDATE.findall(s):
            if t in cols:
                checked += 1
                bad += [f"{where}: {t}.{c}" for c in re.findall(r"(\w+)\s*=", body)
                        if c not in cols[t]]
    assert checked >= 40, f"INSERT·UPDATE 를 {checked}개만 찾았다 — 추출이 깨졌다"
    assert not bad, f"스키마에 없는 열에 쓰는 SQL: {bad}"
