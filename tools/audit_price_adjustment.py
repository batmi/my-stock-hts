"""수정주가 규약 — Open API(원주가 + 분할·무상증자만 보정) vs FDR(완전 수정주가)이 결론을 바꾸는가.

[왜 · 2026-09-18] 국내 일봉 정본을 KRX Open API 로 옮겼다. Open API 일별매매는 **그 날의
원주가**라 `krx_openapi.adjust_splits` 가 액면분할·무상증자(상장주식수 배수 ≥1.5 + 종가/시가
갭 15% 안)만 되감는다. **유상증자 권리락·배당락은 보정하지 않는다** — 그날 하루 1~2% 계단이
남고, EMA·52주 위치·ATR 이 그만큼 어긋난다(실측 에코프로비엠 2026-06 권리락 1.6%).
FDR(네이버)은 전부 보정된 수정주가다. 그러면 '알려진 한계'가 실제로 얼마나 큰가?

[무엇을 재는가]
 1. **계단의 수와 크기**: 같은 종목의 두 시계열을 날짜로 맞춰 일간 수익률 차이가 0.5% 를
    넘는 날을 센다(분할 보정이 맞으면 분할일은 여기 안 잡힌다 — 잡히면 보정 결함이다).
 2. **판정 차이**: 같은 표본·같은 씨드·같은 다이얼로 포트폴리오 백테스트를 두 데이터로
    돌려 수익·MDD·MAR·청산 수·꼬리를 비교한다. 데이터만 다르고 나머지는 전부 같다.

[읽는 법] 2.의 차이가 씨드 간 잡음(같은 데이터·다른 씨드) 안이면 '무해'로 종결한다.
 씨드 잡음을 넘고 방향이 일정하면 권리락 보정(FDR 종가비 팩터)을 붙일 근거다.
 ※ 09-14 이후 FDR 종가는 애프터 최종가라 마지막 며칠은 규약 차이가 하나 더 섞인다 —
   창 끝을 09-13 이전으로 두면 그 오염이 빠진다(--end 20260913 기본).

[실행] python3 tools/audit_price_adjustment.py --days 3650 --trials 15 --seeds 3
"""
import argparse
import contextlib
import os
import random
import sys

import numpy as np
import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tools.audit_common import base_metrics, seed_notice, windows  # noqa: E402

import config  # noqa: E402
from modules import krx_daily  # noqa: E402
from modules import portfolio_backtest as pb  # noqa: E402

INITIAL_CAPITAL = 10_000_000
STEP_PCT = 0.5          # 일간 수익률 차이가 이보다 크면 '계단'


@contextlib.contextmanager
def data_source(kind):
    """'OPENAPI' 또는 'FDR' 로 krx_daily.get_daily 의 소스를 고정한다(메모리 캐시도 비운다)."""
    orig = krx_daily._fetch_openapi
    if kind == "FDR":
        krx_daily._fetch_openapi = lambda code, lookback_days: None
    krx_daily.clear_cache()
    try:
        yield
    finally:
        krx_daily._fetch_openapi = orig
        krx_daily.clear_cache()


def steps(df_a, df_b, end=None):
    """두 시계열의 일간 수익률 차이가 STEP_PCT 를 넘는 날 [(날짜, 차이%)]. end 이후는 안 본다."""
    a = df_a.set_index("date")["close"].astype(float)
    b = df_b.set_index("date")["close"].astype(float)
    idx = a.index.intersection(b.index)
    if end:
        idx = idx[idx <= end]
    if len(idx) < 3:
        return []
    ra = a.loc[idx].pct_change() * 100
    rb = b.loc[idx].pct_change() * 100
    d = (ra - rb).dropna()
    return [(str(k), float(v)) for k, v in d.items() if abs(v) > STEP_PCT]


def prep(targets, days, label, kind, end):
    with data_source(kind):
        dfs, mf, dates, failed = pb.prepare_universe(targets, days)
    if end:
        dates = [d for d in dates if d <= end]
    src = set()
    for df in dfs.values():
        src.add(str(df.attrs.get("source", "?")))
    print(f"[준비] {label}: 요청 {len(targets)} → 사용 {len(dfs)} (실패 {len(failed)}) · 출처 {sorted(src)}")
    return dfs, mf, dates


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=3650)
    ap.add_argument("--trials", type=int, default=15)
    ap.add_argument("--seeds", type=int, default=3)
    ap.add_argument("--seed", type=int, default=20260816)
    ap.add_argument("--sample", type=int, default=25)
    ap.add_argument("--subperiods", type=int, default=3)
    ap.add_argument("--end", default="20260913", help="창 끝(애프터마켓 종가 오염 전). 0 이면 끝까지")
    ap.add_argument("--slots", type=int, default=None)
    args = ap.parse_args()
    seed_notice(args.seeds, example="--seeds 3")
    end = args.end if args.end and args.end != "0" else None

    slots = args.slots or getattr(config, "SYSTEM_MAX_HOLDINGS", 4)
    config.session.load_stock_config()
    live = [(s["code"], s["name"]) for s in config.session.stock_data.get("stocks_kr", [])]
    print(f"[준비] 관심종목 {len(live)}개 · {args.days}일 · 슬롯 {slots} · 창 끝 {end or '끝까지'}")

    dfs_oa, mf_oa, dates = prep(live, args.days, "Open API(원주가+분할보정)", "OPENAPI", end)
    dfs_fd, mf_fd, dates_fd = prep(live, args.days, "FDR(수정주가)", "FDR", end)
    codes = [c for c in dfs_oa if c in dfs_fd]
    names = dict(live)

    # ── 1. 계단 ───────────────────────────────────────────
    print(f"\n{'=' * 100}\n1. 두 시계열의 계단(일간 수익률 차이 > {STEP_PCT}%, {end or '끝'}까지) — 분할일은 잡히면 안 된다\n{'=' * 100}")
    total, rows = 0, []
    for c in codes:
        st = steps(dfs_oa[c], dfs_fd[c], end)
        total += len(st)
        if st:
            rows.append((c, names.get(c, c), st))
    print(f"종목 {len(codes)} · 계단 {total}건 · 계단 있는 종목 {len(rows)}")
    for c, n, st in sorted(rows, key=lambda r: -len(r[2]))[:15]:
        big = sorted(st, key=lambda t: -abs(t[1]))[:3]
        print(f"  {n:<14}{c} {len(st):>3}건  최대 " + " · ".join(f"{d} {v:+.1f}%" for d, v in big))
    if total:
        mags = [abs(v) for _c, _n, st in rows for _d, v in st]
        print(f"계단 크기 중앙 {np.median(mags):.2f}% · 평균 {np.mean(mags):.2f}% · 최대 {max(mags):.1f}%")

    # ── 2. 백테스트 ───────────────────────────────────────
    thresholds = {
        "BUY_SCORE": config.ANALYSIS_THRESHOLDS["BUY_SCORE"],
        "BUY_RSI_MAX": config.ANALYSIS_THRESHOLDS["BUY_RSI_MAX"],
        "RISE_SCORE": config.ANALYSIS_THRESHOLDS["RISE_SCORE"],
        "WEIGHTS": config.SCORING_WEIGHTS,
    }
    st_oa = pb.precompute_status({c: dfs_oa[c] for c in codes}, thresholds)
    st_fd = pb.precompute_status({c: dfs_fd[c] for c in codes}, thresholds)

    from tools.audit_drawdown_axis import market_scale_by_date, make_scale_fn
    p = getattr(config, "RISK_SCALING_PARAMS", {}) or {}
    mkt = market_scale_by_date(dates, args.days)
    dd = None
    if p.get("USE_DRAWDOWN_RISK_SCALING", True):
        dd = (int(p.get("DD_LOOKBACK_DAYS", 90)), float(p.get("DD_LEVEL_1", 5.0)),
              float(p.get("DD_SCALE_1", 0.9)), float(p.get("DD_LEVEL_2", 10.0)),
              float(p.get("DD_SCALE_2", 0.8)))

    arms = [("Open API", dfs_oa, st_oa, mf_oa), ("FDR", dfs_fd, st_fd, mf_fd)]
    wins = windows(dates, max(1, args.subperiods), whole=True)
    results = {w: {a[0]: [] for a in arms} for w, _ in wins}
    # 씨드 잡음의 기준: 같은 데이터(Open API)로 다른 표본 씨드 — 두 데이터 차이가 이 안이면 잡음이다
    results_noise = {w: [] for w, _ in wins}
    total_runs = len(wins) * args.seeds * args.trials * (len(arms) + 1)
    done = 0
    for wname, wdates in wins:
        for si in range(args.seeds):
            rng = random.Random(args.seed + si * 1009)
            for _t in range(args.trials):
                seedv = rng.random()
                r2 = random.Random(int(seedv * 1e9))
                pick = r2.sample(codes, min(args.sample, len(codes)))
                for label, dfs, status, mf in arms:
                    r = pb.run_portfolio({c: dfs[c] for c in pick}, {c: status[c] for c in pick}, wdates,
                                         initial_capital=INITIAL_CAPITAL, slots=slots,
                                         market_filter_dates={c: mf.get(c, set()) for c in pick},
                                         risk_scale_by_date=make_scale_fn(mkt, dd))
                    results[wname][label].append(base_metrics(r))
                    done += 1
                r3 = random.Random(int(seedv * 1e9) + 7)          # 다른 표본, 같은 데이터
                pick2 = r3.sample(codes, min(args.sample, len(codes)))
                r = pb.run_portfolio({c: dfs_oa[c] for c in pick2}, {c: st_oa[c] for c in pick2}, wdates,
                                     initial_capital=INITIAL_CAPITAL, slots=slots,
                                     market_filter_dates={c: mf_oa.get(c, set()) for c in pick2},
                                     risk_scale_by_date=make_scale_fn(mkt, dd))
                results_noise[wname].append(base_metrics(r))
                done += 1
                print(f"  {wname} 씨드{si + 1} {done}/{total_runs}", end="\r", flush=True)
    print(" " * 60, end="\r")

    W = 100
    print(f"\n{'=' * W}\n2. 같은 표본·같은 씨드로 데이터만 바꾼 백테스트 — {args.trials}회 × 씨드 {args.seeds}\n{'=' * W}")
    for wname, wdates in wins:
        res = results[wname]
        base = res["Open API"]
        print(f"\n########## {wname} ({len(wdates)} 거래일) ##########")
        print(f"{'데이터':<22}{'수익%':>9}{'MDD%':>8}{'MAR':>7}{'PF':>6}{'청산':>6}{'승률%':>7}{'상위10%':>8}{'승-무-패':>10}")
        print("-" * W)
        for label, *_ in arms:
            rs = res[label]
            m = lambda k: float(np.median([x[k] for x in rs]))  # noqa: E731
            rw = sum(1 for a, b in zip(rs, base) if a["ret"] > b["ret"] + 1e-9)
            los = sum(1 for a, b in zip(rs, base) if a["ret"] < b["ret"] - 1e-9)
            tie = len(rs) - rw - los
            print(f"{label:<22}{m('ret'):>9.1f}{m('mdd'):>8.1f}{m('mar'):>7.2f}{m('pf'):>6.2f}{m('n'):>6.0f}"
                  f"{m('win'):>7.1f}{m('top10'):>8.1f}{'—' if label == 'Open API' else f'{rw}-{tie}-{los}':>10}")
        gap = [abs(a["ret"] - b["ret"]) for a, b in zip(res["FDR"], base)]
        noise = [abs(a["ret"] - b["ret"]) for a, b in zip(results_noise[wname], base)]
        print(f"  데이터 차이 |Δ수익| 중앙 {np.median(gap):.1f}%p · 최대 {max(gap):.1f}%p   "
              f"vs  같은 데이터·다른 표본 |Δ수익| 중앙 {np.median(noise):.1f}%p")
    print("\n" + "-" * W)
    print("[읽는 법] 데이터 차이의 |Δ수익| 중앙값이 '같은 데이터·다른 표본' 잡음보다 뚜렷이 작고 승-무-패가 갈리지 않으면"
          " 권리락 미보정은 무해 — 종결. 크고 방향이 일정하면 권리락 보정 옵션을 붙일 근거다.")


if __name__ == "__main__":
    main()
