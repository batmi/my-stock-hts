"""지표의 계산 기간 자체에 근거가 있는가 — RSI 14 · MACD 12/26/9 · ADX 14는 왜 그 값인가.

[공백] 스코어링 항목 30개와 파라미터 13개는 전수 검증했지만, 그것은 '어떤 지표에 몇 점을
 주느냐'였다. **지표를 어떤 기간으로 계산하느냐**(`INDICATOR_PARAMS`의 RSI_PERIOD,
 MACD_FAST/SLOW/SIGNAL, ADX_PERIOD, ATR_PERIOD, CCI_WINDOW…)는 감사 도구 48개에 0회
 등장한다. 전부 업계 표준값이고 근거 기록도 '표준'뿐이다.

[왜 조심해야 하는가] 이 축은 **과최적화 위험이 가장 큰 축**이다. 12개 지표의 기간을 훑으면
 어떤 조합이든 이긴다 — 그래서 여기서는 축을 셋(RSI·MACD·ADX)으로 좁히고, 각각 표준값
 주변의 상식적인 값만 본다. 채택 기준도 평소보다 높인다: **전체 창 승 + 하위 구간 전부 승**이
 아니면 현행 유지다. ATR_PERIOD는 손절폭에 직결되어 이미 다른 감사(손절 축)와 얽히므로
 여기서는 건드리지 않는다.

[어떻게 재는가] 팔마다 지표를 **다시 계산**해야 하므로(prepare_universe + precompute_status
 재실행) 비용이 크다. 같은 종목·같은 창·같은 표본 추첨을 공유해 짝비교한다.

[실행] python3 tools/audit_indicator_periods.py --axis rsi,macd,adx --trials 10 --sample 25
"""
import argparse
import copy
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

AXES = {
    "rsi": [("RSI 14 (현행)", {}),
            ("RSI 9", {"RSI_PERIOD": 9}),
            ("RSI 21", {"RSI_PERIOD": 21})],
    "macd": [("MACD 12/26/9 (현행)", {}),
             ("MACD 8/17/9 (빠름)", {"MACD_FAST": 8, "MACD_SLOW": 17}),
             ("MACD 19/39/9 (느림)", {"MACD_FAST": 19, "MACD_SLOW": 39})],
    "adx": [("ADX 14 (현행)", {}),
            ("ADX 10", {"ADX_PERIOD": 10}),
            ("ADX 20", {"ADX_PERIOD": 20})],
}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--axis", default="rsi,macd,adx")
    ap.add_argument("--days", type=int, default=3650)
    ap.add_argument("--trials", type=int, default=10)
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
    orig = copy.deepcopy(config.INDICATOR_PARAMS)

    # 창·표본은 팔 사이에서 공유해야 한다 — 지표가 바뀌면 준비 결과도 바뀌므로
    #  기준 팔(현행)로 한 번 준비해 거래일과 종목 목록을 고정한다.
    base_dfs, base_mf, dates, _f = pb.prepare_universe(live, args.days)
    codes = list(base_dfs)
    print(f"[준비] {len(codes)}종목 · 거래일 {len(dates)} · 슬롯 {slots}", flush=True)
    picks = {sd: [random.Random(sd * 23 + i).sample(codes, min(args.sample, len(codes)))
                  for i in range(args.trials)] for sd in seeds}
    new_scale = new_scale_fn_factory(dates, args.days)

    W = audit_windows(dates, args.subperiods, whole=True)

    for axis in args.axis.split(","):
        arms = AXES.get(axis.strip())
        if not arms:
            continue
        print(f"\n\n=========== 축 {axis.upper()} ===========")
        prepared = {}
        for label, ov in arms:
            config.INDICATOR_PARAMS.clear()
            config.INDICATOR_PARAMS.update(copy.deepcopy(orig))
            config.INDICATOR_PARAMS.update(ov)
            dfs, mf, _d, _fail = pb.prepare_universe(live, args.days)
            thr = {"BUY_SCORE": config.ANALYSIS_THRESHOLDS["BUY_SCORE"],
                   "BUY_RSI_MAX": config.ANALYSIS_THRESHOLDS["BUY_RSI_MAX"],
                   "RISE_SCORE": config.ANALYSIS_THRESHOLDS["RISE_SCORE"],
                   "WEIGHTS": config.SCORING_WEIGHTS}
            prepared[label] = (dfs, mf, pb.precompute_status(dfs, thr))
            print(f"  [준비] {label}", flush=True)
        config.INDICATOR_PARAMS.clear()
        config.INDICATOR_PARAMS.update(copy.deepcopy(orig))

        for wn, wd in W:
            print(f"\n########## {wn} ({len(wd)} 거래일) ##########")
            print(f"{'팔':<24}{'수익%':>9}{'MDD%':>8}{'MAR':>7}{'PF':>6}{'청산':>6}"
                  f"{'상위10%':>9}{'승률%':>7}{'승-무-패':>10}")
            base_res = None
            for label, _ov in arms:
                dfs, mf, status = prepared[label]
                res = []
                for sd in seeds:
                    for pick in picks[sd]:
                        pick = [c for c in pick if c in dfs]
                        r = pb.run_portfolio(
                            {c: dfs[c] for c in pick}, {c: status[c] for c in pick}, wd,
                            initial_capital=INITIAL_CAPITAL, slots=slots,
                            market_filter_dates={c: mf.get(c, set()) for c in pick},
                            risk_scale_by_date=new_scale())
                        res.append(metrics(r))
                g = lambda key: float(np.mean([m[key] for m in res]))  # noqa: E731
                if base_res is None:
                    base_res = res
                    wl = "—"
                else:
                    win = sum(1 for x, y in zip(res, base_res) if x["ret"] > y["ret"] + 1e-9)
                    tie = sum(1 for x, y in zip(res, base_res)
                              if abs(x["ret"] - y["ret"]) <= 1e-9)
                    wl = f"{win}-{tie}-{len(res) - win - tie}"
                print(f"{label:<24}{g('ret'):>9.1f}{g('mdd'):>8.1f}{g('mar'):>7.2f}"
                      f"{g('pf'):>6.2f}{g('n'):>6.0f}{g('top10'):>9.1f}{g('win'):>7.1f}"
                      f"{wl:>10}", flush=True)

    print("\n[채택 기준] 전체 창 승 + 하위 구간 전부 승이 아니면 현행 유지. 이 축은 "
          "훑기만 해도 뭔가는 이기므로, 평소보다 높은 문턱을 미리 못 박아 둔다.")


if __name__ == "__main__":
    main()
