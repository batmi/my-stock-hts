"""하루 중 특정 시간대를 아예 보지 않는 것이 나은가 — 거래 시간대는 한 번도 안 쟀다.

[공백] `SYSTEM_TRADING_START_TIME`(0900) / `END_TIME`(1530)은 감사 도구 48개에 0회 등장한다.
 진입·청산의 '봉 시점'은 분봉으로 쟀지만(audit_entry_bars / audit_exit_bars), **하루 중
 어느 시간대를 아예 배제할 것인가**는 물어본 적이 없다. 개장 직후는 갭·호가 공백으로
 신호가 뒤집히기 쉽다는 통념이 있는데, 이 시스템에서 실제로 그런지 모른다.

[무엇을 재는가] 진입 스캔을 허용하는 봉 시각만 바꾼다. 청산은 모든 팔에서 동일하게 둔다
 — 시간대의 효과와 청산 체결 시점의 효과가 섞이면 무엇을 쟀는지 알 수 없다.
   A. 전 시간대 (현행 실매매)
   B. 개장 첫 봉 제외
   C. 개장 두 봉 제외
   D. 오전만 (12시 이전)
   E. 오후만 (12시 이후)

[한계] 분봉은 3년치(60m)뿐이라 10년 결론과 직접 비교하면 안 된다. 여기서 답하는 것은
 '같은 3년 안에서 시간대를 자르면 좋아지는가'이다. 게이트·커버리지는
 modules/intraday_bars.gate_universe 가 단독으로 판정한다.

[선행] tools/fetch_intraday_tv.py → tools/build_intraday_status.py

[실행] python3 tools/audit_time_of_day.py --interval 60m --trials 15 --sample 20
"""
import argparse
import os
import random
import sys

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import config  # noqa: E402
from modules import intraday_bars as ib  # noqa: E402
from modules import portfolio_backtest as pb  # noqa: E402
from tools.audit_defensive_sector import (  # noqa: E402
    INITIAL_CAPITAL, metrics, new_scale_fn_factory,
)


def build_arms(st, times):
    """봉 시각 집합에서 시간대별 팔을 만든다. 실제 존재하는 봉만 쓴다."""
    ts = sorted(times)
    noon = [t for t in ts if t < "1200"]
    after = [t for t in ts if t >= "1200"]
    return [
        ("A. 전 시간대 (현행)", set(ts)),
        ("B. 첫 봉 제외", set(ts[1:])),
        ("C. 첫 두 봉 제외", set(ts[2:])),
        ("D. 오전만", set(noon)),
        ("E. 오후만", set(after)),
    ]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--interval", default="60m")
    ap.add_argument("--days", type=int, default=1200)
    ap.add_argument("--trials", type=int, default=15)
    ap.add_argument("--sample", type=int, default=20)
    ap.add_argument("--seeds", default="20260816,7,101")
    ap.add_argument("--slots", type=int, default=None)
    ap.add_argument("--min-coverage", type=float, default=0.9)
    ap.add_argument("--subperiods", type=int, default=2)
    args = ap.parse_args()
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
    arms = build_arms(st, times)

    k = max(1, args.subperiods)
    size = max(1, len(dates) // k)
    W = [("전체", list(dates))]
    W += [(f"구간{i + 1}", dates[i * size:(i + 1) * size if i < k - 1 else len(dates)])
          for i in range(k)]

    codes = list(dfs)
    picks = {sd: [random.Random(sd * 19 + i).sample(codes, min(args.sample, len(codes)))
                  for i in range(args.trials)] for sd in seeds}

    print(f"\n표본 {args.sample}종목 · {args.trials}회 × 씨드 {len(seeds)}개 "
          f"(청산은 모든 팔 동일 · 진입 시각만 다름)")
    for wn, wd in W:
        print(f"\n########## {wn} ({len(wd)} 거래일) ##########")
        print(f"{'팔':<20}{'수익%':>9}{'MDD%':>8}{'MAR':>7}{'PF':>6}{'청산':>6}"
              f"{'상위10%':>9}{'승률%':>7}{'승-무-패':>10}")
        base_res = None
        for label, bar_times in arms:
            res = []
            for sd in seeds:
                for pick in picks[sd]:
                    r = pb.run_portfolio(
                        {c: dfs[c] for c in pick}, {c: status[c] for c in pick}, wd,
                        initial_capital=INITIAL_CAPITAL, slots=slots,
                        market_filter_dates={c: mf.get(c, set()) for c in pick},
                        risk_scale_by_date=new_scale(),
                        intraday_status={c: st[c] for c in pick},
                        intraday_entry=True, entry_bar_times=bar_times)
                    res.append(metrics(r))
            g = lambda key: float(np.mean([m[key] for m in res]))  # noqa: E731
            if base_res is None:
                base_res = res
                wl = "—"
            else:
                win = sum(1 for x, y in zip(res, base_res) if x["ret"] > y["ret"] + 1e-9)
                tie = sum(1 for x, y in zip(res, base_res) if abs(x["ret"] - y["ret"]) <= 1e-9)
                wl = f"{win}-{tie}-{len(res) - win - tie}"
            print(f"{label:<20}{g('ret'):>9.1f}{g('mdd'):>8.1f}{g('mar'):>7.2f}{g('pf'):>6.2f}"
                  f"{g('n'):>6.0f}{g('top10'):>9.1f}{g('win'):>7.1f}{wl:>10}", flush=True)

    print("\n[읽는 법] 시간대를 자르면 기회도 함께 줄어든다. 수익이 줄고 MDD도 줄면 그냥 "
          "덜 사는 것이고, 수익이 늘면서 줄면 그 시간대가 실제로 나쁜 것이다.")


if __name__ == "__main__":
    main()
