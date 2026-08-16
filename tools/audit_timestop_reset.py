"""시간청산 시계 리셋 — 백테스트만 증액 때마다 0으로 되돌리고 있다(패리티 결함).

[왜] 실매매는 2026-07-29(engine.resolve_entry_date)부터 보유일수를 **진입일**(보유수량이
 0 → 1 이상이 된 시점)으로 잰다. 분할 매수·피라미딩으로 1주만 더 담아도 시계가 리셋되어
 시간청산이 무한히 미뤄지던 문제를 고친 것이다. 그런데 포트폴리오 백테스트는 그 **이틀
 전**(2026-07-27)에 작성됐고, 증액할 때마다 `pos["buy_dt"]`를 오늘로 갱신한다 —
 주석은 "실매매와 동일하게"라고 적혀 있지만 이미 사실이 아니다.

[무엇이 걸려 있나] 피라미딩은 3차까지 가므로 리셋은 최대 3번이다. 즉 백테스트는 증액된
 포지션을 실매매보다 훨씬 오래 들고 있다. `TIME_STOP_DAYS`(20 → 15)는 그 세계에서
 정해졌으므로, 리셋을 끄면 같은 15일이 전혀 다른 강도가 된다. 이 도구는 (1) 리셋 유무의
 손익 차이와 (2) 리셋을 끈 세계에서 TIME_STOP_DAYS 가 여전히 15가 맞는지를 함께 잰다.

[실행] python3 tools/audit_timestop_reset.py --days 3650 --trials 15 --sample 25 --subperiods 4
"""
import argparse
import os
import random
import sys

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import config  # noqa: E402
from modules import portfolio_backtest as pb  # noqa: E402

INITIAL_CAPITAL = 10_000_000
SELL_REASONS = ("ATR손절", "손절", "본전청산", "시간청산", "트레일링스탑", "점수하락", "이익보호")
BASE = "A. 리셋 O·15일 (현 백테스트)"


def arms():
    """(라벨, run_portfolio 인자, TIME_STOP_DAYS 오버라이드)."""
    return [
        (BASE,                          {"pyr_reset_time_stop": True},  15),
        ("B. 리셋 X·15일 (현 실매매)",  {"pyr_reset_time_stop": False}, 15),
        ("C. 리셋 X·20일",              {"pyr_reset_time_stop": False}, 20),
        ("D. 리셋 X·30일",              {"pyr_reset_time_stop": False}, 30),
    ]


def metrics(r):
    sells = [t for t in r["trades"] if t["reason"] in SELL_REASONS]
    profits = sorted((t["profit"] for t in sells), reverse=True)
    top10 = profits[:max(1, len(profits) // 10)]
    return {
        "ret": r["total_return"], "mdd": r["mdd"],
        "mar": r["total_return"] / abs(r["mdd"]) if r["mdd"] else float("nan"),
        "pf": r["pf"], "n": len(sells),
        "time_n": sum(1 for t in sells if t["reason"] == "시간청산"),
        "pyr_n": r.get("pyramid_count", 0),
        "top10": float(np.mean(top10)) if top10 else 0.0,
        "best": profits[0] if profits else 0.0,
        "big": sum(1 for p in profits if p >= 30),
        "days": float(np.median([t["days"] for t in sells])) if sells else 0.0,
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--trials", type=int, default=15)
    ap.add_argument("--sample", type=int, default=25)
    ap.add_argument("--days", type=int, default=3650)
    ap.add_argument("--slots", type=int, default=None)
    ap.add_argument("--seed-capital", type=int, default=INITIAL_CAPITAL)
    ap.add_argument("--seed", type=int, default=20260816)
    ap.add_argument("--subperiods", type=int, default=4)
    ap.add_argument("--exclude-from", default="20260301")
    args = ap.parse_args()

    slots = args.slots or getattr(config, "SYSTEM_MAX_HOLDINGS", 4)
    config.session.load_stock_config()
    targets = [(s["code"], s["name"]) for s in config.session.stock_data.get("stocks_kr", [])]
    print(f"[준비] 관심종목 {len(targets)}개 · {args.days}일 · 슬롯 {slots}")
    print(f"[기준] TIME_STOP_DAYS {config.SELL_STRATEGY.get('TIME_STOP_DAYS')} · "
          f"피라미딩 최대 {config.ANALYSIS_THRESHOLDS.get('PYRAMIDING_MAX_COUNT')}차")

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
    new_scale_fn = lambda: make_scale_fn(mkt, dd)  # noqa: E731

    cut = "".join(ch for ch in str(args.exclude_from) if ch.isdigit())
    head = [d for d in dates if not cut or cut == "0" or "".join(filter(str.isdigit, d)) < cut]
    tail = [d for d in dates if cut and cut != "0" and "".join(filter(str.isdigit, d)) >= cut]

    sets = arms()
    codes = list(dfs.keys())
    saved_days = config.SELL_STRATEGY["TIME_STOP_DAYS"]

    k = max(1, args.subperiods)
    size = max(1, len(head) // k)
    windows = [("제외 전 전체", head)]
    if k > 1:
        windows += [(f"구간{i + 1}", head[i * size:(i + 1) * size if i < k - 1 else len(head)])
                    for i in range(k)]
    if tail:
        windows.append(("[대조] 제외구간(고변동)", tail))

    all_results = {}
    try:
        for wname, wdates in windows:
            results = {label: [] for label, _kw, _d in sets}
            rng = random.Random(args.seed)
            for t in range(args.trials):
                pick = rng.sample(codes, min(args.sample, len(codes)))
                sd = {c: dfs[c] for c in pick}
                st = {c: status[c] for c in pick}
                sm = {c: mf.get(c, set()) for c in pick}
                for label, kw, tsd in sets:
                    config.SELL_STRATEGY["TIME_STOP_DAYS"] = tsd
                    r = pb.run_portfolio(sd, st, wdates, initial_capital=args.seed_capital,
                                         slots=slots, market_filter_dates=sm,
                                         risk_scale_by_date=new_scale_fn(), **kw)
                    results[label].append(metrics(r))
                print(f"  {wname} 시행 {t + 1}/{args.trials}", end="\r", flush=True)
            all_results[wname] = results
    finally:
        config.SELL_STRATEGY["TIME_STOP_DAYS"] = saved_days
    print(" " * 50, end="\r")

    W = 108
    print(f"\n{'=' * W}")
    print(f"시간청산 시계 리셋 — {args.trials}회 × {args.sample}종목 짝비교 (기준선: 현 백테스트)")
    print(f"{'=' * W}")
    for wname, wdates in windows:
        results = all_results[wname]
        base = results[BASE]
        print(f"\n########## {wname} ({len(wdates)} 거래일) ##########")
        print(f"{'설정':<28}{'수익%':>9}{'MDD%':>8}{'MAR':>7}{'PF':>6}{'청산':>6}{'시간청산':>9}"
              f"{'증액':>6}{'상위10%':>9}{'최대':>9}{'>30%':>6}{'보유일':>7}{'승-무-패':>10}{'MAR승':>7}")
        print("-" * W)
        for label, _kw, _d in sets:
            rs = results[label]
            m = lambda k: float(np.median([x[k] for x in rs]))  # noqa: E731
            is_base = label == BASE
            tie = sum(1 for a, b in zip(rs, base) if abs(a["ret"] - b["ret"]) < 1e-9)
            los = sum(1 for a, b in zip(rs, base) if a["ret"] < b["ret"] - 1e-9)
            rw = sum(1 for a, b in zip(rs, base) if a["ret"] > b["ret"])
            mw = sum(1 for a, b in zip(rs, base) if a["mar"] > b["mar"])
            print(f"{label:<28}{m('ret'):>9.1f}{m('mdd'):>8.1f}{m('mar'):>7.2f}{m('pf'):>6.2f}"
                  f"{m('n'):>6.0f}{m('time_n'):>9.0f}{m('pyr_n'):>6.0f}{m('top10'):>9.1f}"
                  f"{m('best'):>9.1f}{m('big'):>6.0f}{m('days'):>7.1f}"
                  f"{'—' if is_base else f'{rw}-{tie}-{los}':>10}"
                  f"{'—' if is_base else f'{mw}/{len(rs)}':>7}")

    print("\n" + "-" * W)
    print("[읽는 법] A와 B의 차이가 리셋 결함의 크기다. B~D 중 어디가 최선인지가")
    print(" '실매매 세계에서 TIME_STOP_DAYS 를 얼마로 둘 것인가'의 답이다.")


if __name__ == "__main__":
    main()
