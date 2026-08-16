"""청산 체결 시점 — 실매매는 장중에 치고 백테스트는 종가에 판다.

[왜] `config.USE_KRX_CLOSE_AFTER_HOURS` 주석대로 **실매매의 손절·트레일링 트리거는 항상
 실시간가**다. 선을 이탈하는 순간 장중에 나간다. 그런데 포트폴리오 백테스트는 일봉의
 `low` 를 어디에도 쓰지 않는다 — 모든 청산이 종가 체결이다. 즉 손절폭·ATR 배수·TS 발동선·
 콜백·BEP 등 **청산 다이얼 전부가 '종가 체결' 세계에서 정해졌는데 실매매는 다른 세계에서
 돈다.** 두 세계의 차이를 잰 적이 없다(audit_exit_parity 는 '같은 입력에 같은 판정인가'만
 확인하며, 체결 시점 차이는 구조적 한계로 남겨두었다).

[무엇을 보는가]
   · 백테스트가 얼마나 낙관적인가 — 종가 청산 대비 장중 청산의 수익·MDD 격차
   · 어느 다리가 값을 치르는가 — 손절만 장중 / TS만 장중으로 분해
   · 장중 청산이 실제로 몇 건인가 — 이 값이 작으면 애초에 논점이 아니다

[팔 구성] 기준선은 현 실매매(둘 다 장중)다.
   A. 둘 다 종가   — 현 백테스트. 모든 청산 다이얼의 출처
   B. 둘 다 장중   — 현 실매매 (기준선)
   C. 손절만 장중  — 급락은 즉시 막고 트레일링 휩소만 종가로 확인
   D. TS만 장중    — 반대 조합(대조군)

[경로 불확실성] 일봉은 고가·저가의 선후를 모른다. `--path` 로 두 극단을 다 재고 **띠로**
 읽어야 한다. low_first 는 트레일링선을 전일까지의 고점으로 긋고(덜 걸림), high_first 는
 오늘 고가까지 반영해 선을 올린 뒤 저가를 맞힌다(더 걸림). 결론이 두 경로에서 갈리면
 그 결론은 일봉으로 낼 수 없는 것이다.

[실행] python3 tools/audit_exit_timing.py --days 3650 --trials 15 --sample 25 --subperiods 4
      python3 tools/audit_exit_timing.py --path high_first
"""
import argparse
import os
import random
import sys

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import config  # noqa: E402
from modules import portfolio_backtest as pb  # noqa: E402

INITIAL_CAPITAL = 10_000_000
SELL_REASONS = ("ATR손절", "손절", "본전청산", "시간청산", "트레일링스탑", "점수하락", "이익보호")
BASE = "B. 둘 다 장중"


def arms(path):
    return [
        ("A. 둘 다 종가", {}),
        (BASE,            {"exit_intraday": True, "exit_path": path}),
        ("C. 손절만 장중", {"exit_intraday": True, "exit_path": path, "exit_intraday_only": "stop"}),
        ("D. TS만 장중",   {"exit_intraday": True, "exit_path": path, "exit_intraday_only": "ts"}),
    ]


def metrics(r):
    sells = [t for t in r["trades"] if t["reason"] in SELL_REASONS]
    profits = sorted((t["profit"] for t in sells), reverse=True)
    top10 = profits[:max(1, len(profits) // 10)]
    stops = [t for t in sells if t["reason"] in ("손절", "ATR손절")]
    ts = [t for t in sells if t["reason"] == "트레일링스탑"]
    return {
        "ret": r["total_return"], "mdd": r["mdd"],
        "mar": r["total_return"] / abs(r["mdd"]) if r["mdd"] else float("nan"),
        "pf": r["pf"],
        "n": len(sells),
        "win": sum(1 for p in profits if p > 0) / len(profits) * 100 if profits else 0.0,
        "stop_n": len(stops),
        "ts_n": len(ts),
        "intra": r.get("intraday_exits", 0),
        "bad": r.get("intraday_mismatch", 0),
        "top10": float(np.mean(top10)) if top10 else 0.0,
        "best": profits[0] if profits else 0.0,
        "big": sum(1 for p in profits if p >= 30),
        "days": float(np.median([t["days"] for t in sells])) if sells else 0.0,
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--trials", type=int, default=15)
    ap.add_argument("--sample", type=int, default=25)
    ap.add_argument("--days", type=int, default=3650)
    ap.add_argument("--slots", type=int, default=None)
    ap.add_argument("--seed-capital", type=int, default=INITIAL_CAPITAL)
    ap.add_argument("--seed", type=int, default=20260816)
    ap.add_argument("--subperiods", type=int, default=4)
    ap.add_argument("--path", default="low_first", choices=("low_first", "high_first"))
    ap.add_argument("--exclude-from", default="20260301")
    args = ap.parse_args()

    slots = args.slots or getattr(config, "SYSTEM_MAX_HOLDINGS", 4)
    config.session.load_stock_config()
    targets = [(s["code"], s["name"]) for s in config.session.stock_data.get("stocks_kr", [])]
    print(f"[준비] 관심종목 {len(targets)}개 · {args.days}일 · 슬롯 {slots} · 경로 {args.path}")
    print(f"[기준] 손절 ATR×{config.SELL_STRATEGY.get('ATR_STOP_MULTIPLIER')} · "
          f"TS 발동 {config.SELL_STRATEGY.get('TS_ACTIVATION_MODE')} · "
          f"콜백 ATR×{config.SELL_STRATEGY.get('TRAILING_ATR_MULTIPLIER')}")

    dfs, mf, dates, failed = pb.prepare_universe(targets, args.days)
    thresholds = {
        "BUY_SCORE": config.ANALYSIS_THRESHOLDS["BUY_SCORE"],
        "BUY_RSI_MAX": config.ANALYSIS_THRESHOLDS["BUY_RSI_MAX"],
        "RISE_SCORE": config.ANALYSIS_THRESHOLDS["RISE_SCORE"],
        "WEIGHTS": config.SCORING_WEIGHTS,
    }
    status = pb.precompute_status(dfs, thresholds)
    print(f"[준비] 사용 {len(dfs)}종목 / 거래일 {len(dates)}일")

    from tools.audit_drawdown_axis import market_scale_by_date, make_scale_fn
    p = getattr(config, "RISK_SCALING_PARAMS", {}) or {}
    mkt = market_scale_by_date(dates, args.days)
    dd = None
    if p.get("USE_DRAWDOWN_RISK_SCALING", True):
        dd = (int(p.get("DD_LOOKBACK_DAYS", 90)), float(p.get("DD_LEVEL_1", 5.0)),
              float(p.get("DD_SCALE_1", 0.9)), float(p.get("DD_LEVEL_2", 10.0)),
              float(p.get("DD_SCALE_2", 0.8)))
    # [실행마다 새로 만든다] 콜러블이 자산곡선 이력을 들고 있어 재사용하면 짝비교가 깨진다.
    new_scale_fn = lambda: make_scale_fn(mkt, dd)  # noqa: E731

    cut = "".join(ch for ch in str(args.exclude_from) if ch.isdigit())
    head = [d for d in dates if not cut or cut == "0" or "".join(filter(str.isdigit, d)) < cut]
    tail = [d for d in dates if cut and cut != "0" and "".join(filter(str.isdigit, d)) >= cut]

    sets = arms(args.path)
    codes = list(dfs.keys())

    k = max(1, args.subperiods)
    size = max(1, len(head) // k)
    windows = [("제외 전 전체", head)]
    if k > 1:
        windows += [(f"구간{i + 1}", head[i * size:(i + 1) * size if i < k - 1 else len(head)])
                    for i in range(k)]
    if tail:
        windows.append(("[대조] 제외구간(고변동)", tail))

    all_results, bad_total = {}, 0
    for wname, wdates in windows:
        results = {label: [] for label, _kw in sets}
        rng = random.Random(args.seed)
        for t in range(args.trials):
            pick = rng.sample(codes, min(args.sample, len(codes)))
            sd = {c: dfs[c] for c in pick}
            st = {c: status[c] for c in pick}
            sm = {c: mf.get(c, set()) for c in pick}
            for label, kw in sets:
                r = pb.run_portfolio(sd, st, wdates, initial_capital=args.seed_capital,
                                     slots=slots, market_filter_dates=sm,
                                     risk_scale_by_date=new_scale_fn(), **kw)
                m = metrics(r)
                bad_total += m["bad"]
                results[label].append(m)
            print(f"  {wname} 시행 {t + 1}/{args.trials}", end="\r", flush=True)
        all_results[wname] = results
    print(" " * 50, end="\r")

    W = 120
    print(f"\n{'=' * W}")
    print(f"청산 체결 시점 — {args.trials}회 × {args.sample}종목 짝비교 · 경로 {args.path} "
          f"(기준선: {BASE} = 현 실매매)")
    print(f"{'=' * W}")
    for wname, wdates in windows:
        results = all_results[wname]
        base = results[BASE]
        print(f"\n########## {wname} ({len(wdates)} 거래일) ##########")
        print(f"{'설정':<16}{'수익%':>9}{'MDD%':>8}{'MAR':>7}{'PF':>6}{'청산':>6}{'승률%':>7}"
              f"{'손절':>6}{'TS':>5}{'장중':>6}{'상위10%':>9}{'최대':>9}{'>30%':>6}{'보유일':>7}"
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
            print(f"{label:<16}{m('ret'):>9.1f}{m('mdd'):>8.1f}{m('mar'):>7.2f}{m('pf'):>6.2f}"
                  f"{m('n'):>6.0f}{m('win'):>7.1f}{m('stop_n'):>6.0f}{m('ts_n'):>5.0f}"
                  f"{m('intra'):>6.0f}{m('top10'):>9.1f}{m('best'):>9.1f}{m('big'):>6.0f}"
                  f"{m('days'):>7.1f}"
                  f"{'—' if is_base else f'{rw}-{tie}-{los}':>10}"
                  f"{'—' if is_base else f'{mw}/{len(rs)}':>7}")

    print("\n" + "-" * W)
    print(f"청산선 산식 자기검증 실패: {bad_total}건 (0이어야 정상 — decide_sell과 교차대조)")
    print("[읽는 법] A가 이기면 백테스트가 낙관적이었다는 뜻이고, 동시에 '종가 확인 청산'이")
    print(" 개선안 후보가 된다. 단 두 경로(low_first/high_first)에서 같은 방향이어야 한다.")


if __name__ == "__main__":
    main()
