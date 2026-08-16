"""탐색 메뉴의 '적합도 점수'가 실제로 더 나은 관심종목을 고르는가.

[왜] [7-4] 탐색은 후보를 `discover._fit_score`(ATR% 3.0 / 정배열 2.0 / 60일선 유지 2.0 /
 52주 위치 1.5 / 상장 3년 미만 −1.5)로 정렬해 추천 순으로 보여준다. 이 가중치는 추세추종
 원칙에서 연역한 값이고 측정된 적이 없다. 운영자가 위에서부터 고르면 그대로 유니버스
 구성이 되므로, 정렬이 정보를 담고 있는지 아니면 그냥 보기 좋은 순서인지 갈라야 한다.
 (`_fit_score` 주석 자체가 "위쪽만 골라 담는 것이 낫다는 근거는 없다"고 적어 두었다.)

[선견을 어떻게 막는가] 적합도는 **그 시점까지의 데이터로만** 계산하고, 성과는 **그 이후
 12개월**에서 잰다. 오늘 기준으로 점수를 매겨 10년을 돌리면 '지금 좋아 보이는 종목'을
 과거에 심는 셈이라 무엇을 쟀는지 알 수 없게 된다.

[무엇을 재는가]
  A) 신호 성질 — 재조정 시점의 적합도 사분위별로, 이후 1년 진입 신호의 전방 20/60일 수익.
  B) 포트폴리오 짝비교 — 같은 창·같은 풀에서 상위 K / 하위 K / 무작위 K 를 관심종목으로
     삼아 12개월을 돌린다. 무작위가 기준선이다(감사에서 이긴 것은 '무작위 확장'이었다).

[실행] python3 tools/audit_discover_fit.py --pool-size 100 --k 20 --seeds 5
"""
import argparse
import os
import random
import sys

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import config  # noqa: E402
from modules import portfolio_backtest as pb  # noqa: E402
from modules.manage.discover import DEFENSIVE_KEYWORDS, HOLDING_KEYWORDS, _fit_score  # noqa: E402
from tools.audit_defensive_sector import (  # noqa: E402
    INITIAL_CAPITAL, entry_days, metrics, new_scale_fn_factory,
)


def rule_pool(pool, size, seed):
    """탐색 메뉴와 같은 규칙을 통과한 종목에서 시총 구간에 고르게 뽑는다."""
    import FinanceDataReader as fdr
    krx = fdr.StockListing("KRX")
    krx = krx[krx["Market"].isin(["KOSPI", "KOSDAQ"])].dropna(subset=["Marcap"])
    krx = krx.sort_values("Marcap", ascending=False).head(pool)
    desc = fdr.StockListing("KRX-DESC").set_index("Code")
    dept = dict(zip(krx["Code"], krx.get("Dept", krx["Code"] * 0)))

    def ind(code):
        if code not in desc.index:
            return ""
        v = desc.loc[code, "Industry"]
        return str(v.iloc[0] if hasattr(v, "iloc") else v)

    kept = []
    for _, r in krx.iterrows():
        code, name = r["Code"], r["Name"]
        d_txt = str(dept.get(code) or "")
        if "관리종목" in d_txt or "투자주의환기" in d_txt:
            continue
        if "스팩" in name or "리츠" in name or "SPAC" in d_txt or not code.endswith("0"):
            continue
        t = ind(code)
        if any(k in t for k, _lab in DEFENSIVE_KEYWORDS) or any(k in t for k in HOLDING_KEYWORDS):
            continue
        kept.append((code, name))
    if len(kept) <= size:
        return kept
    rng = random.Random(seed)
    buckets = [[] for _ in range(size)]
    step = len(kept) / size
    for i, row in enumerate(kept):
        buckets[min(int(i / step), size - 1)].append(row)
    return [rng.choice(b) for b in buckets if b]


def fit_at(df, idx):
    """idx 시점까지의 데이터로만 적합도를 계산한다 — discover._enrich 와 같은 정의."""
    d = df.iloc[:idx + 1]
    if len(d) < 130:
        return None
    last = d.iloc[-1]
    close = float(last.get("close", 0) or 0)
    if close <= 0:
        return None
    atr = float(last.get("ATR", 0) or 0)
    hi52 = float(d["high"].tail(252).max())
    lo52 = float(d["low"].tail(252).min())
    ema60 = float(last.get("EMA60", 0) or 0)
    ema120 = float(last.get("EMA120", 0) or 0)
    c = {
        "atr_pct": atr / close * 100,
        "w52": ((close - lo52) / (hi52 - lo52) * 100) if hi52 > lo52 else 0.0,
        "above60": close > ema60 > 0,
        "align": close > ema60 > ema120 > 0,
        "years": len(d) / 246,
    }
    try:
        c["persist"] = float((d["close"].tail(120) > d["EMA60"].tail(120)).mean() * 100)
    except Exception:
        c["persist"] = 0.0
    return c


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=3650)
    ap.add_argument("--pool", type=int, default=500, help="시총 상위 범위(탐색 메뉴 기본과 동일)")
    ap.add_argument("--pool-size", type=int, default=100, help="풀에서 실제로 준비할 종목 수")
    ap.add_argument("--k", type=int, default=20, help="한 팔이 들고 가는 관심종목 수")
    ap.add_argument("--seeds", type=int, default=5, help="무작위 팔의 씨드 수")
    ap.add_argument("--months", type=int, default=12, help="재조정 간격(개월)")
    ap.add_argument("--slots", type=int, default=None)
    ap.add_argument("--seed", type=int, default=20260816)
    ap.add_argument("--only", default="A,B")
    args = ap.parse_args()
    only = args.only.split(",")
    slots = args.slots or getattr(config, "SYSTEM_MAX_HOLDINGS", 4)

    targets = rule_pool(args.pool, args.pool_size, args.seed)
    print(f"[준비] 규칙 통과 풀에서 {len(targets)}종목 · {args.days}일 · 슬롯 {slots}", flush=True)
    dfs, mf, dates, failed = pb.prepare_universe(targets, args.days)
    print(f"[준비] 사용 {len(dfs)}종목 (실패 {len(failed)}) · 거래일 {len(dates)}", flush=True)

    thr = {"BUY_SCORE": config.ANALYSIS_THRESHOLDS["BUY_SCORE"],
           "BUY_RSI_MAX": config.ANALYSIS_THRESHOLDS["BUY_RSI_MAX"],
           "RISE_SCORE": config.ANALYSIS_THRESHOLDS["RISE_SCORE"],
           "WEIGHTS": config.SCORING_WEIGHTS}
    status = pb.precompute_status(dfs, thr)
    new_scale = new_scale_fn_factory(dates, args.days)

    # 재조정 시점 — 워밍업 250일 이후부터 args.months 간격.
    step = max(1, int(args.months * 21))
    marks = list(range(250, len(dates) - step, step))
    print(f"[준비] 재조정 {len(marks)}회 · 창 {step}거래일", flush=True)

    # 종목별 date → 행 인덱스
    pos = {c: {str(d): i for i, d in enumerate(df["date"])} for c, df in dfs.items()}

    scores_by_mark = []
    for m in marks:
        day = dates[m]
        row = {}
        for c, df in dfs.items():
            i = pos[c].get(str(day))
            if i is None:
                continue
            f = fit_at(df, i)
            if f:
                row[c] = _fit_score(f)
        scores_by_mark.append((m, day, row))

    # ---------------- A) 적합도 사분위별 전방수익 ----------------
    if "A" in only:
        sig = entry_days(dfs, status, mf)
        buckets = {q: {"f20": [], "f60": []} for q in ("Q1(상위)", "Q2", "Q3", "Q4(하위)")}
        for (m, day, row) in scores_by_mark:
            if len(row) < 8:
                continue
            order = sorted(row, key=lambda c: row[c], reverse=True)
            n = len(order)
            qs = {c: ("Q1(상위)" if r < n / 4 else "Q2" if r < n / 2
                      else "Q3" if r < 3 * n / 4 else "Q4(하위)")
                  for r, c in enumerate(order)}
            lo, hi = m, min(m + step, len(dates) - 1)
            win = set(dates[lo:hi])
            for c, q in qs.items():
                rec, hits = sig.get(c, ([], []))
                if not rec:
                    continue
                cl = np.array([r["close"] for r in rec], dtype=float)
                for i in hits:
                    if str(rec[i]["date"]) not in win:
                        continue
                    if i + 20 < len(rec):
                        buckets[q]["f20"].append((cl[i + 20] - cl[i]) / cl[i] * 100)
                    if i + 60 < len(rec):
                        buckets[q]["f60"].append((cl[i + 60] - cl[i]) / cl[i] * 100)
        print("\n[A] 재조정 시점 적합도 사분위 → 이후 1년 진입 신호의 전방수익")
        print(f"{'분위':<10}{'신호수':>8}{'20일평균':>10}{'20일중앙':>10}{'P90':>8}{'P99':>8}"
              f"{'60일평균':>10}{'승률%':>8}")
        for q in ("Q1(상위)", "Q2", "Q3", "Q4(하위)"):
            a = np.array(buckets[q]["f20"]); b = np.array(buckets[q]["f60"])
            if len(a) < 30:
                print(f"{q:<10}{len(a):>8}   (표본 부족)")
                continue
            print(f"{q:<10}{len(a):>8}{a.mean():>10.2f}{np.median(a):>10.2f}"
                  f"{np.percentile(a, 90):>8.1f}{np.percentile(a, 99):>8.1f}"
                  f"{b.mean() if len(b) else float('nan'):>10.2f}{(a > 0).mean() * 100:>8.1f}")

    # ---------------- B) 포트폴리오 짝비교 ----------------
    if "B" in only:
        def run(codes, wd):
            return metrics(pb.run_portfolio(
                {c: dfs[c] for c in codes}, {c: status[c] for c in codes}, wd,
                initial_capital=INITIAL_CAPITAL, slots=slots,
                market_filter_dates={c: mf.get(c, set()) for c in codes},
                risk_scale_by_date=new_scale()))

        print(f"\n[B] 상위 {args.k} / 하위 {args.k} / 무작위 {args.k}({args.seeds}씨드) · "
              f"재조정 {len(marks)}회 · 각 창 {args.months}개월")
        print(f"{'재조정일':<12}{'상위':>9}{'하위':>9}{'무작위평균':>11}{'상위-무작위':>12}"
              f"{'하위-무작위':>12}")
        rows = []
        for (m, day, row) in scores_by_mark:
            if len(row) < args.k * 2:
                continue
            wd = dates[m:min(m + step, len(dates))]
            order = sorted(row, key=lambda c: row[c], reverse=True)
            top = order[:args.k]
            bot = order[-args.k:]
            rt = run(top, wd)["ret"]
            rb = run(bot, wd)["ret"]
            rr = []
            for s in range(args.seeds):
                pick = random.Random(args.seed + m * 31 + s).sample(list(row), args.k)
                rr.append(run(pick, wd)["ret"])
            rrm = float(np.mean(rr))
            rows.append((day, rt, rb, rrm))
            print(f"{day:<12}{rt:>9.2f}{rb:>9.2f}{rrm:>11.2f}{rt - rrm:>12.2f}{rb - rrm:>12.2f}",
                  flush=True)
        if rows:
            tw = sum(1 for _d, t, _b, r in rows if t > r)
            bw = sum(1 for _d, _t, b, r in rows if b > r)
            n = len(rows)
            print(f"\n[B-집계] 창 {n}개 · 상위>무작위 {tw}/{n} · 하위>무작위 {bw}/{n}")
            print(f"  평균 수익 — 상위 {np.mean([r[1] for r in rows]):.2f}% · "
                  f"하위 {np.mean([r[2] for r in rows]):.2f}% · "
                  f"무작위 {np.mean([r[3] for r in rows]):.2f}%")


if __name__ == "__main__":
    main()
