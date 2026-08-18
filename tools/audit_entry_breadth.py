"""무작위 교란이 기준선을 이기는 기전 — 진입 종목의 '폭'인가.

[관측된 것] `audit_random_block_null`에서 무작위 진입 차단이 기준선을 거의 항상 이겼다.
 기준선 순위가 12장 기준 전체창 13/13·12/13·10/13·12/13(차단율 5·8.3·15·25%), 구간
 1·2·3 각각 12/13·11/13·11/13이다. **차단율을 5배로 올려도 우열이 안 바뀐다** — 변수는
 '얼마나 막느냐'가 아니라 '결정적 선택을 깨느냐'다. 다만 σ가 30 → 87로 벌어지고 최소값이
 기준선 아래로 떨어지므로 '평균이 오른다'는 개선의 증거가 아니다. 기전을 모른다.

[가설] 결정적 랭킹은 같은 종목을 반복해서 고른다. 그래서 10년간 **거쳐 간 서로 다른
 종목 수가 줄고**, 꼬리가 두꺼운 시스템에서 큰 승자를 만날 기회는 거쳐 간 이름의 개수에
 달렸으므로 그것이 손해다. 사실이면 처방은 '무작위로 하라'가 아니라 **'같은 이름 재진입이
 폭을 잠식하지 않게 하라'**가 된다 — 전혀 다른 지침이다.

[이 도구가 하는 일 — 진단만] 백테스트를 새로 설계하지 않는다. 기준선과 교란 팔을 돌려
 매 시행마다 네 가지를 함께 기록하고, **폭이 수익을 예측하는지**를 본다.
   ① 서로 다른 진입 종목 수(폭)  ② 총 진입 횟수  ③ 종목별 진입 쏠림(HHI)
   ④ 그 시행의 수익
 두 층위에서 상관을 본다. **장 사이**(교란 패턴이 다르면 폭도 다른가)와 **시행 안**
 (같은 팔에서도 폭이 넓은 시행이 더 버는가). 시행 안 상관이 살아 있어야 '폭 자체가
 원인'이라는 말이 선다 — 장 사이만 맞으면 교란의 다른 부산물일 수 있다.

[이것으로 결론내지 않는다] 상관은 인과가 아니다. 폭이 수익을 예측하더라도, 그것을
 **결정적 규칙**(같은 종목 재진입 쿨다운 등)으로 재현해 승-무-패로 이겨야 채택 후보가
 된다. 이 도구는 그 규칙을 만들 값이 있는지만 판정한다.

[실행] python3 tools/audit_entry_breadth.py --draws 8 --live-only
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
    INITIAL_CAPITAL, SELL_REASONS, metrics, new_scale_fn_factory,
)
from tools.audit_universe import dead_targets, extend_targets  # noqa: E402


def breadth(r):
    """진입 기록에서 폭·쏠림을 센다. 청산은 세지 않는다(같은 진입의 뒷면일 뿐)."""
    cnt = {}
    for t in r["trades"]:
        if t["reason"] not in SELL_REASONS:
            cnt[t["code"]] = cnt.get(t["code"], 0) + 1
    n = sum(cnt.values())
    if not n:
        return 0, 0, 0.0
    share = np.array(list(cnt.values()), dtype=float) / n
    return len(cnt), n, float((share ** 2).sum())     # HHI: 클수록 쏠림


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=3650)
    ap.add_argument("--trials", type=int, default=12)
    ap.add_argument("--seeds", default="20260816,7,101")
    ap.add_argument("--size", type=int, default=44)
    ap.add_argument("--pool", type=int, default=500)
    ap.add_argument("--dead-frac", type=float, default=0.2)
    ap.add_argument("--rate", type=float, default=8.3, help="무작위 차단율(%%)")
    ap.add_argument("--draws", type=int, default=8)
    ap.add_argument("--live-only", action="store_true")
    args = ap.parse_args()
    seeds = [int(x) for x in args.seeds.split(",")]
    rate = args.rate / 100
    slots = getattr(config, "SYSTEM_MAX_HOLDINGS", 4)

    config.session.load_stock_config()
    live = [(s["code"], s["name"]) for s in config.session.stock_data.get("stocks_kr", [])]
    if args.live_only:
        ext, dead_t, n_dead = [], [], 0
    else:
        ext = extend_targets({c for c, _ in live}, 60, mode="random", pool=args.pool)
        n_dead = int(args.size * args.dead_frac)
        try:
            dead_t = dead_targets(n_dead + 10)
        except Exception as e:
            # 폐지 목록 원본이 죽어도 확장 풀만으로 진행한다 — 다만 폐지가 빠졌다는 사실을
            #  숨기지 않는다. 표본 크기를 유지하려 조용히 생존 종목으로 채우면 생존 편향이
            #  기록 없이 섞인다([[survivorship-premium-2x]]).
            print(f"[경고] 폐지 목록을 못 받았다({type(e).__name__}) — 폐지 0으로 진행한다. "
                  f"생존 편향이 걸린 표본이다.", flush=True)
            dead_t, n_dead = [], 0
    dfs, mf, dates, _f = pb.prepare_universe(live + ext + dead_t, args.days)
    dead_set = {c for c, _ in dead_t}
    dead_c = [c for c in dfs if c in dead_set]
    live_c = [c for c in dfs if c not in dead_set]
    size = min(args.size, len(live_c) + len(dead_c)) if args.live_only else args.size
    n_dead = min(n_dead, len(dead_c))

    thr = {"BUY_SCORE": config.ANALYSIS_THRESHOLDS["BUY_SCORE"],
           "BUY_RSI_MAX": config.ANALYSIS_THRESHOLDS["BUY_RSI_MAX"],
           "RISE_SCORE": config.ANALYSIS_THRESHOLDS["RISE_SCORE"],
           "WEIGHTS": config.SCORING_WEIGHTS}
    status = pb.precompute_status(dfs, thr)
    new_scale = new_scale_fn_factory(dates, args.days)

    picks = {}
    for sd in seeds:
        for i in range(args.trials):
            rng = random.Random(sd * 31 + i)
            picks[(sd, i)] = (rng.sample(dead_c, n_dead)
                              + rng.sample(live_c, min(size - n_dead, len(live_c))))
    keys = [(sd, i) for sd in seeds for i in range(args.trials)]
    print(f"[준비] {len(dfs)}종목 · 표본 {size} · 거래일 {len(dates)} · 슬롯 {slots} · "
          f"차단율 {args.rate}% · 장수 {args.draws}", flush=True)

    def gate(salt):
        def g(day, code, _held):
            return random.Random(f"{salt}|{day}|{code}").random() < rate
        return g

    def run_arm(g):
        rows = []
        for sd, i in keys:
            r = pb.run_portfolio(
                {c: dfs[c] for c in picks[(sd, i)]},
                {c: status[c] for c in picks[(sd, i)]}, dates,
                initial_capital=INITIAL_CAPITAL, slots=slots,
                market_filter_dates={c: mf.get(c, set()) for c in picks[(sd, i)]},
                risk_scale_by_date=new_scale(), entry_gate=g)
            nd, ne, hhi = breadth(r)
            rows.append({"ret": metrics(r)["ret"], "distinct": nd, "entries": ne,
                         "hhi": hhi, "uni": len(picks[(sd, i)])})
        return rows

    def avg(rows, k):
        return float(np.mean([x[k] for x in rows]))

    base = run_arm(None)
    print(f"\n{'팔':<22}{'수익%':>9}{'서로다른종목':>13}{'유니버스대비%':>14}"
          f"{'총진입':>8}{'종목당':>8}{'HHI':>8}")
    print(f"{'[기준선] 차단 없음':<22}{avg(base, 'ret'):>9.1f}{avg(base, 'distinct'):>13.1f}"
          f"{avg(base, 'distinct') / avg(base, 'uni') * 100:>14.1f}{avg(base, 'entries'):>8.1f}"
          f"{avg(base, 'entries') / max(1e-9, avg(base, 'distinct')):>8.2f}{avg(base, 'hhi'):>8.4f}")

    draws = []
    for j in range(args.draws):
        rows = run_arm(gate(f"bw{j}"))
        draws.append(rows)
        print(f"{f'무작위 {args.rate}% ({j + 1}장)':<22}{avg(rows, 'ret'):>9.1f}"
              f"{avg(rows, 'distinct'):>13.1f}"
              f"{avg(rows, 'distinct') / avg(rows, 'uni') * 100:>14.1f}"
              f"{avg(rows, 'entries'):>8.1f}"
              f"{avg(rows, 'entries') / max(1e-9, avg(rows, 'distinct')):>8.2f}"
              f"{avg(rows, 'hhi'):>8.4f}", flush=True)

    # ── ① 장 사이: 교란 패턴이 다르면 폭도 다르고, 폭이 넓은 장이 더 버는가
    dr = np.array([avg(x, "ret") for x in draws])
    dd = np.array([avg(x, "distinct") for x in draws])
    dh = np.array([avg(x, "hhi") for x in draws])
    print(f"\n[1] 장 사이 상관 ({len(draws)}장)")
    if len(draws) >= 3 and dd.std() > 1e-9:
        print(f"   폭 vs 수익  r = {np.corrcoef(dd, dr)[0, 1]:+.3f}   "
              f"(폭 범위 {dd.min():.1f}~{dd.max():.1f})")
        print(f"   쏠림 vs 수익 r = {np.corrcoef(dh, dr)[0, 1]:+.3f}   "
              f"(HHI 범위 {dh.min():.4f}~{dh.max():.4f})")
    else:
        print("   장 사이 폭이 거의 안 변한다 — 이 층위로는 판정할 수 없다.")

    # ── ② 시행 안: 같은 팔에서도 폭이 넓은 시행이 더 버는가 (교란의 부산물 배제)
    print(f"\n[2] 시행 안 상관 — 팔마다 {len(keys)}시행에서 폭 vs 수익")
    rs = []
    for lbl, rows in [("기준선", base)] + [(f"{j + 1}장", d) for j, d in enumerate(draws)]:
        a = np.array([x["distinct"] for x in rows], dtype=float)
        b = np.array([x["ret"] for x in rows], dtype=float)
        r = float(np.corrcoef(a, b)[0, 1]) if a.std() > 1e-9 else float("nan")
        rs.append(r)
        if lbl in ("기준선", "1장"):
            print(f"   {lbl:<6} r = {r:+.3f}", flush=True)
    fin = [x for x in rs[1:] if np.isfinite(x)]
    if fin:
        print(f"   교란 팔 {len(fin)}개 평균 r = {np.mean(fin):+.3f} "
              f"(범위 {min(fin):+.3f}~{max(fin):+.3f})")

    print("\n[읽는 법] 두 층위가 **모두** 양(+)이라야 '폭이 원인'이라는 말이 선다. 장 사이만 "
          "양이면 교란의 다른 부산물일 수 있고, 시행 안만 양이면 종목 운(어떤 44개를 뽑았나)"
          "일 수 있다. 상관이 약하면 기전은 폭이 아니므로 다른 곳을 봐야 한다.")
    print("[다음] 폭이 원인으로 확인되면 **결정적 규칙**(같은 종목 재진입 쿨다운 등)으로 "
          "재현해 승-무-패로 이겨야 채택 후보가 된다. 상관만으로는 아무것도 바꾸지 않는다.")


if __name__ == "__main__":
    main()
