"""진입 체결 시점 — 실매매는 장중 아무 때나 사고, 백테스트는 종가에 산다.

[왜] 실매매는 감시 주기마다 **미확정 장중 봉**으로 다시 채점한다(진행 중 봉을 실시간가로
 덮어 지표를 계산). 그래서 같은 날 안에서 매수 신호가 켜졌다 꺼지면 실제 체결 여부가
 "그날 몇 시에 스캔했는가"에 달린다 — 일봉 백테스트에는 존재하지 않는 자유도이고,
 진입 조건은 가격선이 아니라 **점수**라서 일봉의 고가·저가로는 근사조차 되지 않는다.
 tools/audit_intraday_signal_stability.py 가 30분봉 60일로 그 **빈도**만 쟀고, 손익은
 잰 적이 없었다. 3년치 분봉으로 시점별 판정을 미리 계산해(tools/build_intraday_status.py)
 그 자유도를 그대로 재현한다.

[팔 구성] 청산은 네 팔 모두 종가 모델로 고정한다 — 진입 축만 남기기 위해서다.
   A. 종가 진입(일봉 모델) — 현 백테스트
   B. 매 봉 진입          — 현 실매매 (기준선)
   C. 15:00 1회 스캔      — 마감 30분 전에만 진입 (60분봉 14:00 봉의 종가 = 15:00 가격)
   D. 15:30 1회 스캔      — 분봉 경로로 종가 진입을 재현(A와 비슷해야 정상 · 자기검증)

[읽는 법] B가 A보다 나쁘면 '장중에 켜졌다 꺼지는 신호를 쫓아 사는 것'이 손해라는 뜻이다.
 C가 B를 이기면 진입 스캔을 마감 직전으로 미루는 것이 개선안이 된다. 이번엔 C·D 모두
 그 시점 정보만 쓰므로 종가를 미리 아는 이점이 섞이지 않는다.

[선행] tools/fetch_intraday_tv.py → tools/build_intraday_status.py
[실행] python3 tools/audit_entry_bars.py --days 1200 --trials 15 --sample 20 --subperiods 3
"""
import argparse
import os
import random
import sys

import numpy as np
import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tools.audit_common import exits  # noqa: E402

import config  # noqa: E402
from modules import intraday_bars as ib  # noqa: E402
from modules import portfolio_backtest as pb  # noqa: E402

INITIAL_CAPITAL = 10_000_000
BASE = "B. 매 봉 진입(실매매)"
AT_1500 = {"1400"}
AT_CLOSE = {"1500"}


def arms(st):
    return [
        ("A. 종가 진입(일봉)", {}),
        (BASE,                {"intraday_status": st, "intraday_entry": True}),
        ("C. 15:00 1회 스캔",  {"intraday_status": st, "intraday_entry": True,
                             "entry_bar_times": AT_1500}),
        ("D. 15:30 1회(자기검증)", {"intraday_status": st, "intraday_entry": True,
                                "entry_bar_times": AT_CLOSE}),
    ]


def metrics(r):
    sells = exits(r)
    buys = [t for t in r["trades"] if t["reason"] == "매수"]
    profits = sorted((t["profit"] for t in sells), reverse=True)
    top10 = profits[:max(1, len(profits) // 10)]
    return {
        "ret": r["total_return"], "mdd": r["mdd"],
        "mar": r["total_return"] / abs(r["mdd"]) if r["mdd"] else float("nan"),
        "pf": r["pf"], "n": len(sells), "buys": len(buys),
        "win": sum(1 for p in profits if p > 0) / len(profits) * 100 if profits else 0.0,
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
    ap.add_argument("--min-coverage", type=float, default=0.9)
    ap.add_argument("--exclude-from", default="20260301")
    args = ap.parse_args()

    slots = args.slots or getattr(config, "SYSTEM_MAX_HOLDINGS", 4)
    config.session.load_stock_config()
    stocks = config.session.stock_data.get("stocks_kr", [])
    names = {s["code"]: s["name"] for s in stocks}
    print(f"[준비] 관심종목 {len(stocks)}개 · {args.days}일 · 슬롯 {slots} · {args.interval}")

    dfs, mf, dates, _failed = pb.prepare_universe([(s["code"], s["name"]) for s in stocks],
                                                  args.days)
    # 게이트(일봉 정합 98% · 커버리지)는 modules/intraday_bars.gate_universe 가 단독 보유한다.
    _bars, st, keep, drop = ib.gate_universe(dfs, args.interval,
                                             min_coverage=args.min_coverage)
    if drop:
        print(f"[제외] {len(drop)}종목 — " + ", ".join(f"{names.get(c, c)}({w})" for c, w in drop))
    dfs = {c: dfs[c] for c in keep}
    mf = {c: mf.get(c, set()) for c in keep}
    dates = ib.covered_dates(_bars, dates)
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

    sets = arms(st)
    codes = list(dfs.keys())
    k = max(1, args.subperiods)
    size = max(1, len(head) // k)
    windows = [("제외 전 전체", head)]
    if k > 1:
        windows += [(f"구간{i + 1}", head[i * size:(i + 1) * size if i < k - 1 else len(head)])
                    for i in range(k)]
    if tail:
        windows.append(("[대조] 제외구간(고변동)", tail))

    all_results = {}
    for wname, wdates in windows:
        results = {label: [] for label, _kw in sets}
        rng = random.Random(args.seed)
        for t in range(args.trials):
            pick = rng.sample(codes, min(args.sample, len(codes)))
            sd = {c: dfs[c] for c in pick}
            sc = {c: status[c] for c in pick}
            sm = {c: mf.get(c, set()) for c in pick}
            for label, kw in sets:
                kw2 = dict(kw)
                if "intraday_status" in kw2:
                    kw2["intraday_status"] = {c: st[c] for c in pick}
                r = pb.run_portfolio(sd, sc, wdates, initial_capital=args.seed_capital,
                                     slots=slots, market_filter_dates=sm,
                                     risk_scale_by_date=new_scale_fn(), **kw2)
                results[label].append(metrics(r))
            print(f"  {wname} 시행 {t + 1}/{args.trials}", end="\r", flush=True)
        all_results[wname] = results
    print(" " * 50, end="\r")

    W = 116
    print(f"\n{'=' * W}")
    print(f"진입 스캔 시점(실제 {args.interval} 분봉) — {args.trials}회 × {args.sample}종목 "
          f"짝비교 (기준선: {BASE})")
    print(f"{'=' * W}")
    for wname, wdates in windows:
        results = all_results[wname]
        base = results[BASE]
        print(f"\n########## {wname} ({len(wdates)} 거래일) ##########")
        print(f"{'설정':<22}{'수익%':>9}{'MDD%':>8}{'MAR':>7}{'PF':>6}{'매수':>6}{'청산':>6}"
              f"{'승률%':>7}{'상위10%':>9}{'최대':>9}{'>30%':>6}{'보유일':>7}"
              f"{'승-무-패':>10}{'MAR승':>7}")
        print("-" * W)
        for label, _kw in sets:
            rs = results[label]
            m = lambda k: float(np.median([x[k] for x in rs]))  # noqa: E731
            is_base = label == BASE
            tie = sum(1 for a, b in zip(rs, base) if abs(a["ret"] - b["ret"]) < 1e-9)
            los = sum(1 for a, b in zip(rs, base) if a["ret"] < b["ret"] - 1e-9)
            rw = sum(1 for a, b in zip(rs, base) if a["ret"] > b["ret"])
            mw = sum(1 for a, b in zip(rs, base) if a["mar"] > b["mar"])
            print(f"{label:<22}{m('ret'):>9.1f}{m('mdd'):>8.1f}{m('mar'):>7.2f}{m('pf'):>6.2f}"
                  f"{m('buys'):>6.0f}{m('n'):>6.0f}{m('win'):>7.1f}{m('top10'):>9.1f}"
                  f"{m('best'):>9.1f}{m('big'):>6.0f}{m('days'):>7.1f}"
                  f"{'—' if is_base else f'{rw}-{tie}-{los}':>10}"
                  f"{'—' if is_base else f'{mw}/{len(rs)}':>7}")

    print("\n" + "-" * W)
    print("[읽는 법] D(15:30 1회)가 A(종가 일봉)와 비슷해야 분봉 경로가 건전하다.")
    print(" B가 A보다 낮으면 '장중에 켜졌다 꺼지는 신호를 쫓아 사는 것'의 값이 그 차이다.")


if __name__ == "__main__":
    main()
