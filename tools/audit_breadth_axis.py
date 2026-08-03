"""[검증 전용] 시장 폭(breadth)이 기존 지수-가격 축에 정보를 더하는가.

현행 시장 판단 축(국면·휩소율·시장필터·드로다운)은 전부 '지수 종가'의 함수라
정보 축이 사실상 하나다. 여기서는 상관이 낮은 후보인 **시장 폭**
(= 개별 종목 중 200일선 위에 있는 비율)이 향후 지수 수익률에 대해
기존 축이 못 주는 판별력을 추가하는지 검증한다.

  · 지수는 대형주 몇 개로 버틸 수 있지만 breadth는 그걸 못 숨긴다.
  · 우리 시스템은 4슬롯으로 개별주를 고르므로, 지수보다 breadth가 실제
    진입 환경에 더 가깝다는 것이 가설이다.

[데이터 한계 — 결론 해석 시 반드시 감안]
  1) FinanceDataReader 개별종목은 3000행 상한이라 관측 구간이 2014년~ 로 짧다.
  2) 종목 리스트는 '현재 상장사'라 상장폐지 생존편향이 있다. 폭락기에 사라진
     종목이 빠져 breadth가 실제보다 좋게 나온다 → breadth의 판별력을 **과소**
     추정한다(보수적). 그래도 유의미하면 실제로는 더 강하다는 뜻이다.

사용: python tools/audit_breadth_axis.py [--n-stocks 200] [--ma 200]
"""
import argparse
import os
import sys

import numpy as np
import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import config  # noqa: E402
from tools.audit_market_axes import (  # noqa: E402
    FWD_DAYS, filter_blocked, forward_return, regime_scale, regime_series,
)


def build_breadth(n_stocks, ma, start):
    """상위 시총 n개 종목의 '200일선 위 비율'(%) 일별 시계열."""
    import FinanceDataReader as fdr

    lst = fdr.StockListing('KOSPI')
    lst = lst[~lst['Name'].str.contains('우$|우B$|스팩', regex=True, na=False)]
    codes = lst.nlargest(n_stocks, 'Marcap')['Code'].tolist()

    cols, ok = {}, 0
    for i, code in enumerate(codes, 1):
        try:
            df = fdr.DataReader(code, start)
            s = pd.to_numeric(df['Close'], errors='coerce').dropna()
            if len(s) > ma:
                cols[code] = s
                ok += 1
        except Exception:
            pass
        if i % 50 == 0:
            print(f"    ... {i}/{len(codes)} 조회 (유효 {ok})", flush=True)

    px = pd.DataFrame(cols).sort_index()
    above = px > px.rolling(ma).mean()
    valid = px.rolling(ma).mean().notna() & px.notna()
    cnt = valid.sum(axis=1)
    breadth = (above & valid).sum(axis=1) / cnt.replace(0, np.nan) * 100
    breadth = breadth.where(cnt >= 30)          # 표본 30종목 미만인 워밍업 구간은 버림
    print(f"  종목 {ok}개 / breadth 유효 {int(breadth.notna().sum())}일")
    return breadth.dropna()


def _stats(fwd):
    fwd = fwd[~np.isnan(fwd)]
    if len(fwd) == 0:
        return dict(n=0, mean=np.nan, med=np.nan, p10=np.nan, win=np.nan)
    return dict(n=len(fwd), mean=fwd.mean(), med=np.median(fwd),
                p10=np.percentile(fwd, 10), win=(fwd > 0).mean() * 100)


def _row(label, fwd):
    st = _stats(fwd)
    if not st['n']:
        return f"  {label:<26} {'-':>8}"
    return (f"  {label:<26} {st['n']:>5}일  평균 {st['mean']:>+6.2f}%  "
            f"중앙 {st['med']:>+6.2f}%  하위10% {st['p10']:>+7.2f}%  승률 {st['win']:>5.1f}%")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--n-stocks', type=int, default=200)
    ap.add_argument('--ma', type=int, default=200)
    ap.add_argument('--start', default='2014-01-01')
    args = ap.parse_args()

    import FinanceDataReader as fdr

    print(f"시장 폭 산출: KOSPI 시총 상위 {args.n_stocks}종목의 {args.ma}일선 상회 비율")
    breadth = build_breadth(args.n_stocks, args.ma, args.start)

    idx = fdr.DataReader('KS11', args.start)
    close_s = pd.to_numeric(idx['Close'], errors='coerce').dropna()

    # 지수 축(국면·게이트)은 전체 이력으로 계산한 뒤 breadth 구간으로 맞춘다
    dates = close_s.index
    close = close_s.values.astype(float)
    regimes, _ = regime_series(dates, close)
    rs = regime_scale(regimes)
    gate = filter_blocked(close)
    fwd = forward_return(close)

    df = pd.DataFrame({'close': close, 'rneg': rs < 1.0, 'gate': gate, 'fwd': fwd},
                      index=dates).join(breadth.rename('breadth'), how='inner').dropna(subset=['breadth'])
    print(f"  분석 구간 {df.index[0].date()} ~ {df.index[-1].date()} ({len(df)}일)\n")

    b = df['breadth'].values
    fw = df['fwd'].values
    gt = df['gate'].values.astype(bool)
    rn = df['rneg'].values.astype(bool)

    print(f"[1] breadth 5분위별 향후 {FWD_DAYS}거래일 KOSPI 수익률")
    print(_row("(전체)", fw))
    qs = np.nanpercentile(b, [20, 40, 60, 80])
    bins = [(-np.inf, qs[0]), (qs[0], qs[1]), (qs[1], qs[2]), (qs[2], qs[3]), (qs[3], np.inf)]
    for i, (lo, hi) in enumerate(bins, 1):
        m = (b > lo) & (b <= hi)
        print(_row(f"Q{i} breadth {max(lo, 0):.0f}~{min(hi, 100):.0f}%", fw[m]))

    print("\n[2] 기존 축과의 중복 — breadth 평균(%)을 기존 축 상태별로")
    for label, m in [("둘 다 발동(위험 구간)", gt & rn), ("게이트만", gt & ~rn),
                     ("국면만", ~gt & rn), ("둘 다 미발동", ~gt & ~rn)]:
        if m.sum():
            print(f"  {label:<26} breadth 평균 {b[m].mean():>5.1f}%  (n={m.sum()})")
    print(f"  상관계수 corr(breadth, 지수-SMA80 이격) = "
          f"{np.corrcoef(b, df['close'].values / pd.Series(close, index=dates).rolling(80).mean().reindex(df.index).values)[0, 1]:.3f}")

    print("\n[3] 핵심 검정 — 기존 축이 같은 판정을 내린 날 안에서 breadth가 더 갈라내는가")
    for label, m in [("둘 다 발동(위험 구간)", gt & rn), ("게이트만", gt & ~rn),
                     ("둘 다 미발동", ~gt & ~rn)]:
        if m.sum() < 100:
            continue
        med = np.median(b[m])
        print(f"  ── {label} (n={m.sum()}, breadth 중앙값 {med:.1f}%)")
        print(_row("     breadth 하위 절반", fw[m & (b <= med)]))
        print(_row("     breadth 상위 절반", fw[m & (b > med)]))

    print("\n[4] breadth 급락(20일 변화) 신호")
    d20 = pd.Series(b, index=df.index).diff(20).values
    for label, m in [("breadth 20일 -20%p 이하", d20 <= -20),
                     ("breadth 20일 -10%p 이하", d20 <= -10),
                     ("그 외", d20 > -10)]:
        m = np.nan_to_num(m, nan=False).astype(bool)
        if m.sum():
            print(_row(label, fw[m]))


if __name__ == '__main__':
    main()
