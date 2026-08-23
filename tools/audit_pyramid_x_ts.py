"""피라미딩 × TS 무장 시점 — 발동선을 낮춘 뒤에도 증액 다이얼이 그대로인가.

[왜] 2026-08-11에 TS 발동 배수를 3.5 → 3.0으로 분리·인하해 무장률이 28 → 34.6%가 됐다.
 피라미딩은 +10%에서 증액하는데, 증액하면 평균단가가 올라가고 그 위로 TS가 더 일찍
 걸린다. 즉 '증액분이 무장 직후에 잘리는' 구간이 넓어졌을 수 있다. 두 축의 상호작용은
 잰 적이 없다 — 피라미딩 다이얼은 발동선이 3.5(콜백과 결합)이던 시절에 정해졌다.

[무엇을 보는가] 총수익만 보면 안 된다. 증액이 '되는' 조건은 증액분이 원본 포지션만큼
 오래 살아남는 것이므로, 아래 두 지표가 실체다.
   · 피라미딩 건수 / 청산 건수 — 증액이 실제로 얼마나 일어나는가
   · 무장률·보유일 — 증액이 청산을 앞당기는가
 여기에 기존 청산 다이얼과 같은 잣대(상위10%·최대·>30%·TS이익%)를 함께 본다.

[실행] python3 tools/audit_pyramid_x_ts.py --days 3650 --trials 15 --sample 25 --subperiods 4
"""
import argparse
import os
import random
import sys

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tools.audit_common import exits  # noqa: E402

import config  # noqa: E402
from modules import portfolio_backtest as pb  # noqa: E402

INITIAL_CAPITAL = 10_000_000
TRIG = "PYRAMIDING_PROFIT_TRIGGER"
MAXC = "PYRAMIDING_MAX_COUNT"
USE = "PYRAMIDING_USE"


def dial_sets():
    """(그룹, 라벨, ANALYSIS_THRESHOLDS 오버라이드)."""
    return [
        ("A. 증액 트리거", "OFF",          {USE: False}),
        ("A. 증액 트리거", "+7%",          {TRIG: 7.0}),
        ("A. 증액 트리거", "+10% (현행)",  {TRIG: 10.0}),
        ("A. 증액 트리거", "+15%",         {TRIG: 15.0}),
        ("A. 증액 트리거", "+20%",         {TRIG: 20.0}),
        ("B. 증액 차수", "1차",            {MAXC: 1}),
        ("B. 증액 차수", "2차",            {MAXC: 2}),
        ("B. 증액 차수", "3차 (현행)",     {MAXC: 3}),
        ("B. 증액 차수", "5차",            {MAXC: 5}),
    ]


def metrics(r):
    sells = exits(r)
    pyr = [t for t in r["trades"] if str(t["reason"]).startswith("피라미딩")]
    profits = sorted((t["profit"] for t in sells), reverse=True)
    top10 = profits[:max(1, len(profits) // 10)]
    gross_gain = sum(t["profit_amt"] for t in sells if t["profit_amt"] > 0)
    ts_gain = sum(t["profit_amt"] for t in sells
                  if t["reason"] == "트레일링스탑" and t["profit_amt"] > 0)
    armed = [t for t in sells if t.get("armed")]
    return {
        "ret": r["total_return"], "mdd": r["mdd"],
        "mar": r["total_return"] / abs(r["mdd"]) if r["mdd"] else float("nan"),
        "pf": r["pf"],
        "pyr_n": len(pyr),
        "pyr_per_exit": len(pyr) / len(sells) * 100 if sells else 0.0,
        "armed_share": len(armed) / len(sells) * 100 if sells else 0.0,
        "top10": float(np.mean(top10)) if top10 else 0.0,
        "best": profits[0] if profits else 0.0,
        "big": sum(1 for p in profits if p >= 30),
        "ts_profit_share": (ts_gain / gross_gain * 100) if gross_gain > 0 else 0.0,
        "days": float(np.median([t["days"] for t in sells])) if sells else 0.0,
        "n": len(sells),
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--trials", type=int, default=15)
    ap.add_argument("--sample", type=int, default=25)
    ap.add_argument("--days", type=int, default=3650)
    ap.add_argument("--slots", type=int, default=None)
    ap.add_argument("--seed-capital", type=int, default=INITIAL_CAPITAL)
    ap.add_argument("--only", default=None)
    ap.add_argument("--seed", type=int, default=20260811)
    ap.add_argument("--subperiods", type=int, default=4)
    ap.add_argument("--exclude-from", default="20260301")
    args = ap.parse_args()

    slots = args.slots or getattr(config, "SYSTEM_MAX_HOLDINGS", 4)
    config.session.load_stock_config()
    targets = [(s["code"], s["name"]) for s in config.session.stock_data.get("stocks_kr", [])]
    print(f"[준비] 관심종목 {len(targets)}개 · {args.days}일 · 슬롯 {slots}")
    print(f"[기준] TS 발동 배수 {config.SELL_STRATEGY.get('TS_ACTIVATION_ATR_MULTIPLIER')} · "
          f"상한 {config.SELL_STRATEGY.get('TS_ACTIVATION_MAX_RATE')} · "
          f"콜백 배수 {config.SELL_STRATEGY.get('TRAILING_ATR_MULTIPLIER')}")

    dfs, mf, dates, failed = pb.prepare_universe(targets, args.days)
    thresholds = {
        "BUY_SCORE": config.ANALYSIS_THRESHOLDS["BUY_SCORE"],
        "BUY_RSI_MAX": config.ANALYSIS_THRESHOLDS["BUY_RSI_MAX"],
        "RISE_SCORE": config.ANALYSIS_THRESHOLDS["RISE_SCORE"],
        "WEIGHTS": config.SCORING_WEIGHTS,
    }
    status = pb.precompute_status(dfs, thresholds)
    print(f"[준비] 사용 {len(dfs)}종목 / 거래일 {len(dates)}일")

    from tools.audit_drawdown_axis import market_scale_by_date, make_scale_fn
    p = getattr(config, "RISK_SCALING_PARAMS", {}) or {}
    mkt = market_scale_by_date(dates, args.days)
    dd = None
    if p.get("USE_DRAWDOWN_RISK_SCALING", True):
        dd = (int(p.get("DD_LOOKBACK_DAYS", 90)), float(p.get("DD_LEVEL_1", 5.0)),
              float(p.get("DD_SCALE_1", 0.9)), float(p.get("DD_LEVEL_2", 10.0)),
              float(p.get("DD_SCALE_2", 0.8)))

    cut = "".join(ch for ch in str(args.exclude_from) if ch.isdigit())
    head = [d for d in dates if not cut or cut == "0" or "".join(filter(str.isdigit, d)) < cut]
    tail = [d for d in dates if cut and cut != "0" and "".join(filter(str.isdigit, d)) >= cut]

    sets = dial_sets()
    if args.only:
        sets = [x for x in sets if x[0].startswith(args.only)]
    codes = list(dfs.keys())
    saved = dict(config.ANALYSIS_THRESHOLDS)

    k = max(1, args.subperiods)
    size = max(1, len(head) // k)
    windows = [("제외 전 전체", head)]
    if k > 1:
        windows += [(f"구간{i + 1}", head[i * size:(i + 1) * size if i < k - 1 else len(head)])
                    for i in range(k)]
    if tail:
        windows.append(("[대조] 제외구간(고변동)", tail))

    all_results = {}
    for wname, wdates in windows:
        results = {(g, l): [] for g, l, _o in sets}
        rng = random.Random(args.seed)
        try:
            for t in range(args.trials):
                pick = rng.sample(codes, min(args.sample, len(codes)))
                sd = {c: dfs[c] for c in pick}
                st = {c: status[c] for c in pick}
                sm = {c: mf.get(c, set()) for c in pick}
                for g, label, overrides in sets:
                    config.ANALYSIS_THRESHOLDS.clear()
                    config.ANALYSIS_THRESHOLDS.update(saved)
                    config.ANALYSIS_THRESHOLDS.update(overrides)
                    r = pb.run_portfolio(sd, st, wdates, initial_capital=args.seed_capital,
                                         slots=slots, market_filter_dates=sm,
                                         risk_scale_by_date=make_scale_fn(mkt, dd))
                    results[(g, label)].append(metrics(r))
                print(f"  {wname} 시행 {t + 1}/{args.trials}", end="\r", flush=True)
        finally:
            config.ANALYSIS_THRESHOLDS.clear()
            config.ANALYSIS_THRESHOLDS.update(saved)
        all_results[wname] = results
    print(" " * 50, end="\r")

    W = 112
    print(f"\n{'=' * W}")
    print(f"피라미딩 × TS 무장 — {args.trials}회 × {args.sample}종목 짝비교")
    print(f"{'=' * W}")
    for wname, wdates in windows:
        results = all_results[wname]
        print(f"\n########## {wname} ({len(wdates)} 거래일) ##########")
        last_group = None
        for g, label, _o in sets:
            if g != last_group:
                base_label = next(l for gg, l, _x in sets if gg == g and "현행" in l)
                base = results[(g, base_label)]
                print(f"\n{g}  (기준선: {base_label})")
                print(f"{'설정':<14}{'수익%':>9}{'MDD%':>8}{'MAR':>7}{'PF':>6}{'증액수':>7}"
                      f"{'증액/청산%':>11}{'무장률%':>9}{'상위10%':>9}{'최대':>9}{'>30%':>6}"
                      f"{'보유일':>7}{'승-무-패':>10}{'MAR승':>7}")
                print("-" * W)
                last_group = g
            rs = results[(g, label)]
            m = lambda k: float(np.median([x[k] for x in rs]))  # noqa: E731
            is_base = "현행" in label
            tie = sum(1 for a, b in zip(rs, base) if abs(a["ret"] - b["ret"]) < 1e-9)
            los = sum(1 for a, b in zip(rs, base) if a["ret"] < b["ret"] - 1e-9)
            rw = sum(1 for a, b in zip(rs, base) if a["ret"] > b["ret"])
            mw = sum(1 for a, b in zip(rs, base) if a["mar"] > b["mar"])
            print(f"{label:<14}{m('ret'):>9.1f}{m('mdd'):>8.1f}{m('mar'):>7.2f}{m('pf'):>6.2f}"
                  f"{m('pyr_n'):>7.0f}{m('pyr_per_exit'):>11.1f}{m('armed_share'):>9.1f}"
                  f"{m('top10'):>9.1f}{m('best'):>9.1f}{m('big'):>6.0f}{m('days'):>7.1f}"
                  f"{'—' if is_base else f'{rw}-{tie}-{los}':>10}"
                  f"{'—' if is_base else f'{mw}/{len(rs)}':>7}")

    print("\n" + "-" * W)
    print("증액/청산% = 청산 1건당 증액 발생 비율. 증액이 실제로 일어나는지의 지표.")
    print("[읽는 법] 증액이 늘어도 무장률이 오르고 보유일이 짧아지면, 증액분이 조기에 잘리는 것이다.")


if __name__ == "__main__":
    main()
