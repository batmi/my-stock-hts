"""구간2에서 추세품질(TQ) 상위가 지는 이유 — 명시적으로 열려 있던 질문.

[배경] 증액 감사가 세 라운드 내내 같은 벽에 부딪혔다([[allocation-equal-weight-lead]]).
 TQ 상위에 더 실으면 전체창은 개선되는데 **구간2(코로나 급락·2022 약세)만 6~8/36으로
 진다.** 국면 조건부(PendDown/Bear 해제)도, 모멘텀 크래시 감지기도 그 칸을 못 움직였다.
 결정적 단서: 같은 구간2에서 '건전일 전 종목 +10%'(TQ와 무관한 증액)는 **이긴다.**
 → 손해의 원인은 증액이 아니라 **TQ 상위를 골라 증액한 것**이다. 그래서 당시 결론이
 "증액 다이얼이 아니라 랭킹·스코어링 축의 문제이니 그쪽에서 먼저 풀라"였고, 미해결이다.

[이 도구가 하는 일] 다이얼을 흔들지 않는다. **진단만 한다.**
 기준선(균등) 운용의 진입→청산 짝을 모아 구간별로 갈라, TQ 밴드마다
   ① 실현 손익 분포(평균·중앙·상위10%·승률)
   ② **청산 사유 분포** — 손절로 잘리나, 시간청산으로 마르나, TS로 나가나
   ③ 보유일수 — 추세가 이어지다 끊긴 건가, 애초에 안 붙은 건가
 를 본다. 구간2에서 TQ 상위가 '더 크게 잘리는지' vs '꼬리가 안 나오는지'를 가르는 것이
 이 도구의 유일한 질문이다. 둘은 처방이 정반대다(전자는 손절·사이징, 후자는 선별).

[실행] python3 tools/audit_tq_regime_failure.py --trials 12
"""
import argparse
import os
import random
import sys
from collections import Counter

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tools.audit_common import SELL_REASONS  # noqa: E402

import config  # noqa: E402
import indicators  # noqa: E402
from modules import portfolio_backtest as pb  # noqa: E402
from tools.audit_defensive_sector import (  # noqa: E402
    INITIAL_CAPITAL, new_scale_fn_factory,
)
from tools.audit_universe import dead_targets, extend_targets  # noqa: E402

BANDS = [(-1e9, 0, "하락(<0)"), (0, 10, "미검증(0~10)"), (10, 30, "약함(10~30)"),
         (30, 60, "양호(30~60)"), (60, 1e9, "강함(60+)")]


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
    seeds = [int(x) for x in args.seeds.split(",")]
    slots = getattr(config, "SYSTEM_MAX_HOLDINGS", 4)

    config.session.load_stock_config()
    live = [(s["code"], s["name"]) for s in config.session.stock_data.get("stocks_kr", [])]
    ext = extend_targets({c for c, _ in live}, 60, mode="random", pool=args.pool)
    n_dead = int(args.size * args.dead_frac)
    dead_t = dead_targets(n_dead + 10)
    dfs, mf, dates, _f = pb.prepare_universe(live + ext + dead_t, args.days)
    dead_set = {c for c, _ in dead_t}
    dead_c = [c for c in dfs if c in dead_set]
    live_c = [c for c in dfs if c not in dead_set]

    thr = {"BUY_SCORE": config.ANALYSIS_THRESHOLDS["BUY_SCORE"],
           "BUY_RSI_MAX": config.ANALYSIS_THRESHOLDS["BUY_RSI_MAX"],
           "RISE_SCORE": config.ANALYSIS_THRESHOLDS["RISE_SCORE"],
           "WEIGHTS": config.SCORING_WEIGHTS}
    status = pb.precompute_status(dfs, thr)
    new_scale = new_scale_fn_factory(dates, args.days)

    lb = config.INDICATOR_PARAMS.get("TREND_QUALITY_LOOKBACK", 90)
    tq_map = {}
    for c, df in dfs.items():
        col = indicators.rolling_trend_quality(df["close"], lb) \
            if hasattr(indicators, "rolling_trend_quality") else None
        if col is None:
            from tools.audit_score_weighted_sizing import rolling_trend_quality
            col = rolling_trend_quality(df["close"], lb)
        tq_map[c] = dict(zip((str(d) for d in df["date"]), col))

    k = max(1, args.subperiods)
    step = max(1, len(dates) // k)
    seg_of = {}
    for i in range(k):
        for d in dates[i * step:(i + 1) * step if i < k - 1 else len(dates)]:
            seg_of[d] = f"구간{i + 1}"

    print(f"[준비] {len(dfs)}종목(폐지 {len(dead_c)}) · 거래일 {len(dates)} · "
          f"TQ 룩백 {lb}일 · 슬롯 {slots}", flush=True)

    recs = []      # (구간, TQ, 손익%, 청산사유, 보유일)
    for sd in seeds:
        for i in range(args.trials):
            rng = random.Random(sd * 31 + i)
            pick = (rng.sample(dead_c, min(n_dead, len(dead_c)))
                    + rng.sample(live_c, args.size - n_dead))
            r = pb.run_portfolio(
                {c: dfs[c] for c in pick}, {c: status[c] for c in pick}, dates,
                initial_capital=INITIAL_CAPITAL, slots=slots,
                market_filter_dates={c: mf.get(c, set()) for c in pick},
                risk_scale_by_date=new_scale())
            seq = {}
            for t in r["trades"]:
                seq.setdefault(t["code"], []).append(t)
            for code, ts in seq.items():
                open_day = None
                for t in ts:
                    if t["reason"] not in SELL_REASONS:
                        if open_day is None:
                            open_day = t["date"]
                    elif open_day is not None:
                        q = tq_map.get(code, {}).get(str(open_day))
                        if q is not None and np.isfinite(q):
                            recs.append((seg_of.get(open_day, "?"), float(q),
                                         float(t.get("profit", 0) or 0),
                                         t["reason"], int(t.get("days", 0) or 0)))
                        open_day = None
    print(f"[표본] 진입→청산 {len(recs):,}건", flush=True)

    segs = [f"구간{i + 1}" for i in range(k)]
    print("\n[1] 구간별 · TQ 밴드별 실현 손익 — TQ가 결과를 가르는가")
    for sg in segs:
        sub = [r for r in recs if r[0] == sg]
        print(f"\n  ── {sg} (진입 {len(sub):,}건)")
        print(f"  {'밴드':<16}{'건수':>7}{'평균%':>9}{'중앙%':>9}{'상위10%':>9}"
              f"{'승률%':>8}{'평균보유일':>11}")
        for lo, hi, lab in BANDS:
            seg = [r for r in sub if lo <= r[1] < hi]
            if len(seg) < 20:
                print(f"  {lab:<16}{len(seg):>7}   (표본 부족)")
                continue
            a = np.array([r[2] for r in seg])
            top = np.sort(a)[::-1][:max(1, len(a) // 10)]
            print(f"  {lab:<16}{len(a):>7}{a.mean():>9.2f}{np.median(a):>9.2f}"
                  f"{top.mean():>9.1f}{(a > 0).mean() * 100:>8.1f}"
                  f"{np.mean([r[4] for r in seg]):>11.1f}")

    print("\n[2] 구간2에서 TQ 강함은 '잘리는가' 아니면 '마르는가' — 청산 사유 분포(%)")
    reasons = ["ATR손절", "손절", "본전청산", "시간청산", "트레일링스탑", "점수하락"]
    print(f"  {'구간·밴드':<22}" + "".join(f"{r:>10}" for r in reasons))
    for sg in segs:
        for lo, hi, lab in ((30, 60, "양호"), (60, 1e9, "강함")):
            seg = [r for r in recs if r[0] == sg and lo <= r[1] < hi]
            if len(seg) < 20:
                continue
            cnt = Counter(r[3] for r in seg)
            row = "".join(f"{cnt.get(r, 0) / len(seg) * 100:>10.1f}" for r in reasons)
            print(f"  {f'{sg} TQ {lab}({len(seg)})':<22}{row}")

    print("\n[읽는 법] 구간2에서 TQ 강함의 손절 비중이 다른 구간보다 크면 '잘린다'(처방: 손절·"
          "사이징). 손절은 비슷한데 상위10%만 얇으면 '마른다'(처방: 선별·랭킹). 둘은 정반대다.")


if __name__ == "__main__":
    main()
