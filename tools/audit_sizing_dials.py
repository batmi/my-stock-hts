"""변동성 타겟팅 다이얼(TARGET_VOLATILITY · VOLATILITY_SCALING_MIN)에 실증 근거가 있는가.

[왜] 현행 설정에서 변동성 배수가 40/41 종목에서 하한(0.4)에 눌려 상수가 됐다.
관심종목의 연변동성은 55%~745%로 13.5배 차이인데 배분액은 사실상 전부 같다
(배분 변동계수 2.1%). 변동성 타겟팅의 존재 이유가 '변동성 큰 종목은 적게'인데
그 차등이 사라진 것이다. 동시에 SYSTEM_RISK_PER_TRADE(4%)와 히트 캡(10%)도
한 번도 구속하지 않아, 실질 사이징을 결정하는 것은 하한 하나뿐이다.

배수가 하한을 벗어나려면 ATR기준 연변동 < 62%여야 하는데 중앙값이 130%다.
2년을 거슬러 봐도 하한 근처였다 — 일시적 시장 탓이 아니라 목표(0.25)와 이 시장의
척도가 안 맞는다.

[무엇을 묻는가] '차등을 되살리면 실제로 나아지는가.' 지금 상태가 의도된 보수적
방어일 수도 있으므로, 채택이 아니라 **확정**이 목적이다. 어느 쪽이든 근거를 남긴다.

[비교 방법] 같은 무작위 종목 표본을 모든 후보에 태우는 짝비교. 표본이 겹치면
승률이 부풀려지므로(관심종목 41개에서 30개를 뽑으면 시행끼리 73% 동일) 표본 수를
바꿔가며 민감도를 함께 본다.

[실행] python tools/audit_sizing_dials.py [--days 1095] [--trials 30] [--sample 20]
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

INITIAL_CAPITAL = 10_000_000  # 실거래 시드와 같게 둔다(seed-slot-sizing)


def variants():
    """(라벨, 목표변동성, 배수하한).

    **config 값을 그대로 한 행으로 쓰지 않는다** — 감사 결과에 따라 config를 바꾸면
    다음 실행에서 그 행이 다른 설정을 가리켜 비교하려던 후보가 조용히 사라진다
    (audit_drawdown_axis.py가 2026-08-04에 실제로 겪은 사고). 후보는 전부 명시한다.
    """
    return [
        ("현행 0.25 / 0.40",   0.25, 0.40),   # ← 현재 config
        ("(a) 0.25 / 0.15",    0.25, 0.15),
        ("(a') 0.25 / 0.25",   0.25, 0.25),
        ("(b) 0.65 / 0.40",    0.65, 0.40),
        ("(b') 0.45 / 0.40",   0.45, 0.40),
        ("(a+b) 0.65 / 0.15",  0.65, 0.15),
        ("(a+b') 0.45 / 0.15", 0.45, 0.15),
        ("타겟팅 끔",          None, None),   # USE_VOLATILITY_TARGETING=False
    ]


def apply_variant(tv, smin):
    """config를 후보 값으로 갈아끼우고 원복용 스냅샷을 돌려준다."""
    saved = (config.USE_VOLATILITY_TARGETING,
             config.TARGET_VOLATILITY,
             config.VOLATILITY_SCALING_MIN)
    if tv is None:
        config.USE_VOLATILITY_TARGETING = False
    else:
        config.USE_VOLATILITY_TARGETING = True
        config.TARGET_VOLATILITY = tv
        config.VOLATILITY_SCALING_MIN = smin
    return saved


def restore(saved):
    (config.USE_VOLATILITY_TARGETING,
     config.TARGET_VOLATILITY,
     config.VOLATILITY_SCALING_MIN) = saved


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=1095)
    ap.add_argument("--trials", type=int, default=30)
    ap.add_argument("--sample", type=int, default=20)
    ap.add_argument("--slots", type=int, default=None)
    ap.add_argument("--seed-capital", type=int, default=INITIAL_CAPITAL,
                    help="시드(원). 정수 주식수 양자화 때문에 결론이 시드에 좌우될 수 있다")
    ap.add_argument("--split", type=int, default=0,
                    help="거래일을 N등분해 하위기간별로 따로 본다(견고성 확인)")
    ap.add_argument("--seed", type=int, default=20260804,
                    help="종목 표본 추출 씨드. 경계선 결과는 씨드를 바꿔 재확인한다")
    args = ap.parse_args()
    seed_notice(1)

    slots = args.slots or getattr(config, "SYSTEM_MAX_HOLDINGS", 4)

    # [필수] 시장 필터의 지수 선택은 config.session.stock_data 를 본다.
    #  JSON 을 직접 읽으면 세션이 빈 채로 남아 전 종목이 KOSPI 로 취급된다.
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

    # 하위기간 분해: 한 구간에서만 이기는 설정을 채택하지 않기 위한 견고성 검사.
    #  (breadth 축이 '하위기간 2/3'로 보류된 전례가 있다)
    if args.split > 1:
        n = len(dates) // args.split
        periods = [(f"구간{i + 1}", dates[i * n:(i + 1) * n]) for i in range(args.split)]
    else:
        periods = [("전체", dates)]

    for plabel, pdates in periods:
        results = {name: [] for name, _tv, _sm in vs}
        rng = random.Random(args.seed)
        for t in range(args.trials):
            pick = rng.sample(codes, min(args.sample, len(codes)))
            sub_dfs = {c: dfs[c] for c in pick}
            sub_status = {c: status[c] for c in pick}
            sub_mf = {c: mf_dates.get(c, set()) for c in pick}

            for name, tv, smin in vs:
                saved = apply_variant(tv, smin)
                try:
                    r = pb.run_portfolio(sub_dfs, sub_status, pdates,
                                         initial_capital=args.seed_capital, slots=slots,
                                         market_filter_dates=sub_mf)
                finally:
                    restore(saved)      # 원복을 빠뜨리면 다음 후보가 오염된다
                mdd = r["mdd"]
                results[name].append({
                    "ret": r["total_return"], "mdd": mdd, "pf": r["pf"],
                    "cash": r["avg_cash_ratio"],
                    "wr": r["win"] / max(1, r["win"] + r["loss"]) * 100,
                    "trades": r["win"] + r["loss"],
                    "mar": r["total_return"] / abs(mdd) if mdd else float("nan"),
                })
            print(f"  [{plabel}] 시행 {t + 1}/{args.trials} 완료", end="\r", flush=True)
        print(" " * 50, end="\r")
        report(plabel, pdates, vs, results, args, slots)


def report(plabel, pdates, vs, results, args, slots):
    base_name = "현행 0.25 / 0.40"
    base = results[base_name]
    print(f"\n{'=' * 104}")
    print(f"변동성 타겟팅 다이얼 [{plabel} · {len(pdates)}거래일] — "
          f"{args.trials}회 × {args.sample}종목 무작위 짝비교 "
          f"(기준: {base_name} = 현재 config)")
    print(f"{'=' * 104}")
    print(f"{'설정':<22}{'수익%':>9}{'MDD%':>8}{'MAR':>7}{'PF':>6}{'현금%':>7}{'승률%':>7}"
          f"{'거래':>6}{'ΔMDD':>8}{'Δ수익':>9}{'MDD승':>7}{'MAR승':>7}{'최악MDD':>9}")
    print("-" * 112)
    for name, _tv, _sm in vs:
        rs = results[name]
        med = lambda k: float(np.median([x[k] for x in rs]))  # noqa: E731
        d_mdd = float(np.median([a["mdd"] - b["mdd"] for a, b in zip(rs, base)]))
        d_ret = float(np.median([a["ret"] - b["ret"] for a, b in zip(rs, base)]))
        wins = sum(1 for a, b in zip(rs, base) if a["mdd"] > b["mdd"])
        mar_wins = sum(1 for a, b in zip(rs, base) if a["mar"] > b["mar"])
        worst = float(np.min([x["mdd"] for x in rs]))
        is_base = (name == base_name)
        print(f"{name:<22}{med('ret'):>9.1f}{med('mdd'):>8.1f}{med('mar'):>7.2f}"
              f"{med('pf'):>6.2f}{med('cash'):>7.1f}{med('wr'):>7.1f}{med('trades'):>6.0f}"
              f"{d_mdd:>+8.2f}{d_ret:>+9.2f}"
              f"{'—' if is_base else f'{wins}/{len(rs)}':>7}"
              f"{'—' if is_base else f'{mar_wins}/{len(rs)}':>7}{worst:>9.1f}")
    print("-" * 112)
    print("ΔMDD·Δ수익은 현행 대비 중앙값. ΔMDD +는 낙폭 축소(개선).")
    print("MAR = 수익/|MDD|. 사이징 변경의 가치는 수익도 낙폭도 아닌 MAR로 봐야 한다.")
    print("최악MDD = 전 시행 중 가장 깊은 낙폭(꼬리). 현금%가 오르면 노출이 준 것이다.")


if __name__ == "__main__":
    main()
