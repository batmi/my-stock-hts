"""피라미딩 체결 시점 — 장중 추격인가, 종가로 확인한 뒤인가. (실제 분봉)

[왜] 2026-08-16 `audit_pyramid_perday.py`로 하루 횟수를 재려다 더 큰 축이 나왔다.
 **같은 '하루 1회'라도 종가 체결이 장중 체결을 압도**했다 — 10년 전체창 MAR 8.28 vs 6.16,
 MDD -31.8% vs -37.7%, 씨드 4개 전부 같은 방향. 원인은 분명하다. 장중 추격은 '발동선에
 닿았다가 되밀린 날'에까지 얹어(증액 141 → 169건) 확인되지 않은 돌파에 평단을 올린다.

 그런데 적용을 보류했다. 백테스트의 종가 팔에는 **'그날 종가가 발동선 위였다'를 미리 아는
 이점**이 섞여 있기 때문이다. 종가를 보고 그 종가에 사는 것은 실매매에서 불가능하다.
 이제 분봉이 있으니 그 선견을 걷어낸 대응물로 다시 잰다.

[팔 — 청산은 다섯 팔 모두 분봉으로 고정한다. 움직이는 축은 증액 하나뿐이다.]
   1. 매봉·무제한 (현행)   봉마다 판정·추격 체결. 실매매(_try_pyramid_buy) 그대로.
   2. 매봉·하루1회          시점은 그대로 두고 횟수만 제한 — '횟수'와 '시점'을 가르는 대조군.
   3. 15:00 1회             14:00봉 종가(=15:00 가격)로 하루 한 번. 시스템은 15:20~15:30
                            종가 단일가에 매매하지 않으므로, 실제로 쓸 수 있는 마지막 시점.
   4. 종가→익일시가         일봉 종가로 판정하고 다음 날 시가에 체결. **선견 없는 종가 확인.**
   5. [참고] 종가→종가       15:00봉 종가(=당일 종가)로 판정·체결. 기존 리드의 재현이자
                            선견을 포함한 상한 — 실매매로 옮길 수 없는 팔이다.

[읽는 법] 4번이 1번을 이기면 리드는 선견이 아니다 → 적용 후보. 5번만 이기면 선견이었다.
 3번은 그 중간 — 15:00까지 버틴 돌파만 받아들이는 절충안이다.

[선행] tools/fetch_intraday_tv.py → tools/build_intraday_status.py
[실행] python3 tools/audit_pyramid_fill_timing.py --trials 15 --sample 20 --seeds 3
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
BASE = "1. 매봉·무제한(현행)"

ARMS = [
    (BASE,                     dict(pyr_per_day=0)),
    ("2. 매봉·하루1회",         dict(pyr_per_day=1)),
    ("3. 15:00 1회",           dict(pyr_per_day=1, bar_pyr_times={"1400"})),
    ("4. 종가→익일시가",        dict(pyr_per_day=1, pyr_next_open=True)),
    ("5. [참고]종가→종가",      dict(pyr_per_day=1, bar_pyr_times={"1500"})),
]

# [교차 확인] --daily-exits 는 청산까지 일봉으로 되돌린 대조 실행이다. 기존 리드
#  (audit_pyramid_perday, MAR 8.28 vs 6.16)가 **같은 종목·같은 기간**에서 재현되는지
#  본다. 여기서 재현되고 분봉 세계에서만 사라진다면, 그 리드는 증액 축의 효과가 아니라
#  청산 세계와 얽힌 것이다 — 도구 결함과 구별하는 유일한 방법.
DAILY_ARMS = [
    (BASE,                     dict(pyr_intraday=True, pyr_per_day=0)),
    ("2. 장중·하루1회",         dict(pyr_intraday=True, pyr_per_day=1)),
    ("4. 종가→익일시가",        dict(pyr_per_day=1, pyr_next_open=True)),
    ("5. 종가→종가",            dict(pyr_intraday=False, pyr_per_day=1)),
]


def metrics(r):
    sells = [t for t in r["trades"] if t["reason"] in SELL_REASONS]
    pyr = [t for t in r["trades"] if str(t["reason"]).startswith("피라미딩")]
    profits = sorted((t["profit"] for t in sells), reverse=True)
    top10 = profits[:max(1, len(profits) // 10)]
    gross_gain = sum(t["profit_amt"] for t in sells if t["profit_amt"] > 0)
    ts_gain = sum(t["profit_amt"] for t in sells
                  if t["reason"] == "트레일링스탑" and t["profit_amt"] > 0)
    # 같은 날 2회 이상 얹은 날 수 — 1번 팔에서만 0이 아니어야 한다.
    per_day = {}
    for t in pyr:
        per_day[(t["code"], t["date"])] = per_day.get((t["code"], t["date"]), 0) + 1
    return {
        "ret": r["total_return"], "mdd": r["mdd"],
        "mar": r["total_return"] / abs(r["mdd"]) if r["mdd"] else float("nan"),
        "pf": r["pf"], "n": len(sells),
        "pyr_n": len(pyr),
        "pyr_per_exit": len(pyr) / len(sells) * 100 if sells else 0.0,
        "multi_day": sum(1 for v in per_day.values() if v >= 2),
        "top10": float(np.mean(top10)) if top10 else 0.0,
        "best": profits[0] if profits else 0.0,
        "big": sum(1 for p in profits if p >= 30),
        "ts_share": (ts_gain / gross_gain * 100) if gross_gain > 0 else 0.0,
        "days": float(np.median([t["days"] for t in sells])) if sells else 0.0,
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--trials", type=int, default=15)
    ap.add_argument("--sample", type=int, default=20)
    ap.add_argument("--days", type=int, default=1200)
    ap.add_argument("--seeds", type=int, default=3)
    ap.add_argument("--seed", type=int, default=20260816)
    ap.add_argument("--interval", default="60m")
    ap.add_argument("--slots", type=int, default=None)
    ap.add_argument("--seed-capital", type=int, default=INITIAL_CAPITAL)
    ap.add_argument("--min-coverage", type=float, default=0.9)
    ap.add_argument("--subperiods", type=int, default=3)
    ap.add_argument("--exclude-from", default="20260301")
    ap.add_argument("--daily-exits", action="store_true",
                    help="청산까지 일봉으로 되돌린 대조 실행(기존 리드 재현 확인)")
    ap.add_argument("--no-gate", action="store_true",
                    help="분봉 게이트를 걸지 않는다(--daily-exits 전용: 10년 전체 창을 쓴다)")
    args = ap.parse_args()

    arms_def = DAILY_ARMS if args.daily_exits else ARMS
    slots = args.slots or getattr(config, "SYSTEM_MAX_HOLDINGS", 4)
    config.session.load_stock_config()
    stocks = config.session.stock_data.get("stocks_kr", [])
    names = {s["code"]: s["name"] for s in stocks}
    print(f"[준비] 관심종목 {len(stocks)}개 · {args.days}일 · 슬롯 {slots} · {args.interval}")
    print(f"[기준] 증액 트리거 +{config.ANALYSIS_THRESHOLDS.get('PYRAMIDING_PROFIT_TRIGGER')}% · "
          f"비율 {config.ANALYSIS_THRESHOLDS.get('PYRAMIDING_RATIO')} · "
          f"최대 {config.ANALYSIS_THRESHOLDS.get('PYRAMIDING_MAX_COUNT')}차")

    dfs, mf, dates, _f = pb.prepare_universe([(s["code"], s["name"]) for s in stocks], args.days)
    bars, st = {}, {}
    if args.no_gate:
        # [함정 주의] 분봉을 안 쓰는 실행에 게이트를 걸면 창이 분봉 보유 구간으로 잘린다.
        if not args.daily_exits:
            print("[중단] --no-gate 는 --daily-exits 와만 함께 쓴다")
            return
        print("[게이트 없음] 분봉을 쓰지 않으므로 전체 창·전체 종목을 그대로 쓴다")
    else:
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
    total = len(windows) * args.seeds * args.trials * len(arms_def)
    done = 0
    for wname, wdates in windows:
        res = {label: [] for label, _kw in arms_def}
        for si in range(args.seeds):
            rng = random.Random(args.seed + si * 1009)
            for _t in range(args.trials):
                pick = rng.sample(codes, min(args.sample, len(codes)))
                sd = {c: dfs[c] for c in pick}
                sc = {c: status[c] for c in pick}
                sm = {c: mf.get(c, set()) for c in pick}
                sb = {c: bars[c] for c in pick} if bars else {}
                ss = {c: st[c] for c in pick} if st else {}
                for label, kw in arms_def:
                    extra = ({} if args.daily_exits
                             else {"intraday_bars": sb, "intraday_status": ss})
                    r = pb.run_portfolio(sd, sc, wdates, initial_capital=args.seed_capital,
                                         slots=slots, market_filter_dates=sm,
                                         risk_scale_by_date=new_scale_fn(), **extra, **kw)
                    res[label].append(metrics(r))
                    done += 1
                print(f"  {wname} 씨드{si + 1} {done}/{total}", end="\r", flush=True)
        all_results[wname] = res
    print(" " * 60, end="\r")

    W = 122
    print(f"\n{'=' * W}")
    print(f"피라미딩 체결 시점 — {args.trials}회 × {args.sample}종목 × 씨드 {args.seeds}개 "
          f"(청산 {'일봉' if args.daily_exits else '분봉 고정'} · 기준선 {BASE})")
    print(f"{'=' * W}")
    for wname, wdates in windows:
        res = all_results[wname]
        base = res[BASE]
        print(f"\n########## {wname} ({len(wdates)} 거래일) ##########")
        print(f"{'팔':<20}{'수익%':>9}{'MDD%':>8}{'MAR':>7}{'PF':>6}{'증액수':>7}{'증액/청산%':>11}"
              f"{'같은날2+':>9}{'상위10%':>9}{'최대':>9}{'>30%':>6}{'TS이익%':>8}{'보유일':>7}"
              f"{'승-무-패':>10}{'MAR승':>7}")
        print("-" * W)
        for label, _kw in arms_def:
            rs = res[label]
            m = lambda k: float(np.median([x[k] for x in rs]))  # noqa: E731
            is_base = label == BASE
            tie = sum(1 for a, b in zip(rs, base) if abs(a["ret"] - b["ret"]) < 1e-9)
            los = sum(1 for a, b in zip(rs, base) if a["ret"] < b["ret"] - 1e-9)
            rw = sum(1 for a, b in zip(rs, base) if a["ret"] > b["ret"])
            mw = sum(1 for a, b in zip(rs, base) if a["mar"] > b["mar"])
            print(f"{label:<20}{m('ret'):>9.1f}{m('mdd'):>8.1f}{m('mar'):>7.2f}{m('pf'):>6.2f}"
                  f"{m('pyr_n'):>7.0f}{m('pyr_per_exit'):>11.1f}{m('multi_day'):>9.0f}"
                  f"{m('top10'):>9.1f}{m('best'):>9.1f}{m('big'):>6.0f}{m('ts_share'):>8.1f}"
                  f"{m('days'):>7.1f}"
                  f"{'—' if is_base else f'{rw}-{tie}-{los}':>10}"
                  f"{'—' if is_base else f'{mw}/{len(rs)}':>7}")

    print("\n" + "-" * W)
    print("[핵심] 4번(종가→익일시가)이 1번을 이기면 리드는 선견이 아니다 — 실매매 적용 후보.")
    print("       5번만 이기면 '종가를 보고 그 종가에 산다'는 불가능한 이점이었을 뿐이다.")
    print("[대조] 2번은 시점을 그대로 두고 횟수만 줄인 팔이다. 2번이 1번과 비슷하면 축은 횟수가 아니다.")


if __name__ == "__main__":
    main()
