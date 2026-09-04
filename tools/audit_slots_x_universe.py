"""유니버스를 80으로 늘리면 4슬롯이 여전히 최적인가 — 두 축의 상호작용.

[왜 지금] 두 결론이 각각 따로 서 있고, 그 사이는 비어 있다.
   · `audit_universe` 축 A: 44 → 80종목에서 수익 276 → 606%. 단 **슬롯을 4로 고정**하고
     종목 수만 훑었다.
   · 슬롯 수 재검증(2/3/5 기각, [[seed-slot-sizing]]): **44종목에서의 결론**이다.
 후보가 두 배가 되면 같은 4칸을 두고 경쟁하는 종목이 두 배가 된다. 그러면 ① 칸마다
 더 좋은 종목이 앉으니 슬롯을 늘릴 이유가 줄 수도 있고, ② 좋은 후보를 놓치는 일이
 늘어 슬롯을 늘릴 이유가 커질 수도 있다. **방향이 선험적으로 정해지지 않는다.**

[왜 이 순서인가] 파이3 실측(2026-08-17)으로 80종목이 하드웨어상 가능하다는 것이
 확인됐다(혼잡 시 54초 < 대기 60초). 즉 이 축은 이제 '종이 위의 수치'가 아니라
 **실행 직전의 결정**이다. 유니버스만 늘리고 슬롯을 그대로 두면 이득의 일부를 놓칠 수
 있으므로, 늘리기 전에 함께 물어야 한다.

[격자] 유니버스 {44, 80} × 슬롯 {3, 4, 5, 6}. 기준선은 44종목·4슬롯(현행).
 각 칸에서 **그 유니버스 안의 최적 슬롯**을 보고, 44에서의 최적과 80에서의 최적이
 같은지 다른지를 본다. 그것이 이 도구가 답하는 유일한 질문이다.

[통제] 확장 풀은 `--extend-mode random`(시총 상위 500 안에서 무작위)을 기본으로 쓴다.
 marcap 상위 순으로 채우면 '지금 큰 종목을 과거에 심는' 생존 편향이 이 축에서 최대로
 작동한다. 절대 수익률은 어차피 2배 부풀려져 있으므로([[survivorship-premium-2x]])
 **칸 사이의 상대 비교만** 읽을 것.

[생존 편향 — 이 축에서는 면제가 없다] 확장 풀은 '오늘까지 살아남은' 종목이다. 절대
 수익률이 2배 부풀려진다는 것은 알려져 있고([[survivorship-premium-2x]]) 보통은 다이얼
 **순위**가 두 풀에서 같아 결론이 안전했다. 그러나 **집중도(슬롯 축소)는 생존 편향을
 정확히 증폭하는 방향**이다 — 살아남은 승자에 더 크게 베팅하는 것이기 때문이다. 그래서
 이 도구는 `--dead-frac`으로 폐지 종목을 섞어 같은 격자를 다시 돌린다. 3슬롯 우위가
 폐지 혼합에서도 살아남아야 진짜다.

[실행] python3 tools/audit_slots_x_universe.py --trials 12 --seeds 3
       python3 tools/audit_slots_x_universe.py --dead-frac 0.2 --sizes 80 --slots 3,4,5
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
from tools.audit_universe import extend_targets  # noqa: E402
from tools.audit_common import seed_notice, windows as audit_windows  # noqa: E402


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=3650)
    ap.add_argument("--trials", type=int, default=12)
    ap.add_argument("--seeds", default="20260816,7,101")
    ap.add_argument("--sizes", default="44,80")
    ap.add_argument("--slots", default="3,4,5,6")
    ap.add_argument("--extend-mode", default="random", choices=("random", "marcap"))
    ap.add_argument("--extend-pool", type=int, default=500)
    ap.add_argument("--subperiods", type=int, default=3)
    ap.add_argument("--dead-frac", type=float, default=0.0,
                    help="표본에서 상장폐지 종목이 차지할 비율(실제 폐지율은 대략 0.2)")
    args = ap.parse_args()
    seed_notice(len(args.seeds.split(",")), example="--seeds 20260816,7,101")
    seeds = [int(x) for x in args.seeds.split(",")]
    sizes = [int(x) for x in args.sizes.split(",")]
    slot_list = [int(x) for x in args.slots.split(",")]

    config.session.load_stock_config()
    live = [(s["code"], s["name"]) for s in config.session.stock_data.get("stocks_kr", [])]
    need = max(sizes) - len(live) + 20            # 준비 실패분 여유
    ext = extend_targets({c for c, _ in live}, max(0, need),
                         mode=args.extend_mode, pool=args.extend_pool)
    dfs, mf, dates, failed = pb.prepare_universe(live + ext, args.days)
    live_codes = list(dfs)
    dead_codes = []
    if args.dead_frac > 0:
        from tools.audit_universe import dead_targets
        need = int(max(sizes) * args.dead_frac) + 10
        ddfs, dmf, _dd, _f = pb.prepare_universe(dead_targets(need), args.days)
        dfs.update(ddfs)
        mf.update({c: dmf.get(c, set()) for c in ddfs})
        dead_codes = list(ddfs)
        print(f"[준비] 폐지 풀 {len(dead_codes)}종목 혼합 (목표 비율 {args.dead_frac:.0%})",
              flush=True)
    print(f"[준비] 관심종목 {len(live)} + 확장 {len(ext)}({args.extend_mode}) "
          f"→ 사용 {len(dfs)}종목 · 거래일 {len(dates)}" + (f" · 실패 {failed}" if failed else ""),
          flush=True)

    thr = {"BUY_SCORE": config.ANALYSIS_THRESHOLDS["BUY_SCORE"],
           "BUY_RSI_MAX": config.ANALYSIS_THRESHOLDS["BUY_RSI_MAX"],
           "RISE_SCORE": config.ANALYSIS_THRESHOLDS["RISE_SCORE"],
           "WEIGHTS": config.SCORING_WEIGHTS}
    status = pb.precompute_status(dfs, thr)
    new_scale = new_scale_fn_factory(dates, args.days)

    W = audit_windows(dates, args.subperiods, whole=True)

    # 같은 시행 번호는 같은 종목 집합을 쓴다 — 44는 80의 부분집합으로 뽑아
    #  '종목이 늘어난 것' 외의 차이를 없앤다.
    picks = {}
    for sd in seeds:
        for i in range(args.trials):
            rng = random.Random(sd * 31 + i)
            if dead_codes:
                # 폐지 종목을 목표 비율만큼 섞는다 — 표본 크기는 그대로 유지한다.
                nd = int(max(sizes) * args.dead_frac)
                big = (rng.sample(dead_codes, min(nd, len(dead_codes)))
                       + rng.sample(live_codes, min(max(sizes) - nd, len(live_codes))))
                rng.shuffle(big)
            else:
                big = rng.sample(live_codes, min(max(sizes), len(live_codes)))
            for n in sizes:
                picks[(sd, i, n)] = big[:n]

    base_key = (min(sizes), 4 if 4 in slot_list else slot_list[0])
    for wn, wd in W:
        print(f"\n########## {wn} ({len(wd)} 거래일) ##########")
        print(f"{'유니버스×슬롯':<16}{'수익%':>9}{'MDD%':>8}{'MAR':>7}{'PF':>6}{'청산':>6}"
              f"{'상위10%':>9}{'승률%':>7}{'기준선 대비':>12}")
        cell = {}
        for n in sizes:
            for sl in slot_list:
                res = []
                for sd in seeds:
                    for i in range(args.trials):
                        pick = picks[(sd, i, n)]
                        r = pb.run_portfolio(
                            {c: dfs[c] for c in pick}, {c: status[c] for c in pick}, wd,
                            initial_capital=INITIAL_CAPITAL, slots=sl,
                            market_filter_dates={c: mf.get(c, set()) for c in pick},
                            risk_scale_by_date=new_scale())
                        res.append(metrics(r))
                cell[(n, sl)] = res
        base = cell[base_key]
        for n in sizes:
            for sl in slot_list:
                res = cell[(n, sl)]
                g = lambda key: float(np.mean([m[key] for m in res]))  # noqa: E731
                if (n, sl) == base_key:
                    wl = "— (기준선)"
                else:
                    win = sum(1 for x, y in zip(res, base) if x["ret"] > y["ret"] + 1e-9)
                    tie = sum(1 for x, y in zip(res, base) if abs(x["ret"] - y["ret"]) <= 1e-9)
                    wl = f"{win}-{tie}-{len(res) - win - tie}"
                print(f"{f'{n}종목 × {sl}슬롯':<16}{g('ret'):>9.1f}{g('mdd'):>8.1f}"
                      f"{g('mar'):>7.2f}{g('pf'):>6.2f}{g('n'):>6.0f}{g('top10'):>9.1f}"
                      f"{g('win'):>7.1f}{wl:>12}", flush=True)
        # 이 도구의 유일한 질문: 유니버스마다 최적 슬롯이 같은가
        print("  [최적 슬롯] " + " · ".join(
            f"{n}종목 → MAR {max(slot_list, key=lambda s: np.mean([m['mar'] for m in cell[(n, s)]]))}슬롯"
            f" / 수익 {max(slot_list, key=lambda s: np.mean([m['ret'] for m in cell[(n, s)]]))}슬롯"
            for n in sizes))

    print("\n[읽는 법] 44와 80의 최적 슬롯이 같으면 두 축은 독립이다 — 유니버스만 늘리면 된다. "
          "다르면 유니버스를 늘릴 때 슬롯도 함께 옮겨야 하고, 따로 정한 기존 결론은 못 쓴다.")
    print("[주의] 확장 풀에 생존 편향이 있어 절대 수익률은 부풀려져 있다. 칸 사이 비교만 읽을 것.")


if __name__ == "__main__":
    main()
