"""유니버스 확대의 마지막 관문 — 실제 선정 규칙으로 뽑아도 그 이득이 나오는가.

[왜] 44 → 80종목의 이득(폐지 혼합 4슬롯 기준 211.2% → 336.3%, 구간 셋 모두 승)은
 확장 종목을 **시총 상위 500 안에서 무작위로** 뽑아 쟀다. 그런데 실제로 종목을 추가할
 때는 무작위로 고르지 않는다 — 탐색 메뉴(`modules/manage/discover.py`)가 배제 규칙과
 분산 선정을 거쳐 고른다. **그 규칙으로 고른 80종목에서도 같은 이득이 나오는지는
 확인된 적이 없다.** 나오지 않으면 '80종목으로 늘려라'는 실행 지침이 될 수 없다.

[팔] 모두 80종목 · 4슬롯 · 폐지 20% 혼합. 관심종목 44는 고정이고 확장 36만 다르다.
   · [대조] 시총 상위 500 무작위      — 지금까지 쟀던 방식
   · 탐색 규칙 (배제 + 시총 분산 선정) — 메뉴가 실제로 하는 일 그대로
   · 탐색 규칙 + 적합도 상위          — 메뉴가 추천 순으로 위쪽만 담았을 때
 그리고 기준선으로 44종목(확장 없음)을 함께 둔다 — 이득의 크기를 그 안에서 읽는다.

[정직하게 말해 둘 편향] 탐색 규칙은 **오늘의** 시총·업종·소속부로 고른다. 그것을 10년
 과거에 심으면 미래 정보가 섞인다(오늘 시총 상위 500에 있다는 것 자체가 생존의 증거다).
 무작위 확장도 같은 풀에서 뽑으므로 **두 팔이 같은 편향을 공유한다** — 그래서 이 도구는
 '규칙이 무작위보다 나은가'라는 **상대 비교에만** 답한다. 절대 수익률은 부풀려져 있다.

[실행] python3 tools/audit_discover_universe.py --trials 12
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


def rule_picked(need, pool, seed):
    """탐색 메뉴의 배제 규칙 + 시총 분산 선정을 그대로 거친 후보 목록."""
    from modules.manage import discover
    picked, steps, _defs, n0, n_kept = discover._fetch_candidates(
        target=need, pool=pool, exclude_holding=True, seed=seed)
    print(f"[탐색 규칙] 시총 상위 {n0} → 규칙 통과 {n_kept} → 분산 선정 {len(picked)}", flush=True)
    for label, cnt in steps:
        if cnt:
            print(f"           - {label}: {cnt}종목 제외", flush=True)
    return [(c["code"], c["name"]) for c in picked]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=3650)
    ap.add_argument("--trials", type=int, default=12)
    ap.add_argument("--seeds", default="20260816,7,101")
    ap.add_argument("--size", type=int, default=80)
    ap.add_argument("--slots", type=int, default=None)
    ap.add_argument("--pool", type=int, default=500)
    ap.add_argument("--dead-frac", type=float, default=0.2)
    ap.add_argument("--subperiods", type=int, default=3)
    args = ap.parse_args()
    seed_notice(len(args.seeds.split(",")), example="--seeds 20260816,7,101")
    slots = args.slots or getattr(config, "SYSTEM_MAX_HOLDINGS", 4)
    seeds = [int(x) for x in args.seeds.split(",")]

    config.session.load_stock_config()
    live = [(s["code"], s["name"]) for s in config.session.stock_data.get("stocks_kr", [])]
    n_ext = args.size - len(live)
    print(f"[준비] 관심종목 {len(live)} · 확장 목표 {n_ext} · 슬롯 {slots}", flush=True)

    rnd_t = extend_targets({c for c, _ in live}, n_ext + 25, mode="random", pool=args.pool)
    rule_t = rule_picked(n_ext + 15, args.pool, 20260817)
    dead_t = dead_targets(int(args.size * args.dead_frac) + 10)

    dfs, mf, dates, failed = pb.prepare_universe(
        live + rnd_t + rule_t + dead_t, args.days)
    have = set(dfs)
    live_c = [c for c, _ in live if c in have]
    rnd_c = [c for c, _ in rnd_t if c in have and c not in live_c]
    rule_c = [c for c, _ in rule_t if c in have and c not in live_c]
    dead_c = [c for c, _ in dead_t if c in have]
    print(f"[준비] 사용 {len(dfs)}종목 (관심 {len(live_c)} · 무작위 {len(rnd_c)} · "
          f"규칙 {len(rule_c)} · 폐지 {len(dead_c)}) · 거래일 {len(dates)}", flush=True)

    thr = {"BUY_SCORE": config.ANALYSIS_THRESHOLDS["BUY_SCORE"],
           "BUY_RSI_MAX": config.ANALYSIS_THRESHOLDS["BUY_RSI_MAX"],
           "RISE_SCORE": config.ANALYSIS_THRESHOLDS["RISE_SCORE"],
           "WEIGHTS": config.SCORING_WEIGHTS}
    status = pb.precompute_status(dfs, thr)
    new_scale = new_scale_fn_factory(dates, args.days)

    # 적합도 점수 — 메뉴와 같은 산식(discover._fit_score)에 같은 지표를 먹인다.
    from modules.manage import discover
    fit = {}
    for c in rule_c:
        d = dfs[c]
        try:
            fit[c] = discover._fit_score(discover._enrich({}, d))
        except Exception:
            fit[c] = -99.0
    rule_top = sorted(rule_c, key=lambda c: -fit[c])

    # [폐지 비율을 팔마다 같게 맞춘다] 처음엔 기준선을 '관심 44 + 폐지 16'으로 만들어
    #  총 60종목·폐지 27%가 됐다(팔은 80종목·폐지 20%). 크기도 비율도 달라 비교가 깨진다
    #  — 무작위 확장이 기준선에 지는 것처럼 나왔다. 비율을 고정하고 크기만 다르게 한다.
    n_dead = int(args.size * args.dead_frac)                  # 80 × 0.2 = 16
    n_take = max(0, args.size - n_dead - len(live_c))         # 확장 몫
    n_dead_base = round(len(live_c) / (1 - args.dead_frac) - len(live_c))   # 44 → 11

    def build(kind, rng):
        if kind == "none":                       # 확장 없음 — 관심종목만 (크기가 작다)
            return list(live_c) + rng.sample(dead_c, min(n_dead_base, len(dead_c)))
        if kind == "rnd":
            pool_pick = rng.sample(rnd_c, min(n_take, len(rnd_c)))
        elif kind == "rule":
            pool_pick = rng.sample(rule_c, min(n_take, len(rule_c)))
        else:                                    # 적합도 상위 — 무작위성이 없다
            pool_pick = rule_top[:n_take]
        return list(live_c) + pool_pick + rng.sample(dead_c, min(n_dead, len(dead_c)))

    ARMS = [("[기준선] 확장 없음 (관심 44)", "none"),
            ("[대조] 무작위 확장 → 80", "rnd"),
            ("탐색 규칙 확장 → 80", "rule"),
            ("탐색 규칙 + 적합도 상위 → 80", "top")]
    print(f"[구성] 기준선 {len(live_c) + n_dead_base}종목(관심 {len(live_c)} + 폐지 "
          f"{n_dead_base}) · 확장 팔 {len(live_c) + n_take + n_dead}종목(관심 {len(live_c)} + "
          f"확장 {n_take} + 폐지 {n_dead}) — 폐지 비율 양쪽 {args.dead_frac:.0%}", flush=True)

    k = max(1, args.subperiods)
    step = max(1, len(dates) // k)
    W = [("전체", list(dates))]
    W += [(f"구간{i + 1}", dates[i * step:(i + 1) * step if i < k - 1 else len(dates)])
          for i in range(k)]

    picks = {}
    for sd in seeds:
        for i in range(args.trials):
            for _lbl, kind in ARMS:
                picks[(sd, i, kind)] = build(kind, random.Random(sd * 31 + i))

    for wn, wd in W:
        print(f"\n########## {wn} ({len(wd)} 거래일) ##########")
        print(f"{'팔':<26}{'수익%':>9}{'MDD%':>8}{'MAR':>7}{'PF':>6}{'청산':>6}"
              f"{'상위10%':>9}{'승률%':>7}{'승-무-패':>10}")
        base_res = None
        for label, kind in ARMS:
            res = []
            for sd in seeds:
                for i in range(args.trials):
                    pick = picks[(sd, i, kind)]
                    r = pb.run_portfolio(
                        {c: dfs[c] for c in pick}, {c: status[c] for c in pick}, wd,
                        initial_capital=INITIAL_CAPITAL, slots=slots,
                        market_filter_dates={c: mf.get(c, set()) for c in pick},
                        risk_scale_by_date=new_scale())
                    res.append(metrics(r))
            g = lambda key: float(np.mean([m[key] for m in res]))  # noqa: E731
            if base_res is None:
                base_res, wl = res, "—"
            else:
                win = sum(1 for x, y in zip(res, base_res) if x["ret"] > y["ret"] + 1e-9)
                tie = sum(1 for x, y in zip(res, base_res) if abs(x["ret"] - y["ret"]) <= 1e-9)
                wl = f"{win}-{tie}-{len(res) - win - tie}"
            print(f"{label:<26}{g('ret'):>9.1f}{g('mdd'):>8.1f}{g('mar'):>7.2f}"
                  f"{g('pf'):>6.2f}{g('n'):>6.0f}{g('top10'):>9.1f}{g('win'):>7.1f}"
                  f"{wl:>10}", flush=True)

    print("\n[읽는 법] 규칙 확장이 기준선을 이기면 '80종목으로 늘려라'가 실행 지침이 된다. "
          "무작위 확장까지 이기면 규칙 자체도 값을 하는 것이고, 못 이기면 규칙은 무해할 뿐이다.")


if __name__ == "__main__":
    main()
