"""장중 체결 세계에서 기존 다이얼을 다시 잰다 — 현행값이 여전히 최선인가.

[왜] 시스템의 다이얼은 전부 **종가 체결 백테스트**에서 정해졌다. 하루에 진입·청산·증액이
 각각 한 번뿐인 세계다. 2026-08-16 백테스트를 실매매와 같은 장중 스캔으로 맞췄으니,
 그 다이얼들이 정해진 세계 자체가 바뀌었다. 최적값이 옮겨갔는지는 재보지 않으면 모른다.

 [무엇이 바뀌었나] 장중 세계에서는 (a) 손절·TS가 봉 안에서 걸려 더 일찍 나가고,
 (b) 증액이 같은 날 여러 번 일어나며, (c) 진입도 장중에 슬롯을 먹는다. 즉 손절 폭·
 무장 시점·증액 다이얼은 종가 세계보다 **더 자주 구속**된다. 같은 값이 같은 뜻이 아니다.

[제외한 축]
 · BUY_SCORE / BUY_RSI_MAX — 분봉 상태 캐시(intraday_status)에 임계값이 구워져 있어
   스윕하려면 캐시를 통째로 다시 만들어야 한다(변형당 25분). 이미 별도로 감사됐다.
 · TRAILING_ATR_MULTIPLIER — audit_ts_callback_intraday.py 가 두 세계를 함께 잰다.

[읽는 법] 현행이 최선이면 승-무-패에서 대안이 전부 지는 것으로 나온다. 대안이 이기면
 그 값이 '장중 세계에서만' 이기는지 종가 세계 기존 결론과 대조해야 한다(config 주석).

[선행] tools/fetch_intraday_tv.py → tools/build_intraday_status.py
[실행] python3 tools/audit_dials_intraday.py --trials 15 --sample 20 --seeds 3
"""
import argparse
import os
import random
import sys

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tools.audit_common import exits  # noqa: E402

import config  # noqa: E402
from modules import intraday_bars as ib  # noqa: E402
from modules import portfolio_backtest as pb  # noqa: E402

INITIAL_CAPITAL = 10_000_000
BASE = "현행"

# (그룹, 라벨, [(대상, 키, 값)]) — 대상: sell=SELL_STRATEGY, thr=ANALYSIS_THRESHOLDS, attr=config
GROUPS = [
    ("A. ATR 손절 배수 (현행 2.0)", [
        ("1.5", [("sell", "ATR_STOP_MULTIPLIER", 1.5)]),
        ("2.5", [("sell", "ATR_STOP_MULTIPLIER", 2.5)]),
        ("3.0", [("sell", "ATR_STOP_MULTIPLIER", 3.0)]),
    ]),
    ("B. TS 발동 배수 (현행 3.0)", [
        ("2.0", [("sell", "TS_ACTIVATION_ATR_MULTIPLIER", 2.0)]),
        ("2.5", [("sell", "TS_ACTIVATION_ATR_MULTIPLIER", 2.5)]),
        ("3.5", [("sell", "TS_ACTIVATION_ATR_MULTIPLIER", 3.5)]),
    ]),
    ("C. 증액 트리거 (현행 +10%)", [
        ("+7%", [("thr", "PYRAMIDING_PROFIT_TRIGGER", 7.0)]),
        ("+15%", [("thr", "PYRAMIDING_PROFIT_TRIGGER", 15.0)]),
        ("+20%", [("thr", "PYRAMIDING_PROFIT_TRIGGER", 20.0)]),
    ]),
    ("D. 증액 차수 (현행 3차)", [
        ("OFF", [("thr", "PYRAMIDING_USE", False)]),
        ("1차", [("thr", "PYRAMIDING_MAX_COUNT", 1)]),
        ("5차", [("thr", "PYRAMIDING_MAX_COUNT", 5)]),
    ]),
    ("E. 증액 비율 (현행 0.5)", [
        ("0.3", [("thr", "PYRAMIDING_RATIO", 0.3)]),
        ("0.75", [("thr", "PYRAMIDING_RATIO", 0.75)]),
        ("1.0", [("thr", "PYRAMIDING_RATIO", 1.0)]),
    ]),
    ("F. 시간청산 일수 (현행 15)", [
        ("10일", [("sell", "TIME_STOP_DAYS", 10)]),
        ("20일", [("sell", "TIME_STOP_DAYS", 20)]),
        ("25일", [("sell", "TIME_STOP_DAYS", 25)]),
    ]),
    ("G. 손절 캡 (현행 -15%)", [
        ("-10%", [("sell", "MAX_ATR_STOP_LOSS_RATE", -10.0)]),
        ("-20%", [("sell", "MAX_ATR_STOP_LOSS_RATE", -20.0)]),
        ("해제", [("sell", "MAX_ATR_STOP_LOSS_RATE", 0.0)]),
    ]),
    ("H. 거래당 리스크 (현행 4%)", [
        ("2%", [("attr", "SYSTEM_RISK_PER_TRADE", 2.0)]),
        ("3%", [("attr", "SYSTEM_RISK_PER_TRADE", 3.0)]),
        ("5%", [("attr", "SYSTEM_RISK_PER_TRADE", 5.0)]),
    ]),
]


def apply(overrides):
    """되돌릴 수 있도록 이전 값을 함께 돌려준다."""
    prev = []
    for tgt, key, val in overrides:
        if tgt == "sell":
            prev.append((tgt, key, config.SELL_STRATEGY.get(key)))
            config.SELL_STRATEGY[key] = val
        elif tgt == "thr":
            prev.append((tgt, key, config.ANALYSIS_THRESHOLDS.get(key)))
            config.ANALYSIS_THRESHOLDS[key] = val
        else:
            prev.append((tgt, key, getattr(config, key, None)))
            setattr(config, key, val)
    return prev


def metrics(r):
    sells = exits(r)
    pyr = [t for t in r["trades"] if str(t["reason"]).startswith("피라미딩")]
    profits = sorted((t["profit"] for t in sells), reverse=True)
    top10 = profits[:max(1, len(profits) // 10)]
    gross = sum(t["profit_amt"] for t in sells if t["profit_amt"] > 0)
    ts_gain = sum(t["profit_amt"] for t in sells
                  if t["reason"] == "트레일링스탑" and t["profit_amt"] > 0)
    armed = [t for t in sells if t.get("armed")]
    return {
        "ret": r["total_return"], "mdd": r["mdd"],
        "mar": r["total_return"] / abs(r["mdd"]) if r["mdd"] else float("nan"),
        "pf": r["pf"], "n": len(sells), "pyr_n": len(pyr),
        "armed": len(armed) / len(sells) * 100 if sells else 0.0,
        "top10": float(np.mean(top10)) if top10 else 0.0,
        "best": profits[0] if profits else 0.0,
        "big": sum(1 for p in profits if p >= 30),
        "ts_share": (ts_gain / gross * 100) if gross > 0 else 0.0,
        "days": float(np.median([t["days"] for t in sells])) if sells else 0.0,
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--trials", type=int, default=15)
    ap.add_argument("--sample", type=int, default=20)
    ap.add_argument("--days", type=int, default=1200)
    ap.add_argument("--seeds", type=int, default=3)
    ap.add_argument("--seed", type=int, default=20260816)
    ap.add_argument("--interval", default="60m")
    ap.add_argument("--slots", type=int, default=None)
    ap.add_argument("--seed-capital", type=int, default=INITIAL_CAPITAL)
    ap.add_argument("--min-coverage", type=float, default=0.9)
    ap.add_argument("--subperiods", type=int, default=3)
    ap.add_argument("--only", default=None, help="그룹 접두어(A/B/...)")
    ap.add_argument("--exclude-from", default="20260301")
    args = ap.parse_args()

    groups = [g for g in GROUPS if not args.only or g[0].startswith(args.only)]
    slots = args.slots or getattr(config, "SYSTEM_MAX_HOLDINGS", 4)
    config.session.load_stock_config()
    stocks = config.session.stock_data.get("stocks_kr", [])
    names = {s["code"]: s["name"] for s in stocks}
    print(f"[준비] 관심종목 {len(stocks)}개 · {args.days}일 · 슬롯 {slots} · {args.interval} · 장중 체결")

    dfs, mf, dates, _f = pb.prepare_universe([(s["code"], s["name"]) for s in stocks], args.days)
    bars, st, keep, drop = ib.gate_universe(dfs, args.interval, min_coverage=args.min_coverage)
    if drop:
        print(f"[제외] {len(drop)}종목 — " + ", ".join(f"{names.get(c, c)}({w})" for c, w in drop))
    dfs = {c: dfs[c] for c in keep}
    mf = {c: mf.get(c, set()) for c in keep}
    dates = ib.covered_dates(bars, dates)
    if not dates:
        print("[중단] 겹치는 거래일 없음")
        return

    thresholds = {
        "BUY_SCORE": config.ANALYSIS_THRESHOLDS["BUY_SCORE"],
        "BUY_RSI_MAX": config.ANALYSIS_THRESHOLDS["BUY_RSI_MAX"],
        "RISE_SCORE": config.ANALYSIS_THRESHOLDS["RISE_SCORE"],
        "WEIGHTS": config.SCORING_WEIGHTS,
    }
    status = pb.precompute_status(dfs, thresholds)
    print(f"[준비] 사용 {len(dfs)}종목 / 거래일 {len(dates)}일 ({dates[0]}~{dates[-1]})")

    from tools.audit_drawdown_axis import market_scale_by_date, make_scale_fn
    p = getattr(config, "RISK_SCALING_PARAMS", {}) or {}
    mkt = market_scale_by_date(dates, args.days)
    dd = None
    if p.get("USE_DRAWDOWN_RISK_SCALING", True):
        dd = (int(p.get("DD_LOOKBACK_DAYS", 90)), float(p.get("DD_LEVEL_1", 5.0)),
              float(p.get("DD_SCALE_1", 0.9)), float(p.get("DD_LEVEL_2", 10.0)),
              float(p.get("DD_SCALE_2", 0.8)))
    new_scale_fn = lambda: make_scale_fn(mkt, dd)  # noqa: E731

    cut = "".join(ch for ch in str(args.exclude_from) if ch.isdigit())
    head = [d for d in dates if not cut or cut == "0" or d < cut]
    tail = [d for d in dates if cut and cut != "0" and d >= cut]
    k = max(1, args.subperiods)
    size = max(1, len(head) // k)
    windows = [("제외 전 전체", head)]
    if k > 1:
        windows += [(f"구간{i + 1}", head[i * size:(i + 1) * size if i < k - 1 else len(head)])
                    for i in range(k)]
    if tail:
        windows.append(("[대조] 제외구간(고변동)", tail))

    # 기준선은 그룹마다 같으므로 한 번만 돌린다.
    arms = [(BASE, [])] + [(f"{g[0][0]}|{lbl}", ov) for g in groups for lbl, ov in g[1]]
    codes = list(dfs.keys())
    all_results = {}
    total = len(windows) * args.seeds * args.trials * len(arms)
    done = 0
    for wname, wdates in windows:
        res = {lbl: [] for lbl, _ov in arms}
        for si in range(args.seeds):
            rng = random.Random(args.seed + si * 1009)
            for _t in range(args.trials):
                pick = rng.sample(codes, min(args.sample, len(codes)))
                sd = {c: dfs[c] for c in pick}
                sc = {c: status[c] for c in pick}
                sm = {c: mf.get(c, set()) for c in pick}
                sb = {c: bars[c] for c in pick}
                ss = {c: st[c] for c in pick}
                for lbl, ov in arms:
                    prev = apply(ov)
                    try:
                        r = pb.run_portfolio(sd, sc, wdates, initial_capital=args.seed_capital,
                                             slots=slots, market_filter_dates=sm,
                                             risk_scale_by_date=new_scale_fn(),
                                             intraday_bars=sb, intraday_status=ss)
                    finally:
                        apply(prev)
                    res[lbl].append(metrics(r))
                    done += 1
                print(f"  {wname} 씨드{si + 1} {done}/{total}", end="\r", flush=True)
        all_results[wname] = res
    print(" " * 60, end="\r")

    W = 116
    print(f"\n{'=' * W}")
    print(f"장중 세계 다이얼 재보정 — {args.trials}회 × {args.sample}종목 × 씨드 {args.seeds}개 "
          f"(기준선 = 현행 전 설정)")
    print(f"{'=' * W}")
    for wname, wdates in windows:
        res = all_results[wname]
        base = res[BASE]
        print(f"\n########## {wname} ({len(wdates)} 거래일) ##########")
        for gname, items in groups:
            print(f"\n{gname}")
            print(f"{'설정':<9}{'수익%':>9}{'MDD%':>8}{'MAR':>7}{'PF':>6}{'청산':>6}{'증액':>6}"
                  f"{'무장%':>7}{'상위10%':>9}{'최대':>9}{'>30%':>6}{'TS이익%':>8}{'보유일':>7}"
                  f"{'승-무-패':>10}{'MAR승':>7}")
            print("-" * W)
            for lbl, rs in [("현행", base)] + [(l, res[f"{gname[0]}|{l}"]) for l, _o in items]:
                m = lambda k: float(np.median([x[k] for x in rs]))  # noqa: E731
                is_base = rs is base
                tie = sum(1 for a, b in zip(rs, base) if abs(a["ret"] - b["ret"]) < 1e-9)
                los = sum(1 for a, b in zip(rs, base) if a["ret"] < b["ret"] - 1e-9)
                rw = sum(1 for a, b in zip(rs, base) if a["ret"] > b["ret"])
                mw = sum(1 for a, b in zip(rs, base) if a["mar"] > b["mar"])
                print(f"{lbl:<9}{m('ret'):>9.1f}{m('mdd'):>8.1f}{m('mar'):>7.2f}{m('pf'):>6.2f}"
                      f"{m('n'):>6.0f}{m('pyr_n'):>6.0f}{m('armed'):>7.1f}{m('top10'):>9.1f}"
                      f"{m('best'):>9.1f}{m('big'):>6.0f}{m('ts_share'):>8.1f}{m('days'):>7.1f}"
                      f"{'—' if is_base else f'{rw}-{tie}-{los}':>10}"
                      f"{'—' if is_base else f'{mw}/{len(rs)}':>7}")

    print("\n" + "-" * W)
    print("[읽는 법] 대안이 전체창에서 이겨도 구간별로 갈리면 채택하지 않는다(기존 감사와 같은 잣대).")
    print("[대조] 종가 세계의 기존 결론은 config.py 각 키 주석에 있다. 방향이 뒤집혔는지 확인할 것.")


if __name__ == "__main__":
    main()
