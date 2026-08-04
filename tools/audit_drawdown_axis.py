"""계좌 드로다운 축(RISK_SCALING_PARAMS의 DD_*)에 실증 근거가 있는가.

[왜] 리스크 배수의 세 축 중 국면·휩소율은 백테스트 근거가 config 주석에 남아 있는데,
드로다운 축만 '터틀식'이라는 출처 표기 외에 이 시스템에서의 실측이 없다. 실거래 개시
전에 근거 없는 다이얼을 남겨두지 않기 위해 확인한다.

[왜 포트폴리오 백테스트인가] 이 축은 **계좌 자산곡선 자신**에 반응하는 피드백 루프다.
지수 프록시(tools/audit_market_axes.drawdown_scale)로는 슬롯·사이징·손절이 만드는
실제 자산곡선을 흉내낼 수 없다. run_portfolio에 콜러블 risk_scale을 넘겨,
시뮬레이션이 자기 자산곡선의 HWM을 보고 배수를 정하게 한다(실운영과 같은 구조).

[비교 기준] 이미 있는 축(국면×휩소율) 위에 드로다운 축을 **추가**했을 때 무엇이
달라지는가를 본다. 축 단독 성능이 아니라 한계 기여가 채택 근거이기 때문이다.

[실행] python tools/audit_drawdown_axis.py [--days 1095] [--trials 30] [--sample 30]
"""
import argparse
import os
import random
import sys
from datetime import datetime, timedelta

import numpy as np
import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import config  # noqa: E402
from modules import portfolio_backtest as pb  # noqa: E402
from tools.audit_market_axes import (  # noqa: E402
    load_index, regime_scale, regime_series, whipsaw_scale,
)

INITIAL_CAPITAL = 5_000_000   # 실거래 시드와 같게 둔다(seed-slot-sizing)


# ---------------------------------------------------------------------------
# 배수 산출
# ---------------------------------------------------------------------------
def market_scale_by_date(dates, days):
    """국면×휩소율 배수를 날짜별로. 두 시장 중 **열위 쪽**(trader.risk_scale과 동일)."""
    start = (datetime.now() - timedelta(days=days + 400)).strftime("%Y-%m-%d")
    combined = {}
    for ticker in ("KS11", "KQ11"):
        idx, close = load_index(ticker, start)
        regimes, whips = regime_series(idx, close)
        scale = regime_scale(regimes) * whipsaw_scale(whips)
        for d, s in zip(idx.strftime("%Y%m%d"), scale):
            combined[d] = min(combined.get(d, 1.0), float(s))
    # 지수 휴장일 등으로 비는 날은 직전 값을 잇는다(실운영도 마지막 판정을 유지한다).
    out, last = {}, 1.0
    for d in dates:
        last = combined.get(d, last)
        out[d] = last
    return out


def make_scale_fn(mkt_scale, dd_params, use_market=True):
    """run_portfolio에 넘길 콜러블. 자산곡선 피드백으로 드로다운 배수를 계산한다.

    dd_params=None이면 드로다운 축을 끈다(대조군).
    """
    hist = []   # [(date, equity)] — 시뮬레이션이 진행되며 쌓인다

    def fn(day, equity):
        scale = mkt_scale.get(day, 1.0) if use_market else 1.0
        hist.append((day, float(equity)))
        if dd_params:
            look, l1, s1, l2, s2 = dd_params
            if look and look > 0:
                # 룩백은 실운영과 같이 **달력일** 기준(DD_LOOKBACK_DAYS).
                cutoff = (pd.to_datetime(day, format="%Y%m%d")
                          - timedelta(days=look)).strftime("%Y%m%d")
                window = [e for d, e in hist if d >= cutoff]
            else:
                window = [e for _d, e in hist]
            hwm = max(window) if window else equity
            dd = (1.0 - equity / hwm) * 100.0 if hwm > 0 else 0.0
            if l2 > 0 and dd >= l2:
                scale *= s2
            elif l1 > 0 and dd >= l1:
                scale *= s1
        return scale

    return fn


# ---------------------------------------------------------------------------
# 비교 대상
# ---------------------------------------------------------------------------
def variants():
    """비교 대상. **config 값을 그대로 한 행으로 쓰지 않는다** — 감사 결과에 따라 config를
    바꾸면 다음 실행에서 그 행이 다른 설정을 가리켜, 비교하려던 후보가 조용히 사라진다
    (2026-08-04에 실제로 겪었다: '현행'과 '약하게'가 같은 값이 되어 0.75/0.5가 누락됐다).
    후보는 전부 명시하고, config 값은 어느 행에 해당하는지 표시만 한다.
    """
    return [
        ("축 없음(대조군)",       None),
        ("구 5/10 .75/.5 L90",   (90, 5.0, 0.75, 10.0, 0.5)),
        ("얕게  3/6  .75/.5 L90", (90, 3.0, 0.75, 6.0, 0.5)),
        ("깊게  8/15 .75/.5 L90", (90, 8.0, 0.75, 15.0, 0.5)),
        ("약하게 5/10 .90/.80 L90", (90, 5.0, 0.90, 10.0, 0.80)),
        ("강하게 5/10 .50/.25 L90", (90, 5.0, 0.50, 10.0, 0.25)),
        ("룩백 60",               (60, 5.0, 0.75, 10.0, 0.5)),
        ("룩백 180",              (180, 5.0, 0.75, 10.0, 0.5)),
        ("룩백 전체",             (0, 5.0, 0.75, 10.0, 0.5)),
    ]


# ---------------------------------------------------------------------------
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=1095)
    ap.add_argument("--trials", type=int, default=30)
    ap.add_argument("--sample", type=int, default=30)
    ap.add_argument("--slots", type=int, default=None)
    ap.add_argument("--no-market", action="store_true",
                    help="국면·휩소율 축을 끄고 드로다운 축만 단독으로 본다")
    args = ap.parse_args()

    slots = args.slots or getattr(config, "SYSTEM_MAX_HOLDINGS", 4)

    # [필수] 시장 필터의 지수 선택(KOSPI/KOSDAQ)은 config.session.stock_data 를 본다.
    #  JSON 을 직접 읽어 targets 만 만들면 세션은 빈 채로 남아 전 종목이 KOSPI 로 취급된다.
    config.session.load_stock_config()
    targets = [(s["code"], s["name"])
               for s in config.session.stock_data.get("stocks_kr", [])]
    print(f"[준비] 관심종목 {len(targets)}개 · {args.days}일 · 슬롯 {slots} · 시드 {INITIAL_CAPITAL:,}원")

    dfs, mf_dates, dates, failed = pb.prepare_universe(targets, args.days)
    print(f"[준비] 사용 가능 {len(dfs)}종목 / 거래일 {len(dates)}일" +
          (f" · 제외 {len(failed)}개" if failed else ""))
    if len(dfs) < args.sample:
        args.sample = max(5, len(dfs) // 2)
        print(f"[준비] 표본 수를 {args.sample}로 조정")

    thresholds = {
        "BUY_SCORE": config.ANALYSIS_THRESHOLDS["BUY_SCORE"],
        "BUY_RSI_MAX": config.ANALYSIS_THRESHOLDS["BUY_RSI_MAX"],
        "RISE_SCORE": config.ANALYSIS_THRESHOLDS["RISE_SCORE"],
        "WEIGHTS": config.SCORING_WEIGHTS,
    }
    status = pb.precompute_status(dfs, thresholds)
    mkt = {} if args.no_market else market_scale_by_date(dates, args.days)
    if not args.no_market:
        avg = np.mean(list(mkt.values()))
        print(f"[준비] 국면×휩소율 평균 배수 {avg:.3f} · 축소일 "
              f"{sum(1 for v in mkt.values() if v < 0.999) / len(mkt) * 100:.1f}%")

    codes = list(dfs.keys())
    vs = variants()
    results = {name: [] for name, _ in vs}

    rng = random.Random(20260804)
    for t in range(args.trials):
        pick = rng.sample(codes, min(args.sample, len(codes)))
        sub_dfs = {c: dfs[c] for c in pick}
        sub_status = {c: status[c] for c in pick}
        sub_mf = {c: mf_dates.get(c, set()) for c in pick}

        for name, dd in vs:
            fn = make_scale_fn(mkt, dd, use_market=not args.no_market)
            r = pb.run_portfolio(sub_dfs, sub_status, dates,
                                 initial_capital=INITIAL_CAPITAL, slots=slots,
                                 market_filter_dates=sub_mf, risk_scale_by_date=fn)
            mdd = r["mdd"]
            results[name].append({
                "ret": r["total_return"], "mdd": mdd, "pf": r["pf"],
                "cash": r["avg_cash_ratio"],
                "wr": r["win"] / max(1, r["win"] + r["loss"]) * 100,
                # MAR = 수익 / 최대낙폭. 리스크 축의 가치는 '낙폭을 줄였는가'가 아니라
                #  '같은 낙폭당 수익이 늘었는가'로 봐야 한다.
                "mar": r["total_return"] / abs(mdd) if mdd else float("nan"),
            })
        print(f"  시행 {t + 1}/{args.trials} 완료", end="\r", flush=True)
    print(" " * 40, end="\r")

    # ---------------- 보고 ----------------
    p = getattr(config, 'RISK_SCALING_PARAMS', {}) or {}
    print(f"\n[참고] 현재 config 값 = {p.get('DD_LEVEL_1')}/{p.get('DD_LEVEL_2')} "
          f"{p.get('DD_SCALE_1')}/{p.get('DD_SCALE_2')} L{p.get('DD_LOOKBACK_DAYS')}")
    base = results["축 없음(대조군)"]
    print(f"\n{'=' * 92}")
    print(f"드로다운 축 감사 — {args.trials}회 × {args.sample}종목 무작위 짝비교"
          + (" (드로다운 축 단독)" if args.no_market else " (국면×휩소율 위에 추가)"))
    print(f"{'=' * 92}")
    print(f"{'설정':<24}{'수익%':>9}{'MDD%':>8}{'MAR':>7}{'PF':>6}{'현금%':>7}"
          f"{'ΔMDD':>8}{'Δ수익':>9}{'MDD승':>7}{'MAR승':>7}{'효율':>7}{'최악MDD':>9}")
    print("-" * 100)

    for name, _ in vs:
        rs = results[name]
        med = lambda k: float(np.median([x[k] for x in rs]))  # noqa: E731
        # run_portfolio의 mdd는 음수(-20.6 = 최대 20.6% 낙폭)다. 0에 가까울수록 개선.
        d_mdd = float(np.median([a["mdd"] - b["mdd"] for a, b in zip(rs, base)]))
        d_ret = float(np.median([a["ret"] - b["ret"] for a, b in zip(rs, base)]))
        wins = sum(1 for a, b in zip(rs, base) if a["mdd"] > b["mdd"])
        mar_wins = sum(1 for a, b in zip(rs, base) if a["mar"] > b["mar"])
        worst = float(np.min([x["mdd"] for x in rs]))   # 꼬리: 최악 시행의 낙폭
        # 효율 = MDD 이득 1%p 당 치른 수익 비용의 역수. 클수록 싸게 방어한다.
        eff = (d_mdd / abs(d_ret)) if d_ret < 0 and d_mdd > 0 else (float('inf') if d_ret >= 0 and d_mdd > 0 else 0.0)
        is_base = name.startswith("축 없음")
        eff_s = "—" if is_base else (f"{eff:.2f}" if np.isfinite(eff) else "∞")
        print(f"{name:<24}{med('ret'):>9.1f}{med('mdd'):>8.1f}{med('mar'):>7.2f}"
              f"{med('pf'):>6.2f}{med('cash'):>7.1f}"
              f"{d_mdd:>+8.2f}{d_ret:>+9.2f}"
              f"{'—' if is_base else f'{wins}/{len(rs)}':>7}"
              f"{'—' if is_base else f'{mar_wins}/{len(rs)}':>7}{eff_s:>7}{worst:>9.1f}")

    print("-" * 100)
    print("ΔMDD·Δ수익은 대조군(축 없음) 대비 중앙값. ΔMDD +는 낙폭 축소(개선).")
    print("MAR = 수익/|MDD| (위험조정수익). 리스크 축의 가치는 낙폭 감소가 아니라 MAR 개선으로 봐야 한다.")
    print("최악MDD = 전 시행 중 가장 깊은 낙폭(꼬리). 리스크 축은 이 값을 지켜야 존재 이유가 있다.")
    print("효율 = MDD 이득(%p) / 수익 비용(%p). 참고: 휩소율 최소배수 채택 기준은 0.70,")
    print("      기각된 설정(WS_MIN 0.60)이 0.13이었다 (config.RISK_SCALING_PARAMS 주석).")


if __name__ == "__main__":
    main()
