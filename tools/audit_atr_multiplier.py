"""ATR 배수 재검증 — '2026년 고변동 구간을 빼고' 적정 배수를 찾는다.

[왜] 2026-03 이후 유니버스 ATR/가격 중앙값이 4.5% → 6~8.7%로 뛰면서 손절 캡 구속률이
 66%까지, TS 발동선 중앙이 43%까지 올라갔다. 이 구간이 표본에 섞여 있으면 배수 결론이
 '한 국면'에 끌려간다. 그래서 2026-03-01 이후를 잘라낸 창에서 배수를 다시 잰다.
 잘라낸 구간은 버리지 않고 '제외구간' 창으로 따로 찍어, 저변동에서 고른 값이 고변동에서
 어떻게 되는지(견고성)를 같이 본다.

[주의 · 두 배수는 다른 일을 한다]
  · ATR_STOP_MULTIPLIER(2.0)   = 손절폭. TS 발동선과 무관하다. 좁히면 무장 전 유일한
    보호선이 노이즈 안으로 들어오고, BEP 발동 기준(1R)도 함께 당겨진다.
  · TRAILING_ATR_MULTIPLIER(3.5) = 콜백 + 발동선(cb/(1-cb)). 발동선을 낮추려면 이쪽이다.
    다만 콜백도 같이 좁아진다 — 발동은 빨라지고 청산도 빨라지는 양면 다이얼.

[판정 잣대] 기존 청산 다이얼 결정과 같다. 총수익뿐 아니라
  · 상위10%·최대·>30% : fat-tail이 살아 있는가
  · TS이익% · 손절%   : '주청산은 샹들리에 TS' 설계가 유지되는가
  · 구간 분할          : 단일 창 결론 방지

[실행]
  python3 tools/audit_atr_multiplier.py --days 3650 --trials 15 --sample 25 --subperiods 4
  python3 tools/audit_atr_multiplier.py --only A          # 손절 배수만
  python3 tools/audit_atr_multiplier.py --exclude-from 20260601
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

INITIAL_CAPITAL = 10_000_000  # 실거래 시드와 같게 둔다(seed-slot-sizing)
SL_KEY = "ATR_STOP_MULTIPLIER"
CAP_KEY = "MAX_ATR_STOP_LOSS_RATE"
TS_KEY = "TRAILING_ATR_MULTIPLIER"


def dial_sets():
    return [
        ("A. 손절 ATR 배수", "1.5 (좁힘)",   {SL_KEY: 1.5}),
        ("A. 손절 ATR 배수", "1.75",         {SL_KEY: 1.75}),
        ("A. 손절 ATR 배수", "2.0 (현행)",   {SL_KEY: 2.0}),
        ("A. 손절 ATR 배수", "2.5 (넓힘)",   {SL_KEY: 2.5}),
        ("B. TS ATR 배수", "2.5 (좁힘)",     {TS_KEY: 2.5}),
        ("B. TS ATR 배수", "3.0",            {TS_KEY: 3.0}),
        ("B. TS ATR 배수", "3.5 (현행)",     {TS_KEY: 3.5}),
        ("B. TS ATR 배수", "4.0 (넓힘)",     {TS_KEY: 4.0}),
        # [C] 손절 캡. 2026-08-09에 '10년 중 8년치에서 무영향'으로 -15% 유지를 확정했으나,
        #  그때는 TS 발동이 콜백 배수(3.5)에 묶여 있던 시절이다. 발동을 3.0으로 분리해
        #  무장이 빨라진 지금은 손절선과 TS 청산선의 경합 구간이 달라졌으므로 다시 잰다.
        ("C. ATR 손절 캡", "해제(0)",       {CAP_KEY: 0.0}),
        ("C. ATR 손절 캡", "-25%",          {CAP_KEY: -25.0}),
        ("C. ATR 손절 캡", "-20%",          {CAP_KEY: -20.0}),
        ("C. ATR 손절 캡", "-15% (현행)",   {CAP_KEY: -15.0}),
        ("C. ATR 손절 캡", "-12%",          {CAP_KEY: -12.0}),
    ]


def metrics(r):
    sells = exits(r)
    profits = sorted((t["profit"] for t in sells), reverse=True)
    top10 = profits[:max(1, len(profits) // 10)]
    gross_gain = sum(t["profit_amt"] for t in sells if t["profit_amt"] > 0)
    ts_gain = sum(t["profit_amt"] for t in sells
                  if t["reason"] == "트레일링스탑" and t["profit_amt"] > 0)
    stop_n = sum(1 for t in sells if t["reason"] in ("ATR손절", "손절"))
    mdd = r["mdd"]
    return {
        "ret": r["total_return"], "mdd": mdd,
        "mar": r["total_return"] / abs(mdd) if mdd else float("nan"),
        "pf": r["pf"],
        "wr": r["win"] / max(1, r["win"] + r["loss"]) * 100,
        "top10": float(np.mean(top10)) if top10 else 0.0,
        "best": profits[0] if profits else 0.0,
        "big": sum(1 for p in profits if p >= 30),
        "ts_profit_share": (ts_gain / gross_gain * 100) if gross_gain > 0 else 0.0,
        "stop_share": (stop_n / len(sells) * 100) if sells else 0.0,
        "days": float(np.median([t["days"] for t in sells])) if sells else 0.0,
        "n": len(sells),
    }


def _digits(d):
    return "".join(ch for ch in str(d) if ch.isdigit())


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--trials", type=int, default=15)
    ap.add_argument("--sample", type=int, default=25)
    ap.add_argument("--days", type=int, default=3650, help="달력일. 3650=10년(거래일 ≈2,450)")
    ap.add_argument("--slots", type=int, default=None)
    ap.add_argument("--seed-capital", type=int, default=INITIAL_CAPITAL)
    ap.add_argument("--only", default=None, help="그룹 접두사만 (예: A)")
    ap.add_argument("--seed", type=int, default=20260811)
    ap.add_argument("--subperiods", type=int, default=4, help="제외 후 창을 N등분")
    ap.add_argument("--exclude-from", default="20260301",
                    help="이 날짜(YYYYMMDD) 이후를 본 검증에서 제외한다. 0이면 제외 없음")
    ap.add_argument("--no-tail", action="store_true", help="제외구간 대조 창을 찍지 않는다")
    args = ap.parse_args()

    slots = args.slots or getattr(config, "SYSTEM_MAX_HOLDINGS", 4)
    config.session.load_stock_config()
    targets = [(s["code"], s["name"])
               for s in config.session.stock_data.get("stocks_kr", [])]
    print(f"[준비] 관심종목 {len(targets)}개 · {args.days}일 · 슬롯 {slots} "
          f"· 시드 {args.seed_capital:,}원 · 제외 시작 {args.exclude_from}")

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

    cut = _digits(args.exclude_from)
    head = [d for d in dates if not cut or cut == "0" or _digits(d) < cut]
    tail = [d for d in dates if cut and cut != "0" and _digits(d) >= cut]
    print(f"[창] 검증 {len(head)}일 (~{head[-1] if head else '-'}) "
          f"· 제외 {len(tail)}일 ({tail[0] if tail else '-'}~)")

    k = max(1, args.subperiods)
    size = max(1, len(head) // k)
    windows = ([(f"구간{i + 1}", head[i * size:(i + 1) * size if i < k - 1 else len(head)])
                for i in range(k)] if k > 1 else [("전체", head)])
    windows.insert(0, ("제외 전 전체", head))
    if tail and not args.no_tail:
        windows.append(("[대조] 제외구간(고변동)", tail))

    sets = dial_sets()
    if args.only:
        sets = [x for x in sets if x[0].startswith(args.only)]
    saved = dict(config.SELL_STRATEGY)
    codes = list(dfs.keys())

    all_results = {}
    for wname, wdates in windows:
        results = {(g, l): [] for g, l, _ in sets}
        rng = random.Random(args.seed)
        try:
            for t in range(args.trials):
                pick = rng.sample(codes, min(args.sample, len(codes)))
                sd = {c: dfs[c] for c in pick}
                st = {c: status[c] for c in pick}
                sm = {c: mf.get(c, set()) for c in pick}
                for g, label, overrides in sets:
                    config.SELL_STRATEGY.clear(); config.SELL_STRATEGY.update(saved)
                    config.SELL_STRATEGY.update(overrides)
                    r = pb.run_portfolio(sd, st, wdates, initial_capital=args.seed_capital,
                                         slots=slots, market_filter_dates=sm,
                                         risk_scale_by_date=make_scale_fn(mkt, dd))
                    results[(g, label)].append(metrics(r))
                print(f"  {wname} 시행 {t + 1}/{args.trials}", end="\r", flush=True)
        finally:
            config.SELL_STRATEGY.clear(); config.SELL_STRATEGY.update(saved)
        all_results[wname] = results
    print(" " * 50, end="\r")

    W = 118
    print(f"\n{'=' * W}")
    print(f"ATR 배수 재검증 — {args.trials}회 × {args.sample}종목 무작위 짝비교 "
          f"({args.days}일 창에서 {args.exclude_from} 이후 제외)")
    print(f"{'=' * W}")
    for wname, wdates in windows:
        results = all_results[wname]
        print(f"\n########## {wname} ({len(wdates)} 거래일) ##########")
        last_group = None
        for g, label, _ in sets:
            if g != last_group:
                base_label = next(l for gg, l, _ in sets if gg == g and "현행" in l)
                base = results[(g, base_label)]
                print(f"\n{g}  (기준선: {base_label})")
                print(f"{'설정':<14}{'수익%':>9}{'MDD%':>8}{'MAR':>7}{'PF':>6}{'승률%':>7}"
                      f"{'상위10%':>9}{'최대':>9}{'>30%':>6}{'TS이익%':>9}{'손절%':>7}"
                      f"{'보유일':>7}{'청산':>6}{'수익승':>8}{'MAR승':>7}{'꼬리승':>7}")
                print("-" * W)
                last_group = g
            rs = results[(g, label)]
            m = lambda k: float(np.median([x[k] for x in rs]))  # noqa: E731
            is_base = "현행" in label
            rw = sum(1 for a, b in zip(rs, base) if a["ret"] > b["ret"])
            mw = sum(1 for a, b in zip(rs, base) if a["mar"] > b["mar"])
            tw = sum(1 for a, b in zip(rs, base) if a["top10"] > b["top10"])
            print(f"{label:<14}{m('ret'):>9.1f}{m('mdd'):>8.1f}{m('mar'):>7.2f}{m('pf'):>6.2f}"
                  f"{m('wr'):>7.1f}{m('top10'):>9.1f}{m('best'):>9.1f}{m('big'):>6.0f}"
                  f"{m('ts_profit_share'):>9.1f}{m('stop_share'):>7.1f}{m('days'):>7.1f}"
                  f"{m('n'):>6.0f}"
                  f"{'—' if is_base else f'{rw}/{len(rs)}':>8}"
                  f"{'—' if is_base else f'{mw}/{len(rs)}':>7}"
                  f"{'—' if is_base else f'{tw}/{len(rs)}':>7}")

    print("\n" + "-" * W)
    print("수익승·MAR승·꼬리승 = 각 그룹 현행 대비 개선된 시행 수(짝비교).")
    print("TS이익% = 트레일링 청산이 총이익에서 차지하는 비중 — '주청산은 샹들리에 TS' 설계 유지 지표.")
    print("[읽는 법] 하위 구간 다수에서 이기지 못하면 채택하지 않는다(단일 창 우위는 과최적화).")


if __name__ == "__main__":
    main()
