"""청산 판정이 백테스트와 실매매에서 같은 답을 내는가.

[왜] 진입(스코어링) 패리티는 tools/audit_live_backtest_parity.py 에서 확인했지만,
청산은 **두 곳에 따로 구현**돼 있다.
    실매매   : engine.DefaultStrategy.analyze_sell
    백테스트 : portfolio_backtest.decide_sell
샹들리에 트레일링·BEP·시간청산·점수매도가 각각 별도 코드다. 여기가 어긋나면 백테스트
수치가 실매매를 설명하지 못하고, 그 수치로 정한 모든 파라미터(손절폭·TS 배수·리스크
배수)의 근거가 함께 흔들린다.

[방법] 같은 종목·같은 날의 **동일 입력**을 두 구현에 나란히 넣고 결론을 비교한다.
종목마다 일정 간격으로 가상 포지션을 열고 청산될 때까지 하루씩 굴린다.

[두 가지 불일치를 구분한다]
  ① 로직 불일치 — 같은 입력인데 판정이 다르다. 코드 결함이다.
  ② 관측 불일치 — 입력 자체가 다르다. 백테스트는 그날의 고가(high)를 알지만 실매매는
     주기마다 본 현재가의 최댓값만 안다. 이건 구조적 한계이며 '백테스트가 낙관적'이라는
     뜻이므로, 결함이 아니라 편향의 크기로 보고해야 한다.
  → ①은 high를 양쪽에 똑같이 주고 재고, ②는 실매매식 high(종가 최댓값)로 다시 재서 차이를 본다.

[수급(스마트머니) 축] 기본값은 양쪽 False 고정이다. 이 도구는 자격 증명을 싣지 않으므로,
그냥 두면 양쪽의 KIS 투자자 조회가 **모두 실패해** smart_money 가 조용히 False 로 통일된다.
그러면 '불일치 0'이 축 하나를 건너뛴 결과인지 진짜 일치인지 구분할 수 없다 — 아래 --set
도움말이 경계하는 무의미한 0과 같은 종류다. 그래서 명시적으로 고정하고 출력에 적는다.

--smart-money live 로 축까지 굴릴 수 있다(실측 2026-08-10: 20종목 730일에서 백테스트
smart_money 가 15,022봉 중 322봉(2.14%) True — 축은 실제로 발현한다. 그 상태에서도
청산 판정 불일치는 0건이었다).

  [한계] 그래도 '축까지 검증했다'고 말하면 안 된다. 백테스트는 그날의 수급을 보지만
  실매매 경로(analysis.check_smart_money_turnaround)는 **오늘** 값만 안다 — 과거를
  재생할 방법이 없다. 즉 두 경로는 같은 날의 같은 신호를 비교하는 것이 아니다.
  불일치 0은 '이 표본의 청산 판정을 수급 축이 가르지 않았다'로 읽어야 한다.

[실행] python tools/audit_exit_parity.py [--stocks 20] [--days 730] [--step 10]
       python tools/audit_exit_parity.py --smart-money live
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
from modules import backtest, portfolio_backtest as pbt  # noqa: E402
from modules.auto_trade import DefaultStrategy  # noqa: E402

# 실매매 reason 문자열 → 카테고리. 백테스트 reason은 이미 카테고리와 같다.
def categorize(reason):
    if not reason:
        return "보유"
    # '이익보호'가 빠지면 어느 분기에도 안 걸려 양쪽 다 '점수하락'으로 접히고,
    #  이익보호 ↔ 점수하락 불일치가 통째로 가려진다.
    for key in ("본전청산", "시간청산", "트레일링스탑", "이익보호"):
        if reason.startswith(key):
            return key
    # 'ATR손절'과 '손절'은 같은 판정이다. analyze_sell은 항상 '손절'을 내고,
    #  trader._sell_worker가 ATR 적용 여부를 보고 이름만 바꾼다. 표기 차이를 불일치로
    #  세면 안 되므로 한 카테고리로 접는다.
    if reason.startswith("ATR손절") or reason.startswith("손절"):
        return "손절"
    # 점수매도는 양쪽 모두 상태 사유 문자열을 그대로 쓸 수 있어(백테스트: state_reason,
    #  실매매: '매도진입(...)') 남는 것은 전부 이 계열이다.
    return "점수하락"


def _cfg():
    """[SSOT] 조립은 portfolio_backtest.build_sell_cfg 가 단독으로 한다.

    종전에는 여기서 dict를 손으로 다시 썼고 time_stop_min 이 빠져 있었다 — 백테스트만
    옛 규칙(문턱 0)으로 돌아 TIME_STOP_MIN_PROFIT_RATE 를 켠 대조가 통째로 거짓
    불일치가 됐다(기본값 0.0이라 지금까지 드러나지 않았다).
    """
    return pbt.build_sell_cfg()


def _effective_sl(sl_rate, high, avg, applied):
    """백테스트 _effective_sl 과 같은 규칙 (BEP 상향). BEP 토글도 함께 따른다."""
    s = config.SELL_STRATEGY
    if not s.get("USE_BREAK_EVEN_STOP", False):
        return sl_rate, False
    bep_stop = s.get("BREAK_EVEN_STOP_RATE", 0.5)
    bep_default = s.get("BREAK_EVEN_PROFIT_RATE", 5.0)
    max_profit = (high - avg) / avg * 100
    activation = abs(sl_rate) if (applied and sl_rate < 0) else bep_default
    if max_profit >= activation and sl_rate < bep_stop:
        return bep_stop, True
    return sl_rate, False


def run(dfs, status, step, live_high, thresholds, min_history):
    """(비교건수, 불일치 목록, 카테고리별 교차표)"""
    strat = DefaultStrategy()
    cfg = _cfg()
    s = config.SELL_STRATEGY
    default_sl = s["STOP_LOSS_RATE"]
    atr_mult = s.get("ATR_STOP_MULTIPLIER", 2.0)
    max_hold = 120

    n, mismatches, cross = 0, [], Counter()

    for code, df in dfs.items():
        recs = df.to_dict("records")
        dmap = {str(r["date"]): i for i, r in enumerate(recs)}
        # [중요] 실매매는 CHART_LOOKBACK_DAYS(730일 ≈ 494 거래일)치를 받아 지표를 계산한다.
        #  하네스가 짧은 프레임을 주면 EMA120이 아직 수렴하지 않아(120봉 0.21% → 500봉 0.0004%)
        #  코드가 아니라 워밍업 때문에 판정이 갈린다. 실제 조건과 같은 이력을 보장한다.
        for start in range(min_history, len(recs) - 5, step):
            entry = recs[start]
            avg = float(entry["close"])
            if avg <= 0:
                continue
            sl_rate = pbt._atr_stop_rate(entry.get("ATR", 0), avg, atr_mult)
            applied = sl_rate != 0.0
            if not applied:
                sl_rate = default_sl
            high = avg

            for j in range(start + 1, min(start + max_hold, len(recs))):
                row = recs[j]
                price = float(row["close"])
                if price <= 0:
                    break
                # ① 로직 비교용: 양쪽에 같은 high를 준다. ②면 실매매식(종가 최댓값).
                high = max(high, price if live_high else float(row["high"]))
                eff_sl, is_bep = _effective_sl(sl_rate, high, avg, applied)
                holding_days = (pd.to_datetime(str(row["date"]), format="%Y%m%d")
                                - pd.to_datetime(str(entry["date"]), format="%Y%m%d")).days
                raw_score, sell_check, _cb, state, state_reason = status[code][str(row["date"])]

                b_sell, b_reason = pbt.decide_sell(
                    price=price, high=high, avg=avg, sl_rate=eff_sl, atr_applied=applied,
                    is_bep=is_bep, holding_days=holding_days, state=state,
                    state_reason=state_reason, raw_score=raw_score, sell_check=sell_check,
                    ema60=row.get("EMA60"), atr=row.get("ATR", 0),
                    roll_high_5=row.get("roll_high_5", 0),
                    roll_high_10=row.get("roll_high_10", 0), cfg=cfg)

                sub = df.iloc[:j + 1]
                profit_rate = (price - avg) / avg * 100
                # [중요] 실매매의 engine.build_sell_thresholds 와 **같은 방식**으로 만든다.
                #  ATR 손절 적용 시 BEP 발동 기준을 손절폭 절대값으로 동기화하는 것까지
                #  포함해야 한다. 이걸 빠뜨리면 실매매가 기본 +5%에 BEP를 걸어, 코드가
                #  아니라 하네스 때문에 불일치가 생긴다.
                th = dict(thresholds)
                if applied:
                    th["ATR_APPLIED_SL_RATE"] = sl_rate
                    th["STOP_LOSS_RATE"] = sl_rate
                    if sl_rate < 0:
                        th["BREAK_EVEN_PROFIT_RATE"] = abs(sl_rate)
                res = strat.analyze_sell(code, code, sub, price, avg, profit_rate,
                                         thresholds=th, holding_days=holding_days,
                                         highest_price=high)
                l_sell = res["action"] == "sell"
                l_cat = categorize(res["reason"])
                b_cat = categorize(b_reason) if b_sell else "보유"

                n += 1
                cross[(b_cat, l_cat)] += 1
                if b_sell != l_sell or (b_sell and l_sell and b_cat != l_cat):
                    mismatches.append({
                        "code": code, "date": str(row["date"]), "day": holding_days,
                        "profit": round(profit_rate, 2),
                        "maxprofit": round((high - avg) / avg * 100, 2),
                        "sl": round(eff_sl, 2), "bep": is_bep,
                        "backtest": b_cat, "live": l_cat,
                    })
                if b_sell or l_sell:
                    break   # 어느 쪽이든 청산되면 이 포지션은 종료
    return n, mismatches, cross


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--stocks", type=int, default=20)
    ap.add_argument("--days", type=int, default=730)
    ap.add_argument("--step", type=int, default=10)
    ap.add_argument("--min-history", type=int, default=400,
                    help="실매매가 받는 봉수(≈494)에 맞춰 이 인덱스 이후부터만 비교한다")
    ap.add_argument("--overseas", action="store_true",
                    help="해외 일봉으로 대조한다(stocks_us + etfs_us). 청산 로직은 국내와 "
                         "같은 analyze_sell을 타지만 데이터 소스가 다르다")
    ap.add_argument("--smart-money", choices=["off", "live"], default="off",
                    help="수급(스마트머니) 축 처리. off=양쪽 모두 False로 고정하고 채점 로직만 "
                         "비교(기본·재현 가능). live=실제 API로 양쪽을 굴려 축까지 대조 "
                         "(실전 시세 계좌 필요, 결과가 조회 성공 여부에 좌우된다)")
    ap.add_argument("--mode", default="4", choices=["1", "2", "4"],
                    help="--smart-money live 일 때 쓸 투자 모드 (기본 4=가상투자)")
    ap.add_argument("--set", action="append", default=[], metavar="KEY=VALUE",
                    help="SELL_STRATEGY 값을 덮어쓰고 잰다. 기본 OFF인 청산 스위치는 "
                         "그냥 돌리면 두 구현 모두 그 분기를 타지 않아 '불일치 0'이 "
                         "무의미해진다 (예: --set PROFIT_LOCK_USE=true)")
    args = ap.parse_args()

    for kv in args.set:
        key, _, raw = kv.partition("=")
        key = key.strip()
        v = raw.strip()
        if v.lower() in ("true", "false"):
            val = v.lower() == "true"
        else:
            try:
                val = float(v) if "." in v else int(v)
            except ValueError:
                val = v
        config.SELL_STRATEGY[key] = val
        print(f"[오버라이드] SELL_STRATEGY[{key!r}] = {val!r}")

    # [중요] 수급 축을 '어쩌다 꺼진 채로' 재지 않는다.
    #  이 도구는 자격 증명을 싣지 않아, 세션 없는 환경에서 돌리면 양쪽의 KIS 투자자 조회가
    #  모두 실패해 smart_money 가 조용히 False 로 통일된다. 그러면 '불일치 0'이 축 하나를
    #  건너뛴 결과인지 진짜 일치인지 구분할 수 없다 — --set 도움말이 경계하는 그 무의미한
    #  0과 같은 종류다. 기본값은 양쪽을 명시적으로 False 로 고정하고, 그 사실을 출력한다.
    if args.smart_money == "off":
        from modules import analysis as _an
        _an.check_smart_money_turnaround = lambda code, is_overseas=False: (False, "")
        _orig_append = backtest._append_smart_money_signal
        def _no_smart(df, code, is_overseas):
            df = df.copy()
            df["smart_money"] = False
            return df
        backtest._append_smart_money_signal = _no_smart
        sm_note = "수급 축: 양쪽 False 고정 (채점 로직만 비교 · API 호출 없음)"
    else:
        config.session.initialize(args.mode)
        sm_note = f"수급 축: 실제 조회 (mode {args.mode}) — 조회 실패 시 결과가 무의미해진다"
    print(f"[준비] {sm_note}")

    data = json.load(open(config.STOCK_DATA_FILE))
    keys = ("stocks_us", "etfs_us") if args.overseas else ("stocks_kr",)
    pool = [s for k in keys for s in data.get(k, [])]
    targets = [(s["code"], s["name"]) for s in pool][:args.stocks]
    print(f"[준비] {'해외' if args.overseas else '국내'} {len(targets)}종목 · {args.days}일 "
          f"· 진입간격 {args.step}일 · 최소이력 {args.min_history}봉")

    dfs, _mf, _dates, failed = pbt.prepare_universe(targets, args.days,
                                                    is_overseas=args.overseas)
    thresholds = {
        "BUY_SCORE": config.ANALYSIS_THRESHOLDS["BUY_SCORE"],
        "BUY_RSI_MAX": config.ANALYSIS_THRESHOLDS["BUY_RSI_MAX"],
        "RISE_SCORE": config.ANALYSIS_THRESHOLDS["RISE_SCORE"],
        "WEIGHTS": config.SCORING_WEIGHTS,
    }
    status = pbt.precompute_status(dfs, thresholds)
    print(f"[준비] 사용 {len(dfs)}종목" + (f" · 제외 {len(failed)}" if failed else ""))

    for label, live_high in (("① 로직 대조 (high 동일)", False),
                             ("② 관측 차이 (실매매식 high=종가최댓값)", True)):
        n, mism, cross = run(dfs, status, args.step, live_high, thresholds, args.min_history)
        rate = len(mism) / n * 100 if n else 0.0
        print(f"\n{'=' * 78}\n{label}\n{'=' * 78}")
        print(f"비교 {n:,}건 · 불일치 {len(mism):,}건 ({rate:.2f}%)   [{sm_note}]")

        if mism:
            pat = Counter((m["backtest"], m["live"]) for m in mism)
            print(f"\n{'백테스트':<12}{'실매매':<12}{'건수':>7}   대표 사례")
            print("-" * 78)
            for (b, l), c in pat.most_common(10):
                ex = next(m for m in mism if m["backtest"] == b and m["live"] == l)
                print(f"{b:<12}{l:<12}{c:>7}   {ex['code']} {ex['date']} "
                      f"수익{ex['profit']:+.1f}% 최고{ex['maxprofit']:+.1f}% "
                      f"손절{ex['sl']:.1f}% BEP={ex['bep']}")
        else:
            print("불일치 없음.")


if __name__ == "__main__":
    main()
