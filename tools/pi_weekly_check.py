"""운용 사본 주간 점검 — 운영 기기에서 복사해 온 DB·로그로 '지난 며칠이 정상이었나'를 한 화면에 낸다.

[왜 · 2026-11-14] 파이(가상투자)의 정상 동작은 맥북으로 복사한 사본(db/RasPi3B · logs/RasPi3B)으로 확인한다.
 그때마다 같은 것을 손으로 셌다 — 하루 주기 수, 시장 필터가 막은 주기, 신호 원장과의 일치, 체결·자산,
 주기 소요, 초당 한도 실패, 메모리 경고, 재기동, 리스크 배수·손절 캡. 이 도구는 그 묶음을 한 번에 낸다.

[하지 않는 것] 운영 모듈·config 를 import 하지 않고 SQLite 는 mode=ro 로 연다(사본이든 운영 파일이든 건드리지
 않는다). 전략 성적을 평가하지 않는다 — '기계가 설계대로 돌았는가'만 본다. 모르는 값은 '-' 로 적는다.

[읽는 것]
  · 자동매매 로그(autotrade*.log): `모니터링 완료 (소요 N초` = 한 주기, `[시장 필터링] 하락장 매수 보류 (N종목)`,
    `[메모리 경고]`·`[메모리 위험]`, `시스템 시작`(자동매매 기동), `[리스크 스케일링] 신규 진입 리스크 한도`,
    `[동적 손절 캡]`, ⚠️·🚨·⛔·❌ 가 든 줄.
  · 앱 로그(mystock*.log): `API 요청 최종 실패`(초당 한도·그 밖), `MyStock HTS 시스템 시작 (PID …, 코드 …)`.
  · DB: signal_ledger(일자·종목 누적), paper_fills 또는 trades(체결), daily_asset_history, paper_state.
  · 하트비트(logs 폴더의 heartbeat*.json): 상태·시각·pid·기동 때의 코드 커밋.

[원장 일치] 시장 필터가 막은 주기마다 로그는 막힌 종목 수를 적고, 신호 원장은 종목마다 blocked_market 을 1 올린다.
 그러므로 그날 `Σ(로그의 N종목)` = `Σ(원장 blocked_market)` 이어야 한다. 어긋나면 한쪽이 빠진 것이다.

[실행]
  python tools/pi_weekly_check.py                    # 최근 7일(사본 기본 경로)
  python tools/pi_weekly_check.py --days 14
  python tools/pi_weekly_check.py --since 2026-10-26 --db db/RasPi3B/paper_trading.db --logs logs/RasPi3B
"""
from __future__ import annotations

import argparse
import glob
import json
import os
import re
import sqlite3
import statistics
import sys
import unicodedata
from collections import Counter, defaultdict
from datetime import date, datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_DB = ROOT / "db/RasPi3B/paper_trading.db"
DEFAULT_LOGS = ROOT / "logs/RasPi3B"

_LINE_TS = re.compile(r"^\[(\d{4}-\d{2}-\d{2}) (\d{2}:\d{2}:\d{2})")
_CYCLE = re.compile(r"모니터링 완료 \(소요 ([\d.]+)초")
_FILTER = re.compile(r"\[시장 필터링\] 하락장 매수 보류 \((\d+)종목\)")
_RISK = re.compile(r"\[리스크 스케일링\] 신규 진입 리스크 한도[^—-]*[—-]\s*(.+?)(?:\s*\||$)")
_CAP = re.compile(r"\[동적 손절 캡\] 지수 변동성 배율 x([\d.]+) → ATR 손절 캡 (-?[\d.]+)%")
_START = re.compile(r"MyStock HTS 시스템 시작 \(PID (\d+)(?:, 코드 (.+?))?, 파일 로그")
_ALERT_MARKS = ("⚠️", "🚨", "⛔", "❌")
WEEKDAYS = "월화수목금토일"


# ── 화면 폭(한글 2칸) ────────────────────────────────────────────────────────────────
def _w(s):
    return sum(2 if unicodedata.east_asian_width(c) in "WF" else 1 for c in s)


def _pad(s, width, right=False):
    s = str(s)
    gap = max(0, width - _w(s))
    return (" " * gap + s) if right else (s + " " * gap)


# ── 로그 ────────────────────────────────────────────────────────────────────────────
def _file_day(path, fallback):
    """로그 파일의 날짜. 이름(_YYYYMMDD) → 첫 줄의 날짜 → fallback 순."""
    m = re.search(r"_(\d{8})\.log$", os.path.basename(path))
    if m:
        d = m.group(1)
        return f"{d[:4]}-{d[4:6]}-{d[6:]}"
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            for line in f:
                t = _LINE_TS.match(line)
                if t:
                    return t.group(1)
    except OSError:
        pass
    return fallback


def read_autotrade(logs_dir):
    """날짜별 자동매매 로그 집계. {day: {...}}"""
    days = defaultdict(lambda: {"cycles": 0, "durs": [], "filter_cycles": 0, "filter_codes": 0,
                                "mem": 0, "starts": 0, "risk": None, "cap": None, "alerts": Counter()})
    for path in sorted(glob.glob(os.path.join(logs_dir, "autotrade*.log"))):
        try:
            f = open(path, encoding="utf-8", errors="replace")
        except OSError:
            continue
        with f:
            for line in f:
                t = _LINE_TS.match(line)
                if not t:
                    continue
                d = days[t.group(1)]
                body = line[line.find("]") + 2:].rstrip("\n")
                m = _CYCLE.search(body)
                if m:
                    d["cycles"] += 1
                    d["durs"].append(float(m.group(1)))
                    continue
                m = _FILTER.search(body)
                if m:
                    d["filter_cycles"] += 1
                    d["filter_codes"] += int(m.group(1))
                    continue
                if "[메모리 경고]" in body or "[메모리 위험]" in body:
                    d["mem"] += 1
                if body.strip() == "시스템 시작":
                    d["starts"] += 1
                m = _RISK.search(body)
                if m:
                    d["risk"] = re.sub(r"\(종목당 [\d.]+%\)", "", m.group(1)).strip()
                m = _CAP.search(body)
                if m:
                    d["cap"] = f"x{m.group(1)} → {m.group(2)}%"
                if any(k in body for k in _ALERT_MARKS):
                    d["alerts"][re.sub(r"\d[\d,.:%-]*", "N", body)[:70]] += 1
    return days


def _current_day(logs_dir, path):
    """이름에 날짜가 없는(아직 넘어가지 않은) 앱 로그의 날짜.

    앱 로그 줄에는 시각만 있다. 같은 폴더의 진행 중 자동매매 로그(autotrade.log)가 **같은 날 고쳐진 파일**이면
    그 첫 줄의 날짜를 쓴다(사본을 다음 날 복사해도 내용의 날짜가 맞다). 아니면 — 자동매매를 한동안 돌리지 않아
    autotrade.log 가 옛 파일이면 — 이 파일을 마지막으로 고친 날이다. '오늘'로 치면 며칠 전 사본이 틀린 날에 붙는다.
    """
    try:
        mine = date.fromtimestamp(os.path.getmtime(path))
    except OSError:
        return None
    cur = os.path.join(logs_dir, "autotrade.log")
    try:
        if date.fromtimestamp(os.path.getmtime(cur)) == mine:
            day = _file_day(cur, None)
            if day:
                return day
    except OSError:
        pass
    return mine.isoformat()


def read_app_log(logs_dir):
    """날짜별 앱 로그 집계. {day: {'rate': n, 'fail': n, 'errors': n, 'boots': [(pid, code)]}}"""
    days = defaultdict(lambda: {"rate": 0, "fail": 0, "errors": 0, "boots": []})
    for path in sorted(glob.glob(os.path.join(logs_dir, "mystock*.log"))):
        day = _file_day(path, None) or _current_day(logs_dir, path)
        if not day:
            continue
        d = days[day]
        try:
            f = open(path, encoding="utf-8", errors="replace")
        except OSError:
            continue
        with f:
            for line in f:
                if "API 요청 최종 실패" in line:
                    if "EGW00201" in line or "Rate Limit" in line:
                        d["rate"] += 1
                    else:
                        d["fail"] += 1
                elif "[ERROR]" in line:
                    d["errors"] += 1
                m = _START.search(line)
                if m:
                    d["boots"].append((m.group(1), (m.group(2) or "").strip() or None))
    return days


# ── DB ──────────────────────────────────────────────────────────────────────────────
def read_db(db_path):
    """원장에서 날짜별 값과 계좌 상태를 읽는다. 파일이 없으면 None."""
    if not os.path.exists(db_path):
        return None
    con = sqlite3.connect(Path(db_path).resolve().as_uri() + "?mode=ro", uri=True)
    try:
        tables = {r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        out = {"ledger": {}, "fills": Counter(), "assets": {}, "state": {}, "positions": None,
               "schema": con.execute("PRAGMA user_version").fetchone()[0]}
        if "signal_ledger" in tables:
            for day, n, blocked, passed in con.execute(
                    "SELECT date, COUNT(*), COALESCE(SUM(blocked_market), 0), COALESCE(SUM(passed), 0) "
                    "FROM signal_ledger GROUP BY date"):
                day = str(day)
                key = f"{day[:4]}-{day[4:6]}-{day[6:8]}" if len(day) == 8 else day
                out["ledger"][key] = {"codes": n, "blocked_market": int(blocked), "passed": int(passed)}
        if "paper_fills" in tables:
            for day, n in con.execute("SELECT substr(time, 1, 10), COUNT(*) FROM paper_fills GROUP BY 1"):
                out["fills"][day] += n
        elif "trades" in tables:
            for day, n in con.execute("SELECT substr(time, 1, 10), COUNT(*) FROM trades "
                                      "WHERE order_status LIKE '체결%' GROUP BY 1"):
                out["fills"][day] += n
        if "daily_asset_history" in tables:
            for day, asset, net in con.execute(
                    "SELECT date, SUM(asset), SUM(COALESCE(net_transfer, 0)) FROM daily_asset_history GROUP BY date"):
                out["assets"][day] = (float(asset or 0), float(net or 0))
        if "paper_state" in tables:
            out["state"] = {k: v for k, v in con.execute("SELECT key, value FROM paper_state")}
        if "paper_positions" in tables:
            out["positions"] = con.execute("SELECT COUNT(*) FROM paper_positions WHERE qty > 0").fetchone()[0]
        return out
    finally:
        con.close()


HEARTBEAT_STALE_DAYS = 30     # 가장 새 도장보다 이만큼 오래된 파일은 쓰지 않는 옛 경로로 본다


def read_heartbeats(logs_dir):
    """하트비트 파일들. 가장 새 것보다 한 달 넘게 오래된 것(모드별 경로로 옮기기 전의 옛 파일)은 뺀다."""
    out = []
    for path in sorted(glob.glob(os.path.join(logs_dir, "heartbeat*.json"))):
        if os.path.basename(path).startswith("heartbeat_alert"):
            continue
        try:
            with open(path, encoding="utf-8") as f:
                data = json.load(f)
        except (OSError, ValueError):
            continue
        if isinstance(data, dict) and data.get("state"):
            out.append((os.path.basename(path), data))
    newest = max((float(hb.get("ts") or 0) for _, hb in out), default=0)
    return [(n, hb) for n, hb in out
            if float(hb.get("ts") or 0) >= newest - HEARTBEAT_STALE_DAYS * 86400]


# ── 판정 ────────────────────────────────────────────────────────────────────────────
def build_rows(since, until, auto, app, db):
    rows = []
    day = since
    while day <= until:
        key = day.isoformat()
        a = auto.get(key)
        p = app.get(key)
        led = (db or {}).get("ledger", {}).get(key)
        asset = (db or {}).get("assets", {}).get(key)
        if a or p or led or asset:
            durs = a["durs"] if a else []
            rows.append({
                "day": day, "cycles": a["cycles"] if a else 0,
                "filter_cycles": a["filter_cycles"] if a else 0,
                "filter_codes": a["filter_codes"] if a else 0,
                "ledger": led, "fills": (db or {}).get("fills", {}).get(key, 0) if db else None,
                "asset": asset,
                "dur_med": statistics.median(durs) if durs else None,
                "dur_max": max(durs) if durs else None,
                "rate": p["rate"] if p else None, "fail": p["fail"] if p else None,
                "mem": a["mem"] if a else 0, "starts": a["starts"] if a else 0,
                "boots": p["boots"] if p else [], "risk": a["risk"] if a else None,
                "cap": a["cap"] if a else None, "alerts": a["alerts"] if a else Counter(),
            })
        day += timedelta(days=1)
    return rows


def find_issues(rows, db, heartbeats, interval_sec=60):
    """눈으로 다시 볼 것들. 판정이 아니라 '여기를 보라'는 표시다."""
    issues = []
    active = [r for r in rows if r["cycles"] > 0 and r["day"].weekday() < 5]
    med = statistics.median(r["cycles"] for r in active) if active else 0
    prev_asset = None
    for r in rows:
        tag = r["day"].strftime("%m-%d")
        if r in active and med and r["cycles"] < med * 0.9 and r["starts"] == 0:
            issues.append(f"{tag} 주기 {r['cycles']}회 — 다른 거래일(중앙 {med:.0f}회)보다 10% 넘게 적은데 재기동 기록이 없습니다")
        led = r["ledger"]
        if r["filter_codes"] and led is not None and led["blocked_market"] != r["filter_codes"]:
            issues.append(f"{tag} 시장 필터 원장 불일치 — 로그 {r['filter_codes']:,}건 · 신호 원장 {led['blocked_market']:,}건")
        if r["filter_codes"] and led is None and db is not None:
            issues.append(f"{tag} 시장 필터가 {r['filter_cycles']}주기를 막았는데 신호 원장에 그날 행이 없습니다")
        if r["dur_max"] is not None and r["dur_max"] > interval_sec:
            issues.append(f"{tag} 주기 최대 {r['dur_max']:.1f}초 — 대기 간격({interval_sec}초)보다 길어 청산 감시가 그만큼 늦었습니다")
        if r["mem"]:
            issues.append(f"{tag} 메모리 경고 {r['mem']}건")
        if r["fail"]:
            issues.append(f"{tag} 초당 한도가 아닌 API 최종 실패 {r['fail']}건")
        if r["asset"] is not None:
            asset, net = r["asset"]
            if net:
                issues.append(f"{tag} 순입출금 {net:+,.0f}원이 기록됐습니다")
            if prev_asset and asset and r["fills"] == 0 and not net and abs(asset - prev_asset[0]) > 1 \
                    and prev_asset[1] == 0:
                issues.append(f"{tag} 시작 자산이 {prev_asset[0]:,.0f} → {asset:,.0f}원으로 바뀌었는데 "
                              f"전날 체결이 없습니다")
            prev_asset = (asset, r["fills"] or 0)
        if r["starts"] or r["boots"]:
            boots = ", ".join(f"PID {pid}" + (f" 코드 {code}" if code else "") for pid, code in r["boots"])
            issues.append(f"{tag} 기동 — 자동매매 {r['starts']}회" + (f" · 프로세스 {boots}" if boots else ""))
    for name, hb in heartbeats:
        if hb.get("state") != "alive":
            issues.append(f"하트비트 {name}: 상태 '{hb.get('state')}' ({hb.get('reason') or '사유 없음'}, {hb.get('iso')})")
    return issues


# ── 출력 ────────────────────────────────────────────────────────────────────────────
COLS = (("날짜", 9, False), ("주기", 5, True), ("필터보류", 8, True), ("원장 일치", 14, False),
        ("통과", 5, True), ("체결", 4, True), ("시작 자산", 12, True), ("소요 중앙/최대", 14, True),
        ("한도실패", 8, True), ("메모리", 6, True), ("기동", 4, True), ("손절 캡", 14, False))


def _row_cells(r):
    led = r["ledger"]
    if not r["filter_codes"] and not (led and led["blocked_market"]):
        match = "-"
    elif led is None:
        match = "원장 없음"
    elif led["blocked_market"] == r["filter_codes"]:
        match = f"일치 {led['blocked_market']:,}"
    else:
        match = f"불일치 {led['blocked_market']:,}"
    dur = "-" if r["dur_med"] is None else f"{r['dur_med']:.1f} / {r['dur_max']:.1f}"
    return (f"{r['day'].strftime('%m-%d')}({WEEKDAYS[r['day'].weekday()]})",
            f"{r['cycles']:,}" if r["cycles"] else "-",
            f"{r['filter_cycles']:,}" if r["filter_cycles"] else "-",
            match,
            "-" if led is None else f"{led['passed']:,}",
            "-" if r["fills"] is None else str(r["fills"]),
            "-" if r["asset"] is None else f"{r['asset'][0]:,.0f}",
            dur,
            "-" if r["rate"] is None else f"{r['rate']:,}",
            str(r["mem"]) if r["mem"] else "-",
            str(max(r["starts"], len(r["boots"]))) if (r["starts"] or r["boots"]) else "-",
            r["cap"] or "-")


def render(rows, db, heartbeats, issues, since, until, db_path, logs_dir, out=sys.stdout):
    p = lambda s="": print(s, file=out)      # noqa: E731
    p(f"[운용 사본 주간 점검] {since} ~ {until}")
    p(f"  DB   {db_path}" + ("" if db is not None else "  (없음 — 로그만 봅니다)"))
    p(f"  로그 {logs_dir}")
    if db is not None:
        st = db["state"]
        bits = []
        if st.get("started_at"):
            bits.append(f"원장 시작 {str(st['started_at']).strip(chr(34))}")
        if st.get("seed"):
            bits.append(f"시드 {float(st['seed']):,.0f}")
        if st.get("cash"):
            bits.append(f"현금 {float(st['cash']):,.0f}")
        if db["positions"] is not None:
            bits.append(f"보유 {db['positions']}종목")
        bits.append(f"스키마 번호 {db['schema']}")
        p("  " + " · ".join(bits))
    for name, hb in heartbeats:
        p(f"  하트비트 {name}: {hb.get('state')} {hb.get('iso')} · pid {hb.get('pid')} · {hb.get('host')} · "
          f"코드 {hb.get('commit') or '기록 없음'} · 자동매매 {'실행' if hb.get('auto_running') else '정지'}")
    p()
    if not rows:
        p("이 기간의 기록이 없습니다.")
        return
    p(" ".join(_pad(h, w, right) for h, w, right in COLS))
    p(" ".join("-" * w for _, w, _ in COLS))
    for r in rows:
        p(" ".join(_pad(c, w, right) for c, (_, w, right) in zip(_row_cells(r), COLS)))
    risks = [(r["day"], r["risk"]) for r in rows if r["risk"]]
    if risks:
        p()
        p("리스크 배수(그날 마지막으로 적힌 값):")
        for d, text in risks:
            p(f"  {d.strftime('%m-%d')} {text}")
    alerts = Counter()
    for r in rows:
        alerts.update(r["alerts"])
    if alerts:
        p()
        p("경고 표식이 든 로그 줄(숫자는 N 으로 묶음, 많은 순 10종):")
        for text, n in alerts.most_common(10):
            p(f"  {n:>5}회  {text}")
    p()
    if issues:
        p(f"다시 볼 것 {len(issues)}건:")
        for i in issues:
            p(f"  · {i}")
    else:
        p("다시 볼 것 없음 — 주기·원장·자산·자원에서 눈에 띄는 것이 없습니다.")


def run(db_path=DEFAULT_DB, logs_dir=DEFAULT_LOGS, since=None, days=7, today=None, out=sys.stdout):
    logs_dir = str(logs_dir)
    auto = read_autotrade(logs_dir)
    today = today or date.today()
    app = read_app_log(logs_dir)
    db = read_db(str(db_path))
    #  사본은 며칠 전 것일 수 있다 — 끝은 '기록이 있는 마지막 날'이다.
    seen = sorted(set(auto) | set(k for k, v in app.items() if v["rate"] or v["fail"] or v["boots"])
                  | set((db or {}).get("ledger", {})) | set((db or {}).get("assets", {})))
    until = date.fromisoformat(seen[-1]) if seen else today
    if since is None:
        since = until - timedelta(days=max(1, int(days)) - 1)
    rows = build_rows(since, until, auto, app, db)
    heartbeats = read_heartbeats(logs_dir)
    issues = find_issues(rows, db, heartbeats)
    render(rows, db, heartbeats, issues, since, until, db_path, logs_dir, out=out)
    return rows, issues


def main(argv=None):
    ap = argparse.ArgumentParser(description="운용 사본(DB·로그) 주간 점검 — 읽기 전용")
    ap.add_argument("--db", default=str(DEFAULT_DB), help="원장 DB 사본 경로")
    ap.add_argument("--logs", default=str(DEFAULT_LOGS), help="로그 폴더 사본 경로")
    ap.add_argument("--days", type=int, default=7, help="기록이 있는 마지막 날부터 거슬러 볼 일수(기본 7)")
    ap.add_argument("--since", help="시작일 YYYY-MM-DD (주면 --days 대신 쓴다)")
    a = ap.parse_args(argv)
    since = datetime.strptime(a.since, "%Y-%m-%d").date() if a.since else None
    run(a.db, a.logs, since=since, days=a.days)
    return 0


if __name__ == "__main__":
    sys.exit(main())
