"""무작위 진입 차단이 정말 기준선을 이기는가 — 부산물 주장의 영가설 검정.

[왜 이걸 따로 재는가] TQ 상한 감사(`audit_tq_upper_cap`)의 부산물로 "근거 없이 후보의
 8.3%를 무작위로 버리기만 해도 252.0 → 333.0%"가 나왔다. 사실이면 **한계에서 진입 순위가
 무작위보다 낫지 않다**는 뜻이 되어 [[entry-rank-score-first]]를 다시 봐야 한다. 그런데
 그 근거는 무작위 패턴 5장이 전부였고, 장별 수치를 다시 보면 이렇다.
     대조 5장  409.2 · 331.3 · 391.3 · 252.2 · 281.2   기준선 252.0
 기준선이 6개 중 꼴찌다. **우연히 꼴찌일 확률이 1/6 ≈ 17%**로 유의하지 않다. 게다가 10년
 복리에 꼬리가 두꺼우면 분포가 오른쪽으로 길어져서, 교란을 준 팔들의 *평균*이 기준선
 한 점보다 높게 나오는 것은 개선이 아니라 **치우침의 산물**일 수 있다. 평균 비교로는
 그 둘이 안 갈린다.

[이 도구가 답하는 방식] 두 가지를 본다.
  ① **순위 검정** — 무작위 차단을 여러 장 뽑아 분포를 만들고, 기준선이 그 분포의 어디에
     앉는지 본다. 하위 5% 밖이면 '기준선이 유독 나쁘다'가 되어 주장이 선다. 중앙 근처면
     평균 차이는 치우침이 만든 것이다.
  ② **용량-반응** — 차단율을 0 → 5 → 8.3 → 15 → 25%로 올린다. '덜 사는 것이 좋다'가
     실재하면 어느 지점까지 단조로 좋아지고 그 뒤 꺾여야 한다. 차단율과 무관하게 들쭉날쭉
     하면 그것은 잡음이다. **이쪽이 순위 검정보다 속이기 어렵다.**

[읽을 때 주의] 차단은 후보를 버리는 것이지 슬롯을 비우는 것이 아니다 — 막힌 자리에는
 다음 순위 종목이 들어온다. 그래서 이것은 '덜 산다'가 아니라 **'순위를 흔든다'**에 가깝다.
 효과가 있다면 그 해석부터 바꿔야 한다.

[실행] python3 tools/audit_random_block_null.py --draws 12 --live-only
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


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=3650)
    ap.add_argument("--trials", type=int, default=12)
    ap.add_argument("--seeds", default="20260816,7,101")
    ap.add_argument("--size", type=int, default=44)
    ap.add_argument("--pool", type=int, default=500)
    ap.add_argument("--dead-frac", type=float, default=0.2)
    ap.add_argument("--rates", default="5,8.3,15,25", help="차단율(%%) 목록")
    ap.add_argument("--draws", type=int, default=12, help="차단율마다 뽑을 무작위 패턴 장수")
    ap.add_argument("--subperiods", type=int, default=3)
    ap.add_argument("--sub-rate", type=float, default=8.3,
                    help="구간별로도 볼 차단율 — 전부 다 보면 너무 오래 걸린다")
    ap.add_argument("--live-only", action="store_true")
    # [실매매 순위와 맞추기] portfolio_backtest의 기본 정렬은 **점수만** 본다. 점수는
    #  0~10을 0.5 간격으로 끊은 21개 값뿐이라(99,110건 실측) 슬롯 당락 경계의 45~52%가
    #  동점이고, 그 동점을 파이썬 안정정렬이 **딕셔너리 등록 순서**로 가른다 — 근거 없는
    #  고정 편애다. 실매매(trader.candidate_priority_key)는 이미 (점수 → 추세품질 → 52주
    #  위치 → 체결강도)로 가른다. 이 옵션을 켜면 그 순서를 백테스트에 넣는다.
    # [2026-08-18 이후] 엔진 기본값이 실매매 동점가름으로 바뀌었다. --live-rank는 이제
    #  아무것도 바꾸지 않고(기본), 옛 모델을 보려면 --legacy-rank를 켠다. 이 도구가
    #  드러낸 가짜 신호가 바로 그 옛 모델의 산물이라 대조 통로는 남겨 둔다.
    ap.add_argument("--live-rank", action="store_true",
                    help="(기본값이라 무동작) 실매매와 같은 동점가름 — 하위호환용")
    ap.add_argument("--legacy-rank", action="store_true",
                    help="옛 기본 정렬(점수만·동점은 등록 순서)로 되돌려 잰다")
    args = ap.parse_args()
    seeds = [int(x) for x in args.seeds.split(",")]
    rates = [float(x) / 100 for x in args.rates.split(",")]
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

    picks = {}
    for sd in seeds:
        for i in range(args.trials):
            rng = random.Random(sd * 31 + i)
            picks[(sd, i)] = (rng.sample(dead_c, n_dead)
                              + rng.sample(live_c, min(size - n_dead, len(live_c))))

    # 동점가름은 엔진 기본값(실매매식)을 그대로 쓴다 — 도구가 따로 만들지 않는다.
    rank_fn = "legacy" if args.legacy_rank else None
    lb = config.INDICATOR_PARAMS.get("TREND_QUALITY_LOOKBACK", 90)
    if rank_fn is None:
        # 워밍업 구간은 잘라낸다 — 앞부분은 모든 후보가 이력부족이라 동점가름이 다시
        #  등록 순서로 떨어져 비교가 오염된다(audit_entry_rank_order와 같은 규약).
        dates = dates[lb - 1:]

    print(f"[준비] {len(dfs)}종목(폐지 {len(dead_c)}) · 표본 {size} · 거래일 {len(dates)} · "
          f"슬롯 {slots} · 장수 {args.draws} · "
          f"동점가름 {'옛 기본값(등록 순서)' if args.legacy_rank else '실매매식(점수→추세품질)'}",
          flush=True)

    def gate(rate, salt):
        if rate <= 0:
            return None
        def g(day, code, _held):
            return random.Random(f"{salt}|{day}|{code}").random() < rate
        return g

    def run(wd, g):
        return [metrics(pb.run_portfolio(
                    {c: dfs[c] for c in picks[(sd, i)]},
                    {c: status[c] for c in picks[(sd, i)]}, wd,
                    initial_capital=INITIAL_CAPITAL, slots=slots,
                    market_filter_dates={c: mf.get(c, set()) for c in picks[(sd, i)]},
                    risk_scale_by_date=new_scale(), entry_gate=g, rank_fn=rank_fn))
                for sd in seeds for i in range(args.trials)]

    k = max(1, args.subperiods)
    step = max(1, len(dates) // k)
    W = [("전체", list(dates))]
    W += [(f"구간{i + 1}", dates[i * step:(i + 1) * step if i < k - 1 else len(dates)])
          for i in range(k)]

    for wn, wd in W:
        use = rates if wn == "전체" else [r for r in rates
                                         if abs(r - args.sub_rate / 100) < 1e-9]
        if not use:
            continue
        print(f"\n########## {wn} ({len(wd)} 거래일) ##########", flush=True)
        base = run(wd, None)
        b_ret = float(np.mean([m["ret"] for m in base]))
        b_mdd = float(np.mean([m["mdd"] for m in base]))
        print(f"  기준선(차단 없음)  수익 {b_ret:.1f}%  MDD {b_mdd:.1f}%", flush=True)
        print(f"  {'차단율':<8}{'장평균%':>9}{'중앙%':>9}{'최소%':>9}{'최대%':>9}{'σ':>8}"
              f"{'MDD%':>8}{'기준선 순위':>12}{'승-무-패(누적)':>16}")
        for rate in use:
            per, pooled = [], []
            for j in range(args.draws):
                r = run(wd, gate(rate, f"nb{rate:.4f}|{wn}|{j}"))
                per.append(float(np.mean([m["ret"] for m in r])))
                pooled += r
            a = np.array(per)
            # 기준선이 이 분포의 몇 번째인가 — 1위면 '기준선이 가장 좋다', 꼴찌면 '가장 나쁘다'.
            rank = int((a > b_ret).sum()) + 1        # 1 = 최고
            win = sum(1 for x, y in zip(pooled, base * args.draws)
                      if x["ret"] > y["ret"] + 1e-9)
            tie = sum(1 for x, y in zip(pooled, base * args.draws)
                      if abs(x["ret"] - y["ret"]) <= 1e-9)
            print(f"  {rate * 100:<8.1f}{a.mean():>9.1f}{np.median(a):>9.1f}{a.min():>9.1f}"
                  f"{a.max():>9.1f}{a.std():>8.1f}"
                  f"{float(np.mean([m['mdd'] for m in pooled])):>8.1f}"
                  f"{f'{rank}/{args.draws + 1}':>12}"
                  f"{f'{win}-{tie}-{len(pooled) - win - tie}':>16}", flush=True)

    print("\n[읽는 법] ① 순위: 기준선이 하위 5% 밖(예: 12장 중 13위)이면 '기준선이 유독 나쁘다'가 "
          "성립한다. 중앙 근처면 평균 차이는 치우침이 만든 것이다. ② 용량-반응: 차단율을 "
          "올릴 때 단조로 좋아지다 꺾이면 실재하는 효과다. 들쭉날쭉하면 잡음이다.")
    print("[주의] 차단은 슬롯을 비우지 않는다 — 막힌 자리에 다음 순위가 들어온다. 효과가 "
          "있다면 '덜 산다'가 아니라 '순위를 흔든다'로 해석해야 한다.")


if __name__ == "__main__":
    main()
