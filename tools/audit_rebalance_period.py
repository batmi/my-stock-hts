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

[!! 복리 체인 하나는 추첨이다 · 2026-09-09] 아래 2단계는 팔마다 **최종 배수 한 개**를
 낸다. 그런데 체인은 복리라, 한 창에서 벌어진 차이가 이후 전 구간에 곱해진다 — 초반
 한 번의 운이 9년치 결론을 만든다. 실제로 씨드 셋에서 순위가 1-5-5 / 2-2-3 / 1-1-4 로
 흩어져 **판정 불가**가 나왔다(도구 자신도 "풀 씨드마다 크게 흩어진다"고 경고한다).
 기록된 '고정 대비 10/12승'은 **창마다 승패를 센 것**이라 방법이 다르다 — 두 수치를
 직접 맞대지 말 것.
 → `--windowed` 가 그 방법이다. 창마다 **같은 자본에서 다시 시작**해 팔끼리 짝비교하므로
   경로 의존이 끊기고, 표본이 씨드당 1개에서 창 개수만큼으로 늘어난다.

[실행] python3 tools/audit_rebalance_period.py --turnover-only   # 1단계: 교체율만
       python3 tools/audit_rebalance_period.py                   # 2단계: 복리 체인
       python3 tools/audit_rebalance_period.py --windowed        # 2단계': 창별 승패(권장)
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
from tools.audit_common import seed_notice  # noqa: E402


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
    ap.add_argument("--windowed", action="store_true",
                    help="복리 체인 대신 창마다 승패를 센다(기록된 10/12승과 같은 방식)")
    ap.add_argument("--eval-months", type=int, default=9,
                    help="--windowed 의 평가 창 길이(개월). 9면 9년 구간에 12창")
    ap.add_argument("--fixed-draws", type=int, default=5,
                    help="--windowed: '고정' 대조군 장수. 고정 팔도 한 번의 추첨이라 "
                         "여러 앵커에서 뽑아 분포로 본다")
    args = ap.parse_args()
    seed_notice(len(args.pool_seeds.split(",")), example="--pool-seeds 20260817,31,777")
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

        fixed = fit_pick(args.start)

        if args.windowed:
            # ── 2단계': 창별 승패. 창마다 같은 자본에서 다시 시작해 경로 의존을 끊는다.
            #  체인은 복리라 초반 한 창의 운이 9년치 결론을 만든다(머리말 참고).
            grid = max(21, args.eval_months * 21)

            def run_window(wd, uni):
                if not uni:
                    return None
                r = pb.run_portfolio(
                    {c: dfs[c] for c in uni}, {c: status[c] for c in uni}, wd,
                    initial_capital=INITIAL_CAPITAL, slots=slots,
                    market_filter_dates={c: mf.get(c, set()) for c in uni},
                    risk_scale_by_date=new_scale())
                return r["final_asset"] / INITIAL_CAPITAL

            def uni_at(mo, w0):
                """이 창이 시작될 때 그 주기가 들고 있는 목록(가장 최근 재조정 시점)."""
                step = max(1, mo * 21)
                mark = args.start + ((w0 - args.start) // step) * step
                return fit_pick(mark)

            #  [고정 팔도 추첨이다] 종전엔 시작 시점 한 날의 적합도 상위 44종목 **하나**를
            #   기준선으로 썼다. 그 한 장이 좋으면 재조정 전부가 지고, 나쁘면 전부가 이긴다 —
            #   재조정의 값어치가 아니라 그 장의 운을 재게 된다.
            #   그래서 **앵커를 앞으로 물려 가며 여러 장** 뽑아 분포로 본다. 앵커는 모두
            #   첫 창보다 앞이라 선견이 없다(21거래일씩 뒤로, 60 아래로는 안 간다).
            fixed_anchors = [a for a in
                             (args.start - i * 21 for i in range(max(1, args.fixed_draws)))
                             if a >= 60]
            fixed_unis = [fit_pick(a) for a in fixed_anchors]
            #  [대조] 적합도 정렬 자체가 값을 하는가 — 아무 44종목을 고정으로 들고 간다.
            rnd_unis = []
            for d in range(max(1, args.fixed_draws)):
                pick = list(dfs)
                random.Random(f"{ps}|fixedrnd|{d}").shuffle(pick)
                rnd_unis.append(pick[:args.size])

            wins = {mo: [0, 0, 0] for mo in months}     # 승·무·패 (기준선 고정 대비)
            rets = {mo: [] for mo in months}
            base_rets = []
            draw_rets = [[] for _ in fixed_unis]
            rnd_rets = [[] for _ in rnd_unis]
            n_win = 0
            for w0 in range(args.start, end, grid):
                wd = dates[w0:min(w0 + grid, end)]
                if len(wd) < 20:
                    break          # 꼬리 조각은 버린다 — 창 길이가 다르면 비교가 안 된다
                b = run_window(wd, fixed)
                if b is None:
                    continue
                n_win += 1
                base_rets.append(b)
                for mo in months:
                    v = run_window(wd, uni_at(mo, w0))
                    if v is None:
                        continue
                    rets[mo].append(v)
                    d = v - b
                    wins[mo][0 if d > 1e-9 else (1 if abs(d) <= 1e-9 else 2)] += 1
                for k, uni in enumerate(fixed_unis):
                    v = run_window(wd, uni)
                    if v is not None:
                        draw_rets[k].append(v)
                for k, uni in enumerate(rnd_unis):
                    v = run_window(wd, uni)
                    if v is not None:
                        rnd_rets[k].append(v)

            print(f"\n[2'] 창별 승패 — {args.eval_months}개월 창 {n_win}개 · 창마다 "
                  f"같은 자본에서 재시작 · {args.size}종목 · {slots}슬롯")
            print(f"{'팔':<26}{'평균 배수':>11}{'중앙 배수':>11}{'승-무-패':>12}")
            print(f"{'[기준선] 고정':<26}{np.mean(base_rets):>10.3f}x"
                  f"{np.median(base_rets):>10.3f}x{'— (기준)':>12}")
            for mo in months:
                w, t, l = wins[mo]
                print(f"{f'적합도 {mo}개월 재조정':<26}{np.mean(rets[mo]):>10.3f}x"
                      f"{np.median(rets[mo]):>10.3f}x{f'{w}-{t}-{l}':>12}", flush=True)
            def _spread(label, groups, note):
                vals = sorted(float(np.mean(g)) for g in groups if g)
                if not vals:
                    return
                print(f"{label:<26}{vals[0]:>10.3f}x{vals[len(vals) // 2]:>10.3f}x"
                      f"{vals[-1]:>10.3f}x   {note}")

            print(f"\n{'대조군 (장별 평균 배수)':<26}{'최악':>11}{'중앙':>11}{'최선':>11}")
            _spread("[대조] 적합도 고정 다앵커", draw_rets,
                    f"앵커 {len(draw_rets)}장 · 기준선도 이 분포의 한 장이다")
            _spread("[대조] 무작위 고정", rnd_rets,
                    f"{len(rnd_rets)}장 · 적합도 정렬 자체의 값어치를 가른다")
            print("  [읽는 법] 재조정 팔이 '적합도 고정' 분포의 **최선**을 넘어야 "
                  "'갈아끼우는 행위'가 값을 한 것이다. 분포 안에 들면 그것은 앵커 운이다.")
            print("  [읽는 법] '적합도 고정'이 '무작위 고정'을 넘지 못하면 적합도 정렬 자체가 "
                  "값을 못 하는 것이라, 주기 논의는 그 위에서 무의미하다.")
            print("  [읽는 법] 창마다 자본을 되돌리므로 배수는 '그 창의 수익률'이다. "
                  "복리 체인의 최종 배수와 직접 비교하지 말 것.")
            print("  [주의] 창 개수가 곧 표본이다. 씨드 하나에서 12창이면 판정 근거로 얇다 "
                  "— 풀 씨드 셋에서 같은 방향이 나와야 한다.")
            continue

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
