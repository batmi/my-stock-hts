"""유니버스 크기를 워크포워드로 잰다 — 편향을 제거하는 유일한 설계.

[왜 이것이 마지막 관문인가] 크기 축은 두 번 쟀고 두 번 다 편향에 걸렸다.
  · 무작위 44 → 무작위 80: 1.59배 우세. 편향을 양쪽에 고정했으니 **크기 효과 자체는
    유효**하다. 그러나 확장 종목이 '오늘 시총 상위'라 실제 운용과 다르다.
  · 실제 관심종목 44 vs 확장 80: 확장이 진다(8-0-28 ~ 16-0-20). 그러나 기준선인
    관심종목 44는 **오늘 사람이 고른 목록**이라 과거 성과가 구조적으로 부풀려져 있다
    (같은 조건 무작위 44는 211.2%인데 관심 44는 553.8% — 2.6배가 선택에서만 나온다).
 두 실험 모두 '크기'와 '선택 편향'을 못 가른다. **양쪽 팔을 같은 규칙으로, 그 시점
 정보만으로 고르면** 그 교란이 사라진다. 남는 차이는 크기뿐이다.

[설계] 재조정 시점마다
   ① 그 시점까지의 데이터로만 적합도를 계산한다(`audit_discover_fit.fit_at`).
   ② 상위 K종목을 관심종목으로 삼는다 (K = 44 / 80).
   ③ 다음 창(기본 12개월)을 그 유니버스로 돌리고, 끝난 자산을 다음 창의 시작 자산으로
      **이어 붙인다**(복리). 창마다 현금에서 새로 시작하면 경계 효과가 생긴다
      ([[subwindow-method-verified]]).
 풀에는 **상장폐지 종목을 함께 넣는다.** 그 시점에는 멀쩡했다가 나중에 죽는 종목이
 선택될 수 있어야 편향이 진짜로 빠진다.

[대조] 같은 크기의 무작위 선택도 함께 돌린다. 적합도 상위가 무작위를 못 이기면 이
 실험의 '선택 규칙'은 크기 비교의 배경일 뿐이고, 이기면 규칙도 함께 값을 하는 것이다.

[실행] python3 tools/audit_universe_walkforward.py --sizes 44,80 --months 12 --seeds 3
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
    ap.add_argument("--pool-size", type=int, default=170, help="풀에서 준비할 생존 종목 수")
    ap.add_argument("--dead", type=int, default=45, help="풀에 섞을 폐지 종목 수")
    ap.add_argument("--sizes", default="44,80")
    ap.add_argument("--months", type=int, default=12)
    ap.add_argument("--seeds", default="20260816,7,101")
    ap.add_argument("--slots", type=int, default=None)
    # [견고성] 적합도 팔은 풀이 주어지면 결정적이다 — 유일한 무작위성이 풀 구성이므로
    #  결론을 확정하기 전에 풀 씨드를 바꿔 재확인한다([[audit-seed-robustness]]).
    ap.add_argument("--pool-seed", type=int, default=20260817)
    args = ap.parse_args()
    seed_notice(len(args.seeds.split(",")), example="--seeds 20260816,7,101")
    slots = args.slots or getattr(config, "SYSTEM_MAX_HOLDINGS", 4)
    sizes = [int(x) for x in args.sizes.split(",")]
    seeds = [int(x) for x in args.seeds.split(",")]

    targets = rule_pool(args.pool, args.pool_size, args.pool_seed)
    targets += dead_targets(args.dead)
    dfs, mf, dates, failed = pb.prepare_universe(targets, args.days)
    print(f"[준비] 풀 {len(dfs)}종목 (요청 {len(targets)} · 실패 {len(failed)}) · "
          f"거래일 {len(dates)} · 슬롯 {slots}", flush=True)

    thr = {"BUY_SCORE": config.ANALYSIS_THRESHOLDS["BUY_SCORE"],
           "BUY_RSI_MAX": config.ANALYSIS_THRESHOLDS["BUY_RSI_MAX"],
           "RISE_SCORE": config.ANALYSIS_THRESHOLDS["RISE_SCORE"],
           "WEIGHTS": config.SCORING_WEIGHTS}
    status = pb.precompute_status(dfs, thr)
    new_scale = new_scale_fn_factory(dates, args.days)
    pos = {c: {str(d): i for i, d in enumerate(df["date"])} for c, df in dfs.items()}

    step = max(1, int(args.months * 21))
    marks = list(range(250, len(dates) - step, step))
    print(f"[준비] 재조정 {len(marks)}회 · 창 {step}거래일 ({args.months}개월)", flush=True)

    # 재조정 시점마다 '그 시점까지의 데이터로만' 적합도를 매긴다.
    ranked = []
    for m in marks:
        day = dates[m]
        rows = []
        for c, df in dfs.items():
            i = pos[c].get(day)
            if i is None:                    # 그날 거래가 없으면(미상장·폐지) 후보 아님
                continue
            # fit_at은 점수가 아니라 지표 묶음을 돌려준다 — 메뉴와 같은 산식을 씌운다.
            feat = fit_at(df, i)
            if feat is not None:
                rows.append((_fit_score(feat), c))
        rows.sort(key=lambda x: -x[0])
        ranked.append([c for _f, c in rows])
        print(f"  [{day}] 후보 {len(rows)}종목", flush=True)

    def run_chain(pick_fn):
        """재조정마다 유니버스를 새로 고르고 자산을 이어 붙인다."""
        cap = float(INITIAL_CAPITAL)
        per_win = []
        for wi, m in enumerate(marks):
            uni = pick_fn(wi)
            if not uni:
                continue
            wd = dates[m:m + step]
            r = pb.run_portfolio(
                {c: dfs[c] for c in uni}, {c: status[c] for c in uni}, wd,
                initial_capital=cap, slots=slots,
                market_filter_dates={c: mf.get(c, set()) for c in uni},
                risk_scale_by_date=new_scale())
            per_win.append(r["total_return"])
            cap = r["final_asset"]
        return cap, per_win

    ARMS = []
    for n in sizes:
        ARMS.append((f"적합도 상위 {n}종목", "fit", n, None))
    for n in sizes:
        for sd in seeds:
            ARMS.append((f"[대조] 무작위 {n}종목 (씨드 {sd})", "rnd", n, sd))

    print(f"\n{'팔':<28}{'최종 자산배수':>14}{'연환산%':>9}{'창별 평균%':>11}"
          f"{'창 승수(44 대비)':>16}")
    base_win = {}
    out = {}
    for label, kind, n, sd in ARMS:
        if kind == "fit":
            fn = (lambda wi, _n=n: ranked[wi][:_n])
        else:
            def fn(wi, _n=n, _sd=sd):
                rows = list(ranked[wi])
                random.Random(_sd * 977 + wi).shuffle(rows)
                return rows[:_n]
        cap, wins = run_chain(fn)
        mult = cap / INITIAL_CAPITAL
        years = len(marks) * args.months / 12
        cagr = (mult ** (1 / years) - 1) * 100 if years > 0 and mult > 0 else float("nan")
        key = (kind, sd)
        if n == sizes[0]:
            base_win[key] = wins
            cmp_txt = "— (기준)"
        else:
            b = base_win.get(key, [])
            w = sum(1 for x, y in zip(wins, b) if x > y + 1e-9)
            cmp_txt = f"{w}-{len(wins) - w}"
        out[label] = (mult, cagr, float(np.mean(wins)), cmp_txt)
        print(f"{label:<28}{mult:>13.2f}x{cagr:>9.1f}{np.mean(wins):>11.1f}{cmp_txt:>16}",
              flush=True)

    print("\n[읽는 법] 같은 규칙·같은 시점 정보로 골랐으므로 두 팔의 차이는 **크기뿐**이다. "
          "80이 44를 이기면 크기 레버는 실제 운용에서도 유효하고, 지면 지금까지의 크기 이득은 "
          "선택 편향이었다는 뜻이다.")
    print("[주의] 창을 이어 붙였으므로(복리) 앞 창에서 벌면 뒤 창을 더 큰 자본으로 시작한다. "
          "창별 승수와 최종 배수를 함께 볼 것.")


if __name__ == "__main__":
    main()
