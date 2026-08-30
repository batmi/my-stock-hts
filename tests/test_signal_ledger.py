"""신호 원장(signal_ledger) — 매수 신호가 게이트에서 어떻게 됐는지의 유일한 증거.

[왜 이 원장이 있는가] 실매매에만 있는 게이트(체결강도·매도잔량비·재진입 차단)는 일봉
 백테스트에 아예 없어, 그 게이트가 무엇을 잘랐는지는 **이 기계의 운영 기록으로만** 셀 수
 있다. 그런데 그 기록이 로그 문자열뿐이라 두 가지가 걸렸다.
   ① 30일 뒤 삭제된다 — config가 "3개월쯤 쌓여야 답할 수 있다"고 적어 둔 바로 그 증거를.
   ② 파싱이 위험하다 — `[매도비:3.92]`(정보 표기)와 `매도비:3.92<1.0`(차단)이 한 글자
      차이라 실제로 차단율을 1.3% → 75%로 뒤집어 읽은 적이 있다.

[그래서 무엇을 못 박는가]
  · **(일자, 종목)당 1행에 주기를 누적한다** — 주기마다 1행이면 하루 17,000행이라 파이3에
    부담이다. 감사가 묻는 것은 "그날 한 번도 못 뚫었나(완전 차단) / 일부만 막혔나"이므로
    누적 카운터면 충분하다. 이 누적이 깨지면 완전·부분 차단 구분이 통째로 무너진다.
  · **매수 상태였던 주기만 센다** — 신호가 아니었던 것까지 세면 차단율의 분모가 부풀어
    기회비용을 과대평가한다.
  · **못 잰 값(NULL)과 0을 가른다** — 토스는 체결강도를 제공하지 않는다. NULL을 0으로
    접으면 '체결강도 0%'라는 없는 사실이 생긴다.
"""
import os
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


def _row(code="005930", name="삼성전자", outcome="passed", score=7.5, vol=120.0, abr=2.0,
         blocked_by=None):
    row = {"code": code, "name": name, "outcome": outcome,
           "score": score, "state": "매수", "vol": vol, "abr": abr}
    if blocked_by:
        row["blocked_by"] = blocked_by
    return row


def test_같은_날_같은_종목은_한_행에_주기가_쌓인다(db):
    """이 누적이 '완전 차단 vs 부분 차단'을 가르는 유일한 근거다."""
    db.record_signal_ledger("20260819", [_row(outcome="gate_vol")])
    db.record_signal_ledger("20260819", [_row(outcome="gate_vol")])
    db.record_signal_ledger("20260819", [_row(outcome="passed")])

    rows = db.get_signal_ledger()
    assert len(rows) == 1, f"행이 나뉘었다: {rows}"
    r = rows[0]
    assert r["cycles"] == 3
    assert r["blocked_vol"] == 2
    assert r["passed"] == 1


def test_완전_차단과_부분_차단이_구분된다(db):
    """'한 번이라도 막힘'으로 세면 오후에 통과해 실제로 산 종목까지 차단으로 잡힌다."""
    # A: 그날 전 주기에서 한 번도 못 뚫음 → 완전 차단
    db.record_signal_ledger("20260819", [_row(code="000001", outcome="gate_abr")])
    db.record_signal_ledger("20260819", [_row(code="000001", outcome="gate_abr")])
    # B: 막혔다가 통과 → 부분 차단
    db.record_signal_ledger("20260819", [_row(code="000002", outcome="gate_abr")])
    db.record_signal_ledger("20260819", [_row(code="000002", outcome="passed")])

    by_code = {r["code"]: r for r in db.get_signal_ledger()}
    assert by_code["000001"]["passed"] == 0 and by_code["000001"]["blocked_abr"] == 2
    assert by_code["000002"]["passed"] == 1 and by_code["000002"]["blocked_abr"] == 1


@pytest.mark.parametrize("outcome,column", [
    ("passed", "passed"),
    ("gate_vol", "blocked_vol"),
    ("gate_abr", "blocked_abr"),
    ("gate_hold", "blocked_hold"),
    ("corr", "blocked_corr"),
    ("rs", "blocked_rs"),
    ("tq", "blocked_tq"),
    ("reentry", "blocked_reentry"),
])
def test_판정_사유가_제_칸에_들어간다(db, outcome, column):
    """사유별 칸이 섞이면 '무엇이 막았나'를 못 센다 — 게이트마다 처방이 다르다."""
    db.record_signal_ledger("20260819", [_row(outcome=outcome)])
    r = db.get_signal_ledger()[0]
    assert r[column] == 1
    others = [c for c in r if c.startswith("blocked_") or c == "passed"]
    assert sum(r[c] for c in others) == 1, f"다른 칸도 올랐다: {r}"


def test_모르는_사유는_기타로_떨어진다(db):
    """새 게이트가 생겨도 기록이 통째로 사라지지 않아야 한다 — 조용한 유실이 최악이다."""
    db.record_signal_ledger("20260819", [_row(outcome="새로생긴게이트")])
    assert db.get_signal_ledger()[0]["blocked_other"] == 1


def test_점수는_최대_체결강도는_최대_매도잔량비는_최소로_남는다(db):
    """그날 그 종목이 '가장 좋았을 때'와 '가장 나빴을 때'를 알아야 사후 판정이 된다."""
    db.record_signal_ledger("20260819", [_row(score=7.0, vol=110.0, abr=3.0)])
    db.record_signal_ledger("20260819", [_row(score=8.5, vol=95.0, abr=1.2)])
    r = db.get_signal_ledger()[0]
    assert r["max_score"] == pytest.approx(8.5)
    assert r["max_vol"] == pytest.approx(110.0)
    assert r["min_abr"] == pytest.approx(1.2)


def test_못_잰_값은_0이_아니라_NULL로_남는다(db):
    """토스는 체결강도를 주지 않는다. NULL을 0으로 접으면 없는 사실이 생긴다."""
    db.record_signal_ledger("20260819", [_row(vol=None, abr=None)])
    r = db.get_signal_ledger()[0]
    assert r["max_vol"] is None and r["min_abr"] is None

    # 한쪽 주기에만 값이 있으면 그 값이 남아야 한다 (NULL이 덮어쓰면 안 된다)
    db.record_signal_ledger("20260819", [_row(vol=130.0, abr=2.5)])
    db.record_signal_ledger("20260819", [_row(vol=None, abr=None)])
    r = db.get_signal_ledger()[0]
    assert r["max_vol"] == pytest.approx(130.0)
    assert r["min_abr"] == pytest.approx(2.5)


def test_날짜와_종목으로_조회된다(db):
    db.record_signal_ledger("20260818", [_row(code="000001")])
    db.record_signal_ledger("20260819", [_row(code="000001")])
    db.record_signal_ledger("20260819", [_row(code="000002")])

    assert len(db.get_signal_ledger(start_date="20260819")) == 2
    assert len(db.get_signal_ledger(end_date="20260818")) == 1
    assert len(db.get_signal_ledger(code="000001")) == 2


def test_기록_실패가_매매를_막지_않는다(db, monkeypatch):
    """계측은 매매를 멈추게 하면 안 된다 — 원장이 깨져도 주문은 나가야 한다."""
    def boom(*a, **k):
        raise RuntimeError("디스크 꽉 참")
    monkeypatch.setattr(db, "_get_conn", boom)
    db.record_signal_ledger("20260819", [_row()])   # 예외가 밖으로 나오면 실패


def test_빈_목록은_아무것도_쓰지_않는다(db):
    db.record_signal_ledger("20260819", [])
    assert db.get_signal_ledger() == []


# ==========================================================
# 판정 지점 → 원장 (실매매 경로)
# ==========================================================
# [왜 여기까지 거는가] 위 테스트는 원장이 '받은 것을 제대로 쌓는가'만 본다. 정작 위험한
#  것은 판정 지점이 **원장에 아무것도 넘기지 않는 것**이다 — 그러면 테스트는 전부 통과한
#  채 원장만 비어 있고, 감사는 다시 로그 파싱으로 돌아간다. 그래서 실제 워커를 돌린다.
import numpy as np  # noqa: E402
import pandas as pd  # noqa: E402
from unittest.mock import patch  # noqa: E402

from core import indicators  # noqa: E402
from modules.auto_trade import AutoTrader  # noqa: E402


@pytest.fixture
def trader():
    AutoTrader._instance = None
    prev = (config.USE_MARKET_FILTER, config.USE_CORRELATION_FILTER,
            getattr(config, "USE_RS_FILTER", False))
    config.USE_MARKET_FILTER = False
    config.USE_CORRELATION_FILTER = False
    config.USE_RS_FILTER = False
    patchers = [
        patch("modules.auto_trade.api.get_current_price", return_value=0),
        patch("modules.auto_trade.api.get_order_book",
              return_value={"rt_cd": "0", "output1": {"total_askp_rsqn": "100",
                                                      "total_bidp_rsqn": "100"}}),
        patch("modules.auto_trade.api.is_nxt_tradeable", return_value=True),
        patch("modules.auto_trade.api.get_realtime_vol_strength", return_value=120.0),
        patch("modules.auto_trade.AutoTrader._get_stock_market_type", return_value="KOSPI"),
    ]
    for p in patchers:
        p.start()
    t = AutoTrader()
    t.is_running = True
    yield t
    for p in patchers:
        p.stop()
    (config.USE_MARKET_FILTER, config.USE_CORRELATION_FILTER,
     config.USE_RS_FILTER) = prev
    AutoTrader._instance = None


def _df(daily_drift, length=200):
    dates = pd.date_range("2024-01-01", periods=length).strftime("%Y%m%d")
    prices = 1000 * np.exp(np.arange(length) * daily_drift)
    return pd.DataFrame({"date": dates, "close": prices, "open": prices,
                         "high": prices * 1.005, "low": prices * 0.995, "volume": 1000})


def _analyze(trader, df, *, action="buy", state="매수", vol_reject=None, **kw):
    item = {"code": "005930", "name": "삼성전자", "group": "stocks_kr"}
    with patch.object(trader.strategy, "analyze_buy") as mock_buy:
        mock_buy.return_value = {
            "action": action, "state": state, "score": 8.0, "rsi": 55, "adx": 30,
            "cci": 50, "atr": 1000.0, "psar": 0, "macd": 1.0, "macd_signal": 0.5,
            "w52_pos": 80.0, "vol_strength": 120.0,
            "vol_reject_reason": vol_reject or "",
            "trend_quality": indicators.get_trend_quality(df),
        }
        with patch("modules.auto_trade.api.get_chart_data", return_value=df):
            return trader._analyze_candidate_worker(
                item, holding_codes=set(), rules_map={}, restricted_stocks={},
                market_regime_adj={"KOSPI": 0.0}, safe_delay=0,
                reentry_hurdles=kw.get("reentry_hurdles", {}),
                holdings_dfs={}, holding_groups_map={},
                stop_exit_prices=kw.get("stop_exit_prices"))


def test_후보가_되면_원장에_통과로_남는다(trader):
    res = _analyze(trader, _df(0.0008))
    assert res["type"] == "candidate"
    assert res["ledger"]["outcome"] == "passed"


def test_추세품질_상한에_걸리면_원장에_사유가_남는다(trader):
    steep = _df(0.006)
    with patch.dict(config.ANALYSIS_THRESHOLDS, {"TREND_QUALITY_MAX": 300.0}):
        res = _analyze(trader, steep)
    assert res["type"] == "tq_cap_skip"
    assert res["ledger"]["outcome"] == "tq"


@pytest.mark.parametrize("reason,outcome", [
    ("체결:78.0%<100.0%", "gate_vol"),
    ("매도비:0.51<1.0", "gate_abr"),
    ("체결강도 미확인(보류)", "gate_hold"),
])
def test_수급_게이트_차단이_사유별로_남는다(trader, reason, outcome):
    """로그에서는 `[매도비:3.92]`(정보)와 `매도비:3.92<1.0`(차단)이 한 글자 차이였다."""
    res = _analyze(trader, _df(0.0008), action="wait", vol_reject=reason)
    assert res["ledger"]["outcome"] == outcome


def test_재진입_차단도_원장에_남는다(trader):
    """백테스트로 필요성을 판정할 수 없는 축이라, 이 기록이 유일한 관측이다."""
    res = _analyze(trader, _df(0.0008), stop_exit_prices={"005930": 1.0})
    assert res["type"] == "log_only"
    assert res["ledger"]["outcome"] == "reentry"


def test_매수_상태가_아니면_원장에_남기지_않는다(trader):
    """신호가 아니었던 주기까지 세면 차단율의 분모가 부풀어 기회비용을 과대평가한다."""
    res = _analyze(trader, _df(0.0008), action="wait", state="관망")
    assert res["ledger"] is None


# ==========================================================
# 계좌 구분 (2026-08-19)
# ==========================================================
#
# 실전과 모의는 **같은 trade_history.db** 를 쓰고, trades는 is_sim으로 갈라 적는다.
# 원장만 안 가르면 두 계좌의 판정이 (일자, 종목) 한 행에 합산된다. 체결강도처럼 시세에서
# 나오는 게이트는 계좌와 무관하지만, 재진입 차단·상관 차단은 **그 계좌의 보유 상태**에서
# 나온다 — 섞이면 차단율이 어느 계좌 얘기인지 알 수 없어진다.
# (관찰모드는 DB 파일 자체가 분리되므로 이 구분과 무관하다.)

def test_같은_계좌의_주기는_종전대로_한_행에_누적된다(db, monkeypatch):
    """계좌를 가른다고 주기 누적이 깨지면 완전·부분 차단 구분이 통째로 무너진다."""
    for _ in range(3):
        db.record_signal_ledger("20260819", [_row(outcome="gate_vol")])

    rows = db.get_signal_ledger(is_sim=0)
    assert len(rows) == 1
    assert rows[0]["cycles"] == 3 and rows[0]["blocked_vol"] == 3


def test_옛_원장은_실전으로_옮겨진다(tmp_path, monkeypatch):
    """is_sim 없이 만들어진 원장이 있어도 기동이 깨지지 않고 행이 보존돼야 한다."""
    import sqlite3

    path = tmp_path / "legacy.sqlite"
    conn = sqlite3.connect(str(path))
    conn.execute('''
        CREATE TABLE signal_ledger (
            date TEXT, code TEXT, name TEXT, cycles INTEGER DEFAULT 0,
            passed INTEGER DEFAULT 0, blocked_vol INTEGER DEFAULT 0,
            blocked_abr INTEGER DEFAULT 0, blocked_hold INTEGER DEFAULT 0,
            blocked_corr INTEGER DEFAULT 0, blocked_rs INTEGER DEFAULT 0,
            blocked_tq INTEGER DEFAULT 0, blocked_reentry INTEGER DEFAULT 0,
            blocked_other INTEGER DEFAULT 0, max_score REAL DEFAULT 0.0,
            max_vol REAL, min_abr REAL, last_state TEXT,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            PRIMARY KEY (date, code)
        )''')
    conn.execute("INSERT INTO signal_ledger (date, code, name, cycles, passed, max_score) "
                 "VALUES ('20260818', '005930', '삼성전자', 7, 2, 8.1)")
    conn.commit()
    conn.close()

    original = config.DB_FILE_PATH
    config.DB_FILE_PATH = str(path)
    try:
        manager = DBManager()
        rows = manager.get_signal_ledger()
        assert len(rows) == 1
        assert rows[0]["is_sim"] == 0, "옛 행은 실전으로 옮겨져야 한다"
        assert rows[0]["cycles"] == 7 and rows[0]["passed"] == 2
        # 옮긴 뒤에도 같은 (일자, 종목)에 계속 누적된다.
        manager.record_signal_ledger("20260818", [_row(outcome="passed")])
        rows = manager.get_signal_ledger(is_sim=0)
        assert len(rows) == 1 and rows[0]["cycles"] == 8
    finally:
        if getattr(getattr(manager, "local", None), "conn", None):
            manager.local.conn.close()
        config.DB_FILE_PATH = original


# ──────────────────────────────────────────────────────────────────────────
# 계좌 상태 차단 (blocked_slot / blocked_cash)
#
# [왜 생겼는가] 2026-08-29까지의 가상투자 기록이 이 공백을 그대로 보여줬다.
#  08-27·28 대한항공은 285~287주기 내내 `passed` 인데 매수는 0건이다 — 슬롯이 4/4로
#  꽉 차 있었기 때문인데, 원장에는 그 사실이 어디에도 없다. 사유는 로그에만 남았고,
#  로그 파싱은 이 원장을 만든 이유 자체라 되돌아갈 수 없다.
#  그 상태로 원장을 읽으면 "신호가 287번 섰다"만 보여, 슬롯·시드를 늘리면 그만큼
#  더 샀을 것처럼 읽힌다.
# ──────────────────────────────────────────────────────────────────────────

def test_슬롯_만석은_신호와_함께_기록된다(db):
    """[핵심] '신호는 섰고 막은 것은 계좌다'가 한 행에서 같이 읽혀야 한다."""
    db.record_signal_ledger("20260827", [_row(outcome="passed", blocked_by="slot")])
    r = db.execute_query("SELECT passed, blocked_slot, blocked_cash, cycles "
                         "FROM signal_ledger WHERE date='20260827'", fetch='one')
    assert (r['passed'], r['blocked_slot'], r['blocked_cash'], r['cycles']) == (1, 1, 0, 1)


def test_예수금_부족도_같은_방식으로_기록된다(db):
    db.record_signal_ledger("20260827", [_row(outcome="passed", blocked_by="cash")])
    r = db.execute_query("SELECT passed, blocked_slot, blocked_cash "
                         "FROM signal_ledger WHERE date='20260827'", fetch='one')
    assert (r['passed'], r['blocked_slot'], r['blocked_cash']) == (1, 0, 1)


def test_계좌_상태는_게이트_판정을_덮지_않는다(db):
    """둘은 직교한다 — 게이트 결과를 계좌 사유로 갈아치우면 차단율이 통째로 틀어진다."""
    db.record_signal_ledger("20260827", [_row(outcome="gate_vol", blocked_by="slot")])
    r = db.execute_query("SELECT passed, blocked_vol, blocked_slot "
                         "FROM signal_ledger WHERE date='20260827'", fetch='one')
    assert r['blocked_vol'] == 1
    assert r['passed'] == 0
    assert r['blocked_slot'] == 0, "게이트가 이미 막은 신호를 슬롯 탓으로 셌다(기회비용 과대평가)"


def test_계좌_상태도_주기마다_누적된다(db):
    for _ in range(3):
        db.record_signal_ledger("20260827", [_row(outcome="passed", blocked_by="slot")])
    db.record_signal_ledger("20260827", [_row(outcome="passed")])   # 슬롯이 비었다
    r = db.execute_query("SELECT cycles, passed, blocked_slot "
                         "FROM signal_ledger WHERE date='20260827'", fetch='one')
    assert (r['cycles'], r['passed'], r['blocked_slot']) == (4, 4, 3)


def test_모르는_계좌_사유는_아무것도_세지_않는다(db):
    """새 사유가 생겨도 엉뚱한 칸이 오르지 않는다(blocked_other로 새지도 않는다)."""
    db.record_signal_ledger("20260827", [_row(outcome="passed", blocked_by="새사유")])
    r = db.execute_query("SELECT passed, blocked_slot, blocked_cash, blocked_other "
                         "FROM signal_ledger WHERE date='20260827'", fetch='one')
    assert (r['passed'], r['blocked_slot'], r['blocked_cash'], r['blocked_other']) == (1, 0, 0, 0)


def test_옛_원장_파일에도_컬럼이_생긴다(db, tmp_path):
    """[마이그레이션] 이미 돌고 있는 계좌의 원장이 기동에서 깨지면 안 된다."""
    import sqlite3
    path = str(tmp_path / "old.sqlite")
    con = sqlite3.connect(path)
    con.execute("""CREATE TABLE signal_ledger (
        date TEXT, code TEXT, is_sim INTEGER DEFAULT 0, name TEXT,
        cycles INTEGER DEFAULT 0, passed INTEGER DEFAULT 0,
        blocked_vol INTEGER DEFAULT 0, blocked_abr INTEGER DEFAULT 0,
        blocked_hold INTEGER DEFAULT 0, blocked_corr INTEGER DEFAULT 0,
        blocked_rs INTEGER DEFAULT 0, blocked_tq INTEGER DEFAULT 0,
        blocked_reentry INTEGER DEFAULT 0, blocked_other INTEGER DEFAULT 0,
        max_score REAL DEFAULT 0.0, max_vol REAL, min_abr REAL, last_state TEXT,
        updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        PRIMARY KEY (date, code, is_sim))""")
    con.execute("INSERT INTO signal_ledger (date, code, name, cycles, passed) "
                "VALUES ('20260826','003490','대한항공', 30, 30)")
    con.commit(); con.close()

    original = config.DB_FILE_PATH
    config.DB_FILE_PATH = path
    try:
        old = DBManager()
        old.record_signal_ledger("20260827", [_row(code="003490", name="대한항공",
                                                   outcome="passed", blocked_by="slot")])
        rows = old.execute_query("SELECT date, passed, blocked_slot FROM signal_ledger "
                                 "ORDER BY date", fetch='all')
        assert [r['passed'] for r in rows] == [30, 1], "기존 행이 소실됐다"
        assert rows[1]['blocked_slot'] == 1
        if getattr(getattr(old, "local", None), "conn", None):
            old.local.conn.close()
    finally:
        config.DB_FILE_PATH = original
