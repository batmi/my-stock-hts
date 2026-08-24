"""진입액을 키우는 두 경로를 가른다 — 슬롯 축소 vs 의도적 오버커밋.

[왜] 사용자의 목적은 '최초 진입 시 매수액을 늘리는 것'이고, 그 수단으로 슬롯 4 → 3이
 제안됐다. 그런데 슬롯 축소는 두 가지를 **동시에** 한다.
   ① 기초 비중이 1/4 → 1/3으로 올라 진입액이 33% 커진다.
   ② 동시 보유 가능 종목이 하나 줄어 승자 포착 기회가 준다.
 지금까지의 슬롯 감사(2/3/5 기각)는 ①과 ②를 묶어서 쟀으므로 **어느 쪽이 결과를 만들었는지
 모른다.** 진입액만 키우고 슬롯은 지키는 길(오버커밋)이 있는데 아무도 재지 않았다.

[격자] 2요인. 슬롯 {4,3} × 기초비중 {0.25, 0.30, 0.333}에서 의미 있는 5칸.
   A [기준선] 4슬롯 × 0.25   명목 100%  — 현행
   B          4슬롯 × 0.30   명목 120%  — 진입액만 +20% (오버커밋)
   C          4슬롯 × 0.333  명목 133%  — 진입액만 +33% (D와 진입액 동일)
   D          3슬롯 × 0.333  명목 100%  — 사용자 원안 (①+② 동시)
   E [대조]   3슬롯 × 0.25   명목  75%  — 슬롯만 축소, 진입액 불변 (②만)
 C vs D가 ②의 순수 효과이고, B·C vs A가 ①의 순수 효과다. E는 그 분해를 교차 검증한다.

[생존 편향] 집중도를 바꾸는 축이므로 폐지 혼합이 필수다 — 살아남은 승자에 더 크게
 베팅하는 것이라 생존 편향이 정확히 증폭된다([[universe-size-lever]]의 교훈).
 표본은 관심종목·확장 풀에서 **무작위로** 44종목을 뽑는다(사람이 고른 목록을 그대로 쓰면
 그 자체가 2.6배 부풀린다).

[불변식도 함께 본다] 오버커밋은 성과 이전에 '정해둔 상한을 깨는가'가 먼저다.
 진입 순간 최대 비중·1회 최대 리스크·리스크 한도(4%) 위반 건수를 팔마다 찍는다.

[실행] python3 tools/audit_overcommit.py --trials 12
"""
import argparse
import os
import random
import sys

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import config  # noqa: E402
from modules import portfolio_backtest as pb  # noqa: E402
from tools.audit_defensive_sector import (  # noqa: E402
    INITIAL_CAPITAL, metrics, new_scale_fn_factory,
)
from tools.audit_universe import dead_targets, extend_targets  # noqa: E402
from tools.audit_common import seed_notice  # noqa: E402

ARMS = [
    ("[기준선] 4슬롯 × 0.25", 4, None),      # None = 자동(1/slots)
    ("4슬롯 × 0.30 (명목120%)", 4, 0.30),
    ("4슬롯 × 0.333 (명목133%)", 4, 1.0 / 3),
    ("3슬롯 × 0.333 (원안)", 3, 1.0 / 3),
    ("[대조] 3슬롯 × 0.25", 3, 0.25),
]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=3650)
    ap.add_argument("--trials", type=int, default=12)
    ap.add_argument("--seeds", default="20260816,7,101")
    ap.add_argument("--size", type=int, default=44)
    ap.add_argument("--pool", type=int, default=500)
    ap.add_argument("--dead-frac", type=float, default=0.2)
    ap.add_argument("--subperiods", type=int, default=3)
    args = ap.parse_args()
    seed_notice(len(args.seeds.split(",")), example="--seeds 20260816,7,101")
    seeds = [int(x) for x in args.seeds.split(",")]

    config.session.load_stock_config()
    live = [(s["code"], s["name"]) for s in config.session.stock_data.get("stocks_kr", [])]
    ext = extend_targets({c for c, _ in live}, 60, mode="random", pool=args.pool)
    dfs, mf, dates, failed = pb.prepare_universe(live + ext, args.days)
    live_codes = list(dfs)

    n_dead = int(args.size * args.dead_frac)
    ddfs, dmf, _dd, _f = pb.prepare_universe(dead_targets(n_dead + 10), args.days)
    dfs.update(ddfs)
    mf.update({c: dmf.get(c, set()) for c in ddfs})
    dead_codes = list(ddfs)
    print(f"[준비] 생존 풀 {len(live_codes)} + 폐지 풀 {len(dead_codes)} → 표본 {args.size}종목"
          f"(폐지 {n_dead} = {args.dead_frac:.0%}) · 거래일 {len(dates)}", flush=True)

    thr = {"BUY_SCORE": config.ANALYSIS_THRESHOLDS["BUY_SCORE"],
           "BUY_RSI_MAX": config.ANALYSIS_THRESHOLDS["BUY_RSI_MAX"],
           "RISE_SCORE": config.ANALYSIS_THRESHOLDS["RISE_SCORE"],
           "WEIGHTS": config.SCORING_WEIGHTS}
    status = pb.precompute_status(dfs, thr)
    new_scale = new_scale_fn_factory(dates, args.days)

    # 같은 시행 번호는 모든 팔이 같은 종목 집합을 쓴다 — 차이는 슬롯·비중뿐이다.
    picks = {}
    for sd in seeds:
        for i in range(args.trials):
            rng = random.Random(sd * 31 + i)
            picks[(sd, i)] = (rng.sample(dead_codes, min(n_dead, len(dead_codes)))
                              + rng.sample(live_codes, args.size - n_dead))

    k = max(1, args.subperiods)
    step = max(1, len(dates) // k)
    W = [("전체", list(dates))]
    W += [(f"구간{i + 1}", dates[i * step:(i + 1) * step if i < k - 1 else len(dates)])
          for i in range(k)]

    for wn, wd in W:
        print(f"\n########## {wn} ({len(wd)} 거래일) ##########")
        print(f"{'팔':<26}{'수익%':>9}{'MDD%':>8}{'MAR':>7}{'PF':>6}{'청산':>6}"
              f"{'상위10%':>9}{'승률%':>7}{'현금%':>7}{'진입최대%':>10}{'1회리스크%':>11}"
              f"{'한도위반':>9}{'승-무-패':>10}")
        base = None
        for label, slots, ratio in ARMS:
            res, ext_diag = [], []
            for sd in seeds:
                for i in range(args.trials):
                    pick = picks[(sd, i)]
                    r = pb.run_portfolio(
                        {c: dfs[c] for c in pick}, {c: status[c] for c in pick}, wd,
                        initial_capital=INITIAL_CAPITAL, slots=slots, invest_ratio=ratio,
                        market_filter_dates={c: mf.get(c, set()) for c in pick},
                        risk_scale_by_date=new_scale())
                    res.append(metrics(r))
                    ext_diag.append((r["avg_cash_ratio"], r["max_buy_weight"],
                                     r["max_buy_risk"], r["risk_cap_breaches"],
                                     r["skipped_qty0"]))
            g = lambda key: float(np.mean([m[key] for m in res]))  # noqa: E731
            d = lambda j: float(np.mean([x[j] for x in ext_diag]))  # noqa: E731
            if base is None:
                base, wl = res, "— (기준)"
            else:
                win = sum(1 for x, y in zip(res, base) if x["ret"] > y["ret"] + 1e-9)
                tie = sum(1 for x, y in zip(res, base) if abs(x["ret"] - y["ret"]) <= 1e-9)
                wl = f"{win}-{tie}-{len(res) - win - tie}"
            print(f"{label:<26}{g('ret'):>9.1f}{g('mdd'):>8.1f}{g('mar'):>7.2f}{g('pf'):>6.2f}"
                  f"{g('n'):>6.0f}{g('top10'):>9.1f}{g('win'):>7.1f}{d(0):>7.1f}{d(1):>10.1f}"
                  f"{d(2):>11.2f}{d(3):>9.1f}{wl:>10}", flush=True)

    print("\n[읽는 법] C(4슬롯×0.333) vs D(3슬롯×0.333)는 진입액이 같고 슬롯만 다르다 "
          "— 이 차이가 '슬롯 하나'의 순수 값어치다.")
    print("           B·C vs A는 슬롯이 같고 진입액만 다르다 — 이것이 '진입액 확대'의 순수 효과다.")
    print("[불변식] 진입최대%가 기초 비중을 넘거나 1회리스크가 "
          f"{getattr(config, 'SYSTEM_RISK_PER_TRADE', 4.0)}%를 넘으면 오버커밋이 "
          "정해둔 상한을 깬 것이다 — 성과 이전에 이쪽을 먼저 볼 것.")


if __name__ == "__main__":
    main()
