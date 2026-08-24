"""추세품질(TQ) 상한 게이트 — 진단에서 본 꺾임이 실제 성과로 옮겨가는가.

[무엇을 봤길래] `tools/audit_tq_band_structure.py`가 최상위 밴드 안쪽을 쪼개 보니
 TQ는 단조가 아니었다. 고정 절단과 등분위가 **같은 지점**을 가리켰다.
     60~100  -1.28 │ Q1  60~97  -1.17
    100~300  +2.74 │ Q2  97~140 +5.26   ← 정점
    300~1k   -5.69 │ Q3 140~284 -1.00
    1k+      -2.93 │ Q4 284~∞   -3.74   ← 붕괴
 청산 사유가 형태를 말해 준다 — ATR손절이 20.5% → 40.1% → 45.5%로 뛰고 보유일은
 29.2 → 20.0일로 준다. **마르는 게 아니라 잘린다.** 종목 축의 모멘텀 크래시다.

[그런데 진단은 다이얼이 아니다] 위 표는 '이미 산 것'을 사후에 가른 것이다. 상한을
 실제로 걸면 그 자리에 **다른 종목이 들어온다.** 나쁜 진입을 막아도 대체 진입이 더
 나쁘면 순손실이다. 그래서 승-무-패로 다시 재야 한다.

[팔] 기준선(상한 없음) · 상한 300 · 상한 500 · 상한 1000
 그리고 **[대조] 무작위 차단**을 같은 차단율로 둔다. 이게 이 도구의 핵심이다 —
 진입을 아무렇게나 줄이기만 해도 좋아진다면(슬롯 경쟁이 완화되므로 그럴 수 있다)
 TQ 상한의 공은 TQ가 아니라 '덜 사는 것'에 있다. 대조를 확실히 이겨야 진짜다.
 차단율은 1차 실행에서 실측해 대조에 그대로 먹인다(추정하지 않는다).

[빈도부터 센다] 진단 표본에서 TQ 300+ 진입은 11,221건 중 803건(7.2%)이었다. 무시할
 수 없되 흔하지도 않다 — 구간별 표본이 얇아질 수 있으니 밴드별 건수를 함께 볼 것.

[실행] python3 tools/audit_tq_upper_cap.py --trials 12
       python3 tools/audit_tq_upper_cap.py --trials 12 --live-only   # 목록 원본이 죽었을 때
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


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=3650)
    ap.add_argument("--trials", type=int, default=12)
    ap.add_argument("--seeds", default="20260816,7,101")
    ap.add_argument("--size", type=int, default=44)
    ap.add_argument("--pool", type=int, default=500)
    ap.add_argument("--dead-frac", type=float, default=0.2)
    ap.add_argument("--subperiods", type=int, default=3)
    ap.add_argument("--caps", default="300,500,1000")
    # [하한 축] "회귀선이 우하향(TQ<0)이면 점수를 넘어도 사지 않는다"는 제안을 같은
    #  기계로 잰다. 상한과 방향만 다를 뿐 질문의 구조가 같고(후보를 지우면 그 자리에
    #  다음 후보가 들어온다), 특히 **같은 차단율 무작위 대조**가 필수라 도구를 따로
    #  만들지 않고 여기에 붙인다. 비어 있으면 종전과 완전히 같게 동작한다.
    ap.add_argument("--floors", default="",
                    help="추세품질 하한 게이트(쉼표 구분, 예: 0,10). 이 값 미만이면 배제")
    # [대조군의 분산] 무작위 차단은 '한 번 뽑은 패턴'이다. 1회 실행에서 구간3의 4.0% 대조가
    #  282.2%(기준선 191.8%)로 튀었고 6.5% 대조는 177.6%로 내려갔다 — 차단율이 높은 쪽이
    #  더 나쁜, 순서가 뒤집힌 결과다. 대조가 그만큼 흔들린다는 뜻이므로 한 장으로 판정하면
    #  안 된다. 여러 장을 뽑아 평균한다.
    ap.add_argument("--placebo-draws", type=int, default=5)
    ap.add_argument("--live-only", action="store_true",
                    help="관심종목만 사용 (확장·폐지 풀 없이 — 목록 원본이 죽었을 때)")
    # [계측기 파리티] 기본 정렬은 점수만 봐서 슬롯 당락 경계의 45~52%를 등록 순서로 가른다
    #  — 실매매와 다른 모델이다([[backtest-tiebreak-parity]]). 순위가 결과에 닿는 축은
    #  반드시 켜고 잴 것.
    ap.add_argument("--live-rank", action="store_true",
                    help="(2026-08-18부터 기본값이라 무동작) 실매매식 동점가름 — 하위호환용")
    ap.add_argument("--legacy-rank", action="store_true",
                    help="옛 기본 정렬(점수만·동점은 등록 순서)로 되돌려 잰다")
    args = ap.parse_args()
    seed_notice(len(args.seeds.split(",")), example="--seeds 20260816,7,101")
    seeds = [int(x) for x in args.seeds.split(",")]
    caps = [float(x) for x in args.caps.split(",") if x.strip()]
    floors = [float(x) for x in args.floors.split(",") if x.strip()]
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

    from tools.audit_score_weighted_sizing import rolling_trend_quality
    lb = config.INDICATOR_PARAMS.get("TREND_QUALITY_LOOKBACK", 90)
    tq_map = {}
    for c, df in dfs.items():
        tq_map[c] = dict(zip((str(d) for d in df["date"]),
                             rolling_trend_quality(df["close"], lb)))

    # 동점가름은 엔진 기본값(실매매식)을 쓴다 — 도구가 따로 만들면 두 벌이 되어 어긋난다.
    rank_fn = "legacy" if args.legacy_rank else None
    if rank_fn is None:
        # 워밍업 구간 제외 — 앞부분은 이력부족이라 동점가름이 등록 순서로 떨어진다.
        dates = dates[lb - 1:]

    print(f"[준비] {len(dfs)}종목(폐지 {len(dead_c)}) · 표본 {size} · 거래일 {len(dates)} · "
          f"TQ 룩백 {lb}일 · 슬롯 {slots} · "
          f"동점가름 {'옛 기본값(등록 순서)' if args.legacy_rank else '실매매식'}", flush=True)
    if args.live_only:
        print("[주의] --live-only: 폐지 종목이 없다. 상한의 이득은 보수적으로(작게) 나온다 "
              "— 극단 TQ 뒤 붕괴의 결정적 사례가 표본에서 빠졌기 때문이다.", flush=True)

    picks = {}
    for sd in seeds:
        for i in range(args.trials):
            rng = random.Random(sd * 31 + i)
            picks[(sd, i)] = (rng.sample(dead_c, n_dead)
                              + rng.sample(live_c, min(size - n_dead, len(live_c))))

    k = max(1, args.subperiods)
    step = max(1, len(dates) // k)
    W = [("전체", list(dates))]
    W += [(f"구간{i + 1}", dates[i * step:(i + 1) * step if i < k - 1 else len(dates)])
          for i in range(k)]

    counters = {"calls": 0, "blocks": 0}

    def cap_gate(cap):
        def g(day, code, _held):
            counters["calls"] += 1
            q = tq_map.get(code, {}).get(str(day))
            if q is not None and np.isfinite(q) and q >= cap:
                counters["blocks"] += 1
                return True
            return False
        return g

    def floor_gate(floor):
        """이력 부족(None/비유한)은 통과시킨다 — 상한 게이트·실매매와 같은 fail-open 규약."""
        def g(day, code, _held):
            counters["calls"] += 1
            q = tq_map.get(code, {}).get(str(day))
            if q is not None and np.isfinite(q) and q < floor:
                counters["blocks"] += 1
                return True
            return False
        return g

    def rnd_gate(rate, salt):
        def g(day, code, _held):
            # 결정적 난수 — 같은 (날짜,종목)은 항상 같은 판정이라 팔 사이 비교가 재현된다.
            return random.Random(f"{salt}|{day}|{code}").random() < rate
        return g

    def run(wd, gate):
        res = []
        for sd in seeds:
            for i in range(args.trials):
                pick = picks[(sd, i)]
                r = pb.run_portfolio(
                    {c: dfs[c] for c in pick}, {c: status[c] for c in pick}, wd,
                    initial_capital=INITIAL_CAPITAL, slots=slots,
                    market_filter_dates={c: mf.get(c, set()) for c in pick},
                    risk_scale_by_date=new_scale(), entry_gate=gate, rank_fn=rank_fn)
                res.append(metrics(r))
        return res

    # [대조군을 창마다 다시 맞춘다] 처음엔 차단율을 첫 구간에서 한 번만 재서 모든 창에
    #  같은 값을 썼다 — 그런데 구간1에는 TQ 300+ 진입이 **0건**이라(진단 표에서 확인)
    #  차단율이 0.0%로 나왔고, 무작위 대조가 아무것도 막지 않아 기준선과 똑같아졌다
    #  (승-무-패 0-36-0). 대조가 무효면 '덜 사는 것'과 'TQ를 보고 덜 사는 것'을 못 가른다.
    #  그래서 **창마다 그 창의 상한 팔을 먼저 돌려 실측 차단율을 얻고**, 그 값으로 대조를
    #  만든다. 상한 팔은 어차피 돌려야 하므로 추가 비용이 없다.
    def show(label, res, base):
        g = lambda key: float(np.mean([m[key] for m in res]))  # noqa: E731
        if base is None:
            wl = "— (기준)"
        else:
            win = sum(1 for x, y in zip(res, base) if x["ret"] > y["ret"] + 1e-9)
            tie = sum(1 for x, y in zip(res, base) if abs(x["ret"] - y["ret"]) <= 1e-9)
            wl = f"{win}-{tie}-{len(res) - win - tie}"
        print(f"{label:<30}{g('ret'):>9.1f}{g('mdd'):>8.1f}{g('mar'):>7.2f}"
              f"{g('pf'):>6.2f}{g('n'):>6.0f}{g('top10'):>9.1f}{g('win'):>7.1f}"
              f"{wl:>10}", flush=True)

    for wn, wd in W:
        print(f"\n########## {wn} ({len(wd)} 거래일) ##########")
        print(f"{'팔':<30}{'수익%':>9}{'MDD%':>8}{'MAR':>7}{'PF':>6}{'청산':>6}"
              f"{'상위10%':>9}{'승률%':>7}{'승-무-패':>10}")
        base = run(wd, None)
        show("[기준선] 상한 없음", base, None)
        rates = {}
        for cap in caps:
            counters["calls"] = counters["blocks"] = 0
            res = run(wd, cap_gate(cap))
            rates[cap] = counters["blocks"] / max(1, counters["calls"])
            show(f"TQ 상한 {cap:.0f} (차단 {rates[cap] * 100:.1f}%)", res, base)
        for fl in floors:
            counters["calls"] = counters["blocks"] = 0
            res = run(wd, floor_gate(fl))
            rates[("floor", fl)] = counters["blocks"] / max(1, counters["calls"])
            show(f"TQ 하한 {fl:.0f} (차단 {rates[('floor', fl)] * 100:.1f}%)", res, base)
        for cap in caps:
            if rates[cap] <= 0:
                print(f"  [대조] 상한 {cap:.0f}은 이 창에서 한 건도 차단하지 않았다 "
                      f"— 대조를 세울 것이 없다(비교 불가).", flush=True)
                continue
            # 무작위 패턴을 여러 장 뽑아 **모두 이어 붙여** 하나의 표본으로 본다.
            #  평균만 내면 대조의 분산이 감춰지므로 장마다의 수익도 함께 찍는다.
            pooled, per_draw = [], []
            for j in range(max(1, args.placebo_draws)):
                r = run(wd, rnd_gate(rates[cap], f"plc{cap:.0f}|{wn}|{j}"))
                pooled += r
                per_draw.append(float(np.mean([m["ret"] for m in r])))
            show(f"[대조] 무작위 {rates[cap] * 100:.1f}% ({len(per_draw)}장 평균)",
                 pooled, base * max(1, args.placebo_draws))
            print("      장별 수익%: " + " · ".join(f"{x:.1f}" for x in per_draw)
                  + f"  (표준편차 {np.std(per_draw):.1f})", flush=True)
        for fl in floors:
            rate = rates[("floor", fl)]
            if rate <= 0:
                print(f"  [대조] 하한 {fl:.0f}은 이 창에서 한 건도 차단하지 않았다 "
                      f"— 대조를 세울 것이 없다(비교 불가).", flush=True)
                continue
            pooled, per_draw = [], []
            for j in range(max(1, args.placebo_draws)):
                r = run(wd, rnd_gate(rate, f"plcF{fl:.0f}|{wn}|{j}"))
                pooled += r
                per_draw.append(float(np.mean([m["ret"] for m in r])))
            show(f"[대조] 무작위 {rate * 100:.1f}% (하한{fl:.0f}·{len(per_draw)}장 평균)",
                 pooled, base * max(1, args.placebo_draws))
            print("      장별 수익%: " + " · ".join(f"{x:.1f}" for x in per_draw)
                  + f"  (표준편차 {np.std(per_draw):.1f})", flush=True)

    print("\n[읽는 법] 상한 팔이 기준선을 이기고 **같은 차단율의 무작위 대조까지** 이겨야 "
          "TQ 상한이 값을 하는 것이다. 대조와 비슷하면 공은 TQ가 아니라 '덜 사는 것'에 있다.")
    print("[채택 규칙] 구간이 갈리면 채택하지 않는다. 전체창만 이기는 것은 한 구간이 만든 "
          "평균일 수 있다 — 진단에서 구간1은 300+ 표본이 0건이었다.")


if __name__ == "__main__":
    main()
