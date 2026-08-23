"""오늘의 결론이 체결 가정에 얼마나 기대고 있는가 — 슬리피지 민감도.

[왜] 체결률 실측(tools/audit_fill_rate.py)은 시스템 주문 기록이 쌓여야 가능하다. 그 전까지
 남는 질문은 '가정이 틀렸다면 결론이 뒤집히는가'다. 2026-08-11에 TS 발동 배수를 3.5 → 3.0
 으로 낮추며 10년 누적 수익 -11%를 감수했는데, 그 -11%가 슬리피지 0.2% 가정에서만 나오는
 숫자라면 판단의 근거가 약하다.

[방법] 발동 배수 3.5(종전)와 3.0(현행)을 **슬리피지 0.2 / 0.4 / 0.8%에서 각각** 돌린다.
 관심은 절대 수익이 아니라 **두 설정의 격차가 어떻게 변하는가**다. 슬리피지가 커질수록
 격차가 벌어지면 오늘의 선택이 체결 가정에 취약한 것이고, 격차가 유지되거나 줄면 견고하다.

 슬리피지는 진입·청산·증액 모두에 대칭으로 걸린다(portfolio_backtest가 config를 읽는다).

[실행] python3 tools/audit_slippage_sensitivity.py --days 3650 --trials 15 --sample 25
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
from modules.auto_trade import engine  # noqa: E402

INITIAL_CAPITAL = 10_000_000


def act_fn(mult):
    ss = config.SELL_STRATEGY
    cb_floor = ss.get("TRAILING_STOP_CALLBACK_RATE", 5.0)

    def fn(atr, price):
        return engine.breakeven_activation_rate(atr, price, cb_floor, mult, True)
    return fn


def metrics(r):
    sells = exits(r)
    profits = sorted((t["profit"] for t in sells), reverse=True)
    top10 = profits[:max(1, len(profits) // 10)]
    armed = [t for t in sells if t.get("armed")]
    return {
        "ret": r["total_return"], "mdd": r["mdd"],
        "mar": r["total_return"] / abs(r["mdd"]) if r["mdd"] else float("nan"),
        "top10": float(np.mean(top10)) if top10 else 0.0,
        "best": profits[0] if profits else 0.0,
        "armed_share": len(armed) / len(sells) * 100 if sells else 0.0,
        "n": len(sells),
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--trials", type=int, default=15)
    ap.add_argument("--sample", type=int, default=25)
    ap.add_argument("--days", type=int, default=3650)
    ap.add_argument("--slots", type=int, default=None)
    ap.add_argument("--seed", type=int, default=20260811)
    ap.add_argument("--exclude-from", default="20260301")
    args = ap.parse_args()

    slots = args.slots or getattr(config, "SYSTEM_MAX_HOLDINGS", 4)
    config.SELL_STRATEGY["TS_ACTIVATION_MAX_RATE"] = 0.0
    config.session.load_stock_config()
    targets = [(s["code"], s["name"]) for s in config.session.stock_data.get("stocks_kr", [])]
    dfs, mf, dates, failed = pb.prepare_universe(targets, args.days)
    thr = {"BUY_SCORE": config.ANALYSIS_THRESHOLDS["BUY_SCORE"],
           "BUY_RSI_MAX": config.ANALYSIS_THRESHOLDS["BUY_RSI_MAX"],
           "RISE_SCORE": config.ANALYSIS_THRESHOLDS["RISE_SCORE"],
           "WEIGHTS": config.SCORING_WEIGHTS}
    status = pb.precompute_status(dfs, thr)
    print(f"[준비] 사용 {len(dfs)}종목 / 거래일 {len(dates)}일")

    from tools.audit_drawdown_axis import market_scale_by_date, make_scale_fn
    p = getattr(config, "RISK_SCALING_PARAMS", {}) or {}
    mkt = market_scale_by_date(dates, args.days)
    dd = (int(p.get("DD_LOOKBACK_DAYS", 90)), float(p.get("DD_LEVEL_1", 5.0)),
          float(p.get("DD_SCALE_1", 0.9)), float(p.get("DD_LEVEL_2", 10.0)),
          float(p.get("DD_SCALE_2", 0.8))) if p.get("USE_DRAWDOWN_RISK_SCALING", True) else None

    cut = "".join(ch for ch in str(args.exclude_from) if ch.isdigit())
    head = [d for d in dates if not cut or cut == "0" or "".join(filter(str.isdigit, d)) < cut]

    codes = list(dfs.keys())
    saved_slip = getattr(config, "SLIPPAGE_RATE", 0.002)
    slips = [0.002, 0.004, 0.008]
    mults = [("3.5 (종전)", 3.5), ("3.0 (현행)", 3.0)]

    out = {}
    try:
        for slip in slips:
            config.SLIPPAGE_RATE = slip
            for label, mult in mults:
                res, rng = [], random.Random(args.seed)
                for t in range(args.trials):
                    pick = rng.sample(codes, min(args.sample, len(codes)))
                    r = pb.run_portfolio({c: dfs[c] for c in pick}, {c: status[c] for c in pick},
                                         head, initial_capital=INITIAL_CAPITAL, slots=slots,
                                         market_filter_dates={c: mf.get(c, set()) for c in pick},
                                         risk_scale_by_date=make_scale_fn(mkt, dd),
                                         ts_act_fn=act_fn(mult))
                    res.append(metrics(r))
                out[(slip, label)] = res
                print(f"  슬리피지 {slip * 100:.1f}% · 배수 {label} 완료", end="\r", flush=True)
    finally:
        config.SLIPPAGE_RATE = saved_slip
    print(" " * 50, end="\r")

    W = 92
    print(f"\n{'=' * W}")
    print(f"슬리피지 민감도 — 발동 배수 3.5 vs 3.0 ({args.trials}회 × {args.sample}종목, {len(head)} 거래일)")
    print(f"{'=' * W}")
    print(f"{'슬리피지':<10}{'배수':<12}{'수익%':>10}{'MDD%':>8}{'MAR':>7}{'무장률%':>9}"
          f"{'상위10%':>9}{'최대':>9}{'3.0의 대가':>12}")
    print("-" * W)
    for slip in slips:
        base = float(np.median([x["ret"] for x in out[(slip, "3.5 (종전)")]]))
        for label, _m in mults:
            rs = out[(slip, label)]
            m = lambda k: float(np.median([x[k] for x in rs]))  # noqa: E731
            cost = "" if label.startswith("3.5") else f"{(m('ret') / base - 1) * 100:+.1f}%"
            print(f"{slip * 100:<10.1f}{label:<12}{m('ret'):>10.1f}{m('mdd'):>8.1f}{m('mar'):>7.2f}"
                  f"{m('armed_share'):>9.1f}{m('top10'):>9.1f}{m('best'):>9.1f}{cost:>12}")
        print("-" * W)
    print("[읽는 법] '3.0의 대가'가 슬리피지와 함께 커지면 오늘의 선택이 체결 가정에 취약한 것이다.")


if __name__ == "__main__":
    main()
