"""재선별 워크포워드 — '그 시점에 고르고, N개월 뒤 다시 고른다'를 10년 굴리면 얼마인가.

[왜 · 2026-09-20] 축 C(audit_universe --axis C)는 2016년 시점 유니버스를 **10년 고정**해서 쟀다:
현행풀 340% vs PIT 무작위 21% vs PIT 적합도 상위 고정 2%. 즉 한 번 고른 유니버스의 선별 효과는
없다. 그런데 실제 운용은 관심종목을 계속 갈아 끼운다 — 그러면 진짜 ex-ante 기대치는 "N개월마다
그 시점까지의 데이터로만 적합도 상위 K 를 다시 골라 다음 N개월을 굴린다"로 재야 한다.
audit_discover_fit 이 12개월 재선별을 쟀지만 **현재 상장 목록**에서 뽑아 생존 편향이 남아
있었다. Open API 날짜별 스냅샷으로 매 재선별 시점의 실제 상장 종목에서 뽑는다.

[설계] 창 시작일부터 N개월 단위로 자른다. 각 기간 시작일 d 에서
  · 후보 = d 의 시총 상위 pool(우선주·스팩·리츠 제외, KOSPI·KOSDAQ 주권)
  · 적합도 = d 까지의 봉으로만 discover._fit_score (tools.audit_discover_fit.fit_at 과 같은 정의)
  · 팔: 적합도 상위 K / 무작위 K(같은 후보, 해시) / 적합도 하위 K
  · 그 기간을 run_portfolio 로 굴리고, 기간 끝에 전부 정리한 것으로 보고 다음 기간은 새로 시작
    (기간을 넘는 보유는 모든 팔에서 같은 규칙으로 끊긴다 — 팔 사이 비교엔 공정하다).
  · 10년 수익 = 기간 수익의 복리 곱. MDD 는 기간별 자산곡선을 이어 붙여 잰다.
 현행풀(오늘 관심종목 고정)도 같은 기간 분할로 돌려 '숫자의 자리'를 보여 준다 — 그것은
 hindsight 이므로 비교 대상이 아니라 눈금이다.

[읽는 법] 적합도 상위 − 무작위 = 재선별로 실제 얻는 선별 효과(ex-ante). 상위가 무작위를
 기간 승패에서 꾸준히 이기면 '계속 갈아 끼우는 과정'이 엣지다. 절대 수익은 폐지 종목이
 마지막 봉 종가로 청산되므로 여전히 낙관 쪽이다.

[실행] python3 tools/audit_pit_walkforward.py --days 3650 --months 12 --k 25 --pool 300 --seeds 3
"""
import argparse
import os
import random
import sys

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tools.audit_common import exits, seed_notice  # noqa: E402
from tools.audit_universe import _hash_draw, _is_excluded_ticker, _pit_snapshot  # noqa: E402

import config  # noqa: E402
from modules import backtest as bt  # noqa: E402
from modules import krx_daily  # noqa: E402
from modules import portfolio_backtest as pb  # noqa: E402

INITIAL_CAPITAL = 10_000_000


def _fetched_day_on_or_before(d):
    from modules import krx_openapi as oa
    with oa._DB_LOCK, oa._connect() as conn:
        row = conn.execute("SELECT MAX(bas_dd) FROM fetched WHERE api_id='stk_bydd_trd' AND n>0 AND bas_dd<=?",
                           (d,)).fetchone()
    return row[0] if row and row[0] else None


def candidates(day, pool):
    """day 의 시총 상위 pool 후보 [(code, name)] — 그날 실제로 거래된 종목만."""
    snap = _pit_snapshot(day)
    cand = [(c, v["name"]) for c, v in snap.items() if not _is_excluded_ticker(c, v["name"])]
    cand.sort(key=lambda t: snap[t[0]]["marcap"], reverse=True)
    return cand[:pool]


_FIT_CACHE = {}


def fit_score_at(code, day, lookback_days):
    """day 까지의 봉으로만 적합도. 이력 130봉 미만·조회 실패면 None."""
    key = (code, day)
    if key in _FIT_CACHE:
        return _FIT_CACHE[key]
    from modules.manage.discover import _fit_score
    from tools.audit_discover_fit import fit_at
    score = None
    try:
        df = krx_daily.get_daily(code, lookback_days=lookback_days)
        if df is not None and not df.empty:
            df = df[df["date"].astype(str) <= day].reset_index(drop=True)
            if len(df) >= 130:
                f = fit_at(bt.compute_price_indicators(df), len(df) - 1)
                if f:
                    score = _fit_score(f)
    except Exception:       # noqa: BLE001
        score = None
    _FIT_CACHE[key] = score
    return score


def period_starts(dates, months):
    """dates(YYYYMMDD 오름차순)를 months 개월 단위로 잘라 [(시작idx, 끝idx)]."""
    out, i = [], 0
    while i < len(dates):
        y, m = int(dates[i][:4]), int(dates[i][4:6])
        m2 = m + months
        y2 = y + (m2 - 1) // 12
        m2 = (m2 - 1) % 12 + 1
        cut = f"{y2:04d}{m2:02d}01"
        j = i
        while j < len(dates) and dates[j] < cut:
            j += 1
        if j - i >= 40:                  # 너무 짧은 꼬리 기간은 앞 기간에 붙인다
            out.append((i, j))
        elif out:
            out[-1] = (out[-1][0], j)
        i = j
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=3650)
    ap.add_argument("--months", type=int, default=12, help="재선별 주기(개월)")
    ap.add_argument("--k", type=int, default=25, help="한 기간의 관심종목 수")
    ap.add_argument("--pool", type=int, default=300, help="재선별 시점 시총 상위 후보 수")
    ap.add_argument("--seeds", type=int, default=3, help="무작위 팔의 씨드 수")
    ap.add_argument("--seed", type=int, default=20260816)
    ap.add_argument("--slots", type=int, default=None)
    args = ap.parse_args()
    seed_notice(args.seeds, example="--seeds 3")

    slots = args.slots or getattr(config, "SYSTEM_MAX_HOLDINGS", 4)
    config.session.load_stock_config()
    live = [(s["code"], s["name"]) for s in config.session.stock_data.get("stocks_kr", [])]

    # 거래일 축은 현행풀에서 받는다(어느 종목이든 같은 달력이다)
    live_dfs, live_mf, dates = pb.prepare_universe(live, args.days)[:3]
    periods = period_starts(dates, args.months)
    print(f"[준비] {dates[0]}~{dates[-1]} · {len(periods)}기간({args.months}개월) · K={args.k} · pool={args.pool} · 슬롯 {slots}")

    # 기간마다 후보·적합도 → 팔별 유니버스
    plan = []          # (기간, 시작일, {팔: [codes]})
    all_codes = set(live_dfs)
    for pi, (i, j) in enumerate(periods):
        d = _fetched_day_on_or_before(dates[i]) or dates[i]
        cand = candidates(d, args.pool)
        scored = [(fit_score_at(c, d, args.days + 400), c) for c, _n in cand]
        scored = [(s, c) for s, c in scored if s is not None]
        scored.sort(reverse=True)
        top = [c for _s, c in scored[:args.k]]
        bottom = [c for _s, c in scored[-args.k:]]
        arms = {"적합도 상위": top, "적합도 하위": bottom}
        for si in range(args.seeds):
            arms[f"무작위#{si + 1}"] = [c for c, _n in _hash_draw([(c, "") for _s, c in scored], len(scored), args.k,
                                                                  args.seed + si * 1009 + pi)]
        for v in arms.values():
            all_codes.update(v)
        plan.append((pi, d, arms))
        print(f"  기간{pi + 1} {dates[i]}~{dates[j - 1]} 후보 {len(scored)} · 상위 {', '.join(top[:5])} …", flush=True)

    targets = [(c, c) for c in sorted(all_codes) if c not in live_dfs]
    dfs, mf, _d, failed = pb.prepare_universe(targets, args.days)
    dfs.update(live_dfs)
    mf.update(live_mf)
    print(f"[준비] 유니버스 합집합 {len(dfs)}종목 (실패 {len(failed)})")
    thresholds = {"BUY_SCORE": config.ANALYSIS_THRESHOLDS["BUY_SCORE"],
                  "BUY_RSI_MAX": config.ANALYSIS_THRESHOLDS["BUY_RSI_MAX"],
                  "RISE_SCORE": config.ANALYSIS_THRESHOLDS["RISE_SCORE"], "WEIGHTS": config.SCORING_WEIGHTS}
    status = pb.precompute_status(dfs, thresholds)

    from tools.audit_drawdown_axis import market_scale_by_date, make_scale_fn
    p = getattr(config, "RISK_SCALING_PARAMS", {}) or {}
    mkt = market_scale_by_date(dates, args.days)
    dd = None
    if p.get("USE_DRAWDOWN_RISK_SCALING", True):
        dd = (int(p.get("DD_LOOKBACK_DAYS", 90)), float(p.get("DD_LEVEL_1", 5.0)),
              float(p.get("DD_SCALE_1", 0.9)), float(p.get("DD_LEVEL_2", 10.0)), float(p.get("DD_SCALE_2", 0.8)))

    arm_names = ["적합도 상위", "적합도 하위"] + [f"무작위#{si + 1}" for si in range(args.seeds)] + ["현행풀(hindsight)"]
    per = {a: [] for a in arm_names}          # 기간별 (수익%, 청산수, 승률)
    curves = {a: [] for a in arm_names}       # 기간 끝점을 이어 붙인 자산
    live_codes = list(live_dfs)

    def run(codes, wdates):
        codes = [c for c in codes if c in dfs]
        if not codes:
            return None
        r = pb.run_portfolio({c: dfs[c] for c in codes}, {c: status[c] for c in codes}, wdates,
                             initial_capital=INITIAL_CAPITAL, slots=slots,
                             market_filter_dates={c: mf.get(c, set()) for c in codes},
                             risk_scale_by_date=make_scale_fn(mkt, dd))
        return r

    for pi, d, arms in plan:
        i, j = periods[pi]
        wdates = dates[i:j]
        rng = random.Random(args.seed + pi)
        for a in arm_names:
            codes = live_codes if a.startswith("현행풀") else arms[a]
            if a.startswith("현행풀"):
                codes = rng.sample(live_codes, min(args.k, len(live_codes)))
            r = run(codes, wdates)
            if r is None:
                per[a].append((0.0, 0, 0.0, 0.0)); continue
            sells = exits(r)
            wr = (sum(1 for t in sells if t["profit"] > 0) / len(sells) * 100) if sells else 0.0
            per[a].append((float(r["total_return"]), len(sells), wr, float(r["mdd"])))
            base = curves[a][-1] if curves[a] else 1.0
            curves[a].append(base * (1 + float(r["total_return"]) / 100))
        print(f"  기간{pi + 1} 완료", end="\r", flush=True)
    print(" " * 40, end="\r")

    def compound(rs):
        v = 1.0
        for r in rs:
            v *= 1 + r / 100
        return (v - 1) * 100

    def mdd(a):
        """기간 안 MDD 의 최악과 기간 끝점끼리의 낙폭 중 더 나쁜 쪽(연속 자산곡선이 없어 하한 근사)."""
        peak, worst = -1e18, 0.0
        for v in [1.0] + curves[a]:
            peak = max(peak, v)
            worst = min(worst, (v - peak) / peak * 100 if peak > 0 else 0.0)
        return min(worst, min(x[3] for x in per[a]))

    W = 110
    print(f"\n{'=' * W}\n재선별 워크포워드 — {args.months}개월마다 그 시점 데이터로만 다시 고른다 ({len(periods)}기간, K={args.k}, 후보 {args.pool})\n{'=' * W}")
    print(f"{'팔':<18}{'10y 복리%':>10}{'MDD%':>8}{'기간평균%':>10}{'기간중앙%':>10}{'승기간':>8}{'청산':>6}{'승률%':>7}  vs무작위(기간 승-패)")
    print("-" * W)
    rnd_avg = [float(np.mean([per[f'무작위#{si + 1}'][pi][0] for si in range(args.seeds)])) for pi in range(len(periods))]
    for a in arm_names:
        rs = [x[0] for x in per[a]]
        wins = sum(1 for r in rs if r > 0)
        vs = sum(1 for pi, r in enumerate(rs) if r > rnd_avg[pi] + 1e-9)
        ls = sum(1 for pi, r in enumerate(rs) if r < rnd_avg[pi] - 1e-9)
        print(f"{a:<18}{compound(rs):>10.1f}{mdd(a):>8.1f}{np.mean(rs):>10.1f}{np.median(rs):>10.1f}"
              f"{wins:>5}/{len(rs):<3}{np.mean([x[1] for x in per[a]]):>6.0f}{np.mean([x[2] for x in per[a]]):>7.1f}"
              f"  {'—' if a.startswith('무작위') else f'{vs}-{ls}'}")
    print("\n기간별 수익% (적합도 상위 / 무작위 평균 / 현행풀):")
    for pi, (i, j) in enumerate(periods):
        print(f"  {dates[i]}~{dates[j - 1]}  {per['적합도 상위'][pi][0]:>7.1f} / {rnd_avg[pi]:>7.1f} / {per['현행풀(hindsight)'][pi][0]:>7.1f}")
    print("\n" + "-" * W)
    print("[읽는 법] '적합도 상위 − 무작위'가 재선별로 실제 얻는 선별 효과(ex-ante)다. 기간 승패가 꾸준하면 '계속 갈아 끼우는"
          " 과정'이 엣지다. 현행풀은 hindsight 눈금이지 비교 대상이 아니다. 폐지 종목은 마지막 봉 종가로 청산되므로 낙관 쪽.")


if __name__ == "__main__":
    main()
