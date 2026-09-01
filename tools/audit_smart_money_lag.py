"""수급 축이 백테스트에서 하루 앞을 보는가 — 그 크기를 잰다.

[무엇이 문제인가] backtest._append_smart_money_signal 은 D일 행의 smart_money 를
 **D일 하루치 순매수 최종값**으로 만든다. 그런데 실매매가 D일에 보는 값은
 '장중 잠정치가 천천히 갱신되는 일단위 집계'(api/quotes/price.py)라, 그날 순매수가
 얼마로 끝날지 모른다. 장중 스캔 모드는 더 노골적이다 — intraday_bars 가 09:30 봉부터
 그날의 확정 수급을 달고 있다(비가격 컬럼 재사용). 즉 **백테스트만 하루 앞을 본다.**

[왜 기존 테스트가 못 잡나] tests/test_portfolio_backtest.py 의 선견 회귀는 미래
 **날짜**를 잘라 비교한다. 같은 날 안에서 새는 것은 구조적으로 보이지 않는다.

  팔1 lag=0 (현행) — D일 확정 수급을 D일에 쓴다. 선견을 포함한다.
  팔2 lag=1        — D-1까지 확정된 수급만 쓴다. 실매매가 확실히 아는 것과 같다.

[읽는 법] 이 축을 통째로 끈 것(2026-08-24 실측: 수익 67.51%→67.86%)이 대략의 상한이라
 결론을 뒤집을 크기는 아닐 것으로 본다. 이 도구는 그 짐작을 수치로 바꾼다.
   · 팔1이 팔2보다 뚜렷이 좋으면 → 그 차이는 실매매로 옮겨갈 수 없는 몫이다.
   · 사실상 같으면 → 선견은 실재하되 값이 없다. lag=1 로 굳혀 걱정을 끝낸다.
 진단부는 판정이 실제로 몇 번 갈리는지 먼저 보여준다. 여기가 0에 가까우면 아래 표의
 차이는 전부 잡음이다.

[실측 2026-09-01] 44종목·2,449거래일 준비 → 25종목 표본 12회 × 씨드 3개, 출처 KRX 44/44.
   전체 수익 321.3(lag=0) vs 315.0(lag=1) · MDD -32.8 vs -33.0 · 짝비교 22-1-13.
   구간별 승-무-패 9-16-11 / 6-12-18 / 17-2-17 — **방향이 구간마다 뒤집힌다.** lag=1 이
   더 자주 이기는데 평균은 낮다(꼬리 몇 건에 몰려 있다).
   진단(봉 99,234): 플래그 뒤집힘 33.03% · 점수 변화 11.29% · **상태 변화 0.28% ·
   매수 가능 여부 변화 0.01%** — 원 축에서는 크지만 하류(OBV 와의 OR + 점수 문턱)가
   거의 전부 흡수한다.
   → 선견은 **실재하지만 값이 없다.** 채택 규칙 어느 쪽에도 안 걸리는 잡음이라 기본값을
   0(종전 동작)으로 유지했다 — 1로 굳히면 이전 감사 수치와의 연속성이 끊긴다.

[주의] 이 축은 KRX_ID/KRX_PW 가 있어야 전 구간이 켜진다. 자격증명 없이 돌리면 두 팔이
 거의 같아지는 것이 당연하다 — 준비 단계가 출처를 찍으니 반드시 확인할 것.

[실행] python3 tools/audit_smart_money_lag.py --trials 12 --sample 25 --seeds 20260816,7,101
"""
import argparse
import os
import random
import sys

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tools.audit_common import seed_notice, windows as audit_windows  # noqa: E402

import config  # noqa: E402
from modules import backtest  # noqa: E402
from modules import portfolio_backtest as pb  # noqa: E402
from tools.audit_defensive_sector import (  # noqa: E402
    INITIAL_CAPITAL, metrics, new_scale_fn_factory,
)


def _thresholds():
    return {"BUY_SCORE": config.ANALYSIS_THRESHOLDS["BUY_SCORE"],
            "BUY_RSI_MAX": config.ANALYSIS_THRESHOLDS["BUY_RSI_MAX"],
            "RISE_SCORE": config.ANALYSIS_THRESHOLDS["RISE_SCORE"],
            "WEIGHTS": config.SCORING_WEIGHTS}


def build(lag, targets, days):
    """lag 를 걸고 유니버스를 준비한다. 일봉·수급 모두 캐시라 두 번째부터는 싸다."""
    backtest.SMART_MONEY_LAG = lag
    try:
        return pb.prepare_universe(targets, days)
    finally:
        backtest.SMART_MONEY_LAG = 0


def diagnose(dfs_a, dfs_b, st_a, st_b):
    """판정이 실제로 몇 번 갈리는가 — 표의 차이를 읽기 전에 봐야 하는 숫자."""
    bars = flag = score = state = buyable = 0
    for code in dfs_a:
        a, b = dfs_a[code], dfs_b.get(code)
        if b is None or len(a) != len(b):
            continue
        bars += len(a)
        flag += int((a["smart_money"].values != b["smart_money"].values).sum())
        sa, sb = st_a.get(code, {}), st_b.get(code, {})
        for day, va in sa.items():
            vb = sb.get(day)
            if vb is None:
                continue
            if abs(float(va[0]) - float(vb[0])) > 1e-9:
                score += 1
            if va[3] != vb[3]:
                state += 1
            if bool(va[2]) != bool(vb[2]):
                buyable += 1
    pct = lambda n: (100.0 * n / bars) if bars else 0.0   # noqa: E731
    print(f"\n{'=' * 100}\n[진단] 하루 늦추면 무엇이 달라지나 (봉 {bars:,})\n{'=' * 100}")
    print(f"  수급 플래그가 뒤집힌 봉 : {flag:,} ({pct(flag):.2f}%)")
    print(f"  점수가 달라진 봉        : {score:,} ({pct(score):.2f}%)")
    print(f"  상태(매수/관망…)가 달라진 봉 : {state:,} ({pct(state):.2f}%)")
    print(f"  매수 가능 여부가 뒤집힌 봉   : {buyable:,} ({pct(buyable):.2f}%)")
    if not flag:
        print("  → 플래그가 한 번도 안 뒤집혔다. 수급 축이 꺼진 채로 돌았다는 뜻이다"
              " (KRX_ID/KRX_PW 확인). 아래 표는 의미가 없다.", flush=True)
    return flag


def run_arm(dfs, status, mf, picks, seeds, wd, slots, new_scale):
    out = []
    for sd in seeds:
        for pick in picks[sd]:
            r = pb.run_portfolio(
                {c: dfs[c] for c in pick}, {c: status[c] for c in pick}, wd,
                initial_capital=INITIAL_CAPITAL, slots=slots,
                market_filter_dates={c: mf.get(c, set()) for c in pick},
                # 콜러블은 자산곡선 이력을 들고 있다 — 실행마다 새로 만든다.
                risk_scale_by_date=new_scale())
            out.append(metrics(r))
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=3650)
    ap.add_argument("--trials", type=int, default=12)
    ap.add_argument("--sample", type=int, default=25)
    ap.add_argument("--seeds", default="20260816,7,101")
    ap.add_argument("--slots", type=int, default=None)
    ap.add_argument("--subperiods", type=int, default=3)
    args = ap.parse_args()
    seed_notice(len(args.seeds.split(",")), example="--seeds 20260816,7,101")
    slots = args.slots or getattr(config, "SYSTEM_MAX_HOLDINGS", 4)
    seeds = [int(x) for x in args.seeds.split(",")]

    config.session.load_stock_config()
    live = [(s["code"], s["name"]) for s in config.session.stock_data.get("stocks_kr", [])]

    dfs0, mf, dates, failed = build(0, live, args.days)
    src = backtest.smart_money_source_summary()
    print(f"[준비] lag=0 · {len(dfs0)}종목 · 거래일 {len(dates)} · 슬롯 {slots} · "
          f"수급 출처 {src}" + (f" · 제외 {failed}" if failed else ""), flush=True)
    if not src.get("KRX"):
        print("[경고] KRX 수급이 하나도 없다 — 이 축이 꺼진 채로 재고 있다"
              " (KRX_ID/KRX_PW 확인).", flush=True)
    dfs1, _mf1, dates1, _f1 = build(1, live, args.days)
    if dates1 != dates:
        print("[경고] 두 팔의 거래일이 다르다 — 짝비교가 성립하지 않는다.", flush=True)

    thr = _thresholds()
    st0 = pb.precompute_status(dfs0, thr)
    st1 = pb.precompute_status(dfs1, thr)
    diagnose(dfs0, dfs1, st0, st1)

    new_scale = new_scale_fn_factory(dates, args.days)
    codes = list(dfs0)
    picks = {sd: [random.Random(sd * 17 + i).sample(codes, min(args.sample, len(codes)))
                  for i in range(args.trials)] for sd in seeds}

    print(f"\n표본 {args.sample}종목 · {args.trials}회 × 씨드 {len(seeds)}개 "
          f"= 팔당 {args.trials * len(seeds)}회")
    for wn, wd in audit_windows(dates, args.subperiods, whole=True):
        print(f"\n########## {wn} ({len(wd)} 거래일) ##########")
        print(f"{'팔':<18}{'수익%':>9}{'MDD%':>8}{'MAR':>7}{'PF':>6}{'청산':>6}"
              f"{'상위10%':>9}{'+30%':>6}{'수익 승-무-패':>14}")
        base = None
        for label, dfs, st in (("lag=0 (현행)", dfs0, st0), ("lag=1 (선견 제거)", dfs1, st1)):
            res = run_arm(dfs, st, mf, picks, seeds, wd, slots, new_scale)
            g = lambda k: float(np.mean([m[k] for m in res]))    # noqa: E731
            if base is None:
                base, wl = res, "—"
            else:
                win = sum(1 for x, y in zip(res, base) if x["ret"] > y["ret"] + 1e-9)
                tie = sum(1 for x, y in zip(res, base) if abs(x["ret"] - y["ret"]) <= 1e-9)
                wl = f"{win}-{tie}-{len(res) - win - tie}"
            print(f"{label:<18}{g('ret'):>9.1f}{g('mdd'):>8.1f}{g('mar'):>7.2f}{g('pf'):>6.2f}"
                  f"{g('n'):>6.0f}{g('top10'):>9.1f}{g('big'):>6.1f}{wl:>14}", flush=True)

    print("\n[판정] 승-무-패는 lag=0 대비 짝비교다.")
    print("  · lag=1 이 뚜렷이 지면 → 그만큼이 선견이었고, 실매매로 옮겨갈 수 없는 몫이다.")
    print("  · 무승부가 대부분이면 → 선견은 실재하되 값이 없다. lag=1 로 굳히면 끝난다.")


if __name__ == "__main__":
    main()
