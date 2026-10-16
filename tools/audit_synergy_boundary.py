"""시너지 경계(2점 급변) 감사 — 경계가 진입을 얼마나 가르는가, 부드럽게 하면 나아지는가.

[질문 · 지시서 5-4] 시너지 두 항목(각 1점)은 'MACD 확산 유지'(macd_hist ≥ prev × 0.8, 둘 다
 양수)를 공유한다. 그래서 0.8000 → 7.5점, 0.7999 → 5.5점처럼 경계 하나가 2점을 함께 옮긴다.
   A. 신호 단위: 경계 근처(비율 0.7~0.9) 날이 얼마나 되고, 그중 경계가 BUY_SCORE 를 갈라
      **상태 분류까지 바꾼** 날(매수 상태를 들인 날·뺀 날)은 얼마나 되나. 하루 사이 확산이
      뒤집힌 횟수(경계 흔들림)도 센다.
   B. 포트폴리오 짝비교: 기준선 vs 후보 —
      · 단계 가점: 비율 0.7~0.9 구간에서 시너지 점수를 선형으로(0.7=0, 0.9=만점)
      · 히스테리시스: 켜는 문턱 0.8, 끄는 문턱 0.7(전일 상태는 **일봉 이력에서** 다시 계산되므로
        재시작·백테스트에서 같은 상태 전이가 재현된다 — 저장 상태가 없다)
 만점·BUY_SCORE·다른 항목은 그대로다(시너지 슬롯 2점 안에서만 바꾼다 — 문턱 완화 혼입 없음).

[판정 규약] 이 도구는 표를 낼 뿐이다. 채택은 기존 규칙으로 한다 — 하위 구간 짝비교 승수
 31/60 이상 · 표본 씨드 5개에서 유지 · MDD 상대 10% 악화 + 꼬리 축소면 기각
 ([[audit-seed-robustness]] [[adoption-rule-drawdown-veto]]). 점수가 부드러워졌다는 것만으로는
 채택하지 않는다(진입 지연·큰 수익 기회 누락이 함께 재진다 — 꼬리·최대·>30% 칸).

[데이터] 기본은 **오프라인**: 외부 연결을 막고 KRX Open API 로컬 저장소(data/krx_openapi.db)만 쓴다.
 표본 유니버스도 그 저장소에서 시점 기준으로 뽑는다(시작일 시가총액 상위 풀 → 씨드 표본,
 이후 폐지 종목 포함). 수급(KIS)은 오프라인이면 빠진다 — 실행 기록(data/audit_runs/)의
 data_sources 에 그 사실이 남는다. 실매매와 같은 데이터가 아니므로 결론에 함께 적을 것.

[실행] python tools/audit_synergy_boundary.py [--pool-seeds 1,2,3,4,5] [--stocks 30] [--trials 10]
"""
import argparse
import os
import random
import sqlite3
import sys

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import config  # noqa: E402
from modules import analysis, backtest, score_model  # noqa: E402
from modules import portfolio_backtest as pb  # noqa: E402
from tools.audit_common import base_metrics, exits, seed_notice  # noqa: E402
from tools.audit_common import windows as audit_windows  # noqa: E402
from modules import run_manifest  # noqa: E402

INITIAL_CAPITAL = 10_000_000
SYN_IDS = ("synergy.trend_start", "synergy.momentum_thrust")
LOW, HIGH = 0.7, 0.9


# ── 오프라인 · 시점 기준 표본 ─────────────────────────────────────────────────────
def block_network():
    """외부 연결을 막는다(로컬 저장소·캐시만). 이름 해석만 남고 접속은 즉시 실패한다."""
    import socket

    def _blocked(self, addr, *a, **k):
        raise OSError(f"오프라인 감사 — 외부 연결 차단: {addr}")
    socket.socket.connect = _blocked
    #  저장소 읽기는 키 '존재'만 본다(받기는 위에서 실패한다). 운영 키를 쓰지 않는다.
    os.environ.setdefault("KRX_OPENAPI_KEY", "offline-audit")
    #  평가 종료일 = 저장소의 마지막 확정일. 그 뒤 영업일이 비어 있으면 저장소는 '구간 불완전'으로
    #   일봉을 거절한다(잘린 시계열로 검증하지 않는 규약). 오프라인에서는 그 날짜까지로 못박는다.
    from modules import krx_openapi
    c = sqlite3.connect(f"file:{krx_openapi._db_path()}?mode=ro", uri=True)
    try:
        #  종목 일봉 API 들의 '공통' 마지막 확정일 — 지수 API 가 더 최근까지 있어도 종목 쪽이 기준이다
        q = ",".join("?" * len(krx_openapi.STOCK_APIS))
        last = c.execute(f"SELECT MIN(m) FROM (SELECT MAX(bas_dd) AS m FROM fetched WHERE n > 0 "
                         f"AND api_id IN ({q}) GROUP BY api_id)", krx_openapi.STOCK_APIS).fetchone()[0]
    finally:
        c.close()
    krx_openapi.latest_available_dd = lambda now=None: last
    #  수급(KIS)은 오프라인에서 받을 수 없다 — 재시도 대기(종목당 ~16초)를 건너뛰고 '없음'으로 둔다.
    #   출처 표에는 종전과 같이 None 으로 남아 실행 기록·준비 안내가 그 사실을 알린다.
    def _no_smart_money(df, code, is_overseas, lag=None):
        df["smart_money"] = False
        backtest._SMART_MONEY_SOURCE[str(code)] = None
        return df
    backtest._append_smart_money_signal = _no_smart_money
    run_manifest.note("offline_end_dd", last)
    print(f"[오프라인] 외부 연결 차단 · 평가 종료일 = 저장소 마지막 확정일 {last}", flush=True)
    return last


def pit_targets(days, pool, stocks, seed, store=None):
    """시작일 시가총액 상위 pool 개(코스피·코스닥 보통주)에서 seed 로 stocks 개를 뽑는다."""
    from datetime import datetime, timedelta
    store = store or os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                                  "data", "krx_openapi.db")
    start = (datetime.now() - timedelta(days=days)).strftime("%Y%m%d")
    c = sqlite3.connect(f"file:{store}?mode=ro", uri=True)
    try:
        dd = c.execute("SELECT MIN(bas_dd) FROM stock_daily WHERE bas_dd >= ?", (start,)).fetchone()[0]
        rows = c.execute(
            "SELECT code, name FROM stock_daily WHERE bas_dd=? AND market IN ('KOSPI','KOSDAQ') "
            "ORDER BY marcap DESC", (dd,)).fetchall()
    finally:
        c.close()
    common = [(code, name) for code, name in rows
              if code[-1] == "0" and "스팩" not in str(name)
              and not str(name).endswith(("우", "우B", "우C", "(전환)"))][:pool]
    rng = random.Random(seed)
    return rng.sample(common, min(stocks, len(common))), dd


# ── 신호 단위 계측 ───────────────────────────────────────────────────────────────
def signal_census(dfs, thr):
    buy = thr["BUY_SCORE"]
    buyish = ("매수", "강매수", "역매수")
    out = dict(days=0, near=0, cross_near=0, admitted=0, excluded=0, gained_buy=0, lost_buy=0,
               flips=0, flips_near=0, fail_open=0)
    for _code, df in dfs.items():
        prev_row, prev_exp = None, None
        for row in df.to_dict("records"):
            kw = backtest.score_inputs(row, prev_row, thr)
            res = analysis.score_breakdown(**kw)
            ev = score_model.boundary_event(res, buy)
            out["days"] += 1
            out["near"] += ev["near"]
            out["fail_open"] += ev["fail_open"]
            if prev_exp is not None and prev_exp != ev["expanding"]:
                out["flips"] += 1
                out["flips_near"] += ev["near"]
            prev_exp = ev["expanding"]
            if ev["cross"] and ev["near"]:
                out["cross_near"] += 1
                out[ev["cross"]] += 1
                kw.pop("weights")
                prev_rsi = prev_row["RSI"] if prev_row is not None else None
                st = analysis.classify_stock_state(prev_rsi=prev_rsi, thresholds=thr, score=res.total,
                                                   is_yangbong=row["close"] > row["open"], **kw)[0]
                cf = analysis.classify_stock_state(prev_rsi=prev_rsi, thresholds=thr, score=ev["cf_score"],
                                                   is_yangbong=row["close"] > row["open"], **kw)[0]
                if cf in buyish and st not in buyish:
                    out["gained_buy"] += 1
                if st in buyish and cf not in buyish:
                    out["lost_buy"] += 1
            prev_row = row
    return out


# ── 팔(arm) — calculate_score 를 감싸 시너지 슬롯 2점 안에서만 바꾼다 ────────────────────
_CTX = {"code": None, "prev_on": {}}


def _ratio(res):
    dg = res.diagnostics
    h, ph = dg.get("macd_hist"), dg.get("prev_macd_hist")
    if score_model.is_missing(h) or score_model.is_missing(ph) or not (h > 0 and ph > 0):
        return None
    return float(h) / float(ph)


def synergy_factor(arm, res, prev_on):
    """시너지 슬롯 배수(0~1)와 이 날의 '켜짐' 상태. 기준선은 확산 조건 그대로다."""
    exp = bool(res.diagnostics.get("macd_expanding"))
    r = _ratio(res)
    if arm == "baseline" or r is None:
        return (1.0 if exp else 0.0), exp
    if arm == "graded":
        f = min(1.0, max(0.0, (r - LOW) / (HIGH - LOW)))
        return f, f > 0
    if arm == "hysteresis":
        on = r >= score_model.MACD_EXPAND_RATIO or (bool(prev_on) and r >= LOW)
        return (1.0 if on else 0.0), on
    raise ValueError(arm)


def make_patches(orig_status, arm):
    def patched_status(row, prev, thresholds=None):
        return orig_status(row, prev, thresholds=thresholds)

    breakdown = analysis.score_breakdown

    def patched_score(*a, **kw):
        res = breakdown(*a, **kw)
        code = _CTX["code"]
        prev_on = _CTX["prev_on"].get(code)
        f, on = synergy_factor(arm, res, prev_on)
        _CTX["prev_on"][code] = on
        current = sum(res.contribution(i) for i in SYN_IDS)
        stake = float(res.diagnostics.get("synergy_stake") or 0.0)
        raw = res.raw_total - current + stake * f
        return round(max(raw, 0.0), 2), res.details

    return patched_status, patched_score


def precompute(dfs, thr, arm):
    """종목·일자 순서로 상태를 계산한다(히스테리시스의 전일 상태가 그 순서에 기댄다)."""
    orig_status, orig_score = backtest.calculate_daily_status, analysis.calculate_score
    _, ps = make_patches(orig_status, arm)
    out = {}
    try:
        analysis.calculate_score = ps
        for code, df in dfs.items():
            _CTX["code"] = code
            _CTX["prev_on"].pop(code, None)
            status, prev = {}, None
            for row in df.to_dict("records"):
                try:
                    status[str(row["date"])] = backtest.calculate_daily_status(row, prev, thresholds=thr)
                except Exception:   # noqa: BLE001  (precompute_status 와 같은 규약)
                    status[str(row["date"])] = (0.0, 0.0, False, "-", "데이터 부족")
                prev = row
            out[code] = status
    finally:
        analysis.calculate_score = orig_score
    return out


def metrics(r):
    m = base_metrics(r)
    sells = exits(r)
    profits = sorted((t["profit"] for t in sells), reverse=True)
    m["best"] = profits[0] if profits else 0.0
    m["big"] = sum(1 for p in profits if p >= 30)
    m["n"] = len(sells)
    return m


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=3650)
    ap.add_argument("--pool", type=int, default=400)
    ap.add_argument("--stocks", type=int, default=30)
    ap.add_argument("--pool-seeds", default="20260817,31,777,4242,9001")
    ap.add_argument("--trials", type=int, default=10)
    ap.add_argument("--sample", type=int, default=20)
    ap.add_argument("--seed", type=int, default=20261016)
    ap.add_argument("--subperiods", type=int, default=4)
    ap.add_argument("--online", action="store_true", help="외부 조회 허용(기본은 오프라인)")
    args = ap.parse_args()
    seed_notice(len([x for x in args.pool_seeds.split(",") if x.strip()]), flag="--pool-seeds")
    if not args.online:
        block_network()
    seeds = [int(x) for x in args.pool_seeds.split(",") if x.strip()]
    slots = getattr(config, "SYSTEM_MAX_HOLDINGS", 4)
    thr = {"BUY_SCORE": config.ANALYSIS_THRESHOLDS["BUY_SCORE"],
           "BUY_RSI_MAX": config.ANALYSIS_THRESHOLDS["BUY_RSI_MAX"],
           "RISE_SCORE": config.ANALYSIS_THRESHOLDS["RISE_SCORE"], "WEIGHTS": config.SCORING_WEIGHTS}
    arms = ("baseline", "graded", "hysteresis")
    summary = {}
    for seed in seeds:
        targets, start_dd = pit_targets(args.days, args.pool, args.stocks, seed)
        run_manifest.note_universe(seed, targets)
        dfs, mf, dates, failed = pb.prepare_universe(targets, args.days)
        print(f"\n===== 표본 씨드 {seed} · 시작일 {start_dd} · 사용 {len(dfs)}/{len(targets)}종목 · "
              f"거래일 {len(dates)} =====", flush=True)
        if len(dfs) < max(5, args.sample // 2):
            print("  표본이 너무 적어 건너뜀")
            continue
        c = signal_census(dfs, thr)
        print(f"  [신호] 종목·일 {c['days']:,} · 경계 근처 {c['near']:,}({c['near'] / c['days'] * 100:.2f}%) · "
              f"근처에서 문턱 교차 {c['cross_near']:,} (들임 {c['admitted']}·뺌 {c['excluded']}) · "
              f"상태가 매수로 바뀜 {c['gained_buy']} / 매수에서 빠짐 {c['lost_buy']} · "
              f"확산 뒤집힘 {c['flips']:,}(근처 {c['flips_near']:,}) · fail-open {c['fail_open']}", flush=True)
        # 기준선 무결성: 패치 경로의 기준선 == 원본
        st_orig = pb.precompute_status(dfs, thr)
        st_base = precompute(dfs, thr, "baseline")
        same = all(st_orig[cd] == st_base[cd] for cd in dfs)
        print(f"  [무결성] 패치 경로 기준선이 원본과 {'같다' if same else '**다르다 — 결과 무효**'}", flush=True)
        if not same:
            continue
        status = {"baseline": st_orig, "graded": precompute(dfs, thr, "graded"),
                  "hysteresis": precompute(dfs, thr, "hysteresis")}
        k = max(1, args.subperiods)
        windows = audit_windows(dates, k, whole=True)       # 구간 분할은 audit_common 하나
        codes = list(dfs)
        res = {a: {w: [] for w, _ in windows} for a in arms}
        for wname, wdates in windows:
            rng = random.Random(args.seed)
            for _t in range(args.trials):
                pick = rng.sample(codes, min(args.sample, len(codes)))
                for a in arms:
                    r = pb.run_portfolio({cd: dfs[cd] for cd in pick}, {cd: status[a][cd] for cd in pick},
                                         wdates, initial_capital=INITIAL_CAPITAL, slots=slots,
                                         market_filter_dates={cd: mf.get(cd, set()) for cd in pick})
                    res[a][wname].append(metrics(r))
        print(f"  {'팔':<12}{'수익%':>9}{'MDD%':>8}{'상위10%':>9}{'최대':>8}{'>30%':>6}{'거래':>6}"
              f"{'전체 승-무-패':>14}{'하위구간 승':>12}{'MDD 악화':>10}")
        for a in arms:
            m = res[a]["전체"]
            line = (f"  {a:<12}{np.median([x['ret'] for x in m]):>9.1f}{np.median([x['mdd'] for x in m]):>8.1f}"
                    f"{np.median([x['top10'] for x in m]):>9.1f}{np.median([x['best'] for x in m]):>8.1f}"
                    f"{np.median([x['big'] for x in m]):>6.0f}{np.median([x['n'] for x in m]):>6.0f}")
            if a == "baseline":
                print(line + f"{'—':>14}{'—':>12}{'—':>10}")
                continue
            b = res["baseline"]

            def tally(w):
                x, y = res[a][w], b[w]
                win = sum(1 for p, q in zip(x, y) if p["ret"] > q["ret"] + 1e-9)
                tie = sum(1 for p, q in zip(x, y) if abs(p["ret"] - q["ret"]) <= 1e-9)
                return win, tie, len(x) - win - tie
            aw, at, al = tally("전체")
            sub = sum(tally(f"구간{i + 1}")[0] for i in range(k))
            mdd_worse = np.median([abs(p["mdd"]) for p in m]) > 1.1 * np.median([abs(q["mdd"]) for q in b["전체"]])
            print(line + f"{f'{aw}-{at}-{al}':>14}{f'{sub}/{k * args.trials}':>12}{('예' if mdd_worse else '아니오'):>10}")
            summary.setdefault(a, []).append({"seed": seed, "all": (aw, at, al), "sub": (sub, k * args.trials),
                                              "mdd_worse": bool(mdd_worse)})
        summary.setdefault("_census", []).append({"seed": seed, **c})
    run_manifest.note("summary", summary)
    n_sub = max(1, args.subperiods) * args.trials
    need = -(-31 * n_sub // 60)                                  # 31/60 비율을 이번 표본 수에 맞춘다
    print(f"\n[판정 규약] 하위 구간 승 {need}/{n_sub} 이상(31/60 비율)이 5씨드에서 유지되고 "
          "MDD 거부에 걸리지 않을 때만 채택 검토.")
    print("            무승부가 대부분이면 그 경계는 이 표본에서 슬롯 주인을 바꾸지 못한다는 뜻이다.")


if __name__ == "__main__":
    main()
