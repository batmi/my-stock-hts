"""청산 체결 시점 — 실제 분봉으로 하루를 되감아 마지막 반론까지 지운다.

[어디까지 왔나] tools/audit_exit_timing.py 가 일봉의 고가·저가로 근사해, 장중 청산이
 종가 청산보다 크게 열위이고 그 원인이 트레일링스탑임을 보였다. 익일 시가 브래킷으로
 '집행가를 미리 안다'는 반론도 지웠다. 남은 반론은 하나였다 —
   **"종가 판정 팔은 그날 종가가 선 위였다는 걸 미리 안다"(판정 시점의 이점)**
 이건 일봉으로는 지울 수 없다. 분봉이 있어야 한다.

[분봉이 지우는 것]
   · 고가·저가의 선후 가정(low_first/high_first)이 사라진다 — 봉 순서가 사실이다
   · 판정 시각을 봉으로 고정할 수 있다. 60분봉의 **14:00 봉 종가 = 15:00 가격**이므로
     '마감 30분 전 1회 판정'을 재현할 수 있고, 그 시점에는 종가를 알 수 없다
     (실제 운용안인 15:20 판정보다 30분 이르므로 이쪽이 더 불리한 가정이다)

[모델 규약]
   · 판정 가격 = 봉 **종가**(그 시점의 현재가). 봉 저가를 쓰면 '매 순간 감시'가 되어
     실매매(주기 감시)보다 과해진다. 60분봉은 하루 7회 감시이므로 실매매(60초 주기)보다
     **덜** 잡는다 → 장중 팔에 유리한 방향으로 보수적이다.
   · 트레일링 고점 = 그 봉까지의 고가 러닝맥스(실매매 highest_price와 같은 갱신)
   · ATR 등 지표 = **전일 확정 봉**. 그 시점에 확정된 정보만 쓴다.

[팔 구성] 기준선은 현 실매매(매 봉 판정).
   A. 종가(일봉 모델)   — 기존 감사와의 연결선
   B. 매 봉 판정        — 현 실매매 (기준선)
   C. 15:00 1회 판정    — 손절·TS 모두 마감 30분 전에만
   D. 손절 매 봉·TS 15:00 — **적용 후보.** 급락은 즉시 막고 트레일링만 확인 후 집행
   D2. D + TS를 종가(단일가)에 체결   — 15:00에 못 팔고 밀린 경우
   D3. D + TS를 익일 시가에 체결      — 그날 아예 못 판 경우(하룻밤 갭 전부 감수)
   E. 15:30 1회 판정    — 분봉 경로로 종가 판정을 재현(A와 거의 같아야 정상 · 자기검증)

[데이터] tools/fetch_intraday_tv.py 로 먼저 캐시할 것. 일봉과 OHLC 98% 미만으로
 어긋나는 종목은 자동 제외한다(TV 심볼 불일치·수정주가 소급 차이).

[실행] python3 tools/audit_exit_bars.py --days 1080 --trials 15 --sample 20 --subperiods 3
"""
import argparse
import os
import random
import sys

import numpy as np
import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tools.audit_common import exits  # noqa: E402

import config  # noqa: E402
from modules import intraday_bars as ib  # noqa: E402
from modules import portfolio_backtest as pb  # noqa: E402

INITIAL_CAPITAL = 10_000_000
BASE = "B. 매 봉 판정(실매매)"
AT_1500 = {"1400"}   # 14:00 봉의 종가 = 15:00 가격
AT_CLOSE = {"1500"}  # 15:00 봉의 종가 = 15:30 종가


def arms(bars, st):
    # intraday_status 는 ATR 을 '그 시점의 진행 봉' 기준으로 맞추기 위해 넘긴다.
    #  intraday_entry 를 켜지 않으므로 진입은 네 팔 모두 종가 그대로다(축 분리).
    # [축 분리] 증액은 네 팔 모두 일봉(종가)으로 고정한다. 켜두면 증액 건수가 함께 변해
    #  청산 축만 재는 이 실험이 오염되고, E팔의 자기검증(일봉 모델과 일치)도 깨진다.
    b = {"intraday_bars": bars, "intraday_status": st, "intraday_pyramid": False}
    return [
        ("A. 종가(일봉 모델)", {}),
        (BASE,                 dict(b)),
        ("C. 15:00 1회",       {**b, "bar_stop_times": AT_1500, "bar_ts_times": AT_1500}),
        ("D. 손절매봉·TS15:00", {**b, "bar_ts_times": AT_1500}),
        # [체결 위험 브래킷] 15:00에 판정해도 그 자리에서 못 팔 수 있다. 두 단계로 값을 매긴다.
        #  15:20~15:30은 KRX 종가 단일가라 시스템이 매매하지 않으므로(common.is_system_market_open)
        #  '종가까지 밀림'과 '그날 못 팔고 익일 시가'가 현실적인 최악 두 단계다.
        ("D2. TS15:00·종가체결", {**b, "bar_ts_times": AT_1500, "bar_ts_defer": "close"}),
        ("D3. TS15:00·익일시가", {**b, "bar_ts_times": AT_1500, "bar_ts_defer": "next_open"}),
        ("E. 15:30 1회(자기검증)", {**b, "bar_stop_times": AT_CLOSE, "bar_ts_times": AT_CLOSE}),
    ]


def metrics(r):
    sells = exits(r)
    profits = sorted((t["profit"] for t in sells), reverse=True)
    top10 = profits[:max(1, len(profits) // 10)]
    return {
        "ret": r["total_return"], "mdd": r["mdd"],
        "mar": r["total_return"] / abs(r["mdd"]) if r["mdd"] else float("nan"),
        "pf": r["pf"], "n": len(sells),
        "stop_n": sum(1 for t in sells if t["reason"] in ("손절", "ATR손절")),
        "ts_n": sum(1 for t in sells if t["reason"] == "트레일링스탑"),
        "intra": r.get("intraday_exits", 0),
        "top10": float(np.mean(top10)) if top10 else 0.0,
        "best": profits[0] if profits else 0.0,
        "big": sum(1 for p in profits if p >= 30),
        "days": float(np.median([t["days"] for t in sells])) if sells else 0.0,
    }



def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--trials", type=int, default=15)
    ap.add_argument("--sample", type=int, default=20)
    ap.add_argument("--days", type=int, default=1080)
    ap.add_argument("--interval", default="60m")
    ap.add_argument("--slots", type=int, default=None)
    ap.add_argument("--seed-capital", type=int, default=INITIAL_CAPITAL)
    ap.add_argument("--seed", type=int, default=20260816)
    ap.add_argument("--subperiods", type=int, default=3)
    ap.add_argument("--min-coverage", type=float, default=0.9,
                    help="분봉 일수가 중앙값의 이 배 미만인 종목은 제외(늦은 상장 등)")
    ap.add_argument("--exclude-from", default="20260301")
    args = ap.parse_args()

    slots = args.slots or getattr(config, "SYSTEM_MAX_HOLDINGS", 4)
    config.session.load_stock_config()
    stocks = config.session.stock_data.get("stocks_kr", [])
    targets = [(s["code"], s["name"]) for s in stocks]
    names = {s["code"]: s["name"] for s in stocks}
    print(f"[준비] 관심종목 {len(targets)}개 · {args.days}일 · 슬롯 {slots} · {args.interval}")

    dfs, mf, dates, failed = pb.prepare_universe(targets, args.days)
    # 게이트(일봉 정합 98% · 커버리지)는 modules/intraday_bars.gate_universe 가 단독 보유한다.
    bars, stat, keep, drop = ib.gate_universe(dfs, args.interval,
                                              min_coverage=args.min_coverage)
    if drop:
        print(f"[제외] {len(drop)}종목 — "
              + ", ".join(f"{names.get(c, c)}({why})" for c, why in drop))
    dfs = {c: dfs[c] for c in keep}
    mf = {c: mf.get(c, set()) for c in keep}

    dates = ib.covered_dates(bars, dates)
    thresholds = {
        "BUY_SCORE": config.ANALYSIS_THRESHOLDS["BUY_SCORE"],
        "BUY_RSI_MAX": config.ANALYSIS_THRESHOLDS["BUY_RSI_MAX"],
        "RISE_SCORE": config.ANALYSIS_THRESHOLDS["RISE_SCORE"],
        "WEIGHTS": config.SCORING_WEIGHTS,
    }
    status = pb.precompute_status(dfs, thresholds)
    print(f"[준비] 사용 {len(dfs)}종목 / 분봉 있는 거래일 {len(dates)}일 "
          f"({dates[0]}~{dates[-1]})" if dates else "[중단] 겹치는 거래일 없음")
    if not dates:
        return

    from tools.audit_drawdown_axis import market_scale_by_date, make_scale_fn
    p = getattr(config, "RISK_SCALING_PARAMS", {}) or {}
    mkt = market_scale_by_date(dates, args.days)
    dd = None
    if p.get("USE_DRAWDOWN_RISK_SCALING", True):
        dd = (int(p.get("DD_LOOKBACK_DAYS", 90)), float(p.get("DD_LEVEL_1", 5.0)),
              float(p.get("DD_SCALE_1", 0.9)), float(p.get("DD_LEVEL_2", 10.0)),
              float(p.get("DD_SCALE_2", 0.8)))
    new_scale_fn = lambda: make_scale_fn(mkt, dd)  # noqa: E731

    cut = "".join(ch for ch in str(args.exclude_from) if ch.isdigit())
    head = [d for d in dates if not cut or cut == "0" or d < cut]
    tail = [d for d in dates if cut and cut != "0" and d >= cut]

    sets = arms(bars, stat)
    codes = list(dfs.keys())
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
        results = {label: [] for label, _kw in sets}
        rng = random.Random(args.seed)
        for t in range(args.trials):
            pick = rng.sample(codes, min(args.sample, len(codes)))
            sd = {c: dfs[c] for c in pick}
            st = {c: status[c] for c in pick}
            sm = {c: mf.get(c, set()) for c in pick}
            for label, kw in sets:
                kw2 = dict(kw)
                if "intraday_bars" in kw2:
                    kw2["intraday_bars"] = {c: bars[c] for c in pick}
                    kw2["intraday_status"] = {c: stat[c] for c in pick}
                r = pb.run_portfolio(sd, st, wdates, initial_capital=args.seed_capital,
                                     slots=slots, market_filter_dates=sm,
                                     risk_scale_by_date=new_scale_fn(), **kw2)
                results[label].append(metrics(r))
            print(f"  {wname} 시행 {t + 1}/{args.trials}", end="\r", flush=True)
        all_results[wname] = results
    print(" " * 50, end="\r")

    W = 122
    print(f"\n{'=' * W}")
    print(f"청산 판정 시점(실제 {args.interval} 분봉) — {args.trials}회 × {args.sample}종목 "
          f"짝비교 (기준선: {BASE})")
    print(f"{'=' * W}")
    for wname, wdates in windows:
        results = all_results[wname]
        base = results[BASE]
        print(f"\n########## {wname} ({len(wdates)} 거래일) ##########")
        print(f"{'설정':<22}{'수익%':>9}{'MDD%':>8}{'MAR':>7}{'PF':>6}{'청산':>6}{'손절':>6}"
              f"{'TS':>5}{'장중':>6}{'상위10%':>9}{'최대':>9}{'>30%':>6}{'보유일':>7}"
              f"{'승-무-패':>10}{'MAR승':>7}")
        print("-" * W)
        for label, _kw in sets:
            rs = results[label]
            m = lambda k: float(np.median([x[k] for x in rs]))  # noqa: E731
            is_base = label == BASE
            tie = sum(1 for a, b in zip(rs, base) if abs(a["ret"] - b["ret"]) < 1e-9)
            los = sum(1 for a, b in zip(rs, base) if a["ret"] < b["ret"] - 1e-9)
            rw = sum(1 for a, b in zip(rs, base) if a["ret"] > b["ret"])
            mw = sum(1 for a, b in zip(rs, base) if a["mar"] > b["mar"])
            print(f"{label:<22}{m('ret'):>9.1f}{m('mdd'):>8.1f}{m('mar'):>7.2f}{m('pf'):>6.2f}"
                  f"{m('n'):>6.0f}{m('stop_n'):>6.0f}{m('ts_n'):>5.0f}{m('intra'):>6.0f}"
                  f"{m('top10'):>9.1f}{m('best'):>9.1f}{m('big'):>6.0f}{m('days'):>7.1f}"
                  f"{'—' if is_base else f'{rw}-{tie}-{los}':>10}"
                  f"{'—' if is_base else f'{mw}/{len(rs)}':>7}")

    print("\n" + "-" * W)
    print("[읽는 법] E(15:30 1회)가 A(종가 일봉 모델)와 비슷해야 분봉 경로가 건전하다.")
    print(" C·D가 B를 이기면 '판정을 마감 직전으로 미루는 것'이 유리하다는 뜻이고,")
    print(" 이번엔 종가를 미리 아는 이점이 섞이지 않았다(14:00 봉 종가 = 15:00 가격).")


if __name__ == "__main__":
    main()
