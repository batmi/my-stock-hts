"""[검증 전용] 백테스트와 실매매가 같은 날 같은 종목에 같은 판정을 내리는가.

모든 전략 파라미터가 백테스트로 결정됐으므로, 두 경로의 판정이 어긋나면 그 검증들이
전부 무효가 된다. 그런데 두 경로는 **같은 채점 함수를 서로 다른 방식으로 호출**한다.

  · 백테스트: backtest.compute_price_indicators 로 지표를 전 구간 일괄 계산한 뒤,
              row 딕셔너리에서 스칼라를 꺼내 backtest.calculate_daily_status 에 넘긴다.
  · 실매매  : indicators.calculate_indicators(df) 로 마지막 시점 지표만 구해
              analysis.classify_stock_state / calculate_score 에 df=·ind= 로 넘긴다.

조립 계층이 두 벌이라, 필드 하나가 빠지거나 NaN 처리가 달라도 조용히 다른 점수가 나온다.
이 스크립트는 같은 일봉을 두 경로에 넣고 점수·상태를 전수 대조한다.

[두 가지 대조를 구분해서 본다]
  A vs B-full : 실매매가 백테스트와 같은 길이의 이력을 봤을 때의 차이
                → 순수한 '조립 방식/로직' 차이. 0이어야 정상이다.
  A vs B-live : 실매매가 실제로 들고 있는 봉 수(기본 260)만 봤을 때의 차이
                → EMA120·OBV 등 장기 지표의 워밍업 부족까지 포함한 실제 괴리.
                  0이 아닌 것이 자연스럽지만, 크기를 알고 있어야 한다.

데이터는 FinanceDataReader 일봉을 쓴다(KIS API 불필요). smart_money 는 양쪽 모두 False로
고정해 네트워크 의존 신호를 제거하고 채점 로직만 비교한다.

사용:
  python tools/audit_live_backtest_parity.py                      # 기본 15종목
  python tools/audit_live_backtest_parity.py --stocks 30 --step 3
"""
import argparse
import os
import sys
import warnings

import numpy as np
import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import config  # noqa: E402
from core import indicators  # noqa: E402
from modules import analysis, backtest  # noqa: E402

warnings.filterwarnings("ignore")

LIVE_WINDOW = 260   # 실매매가 지표 계산에 들고 있는 일봉 수
WARMUP = 260        # 대조 시작 인덱스(EMA120·52주 위치가 자리잡는 구간 이후만 비교)


def load_daily(code, lookback_days=4000):
    """일봉 — 앱·백테스트와 같은 정본 경로(krx_daily.get_daily: Open API → FDR).

    [왜 · 2026-09-20] 종전엔 FDR 을 직접 읽어 앱이 버리는 0원 거래정지 봉이 그대로 들어왔고,
    그 봉이 조립 차이처럼 보였다(20190430 삼성전자). 정본 경로를 타면 데이터 정제까지 같다.
    """
    from modules import krx_daily
    df = krx_daily.get_daily(code, lookback_days=lookback_days)
    if df is None or df.empty:
        return None
    df = df[['date', 'open', 'high', 'low', 'close', 'volume']].copy()
    for c in ('open', 'high', 'low', 'close', 'volume'):
        df[c] = pd.to_numeric(df[c], errors='coerce')
    df = df.dropna().reset_index(drop=True)
    return df if len(df) > WARMUP + 50 else None


def judge_backtest(df):
    """백테스트 경로 — 전 구간 지표 일괄 계산 후 row 단위 판정."""
    d = backtest.compute_price_indicators(df.copy())
    d['smart_money'] = False
    rows = d.to_dict('records')
    out, prev = [], None
    for row in rows:
        try:
            raw_score, _, can_buy, state, _ = backtest.calculate_daily_status(row, prev)
        except Exception as e:
            raw_score, can_buy, state = np.nan, None, f"ERR:{type(e).__name__}"
        out.append((raw_score, can_buy, state))
        prev = row
    return out


def judge_live(df, t, window=None):
    """실매매 경로 — t시점까지의 df만 주고 engine.DefaultStrategy.analyze_buy 와 같은 호출을 한다."""
    lo = 0 if window is None else max(0, t + 1 - window)
    sub = df.iloc[lo:t + 1].reset_index(drop=True)
    ind = indicators.calculate_indicators(sub)
    prev_rsi = ind.get('prev_rsi') if len(sub) >= 16 else None

    # engine.analyze_buy 와 동일한 52주 위치 산식 — indicators.w52_position(365 달력일 창).
    #  [Fix 2026-09-17] 종전엔 tail(250) 사본이었다. 실매매는 2026-07-24, 백테스트는
    #  2026-09-04(4b571c1)에 365일 창으로 옮겼는데 이 계측기만 옛 정의로 남아 '조립 방식
    #  차이 0.19%'(전부 가격 모멘텀 항목 ±0.5)를 **엔진 결함처럼** 보고했다. 실매매는
    #  now=datetime.now() 로 부르므로 여기서는 그 봉의 날짜를 '오늘'로 준다.
    from datetime import datetime as _dt
    price = float(sub.iloc[-1]['close'])
    bar_day = _dt.strptime(str(sub.iloc[-1]['date'])[:8], '%Y%m%d')
    w52_pos = indicators.w52_position(sub, price, now=bar_day)

    state, _, _ = analysis.classify_stock_state(
        df=sub, ind=ind, prev_rsi=prev_rsi, thresholds=None,
        w52_pos=w52_pos, smart_money=False)
    # engine.analyze_buy와 동일하게 w52_pos를 함께 넘긴다 — 넘기지 않으면 calculate_score가
    #  _w52_band(365 달력일) 폴백을 써서 상태 분류와 다른 52주 위치로 채점된다.
    score, _ = analysis.calculate_score(df=sub, ind=ind, weights=None, smart_money=False,
                                        w52_pos=w52_pos)
    return round(float(score), 1), state


def compare(name, code, df, step):
    bt = judge_backtest(df)
    idx = list(range(WARMUP, len(df), step))

    rec = []
    for t in idx:
        b_score, _, b_state = bt[t]
        try:
            f_score, f_state = judge_live(df, t, window=None)
        except Exception as e:
            f_score, f_state = np.nan, f"ERR:{type(e).__name__}"
        try:
            l_score, l_state = judge_live(df, t, window=LIVE_WINDOW)
        except Exception as e:
            l_score, l_state = np.nan, f"ERR:{type(e).__name__}"
        rec.append(dict(date=df.iloc[t]['date'], bt_score=b_score, bt_state=b_state,
                        full_score=f_score, full_state=f_state,
                        live_score=l_score, live_state=l_state))
    return pd.DataFrame(rec)


def summarize(tag, r, col_s, col_st):
    d = (r['bt_score'] - r[col_s]).abs()
    d = d[~d.isna()]
    st_mismatch = (r['bt_state'] != r[col_st]).mean() * 100
    return dict(tag=tag, n=len(r),
                score_mismatch=(d > 0.05).mean() * 100 if len(d) else np.nan,
                score_mae=d.mean() if len(d) else np.nan,
                score_max=d.max() if len(d) else np.nan,
                state_mismatch=st_mismatch)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--stocks', type=int, default=15)
    ap.add_argument('--step', type=int, default=5, help='몇 거래일마다 대조할지')
    args = ap.parse_args()

    from tools.audit_common import listing
    lst = listing('KOSPI')
    lst = lst[~lst['Name'].str.contains('우$|우B$|스팩', regex=True, na=False)]
    targets = lst.nlargest(args.stocks, 'Marcap')[['Code', 'Name']].values.tolist()

    print(f"백테스트 ↔ 실매매 판정 대조 — {len(targets)}종목, {args.step}거래일 간격")
    print(f"  A = backtest.calculate_daily_status (전 구간 일괄 지표)")
    print(f"  B-full = analysis.classify_stock_state/calculate_score (같은 길이 이력)")
    print(f"  B-live = 같은 함수, 직전 {LIVE_WINDOW}봉만 (실매매 조건)\n")

    all_rows, summaries = [], []
    for code, name in targets:
        df = load_daily(code)
        if df is None:
            print(f"  {name:<12} 데이터 부족 — 건너뜀")
            continue
        r = compare(name, code, df, args.step)
        r['code'], r['name'] = code, name
        all_rows.append(r)
        s_full = summarize('full', r, 'full_score', 'full_state')
        s_live = summarize('live', r, 'live_score', 'live_state')
        summaries.append((name, s_full, s_live))
        print(f"  {name:<12} n={s_full['n']:>4}  "
              f"[A vs B-full] 점수불일치 {s_full['score_mismatch']:>5.1f}% "
              f"MAE {s_full['score_mae']:.3f} 최대 {s_full['score_max']:.1f} "
              f"상태불일치 {s_full['state_mismatch']:>5.1f}%   "
              f"[A vs B-live] 점수불일치 {s_live['score_mismatch']:>5.1f}% "
              f"상태불일치 {s_live['state_mismatch']:>5.1f}%")

    if not all_rows:
        print("대조 가능한 종목이 없습니다.")
        return

    R = pd.concat(all_rows, ignore_index=True)
    print(f"\n{'=' * 96}\n[종합] 전체 {len(R)}건\n{'=' * 96}")
    for tag, cs, ct in [("A vs B-full (조립 방식 차이만)", 'full_score', 'full_state'),
                        ("A vs B-live (실매매 봉 수 반영)", 'live_score', 'live_state')]:
        s = summarize(tag, R, cs, ct)
        print(f"  {tag}")
        print(f"    점수 불일치(>0.05) {s['score_mismatch']:.2f}%   "
              f"평균오차 {s['score_mae']:.4f}   최대오차 {s['score_max']:.2f}")
        print(f"    상태 불일치        {s['state_mismatch']:.2f}%")

    # 매수 가능 상태의 불일치는 실제 진입/미진입을 가르므로 따로 본다
    print("\n[핵심] '매수 후보 상태' 판정이 갈리는 비율")
    for tag, ct in [("B-full", 'full_state'), ("B-live", 'live_state')]:
        buyish = {"매수", "역매수", "강력매수"}
        a = R['bt_state'].isin(buyish)
        b = R[ct].isin(buyish)
        print(f"  {tag}: 백테스트만 매수후보 {(a & ~b).mean() * 100:.2f}%  "
              f"실매매만 매수후보 {(~a & b).mean() * 100:.2f}%  "
              f"일치 {(a == b).mean() * 100:.2f}%")

    mis = R[(R['bt_score'] - R['full_score']).abs() > 0.05]
    if len(mis):
        print(f"\n[A vs B-full 불일치 사례 상위 10건]")
        m = mis.assign(diff=(mis['bt_score'] - mis['full_score']).abs()).nlargest(10, 'diff')
        for _, x in m.iterrows():
            print(f"  {x['name']:<10} {x['date']}  백테스트 {x['bt_score']:>5.1f}/{x['bt_state']:<6} "
                  f"↔ 실매매 {x['full_score']:>5.1f}/{x['full_state']:<6}  차 {x['diff']:.2f}")

    out = os.path.join(os.path.dirname(os.path.abspath(__file__)), '..',
                       'logs', 'parity_audit.csv')
    try:
        R.to_csv(out, index=False, encoding='utf-8-sig')
        print(f"\n  전체 결과 저장: {os.path.normpath(out)}")
    except Exception:
        pass


if __name__ == '__main__':
    main()
