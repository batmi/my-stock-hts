"""피라미딩 하루 다회 vs 하루 1회 — 같은 날 3차까지 얹는 것이 유리한가.

[왜] 실매매(_try_pyramid_buy)는 감시 주기마다 실시간가로 판정하고 같은 날의 증액을 막는
 장치가 없다. 증액하면 평단이 오르지만 트리거는 +10%뿐이라, 하루종일 상한가로 밀려
 올라가는 종목은 하루 만에 1 → 2 → 3차가 모두 나갈 수 있다(평단 기준 +10.0% → +13.7%
 → +17.5%면 3차까지 도달한다). 반면 일봉 백테스트는 하루 한 번밖에 낼 수 없어서
 **지금까지의 모든 피라미딩 근거(3차 확정 포함)는 '하루 1회' 세계에서 측정된 것**이다.
 즉 실매매가 실제로 하는 일은 검증된 적이 없다.

[무엇을 보는가] 다회 증액은 두 방향으로 작동한다.
   (+) 급등 초입에서 비중을 빨리 키운다 = fat-tail 강화
   (-) 같은 날 위로 쫓아 사므로 평단이 급히 올라간다 = 되돌림에 약해진다
   (-) 하루에 현금을 다 써서 다른 종목 신규 진입을 버린다
 그래서 총수익만이 아니라 상위10%·최대·>30%(꼬리), MDD·PF(질), 증액 건수와
 '같은 날 2회 이상' 발생 빈도를 함께 본다.

[팔 구성] 체결가 모델과 하루 횟수는 별개 축이라 섞이면 안 된다. B(하루1회·장중)를
 끼워 '장중 체결로 바꾼 효과'와 '다회로 바꾼 효과'를 분리한다.
   A. 하루1회·종가  — 종전 백테스트 모델(과거 근거의 출처)
   B. 하루1회·장중  — 제안: 하루 한 번으로 제한했을 때
   C. 하루2회·장중  — 중간값
   D. 무제한·장중   — 현 실매매 (기준선)

[한계] 판정에 쓰는 state는 그날 종가로 계산된 값이라 장중 체결에는 앞을 본다.
 네 팔이 같은 편향을 공유하므로 짝비교는 성립하지만 절대 수치는 낙관 쪽이다.
 --fill-cap 을 주면 전일 종가 대비 그 %를 넘는 체결을 '호가 공백(상한가)'으로 보고
 버린다 — 낙관 편향을 걷어낸 대조 실행에 쓴다.

[실행] python3 tools/audit_pyramid_perday.py --days 3650 --trials 15 --sample 25 --subperiods 4
      python3 tools/audit_pyramid_perday.py --fill-cap 25   (상한가 미체결 가정)
"""
import argparse
import os
import random
import sys
from collections import Counter

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tools.audit_common import exits, seed_notice  # noqa: E402

import config  # noqa: E402
from modules import portfolio_backtest as pb  # noqa: E402

INITIAL_CAPITAL = 10_000_000
BASE = "D. 무제한·장중"


def arms(fill_cap):
    """(라벨, run_portfolio 추가 인자)."""
    cap = {"pyr_fill_cap": fill_cap} if fill_cap else {}
    return [
        ("A. 하루1회·종가", {"pyr_intraday": False, "pyr_per_day": 1}),
        ("B. 하루1회·장중", {"pyr_intraday": True, "pyr_per_day": 1, **cap}),
        ("C. 하루2회·장중", {"pyr_intraday": True, "pyr_per_day": 2, **cap}),
        (BASE,              {"pyr_intraday": True, "pyr_per_day": 0, **cap}),
    ]


def metrics(r):
    sells = exits(r)
    pyr = [t for t in r["trades"] if str(t["reason"]).startswith("피라미딩")]
    profits = sorted((t["profit"] for t in sells), reverse=True)
    top10 = profits[:max(1, len(profits) // 10)]
    # 같은 날 2회 이상 얹은 (종목, 날짜) 수 — 이 축이 실제로 발동하는지의 지표.
    per_day = Counter((t["code"], t["date"]) for t in pyr)
    multi = sum(1 for v in per_day.values() if v >= 2)
    burst3 = sum(1 for v in per_day.values() if v >= 3)
    return {
        "ret": r["total_return"], "mdd": r["mdd"],
        "mar": r["total_return"] / abs(r["mdd"]) if r["mdd"] else float("nan"),
        "pf": r["pf"],
        "pyr_n": len(pyr),
        "third": sum(1 for t in pyr if t["reason"] == "피라미딩3차"),
        "multi": multi,
        "burst3": burst3,
        "top10": float(np.mean(top10)) if top10 else 0.0,
        "best": profits[0] if profits else 0.0,
        "big": sum(1 for p in profits if p >= 30),
        "days": float(np.median([t["days"] for t in sells])) if sells else 0.0,
        "n": len(sells),
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--trials", type=int, default=15)
    ap.add_argument("--sample", type=int, default=25)
    ap.add_argument("--days", type=int, default=3650)
    ap.add_argument("--slots", type=int, default=None)
    ap.add_argument("--seed-capital", type=int, default=INITIAL_CAPITAL)
    ap.add_argument("--seed", type=int, default=20260816)
    ap.add_argument("--subperiods", type=int, default=4)
    ap.add_argument("--fill-cap", type=float, default=None,
                    help="전일 종가 대비 이 %%를 넘는 장중 체결은 불가로 본다(상한가 호가 공백)")
    ap.add_argument("--exclude-from", default="20260301")
    args = ap.parse_args()
    seed_notice(1)

    slots = args.slots or getattr(config, "SYSTEM_MAX_HOLDINGS", 4)
    config.session.load_stock_config()
    targets = [(s["code"], s["name"]) for s in config.session.stock_data.get("stocks_kr", [])]
    print(f"[준비] 관심종목 {len(targets)}개 · {args.days}일 · 슬롯 {slots}")
    print(f"[기준] 증액 트리거 +{config.ANALYSIS_THRESHOLDS.get('PYRAMIDING_PROFIT_TRIGGER')}% · "
          f"비율 {config.ANALYSIS_THRESHOLDS.get('PYRAMIDING_RATIO')} · "
          f"최대 {config.ANALYSIS_THRESHOLDS.get('PYRAMIDING_MAX_COUNT')}차"
          + (f" · 체결상한 전일比 +{args.fill_cap}%" if args.fill_cap else ""))

    dfs, mf, dates, failed = pb.prepare_universe(targets, args.days)
    thresholds = {
        "BUY_SCORE": config.ANALYSIS_THRESHOLDS["BUY_SCORE"],
        "BUY_RSI_MAX": config.ANALYSIS_THRESHOLDS["BUY_RSI_MAX"],
        "RISE_SCORE": config.ANALYSIS_THRESHOLDS["RISE_SCORE"],
        "WEIGHTS": config.SCORING_WEIGHTS,
    }
    status = pb.precompute_status(dfs, thresholds)
    print(f"[준비] 사용 {len(dfs)}종목 / 거래일 {len(dates)}일")

    from tools.audit_drawdown_axis import market_scale_by_date, make_scale_fn
    p = getattr(config, "RISK_SCALING_PARAMS", {}) or {}
    mkt = market_scale_by_date(dates, args.days)
    dd = None
    if p.get("USE_DRAWDOWN_RISK_SCALING", True):
        dd = (int(p.get("DD_LOOKBACK_DAYS", 90)), float(p.get("DD_LEVEL_1", 5.0)),
              float(p.get("DD_SCALE_1", 0.9)), float(p.get("DD_LEVEL_2", 10.0)),
              float(p.get("DD_SCALE_2", 0.8)))
    # [실행마다 새로 만든다] 콜러블이 자산곡선 이력을 들고 있어 재사용하면 짝비교가 깨진다.
    new_scale_fn = lambda: make_scale_fn(mkt, dd)  # noqa: E731

    cut = "".join(ch for ch in str(args.exclude_from) if ch.isdigit())
    head = [d for d in dates if not cut or cut == "0" or "".join(filter(str.isdigit, d)) < cut]
    tail = [d for d in dates if cut and cut != "0" and "".join(filter(str.isdigit, d)) >= cut]

    sets = arms(args.fill_cap)
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
        results = {label: [] for label, _kw in sets}
        rng = random.Random(args.seed)
        for t in range(args.trials):
            pick = rng.sample(codes, min(args.sample, len(codes)))
            sd = {c: dfs[c] for c in pick}
            st = {c: status[c] for c in pick}
            sm = {c: mf.get(c, set()) for c in pick}
            for label, kw in sets:
                r = pb.run_portfolio(sd, st, wdates, initial_capital=args.seed_capital,
                                     slots=slots, market_filter_dates=sm,
                                     risk_scale_by_date=new_scale_fn(), **kw)
                results[label].append(metrics(r))
            print(f"  {wname} 시행 {t + 1}/{args.trials}", end="\r", flush=True)
        all_results[wname] = results
    print(" " * 50, end="\r")

    W = 118
    print(f"\n{'=' * W}")
    print(f"피라미딩 하루 횟수 — {args.trials}회 × {args.sample}종목 짝비교 (기준선: {BASE} = 현 실매매)")
    print(f"{'=' * W}")
    for wname, wdates in windows:
        results = all_results[wname]
        base = results[BASE]
        print(f"\n########## {wname} ({len(wdates)} 거래일) ##########")
        print(f"{'설정':<18}{'수익%':>9}{'MDD%':>8}{'MAR':>7}{'PF':>6}{'증액수':>7}{'3차':>6}"
              f"{'같은날2+':>9}{'같은날3':>8}{'상위10%':>9}{'최대':>9}{'>30%':>6}{'보유일':>7}"
              f"{'승-무-패':>10}{'MAR승':>7}")
        print("-" * W)
        for label, _kw in sets:
            rs = results[label]
            m = lambda k: float(np.median([x[k] for x in rs]))  # noqa: E731
            is_base = label == BASE
            tie = sum(1 for a, b in zip(rs, base) if abs(a["ret"] - b["ret"]) < 1e-9)
            los = sum(1 for a, b in zip(rs, base) if a["ret"] < b["ret"] - 1e-9)
            rw = sum(1 for a, b in zip(rs, base) if a["ret"] > b["ret"])
            mw = sum(1 for a, b in zip(rs, base) if a["mar"] > b["mar"])
            print(f"{label:<18}{m('ret'):>9.1f}{m('mdd'):>8.1f}{m('mar'):>7.2f}{m('pf'):>6.2f}"
                  f"{m('pyr_n'):>7.0f}{m('third'):>6.0f}{m('multi'):>9.0f}{m('burst3'):>8.0f}"
                  f"{m('top10'):>9.1f}{m('best'):>9.1f}{m('big'):>6.0f}{m('days'):>7.1f}"
                  f"{'—' if is_base else f'{rw}-{tie}-{los}':>10}"
                  f"{'—' if is_base else f'{mw}/{len(rs)}':>7}")

    print("\n" + "-" * W)
    print("같은날2+ = 하루에 2차 이상 얹은 (종목,날짜) 수. 0이면 이 축은 애초에 발동하지 않는다.")
    print("[읽는 법] 다회가 이기려면 꼬리(상위10%·최대·>30%)가 커야 한다. 수익만 늘고 PF·MDD가")
    print(" 나빠지면 그건 레버리지지 추세추종이 아니다.")


if __name__ == "__main__":
    main()
