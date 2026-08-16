"""트레일링 콜백 폭(TRAILING_ATR_MULTIPLIER) — 장중 체결 세계에서 다시 잰다.

[왜 다시 재는가] 현행 3.5는 **종가 체결 백테스트**에서 정해졌다(2.5·3.0은 수익승 0/15로
 완패, 4.0은 구간 편차로 기각). 그런데 실매매는 감시 주기마다 실시간가로 판정·집행한다 —
 2026-08-16 분봉 검증에서 두 세계의 성과가 크게 갈렸다(전체창 수익 108~114% vs 133~168%).
 **같은 다이얼의 최적값이 두 세계에서 같다는 보장이 없다.** 특히 장중 체결은 되밀린 꼬리
 (wick)에 걸리므로, 콜백이 좁을수록 손해가 증폭될 수 있다.

[무엇을 보는가] 콜백은 주청산 수단이라 총수익만으로는 못 고른다.
   · 반납률 — TS 청산 건의 (MFE - 실현수익). 콜백 폭이 직접 만드는 값이다.
   · fat-tail — 상위10%·최대·>30%. 좁히면 여기가 먼저 무너진다(기존 실측의 패턴).
   · TS 건수·무장률 — 콜백이 실제로 구속하는가.
 두 세계를 **같은 실행 안에서** 나란히 재야 '최적값이 옮겨갔다'를 말할 수 있다.

[모델] 장중 팔은 실제 60분봉으로 봉마다 판정한다(체결가 = 그 봉 종가). 증액은 두 세계
 모두 일봉으로 고정한다 — 축을 하나만 움직이기 위해서다.

[선행] tools/fetch_intraday_tv.py → tools/build_intraday_status.py
[실행] python3 tools/audit_ts_callback_intraday.py --days 1200 --trials 15 --sample 20
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

INITIAL_CAPITAL = 10_000_000
SELL_REASONS = ("ATR손절", "손절", "본전청산", "시간청산", "트레일링스탑", "점수하락", "이익보호")
MULTS = (2.0, 2.5, 3.0, 3.5, 4.0, 4.5, 5.0)
BASE = "장중 3.5 (현행)"


def arms(bars, st, worlds):
    out = []
    for w in worlds:
        for m in MULTS:
            kw = ({"intraday_bars": bars, "intraday_status": st, "intraday_pyramid": False}
                  if w == "장중" else {})
            label = f"{w} {m}" + (" (현행)" if (w == "장중" and m == 3.5) else "")
            out.append((label, w, m, kw))
    return out


def metrics(r):
    sells = [t for t in r["trades"] if t["reason"] in SELL_REASONS]
    ts = [t for t in sells if t["reason"] == "트레일링스탑"]
    profits = sorted((t["profit"] for t in sells), reverse=True)
    top10 = profits[:max(1, len(profits) // 10)]
    gb = [t["mfe"] - t["profit"] for t in ts if t.get("mfe") is not None]
    armed = [t for t in sells if t.get("armed")]
    return {
        "ret": r["total_return"], "mdd": r["mdd"],
        "mar": r["total_return"] / abs(r["mdd"]) if r["mdd"] else float("nan"),
        "pf": r["pf"], "n": len(sells), "ts_n": len(ts),
        "armed": len(armed) / len(sells) * 100 if sells else 0.0,
        "giveback": float(np.median(gb)) if gb else 0.0,
        "top10": float(np.mean(top10)) if top10 else 0.0,
        "best": profits[0] if profits else 0.0,
        "big": sum(1 for p in profits if p >= 30),
        "days": float(np.median([t["days"] for t in sells])) if sells else 0.0,
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--trials", type=int, default=15)
    ap.add_argument("--sample", type=int, default=20)
    ap.add_argument("--days", type=int, default=1200)
    ap.add_argument("--interval", default="60m")
    ap.add_argument("--slots", type=int, default=None)
    ap.add_argument("--seed-capital", type=int, default=INITIAL_CAPITAL)
    ap.add_argument("--seed", type=int, default=20260816)
    ap.add_argument("--subperiods", type=int, default=3)
    ap.add_argument("--worlds", default="장중,종가", help="비교할 체결 세계")
    ap.add_argument("--min-coverage", type=float, default=0.9)
    ap.add_argument("--exclude-from", default="20260301")
    args = ap.parse_args()

    worlds = [w for w in args.worlds.split(",") if w]
    slots = args.slots or getattr(config, "SYSTEM_MAX_HOLDINGS", 4)
    config.session.load_stock_config()
    stocks = config.session.stock_data.get("stocks_kr", [])
    names = {s["code"]: s["name"] for s in stocks}
    print(f"[준비] 관심종목 {len(stocks)}개 · {args.days}일 · 슬롯 {slots} · {args.interval}")
    print(f"[기준] 콜백 하한 {config.SELL_STRATEGY.get('TRAILING_STOP_CALLBACK_RATE')}% · "
          f"발동 배수 {config.SELL_STRATEGY.get('TS_ACTIVATION_ATR_MULTIPLIER')} · "
          f"발동 모드 {config.SELL_STRATEGY.get('TS_ACTIVATION_MODE')} · "
          f"반납 상한 {config.SELL_STRATEGY.get('TS_MAX_GIVEBACK_RATIO')}")

    dfs, mf, dates, _f = pb.prepare_universe([(s["code"], s["name"]) for s in stocks], args.days)
    # [중요] 분봉 게이트는 **장중 팔이 있을 때만** 건다. 종가 세계만 재는데도 게이트를 걸면
    #  창이 분봉 보유 구간(2023-09~)으로 잘려 '10년을 쟀다'고 착각하게 된다 — 실제로 그 착오로
    #  3년치를 10년이라 보고한 적이 있다(2026-08-16). 종가 전용 실행은 전체 창을 그대로 쓴다.
    bars, st = {}, {}
    if "장중" in worlds:
        bars, st, keep, drop = ib.gate_universe(dfs, args.interval,
                                                min_coverage=args.min_coverage)
        if drop:
            print(f"[제외] {len(drop)}종목 — "
                  + ", ".join(f"{names.get(c, c)}({w})" for c, w in drop))
        dfs = {c: dfs[c] for c in keep}
        mf = {c: mf.get(c, set()) for c in keep}
        dates = ib.covered_dates(bars, dates)
    if not dates:
        print("[중단] 겹치는 거래일 없음")
        return

    thresholds = {
        "BUY_SCORE": config.ANALYSIS_THRESHOLDS["BUY_SCORE"],
        "BUY_RSI_MAX": config.ANALYSIS_THRESHOLDS["BUY_RSI_MAX"],
        "RISE_SCORE": config.ANALYSIS_THRESHOLDS["RISE_SCORE"],
        "WEIGHTS": config.SCORING_WEIGHTS,
    }
    status = pb.precompute_status(dfs, thresholds)
    print(f"[준비] 사용 {len(dfs)}종목 / 거래일 {len(dates)}일 ({dates[0]}~{dates[-1]})")

    from tools.audit_drawdown_axis import market_scale_by_date, make_scale_fn
    p = getattr(config, "RISK_SCALING_PARAMS", {}) or {}
    mkt = market_scale_by_date(dates, args.days)
    dd = None
    if p.get("USE_DRAWDOWN_RISK_SCALING", True):
        dd = (int(p.get("DD_LOOKBACK_DAYS", 90)), float(p.get("DD_LEVEL_1", 5.0)),
              float(p.get("DD_SCALE_1", 0.9)), float(p.get("DD_LEVEL_2", 10.0)),
              float(p.get("DD_SCALE_2", 0.8)))
    new_scale_fn = lambda: make_scale_fn(mkt, dd)  # noqa: E731

    cut = "".join(ch for ch in str(args.exclude_from) if ch.isdigit())
    head = [d for d in dates if not cut or cut == "0" or d < cut]
    tail = [d for d in dates if cut and cut != "0" and d >= cut]

    sets = arms(bars, st, worlds)
    codes = list(dfs.keys())
    k = max(1, args.subperiods)
    size = max(1, len(head) // k)
    windows = [("제외 전 전체", head)]
    if k > 1:
        windows += [(f"구간{i + 1}", head[i * size:(i + 1) * size if i < k - 1 else len(head)])
                    for i in range(k)]
    if tail:
        windows.append(("[대조] 제외구간(고변동)", tail))

    saved = config.SELL_STRATEGY["TRAILING_ATR_MULTIPLIER"]
    all_results = {}
    try:
        for wname, wdates in windows:
            results = {label: [] for label, _w, _m, _kw in sets}
            rng = random.Random(args.seed)
            for t in range(args.trials):
                pick = rng.sample(codes, min(args.sample, len(codes)))
                sd = {c: dfs[c] for c in pick}
                sc = {c: status[c] for c in pick}
                sm = {c: mf.get(c, set()) for c in pick}
                for label, _w, m, kw in sets:
                    kw2 = dict(kw)
                    if "intraday_bars" in kw2:
                        kw2["intraday_bars"] = {c: bars[c] for c in pick}
                        kw2["intraday_status"] = {c: st[c] for c in pick}
                    config.SELL_STRATEGY["TRAILING_ATR_MULTIPLIER"] = m
                    r = pb.run_portfolio(sd, sc, wdates, initial_capital=args.seed_capital,
                                         slots=slots, market_filter_dates=sm,
                                         risk_scale_by_date=new_scale_fn(), **kw2)
                    results[label].append(metrics(r))
                print(f"  {wname} 시행 {t + 1}/{args.trials}", end="\r", flush=True)
            all_results[wname] = results
    finally:
        config.SELL_STRATEGY["TRAILING_ATR_MULTIPLIER"] = saved
    print(" " * 50, end="\r")

    W = 118
    print(f"\n{'=' * W}")
    print(f"TS 콜백 배수 × 체결 세계 — {args.trials}회 × {args.sample}종목 짝비교 (기준선: {BASE})")
    print(f"{'=' * W}")
    for wname, wdates in windows:
        results = all_results[wname]
        base = results.get(BASE)
        print(f"\n########## {wname} ({len(wdates)} 거래일) ##########")
        print(f"{'설정':<16}{'수익%':>9}{'MDD%':>8}{'MAR':>7}{'PF':>6}{'TS건':>6}{'무장%':>7}"
              f"{'반납%p':>8}{'상위10%':>9}{'최대':>9}{'>30%':>6}{'보유일':>7}{'승-무-패':>10}{'MAR승':>7}")
        print("-" * W)
        last_world = None
        for label, w, _m, _kw in sets:
            if w != last_world:
                print(f"[{w} 체결]")
                last_world = w
            rs = results[label]
            m_ = lambda k: float(np.median([x[k] for x in rs]))  # noqa: E731
            is_base = label == BASE
            if base is None:
                rec = mw = "—"
            else:
                tie = sum(1 for a, b in zip(rs, base) if abs(a["ret"] - b["ret"]) < 1e-9)
                los = sum(1 for a, b in zip(rs, base) if a["ret"] < b["ret"] - 1e-9)
                rw = sum(1 for a, b in zip(rs, base) if a["ret"] > b["ret"])
                rec = "—" if is_base else f"{rw}-{tie}-{los}"
                mww = sum(1 for a, b in zip(rs, base) if a["mar"] > b["mar"])
                mw = "—" if is_base else f"{mww}/{len(rs)}"
            print(f"{label:<16}{m_('ret'):>9.1f}{m_('mdd'):>8.1f}{m_('mar'):>7.2f}{m_('pf'):>6.2f}"
                  f"{m_('ts_n'):>6.0f}{m_('armed'):>7.1f}{m_('giveback'):>8.1f}{m_('top10'):>9.1f}"
                  f"{m_('best'):>9.1f}{m_('big'):>6.0f}{m_('days'):>7.1f}{rec:>10}{mw:>7}")

    print("\n" + "-" * W)
    print("반납%p = TS 청산 건의 (보유 중 최대 평가수익률 − 실현 수익률) 중앙값. 콜백 폭이 직접 만든다.")
    print("[읽는 법] 좁히는 쪽(2.0~3.0)이 이기려면 반납이 줄면서 fat-tail(상위10%·최대)이")
    print(" 버텨야 한다. 반납만 줄고 꼬리가 깎이면 그건 승자를 일찍 자른 것이다.")


if __name__ == "__main__":
    main()
