"""본전 청산(BEP)이 추세추종 원칙에 맞는가 — 같은 진입에 대한 직접 반사실 비교.

[BEP가 하는 일] 최고 수익이 발동 기준(ATR 손절 사용 시 손절폭 절대값, 예 +9%)에 닿으면
손절선을 +0.5%로 끌어올린다. 이후 눌리면 본전 근처에서 청산된다.

[왜 원칙과 충돌할 수 있는가] 추세추종의 수익 구조는 fat-tail이다 — 소수의 큰 추세가
다수의 작은 손실을 덮는다. BEP는 '한 번 올랐다가 눌린' 포지션을 본전에서 끊는데,
추세 초입의 정상 눌림이 정확히 그 모양이다. 즉 BEP는 손실을 막는 대신 **미래의 큰
추세를 스스로 포기**할 수 있다. 반납 상한(TS_MAX_GIVEBACK_RATIO)을 같은 이유로 해제한
지금, 승자를 조기에 끊을 수 있는 장치는 BEP만 남았다.

[왜 포트폴리오 비교로는 부족한가] 슬롯 경쟁 때문에 BEP 하나를 바꾸면 이후 진입 종목이
통째로 달라진다(경로 분기). 그래서 차이가 잡음에 묻힌다 — 실제로 25회 짝비교에서
수익승 13/25로 판별이 안 됐다. 여기서는 **같은 진입 신호**에 대해 BEP만 켜고 끄고
끝까지 굴려, BEP가 실제로 발동한 거래에서 무슨 일이 일어났는지 직접 본다.

[핵심 질문] BEP가 발동한 거래들은, BEP가 없었다면 어떻게 끝났는가?

[실행] python tools/audit_bep.py [--stocks 41] [--days 1095]
"""
import argparse
import json
import os
import sys
from collections import Counter

import numpy as np
import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import config  # noqa: E402
import utils  # noqa: E402
from modules import portfolio_backtest as pb  # noqa: E402

MAX_HOLD = 400   # 한 포지션을 최대 이만큼 굴린다(추세를 끝까지 보기 위해 넉넉히)


def _cfg():
    s = config.SELL_STRATEGY
    return {
        "use_atr": s.get("USE_ATR_STOP", True),
        "use_time_stop": s.get("TIME_STOP_USE", True) and s.get("TIME_STOP_DAYS", 20) > 0,
        "time_stop_days": s.get("TIME_STOP_DAYS", 20),
        "ts_act": s.get("TRAILING_STOP_ACTIVATION_RATE", 10.0),
        "ts_callback": s.get("TRAILING_STOP_CALLBACK_RATE", 5.0),
        "ts_atr_mult": s.get("TRAILING_ATR_MULTIPLIER", 3.5),
        "sell_score_limit": s.get("SELL_SCORE", 4.0),
    }


def simulate(recs, status_map, start, sl_rate, applied, use_bep, cfg,
             bep_stop=None, fixed_activation=None):
    """한 포지션을 청산까지 굴린다 → (수익률%, 사유, 보유일, BEP발동여부).

    bep_stop: 끌어올릴 손절선(기본 BREAK_EVEN_STOP_RATE).
    fixed_activation: 발동 기준을 손절폭 대신 이 고정값(%)으로 쓴다(동기화 이전 설계 재현).
    """
    s = config.SELL_STRATEGY
    bep_stop = s.get("BREAK_EVEN_STOP_RATE", 0.5) if bep_stop is None else bep_stop
    bep_default = s.get("BREAK_EVEN_PROFIT_RATE", 5.0)
    slippage = getattr(config, "SLIPPAGE_RATE", 0.002)

    entry = recs[start]
    avg = utils.adjust_to_tick(entry["close"] * (1 + slippage), False) or entry["close"]
    high = avg
    bep_fired = False

    for j in range(start + 1, min(start + MAX_HOLD, len(recs))):
        row = recs[j]
        price = float(row["close"])
        if price <= 0:
            break
        high = max(high, float(row["high"]))

        eff_sl, is_bep = sl_rate, False
        if use_bep:
            max_profit = (high - avg) / avg * 100
            if fixed_activation is not None:
                activation = fixed_activation
            else:
                activation = abs(sl_rate) if (applied and sl_rate < 0) else bep_default
            if max_profit >= activation and sl_rate < bep_stop:
                eff_sl, is_bep = bep_stop, True
                bep_fired = True

        holding_days = (pd.to_datetime(str(row["date"]), format="%Y%m%d")
                        - pd.to_datetime(str(entry["date"]), format="%Y%m%d")).days
        raw, chk, _cb, state, reason = status_map[str(row["date"])]
        sell, why = pb.decide_sell(
            price=price, high=high, avg=avg, sl_rate=eff_sl, atr_applied=applied,
            is_bep=is_bep, holding_days=holding_days, state=state, state_reason=reason,
            raw_score=raw, sell_check=chk, ema60=row.get("EMA60"), atr=row.get("ATR", 0),
            roll_high_5=row.get("roll_high_5", 0), roll_high_10=row.get("roll_high_10", 0),
            cfg=cfg)
        if sell:
            exit_px = utils.adjust_to_tick(price * (1 - slippage), False) or price
            return (exit_px - avg) / avg * 100, why, holding_days, bep_fired

    last = float(recs[min(start + MAX_HOLD, len(recs)) - 1]["close"])
    return (last - avg) / avg * 100, "미청산", MAX_HOLD, bep_fired


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--stocks", type=int, default=41)
    ap.add_argument("--days", type=int, default=1095)
    args = ap.parse_args()

    data = json.load(open(config.STOCK_DATA_FILE))
    targets = [(s["code"], s["name"]) for s in data.get("stocks_kr", [])][:args.stocks]
    print(f"[준비] {len(targets)}종목 · {args.days}일")

    dfs, mf, dates, failed = pb.prepare_universe(targets, args.days)
    thresholds = {
        "BUY_SCORE": config.ANALYSIS_THRESHOLDS["BUY_SCORE"],
        "BUY_RSI_MAX": config.ANALYSIS_THRESHOLDS["BUY_RSI_MAX"],
        "RISE_SCORE": config.ANALYSIS_THRESHOLDS["RISE_SCORE"],
        "WEIGHTS": config.SCORING_WEIGHTS,
    }
    status = pb.precompute_status(dfs, thresholds)
    cfg = _cfg()
    thr = config.ANALYSIS_THRESHOLDS
    buy_score, buy_rsi = thr["BUY_SCORE"], thr["BUY_RSI_MAX"]
    super_use = thr.get("SUPER_MOMENTUM_USE", True)
    super_score = thr.get("SUPER_MOMENTUM_SCORE", 8.0)
    super_w52 = thr.get("SUPER_MOMENTUM_W52_POS", 90.0)
    super_rsi = thr.get("SUPER_BUY_RSI_MAX", 80.0)
    atr_mult = config.SELL_STRATEGY.get("ATR_STOP_MULTIPLIER", 2.0)
    use_atr = config.SELL_STRATEGY.get("USE_ATR_STOP", True)
    default_sl = config.SELL_STRATEGY["STOP_LOSS_RATE"]
    slippage = getattr(config, "SLIPPAGE_RATE", 0.002)

    on, off, pairs = [], [], []
    variants = {"+2.0% (강화)": [], "+0.5% · 발동 5% 고정": []}
    for code, df in dfs.items():
        recs = df.to_dict("records")
        smap = status[code]
        blocked = mf.get(code, set())
        held_until = -1
        for i in range(60, len(recs) - 5):
            row = recs[i]
            day = str(row["date"])
            if i <= held_until:
                continue        # 같은 추세에서 매일 중복 진입하지 않도록 직전 포지션 종료까지 대기
            raw, _chk, can_buy, state, _r = smap[day]
            if not can_buy or not (raw >= buy_score or state == "역매수"):
                continue
            is_super = super_use and raw >= super_score and row.get("w52_pos", 0) >= super_w52
            if row["RSI"] >= (super_rsi if is_super else buy_rsi):
                continue
            if day in blocked:
                continue

            buy_px = utils.adjust_to_tick(row["close"] * (1 + slippage), False) or row["close"]
            sl = pb._atr_stop_rate(row.get("ATR", 0), buy_px, atr_mult) if use_atr else default_sl
            applied = sl != 0.0
            if not applied:
                sl = default_sl

            r_on = simulate(recs, smap, i, sl, applied, True, cfg)
            r_off = simulate(recs, smap, i, sl, applied, False, cfg)
            on.append(r_on)
            off.append(r_off)
            variants["+2.0% (강화)"].append(
                simulate(recs, smap, i, sl, applied, True, cfg, bep_stop=2.0))
            variants["+0.5% · 발동 5% 고정"].append(
                simulate(recs, smap, i, sl, applied, True, cfg, fixed_activation=5.0))
            pairs.append((code, day, r_on, r_off))
            held_until = i + max(1, int(r_on[2] * 0.7))   # 대략적인 보유 구간만큼 건너뛴다

    n = len(on)
    print(f"[표본] 진입 {n:,}건 (슬롯 경쟁 없음 — 청산 다이얼만 비교)\n")

    def dist(rows, label):
        p = np.array([x[0] for x in rows])
        top10 = np.sort(p)[::-1][:max(1, len(p) // 10)]
        return (f"{label:<12}{p.mean():>9.2f}{np.median(p):>9.2f}"
                f"{(p > 0).mean() * 100:>8.1f}{top10.mean():>10.2f}{p.max():>9.1f}"
                f"{(p >= 30).sum():>8}{(p >= 50).sum():>8}"
                f"{np.mean([x[2] for x in rows]):>8.1f}")

    print(f"{'설정':<12}{'평균%':>9}{'중앙%':>9}{'승률%':>8}{'상위10%':>10}{'최대%':>9}"
          f"{'>30%':>8}{'>50%':>8}{'보유일':>8}")
    print("-" * 80)
    print(dist(off, "BEP 미사용"))
    print(dist(on, "+0.5%(현행)"))
    for k, v in variants.items():
        print(dist(v, k))

    # ---- BEP가 실제로 발동한 거래만 ----
    idx = [i for i in range(n) if on[i][3]]
    print(f"\n[BEP 발동 거래] {len(idx):,}건 ({len(idx) / max(1, n) * 100:.1f}%)")
    if idx:
        a = np.array([on[i][0] for i in idx])
        b = np.array([off[i][0] for i in idx])
        print(f"  BEP 사용   평균 {a.mean():+.2f}% · 중앙 {np.median(a):+.2f}% · "
              f"승률 {(a > 0).mean() * 100:.1f}% · 최대 {a.max():+.1f}%")
        print(f"  BEP 미사용 평균 {b.mean():+.2f}% · 중앙 {np.median(b):+.2f}% · "
              f"승률 {(b > 0).mean() * 100:.1f}% · 최대 {b.max():+.1f}%")
        print(f"  → 같은 거래의 차이: 평균 {(a - b).mean():+.2f}%p "
              f"(BEP가 더 좋았던 비율 {(a > b).mean() * 100:.1f}%)")
        better = int((b - a >= 10).sum())
        print(f"  → BEP 때문에 10%p 이상 놓친 거래: {better}건 "
              f"({better / len(idx) * 100:.1f}%)")
        big = int(((b >= 30) & (a < 30)).sum())
        print(f"  → BEP가 없었다면 +30% 이상이었을 거래: {big}건")
        cut = int(((a < 5) & (b >= 20)).sum())
        print(f"  → 본전 근처(+5% 미만)에서 끊었는데 실제로는 +20% 이상 갔을 거래: {cut}건")

        rc = Counter(off[i][1] for i in idx)
        print("  → BEP 미사용 시 그 거래들의 청산 사유: "
              + ", ".join(f"{k} {v}" for k, v in rc.most_common(5)))

    # ---- 기대값(전체 표본) ----
    pa = np.array([x[0] for x in on])
    pb_ = np.array([x[0] for x in off])
    print(f"\n[전체 기대값] BEP 사용 {pa.mean():+.2f}% vs 미사용 {pb_.mean():+.2f}% "
          f"→ 차이 {pa.mean() - pb_.mean():+.2f}%p")
    print(f"[꼬리]       >30% 거래 {int((pa >= 30).sum())} vs {int((pb_ >= 30).sum())}건 · "
          f">50% 거래 {int((pa >= 50).sum())} vs {int((pb_ >= 50).sum())}건")


if __name__ == "__main__":
    main()
