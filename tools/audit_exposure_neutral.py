"""노출 중립 대조 — 그 이득이 다이얼의 우위인가, 그냥 돈을 더 넣은 것인가.

[왜 있는가] 채택 규칙 (A)는 이렇다. **평균 투입자본을 올리는 안은, 같은 평균 노출로
 스케일업한 기준선을 이겨야 한다.** 못 이기면 그 이득은 다이얼이 아니라 레버리지이고,
 레버리지는 다이얼 결정이 아니라 **시드 결정**이다(늘리려면 시드를 늘리면 된다).
 그런데 이 조항은 2026-08-23까지 규칙 메모에 '도구 미구현'으로 남아 있었다 — 감사마다
 손으로 다시 짜야 했고, 그래서 대부분의 축에서 그냥 건너뛰었다.

[왜 이 함정이 흔한가] 청산을 늦추는 다이얼(시간청산 연장·트레일링 완화·손절 확대)은
 거의 전부 **보유일을 늘려 평균 투입자본을 올린다.** 10년 복리에서 투입자본이 1%p만
 높아도 총수익이 눈에 띄게 벌어진다. 승-무-패 표는 그 차이를 다이얼의 공으로 적는다.

[어떻게 맞추는가] 노출의 자로 **평균 투입자본 = 100 - avg_cash_ratio(%)** 를 쓴다.
 평균 슬롯 수가 아니다 — 슬롯이 같아도 종목당 투입액이 다르면 노출이 다르다.
 기준선의 `invest_ratio`(종목당 기초 비중)를 할선법으로 훑어 **안과 같은 투입자본**을
 내는 값을 찾고, 그 스케일업 기준선과 짝비교한다.

[적용 범위] 안이 노출을 **올리지 않으면**(오차 이내이거나 오히려 낮으면) 규칙 (A)는
 애초에 걸리지 않는다. 그때는 보정 없이 그대로 짝비교만 찍고 그 사실을 말한다.
 노출을 **낮추면서** 이기는 안은 규칙 (A)의 반대편이라 오히려 강한 결과다.

[읽을 때 주의]
 · 스케일업 기준선은 실매매 설정이 아니다. 이것은 **대조군**이지 제안이 아니다.
 · 절대 수치는 표본·기간·비용 국면에 좌우된다. 비교해도 되는 것은 같은 실행 안의 두 팔뿐이다.
 · 승-무-패는 같은 (씨드, 시행)의 종목 표본을 두 팔에 그대로 물려 만든 **짝비교**다.

[실행]
  # 시간청산 25일이 15일을 이긴 것이 레버리지인지 본다
  python3 tools/audit_exposure_neutral.py --set SELL_STRATEGY.TIME_STOP_DAYS=25
  # 손절 확대
  python3 tools/audit_exposure_neutral.py --set SELL_STRATEGY.ATR_STOP_MULTIPLIER=2.5
"""
import argparse
import os
import random
import sys

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import config  # noqa: E402
from modules import portfolio_backtest as pb  # noqa: E402
from tools.audit_common import base_metrics, seed_notice, windows as audit_windows  # noqa: E402
from tools.audit_defensive_sector import INITIAL_CAPITAL, new_scale_fn_factory  # noqa: E402

# --set 이 가리킬 수 있는 config 딕셔너리. 최상위 값은 SETTINGS.<이름> 으로 쓴다.
GROUPS = ("SELL_STRATEGY", "ANALYSIS_THRESHOLDS", "RISK_SCALING_PARAMS",
          "INDICATOR_PARAMS", "MARKET_REGIME_PARAMS", "SCORING_WEIGHTS")


def parse_set(text):
    """'GROUP.KEY=VALUE' → (그룹, 키, 파이썬 값). 값은 리터럴로 읽는다."""
    import ast
    head, _, raw = text.partition("=")
    if not raw:
        raise argparse.ArgumentTypeError(f"'{text}' — GROUP.KEY=VALUE 형태로 쓸 것")
    group, _, key = head.partition(".")
    if not key:
        raise argparse.ArgumentTypeError(f"'{text}' — 그룹이 빠졌다. 예: SELL_STRATEGY.{head}=…")
    if group not in GROUPS and group != "SETTINGS":
        raise argparse.ArgumentTypeError(
            f"'{group}' 은 모르는 그룹이다. 쓸 수 있는 것: {', '.join(GROUPS)}, SETTINGS")
    try:
        val = ast.literal_eval(raw)
    except Exception:
        val = raw                       # 문자열은 따옴표 없이도 받는다
    return (group, key, val)


def apply_overrides(pairs):
    """오버라이드를 걸고 **되돌릴 목록**을 돌려준다. 반드시 finally 에서 되돌릴 것.

    [주의] 키가 원래 없던 경우와 값이 None 인 경우를 구분한다 — 구분하지 않으면
    되돌릴 때 없던 키가 None 으로 남아 다음 실행이 조용히 달라진다.
    """
    MISSING = object()
    prev = []
    for group, key, val in pairs:
        if group == "SETTINGS":
            prev.append((None, key, getattr(config, key, MISSING)))
            setattr(config, key, val)
        else:
            d = getattr(config, group)
            prev.append((d, key, d.get(key, MISSING)))
            d[key] = val
    return prev, MISSING


def undo_overrides(prev, missing):
    for d, key, old in reversed(prev):
        if d is None:
            if old is missing:
                delattr(config, key)
            else:
                setattr(config, key, old)
        elif old is missing:
            d.pop(key, None)
        else:
            d[key] = old


def deployed(ms):
    """평균 투입자본(%) — 노출의 자. avg_cash_ratio 는 이미 %다."""
    return 100.0 - float(np.mean([m["cash"] for m in ms]))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--set", dest="sets", action="append", type=parse_set, default=[],
                    metavar="GROUP.KEY=VALUE",
                    help="검증할 안(案). 여러 번 쓸 수 있다. 예: SELL_STRATEGY.TIME_STOP_DAYS=25")
    ap.add_argument("--baseline-set", dest="base_sets", action="append", type=parse_set,
                    default=[], metavar="GROUP.KEY=VALUE",
                    help="기준선도 바꿔서 잰다(기본: 현행 설정 그대로)")
    ap.add_argument("--days", type=int, default=3650)
    ap.add_argument("--sample", type=int, default=25)
    ap.add_argument("--trials", type=int, default=12)
    ap.add_argument("--seeds", default="20260816,7,101")
    ap.add_argument("--slots", type=int, default=None)
    ap.add_argument("--subperiods", type=int, default=4)
    ap.add_argument("--seed-capital", type=int, default=INITIAL_CAPITAL)
    ap.add_argument("--calib-trials", type=int, default=None,
                    help="보정 단계의 시행 수(기본: --trials 의 절반). 보정은 대략만 맞으면 된다")
    ap.add_argument("--tol", type=float, default=0.15,
                    help="투입자본 허용 오차(%%p). 이 안에 들면 보정을 끝낸다")
    ap.add_argument("--max-iter", type=int, default=6, help="보정 반복 상한")
    args = ap.parse_args()
    seed_notice(len(args.seeds.split(",")), example="--seeds 20260816,7,101")
    if not args.sets:
        ap.error("--set 으로 검증할 안을 하나 이상 줄 것 "
                 "(예: --set SELL_STRATEGY.TIME_STOP_DAYS=25)")

    seeds = [int(x) for x in args.seeds.split(",")]
    slots = args.slots or getattr(config, "SYSTEM_MAX_HOLDINGS", 4)
    calib_trials = args.calib_trials or max(1, args.trials // 2)

    config.session.load_stock_config()
    targets = [(s["code"], s["name"])
               for s in config.session.stock_data.get("stocks_kr", [])]
    label = " · ".join(f"{g}.{k}={v!r}" for g, k, v in args.sets)
    print(f"[준비] 관심종목 {len(targets)}개 · {args.days}일 · 슬롯 {slots} "
          f"· 시드 {args.seed_capital:,}원\n[안] {label}", flush=True)

    dfs, mf, dates, failed = pb.prepare_universe(targets, args.days)
    thr = {"BUY_SCORE": config.ANALYSIS_THRESHOLDS["BUY_SCORE"],
           "BUY_RSI_MAX": config.ANALYSIS_THRESHOLDS["BUY_RSI_MAX"],
           "RISE_SCORE": config.ANALYSIS_THRESHOLDS["RISE_SCORE"],
           "WEIGHTS": config.SCORING_WEIGHTS}
    status = pb.precompute_status(dfs, thr)
    codes_all = list(dfs.keys())
    new_scale = new_scale_fn_factory(dates, args.days)
    print(f"[준비] 사용 {len(dfs)}종목 / 거래일 {len(dates)}일"
          + (f" · 제외 {len(failed)}" if failed else ""), flush=True)

    # 두 팔이 **같은 표본**을 본다 — 짝비교의 전제다.
    picks = {(sd, i): random.Random(sd * 7 + i).sample(codes_all,
                                                       min(args.sample, len(codes_all)))
             for sd in seeds for i in range(args.trials)}

    def run(wd, overrides, iratio, trials):
        prev, missing = apply_overrides(overrides)
        try:
            out = []
            for sd in seeds:
                for i in range(trials):
                    codes = picks[(sd, i)]
                    r = pb.run_portfolio(
                        {c: dfs[c] for c in codes}, {c: status[c] for c in codes}, wd,
                        initial_capital=args.seed_capital, slots=slots,
                        market_filter_dates={c: mf.get(c, set()) for c in codes},
                        # 콜러블은 자산곡선 이력을 들고 있다 — 실행마다 새로 만든다
                        #  (make_scale_fn 오염, 2026-08-16).
                        risk_scale_by_date=new_scale(),
                        invest_ratio=iratio)
                    m = base_metrics(r)
                    m["cash"] = r.get("avg_cash_ratio", 0.0)
                    m["slots"] = r.get("avg_slots", 0.0)
                    out.append(m)
            return out
        finally:
            undo_overrides(prev, missing)

    whole = list(dates)
    default_ratio = 1.0 / max(1, slots)

    # ── ① 두 팔의 노출을 잰다
    print(f"\n[①] 투입자본 측정 (씨드 {len(seeds)}개 × {calib_trials}회)", flush=True)
    arm_ms = run(whole, args.sets, None, calib_trials)
    base_ms = run(whole, args.base_sets, None, calib_trials)
    d_arm, d_base = deployed(arm_ms), deployed(base_ms)
    print(f"  안    투입자본 {d_arm:.3f}%   (평균슬롯 {np.mean([m['slots'] for m in arm_ms]):.3f})")
    print(f"  기준선 투입자본 {d_base:.3f}%   (평균슬롯 {np.mean([m['slots'] for m in base_ms]):.3f})")

    # ── ② 노출을 맞춘다 (안이 더 많이 넣을 때만)
    ratio, calibrated = None, False
    if d_arm - d_base <= args.tol:
        print(f"\n[②] 안이 노출을 올리지 않는다(차이 {d_arm - d_base:+.3f}%p ≤ 허용 {args.tol}%p)"
              f" — 규칙 (A)는 걸리지 않는다. 보정 없이 그대로 짝비교한다.", flush=True)
        if d_arm < d_base - args.tol:
            print("  ※ 오히려 **덜 넣는다.** 이 안이 이긴다면 규칙 (A)의 반대편이라 더 강한 결과다.")
    else:
        print(f"\n[②] 안이 노출을 {d_arm - d_base:+.3f}%p 더 쓴다 — 기준선을 같은 노출로 올린다."
              f"\n    (invest_ratio 를 할선법으로 훑는다. 현행 {default_ratio:.4f})", flush=True)
        # 할선법: (비중, 투입자본) 두 점에서 목표를 향해 밀고, 그 사이를 좁힌다.
        pts = [(default_ratio, d_base)]
        guess = default_ratio * (d_arm / max(d_base, 1e-9))
        for it in range(args.max_iter):
            guess = min(1.0, max(1e-4, guess))
            ms = run(whole, args.base_sets, guess, calib_trials)
            got = deployed(ms)
            print(f"    {it + 1}회 invest_ratio={guess:.4f} → 투입자본 {got:.3f}% "
                  f"(목표 {d_arm:.3f}%)", flush=True)
            pts.append((guess, got))
            if abs(got - d_arm) <= args.tol:
                break
            pts_s = sorted(set(pts))
            # 두 점의 기울기로 다음 비중을 민다. 기울기가 죽으면(포화) 더 밀어봐야 소용없다.
            (r0, y0), (r1, y1) = pts_s[0], pts_s[-1]
            if abs(y1 - y0) < 1e-6:
                print("    투입자본이 비중에 반응하지 않는다(포화) — 보정을 여기서 멈춘다.")
                break
            guess = r0 + (d_arm - y0) * (r1 - r0) / (y1 - y0)
        ratio = min(pts, key=lambda p: abs(p[1] - d_arm))[0]
        calibrated = True
        got = min(abs(p[1] - d_arm) for p in pts)
        print(f"  → 노출을 맞추는 비중 {ratio:.4f} (남은 오차 {got:.3f}%p)"
              + ("  ※ 허용 오차를 못 맞췄다 — 아래 표의 '투입%' 두 열을 직접 볼 것"
                 if got > args.tol else ""), flush=True)

    # ── ③ 짝비교
    W = [("전체", whole)] + list(audit_windows(dates, args.subperiods))
    head = "노출을 맞춘 기준선" if calibrated else "기준선(현행)"
    print(f"\n{'=' * 116}\n안 vs {head}   씨드 {len(seeds)}개 × {args.trials}회 "
          f"= {len(seeds) * args.trials}쌍\n{'=' * 116}")
    print(f"{'창':<10}{'승':>5}{'무':>4}{'패':>5}{'승률%':>7}{'안 수익%':>10}{'기준 수익%':>11}"
          f"{'차이':>9}{'안 MDD':>9}{'기준 MDD':>10}{'MDD상대':>9}"
          f"{'안 꼬리':>9}{'기준 꼬리':>10}{'안 투입%':>9}{'기준 투입%':>11}")
    rows = []
    for wn, wd in W:
        if not wd:
            continue
        a = run(wd, args.sets, None, args.trials)
        b = run(wd, args.base_sets, ratio, args.trials)
        w = sum(1 for x, y in zip(a, b) if x["ret"] > y["ret"] + 1e-9)
        tie = sum(1 for x, y in zip(a, b) if abs(x["ret"] - y["ret"]) <= 1e-9)
        f = lambda ms, k: float(np.mean([m[k] for m in ms]))  # noqa: E731
        mdd_rel = ((abs(f(a, "mdd")) - abs(f(b, "mdd")))
                   / max(abs(f(b, "mdd")), 1e-9) * 100)
        rows.append({"w": wn, "win": w, "loss": len(a) - w - tie,
                     "ret": f(a, "ret") - f(b, "ret"), "mdd_rel": mdd_rel,
                     "tail": f(a, "top10") - f(b, "top10")})
        print(f"{wn:<10}{w:>5}{tie:>4}{len(a) - w - tie:>5}{w / len(a) * 100:>7.1f}"
              f"{f(a, 'ret'):>10.2f}{f(b, 'ret'):>11.2f}{f(a, 'ret') - f(b, 'ret'):>9.2f}"
              f"{f(a, 'mdd'):>9.2f}{f(b, 'mdd'):>10.2f}{mdd_rel:>+9.1f}"
              f"{f(a, 'top10'):>9.2f}{f(b, 'top10'):>10.2f}"
              f"{100 - f(a, 'cash'):>9.2f}{100 - f(b, 'cash'):>11.2f}", flush=True)

    # ── ④ 판정
    whole_row = rows[0]
    subs = rows[1:]
    sub_win = sum(1 for r in subs if r["win"] > r["loss"])
    print(f"\n{'-' * 116}\n[규칙 (A) 노출 중립] 전체창 {whole_row['win']}-{whole_row['loss']} "
          f"({'승' if whole_row['win'] > whole_row['loss'] else '패'}) · "
          f"하위구간 우세 {sub_win}/{len(subs)}")
    if calibrated:
        print("  → 노출을 맞춘 기준선을 못 이기면 그 이득은 다이얼이 아니라 **레버리지**다. "
              "레버리지는 시드 결정이지 다이얼 결정이 아니다.")
    else:
        print("  → 안이 노출을 올리지 않으므로 규칙 (A)로 기각될 여지는 없다. "
              "위 표는 보정 없는 통상 짝비교다.")
    bad_mdd = [r["w"] for r in rows if r["mdd_rel"] >= 10.0]
    print(f"[규칙 (B) 낙폭 거부] 상대 10%+ 악화 창 {len(bad_mdd)}/{len(rows)}"
          f" {bad_mdd if bad_mdd else ''} → "
          + ("기각" if whole_row["mdd_rel"] >= 10.0 and len(bad_mdd) > len(rows) / 2
             else "통과"))
    bad_tail = [r["w"] for r in rows if r["tail"] < 0]
    print(f"[규칙 (C) 꼬리]     상위10% 축소 창 {len(bad_tail)}/{len(rows)}"
          f" {bad_tail if bad_tail else ''} → " + ("주의" if bad_tail else "통과"))
    print("\n[주의] 스케일업 기준선은 대조군이지 제안이 아니다. 절대 수치는 표본·기간·비용 "
          "국면에 좌우되므로, 비교해도 되는 것은 같은 실행 안의 두 팔뿐이다.")


if __name__ == "__main__":
    main()
