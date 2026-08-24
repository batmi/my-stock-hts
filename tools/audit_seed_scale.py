"""시드를 500만 → 1,000만으로 올리면 슬롯·피라미딩 결론이 바뀌는가.

[왜 시드가 결론을 바꿀 수 있나] 사이징은 자산에 비례하지만 **주식은 정수 단위로만**
살 수 있다. 배분액이 1주 값에 못 미치면 그 진입은 버려지거나(백테스트) 배분액을 넘겨
집행된다(실매매·MAX_POSITION_OVERSHOOT). 이 양자화는 시드가 작을수록 심하게 물리므로,
'시드 500만'에서 잰 결론이 1,000만에서도 성립한다는 보장이 없다. 실제로 config 주석의
PYRAMIDING_MAX_COUNT 근거는 "[소액 시드] 시드가 작을수록 이점이 크다"라고 명시한다.

[무엇을 묻는가]
  ① 스케일 불변성 — 같은 설정에서 시드만 2배로 하면 결과가 얼마나 달라지는가.
     달라지지 않는다면 500만에서 잰 다른 결론들도 그대로 쓸 수 있다.
  ② 슬롯 수 — 1,000만에서도 4개가 맞는가(5개로 늘릴 여지가 생겼는가).
  ③ 피라미딩 차수 — 3차의 이점이 시드가 커져도 남는가.
  ④ 버려지는 진입 기회(skipped_qty0)·유휴현금이 시드 확대로 실제로 줄어드는가.

[방법] 같은 무작위 종목 표본을 모든 후보에 태우는 짝비교. 단일 경로는 종목 구성에
좌우되므로(메뉴 [4] 백테스트의 한계) 반드시 여러 표본의 짝비교로 본다.

[실행] python tools/audit_seed_scale.py [--days 1095] [--trials 30] [--sample 20]
       python tools/audit_seed_scale.py --split 3      # 하위기간 견고성
"""
import argparse
import os
import random
import sys

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import config  # noqa: E402
from modules import portfolio_backtest as pb  # noqa: E402
from tools.audit_common import seed_notice  # noqa: E402

OLD_CAPITAL = 5_000_000    # 종전 검증 시드(config 주석들의 근거)
NEW_CAPITAL = 10_000_000   # 실거래 전환 시드


def variants(old, new):
    """(라벨, 시드, 슬롯, 피라미딩차수).

    후보를 전부 명시한다 — config 값을 한 행으로 읽으면 감사 후 config를 바꿨을 때
    비교하려던 후보가 조용히 사라진다(audit_sizing_dials.py의 같은 주의).
    """
    return [
        ("종전 검증 500만·슬롯4·피라3", old, 4, 3),   # ← 기존 결론의 근거 조건
        ("현행 계획 1000만·슬롯4·피라3", new, 4, 3),   # ← 전환 계획
        ("(a) 1000만·슬롯3·피라3", new, 3, 3),
        ("(b) 1000만·슬롯5·피라3", new, 5, 3),
        ("(c) 1000만·슬롯6·피라3", new, 6, 3),
        ("(d) 1000만·슬롯4·피라2", new, 4, 2),
        ("(e) 1000만·슬롯4·피라1", new, 4, 1),
        ("(f) 1000만·슬롯4·피라4", new, 4, 4),
    ]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=1095)
    ap.add_argument("--trials", type=int, default=30)
    ap.add_argument("--sample", type=int, default=20)
    ap.add_argument("--old-capital", type=int, default=OLD_CAPITAL)
    ap.add_argument("--new-capital", type=int, default=NEW_CAPITAL)
    ap.add_argument("--split", type=int, default=0,
                    help="거래일을 N등분해 하위기간별로 따로 본다(견고성 확인)")
    ap.add_argument("--seed", type=int, default=20260805,
                    help="종목 표본 추출 씨드. 경계선 결과는 씨드를 바꿔 재확인한다")
    args = ap.parse_args()
    seed_notice(1)

    # 시장 필터의 지수 선택은 config.session.stock_data 를 본다(JSON 직접 읽기 금지).
    config.session.load_stock_config()
    targets = [(s["code"], s["name"])
               for s in config.session.stock_data.get("stocks_kr", [])]
    print(f"[준비] 관심종목 {len(targets)}개 · {args.days}일 · "
          f"시드 {args.old_capital:,} vs {args.new_capital:,}원")

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
    vs = variants(args.old_capital, args.new_capital)

    if args.split > 1:
        n = len(dates) // args.split
        periods = [(f"구간{i + 1}", dates[i * n:(i + 1) * n]) for i in range(args.split)]
    else:
        periods = [("전체", dates)]

    for plabel, pdates in periods:
        results = {name: [] for name, *_ in vs}
        rng = random.Random(args.seed)
        for t in range(args.trials):
            pick = rng.sample(codes, min(args.sample, len(codes)))
            sub_dfs = {c: dfs[c] for c in pick}
            sub_status = {c: status[c] for c in pick}
            sub_mf = {c: mf_dates.get(c, set()) for c in pick}

            for name, cap, slots, pyr in vs:
                r = pb.run_portfolio(sub_dfs, sub_status, pdates,
                                     initial_capital=cap, slots=slots,
                                     pyramiding_max=pyr, market_filter_dates=sub_mf)
                mdd = r["mdd"]
                profits = sorted((s["profit"] for s in r["sells"]), reverse=True)
                n_sell = len(profits)
                results[name].append({
                    "ret": r["total_return"], "mdd": mdd, "pf": r["pf"],
                    "wr": r["win"] / max(1, r["win"] + r["loss"]) * 100,
                    "trades": n_sell,
                    "mar": r["total_return"] / abs(mdd) if mdd else float("nan"),
                    # fat-tail — 추세추종의 손익은 상위 거래가 만든다
                    "top10": float(np.mean(profits[:max(1, n_sell // 10)])) if n_sell else 0.0,
                    "big30": sum(1 for p in profits if p >= 30),
                    "pyr": r["pyramid_count"],
                    "skip": r["skipped_qty0"],           # 1주도 못 사서 버린 진입 기회
                    "pyrblk": r.get("pyramid_blocked_qty0", 0),  # 수량 부족으로 불발된 증액
                    "cash": r.get("avg_cash_ratio", float("nan")),
                    "slotu": r["avg_slots"],
                })
            print(f"  [{plabel}] 시행 {t + 1}/{args.trials} 완료", end="\r", flush=True)
        print(" " * 50, end="\r")
        report(plabel, pdates, vs, results, args)


def report(plabel, pdates, vs, results, args):
    base_name = vs[1][0]   # 기준 = 전환 계획(1000만·슬롯4·피라3)
    base = results[base_name]
    med = lambda rs, k: float(np.median([x[k] for x in rs]))  # noqa: E731

    print(f"\n{'=' * 126}")
    print(f"시드 확대 재검증 [{plabel} · {len(pdates)}거래일] — "
          f"{args.trials}회 × {args.sample}종목 무작위 짝비교 (기준: {base_name})")
    print(f"{'=' * 126}")
    print(f"{'설정':<30}{'수익%':>9}{'MDD%':>8}{'MAR':>7}{'PF':>6}{'승률%':>7}{'청산':>6}"
          f"{'상위10%':>9}{'>30%':>6}{'증액':>6}{'버린기회':>9}{'증액불발':>9}{'현금%':>7}"
          f"{'슬롯':>6}{'Δ수익':>9}{'MAR승':>8}")
    print("-" * 126)
    for name, cap, slots, pyr in vs:
        rs = results[name]
        d_ret = med(rs, "ret") - med(base, "ret")
        w_mar = sum(1 for a, b in zip(rs, base)
                    if a["mar"] > b["mar"])
        mark = "  ←기준" if name == base_name else ""
        print(f"{name:<30}{med(rs, 'ret'):>9.1f}{med(rs, 'mdd'):>8.1f}{med(rs, 'mar'):>7.2f}"
              f"{med(rs, 'pf'):>6.2f}{med(rs, 'wr'):>7.1f}{med(rs, 'trades'):>6.0f}"
              f"{med(rs, 'top10'):>9.1f}{med(rs, 'big30'):>6.0f}{med(rs, 'pyr'):>6.0f}"
              f"{med(rs, 'skip'):>9.0f}{med(rs, 'pyrblk'):>9.0f}{med(rs, 'cash'):>7.1f}"
              f"{med(rs, 'slotu'):>6.2f}{d_ret:>9.1f}{w_mar:>5}/{len(base)}{mark}")
    print("-" * 126)
    print("버린기회 = 1주도 못 사서 건너뛴 진입 후보 / 증액불발 = 수량 부족으로 못 한 피라미딩.")
    print("  둘 다 시드가 작을수록 커지는 '양자화 비용'이다 — 시드 확대의 직접 효과가 여기 나온다.")
    print("상위10%·>30% = fat-tail 지표. 추세추종의 손익은 이 꼬리가 만든다(평균만 보면 안 된다).")
    print("Δ수익·MAR승 = 전환 계획(1000만·슬롯4·피라3) 대비. 시드가 다른 행끼리의 수익률 비교는")
    print("  '같은 시드에서의 우열'이 아니므로, 500만 행은 비율 지표(MAR·PF·승률)로만 읽는다.")


if __name__ == "__main__":
    main()
