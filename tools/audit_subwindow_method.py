"""구간별 판정 방식 자체를 검증한다 — 채택 잣대의 바닥을 점검하는 감사.

[왜] 이 프로젝트의 채택 규칙은 '구간이 갈리면 채택하지 않는다'이고, 수십 개 축이 그
 규칙 하나로 보류·기각됐다. 그런데 그 '구간 수치'가 만들어지는 방식에 구조적 허점이 있다.

   지금 방식(독립 실행): 구간마다 **현금에서 새로 시작**하는 별도 백테스트를 돌린다.
     → 구간 끝에 포지션을 안고 있으면 그 평가익이 그 구간의 성적으로 잡히고,
       **다음 구간은 그 포지션 없이 새로 시작하므로 반납이 어디에도 안 잡힌다.**
     → 늦게 자르는 팔일수록 경계에서 포지션을 많이 안고 끝나 유리해진다.

 실측(2026-08-17, audit_late_exit_joint 구간1 197일):
   현행    총 +1.9%  = 실현 -0.7% + 미실현  +2.9%
   콜백4.5 총 +54.1% = 실현 -7.9% + 미실현 +62.5%
 콜백4.5가 그 구간에서 '이긴' 54%p는 전부 아직 안 판 이익이다. 평가액 기준 자체는
 틀리지 않지만(항상 만재 투자라 전체창도 미실현 56~63%), **창을 잘라 독립 실행하면
 반납이 사라진다**는 것이 문제다.

[대안] 연속 분할: 전체 창을 **한 번만** 돌리고 자산곡선을 구간별로 자른다.
   구간 수익 = eq[구간끝] / eq[구간시작] - 1,  구간 MDD = 그 조각 안의 최대 낙폭.
 경계를 넘어간 포지션의 반납은 다음 구간의 성적으로 정확히 청구된다.

[무엇을 답하나] 두 방식이 같은 판정을 내는가. 다르다면 **얼마나 자주, 어느 방향으로**
 다른가. 늦게 자르는 팔에서만 갈린다면 그 축들의 보류 근거를 다시 봐야 한다.
 반대로 판정이 대체로 같다면 지금까지의 결론은 안전하고, 이 감사는 그것을 확인해 준다.

[주의] 연속 분할이 무조건 옳은 것도 아니다 — 구간1에서 크게 번 팔은 구간2를 더 큰
 자본으로 시작하므로 %수익이 자본 규모에 따라 달라진다. 그래서 **둘을 나란히 보고
 어긋나는 지점을 찾는 것**이 이 도구의 목적이지, 한쪽으로 갈아타자는 것이 아니다.

[두 세계에서 잰다] 경계 효과는 **창이 짧을수록** 커진다. 종가 10년 세계는 구간이 790일씩이라
 경계 비중이 작지만, 문제를 처음 발견한 장중 세계는 구간이 197일뿐이고 팔도 '늦게 자르는'
 쪽이었다. 우려가 나온 세계에서 답하지 않으면 답한 것이 아니다.

[실행] python3 tools/audit_subwindow_method.py --trials 12 --sample 25
       python3 tools/audit_subwindow_method.py --world intraday --trials 15 --sample 20
"""
import argparse
import os
import random
import sys

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import config  # noqa: E402
from modules import portfolio_backtest as pb  # noqa: E402
from tools.audit_defensive_sector import (  # noqa: E402
    INITIAL_CAPITAL, new_scale_fn_factory,
)
from tools.audit_dials_intraday import apply  # noqa: E402
from tools.audit_scoring_weights import rolling_trend_quality  # noqa: E402
from tools.audit_common import seed_notice  # noqa: E402

NEG = float("-inf")


def seg_metrics(eq, lo, hi):
    """자산곡선 조각의 수익%·MDD% — 구간 시작 자산을 100으로 본다."""
    a = np.asarray(eq[lo:hi], dtype=float)
    if len(a) < 2 or a[0] <= 0:
        return 0.0, 0.0
    ret = (a[-1] / a[0] - 1.0) * 100
    peak = np.maximum.accumulate(a)
    mdd = float(np.min((a - peak) / peak * 100))
    return ret, mdd


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=3650)
    ap.add_argument("--trials", type=int, default=12)
    ap.add_argument("--sample", type=int, default=25)
    ap.add_argument("--seeds", default="20260816,7,101")
    ap.add_argument("--slots", type=int, default=None)
    ap.add_argument("--subperiods", type=int, default=3)
    ap.add_argument("--world", default="close", choices=("close", "intraday"))
    ap.add_argument("--interval", default="60m")
    ap.add_argument("--min-coverage", type=float, default=0.9)
    args = ap.parse_args()
    seed_notice(len(args.seeds.split(",")), example="--seeds 20260816,7,101")
    slots = args.slots or getattr(config, "SYSTEM_MAX_HOLDINGS", 4)
    seeds = [int(x) for x in args.seeds.split(",")]

    config.session.load_stock_config()
    live = [(s["code"], s["name"]) for s in config.session.stock_data.get("stocks_kr", [])]
    dfs, mf, dates, failed = pb.prepare_universe(live, args.days)
    thr = {"BUY_SCORE": config.ANALYSIS_THRESHOLDS["BUY_SCORE"],
           "BUY_RSI_MAX": config.ANALYSIS_THRESHOLDS["BUY_RSI_MAX"],
           "RISE_SCORE": config.ANALYSIS_THRESHOLDS["RISE_SCORE"],
           "WEIGHTS": config.SCORING_WEIGHTS}
    status = pb.precompute_status(dfs, thr)
    new_scale = new_scale_fn_factory(dates, args.days)
    base_ratio = 1.0 / slots

    lookback = int(config.INDICATOR_PARAMS.get("TREND_QUALITY_LOOKBACK", 90))
    tq_by_code = {c: rolling_trend_quality(df, lookback) for c, df in dfs.items()}
    extra = {}
    if args.world == "intraday":
        from modules import intraday_bars as ib
        bars, ist, keep, drop = ib.gate_universe(dfs, args.interval,
                                                 min_coverage=args.min_coverage)
        dfs = {c: dfs[c] for c in keep}
        mf = {c: mf.get(c, set()) for c in keep}
        status = {c: status[c] for c in keep}
        dates = ib.covered_dates(bars, dates)
        extra = {"bars": bars, "ist": ist}
        print(f"[준비] 장중 세계 — {len(dfs)}종목 · 거래일 {len(dates)} "
              f"({dates[0]}~{dates[-1]}) · 제외 {len(drop)}종목", flush=True)
    else:
        dates = dates[lookback - 1:]  # 워밍업 제외 — 순위·게이트 팔의 규약과 같게
        print(f"[준비] 종가 세계 — {len(dfs)}종목 · 거래일 {len(dates)} · 슬롯 {slots}"
              + (f" · 제외 {failed}" if failed else ""), flush=True)

    def tq(code, day):
        v = tq_by_code.get(code, {}).get(day)
        return NEG if v is None else float(v)

    def score_of(code, day):
        st = status.get(code, {}).get(day)
        return float(st[0]) if st else 0.0

    # 구간 판정이 채택을 막았던 축들만 고른다 — 방식이 바뀌면 결론이 흔들릴 후보다.
    def f_tq_score_up(day, code):
        q, s = tq(code, day), score_of(code, day)
        return base_ratio * (1.2 if (q != NEG and q >= 60 and s >= 8.0) else 1.0)

    def g_drop_neg(day, code, held):
        v = tq(code, day)
        return v != NEG and v < 0

    def rank_tq_then_score(s, c, r, d):
        return (tq(c, d), s)

    if args.world == "close":
        # 구간 판정이 채택을 막았던 축들 (종가 10년 세계)
        ARMS = [
            ("기준선 (현행)", {}, []),
            ("TQ+점수8 증액 +20%", {"invest_ratio_fn": f_tq_score_up}, []),
            ("TQ<0 진입 배제", {"entry_gate": g_drop_neg}, []),
            ("추세품질 1순위", {"rank_fn": rank_tq_then_score}, []),
        ]
    else:
        # 경계 효과를 처음 목격한 '늦게 자르는' 축들 (장중 세계, 구간 197일)
        ARMS = [
            ("기준선 (현행)", {}, []),
            ("TS 발동 3.5", {}, [("sell", "TS_ACTIVATION_ATR_MULTIPLIER", 3.5)]),
            ("TS 콜백 4.5", {}, [("sell", "TRAILING_ATR_MULTIPLIER", 4.5)]),
            ("셋 다 (3.5/4.5/+7%)", {}, [("sell", "TS_ACTIVATION_ATR_MULTIPLIER", 3.5),
                                        ("sell", "TRAILING_ATR_MULTIPLIER", 4.5),
                                        ("thr", "PYRAMIDING_PROFIT_TRIGGER", 7.0)]),
        ]

    k = max(1, args.subperiods)
    size = max(1, len(dates) // k)
    bounds = [(i * size, (i + 1) * size if i < k - 1 else len(dates)) for i in range(k)]
    W = [("전체", 0, len(dates))] + [(f"구간{i + 1}", lo, hi) for i, (lo, hi) in enumerate(bounds)]

    picks = [(sd, random.Random(sd * 31 + i).sample(list(dfs), min(args.sample, len(dfs))))
             for sd in seeds for i in range(args.trials)]

    # A안(독립 실행)과 B안(연속 분할)을 **같은 표본·같은 팔**로 동시에 만든다.
    resA = {lbl: {w[0]: [] for w in W} for lbl, _kw, _ov in ARMS}
    resB = {lbl: {w[0]: [] for w in W} for lbl, _kw, _ov in ARMS}
    for pi, (_sd, pick) in enumerate(picks):
        sub = dict(dfs=({c: dfs[c] for c in pick}), st=({c: status[c] for c in pick}),
                   mf=({c: mf.get(c, set()) for c in pick}))
        if extra:
            sub["bars"] = {c: extra["bars"][c] for c in pick}
            sub["ist"] = {c: extra["ist"][c] for c in pick}

        def _run(wdates, kw):
            iw = ({"intraday_bars": sub["bars"], "intraday_status": sub["ist"]}
                  if extra else {})
            return pb.run_portfolio(sub["dfs"], sub["st"], wdates,
                                    initial_capital=INITIAL_CAPITAL, slots=slots,
                                    market_filter_dates=sub["mf"],
                                    risk_scale_by_date=new_scale(), **iw, **kw)

        for lbl, kw, ov in ARMS:
            prev = apply(ov) if ov else None
            try:
                # B안: 전체 창을 한 번만 돌리고 자산곡선을 자른다
                r = _run(dates, kw)
                eq = r["equity"]
                for wn, lo, hi in W:
                    ret, mdd = seg_metrics(eq, lo, hi)
                    resB[lbl][wn].append({"ret": ret, "mdd": mdd})
                resA[lbl]["전체"].append({"ret": r["total_return"], "mdd": r["mdd"]})
                # A안: 구간마다 현금에서 새로 시작
                for wn, lo, hi in W[1:]:
                    r2 = _run(dates[lo:hi], kw)
                    resA[lbl][wn].append({"ret": r2["total_return"], "mdd": r2["mdd"]})
            finally:
                if prev:
                    apply(prev)
        print(f"  {pi + 1}/{len(picks)}", end="\r", flush=True)
    print(" " * 30, end="\r")

    print(f"\n[방식 대조] 표본 {args.sample} · {len(picks)}쌍 "
          f"· A안=구간마다 현금에서 새로 시작(현행) · B안=전체 1회 실행 후 자산곡선 분할")
    for wn, _lo, _hi in W:
        print(f"\n########## {wn} ##########")
        print(f"{'팔':<22}{'A 수익%':>9}{'A MDD':>8}{'A 승-무-패':>12}"
              f"{'B 수익%':>9}{'B MDD':>8}{'B 승-무-패':>12}{'판정':>8}")
        baseA, baseB = resA[ARMS[0][0]][wn], resB[ARMS[0][0]][wn]
        for lbl, _kw, _ov in ARMS:
            ra, rb = resA[lbl][wn], resB[lbl][wn]
            g = lambda rs, key: float(np.mean([x[key] for x in rs]))  # noqa: E731

            def wl(rs, bs):
                w = sum(1 for x, y in zip(rs, bs) if x["ret"] > y["ret"] + 1e-9)
                t = sum(1 for x, y in zip(rs, bs) if abs(x["ret"] - y["ret"]) <= 1e-9)
                return w, t, len(rs) - w - t

            if lbl == ARMS[0][0]:
                sa = sb = "—"
                verdict = ""
            else:
                wa, ta, la = wl(ra, baseA)
                wb, tb, lb = wl(rb, baseB)
                sa, sb = f"{wa}-{ta}-{la}", f"{wb}-{tb}-{lb}"
                # 판정이 뒤집혔는가 (과반 기준)
                verdict = "동일" if (wa > la) == (wb > lb) else "★뒤집힘"
            print(f"{lbl:<22}{g(ra, 'ret'):>9.1f}{g(ra, 'mdd'):>8.1f}{sa:>12}"
                  f"{g(rb, 'ret'):>9.1f}{g(rb, 'mdd'):>8.1f}{sb:>12}{verdict:>8}", flush=True)

    print("\n[읽는 법] '★뒤집힘'이 있으면 그 축의 보류·기각 근거를 다시 봐야 한다. "
          "전부 '동일'이면 지금까지의 구간 판정은 방식에 흔들리지 않는다는 뜻이다.")
    print("[주의] B안도 완벽하지 않다 — 앞 구간에서 크게 번 팔은 다음 구간을 더 큰 자본으로 "
          "시작한다. 두 방식이 어긋나는 지점을 찾는 것이 목적이다.")


if __name__ == "__main__":
    main()
