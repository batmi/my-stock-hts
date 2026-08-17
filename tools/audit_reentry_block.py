"""손절가 재진입 차단 게이트 — 실매매에만 있던 마지막 축.

[왜] `REENTRY_BLOCK_ABOVE_STOP_PRICE`는 trader.py에만 있고 portfolio_backtest에는 없었다.
 감사 도구 69개 어디에도 등장하지 않는다. 2026-08-05에 **로그 관측**으로 도입됐다 —
 손절 직후 같은 종목을 10초 뒤 1,000원 비싸게 재매수하는 일이 매 주기 반복됐고, 왕복
 스프레드(약 0.65%)만큼 실현 손실만 쌓였다. **막아서 아낀 돈은 관측됐지만, 막아서 놓친
 진입의 비용은 한 번도 재지 않았다.** 추세추종에서 손절 후 되돌림 재진입은 정상 경로다.

[일봉으로는 못 잰다] 종가 모델은 매도·매수가 같은 가격이라 '더 비싸게 되사기'라는
 현상 자체가 없다. **60분봉 세계에서만** 손절 봉 이후 더 높은 봉에서 재진입이 일어난다.
 그래서 이 감사는 진입·청산·증액을 모두 봉 단위로 돌린다([[backtest-intraday-scan-mode]]).

[먼저 빈도부터 센다] 저장소 규약([[residual-dials-closed]]). 게이트가 10년에 몇 번
 걸리지 않으면 성과 차이는 잡음이다. pb가 `reentry_blocked`로 실제 차단 횟수를 돌려준다.

[팔] 게이트 OFF(백테스트 종전 동작) vs ON(실매매 현행). 그 외 조건은 전부 같다.

[실행] python3 tools/audit_reentry_block.py --trials 15
"""
import argparse
import os
import random
import sys

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import config  # noqa: E402
from modules import intraday_bars as ib  # noqa: E402
from modules import portfolio_backtest as pb  # noqa: E402
from tools.audit_defensive_sector import metrics  # noqa: E402
from tools.audit_drawdown_axis import market_scale_by_date, make_scale_fn  # noqa: E402


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=1200)
    ap.add_argument("--interval", default="60m")
    ap.add_argument("--min-coverage", type=float, default=0.9)
    ap.add_argument("--trials", type=int, default=15)
    ap.add_argument("--sample", type=int, default=20)
    ap.add_argument("--seeds", default="20260816,7,101")
    ap.add_argument("--seed-capital", type=int, default=10_000_000)
    ap.add_argument("--slots", type=int, default=None)
    ap.add_argument("--subperiods", type=int, default=3)
    args = ap.parse_args()
    seeds = [int(x) for x in args.seeds.split(",")]
    slots = args.slots or getattr(config, "SYSTEM_MAX_HOLDINGS", 4)

    config.session.load_stock_config()
    stocks = config.session.stock_data.get("stocks_kr", [])
    names = {s["code"]: s["name"] for s in stocks}
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

    thr = {"BUY_SCORE": config.ANALYSIS_THRESHOLDS["BUY_SCORE"],
           "BUY_RSI_MAX": config.ANALYSIS_THRESHOLDS["BUY_RSI_MAX"],
           "RISE_SCORE": config.ANALYSIS_THRESHOLDS["RISE_SCORE"],
           "WEIGHTS": config.SCORING_WEIGHTS}
    status = pb.precompute_status(dfs, thr)
    print(f"[준비] {len(dfs)}종목 · 거래일 {len(dates)} ({dates[0]}~{dates[-1]}) · "
          f"슬롯 {slots} · {args.interval} · 진입·청산·증액 모두 봉 단위", flush=True)

    p = getattr(config, "RISK_SCALING_PARAMS", {}) or {}
    mkt = market_scale_by_date(dates, args.days) if p.get("USE_MARKET_RISK_SCALING", True) else None
    dd = None
    if p.get("USE_DRAWDOWN_RISK_SCALING", True):
        dd = (int(p.get("DD_LOOKBACK_DAYS", 90)), float(p.get("DD_LEVEL_1", 5.0)),
              float(p.get("DD_SCALE_1", 0.9)), float(p.get("DD_LEVEL_2", 10.0)),
              float(p.get("DD_SCALE_2", 0.8)))

    codes = list(dfs)
    k = max(1, args.subperiods)
    size = max(1, len(dates) // k)
    W = [("전체", list(dates))]
    W += [(f"구간{i + 1}", dates[i * size:(i + 1) * size if i < k - 1 else len(dates)])
          for i in range(k)]

    picks = {}
    for sd_ in seeds:
        for t in range(args.trials):
            picks[(sd_, t)] = random.Random(sd_ * 31 + t).sample(
                codes, min(args.sample, len(codes)))

    for wn, wd in W:
        print(f"\n########## {wn} ({len(wd)} 거래일) ##########")
        print(f"{'팔':<24}{'수익%':>9}{'MDD%':>8}{'MAR':>7}{'PF':>6}{'청산':>6}"
              f"{'상위10%':>9}{'승률%':>7}{'차단':>7}{'승-무-패':>10}")
        base, cells = None, {}
        for label, gate in (("[기준선] 게이트 OFF", False), ("게이트 ON (실매매 현행)", True)):
            res, blocked = [], []
            for sd_ in seeds:
                for t in range(args.trials):
                    pick = picks[(sd_, t)]
                    r = pb.run_portfolio(
                        {c: dfs[c] for c in pick}, {c: status[c] for c in pick}, wd,
                        initial_capital=args.seed_capital, slots=slots,
                        market_filter_dates={c: mf.get(c, set()) for c in pick},
                        risk_scale_by_date=make_scale_fn(mkt, dd),
                        intraday_bars={c: bars[c] for c in pick},
                        intraday_status={c: st[c] for c in pick},
                        intraday_entry=True, pyr_intraday=True)
                    res.append(metrics(r))
                    blocked.append(r["reentry_blocked"])
            cells[label] = res
            g = lambda key: float(np.mean([m[key] for m in res]))  # noqa: E731
            if base is None:
                base, wl = res, "— (기준)"
            else:
                win = sum(1 for x, y in zip(res, base) if x["ret"] > y["ret"] + 1e-9)
                tie = sum(1 for x, y in zip(res, base) if abs(x["ret"] - y["ret"]) <= 1e-9)
                wl = f"{win}-{tie}-{len(res) - win - tie}"
            print(f"{label:<24}{g('ret'):>9.1f}{g('mdd'):>8.1f}{g('mar'):>7.2f}{g('pf'):>6.2f}"
                  f"{g('n'):>6.0f}{g('top10'):>9.1f}{g('win'):>7.1f}"
                  f"{np.mean(blocked):>7.1f}{wl:>10}", flush=True)
        if wn == "전체":
            tot = float(np.mean([b for b in blocked]))
            print(f"  [빈도] 시행당 평균 차단 {tot:.1f}건 / 거래일 {len(wd)} "
                  f"= {tot / len(wd) * 100:.2f}일당 1건 꼴. 차단이 드물면 성과 차이는 잡음이다.")

    print("\n[읽는 법] 게이트 ON이 지면 '판 값보다 비싸게 되사기'를 막는 대가가 놓친 진입보다 "
          "크다는 뜻이다. 실매매에만 있던 장치이므로 기존 다이얼 결론은 이 게이트가 없는 "
          "세계에서 정해졌다는 점도 함께 볼 것.")


if __name__ == "__main__":
    main()
