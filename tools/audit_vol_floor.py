"""변동성 축소 하한(VOLATILITY_SCALING_MIN)을 올리면 어떻게 되는가 — 마지막 미측정 경로.

[왜] 진입액을 키우는 손잡이를 전수 조사한 결과(2026-08-17) 하나만 직접 잰 적이 없었다.
 사이징 3층 중 변동성층이 95~99.8%를 구속하고, 그 배수는 관심종목 대부분에서 하한(0.40)에
 눌려 있다. 즉 **진입액 = 자산 × 기초비중 × 0.40**이고 하한이 사실상 유일한 승수다.
 하한을 0.5로 올리면 진입액이 그대로 25% 커진다. 0.5는 2026-07 이전의 옛 기본값이다.

[기록된 기울기는 반대쪽을 가리킨다] 0.15·0.25로 **낮추면** 4구간 전부 MDD승·MAR승이라는
 측정이 있다(config 주석, tools/audit_sizing_dials.py). 채택은 실거래 입자 크기 문제로
 보류됐을 뿐 성과는 인하 우위였다. 그러니 상향은 기울기 역방향이다 — 그래도 그 값 자체를
 잰 적은 없으므로 확인한다.

[양성 대조군] 팔에 0.25를 함께 넣는다. 이 격자가 '0.25가 현행보다 낫다'는 기존 기록을
 재현하지 못하면 0.50 결과도 믿을 수 없다. **대조군이 먼저 통과해야 본 팔을 읽는다.**

[씨드] 채택 후보이므로 처음부터 5개로 간다([[audit-seed-robustness]] — 2026-08-17
 오버커밋 감사에서 '세 구간 전부 58% 승'이 씨드를 바꾸자 통째로 뒤집힌 사례가 있다).

[실행] python3 tools/audit_vol_floor.py --trials 12
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
    ("[대조] 하한 0.25 (인하)", 0.25),
    ("[기준선] 하한 0.40 (현행)", 0.40),
    ("하한 0.50 (진입액 +25%)", 0.50),
    ("하한 0.60 (진입액 +50%)", 0.60),
]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=3650)
    ap.add_argument("--trials", type=int, default=12)
    ap.add_argument("--seeds", default="20260816,7,101,31,777")
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
    dfs, mf, dates, _failed = pb.prepare_universe(live + ext, args.days)
    live_codes = list(dfs)

    n_dead = int(args.size * args.dead_frac)
    ddfs, dmf, _dd, _f = pb.prepare_universe(dead_targets(n_dead + 10), args.days)
    dfs.update(ddfs)
    mf.update({c: dmf.get(c, set()) for c in ddfs})
    dead_codes = list(ddfs)
    print(f"[준비] 생존 {len(live_codes)} + 폐지 {len(dead_codes)} → 표본 {args.size}종목 "
          f"(폐지 {n_dead}) · 거래일 {len(dates)} · 씨드 {len(seeds)}개 × {args.trials}회", flush=True)

    thr = {"BUY_SCORE": config.ANALYSIS_THRESHOLDS["BUY_SCORE"],
           "BUY_RSI_MAX": config.ANALYSIS_THRESHOLDS["BUY_RSI_MAX"],
           "RISE_SCORE": config.ANALYSIS_THRESHOLDS["RISE_SCORE"],
           "WEIGHTS": config.SCORING_WEIGHTS}
    # 하한은 allocate_amount에서만 읽힌다 — 지표·상태 캐시는 팔마다 다시 만들 필요가 없다.
    status = pb.precompute_status(dfs, thr)
    new_scale = new_scale_fn_factory(dates, args.days)

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

    orig = getattr(config, "VOLATILITY_SCALING_MIN", 0.4)
    try:
        for wn, wd in W:
            print(f"\n########## {wn} ({len(wd)} 거래일) ##########")
            print(f"{'팔':<26}{'수익%':>9}{'MDD%':>8}{'MAR':>7}{'PF':>6}{'청산':>6}"
                  f"{'상위10%':>9}{'승률%':>7}{'현금%':>7}{'진입최대%':>10}"
                  f"{'1주미달':>8}{'승-무-패':>10}")
            base = None
            for label, floor in ARMS:
                setattr(config, "VOLATILITY_SCALING_MIN", floor)
                res, diag = [], []
                for sd in seeds:
                    for i in range(args.trials):
                        pick = picks[(sd, i)]
                        r = pb.run_portfolio(
                            {c: dfs[c] for c in pick}, {c: status[c] for c in pick}, wd,
                            initial_capital=INITIAL_CAPITAL, slots=4,
                            market_filter_dates={c: mf.get(c, set()) for c in pick},
                            risk_scale_by_date=new_scale())
                        res.append(metrics(r))
                        diag.append((r["avg_cash_ratio"], r["max_buy_weight"],
                                     r["skipped_qty0"]))
                g = lambda key: float(np.mean([m[key] for m in res]))  # noqa: E731
                d = lambda j: float(np.mean([x[j] for x in diag]))     # noqa: E731
                # 기준선은 두 번째 팔(현행 0.40)이다 — 대조군을 기준으로 삼지 않는다.
                if floor == 0.40:
                    base, wl = res, "— (기준)"
                elif base is None:
                    wl = "(대조·후산출)"
                else:
                    win = sum(1 for x, y in zip(res, base) if x["ret"] > y["ret"] + 1e-9)
                    tie = sum(1 for x, y in zip(res, base) if abs(x["ret"] - y["ret"]) <= 1e-9)
                    wl = f"{win}-{tie}-{len(res) - win - tie}"
                print(f"{label:<26}{g('ret'):>9.1f}{g('mdd'):>8.1f}{g('mar'):>7.2f}{g('pf'):>6.2f}"
                      f"{g('n'):>6.0f}{g('top10'):>9.1f}{g('win'):>7.1f}{d(0):>7.1f}"
                      f"{d(1):>10.1f}{d(2):>8.1f}{wl:>10}", flush=True)
                if floor == 0.25:
                    ctrl = res
            # 대조군은 기준선이 나온 뒤에야 승패를 낼 수 있다 — 여기서 따로 찍는다.
            win = sum(1 for x, y in zip(ctrl, base) if x["ret"] > y["ret"] + 1e-9)
            mdd = sum(1 for x, y in zip(ctrl, base) if x["mdd"] > y["mdd"] + 1e-9)
            mar = sum(1 for x, y in zip(ctrl, base) if x["mar"] > y["mar"] + 1e-9)
            print(f"  [양성 대조군 0.25 vs 현행] 수익승 {win}/{len(ctrl)} · "
                  f"MDD승 {mdd}/{len(ctrl)} · MAR승 {mar}/{len(ctrl)}", flush=True)
    finally:
        setattr(config, "VOLATILITY_SCALING_MIN", orig)

    print("\n[읽는 법] 대조군(0.25)이 기존 기록대로 MDD·MAR에서 우위를 재현해야 이 격자를 "
          "신뢰할 수 있다. 재현하지 못하면 0.50·0.60 결과도 읽지 말 것.")
    print("[진입액] 하한이 곧 승수다 — 0.40 → 0.50이면 진입액 +25%, 0.60이면 +50%.")


if __name__ == "__main__":
    main()
