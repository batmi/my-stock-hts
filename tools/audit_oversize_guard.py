"""배분액을 넘겨 '1주라도' 사는 것이 소액 시드에서 정당한가.

[문제] trader._execute_buy_orders 는 배분액이 1주 값보다 작으면 배분액을 1주 값까지
끌어올린다("최소 주문 금액 보정"). 큰 계좌에서는 발동하지 않지만 시드 500만에서는
관심목록 41종목 중 7종목(17%)이 이 경로를 탄다. 그 결과 변동성 타겟팅·리스크 한도가
합의한 상한이 1주 값 하나에 덮어써진다.

  SK하이닉스 1,577,000원 · 목표 배분 500,000원 → 실제 집행 1,577,000원 = 계좌의 31.5%
  1회 리스크 = 1,577,000 × 15% = 236,550원 = 계좌의 4.7% > SYSTEM_RISK_PER_TRADE(4.0%)

[파리티 문제이기도 하다] 백테스트(portfolio_backtest)는 같은 상황에서 **건너뛴다**.
즉 config.py에 기록된 사이징·리스크 결론은 전부 '못 사면 안 산다' 모델에서 나왔는데
실매매만 '1주는 산다'로 다르게 동작해 왔다. 어느 쪽이 옳은지를 여기서 정한다.

[무엇을 묻는가] 초과 허용 배수(oversize_limit)를 바꿔가며,
  · 집중도(한 종목 최대 계좌 비중)가 실제로 내려가는가
  · 그 대가로 수익/MAR을 얼마나 잃는가
를 같은 표본 짝비교로 본다. 고가주를 버리는 것은 진입 기회를 버리는 것이므로
추세추종에서 공짜가 아니다 — 대가의 크기를 알고 정해야 한다.

[실행] python tools/audit_oversize_guard.py [--days 1095] [--trials 30] [--sample 20]
       python tools/audit_oversize_guard.py --split 3      # 하위기간 견고성
"""
import argparse
import os
import random
import sys

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import config  # noqa: E402
from modules import portfolio_backtest as pb  # noqa: E402

INITIAL_CAPITAL = 10_000_000  # 실거래 시드와 같게 둔다(seed-slot-sizing)


def variants():
    """(라벨, 초과 허용 배수).

    후보를 전부 명시한다 — config 값을 한 행으로 읽으면 감사 후 config를 바꿨을 때
    비교하려던 후보가 조용히 사라진다(audit_sizing_dials.py의 같은 주의).
    """
    return [
        ("현행 실매매(무제한)", 99.0),   # ← trader.py의 현재 동작
        ("(a) 3.0배까지 허용", 3.0),
        ("(b) 2.0배까지 허용", 2.0),
        ("(c) 1.5배까지 허용", 1.5),
        ("(d) 1.3배까지 허용", 1.3),
        ("(e) 초과 불허(=백테스트)", 1.0),
    ]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=1095)
    ap.add_argument("--trials", type=int, default=30)
    ap.add_argument("--sample", type=int, default=20)
    ap.add_argument("--slots", type=int, default=None)
    ap.add_argument("--seed-capital", type=int, default=INITIAL_CAPITAL)
    ap.add_argument("--split", type=int, default=0,
                    help="거래일을 N등분해 하위기간별로 따로 본다(견고성 확인)")
    args = ap.parse_args()

    slots = args.slots or getattr(config, "SYSTEM_MAX_HOLDINGS", 4)

    # 시장 필터의 지수 선택은 config.session.stock_data 를 본다(JSON 직접 읽기 금지).
    config.session.load_stock_config()
    targets = [(s["code"], s["name"])
               for s in config.session.stock_data.get("stocks_kr", [])]
    print(f"[준비] 관심종목 {len(targets)}개 · {args.days}일 · 슬롯 {slots} "
          f"· 시드 {args.seed_capital:,}원")

    dfs, mf_dates, dates, failed = pb.prepare_universe(targets, args.days)
    print(f"[준비] 사용 가능 {len(dfs)}종목 / 거래일 {len(dates)}일"
          + (f" · 제외 {len(failed)}개" if failed else ""))
    if len(dfs) < args.sample:
        args.sample = max(5, len(dfs) // 2)
        print(f"[준비] 표본 수를 {args.sample}로 조정")
    print(f"[준비] 표본 {args.sample}/{len(dfs)}종목 · 시행 간 중복률 "
          f"{args.sample / len(dfs) * 100:.0f}% (승률 부풀림 주의)")

    thresholds = {
        "BUY_SCORE": config.ANALYSIS_THRESHOLDS["BUY_SCORE"],
        "BUY_RSI_MAX": config.ANALYSIS_THRESHOLDS["BUY_RSI_MAX"],
        "RISE_SCORE": config.ANALYSIS_THRESHOLDS["RISE_SCORE"],
        "WEIGHTS": config.SCORING_WEIGHTS,
    }
    status = pb.precompute_status(dfs, thresholds)

    codes = list(dfs.keys())
    vs = variants()

    if args.split > 1:
        n = len(dates) // args.split
        periods = [(f"구간{i + 1}", dates[i * n:(i + 1) * n]) for i in range(args.split)]
    else:
        periods = [("전체", dates)]

    for plabel, pdates in periods:
        results = {name: [] for name, _lim in vs}
        rng = random.Random(20260805)
        for t in range(args.trials):
            pick = rng.sample(codes, min(args.sample, len(codes)))
            sub_dfs = {c: dfs[c] for c in pick}
            sub_status = {c: status[c] for c in pick}
            sub_mf = {c: mf_dates.get(c, set()) for c in pick}

            for name, lim in vs:
                r = pb.run_portfolio(sub_dfs, sub_status, pdates,
                                     initial_capital=args.seed_capital, slots=slots,
                                     market_filter_dates=sub_mf, oversize_limit=lim)
                mdd = r["mdd"]
                results[name].append({
                    "ret": r["total_return"], "mdd": mdd, "pf": r["pf"],
                    "wr": r["win"] / max(1, r["win"] + r["loss"]) * 100,
                    "trades": r["win"] + r["loss"],
                    "mar": r["total_return"] / abs(mdd) if mdd else float("nan"),
                    "maxw": r["max_buy_weight"],
                    "maxr": r["max_buy_risk"],
                    "brch": r["risk_cap_breaches"],
                    "over": r["oversized_buys"],
                    "skip": r["skipped_qty0"],
                })
            print(f"  [{plabel}] 시행 {t + 1}/{args.trials} 완료", end="\r", flush=True)
        print(" " * 50, end="\r")
        report(plabel, pdates, vs, results, args, slots)


def report(plabel, pdates, vs, results, args, slots):
    base_name = "현행 실매매(무제한)"
    base = results[base_name]
    print(f"\n{'=' * 112}")
    print(f"1주 강제 매수 가드 [{plabel} · {len(pdates)}거래일] — "
          f"{args.trials}회 × {args.sample}종목 무작위 짝비교 "
          f"(기준: {base_name} = 현재 trader.py)")
    print(f"시드 {args.seed_capital:,}원 · 슬롯 {slots} · 목표 비중 {100 / slots:.0f}%")
    print(f"{'=' * 112}")
    cap = getattr(config, "SYSTEM_RISK_PER_TRADE", 4.0)
    print(f"{'설정':<24}{'수익%':>9}{'MDD%':>8}{'MAR':>7}{'PF':>6}{'거래':>6}"
          f"{'진입최대%':>10}{'1회리스크%':>11}{'한도초과':>9}{'초과매수':>9}{'버린기회':>9}"
          f"{'Δ수익':>9}{'MAR승':>8}")
    print("-" * 118)
    for name, _lim in vs:
        rs = results[name]
        med = lambda k: float(np.median([x[k] for x in rs]))  # noqa: E731
        mx = lambda k: float(np.max([x[k] for x in rs]))      # noqa: E731
        d_ret = float(np.median([a["ret"] - b["ret"] for a, b in zip(rs, base)]))
        mar_wins = sum(1 for a, b in zip(rs, base) if a["mar"] > b["mar"])
        mark = "  ←기준" if name == base_name else ""
        print(f"{name:<24}{med('ret'):>9.1f}{med('mdd'):>8.1f}{med('mar'):>7.2f}"
              f"{med('pf'):>6.2f}{med('trades'):>6.0f}"
              f"{mx('maxw'):>10.1f}{mx('maxr'):>11.2f}{med('brch'):>9.0f}"
              f"{med('over'):>9.0f}{med('skip'):>9.0f}"
              f"{d_ret:>9.1f}{mar_wins:>6d}/{len(rs)}{mark}")
    print("-" * 118)
    print(f"진입최대% / 1회리스크% = 진입 '순간'의 최대치(시행 전체의 최댓값). 목표 비중은 "
          f"{100 / slots:.0f}%, 리스크 한도는 {cap}%.")
    print("  (보유 중 최대 비중은 피라미딩이 지배하므로 사이징 상한 검증에는 쓸 수 없다)")
    print(f"한도초과 = 1회 리스크가 SYSTEM_RISK_PER_TRADE({cap}%)를 넘긴 매수 건수 — 이 가드가 지키려는 불변식.")
    print("초과매수 = 배분액을 넘겨 1주 강제로 집행한 매수 횟수 / 버린기회 = 1주도 못 사서 건너뛴 후보 수.")


if __name__ == "__main__":
    main()
