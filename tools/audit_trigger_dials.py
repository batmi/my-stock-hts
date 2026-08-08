"""발동 기준(+10%) 검증 — TS 감시 시작 · 피라미딩 증액.

[왜 묻는가] 두 다이얼이 나란히 +10%로 고정돼 있는데, 근거가 서로 다르다.
  · TRAILING_STOP_ACTIVATION_RATE=10 — '10→15는 효과 정확히 0'이라는 기록만 있고
    (config 주석, 2026-07-27) 낮추는 방향은 잰 적이 없다.
  · PYRAMIDING_PROFIT_TRIGGER=10 — 'TS 감시 시작과 동일선'이라는 이유뿐, 실측이 없다.
둘 다 '수익률 %'라는 절대 기준이라 종목 변동성을 무시한다. ATR이 하루 6%인 종목의
+10%와 1.5%인 종목의 +10%는 전혀 다른 사건이다(전자는 이틀치 노이즈, 후자는 1주일 추세).

[무엇을 재는가]
  A) TS 발동 기준 — 0 / 5 / 10(현행) / 15 / 20 %, 그리고 변동성 연동 3종
     · ATR 배수: 발동 = k × ATR/매수가 (k=2.0은 손절폭 1R과 같은 크기)
     · 손익분기 연동: 샹들리에 청산선이 매수가 이상으로 올라오는 순간 무장(파라미터 없음)
  B) 피라미딩 발동 기준 — 5 / 10(현행) / 15 / 20 %, 그리고 ATR 배수 연동(k=2.0, 3.0)

[핵심 진단] TS게이트 = 발동 기준이 없었다면 트레일링으로 팔렸을 일수.
  이 값이 0에 가까우면 발동 기준은 장식이고(콜백이 먼저 구속), 크면 실제 손잡이다.

[판정 기준] 기존 청산 다이얼 결정과 같은 잣대 — 총수익뿐 아니라
  · TS이익비중: '주청산은 샹들리에 TS' 설계가 유지되는가
  · 상위10%·최대: fat-tail이 살아 있는가
  구간을 쪼개도 같은 방향인지(--subperiods)로 과최적화를 점검한다.

[실행] python tools/audit_trigger_dials.py [--trials 25] [--sample 25] [--subperiods 3]
"""
import argparse
import os
import random
import sys

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import config  # noqa: E402
from modules import portfolio_backtest as pb  # noqa: E402

INITIAL_CAPITAL = 10_000_000  # 실거래 시드와 같게 둔다(seed-slot-sizing)


def _atr_pct(atr, price):
    """ATR을 매수가 대비 %로. 산출 불가하면 None."""
    try:
        atr, price = float(atr or 0), float(price or 0)
    except (TypeError, ValueError):
        return None
    return (atr / price * 100) if (atr > 0 and price > 0) else None


def atr_multiple_fn(k, floor=0.0, cap=40.0):
    """발동 기준 = k × ATR/가격(%). ATR을 못 구하면 현행 고정값으로 되돌린다."""
    def fn(atr, price, _k=k, _f=floor, _c=cap):
        a = _atr_pct(atr, price)
        if a is None:
            return 10.0
        return min(_c, max(_f, _k * a))
    return fn


def breakeven_fn(atr, price):
    """샹들리에 청산선이 매수가 위로 올라오는 순간 무장한다.

    청산선 = 고점 × (1 - cb), 고점 = 매수가 × (1 + MFE).
    청산선 ≥ 매수가 ⟺ MFE ≥ cb / (1 - cb).  → 자유 파라미터가 없다.
    """
    ss = config.SELL_STRATEGY
    a = _atr_pct(atr, price)
    cb = max(ss.get("TRAILING_STOP_CALLBACK_RATE", 5.0),
             (a or 0) * ss.get("TRAILING_ATR_MULTIPLIER", 3.5)) / 100.0
    cb = min(cb, 0.6)
    return cb / (1 - cb) * 100


def dial_sets():
    """(그룹, 라벨, {SELL_STRATEGY/ANALYSIS_THRESHOLDS 오버라이드}, {run_portfolio 인자})."""
    A = "TRAILING_STOP_ACTIVATION_RATE"
    P = "PYRAMIDING_PROFIT_TRIGGER"
    return [
        ("A. TS 발동 기준", "0% (즉시)",      {A: 0.0}, {}),
        ("A. TS 발동 기준", "5%",             {A: 5.0}, {}),
        ("A. TS 발동 기준", "10% (현행)",     {A: 10.0}, {}),
        ("A. TS 발동 기준", "15%",            {A: 15.0}, {}),
        ("A. TS 발동 기준", "20%",            {A: 20.0}, {}),
        ("A. TS 발동 기준", "25%",            {A: 25.0}, {}),
        ("A. TS 발동 기준", "30%",            {A: 30.0}, {}),
        ("A. TS 발동 기준", "40%",            {A: 40.0}, {}),
        ("A. TS 발동 기준", "동적 ATR×2.0",   {}, {"ts_act_fn": atr_multiple_fn(2.0)}),
        ("A. TS 발동 기준", "동적 ATR×3.5",   {}, {"ts_act_fn": atr_multiple_fn(3.5)}),
        ("A. TS 발동 기준", "동적 손익분기",   {}, {"ts_act_fn": breakeven_fn}),
        # 위는 감사용 근사식(ATR/매수가). 아래는 실제 운용 산식(청산선 ≥ 매수가)을 그대로 탄다.
        ("A. TS 발동 기준", "손익분기(운용식)", {"TS_ACTIVATION_MODE": "breakeven"}, {}),
        ("B. 피라미딩 발동", "5%",            {P: 5.0}, {}),
        ("B. 피라미딩 발동", "10% (현행)",    {P: 10.0}, {}),
        ("B. 피라미딩 발동", "15%",           {P: 15.0}, {}),
        ("B. 피라미딩 발동", "20%",           {P: 20.0}, {}),
        ("B. 피라미딩 발동", "동적 ATR×2.0",  {}, {"pyr_trigger_fn": atr_multiple_fn(2.0)}),
        ("B. 피라미딩 발동", "동적 ATR×3.0",  {}, {"pyr_trigger_fn": atr_multiple_fn(3.0)}),
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
        "ts_profit_share": (ts_gain / gross_gain * 100) if gross_gain > 0 else 0.0,
        "big": sum(1 for p in profits if p >= 30),
        "ts_n": sum(1 for t in sells if t["reason"] == "트레일링스탑"),
        "pyr": r.get("pyramid_count", 0),
        "gate": r.get("ts_gated_days", 0),
        "n": len(sells),
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--trials", type=int, default=25)
    ap.add_argument("--sample", type=int, default=25)
    ap.add_argument("--days", type=int, default=1095)
    ap.add_argument("--slots", type=int, default=None)
    ap.add_argument("--seed-capital", type=int, default=INITIAL_CAPITAL)
    ap.add_argument("--only", default=None, help="그룹 접두사만 실행 (예: A)")
    ap.add_argument("--confirm", action="store_true",
                    help="유력 후보(현행·20%%·40%%·동적 손익분기)만 남겨 시행 수를 늘린다")
    ap.add_argument("--seed", type=int, default=20260808,
                    help="종목 표본 추출 난수. 결론이 표본 운에 좌우되지 않는지 확인용")
    ap.add_argument("--subperiods", type=int, default=1,
                    help="거래일을 N등분해 구간별로 따로 잰다(과최적화 점검)")
    args = ap.parse_args()

    slots = args.slots or getattr(config, "SYSTEM_MAX_HOLDINGS", 4)
    config.session.load_stock_config()
    targets = [(s["code"], s["name"])
               for s in config.session.stock_data.get("stocks_kr", [])]
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

    from tools.audit_drawdown_axis import market_scale_by_date, make_scale_fn
    p = getattr(config, "RISK_SCALING_PARAMS", {}) or {}
    mkt = market_scale_by_date(dates, args.days)
    dd = None
    if p.get("USE_DRAWDOWN_RISK_SCALING", True):
        dd = (int(p.get("DD_LOOKBACK_DAYS", 90)), float(p.get("DD_LEVEL_1", 5.0)),
              float(p.get("DD_SCALE_1", 0.9)), float(p.get("DD_LEVEL_2", 10.0)),
              float(p.get("DD_SCALE_2", 0.8)))
    print(f"[준비] 리스크 배수 적용 · TS 배수 {config.SELL_STRATEGY.get('TRAILING_ATR_MULTIPLIER')}"
          f" · 반납 상한 R={config.SELL_STRATEGY.get('TS_MAX_GIVEBACK_RATIO')}"
          f" · 피라미딩 {config.ANALYSIS_THRESHOLDS.get('PYRAMIDING_MAX_COUNT')}차")

    sets = dial_sets()
    if args.confirm:
        keep = ("10% (현행)", "20%", "동적 손익분기", "손익분기(운용식)")
        sets = [x for x in sets if x[1] in keep]
    if args.only:
        sets = [x for x in sets if x[0].startswith(args.only)]
    saved_sell = dict(config.SELL_STRATEGY)
    saved_thr = dict(config.ANALYSIS_THRESHOLDS)
    codes = list(dfs.keys())

    k = max(1, args.subperiods)
    size = len(dates) // k
    windows = [(f"구간{i+1}", dates[i * size:(i + 1) * size if i < k - 1 else len(dates)])
               for i in range(k)] if k > 1 else [("전체", dates)]

    all_results = {}
    for wname, wdates in windows:
        results = {(g, l): [] for g, l, _, _ in sets}
        rng = random.Random(args.seed)
        try:
            for t in range(args.trials):
                pick = rng.sample(codes, min(args.sample, len(codes)))
                sd = {c: dfs[c] for c in pick}
                ss = {c: status[c] for c in pick}
                sm = {c: mf.get(c, set()) for c in pick}
                for g, label, overrides, kwargs in sets:
                    config.SELL_STRATEGY.clear(); config.SELL_STRATEGY.update(saved_sell)
                    config.ANALYSIS_THRESHOLDS.clear(); config.ANALYSIS_THRESHOLDS.update(saved_thr)
                    # A축(TS 발동)은 모드가 섞이면 비교가 성립하지 않으므로 명시하지 않은
                    #  항목을 고정(fixed)으로 둔다. B축(피라미딩)은 **운용 기본 모드 그대로**
                    #  둔다 — 청산 체제가 바뀌면 증액 시점의 최적값도 달라질 수 있어,
                    #  실제로 돌아갈 체제에서 재야 한다.
                    if g.startswith("A"):
                        config.SELL_STRATEGY["TS_ACTIVATION_MODE"] = "fixed"
                    for key, val in overrides.items():
                        (config.ANALYSIS_THRESHOLDS if key.startswith("PYRAMIDING")
                         else config.SELL_STRATEGY)[key] = val
                    r = pb.run_portfolio(sd, ss, wdates, initial_capital=args.seed_capital,
                                         slots=slots, market_filter_dates=sm,
                                         risk_scale_by_date=make_scale_fn(mkt, dd), **kwargs)
                    results[(g, label)].append(metrics(r))
                print(f"  {wname} 시행 {t + 1}/{args.trials}", end="\r", flush=True)
        finally:
            config.SELL_STRATEGY.clear(); config.SELL_STRATEGY.update(saved_sell)
            config.ANALYSIS_THRESHOLDS.clear(); config.ANALYSIS_THRESHOLDS.update(saved_thr)
        all_results[wname] = results
    print(" " * 44, end="\r")

    W = 125
    print(f"\n{'=' * W}")
    print(f"발동 기준 검증 — {args.trials}회 × {args.sample}종목 무작위 짝비교")
    print(f"{'=' * W}")

    for wname, results in all_results.items():
        if len(all_results) > 1:
            print(f"\n########## {wname} ({len(dict(windows)[wname])} 거래일) ##########")
        last_group = None
        for g, label, _, _ in sets:
            if g != last_group:
                base_label = next(l for gg, l, _, _ in sets if gg == g and "현행" in l)
                base = results[(g, base_label)]
                print(f"\n{g}  (기준선: {base_label})")
                print(f"{'설정':<16}{'수익%':>9}{'MDD%':>8}{'MAR':>7}{'PF':>6}{'승률%':>7}"
                      f"{'상위10%':>9}{'최대':>9}{'>30%':>6}{'TS이익%':>9}{'증액':>6}"
                      f"{'TS게이트':>9}{'TS청산':>7}{'청산':>6}{'수익승':>8}{'MAR승':>7}")
                print("-" * W)
                last_group = g
            rs = results[(g, label)]
            m = lambda key: float(np.median([x[key] for x in rs]))  # noqa: E731
            is_base = "현행" in label
            rw = sum(1 for a, b in zip(rs, base) if a["ret"] > b["ret"])
            mw = sum(1 for a, b in zip(rs, base) if a["mar"] > b["mar"])
            print(f"{label:<16}{m('ret'):>9.1f}{m('mdd'):>8.1f}{m('mar'):>7.2f}{m('pf'):>6.2f}"
                  f"{m('wr'):>7.1f}{m('top10'):>9.1f}{m('best'):>9.1f}{m('big'):>6.0f}"
                  f"{m('ts_profit_share'):>9.1f}{m('pyr'):>6.0f}{m('gate'):>9.0f}"
                  f"{m('ts_n'):>7.0f}{m('n'):>6.0f}"
                  f"{'—' if is_base else f'{rw}/{len(rs)}':>8}"
                  f"{'—' if is_base else f'{mw}/{len(rs)}':>7}")

    print("\n" + "-" * W)
    print("TS게이트 = 발동 기준이 없었다면 트레일링으로 팔렸을 일수(중앙값). 0이면 이 다이얼은 장식이다.")
    print("TS이익%  = 트레일링 청산이 총이익에서 차지하는 비중 — '주청산은 샹들리에 TS' 설계 유지 지표.")
    print(">30%·상위10%·최대 = fat-tail 보존 지표. 수익승·MAR승은 각 그룹 현행 대비 개선 시행 수.")


if __name__ == "__main__":
    main()
