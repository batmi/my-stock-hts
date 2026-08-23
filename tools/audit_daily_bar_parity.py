"""실매매 일봉(KIS) vs 백테스트 일봉(pykrx/FDR) 대조.

[왜 이것이 마지막 관문인가] 지금까지 검증한 모든 것 — 지표 12종, 손절폭, TS 발동선,
포지션 크기, 캡 구속률 — 의 **입력**이 일봉이다. 두 소스가 다르면 그 검증이 통째로
실거래에 옮겨가지 않는다.

  실매매   : KIS inquire-daily-itemchartprice (FID_COND_MRKT_DIV_CODE='J' = KRX,
             FID_ORG_ADJ_PRC='0' = 수정주가)
  백테스트 : pykrx/FDR (KRX 공식, 수정주가)

둘 다 '수정주가'라고 말하지만 **권리락·배당락·액면분할 소급 반영 시점과 반올림이 다를 수
있다.** 실제로 같은 값인지는 확인한 적이 없다.

[무엇을 보는가]
  · OHLC 절대 일치율 — 원 단위로 같은가
  · ATR 괴리 — 다르면 손절폭·포지션 크기가 갈린다(가장 비싼 결과)
  · 지표 괴리 — RSI/ADX 등 판정에 쓰이는 값
  · 봉 개수·날짜 정렬 — 한쪽에만 있는 날(휴장일 처리 차이)

[안전] 모의투자 키(mode 1)로 조회한다. 시세는 실전과 동일하고 실계좌를 건드리지 않는다.
       조회 전용이며 주문 경로는 타지 않는다.

[실행] python3 tools/audit_daily_bar_parity.py [--stocks 12] [--mode 1]
"""
import argparse
import os
import sys

import numpy as np
import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import config  # noqa: E402
import indicators  # noqa: E402


def kis_daily(code):
    """실매매가 쓰는 경로 그대로. realtime=False로 당일 오버레이를 끈다."""
    import api
    df = api.get_chart_data(code, is_overseas=False, period_type='daily', realtime=False)
    if df is None or df.empty:
        return None
    d = df.copy()
    d['date'] = d['date'].astype(str)
    return d.set_index('date')[['open', 'high', 'low', 'close', 'volume']].astype(float)


def krx_daily(code, days):
    """백테스트가 쓰는 경로 그대로."""
    from modules import backtest
    df = backtest.get_backtest_data(code, False, days)
    if df is None or df.empty:
        return None
    d = df.copy()
    d['date'] = d['date'].astype(str)
    return d.set_index('date')[['open', 'high', 'low', 'close', 'volume']].astype(float)


def atr_of(d):
    return indicators.get_atr_full_series(d.reset_index())


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--stocks", type=int, default=12)
    ap.add_argument("--days", type=int, default=730)
    ap.add_argument("--mode", default="1", help="1=모의투자(권장) / 2=실전")
    args = ap.parse_args()

    config.session.initialize(mode=args.mode)
    config.session.load_stock_config()
    targets = [(s["code"], s["name"])
               for s in config.session.stock_data.get("stocks_kr", [])][:args.stocks]
    print(f"[대조] {len(targets)}종목 · KIS(mode {args.mode}) vs pykrx/FDR")
    print("  KIS는 250봉 고정(api/charts.py tail(250)). 지표 비교는 앞 150봉(워밍업) 제외.\n")

    hdr = (f"{'종목':<14}{'공통봉':>7}{'KIS만':>6}{'KRX만':>6}{'OHLC일치':>9}"
           f"{'종가불일치':>10}{'ATR괴리중앙':>12}{'ATR괴리최대':>12}{'RSI최대차':>10}")
    print(hdr); print("-" * len(hdr))

    agg = {"bars": 0, "ohlc_ok": 0, "close_bad": 0, "atr": [], "rsi": [], "bad": []}
    for code, name in targets:
        try:
            a = kis_daily(code)
            b = krx_daily(code, args.days)
        except Exception as e:
            print(f"{name:<14} 조회 실패: {e}")
            continue
        if a is None or b is None:
            print(f"{name:<14} 조회 실패(빈 데이터)")
            continue

        common = sorted(set(a.index) & set(b.index))
        only_a, only_b = len(set(a.index) - set(b.index)), len(set(b.index) - set(a.index))
        if len(common) < 60:
            print(f"{name:<14} 공통 봉 부족({len(common)})")
            continue
        A, B = a.loc[common], b.loc[common]

        same = (A[['open', 'high', 'low', 'close']].round(0)
                == B[['open', 'high', 'low', 'close']].round(0)).all(axis=1)
        close_bad = int((A['close'].round(0) != B['close'].round(0)).sum())

        # [워밍업 제외] KIS는 250봉만 준다(api/charts.py tail(250)). KRX는 그보다 훨씬 길다.
        #  공통 구간 앞부분에서는 KIS 쪽 Wilder EMA(ATR·RSI)가 아직 수렴하지 않아 큰 괴리가
        #  나는데, 이는 **데이터 차이가 아니라 이력 길이 차이**다. 두 소스가 같은 값인지
        #  묻는 이 대조에서는 잡음이므로, 앞 WARMUP봉을 빼고 잰다.
        #  (이력 길이 차이 자체의 영향은 tools/audit_live_backtest_parity.py 의
        #   'A vs B-live'가 이미 계량했다 — 매수후보 판정 99.98% 일치.)
        WARMUP = 150
        cmp_days = common[WARMUP:]
        if len(cmp_days) < 30:
            cmp_days = common[len(common) // 2:]
        aa, bb = atr_of(a), atr_of(b)
        aa.index, bb.index = a.index, b.index
        m = aa.loc[cmp_days].notna() & bb.loc[cmp_days].notna()
        atr_gap = ((aa.loc[cmp_days][m] / bb.loc[cmp_days][m] - 1).abs() * 100)
        ra, rb = indicators.get_rsi_full_series(a.reset_index()), indicators.get_rsi_full_series(b.reset_index())
        ra.index, rb.index = a.index, b.index
        mr = ra.loc[cmp_days].notna() & rb.loc[cmp_days].notna()
        rsi_gap = (ra.loc[cmp_days][mr] - rb.loc[cmp_days][mr]).abs()

        agg["bars"] += len(common); agg["ohlc_ok"] += int(same.sum())
        agg["close_bad"] += close_bad
        agg["atr"].append(atr_gap.to_numpy()); agg["rsi"].append(rsi_gap.to_numpy())
        if close_bad:
            diff = A.index[(A['close'].round(0) != B['close'].round(0)).to_numpy()]
            agg["bad"].append((name, code, list(diff[:3]),
                               [(float(A.loc[d, 'close']), float(B.loc[d, 'close'])) for d in diff[:3]]))

        print(f"{name:<14}{len(common):>7}{only_a:>6}{only_b:>6}"
              f"{same.mean()*100:>8.1f}%{close_bad:>10}"
              f"{atr_gap.median():>11.4f}%{atr_gap.max():>11.4f}%{rsi_gap.max():>9.3f}")

    if not agg["atr"]:
        print("\n비교 가능한 데이터가 없습니다.")
        return
    atr = np.concatenate(agg["atr"]); rsi = np.concatenate(agg["rsi"])
    print("\n" + "=" * 78)
    print(f"[종합] 공통 봉 {agg['bars']:,}개")
    print(f"  OHLC 4값 완전일치 {agg['ohlc_ok']/agg['bars']*100:.2f}%   종가 불일치 {agg['close_bad']}건")
    print(f"  ATR 괴리  중앙 {np.median(atr):.4f}%  p95 {np.percentile(atr,95):.4f}%  최대 {atr.max():.4f}%")
    print(f"  RSI 괴리  중앙 {np.median(rsi):.4f}   p95 {np.percentile(rsi,95):.4f}   최대 {rsi.max():.4f}")
    print()
    if atr.max() < 0.1 and agg["close_bad"] == 0:
        print("  → 두 소스가 사실상 동일하다. 백테스트 검증 결과를 실거래에 그대로 옮길 수 있다.")
    else:
        print("  → 괴리가 있다. 아래 불일치 표본을 확인할 것 (권리락·배당락 처리 차이 의심).")
        for name, code, days_, vals in agg["bad"][:5]:
            print(f"     {name}({code}) {days_} KIS={[v[0] for v in vals]} KRX={[v[1] for v in vals]}")


if __name__ == "__main__":
    main()
