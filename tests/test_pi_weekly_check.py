"""운용 사본 주간 점검 도구(tools/pi_weekly_check.py) — 합성 사본으로 집계와 '다시 볼 것'을 고정한다.

이 도구는 사본을 **읽기만** 하고(운영 모듈·config 를 import 하지 않는다) 기계가 설계대로 돌았는지만 본다.
"""
import io
import json
import os
import sqlite3
import sys
from datetime import date

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "tools"))
import pi_weekly_check as wk  # noqa: E402


def _auto(logs, day, cycles, blocked_cycles, codes=40, extra=()):
    lines = []
    for i in range(cycles):
        lines.append(f"[{day} 09:{i % 60:02d}:00.000] 모니터링 주기 시작...")
        if i < blocked_cycles:
            lines.append(f"[{day} 09:{i % 60:02d}:01.000] [시장 필터링] 하락장 매수 보류 ({codes}종목): 삼성전자, …")
        lines.append(f"[{day} 09:{i % 60:02d}:03.000] 모니터링 완료 (소요 {3.0 + (i % 3)}초 · 다음 주기까지 60초 대기 "
                     f"→ 청산 감시 간격 63초). 대기 중...")
    lines += [f"[{day} 16:00:00.000] {x}" for x in extra]
    name = f"autotrade_{day.replace('-', '')}.log"
    (logs / name).write_text("\n".join(lines) + "\n", encoding="utf-8")


def _app(logs, day, rate=0, other=0, boots=()):
    lines = ["09:00:01.000 [ERROR] [at_prefetch_1] http.py:719 - ⚠️ API 요청 최종 실패. 사유: Rate Limit Exceeded "
             "(EGW00201): 초당 거래건수를 초과하였습니다."] * rate
    lines += ["09:00:02.000 [ERROR] [MainThread] http.py:719 - ⚠️ API 요청 최종 실패. 사유: HTTPSConnectionPool"] * other
    for pid, code in boots:
        lines.append(f"17:14:47.219 [INFO] [MainThread] config.py:4015 - === MyStock HTS 시스템 시작 "
                     f"(PID {pid}, {'코드 ' + code + ', ' if code else ''}파일 로그 레벨: WARNING) ===")
    (logs / f"mystock_{day.replace('-', '')}.log").write_text("\n".join(lines) + "\n", encoding="utf-8")


def _db(path, ledger, assets, fills=()):
    con = sqlite3.connect(path)
    con.executescript("""
        CREATE TABLE signal_ledger (date TEXT, code TEXT, is_sim INTEGER, cycles INTEGER, passed INTEGER,
                                    blocked_market INTEGER);
        CREATE TABLE paper_fills (id INTEGER PRIMARY KEY, time TEXT, type TEXT, code TEXT, qty INTEGER);
        CREATE TABLE daily_asset_history (date TEXT, account TEXT, asset REAL, net_transfer REAL, principal REAL);
        CREATE TABLE paper_state (key TEXT, value TEXT);
        CREATE TABLE paper_positions (code TEXT, qty INTEGER);
        INSERT INTO paper_state VALUES ('seed', '10000000'), ('cash', '10000000'),
                                       ('started_at', '"2026-10-25 20:48:35"');
        PRAGMA user_version = 2;
    """)
    for day, codes, per_code in ledger:
        con.executemany("INSERT INTO signal_ledger VALUES (?,?,1,?,0,?)",
                        [(day.replace("-", ""), f"{i:06d}", per_code, per_code) for i in range(codes)])
    con.executemany("INSERT INTO daily_asset_history VALUES (?, 'PAPER-', ?, ?, NULL)", assets)
    con.executemany("INSERT INTO paper_fills (time, type, code, qty) VALUES (?, '매수', '005930', 1)",
                    [(f"{d} 10:00:00",) for d in fills])
    con.commit()
    con.close()


@pytest.fixture
def copy(tmp_path):
    logs = tmp_path / "pi_copy_logs"      # conftest 가 tmp_path/logs 를 이미 쓴다
    logs.mkdir()
    return tmp_path, logs


def _run(tmp_path, logs, **kw):
    out = io.StringIO()
    rows, issues = wk.run(tmp_path / "paper.db", logs, out=out, **kw)
    return rows, issues, out.getvalue()


def test_정상인_주는_다시_볼_것이_없다(copy):
    tmp_path, logs = copy
    days = ("2026-10-26", "2026-10-27", "2026-10-28")
    for d in days:
        _auto(logs, d, cycles=20, blocked_cycles=18,
              extra=["[동적 손절 캡] 지수 변동성 배율 x3.00 → ATR 손절 캡 -26.0% (기준 10/26 확정 종가, YF 3,679봉)"])
        _app(logs, d, rate=5)
    _db(tmp_path / "paper.db", [(d, 40, 18) for d in days], [(d, 10_000_000.0, 0.0) for d in days])
    (logs / "heartbeat.paper.json").write_text(json.dumps(
        {"state": "alive", "ts": 1793622051.0, "iso": "2026-10-28 21:20:51", "pid": 1, "host": "Earth",
         "commit": "4a23f94", "auto_running": True}), encoding="utf-8")

    rows, issues, text = _run(tmp_path, logs)
    assert [r["cycles"] for r in rows] == [20, 20, 20]
    assert all(r["filter_cycles"] == 18 and r["filter_codes"] == 720 for r in rows)
    assert all(r["ledger"]["blocked_market"] == 720 for r in rows)
    assert all(r["rate"] == 5 and r["cap"] == "x3.00 → -26.0%" for r in rows)
    assert rows[0]["dur_med"] == 4.0 and rows[0]["dur_max"] == 5.0
    assert issues == [], issues
    assert "일치 720" in text and "코드 4a23f94" in text and "다시 볼 것 없음" in text
    table = [line for line in text.splitlines() if line.startswith(("날짜", "---", "10-2"))]
    assert len(table) == 5 and max(wk._w(line) for line in table) <= 135, "표 폭 상한(135열)을 넘는다"


def test_원장_불일치와_자산_변화와_자원_신호를_짚는다(copy):
    tmp_path, logs = copy
    _auto(logs, "2026-10-26", cycles=20, blocked_cycles=18)
    _auto(logs, "2026-10-27", cycles=20, blocked_cycles=18,
          extra=["⚠️ [메모리 경고] 가용 메모리 116MB (문턱 120MB)", "시스템 시작",
                 "모니터링 완료 (소요 75.0초 · 다음 주기까지 60초 대기 → 청산 감시 간격 135초). 대기 중..."])
    _auto(logs, "2026-10-28", cycles=9, blocked_cycles=9)                 # 주기가 절반 — 재기동 기록 없음
    _auto(logs, "2026-10-29", cycles=20, blocked_cycles=18)
    _app(logs, "2026-10-27", rate=3, other=2, boots=[("820292", "4a23f94 (main)")])
    _db(tmp_path / "paper.db",
        [("2026-10-26", 40, 18), ("2026-10-27", 40, 17), ("2026-10-29", 40, 18)],     # 27일 한 주기 빠짐, 28일 행 없음
        [("2026-10-26", 10_000_000.0, 0.0), ("2026-10-27", 10_000_000.0, 0.0),
         ("2026-10-28", 9_500_000.0, 0.0), ("2026-10-29", 9_500_000.0, -300_000.0)])

    _, issues, text = _run(tmp_path, logs)
    joined = "\n".join(issues)
    assert "10-27 시장 필터 원장 불일치 — 로그 720건 · 신호 원장 680건" in joined
    assert "10-28 시장 필터가 9주기를 막았는데 신호 원장에 그날 행이 없습니다" in joined
    assert "10-28 주기 9회" in joined and "재기동 기록이 없습니다" in joined
    assert "10-27 주기 최대 75.0초" in joined
    assert "10-27 메모리 경고 1건" in joined
    assert "10-27 초당 한도가 아닌 API 최종 실패 2건" in joined
    assert "10-27 기동 — 자동매매 1회 · 프로세스 PID 820292 코드 4a23f94 (main)" in joined
    assert "10-28 시작 자산이 10,000,000 → 9,500,000원으로 바뀌었는데 전날 체결이 없습니다" in joined
    assert "10-29 순입출금 -300,000원" in joined
    assert "불일치 680" in text and "원장 없음" in text


def test_체결이_있던_날의_자산_변화는_짚지_않는다(copy):
    tmp_path, logs = copy
    _auto(logs, "2026-10-26", cycles=20, blocked_cycles=0)
    _auto(logs, "2026-10-27", cycles=20, blocked_cycles=0)
    _db(tmp_path / "paper.db", [], [("2026-10-26", 10_000_000.0, 0.0), ("2026-10-27", 9_900_000.0, 0.0)],
        fills=["2026-10-26"])
    _, issues, _ = _run(tmp_path, logs)
    assert not any("시작 자산" in i for i in issues), issues


def test_날짜_없는_진행_중_앱_로그는_사본의_날짜에_붙는다(copy):
    """mystock.log 줄에는 시각만 있다 — '오늘'로 치면 며칠 전 사본의 한도 실패가 틀린 날에 붙는다."""
    tmp_path, logs = copy
    (logs / "autotrade.log").write_text(
        "[2026-11-02 09:00:03.000] 모니터링 완료 (소요 3.0초 · 다음 주기까지 60초 대기 → 청산 감시 간격 63초). 대기 중...\n",
        encoding="utf-8")
    (logs / "mystock.log").write_text(
        "09:00:01.000 [ERROR] [x] http.py:719 - ⚠️ API 요청 최종 실패. 사유: Rate Limit Exceeded (EGW00201)\n",
        encoding="utf-8")
    rows, _, _ = _run(tmp_path, logs, today=date(2026, 11, 14))
    assert [(r["day"].isoformat(), r["cycles"], r["rate"]) for r in rows] == [("2026-11-02", 1, 1)]


def test_옛_경로의_하트비트는_뺀다(copy):
    tmp_path, logs = copy
    _auto(logs, "2026-10-26", cycles=20, blocked_cycles=0)
    (logs / "heartbeat.json").write_text(json.dumps(
        {"state": "stopped", "ts": 1788566297.0, "iso": "2026-09-05 08:58:17", "reason": "스케줄러 종료"}), "utf-8")
    (logs / "heartbeat.paper.json").write_text(json.dumps(
        {"state": "alive", "ts": 1793622051.0, "iso": "2026-11-02 21:20:51"}), "utf-8")
    _, issues, text = _run(tmp_path, logs)
    assert "heartbeat.paper.json" in text and "heartbeat.json:" not in text
    assert not any("하트비트" in i for i in issues)


def test_DB_가_없어도_로그만으로_돈다(copy):
    tmp_path, logs = copy
    _auto(logs, "2026-10-26", cycles=5, blocked_cycles=5)
    rows, issues, text = _run(tmp_path, logs)
    assert rows[0]["ledger"] is None and "없음 — 로그만 봅니다" in text
    assert not any("신호 원장" in i for i in issues)


def test_사본을_읽기만_한다(copy):
    """운영 모듈·config 를 import 하지 않고, DB 는 mode=ro 로 연다."""
    src = open(os.path.join(ROOT, "tools", "pi_weekly_check.py"), encoding="utf-8").read()
    assert "?mode=ro" in src
    for banned in ("import config", "from modules", "import api", "from core", "from brokers"):
        assert banned not in src, f"점검 도구가 운영 코드를 불러온다: {banned}"
