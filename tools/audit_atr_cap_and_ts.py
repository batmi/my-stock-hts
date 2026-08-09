"""ATR 손절 캡(-15%)과 TS 발동 시점 — 10년 창 합동 검증.

두 질문을 한 번에 잰다. 유니버스·창·표본을 공유하므로 따로 돌리는 것보다 데이터 수집이
절반 이하로 줄고, 무엇보다 **같은 표본에서 나온 수치라 서로 비교할 수 있다.**

[질문 1] ATR 손절 캡 -15%가 fat-tail을 자르고 있는가
  캡은 산식값(ATR×2/가격)이 -15%보다 넓을 때 -15%로 **좁힌다**. 로컬 일봉 실측(2026-08-09,
  99종목·1년)에서 상승추세 봉의 7.85%가 이 캡에 걸렸다 — 드물지 않다.
  결정적인 것은 순효과다. allocate_budget 실측(2026-07-27)상 배분액은 항상 3)변동성
  타겟팅층이 구속하고 2)리스크층은 최종액을 정하지 않는다. 따라서 캡이 손절폭을 좁혀도
  **포지션이 커지지 않는다** → 순효과는 '청산선만 노이즈 안으로 들어옴' 한 방향이다.
  조기 이탈 대상은 변동성 상위 = 대개 모멘텀 상위 종목이라, 추세추종에서 가장 비싼 쪽이다.
  (조이는 방향은 이미 열위로 확인됨 — settings.py: -15→-1%에서 거래 2배·PF 1.91→1.56.
   미검증인 것은 푸는 방향이다.)

[질문 2] TS 발동선이 저변동 국면에서 너무 높은가
  발동선 = cb/(1-cb), cb = max(콜백하한, ATR×3.5/매수가). 변동성이 줄면 ATR이 작아져
  발동선도 **함께 내려간다** — 산식은 우려와 반대로 작동한다. 다만 콜백 하한
  (TRAILING_STOP_CALLBACK_RATE=5%)이 바닥을 만들어 발동선이 5.3% 아래로는 못 내려간다.
  저변동 국면에서 실제로 구속하는 것은 ATR이 아니라 이 하한이다. 하한을 낮추는 반사실로 잰다.

[판정 잣대] 기존 청산 다이얼 결정과 같다 — 총수익만 보지 않는다.
  · 상위10%·최대·>30% : fat-tail이 살아 있는가 (이 검증의 핵심 질문)
  · 손절%·TS이익%      : 청산이 어느 문으로 나가는가 ('주청산은 샹들리에 TS' 설계 유지)
  · 보유일             : 조기 이탈이면 짧아진다
  · 구간 분할          : 단일 창 결론 방지 (--subperiods)

[주의] 창 표기. --days 는 거래일이 아니라 **달력일**이다. 3650 = 10년 ≈ 거래일 2,450일.
  기존 청산 다이얼 검증은 --days 2450(=6.7년)에서 이뤄졌다.

[실행]
  python3 tools/audit_atr_cap_and_ts.py --days 3650 --trials 20 --sample 25 --subperiods 5
  python3 tools/audit_atr_cap_and_ts.py --only A          # 캡만
  python3 tools/audit_atr_cap_and_ts.py --vol-only        # 변동성 진단만(시뮬 없음, 수 초)
"""
import argparse
import os
import random
import sys
from collections import defaultdict

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import config  # noqa: E402
from modules import portfolio_backtest as pb  # noqa: E402
from modules.auto_trade import engine  # noqa: E402

INITIAL_CAPITAL = 10_000_000        # 실거래 시드와 같게 둔다(seed-slot-sizing)
CAP_KEY = "MAX_ATR_STOP_LOSS_RATE"
CB_KEY = "TRAILING_STOP_CALLBACK_RATE"


def dial_sets():
    """(그룹, 라벨, SELL_STRATEGY 오버라이드)."""
    return [
        # A) 손절 캡 — 푸는 방향. 0은 캡 해제(순수 ATR×배수).
        ("A. ATR 손절 캡", "해제(0)",      {CAP_KEY: 0.0}),
        ("A. ATR 손절 캡", "-25%",         {CAP_KEY: -25.0}),
        ("A. ATR 손절 캡", "-20%",         {CAP_KEY: -20.0}),
        ("A. ATR 손절 캡", "-15% (현행)",  {CAP_KEY: -15.0}),
        ("A. ATR 손절 캡", "-12%",         {CAP_KEY: -12.0}),
        # B) TS 콜백 하한 — 저변동 국면에서 발동선의 바닥을 만드는 값.
        #    낮추면 발동이 빨라지지만 콜백도 좁아져 조기 털림이 늘 수 있다(양면).
        ("B. TS 콜백 하한", "3%",          {CB_KEY: 3.0}),
        ("B. TS 콜백 하한", "4%",          {CB_KEY: 4.0}),
        ("B. TS 콜백 하한", "5% (현행)",   {CB_KEY: 5.0}),
        ("B. TS 콜백 하한", "7%",          {CB_KEY: 7.0}),
        # C) TS 무장 래치 — 발동선이 매일 '현재 봉' ATR로 재계산돼 변동성이 오르면 이미
        #    무장된 TS가 풀린다(해제율 10년 22~40% / 2026년 70.9%). 결함으로 의심했으나
        #    2026-08-09 실측에서 기각 — ON이 5구간 중 4구간 열위, 구간5 수익 113.2→74.6%,
        #    상위10% 72.4→55.1. 해제는 고변동에서 포지션에 여유를 주는 적응 장치였다.
        #    재검증용으로 남긴다.
        ("C. TS 무장 래치", "OFF (현행)",  {"TS_ARM_LATCH": False}),
        ("C. TS 무장 래치", "ON",          {"TS_ARM_LATCH": True}),
    ]


SELL_REASONS = ("ATR손절", "손절", "본전청산", "시간청산", "트레일링스탑", "점수하락", "이익보호")


def metrics(r):
    sells = [t for t in r["trades"] if t["reason"] in SELL_REASONS]
    profits = sorted((t["profit"] for t in sells), reverse=True)
    top10 = profits[:max(1, len(profits) // 10)]
    gross_gain = sum(t["profit_amt"] for t in sells if t["profit_amt"] > 0)
    ts_gain = sum(t["profit_amt"] for t in sells
                  if t["reason"] == "트레일링스탑" and t["profit_amt"] > 0)
    stop_n = sum(1 for t in sells if t["reason"] in ("ATR손절", "손절"))
    mdd = r["mdd"]
    return {
        "ret": r["total_return"], "mdd": mdd,
        "mar": r["total_return"] / abs(mdd) if mdd else float("nan"),
        "pf": r["pf"],
        "wr": r["win"] / max(1, r["win"] + r["loss"]) * 100,
        "top10": float(np.mean(top10)) if top10 else 0.0,
        "best": profits[0] if profits else 0.0,
        "big": sum(1 for p in profits if p >= 30),
        "ts_profit_share": (ts_gain / gross_gain * 100) if gross_gain > 0 else 0.0,
        "ts_n": sum(1 for t in sells if t["reason"] == "트레일링스탑"),
        "stop_share": (stop_n / len(sells) * 100) if sells else 0.0,
        "days": float(np.median([t["days"] for t in sells])) if sells else 0.0,
        "n": len(sells),
    }


def volatility_report(dfs, dates):
    """레버와 무관한 순수 측정 — 연도별 변동성과 그로부터 나오는 발동선·캡 구속률.

    시뮬레이션이 필요 없어 수 초면 끝난다. '변동성이 줄면 발동선이 어떻게 되는가'라는
    질문에 대한 직접 답이며, 캡 구속률도 같은 표본에서 나온다.
    """
    ss = config.SELL_STRATEGY
    atr_mult = ss.get("ATR_STOP_MULTIPLIER", 2.0)
    cap = ss.get(CAP_KEY, -15.0)
    cb_floor = ss.get(CB_KEY, 5.0)
    ts_mult = ss.get("TRAILING_ATR_MULTIPLIER", 3.5)

    by_year = defaultdict(lambda: {"atr": [], "act": [], "capped": 0, "floored": 0, "n": 0})
    for code, df in dfs.items():
        if "ATR" not in df.columns:
            continue
        for d, atr, close in zip(df["date"].astype(str), df["ATR"], df["close"]):
            try:
                atr = float(atr); close = float(close)
            except (TypeError, ValueError):
                continue
            if not (atr > 0 and close > 0):
                continue
            y = d[:4]
            b = by_year[y]
            b["n"] += 1
            b["atr"].append(atr / close * 100)
            raw = -(atr * atr_mult / close * 100)
            if cap and raw < cap:
                b["capped"] += 1
            cb = atr * ts_mult / close * 100
            if cb <= cb_floor:
                b["floored"] += 1
            b["act"].append(engine.breakeven_activation_rate(atr, close, cb_floor, ts_mult, True))

    print(f"\n{'=' * 96}")
    print("변동성 진단 — 연도별 (레버 무관, 유니버스 전체 봉)")
    print(f"{'=' * 96}")
    print(f"{'연도':<8}{'봉수':>9}{'ATR/가격 중앙':>14}{'  ATR 95%':>10}"
          f"{'TS발동선 중앙':>15}{'발동선 95%':>12}{'캡 구속%':>10}{'하한 구속%':>12}")
    print("-" * 96)
    for y in sorted(by_year):
        b = by_year[y]
        if b["n"] < 100:
            continue
        print(f"{y:<8}{b['n']:>9,}{np.median(b['atr']):>14.2f}{np.percentile(b['atr'], 95):>10.2f}"
              f"{np.median(b['act']):>15.2f}{np.percentile(b['act'], 95):>12.2f}"
              f"{100 * b['capped'] / b['n']:>10.2f}{100 * b['floored'] / b['n']:>12.2f}")
    print("-" * 96)
    print("캡 구속%   = ATR×배수 손절폭이 MAX_ATR_STOP_LOSS_RATE보다 넓어 좁혀진 봉의 비율.")
    print("하한 구속% = ATR×3.5 콜백이 TRAILING_STOP_CALLBACK_RATE 아래라 하한이 발동선을 정한 봉.")
    print("             이 값이 크면 저변동 국면에서 발동선을 정하는 것은 ATR이 아니라 하한이다.")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--trials", type=int, default=20)
    ap.add_argument("--sample", type=int, default=25)
    ap.add_argument("--days", type=int, default=3650, help="달력일. 3650=10년(거래일 ≈2,450)")
    ap.add_argument("--slots", type=int, default=None)
    ap.add_argument("--seed-capital", type=int, default=INITIAL_CAPITAL)
    ap.add_argument("--only", default=None, help="그룹 접두사만 실행 (예: A)")
    ap.add_argument("--seed", type=int, default=20260809)
    ap.add_argument("--subperiods", type=int, default=1)
    ap.add_argument("--vol-only", action="store_true", help="변동성 진단만(시뮬 생략)")
    args = ap.parse_args()

    slots = args.slots or getattr(config, "SYSTEM_MAX_HOLDINGS", 4)
    config.session.load_stock_config()
    targets = [(s["code"], s["name"])
               for s in config.session.stock_data.get("stocks_kr", [])]
    yrs = args.days / 365.0
    print(f"[준비] 관심종목 {len(targets)}개 · {args.days}일(≈{yrs:.1f}년) · 슬롯 {slots} "
          f"· 시드 {args.seed_capital:,}원")

    dfs, mf, dates, failed = pb.prepare_universe(targets, args.days)
    print(f"[준비] 사용 {len(dfs)}종목 / 거래일 {len(dates)}일"
          + (f" · 제외 {len(failed)}" if failed else ""))

    volatility_report(dfs, dates)
    if args.vol_only:
        return

    thresholds = {
        "BUY_SCORE": config.ANALYSIS_THRESHOLDS["BUY_SCORE"],
        "BUY_RSI_MAX": config.ANALYSIS_THRESHOLDS["BUY_RSI_MAX"],
        "RISE_SCORE": config.ANALYSIS_THRESHOLDS["RISE_SCORE"],
        "WEIGHTS": config.SCORING_WEIGHTS,
    }
    status = pb.precompute_status(dfs, thresholds)

    from tools.audit_drawdown_axis import market_scale_by_date, make_scale_fn
    p = getattr(config, "RISK_SCALING_PARAMS", {}) or {}
    mkt = market_scale_by_date(dates, args.days)
    dd = None
    if p.get("USE_DRAWDOWN_RISK_SCALING", True):
        dd = (int(p.get("DD_LOOKBACK_DAYS", 90)), float(p.get("DD_LEVEL_1", 5.0)),
              float(p.get("DD_SCALE_1", 0.9)), float(p.get("DD_LEVEL_2", 10.0)),
              float(p.get("DD_SCALE_2", 0.8)))

    sets = dial_sets()
    if args.only:
        sets = [x for x in sets if x[0].startswith(args.only)]
    saved = dict(config.SELL_STRATEGY)
    codes = list(dfs.keys())

    k = max(1, args.subperiods)
    size = len(dates) // k
    windows = [(f"구간{i + 1}", dates[i * size:(i + 1) * size if i < k - 1 else len(dates)])
               for i in range(k)] if k > 1 else [("전체", dates)]

    all_results = {}
    for wname, wdates in windows:
        results = {(g, l): [] for g, l, _ in sets}
        rng = random.Random(args.seed)
        try:
            for t in range(args.trials):
                pick = rng.sample(codes, min(args.sample, len(codes)))
                sd = {c: dfs[c] for c in pick}
                st = {c: status[c] for c in pick}
                sm = {c: mf.get(c, set()) for c in pick}
                for g, label, overrides in sets:
                    config.SELL_STRATEGY.clear(); config.SELL_STRATEGY.update(saved)
                    config.SELL_STRATEGY.update(overrides)
                    r = pb.run_portfolio(sd, st, wdates, initial_capital=args.seed_capital,
                                         slots=slots, market_filter_dates=sm,
                                         risk_scale_by_date=make_scale_fn(mkt, dd))
                    results[(g, label)].append(metrics(r))
                print(f"  {wname} 시행 {t + 1}/{args.trials}", end="\r", flush=True)
        finally:
            config.SELL_STRATEGY.clear(); config.SELL_STRATEGY.update(saved)
        all_results[wname] = results
    print(" " * 44, end="\r")

    W = 118
    print(f"\n{'=' * W}")
    print(f"ATR 캡 · TS 콜백 하한 검증 — {args.trials}회 × {args.sample}종목 무작위 짝비교 "
          f"({args.days}일 ≈ {yrs:.1f}년)")
    print(f"{'=' * W}")
    for wname, results in all_results.items():
        if len(all_results) > 1:
            print(f"\n########## {wname} ({len(dict(windows)[wname])} 거래일) ##########")
        last_group = None
        for g, label, _ in sets:
            if g != last_group:
                base_label = next(l for gg, l, _ in sets if gg == g and "현행" in l)
                base = results[(g, base_label)]
                print(f"\n{g}  (기준선: {base_label})")
                print(f"{'설정':<14}{'수익%':>9}{'MDD%':>8}{'MAR':>7}{'PF':>6}{'승률%':>7}"
                      f"{'상위10%':>9}{'최대':>9}{'>30%':>6}{'TS이익%':>9}{'손절%':>7}"
                      f"{'보유일':>7}{'청산':>6}{'수익승':>8}{'MAR승':>7}{'꼬리승':>7}")
                print("-" * W)
                last_group = g
            rs = results[(g, label)]
            m = lambda key: float(np.median([x[key] for x in rs]))  # noqa: E731
            is_base = "현행" in label
            rw = sum(1 for a, b in zip(rs, base) if a["ret"] > b["ret"])
            mw = sum(1 for a, b in zip(rs, base) if a["mar"] > b["mar"])
            tw = sum(1 for a, b in zip(rs, base) if a["top10"] > b["top10"])
            print(f"{label:<14}{m('ret'):>9.1f}{m('mdd'):>8.1f}{m('mar'):>7.2f}{m('pf'):>6.2f}"
                  f"{m('wr'):>7.1f}{m('top10'):>9.1f}{m('best'):>9.1f}{m('big'):>6.0f}"
                  f"{m('ts_profit_share'):>9.1f}{m('stop_share'):>7.1f}{m('days'):>7.0f}"
                  f"{m('n'):>6.0f}"
                  f"{'—' if is_base else f'{rw}/{len(rs)}':>8}"
                  f"{'—' if is_base else f'{mw}/{len(rs)}':>7}"
                  f"{'—' if is_base else f'{tw}/{len(rs)}':>7}")

    print("\n" + "-" * W)
    print("상위10%·최대·>30% = fat-tail 보존 지표. 이 검증의 핵심 질문이다 —")
    print("  캡이 조기 이탈을 만든다면 총수익보다 여기가 먼저 무너진다.")
    print("손절% = 전체 청산 중 손절이 차지하는 비중. 캡을 조일수록 올라가야 정합적이다.")
    print("TS이익% = 트레일링 청산이 총이익에서 차지하는 비중 ('주청산은 샹들리에 TS' 유지 지표).")
    print("꼬리승 = 현행 대비 상위10%가 개선된 시행 수.")


if __name__ == "__main__":
    main()
