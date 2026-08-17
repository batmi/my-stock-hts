"""남은 미검증 다이얼 — config 키를 감사 도구 전체와 다시 대조해 걸러낸 셋.

[어떻게 골랐나] config의 전략 키 194개를 tools/audit_*.py 전체와 대조하니 108개가 등장
 0회였다. 그중 인프라(API 키·경로·TPS·WS·텔레그램)와 **이미 꺼져 있어 무동작인 것**을
 걷어내면 실제로 매매 결정을 바꾸는 것은 셋만 남는다.
   · [무동작 확인] USE_MEAN_REVERSION=False · HALF_TAKE_PROFIT_USE=False ·
     TAKE_PROFIT_RATE=0.0 · TAKE_PROFIT_RSI=0.0 · DEFENSIVE_HALF_SELL_USE=False
     → 평균회귀 매수 경로와 고정 익절은 전부 꺼져 있다. 값을 재도 아무 일도 안 일어난다.
   · [폴백 잔여] REGIME_MA_PERIOD · REGIME_ADX_THRESHOLD 는 데이터 부족 시에만 쓰는
     구 방식 잔여물이다(settings.py 주석). 실사용 경로가 아니다.

  A) 휩소율 룩백 `REGIME_WHIPSAW_LOOKBACK`(8회)
     국면 배수와 곱해져 **포지션 크기를 매일 조절**하는 값인데 한 번도 안 훑었다.
     2026-08-17 실측에서 이 배수가 거의 매일 1.0을 살짝 밑돈다는 것이 드러났으므로
     (거래일의 88.5%) 상시 구속하는 축이다. 8회라는 값에 근거가 없다.
     ※ 이 값을 바꾸면 시장 배수 시계열 자체가 달라진다 — 팔마다 스케일을 **다시 만든다.**

  B) 동적 ATR 캡의 **비율 클램프**(0.4~3.0)와 변동성 창(60일)
     캡을 끄면 268.2%로 14-2-20 열위 = 캡 자체는 값을 한다. 민감도 지수(VOL_POWER)는
     무동작이었다. 남은 것이 캡의 움직임 범위를 정하는 값들인데, **재기 전에 산술로
     먼저 걸렀다**(2026-08-17):
       캡 = -15 × r^0.5, r은 [RATIO_MIN, RATIO_MAX]로 클립 → 캡 범위 [-25.98, -11.79]
       실측 r(2,986일): min 0.62 · p50 1.20 · max 3.00(상한에 붙음)
       → `ATR_CAP_CEIL`(-6)·`ATR_CAP_FLOOR`(-35)에 닿는 날 **0일 / 2,986일 = 사문(死文)**
       → `ATR_CAP_RATIO_MIN`(0.4)도 r 최저가 0.62라 **한 번도 안 걸린다**
     즉 실제로 구속하는 것은 **RATIO_MAX(3.0)와 변동성 창**뿐이다. 그 둘만 잰다.
     [교훈] 경계값은 백테스트 전에 '그 경계에 닿는 날이 며칠인가'부터 세면 대부분
      돌릴 필요가 없다.

  C) 가격 모멘텀 룩백 `MOMENTUM_LOOKBACK_1M`(21) / `MOMENTUM_LOOKBACK_3M`(63)
     스코어링 항목 '가격 모멘텀'의 기간. 지표 기간 감사(audit_indicator_periods)는
     RSI·MACD·ADX만 훑었고 이 둘은 빠졌다. 점수를 바꾸므로 **상태 캐시를 다시 만든다**
     (그래서 비싸다 — 팔당 precompute_status 1회).

[과최적화 주의] 기간·경계 축은 훑으면 뭐든 이긴다. 채택 기준을 미리 못박는다 —
 **전체창 승 + 하위 구간 전부 승**이 아니면 현행 유지. 애매하면 현행이다.

[실행] python3 tools/audit_residual_dials.py --axis A,B,C --trials 12 --sample 25
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
from tools.audit_dials_intraday import apply  # noqa: E402

AXES = {
    "A": ("휩소율 룩백 (현행 8회)", "scale", [
        ("현행 8", []),
        ("4 (짧게)", [("regime", "REGIME_WHIPSAW_LOOKBACK", 4)]),
        ("12", [("regime", "REGIME_WHIPSAW_LOOKBACK", 12)]),
        ("16 (길게)", [("regime", "REGIME_WHIPSAW_LOOKBACK", 16)]),
    ]),
    # 사문인 CEIL/FLOOR/RATIO_MIN은 뺐다(위 산술 참조). 실제로 구속하는 둘만 남긴다.
    "B": ("동적 ATR 캡 — 비율 상한(3.0)과 변동성 창(60일)", "vol", [
        ("현행 3.0 / 60일", []),
        ("비율 상한 2.0", [("sell", "ATR_CAP_RATIO_MAX", 2.0)]),
        ("비율 상한 4.0", [("sell", "ATR_CAP_RATIO_MAX", 4.0)]),
        ("클램프 해제 0.2~6.0", [("sell", "ATR_CAP_RATIO_MIN", 0.2),
                              ("sell", "ATR_CAP_RATIO_MAX", 6.0)]),
        ("변동성창 20일", [("sell", "ATR_CAP_VOL_WINDOW", 20)]),
        ("변동성창 120일", [("sell", "ATR_CAP_VOL_WINDOW", 120)]),
    ]),
    "C": ("가격 모멘텀 룩백 (현행 21/63일)", "status", [
        ("현행 21/63", []),
        ("10/42 (짧게)", [("ind", "MOMENTUM_LOOKBACK_1M", 10), ("ind", "MOMENTUM_LOOKBACK_3M", 42)]),
        ("42/126 (길게)", [("ind", "MOMENTUM_LOOKBACK_1M", 42), ("ind", "MOMENTUM_LOOKBACK_3M", 126)]),
        ("21/126 (장기만)", [("ind", "MOMENTUM_LOOKBACK_3M", 126)]),
    ]),
}


def apply2(pairs):
    """audit_dials_intraday.apply 에 없는 대상(regime/ind)까지 처리한다."""
    prev = []
    for tgt, key, val in pairs:
        d = {"regime": config.MARKET_REGIME_PARAMS,
             "ind": config.INDICATOR_PARAMS}.get(tgt)
        if d is None:
            prev += apply([(tgt, key, val)])
            continue
        prev.append((tgt, key, d.get(key)))
        d[key] = val
    return prev


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--axis", default="A,B,C")
    ap.add_argument("--days", type=int, default=3650)
    ap.add_argument("--trials", type=int, default=12)
    ap.add_argument("--sample", type=int, default=25)
    ap.add_argument("--seeds", default="20260816,7,101")
    ap.add_argument("--slots", type=int, default=None)
    ap.add_argument("--subperiods", type=int, default=3)
    args = ap.parse_args()
    slots = args.slots or getattr(config, "SYSTEM_MAX_HOLDINGS", 4)
    seeds = [int(x) for x in args.seeds.split(",")]

    config.session.load_stock_config()
    live = [(s["code"], s["name"]) for s in config.session.stock_data.get("stocks_kr", [])]
    dfs, mf, dates, failed = pb.prepare_universe(live, args.days)
    print(f"[준비] {len(dfs)}종목 · 거래일 {len(dates)} · 슬롯 {slots}"
          + (f" · 제외 {failed}" if failed else ""), flush=True)

    def make_thr():
        return {"BUY_SCORE": config.ANALYSIS_THRESHOLDS["BUY_SCORE"],
                "BUY_RSI_MAX": config.ANALYSIS_THRESHOLDS["BUY_RSI_MAX"],
                "RISE_SCORE": config.ANALYSIS_THRESHOLDS["RISE_SCORE"],
                "WEIGHTS": config.SCORING_WEIGHTS}

    def recompute_mom():
        """가격 모멘텀 컬럼을 지금 config 값으로 다시 만든다(prepare_universe가 굳힌 것)."""
        for _c, _df in dfs.items():
            for col, key, dflt in (("MOM_RET_1M", "MOMENTUM_LOOKBACK_1M", 21),
                                   ("MOM_RET_3M", "MOMENTUM_LOOKBACK_3M", 63)):
                lb = int(config.INDICATOR_PARAMS.get(key, dflt))
                _df[col] = _df["close"].pct_change(periods=lb, fill_method=None) * 100

    vol_state = {}
    base_status = pb.precompute_status(dfs, make_thr())
    base_scale = new_scale_fn_factory(dates, args.days)

    k = max(1, args.subperiods)
    size = max(1, len(dates) // k)
    W = [("전체", list(dates))]
    W += [(f"구간{i + 1}", dates[i * size:(i + 1) * size if i < k - 1 else len(dates)])
          for i in range(k)]
    picks = {sd: [random.Random(sd * 31 + i).sample(list(dfs), min(args.sample, len(dfs)))
                  for i in range(args.trials)] for sd in seeds}

    for ax in [a.strip() for a in args.axis.split(",")]:
        if ax not in AXES:
            continue
        title, kind, arms = AXES[ax]
        print(f"\n\n=========== 축 {ax} · {title} ===========", flush=True)

        # 팔마다 무엇을 다시 만들어야 하는지가 다르다 — 여기서 한 번에 준비한다.
        prepared = []
        for label, ov in arms:
            prev = apply2(ov)
            try:
                st, sc = base_status, base_scale
                if ov and kind == "status":
                    # [함정] 가격 모멘텀은 prepare_universe가 만든 **컬럼**으로 굳어 있다.
                    #  config만 바꾸고 점수를 다시 계산하면 옛 컬럼을 그대로 읽어 전부
                    #  완전 동률(0-N-0)로 나온다 — 실제로 한 번 그랬다. 컬럼을 다시 만든다.
                    recompute_mom()
                    st = pb.precompute_status(dfs, make_thr())
                elif ov and kind == "scale":
                    sc = new_scale_fn_factory(dates, args.days)
                elif ov and kind == "vol":
                    # [함정] prepare_vol_regime의 캐시 키에 비율 상·하한이 없다.
                    #  키를 지우지 않으면 옛 배율 시계열을 그대로 쓴다.
                    backtest._VOL_REGIME_STATE["key"] = None
                    backtest.prepare_vol_regime(args.days, False)
                    vol_state[label] = dict(backtest._VOL_REGIME_STATE["by_date"])
            finally:
                apply2(prev)
            prepared.append((label, ov, st, sc))
            if ov and kind != "plain":
                what = {"status": "상태 캐시", "scale": "시장 배수",
                        "vol": "변동성 배율"}[kind]
                print(f"  [재계산] {label} — {what}", flush=True)
        if kind == "status":    # 마지막 팔의 컬럼이 남아 있다 — 원본으로 되돌린다
            recompute_mom()
        if kind == "vol":       # 기준선 배율도 원본으로 확보해 둔다
            backtest._VOL_REGIME_STATE["key"] = None
            backtest.prepare_vol_regime(args.days, False)
            vol_state[arms[0][0]] = dict(backtest._VOL_REGIME_STATE["by_date"])
            rs = {k: np.array(list(v.values())) for k, v in vol_state.items()}
            print("  [배율 분포] " + " · ".join(
                f"{k}: p50 {np.median(v):.2f}/max {v.max():.2f}" for k, v in rs.items()),
                flush=True)

        for wn, wd in W:
            print(f"\n########## {wn} ({len(wd)} 거래일) ##########")
            print(f"{'설정':<18}{'수익%':>9}{'MDD%':>8}{'MAR':>7}{'PF':>6}{'청산':>6}"
                  f"{'상위10%':>9}{'승률%':>7}{'승-무-패':>10}")
            base_res = None
            for label, ov, st, sc in prepared:
                res = []
                prev = apply2(ov)
                if kind == "vol":
                    backtest._VOL_REGIME_STATE["by_date"] = vol_state[label]
                try:
                    for sd in seeds:
                        for pick in picks[sd]:
                            r = pb.run_portfolio(
                                {c: dfs[c] for c in pick}, {c: st[c] for c in pick}, wd,
                                initial_capital=INITIAL_CAPITAL, slots=slots,
                                market_filter_dates={c: mf.get(c, set()) for c in pick},
                                risk_scale_by_date=sc())
                            res.append(metrics(r))
                finally:
                    apply2(prev)
                g = lambda key: float(np.mean([m[key] for m in res]))  # noqa: E731
                if base_res is None:
                    base_res, wl = res, "—"
                else:
                    win = sum(1 for x, y in zip(res, base_res) if x["ret"] > y["ret"] + 1e-9)
                    tie = sum(1 for x, y in zip(res, base_res)
                              if abs(x["ret"] - y["ret"]) <= 1e-9)
                    wl = f"{win}-{tie}-{len(res) - win - tie}"
                print(f"{label:<18}{g('ret'):>9.1f}{g('mdd'):>8.1f}{g('mar'):>7.2f}"
                      f"{g('pf'):>6.2f}{g('n'):>6.0f}{g('top10'):>9.1f}{g('win'):>7.1f}"
                      f"{wl:>10}", flush=True)

    print("\n[채택 기준] 전체창 승 + 하위 구간 전부 승이 아니면 현행 유지. 기간·경계 축은 "
          "훑으면 뭐든 이기므로 기준을 미리 못박고 시작했다.")
    print("[완전 동률(0-N-0)] 그 다이얼이 아무 일도 하지 않는다는 뜻이다 — 튜닝 대상이 아니다.")


if __name__ == "__main__":
    main()
