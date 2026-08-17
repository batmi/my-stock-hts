"""ETF를 자동매매 대상에 넣을 것인가 — 근거 기록이 없는 스위치.

[왜] `SYSTEM_INCLUDE_ETF = False`는 감사 도구 69개 어디에도 등장하지 않는다. 관심종목에
 국내 ETF가 20종목 있는데 전부 자동매매에서 빠져 있고, 그 근거가 기록된 적이 없다.

[동기 하나는 이미 죽었다] '진입액이 커질 것'이라는 기대는 사전 진단으로 기각됐다 —
 ETF도 19/20이 변동성 하한에 눌린다(ATR/가격 중앙 5.27% vs 주식 6.61%, 진입액 둘 다
 100만원). 업종·테마 ETF라 개별주만큼 흔들리기 때문이다. 남은 질문은 **후보로서
 값을 하는가** 하나다.

[팔] 크기를 통제한다. 크기는 레버가 아니지만([[universe-size-lever]]) 통제는 해야 한다.
   A [기준선] 주식 44                — 현행
   B 주식 34 + ETF 10  (치환, 44)    — 일부를 ETF로 바꾸면
   C 주식 24 + ETF 20  (치환, 44)    — 전부 바꾸면
   D 주식 44 + ETF 20  (추가, 64)    — SYSTEM_INCLUDE_ETF=True 의 실제 동작
   E [대조] 주식 64     (추가, 64)   — D와 같은 크기. 늘어서 좋은 건지 ETF여서 좋은 건지
 D vs E가 이 도구의 핵심이다 — 그 차이만이 'ETF라서'다.

[실행] python3 tools/audit_etf_inclusion.py --trials 12
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
    INITIAL_CAPITAL, metrics, new_scale_fn_factory,
)
from tools.audit_universe import extend_targets  # noqa: E402


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=3650)
    ap.add_argument("--trials", type=int, default=12)
    ap.add_argument("--seeds", default="20260816,7,101")
    ap.add_argument("--size", type=int, default=44)
    ap.add_argument("--pool", type=int, default=500)
    ap.add_argument("--subperiods", type=int, default=3)
    args = ap.parse_args()
    seeds = [int(x) for x in args.seeds.split(",")]

    config.session.load_stock_config()
    sd = config.session.stock_data
    live = [(s["code"], s["name"]) for s in sd.get("stocks_kr", [])]
    etfs = [(s["code"], s["name"]) for s in sd.get("etfs_kr", [])]
    ext = extend_targets({c for c, _ in live}, 45, mode="random", pool=args.pool)

    dfs, mf, dates, failed = pb.prepare_universe(live + ext + etfs, args.days)
    etf_c = [c for c, _ in etfs if c in dfs]
    stock_c = [c for c, _ in (live + ext) if c in dfs]
    n_etf = len(etf_c)
    print(f"[준비] 주식 풀 {len(stock_c)} · ETF {n_etf} · 거래일 {len(dates)} "
          f"(실패 {len(failed)})", flush=True)

    thr = {"BUY_SCORE": config.ANALYSIS_THRESHOLDS["BUY_SCORE"],
           "BUY_RSI_MAX": config.ANALYSIS_THRESHOLDS["BUY_RSI_MAX"],
           "RISE_SCORE": config.ANALYSIS_THRESHOLDS["RISE_SCORE"],
           "WEIGHTS": config.SCORING_WEIGHTS}
    status = pb.precompute_status(dfs, thr)
    new_scale = new_scale_fn_factory(dates, args.days)
    slots = getattr(config, "SYSTEM_MAX_HOLDINGS", 4)

    half = max(1, n_etf // 2)
    ARMS = [(f"[기준선] 주식 {args.size}", "base"),
            (f"주식 {args.size - half} + ETF {half} (치환)", "sub_half"),
            (f"주식 {args.size - n_etf} + ETF {n_etf} (치환)", "sub_all"),
            (f"주식 {args.size} + ETF {n_etf} (추가)", "add_etf"),
            (f"[대조] 주식 {args.size + n_etf} (추가)", "add_stock")]

    picks = {}
    for sd_ in seeds:
        for i in range(args.trials):
            rng = random.Random(sd_ * 31 + i)
            base = rng.sample(stock_c, args.size)
            rest = [c for c in stock_c if c not in base]
            extra = rng.sample(rest, min(n_etf, len(rest)))
            picks[(sd_, i, "base")] = base
            picks[(sd_, i, "sub_half")] = base[:args.size - half] + etf_c[:half]
            picks[(sd_, i, "sub_all")] = base[:args.size - n_etf] + etf_c
            picks[(sd_, i, "add_etf")] = base + etf_c
            picks[(sd_, i, "add_stock")] = base + extra

    k = max(1, args.subperiods)
    step = max(1, len(dates) // k)
    W = [("전체", list(dates))]
    W += [(f"구간{i + 1}", dates[i * step:(i + 1) * step if i < k - 1 else len(dates)])
          for i in range(k)]

    for wn, wd in W:
        print(f"\n########## {wn} ({len(wd)} 거래일) ##########")
        print(f"{'팔':<28}{'수익%':>9}{'MDD%':>8}{'MAR':>7}{'PF':>6}{'청산':>6}"
              f"{'상위10%':>9}{'승률%':>7}{'현금%':>7}{'승-무-패':>10}")
        base_res, cell = None, {}
        for label, kind in ARMS:
            res = []
            for sd_ in seeds:
                for i in range(args.trials):
                    pick = picks[(sd_, i, kind)]
                    r = pb.run_portfolio(
                        {c: dfs[c] for c in pick}, {c: status[c] for c in pick}, wd,
                        initial_capital=INITIAL_CAPITAL, slots=slots,
                        market_filter_dates={c: mf.get(c, set()) for c in pick},
                        risk_scale_by_date=new_scale())
                    res.append(dict(metrics(r), cash=r["avg_cash_ratio"]))
            cell[kind] = res
            g = lambda key: float(np.mean([m[key] for m in res]))  # noqa: E731
            if base_res is None:
                base_res, wl = res, "— (기준)"
            else:
                win = sum(1 for x, y in zip(res, base_res) if x["ret"] > y["ret"] + 1e-9)
                tie = sum(1 for x, y in zip(res, base_res) if abs(x["ret"] - y["ret"]) <= 1e-9)
                wl = f"{win}-{tie}-{len(res) - win - tie}"
            print(f"{label:<28}{g('ret'):>9.1f}{g('mdd'):>8.1f}{g('mar'):>7.2f}{g('pf'):>6.2f}"
                  f"{g('n'):>6.0f}{g('top10'):>9.1f}{g('win'):>7.1f}{g('cash'):>7.1f}"
                  f"{wl:>10}", flush=True)
        a, b = cell["add_etf"], cell["add_stock"]
        w = sum(1 for x, y in zip(a, b) if x["ret"] > y["ret"] + 1e-9)
        print(f"  [핵심] ETF 추가 vs 같은 크기 주식 추가: {w}-{len(a) - w} "
              f"(ETF {np.mean([m['ret'] for m in a]):.1f}% vs 주식 "
              f"{np.mean([m['ret'] for m in b]):.1f}%)", flush=True)

    print("\n[읽는 법] D(ETF 추가) vs E(주식 추가)만이 'ETF라서'를 잰다. 나머지 비교는 "
          "크기 효과가 섞인다.")


if __name__ == "__main__":
    main()
