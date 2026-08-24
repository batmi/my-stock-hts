"""지표를 '어떻게 계산하는가' — 살아있는 마지막 미검증 계열.

[왜] 스코어링 감사는 **가중치와 임계값**을 전수로 쟀고(항목30·파라미터13), 기간 감사는
 RSI·ADX **기간**만 쟀다. 그런데 지표를 만드는 파라미터 자체는 아무도 건드린 적이 없다 —
 전부 Wilder/전통 기본값 그대로이고 근거 기록이 없다. 이들은 임계값을 흔드는 것이 아니라
 **지표 컬럼을 다시 만들어** 스코어링 항목의 입력을 통째로 바꾼다.

[축] config 등장 0회였던 것 중 백테스트 컬럼에 실제로 반영되는 것만.
   A. SAR (AF 시작/증분/최대 0.02/0.02/0.2) — 추세SMO의 'S' 점수
   B. MACD 시그널 기간 9                    — MACD/MACD_Hist 점수
   C. OBV 이평 기간 5                       — OBV 추세 점수
   D. 거래량 이평 기간 20                   — 거래량 점수
   E. 단기 EMA 기간 5                       — Early 추세

[반드시 컬럼을 다시 만들어야 한다] `prepare_universe`가 지표를 **컬럼으로 굳혀** 두므로
 config만 바꾸고 점수를 재계산하면 옛 컬럼을 그대로 읽어 **0-N-0 완전 동률**로 나온다.
 가격 모멘텀에서 실제로 한 번 당했다([[residual-dials-closed]]). 여기서는 팔마다
 `backtest.calculate_indicators`를 다시 돌리고 상태 캐시도 새로 만든다.

[실행] python3 tools/audit_indicator_construction.py --axis A,B,C,D,E --trials 12
"""
import argparse
import os
import random
import sys

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import config  # noqa: E402
from modules import backtest  # noqa: E402
from modules import portfolio_backtest as pb  # noqa: E402
from tools.audit_defensive_sector import (  # noqa: E402
    INITIAL_CAPITAL, metrics, new_scale_fn_factory,
)
from tools.audit_universe import dead_targets, extend_targets  # noqa: E402
from tools.audit_common import seed_notice  # noqa: E402

# (라벨, {INDICATOR_PARAMS 키: 값}) — None이면 기준선(현행)
AXES = {
    "A": ("SAR 가속계수 (추세 전환 민감도)", [
        ("[기준선] 0.02 / 0.02 / 0.20", None),
        ("느리게 0.01 / 0.01 / 0.20", {"SAR_AF_START": 0.01, "SAR_AF_STEP": 0.01}),
        ("빠르게 0.04 / 0.04 / 0.20", {"SAR_AF_START": 0.04, "SAR_AF_STEP": 0.04}),
        ("상한 상향 0.02 / 0.02 / 0.30", {"SAR_AF_MAX": 0.30}),
    ]),
    "B": ("MACD 시그널 기간", [
        ("[기준선] 9", None),
        ("6 (빠름)", {"MACD_SIGNAL": 6}),
        ("12 (느림)", {"MACD_SIGNAL": 12}),
    ]),
    # [주의] 이 축은 2026-08-17에 5 → 10으로 **채택돼 config가 이미 바뀌었다.** 기준선을
    #  None(=현행 config)으로 두면 기준선과 '10' 팔이 같아져 0-N-0으로 나온다. 그래서
    #  두 값을 모두 명시한다 — 재실행 때 옛 값과 새 값을 그대로 맞대볼 수 있어야 한다.
    "C": ("OBV 이평 기간", [
        ("[기준선] 5 (2026-08-17 이전)", {"OBV_MA_PERIOD": 5}),
        ("10 (현행)", {"OBV_MA_PERIOD": 10}),
        ("20", {"OBV_MA_PERIOD": 20}),
    ]),
    "D": ("거래량 이평 기간", [
        ("[기준선] 20", None),
        ("10", {"VOLUME_MA_PERIOD": 10}),
        ("40", {"VOLUME_MA_PERIOD": 40}),
    ]),
    "E": ("단기 EMA 기간", [
        ("[기준선] 5", None),
        ("3", {"EMA_SHORT": 3}),
        ("8", {"EMA_SHORT": 8}),
    ]),
}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--axis", default="A,B,C,D,E")
    ap.add_argument("--days", type=int, default=3650)
    ap.add_argument("--trials", type=int, default=12)
    ap.add_argument("--seeds", default="20260816,7,101")
    ap.add_argument("--size", type=int, default=44)
    ap.add_argument("--pool", type=int, default=500)
    ap.add_argument("--dead-frac", type=float, default=0.2)
    ap.add_argument("--subperiods", type=int, default=3)
    args = ap.parse_args()
    seed_notice(len(args.seeds.split(",")), example="--seeds 20260816,7,101")
    seeds = [int(x) for x in args.seeds.split(",")]
    slots = getattr(config, "SYSTEM_MAX_HOLDINGS", 4)

    config.session.load_stock_config()
    live = [(s["code"], s["name"]) for s in config.session.stock_data.get("stocks_kr", [])]
    ext = extend_targets({c for c, _ in live}, 60, mode="random", pool=args.pool)
    n_dead = int(args.size * args.dead_frac)
    dfs, mf, dates, _f = pb.prepare_universe(
        live + ext + dead_targets(n_dead + 10), args.days)
    # [계측기] 진입 순위가 닿는 축이다(점수가 바뀌면 슬롯 경쟁의 주인이 바뀐다).
    #  엔진 기본 정렬은 실매매 동점가름이지만 앞의 룩백-1일은 추세품질 이력이 없다.
    _lb = config.INDICATOR_PARAMS.get("TREND_QUALITY_LOOKBACK", 90)
    dates = dates[_lb - 1:]
    dead_set = {c for c, _ in dead_targets(n_dead + 10)}
    dead_c = [c for c in dfs if c in dead_set]
    live_c = [c for c in dfs if c not in dead_set]
    print(f"[준비] 생존 {len(live_c)} + 폐지 {len(dead_c)} → 표본 {args.size}종목 · "
          f"거래일 {len(dates)}", flush=True)

    # 원본 OHLCV만 보관해 둔다 — 팔마다 여기서 지표를 다시 만든다.
    raw = {c: df[["date", "open", "high", "low", "close", "volume", "smart_money"]].copy()
           if "smart_money" in df else df[["date", "open", "high", "low", "close", "volume"]].copy()
           for c, df in dfs.items()}

    def make_thr():
        return {"BUY_SCORE": config.ANALYSIS_THRESHOLDS["BUY_SCORE"],
                "BUY_RSI_MAX": config.ANALYSIS_THRESHOLDS["BUY_RSI_MAX"],
                "RISE_SCORE": config.ANALYSIS_THRESHOLDS["RISE_SCORE"],
                "WEIGHTS": config.SCORING_WEIGHTS}

    def rebuild():
        """지금 config 값으로 지표 컬럼과 상태 캐시를 통째로 다시 만든다."""
        out = {}
        for c, r in raw.items():
            d = r.copy()
            backtest.compute_price_indicators(d)
            out[c] = d
        return out, pb.precompute_status(out, make_thr())

    new_scale = new_scale_fn_factory(dates, args.days)
    picks = {}
    for sd in seeds:
        for i in range(args.trials):
            rng = random.Random(sd * 31 + i)
            picks[(sd, i)] = (rng.sample(dead_c, min(n_dead, len(dead_c)))
                              + rng.sample(live_c, args.size - n_dead))

    k = max(1, args.subperiods)
    step = max(1, len(dates) // k)
    W = [("전체", list(dates))]
    W += [(f"구간{i + 1}", dates[i * step:(i + 1) * step if i < k - 1 else len(dates)])
          for i in range(k)]

    P = config.INDICATOR_PARAMS
    for ax in [a.strip() for a in args.axis.split(",")]:
        if ax not in AXES:
            continue
        title, arms = AXES[ax]
        print(f"\n\n=========== 축 {ax} · {title} ===========", flush=True)

        prepared = []
        for label, ov in arms:
            prev = {kk: P.get(kk) for kk in (ov or {})}
            if ov:
                P.update(ov)
            try:
                d2, s2 = rebuild()
            finally:
                for kk, vv in prev.items():
                    P[kk] = vv
            # 컬럼이 실제로 달라졌는지 확인한다 — 안 바뀌면 0-N-0이 나오고 오독한다.
            #  (첫 팔은 비교 대상이 없다 — 기준선이 곧 자기 자신이다.)
            if ov and prepared:
                c0 = next(iter(d2))
                same = np.allclose(d2[c0].select_dtypes("number").fillna(0).values,
                                   prepared[0][1][c0].select_dtypes("number").fillna(0).values)
                print(f"  [재계산] {label} — 지표 컬럼 {'동일(!)' if same else '변경됨'}", flush=True)
            prepared.append((label, d2, s2))

        for wn, wd in W:
            print(f"\n--- {wn} ({len(wd)} 거래일) ---")
            print(f"{'팔':<28}{'수익%':>9}{'MDD%':>8}{'MAR':>7}{'PF':>6}{'청산':>6}"
                  f"{'상위10%':>9}{'승률%':>7}{'승-무-패':>10}")
            base = None
            for label, d2, s2 in prepared:
                res = []
                for sd in seeds:
                    for i in range(args.trials):
                        pick = picks[(sd, i)]
                        r = pb.run_portfolio(
                            {c: d2[c] for c in pick}, {c: s2[c] for c in pick}, wd,
                            initial_capital=INITIAL_CAPITAL, slots=slots,
                            market_filter_dates={c: mf.get(c, set()) for c in pick},
                            risk_scale_by_date=new_scale())
                        res.append(metrics(r))
                g = lambda key: float(np.mean([m[key] for m in res]))  # noqa: E731
                if base is None:
                    base, wl = res, "— (기준)"
                else:
                    win = sum(1 for x, y in zip(res, base) if x["ret"] > y["ret"] + 1e-9)
                    tie = sum(1 for x, y in zip(res, base) if abs(x["ret"] - y["ret"]) <= 1e-9)
                    wl = f"{win}-{tie}-{len(res) - win - tie}"
                print(f"{label:<28}{g('ret'):>9.1f}{g('mdd'):>8.1f}{g('mar'):>7.2f}"
                      f"{g('pf'):>6.2f}{g('n'):>6.0f}{g('top10'):>9.1f}{g('win'):>7.1f}"
                      f"{wl:>10}", flush=True)

    print("\n[읽는 법] 완전 동률(0-N-0)이 나오면 먼저 위 [재계산] 줄이 '변경됨'인지 볼 것 — "
          "'동일(!)'이면 오버라이드가 안 먹은 것이지 다이얼이 무의미한 것이 아니다.")


if __name__ == "__main__":
    main()
