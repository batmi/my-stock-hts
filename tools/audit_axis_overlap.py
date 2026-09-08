"""[검증 전용] 시장 감속 4축이 **같은 정보를 여러 번 과세**하는가 — 전략 수준에서.

[왜 지금] `tools/audit_market_axes.py` 가 중복을 실측했다(KOSPI 4,095일):
  · P(휩소|시장필터) 87.5% · P(시장필터|국면) 79.5% · P(드로다운|시장필터) 82.7%
  · 3축 이상 동시 발동 **36.1%** (3축 22.2% + 4축 13.9%)
  · 결합 배수와 향후 20일 지수 수익률이 **단조가 아니다**:
      0.00~0.45 -3.27% · 0.45~0.60 +0.11% · **0.60~0.80 +2.14%(승률 66.1%)** ·
      0.80~0.99 +0.36% · 0.99~1.01 +1.71%
    → 가장 좋은 구간에서 노출을 크게 깎고 있다.

**그러나 그것은 지수 수익률이지 전략 수익률이 아니다.** 축이 겹친다는 사실만으로는
아무것도 결정할 수 없다 — 겹치는 축을 곱해도 결과가 더 좋을 수 있다. 그래서 여기서
**같은 백테스트에 결합 방식만 바꿔 넣고** 잰다.

[무엇을 재나 — 축을 빼는 것이 아니라 '합치는 법'을 바꾼다]
 축을 아예 빼는 방향은 **이미 반증돼 있다**: 같은 평균 배수(0.694)를 상수로 준 대조군은
 수익이 절반이었다(146.5% → 71.0%, trader._update_risk_scale 주석). 타이밍은 기여한다.
 그래서 팔은 '언제 줄이나'는 그대로 두고 '얼마나 줄이나'만 건드린다.

  · 현행(곱)        국면 × 휩소 × 드로다운 — 세 축이 다 걸리면 0.6×0.85×0.8 = 0.41
  · min 결합        셋 중 **가장 나쁜 축 하나만** 듣는다 → 0.60. 중복 과세만 제거한다
  · 하한 0.60       곱하되 0.60 밑으로 못 내려가게 한다(위 [D] 최고 구간을 지킨다)
  · 하한 0.45       더 느슨한 하한 — 하한의 위치가 결론을 가르는지 본다
  · 스케일링 OFF    대조군. 감속 자체의 값어치를 이 실행 안에서 다시 확인한다

[잣대] 채택 규칙은 그대로다 — 전체창에서 이겨도 구간이 갈리면 채택하지 않는다
 ([[adoption-rule-drawdown-veto]] · [[adoption-rule-defensive-exception]]). 이 축은
 **낙폭 통제 장치**이므로 수익이 늘어도 MDD 가 상대 10% 넘게 나빠지면 기각이다.

[실행] python3 tools/audit_axis_overlap.py --trials 30 --sample 30 --seeds 3
"""
import argparse
import os
import random
import sys

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import config  # noqa: E402
from modules import portfolio_backtest as pb  # noqa: E402
from tools.audit_common import base_metrics, seed_notice, windows  # noqa: E402
from tools.audit_drawdown_axis import make_scale_fn, market_scale_by_date  # noqa: E402

INITIAL_CAPITAL = 10_000_000

#  (라벨, market 결합, scale_fn 결합, 하한, 축 사용)
#   market 결합 = 국면·휩소를 합치는 법 / scale_fn 결합 = 그 결과와 드로다운을 합치는 법
ARMS = [
    ("현행 (전부 곱)",      "product", "product", None, True),
    ("min 결합",            "min",     "min",     None, True),
    ("하한 0.60",           "product", "product", 0.60, True),
    ("하한 0.45",           "product", "product", 0.45, True),
    ("[대조] 스케일링 OFF", "product", "product", None, False),
]


def dd_params():
    """현행 드로다운 축 설정을 config 에서 읽는다(리터럴을 베끼지 않는다)."""
    p = getattr(config, "RISK_SCALING_PARAMS", {}) or {}
    return (p.get("DD_LOOKBACK_DAYS", 0), p.get("DD_LEVEL_1", 0), p.get("DD_SCALE_1", 1.0),
            p.get("DD_LEVEL_2", 0), p.get("DD_SCALE_2", 1.0))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=3650)
    ap.add_argument("--trials", type=int, default=30)
    ap.add_argument("--sample", type=int, default=30)
    ap.add_argument("--seeds", type=int, default=3)
    ap.add_argument("--seed", type=int, default=20260908)
    ap.add_argument("--slots", type=int, default=None)
    ap.add_argument("--subperiods", type=int, default=3)
    args = ap.parse_args()
    seed_notice(args.seeds, example="--seeds 3")

    slots = args.slots or getattr(config, "SYSTEM_MAX_HOLDINGS", 4)
    config.session.load_stock_config()
    targets = [(s["code"], s["name"])
               for s in config.session.stock_data.get("stocks_kr", [])]
    print(f"[준비] 관심종목 {len(targets)}개 · {args.days}일 · 슬롯 {slots}")

    dfs, mf_dates, dates, failed = pb.prepare_universe(targets, args.days)
    print(f"[준비] 사용 {len(dfs)}종목 / 거래일 {len(dates)}일"
          + (f" · 제외 {len(failed)}개" if failed else ""))
    if len(dfs) < args.sample:
        args.sample = max(5, len(dfs) // 2)
        print(f"[준비] 표본 수를 {args.sample}로 조정")

    thresholds = {
        "BUY_SCORE": config.ANALYSIS_THRESHOLDS["BUY_SCORE"],
        "BUY_RSI_MAX": config.ANALYSIS_THRESHOLDS["BUY_RSI_MAX"],
        "RISE_SCORE": config.ANALYSIS_THRESHOLDS["RISE_SCORE"],
        "WEIGHTS": config.SCORING_WEIGHTS,
    }
    status = pb.precompute_status(dfs, thresholds)

    #  결합 방식마다 국면×휩소 계열이 달라진다 — 미리 두 벌 만들어 둔다.
    mkt = {c: market_scale_by_date(dates, args.days, combine=c)
           for c in ("product", "min")}
    for c, m in mkt.items():
        vals = list(m.values())
        print(f"[준비] 국면·휩소 결합={c}: 평균 배수 {np.mean(vals):.3f} · "
              f"축소일 {sum(1 for v in vals if v < 0.999) / len(vals) * 100:.1f}%")

    ddp = dd_params()
    codes = list(dfs.keys())
    wins = windows(dates, args.subperiods, whole=True)
    total = len(wins) * args.seeds * args.trials * len(ARMS)
    done = 0

    for wname, wdates in wins:
        sub_status = {c: {d: v for d, v in status[c].items() if d in set(wdates)}
                      for c in codes} if wname != "전체" else status
        res = {lbl: [] for lbl, *_ in ARMS}
        pairs = {lbl: [0, 0, 0] for lbl, *_ in ARMS}   # 승-무-패 (현행 대비)
        marw = {lbl: 0 for lbl, *_ in ARMS}

        for si in range(args.seeds):
            rng = random.Random(args.seed + si * 977)
            for _t in range(args.trials):
                pick = rng.sample(codes, min(args.sample, len(codes)))
                sd = {c: dfs[c] for c in pick}
                ss = {c: sub_status[c] for c in pick}
                sm = {c: mf_dates.get(c, set()) for c in pick}
                trial = {}
                for lbl, mcomb, scomb, floor, use in ARMS:
                    #  [필수] 콜러블은 실행마다 새로 만든다 — 자산곡선 이력이 남으면
                    #   대안 팔만 깎이는 편향이 생긴다([[audit-scale-fn-contamination]]).
                    fn = make_scale_fn(mkt[mcomb], ddp if use else None,
                                       use_market=use, combine=scomb, floor=floor)
                    r = pb.run_portfolio(sd, ss, wdates, initial_capital=INITIAL_CAPITAL,
                                         slots=slots, market_filter_dates=sm,
                                         risk_scale_by_date=fn)
                    m = base_metrics(r)
                    m["cash"] = r.get("avg_cash_ratio")
                    res[lbl].append(m)
                    trial[lbl] = m
                    done += 1
                    if done % 25 == 0:
                        print(f"  {wname} 씨드{si + 1} {done}/{total}", end="  ", flush=True)
                base = trial[ARMS[0][0]]
                for lbl, *_ in ARMS[1:]:
                    d = trial[lbl]["ret"] - base["ret"]
                    pairs[lbl][0 if d > 1e-9 else (2 if d < -1e-9 else 1)] += 1
                    if (trial[lbl]["mar"] or 0) > (base["mar"] or 0):
                        marw[lbl] += 1

        print(f"\n\n########## {wname} ({len(wdates)} 거래일) ##########\n")
        print(f"{'팔':<20}{'수익%':>8}{'MDD%':>8}{'MAR':>7}{'PF':>6}{'청산':>6}"
              f"{'승률':>7}{'상위10%':>9}{'현금%':>7}{'승-무-패':>11}{'MAR승':>7}")
        print("-" * 100)
        n = args.seeds * args.trials
        for lbl, *_ in ARMS:
            a = res[lbl]
            g = lambda k: np.nanmean([x[k] for x in a])  # noqa: E731
            wl = ("—" if lbl == ARMS[0][0]
                  else f"{pairs[lbl][0]}-{pairs[lbl][1]}-{pairs[lbl][2]}")
            mw = "—" if lbl == ARMS[0][0] else f"{marw[lbl]}/{n}"
            print(f"{lbl:<20}{g('ret'):>8.1f}{g('mdd'):>8.1f}{g('mar'):>7.2f}"
                  f"{g('pf'):>6.2f}{g('n'):>6.0f}{g('win'):>7.1f}{g('top10'):>9.1f}"
                  f"{g('cash'):>7.1f}{wl:>11}{mw:>7}")

    print("\n" + "-" * 100)
    print("[읽는 법] 이 축은 낙폭 통제 장치다 — 수익이 늘어도 MDD 가 상대 10% 넘게")
    print("  나빠지면 기각한다([[adoption-rule-drawdown-veto]]). 구간이 갈려도 기각한다.")
    print("[대조] '스케일링 OFF' 가 현행을 이기면, 이번 실행의 표본이 감속이 불리한")
    print("  구간에 쏠린 것이다 — 그때는 다른 팔의 승패도 그만큼 할인해 읽어야 한다.")


if __name__ == "__main__":
    main()
