"""TS 발동선만 낮출 수 있는가 — 콜백과 분리해서 잰다.

[문제] TRAILING_ATR_MULTIPLIER(3.5) 하나가 두 가지를 동시에 정한다.
   발동선 = cb/(1-cb),  cb = ATR×3.5/매수가      ← 언제 무장하는가
   콜백   = ATR×3.5/고점                          ← 얼마나 넓게 따라가는가
 그래서 배수를 낮추면 발동이 빨라지는 대신 청산선도 같이 좁아진다. 2026-08-11 측정에서
 2.5·3.0이 수익승 0/15로 완패한 것은 이 **합작 효과**이며, '발동을 앞당기면 진다'는
 증거가 아니다. 두 축을 분리한 측정은 없었다.

[분리 방법] run_portfolio(ts_act_fn=) 훅은 발동 기준만 바깥에서 정한다. 이때 콜백은
 config의 TRAILING_ATR_MULTIPLIER(3.5)를 그대로 쓰므로, '발동만 앞당기고 청산선은
 현행 폭 유지'라는 반사실을 정확히 만들 수 있다.

[A] 발동 전용 배수 분리 — 발동선을 ATR×m(m=2.0~3.5)로 계산하고 콜백은 3.5 고정.
[B] 발동선 상한 캡 — 산식값은 그대로 두되 상한만 씌운다(min(act, X%)).
    고변동 종목에서만 구속하므로 평시 동작은 건드리지 않는다. 지금 잔고에서 +50%가
    나오는 종목이 바로 이 캡의 대상이다.

[판정 잣대] 기존 청산 다이얼 결정과 같다. 총수익·MDD와 함께
  · 무장률 — 이 검증의 목적 지표. 발동선을 낮추면 실제로 얼마나 더 무장하는가
  · 상위10%·최대·>30% — fat-tail이 살아 있는가 (트레이드오프의 대가)
  · TS이익% — '주청산은 샹들리에 TS' 설계가 유지되는가

[실행] python3 tools/audit_ts_arm_threshold.py --days 3650 --trials 15 --sample 25 --subperiods 4
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
from modules.auto_trade import engine  # noqa: E402

INITIAL_CAPITAL = 10_000_000


def act_fn(mult=None, cap=None, be_floor=False):
    """발동 기준(%)만 계산한다. 콜백은 건드리지 않는다(config 값 유지).

    be_floor: '청산선이 매수가 아래인 동안에는 무장하지 않는다'는 하한을 건다.
      청산선 = 고점 - ATR×콜백배수 이므로, 본전 조건은 MFE ≥ ATR×콜백배수/매수가 다.
      (배수를 낮추거나 캡을 씌우면 무장이 이 지점보다 **앞서** 일어나 청산선이 매수가
       아래에 놓인다 — 그 상태를 없애는 반사실이다.)
    """
    ss = config.SELL_STRATEGY
    cb_floor = ss.get("TRAILING_STOP_CALLBACK_RATE", 5.0)
    m = mult if mult is not None else ss.get("TRAILING_ATR_MULTIPLIER", 3.5)

    def fn(atr, price):
        act = engine.breakeven_activation_rate(atr, price, cb_floor, m, True)
        if cap:
            act = min(act, cap)
        if be_floor and atr > 0 and price > 0:
            # 콜백 배수는 호출 시점 config를 읽는다(콜백 스윕과 함께 써도 어긋나지 않게).
            act = max(act, atr * ss.get("TRAILING_ATR_MULTIPLIER", 3.5) / price * 100)
        return act
    return fn


def dial_sets():
    """(그룹, 라벨, ts_act_fn 또는 None=현행, SELL_STRATEGY 오버라이드).

    act_fn은 발동선만 정하고 콜백은 config를 따르므로, 오버라이드로 콜백 배수를 바꾸면
    '발동은 고정한 채 콜백만' 움직이는 반사실이 된다(그 반대는 act_fn이 담당).
    """
    return [
        ("A. 발동 전용 배수(콜백 3.5 고정)", "3.5 (현행)", None, {}),
        ("A. 발동 전용 배수(콜백 3.5 고정)", "3.0", act_fn(mult=3.0), {}),
        ("A. 발동 전용 배수(콜백 3.5 고정)", "2.5", act_fn(mult=2.5), {}),
        ("A. 발동 전용 배수(콜백 3.5 고정)", "2.0", act_fn(mult=2.0), {}),
        ("B. 발동선 상한 캡", "없음 (현행)", None, {}),
        ("B. 발동선 상한 캡", "30%", act_fn(cap=30.0), {}),
        ("B. 발동선 상한 캡", "25%", act_fn(cap=25.0), {}),
        ("B. 발동선 상한 캡", "20%", act_fn(cap=20.0), {}),
        ("B. 발동선 상한 캡", "15%", act_fn(cap=15.0), {}),
        # [C] A와 B를 함께 건 조합. 두 레버는 다른 종목군에 작용한다 — 배수는 전 종목의
        #  발동선을 비례로 내리고, 캡은 고ATR 종목만 잘라낸다. 따라서 합이 각각의 합과
        #  같으리라 가정할 수 없어 따로 잰다.
        ("C. A+B 조합(콜백 3.5 고정)", "현행", None, {}),
        ("C. A+B 조합(콜백 3.5 고정)", "3.0 + 캡30%", act_fn(mult=3.0, cap=30.0), {}),
        ("C. A+B 조합(콜백 3.5 고정)", "3.0 + 캡25%", act_fn(mult=3.0, cap=25.0), {}),
        ("C. A+B 조합(콜백 3.5 고정)", "3.0 + 캡20%", act_fn(mult=3.0, cap=20.0), {}),
        # [D] 발동 배수 3.0을 채택한 뒤의 캡 4단 비교. 배수는 고정이므로 캡만의 효과가 나온다.
        ("D. 캡 4단(배수 3.0 고정)", "캡 없음 (현행)", act_fn(mult=3.0), {}),
        ("D. 캡 4단(배수 3.0 고정)", "캡 30%", act_fn(mult=3.0, cap=30.0), {}),
        ("D. 캡 4단(배수 3.0 고정)", "캡 25%", act_fn(mult=3.0, cap=25.0), {}),
        ("D. 캡 4단(배수 3.0 고정)", "캡 20%", act_fn(mult=3.0, cap=20.0), {}),
        # [E] 채택안(발동 3.0 + 캡 20%)을 고정한 채 **콜백 배수만** 낮춘다.
        #  콜백을 좁히면 무장 시점의 청산선이 매수가에 가까워지지만(본전 보장 회복 방향),
        #  동시에 추세 추종 폭이 줄어 fat-tail을 깎는다 — 그 교환비를 본다.
        ("E. 콜백 배수(발동 3.0+캡20 고정)", "3.5 (현행)", act_fn(mult=3.0, cap=20.0), {}),
        ("E. 콜백 배수(발동 3.0+캡20 고정)", "3.25", act_fn(mult=3.0, cap=20.0),
         {"TRAILING_ATR_MULTIPLIER": 3.25}),
        ("E. 콜백 배수(발동 3.0+캡20 고정)", "3.0", act_fn(mult=3.0, cap=20.0),
         {"TRAILING_ATR_MULTIPLIER": 3.0}),
        ("E. 콜백 배수(발동 3.0+캡20 고정)", "2.5", act_fn(mult=3.0, cap=20.0),
         {"TRAILING_ATR_MULTIPLIER": 2.5}),
        # [F] '청산선이 매수가 아래면 무장하지 않는다'는 하한. 발동선을 낮춘 대가(무장 시
        #  청산선이 -13%까지 내려앉는 것)를 되돌리는 방향이다. 고ATR 종목에서는 하한이
        #  캡을 무력화하므로, 캡 20%의 효과를 상당 부분 반납하는 교환이 된다.
        ("F. 본전 하한(발동 3.0+캡20 기준)", "없음 (현행)", act_fn(mult=3.0, cap=20.0), {}),
        ("F. 본전 하한(발동 3.0+캡20 기준)", "본전 하한 ON",
         act_fn(mult=3.0, cap=20.0, be_floor=True), {}),
        ("F. 본전 하한(발동 3.0+캡20 기준)", "[참고] 순수 본전선",
         act_fn(mult=0.0001, cap=None, be_floor=True), {}),
        # [G][H] 같은 두 질문을 **캡 없이** 다시 본다. 캡이 걸린 상태에서는 캡·콜백·하한이
        #  서로를 가려 어느 레버의 효과인지 분리되지 않는다.
        ("G. 콜백 배수(발동 3.0·캡없음)", "3.5 (현행)", act_fn(mult=3.0), {}),
        ("G. 콜백 배수(발동 3.0·캡없음)", "3.25", act_fn(mult=3.0),
         {"TRAILING_ATR_MULTIPLIER": 3.25}),
        ("G. 콜백 배수(발동 3.0·캡없음)", "3.0", act_fn(mult=3.0),
         {"TRAILING_ATR_MULTIPLIER": 3.0}),
        ("G. 콜백 배수(발동 3.0·캡없음)", "2.5", act_fn(mult=3.0),
         {"TRAILING_ATR_MULTIPLIER": 2.5}),
        ("H. 본전 하한(발동 3.0·캡없음)", "없음 (현행)", act_fn(mult=3.0), {}),
        ("H. 본전 하한(발동 3.0·캡없음)", "본전 하한 ON", act_fn(mult=3.0, be_floor=True), {}),
        # [I] 콜백 절대 상한. 콜백 배수를 낮추는 것(그룹 E·G)은 전 종목을 일률적으로 조여
        #  참사였지만, 상한은 고ATR 종목에서만 구속한다 — 발동선 캡과 같은 모양의 레버다.
        #  기준선은 현행(발동 3.0 · 캡 해제 · 콜백 3.5).
        ("I. 콜백 절대 상한", "해제 (현행)", None, {}),
        ("I. 콜백 절대 상한", "25%", None, {"TRAILING_STOP_CALLBACK_MAX": 25.0}),
        ("I. 콜백 절대 상한", "20%", None, {"TRAILING_STOP_CALLBACK_MAX": 20.0}),
        ("I. 콜백 절대 상한", "15%", None, {"TRAILING_STOP_CALLBACK_MAX": 15.0}),
        ("I. 콜백 절대 상한", "12%", None, {"TRAILING_STOP_CALLBACK_MAX": 12.0}),
    ]


def metrics(r):
    sells = exits(r)
    profits = sorted((t["profit"] for t in sells), reverse=True)
    top10 = profits[:max(1, len(profits) // 10)]
    gross_gain = sum(t["profit_amt"] for t in sells if t["profit_amt"] > 0)
    ts_gain = sum(t["profit_amt"] for t in sells
                  if t["reason"] == "트레일링스탑" and t["profit_amt"] > 0)
    armed = [t for t in sells if t.get("armed")]
    # [목적 지표] 이 검증이 줄이려는 대상 — MFE 15% 이상 갔는데 무장하지 못한 청산.
    gate = [t for t in sells if t.get("mfe", 0) >= 15 and not t.get("armed")]
    return {
        "gate_n": len(gate),
        "gate_give": float(np.mean([t["mfe"] - t["profit"] for t in gate])) if gate else 0.0,
        "ret": r["total_return"], "mdd": r["mdd"],
        "mar": r["total_return"] / abs(r["mdd"]) if r["mdd"] else float("nan"),
        "pf": r["pf"],
        "top10": float(np.mean(top10)) if top10 else 0.0,
        "best": profits[0] if profits else 0.0,
        "big": sum(1 for p in profits if p >= 30),
        "ts_profit_share": (ts_gain / gross_gain * 100) if gross_gain > 0 else 0.0,
        "armed_share": len(armed) / len(sells) * 100 if sells else 0.0,
        "ts_n": sum(1 for t in sells if t["reason"] == "트레일링스탑"),
        "days": float(np.median([t["days"] for t in sells])) if sells else 0.0,
        "n": len(sells),
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--trials", type=int, default=15)
    ap.add_argument("--sample", type=int, default=25)
    ap.add_argument("--days", type=int, default=3650)
    ap.add_argument("--slots", type=int, default=None)
    ap.add_argument("--seed-capital", type=int, default=INITIAL_CAPITAL)
    ap.add_argument("--only", default=None)
    ap.add_argument("--seed", type=int, default=20260811)
    ap.add_argument("--subperiods", type=int, default=4)
    ap.add_argument("--exclude-from", default="20260301")
    args = ap.parse_args()

    slots = args.slots or getattr(config, "SYSTEM_MAX_HOLDINGS", 4)
    # [중요] breakeven_activation_rate가 config의 TS_ACTIVATION_MAX_RATE를 스스로 적용하므로,
    #  꺼두지 않으면 '캡 없음' 대조군이 조용히 캡에 걸린 채로 측정된다.
    config.SELL_STRATEGY["TS_ACTIVATION_MAX_RATE"] = 0.0
    config.session.load_stock_config()
    targets = [(s["code"], s["name"]) for s in config.session.stock_data.get("stocks_kr", [])]
    print(f"[준비] 관심종목 {len(targets)}개 · {args.days}일 · 슬롯 {slots} · 시드 {args.seed_capital:,}원")

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

    cut = "".join(ch for ch in str(args.exclude_from) if ch.isdigit())
    head = [d for d in dates if not cut or cut == "0" or "".join(filter(str.isdigit, d)) < cut]
    tail = [d for d in dates if cut and cut != "0" and "".join(filter(str.isdigit, d)) >= cut]
    print(f"[창] 검증 {len(head)}일 (~{head[-1] if head else '-'}) · 제외 {len(tail)}일")

    sets = dial_sets()
    if args.only:
        sets = [x for x in sets if x[0].startswith(args.only)]
    codes = list(dfs.keys())
    saved = dict(config.SELL_STRATEGY)   # 오버라이드 복원용(캡 0 포함)

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
        results = {(g, l): [] for g, l, _f, _o in sets}
        rng = random.Random(args.seed)
        for t in range(args.trials):
            pick = rng.sample(codes, min(args.sample, len(codes)))
            sd = {c: dfs[c] for c in pick}
            st = {c: status[c] for c in pick}
            sm = {c: mf.get(c, set()) for c in pick}
            for g, label, fn, overrides in sets:
                config.SELL_STRATEGY.clear(); config.SELL_STRATEGY.update(saved)
                config.SELL_STRATEGY.update(overrides)
                r = pb.run_portfolio(sd, st, wdates, initial_capital=args.seed_capital,
                                     slots=slots, market_filter_dates=sm,
                                     risk_scale_by_date=make_scale_fn(mkt, dd),
                                     ts_act_fn=fn)
                results[(g, label)].append(metrics(r))
            print(f"  {wname} 시행 {t + 1}/{args.trials}", end="\r", flush=True)
        config.SELL_STRATEGY.clear(); config.SELL_STRATEGY.update(saved)
        all_results[wname] = results
    print(" " * 50, end="\r")

    W = 112
    print(f"\n{'=' * W}")
    print(f"TS 발동선 — 콜백과 분리한 재측정 ({args.trials}회 × {args.sample}종목 짝비교)")
    print(f"{'=' * W}")
    for wname, wdates in windows:
        results = all_results[wname]
        print(f"\n########## {wname} ({len(wdates)} 거래일) ##########")
        last_group = None
        for g, label, _f, _o in sets:
            if g != last_group:
                base_label = next(l for gg, l, _x, _y in sets if gg == g and "현행" in l)
                base = results[(g, base_label)]
                print(f"\n{g}  (기준선: {base_label})")
                print(f"{'설정':<14}{'수익%':>9}{'MDD%':>8}{'MAR':>7}{'PF':>6}{'무장률%':>9}"
                      f"{'TS청산':>8}{'미무장15+':>11}{'그반납':>8}{'상위10%':>9}{'최대':>9}"
                      f"{'>30%':>6}{'TS이익%':>9}{'승-무-패':>10}{'MAR승':>7}{'꼬리승':>7}")
                print("-" * W)
                last_group = g
            rs = results[(g, label)]
            m = lambda k: float(np.median([x[k] for x in rs]))  # noqa: E731
            is_base = "현행" in label
            # [무승부 구분] 캡처럼 '대부분의 시행에서 아무 일도 하지 않는' 레버는 승수만 보면
            #  열위로 보인다 — 동점을 패배와 섞어 세기 때문이다. 승-무-패로 나눠 적는다.
            tie = sum(1 for a, b in zip(rs, base) if abs(a["ret"] - b["ret"]) < 1e-9)
            los = sum(1 for a, b in zip(rs, base) if a["ret"] < b["ret"] - 1e-9)
            rw = sum(1 for a, b in zip(rs, base) if a["ret"] > b["ret"])
            mw = sum(1 for a, b in zip(rs, base) if a["mar"] > b["mar"])
            tw = sum(1 for a, b in zip(rs, base) if a["top10"] > b["top10"])
            print(f"{label:<14}{m('ret'):>9.1f}{m('mdd'):>8.1f}{m('mar'):>7.2f}{m('pf'):>6.2f}"
                  f"{m('armed_share'):>9.1f}{m('ts_n'):>8.0f}{m('gate_n'):>11.0f}"
                  f"{m('gate_give'):>8.1f}{m('top10'):>9.1f}{m('best'):>9.1f}"
                  f"{m('big'):>6.0f}{m('ts_profit_share'):>9.1f}"
                  f"{'—' if is_base else f'{rw}-{tie}-{los}':>8}"
                  f"{'—' if is_base else f'{mw}/{len(rs)}':>7}"
                  f"{'—' if is_base else f'{tw}/{len(rs)}':>7}")

    print("\n" + "-" * W)
    print("무장률% = 청산된 거래 중 보유기간에 한 번이라도 TS가 무장했던 비율 — 이 검증의 목적 지표.")
    print("[읽는 법] 목적 지표(무장률)가 올라도 상위10%·최대가 무너지면 대가가 더 크다.")


if __name__ == "__main__":
    main()
