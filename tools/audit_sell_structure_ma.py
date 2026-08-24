"""점수하락 매도의 '구조 훼손' 기준선 — 60일선이 맞는가.

[왜] 점수 매도는 `점수 < SELL_SCORE` **그리고** `주가 < 60일선`을 동시에 요구한다.
 점수 쪽(4.0)은 감사됐지만 **60일이라는 기간은 코드에 박혀 있고 한 번도 스윕된 적이 없다**
 (backtest.py·portfolio_backtest.py·engine.analyze_sell 세 곳 모두 EMA60 하드코딩).

 2026-08-16 `audit_exit_reason_mix.py`에서 이 규칙의 중요도가 올라갔다 — TS 콜백을 넓히면
 점수하락이 **주청산이 된다**(구간1·3에서 TS를 제치고 이익 기여 1위). 부수적 안전장치가
 아니라 잠재적 주청산 경로이므로, 그 문턱이 어디 있어야 하는지는 답이 있어야 한다.

[무엇을 보는가] 기준선을 낮추면(EMA20) 더 일찍 팔고 높이면(EMA120) 더 늦게 판다.
   · 점수하락 건수·이익 기여 — 이 규칙이 실제로 얼마나 일하는가
   · TS이익비중 — '주청산은 TS'라는 설계가 유지되는가
   · fat-tail(상위10%·최대) — 늦게 파는 쪽이 승자를 더 태우는가
 '없음'(조건 제거) 팔을 함께 둔다 — 조건 자체의 값어치를 재는 대조군이다.

[실행] python3 tools/audit_sell_structure_ma.py --days 3650 --trials 15 --sample 25 --seeds 3
"""
import argparse
import os
import random
import sys

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tools.audit_common import exits, seed_notice  # noqa: E402

import config  # noqa: E402
from modules import portfolio_backtest as pb  # noqa: E402

INITIAL_CAPITAL = 10_000_000
BASE = "EMA60 (현행)"
# (라벨, 컬럼) — EMA40·EMA90은 감사에서 직접 계산해 붙인다.
ARMS = [
    ("없음(조건제거)", "__none__"),
    ("EMA20", "EMA20"),
    ("EMA40", "EMA40"),
    (BASE, "EMA60"),
    ("EMA90", "EMA90"),
    ("EMA120", "EMA120"),
]


def metrics(r):
    sells = exits(r)
    sc = [t for t in sells if t["reason"] == "점수하락"]
    ts = [t for t in sells if t["reason"] == "트레일링스탑"]
    profits = sorted((t["profit"] for t in sells), reverse=True)
    top10 = profits[:max(1, len(profits) // 10)]
    gross = sum(t["profit_amt"] for t in sells if t["profit_amt"] > 0)
    g = lambda xs: sum(t["profit_amt"] for t in xs if t["profit_amt"] > 0)  # noqa: E731
    return {
        "ret": r["total_return"], "mdd": r["mdd"],
        "mar": r["total_return"] / abs(r["mdd"]) if r["mdd"] else float("nan"),
        "pf": r["pf"], "n": len(sells),
        "sc_n": len(sc),
        "sc_share": g(sc) / gross * 100 if gross > 0 else 0.0,
        "sc_rate": float(np.median([t["profit"] for t in sc])) if sc else 0.0,
        "sc_days": float(np.median([t["days"] for t in sc])) if sc else 0.0,
        "ts_share": g(ts) / gross * 100 if gross > 0 else 0.0,
        "top10": float(np.mean(top10)) if top10 else 0.0,
        "best": profits[0] if profits else 0.0,
        "big": sum(1 for p in profits if p >= 30),
        "days": float(np.median([t["days"] for t in sells])) if sells else 0.0,
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--trials", type=int, default=15)
    ap.add_argument("--sample", type=int, default=25)
    ap.add_argument("--days", type=int, default=3650)
    ap.add_argument("--seeds", type=int, default=3)
    ap.add_argument("--seed", type=int, default=20260816)
    ap.add_argument("--slots", type=int, default=None)
    ap.add_argument("--seed-capital", type=int, default=INITIAL_CAPITAL)
    ap.add_argument("--subperiods", type=int, default=3)
    ap.add_argument("--exclude-from", default="20260301")
    args = ap.parse_args()
    seed_notice(args.seeds, example="--seeds 3")

    slots = args.slots or getattr(config, "SYSTEM_MAX_HOLDINGS", 4)
    config.session.load_stock_config()
    stocks = config.session.stock_data.get("stocks_kr", [])
    print(f"[준비] 관심종목 {len(stocks)}개 · {args.days}일 · 슬롯 {slots}")
    print(f"[기준] SELL_SCORE {config.SELL_STRATEGY.get('SELL_SCORE')} · 구조 기준선 EMA60(하드코딩)")

    dfs, mf, dates, _f = pb.prepare_universe([(s["code"], s["name"]) for s in stocks], args.days)
    # 감사 전용 컬럼 — 기존 지표 계산을 건드리지 않고 여기서만 붙인다.
    for code, df in dfs.items():
        for span in (40, 90):
            df[f"EMA{span}"] = df["close"].ewm(span=span, adjust=False).mean()

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
    k = max(1, args.subperiods)
    size = max(1, len(head) // k)
    windows = [("제외 전 전체", head)]
    if k > 1:
        windows += [(f"구간{i + 1}", head[i * size:(i + 1) * size if i < k - 1 else len(head)])
                    for i in range(k)]
    if tail:
        windows.append(("[대조] 제외구간(고변동)", tail))

    codes = list(dfs.keys())
    all_results = {}
    total = len(windows) * args.seeds * args.trials * len(ARMS)
    done = 0
    for wname, wdates in windows:
        res = {lbl: [] for lbl, _c in ARMS}
        for si in range(args.seeds):
            rng = random.Random(args.seed + si * 1009)
            for _t in range(args.trials):
                pick = rng.sample(codes, min(args.sample, len(codes)))
                sd = {c: dfs[c] for c in pick}
                sc_ = {c: status[c] for c in pick}
                sm = {c: mf.get(c, set()) for c in pick}
                for lbl, col in ARMS:
                    r = pb.run_portfolio(sd, sc_, wdates, initial_capital=args.seed_capital,
                                         slots=slots, market_filter_dates=sm,
                                         risk_scale_by_date=new_scale_fn(),
                                         sell_structure_ma=col)
                    res[lbl].append(metrics(r))
                    done += 1
                print(f"  {wname} 씨드{si + 1} {done}/{total}", end="\r", flush=True)
        all_results[wname] = res
    print(" " * 60, end="\r")

    W = 122
    print(f"\n{'=' * W}")
    print(f"점수하락 매도의 구조 기준선 — {args.trials}회 × {args.sample}종목 × 씨드 {args.seeds}개 "
          f"(기준선 {BASE})")
    print(f"{'=' * W}")
    for wname, wdates in windows:
        res = all_results[wname]
        base = res[BASE]
        print(f"\n########## {wname} ({len(wdates)} 거래일) ##########")
        print(f"{'기준선':<14}{'수익%':>9}{'MDD%':>8}{'MAR':>7}{'PF':>6}{'청산':>6}{'점수하락':>8}"
              f"{'점수이익%':>10}{'점수중앙%':>10}{'점수일':>7}{'TS이익%':>8}{'상위10%':>9}"
              f"{'최대':>9}{'승-무-패':>10}{'MAR승':>7}")
        print("-" * W)
        for lbl, _c in ARMS:
            rs = res[lbl]
            m = lambda k: float(np.median([x[k] for x in rs]))  # noqa: E731
            is_base = lbl == BASE
            tie = sum(1 for a, b in zip(rs, base) if abs(a["ret"] - b["ret"]) < 1e-9)
            los = sum(1 for a, b in zip(rs, base) if a["ret"] < b["ret"] - 1e-9)
            rw = sum(1 for a, b in zip(rs, base) if a["ret"] > b["ret"])
            mw = sum(1 for a, b in zip(rs, base) if a["mar"] > b["mar"])
            print(f"{lbl:<14}{m('ret'):>9.1f}{m('mdd'):>8.1f}{m('mar'):>7.2f}{m('pf'):>6.2f}"
                  f"{m('n'):>6.0f}{m('sc_n'):>8.0f}{m('sc_share'):>10.1f}{m('sc_rate'):>+10.1f}"
                  f"{m('sc_days'):>7.0f}{m('ts_share'):>8.1f}{m('top10'):>9.1f}{m('best'):>9.1f}"
                  f"{'—' if is_base else f'{rw}-{tie}-{los}':>10}"
                  f"{'—' if is_base else f'{mw}/{len(rs)}':>7}")

    print("\n" + "-" * W)
    print("[읽는 법] '없음'이 이기면 구조 조건 자체가 손해다. 기준선이 낮을수록(EMA20) 일찍 판다.")
    print("[주의] 실매매는 EMA60 하드코딩이다. 바꾸려면 engine.analyze_sell·backtest 세 곳을 함께 고칠 것.")


if __name__ == "__main__":
    main()
