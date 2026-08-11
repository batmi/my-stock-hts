"""슬롯 점유·수익 반납 감사 — "TS 발동선이 높으면 반납만 하고 슬롯을 문다"는 의견의 검증.

[의견] 발동선이 높으면 발동 직전까지 올랐다가 되돌아오는 포지션에 아무 액션을 못 하고
 슬롯만 계속 점유한다.

[사실관계] 발동선 아래에도 방어선은 있다 — BEP(본전청산)는 MFE ≥ 1R(ATR 손절폭)에서
 무장해 손절선을 +0.5%로 올린다. 따라서 되돌아오면 본전 부근에서 슬롯이 회수된다.
 실제 비용은 '무한 점유'가 아니라 **그 구간의 수익 반납**이다. 다만 시간청산은
 `profit_rate < TIME_STOP_MIN_PROFIT_RATE(0.0)` 이라 **손실일 때만** 걸리므로,
 MFE가 1R에 못 미친 채 소폭 플러스로 정체하는 포지션은 어느 문에도 걸리지 않는다.

[순서]
  1) 진단  — 반납·좀비가 실제로 얼마짜리인지 먼저 잰다(다이얼 변경 없음).
       · MFE 구간 × TS 무장여부별 건수·반납폭·슬롯일 비중
       · 좀비(청산 시 0 ≤ 수익 < 3% 이고 보유 30일 이상)의 슬롯일 비중
  2) 반사실 A — TIME_STOP_MIN_PROFIT_RATE 0(현행) / +1% / +3%. 위 구멍을 직접 겨냥.
  3) 반사실 B — PROFIT_LOCK(이익보호선) 전역 ON. 2026-08-09에 기각된 값의 재확인.
  4) 반사실 C — PROFIT_LOCK을 **강세 국면에만** ON. config에 "국면 조건부로만 의미가
       있을 수 있는데 측정한 적 없는 별개 축"이라 적혀 있던 바로 그 축이다.

[판정 잣대] 기존 청산 다이얼 결정과 같다 — 총수익·MDD뿐 아니라 상위10%·최대·>30%로
 fat-tail을, TS이익%로 '주청산은 샹들리에 TS' 설계 유지를 함께 본다. 하위 구간 다수에서
 이기지 못하면 채택하지 않는다.

[실행]
  python3 tools/audit_slot_cost.py --days 3650 --trials 15 --sample 25 --subperiods 4
  python3 tools/audit_slot_cost.py --diag-only        # 1) 진단만
"""
import argparse
import os
import random
import sys
from collections import defaultdict

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import config  # noqa: E402
from modules import portfolio_backtest as pb  # noqa: E402

INITIAL_CAPITAL = 10_000_000
TS_MIN = "TIME_STOP_MIN_PROFIT_RATE"
LOCK_USE, LOCK_MFE, LOCK_GB = "PROFIT_LOCK_USE", "PROFIT_LOCK_MIN_MFE", "PROFIT_LOCK_GIVEBACK"
SELL_REASONS = ("ATR손절", "손절", "본전청산", "시간청산", "트레일링스탑", "점수하락", "이익보호")
MFE_BUCKETS = [(0, 5), (5, 10), (10, 15), (15, 20), (20, 30), (30, 1e9)]


def dial_sets():
    """(그룹, 라벨, SELL_STRATEGY 오버라이드, 국면조건부 여부)."""
    return [
        ("A. 시간청산 최소이익", "0% (현행)", {}, False),
        ("A. 시간청산 최소이익", "+1%", {TS_MIN: 1.0}, False),
        ("A. 시간청산 최소이익", "+3%", {TS_MIN: 3.0}, False),
        ("B. 이익보호선(전역)", "OFF (현행)", {}, False),
        ("B. 이익보호선(전역)", "MFE25·반납0.5", {LOCK_USE: True, LOCK_MFE: 25.0, LOCK_GB: 0.5}, False),
        ("B. 이익보호선(전역)", "MFE15·반납0.5", {LOCK_USE: True, LOCK_MFE: 15.0, LOCK_GB: 0.5}, False),
        ("C. 이익보호선(강세만)", "OFF (현행)", {}, False),
        ("C. 이익보호선(강세만)", "MFE25·반납0.5", {LOCK_MFE: 25.0, LOCK_GB: 0.5}, True),
        ("C. 이익보호선(강세만)", "MFE15·반납0.5", {LOCK_MFE: 15.0, LOCK_GB: 0.5}, True),
    ]


def metrics(r):
    sells = [t for t in r["trades"] if t["reason"] in SELL_REASONS]
    profits = sorted((t["profit"] for t in sells), reverse=True)
    top10 = profits[:max(1, len(profits) // 10)]
    gross_gain = sum(t["profit_amt"] for t in sells if t["profit_amt"] > 0)
    ts_gain = sum(t["profit_amt"] for t in sells
                  if t["reason"] == "트레일링스탑" and t["profit_amt"] > 0)
    slot_days = sum(t["days"] for t in sells) or 1
    zombie = [t for t in sells if 0 <= t["profit"] < 3.0 and t["days"] >= 30]
    return {
        "ret": r["total_return"], "mdd": r["mdd"],
        "mar": r["total_return"] / abs(r["mdd"]) if r["mdd"] else float("nan"),
        "pf": r["pf"],
        "top10": float(np.mean(top10)) if top10 else 0.0,
        "best": profits[0] if profits else 0.0,
        "big": sum(1 for p in profits if p >= 30),
        "ts_profit_share": (ts_gain / gross_gain * 100) if gross_gain > 0 else 0.0,
        "days": float(np.median([t["days"] for t in sells])) if sells else 0.0,
        "zombie_share": sum(t["days"] for t in zombie) / slot_days * 100,
        "n": len(sells),
    }


def diagnose(all_trades):
    """MFE 구간 × 무장여부 — 반납이 어디서 얼마나 나는가."""
    sells = [t for t in all_trades if t["reason"] in SELL_REASONS and "mfe" in t]
    if not sells:
        print("진단할 청산이 없다.")
        return
    total_slot = sum(t["days"] for t in sells) or 1

    print(f"\n{'=' * 104}")
    print("1) 진단 — MFE 구간 × TS 무장여부 (다이얼 변경 없음, 현행 설정)")
    print(f"{'=' * 104}")
    print(f"{'MFE 구간':<12}{'무장':<7}{'건수':>7}{'비중%':>7}{'평균MFE':>9}{'평균실현':>9}"
          f"{'평균반납':>9}{'중앙보유일':>11}{'슬롯일%':>9}{'BEP청산%':>10}")
    print("-" * 104)
    for lo, hi in MFE_BUCKETS:
        for armed in (False, True):
            g = [t for t in sells if lo <= t["mfe"] < hi and bool(t["armed"]) is armed]
            if not g:
                continue
            label = f"{lo}~{'∞' if hi > 1e8 else int(hi)}%"
            print(f"{label:<12}{'무장' if armed else '미무장':<7}{len(g):>7}"
                  f"{len(g) / len(sells) * 100:>7.1f}"
                  f"{np.mean([t['mfe'] for t in g]):>9.1f}"
                  f"{np.mean([t['profit'] for t in g]):>9.1f}"
                  f"{np.mean([t['mfe'] - t['profit'] for t in g]):>9.1f}"
                  f"{np.median([t['days'] for t in g]):>11.0f}"
                  f"{sum(t['days'] for t in g) / total_slot * 100:>9.1f}"
                  f"{100 * sum(1 for t in g if t.get('bep')) / len(g):>10.1f}")

    print("-" * 104)
    gate = [t for t in sells if t["mfe"] >= 15 and not t["armed"]]
    armed = [t for t in sells if t["armed"]]
    zombie = [t for t in sells if 0 <= t["profit"] < 3.0 and t["days"] >= 30]
    dead = [t for t in sells if t["mfe"] < 15]
    print(f"의견의 대상 — MFE 15% 이상 도달했으나 무장 못 한 청산: {len(gate)}건 "
          f"({len(gate) / len(sells) * 100:.1f}%) · 슬롯일 {sum(t['days'] for t in gate) / total_slot * 100:.1f}% · "
          f"평균 MFE {np.mean([t['mfe'] for t in gate]) if gate else 0:.1f}% → 실현 "
          f"{np.mean([t['profit'] for t in gate]) if gate else 0:.1f}%")
    print(f"대조 — 무장에 성공한 청산: {len(armed)}건 ({len(armed) / len(sells) * 100:.1f}%) · "
          f"슬롯일 {sum(t['days'] for t in armed) / total_slot * 100:.1f}% · "
          f"평균 실현 {np.mean([t['profit'] for t in armed]) if armed else 0:.1f}%")
    print(f"좀비(0 ≤ 실현 < 3% 이고 30일 이상 보유): {len(zombie)}건 "
          f"({len(zombie) / len(sells) * 100:.1f}%) · 슬롯일 "
          f"{sum(t['days'] for t in zombie) / total_slot * 100:.1f}% · "
          f"중앙 보유 {np.median([t['days'] for t in zombie]) if zombie else 0:.0f}일")
    print(f"참고 — MFE가 15%에 못 미친 청산(발동선을 낮춰도 무장 불가): {len(dead)}건 "
          f"({len(dead) / len(sells) * 100:.1f}%) · 슬롯일 "
          f"{sum(t['days'] for t in dead) / total_slot * 100:.1f}%")
    for reason in SELL_REASONS:
        g = [t for t in sells if t["reason"] == reason]
        if g:
            print(f"  · {reason:<8} {len(g):>5}건 · 평균 {np.mean([t['profit'] for t in g]):>6.1f}% "
                  f"· 중앙 {np.median([t['days'] for t in g]):>3.0f}일 "
                  f"· 슬롯일 {sum(t['days'] for t in g) / total_slot * 100:>5.1f}%")


def bull_dates(dates, days):
    """강세 국면 거래일 — 두 지수 모두 Bull/PendUp 인 날(리스크 배수와 같은 '열위 기준')."""
    from datetime import datetime, timedelta
    from tools.audit_market_axes import load_index, regime_series
    start = (datetime.now() - timedelta(days=days + 400)).strftime("%Y-%m-%d")
    per_index = []
    for ticker in ("KS11", "KQ11"):
        idx, close = load_index(ticker, start)
        regimes, _ = regime_series(idx, close)
        per_index.append({d: r for d, r in zip(idx.strftime("%Y%m%d"), regimes)})
    out, last = set(), True
    for d in dates:
        vals = [m.get(d) for m in per_index]
        if all(v is not None for v in vals):
            last = all(v in ("Bull", "PendUp") for v in vals)
        if last:
            out.add(d)
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--trials", type=int, default=15)
    ap.add_argument("--sample", type=int, default=25)
    ap.add_argument("--days", type=int, default=3650)
    ap.add_argument("--slots", type=int, default=None)
    ap.add_argument("--seed-capital", type=int, default=INITIAL_CAPITAL)
    ap.add_argument("--only", default=None, help="그룹 접두사만 (예: A)")
    ap.add_argument("--seed", type=int, default=20260811)
    ap.add_argument("--subperiods", type=int, default=4)
    ap.add_argument("--exclude-from", default="20260301",
                    help="이 날짜 이후를 본 검증에서 제외(대조 창으로 따로 표시). 0이면 제외 없음")
    ap.add_argument("--diag-only", action="store_true")
    args = ap.parse_args()

    slots = args.slots or getattr(config, "SYSTEM_MAX_HOLDINGS", 4)
    config.session.load_stock_config()
    targets = [(s["code"], s["name"]) for s in config.session.stock_data.get("stocks_kr", [])]
    print(f"[준비] 관심종목 {len(targets)}개 · {args.days}일 · 슬롯 {slots} · 시드 {args.seed_capital:,}원")

    dfs, mf, dates, failed = pb.prepare_universe(targets, args.days)
    thresholds = {
        "BUY_SCORE": config.ANALYSIS_THRESHOLDS["BUY_SCORE"],
        "BUY_RSI_MAX": config.ANALYSIS_THRESHOLDS["BUY_RSI_MAX"],
        "RISE_SCORE": config.ANALYSIS_THRESHOLDS["RISE_SCORE"],
        "WEIGHTS": config.SCORING_WEIGHTS,
    }
    status = pb.precompute_status(dfs, thresholds)
    print(f"[준비] 사용 {len(dfs)}종목 / 거래일 {len(dates)}일"
          + (f" · 제외 {len(failed)}" if failed else ""))

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
    print(f"[창] 검증 {len(head)}일 (~{head[-1] if head else '-'}) "
          f"· 제외 {len(tail)}일 ({tail[0] if tail else '-'}~)")

    bulls = bull_dates(dates, args.days)
    print(f"[국면] 강세(두 지수 모두 Bull/PendUp) 거래일 {len(bulls)}일 "
          f"({len(bulls) / max(1, len(dates)) * 100:.1f}%)")

    codes = list(dfs.keys())
    saved = dict(config.SELL_STRATEGY)

    # ---------------- 1) 진단 ----------------
    diag_trades = []
    rng = random.Random(args.seed)
    for t in range(args.trials):
        pick = rng.sample(codes, min(args.sample, len(codes)))
        r = pb.run_portfolio({c: dfs[c] for c in pick}, {c: status[c] for c in pick}, head,
                             initial_capital=args.seed_capital, slots=slots,
                             market_filter_dates={c: mf.get(c, set()) for c in pick},
                             risk_scale_by_date=make_scale_fn(mkt, dd))
        diag_trades += r["trades"]
        print(f"  진단 시행 {t + 1}/{args.trials}", end="\r", flush=True)
    print(" " * 40, end="\r")
    diagnose(diag_trades)
    if args.diag_only:
        return

    # ---------------- 2~4) 반사실 ----------------
    sets = dial_sets()
    if args.only:
        sets = [x for x in sets if x[0].startswith(args.only)]

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
        results = {(g, l): [] for g, l, _o, _b in sets}
        rng = random.Random(args.seed)
        try:
            for t in range(args.trials):
                pick = rng.sample(codes, min(args.sample, len(codes)))
                sd = {c: dfs[c] for c in pick}
                st = {c: status[c] for c in pick}
                sm = {c: mf.get(c, set()) for c in pick}
                for g, label, overrides, regime_only in sets:
                    config.SELL_STRATEGY.clear(); config.SELL_STRATEGY.update(saved)
                    config.SELL_STRATEGY.update(overrides)
                    r = pb.run_portfolio(sd, st, wdates, initial_capital=args.seed_capital,
                                         slots=slots, market_filter_dates=sm,
                                         risk_scale_by_date=make_scale_fn(mkt, dd),
                                         profit_lock_dates=(bulls if regime_only else None))
                    results[(g, label)].append(metrics(r))
                print(f"  {wname} 시행 {t + 1}/{args.trials}", end="\r", flush=True)
        finally:
            config.SELL_STRATEGY.clear(); config.SELL_STRATEGY.update(saved)
        all_results[wname] = results
    print(" " * 50, end="\r")

    W = 112
    print(f"\n{'=' * W}")
    print(f"2~4) 반사실 — {args.trials}회 × {args.sample}종목 무작위 짝비교")
    print(f"{'=' * W}")
    for wname, wdates in windows:
        results = all_results[wname]
        print(f"\n########## {wname} ({len(wdates)} 거래일) ##########")
        last_group = None
        for g, label, _o, _b in sets:
            if g != last_group:
                base_label = next(l for gg, l, _o2, _b2 in sets if gg == g and "현행" in l)
                base = results[(g, base_label)]
                print(f"\n{g}  (기준선: {base_label})")
                print(f"{'설정':<16}{'수익%':>9}{'MDD%':>8}{'MAR':>7}{'PF':>6}{'상위10%':>9}"
                      f"{'최대':>9}{'>30%':>6}{'TS이익%':>9}{'좀비슬롯%':>11}{'보유일':>7}"
                      f"{'청산':>6}{'수익승':>8}{'MAR승':>7}{'꼬리승':>7}")
                print("-" * W)
                last_group = g
            rs = results[(g, label)]
            m = lambda k: float(np.median([x[k] for x in rs]))  # noqa: E731
            is_base = "현행" in label
            rw = sum(1 for a, b in zip(rs, base) if a["ret"] > b["ret"])
            mw = sum(1 for a, b in zip(rs, base) if a["mar"] > b["mar"])
            tw = sum(1 for a, b in zip(rs, base) if a["top10"] > b["top10"])
            print(f"{label:<16}{m('ret'):>9.1f}{m('mdd'):>8.1f}{m('mar'):>7.2f}{m('pf'):>6.2f}"
                  f"{m('top10'):>9.1f}{m('best'):>9.1f}{m('big'):>6.0f}"
                  f"{m('ts_profit_share'):>9.1f}{m('zombie_share'):>11.1f}{m('days'):>7.1f}"
                  f"{m('n'):>6.0f}"
                  f"{'—' if is_base else f'{rw}/{len(rs)}':>8}"
                  f"{'—' if is_base else f'{mw}/{len(rs)}':>7}"
                  f"{'—' if is_base else f'{tw}/{len(rs)}':>7}")

    print("\n" + "-" * W)
    print("좀비슬롯% = 실현 0~3%인데 30일 이상 물고 있던 청산이 차지한 슬롯일 비중.")
    print("[읽는 법] 하위 구간 다수에서 이기지 못하면 채택하지 않는다(단일 창 우위는 과최적화).")


if __name__ == "__main__":
    main()
