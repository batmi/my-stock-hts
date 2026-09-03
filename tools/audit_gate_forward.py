"""체결강도·호가잔량비 게이트가 이익인가 손해인가 — 막힌 신호의 전방 수익으로 답한다.

[왜 이 도구인가] 이 게이트들은 실시간 호가·체결 데이터를 보므로 **백테스트로는 영원히
 못 잰다**([[live-only-axes-audited]]). 그래서 지금까지 '유지'의 근거는 '좋다고 증명돼서'가
 아니라 '잴 수 없어서'였다. 신호 원장(signal_ledger)이 쌓이면 답할 수 있다 — 게이트가
 막은 신호가 이후 어떻게 됐는지를 통과한 신호와 나란히 놓으면 된다.

[게이트의 주장] "체결강도 100 미만 = 가짜 상승 에너지". 참이라면 **막힌 쪽의 전방 수익이
 통과한 쪽보다 나빠야** 한다. 그렇지 않다면 이 게이트는 강추세 진입을 막기만 하는 셈이다.

[세는 법 — 중요] '차단 주기 비중'으로 세면 안 된다. 같은 (일,종목)이 그날 다른 주기에
 통과하는 일이 흔해서, 그렇게 세면 1.3% → 75% 로 부풀어 정반대 결론이 난다(실제로 한 번
 틀렸다). 이 도구는 원장의 `passed == 0`, 즉 **그날 끝내 한 번도 못 산 (일,종목)** 만
 '막혔다'로 센다.

[한계] 표본이 작다. 원장이 쌓일수록 답이 선명해지며, 그 전에는 방향만 읽을 것.
 그리고 '막힌 신호를 샀다면'은 반사실이다 — 슬롯·현금 제약을 무시한 상한이다.

[실행] python3 tools/audit_gate_forward.py [--db db/paper_trading_mode1.db] [--days 3,5,10,20]
"""
import argparse
import os
import sqlite3
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import numpy as np  # noqa: E402

GATES = [("blocked_vol", "체결강도"), ("blocked_abr", "호가잔량비"),
         ("blocked_corr", "상관"), ("blocked_slot", "슬롯만석"),
         ("blocked_hold", "보유중"), ("blocked_rs", "RS"), ("blocked_tq", "추세품질"),
         ("blocked_reentry", "재진입"), ("blocked_cash", "현금"), ("blocked_other", "기타")]
# 시장 구조상의 제약이지 '신호 판정'이 아닌 것 — 게이트 귀책에서 제외한다.
NOT_A_JUDGEMENT = {"blocked_slot", "blocked_cash", "blocked_hold"}


def load_rows(db_path):
    con = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    try:
        return [dict(r) for r in con.execute("SELECT * FROM signal_ledger")]
    finally:
        con.close()


def forward_returns(code, date, horizons):
    """그 일자 종가 대비 N거래일 뒤 종가 수익률(%). 봉이 모자라면 그 항목은 None."""
    from modules import krx_daily
    df = krx_daily.get_daily(code, lookback_days=400)
    if df is None or df.empty:
        return {n: None for n in horizons}
    d = df["date"].astype(str).tolist()
    closes = df["close"].astype(float).tolist()
    if date not in d:
        return {n: None for n in horizons}
    i = d.index(date)
    base = closes[i]
    out = {}
    for n in horizons:
        out[n] = ((closes[i + n] / base - 1) * 100) if (i + n) < len(closes) and base > 0 else None
    return out


def summarize(label, vals):
    v = [x for x in vals if x is not None]
    if not v:
        return f"  {label:<22} 표본 없음"
    a = np.array(v)
    return (f"  {label:<22} n={len(a):>3}  평균 {a.mean():>7.2f}%  중앙 {np.median(a):>7.2f}%  "
            f"승률 {(a > 0).mean() * 100:>5.1f}%  최대 {a.max():>7.1f}%")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default="db/paper_trading_mode1.db")
    ap.add_argument("--days", default="3,5,10,20")
    ap.add_argument("--min-score", type=float, default=0.0,
                    help="이 점수 이상인 신호만 (기본 0 = 전부)")
    args = ap.parse_args()
    horizons = [int(x) for x in args.days.split(",")]

    rows = load_rows(args.db)
    if args.min_score > 0:
        rows = [r for r in rows if (r["max_score"] or 0) >= args.min_score]
    blocked, passed = [], []
    for r in rows:
        (blocked if (r["passed"] or 0) == 0 else passed).append(r)

    print(f"[원장] {args.db} · (일,종목) {len(rows)}건"
          + (f" (점수 {args.min_score}+ 만)" if args.min_score else ""))
    print(f"  완전 차단 {len(blocked)}건 · 한 번이라도 통과 {len(passed)}건")

    # 완전 차단의 '귀책 게이트' — 판정 게이트 중 가장 많이 막은 것
    def owner(r):
        cand = [(r[g] or 0, ko) for g, ko in GATES if g not in NOT_A_JUDGEMENT]
        best = max(cand, key=lambda t: t[0])
        return best[1] if best[0] > 0 else "구조제약(슬롯/현금)"

    fr = {}
    for r in rows:
        fr[(r["date"], r["code"])] = forward_returns(r["code"], str(r["date"]), horizons)

    for n in horizons:
        print(f"\n{'=' * 88}\n[전방 {n}거래일 수익률]\n{'=' * 88}")
        print(summarize("통과(실제 매수 가능)", [fr[(r['date'], r['code'])][n] for r in passed]))
        print(summarize("완전 차단(못 삼)", [fr[(r['date'], r['code'])][n] for r in blocked]))
        for _g, ko in GATES:
            if _g in NOT_A_JUDGEMENT:
                continue
            sub = [fr[(r['date'], r['code'])][n] for r in blocked if owner(r) == ko]
            if sub:
                print(summarize(f"  └ {ko} 귀책", sub))

    print(f"\n{'=' * 88}\n[읽는 법]\n{'=' * 88}")
    print("  · 게이트의 주장은 '막은 것이 가짜 상승'이다 → 막힌 쪽의 전방 수익이 통과한")
    print("    쪽보다 **나빠야** 주장이 성립한다.")
    print("  · 막힌 쪽이 더 좋으면 게이트는 강추세 진입을 막기만 한 것이다.")
    print("  · 표본이 작으면 방향만 읽을 것 — 원장이 쌓일수록 선명해진다.")
    print("  · '막힌 신호를 샀다면'은 슬롯·현금을 무시한 반사실 상한이다.")

    print(f"\n{'=' * 88}\n[완전 차단 상세]\n{'=' * 88}")
    print(f"{'일자':<10}{'종목':<12}{'점수':>5}{'귀책':>10}" +
          "".join(f"{'+' + str(n) + '일':>9}" for n in horizons))
    for r in sorted(blocked, key=lambda x: -(x["max_score"] or 0)):
        f = fr[(r["date"], r["code"])]
        cells = "".join((f"{f[n]:>9.2f}" if f[n] is not None else f"{'-':>9}") for n in horizons)
        print(f"{r['date']:<10}{str(r['name'])[:10]:<12}{r['max_score']:>5.1f}{owner(r):>10}{cells}")


if __name__ == "__main__":
    main()
