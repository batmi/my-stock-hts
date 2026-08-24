"""유니버스 — 몇 종목을 보고 있어야 하는가, 그리고 생존 편향은 결론을 바꾸는가.

[축 A · 크기] 슬롯은 4개인데 관심종목은 44개다. 이 숫자가 정해진 근거는 없다.
 메모리에 "시드는 성과가 아니라 커버리지 문제"라고 적혀 있는데 정작 커버리지 자체를
 잰 적이 없다. 표본 크기를 바꿔 재면 '종목을 더 넣으면 슬롯 경쟁이 좋아지는가, 아니면
 잡음만 느는가'가 나온다. 이건 사용자가 곧바로 실행할 수 있는 결론이다(관심종목 추가).
 ※ 유니버스가 커질수록 종목당 데이터가 같으니 총 자본은 고정한다 — 슬롯 4개 경쟁만 바뀐다.

[축 B · 생존 편향] 모든 감사가 **현재 상장된** 44종목에서 표본을 뽑는다. 10년 백테스트인데
 10년 전에는 이 44개를 고를 수 없었다. 절대 수익률이 부풀 뿐 아니라 **다이얼 결론의 방향까지
 바뀔 수 있다** — 손절·시간청산이 '결국 살아남은 종목'에만 맞춰 느슨하게 튜닝됐을 수 있다.
 FDR의 상장폐지 목록(KRX-DELISTING)으로 2016년 이후 폐지된 주권을 섞어 다시 잰다.
 폐지 종목의 일봉은 프로젝트 데이터 경로(pykrx/FDR)로 그대로 조회된다(확인 완료).

 [무엇이 공정한 비교인가] 같은 표본 크기에서 '현행 풀'과 '폐지 포함 풀'을 비교한다.
 절대 성과 차이 = 생존 프리미엄. 여기에 손절·시간청산 다이얼을 함께 흔들어, 폐지 종목이
 섞이면 **최적값이 옮겨가는지**를 본다. 옮겨가지 않으면 기존 결론들은 안전하다.

[실행] python3 tools/audit_universe.py --axis A --days 3650 --trials 15 --seeds 3
       python3 tools/audit_universe.py --axis B --days 3650 --trials 15 --seeds 3 --dead 40
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
# 축 B에서 함께 흔들 다이얼 — 생존 편향이 '느슨한 튜닝'을 만들었다면 여기서 드러난다.
DIALS = [
    ("현행", []),
    ("ATR손절 1.5", [("sell", "ATR_STOP_MULTIPLIER", 1.5)]),
    ("시간청산 10일", [("sell", "TIME_STOP_DAYS", 10)]),
    ("콜백 2.5", [("sell", "TRAILING_ATR_MULTIPLIER", 2.5)]),
]


def apply(ov):
    prev = []
    for tgt, key, val in ov:
        d = config.SELL_STRATEGY if tgt == "sell" else config.ANALYSIS_THRESHOLDS
        prev.append((tgt, key, d.get(key)))
        d[key] = val
    return prev


def _listing(kind):
    """FDR 종목 목록을 디스크에 캐시한다 — 감사 도구가 매번 원격을 두드리면 429로 막힌다.

    2026-08-17에 `StockListing("KRX")`가 HTTP 429로 실패해 감사가 통째로 죽었다. 목록은
    하루 단위로도 거의 안 변하는 데다, 팔 사이 비교의 **배경**일 뿐이어서 최신성이
    결론에 영향을 주지 않는다. 그러니 받으면 남기고, 못 받으면 남은 것을 쓴다.
    캐시조차 없으면 조용히 빈 결과를 주지 않고 그대로 터뜨린다 — 표본이 없으면 없다고
    말하는 것이 규약이다.
    """
    import os
    import pandas as pd
    d = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                     "data", "listing_cache")
    os.makedirs(d, exist_ok=True)
    f = os.path.join(d, kind.replace("/", "_") + ".csv")
    try:
        import FinanceDataReader as fdr
        df = fdr.StockListing(kind)
        if df is not None and len(df):
            df.to_csv(f, index=False, encoding="utf-8")
            return df
        raise RuntimeError("빈 목록")
    except Exception as e:
        if os.path.exists(f):
            print(f"[캐시] {kind} 원격 실패({type(e).__name__}) → 캐시 사용: {f}", flush=True)
            return pd.read_csv(f, dtype={"Code": str, "Symbol": str})
        raise


def extend_targets(exclude, limit, mode="marcap", pool=500, seed=20260816):
    """관심종목에 없는 종목으로 풀을 넓힌다. '44개를 넘기면 나아지는가'를 재려면 필요하다.

    우선주·스팩·리츠는 뺀다 — 추세추종 대상이 아니고 유동성 성격도 다르다.

    [mode='marcap'] **현재** 시총 상위에서 뽑는다. 편하지만 생존 편향이 이 축에서 최대로
      작동한다 — 지금 시총이 큰 종목은 정의상 지난 10년간 크게 오른 종목이다. 이 팔만으로는
      '종목을 늘려서 좋아진 것'과 '지금 큰 종목을 과거에 심어서 좋아진 것'을 못 가른다.
    [mode='random'] 시총 상위 `pool`개 안에서 **무작위로** 뽑는다. '거래 가능성'만 통제하고
      승자 선택은 제거한다. 이쪽에서도 좋아지면 크기 효과가 진짜다.
    """
    import random as _r
    df = _listing("KRX")
    df = df[df["Market"].isin(["KOSPI", "KOSDAQ"])].dropna(subset=["Marcap"])
    bad = df["Name"].str.contains("스팩|리츠", na=False) | df["Code"].str.endswith(("5", "7", "9"))
    df = df[~bad].sort_values("Marcap", ascending=False)
    cand = [(r["Code"], r["Name"]) for _, r in df.iterrows() if r["Code"] not in exclude]
    if mode == "random":
        cand = cand[:pool]
        _r.Random(seed).shuffle(cand)
    return cand[:limit]


def dead_targets(limit, since="2016-01-01"):
    """상장폐지 주권 목록. 스팩·피흡수합병처럼 '전략과 무관한 소멸'은 뺀다."""
    import pandas as pd
    df = _listing("KRX-DELISTING")
    df["DelistingDate"] = pd.to_datetime(df["DelistingDate"], errors="coerce")
    m = (df["DelistingDate"] >= since) & (df["SecuGroup"] == "주권")
    df = df[m].copy()
    drop = df["Name"].str.contains("스팩", na=False)
    df = df[~drop]
    # 폐지 사유가 합병·완전자회사화면 주가가 급락으로 끝나지 않아 편향 측정 대상이 아니다.
    keep = ~df["Reason"].fillna("").str.contains("합병|완전자회사|해산|지주회사")
    df = df[keep]
    df = df.sort_values("DelistingDate", ascending=False).head(limit * 3)
    return [(r["Symbol"], r["Name"]) for _, r in df.iterrows()]


def metrics(r):
    sells = exits(r)
    profits = sorted((t["profit"] for t in sells), reverse=True)
    top10 = profits[:max(1, len(profits) // 10)]
    return {
        "ret": r["total_return"], "mdd": r["mdd"],
        "mar": r["total_return"] / abs(r["mdd"]) if r["mdd"] else float("nan"),
        "pf": r["pf"], "n": len(sells),
        "top10": float(np.mean(top10)) if top10 else 0.0,
        "best": profits[0] if profits else 0.0,
        "big": sum(1 for p in profits if p >= 30),
        "worst": profits[-1] if profits else 0.0,
        "loss30": sum(1 for p in profits if p <= -30),
        "days": float(np.median([t["days"] for t in sells])) if sells else 0.0,
        "cash": r.get("avg_cash_ratio", 0.0),
    }


def prep(targets, days, label):
    dfs, mf, dates, failed = pb.prepare_universe(targets, days)
    print(f"[준비] {label}: 요청 {len(targets)} → 사용 {len(dfs)}종목 (실패 {len(failed)})")
    return dfs, mf, dates


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--axis", default="A", choices=["A", "B"])
    ap.add_argument("--trials", type=int, default=15)
    ap.add_argument("--days", type=int, default=3650)
    ap.add_argument("--seeds", type=int, default=3)
    ap.add_argument("--seed", type=int, default=20260816)
    ap.add_argument("--sizes", default="10,20,30,44")
    ap.add_argument("--extend-mode", default="marcap", choices=["marcap", "random"],
                    help="확장 종목 선정: marcap=현재 시총 상위(생존 편향 최대) / random=상위 pool 내 무작위")
    ap.add_argument("--extend-pool", type=int, default=500)
    ap.add_argument("--extend", type=int, default=0,
                    help="축 A: 시총 상위에서 관심종목에 없는 종목을 이만큼 추가해 44개 너머를 잰다")
    ap.add_argument("--dead", type=int, default=40, help="축 B: 섞을 폐지 종목 수")
    ap.add_argument("--dead-frac", type=float, default=0.0,
                    help="축 A: 표본을 이 비율만큼 폐지 종목으로 채운다. 10년 실제 폐지율은 "
                         "약 20%%(563/2700)다 — 과거에 고른 관심종목이라면 그만큼 사라졌다")
    ap.add_argument("--sample", type=int, default=25, help="축 B: 고정 표본 크기")
    ap.add_argument("--slots", type=int, default=None)
    ap.add_argument("--seed-capital", type=int, default=INITIAL_CAPITAL)
    ap.add_argument("--subperiods", type=int, default=3)
    ap.add_argument("--exclude-from", default="20260301")
    args = ap.parse_args()
    seed_notice(args.seeds, example="--seeds 3")

    slots = args.slots or getattr(config, "SYSTEM_MAX_HOLDINGS", 4)
    config.session.load_stock_config()
    live = [(s["code"], s["name"]) for s in config.session.stock_data.get("stocks_kr", [])]
    print(f"[준비] 축 {args.axis} · 관심종목 {len(live)}개 · {args.days}일 · 슬롯 {slots}")

    dfs, mf, dates = prep(live, args.days, "현행 풀")
    ext_dfs = {}
    if args.axis == "A" and args.extend > 0:
        et = extend_targets({c for c, _n in live}, args.extend,
                            mode=args.extend_mode, pool=args.extend_pool, seed=args.seed)
        tag = "시총 상위" if args.extend_mode == "marcap" else f"상위{args.extend_pool} 내 무작위"
        ext_dfs, ext_mf, _ed = prep(et, args.days, f"확장 풀({tag} {len(et)})")
        mf.update({c: ext_mf.get(c, set()) for c in ext_dfs})
    dead_dfs = {}
    if args.axis == "A" and args.dead_frac > 0:
        # [크기 축의 진짜 반례] 확장 풀도 '오늘까지 살아남은' 종목이라, 종목을 늘린 효과에
        #  생존 편향이 그대로 얹힌다. 표본을 실제 폐지율만큼 폐지 종목으로 채워, 과거에
        #  실제로 고를 수 있었던 관심종목에 가깝게 만든 뒤 같은 기울기가 남는지 본다.
        # 필요한 폐지 종목 수는 '가장 큰 표본 × 폐지 비율'이다. 100으로 고정해 두면
        # 100종목 너머를 잴 때 폐지 풀이 모자라 혼합 비율이 조용히 낮아진다.
        _smax = max([int(x) for x in args.sizes.split(",") if x] or [100])
        need = int(max(_smax, 100) * args.dead_frac) + 10
        dt = dead_targets(need)
        dead_dfs, dead_mf, _dd = prep(dt, args.days, f"폐지 풀(요청 {len(dt)})")
        mf.update({c: dead_mf.get(c, set()) for c in dead_dfs})
    if args.axis == "B":
        dt = dead_targets(args.dead)
        dead_dfs, dead_mf, _dd = prep(dt, args.days, f"폐지 풀(요청 {len(dt)})")
        # 폐지 종목은 창 끝까지 데이터가 없다 — 그게 이 감사의 요점이다.
        keep = list(dead_dfs)[:args.dead]
        dead_dfs = {c: dead_dfs[c] for c in keep}
        mf.update({c: dead_mf.get(c, set()) for c in keep})
        print(f"[준비] 폐지 풀 확정 {len(dead_dfs)}종목")

    thresholds = {
        "BUY_SCORE": config.ANALYSIS_THRESHOLDS["BUY_SCORE"],
        "BUY_RSI_MAX": config.ANALYSIS_THRESHOLDS["BUY_RSI_MAX"],
        "RISE_SCORE": config.ANALYSIS_THRESHOLDS["RISE_SCORE"],
        "WEIGHTS": config.SCORING_WEIGHTS,
    }
    allf = dict(dfs); allf.update(dead_dfs); allf.update(ext_dfs)
    status = pb.precompute_status(allf, thresholds)
    print(f"[준비] 거래일 {len(dates)}일 ({dates[0]}~{dates[-1]})")

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

    live_codes = list(dfs)
    dead_codes = list(dead_dfs)
    ext_codes = list(ext_dfs)
    pool_a = live_codes + ext_codes
    if args.axis == "A":
        sizes = [int(x) for x in args.sizes.split(",") if x]
        sizes = [s for s in sizes if s <= len(pool_a)]
        arms = []
        for s in sizes:
            tag = " (현행)" if s == len(live_codes) else (" +확장" if s > len(live_codes) else "")
            arms.append((f"{s}종목{tag}", s, []))
        base_label = next((l for l, s, _ in arms if s == len(live_codes)), arms[-1][0])
    else:
        arms = [(f"현행풀·{lbl}", args.sample, ov) for lbl, ov in DIALS]
        arms += [(f"폐지포함·{lbl}", args.sample, ov) for lbl, ov in DIALS]
        base_label = "현행풀·현행"

    all_results = {}
    total = len(windows) * args.seeds * args.trials * len(arms)
    done = 0
    for wname, wdates in windows:
        res = {lbl: [] for lbl, _s, _o in arms}
        for si in range(args.seeds):
            rng = random.Random(args.seed + si * 1009)
            for _t in range(args.trials):
                # 같은 시행 안에서는 같은 난수열을 쓰되, 팔마다 필요한 만큼 뽑는다.
                seedv = rng.random()
                for lbl, n, ov in arms:
                    r2 = random.Random(int(seedv * 1e9) + n)
                    pool = pool_a if args.axis == "A" else live_codes
                    if args.axis == "B" and lbl.startswith("폐지포함"):
                        pool = live_codes + dead_codes
                    if args.axis == "A" and args.dead_frac > 0 and dead_codes:
                        nd = min(int(round(n * args.dead_frac)), len(dead_codes))
                        pick = (r2.sample(dead_codes, nd)
                                + r2.sample(pool, min(n - nd, len(pool))))
                    else:
                        pick = r2.sample(pool, min(n, len(pool)))
                    sd = {c: allf[c] for c in pick}
                    sc = {c: status[c] for c in pick}
                    sm = {c: mf.get(c, set()) for c in pick}
                    prev = apply(ov)
                    try:
                        r = pb.run_portfolio(sd, sc, wdates,
                                             initial_capital=args.seed_capital, slots=slots,
                                             market_filter_dates=sm,
                                             risk_scale_by_date=new_scale_fn())
                    finally:
                        apply(prev)
                    res[lbl].append(metrics(r))
                    done += 1
                print(f"  {wname} 씨드{si + 1} {done}/{total}", end="\r", flush=True)
        all_results[wname] = res
    print(" " * 60, end="\r")

    W = 118
    title = ("유니버스 크기 — 몇 종목을 보고 있어야 하는가"
             if args.axis == "A" else
             f"생존 편향 — 폐지 {len(dead_codes)}종목을 섞으면 결론이 바뀌는가")
    print(f"\n{'=' * W}\n{title} — {args.trials}회 × 씨드 {args.seeds}개 (기준선 {base_label})\n{'=' * W}")
    for wname, wdates in windows:
        res = all_results[wname]
        base = res[base_label]
        print(f"\n########## {wname} ({len(wdates)} 거래일) ##########")
        print(f"{'팔':<18}{'수익%':>9}{'MDD%':>8}{'MAR':>7}{'PF':>6}{'청산':>6}{'상위10%':>9}"
              f"{'최대':>9}{'>30%':>6}{'최악%':>8}{'≤-30%':>7}{'현금%':>7}{'보유일':>7}"
              f"{'승-무-패':>10}{'MAR승':>7}")
        print("-" * W)
        for lbl, _n, _o in arms:
            rs = res[lbl]
            m = lambda k: float(np.median([x[k] for x in rs]))  # noqa: E731
            is_base = lbl == base_label
            tie = sum(1 for a, b in zip(rs, base) if abs(a["ret"] - b["ret"]) < 1e-9)
            los = sum(1 for a, b in zip(rs, base) if a["ret"] < b["ret"] - 1e-9)
            rw = sum(1 for a, b in zip(rs, base) if a["ret"] > b["ret"])
            mw = sum(1 for a, b in zip(rs, base) if a["mar"] > b["mar"])
            print(f"{lbl:<18}{m('ret'):>9.1f}{m('mdd'):>8.1f}{m('mar'):>7.2f}{m('pf'):>6.2f}"
                  f"{m('n'):>6.0f}{m('top10'):>9.1f}{m('best'):>9.1f}{m('big'):>6.0f}"
                  f"{m('worst'):>8.1f}{m('loss30'):>7.0f}{m('cash'):>7.1f}{m('days'):>7.1f}"
                  f"{'—' if is_base else f'{rw}-{tie}-{los}':>10}"
                  f"{'—' if is_base else f'{mw}/{len(rs)}':>7}")

    print("\n" + "-" * W)
    if args.axis == "A":
        print("[읽는 법] 종목을 늘려도 성과가 그대로면 커버리지는 이미 충분하다. 늘수록 좋아지면 관심종목을 늘려라.")
        print("          현금% 가 함께 내려가는지 볼 것 — 슬롯을 못 채우는 것이 진짜 병목이면 여기서 드러난다.")
    else:
        print("[읽는 법] 절대 성과 차이 = 생존 프리미엄. 다이얼 순위가 두 풀에서 같으면 기존 결론은 안전하다.")
        print("[한계] 폐지 종목은 창 중간에 데이터가 끝난다. 그 종목이 뽑힌 시행은 실질 유니버스가 작아진다.")


if __name__ == "__main__":
    main()
