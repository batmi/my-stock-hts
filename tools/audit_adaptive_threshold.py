"""적응형 임계값(시장 국면별 매수 문턱 ±0.5)에 근거가 있는가.

[구조적 공백] `MARKET_REGIME_PARAMS`의 국면 보정은 engine.build_buy_thresholds 에서
 `BUY_SCORE + score_adj` 로 매수 문턱에 직접 더해진다 — 상승 확정이면 6.5점에 사고,
 하락 확정·하락 미확정이면 7.5점을 요구한다. 그런데 portfolio_backtest 에는 이 개념이
 아예 없다(SCORE_ADJ 0회). **지금까지 정한 모든 다이얼은 문턱 7.0 고정 세계에서
 최적화됐고, 실매매는 다른 문턱으로 산다.** `USE_ADAPTIVE_THRESHOLD` 라는 문자열은
 감사 도구 48개 어디에도 없었다.

[무엇을 재는가] run_portfolio 의 buy_score_fn 훅으로 날짜×시장별 문턱을 실매매와 같은
 규칙으로 흔든다. 국면 판정은 프록시가 아니라 운영 코드(analysis.classify_regime_from_df)를
 그대로 호출하고, 실매매처럼 **직전 250봉만** 보고 판정한다(선견 차단).
  팔1 고정 7.0 (현행 백테스트 = 지금까지의 모든 결론이 선 지반)
  팔2 적응형 (현행 실매매: Bull −0.5 · Bear/PendDown +0.5)
  팔3 역방향 (Bull +0.5 · Bear/PendDown −0.5) — 방향이 정보인지 보는 대조군.
      팔2가 이기는데 팔3도 이기면 이긴 것은 방향이 아니라 '문턱을 흔든다'는 사실이다.
  팔4 강화만 (Bear/PendDown +0.5, 상승장 완화 없음) — 어느 쪽 반쪽이 일하는지 가른다.

[종목의 시장 구분] 실매매는 종목이 속한 시장(KOSPI/KOSDAQ)의 국면을 쓴다. 여기서도
 FDR 상장 목록으로 종목→시장을 만들어 같게 맞춘다.

[실행] python3 tools/audit_adaptive_threshold.py --trials 12 --sample 25 --seeds 3
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
from tools.audit_market_axes import load_index, regime_series  # noqa: E402

ARMS = [
    ("고정 7.0 (현행 백테스트)", None),
    ("적응형 (현행 실매매)", {"Bull": -0.5, "PendUp": 0.0, "PendDown": 0.5, "Bear": 0.5}),
    ("역방향 (대조군)", {"Bull": 0.5, "PendUp": 0.0, "PendDown": -0.5, "Bear": -0.5}),
    ("강화만 (완화 없음)", {"Bull": 0.0, "PendUp": 0.0, "PendDown": 0.5, "Bear": 0.5}),
]


def regime_by_date(dates, days):
    """날짜 → {시장: 국면}. 실매매와 같은 판정 함수·같은 창(250봉)을 쓴다."""
    from datetime import datetime, timedelta
    start = (datetime.now() - timedelta(days=days + 400)).strftime("%Y-%m-%d")
    out = {"KOSPI": {}, "KOSDAQ": {}}
    for ticker, mkt in (("KS11", "KOSPI"), ("KQ11", "KOSDAQ")):
        idx, close = load_index(ticker, start)
        regimes, _whip = regime_series(idx, close)
        for d, r in zip(idx.strftime("%Y%m%d"), regimes):
            out[mkt][d] = str(r)
    # 지수 휴장일 등으로 비는 날은 직전 판정을 잇는다(실운영도 마지막 판정을 유지한다).
    filled = {}
    for mkt in out:
        last = "Sideways"
        filled[mkt] = {}
        for d in dates:
            last = out[mkt].get(d, last)
            filled[mkt][d] = last
    return filled


def market_map(codes):
    import FinanceDataReader as fdr
    df = fdr.StockListing("KRX")
    m = dict(zip(df["Code"], df["Market"]))
    return {c: ("KOSDAQ" if str(m.get(c, "KOSPI")).upper().startswith("KOSDAQ") else "KOSPI")
            for c in codes}


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
    base_score = thr["BUY_SCORE"]

    reg = regime_by_date(dates, args.days)
    mkt = market_map(list(dfs))
    cnt = {}
    for d in dates:
        cnt[reg["KOSPI"][d]] = cnt.get(reg["KOSPI"][d], 0) + 1
    print("[준비] KOSPI 국면 분포: "
          + " · ".join(f"{k} {v}일({v / len(dates) * 100:.0f}%)" for k, v in sorted(cnt.items())))

    new_scale = new_scale_fn_factory(dates, args.days)

    W = audit_windows(dates, args.subperiods, whole=True)

    def make_fn(adj):
        if adj is None:
            return None

        def fn(day, code):
            return base_score + adj.get(reg[mkt.get(code, "KOSPI")][day], 0.0)
        return fn

    codes = list(dfs)
    picks = {sd: [random.Random(sd * 13 + i).sample(codes, min(args.sample, len(codes)))
                  for i in range(args.trials)] for sd in seeds}

    print(f"\n표본 {args.sample}종목 · {args.trials}회 × 씨드 {len(seeds)}개 "
          f"(기준선 = 고정 {base_score})")
    for wn, wd in W:
        print(f"\n########## {wn} ({len(wd)} 거래일) ##########")
        print(f"{'팔':<24}{'수익%':>9}{'MDD%':>8}{'MAR':>7}{'PF':>6}{'청산':>6}"
              f"{'상위10%':>9}{'현금%':>7}{'승-무-패':>10}")
        base_res = None
        for label, adj in ARMS:
            res = []
            fn = make_fn(adj)
            for sd in seeds:
                for pick in picks[sd]:
                    r = pb.run_portfolio(
                        {c: dfs[c] for c in pick}, {c: status[c] for c in pick}, wd,
                        initial_capital=INITIAL_CAPITAL, slots=slots,
                        market_filter_dates={c: mf.get(c, set()) for c in pick},
                        risk_scale_by_date=new_scale(), buy_score_fn=fn)
                    res.append(metrics(r))
            g = lambda key: float(np.mean([m[key] for m in res]))  # noqa: E731
            if base_res is None:
                base_res = res
                wl = "—"
            else:
                win = sum(1 for x, y in zip(res, base_res) if x["ret"] > y["ret"] + 1e-9)
                tie = sum(1 for x, y in zip(res, base_res) if abs(x["ret"] - y["ret"]) <= 1e-9)
                wl = f"{win}-{tie}-{len(res) - win - tie}"
            print(f"{label:<24}{g('ret'):>9.1f}{g('mdd'):>8.1f}{g('mar'):>7.2f}{g('pf'):>6.2f}"
                  f"{g('n'):>6.0f}{g('top10'):>9.1f}{g('slots'):>7.2f}{wl:>10}", flush=True)

    print("\n[읽는 법] 팔2(적응형)가 팔1을 이기고 팔3(역방향)이 지면 국면 보정의 '방향'이 "
          "정보다. 둘 다 이기면 이긴 것은 방향이 아니라 문턱을 흔든 것 자체다.")


if __name__ == "__main__":
    main()
