"""추세품질(TQ) 최상위 밴드 안쪽에 구조가 있는가 — 밴드 정의의 해상도를 잰다.

[왜] 10년 95,230 종목·일의 TQ 분포를 재 보니 밴드가 이렇게 앉아 있다.
     하락 45.1% · 미검증 19.2% · 약함 9.4% · 양호 7.8% · **강함(60+) 18.5%**
 그런데 강함 밴드 하나가 **60부터 33,834까지** 전부를 담는다(90분위 128 · 99분위 1,078).
 TQ 61인 종목과 TQ 1,000인 종목이 같은 이름으로 묶여 있다는 뜻이다.

[무엇이 걸려 있나] 랭킹은 연속값을 쓰므로 **매매 판단의 결함은 아니다.** 걸려 있는 것은
 두 가지다. ① 화면·도움말이 읽는 사람에게 주는 정보. ② 지금까지 밴드 언어로 기록한
 감사 결론들 — "구간3에서 TQ 강함 +0.85%"는 60짜리와 33,834짜리를 한 바구니에 넣고
 낸 평균이다. 그 안이 단조롭지 않으면 그 문장은 평균이 가린 서로 다른 두 이야기다.

[유일한 질문] 60+ 안에서 TQ가 높을수록 성과가 좋은가(단조), 아니면 어느 지점에서
 꺾이는가(포물선 급등의 역전)?
   · 단조롭다 → 밴드가 해상도를 잃고 있을 뿐, 신호 자체는 건강하다. 표시 문제.
   · 꺾인다   → **종목 축의 모멘텀 크래시**다. 증액 감사가 시장 급락 감지기로 구간2를
     못 움직였던 것이, 원인이 시장이 아니라 종목 과열이었기 때문일 수 있다.

[측정 설계] 고정 절단(60~100/100~300/300~1000/1000+)과 **60+ 안의 등분위 4등분**을
 함께 낸다. 고정 절단은 읽기 쉽지만 최상단 표본이 얇아질 수 있고, 등분위는 표본이
 균등하지만 경계가 해석하기 어렵다. **둘이 같은 방향을 가리켜야** 결론으로 삼는다.

[진단 전용] 다이얼을 하나도 바꾸지 않는다. 기준선(현행 설정) 운용의 진입→청산 짝을
 모아 TQ로 가를 뿐이다. `tools/audit_tq_regime_failure.py`와 같은 표본·같은 수집 경로다.

[실행] python3 tools/audit_tq_band_structure.py --trials 12
"""
import argparse
import os
import random
import sys
from collections import Counter

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tools.audit_common import SELL_REASONS, seed_notice  # noqa: E402

import config  # noqa: E402
from modules import portfolio_backtest as pb  # noqa: E402
from tools.audit_defensive_sector import (  # noqa: E402
    INITIAL_CAPITAL, new_scale_fn_factory,
)
from tools.audit_universe import dead_targets, extend_targets  # noqa: E402

# 현행 밴드 아래쪽은 그대로 두고, 최상위만 쪼갠다.
COARSE = [(-1e9, 0, "하락(<0)"), (0, 10, "미검증(0~10)"), (10, 30, "약함(10~30)"),
          (30, 60, "양호(30~60)")]
FINE = [(60, 100, "강함 60~100"), (100, 300, "강함 100~300"),
        (300, 1000, "강함 300~1k"), (1000, 1e9, "강함 1k+")]
MIN_N = 20              # 이보다 적으면 판정하지 않는다 — 표본이 없으면 없다고 말한다


def describe(seg):
    a = np.array([r[2] for r in seg], dtype=float)
    top = np.sort(a)[::-1][:max(1, len(a) // 10)]
    return (len(a), a.mean(), float(np.median(a)), top.mean(),
            (a > 0).mean() * 100, float(np.mean([r[4] for r in seg])))


def row(lab, seg, width=18):
    if len(seg) < MIN_N:
        return f"  {lab:<{width}}{len(seg):>7}   (표본 부족 — 판정하지 않는다)"
    n, mean, med, top, win, hold = describe(seg)
    return (f"  {lab:<{width}}{n:>7}{mean:>9.2f}{med:>9.2f}{top:>9.1f}"
            f"{win:>8.1f}{hold:>11.1f}")


HEAD = f"  {'밴드':<18}{'건수':>7}{'평균%':>9}{'중앙%':>9}{'상위10%':>9}{'승률%':>8}{'평균보유일':>11}"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=3650)
    ap.add_argument("--trials", type=int, default=12)
    ap.add_argument("--seeds", default="20260816,7,101")
    ap.add_argument("--size", type=int, default=44)
    ap.add_argument("--pool", type=int, default=500)
    ap.add_argument("--dead-frac", type=float, default=0.2)
    ap.add_argument("--subperiods", type=int, default=3)
    # [비대칭 실행] KRX 시총·폐지 목록 원본이 죽으면(2026-08-18: raw.githubusercontent 503)
    #  확장·폐지 풀을 못 만든다. 그때는 관심종목만으로 돌릴 수 있게 열어 두되, 결과의
    #  성격이 달라진다는 것을 잊지 말 것 — 폐지 종목은 '극단 TQ 뒤 붕괴'의 가장 결정적인
    #  사례다. 빼고 재면 **꺾임이 보이면 보수적으로 참(실제는 더 크다), 안 보이면 판정 불가**다.
    ap.add_argument("--live-only", action="store_true",
                    help="관심종목만 사용 (확장·폐지 풀 없이 — 목록 원본이 죽었을 때)")
    args = ap.parse_args()
    seed_notice(len(args.seeds.split(",")), example="--seeds 20260816,7,101")
    seeds = [int(x) for x in args.seeds.split(",")]
    slots = getattr(config, "SYSTEM_MAX_HOLDINGS", 4)

    config.session.load_stock_config()
    live = [(s["code"], s["name"]) for s in config.session.stock_data.get("stocks_kr", [])]
    if args.live_only:
        ext, dead_t, n_dead = [], [], 0
    else:
        ext = extend_targets({c for c, _ in live}, 60, mode="random", pool=args.pool)
        n_dead = int(args.size * args.dead_frac)
        try:
            dead_t = dead_targets(n_dead + 10)
        except Exception as e:
            # 폐지 목록 원본이 죽어도 확장 풀만으로 진행한다 — 다만 폐지가 빠졌다는 사실을
            #  숨기지 않는다. 표본 크기를 유지하려 조용히 생존 종목으로 채우면 생존 편향이
            #  기록 없이 섞인다([[survivorship-premium-2x]]).
            print(f"[경고] 폐지 목록을 못 받았다({type(e).__name__}) — 폐지 0으로 진행한다. "
                  f"생존 편향이 걸린 표본이다.", flush=True)
            dead_t, n_dead = [], 0
    dfs, mf, dates, _f = pb.prepare_universe(live + ext + dead_t, args.days)
    dead_set = {c for c, _ in dead_t}
    dead_c = [c for c in dfs if c in dead_set]
    live_c = [c for c in dfs if c not in dead_set]
    size = min(args.size, len(live_c) + len(dead_c)) if args.live_only else args.size
    n_dead = min(n_dead, len(dead_c))

    thr = {"BUY_SCORE": config.ANALYSIS_THRESHOLDS["BUY_SCORE"],
           "BUY_RSI_MAX": config.ANALYSIS_THRESHOLDS["BUY_RSI_MAX"],
           "RISE_SCORE": config.ANALYSIS_THRESHOLDS["RISE_SCORE"],
           "WEIGHTS": config.SCORING_WEIGHTS}
    status = pb.precompute_status(dfs, thr)
    new_scale = new_scale_fn_factory(dates, args.days)

    # 감사 경로의 TQ 구현을 쓴다 — 표시 경로(indicators.get_trend_quality)와 파리티는
    #  2026-08-17에 확인했다(최대 차이 0.005). audit_tq_regime_failure와 같은 산식이다.
    from tools.audit_score_weighted_sizing import rolling_trend_quality
    lb = config.INDICATOR_PARAMS.get("TREND_QUALITY_LOOKBACK", 90)
    tq_map = {}
    for c, df in dfs.items():
        col = rolling_trend_quality(df["close"], lb)
        tq_map[c] = dict(zip((str(d) for d in df["date"]), col))

    k = max(1, args.subperiods)
    step = max(1, len(dates) // k)
    seg_of = {}
    for i in range(k):
        for d in dates[i * step:(i + 1) * step if i < k - 1 else len(dates)]:
            seg_of[d] = f"구간{i + 1}"

    print(f"[준비] {len(dfs)}종목(폐지 {len(dead_c)}) · 표본 크기 {size} · 거래일 {len(dates)} · "
          f"TQ 룩백 {lb}일 · 슬롯 {slots}", flush=True)
    if args.live_only:
        print("[주의] --live-only: 폐지 종목이 없다. '극단 TQ 뒤 붕괴'의 결정적 사례가 빠졌으므로 "
              "**꺾임이 보이면 보수적으로 참, 안 보이면 판정 불가**로 읽어야 한다.", flush=True)

    recs = []      # (구간, TQ, 손익%, 청산사유, 보유일)
    for sd in seeds:
        for i in range(args.trials):
            rng = random.Random(sd * 31 + i)
            pick = (rng.sample(dead_c, n_dead)
                    + rng.sample(live_c, min(size - n_dead, len(live_c))))
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

    strong = [r for r in recs if r[1] >= 60]
    qs = np.percentile([r[1] for r in strong], [25, 50, 75]) if strong else []
    print(f"[진입 시점 TQ 분포] 전체 중앙 {np.median([r[1] for r in recs]):.1f} · "
          f"60+ 진입 {len(strong):,}건({len(strong) / max(1, len(recs)) * 100:.1f}%) · "
          f"60+ 안의 사분위 {qs[0]:.0f} / {qs[1]:.0f} / {qs[2]:.0f}", flush=True)

    # ── ① 전체창: 고정 절단
    print("\n[1] 전체창 — 최상위 밴드를 고정 절단으로 쪼갠다")
    print(HEAD)
    for lo, hi, lab in COARSE + FINE:
        print(row(lab, [r for r in recs if lo <= r[1] < hi]))

    # ── ② 전체창: 60+ 안의 등분위 4등분 (표본을 균등하게 맞춘 재확인)
    print("\n[2] 전체창 — 60+ 안을 등분위 4등분 (표본 균등, 경계는 데이터가 정한다)")
    print(HEAD)
    if len(strong) >= MIN_N * 4:
        edges = [60.0] + list(qs) + [1e9]
        for j in range(4):
            lo, hi = edges[j], edges[j + 1]
            lab = f"Q{j + 1} {lo:.0f}~" + ("∞" if hi > 1e8 else f"{hi:.0f}")
            print(row(lab, [r for r in strong if lo <= r[1] < hi]))
    else:
        print(f"  60+ 표본 {len(strong)}건 — 4등분 불가")

    # ── ③ 구간별 (전체창 평균이 가린 것이 있는지)
    segs = [f"구간{i + 1}" for i in range(k)]
    print("\n[3] 구간별 — 전체창의 그림이 구간마다 유지되는가")
    for sg in segs:
        sub = [r for r in recs if r[0] == sg]
        print(f"\n  ── {sg} (진입 {len(sub):,}건)")
        print(HEAD)
        for lo, hi, lab in [(30, 60, "양호(30~60)")] + FINE:
            print(row(lab, [r for r in sub if lo <= r[1] < hi]))

    # ── ④ 청산 사유 — 꺾인다면 무엇으로 나가는가
    print("\n[4] 최상위 밴드의 청산 사유 분포(%) — 꺾임이 있다면 그 형태를 본다")
    reasons = ["ATR손절", "손절", "본전청산", "시간청산", "트레일링스탑", "점수하락"]
    print(f"  {'밴드':<18}" + "".join(f"{r:>10}" for r in reasons))
    for lo, hi, lab in [(30, 60, "양호(30~60)")] + FINE:
        seg = [r for r in recs if lo <= r[1] < hi]
        if len(seg) < MIN_N:
            continue
        cnt = Counter(r[3] for r in seg)
        print(f"  {lab:<18}" + "".join(f"{cnt.get(r, 0) / len(seg) * 100:>10.1f}"
                                       for r in reasons))

    print("\n[읽는 법] [1]과 [2]가 **같은 방향**을 가리켜야 결론이다. 평균·상위10%가 TQ를 따라 "
          "계속 오르면 단조 — 밴드가 해상도를 잃은 표시 문제일 뿐이다. 어느 지점에서 꺾이면 "
          "종목 축의 과열이 실재하고, 그때만 상한 게이트를 검토할 값이 있다.")
    print("[주의] 이 도구는 진단이다. 여기서 꺾임이 보여도 그것만으로 다이얼을 바꾸지 않는다 "
          "— 상한을 실제로 거는 팔을 따로 세워 승-무-패로 재야 한다.")


if __name__ == "__main__":
    main()
