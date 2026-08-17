"""관심종목을 얼마나 자주 다시 고를 것인가 — 워크포워드 결론의 직접 후속.

[왜] 2026-08-17 워크포워드가 '몇 개를 담느냐'(크기)를 기각하고 '무엇을 담느냐'(적합도
 정렬)를 진짜 레버로 세웠다(세 풀 × 두 크기 6/6 압승). 그런데 **얼마나 자주 다시
 정렬해야 하는가**는 재지 않았다 — 12개월 하나로만 돌렸다. 실행 지침("적합도 상위로
 갈아끼워라")이 성립하려면 이 주기가 정해져야 한다.

[먼저 교체율부터 센다] 저장소 규약: 기간·경계 축은 백테스트 전에 '그 조건이 며칠에
 걸리나'부터 센다([[residual-dials-closed]]). 여기서는 **주기를 줄이면 목록이 실제로
 바뀌는가**다. 3개월 재조정이 12개월과 거의 같은 목록을 낳으면 축 자체가 작고, 그러면
 36쌍짜리 백테스트를 돌릴 이유가 없다.

[비교 창을 맞춘다] 주기가 다르면 재조정 횟수가 달라 체인 구간이 어긋난다. 모든 팔이
 **같은 [시작, 끝]** 을 덮도록 마지막 창을 잘라 맞춘다. 이것을 안 하면 주기가 짧은 팔이
 더 긴 기간을 굴려 최종 배수가 부풀려진다.

[실행] python3 tools/audit_rebalance_period.py --turnover-only   # 1단계: 교체율만
       python3 tools/audit_rebalance_period.py                   # 2단계: 전체
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
    INITIAL_CAPITAL, new_scale_fn_factory,
)
from modules.manage.discover import _fit_score  # noqa: E402
from tools.audit_discover_fit import fit_at, rule_pool  # noqa: E402
from tools.audit_universe import dead_targets  # noqa: E402


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=3650)
    ap.add_argument("--pool", type=int, default=500)
    ap.add_argument("--pool-size", type=int, default=170)
    ap.add_argument("--dead", type=int, default=45)
    ap.add_argument("--size", type=int, default=44)
    ap.add_argument("--months", default="3,6,12,24")
    ap.add_argument("--pool-seeds", default="20260817,31,777")
    ap.add_argument("--slots", type=int, default=None)
    ap.add_argument("--start", type=int, default=250, help="워밍업 거래일")
    ap.add_argument("--turnover-only", action="store_true")
    args = ap.parse_args()
    slots = args.slots or getattr(config, "SYSTEM_MAX_HOLDINGS", 4)
    months = [int(x) for x in args.months.split(",")]
    pool_seeds = [int(x) for x in args.pool_seeds.split(",")]

    thr = {"BUY_SCORE": config.ANALYSIS_THRESHOLDS["BUY_SCORE"],
           "BUY_RSI_MAX": config.ANALYSIS_THRESHOLDS["BUY_RSI_MAX"],
           "RISE_SCORE": config.ANALYSIS_THRESHOLDS["RISE_SCORE"],
           "WEIGHTS": config.SCORING_WEIGHTS}

    for ps in pool_seeds:
        targets = rule_pool(args.pool, args.pool_size, ps) + dead_targets(args.dead)
        dfs, mf, dates, failed = pb.prepare_universe(targets, args.days)
        print(f"\n\n=========== 풀 씨드 {ps} · {len(dfs)}종목 · 거래일 {len(dates)} "
              f"(실패 {len(failed)}) ===========", flush=True)
        pos = {c: {str(d): i for i, d in enumerate(df["date"])} for c, df in dfs.items()}
        end = len(dates)

        def rank_at(m):
            """그 시점까지의 데이터로만 매긴 적합도 순위."""
            day = dates[m]
            rows = []
            for c, df in dfs.items():
                i = pos[c].get(day)
                if i is None:
                    continue
                feat = fit_at(df, i)
                if feat is not None:
                    rows.append((_fit_score(feat), c))
            rows.sort(key=lambda x: -x[0])
            return [c for _f, c in rows]

        # ── 1단계: 교체율. 주기를 줄이면 목록이 정말 바뀌는가?
        print(f"\n[1] 재조정 교체율 — 상위 {args.size}종목이 직전 대비 몇 종목 바뀌나")
        print(f"{'주기':<10}{'재조정':>7}{'교체 중앙':>10}{'교체 평균':>10}{'교체율%':>9}"
              f"{'연간 교체':>10}")
        cache = {}
        for mo in months:
            step = max(1, mo * 21)
            marks = list(range(args.start, end, step))
            sets = []
            for m in marks:
                if m not in cache:
                    cache[m] = rank_at(m)
                sets.append(set(cache[m][:args.size]))
            ch = [len(sets[i] - sets[i - 1]) for i in range(1, len(sets))]
            if not ch:
                continue
            per_year = float(np.mean(ch)) * (12.0 / mo)
            print(f"{f'{mo}개월':<10}{len(marks):>7}{np.median(ch):>10.0f}"
                  f"{np.mean(ch):>10.1f}{np.mean(ch) / args.size * 100:>9.1f}"
                  f"{per_year:>10.1f}", flush=True)
        print("  [읽는 법] 교체율이 주기와 무관하게 비슷하면 '자주 보나 가끔 보나 같은 목록'이라 "
              "축이 작다. 짧은 주기에서 교체가 크게 늘면 그만큼 회전 비용도 늘어난다.")
        if args.turnover_only:
            continue

        # ── 2단계: 성과. 모든 팔이 같은 [start, end)를 덮게 맞춘다.
        status = pb.precompute_status(dfs, thr)
        new_scale = new_scale_fn_factory(dates, args.days)

        def run_chain(mo, pick):
            step = max(1, mo * 21)
            cap = float(INITIAL_CAPITAL)
            for m in range(args.start, end, step):
                wd = dates[m:min(m + step, end)]
                if len(wd) < 5:
                    break
                uni = pick(m)
                if not uni:
                    continue
                r = pb.run_portfolio(
                    {c: dfs[c] for c in uni}, {c: status[c] for c in uni}, wd,
                    initial_capital=cap, slots=slots,
                    market_filter_dates={c: mf.get(c, set()) for c in uni},
                    risk_scale_by_date=new_scale())
                cap = r["final_asset"]
            return cap / INITIAL_CAPITAL

        def fit_pick(m):
            if m not in cache:
                cache[m] = rank_at(m)
            return cache[m][:args.size]

        years = (end - args.start) / 252.0
        print(f"\n[2] 성과 — 같은 구간 {args.start}~{end}({years:.1f}년) 복리 연결 · "
              f"{args.size}종목 · {slots}슬롯")
        print(f"{'팔':<26}{'최종 배수':>11}{'연환산%':>9}")
        rows = []
        for mo in months:
            mult = run_chain(mo, fit_pick)
            rows.append((f"적합도 {mo}개월 재조정", mult))
            print(f"{rows[-1][0]:<26}{mult:>10.2f}x"
                  f"{((mult ** (1 / years) - 1) * 100):>9.1f}", flush=True)
        # 고정: 시작 시점에 한 번 뽑고 끝까지 간다 — 재조정의 값어치는 이것 대비로 읽는다.
        fixed = fit_pick(args.start)
        mult = run_chain(999, lambda _m: fixed)
        print(f"{'[기준선] 재조정 없음(고정)':<26}{mult:>10.2f}x"
              f"{((mult ** (1 / years) - 1) * 100):>9.1f}", flush=True)
        rnd = list(dfs)
        random.Random(ps).shuffle(rnd)
        mult = run_chain(999, lambda _m: rnd[:args.size])
        print(f"{'[대조] 무작위 고정':<26}{mult:>10.2f}x"
              f"{((mult ** (1 / years) - 1) * 100):>9.1f}", flush=True)

    print("\n[읽는 법] 재조정 주기들이 '고정'을 못 이기면 갈아끼우는 행위 자체가 값을 못 하는 "
          "것이고, 이기면 그중 가장 빠른 회복 구간이 권장 주기다.")
    print("[주의] 풀 씨드마다 최종 배수가 크게 흩어진다 — 세 풀에서 같은 순서가 나와야 판정한다.")


if __name__ == "__main__":
    main()
