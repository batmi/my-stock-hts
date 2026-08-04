"""청산 다이얼 재검증 — 반납 상한 해제(2026-08-04) 이후 기존 값이 여전히 맞는가.

[왜 필요한가] 트레일링 반납 상한을 0.35 → 0.0으로 되돌리면서 주청산의 동작이 바뀌었다.
청산 다이얼끼리는 서로를 대체하는 관계라(배수를 넓히는 것 = 상한을 푸는 것 = 청산을
늦추는 일), 하나를 바꾸면 나머지의 최적값도 움직일 수 있다.

[무엇을 다시 보는가]
  A) TRAILING_ATR_MULTIPLIER — 2026-07-27에 3.5로 정했다. 당시 조건은 지금과 같지만
     (그때도 상한 0.0), 그 뒤 진입 쪽이 바뀌었다(시장필터 60일 → 80일+1% 밴드, 2026-08-03).
     포트폴리오 경로가 달라졌으므로 결론이 유지되는지 확인한다.
  B) BREAK_EVEN_STOP_RATE — 상한이 사라진 지금, 승자를 조기에 끊을 수 있는 장치는
     BEP만 남는다. '실측상 fat-tail 무손상'이라는 존치 근거가 지금도 성립하는지 본다.
  C) TRAILING_STOP_ACTIVATION_RATE — '배수 3.5에서는 10→15가 효과 정확히 0'이라는
     기록이 지금도 맞는지 확인한다.

[판정 기준] 원 결정과 같은 잣대를 쓴다. 총수익뿐 아니라
  · TS가 총이익에서 차지하는 비중 — '주청산은 샹들리에 TS'라는 설계가 유지되는가
  · 상위 10% 청산 평균 수익 / 최대 단일 수익 — fat-tail이 살아 있는가

[실행] python tools/audit_exit_dials.py [--trials 25] [--sample 25] [--days 1095]
"""
import argparse
import json
import os
import random
import sys

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import config  # noqa: E402
from modules import portfolio_backtest as pb  # noqa: E402

INITIAL_CAPITAL = 5_000_000
BEP_OFF = -999.0   # sl < bep_stop 이 성립할 수 없게 만들어 BEP를 무력화한다


def dial_sets():
    """(그룹, 라벨, {SELL_STRATEGY 키: 값}) — 기준선은 각 그룹의 '현행'."""
    return [
        ("A. TS ATR 배수", "2.5 (좁힘)",      {"TRAILING_ATR_MULTIPLIER": 2.5}),
        ("A. TS ATR 배수", "3.0",             {"TRAILING_ATR_MULTIPLIER": 3.0}),
        ("A. TS ATR 배수", "3.5 (현행)",      {"TRAILING_ATR_MULTIPLIER": 3.5}),
        ("A. TS ATR 배수", "4.0",             {"TRAILING_ATR_MULTIPLIER": 4.0}),
        ("A. TS ATR 배수", "5.0 (넓힘)",      {"TRAILING_ATR_MULTIPLIER": 5.0}),
        ("B. 본전청산(BEP)", "OFF",           {"BREAK_EVEN_STOP_RATE": BEP_OFF}),
        ("B. 본전청산(BEP)", "+0.5% (현행)",  {"BREAK_EVEN_STOP_RATE": 0.5}),
        ("B. 본전청산(BEP)", "+2.0% (강화)",  {"BREAK_EVEN_STOP_RATE": 2.0}),
        ("C. TS 발동 기준", "10% (현행)",     {"TRAILING_STOP_ACTIVATION_RATE": 10.0}),
        ("C. TS 발동 기준", "15%",            {"TRAILING_STOP_ACTIVATION_RATE": 15.0}),
        ("C. TS 발동 기준", "20%",            {"TRAILING_STOP_ACTIVATION_RATE": 20.0}),
    ]


def metrics(r):
    sells = [t for t in r["trades"] if t["reason"] != "매수"]
    profits = sorted((t["profit"] for t in sells), reverse=True)
    top10 = profits[:max(1, len(profits) // 10)]
    gross_gain = sum(t["profit_amt"] for t in sells if t["profit_amt"] > 0)
    ts_gain = sum(t["profit_amt"] for t in sells
                  if t["reason"] == "트레일링스탑" and t["profit_amt"] > 0)
    mdd = r["mdd"]
    return {
        "ret": r["total_return"], "mdd": mdd,
        "mar": r["total_return"] / abs(mdd) if mdd else float("nan"),
        "pf": r["pf"],
        "wr": r["win"] / max(1, r["win"] + r["loss"]) * 100,
        "top10": float(np.mean(top10)) if top10 else 0.0,
        "best": profits[0] if profits else 0.0,
        # 원 결정에서 4.0·5.0을 기각한 잣대 — TS가 총이익에서 차지하는 비중
        "ts_profit_share": (ts_gain / gross_gain * 100) if gross_gain > 0 else 0.0,
        "n": len(sells),
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--trials", type=int, default=25)
    ap.add_argument("--sample", type=int, default=25)
    ap.add_argument("--days", type=int, default=1095)
    ap.add_argument("--slots", type=int, default=None)
    ap.add_argument("--only", default=None, help="그룹 접두사만 실행 (예: A)")
    ap.add_argument("--subperiods", type=int, default=1,
                    help="거래일을 N등분해 구간별로 따로 잰다(과최적화 점검)")
    args = ap.parse_args()

    slots = args.slots or getattr(config, "SYSTEM_MAX_HOLDINGS", 4)
    data = json.load(open(config.STOCK_DATA_FILE))
    targets = [(s["code"], s["name"]) for s in data.get("stocks_kr", [])]
    print(f"[준비] 관심종목 {len(targets)}개 · {args.days}일 · 슬롯 {slots} · 시드 {INITIAL_CAPITAL:,}원")

    dfs, mf, dates, failed = pb.prepare_universe(targets, args.days)
    thresholds = {
        "BUY_SCORE": config.ANALYSIS_THRESHOLDS["BUY_SCORE"],
        "BUY_RSI_MAX": config.ANALYSIS_THRESHOLDS["BUY_RSI_MAX"],
        "RISE_SCORE": config.ANALYSIS_THRESHOLDS["RISE_SCORE"],
        "WEIGHTS": config.SCORING_WEIGHTS,
    }
    status = pb.precompute_status(dfs, thresholds)
    print(f"[준비] 사용 {len(dfs)}종목 / 거래일 {len(dates)}일"
          + (f" · 제외 {len(failed)}" if failed else ""))

    # 실제 운용 스택(국면×휩소율×드로다운 + 시장필터)에서 잰다.
    from tools.audit_drawdown_axis import market_scale_by_date, make_scale_fn
    p = getattr(config, "RISK_SCALING_PARAMS", {}) or {}
    mkt = market_scale_by_date(dates, args.days)
    dd = None
    if p.get("USE_DRAWDOWN_RISK_SCALING", True):
        dd = (int(p.get("DD_LOOKBACK_DAYS", 90)), float(p.get("DD_LEVEL_1", 5.0)),
              float(p.get("DD_SCALE_1", 0.9)), float(p.get("DD_LEVEL_2", 10.0)),
              float(p.get("DD_SCALE_2", 0.8)))
    print(f"[준비] 리스크 배수 적용 · 반납 상한 R={config.SELL_STRATEGY.get('TS_MAX_GIVEBACK_RATIO')}")

    sets = dial_sets()
    if args.only:
        sets = [x for x in sets if x[0].startswith(args.only)]
    saved = dict(config.SELL_STRATEGY)
    codes = list(dfs.keys())

    # 구간 분할 — 한 구간에서만 좋은 값을 채택하는 과최적화를 막는다.
    k = max(1, args.subperiods)
    size = len(dates) // k
    windows = [(f"구간{i+1}", dates[i * size:(i + 1) * size if i < k - 1 else len(dates)])
               for i in range(k)] if k > 1 else [("전체", dates)]

    all_results = {}
    for wname, wdates in windows:
        results = {(g, l): [] for g, l, _ in sets}
        rng = random.Random(20260804)
        try:
            for t in range(args.trials):
                pick = rng.sample(codes, min(args.sample, len(codes)))
                sd = {c: dfs[c] for c in pick}
                ss = {c: status[c] for c in pick}
                sm = {c: mf.get(c, set()) for c in pick}
                for g, label, overrides in sets:
                    config.SELL_STRATEGY.update(saved)  # 매번 현행으로 되돌린 뒤 한 다이얼만 바꾼다
                    config.SELL_STRATEGY.update(overrides)
                    r = pb.run_portfolio(sd, ss, wdates, initial_capital=INITIAL_CAPITAL,
                                         slots=slots, market_filter_dates=sm,
                                         risk_scale_by_date=make_scale_fn(mkt, dd))
                    results[(g, label)].append(metrics(r))
                print(f"  {wname} 시행 {t + 1}/{args.trials}", end="\r", flush=True)
        finally:
            config.SELL_STRATEGY.clear()
            config.SELL_STRATEGY.update(saved)
        all_results[wname] = results
    print(" " * 44, end="\r")

    print(f"\n{'=' * 102}")
    print(f"청산 다이얼 재검증 — {args.trials}회 × {args.sample}종목 무작위 짝비교 "
          f"(반납 상한 해제 후)")
    print(f"{'=' * 102}")

    for wname, results in all_results.items():
      if len(all_results) > 1:
        print(f"\n########## {wname} ({len(dict(windows)[wname])} 거래일) ##########")
      last_group = None
      for g, label, _ in sets:
        if g != last_group:
            base_label = next(l for gg, l, _ in sets if gg == g and "현행" in l)
            base = results[(g, base_label)]
            print(f"\n{g}  (기준선: {base_label})")
            print(f"{'설정':<16}{'수익%':>9}{'MDD%':>8}{'MAR':>7}{'PF':>6}{'승률%':>7}"
                  f"{'상위10%':>9}{'최대':>9}{'TS이익비중%':>12}{'청산':>6}{'수익승':>8}{'MAR승':>7}")
            print("-" * 102)
            last_group = g
        rs = results[(g, label)]
        m = lambda k: float(np.median([x[k] for x in rs]))  # noqa: E731
        is_base = "현행" in label
        rw = sum(1 for a, b in zip(rs, base) if a["ret"] > b["ret"])
        mw = sum(1 for a, b in zip(rs, base) if a["mar"] > b["mar"])
        print(f"{label:<16}{m('ret'):>9.1f}{m('mdd'):>8.1f}{m('mar'):>7.2f}{m('pf'):>6.2f}"
              f"{m('wr'):>7.1f}{m('top10'):>9.1f}{m('best'):>9.1f}"
              f"{m('ts_profit_share'):>12.1f}{m('n'):>6.0f}"
              f"{'—' if is_base else f'{rw}/{len(rs)}':>8}"
              f"{'—' if is_base else f'{mw}/{len(rs)}':>7}")

    print("\n" + "-" * 102)
    print("TS이익비중 = 트레일링 청산이 총이익에서 차지하는 비중. '주청산은 샹들리에 TS'가 유지되는지의 지표")
    print("            (2026-07-27에 배수 4.0·5.0을 기각한 잣대와 같다).")
    print("상위10%·최대 = fat-tail 보존 지표. 수익승·MAR승은 각 그룹의 현행 대비 개선 시행 수.")


if __name__ == "__main__":
    main()
