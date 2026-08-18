"""실매매에만 있는 진입 게이트(체결강도·매도잔량비)가 실제로 무엇을 자르는가.

[왜] `BUY_VOL_STRENGTH`(100.0)와 `BUY_ASK_BID_RATIO`(1.0)는 engine.build_buy_thresholds를
 지나 매수 직전에 걸리는데, portfolio_backtest에는 두 키가 아예 없다. 즉 지금까지 정한
 모든 다이얼은 이 게이트가 없는 세계에서 최적화됐다. tools/audit_entry_gate_parity.py 는
 "실시간 체결 데이터라 일봉으로 재현할 수 없다"며 이 축을 비워 두고 넘어갔다.

[다르게 접근한다] 백테스트로 못 만들면 **운영 기록으로 센다.** 자동매매는 주기마다
 종목별 [분석] 줄을 남기고, 게이트에 걸리면 그 줄에 사유가 붙는다:
   ... 점수=7.5, 상태=매수, ... 체결=138.2% [매도비:3.92<1.0]
 여기서 두 가지를 얻는다. ① 매수 문턱을 넘은 신호 중 몇 %가 게이트에서 죽는가
 ② 죽은 신호가 그 뒤 올랐는가 내렸는가(일봉으로 사후 계산).

[함정] `[매도비:3.92]`처럼 `<` 없이 붙는 것은 **차단이 아니라 정보 표기**다. 이걸 차단으로
 세면 차단율이 1.3% → 75%로 부풀어 정반대 결론이 난다(2026-08-16 실제로 한 번 틀렸다).
 차단 판정은 반드시 부등호까지 확인한다.

[한계] 로그는 그 기계가 돌던 기간·모드의 기록이다. 토스 모드는 체결강도를 제공하지 않아
 매도잔량비가 유일한 게이트이므로, 모드가 섞이면 두 게이트의 비중이 왜곡된다.

[대체됨 · 2026-08-19] 이 도구가 씨름한 문제(부등호 오독·짧은 로그 창)는 판정 지점이
 결과를 DB에 직접 남기면서 사라졌다. **새 작업은 `tools/audit_signal_ledger.py` 를 쓸 것.**
 이 도구는 원장이 없던 기간(2026-08-19 이전)의 로그를 읽기 위해 남긴다.

[실행] python3 tools/audit_live_gate_impact.py --logs 'logs/*.log' --forward 20
"""
import argparse
import glob
import io
import os
import re
import sys
from collections import defaultdict

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import config  # noqa: E402

LINE = re.compile(
    r"\[(?P<ts>\d{4}-\d{2}-\d{2}) [^\]]*\] \[분석\] (?P<name>[^(]+)\((?P<code>[A-Z0-9]{6})\).*?"
    r"점수=(?P<score>[0-9.]+), 상태=(?P<state>[^,]+),.*?RSI=(?P<rsi>[0-9.]+)")
VOL = re.compile(r"체결=(?P<v>[0-9.]+)%")
REJ_VOL = re.compile(r"체결:([0-9.]+)%<([0-9.]+)%")      # 부등호가 있어야 차단이다
REJ_AB = re.compile(r"매도비:([0-9.]+)<([0-9.]+)")
REJ_HOLD = re.compile(r"체결강도 미확인")


def parse(patterns):
    rows = []
    for pat in patterns:
        for path in sorted(glob.glob(pat)):
            for line in io.open(path, encoding="utf-8", errors="ignore"):
                m = LINE.search(line)
                if not m:
                    continue
                v = VOL.search(line)
                rows.append({
                    "date": m.group("ts").replace("-", ""),
                    "code": m.group("code"), "name": m.group("name").strip(),
                    "score": float(m.group("score")), "state": m.group("state"),
                    "rsi": float(m.group("rsi")),
                    "vol": float(v.group("v")) if v else None,
                    "rej_vol": bool(REJ_VOL.search(line)),
                    "rej_ab": bool(REJ_AB.search(line)),
                    "rej_hold": bool(REJ_HOLD.search(line)),
                })
    return rows


def dedup(rows):
    """같은 날 같은 종목은 주기마다 반복 기록된다. 신호 1건으로 접되,
    한 번이라도 차단됐으면 차단으로 본다(그 주기에 못 산 것은 사실이다)."""
    out = {}
    for r in rows:
        k = (r["date"], r["code"])
        cur = out.get(k)
        if cur is None:
            out[k] = dict(r, hits=1)
            continue
        cur["hits"] += 1
        cur["score"] = max(cur["score"], r["score"])
        for f in ("rej_vol", "rej_ab", "rej_hold"):
            cur[f] = cur[f] or r[f]
    return list(out.values())


def forward_returns(sigs, days, horizon):
    """차단된 신호가 그 뒤 올랐는지 내렸는지 — 일봉으로 사후 계산한다."""
    from modules import backtest
    by_code = defaultdict(list)
    for s in sigs:
        by_code[s["code"]].append(s)
    out = []
    for code, group in by_code.items():
        try:
            df = backtest.get_backtest_data(code, False, days)
        except Exception:
            df = None
        if df is None or df.empty:
            continue
        dates = [str(d) for d in df["date"]]
        idx = {d: i for i, d in enumerate(dates)}
        close = df["close"].astype(float).to_numpy()
        for s in group:
            i = idx.get(s["date"])
            if i is None or i + horizon >= len(close):
                continue
            out.append(dict(s, fwd=(close[i + horizon] - close[i]) / close[i] * 100))
    return out


def summarize(tag, arr):
    if not arr:
        return f"{tag:<16}{'(표본 없음)':>10}"
    a = np.array(arr)
    return (f"{tag:<16}{len(a):>7}{a.mean():>10.2f}{np.median(a):>10.2f}"
            f"{np.percentile(a, 90):>9.1f}{(a > 0).mean() * 100:>9.1f}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--logs", nargs="*", default=["logs/*.log"])
    ap.add_argument("--forward", type=int, default=20)
    ap.add_argument("--days", type=int, default=1200)
    args = ap.parse_args()

    rows = parse(args.logs)
    if not rows:
        print("[중단] 로그에서 [분석] 줄을 찾지 못했다.")
        return
    sigs = dedup(rows)
    buy_score = config.ANALYSIS_THRESHOLDS["BUY_SCORE"]
    days_seen = sorted({s["date"] for s in sigs})
    print(f"[준비] 로그 {len(rows):,}줄 → 신호 {len(sigs):,}건 "
          f"({days_seen[0]}~{days_seen[-1]}, {len(days_seen)}일) · 매수 문턱 {buy_score}점")

    # ── 점수대별 차단율 ────────────────────────────────────────────
    print(f"\n[1] 점수대별 게이트 차단율 (같은 날·같은 종목은 1건으로 접음)")
    print(f"{'점수대':<12}{'신호':>7}{'체결강도차단':>13}{'매도잔량비차단':>15}"
          f"{'보류(미확인)':>13}{'체결강도중앙':>13}")
    bands = [(0, 4), (4, 6), (6, buy_score), (buy_score, 8), (8, 99)]
    for lo, hi in bands:
        seg = [s for s in sigs if lo <= s["score"] < hi]
        if not seg:
            continue
        v = sum(1 for s in seg if s["rej_vol"])
        a = sum(1 for s in seg if s["rej_ab"])
        h = sum(1 for s in seg if s["rej_hold"])
        vols = [s["vol"] for s in seg if s["vol"] is not None]
        print(f"{f'{lo:.1f}~{hi:.1f}':<12}{len(seg):>7}"
              f"{f'{v} ({v / len(seg) * 100:.1f}%)':>13}"
              f"{f'{a} ({a / len(seg) * 100:.1f}%)':>15}"
              f"{f'{h} ({h / len(seg) * 100:.1f}%)':>13}"
              f"{np.median(vols) if vols else float('nan'):>13.1f}")

    # ── 매수 문턱을 넘은 신호만 따로 ───────────────────────────────
    hi_sigs = [s for s in sigs if s["score"] >= buy_score]
    blocked = [s for s in hi_sigs if s["rej_vol"] or s["rej_ab"] or s["rej_hold"]]
    passed = [s for s in hi_sigs if s not in blocked]
    print(f"\n[2] 매수 문턱({buy_score}점) 이상 {len(hi_sigs)}건 중 게이트 차단 {len(blocked)}건 "
          f"({len(blocked) / max(1, len(hi_sigs)) * 100:.1f}%)")
    if blocked:
        for s in sorted(blocked, key=lambda x: x["date"])[:20]:
            why = ("체결강도" if s["rej_vol"] else "매도잔량비" if s["rej_ab"] else "보류")
            print(f"   {s['date']}  {s['name'][:12]:<13}{s['score']:>5.1f}점  {why}")

    # ── 차단된 신호의 이후 수익 ────────────────────────────────────
    if blocked:
        print(f"\n[3] 게이트가 자른 신호의 이후 {args.forward}일 수익 "
              f"— 손실을 막았나, 수익을 막았나")
        fb = forward_returns(blocked, args.days, args.forward)
        fp = forward_returns(passed, args.days, args.forward)
        print(f"{'구분':<16}{'건수':>7}{'평균':>10}{'중앙':>10}{'P90':>9}{'승률%':>9}")
        print(summarize("게이트 차단", [x["fwd"] for x in fb]))
        print(summarize("게이트 통과", [x["fwd"] for x in fp]))
        print("[읽는 법] 차단된 쪽이 더 낮으면 게이트가 일한 것이다. 더 높으면 "
              "백테스트가 세운 다이얼을 실매매가 스스로 깎고 있다는 뜻이다.")
    else:
        print("\n[3] 차단된 신호가 없어 사후 수익 비교는 생략한다. "
              "게이트는 이 표본에서 아무것도 자르지 않았다.")


if __name__ == "__main__":
    main()
