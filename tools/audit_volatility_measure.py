"""변동성 측정이 과한가 — 10년 창 검증.

[배경] 2026년 들어 변동성 적응 장치 두 개가 **동시에 한계값에 붙었다**(실측 2026-08-09,
41종목·2,449 거래일 KRX 확정 일봉).
  · ATR 손절 캡(-15%)  : 2026-07 봉의 66.4%에서 구속 → 손절폭이 사실상 고정 -15%
  · 변동성 배수 하한(0.4): 2026-07 봉의 98.3%에서 구속 → 포지션 크기가 사실상 고정 0.4배
둘 다 '변동성에 맞춰 조절하라'고 만든 장치인데, 정작 조절이 가장 필요한 국면에서
상수가 되어 있다.

[측정 자체는 정상이다 — 여기서 다시 의심하지 말 것]
  · ATR 산식은 표준 Wilder(alpha=1/period)이며 참조 구현과 오차 1e-11.
  · 일봉은 KRX 공식 시세와 원 단위까지 일치(pykrx 대조). NXT 혼입 없음.
  · ATR/가격을 일간 σ 대신 쓰는 것은 약 1.5배 과대평가지만 **배율이 11년간 1.42~1.59로
    상수**라 TARGET_VOLATILITY 상수에 흡수된다(2026년은 오히려 최저 1.42).
    → 국면 의존 왜곡 없음. σ로 바꾸는 것은 TARGET_VOLATILITY를 1.5배 올리는 것과 동치.

[그래서 남는 질문] 측정이 아니라 **반응**을 줄이면 어떻게 되는가. 세 레버가 서로 다른
곳을 건드리므로 나눠 잰다.
  D) TARGET_VOLATILITY   — 포지션 사이징만. 단, 하한 0.4에 걸려 있어 2026년 국면에서는
                            0.37을 넘기 전까지 **아무 효과가 없다**(0.4 x 연환산 0.93).
  E) ATR_PERIOD          — 손절폭·TS 콜백·발동선·사이징 **전부**. 길수록 매끄럽고 느리다.
  F) VOLATILITY_SCALING_MIN — 지금 실제로 포지션 크기를 정하고 있는 값. 현행 0.4.

[판정 잣대] 기존 청산·리스크 다이얼 결정과 같다. 총수익만 보지 않는다.
  상위10%·최대·>30%(fat-tail) · MDD·MAR · 손절% · 보유일 · 구간 분할 일관성.

[실행]
  python3 tools/audit_volatility_measure.py --days 3650 --trials 15 --sample 25 --subperiods 5
  python3 tools/audit_volatility_measure.py --only D      # 특정 그룹만
"""
import argparse
import math
import os
import random
import sys
from collections import defaultdict

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import config  # noqa: E402
import indicators  # noqa: E402
from modules import portfolio_backtest as pb  # noqa: E402

from tools.audit_atr_cap_and_ts import SELL_REASONS, metrics  # noqa: E402

INITIAL_CAPITAL = 10_000_000


def dial_sets():
    """(그룹, 라벨, {스코프: {키: 값}}). 스코프 = cfg(모듈 속성) | ind(INDICATOR_PARAMS)."""
    return [
        # D) 사이징만. 하한 0.4 때문에 0.37 이하는 현행과 같아야 한다 — 그 예측도 함께 검증된다.
        ("D. TARGET_VOLATILITY", "0.25 (현행)", {"cfg": {"TARGET_VOLATILITY": 0.25}}),
        ("D. TARGET_VOLATILITY", "0.35",       {"cfg": {"TARGET_VOLATILITY": 0.35}}),
        ("D. TARGET_VOLATILITY", "0.50",       {"cfg": {"TARGET_VOLATILITY": 0.50}}),
        ("D. TARGET_VOLATILITY", "0.70",       {"cfg": {"TARGET_VOLATILITY": 0.70}}),
        # E) 전부. 기간을 늘리면 ATR이 매끄러워지고, 급등 국면에서는 값이 작아진다.
        ("E. ATR_PERIOD", "10",        {"ind": {"ATR_PERIOD": 10}}),
        ("E. ATR_PERIOD", "14 (현행)", {"ind": {"ATR_PERIOD": 14}}),
        ("E. ATR_PERIOD", "21",        {"ind": {"ATR_PERIOD": 21}}),
        ("E. ATR_PERIOD", "30",        {"ind": {"ATR_PERIOD": 30}}),
        # F) 지금 국면에서 포지션 크기를 실제로 정하는 값.
        ("F. 변동성배수 하한", "0.25",       {"cfg": {"VOLATILITY_SCALING_MIN": 0.25}}),
        ("F. 변동성배수 하한", "0.40 (현행)", {"cfg": {"VOLATILITY_SCALING_MIN": 0.40}}),
        ("F. 변동성배수 하한", "0.60",       {"cfg": {"VOLATILITY_SCALING_MIN": 0.60}}),
        ("F. 변동성배수 하한", "1.00(무효화)", {"cfg": {"VOLATILITY_SCALING_MIN": 1.00}}),
    ]


def rebuild_atr(dfs, period):
    """ATR 컬럼만 다시 계산한 사본을 돌려준다. (원본 불변)

    compute_price_indicators에서 ATR_PERIOD에 의존하는 컬럼은 df['ATR'] 하나뿐이라
    (modules/backtest.py:289) 전체 재계산 없이 이 컬럼만 갈아끼우면 된다.
    """
    out = {}
    for c, df in dfs.items():
        d = df.copy()
        d["ATR"] = indicators.get_atr_full_series(d, period=period)
        out[c] = d
    return out


def apply_overrides(ov, saved_cfg):
    for k, v in ov.get("cfg", {}).items():
        setattr(config, k, v)
    for k, v in ov.get("ind", {}).items():
        config.INDICATOR_PARAMS[k] = v


def restore(saved_cfg, saved_ind):
    for k, v in saved_cfg.items():
        setattr(config, k, v)
    config.INDICATOR_PARAMS.clear()
    config.INDICATOR_PARAMS.update(saved_ind)


def fmt_table(group, rows, base_label):
    print(f"\n{group}  (기준선: {base_label})")
    hdr = (f"{'설정':<18}{'수익%':>7}{'MDD%':>8}{'MAR':>7}{'PF':>6}{'승률%':>7}"
           f"{'상위10%':>9}{'최대':>9}{'>30%':>6}{'TS이익%':>9}{'손절%':>7}"
           f"{'보유일':>7}{'청산':>6}{'수익승':>8}{'MAR승':>7}{'꼬리승':>7}")
    print(hdr)
    print("-" * len(hdr))
    base = rows[base_label]
    for label, ms in rows.items():
        def avg(k):
            return float(np.mean([m[k] for m in ms]))
        if label == base_label:
            w = mw = tw = "—"
        else:
            w = f"{sum(1 for a, b in zip(ms, base) if a['ret'] > b['ret'])}/{len(ms)}"
            mw = f"{sum(1 for a, b in zip(ms, base) if a['mar'] > b['mar'])}/{len(ms)}"
            tw = f"{sum(1 for a, b in zip(ms, base) if a['top10'] > b['top10'])}/{len(ms)}"
        print(f"{label:<18}{avg('ret'):>7.1f}{avg('mdd'):>8.1f}{avg('mar'):>7.2f}"
              f"{avg('pf'):>6.2f}{avg('wr'):>7.1f}{avg('top10'):>9.1f}{avg('best'):>9.1f}"
              f"{avg('big'):>6.0f}{avg('ts_profit_share'):>9.1f}{avg('stop_share'):>7.1f}"
              f"{avg('days'):>7.0f}{avg('n'):>6.0f}{w:>8}{mw:>7}{tw:>7}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--trials", type=int, default=15)
    ap.add_argument("--sample", type=int, default=25)
    ap.add_argument("--days", type=int, default=3650, help="달력일. 3650=10년")
    ap.add_argument("--slots", type=int, default=None)
    ap.add_argument("--seed-capital", type=int, default=INITIAL_CAPITAL)
    ap.add_argument("--only", default=None)
    ap.add_argument("--seed", type=int, default=20260809)
    ap.add_argument("--subperiods", type=int, default=5)
    args = ap.parse_args()

    slots = args.slots or getattr(config, "SYSTEM_MAX_HOLDINGS", 4)
    config.session.load_stock_config()
    targets = [(s["code"], s["name"])
               for s in config.session.stock_data.get("stocks_kr", [])]
    print(f"[준비] 관심종목 {len(targets)}개 · {args.days}일(≈{args.days/365:.1f}년) "
          f"· 슬롯 {slots} · 시드 {args.seed_capital:,}원")

    dfs, mf, dates, failed = pb.prepare_universe(targets, args.days)
    print(f"[준비] 사용 {len(dfs)}종목 / 거래일 {len(dates)}일"
          + (f" · 제외 {len(failed)}" if failed else ""))

    thresholds = {
        "BUY_SCORE": config.ANALYSIS_THRESHOLDS["BUY_SCORE"],
        "BUY_RSI_MAX": config.ANALYSIS_THRESHOLDS["BUY_RSI_MAX"],
        "RISE_SCORE": config.ANALYSIS_THRESHOLDS["RISE_SCORE"],
        "WEIGHTS": config.SCORING_WEIGHTS,
    }

    sets = dial_sets()
    if args.only:
        sets = [x for x in sets if x[0].startswith(args.only)]

    # ATR_PERIOD가 바뀌면 지표·상태를 다시 만들어야 한다. 기간별로 한 번만 만들어 재사용.
    periods = sorted({ov.get("ind", {}).get("ATR_PERIOD",
                                            config.INDICATOR_PARAMS["ATR_PERIOD"])
                      for _g, _l, ov in sets})
    universe = {}
    for p in periods:
        d = dfs if p == config.INDICATOR_PARAMS["ATR_PERIOD"] else rebuild_atr(dfs, p)
        print(f"  [준비] ATR_PERIOD={p} 상태 사전계산...", end="\r", flush=True)
        universe[p] = (d, pb.precompute_status(d, thresholds))
    print(" " * 60, end="\r")

    from tools.audit_drawdown_axis import market_scale_by_date, make_scale_fn
    p_risk = getattr(config, "RISK_SCALING_PARAMS", {}) or {}
    mkt = market_scale_by_date(dates, args.days)
    dd = None
    if p_risk.get("USE_DRAWDOWN_RISK_SCALING", True):
        dd = (int(p_risk.get("DD_LOOKBACK_DAYS", 90)), float(p_risk.get("DD_LEVEL_1", 5.0)),
              float(p_risk.get("DD_SCALE_1", 0.9)), float(p_risk.get("DD_LEVEL_2", 10.0)),
              float(p_risk.get("DD_SCALE_2", 0.8)))

    saved_cfg = {k: getattr(config, k, None)
                 for k in ("TARGET_VOLATILITY", "VOLATILITY_SCALING_MIN")}
    saved_ind = dict(config.INDICATOR_PARAMS)
    codes = list(dfs.keys())

    k = max(1, args.subperiods)
    size = len(dates) // k
    windows = ([(f"구간{i+1}", dates[i*size:(i+1)*size if i < k-1 else len(dates)])
                for i in range(k)] if k > 1 else [("전체", dates)])

    print(f"\n{'='*126}")
    print(f"변동성 반응 다이얼 검증 — {args.trials}회 × {args.sample}종목 무작위 짝비교 "
          f"({args.days}일 ≈ {args.days/365:.1f}년)")
    print("=" * 126)

    for wname, wdates in windows:
        results = {(g, l): [] for g, l, _ in sets}
        rng = random.Random(args.seed)
        try:
            for t in range(args.trials):
                pick = rng.sample(codes, min(args.sample, len(codes)))
                sm = {c: mf.get(c, set()) for c in pick}
                for g, label, ov in sets:
                    per = ov.get("ind", {}).get("ATR_PERIOD",
                                                saved_ind["ATR_PERIOD"])
                    udfs, ustatus = universe[per]
                    restore(saved_cfg, saved_ind)
                    apply_overrides(ov, saved_cfg)
                    r = pb.run_portfolio({c: udfs[c] for c in pick},
                                         {c: ustatus[c] for c in pick},
                                         wdates, initial_capital=args.seed_capital,
                                         slots=slots, market_filter_dates=sm,
                                         risk_scale_by_date=make_scale_fn(mkt, dd))
                    results[(g, label)].append(metrics(r))
                print(f"  {wname} 시행 {t+1}/{args.trials}", end="\r", flush=True)
        finally:
            restore(saved_cfg, saved_ind)

        print(f"\n{'#'*10} {wname} ({len(wdates)} 거래일) {'#'*10}")
        by_group = defaultdict(dict)
        for (g, l), ms in results.items():
            by_group[g][l] = ms
        for g in dict.fromkeys(x[0] for x in sets):
            rows = by_group[g]
            base = next(l for l in rows if "현행" in l)
            fmt_table(g, rows, base)

    print("\n" + "-" * 126)
    print("상위10%·최대·>30% = fat-tail 보존 지표. 변동성 반응을 줄이면 포지션이 커지므로")
    print("  수익과 MDD가 함께 오른다 — 총수익만 보면 반드시 오판한다. MAR로 볼 것.")
    print("D는 하한 0.4 때문에 0.37 이하 설정이 현행과 같아야 정상이다(예측 검증).")


if __name__ == "__main__":
    main()
