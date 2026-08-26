"""증액(피라미딩)을 하루 중 어느 봉에서 내는가 — 진입 시각 축의 짝인데 한 번도 안 쟀다.

[공백] tools/audit_time_of_day.py 는 **진입 봉 시각만** 잘랐다(entry_bar_times). 증액은
 모든 팔에서 전 봉 그대로였다. 그런데 실매매의 개장 지연 게이트
 (SYSTEM_ENTRY_OPEN_DELAY_USE)는 _check_buy_conditions 안에만 있고, 증액은
 _check_sell_conditions 안의 _try_pyramid_buy 로 호출되어 **게이트 밖**이다.
 즉 "개장 직후는 진입을 보류한다"가 증액에는 적용되지 않는데, 그 상태가 나은지 나쁜지를
 측정한 적이 없다. (실측 2026-08-26 09:01:30 한국콜마 증액 — 개장 1분 30초)

[무엇을 재는가] 진입 봉과 증액 봉을 2×2로 분해한다. 청산은 모든 팔에서 동일하다.
   A. 현행          — 진입 전 봉 · 증액 전 봉
   B. 진입만 첫 봉 제외 — audit_time_of_day 의 B와 같은 팔(게이트가 노린 것)
   C. 증액만 첫 봉 제외 — 게이트를 증액까지 넓혔을 때의 순효과
   D. 둘 다 첫 봉 제외  — 진입·증액 모두 보류

[봉 라벨 규약] intraday_bars.by_day 의 라벨은 **봉 시작 시각**이고(TV 규약; 09:00 봉의
 open = 일봉 시가로 확인), portfolio_backtest 는 그 봉의 **종가**에 체결한다
 (`px = srow["close"]`). 따라서 라벨 '0900'(30m) = 09:30 체결이고,
 '첫 봉 제외' = 실매매의 **60분** 지연이다. 30분 지연은 09:30 체결을 그대로 허용하므로
 이 세계에서는 A와 구분되지 않는다.

[한계] 분봉은 3년치라 10년 결론과 직접 비교하면 안 된다. 증액은 보유 중일 때만 나므로
 표본이 진입보다 훨씬 얇다 — 판정이 갈리지 않으면 '차이 없음'이 아니라 '못 갈랐다'로 읽을 것.

[실행] python3 tools/audit_pyramid_time_of_day.py --interval 30m --trials 5 --sample 20
"""
import argparse
import os
import random
import sys

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tools.audit_common import seed_notice, windows as audit_windows  # noqa: E402

import config  # noqa: E402
from modules import intraday_bars as ib  # noqa: E402
from modules import portfolio_backtest as pb  # noqa: E402
from tools.audit_defensive_sector import (  # noqa: E402
    INITIAL_CAPITAL, metrics, new_scale_fn_factory,
)


def build_arms(times):
    """진입 봉 × 증액 봉 2×2. None = 전 봉 허용(현행)."""
    ts = sorted(times)
    rest = set(ts[1:])
    return [
        ("A. 현행(진입·증액 전봉)", None, None),
        ("B. 진입만 첫봉 제외", rest, None),
        ("C. 증액만 첫봉 제외", None, rest),
        ("D. 둘 다 첫봉 제외", rest, rest),
    ]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--interval", default="30m")
    ap.add_argument("--days", type=int, default=1200)
    ap.add_argument("--trials", type=int, default=5)
    ap.add_argument("--sample", type=int, default=20)
    ap.add_argument("--seeds", default="20260816,7,101")
    ap.add_argument("--slots", type=int, default=None)
    ap.add_argument("--min-coverage", type=float, default=0.9)
    ap.add_argument("--subperiods", type=int, default=3)
    args = ap.parse_args()
    seed_notice(len(args.seeds.split(",")), example="--seeds 20260816,7,101")
    slots = args.slots or getattr(config, "SYSTEM_MAX_HOLDINGS", 4)
    seeds = [int(x) for x in args.seeds.split(",")]

    config.session.load_stock_config()
    stocks = config.session.stock_data.get("stocks_kr", [])
    names = {s["code"]: s["name"] for s in stocks}
    dfs, mf, dates, _f = pb.prepare_universe([(s["code"], s["name"]) for s in stocks], args.days)
    _bars, st, keep, drop = ib.gate_universe(dfs, args.interval, min_coverage=args.min_coverage)
    if drop:
        print(f"[제외] {len(drop)}종목 — " + ", ".join(f"{names.get(c, c)}({w})" for c, w in drop))
    dfs = {c: dfs[c] for c in keep}
    mf = {c: mf.get(c, set()) for c in keep}
    dates = ib.covered_dates(_bars, dates)
    if not dates:
        print("[중단] 겹치는 거래일 없음")
        return

    thr = {"BUY_SCORE": config.ANALYSIS_THRESHOLDS["BUY_SCORE"],
           "BUY_RSI_MAX": config.ANALYSIS_THRESHOLDS["BUY_RSI_MAX"],
           "RISE_SCORE": config.ANALYSIS_THRESHOLDS["RISE_SCORE"],
           "WEIGHTS": config.SCORING_WEIGHTS}
    status = pb.precompute_status(dfs, thr)
    new_scale = new_scale_fn_factory(dates, args.days)

    times = sorted({t for c in st for d in st[c] for t in st[c][d]})
    print(f"[준비] {len(dfs)}종목 · 거래일 {len(dates)} ({dates[0]}~{dates[-1]}) · "
          f"봉 시각 {', '.join(times)}")
    print(f"[규약] 첫 봉 '{times[0]}' 제외 = 실매매 지연 "
          f"{'60' if args.interval == '30m' else '120'}분 등가 (봉 종가 체결)")
    arms = build_arms(times)

    W = audit_windows(dates, args.subperiods, whole=True)
    codes = list(dfs)
    picks = {sd: [random.Random(sd * 19 + i).sample(codes, min(args.sample, len(codes)))
                  for i in range(args.trials)] for sd in seeds}

    print(f"\n표본 {args.sample}종목 · {args.trials}회 × 씨드 {len(seeds)}개 "
          f"(청산은 모든 팔 동일)")
    for wn, wd in W:
        print(f"\n########## {wn} ({len(wd)} 거래일) ##########")
        print(f"{'팔':<22}{'수익%':>9}{'MDD%':>8}{'MAR':>7}{'PF':>6}{'청산':>6}"
              f"{'증액':>6}{'상위10%':>9}{'승률%':>7}{'승-무-패':>10}")
        base_res = None
        for label, e_times, p_times in arms:
            res, pyr = [], []
            for sd in seeds:
                for pick in picks[sd]:
                    r = pb.run_portfolio(
                        {c: dfs[c] for c in pick}, {c: status[c] for c in pick}, wd,
                        initial_capital=INITIAL_CAPITAL, slots=slots,
                        market_filter_dates={c: mf.get(c, set()) for c in pick},
                        risk_scale_by_date=new_scale(),
                        intraday_status={c: st[c] for c in pick},
                        intraday_entry=True, entry_bar_times=e_times,
                        bar_pyr_times=p_times)
                    res.append(metrics(r))
                    pyr.append(r["pyramid_count"])
            g = lambda key: float(np.mean([m[key] for m in res]))  # noqa: E731
            if base_res is None:
                base_res = res
                wl = "—"
            else:
                win = sum(1 for x, y in zip(res, base_res) if x["ret"] > y["ret"] + 1e-9)
                tie = sum(1 for x, y in zip(res, base_res) if abs(x["ret"] - y["ret"]) <= 1e-9)
                wl = f"{win}-{tie}-{len(res) - win - tie}"
            print(f"{label:<22}{g('ret'):>9.1f}{g('mdd'):>8.1f}{g('mar'):>7.2f}{g('pf'):>6.2f}"
                  f"{g('n'):>6.0f}{float(np.mean(pyr)):>6.1f}{g('top10'):>9.1f}"
                  f"{g('win'):>7.1f}{wl:>10}", flush=True)

    print("\n[읽는 법] C(증액만 제외)가 A와 소수점까지 같으면 개장 첫 봉에 증액이 아예 없다는 "
          "뜻이므로 게이트 확장은 무동작이다. '증액' 열이 팔마다 다른데 성적이 같으면 "
          "증액 시각은 정보가 아니다. D가 B보다 나으면 게이트를 증액까지 넓힐 근거가 된다.")


if __name__ == "__main__":
    main()
