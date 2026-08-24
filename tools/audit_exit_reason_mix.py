"""TS 콜백을 넓히면 이익은 어디로 가는가 — 청산사유별 자금 이동 분해.

[왜] `TRAILING_ATR_MULTIPLIER` 4.5는 10년 종가 세계에서 수익승 50/60·MAR승 48/60으로
 3.5를 압도했는데도 채택을 보류했다. 이유는 단 하나 — **TS이익비중이 내려가 '주청산은
 샹들리에 TS'라는 설계 전제가 깨진다**는 것이었다. 그런데 그 전제가 실제로 깨지는지는
 잰 적이 없다. TS이익비중이 내려가는 데는 두 가지가 있고 뜻이 정반대다.

   (a) 점수하락으로 이동 → 추세 종료를 **판단해서** 나갔다. 설계와 정합. 4.5 채택 가능.
   (b) 시간청산으로 이동 → 아무 판단 없이 **시계로** 잘렸다. 설계 훼손 확정. 3.5 유지.

 콜백을 넓히면 TS가 늦게 걸리므로, 그 사이 다른 청산이 먼저 잡는다. 그 '다른 청산'의
 정체가 결론을 가른다.

[판정 기준 — 사전 고정]
   TS 총이익 기여도 하락분의 **절반 이상이 시간청산으로** 가면 훼손(3.5 유지),
   점수하락이 흡수하면 정합(4.5 채택 검토). 어느 쪽도 아니면(손절/BEP 등으로 분산)
   콜백이 손실 구간까지 늘어진 것이므로 훼손으로 본다.

[무엇을 보는가] 건수 비중이 아니라 **이익금 기여**다. 건수는 소액 거래에 휘둘린다.
   · 사유별 총이익 기여% — 양(+)의 실현손익 합 대비. '주청산' 여부를 정하는 값.
   · 사유별 순손익 기여% — 손실까지 포함. 콜백이 손실 구간을 물고 있는지 본다.
   · TS 1건당 이익금 — 건수가 줄어도 건당이 커지면 절대 TS이익은 유지된다.
   · 사유별 중앙 보유일 — 시간청산으로의 이동은 보유일 상한에 붙는 형태로 나타난다.

[선행] 없음(종가 세계). --worlds 에 장중을 넣으면 분봉 게이트가 걸려 창이 좁아진다.
[실행] python3 tools/audit_exit_reason_mix.py --days 3650 --trials 15 --sample 25 --seeds 3
"""
import argparse
import os
import random
import sys
from collections import defaultdict

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tools.audit_common import exits, seed_notice  # noqa: E402

import config  # noqa: E402
from modules import intraday_bars as ib  # noqa: E402
from modules import portfolio_backtest as pb  # noqa: E402

INITIAL_CAPITAL = 10_000_000
# 표시 순서 — 판단 기반(TS·점수하락)을 앞에, 무판단(시간청산)·방어(손절)를 뒤에 둔다.
ORDER = ("트레일링스탑", "점수하락", "시간청산", "ATR손절", "손절", "본전청산", "이익보호")
SHORT = {"트레일링스탑": "TS", "점수하락": "점수하락", "시간청산": "시간청산",
         "ATR손절": "ATR손절", "손절": "손절", "본전청산": "본전", "이익보호": "이익보호"}
MULTS = (3.0, 3.5, 4.0, 4.5, 5.0)
BASE = 3.5


def decompose(r):
    """1회 실행 → 사유별 기여도. 비중은 실행 내부에서 정규화한다(실행 간 규모 차이 제거)."""
    sells = exits(r)
    gross_gain = sum(t["profit_amt"] for t in sells if t["profit_amt"] > 0)
    net_all = sum(t["profit_amt"] for t in sells)
    by = defaultdict(list)
    for t in sells:
        by[t["reason"]].append(t)
    out = {"n": len(sells), "gross_gain": gross_gain, "net": net_all,
           "ret": r["total_return"], "mdd": r["mdd"]}
    for reason in ORDER:
        ts_ = by.get(reason, [])
        gain = sum(t["profit_amt"] for t in ts_ if t["profit_amt"] > 0)
        net = sum(t["profit_amt"] for t in ts_)
        out[f"cnt::{reason}"] = len(ts_)
        out[f"cntsh::{reason}"] = len(ts_) / len(sells) * 100 if sells else 0.0
        out[f"gainsh::{reason}"] = gain / gross_gain * 100 if gross_gain > 0 else 0.0
        out[f"netsh::{reason}"] = net / net_all * 100 if net_all else 0.0
        out[f"per::{reason}"] = (net / len(ts_) / 1e4) if ts_ else 0.0     # 만원/건
        out[f"days::{reason}"] = float(np.median([t["days"] for t in ts_])) if ts_ else 0.0
        out[f"rate::{reason}"] = float(np.median([t["profit"] for t in ts_])) if ts_ else 0.0
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--trials", type=int, default=15)
    ap.add_argument("--sample", type=int, default=25)
    ap.add_argument("--days", type=int, default=3650)
    ap.add_argument("--seeds", type=int, default=3)
    ap.add_argument("--seed", type=int, default=20260816)
    ap.add_argument("--slots", type=int, default=None)
    ap.add_argument("--seed-capital", type=int, default=INITIAL_CAPITAL)
    ap.add_argument("--worlds", default="종가", help="종가 | 장중 | 종가,장중")
    ap.add_argument("--interval", default="60m")
    ap.add_argument("--min-coverage", type=float, default=0.9)
    ap.add_argument("--subperiods", type=int, default=3)
    ap.add_argument("--exclude-from", default="20260301")
    args = ap.parse_args()
    seed_notice(args.seeds, example="--seeds 3")

    worlds = [w for w in args.worlds.split(",") if w]
    slots = args.slots or getattr(config, "SYSTEM_MAX_HOLDINGS", 4)
    config.session.load_stock_config()
    stocks = config.session.stock_data.get("stocks_kr", [])
    names = {s["code"]: s["name"] for s in stocks}
    print(f"[준비] 관심종목 {len(stocks)}개 · {args.days}일 · 슬롯 {slots} · 세계 {'/'.join(worlds)}")

    dfs, mf, dates, _f = pb.prepare_universe([(s["code"], s["name"]) for s in stocks], args.days)
    # [함정 주의] 분봉 게이트는 장중 팔이 있을 때만 건다. 종가 전용인데도 걸면 창이 잘린다.
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
    # [오염 주의] make_scale_fn 콜러블은 실행마다 새로 만든다(에쿼티 곡선 재사용 금지).
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
    saved = config.SELL_STRATEGY["TRAILING_ATR_MULTIPLIER"]
    all_results = {}
    total = len(windows) * args.seeds * args.trials * len(MULTS) * len(worlds)
    done = 0
    try:
        for wname, wdates in windows:
            res = {(w, m): [] for w in worlds for m in MULTS}
            for si in range(args.seeds):
                rng = random.Random(args.seed + si * 1009)
                for _t in range(args.trials):
                    pick = rng.sample(codes, min(args.sample, len(codes)))
                    sd = {c: dfs[c] for c in pick}
                    sc = {c: status[c] for c in pick}
                    sm = {c: mf.get(c, set()) for c in pick}
                    for w in worlds:
                        kw = ({"intraday_bars": {c: bars[c] for c in pick},
                               "intraday_status": {c: st[c] for c in pick},
                               "intraday_pyramid": False} if w == "장중" else {})
                        for m in MULTS:
                            config.SELL_STRATEGY["TRAILING_ATR_MULTIPLIER"] = m
                            r = pb.run_portfolio(sd, sc, wdates,
                                                 initial_capital=args.seed_capital,
                                                 slots=slots, market_filter_dates=sm,
                                                 risk_scale_by_date=new_scale_fn(), **kw)
                            res[(w, m)].append(decompose(r))
                            done += 1
                    print(f"  {wname} 씨드{si + 1} {done}/{total}", end="\r", flush=True)
            all_results[wname] = res
    finally:
        config.SELL_STRATEGY["TRAILING_ATR_MULTIPLIER"] = saved
    print(" " * 60, end="\r")

    W = 108
    med = lambda rs, key: float(np.median([x[key] for x in rs]))  # noqa: E731

    print(f"\n{'=' * W}")
    print(f"청산사유별 자금 이동 — {args.trials}회 × {args.sample}종목 × 씨드 {args.seeds}개 "
          f"(기준선 {BASE})")
    print(f"{'=' * W}")

    for wname, wdates in windows:
        res = all_results[wname]
        print(f"\n########## {wname} ({len(wdates)} 거래일) ##########")
        for w in worlds:
            print(f"\n[{w} 체결] ── 사유별 **총이익 기여%** (양의 실현손익 합 대비)")
            hdr = f"{'배수':<7}" + "".join(f"{SHORT[r]:>10}" for r in ORDER) + f"{'수익%':>9}{'MDD%':>8}"
            print(hdr)
            print("-" * len(hdr))
            for m in MULTS:
                rs = res[(w, m)]
                tag = f"{m}" + ("*" if m == BASE else " ")
                line = f"{tag:<7}" + "".join(f"{med(rs, f'gainsh::{r}'):>10.1f}" for r in ORDER)
                line += f"{med(rs, 'ret'):>9.1f}{med(rs, 'mdd'):>8.1f}"
                print(line)

            print(f"\n[{w} 체결] ── 사유별 건수 (건 / 전체 대비 %)")
            print(f"{'배수':<7}" + "".join(f"{SHORT[r]:>12}" for r in ORDER) + f"{'총건수':>8}")
            for m in MULTS:
                rs = res[(w, m)]
                tag = f"{m}" + ("*" if m == BASE else " ")
                line = f"{tag:<7}"
                for r in ORDER:
                    line += f"{med(rs, f'cnt::{r}'):>6.0f}({med(rs, f'cntsh::{r}'):>4.1f}%)"
                line += f"{med(rs, 'n'):>8.0f}"
                print(line)

            print(f"\n[{w} 체결] ── 기준선({BASE}) 대비 총이익 기여%p 이동")
            print(f"{'배수':<7}" + "".join(f"{SHORT[r]:>10}" for r in ORDER)
                  + f"{'TS건당만원':>12}{'TS중앙일':>9}")
            base_rs = res[(w, BASE)]
            for m in MULTS:
                if m == BASE:
                    continue
                rs = res[(w, m)]
                deltas = {r: med(rs, f'gainsh::{r}') - med(base_rs, f'gainsh::{r}') for r in ORDER}
                line = f"{m:<7}" + "".join(f"{deltas[r]:>+10.1f}" for r in ORDER)
                line += f"{med(rs, 'per::트레일링스탑'):>12.0f}{med(rs, 'days::트레일링스탑'):>9.1f}"
                print(line)
                # 판정 — TS 하락분을 누가 흡수했는가
                drop = -deltas["트레일링스탑"]
                if drop > 0.5:
                    absorb = sorted(((deltas[r], r) for r in ORDER if r != "트레일링스탑"),
                                    reverse=True)
                    top = ", ".join(f"{SHORT[r]} {d:+.1f}%p" for d, r in absorb[:3] if d > 0.1)
                    ts_share = deltas["시간청산"] / drop * 100
                    sc_share = deltas["점수하락"] / drop * 100
                    verdict = ("훼손(시간청산 흡수)" if ts_share >= 50 else
                               "정합(점수하락 흡수)" if sc_share >= 50 else "판정보류(분산)")
                    print(f"       └ TS -{drop:.1f}%p 이동 → {top}"
                          f"  | 시간청산 {ts_share:.0f}% · 점수하락 {sc_share:.0f}% → {verdict}")
                else:
                    print(f"       └ TS 기여도 하락 없음({-drop:+.1f}%p) — 설계 훼손 근거 없음")

            print(f"\n[{w} 체결] ── 사유별 중앙 보유일 / 중앙 수익률%")
            print(f"{'배수':<7}" + "".join(f"{SHORT[r]:>14}" for r in ORDER))
            for m in MULTS:
                rs = res[(w, m)]
                tag = f"{m}" + ("*" if m == BASE else " ")
                line = f"{tag:<7}"
                for r in ORDER:
                    line += f"{med(rs, f'days::{r}'):>6.0f}일{med(rs, f'rate::{r}'):>+7.1f}%"
                print(line)

    print("\n" + "-" * W)
    print("[판정 기준] TS 총이익 기여도 하락분의 50%↑가 시간청산으로 → 설계 훼손(3.5 유지).")
    print("            점수하락이 흡수 → 추세 종료를 판단해 나간 것이므로 정합(4.5 검토).")
    print("[읽는 법] '주청산'은 건수가 아니라 이익 기여로 정한다. 건수는 소액 거래에 휘둘린다.")


if __name__ == "__main__":
    main()
