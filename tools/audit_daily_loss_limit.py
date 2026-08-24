"""일일 손실 한도(방어 모드)에 근거가 있는가 — 10%는 어디서 온 숫자인가.

[구조적 공백] `SYSTEM_DAILY_LOSS_LIMIT`(기본 10.0)은 engine.check_loss_limit 에서
 '그날 시작 자산 대비 -10%'에 닿으면 **신규 매수를 멈추고 청산 감시만 남기는** 방어 모드를
 켠다. 그런데 portfolio_backtest 에는 이 개념이 없었고(키 0회), 감사 도구 48개에도 0회다.
 10년 동안 이 스위치가 몇 번 켜졌을지, 켜져서 드로다운을 줄였는지 반등을 놓쳤는지
 **한 번도 측정된 적이 없다.** 값 10%도 근거 기록이 없다.

[무엇을 재는가] run_portfolio 의 daily_loss_limit 훅으로 같은 규칙을 재현한다. 일봉
 세계에서는 '전일 종가 자산 대비 오늘 종가 자산'이 그날의 손실률이다(실매매는 장중에
 발동하므로, 이 재현은 **발동 빈도를 과소평가**한다 — 장중에 -10%를 찍고 종가에 회복한
 날은 잡히지 않는다. 방향은 '보수적으로 적게 발동').

  팔1 없음(현행 백테스트) · 팔2 -10%(현행 실매매) · 팔3 -7% · 팔4 -5% · 팔5 -15%
  문턱을 좁히면 발동이 늘어난다. 늘어난 발동이 무엇을 바꾸는지 본다.

[읽는 법] 이 장치는 수익을 늘리려고 다는 것이 아니라 파산을 막으려고 다는 것이다.
 그래서 판정 기준은 총수익이 아니라 **MDD와 최악 구간**이다. 수익이 조금 깎여도 MDD가
 유의미하게 줄면 보험료로 볼 수 있고, 수익도 MDD도 그대로면 그냥 안 걸리는 장치다.

[실행] python3 tools/audit_daily_loss_limit.py --trials 12 --sample 25 --seeds 3
"""
import argparse
import os
import random
import sys

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tools.audit_common import seed_notice, windows as audit_windows  # noqa: E402

import config  # noqa: E402
from modules import portfolio_backtest as pb  # noqa: E402
from tools.audit_defensive_sector import (  # noqa: E402
    INITIAL_CAPITAL, metrics, new_scale_fn_factory,
)

ARMS = [("없음 (현행 백테스트)", None), ("-10% (현행 실매매)", 10.0),
        ("-7%", 7.0), ("-5%", 5.0), ("-15%", 15.0)]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=3650)
    ap.add_argument("--trials", type=int, default=12)
    ap.add_argument("--sample", type=int, default=25)
    ap.add_argument("--seeds", default="20260816,7,101")
    ap.add_argument("--slots", type=int, default=None)
    ap.add_argument("--subperiods", type=int, default=3)
    args = ap.parse_args()
    seed_notice(len(args.seeds.split(",")), example="--seeds 20260816,7,101")
    slots = args.slots or getattr(config, "SYSTEM_MAX_HOLDINGS", 4)
    seeds = [int(x) for x in args.seeds.split(",")]

    config.session.load_stock_config()
    live = [(s["code"], s["name"]) for s in config.session.stock_data.get("stocks_kr", [])]
    dfs, mf, dates, failed = pb.prepare_universe(live, args.days)
    print(f"[준비] {len(dfs)}종목 · 거래일 {len(dates)} · 슬롯 {slots}"
          + (f" · 제외 {failed}" if failed else ""), flush=True)

    thr = {"BUY_SCORE": config.ANALYSIS_THRESHOLDS["BUY_SCORE"],
           "BUY_RSI_MAX": config.ANALYSIS_THRESHOLDS["BUY_RSI_MAX"],
           "RISE_SCORE": config.ANALYSIS_THRESHOLDS["RISE_SCORE"],
           "WEIGHTS": config.SCORING_WEIGHTS}
    status = pb.precompute_status(dfs, thr)
    new_scale = new_scale_fn_factory(dates, args.days)

    W = audit_windows(dates, args.subperiods, whole=True)

    codes = list(dfs)
    picks = {sd: [random.Random(sd * 17 + i).sample(codes, min(args.sample, len(codes)))
                  for i in range(args.trials)] for sd in seeds}

    # 발동 빈도부터 — 안 걸리는 장치면 나머지 비교는 의미가 없다.
    print("\n[0] 발동 빈도 — 표본별로 '전일 대비 종가 자산 -X% 이하'인 날이 며칠인가")
    eq_days = []
    for sd in seeds[:1]:
        for pick in picks[sd][:6]:
            r = pb.run_portfolio({c: dfs[c] for c in pick}, {c: status[c] for c in pick},
                                 dates, initial_capital=INITIAL_CAPITAL, slots=slots,
                                 market_filter_dates={c: mf.get(c, set()) for c in pick},
                                 risk_scale_by_date=new_scale())
            eq = np.array(r["equity"], dtype=float)
            chg = np.diff(eq) / np.maximum(eq[:-1], 1) * 100
            eq_days.append(chg)
    chg = np.concatenate(eq_days) if eq_days else np.array([0.0])
    print(f"{'문턱':<10}{'발동일':>8}{'전체일':>8}{'비율%':>8}")
    for lim in (5.0, 7.0, 10.0, 15.0):
        n = int((chg <= -lim).sum())
        print(f"{f'-{lim:.0f}%':<10}{n:>8}{len(chg):>8}{n / len(chg) * 100:>8.2f}")
    print(f"  (참고) 일간 자산 변동 최악 {chg.min():.2f}% · 1퍼센타일 {np.percentile(chg, 1):.2f}%")

    print(f"\n표본 {args.sample}종목 · {args.trials}회 × 씨드 {len(seeds)}개")
    for wn, wd in W:
        print(f"\n########## {wn} ({len(wd)} 거래일) ##########")
        print(f"{'팔':<20}{'수익%':>9}{'MDD%':>8}{'MAR':>7}{'PF':>6}{'청산':>6}"
              f"{'상위10%':>9}{'승-무-패':>10}{'MDD개선':>9}")
        base_res = None
        for label, lim in ARMS:
            res = []
            for sd in seeds:
                for pick in picks[sd]:
                    r = pb.run_portfolio(
                        {c: dfs[c] for c in pick}, {c: status[c] for c in pick}, wd,
                        initial_capital=INITIAL_CAPITAL, slots=slots,
                        market_filter_dates={c: mf.get(c, set()) for c in pick},
                        risk_scale_by_date=new_scale(), daily_loss_limit=lim)
                    res.append(metrics(r))
            g = lambda key: float(np.mean([m[key] for m in res]))  # noqa: E731
            if base_res is None:
                base_res = res
                wl, dd = "—", "—"
            else:
                win = sum(1 for x, y in zip(res, base_res) if x["ret"] > y["ret"] + 1e-9)
                tie = sum(1 for x, y in zip(res, base_res) if abs(x["ret"] - y["ret"]) <= 1e-9)
                wl = f"{win}-{tie}-{len(res) - win - tie}"
                better = sum(1 for x, y in zip(res, base_res) if x["mdd"] > y["mdd"] + 1e-9)
                dd = f"{better}/{len(res)}"
            print(f"{label:<20}{g('ret'):>9.1f}{g('mdd'):>8.1f}{g('mar'):>7.2f}{g('pf'):>6.2f}"
                  f"{g('n'):>6.0f}{g('top10'):>9.1f}{wl:>10}{dd:>9}", flush=True)

    print("\n[읽는 법] 이 장치의 값어치는 MDD와 최악 구간에 있다. 수익·MDD가 둘 다 그대로면 "
          "'거의 걸리지 않는 보험'이고, 그렇다면 값을 흔들 이유도 없다.")


if __name__ == "__main__":
    main()
