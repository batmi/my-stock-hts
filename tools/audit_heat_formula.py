"""포트폴리오 히트 산식이 **실제 청산선**과 맞는가 — 정합성과 캡 구속 빈도 측정.

[무엇을 재는가] RiskManager.compute_portfolio_heat 는 '현재가 → 유효 손절선'의 합으로
총 오픈 리스크를 센다. 그 손절선이 실제 청산 로직의 선과 다르면, 히트 캡은 존재하지 않는
위험을 막거나(과대) 있는 위험을 놓친다(과소). 여기서는 세 팔을 같은 포지션-일에 세운다.

  old  : 2026-08-29 이전 산식 — TS 콜백으로 고정 하한(TRAILING_STOP_CALLBACK_RATE)만 사용
  new  : 현행 산식 — 콜백을 effective_callback(하한, ATR×TRAILING_ATR_MULTIPLIER)로 산출
  true : 실제 청산선 — compute_trailing_stop 에 **그 봉의 진짜 ATR**을 넣은 값

[왜 old가 위험한가] 실효 콜백은 max(하한, ATR×배수)라 **항상 하한 이상**이다. 따라서
고정 하한은 실제보다 높은 청산선을 가정하고, 그만큼 오픈 리스크를 과소 계상한다 —
compute_portfolio_heat 독스트링이 표방하는 '보수적(과대평가)' 방향의 정반대다.
2026-08-19에 고친 BEP 결함(토글을 보지 않고 손절선을 본전으로 올리던 것)과 같은 계열이다.

[new와 true의 차이] 실매매에는 그 시점의 ATR 시계열이 없어, new는 매수 시점 손절률에서
ATR을 역산한다(ATR = |sl_rate|×매수가/ATR_STOP_MULTIPLIER). true는 그 근사가 얼마나
유효한지 재기 위한 기준선이며, 실매매가 도달할 수 없는 상한이 아니라 '목표'다.

[측정 모델] 슬롯 경쟁·복리를 흉내 내지 않는다. 진입 신호마다 포지션을 열어 청산까지
굴리고(단일 종목 세계), 날짜별로 동시 보유를 슬롯 수까지만 채워 등가중으로 세운다.
자산은 고정이다 — 이 도구가 답하는 것은 수익이 아니라 **'히트가 얼마이고 캡이 언제
물리는가'** 이므로, 자산곡선 피드백을 넣으면 오히려 팔끼리 다른 경로를 타 비교가 깨진다.

[실행] python tools/audit_heat_formula.py [--stocks 44] [--days 3650] [--slots 4]
"""
import argparse
import os
import sys

import numpy as np
import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import config  # noqa: E402
from core import utils  # noqa: E402
from modules import portfolio_backtest as pb  # noqa: E402
from modules.auto_trade import engine  # noqa: E402
from tools.audit_common import windows  # noqa: E402

MAX_HOLD = 400


class _StubTrader:
    """compute_portfolio_heat 가 참조하는 최소 표면(락·트레일링 캐시)."""

    def __init__(self):
        import threading
        self._lock = threading.RLock()
        self.trailing_stop_cache = {}

    def log(self, *a, **k):
        pass


def _cfg():
    s = config.SELL_STRATEGY
    return {
        "use_atr": s.get("USE_ATR_STOP", True),
        "use_time_stop": s.get("TIME_STOP_USE", True) and s["TIME_STOP_DAYS"] > 0,
        "time_stop_days": s["TIME_STOP_DAYS"],
        "ts_act": s.get("TRAILING_STOP_ACTIVATION_RATE", 10.0),
        "ts_callback": s.get("TRAILING_STOP_CALLBACK_RATE", 5.0),
        "ts_atr_mult": s.get("TRAILING_ATR_MULTIPLIER", 3.5),
        "sell_score_limit": s.get("SELL_SCORE", 4.0),
    }


def walk(recs, smap, start, sl_rate, applied, cfg, use_bep):
    """한 포지션을 청산까지 굴리며 **보유 일자별 상태**를 남긴다.

    audit_bep.simulate 와 같은 청산 판정(pb.decide_sell)을 쓰되, 수익률 대신 그날의
    (평단·최고가·종가·ATR)을 돌려준다 — 히트는 청산 결과가 아니라 보유 중의 값이다.
    """
    s = config.SELL_STRATEGY
    bep_stop = s.get("BREAK_EVEN_STOP_RATE", 0.5)
    bep_default = s.get("BREAK_EVEN_PROFIT_RATE", 5.0)
    slippage = getattr(config, "SLIPPAGE_RATE", 0.002)

    entry = recs[start]
    avg = utils.adjust_to_tick(entry["close"] * (1 + slippage), False) or entry["close"]
    high = avg
    days = []

    for j in range(start + 1, min(start + MAX_HOLD, len(recs))):
        row = recs[j]
        price = float(row["close"])
        if price <= 0:
            break
        high = max(high, float(row["high"]))

        eff_sl, is_bep = sl_rate, False
        if use_bep:
            max_profit = (high - avg) / avg * 100
            activation = abs(sl_rate) if (applied and sl_rate < 0) else bep_default
            if max_profit >= activation and sl_rate < bep_stop:
                eff_sl, is_bep = bep_stop, True

        days.append({"date": str(row["date"]), "avg": avg, "high": high,
                     "close": price, "atr": float(row.get("ATR", 0) or 0),
                     "sl": sl_rate, "is_bep": is_bep})

        holding_days = (pd.to_datetime(str(row["date"]), format="%Y%m%d")
                        - pd.to_datetime(str(entry["date"]), format="%Y%m%d")).days
        raw, chk, _cb, state, reason = smap[str(row["date"])]
        sell, _why = pb.decide_sell(
            price=price, high=high, avg=avg, sl_rate=eff_sl, atr_applied=applied,
            is_bep=is_bep, holding_days=holding_days, state=state, state_reason=reason,
            raw_score=raw, sell_check=chk, ema60=row.get("EMA60"), atr=row.get("ATR", 0),
            roll_high_5=row.get("roll_high_5", 0), roll_high_10=row.get("roll_high_10", 0),
            cfg=cfg)
        if sell:
            break
    return days


def collect_positions(dfs, status, mf, args):
    """진입 게이트를 통과한 포지션들을 모아 보유 일자별 상태를 만든다."""
    thr = config.ANALYSIS_THRESHOLDS
    buy_score, buy_rsi = thr["BUY_SCORE"], thr["BUY_RSI_MAX"]
    super_use = thr.get("SUPER_MOMENTUM_USE", True)
    super_score = thr.get("SUPER_MOMENTUM_SCORE", 8.0)
    super_w52 = thr.get("SUPER_MOMENTUM_W52_POS", 90.0)
    super_rsi = thr.get("SUPER_BUY_RSI_MAX", 80.0)
    atr_mult = config.SELL_STRATEGY.get("ATR_STOP_MULTIPLIER", 2.0)
    use_atr = config.SELL_STRATEGY.get("USE_ATR_STOP", True)
    use_bep = config.SELL_STRATEGY.get("USE_BREAK_EVEN_STOP", False)
    default_sl = config.SELL_STRATEGY["STOP_LOSS_RATE"]
    slippage = getattr(config, "SLIPPAGE_RATE", 0.002)
    cfg = _cfg()

    positions = []
    for code, df in dfs.items():
        recs = df.to_dict("records")
        smap = status[code]
        blocked = mf.get(code, set())
        held_until = -1
        for i in range(60, len(recs) - 5):
            row = recs[i]
            day = str(row["date"])
            if i <= held_until:
                continue
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

            days = walk(recs, smap, i, sl, applied, cfg, use_bep)
            if days:
                positions.append({"code": code, "entry": day, "days": days})
                held_until = i + len(days)
    return positions


def true_stop(rec):
    """실제 청산 로직이 그날 쓰는 손절선 — ATR 손절선과 (무장 시) TS선 중 높은 쪽."""
    stop = rec["avg"] * (1 + (0.5 if rec["is_bep"] else rec["sl"]) / 100.0)
    info = engine.compute_trailing_stop(highest_price=rec["high"], buy_price=rec["avg"],
                                        current_price=rec["close"], ind={"atr": rec["atr"]})
    if info and info["armed"]:
        stop = max(stop, info["stop_price"])
    return stop


def heat_of(day_recs, qty_map, rm, trader, price_override=None):
    """현행 compute_portfolio_heat 로 그날의 총 오픈 리스크(원)를 낸다.

    price_override: 현재가를 강제한다. 정합성 측정에서 손절선을 역산할 때 쓴다 —
     손절선이 현재가 위면 산식이 리스크를 0으로 자르므로(이익 잠김), 그 상태로는
     '얼마나 위인지'를 알 수 없다. 충분히 높은 가격을 넣으면 클립이 걸리지 않아
     손절선 = 현재가 − 리스크 로 정확히 되짚을 수 있다.
    """
    holdings, buy_map = [], {}
    trader.trailing_stop_cache = {}
    for code, rec in day_recs:
        qty = qty_map[code]
        px = price_override if price_override is not None else rec['close']
        holdings.append({'pdno': code, 'hldg_qty': str(qty),
                         'pchs_avg_pric': f"{rec['avg']:.4f}", 'prpr': str(int(px))})
        buy_map[code] = [{'qty': qty, 'stop_loss_rate': rec['sl']}]
        trader.trailing_stop_cache[code] = rec['high']
    return rm.compute_portfolio_heat(holdings, buy_map)


def assumed_stop(rec, rm, trader):
    """히트 산식이 그 포지션에 가정하는 손절선(원). 리스크 클립을 피해 역산한다."""
    probe = max(rec['close'], rec['high']) * 10.0
    return probe - heat_of([("PROBE", rec)], {"PROBE": 1}, rm, trader, price_override=probe)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--stocks", type=int, default=44)
    ap.add_argument("--days", type=int, default=3650)
    ap.add_argument("--slots", type=int, default=None)
    ap.add_argument("--equity", type=float, default=10_000_000)
    args = ap.parse_args()
    slots = args.slots or getattr(config, "SYSTEM_MAX_HOLDINGS", 4)

    config.session.load_stock_config()
    targets = [(s["code"], s["name"])
               for s in config.session.stock_data.get("stocks_kr", [])][:args.stocks]
    print(f"[준비] {len(targets)}종목 · {args.days}일 · 슬롯 {slots} · 자산 {args.equity:,.0f}원")

    dfs, mf, _dates, failed = pb.prepare_universe(targets, args.days)
    if failed:
        print(f"[주의] 데이터 미확보 {len(failed)}종목 제외")
    thresholds = {
        "BUY_SCORE": config.ANALYSIS_THRESHOLDS["BUY_SCORE"],
        "BUY_RSI_MAX": config.ANALYSIS_THRESHOLDS["BUY_RSI_MAX"],
        "RISE_SCORE": config.ANALYSIS_THRESHOLDS["RISE_SCORE"],
        "WEIGHTS": config.SCORING_WEIGHTS,
    }
    status = pb.precompute_status(dfs, thresholds)
    positions = collect_positions(dfs, status, mf, args)

    # ---------- 1) 정합성: 가정 청산선 vs 실제 청산선 ----------
    trader = _StubTrader()
    rm = engine.RiskManager(trader)
    ts_cb = config.SELL_STRATEGY.get("TRAILING_STOP_CALLBACK_RATE", 5.0)

    def as_old():
        """옛 산식 재현 — 콜백을 고정 하한으로 되돌린다(복제본 대신 한 줄 치환)."""
        return lambda cb, dynamic, mfe: cb

    err_old, err_new, over_old, over_new, armed_n = [], [], 0, 0, 0
    for pos in positions:
        for rec in pos["days"]:
            info = engine.compute_trailing_stop(highest_price=rec["high"], buy_price=rec["avg"],
                                                current_price=rec["close"], ind={"atr": rec["atr"]})
            if not (info and info["armed"]):
                continue        # 무장 전에는 두 산식이 같은 ATR 손절선을 쓴다
            armed_n += 1
            t = true_stop(rec)
            orig = engine.effective_callback
            try:
                engine.effective_callback = as_old()
                s_old = assumed_stop(rec, rm, trader)
            finally:
                engine.effective_callback = orig
            s_new = assumed_stop(rec, rm, trader)
            err_old.append((s_old - t) / rec["close"] * 100)
            err_new.append((s_new - t) / rec["close"] * 100)
            over_old += 1 if s_old > t + 1e-9 else 0
            over_new += 1 if s_new > t + 1e-9 else 0

    print(f"\n[표본] 포지션 {len(positions):,}건 · 보유일 "
          f"{sum(len(p['days']) for p in positions):,}일 · TS 무장일 {armed_n:,}일")
    print("\n=== 1) 정합성 — 히트가 가정한 청산선 − 실제 청산선 (현재가 대비 %p, TS 무장일만) ===")
    print(f"{'산식':<10}{'중앙':>9}{'평균':>9}{'최대':>9}{'실제보다 높게 잡은 비율':>24}")
    print("-" * 62)
    for label, e, over in (("old(고정)", err_old, over_old), ("new(실효)", err_new, over_new)):
        a = np.array(e) if e else np.array([0.0])
        print(f"{label:<10}{np.median(a):>+9.2f}{a.mean():>+9.2f}{a.max():>+9.2f}"
              f"{over / max(1, armed_n) * 100:>23.1f}%")
    print("  ※ 양수 = 실제보다 높은 손절선을 가정 = 오픈 리스크 과소 계상 (방어가 무뎌지는 방향)")

    # ---------- 2) 포트폴리오 히트와 캡 구속 ----------
    by_date = {}
    for idx, pos in enumerate(positions):
        for rec in pos["days"]:
            by_date.setdefault(rec["date"], []).append((idx, pos["code"], rec))

    open_slots, started, qty_map, rows = {}, set(), {}, []
    for date in sorted(by_date):
        todays = by_date[date]
        live = {i for i, _c, _r in todays}
        for i in list(open_slots):
            if i not in live:
                del open_slots[i]
        for i, code, _rec in todays:
            # 진입 첫날에만 슬롯을 준다 — 슬롯이 비었다고 추세 중간에 올라타지 않는다.
            if i in started:
                continue
            started.add(i)
            if len(open_slots) < slots:
                open_slots[i] = code
        held = [(c, r) for i, c, r in todays if i in open_slots]
        if not held:
            continue
        for c, r in held:
            qty_map[c] = max(1, int(args.equity / slots / r["avg"]))
        day_recs = [(c, r) for c, r in held]

        orig = engine.effective_callback
        try:
            engine.effective_callback = as_old()
            h_old = heat_of(day_recs, qty_map, rm, trader)
        finally:
            engine.effective_callback = orig
        h_new = heat_of(day_recs, qty_map, rm, trader)
        h_true = sum(qty_map[c] * max(0.0, r["close"] - true_stop(r)) for c, r in day_recs)
        rows.append((date, h_old, h_new, h_true))

    cap_pct = getattr(config, "SYSTEM_MAX_PORTFOLIO_RISK", 10.0)
    cap = args.equity * cap_pct / 100.0
    cap_scaled = cap * 0.68        # 관측된 실계좌 스케일(휩소 0.85 × 드로다운 0.8)

    def report(sel, title):
        if not sel:
            return
        arr = np.array([[r[1], r[2], r[3]] for r in sel], dtype=float)
        print(f"\n{title}  ({len(sel):,}일)")
        print(f"{'산식':<10}{'중앙 히트%':>12}{'평균%':>9}{'최대%':>9}"
              f"{f'캡{cap_pct:g}% 초과일':>14}{'캡6.8% 초과일':>15}")
        print("-" * 70)
        for k, label in ((0, "old(고정)"), (1, "new(실효)"), (2, "true(실제)")):
            col = arr[:, k]
            print(f"{label:<10}{np.median(col) / args.equity * 100:>12.2f}"
                  f"{col.mean() / args.equity * 100:>9.2f}{col.max() / args.equity * 100:>9.2f}"
                  f"{(col > cap).mean() * 100:>13.1f}%{(col > cap_scaled).mean() * 100:>14.1f}%")

    print(f"\n=== 2) 포트폴리오 히트 (자산 {args.equity:,.0f}원 · 등가중 {slots}슬롯 모델) ===")
    report(rows, "[전체 구간]")
    dates = [r[0] for r in rows]
    for label, chunk in windows(dates, 3):
        span = set(chunk)
        sel = [r for r in rows if r[0] in span]
        report(sel, f"[{label} {chunk[0]}~{chunk[-1]}]" if chunk else f"[{label}]")

    print("\n[해석] new 가 true 에 가깝고 old 가 그보다 위(=리스크 과소)면 이번 수정은 "
          "'히트가 실제 청산선을 보게 만든 것'이고, 캡 초과일 증가는 그 정직해진 값의 결과다.")


if __name__ == "__main__":
    main()
