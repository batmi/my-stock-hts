"""'늦게 자른다' 축 셋을 **함께** 연다 — 따로 재면 이중 지연을 놓친다.

[왜 지금] 미결로 남은 다이얼 셋은 전부 같은 방향이다.
   · TS 발동 배수 3.0 → 3.5   (더 늦게 무장한다)
   · TS 콜백 배수  3.5 → 4.5  (더 늦게 자른다)
   · 증액 트리거  +10% → +7%  (더 일찍 얹는다 = 포지션을 더 오래·크게 안고 간다)
 셋 다 '전체창에서 이기고 일부 구간에서 진다'는 똑같은 모양이라 기존 잣대로 전부 보류됐다.
 그런데 **한 축씩 따로 잰 결과만 있다.** 발동을 늦추면 무장 자체가 늦어지고, 콜백까지
 넓히면 무장한 뒤의 여유도 커진다 — 두 지연이 곱해지면 한쪽만 열었을 때와 다른 것이
 나올 수 있다. 좋은 쪽으로든(추세를 끝까지 태운다) 나쁜 쪽으로든(반납이 누적된다).

[무엇을 답하나]
  ① 합동이 단독들의 단순 합인가, 아니면 상호작용이 있는가.
     상호작용 = 합동 − (단독A + 단독B − 기준선). 0에서 멀면 따로 잰 결론은 무효다.
  ② 단독들이 각각 졌던 그 구간에서, 합동은 어떻게 되나. 같은 구간에서 같이 지면
     '늦게 자르는 것' 자체가 그 국면에서 안 되는 것이고, 그건 다이얼이 아니라 설계 얘기다.

[세계] 장중 체결 세계에서 잰다. 세 값이 마지막으로 측정된 세계이고, 지금 시스템이 사는
 세계다. 프로토콜도 그때와 맞춘다(15회 × 20종목 × 씨드 3 = 45쌍, 고변동 대조 구간 분리).
 ※ [도구 함정] 장중 게이트는 창을 702일로 자른다. 이 도구의 결과를 '10년'이라고 쓰면
   안 된다 — 종가 10년 결론(config 주석)과는 창이 다르다.

[선행] tools/fetch_intraday_tv.py
[실행] python3 tools/audit_late_exit_joint.py --trials 15 --sample 20 --seeds 3
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
from tools.audit_dials_intraday import INITIAL_CAPITAL, apply, metrics  # noqa: E402

BASE = "현행 (3.0/3.5/+10%)"

# (라벨, 오버라이드) — 대상: sell=SELL_STRATEGY, thr=ANALYSIS_THRESHOLDS
ARMS = [
    (BASE, []),
    ("발동 3.5 단독", [("sell", "TS_ACTIVATION_ATR_MULTIPLIER", 3.5)]),
    ("콜백 4.5 단독", [("sell", "TRAILING_ATR_MULTIPLIER", 4.5)]),
    ("트리거 +7% 단독", [("thr", "PYRAMIDING_PROFIT_TRIGGER", 7.0)]),
    ("발동3.5 + 콜백4.5", [("sell", "TS_ACTIVATION_ATR_MULTIPLIER", 3.5),
                          ("sell", "TRAILING_ATR_MULTIPLIER", 4.5)]),
    ("셋 다 (3.5/4.5/+7%)", [("sell", "TS_ACTIVATION_ATR_MULTIPLIER", 3.5),
                            ("sell", "TRAILING_ATR_MULTIPLIER", 4.5),
                            ("thr", "PYRAMIDING_PROFIT_TRIGGER", 7.0)]),
]
PAIR = ("발동 3.5 단독", "콜백 4.5 단독", "발동3.5 + 콜백4.5")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--trials", type=int, default=15)
    ap.add_argument("--sample", type=int, default=20)
    ap.add_argument("--days", type=int, default=1200)
    ap.add_argument("--seeds", type=int, default=3)
    ap.add_argument("--seed", type=int, default=20260816)
    ap.add_argument("--interval", default="60m")
    ap.add_argument("--slots", type=int, default=None)
    ap.add_argument("--min-coverage", type=float, default=0.9)
    ap.add_argument("--subperiods", type=int, default=3)
    ap.add_argument("--exclude-from", default="20260301")
    args = ap.parse_args()

    slots = args.slots or getattr(config, "SYSTEM_MAX_HOLDINGS", 4)
    config.session.load_stock_config()
    stocks = config.session.stock_data.get("stocks_kr", [])
    names = {s["code"]: s["name"] for s in stocks}
    dfs, mf, dates, _f = pb.prepare_universe([(s["code"], s["name"]) for s in stocks], args.days)
    bars, st, keep, drop = ib.gate_universe(dfs, args.interval, min_coverage=args.min_coverage)
    if drop:
        print(f"[제외] {len(drop)}종목 — " + ", ".join(f"{names.get(c, c)}({w})" for c, w in drop))
    dfs = {c: dfs[c] for c in keep}
    mf = {c: mf.get(c, set()) for c in keep}
    dates = ib.covered_dates(bars, dates)
    if not dates:
        print("[중단] 겹치는 거래일 없음")
        return

    thresholds = {"BUY_SCORE": config.ANALYSIS_THRESHOLDS["BUY_SCORE"],
                  "BUY_RSI_MAX": config.ANALYSIS_THRESHOLDS["BUY_RSI_MAX"],
                  "RISE_SCORE": config.ANALYSIS_THRESHOLDS["RISE_SCORE"],
                  "WEIGHTS": config.SCORING_WEIGHTS}
    status = pb.precompute_status(dfs, thresholds)
    print(f"[준비] {len(dfs)}종목 / 거래일 {len(dates)}일 ({dates[0]}~{dates[-1]}) · 슬롯 {slots} "
          f"· {args.interval} 장중 체결", flush=True)

    from tools.audit_drawdown_axis import market_scale_by_date, make_scale_fn
    p = getattr(config, "RISK_SCALING_PARAMS", {}) or {}
    mkt = market_scale_by_date(dates, args.days)
    dd = None
    if p.get("USE_DRAWDOWN_RISK_SCALING", True):
        dd = (int(p.get("DD_LOOKBACK_DAYS", 90)), float(p.get("DD_LEVEL_1", 5.0)),
              float(p.get("DD_SCALE_1", 0.9)), float(p.get("DD_LEVEL_2", 10.0)),
              float(p.get("DD_SCALE_2", 0.8)))
    # 콜러블은 실행마다 새로 만든다 — 자산곡선 이력이 남으면 뒤 팔이 일괄로 깎인다.
    new_scale_fn = lambda: make_scale_fn(mkt, dd)  # noqa: E731

    cut = "".join(ch for ch in str(args.exclude_from) if ch.isdigit())
    head = [d for d in dates if not cut or cut == "0" or d < cut]
    tail = [d for d in dates if cut and cut != "0" and d >= cut]
    k = max(1, args.subperiods)
    size = max(1, len(head) // k)
    windows = [("제외 전 전체", head)]
    if k > 1:
        windows += [(f"구간{i + 1}", head[i * size:(i + 1) * size if i < k - 1 else len(head)])
                    for i in range(k)]
    if tail:
        windows.append(("[대조] 제외구간(고변동)", tail))

    codes = list(dfs)
    all_res = {}
    total = len(windows) * args.seeds * args.trials * len(ARMS)
    done = 0
    for wname, wdates in windows:
        res = {lbl: [] for lbl, _ in ARMS}
        for si in range(args.seeds):
            rng = random.Random(args.seed + si * 1009)
            for _t in range(args.trials):
                pick = rng.sample(codes, min(args.sample, len(codes)))
                sd = {c: dfs[c] for c in pick}
                sc = {c: status[c] for c in pick}
                sm = {c: mf.get(c, set()) for c in pick}
                sb = {c: bars[c] for c in pick}
                ss = {c: st[c] for c in pick}
                for lbl, ov in ARMS:
                    prev = apply(ov)
                    try:
                        r = pb.run_portfolio(sd, sc, wdates, initial_capital=INITIAL_CAPITAL,
                                             slots=slots, market_filter_dates=sm,
                                             risk_scale_by_date=new_scale_fn(),
                                             intraday_bars=sb, intraday_status=ss)
                    finally:
                        apply(prev)
                    res[lbl].append(metrics(r))
                    done += 1
                print(f"  {wname} 씨드{si + 1} {done}/{total}", end="\r", flush=True)
        all_res[wname] = res
    print(" " * 70, end="\r")

    W = 112
    print(f"\n{'=' * W}")
    print(f"'늦게 자른다' 축 합동 — {args.trials}회 × {args.sample}종목 × 씨드 {args.seeds}개 "
          f"= {args.trials * args.seeds}쌍 · 장중 체결 {len(dates)}일")
    print(f"{'=' * W}")
    for wname, wdates in windows:
        res = all_res[wname]
        base = res[BASE]
        print(f"\n########## {wname} ({len(wdates)} 거래일) ##########")
        print(f"{'설정':<20}{'수익%':>9}{'MDD%':>8}{'MAR':>7}{'PF':>6}{'청산':>6}{'증액':>6}"
              f"{'무장%':>7}{'상위10%':>9}{'최대':>9}{'보유일':>7}{'승-무-패':>10}{'MAR승':>7}")
        print("-" * W)
        for lbl, _ov in ARMS:
            rs = res[lbl]
            m = lambda key: float(np.median([x[key] for x in rs]))  # noqa: E731
            is_base = lbl == BASE
            tie = sum(1 for a, b in zip(rs, base) if abs(a["ret"] - b["ret"]) < 1e-9)
            rw = sum(1 for a, b in zip(rs, base) if a["ret"] > b["ret"] + 1e-9)
            mw = sum(1 for a, b in zip(rs, base) if a["mar"] > b["mar"])
            print(f"{lbl:<20}{m('ret'):>9.1f}{m('mdd'):>8.1f}{m('mar'):>7.2f}{m('pf'):>6.2f}"
                  f"{m('n'):>6.0f}{m('pyr_n'):>6.0f}{m('armed'):>7.1f}{m('top10'):>9.1f}"
                  f"{m('best'):>9.1f}{m('days'):>7.1f}"
                  f"{'—' if is_base else f'{rw}-{tie}-{len(rs) - rw - tie}':>10}"
                  f"{'—' if is_base else f'{mw}/{len(rs)}':>7}")

        # ── 상호작용: 합동이 단독들의 합인가
        a, b, ab = (np.mean([x["ret"] for x in res[k2]]) for k2 in PAIR)
        b0 = np.mean([x["ret"] for x in base])
        inter = ab - (a + b - b0)
        print(f"  [상호작용] 단독A {a - b0:+.1f}%p · 단독B {b - b0:+.1f}%p · "
              f"합동 {ab - b0:+.1f}%p → 상호작용 {inter:+.1f}%p"
              + ("  (합이 아니다 — 따로 잰 결론은 못 쓴다)" if abs(inter) >= 3 else
                 "  (사실상 가법 — 따로 잰 결론이 유효하다)"))

    print("\n" + "-" * W)
    print("[읽는 법] 단독들이 졌던 구간에서 합동도 지면, 그 국면에서 안 되는 것은 다이얼 값이"
          " 아니라 '늦게 자른다'는 방향 자체다.")
    print("[창 주의] 장중 게이트로 창이 잘렸다 — 종가 10년 결론(config 주석)과 직접 비교 금지.")


if __name__ == "__main__":
    main()
