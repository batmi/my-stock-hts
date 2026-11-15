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

[대조군 · 2026-10-17] 표 아래에 '같은 종목 동일가중 단순 보유(왕복 비용 차감)'·'후보 전체 보유'·
 코스피·코스닥을 같은 기간 분할로 붙인다(tools/audit_common.buy_hold·index_return). 팔끼리의 순위는
 위 표가, '매매가 그냥 들고 있기·지수보다 나은가'는 이 줄들이 답한다.

[노출을 맞춘 지수 · 열위 분해 · 2026-11-15] 지수 줄은 자산 100%를 지수에 넣은 것이고 전략은 평균 현금이 절반을 넘는다.
 그대로 견주면 '덜 들고 있어서 뒤졌나'와 '종목을 못 골라서 뒤졌나'가 섞인다. 적합도 상위 팔이 그날 들고 있던 주식
 비중만큼만 코스피를 든 줄(modules.benchmark.exposure_matched)을 사이에 두고 기간마다 둘로 가른다:
   노출 몫      = 코스피(전략 노출) − 코스피      덜 들고 있어서 놓친(음수)·피한(양수) 몫
   선별·매매 몫 = 전략 − 코스피(전략 노출)        같은 노출에서 종목 선별·매매가 더한 몫

[시작점 이동(--anchors) · 2026-11-15] 적합도 상위 팔은 씨드가 없다(무작위 팔만 씨드를 탄다). 그런데 같은 설정이 기간을
 12개월로 자르느냐 6개월로 자르느냐만으로 +74% ↔ +156% 로 갈렸다 — 경로 의존이 크다. 그래서 '전략 vs 지수'의 재확인은
 씨드가 아니라 **기간 경계를 한 달씩 밀어**(--anchor-step 거래일) 여러 번 재는 것으로 한다. 한두 시작점에서만 이기면
 그것은 경계 운이다.

[--offline] 외부 연결을 막고 KRX Open API 로컬 저장소만 쓴다(tools.audit_synergy_boundary.block_network).
 수급(KIS)은 빠진다 — 2026-10-17 첫 측정과 같은 조건이다. 켜고 끈 수치를 나란히 놓지 말 것.

[실행] python3 tools/audit_pit_walkforward.py --days 3650 --months 12 --k 25 --pool 300 --seeds 3 --offline --anchors 5
"""
import argparse
import os
import random
import sys

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tools.audit_common import buy_hold, exits, index_return, seed_notice  # noqa: E402
from modules import benchmark  # noqa: E402
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
_DAILY = {}         # code -> 전 구간 일봉(후보 전체 보유 대조군이 다시 쓴다 — 같은 조회를 두 번 하지 않는다)


def fit_score_at(code, day, lookback_days):
    """day 까지의 봉으로만 적합도. 이력 130봉 미만·조회 실패면 None."""
    key = (code, day)
    if key in _FIT_CACHE:
        return _FIT_CACHE[key]
    from modules.manage.discover import _fit_score
    from tools.audit_discover_fit import fit_at
    score = None
    try:
        if code not in _DAILY:
            _DAILY[code] = krx_daily.get_daily(code, lookback_days=lookback_days)
        df = _DAILY[code]
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


def chain(points):
    """기간별 (수익%, MDD%) 를 이어 붙인 (복리 수익%, MDD%).

    MDD 는 기간 안 MDD 의 최악과 기간 끝점끼리의 낙폭 중 더 나쁜 쪽이다(기간마다 새로 시작해 연속 자산곡선이
    없으므로 하한 근사 — 모든 줄이 같은 규약이라 줄끼리 견줄 수 있다).
    """
    v, peak, worst = 1.0, 1.0, 0.0
    for r, _m in points:
        v *= 1 + r / 100
        peak = max(peak, v)
        worst = min(worst, (v / peak - 1) * 100)
    return (v - 1) * 100, min([worst] + [m for _r, m in points if m is not None])


def annual_over_mdd(total_pct, mdd_pct, years):
    """연 수익% ÷ |MDD%| — 지수와 전략을 한 숫자로 견주는 잣대(benchmark-vs-buyhold 기록과 같은 정의)."""
    if years <= 0 or mdd_pct is None or abs(mdd_pct) < 1e-9 or total_pct <= -100:
        return None
    return (((1 + total_pct / 100) ** (1 / years) - 1) * 100) / abs(mdd_pct)


def _years(d0, d1):
    from datetime import datetime
    return max((datetime.strptime(d1, "%Y%m%d") - datetime.strptime(d0, "%Y%m%d")).days / 365.25, 1e-9)


def build_plan(dates, periods, args, anchor=0, verbose=True):
    """기간마다 후보·적합도 → 팔별 유니버스. [(기간, 시작일, {팔: [codes]}, 후보 전체)]"""
    plan = []
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
            #  시작점 0 의 뽑기는 종전과 같다(anchor 항이 0) — 이전 실행과 같은 무작위 팔을 잰다.
            arms[f"무작위#{si + 1}"] = [c for c, _n in _hash_draw([(c, "") for _s, c in scored], len(scored), args.k,
                                                                  args.seed + si * 1009 + pi + anchor * 100003)]
        #  후보 전체는 보유 대조군에만 쓴다 — 전략 유니버스에 넣으면 지표 준비가 300×기간으로 커진다.
        plan.append((pi, d, arms, [c for _s, c in scored]))
        if verbose:
            print(f"  기간{pi + 1} {dates[i]}~{dates[j - 1]} 후보 {len(scored)} · 상위 {', '.join(top[:5])} …", flush=True)
    return plan


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=3650)
    ap.add_argument("--months", type=int, default=12, help="재선별 주기(개월)")
    ap.add_argument("--k", type=int, default=25, help="한 기간의 관심종목 수")
    ap.add_argument("--pool", type=int, default=300, help="재선별 시점 시총 상위 후보 수")
    ap.add_argument("--seeds", type=int, default=3, help="무작위 팔의 씨드 수")
    ap.add_argument("--seed", type=int, default=20260816)
    ap.add_argument("--slots", type=int, default=None)
    ap.add_argument("--offline", action="store_true",
                    help="외부 연결 차단 — KRX Open API 로컬 저장소만 쓴다(수급 빠짐)")
    ap.add_argument("--anchors", type=int, default=1,
                    help="기간 경계를 밀어 가며 잴 시작점 수(1=종전과 같음). '전략 vs 지수' 재확인은 3~5")
    ap.add_argument("--anchor-step", type=int, default=21, help="시작점 사이 간격(거래일)")
    args = ap.parse_args()
    seed_notice(args.seeds, example="--seeds 3")
    if args.offline:
        from tools.audit_synergy_boundary import block_network
        block_network()

    slots = args.slots or getattr(config, "SYSTEM_MAX_HOLDINGS", 4)
    config.session.load_stock_config()
    live = [(s["code"], s["name"]) for s in config.session.stock_data.get("stocks_kr", [])]

    # 거래일 축은 현행풀에서 받는다(어느 종목이든 같은 달력이다)
    live_dfs, live_mf, dates = pb.prepare_universe(live, args.days)[:3]
    n_anchor = max(1, args.anchors)
    anchors = []        # (시작점 번호, 날짜 축, 기간, 계획)
    all_codes = set(live_dfs)
    for k in range(n_anchor):
        adates = dates[k * max(1, args.anchor_step):]
        periods = period_starts(adates, args.months)
        if not periods:
            break
        if k == 0:
            print(f"[준비] {adates[0]}~{adates[-1]} · {len(periods)}기간({args.months}개월) · K={args.k} · pool={args.pool} · 슬롯 {slots}")
        else:
            print(f"[준비] 시작점 {k + 1}/{n_anchor} {adates[0]}~ · {len(periods)}기간", flush=True)
        plan = build_plan(adates, periods, args, anchor=k, verbose=(k == 0))
        for _pi, _d, arms, _pool in plan:
            for v in arms.values():
                all_codes.update(v)
        anchors.append((k, adates, periods, plan))

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

    rnd_names = [f"무작위#{si + 1}" for si in range(args.seeds)]
    full_arms = ["적합도 상위", "적합도 하위"] + rnd_names + ["현행풀(hindsight)"]
    live_codes = list(live_dfs)
    bench_names = ["보유 · 적합도 상위", "보유 · 무작위 평균", "보유 · 후보 전체", "코스피", "코스닥"]
    EXPO = "코스피(전략 노출)"

    def run(codes, wdates):
        codes = [c for c in codes if c in dfs]
        if not codes:
            return None
        r = pb.run_portfolio({c: dfs[c] for c in codes}, {c: status[c] for c in codes}, wdates,
                             initial_capital=INITIAL_CAPITAL, slots=slots,
                             market_filter_dates={c: mf.get(c, set()) for c in codes},
                             risk_scale_by_date=make_scale_fn(mkt, dd))
        return r

    def evaluate(adates, periods, plan, arm_names, progress=True):
        """한 시작점을 굴린다 → 팔·대조군의 기간별 값."""
        per = {a: [] for a in arm_names}          # 기간별 (수익%, 청산수, 승률, MDD%)
        bench = {b: [] for b in bench_names}      # 기간별 (수익%, MDD%)
        expo = []                                 # 기간별 (노출 맞춘 코스피 수익%, MDD%, 평균 노출%, 필터 차단%)
        for pi, d, arms, pool_codes in plan:
            i, j = periods[pi]
            wdates = adates[i:j]
            d0, d1 = wdates[0], wdates[-1]
            bench["보유 · 적합도 상위"].append(buy_hold([dfs.get(c) for c in arms["적합도 상위"]], d0, d1, wdates))
            rnd = [buy_hold([dfs.get(c) for c in arms[n]], d0, d1, wdates) for n in rnd_names]
            rnd = [x for x in rnd if x[0] is not None]
            bench["보유 · 무작위 평균"].append((float(np.mean([x[0] for x in rnd])), float(np.mean([x[1] for x in rnd])))
                                         if rnd else (None, None))
            bench["보유 · 후보 전체"].append(buy_hold([_DAILY.get(c) for c in pool_codes], d0, d1, wdates))
            bench["코스피"].append(index_return("코스피", d0, d1))
            bench["코스닥"].append(index_return("코스닥", d0, d1))
            rng = random.Random(args.seed + pi)
            for a in arm_names:
                codes = arms.get(a, [])
                if a.startswith("현행풀"):
                    codes = rng.sample(live_codes, min(args.k, len(live_codes)))
                r = run(codes, wdates)
                if a == "적합도 상위":
                    em = benchmark.exposure_matched(benchmark.index_series("코스피", d0, d1), wdates,
                                                    r.get("cash_curve") if r else None)
                    held = [c for c in codes if c in dfs]
                    blocked = (float(np.mean([sum(1 for c in held if x in mf.get(c, ())) / len(held) for x in wdates])) * 100
                               if held else None)
                    expo.append((*em, blocked))
                if r is None:
                    per[a].append((0.0, 0, 0.0, 0.0)); continue
                sells = exits(r)
                wr = (sum(1 for t in sells if t["profit"] > 0) / len(sells) * 100) if sells else 0.0
                per[a].append((float(r["total_return"]), len(sells), wr, float(r["mdd"])))
            if progress:
                print(f"  기간{pi + 1} 완료", end="\r", flush=True)
        if progress:
            print(" " * 40, end="\r")
        return per, bench, expo

    def compound(rs):
        v = 1.0
        for r in rs:
            v *= 1 + r / 100
        return (v - 1) * 100

    # ── 시작점 1: 종전과 같은 전체 표 ────────────────────────────────────────────────
    _k0, adates, periods, plan = anchors[0]
    per, bench, expo = evaluate(adates, periods, plan, full_arms)
    results = [(adates, periods, per, bench, expo)]

    W = 110
    print(f"\n{'=' * W}\n재선별 워크포워드 — {args.months}개월마다 그 시점 데이터로만 다시 고른다 ({len(periods)}기간, K={args.k}, 후보 {args.pool})\n{'=' * W}")
    print(f"{'팔':<18}{'10y 복리%':>10}{'MDD%':>8}{'기간평균%':>10}{'기간중앙%':>10}{'승기간':>8}{'청산':>6}{'승률%':>7}  vs무작위(기간 승-패)")
    print("-" * W)
    rnd_avg = [float(np.mean([per[n][pi][0] for n in rnd_names])) for pi in range(len(periods))]
    for a in full_arms:
        rs = [x[0] for x in per[a]]
        wins = sum(1 for r in rs if r > 0)
        vs = sum(1 for pi, r in enumerate(rs) if r > rnd_avg[pi] + 1e-9)
        ls = sum(1 for pi, r in enumerate(rs) if r < rnd_avg[pi] - 1e-9)
        print(f"{a:<18}{compound(rs):>10.1f}{chain([(x[0], x[3]) for x in per[a]])[1]:>8.1f}{np.mean(rs):>10.1f}{np.median(rs):>10.1f}"
              f"{wins:>5}/{len(rs):<3}{np.mean([x[1] for x in per[a]]):>6.0f}{np.mean([x[2] for x in per[a]]):>7.1f}"
              f"  {'—' if a.startswith('무작위') else f'{vs}-{ls}'}")
    print("\n[대조군] 같은 기간 분할 — 단순 보유(동일가중·왕복 비용 차감)와 지수")
    print(f"{'대조':<18}{'10y 복리%':>10}{'MDD%':>8}{'기간평균%':>10}{'기간중앙%':>10}{'승기간':>8}   전략 상위가 이긴 기간")
    print("-" * W)
    top_rs = [x[0] for x in per["적합도 상위"]]
    expo_ok = all(x[0] is not None for x in expo) and len(expo) == len(periods)
    rows = [(b, bench[b]) for b in bench_names] + [(EXPO, [(x[0], x[1]) for x in expo])]
    for b, vals in rows:
        pts = [x for x in vals if x[0] is not None]
        if len(pts) != len(periods):
            print(f"{b:<18}  (일부 기간 데이터 없음 — {len(pts)}/{len(periods)})")
            continue
        rs = [x[0] for x in pts]
        beat = sum(1 for pi in range(len(periods)) if top_rs[pi] > rs[pi])
        print(f"{b:<18}{compound(rs):>10.1f}{chain(pts)[1]:>8.1f}{np.mean(rs):>10.1f}"
              f"{np.median(rs):>10.1f}{sum(1 for r in rs if r > 0):>5}/{len(rs):<3}   {beat}/{len(rs)}")
    print("\n기간별 수익% (적합도 상위 / 무작위 평균 / 현행풀 | 보유 상위 / 코스피):")
    for pi, (i, j) in enumerate(periods):
        _bt, _kp = bench["보유 · 적합도 상위"][pi][0], bench["코스피"][pi][0]
        print(f"  {adates[i]}~{adates[j - 1]}  {per['적합도 상위'][pi][0]:>7.1f} / {rnd_avg[pi]:>7.1f} / {per['현행풀(hindsight)'][pi][0]:>7.1f}"
              f" | {'—' if _bt is None else f'{_bt:.1f}':>7} / {'—' if _kp is None else f'{_kp:.1f}':>7}")

    # ── 지수 대비 차이를 둘로 가른다 ────────────────────────────────────────────────
    kospi_ok = all(x[0] is not None for x in bench["코스피"])
    if expo_ok and kospi_ok:
        print("\n[지수 대비 차이의 분해] 적합도 상위 팔 — 노출 몫 = 코스피(전략 노출) − 코스피 · 선별·매매 몫 = 전략 − 코스피(전략 노출)")
        print(f"{'기간':<19}{'전략%':>8}{'코스피%':>9}{'코스피(노출)%':>13}{'평균노출%':>10}{'필터차단%':>10}{'노출 몫':>10}{'선별·매매 몫':>12}")
        print("-" * W)
        for pi, (i, j) in enumerate(periods):
            s_, k_ = top_rs[pi], bench["코스피"][pi][0]
            e_, ex_, bl_ = expo[pi][0], expo[pi][2], expo[pi][3]
            print(f"{adates[i]}~{adates[j - 1]:<10}{s_:>8.1f}{k_:>9.1f}{e_:>13.1f}{ex_:>10.1f}"
                  f"{'—' if bl_ is None else f'{bl_:.1f}':>10}{e_ - k_:>10.1f}{s_ - e_:>12.1f}")
        cs, ck, ce = compound(top_rs), compound([x[0] for x in bench["코스피"]]), compound([x[0] for x in expo])
        print("-" * W)
        print(f"{'복리 합계':<19}{cs:>8.1f}{ck:>9.1f}{ce:>13.1f}{np.mean([x[2] for x in expo]):>10.1f}"
              f"{np.mean([x[3] for x in expo if x[3] is not None]):>10.1f}{ce - ck:>10.1f}{cs - ce:>12.1f}")
        print("  · 필터차단% = 그 팔의 종목·일 가운데 시장 필터가 신규 진입을 막고 있던 비율(보유분 청산과는 무관)")
        print("  · 합계 줄의 두 몫은 복리 수익끼리의 차이라 기간 줄의 합과 같지 않다")

    # ── 시작점을 밀어 가며 '전략 vs 지수'를 다시 잰다 ──────────────────────────────
    if len(anchors) > 1:
        lean_arms = ["적합도 상위"] + rnd_names
        for k, adates_k, periods_k, plan_k in anchors[1:]:
            print(f"  시작점 {k + 1}/{len(anchors)} 계산 중…", end="\r", flush=True)
            per_k, bench_k, expo_k = evaluate(adates_k, periods_k, plan_k, lean_arms, progress=False)
            results.append((adates_k, periods_k, per_k, bench_k, expo_k))
        print(" " * 40, end="\r")
        print(f"\n{'=' * W}\n시작점 이동 — 기간 경계를 {args.anchor_step}거래일씩 밀어 {len(results)}번 (적합도 상위 팔 vs 코스피)\n{'=' * W}")
        print(f"{'시작일':<10}{'기간':>4} | {'전략%':>8}{'MDD':>7}{'연/낙':>6} | {'코스피%':>8}{'MDD':>7}{'연/낙':>6} |"
              f" {'코스피(노출)%':>12}{'MDD':>7}{'노출%':>6} | {'후보보유%':>9} | {'승기간 vs지수':>12}{'vs노출':>7}")
        print("-" * W)
        tally = {"수익": 0, "연/낙": 0, "노출": 0, "n": 0}

        def _f(v, w, nd=1):
            return f"{'—':>{w}}" if v is None else f"{v:>{w}.{nd}f}"
        for adates_k, periods_k, per_k, bench_k, expo_k in results:
            yrs = _years(adates_k[periods_k[0][0]], adates_k[periods_k[-1][1] - 1])
            s_ret, s_mdd = chain([(x[0], x[3]) for x in per_k["적합도 상위"]])
            kp = bench_k["코스피"]
            hold = bench_k["보유 · 후보 전체"]
            k_ok = all(x[0] is not None for x in kp)
            e_ok = all(x[0] is not None for x in expo_k)
            k_ret, k_mdd = chain(kp) if k_ok else (None, None)
            e_ret, e_mdd = chain([(x[0], x[1]) for x in expo_k]) if e_ok else (None, None)
            h_ret = chain(hold)[0] if all(x[0] is not None for x in hold) else None
            s_m = annual_over_mdd(s_ret, s_mdd, yrs)
            k_m = annual_over_mdd(k_ret, k_mdd, yrs) if k_ok else None
            top_k = [x[0] for x in per_k["적합도 상위"]]
            w_idx = sum(1 for pi in range(len(periods_k)) if k_ok and top_k[pi] > kp[pi][0])
            w_exp = sum(1 for pi in range(len(periods_k)) if e_ok and top_k[pi] > expo_k[pi][0])
            print(f"{adates_k[periods_k[0][0]]:<10}{len(periods_k):>4} | {s_ret:>8.1f}{s_mdd:>7.1f}{_f(s_m, 6, 2)} |"
                  f" {_f(k_ret, 8)}{_f(k_mdd, 7)}{_f(k_m, 6, 2)} | {_f(e_ret, 12)}{_f(e_mdd, 7)}"
                  f"{_f(np.mean([x[2] for x in expo_k]) if e_ok else None, 6)} | {_f(h_ret, 9)} |"
                  f" {w_idx:>9}/{len(periods_k):<2}{w_exp:>4}/{len(periods_k):<2}")
            if k_ok and e_ok:
                tally["n"] += 1
                tally["수익"] += s_ret > k_ret
                tally["연/낙"] += (s_m is not None and k_m is not None and s_m > k_m)
                tally["노출"] += s_ret > e_ret
        print("-" * W)
        n = tally["n"]
        print(f"[집계] 시작점 {n}개 가운데 전략이 코스피보다 — 복리 수익 우위 {tally['수익']}/{n} · 연수익÷낙폭 우위 {tally['연/낙']}/{n}"
              f" · 같은 노출의 코스피보다 수익 우위 {tally['노출']}/{n}")
        print("  · 연/낙 = 연 수익% ÷ |MDD%|. 시작점마다 창 길이가 조금씩 다르므로 줄 안에서만(전략 vs 그 줄의 지수) 견준다.")
        print("  · 한두 시작점에서만 이기면 경계 운이다 — 전 시작점에서 같은 방향일 때만 '우위'로 읽는다.")

    print("\n" + "-" * W)
    print("[읽는 법] '적합도 상위 − 무작위'가 재선별로 실제 얻는 선별 효과(ex-ante)다. 기간 승패가 꾸준하면 '계속 갈아 끼우는"
          " 과정'이 엣지다. 현행풀은 hindsight 눈금이지 비교 대상이 아니다. 폐지 종목은 마지막 봉 종가로 청산되므로 낙관 쪽.")


if __name__ == "__main__":
    main()
