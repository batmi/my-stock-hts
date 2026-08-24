"""시간청산 다이얼(TIME_STOP_DAYS / TIME_STOP_MIN_PROFIT_RATE)에 실증 근거가 있는가.

[출발점] TIME_STOP_DAYS 는 '추세 전개에 충분한 시간을 준다'는 취지로 10 → 20 으로 완화됐다
 (0d08cec, 추세추종 전면 정합화). 설계 판단이었을 뿐 짝비교로 잰 적이 없다. 20일이 길어
 정체 포지션이 슬롯을 오래 물고 있는 것 아닌가 하는 의심에서 다시 잰다.

[먼저 짚을 것 — 이 조항은 손실 포지션만 자른다]
 발동 조건은 `holding_days >= TIME_STOP_DAYS and 수익률 < TIME_STOP_MIN_PROFIT_RATE(0.0)` 이고,
 여기에 상방 모멘텀 유예(매수 계열 상태 + 최근 5일 고점 ≥ 10일 고점)가 붙는다. 즉 수익 중인
 포지션은 이 조항으로 잘리지 않는다 — 일수를 줄이는 것은 '승자를 조기에 끊는 일'이 아니라
 '패자를 ATR손절(-9%대)까지 끌고 가기 전에 -2%대에서 방출하고 자리를 회전시키는 일'이다.
 그래서 두 다이얼을 함께 잰다. 일수(B)는 패자의 체류 시간을, 문턱(C)은 '승자도 자를 것인가'를
 정한다. 둘을 섞어 보면 추세추종 원칙이 어느 쪽에 있는지 드러난다.

[단위 주의] holding_days 는 **달력일**이다(백테스트·실매매 모두 (오늘 - 매수일).days).
 20일 ≈ 거래일 14일, 15일 ≈ 거래일 10~11일이다.

[판정] 전수 1회 실행은 복리 경로의 추첨이다(같은 유니버스에서 한 자리가 하루 당겨지면
 이후 경로가 통째로 갈린다). 반드시 같은 표본·같은 창에서 다이얼만 바꾼 짝비교의 승패로
 판정하고, 씨드를 바꿔 재확인한다(audit-seed-robustness).

[실행] python tools/audit_time_stop.py [--only B,C] [--trials 12] [--sample 25]
"""
import argparse
import contextlib
import os
import random
import sys

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tools.audit_common import exits, seed_notice  # noqa: E402

import config  # noqa: E402
from modules import portfolio_backtest as pb  # noqa: E402
from tools.audit_defensive_sector import (  # noqa: E402
    INITIAL_CAPITAL, metrics, new_scale_fn_factory,
)

DAYS_ARMS = [5, 8, 10, 12, 15, 20, 25, 30]
MIN_PROFIT_ARMS = [0.0, 2.0, 5.0, 10.0]


@contextlib.contextmanager
def sell_cfg(**kw):
    old = {k: config.SELL_STRATEGY.get(k) for k in kw}
    config.SELL_STRATEGY.update(kw)
    try:
        yield
    finally:
        config.SELL_STRATEGY.update(old)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--only", default="A,B,C")
    ap.add_argument("--days", type=int, default=3650)
    ap.add_argument("--trials", type=int, default=12)
    ap.add_argument("--sample", type=int, default=25)
    ap.add_argument("--slots", type=int, default=None)
    ap.add_argument("--seeds", default="20260813,7,101,4242,31337")
    args = ap.parse_args()
    seed_notice(len(args.seeds.split(",")), example="--seeds 20260816,7,101")
    only = args.only.split(",")
    slots = args.slots or getattr(config, "SYSTEM_MAX_HOLDINGS", 4)

    config.session.load_stock_config()
    targets = [(s["code"], s["name"]) for s in config.session.stock_data.get("stocks_kr", [])]
    print(f"[준비] 관심종목 {len(targets)}개 · {args.days}일 · 슬롯 {slots}"
          f" · 현행 {config.SELL_STRATEGY.get('TIME_STOP_DAYS')}일/"
          f"{config.SELL_STRATEGY.get('TIME_STOP_MIN_PROFIT_RATE')}%", flush=True)
    dfs, mf, dates, failed = pb.prepare_universe(targets, args.days)
    print(f"[준비] 사용 {len(dfs)}종목 · 거래일 {len(dates)}"
          + (f" · 제외 {failed}" if failed else ""), flush=True)
    thr = {"BUY_SCORE": config.ANALYSIS_THRESHOLDS["BUY_SCORE"],
           "BUY_RSI_MAX": config.ANALYSIS_THRESHOLDS["BUY_RSI_MAX"],
           "RISE_SCORE": config.ANALYSIS_THRESHOLDS["RISE_SCORE"],
           "WEIGHTS": config.SCORING_WEIGHTS}
    status = pb.precompute_status(dfs, thr)
    codes_all = list(dfs.keys())
    new_scale = new_scale_fn_factory(dates, args.days)

    bounds = ["20160816", "20180101", "20200101", "20220101", "20240101", "20270101"]
    W = [("전체", list(dates))]
    W += [(f"{bounds[k][2:6]}~{bounds[k + 1][2:6]}",
           [d for d in dates if bounds[k] <= d < bounds[k + 1]]) for k in range(5)]
    W = [(n, d) for n, d in W if d]

    def run(codes, wd):
        return pb.run_portfolio({c: dfs[c] for c in codes}, {c: status[c] for c in codes}, wd,
                                initial_capital=INITIAL_CAPITAL, slots=slots,
                                market_filter_dates={c: mf.get(c, set()) for c in codes},
                                risk_scale_by_date=new_scale())

    # ---------------- A) 청산 사유 구성 — 일수를 줄이면 무엇이 바뀌는가 ----------------
    if "A" in only:
        print("\n[A] 청산 사유 구성 (전수 1회 — 방향을 보는 진단이지 판정 근거가 아니다)")
        for d in DAYS_ARMS + [0]:
            with sell_cfg(TIME_STOP_DAYS=d, TIME_STOP_USE=bool(d)):
                r = run(codes_all, list(dates))
            sells = exits(r)
            print(f"\n  --- {d if d else 'OFF'}일 | 수익 {r['total_return']:.1f}% · 청산 {len(sells)}건 "
                  f"· 평균슬롯 {r['avg_slots']:.2f} ---")
            print(f"  {'사유':<12}{'건수':>6}{'비중%':>7}{'평균%':>8}{'중앙%':>8}"
                  f"{'손익합(원)':>13}{'평균보유':>9}{'MFE평균':>9}")
            for reason in ("ATR손절", "본전청산", "시간청산", "트레일링스탑", "점수하락"):
                g = [t for t in sells if t["reason"] == reason]
                if not g: continue
                p = np.array([t["profit"] for t in g])
                print(f"  {reason:<12}{len(g):>6}{len(g) / len(sells) * 100:>7.1f}{p.mean():>8.2f}"
                      f"{np.median(p):>8.2f}{sum(t['profit_amt'] for t in g):>13,.0f}"
                      f"{np.mean([t['days'] for t in g]):>9.1f}"
                      f"{np.mean([t.get('mfe', 0) for t in g]):>9.2f}")

    seeds = [int(s) for s in args.seeds.split(",")]
    picks = {sd: [random.Random(sd * 7 + i).sample(codes_all, min(args.sample, len(codes_all)))
                  for i in range(args.trials)] for sd in seeds}
    cur_days = int(config.SELL_STRATEGY.get("TIME_STOP_DAYS", 20))

    def paired(title, arms, cfg_key):
        print(f"\n{title} — 표본 {args.sample}/{len(codes_all)} · {args.trials}회 × 씨드 {len(seeds)}개")
        for wn, wd in W:
            with sell_cfg(**{cfg_key: arms["base"]}):
                b = [metrics(run(p, wd)) for sd in seeds for p in picks[sd]]
            bm = lambda k: np.mean([m[k] for m in b])  # noqa: E731
            print(f"\n--- 창 {wn} (기준 {arms['base']}: 수익 {bm('ret'):.2f}% · MDD {bm('mdd'):.2f}% "
                  f"· MAR {bm('mar'):.2f} · 상위10% {bm('top10'):.2f}) ---")
            print(f"{'팔':<14}{'승':>5}{'무':>4}{'패':>5}{'수익%':>9}{'차이':>9}{'MDD%':>9}"
                  f"{'MAR':>8}{'상위10%':>9}{'최대':>9}{'30%+':>7}{'청산':>7}")
            for v in arms["values"]:
                kw = {cfg_key: v}
                if cfg_key == "TIME_STOP_DAYS":
                    kw["TIME_STOP_USE"] = bool(v)
                with sell_cfg(**kw):
                    ms = [metrics(run(p, wd)) for sd in seeds for p in picks[sd]]
                w = sum(1 for x, y in zip(ms, b) if x["ret"] > y["ret"] + 1e-9)
                t = sum(1 for x, y in zip(ms, b) if abs(x["ret"] - y["ret"]) <= 1e-9)
                g = lambda k: np.mean([m[k] for m in ms])  # noqa: E731
                mark = " (현행)" if v == arms["base"] else ""
                print(f"{str(v) + mark:<14}{w:>5}{t:>4}{len(ms) - w - t:>5}{g('ret'):>9.2f}"
                      f"{g('ret') - bm('ret'):>9.2f}{g('mdd'):>9.2f}{g('mar'):>8.2f}"
                      f"{g('top10'):>9.2f}{g('best'):>9.2f}{g('big'):>7.1f}{g('n'):>7.1f}")

    if "B" in only:
        paired("[B] TIME_STOP_DAYS (달력일)", {"base": cur_days, "values": DAYS_ARMS},
               "TIME_STOP_DAYS")
    if "C" in only:
        cur_mp = float(config.SELL_STRATEGY.get("TIME_STOP_MIN_PROFIT_RATE", 0.0))
        paired("[C] TIME_STOP_MIN_PROFIT_RATE (이 수익률 미만이면 청산 = 승자도 자를 것인가)",
               {"base": cur_mp, "values": MIN_PROFIT_ARMS}, "TIME_STOP_MIN_PROFIT_RATE")


if __name__ == "__main__":
    main()
