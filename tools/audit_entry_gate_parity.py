"""진입 게이트 파리티 — 백테스트의 후보 집합이 실매매와 같은가.

[왜 지금] 2026-08-12 C그룹에서 '실매매가 쓰는 순위(추세품질 1순위)가 백테스트 전제(점수순)
 보다 열위'라는 결과가 나왔다. 순위 키를 바꾸자는 제안으로 이어지는 결과인데, 그 실험은
 **후보 집합이 실매매와 같다는 전제** 위에 서 있다. 실제로는 실매매에만 있는 게이트가 셋이다.

   ① 상관관계 보류 — 보유 종목과 수익률 상관 0.7 이상이면 매수하지 않는다
      (trader._analyze_candidate_worker, USE_CORRELATION_FILTER/CORRELATION_THRESHOLD).
      후보 순위에 직접 개입한다: 1순위가 보유와 같은 흐름이면 실매매는 건너뛰고 2순위를 산다.
   ② 당일 재진입 허들 — 그날 판 종목을 다시 사려면 직전 매수 때보다 체결강도가 높아야 한다.
      백테스트에는 이 문턱이 없어, 같은 날 같은 종가에 팔고 되사는 경로가 열려 있다.
   ③ 체결강도·호가비 게이트 — 실시간 체결 데이터라 **일봉으로는 재현할 수 없다.**
      재현 불가는 숨기지 말고 크기만이라도 남긴다: 이 게이트는 후보를 더 걸러내므로
      실제 경쟁률은 여기서 측정한 값보다 낮다(순위 축의 영향은 그만큼 더 얇은 표본에 선다).

[무엇을 재는가]
  G1. 게이트 자체의 효과 — 상관관계 게이트를 켜면 성과가 어떻게 변하는가(순위는 점수순 고정).
  G2. 게이트를 켠 상태에서 순위 잣대 — C그룹의 판정이 후보 집합을 맞춘 뒤에도 유지되는가.
      여기서 뒤집히면 순위 키 교체 제안은 근거를 잃는다.
  함께 찍는 것: 상관관계로 걸린 후보-일 수, **당일 재매수 건수**(②의 크기).

[실행] python tools/audit_entry_gate_parity.py [--trials 15] [--sample 25]
"""
import argparse
import os
import random
import sys

import numpy as np
import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import config  # noqa: E402
from modules import portfolio_backtest as pb  # noqa: E402
from tools.audit_scoring_weights import (Probe, metrics, rolling_trend_quality,  # noqa: E402
                                         verify_tq_parity)
from tools.audit_common import seed_notice  # noqa: E402

INITIAL_CAPITAL = 10_000_000
# 실매매의 상관관계 창 = 차트 조회분(약 250봉)의 겹치는 구간. 최소 겹침 30일 초과도 같다.
CORR_WINDOW = 250
CORR_MIN = 31


class CorrGate:
    """실매매의 상관관계 보류를 백테스트에서 재현한다.

    실매매는 후보×보유 조합마다 그 자리에서 상관을 구하지만, 백테스트는 2,338일 × 15시행을
    돌아야 하므로 종목쌍별 롤링 상관을 **한 번만** 깔고 날짜로 찾아 쓴다. 같은 창(250일)·
    같은 문턱·같은 최소 겹침을 쓰므로 판정은 동일하다. 다른 그룹(ETF 등)끼리는 비교하지
    않는 실매매 규칙은 이 유니버스가 전부 국내주식이라 자동으로 만족된다.
    """

    def __init__(self, dfs, dates, threshold):
        self.threshold = threshold
        self.idx = {d: i for i, d in enumerate(dates)}
        close = pd.DataFrame({c: pd.Series(
            {str(r["date"]): float(r["close"]) for r in df.to_dict("records")})
            for c, df in dfs.items()}).reindex(dates)
        ret = close.pct_change()
        codes = list(dfs.keys())
        self.blocked = {}
        for i, a in enumerate(codes):
            for b in codes[i + 1:]:
                corr = ret[a].rolling(CORR_WINDOW, min_periods=CORR_MIN).corr(ret[b])
                arr = (corr >= threshold).to_numpy()
                self.blocked[(a, b)] = arr
                self.blocked[(b, a)] = arr
        self.skips = 0

    def __call__(self, day, code, held):
        if not held:
            return False
        i = self.idx.get(day)
        if i is None:
            return False
        for h in held:
            arr = self.blocked.get((code, h))
            if arr is not None and arr[i]:
                self.skips += 1
                return True
        return False


def same_day_rebuys(trades):
    """같은 날 팔고 되산 건수 — 실매매의 '당일 재진입 허들'이 막는 경로다."""
    sold, bought = {}, {}
    for t in trades:
        key = (t["code"], t["date"])
        if t["reason"] == "매수":
            bought[key] = True
        elif not t["reason"].startswith("피라미딩"):
            sold[key] = True
    return sum(1 for k in bought if k in sold)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--trials", type=int, default=15)
    ap.add_argument("--sample", type=int, default=25)
    ap.add_argument("--days", type=int, default=3650)
    ap.add_argument("--slots", type=int, default=None)
    ap.add_argument("--seed", type=int, default=20260811)
    ap.add_argument("--subperiods", type=int, default=4)
    ap.add_argument("--exclude-from", default="20260301")
    args = ap.parse_args()
    seed_notice(1)

    slots = args.slots or getattr(config, "SYSTEM_MAX_HOLDINGS", 4)
    config.session.load_stock_config()
    targets = [(s["code"], s["name"]) for s in config.session.stock_data.get("stocks_kr", [])]
    print(f"[준비] 관심종목 {len(targets)}개 · {args.days}일 · 슬롯 {slots}")

    dfs, mf, dates, failed = pb.prepare_universe(targets, args.days)
    print(f"[준비] 사용 {len(dfs)}종목 / 거래일 {len(dates)}일"
          + (f" · 제외 {len(failed)}" if failed else ""))

    thr = {
        "BUY_SCORE": config.ANALYSIS_THRESHOLDS["BUY_SCORE"],
        "BUY_RSI_MAX": config.ANALYSIS_THRESHOLDS["BUY_RSI_MAX"],
        "RISE_SCORE": config.ANALYSIS_THRESHOLDS["RISE_SCORE"],
        "WEIGHTS": config.SCORING_WEIGHTS,
    }
    status = pb.precompute_status(dfs, thr)

    lookback = int(config.INDICATOR_PARAMS.get("TREND_QUALITY_LOOKBACK", 90))
    tq_by_code = {c: rolling_trend_quality(df, lookback) for c, df in dfs.items()}
    bad = verify_tq_parity(dfs, tq_by_code, lookback)
    print(f"[검증] 추세품질 산식 대조: 불일치 {bad}건" + ("  ← C그룹 결과를 쓰면 안 된다." if bad else ""))

    corr_thr = float(getattr(config, "CORRELATION_THRESHOLD", 0.7))
    use_corr = bool(getattr(config, "USE_CORRELATION_FILTER", True))
    print(f"[게이트] 상관관계 필터 {'ON' if use_corr else 'OFF'} · 문턱 {corr_thr} · 창 {CORR_WINDOW}일")

    from tools.audit_drawdown_axis import market_scale_by_date, make_scale_fn
    p = getattr(config, "RISK_SCALING_PARAMS", {}) or {}
    mkt = market_scale_by_date(dates, args.days)
    dd = None
    if p.get("USE_DRAWDOWN_RISK_SCALING", True):
        dd = (int(p.get("DD_LOOKBACK_DAYS", 90)), float(p.get("DD_LEVEL_1", 5.0)),
              float(p.get("DD_SCALE_1", 0.9)), float(p.get("DD_LEVEL_2", 10.0)),
              float(p.get("DD_SCALE_2", 0.8)))
    # [실행마다 새로 만든다 — 2026-08-13] make_scale_fn 이 돌려주는 콜러블은 내부에 자산곡선
    #  이력(hist)을 들고 있다. 하나를 만들어 모든 팔·시행에 돌려쓰면 앞선 실행의 자산곡선이
    #  남아 드로다운 판정이 오염되고, 팔의 실행 순서에 따라 결과가 달라진다(같은 설정을 두 번
    #  돌리면 392.66% → 284.79%). 짝비교의 전제가 깨지므로 팩토리로 두고 매번 새로 만든다.
    new_scale_fn = lambda: make_scale_fn(mkt, dd)  # noqa: E731

    cut = "".join(ch for ch in str(args.exclude_from) if ch.isdigit())
    head = [d for d in dates if not cut or "".join(filter(str.isdigit, d)) < cut]
    tail = [d for d in dates if cut and "".join(filter(str.isdigit, d)) >= cut]
    # 추세품질 워밍업(90일) 구간은 잘라낸다 — 앞부분은 모든 후보가 이력부족이라 순위가
    #  등록 순서로 정해져 비교가 오염된다(C그룹과 같은 규약).
    head = head[lookback - 1:]
    print(f"[창] 검증 {len(head)}일 · 제외 {len(tail)}일")

    NEG = float("-inf")

    def tq(code, day):
        v = tq_by_code.get(code, {}).get(day)
        return NEG if v is None else v

    rank_score = None
    def rank_live(s, c, r, d):        # noqa: E306  실매매 순위 (추세품질 1순위)
        return (tq(c, d), s, float(r.get("w52_pos", 0.0) or 0.0))

    def rank_tie(s, c, r, d):         # noqa: E306  점수 1순위 + 동점만 추세품질
        return (s, tq(c, d))

    # (그룹, 라벨, 게이트 사용, rank_fn)
    sets = [
        ("G1. 상관관계 게이트 자체 (순위=점수순 고정)", "게이트 OFF (현행 백테스트)", False, rank_score),
        ("G1. 상관관계 게이트 자체 (순위=점수순 고정)", "게이트 ON (실매매)", True, rank_score),
        ("G2. 게이트 ON 상태의 순위 잣대", "점수순", True, rank_score),
        ("G2. 게이트 ON 상태의 순위 잣대", "추세품질→점수 (실매매)", True, rank_live),
        ("G2. 게이트 ON 상태의 순위 잣대", "점수→추세품질(동점만)", True, rank_tie),
    ]

    codes = list(dfs.keys())
    k = max(1, args.subperiods)
    size = max(1, len(head) // k)
    windows = [("제외 전 전체", head)]
    if k > 1:
        windows += [(f"구간{i + 1}", head[i * size:(i + 1) * size if i < k - 1 else len(head)])
                    for i in range(k)]
    if tail:
        windows.append(("[대조] 제외구간(고변동)", tail))

    all_results = {}
    for wname, wdates in windows:
        results = {(g, l): [] for g, l, _u, _f in sets}
        rng = random.Random(args.seed)
        for t in range(args.trials):
            pick = rng.sample(codes, min(args.sample, len(codes)))
            sd = {c: dfs[c] for c in pick}
            sm = {c: mf.get(c, set()) for c in pick}
            st = {c: status[c] for c in pick}
            gate_src = CorrGate(sd, dates, corr_thr)
            for g, label, use_gate, fn in sets:
                gate = None
                if use_gate:
                    gate_src.skips = 0
                    gate = gate_src
                probe = Probe(fn)
                r = pb.run_portfolio(sd, st, wdates, initial_capital=INITIAL_CAPITAL,
                                     slots=slots, market_filter_dates=sm,
                                     risk_scale_by_date=new_scale_fn(), rank_fn=fn,
                                     probe_fn=probe, entry_gate=gate)
                cm, cc, tie = probe.stats()
                m = metrics(r, cm, cc, tie)
                m["skips"] = gate_src.skips if use_gate else 0
                m["rebuy"] = same_day_rebuys(r["trades"])
                results[(g, label)].append(m)
            print(f"  {wname} 시행 {t + 1}/{args.trials}   ", end="\r", flush=True)
        all_results[wname] = results
    print(" " * 60, end="\r")

    W = 112
    print(f"\n{'=' * W}")
    print(f"진입 게이트 파리티 ({args.trials}회 × {args.sample}종목 짝비교)")
    print(f"{'=' * W}")
    for g in dict.fromkeys(x[0] for x in sets):
        members = [x for x in sets if x[0] == g]
        base_label = members[0][1]
        print(f"\n\n{'#' * W}\n{g}  (기준선: {base_label})\n{'#' * W}")
        for wname, wdates in windows:
            results = all_results[wname]
            base = results[(g, base_label)]
            print(f"\n########## {wname} ({len(wdates)} 거래일) ##########")
            print(f"{'설정':<24}{'수익%':>9}{'MDD%':>8}{'MAR':>7}{'PF':>6}{'청산':>6}"
                  f"{'상위10%':>9}{'최대':>9}{'>30%':>6}{'후보':>6}{'경쟁%':>7}{'동점%':>7}"
                  f"{'차단':>7}{'당일재매수':>10}{'승-무-패':>10}{'MAR승':>7}{'꼬리승':>7}")
            print("-" * W)
            for _g, label, _u, _f in members:
                rs = results[(g, label)]
                m = lambda key: float(np.median([x[key] for x in rs]))  # noqa: E731
                is_base = label == base_label
                tie = sum(1 for a, b in zip(rs, base) if abs(a["ret"] - b["ret"]) < 1e-9)
                los = sum(1 for a, b in zip(rs, base) if a["ret"] < b["ret"] - 1e-9)
                rw = sum(1 for a, b in zip(rs, base) if a["ret"] > b["ret"])
                mw = sum(1 for a, b in zip(rs, base) if a["mar"] > b["mar"])
                tw = sum(1 for a, b in zip(rs, base) if a["top10"] > b["top10"])
                print(f"{label:<24}{m('ret'):>9.1f}{m('mdd'):>8.1f}{m('mar'):>7.2f}{m('pf'):>6.2f}"
                      f"{m('n'):>6.0f}{m('top10'):>9.1f}{m('best'):>9.1f}{m('big'):>6.0f}"
                      f"{m('cand'):>6.1f}{m('comp'):>7.1f}{m('tie'):>7.1f}"
                      f"{m('skips'):>7.0f}{m('rebuy'):>10.0f}"
                      f"{'—' if is_base else f'{rw}-{tie}-{los}':>8}"
                      f"{'—' if is_base else f'{mw}/{len(rs)}':>7}"
                      f"{'—' if is_base else f'{tw}/{len(rs)}':>7}")

    print("\n" + "-" * W)
    print("차단 = 상관관계로 후보에서 빠진 (종목, 일) 건수 · 당일재매수 = 같은 날 팔고 되산 건수.")
    print("[한계] 체결강도·호가비 게이트는 실시간 데이터라 일봉으로 재현할 수 없다 —")
    print("       실매매의 후보는 여기서보다 더 좁고, 따라서 실제 경쟁률은 측정치보다 낮다.")


if __name__ == "__main__":
    main()
