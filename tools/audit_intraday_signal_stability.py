"""[검증 전용] 장중 판정이 흔들리는가 — 백테스트가 재현하지 못하는 자유도의 크기.

백테스트는 '확정 종가로 판정하고 종가에 체결'한다. 실매매는 주기마다 **미확정 장중가**로
다시 판정하므로, 같은 날 안에서 매수 신호가 켜졌다 꺼지면 실제 체결은 "그날 몇 시에
스캔했는가"에 좌우된다. 백테스트에 존재하지 않는 자유도이고, 백테스트 성과가 실매매에서
재현되지 않는 흔한 원인이다. 이 스크립트는 그 크기를 실측한다.

[당일 봉 재현] 실매매는 과거 일봉에 '진행 중인 당일 봉'을 얹어 지표를 계산한다
(api._get_cached_chart 의 현재가 오버레이: 시가=당일 시가, 고/저=현재까지의 고저,
 종가=현재가, 거래량=누적). 여기서는 30분봉을 누적해 같은 모양의 당일 봉을 만든다.

  일봉(전일까지, FDR) + [시가, 누적고가, 누적저가, 현재가, 누적거래량] → 실매매와 같은 채점

[데이터] 30분봉은 yfinance(최근 60일)만 제공한다. 관측 구간이 짧으므로 결과는 '경향'으로
읽어야 한다. 일봉은 FDR(KRX 정규장)이고 분봉은 yfinance라 소수 종목에서 미세한 기준 차이가
있을 수 있어, 종가 시점 판정이 일봉 기준 판정과 같은지 자체 검증(sanity)도 함께 출력한다.

사용: python tools/audit_intraday_signal_stability.py [--stocks 8] [--interval 30m]
"""
import argparse
import os
import sys
import warnings

import numpy as np
import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import config  # noqa: E402
import indicators  # noqa: E402
from modules import analysis  # noqa: E402
from tools.audit_live_backtest_parity import judge_live, load_daily  # noqa: E402

warnings.filterwarnings("ignore")

BUYISH = {"매수", "역매수", "강력매수"}
HIST_BARS = 400   # 당일 봉 앞에 붙일 과거 일봉 수(EMA120·52주 위치 워밍업 확보)


def load_intraday(code, interval):
    """yfinance 분봉 → {YYYYMMDD: DataFrame(open/high/low/close/volume)} (KST 기준)."""
    import yfinance as yf
    d = yf.download(f"{code}.KS", period="60d", interval=interval,
                    progress=False, auto_adjust=False)
    if d is None or d.empty:
        return {}
    if isinstance(d.columns, pd.MultiIndex):
        d.columns = d.columns.get_level_values(0)
    d = d.rename(columns=str.lower)[['open', 'high', 'low', 'close', 'volume']].dropna()
    try:
        d.index = d.index.tz_convert('Asia/Seoul')
    except Exception:
        pass
    out = {}
    for day, g in d.groupby(d.index.strftime('%Y%m%d')):
        if len(g) >= 4:
            out[day] = g.sort_index()
    return out


def day_trajectory(hist, bars, day, eod_bar=None):
    """하루치 판정 궤적 — 각 분봉 시점의 (시각, 점수, 상태).

    마지막 점은 분봉이 아니라 **확정 일봉**(eod_bar)으로 채운다. yfinance 30분봉은 KRX
    종가 단일가(15:20~15:30)를 담지 않아, 마지막 분봉 종가가 일봉 종가와 중앙 0.63%
    어긋나고 거래량도 81% 수준이다(고가·저가는 오차 0%). 마지막 점을 일봉으로 두면
    '종가 시점 판정 = 백테스트가 보는 판정'이 구성상 정확히 같아져, 장중 대비 종가 비교가
    데이터 출처 차이에 오염되지 않는다.
    """
    o = float(bars.iloc[0]['open'])
    hi = lo = None
    vol = 0.0
    traj = []
    for ts, b in bars.iterrows():
        hi = float(b['high']) if hi is None else max(hi, float(b['high']))
        lo = float(b['low']) if lo is None else min(lo, float(b['low']))
        vol += float(b['volume'])
        today = {'date': day, 'open': o, 'high': hi, 'low': lo,
                 'close': float(b['close']), 'volume': vol}
        df = pd.concat([hist, pd.DataFrame([today])], ignore_index=True)
        try:
            score, state = judge_live(df, len(df) - 1, window=None)
        except Exception:
            continue
        traj.append((ts.strftime('%H:%M'), score, state))

    if eod_bar is not None:
        df = pd.concat([hist, pd.DataFrame([eod_bar])], ignore_index=True)
        try:
            score, state = judge_live(df, len(df) - 1, window=None)
            traj.append(('종가', score, state))
        except Exception:
            pass
    return traj


def analyze(code, name, interval):
    daily = load_daily(code)
    intra = load_intraday(code, interval)
    if daily is None or not intra:
        return None

    rows = []
    for day in sorted(intra):
        hist = daily[daily['date'] < day].tail(HIST_BARS).reset_index(drop=True)
        if len(hist) < 260:
            continue
        drow = daily[daily['date'] == day]
        if not len(drow):
            continue
        traj = day_trajectory(hist, intra[day], day, eod_bar=drow.iloc[0].to_dict())
        if len(traj) < 5 or traj[-1][0] != '종가':
            continue

        # 장중(=스캔 시점에 따라 달라지는 구간)과 종가(=백테스트가 보는 시점)를 분리한다
        intraday = traj[:-1]
        eod_score, eod_state = traj[-1][1], traj[-1][2]
        scores = np.array([s for _, s, _ in intraday])
        buy = np.array([st in BUYISH for _, _, st in intraday])

        rows.append(dict(
            code=code, name=name, day=day, n=len(intraday),
            score_range=float(scores.max() - scores.min()),
            score_std=float(scores.std()),
            intraday_eod_gap=float(abs(scores[-1] - eod_score)),
            flips=int((buy[1:] != buy[:-1]).sum()),
            any_buy=bool(buy.any()), last_buy=bool(buy[-1]),
            eod_buy=bool(eod_state in BUYISH), eod_score=float(eod_score),
            first_half_buy=bool(buy[:max(1, len(buy) // 2)].any()),
        ))
    return pd.DataFrame(rows) if rows else None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--stocks', type=int, default=8)
    ap.add_argument('--interval', default='30m')
    args = ap.parse_args()

    import FinanceDataReader as fdr
    lst = fdr.StockListing('KOSPI')
    lst = lst[~lst['Name'].str.contains('우$|우B$|스팩', regex=True, na=False)]
    targets = lst.nlargest(args.stocks, 'Marcap')[['Code', 'Name']].values.tolist()

    print(f"장중 판정 안정성 — {len(targets)}종목, {args.interval}봉, 최근 60일")
    print("  실매매와 같은 방식으로 '진행 중인 당일 봉'을 만들어 매 시점 채점한다.\n")

    parts = []
    for code, name in targets:
        r = analyze(code, name, args.interval)
        if r is None or r.empty:
            print(f"  {name:<12} 데이터 부족 — 건너뜀")
            continue
        parts.append(r)
        print(f"  {name:<12} {len(r):>3}일  일중점수폭 평균 {r['score_range'].mean():.2f} "
              f"최대 {r['score_range'].max():.1f}  신호전환 {r['flips'].sum()}회  "
              f"장중매수후보 {r['any_buy'].sum()}일 / 종가기준 {int(r['eod_buy'].fillna(False).sum())}일")

    if not parts:
        print("분석 가능한 종목이 없습니다.")
        return
    R = pd.concat(parts, ignore_index=True)
    ok = R[R['eod_buy'].notna()]

    print(f"\n{'=' * 92}\n[종합] 종목-일 {len(R)}건\n{'=' * 92}")
    print(f"  일중 점수 변동폭   평균 {R['score_range'].mean():.2f}점  "
          f"중앙 {R['score_range'].median():.2f}점  "
          f"1점 이상 {(R['score_range'] >= 1.0).mean() * 100:.1f}%  "
          f"최대 {R['score_range'].max():.1f}점")
    print(f"  매수후보 신호 전환 하루 평균 {R['flips'].mean():.2f}회  "
          f"1회 이상 전환한 날 {(R['flips'] > 0).mean() * 100:.1f}%")

    print(f"\n[핵심] 백테스트(종가 기준)가 보지 못하는 진입 — {len(ok)}건 기준")
    ghost = ok['any_buy'] & ~ok['eod_buy'].astype(bool)
    missed = ~ok['any_buy'] & ok['eod_buy'].astype(bool)
    both = ok['any_buy'] & ok['eod_buy'].astype(bool)
    print(f"  장중엔 매수후보였으나 종가엔 아님   {ghost.sum():>4}일 ({ghost.mean() * 100:5.2f}%)"
          f"  ← 백테스트에 없는 진입")
    print(f"  종가엔 매수후보였으나 장중엔 아님   {missed.sum():>4}일 ({missed.mean() * 100:5.2f}%)")
    print(f"  둘 다 매수후보                     {both.sum():>4}일 ({both.mean() * 100:5.2f}%)")
    if ok['any_buy'].sum():
        print(f"  → 장중 신호 중 종가까지 살아남는 비율 "
              f"{both.sum() / ok['any_buy'].sum() * 100:.1f}%")

    print("\n[참고] 스캔 시점 의존성")
    print(f"  장 전반부에 이미 매수후보였던 날 {R['first_half_buy'].sum()}일 / "
          f"장중 한 번이라도 매수후보 {R['any_buy'].sum()}일")
    print(f"  장 마감 직전 분봉과 확정 종가의 점수 차 평균 {R['intraday_eod_gap'].mean():.2f}점 "
          f"(0.05 초과 {(R['intraday_eod_gap'] > 0.05).mean() * 100:.1f}%)"
          f"  ← 종가 단일가가 판정을 바꾸는 정도")

    # 추가 진입이 '좋은 진입'인지까지 보려면 사후 수익률이 필요하지만, 30분봉이 60일치뿐이라
    #  표본이 수십 건에 그친다. 참고로만 출력하고 결론을 내지 않는다.
    print("\n[참고 — 표본 부족, 결론 금지] 진입일 종가 대비 사후 수익률")
    fwd = {}
    for code in R['code'].unique():
        d = load_daily(code)
        if d is None:
            continue
        c = d['close'].values
        for h in (5, 20):
            f = np.full(len(c), np.nan)
            f[:-h] = (c[h:] / c[:-h] - 1) * 100
            fwd[(code, h)] = dict(zip(d['date'], f))

    def _fs(sub, h):
        v = np.array([fwd.get((r.code, h), {}).get(r.day, np.nan) for r in sub.itertuples()])
        v = v[~np.isnan(v)]
        return (f"n={len(v):>3}  평균 {v.mean():+6.2f}%  승률 {(v > 0).mean() * 100:5.1f}%"
                if len(v) else "n=0")

    for h in (5, 20):
        print(f"  {h}거래일 후 | 장중만 {_fs(R[R['any_buy'] & ~R['eod_buy']], h)}"
              f" | 장중+종가 {_fs(R[R['any_buy'] & R['eod_buy']], h)}"
              f" | 대조군 {_fs(R[~R['any_buy'] & ~R['eod_buy']], h)}")
    print("  ※ 각 그룹 n이 20 내외라 5일·20일 결과가 서로 모순된다. 이 표로 판단하지 말 것.")

    out = os.path.join(os.path.dirname(os.path.abspath(__file__)), '..',
                       'logs', 'intraday_stability.csv')
    try:
        R.to_csv(out, index=False, encoding='utf-8-sig')
        print(f"\n  전체 결과 저장: {os.path.normpath(out)}")
    except Exception:
        pass


if __name__ == '__main__':
    main()
