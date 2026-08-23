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

from tools.audit_common import windows as audit_windows  # noqa: E402

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


def rnd_gate(rate, salt):
    """[대조] 같은 차단량을 **아무 이유 없이** 무작위로 막는 게이트.

    봉 라벨 대조(build_placebo_arms)만으로는 부족하다 — 진입이 개장 봉에 몰려 있으면
    다른 봉을 잘라도 차단량이 0이라 '덜 사는 효과'가 대조에 들어오지 않는다. 그래서
    B가 실제로 줄인 진입 수만큼을 무작위로 막아 같은 양을 비교한다.
    결정적 난수라 같은 (날짜,종목)은 항상 같은 판정 — 팔 사이 비교가 재현된다.
    """
    def g(day, code, _held):
        return random.Random(f"{salt}|{day}|{code}").random() < rate
    return g


def build_placebo_arms(times):
    """[대조] '첫 봉 제외'와 **같은 구조**로 다른 봉 하나씩을 제외한 팔들.

    진입 필터는 같은 차단율 무작위 대조를 통과해야 채택한다(entry-filter-random-control).
    이 축에서는 난수 게이트보다 이쪽이 강한 대조다 — 차단 단위(하루 1개 봉)와 차단량이
    B와 같고 **시각 라벨만 다르기** 때문에, B의 이득이 '개장 직후라서'인지 '그냥 하루에
    한 봉 덜 사서'인지를 직접 가른다. B가 이 분포의 위쪽에 있지 않으면 개장 봉은 특별하지
    않다(= 채택 근거 없음).
    """
    ts = sorted(times)
    return [(f"  · {t} 제외", set(ts) - {t}) for t in ts[1:]]


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
    ap.add_argument("--placebo", action="store_true",
                    help="'첫 봉 제외'와 같은 구조로 다른 봉을 하나씩 자른 대조군을 함께 잰다")
    ap.add_argument("--placebo-draws", type=int, default=5,
                    help="같은 차단량을 무작위로 막는 대조를 몇 장 뽑을지 (--placebo와 함께)")
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
    placebo = build_placebo_arms(times) if args.placebo else []

    W = audit_windows(dates, args.subperiods, whole=True)

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
        by_label = {}
        for label, bar_times in arms + placebo:
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
            by_label[label] = res
            g = lambda key: float(np.mean([m[key] for m in res]))  # noqa: E731
            if base_res is None:
                base_res = res
                wl = "—"
            else:
                win = sum(1 for x, y in zip(res, base_res) if x["ret"] > y["ret"] + 1e-9)
                tie = sum(1 for x, y in zip(res, base_res) if abs(x["ret"] - y["ret"]) <= 1e-9)
                wl = f"{win}-{tie}-{len(res) - win - tie}"
            if label.startswith("  ·") and label == placebo[0][0]:
                print(f"{'[대조] 다른 봉 하나만 제외':<20}", flush=True)
            print(f"{label:<20}{g('ret'):>9.1f}{g('mdd'):>8.1f}{g('mar'):>7.2f}{g('pf'):>6.2f}"
                  f"{g('n'):>6.0f}{g('top10'):>9.1f}{g('win'):>7.1f}{wl:>10}", flush=True)

        if placebo:
            b_res = by_label["B. 첫 봉 제외"]
            b_ret = float(np.mean([m["ret"] for m in b_res]))
            rets = sorted(float(np.mean([m["ret"] for m in by_label[lb]])) for lb, _ in placebo)
            # B가 대조들을 짝비교로 몇 번이나 이기는지 — 평균만 보면 분산이 감춰진다.
            beat = sum(1 for lb, _ in placebo
                       if sum(1 for x, y in zip(b_res, by_label[lb])
                              if x["ret"] > y["ret"] + 1e-9) * 2 > len(b_res))
            rank = sum(1 for r in rets if r >= b_ret) + 1
            print(f"  [대조 요약] 첫 봉 제외 {b_ret:.1f}% vs 다른 봉 제외 {len(rets)}개: "
                  f"중앙 {np.median(rets):.1f}% (최소 {rets[0]:.1f} ~ 최대 {rets[-1]:.1f}) · "
                  f"순위 {rank}/{len(rets) + 1} · 짝비교 우세 {beat}/{len(rets)}", flush=True)

            # [같은 차단량 무작위 대조] B가 줄인 진입 수를 청산 건수 차이로 추정하고,
            #  그 비율만큼 무작위로 막는다. B가 이 대조까지 이겨야 '개장 직후'가 정보다.
            n_a = float(np.mean([m["n"] for m in base_res]))
            n_b = float(np.mean([m["n"] for m in b_res]))
            rate = max(0.0, (n_a - n_b) / n_a) if n_a > 0 else 0.0
            if rate <= 0:
                print("  [무작위 대조] B가 줄인 진입이 없어 대조를 세울 수 없다(비교 불가).",
                      flush=True)
            else:
                print(f"[대조] 같은 차단량({rate * 100:.1f}%) 무작위", flush=True)
                r_rets, r_beat = [], 0
                for j in range(max(1, args.placebo_draws)):
                    res = []
                    for sd in seeds:
                        for pick in picks[sd]:
                            r = pb.run_portfolio(
                                {c: dfs[c] for c in pick}, {c: status[c] for c in pick}, wd,
                                initial_capital=INITIAL_CAPITAL, slots=slots,
                                market_filter_dates={c: mf.get(c, set()) for c in pick},
                                risk_scale_by_date=new_scale(),
                                intraday_status={c: st[c] for c in pick},
                                intraday_entry=True, entry_bar_times=arms[0][1],
                                entry_gate=rnd_gate(rate, f"rnd|{wn}|{j}"))
                            res.append(metrics(r))
                    g2 = lambda key: float(np.mean([m[key] for m in res]))  # noqa: E731
                    win = sum(1 for x, y in zip(res, base_res) if x["ret"] > y["ret"] + 1e-9)
                    tie = sum(1 for x, y in zip(res, base_res) if abs(x["ret"] - y["ret"]) <= 1e-9)
                    r_rets.append(g2("ret"))
                    if sum(1 for x, y in zip(b_res, res) if x["ret"] > y["ret"] + 1e-9) * 2 > len(b_res):
                        r_beat += 1
                    print(f"{'  · 무작위 ' + str(j + 1) + '장':<20}{g2('ret'):>9.1f}{g2('mdd'):>8.1f}"
                          f"{g2('mar'):>7.2f}{g2('pf'):>6.2f}{g2('n'):>6.0f}{g2('top10'):>9.1f}"
                          f"{g2('win'):>7.1f}{f'{win}-{tie}-{len(res) - win - tie}':>10}", flush=True)
                print(f"  [무작위 대조 요약] 첫 봉 제외 {b_ret:.1f}% vs 무작위 "
                      f"{len(r_rets)}장 중앙 {np.median(r_rets):.1f}% "
                      f"(최소 {min(r_rets):.1f} ~ 최대 {max(r_rets):.1f}) · "
                      f"B 우세 {r_beat}/{len(r_rets)}", flush=True)

    print("\n[읽는 법] 시간대를 자르면 기회도 함께 줄어든다. 수익이 줄고 MDD도 줄면 그냥 "
          "덜 사는 것이고, 수익이 늘면서 줄면 그 시간대가 실제로 나쁜 것이다.")
    if placebo:
        print("[대조 읽는 법] '첫 봉 제외'가 기준선을 이기는 것만으로는 부족하다. 같은 구조로 "
              "아무 봉이나 하나 자른 대조들보다 위에 있어야 개장 직후라는 라벨이 정보를 갖는다 "
              "— 순위가 중간이면 그저 '하루에 한 봉 덜 산' 효과다.")


if __name__ == "__main__":
    main()
