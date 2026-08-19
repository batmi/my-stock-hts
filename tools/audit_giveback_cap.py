"""트레일링 반납 상한(TS_MAX_GIVEBACK_RATIO)을 쓰는 것이 맞는가.

[무엇인가] 샹들리에 TS의 콜백은 ATR로 정해지는데, 여기에 '최고 수익의 R배 이상은
반납하지 않는다'는 상한을 덧씌운다(engine.giveback_callback_cap). R=0이면 상한 해제
= 순수 샹들리에.

[왜 재는가] 이 시스템의 주청산은 샹들리에 TS이고, 전략의 수익 구조는 fat-tail 추종이다
(소수의 큰 추세가 다수의 작은 손실을 덮는다). 반납 상한은 그 fat-tail을 **의도적으로
잘라내는** 장치이므로, 전략의 전제와 정면으로 맞물린다. 실측상 상한을 켜면 트레일링
청산 비중이 13% → 28%로 뛴다 — 승자를 훨씬 일찍 자른다는 뜻이다.

[무엇을 보는가] 총수익·MDD만으로는 판단할 수 없다. fat-tail이 살아 있는지를 직접 본다.
  · 상위 10% 청산의 평균 수익률, 최대 단일 수익률 — 큰 추세를 끝까지 탔는가
  · MAR(수익/MDD) — 위험조정 성과
  · 최악 시행 MDD — 꼬리 위험

[실행] python tools/audit_giveback_cap.py [--trials 25] [--sample 25] [--days 1095]
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

from tools.audit_common import exits  # noqa: E402

INITIAL_CAPITAL = 10_000_000  # 실거래 시드와 같게 둔다(seed-slot-sizing)

RATIOS = [
    ("상한 없음(순수 샹들리에)", 0.0),
    ("R=0.20 (강한 상한)", 0.20),
    ("R=0.35 (현행)", 0.35),
    ("R=0.50 (약한 상한)", 0.50),
]


def metrics(r):
    sells = exits(r)
    profits = sorted((t["profit"] for t in sells), reverse=True)
    top10 = profits[:max(1, len(profits) // 10)]
    ts_share = (sum(1 for t in sells if t["reason"] == "트레일링스탑")
                / max(1, len(sells)) * 100)
    mdd = r["mdd"]
    return {
        "ret": r["total_return"],
        "mdd": mdd,
        "mar": r["total_return"] / abs(mdd) if mdd else float("nan"),
        "pf": r["pf"],
        "wr": r["win"] / max(1, r["win"] + r["loss"]) * 100,
        "top10": float(np.mean(top10)) if top10 else 0.0,
        "best": profits[0] if profits else 0.0,
        "ts_share": ts_share,
        "n": len(sells),
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--trials", type=int, default=25)
    ap.add_argument("--sample", type=int, default=25)
    ap.add_argument("--days", type=int, default=1095)
    ap.add_argument("--slots", type=int, default=None)
    ap.add_argument("--seed-capital", type=int, default=INITIAL_CAPITAL,
                    help="시드(원). 정수 주식수 양자화 때문에 결론이 시드에 좌우될 수 있다")
    ap.add_argument("--with-risk-scaling", action="store_true",
                    help="국면×휩소율×드로다운 배수를 함께 적용한다(실제 운용 스택). "
                         "상한 없이 돌렸을 때의 꼬리가 이 층들로 얼마나 눌리는지 본다.")
    args = ap.parse_args()

    slots = args.slots or getattr(config, "SYSTEM_MAX_HOLDINGS", 4)
    data = json.load(open(config.STOCK_DATA_FILE))
    targets = [(s["code"], s["name"]) for s in data.get("stocks_kr", [])]
    print(f"[준비] 관심종목 {len(targets)}개 · {args.days}일 · 슬롯 {slots} · 시드 {args.seed_capital:,}원")

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

    # 실제 운용 스택 재현 — 국면×휩소율×드로다운 배수. 상한을 뺐을 때의 꼬리가
    #  이 층들로 얼마나 눌리는지가 '상한을 뺄 수 있는가'의 실질 판단 근거다.
    mkt, dd_params = None, None
    if args.with_risk_scaling:
        from tools.audit_drawdown_axis import market_scale_by_date, make_scale_fn
        p = getattr(config, "RISK_SCALING_PARAMS", {}) or {}
        mkt = market_scale_by_date(dates, args.days)
        if p.get("USE_DRAWDOWN_RISK_SCALING", True):
            dd_params = (int(p.get("DD_LOOKBACK_DAYS", 90)), float(p.get("DD_LEVEL_1", 5.0)),
                         float(p.get("DD_SCALE_1", 0.9)), float(p.get("DD_LEVEL_2", 10.0)),
                         float(p.get("DD_SCALE_2", 0.8)))
        print(f"[준비] 리스크 배수 적용 — 국면×휩소율 평균 {np.mean(list(mkt.values())):.3f}"
              f" · 드로다운 {dd_params}")
        globals()["_make_fn"] = lambda: make_scale_fn(mkt, dd_params)

    codes = list(dfs.keys())
    results = {name: [] for name, _ in RATIOS}
    saved = config.SELL_STRATEGY.get("TS_MAX_GIVEBACK_RATIO")
    rng = random.Random(20260804)
    try:
        for t in range(args.trials):
            pick = rng.sample(codes, min(args.sample, len(codes)))
            sd = {c: dfs[c] for c in pick}
            ss = {c: status[c] for c in pick}
            sm = {c: mf.get(c, set()) for c in pick}
            for name, ratio in RATIOS:
                config.SELL_STRATEGY["TS_MAX_GIVEBACK_RATIO"] = ratio
                # 배수 콜러블은 시행마다 새로 만든다(자산곡선 이력을 누적하므로 재사용 금지).
                scale_fn = globals()["_make_fn"]() if args.with_risk_scaling else None
                r = pb.run_portfolio(sd, ss, dates, initial_capital=args.seed_capital,
                                     slots=slots, market_filter_dates=sm,
                                     risk_scale_by_date=scale_fn)
                results[name].append(metrics(r))
            print(f"  시행 {t + 1}/{args.trials} 완료", end="\r", flush=True)
    finally:
        config.SELL_STRATEGY["TS_MAX_GIVEBACK_RATIO"] = saved
    print(" " * 40, end="\r")

    base = results["상한 없음(순수 샹들리에)"]
    print(f"\n{'=' * 104}")
    print(f"트레일링 반납 상한 감사 — {args.trials}회 × {args.sample}종목 무작위 짝비교 "
          f"(현재 config = {saved})")
    print(f"{'=' * 104}")
    print(f"{'설정':<26}{'수익%':>9}{'MDD%':>8}{'MAR':>7}{'PF':>6}{'승률%':>7}"
          f"{'상위10%':>9}{'최대':>9}{'TS비중%':>9}{'청산':>6}{'수익승':>8}{'MAR승':>7}")
    print("-" * 104)
    for name, _ in RATIOS:
        rs = results[name]
        m = lambda k: float(np.median([x[k] for x in rs]))  # noqa: E731
        is_base = name.startswith("상한 없음")
        rw = sum(1 for a, b in zip(rs, base) if a["ret"] > b["ret"])
        mw = sum(1 for a, b in zip(rs, base) if a["mar"] > b["mar"])
        print(f"{name:<26}{m('ret'):>9.1f}{m('mdd'):>8.1f}{m('mar'):>7.2f}{m('pf'):>6.2f}"
              f"{m('wr'):>7.1f}{m('top10'):>9.1f}{m('best'):>9.1f}{m('ts_share'):>9.1f}"
              f"{m('n'):>6.0f}"
              f"{'—' if is_base else f'{rw}/{len(rs)}':>8}"
              f"{'—' if is_base else f'{mw}/{len(rs)}':>7}")
    print("-" * 104)
    print("상위10% = 수익 상위 10% 청산의 평균 수익률(%). '큰 추세를 끝까지 탔는가'의 지표.")
    print("최대 = 단일 청산 최대 수익률(%). 수익승·MAR승은 '상한 없음' 대비 개선 시행 수.")

    worst = {n: float(np.min([x["mdd"] for x in results[n]])) for n, _ in RATIOS}
    print("\n최악 시행 MDD: " + " · ".join(f"{n.split('(')[0].strip()} {v:.1f}%"
                                          for n, v in worst.items()))


if __name__ == "__main__":
    main()
