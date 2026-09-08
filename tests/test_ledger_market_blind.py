"""시장 필터가 켜진 계좌에서 신호 원장이 통째로 비던 자리.

[무엇이 있었나 · 2026-09-08] 원장은 2026-08-19에 생겼는데, 실전 계좌(trade_history.db)에는
 2026-09-08까지 **한 행도 없었다**. 종목별 시장 필터 검사가 분석보다 먼저 있고
 (trader._analyze_candidate_worker 의 4번), 필터가 막으면 그 자리에서 반환하는데 그
 반환값에 'ledger' 키가 아예 없었기 때문이다. 필터를 끈 계좌(라즈베리파이 가상투자)만
 원장이 쌓였고, 정작 "왜 120일간 못 샀나"를 물어야 할 계좌가 침묵했다.

[왜 이것이 결함인가] 원장의 존재 이유가 '0행'을 가르는 것이다 —
 **신호가 없었다**와 **계측이 돌지 않았다**는 완전히 다른 사실인데, 종전 원장은 둘을
 똑같이 빈 표로 답했다. 로그 파싱으로 돌아가는 것은 원장을 만든 이유가 막은 길이다.

[어떻게 세는가] 시장 필터에 잘린 주기는 다른 차단 사유와 성격이 다르다. 나머지는
 '분석해 보니 미달'이지만 이것은 **분석 자체를 막은** 주기라, 그 종목이 신호였는지
 원장은 모른다. 그래서 전용 칸(blocked_market)에 따로 세고, 게이트 차단율의 분자에
 섞지 않으며 분모에서는 뺀다.
"""
import os
import sqlite3
import sys

import pytest

sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import config  # noqa: E402
from modules.db_manager import DBManager  # noqa: E402


@pytest.fixture
def db(tmp_path):
    original = config.DB_FILE_PATH
    config.DB_FILE_PATH = str(tmp_path / "ledger.sqlite")
    manager = DBManager()
    yield manager
    if getattr(getattr(manager, "local", None), "conn", None):
        manager.local.conn.close()
    config.DB_FILE_PATH = original


def _market(code="005930", name="삼성전자"):
    """시장 필터가 자른 주기 — 판정 전이라 점수도 상태도 없다."""
    return {"code": code, "name": name, "outcome": "market"}


def _passed(code="005930", name="삼성전자", score=8.0):
    return {"code": code, "name": name, "outcome": "passed", "score": score,
            "state": "매수", "vol": 130.0, "abr": 2.0}


# ---------------------------------------------------------------------------
# 기록
# ---------------------------------------------------------------------------

def test_시장_필터가_자른_주기가_원장에_남는다(db):
    db.record_signal_ledger("20260908", [_market(), _market("000660", "SK하이닉스")])
    rows = {r["code"]: r for r in db.get_signal_ledger()}
    assert len(rows) == 2, "필터에 잘린 종목이 원장에서 사라졌다 — 0행 문제가 그대로다"
    assert rows["005930"]["blocked_market"] == 1
    assert rows["005930"]["cycles"] == 1


def test_시장_차단은_게이트_칸을_건드리지_않는다(db):
    """게이트가 막은 것처럼 세면 '체결강도가 이만큼 잘랐다'가 부풀어 오른다."""
    db.record_signal_ledger("20260908", [_market()])
    r = db.get_signal_ledger()[0]
    for col in ("passed", "blocked_vol", "blocked_abr", "blocked_hold",
                "blocked_corr", "blocked_rs", "blocked_tq", "blocked_reentry",
                "blocked_other", "blocked_slot", "blocked_cash"):
        assert r[col] == 0, f"{col} 이 함께 올랐다: {dict(r)}"


def test_판정_불가_주기가_직전에_읽은_상태를_지우지_않는다(db):
    """필터에 잘린 주기는 상태를 재지 못한다(NULL). 그 NULL 이 진짜 상태를 덮으면
    필터가 켜진 하루의 끝에서 원장은 그 종목이 어떤 상태였는지 통째로 잊는다."""
    db.record_signal_ledger("20260908", [_passed()])
    db.record_signal_ledger("20260908", [_market()])
    r = db.get_signal_ledger()[0]
    assert r["last_state"] == "매수", f"마지막으로 읽은 상태가 지워졌다: {dict(r)}"
    assert r["max_score"] == 8.0, f"최고 점수가 0 으로 덮였다: {dict(r)}"
    assert r["cycles"] == 2 and r["passed"] == 1 and r["blocked_market"] == 1


# ---------------------------------------------------------------------------
# 마이그레이션 — 이미 돌고 있는 운영 DB 가 있다
# ---------------------------------------------------------------------------

def test_칸이_없던_원장에_칸이_붙고_기존_행은_그대로다(tmp_path):
    """운영기(파이·맥)에는 이미 원장이 쌓여 있다. 그 행을 잃으면 안 된다."""
    path = tmp_path / "old.sqlite"
    conn = sqlite3.connect(str(path))
    conn.execute("""
        CREATE TABLE signal_ledger (
            date TEXT, code TEXT, is_sim INTEGER DEFAULT 0, name TEXT,
            cycles INTEGER DEFAULT 0, passed INTEGER DEFAULT 0,
            blocked_vol INTEGER DEFAULT 0, blocked_abr INTEGER DEFAULT 0,
            blocked_hold INTEGER DEFAULT 0, blocked_corr INTEGER DEFAULT 0,
            blocked_rs INTEGER DEFAULT 0, blocked_tq INTEGER DEFAULT 0,
            blocked_reentry INTEGER DEFAULT 0, blocked_other INTEGER DEFAULT 0,
            blocked_slot INTEGER DEFAULT 0, blocked_cash INTEGER DEFAULT 0,
            max_score REAL DEFAULT 0.0, max_vol REAL, min_abr REAL, last_state TEXT,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            PRIMARY KEY (date, code, is_sim))
    """)
    conn.execute("INSERT INTO signal_ledger (date, code, is_sim, name, cycles, passed, "
                 "max_score, last_state) VALUES ('20260901','005930',0,'삼성전자',9,4,7.5,'매수')")
    conn.commit(); conn.close()

    original = config.DB_FILE_PATH
    config.DB_FILE_PATH = str(path)
    try:
        manager = DBManager()
        rows = manager.get_signal_ledger()
        assert len(rows) == 1
        assert rows[0]["cycles"] == 9 and rows[0]["passed"] == 4
        assert rows[0]["blocked_market"] == 0, "새 칸이 기존 행에 0 으로 붙어야 한다"
        # 붙은 칸이 실제로 쓰이는가
        manager.record_signal_ledger("20260901", [_market()])
        assert manager.get_signal_ledger()[0]["blocked_market"] == 1
        if getattr(getattr(manager, "local", None), "conn", None):
            manager.local.conn.close()
    finally:
        config.DB_FILE_PATH = original


# ---------------------------------------------------------------------------
# 읽는 쪽 — 계측기가 스스로 조용해지지 않아야 한다
# ---------------------------------------------------------------------------

def test_원장을_못_읽으면_빈_목록이_아니라_모른다고_답한다(db, monkeypatch):
    """빈 목록은 '그 기간에 신호가 없었다'로 읽힌다 — 조회 실패와 글자 하나 다르지 않다."""
    assert db.get_signal_ledger() == [], "행이 없는 것은 여전히 빈 목록이어야 한다"

    def _boom(*a, **k):
        raise sqlite3.OperationalError("no such table: signal_ledger")

    monkeypatch.setattr(type(db), "_get_conn", _boom)
    assert db.get_signal_ledger() is None, "조회 실패가 '신호 없음'으로 접혔다"


def test_감사_도구가_시장_차단을_분모에서_빼고_따로_적는다():
    """시장 차단을 게이트와 같은 분모로 세면 게이트 통과율이 눌려 잘못 읽힌다."""
    import inspect
    from tools import audit_signal_ledger as tool

    src = inspect.getsource(tool.main)
    assert "MARKET_COL" in src, "감사 도구가 시장 차단 칸을 아예 보지 않는다"
    assert "judged = total_cycles - market_cycles" in src, (
        "게이트 비율의 분모에서 시장 차단 주기를 빼지 않는다")
    assert tool.MARKET_COL not in tool.GATE_COLS, "시장 차단이 수급 게이트로 섞였다"
    assert tool.MARKET_COL not in tool.BLOCK_COLS, (
        "시장 차단이 사유별 차단 표에 섞였다 — 분모가 다르므로 따로 적어야 한다")


def test_감사_도구가_못_읽은_원장을_비어_있다고_적지_않는다(monkeypatch):
    import inspect
    from tools import audit_signal_ledger as tool

    src = inspect.getsource(tool.main)
    assert "if rows is None:" in src, (
        "None(조회 실패)을 빈 목록과 같이 다루면, 이 도구가 막으려던 오독을 이 도구가 한다")
    none_at = src.index("if rows is None:")
    empty_at = src.index("if not rows:")
    assert none_at < empty_at, "'비어 있다' 분기가 먼저라 실패가 그리로 흘러간다"
