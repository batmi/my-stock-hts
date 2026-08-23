"""슬롯 교체 규칙 검증 — '더 강한 후보가 나타나도 4칸이 차 있으면 못 산다'를 고칠 것인가.

[구조적 공백] 이 시스템에는 교체 경로가 아예 없다. 슬롯은 청산 룰(손절·TS·시간·점수)이
 걸려야만 풀리고, "지금 보유한 것보다 훨씬 강한 후보가 떴다"는 이유로는 절대 안 풀린다.
 2026-08-11 세션의 출발점이 정확히 "발동선이 높으면 아무 액션 없이 슬롯만 문다"였는데,
 TS 발동선 조정은 그 증상을 완화한 것이고 원인 쪽 손잡이는 이쪽이다.

[그런데 추세추종에서 교체는 위험하다] 두 가지 이유로 원칙과 정면으로 부딪친다.
  ① 승자를 잘라낸다. 이 시스템의 수익은 fat-tail에서 나온다(상위 10% 청산이 대부분).
     점수는 진입 신호지 보유 이유가 아니다 — 크게 오른 종목은 RSI가 높아 점수가 내려가
     '약한 보유'로 잡히기 쉽다. 교체가 이를 잘라내면 설계가 무너진다.
  ② 왕복 비용을 문다. 2026-08-11 슬리피지 감사에서 이 시스템은 체결 품질 0.2%p에
     10년 수익 18%가 걸릴 만큼 비용에 민감하다. 교체는 그 비용을 자발적으로 늘린다.

[그래서 가드를 함께 잰다] 교체 자체가 아니라 '어떤 교체가 살아남는가'를 묻는다.
  · margin      — 점수차 문턱. 낮으면 잦은 교체, 높으면 사실상 무동작.
  · only_unarmed— TS 무장 경험이 없는 보유만. ①에 대한 직접 방어이며 가장 중요한 가드다.
  · min_days    — 최소 보유일. churn 억제.
  · only_losing — 손실 중인 보유만. 가장 보수적인 형태.

[판정 잣대] 기존 결정과 같다. 총수익·MDD뿐 아니라 상위10%·최대·>30%로 fat-tail을 보고,
 하위 구간 다수에서 이기지 못하면 채택하지 않는다. 특히 여기서는 **최대 단일 수익**이
 핵심 지표다 — 교체가 승자를 자르면 총수익이 비슷해도 그 값이 먼저 무너진다.

[실행] python tools/audit_slot_rotation.py [--trials 15] [--sample 25] [--only A]
"""
import argparse
import os
import random
import sys

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tools.audit_common import exits  # noqa: E402

import config  # noqa: E402
from modules import portfolio_backtest as pb  # noqa: E402

INITIAL_CAPITAL = 10_000_000


def rot_sets():
    """(그룹, 라벨, rotation dict 또는 None=현행)."""
    return [
        # [A] 순수 점수차만. 가드 없이 교체하면 어떻게 되는가 — 위험의 크기를 먼저 본다.
        ("A. 점수차 문턱 (가드 없음)", "교체 없음 (현행)", None),
        ("A. 점수차 문턱 (가드 없음)", "점수차 1.0", {"margin": 1.0}),
        ("A. 점수차 문턱 (가드 없음)", "점수차 2.0", {"margin": 2.0}),
        ("A. 점수차 문턱 (가드 없음)", "점수차 3.0", {"margin": 3.0}),
        # [B] 승자 보호 가드. 무장 경험이 있는 포지션은 건드리지 않는다.
        ("B. 승자 보호(미무장 한정)", "교체 없음 (현행)", None),
        ("B. 승자 보호(미무장 한정)", "1.0 + 미무장", {"margin": 1.0, "only_unarmed": True}),
        ("B. 승자 보호(미무장 한정)", "2.0 + 미무장", {"margin": 2.0, "only_unarmed": True}),
        ("B. 승자 보호(미무장 한정)", "3.0 + 미무장", {"margin": 3.0, "only_unarmed": True}),
        # [C] churn 억제. 교체는 왕복 비용을 무므로 회전이 빨라지면 그 자체가 손실이다.
        ("C. 최소 보유일(2.0+미무장 고정)", "0일 (제한 없음)", {"margin": 2.0, "only_unarmed": True}),
        ("C. 최소 보유일(2.0+미무장 고정)", "5일", {"margin": 2.0, "only_unarmed": True, "min_days": 5}),
        ("C. 최소 보유일(2.0+미무장 고정)", "10일", {"margin": 2.0, "only_unarmed": True, "min_days": 10}),
        ("C. 최소 보유일(2.0+미무장 고정)", "20일", {"margin": 2.0, "only_unarmed": True, "min_days": 20}),
        # [D] 가장 보수적인 형태 — 손실 중인 미무장 포지션만. '좀비 슬롯'만 겨냥한다.
        ("D. 보수형(손실 보유 한정)", "교체 없음 (현행)", None),
        ("D. 보수형(손실 보유 한정)", "2.0 + 미무장 + 손실",
         {"margin": 2.0, "only_unarmed": True, "only_losing": True}),
        ("D. 보수형(손실 보유 한정)", "2.0 + 미무장 + 손실 + 10일",
         {"margin": 2.0, "only_unarmed": True, "only_losing": True, "min_days": 10}),
        ("D. 보수형(손실 보유 한정)", "1.0 + 미무장 + 손실 + 10일",
         {"margin": 1.0, "only_unarmed": True, "only_losing": True, "min_days": 10}),
    ]


def metrics(r):
    sells = exits(r)
    profits = sorted((t["profit"] for t in sells), reverse=True)
    top10 = profits[:max(1, len(profits) // 10)]
    wins = [p for p in profits if p > 0]
    rot = [t for t in sells if t["reason"] == "교체"]
    return {
        "ret": r["total_return"], "mdd": r["mdd"],
        "mar": r["total_return"] / abs(r["mdd"]) if r["mdd"] else float("nan"),
        "pf": r["pf"],
        "top10": float(np.mean(top10)) if top10 else 0.0,
        "best": profits[0] if profits else 0.0,
        "big": sum(1 for p in profits if p >= 30),
        "win": len(wins) / len(profits) * 100 if profits else 0.0,
        "days": float(np.median([t["days"] for t in sells])) if sells else 0.0,
        "n": len(sells),
        "rot": r.get("rotations", 0),
        # [교체의 질] 교체로 판 것들의 평균 손익. 양수면 이기고 있던 것을 판 셈이다.
        "rot_p": float(np.mean([t["profit"] for t in rot])) if rot else 0.0,
        # [핵심 진단] 교체로 판 것 중 MFE가 15% 이상이던 건수 — 잘라낸 승자의 수.
        "rot_cut": sum(1 for t in rot if t.get("mfe", 0) >= 15),
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--trials", type=int, default=15)
    ap.add_argument("--sample", type=int, default=25)
    ap.add_argument("--days", type=int, default=3650)
    ap.add_argument("--slots", type=int, default=None)
    ap.add_argument("--only", default=None)
    ap.add_argument("--seed", type=int, default=20260811)
    ap.add_argument("--subperiods", type=int, default=4)
    ap.add_argument("--exclude-from", default="20260301")
    args = ap.parse_args()

    slots = args.slots or getattr(config, "SYSTEM_MAX_HOLDINGS", 4)
    config.session.load_stock_config()
    targets = [(s["code"], s["name"]) for s in config.session.stock_data.get("stocks_kr", [])]
    print(f"[준비] 관심종목 {len(targets)}개 · {args.days}일 · 슬롯 {slots}")

    dfs, mf, dates, failed = pb.prepare_universe(targets, args.days)
    thresholds = {
        "BUY_SCORE": config.ANALYSIS_THRESHOLDS["BUY_SCORE"],
        "BUY_RSI_MAX": config.ANALYSIS_THRESHOLDS["BUY_RSI_MAX"],
        "RISE_SCORE": config.ANALYSIS_THRESHOLDS["RISE_SCORE"],
        "WEIGHTS": config.SCORING_WEIGHTS,
    }
    status = pb.precompute_status(dfs, thresholds)
    print(f"[준비] 사용 {len(dfs)}종목 / 거래일 {len(dates)}일"
          + (f" · 제외 {len(failed)}" if failed else ""))

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
    head = [d for d in dates if not cut or cut == "0" or "".join(filter(str.isdigit, d)) < cut]
    tail = [d for d in dates if cut and cut != "0" and "".join(filter(str.isdigit, d)) >= cut]
    print(f"[창] 검증 {len(head)}일 (~{head[-1] if head else '-'}) · 제외 {len(tail)}일")

    sets = rot_sets()
    if args.only:
        sets = [x for x in sets if x[0].startswith(args.only)]
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
        results = {(g, l): [] for g, l, _r in sets}
        rng = random.Random(args.seed)
        for t in range(args.trials):
            pick = rng.sample(codes, min(args.sample, len(codes)))
            sd = {c: dfs[c] for c in pick}
            st = {c: status[c] for c in pick}
            sm = {c: mf.get(c, set()) for c in pick}
            for g, label, rot in sets:
                r = pb.run_portfolio(sd, st, wdates, initial_capital=INITIAL_CAPITAL,
                                     slots=slots, market_filter_dates=sm,
                                     risk_scale_by_date=new_scale_fn(), rotation=rot)
                results[(g, label)].append(metrics(r))
            print(f"  {wname} 시행 {t + 1}/{args.trials}", end="\r", flush=True)
        all_results[wname] = results
    print(" " * 50, end="\r")

    W = 112
    print(f"\n{'=' * W}")
    print(f"슬롯 교체 규칙 ({args.trials}회 × {args.sample}종목 짝비교)")
    print(f"{'=' * W}")
    for wname, wdates in windows:
        results = all_results[wname]
        print(f"\n########## {wname} ({len(wdates)} 거래일) ##########")
        last_group = None
        for g, label, _r in sets:
            if g != last_group:
                base_label = next(l for gg, l, _x in sets if gg == g)
                base = results[(g, base_label)]
                print(f"\n{g}  (기준선: {base_label})")
                print(f"{'설정':<24}{'수익%':>9}{'MDD%':>8}{'MAR':>7}{'PF':>6}{'승률%':>7}"
                      f"{'청산':>6}{'보유일':>7}{'교체':>6}{'교체손익':>9}{'자른승자':>9}"
                      f"{'상위10%':>9}{'최대':>9}{'>30%':>6}{'승-무-패':>10}{'꼬리승':>7}")
                print("-" * W)
                last_group = g
            rs = results[(g, label)]
            m = lambda key: float(np.median([x[key] for x in rs]))  # noqa: E731
            is_base = label == base_label
            tie = sum(1 for a, b in zip(rs, base) if abs(a["ret"] - b["ret"]) < 1e-9)
            los = sum(1 for a, b in zip(rs, base) if a["ret"] < b["ret"] - 1e-9)
            rw = sum(1 for a, b in zip(rs, base) if a["ret"] > b["ret"])
            tw = sum(1 for a, b in zip(rs, base) if a["top10"] > b["top10"])
            print(f"{label:<24}{m('ret'):>9.1f}{m('mdd'):>8.1f}{m('mar'):>7.2f}{m('pf'):>6.2f}"
                  f"{m('win'):>7.1f}{m('n'):>6.0f}{m('days'):>7.0f}{m('rot'):>6.0f}"
                  f"{m('rot_p'):>9.1f}{m('rot_cut'):>9.0f}{m('top10'):>9.1f}"
                  f"{m('best'):>9.1f}{m('big'):>6.0f}"
                  f"{'—' if is_base else f'{rw}-{tie}-{los}':>8}"
                  f"{'—' if is_base else f'{tw}/{len(rs)}':>7}")

    print("\n" + "-" * W)
    print("교체 = 교체로 비운 포지션 수 · 교체손익 = 그 평균 실현손익(%) · 자른승자 = 그중 MFE 15%+ 건수.")
    print("[읽는 법] '자른승자'가 0이 아니면 교체가 fat-tail을 갉고 있다 — 최대·상위10%를 함께 볼 것.")
    print("[읽는 법] 교체 0건이면 그 문턱은 무동작이다(무승부는 열위가 아니다).")


if __name__ == "__main__":
    main()
