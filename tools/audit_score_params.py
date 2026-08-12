"""스코어링 파라미터 전수 검증 — 점수를 만드는 숫자 하나하나에 근거가 있는가.

[왜] 2026-08-12까지 잰 것은 팩터 4개의 **가중치 형태**(A그룹)와 순위로서의 값어치(B·C그룹)뿐이다.
 정작 각 항목이 언제 켜지는지를 정하는 **문턱값**들은 도입 이래 한 번도 실측된 적이 없다.
 ADX 20, RSI 50/60/80/40, 52주 위치 80%, 추세지속 70%/120일, 6개월 모멘텀, 거래량 2배,
 그리고 매수·매도 문턱(7.0/4.0)이 그것이다. 이 숫자들이 임의값이면 그 위에 쌓은 가중치
 실험도 임의값 위에 선다.

[방법] 다른 축과 같은 판정 규약을 쓴다 — 10년(2026-03+ 고변동 제외) 15회 × 25종목 짝비교,
 하위 4구간 다수 우세(31/60 이상)를 넘지 못하면 현행 유지. 파라미터마다 현행 ±로 두 값을
 세워 **단조성**을 본다. 한쪽만 좋으면 방향이 있는 것이고, 양쪽 다 나쁘면 현행이 극점이다.

[구현 주의] 파라미터는 config 전역에서 읽히므로 팔마다 config를 갈아끼우고 상태를 다시
 깐다(precompute_status). 사전계산 컬럼(MOM_RET·TREND_PERSIST·VOL_SPIKE)에 들어가는 값은
 컬럼까지 다시 계산해야 실제로 반영된다 — 안 그러면 '바꿨는데 아무 일도 안 일어나는'
 가짜 무승부가 나온다. 기준선은 한 번만 재서 모든 그룹이 공유한다(표본·시드 동일).

[실행] python tools/audit_score_params.py [--trials 15] [--sample 25] [--only ADX,RSI]
"""
import argparse
import copy
import os
import random
import sys

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import config  # noqa: E402
from modules import portfolio_backtest as pb  # noqa: E402
from tools.audit_scoring_weights import metrics  # noqa: E402

INITIAL_CAPITAL = 10_000_000

# (그룹, 파라미터 키, 저장소, 현행값 설명, [대안값...])
#   store: "ind"=INDICATOR_PARAMS · "thr"=매수 문턱(precompute) · "sell"=SELL_STRATEGY
PARAMS = [
    ("강도 ADX 문턱",        "SCORE_ADX_MIN",           "ind",  [15, 25]),
    ("가격모멘텀 52주 위치",   "MOMENTUM_W52_NEAR",       "ind",  [70, 90]),
    ("가격모멘텀 룩백",       "MOMENTUM_LOOKBACK",       "ind",  [63, 252]),
    ("추세지속 문턱",         "TREND_PERSIST_MIN",       "ind",  [60, 80]),
    ("추세지속 룩백",         "TREND_PERSIST_LOOKBACK",  "ind",  [60, 200]),
    ("RSI 중립선",           "SCORE_RSI_MID",           "ind",  [45, 55]),
    ("RSI 강세선",           "SCORE_RSI_STRONG",        "ind",  [55, 65]),
    ("RSI 과열선",           "SCORE_RSI_OVERHEAT",      "ind",  [75, 85]),
    ("RSI 눌림 하한",        "SCORE_RSI_REBOUND",       "ind",  [35, 45]),
    ("CCI 상승 기준",        "SCORE_CCI_STRONG",        "ind",  [-50, 50]),
    ("거래량 급증 배수",      "VOLUME_SPIKE_RATIO",      "ind",  [1.5, 3.0]),
    ("매수 문턱",            "BUY_SCORE",               "thr",  [6.5, 7.5]),
    ("매도 문턱",            "SELL_SCORE",              "sell", [3.5, 4.5]),
]

# 사전계산 컬럼에 반영해야 하는 파라미터 → 다시 깔 컬럼
RECOMPUTE = {
    "MOMENTUM_LOOKBACK": "MOM_RET",
    "TREND_PERSIST_LOOKBACK": "TREND_PERSIST",
    "VOLUME_SPIKE_RATIO": "VOL_SPIKE",
}


def recompute_column(dfs, key):
    """파라미터가 바뀌면 사전계산 컬럼도 다시 깐다 (정의는 backtest.compute_price_indicators와 동일)."""
    col = RECOMPUTE[key]
    for df in dfs.values():
        if col == "MOM_RET":
            lb = config.INDICATOR_PARAMS.get("MOMENTUM_LOOKBACK", 126)
            df["MOM_RET"] = df["close"].pct_change(periods=lb, fill_method=None) * 100
        elif col == "TREND_PERSIST":
            lb = config.INDICATOR_PARAMS.get("TREND_PERSIST_LOOKBACK", 120)
            df["TREND_PERSIST"] = (df["close"] > df["EMA60"]).rolling(lb, min_periods=lb).mean() * 100
        elif col == "VOL_SPIKE":
            ratio = config.INDICATOR_PARAMS.get("VOLUME_SPIKE_RATIO", 2.0)
            df["VOL_SPIKE"] = ((df["VOL_MA20"] > 0)
                               & (df["volume"] >= df["VOL_MA20"] * ratio)
                               & (df["close"] > df["open"]))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--trials", type=int, default=15)
    ap.add_argument("--sample", type=int, default=25)
    ap.add_argument("--days", type=int, default=3650)
    ap.add_argument("--slots", type=int, default=None)
    ap.add_argument("--seed", type=int, default=20260811)
    ap.add_argument("--subperiods", type=int, default=4)
    ap.add_argument("--exclude-from", default="20260301")
    ap.add_argument("--only", default=None, help="파라미터 키 일부(쉼표) — 부분 실행")
    args = ap.parse_args()

    slots = args.slots or getattr(config, "SYSTEM_MAX_HOLDINGS", 4)
    config.session.load_stock_config()
    targets = [(s["code"], s["name"]) for s in config.session.stock_data.get("stocks_kr", [])]
    print(f"[준비] 관심종목 {len(targets)}개 · {args.days}일 · 슬롯 {slots}", flush=True)
    dfs, mf, dates, failed = pb.prepare_universe(targets, args.days)
    print(f"[준비] 사용 {len(dfs)}종목 / 거래일 {len(dates)}일"
          + (f" · 제외 {len(failed)}" if failed else ""), flush=True)

    from tools.audit_drawdown_axis import market_scale_by_date, make_scale_fn
    rp = getattr(config, "RISK_SCALING_PARAMS", {}) or {}
    mkt = market_scale_by_date(dates, args.days)
    dd = None
    if rp.get("USE_DRAWDOWN_RISK_SCALING", True):
        dd = (int(rp.get("DD_LOOKBACK_DAYS", 90)), float(rp.get("DD_LEVEL_1", 5.0)),
              float(rp.get("DD_SCALE_1", 0.9)), float(rp.get("DD_LEVEL_2", 10.0)),
              float(rp.get("DD_SCALE_2", 0.8)))
    # [실행마다 새로 만든다 — 2026-08-13] make_scale_fn 이 돌려주는 콜러블은 내부에 자산곡선
    #  이력(hist)을 들고 있다. 하나를 만들어 모든 팔·시행에 돌려쓰면 앞선 실행의 자산곡선이
    #  남아 드로다운 판정이 오염되고, 팔의 실행 순서에 따라 결과가 달라진다(같은 설정을 두 번
    #  돌리면 392.66% → 284.79%). 짝비교의 전제가 깨지므로 팩토리로 두고 매번 새로 만든다.
    new_scale_fn = lambda: make_scale_fn(mkt, dd)  # noqa: E731

    cut = "".join(ch for ch in str(args.exclude_from) if ch.isdigit())
    head = [d for d in dates if not cut or "".join(filter(str.isdigit, d)) < cut]
    tail = [d for d in dates if cut and "".join(filter(str.isdigit, d)) >= cut]
    k = max(1, args.subperiods)
    size = max(1, len(head) // k)
    windows = [("전체", head)]
    windows += [(f"구간{i + 1}", head[i * size:(i + 1) * size if i < k - 1 else len(head)])
                for i in range(k)]
    if tail:
        windows.append(("고변동", tail))
    print(f"[창] 검증 {len(head)}일 · 제외(고변동 대조) {len(tail)}일", flush=True)

    base_ind = copy.deepcopy(config.INDICATOR_PARAMS)
    base_sell = copy.deepcopy(config.SELL_STRATEGY)
    codes = list(dfs.keys())

    def thr_for(buy_score=None):
        # [주의] 매수 문턱은 두 곳에서 읽힌다 — 상태 분류(precompute_status에 넘기는 이 dict)와
        #  포트폴리오의 후보 게이트(run_portfolio가 config.ANALYSIS_THRESHOLDS를 직접 읽는다).
        #  여기만 바꾸면 상태만 바뀌고 게이트는 7.0인 채로 남아 '절반만 반영된' 가짜 결과가 나온다.
        if buy_score is not None:
            config.ANALYSIS_THRESHOLDS["BUY_SCORE"] = buy_score
        return {
            "BUY_SCORE": buy_score if buy_score is not None else config.ANALYSIS_THRESHOLDS["BUY_SCORE"],
            "BUY_RSI_MAX": config.ANALYSIS_THRESHOLDS["BUY_RSI_MAX"],
            "RISE_SCORE": config.ANALYSIS_THRESHOLDS["RISE_SCORE"],
            "WEIGHTS": config.SCORING_WEIGHTS,
        }

    def run_arm(buy_score=None):
        """현재 config 상태로 상태를 다시 깔고 전 창·전 시행을 돈다."""
        status = pb.precompute_status(dfs, thr_for(buy_score))
        out = {}
        for wname, wdates in windows:
            res = []
            rng = random.Random(args.seed)
            for _t in range(args.trials):
                pick = rng.sample(codes, min(args.sample, len(codes)))
                sd = {c: dfs[c] for c in pick}
                sm = {c: mf.get(c, set()) for c in pick}
                st = {c: status[c] for c in pick}
                r = pb.run_portfolio(sd, st, wdates, initial_capital=INITIAL_CAPITAL,
                                     slots=slots, market_filter_dates=sm,
                                     risk_scale_by_date=new_scale_fn())
                res.append(metrics(r))
            out[wname] = res
        return out

    print("\n[기준선] 현행 파라미터로 먼저 잰다 (모든 그룹이 공유).", flush=True)
    base = run_arm()
    for wname, _wd in windows:
        m = base[wname]
        print(f"    {wname:<6} 수익 {np.median([x['ret'] for x in m]):>7.1f}%"
              f"  MDD {np.median([x['mdd'] for x in m]):>6.1f}"
              f"  상위10% {np.median([x['top10'] for x in m]):>5.1f}", flush=True)

    W = 118
    print(f"\n{'=' * W}")
    print(f"스코어링 파라미터 스윕 ({args.trials}회 × {args.sample}종목 짝비교 · 기준선=현행)")
    print(f"{'=' * W}", flush=True)
    hdr = (f"{'파라미터':<26}{'값':>8}{'수익%':>9}{'MDD%':>8}{'MAR':>7}{'상위10%':>9}{'최대':>8}"
           f"{'전체 승-무-패':>14}{'하위4구간':>11}{'MAR승':>8}{'꼬리승':>8}{'고변동':>11}")

    only = {x.strip().upper() for x in args.only.split(",")} if args.only else None
    for gname, key, store, alts in PARAMS:
        if only and not any(o in key.upper() or o in gname.upper() for o in only):
            continue
        cur = (config.INDICATOR_PARAMS.get(key) if store == "ind"
               else config.SELL_STRATEGY.get(key) if store == "sell"
               else config.ANALYSIS_THRESHOLDS.get(key))
        print(f"\n{'#' * W}\n{gname} — {key} (현행 {cur})\n{'#' * W}")
        print(hdr)
        print("-" * W, flush=True)
        # 기준선 행
        bm = base["전체"]
        print(f"{'현행 (기준선)':<26}{str(cur):>8}"
              f"{np.median([x['ret'] for x in bm]):>9.1f}{np.median([x['mdd'] for x in bm]):>8.1f}"
              f"{np.median([x['mar'] for x in bm]):>7.2f}{np.median([x['top10'] for x in bm]):>9.1f}"
              f"{np.median([x['best'] for x in bm]):>8.1f}{'—':>14}{'—':>11}{'—':>8}{'—':>8}{'—':>11}", flush=True)

        for val in alts:
            if store == "ind":
                config.INDICATOR_PARAMS[key] = val
                if key in RECOMPUTE:
                    recompute_column(dfs, key)
            elif store == "sell":
                config.SELL_STRATEGY[key] = val
            arm = run_arm(buy_score=val if store == "thr" else None)

            def tally(wname):
                a, b = arm[wname], base[wname]
                w = sum(1 for x, y in zip(a, b) if x["ret"] > y["ret"])
                t = sum(1 for x, y in zip(a, b) if abs(x["ret"] - y["ret"]) < 1e-9)
                return w, t, len(a) - w - t

            sub_w = sum(tally(f"구간{i + 1}")[0] for i in range(k))
            sub_n = k * args.trials
            aw, at, al = tally("전체")
            hv = tally("고변동") if tail else (0, 0, 0)
            am = arm["전체"]
            mw = sum(1 for x, y in zip(am, bm) if x["mar"] > y["mar"])
            tw = sum(1 for x, y in zip(am, bm) if x["top10"] > y["top10"])
            print(f"{'':<26}{str(val):>8}"
                  f"{np.median([x['ret'] for x in am]):>9.1f}{np.median([x['mdd'] for x in am]):>8.1f}"
                  f"{np.median([x['mar'] for x in am]):>7.2f}{np.median([x['top10'] for x in am]):>9.1f}"
                  f"{np.median([x['best'] for x in am]):>8.1f}"
                  f"{f'{aw}-{at}-{al}':>14}{f'{sub_w}/{sub_n}':>11}"
                  f"{f'{mw}/{len(am)}':>8}{f'{tw}/{len(am)}':>8}"
                  f"{f'{hv[0]}-{hv[1]}-{hv[2]}':>11}", flush=True)

            # 원복 — 다음 팔이 앞 팔의 값을 물려받으면 결과가 통째로 거짓이 된다.
            if store == "ind":
                config.INDICATOR_PARAMS[key] = base_ind[key]
                if key in RECOMPUTE:
                    recompute_column(dfs, key)
            elif store == "sell":
                config.SELL_STRATEGY[key] = base_sell[key]
            elif store == "thr":
                config.ANALYSIS_THRESHOLDS[key] = cur

    print("\n" + "-" * W)
    print("하위4구간 = 4개 하위 구간 짝비교 승수 합(60 중). 31/60 이상이라야 채택 검토 대상이다.")
    print("[읽는 법] 현행 ±가 둘 다 지면 현행이 극점이다. 한쪽만 이기면 그 방향으로 더 밀어볼 것.")
    print("[읽는 법] 무승부가 대부분이면 그 파라미터는 이 표본에서 아무것도 바꾸지 못한다(무동작).")


if __name__ == "__main__":
    main()
