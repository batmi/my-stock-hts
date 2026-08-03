"""[검증 전용] 시장 판단 4축의 중복·과잉 감속 감사.

현재 시스템은 신규 진입 리스크를 네 갈래로 줄인다.
  ① 국면 4상태 (EMA9/41 + 5% 확인)        → PENDING_DOWN_RISK_SCALE
  ② 휩소율   (같은 EMA 교차 이력 파생)     → WHIPSAW_MIN_SCALE 선형 보간
  ③ 시장필터 (SMA80 ± 1% 히스테리시스)     → 신규 매수 전면 차단(이진 게이트)
  ④ 계좌 드로다운 (자산곡선 HWM 대비)      → DD_SCALE_1 / DD_SCALE_2
①~③은 모두 '지수 종가' 하나의 함수다. 배수는 곱으로 결합되므로(trader._update_risk_scale)
같은 정보가 여러 번 과세될 수 있다. 이 스크립트는 그 중복을 실측한다.

판정은 복제하지 않고 운영 코드를 그대로 호출한다.
  · 국면/휩소율 : analysis.classify_regime_from_df  (실매매와 같은 260봉 롤링 윈도로 호출)
  · 시장필터    : indicators.get_market_filter_blocked
설정값도 config에서 읽으므로, config를 바꾸고 다시 돌리면 그 설정의 감사 결과가 나온다.

[④의 취급] 계좌 드로다운은 자산곡선이 있어야 정확하다. 15년 포트폴리오 시뮬레이션은
종목 일봉 전량이 필요해 이 스크립트 범위 밖이므로, **지수 드로다운을 프록시로** 쓴다.
4슬롯 롱온리 주식 포트폴리오의 DD는 지수 DD보다 크므로 이 프록시는 ④의 발동을
'과소' 추정한다 → 중복 결론은 보수적(실제 중복은 여기 수치보다 심하다).
--dd-beta 로 배율을 줘서 민감도를 확인할 수 있다.

사용:
  python tools/audit_market_axes.py                 # 전 구간, 기본 설정
  python tools/audit_market_axes.py --start 2015-01-01 --dd-beta 1.3
"""
import argparse
import os
import sys

import numpy as np
import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import config  # noqa: E402
import indicators  # noqa: E402
from modules import analysis  # noqa: E402

FWD_DAYS = 20          # 향후 수익률 관측 구간(거래일) — 국면 검증 때와 동일 기준
LIVE_WINDOW = 260      # 실매매가 지수 차트로 들고 있는 봉 수(_toss_index_chart_data target)


# ----------------------------------------------------------------------------
# 데이터
# ----------------------------------------------------------------------------
def load_index(ticker, start):
    """FDR 지수 일봉 → (DatetimeIndex, close ndarray). KRX 정규장 확정 종가."""
    import FinanceDataReader as fdr
    df = fdr.DataReader(ticker, start)
    close = pd.to_numeric(df['Close'], errors='coerce').dropna()
    return close.index, close.values.astype(float)


# ----------------------------------------------------------------------------
# 축별 시계열 — 판정은 전부 운영 코드 호출
# ----------------------------------------------------------------------------
def regime_series(dates, close, window=LIVE_WINDOW):
    """각 시점의 국면·휩소율. 실매매와 동일하게 직전 `window`봉만 보고 판정한다."""
    params = getattr(config, 'MARKET_REGIME_PARAMS', {}) or {}
    regimes, whipsaws = [], []
    for i in range(len(close)):
        lo = max(0, i + 1 - window)
        df = pd.DataFrame({'close': close[lo:i + 1]})
        info = analysis.classify_regime_from_df(df, params)
        regimes.append(info['regime'])
        whipsaws.append(info['whipsaw_ratio'])
    return np.array(regimes), np.array([np.nan if w is None else w for w in whipsaws])


def regime_scale(regimes):
    """국면 배수 — trader._update_risk_scale 과 같은 매핑."""
    p = getattr(config, 'RISK_SCALING_PARAMS', {}) or {}
    if not p.get("USE_REGIME_RISK_SCALING", True):
        return np.ones(len(regimes))
    pend = float(p.get("PENDING_DOWN_RISK_SCALE", 0.6))
    bear = float(p.get("BEAR_RISK_SCALE", 1.0))
    out = np.ones(len(regimes))
    out[regimes == "PendDown"] = pend
    out[regimes == "Bear"] = bear
    return out


def whipsaw_scale(whipsaws):
    """휩소율 배수 — LO 이하 1.0, HI 이상 MIN, 사이 선형 보간(산출 불가 시 1.0)."""
    p = getattr(config, 'RISK_SCALING_PARAMS', {}) or {}
    if not p.get("USE_WHIPSAW_RISK_SCALING", True):
        return np.ones(len(whipsaws))
    lo = float(p.get("WHIPSAW_LO", 0.40))
    hi = float(p.get("WHIPSAW_HI", 0.75))
    mn = float(p.get("WHIPSAW_MIN_SCALE", 0.85))
    w = np.where(np.isnan(whipsaws), lo, whipsaws)
    t = np.clip((w - lo) / max(1e-9, hi - lo), 0.0, 1.0)
    return 1.0 - t * (1.0 - mn)


def drawdown_scale(close, beta=1.0):
    """계좌 드로다운 배수의 프록시 — 지수의 트레일링 HWM 대비 낙폭에 beta를 곱해 적용."""
    p = getattr(config, 'RISK_SCALING_PARAMS', {}) or {}
    if not p.get("USE_DRAWDOWN_RISK_SCALING", True):
        return np.ones(len(close)), np.zeros(len(close))
    look = int(p.get("DD_LOOKBACK_DAYS", 90))
    l1, s1 = float(p.get("DD_LEVEL_1", 5.0)), float(p.get("DD_SCALE_1", 0.75))
    l2, s2 = float(p.get("DD_LEVEL_2", 10.0)), float(p.get("DD_SCALE_2", 0.5))

    s = pd.Series(close)
    hwm = s.rolling(window=look, min_periods=1).max().values
    dd = (close / hwm - 1.0) * 100.0 * beta   # 음수 %
    out = np.ones(len(close))
    out[dd <= -l1] = s1
    out[dd <= -l2] = s2
    return out, dd


def filter_blocked(close):
    """시장필터 게이트 — 운영 함수 그대로."""
    return indicators.get_market_filter_blocked(close).values.astype(bool)


def forward_return(close, days=FWD_DAYS):
    out = np.full(len(close), np.nan)
    if len(close) > days:
        out[:-days] = (close[days:] / close[:-days] - 1.0) * 100.0
    return out


# ----------------------------------------------------------------------------
# 출력 도우미
# ----------------------------------------------------------------------------
def _stats(fwd):
    fwd = fwd[~np.isnan(fwd)]
    if len(fwd) == 0:
        return dict(n=0, mean=np.nan, med=np.nan, p10=np.nan, win=np.nan)
    return dict(n=len(fwd), mean=fwd.mean(), med=np.median(fwd),
                p10=np.percentile(fwd, 10), win=(fwd > 0).mean() * 100)


def _row(label, fwd, total):
    st = _stats(fwd)
    share = st['n'] / total * 100 if total else 0
    return (f"  {label:<26} {st['n']:>6}일 ({share:>5.1f}%)  "
            f"평균 {st['mean']:>+6.2f}%  중앙 {st['med']:>+6.2f}%  "
            f"하위10% {st['p10']:>+7.2f}%  승률 {st['win']:>5.1f}%")


def audit(name, ticker, start, dd_beta):
    dates, close = load_index(ticker, start)
    print(f"\n{'=' * 100}")
    print(f"[{name}]  {dates[0].date()} ~ {dates[-1].date()}  ({len(close)}거래일)")
    print('=' * 100)

    regimes, whips = regime_series(dates, close)
    rs = regime_scale(regimes)
    ws = whipsaw_scale(whips)
    ds, dd = drawdown_scale(close, beta=dd_beta)
    gate = filter_blocked(close)
    combined = rs * ws * ds
    fwd = forward_return(close)
    n = len(close)
    base = _stats(fwd)

    # ── A. 축별 발동 빈도 ────────────────────────────────────────────────
    print("\n[A] 축별 발동 빈도 (배수<1.0 또는 게이트 차단)")
    axes = {
        "① 국면(PendDown/Bear)": rs < 1.0,
        "② 휩소율": ws < 1.0,
        "③ 시장필터 게이트": gate,
        "④ 드로다운(프록시)": ds < 1.0,
    }
    for label, mask in axes.items():
        print(f"  {label:<26} {mask.sum():>6}일 ({mask.mean() * 100:>5.1f}%)")

    # ── B. 동시 발동(중복) ──────────────────────────────────────────────
    fired = np.vstack([m for m in axes.values()]).sum(axis=0)
    print(f"\n[B] 동시 발동 축 수  (기준일 {n}일)")
    for k in range(5):
        m = fired == k
        print(f"  {k}축 동시 {m.sum():>6}일 ({m.mean() * 100:>5.1f}%)"
              + ("" if k == 0 else f"   ← 감속 중복"))

    # ── C. 축 간 조건부 확률 (같은 정보인가) ────────────────────────────
    print("\n[C] 축 간 조건부 확률  P(열=발동 | 행=발동)  — 높을수록 같은 정보")
    keys = list(axes.keys())
    print(f"  {'':<26}" + "".join(f"{k.split('(')[0][:12]:>14}" for k in keys))
    for a in keys:
        ma = axes[a]
        cells = []
        for b in keys:
            mb = axes[b]
            cells.append(f"{(mb[ma].mean() * 100 if ma.sum() else 0):>13.1f}%")
        print(f"  {a:<26}" + "".join(cells))

    # ── D. 결합 배수 구간별 향후 20일 지수 수익률 ───────────────────────
    print(f"\n[D] 결합 배수(①×②×④) 구간별 향후 {FWD_DAYS}거래일 지수 수익률")
    print(f"  {'(전체 평균)':<26} {base['n']:>6}일 (100.0%)  "
          f"평균 {base['mean']:>+6.2f}%  중앙 {base['med']:>+6.2f}%  "
          f"하위10% {base['p10']:>+7.2f}%  승률 {base['win']:>5.1f}%")
    edges = [(0.0, 0.45), (0.45, 0.6), (0.6, 0.8), (0.8, 0.99), (0.99, 1.01)]
    for lo, hi in edges:
        m = (combined > lo) & (combined <= hi)
        if m.sum():
            print(_row(f"배수 {lo:.2f}~{hi:.2f}", fwd[m], n))
    print(f"\n  평균 결합 배수 {combined.mean():.3f}   "
          f"배수<1.0인 날 {(combined < 1.0).mean() * 100:.1f}%   "
          f"배수<=0.6인 날 {(combined <= 0.6).mean() * 100:.1f}%")

    # ── E. 게이트까지 포함한 실효 노출 ──────────────────────────────────
    print("\n[E] 게이트 포함 실효 신규진입 노출 (게이트 차단 = 노출 0)")
    eff = np.where(gate, 0.0, combined)
    print(f"  실효 노출 평균 {eff.mean():.3f}  "
          f"(게이트 차단 {gate.mean() * 100:.1f}% + 배수 축소)")
    for label, m in [("게이트 차단일", gate), ("게이트 통과일", ~gate)]:
        print(_row(label, fwd[m], n))

    # ── F. 2순위: 게이트 vs 국면 잉여성 ─────────────────────────────────
    #  주의: 국면을 '리스크 배수가 깎이는가'(=PendDown)로 이진화하면 Bear가 '국면 정상' 쪽에
    #  섞여 들어가 결론이 뒤집혀 보인다. 반드시 4상태 그대로 분해해서 봐야 한다.
    print("\n[F] 2순위 — 시장필터 게이트 차단일을 국면 4상태로 분해")
    rneg = rs < 1.0
    print(f"  게이트 vs 국면(PendDown) 판정 일치율 {(gate == rneg).mean() * 100:.1f}%")
    for r in ("Bull", "PendUp", "PendDown", "Bear", "Sideways"):
        m = gate & (regimes == r)
        if m.sum():
            print(_row(f"게이트 ∧ {r}", fwd[m], n))
    print(_row("(게이트 통과일)", fwd[~gate], n))

    # ── G. [F]의 하위기간 견고성 ────────────────────────────────────────
    print("\n[G] 하위기간 견고성 — [F]의 주요 구간을 기간별로 분해 (평균 향후 20일 %)")
    cuts = [(dates[0], pd.Timestamp('2015-12-31')),
            (pd.Timestamp('2016-01-01'), pd.Timestamp('2020-12-31')),
            (pd.Timestamp('2021-01-01'), dates[-1])]
    combos = [("∧Bear", gate & (regimes == "Bear")),
              ("∧PendDown", gate & (regimes == "PendDown")),
              ("∧PendUp", gate & (regimes == "PendUp")),
              ("게이트 통과", ~gate)]
    print(f"  {'기간':<22}" + "".join(f"{c[0]:>14}" for c in combos))
    for lo, hi in cuts:
        sel = (dates >= lo) & (dates <= hi)
        cells = []
        for _, m in combos:
            st = _stats(fwd[sel & m])
            cells.append(f"{st['mean']:>+9.2f}%({st['n']:>3})" if st['n'] else f"{'-':>14}")
        print(f"  {str(lo.date()) + '~' + str(hi.date()):<22}" + "".join(cells))

    return dict(name=name, dates=dates, close=close, regimes=regimes,
                rs=rs, ws=ws, ds=ds, gate=gate, combined=combined, fwd=fwd, dd=dd)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--start', default='2010-01-01')
    ap.add_argument('--dd-beta', type=float, default=1.0,
                    help='지수 DD → 계좌 DD 환산 배율(프록시 민감도)')
    args = ap.parse_args()

    print(f"설정: 국면 EMA{config.MARKET_REGIME_PARAMS['REGIME_EMA_FAST']}/"
          f"{config.MARKET_REGIME_PARAMS['REGIME_EMA_SLOW']} "
          f"+{config.MARKET_REGIME_PARAMS['REGIME_CONFIRM_PCT']:g}% | "
          f"필터 SMA{getattr(config, 'MARKET_FILTER_MA', 80)}"
          f"±{getattr(config, 'MARKET_FILTER_BAND', 1.0):g}% | "
          f"PendDown×{config.RISK_SCALING_PARAMS['PENDING_DOWN_RISK_SCALE']} "
          f"휩소min×{config.RISK_SCALING_PARAMS['WHIPSAW_MIN_SCALE']} "
          f"DD×{config.RISK_SCALING_PARAMS['DD_SCALE_1']}/"
          f"{config.RISK_SCALING_PARAMS['DD_SCALE_2']} (beta={args.dd_beta})")

    for name, ticker in [("KOSPI", "KS11"), ("KOSDAQ", "KQ11")]:
        audit(name, ticker, args.start, args.dd_beta)


if __name__ == '__main__':
    main()
