"""히트 캡을 없앤 것과 같은가 — 진입 대비 정의로 바꾼 대가를 잰다.

[왜 이 감사인가] 2026-09-01에 오픈 리스크의 **기준**을 현재가 → 매수가로 바꿨다.
 종전 정의는 손절선이 고정된 채 값만 올라도 히트가 부풀어, 피라미딩 발동선(+10%)과
 TS 무장(+23%) 사이 — 정확히 추세가 잘 가는 구간 — 에서 증액을 막았다. 그 성질을
 없앤 것은 맞는데, **부작용이 하나 남는다**: 종전 정의는 강세장에서 캡이 조여
 사실상 디레버리지 역할을 했다. 그 방어가 사라진 대가(MDD)를 아직 아무도 안 쟀다.

[왜 지금까지 못 쟀나] 백테스트의 히트 산식이 실매매와 달랐다(`수량 × 종가 × |손절률|`).
 청산선을 보지 않아 이미 이익이 잠긴 포지션도 최초 손절폭만큼 예산을 계속 먹었고,
 그래서 실매매식의 2.3배였다. 세 정의가 다 다르면 비교 자체가 성립하지 않는다.
 이번에 백테스트가 매도 경로와 같은 청산선(_intraday_stop_level)을 쓰도록 맞추고,
 기준만 `heat_basis`로 가르게 했다.

  팔1 캡 해제        — 캡이 아예 없을 때. 다른 두 팔의 상한선이자 '캡의 값어치' 기준선.
  팔2 진입대비(현행) — 수량 × max(0, 매수가 − 청산선)
  팔3 현재가대비(폐기)— 수량 × max(0, 종가 − 청산선)
  팔4 종전 백테스트   — 수량 × 종가 × |손절률| (legacy). 이전 감사 수치가 어떤 세기의
                        캡 아래에서 나왔는지 되짚기 위한 대조군.

[판정] 이 장치는 수익을 늘리려고 다는 것이 아니라 파산을 막으려고 단다. 그래서
 총수익이 아니라 **MDD와 꼬리**를 본다([[adoption-rule-drawdown-veto]]).
 팔2가 팔3보다 MDD가 상대 10% 넘게 나쁘고 꼬리도 줄면 정의 변경을 되돌려야 한다.
 팔1과 팔2가 사실상 같으면, 진입대비 캡은 '거의 안 걸리는 보험'이라는 뜻이다.

[주의] 절대 수치는 이전 감사와 비교 금지 — 모든 이전 실행은 팔4의 캡 아래에서 돌았다.

[실행] python3 tools/audit_heat_basis.py --trials 12 --sample 25 --seeds 20260816,7,101
"""
import argparse
import os
import random
import sys

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tools.audit_common import seed_notice, windows as audit_windows  # noqa: E402

import config  # noqa: E402
from modules import portfolio_backtest as pb  # noqa: E402
from tools.audit_defensive_sector import (  # noqa: E402
    INITIAL_CAPITAL, metrics, new_scale_fn_factory,
)

# (라벨, heat_cap_pct, heat_basis)  — cap None 이면 config 값을 쓴다.
ARMS = [("캡 해제", 0.0, "cost"),
        ("진입대비 (현행)", None, "cost"),
        ("현재가대비 (폐기)", None, "mark"),
        ("종전 백테스트 산식", None, "legacy")]


def _run_arm(dfs, status, mf, picks, seeds, wd, slots, new_scale, cap, basis,
             iratio=None, trials=None):
    out = []
    for sd in seeds:
        for pick in (picks[sd] if trials is None else picks[sd][:trials]):
            r = pb.run_portfolio(
                {c: dfs[c] for c in pick}, {c: status[c] for c in pick}, wd,
                initial_capital=INITIAL_CAPITAL, slots=slots,
                market_filter_dates={c: mf.get(c, set()) for c in pick},
                # 콜러블은 자산곡선 이력을 들고 있다 — 실행마다 새로 만든다.
                risk_scale_by_date=new_scale(),
                heat_cap_pct=cap, heat_basis=basis, invest_ratio=iratio)
            m = metrics(r)
            m["cash"] = r.get("avg_cash_ratio", 0.0)
            out.append(m)
    return out


def exposure_neutral(dfs, status, mf, dates, picks, seeds, slots, cap_cfg, new_scale, args):
    """[채택 규칙 (A)] 진입대비가 이긴 것이 다이얼인가, 그냥 노출을 더 쓴 것인가.

    진입대비는 캡을 덜 물리므로 평균 투입자본이 올라간다. 그 이득이 레버리지라면
    '현재가대비를 같은 노출로 스케일업한 대조군'에게 져야 한다. 레버리지는 다이얼
    결정이 아니라 시드 결정이다([[seed-slot-sizing]]).
    """
    deployed = lambda ms: 100.0 - float(np.mean([m["cash"] for m in ms]))  # noqa: E731
    whole = list(dates)
    cal = max(2, args.trials // 3)
    print(f"\n{'=' * 100}\n[규칙 A] 노출 중립 대조 (보정 {len(seeds)}×{cal}회)\n{'=' * 100}",
          flush=True)

    arm = _run_arm(dfs, status, mf, picks, seeds, whole, slots, new_scale,
                   cap_cfg, "cost", trials=cal)
    base = _run_arm(dfs, status, mf, picks, seeds, whole, slots, new_scale,
                    cap_cfg, "mark", trials=cal)
    d_arm, d_base = deployed(arm), deployed(base)
    print(f"  진입대비   투입자본 {d_arm:.3f}%")
    print(f"  현재가대비 투입자본 {d_base:.3f}%")
    if d_arm - d_base <= args.tol:
        print(f"  차이 {d_arm - d_base:+.3f}%p ≤ 허용 {args.tol}%p — 규칙 (A)는 걸리지 않는다.")
        return

    default_ratio = 1.0 / max(1, slots)
    print(f"  {d_arm - d_base:+.3f}%p 더 쓴다 — 현재가대비를 같은 노출로 올린다"
          f"(invest_ratio 할선법, 현행 {default_ratio:.4f})", flush=True)
    pts = [(default_ratio, d_base)]
    guess = default_ratio * (d_arm / max(d_base, 1e-9))
    for it in range(args.max_iter):
        guess = min(1.0, max(1e-4, guess))
        got = deployed(_run_arm(dfs, status, mf, picks, seeds, whole, slots, new_scale,
                                cap_cfg, "mark", iratio=guess, trials=cal))
        print(f"    {it + 1}회 invest_ratio={guess:.4f} → 투입자본 {got:.3f}% "
              f"(목표 {d_arm:.3f}%)", flush=True)
        pts.append((guess, got))
        if abs(got - d_arm) <= args.tol:
            break
        ps = sorted(set(pts))
        (r0, y0), (r1, y1) = ps[0], ps[-1]
        if abs(y1 - y0) < 1e-6:
            print("    투입자본이 비중에 반응하지 않는다(포화) — 여기서 멈춘다.")
            break
        guess = r0 + (d_arm - y0) * (r1 - r0) / (y1 - y0)
    ratio = min(pts, key=lambda p: abs(p[1] - d_arm))[0]
    print(f"  → 노출을 맞추는 비중 {ratio:.4f} (남은 오차 "
          f"{min(abs(p[1] - d_arm) for p in pts):.3f}%p)", flush=True)

    W = [("전체", whole)] + list(audit_windows(dates, args.subperiods))
    print(f"\n{'구간':<10}{'진입대비 수익%':>14}{'중립 대조 수익%':>16}{'투입% (안/대조)':>18}"
          f"{'MDD (안/대조)':>18}{'승-무-패':>10}")
    for wn, wd in W:
        a = _run_arm(dfs, status, mf, picks, seeds, wd, slots, new_scale, cap_cfg, "cost")
        b = _run_arm(dfs, status, mf, picks, seeds, wd, slots, new_scale, cap_cfg,
                     "mark", iratio=ratio)
        win = sum(1 for x, y in zip(a, b) if x["ret"] > y["ret"] + 1e-9)
        tie = sum(1 for x, y in zip(a, b) if abs(x["ret"] - y["ret"]) <= 1e-9)
        g = lambda ms, k: float(np.mean([m[k] for m in ms]))  # noqa: E731
        dep = f"{100 - g(a, 'cash'):.1f} / {100 - g(b, 'cash'):.1f}"
        mdd = f"{g(a, 'mdd'):.1f} / {g(b, 'mdd'):.1f}"
        wl = f"{win}-{tie}-{len(a) - win - tie}"
        print(f"{wn:<10}{g(a, 'ret'):>14.1f}{g(b, 'ret'):>16.1f}{dep:>18}{mdd:>18}{wl:>10}",
              flush=True)
    print("\n[판정] 같은 노출에서도 진입대비가 이기면 그 이득은 레버리지가 아니라 정의의 우위다.")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=3650)
    ap.add_argument("--trials", type=int, default=12)
    ap.add_argument("--sample", type=int, default=25)
    ap.add_argument("--seeds", default="20260816,7,101")
    ap.add_argument("--slots", type=int, default=None)
    ap.add_argument("--subperiods", type=int, default=3)
    ap.add_argument("--neutral", action="store_true",
                    help="채택 규칙 (A) — 진입대비가 늘린 노출만큼 현재가대비를 올려 짝비교")
    ap.add_argument("--tol", type=float, default=0.25, help="투입자본 허용 오차(%%p)")
    ap.add_argument("--max-iter", type=int, default=6)
    args = ap.parse_args()
    seed_notice(len(args.seeds.split(",")), example="--seeds 20260816,7,101")
    slots = args.slots or getattr(config, "SYSTEM_MAX_HOLDINGS", 4)
    seeds = [int(x) for x in args.seeds.split(",")]
    cap_cfg = getattr(config, "SYSTEM_MAX_PORTFOLIO_RISK", 10.0)

    config.session.load_stock_config()
    live = [(s["code"], s["name"]) for s in config.session.stock_data.get("stocks_kr", [])]
    dfs, mf, dates, failed = pb.prepare_universe(live, args.days)
    print(f"[준비] {len(dfs)}종목 · 거래일 {len(dates)} · 슬롯 {slots} · 캡 {cap_cfg:g}%"
          + (f" · 제외 {failed}" if failed else ""), flush=True)

    thr = {"BUY_SCORE": config.ANALYSIS_THRESHOLDS["BUY_SCORE"],
           "BUY_RSI_MAX": config.ANALYSIS_THRESHOLDS["BUY_RSI_MAX"],
           "RISE_SCORE": config.ANALYSIS_THRESHOLDS["RISE_SCORE"],
           "WEIGHTS": config.SCORING_WEIGHTS}
    status = pb.precompute_status(dfs, thr)
    new_scale = new_scale_fn_factory(dates, args.days)

    W = audit_windows(dates, args.subperiods, whole=True)
    codes = list(dfs)
    picks = {sd: [random.Random(sd * 17 + i).sample(codes, min(args.sample, len(codes)))
                  for i in range(args.trials)] for sd in seeds}

    print(f"\n표본 {args.sample}종목 · {args.trials}회 × 씨드 {len(seeds)}개 "
          f"= 팔당 {args.trials * len(seeds)}회")
    for wn, wd in W:
        print(f"\n########## {wn} ({len(wd)} 거래일) ##########")
        print(f"{'팔':<20}{'수익%':>9}{'MDD%':>8}{'MAR':>7}{'PF':>6}{'청산':>6}"
              f"{'상위10%':>9}{'+30%':>6}{'슬롯':>6}{'캡차단':>8}{'수익 승-무-패':>14}{'MDD개선':>9}")
        base_res = None
        for label, cap, basis in ARMS:
            res, blocked = [], []
            for sd in seeds:
                for pick in picks[sd]:
                    r = pb.run_portfolio(
                        {c: dfs[c] for c in pick}, {c: status[c] for c in pick}, wd,
                        initial_capital=INITIAL_CAPITAL, slots=slots,
                        market_filter_dates={c: mf.get(c, set()) for c in pick},
                        risk_scale_by_date=new_scale(),
                        heat_cap_pct=(cap_cfg if cap is None else cap),
                        heat_basis=basis)
                    res.append(metrics(r))
                    blocked.append(r.get("heat_capped_buys", 0) + r.get("heat_capped_pyr", 0))
            g = lambda key: float(np.mean([m[key] for m in res]))  # noqa: E731
            if base_res is None:
                base_res = res
                wl, dd = "—", "—"
            else:
                win = sum(1 for x, y in zip(res, base_res) if x["ret"] > y["ret"] + 1e-9)
                tie = sum(1 for x, y in zip(res, base_res) if abs(x["ret"] - y["ret"]) <= 1e-9)
                wl = f"{win}-{tie}-{len(res) - win - tie}"
                better = sum(1 for x, y in zip(res, base_res) if x["mdd"] > y["mdd"] + 1e-9)
                dd = f"{better}/{len(res)}"
            print(f"{label:<20}{g('ret'):>9.1f}{g('mdd'):>8.1f}{g('mar'):>7.2f}{g('pf'):>6.2f}"
                  f"{g('n'):>6.0f}{g('top10'):>9.1f}{g('big'):>6.1f}{g('slots'):>6.2f}"
                  f"{float(np.mean(blocked)):>8.1f}{wl:>14}{dd:>9}", flush=True)

    print("\n[읽는 법] 승-무-패·MDD개선은 모두 **팔1(캡 해제) 대비** 짝비교다.")
    print("  · 팔2가 팔1과 거의 같고 캡차단도 0에 가까우면 → 진입대비 캡은 거의 안 걸리는 보험이다.")
    print("  · 팔3이 팔1보다 MDD가 뚜렷이 좋으면 → 종전 정의의 조임이 실제 방어였다는 뜻이고,")
    print("    그 방어를 팔2가 얼마나 잃었는지가 이번 변경의 대가다.")
    print("  · 팔3의 수익 손실이 크면 → 그 방어의 값이 비쌌다는 뜻(승자를 못 태운 대가).")

    if args.neutral:
        exposure_neutral(dfs, status, mf, dates, picks, seeds, slots, cap_cfg,
                         new_scale, args)


if __name__ == "__main__":
    main()
