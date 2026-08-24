"""[검증 전용] 리스크 축의 잉여성과 결합 방식(곱 vs min vs 축 제거) 비교.

tools/audit_market_axes.py 가 "네 축이 겹친다"까지 보였다면, 여기서는 그 다음 질문에 답한다.
  ① 휩소율 축은 국면·게이트 위에 정보를 더하는가, 아니면 잉여인가?
  ② 네 축을 곱으로 묶는 현행 결합이 최선인가? (min·축 제거·게이트 변형과 비교)

[평가 방법] '향후 수익률 예측력'만으로는 리스크 축의 가치를 잴 수 없다. 감속의 목적은
수익 예측이 아니라 위험 조절이기 때문이다. 그래서 각 결합 방식으로 **지수에 그 배수만큼
노출한 자산곡선**을 만들어 CAGR·MDD·Sharpe·평균 노출을 직접 비교한다.
  · 노출은 반드시 전일 종가까지의 정보로 정한 배수를 당일 수익률에 적용한다(미래 참조 금지).
  · 계좌 드로다운 축은 시뮬레이션된 자산곡선 자체의 HWM으로 계산한다(피드백 포함).
    audit_market_axes.py 의 '지수 DD 프록시'보다 이쪽이 실제 동작에 가깝다.

[한계] 대상이 지수 1개라, 4슬롯 종목 포트폴리오의 손익과는 다르다. 여기서 재는 것은
'시장 타이밍 오버레이로서의 네 축'의 순효과뿐이다. 종목 단위 확인은 포트폴리오 백테스트 몫.

사용: python tools/audit_axis_combination.py
"""
import os
import sys

import numpy as np
import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import config  # noqa: E402
from core import indicators  # noqa: E402
from tools.audit_market_axes import (  # noqa: E402
    FWD_DAYS, forward_return, load_index, regime_scale, regime_series, whipsaw_scale,
)

TRADING_DAYS = 252


# ----------------------------------------------------------------------------
# 자산곡선 시뮬레이션
# ----------------------------------------------------------------------------
def simulate(close, base_scale, use_dd=True):
    """배수 시계열대로 지수에 노출했을 때의 자산곡선.

    base_scale[t] = t시점 종가까지의 정보로 정한 노출(0~1). 이를 t+1 수익률에 적용한다.
    use_dd=True면 시뮬레이션 자산곡선의 트레일링 HWM 대비 낙폭으로 드로다운 배수를 곱한다
    (실제 운영과 같은 피드백 구조 — 줄이면 회복도 느려지는 효과까지 반영된다).
    """
    p = getattr(config, 'RISK_SCALING_PARAMS', {}) or {}
    look = int(p.get("DD_LOOKBACK_DAYS", 90))
    l1, s1 = float(p.get("DD_LEVEL_1", 5.0)), float(p.get("DD_SCALE_1", 0.75))
    l2, s2 = float(p.get("DD_LEVEL_2", 10.0)), float(p.get("DD_SCALE_2", 0.5))

    ret = np.zeros(len(close))
    ret[1:] = close[1:] / close[:-1] - 1.0

    equity = np.ones(len(close))
    exposure = np.zeros(len(close))
    for t in range(1, len(close)):
        sc = base_scale[t - 1]
        if use_dd:
            lo = max(0, t - look)
            hwm = equity[lo:t].max()
            dd = (equity[t - 1] / hwm - 1.0) * 100.0 if hwm > 0 else 0.0
            if dd <= -l2:
                sc *= s2
            elif dd <= -l1:
                sc *= s1
        exposure[t] = sc
        equity[t] = equity[t - 1] * (1.0 + sc * ret[t])
    return equity, exposure, ret


def metrics(equity, exposure, ret, n_years):
    port_ret = np.zeros(len(equity))
    port_ret[1:] = equity[1:] / equity[:-1] - 1.0
    hwm = np.maximum.accumulate(equity)
    mdd = ((equity / hwm - 1.0).min()) * 100.0
    cagr = (equity[-1] ** (1.0 / n_years) - 1.0) * 100.0
    sd = port_ret[1:].std()
    sharpe = (port_ret[1:].mean() / sd * np.sqrt(TRADING_DAYS)) if sd > 0 else 0.0
    return dict(cagr=cagr, mdd=mdd, sharpe=sharpe, expo=exposure[1:].mean(),
                final=equity[-1])


# ----------------------------------------------------------------------------
# 2단계 — 휩소율 축의 잉여성
# ----------------------------------------------------------------------------
def _stat(x):
    x = x[~np.isnan(x)]
    if not len(x):
        return "-"
    return (f"n={len(x):>4}  평균 {x.mean():+6.2f}%  중앙 {np.median(x):+6.2f}%  "
            f"승률 {(x > 0).mean() * 100:5.1f}%  하위10% {np.percentile(x, 10):+7.2f}%")


def audit_whipsaw(name, dates, close, regimes, whips, gate, fwd):
    print(f"\n{'=' * 96}\n[2단계] {name} — 휩소율 축은 잉여인가\n{'=' * 96}")

    valid = ~np.isnan(whips)
    print(f"  휩소율 산출 가능 {valid.sum()}일 / 전체 {len(whips)}일")
    print(f"\n  (a) 휩소율 구간별 향후 {FWD_DAYS}거래일 지수 수익률")
    print(f"    {'(전체)':<22} {_stat(fwd)}")
    for lo, hi in [(0.0, 0.375), (0.375, 0.5), (0.5, 0.625), (0.625, 0.75), (0.75, 1.01)]:
        m = valid & (whips >= lo) & (whips < hi)
        if m.sum():
            print(f"    {f'휩소율 {lo:.2f}~{hi:.2f}':<22} {_stat(fwd[m])}")

    print("\n  (b) 국면·게이트를 통제한 뒤에도 휩소율이 갈라내는가")
    for label, base in [("게이트 통과 + Bull/PendUp", ~gate & np.isin(regimes, ("Bull", "PendUp"))),
                        ("게이트 차단 + PendDown", gate & (regimes == "PendDown")),
                        ("게이트 차단 + Bear", gate & (regimes == "Bear"))]:
        m = base & valid
        if m.sum() < 100:
            continue
        med = np.median(whips[m])
        print(f"    ── {label} (n={m.sum()}, 휩소율 중앙 {med:.2f})")
        print(f"       휩소율 낮음(정상)  {_stat(fwd[m & (whips <= med)])}")
        print(f"       휩소율 높음(톱니)  {_stat(fwd[m & (whips > med)])}")

    print("\n  (c) 휩소율 배수의 실제 작용 범위")
    ws = whipsaw_scale(whips)
    print(f"    평균 배수 {ws.mean():.3f}   배수<1.0인 날 {(ws < 1.0).mean() * 100:.1f}%   "
          f"최소 {ws.min():.2f}")


# ----------------------------------------------------------------------------
# 3단계 — 결합 방식 비교
# ----------------------------------------------------------------------------
def audit_combination(name, dates, close, regimes, whips, gate_old, gate_new):
    print(f"\n{'=' * 96}\n[3단계] {name} — 결합 방식별 자산곡선 (지수 노출 시뮬레이션)\n{'=' * 96}")
    n_years = (dates[-1] - dates[0]).days / 365.25
    rs, ws = regime_scale(regimes), whipsaw_scale(whips)
    one = np.ones(len(close))

    variants = [
        ("① 매수후보유 (기준선)",            one,                              False),
        ("② 현행: 게이트+곱(국면×휩소×DD)",  np.where(gate_old, 0.0, rs * ws), True),
        ("③ 현행에서 휩소율 축 제거",         np.where(gate_old, 0.0, rs),      True),
        ("④ 현행에서 드로다운 축 제거",       np.where(gate_old, 0.0, rs * ws), False),
        ("⑤ 게이트만 (배수 전부 제거)",       np.where(gate_old, 0.0, one),     False),
        ("⑥ 배수만 (게이트 제거)",           rs * ws,                          True),
        ("⑦ 곱 대신 min(국면,휩소)",         np.where(gate_old, 0.0, np.minimum(rs, ws)), True),
        ("⑧ Bear해제 게이트 + 곱",           np.where(gate_new, 0.0, rs * ws), True),
        ("⑨ Bear해제 게이트 + 휩소 제거",     np.where(gate_new, 0.0, rs),      True),
        ("⑩ Bear해제 게이트만",              np.where(gate_new, 0.0, one),     False),
    ]

    print(f"  {'구성':<30}{'CAGR':>8}{'MDD':>9}{'Sharpe':>8}{'평균노출':>9}{'최종배수':>9}"
          f"{'노출효율':>9}")
    base_cagr = None
    for label, scale, use_dd in variants:
        eq, ex, ret = simulate(close, scale, use_dd=use_dd)
        m = metrics(eq, ex, ret, n_years)
        if base_cagr is None:
            base_cagr = m['cagr']
        eff = m['cagr'] / m['expo'] if m['expo'] > 0 else 0.0
        print(f"  {label:<30}{m['cagr']:>7.2f}%{m['mdd']:>8.1f}%{m['sharpe']:>8.2f}"
              f"{m['expo']:>9.3f}{m['final']:>8.2f}x{eff:>8.2f}")


def audit_robustness(name, dates, close, regimes, whips, gate_old, gate_new):
    """핵심 후보 몇 개만 하위기간으로 쪼개 본다 — 전 구간 1등이 특정 시기의 산물인지 확인."""
    print(f"\n[3단계-견고성] {name} — 하위기간별 CAGR / MDD / Sharpe")
    rs, ws = regime_scale(regimes), whipsaw_scale(whips)
    one = np.ones(len(close))
    cands = [
        ("① 매수후보유",          one,                              False),
        ("② 현행",               np.where(gate_old, 0.0, rs * ws), True),
        ("⑧ Bear해제+곱",        np.where(gate_new, 0.0, rs * ws), True),
        ("⑩ Bear해제 게이트만",   np.where(gate_new, 0.0, one),     False),
    ]
    cuts = [('2010-01-01', '2015-12-31'), ('2016-01-01', '2020-12-31'),
            ('2021-01-01', '2026-12-31')]
    print(f"  {'구성':<22}" + "".join(f"{lo[:4]}~{hi[:4]:<12}" for lo, hi in cuts))
    for label, scale, use_dd in cands:
        cells = []
        for lo, hi in cuts:
            sel = (dates >= lo) & (dates <= hi)
            sub_close, sub_scale = close[sel], scale[sel]
            yrs = (dates[sel][-1] - dates[sel][0]).days / 365.25
            eq, ex, ret = simulate(sub_close, sub_scale, use_dd=use_dd)
            m = metrics(eq, ex, ret, yrs)
            cells.append(f"{m['cagr']:>6.2f}%{m['mdd']:>7.1f}%{m['sharpe']:>6.2f}  ")
        print(f"  {label:<22}" + "".join(cells))


def main():
    for name, tk in [("KOSPI", "KS11"), ("KOSDAQ", "KQ11")]:
        dates, close = load_index(tk, '2010-01-01')
        regimes, whips = regime_series(dates, close)
        fwd = forward_return(close)

        config.settings.MARKET_FILTER_RELEASE_ON_BEAR = False
        gate_old = indicators.get_market_filter_blocked(pd.Series(close)).values.astype(bool)
        config.settings.MARKET_FILTER_RELEASE_ON_BEAR = True
        gate_new = indicators.get_market_filter_blocked(pd.Series(close)).values.astype(bool)
        config.settings.MARKET_FILTER_RELEASE_ON_BEAR = False

        audit_whipsaw(name, dates, close, regimes, whips, gate_old, fwd)
        audit_combination(name, dates, close, regimes, whips, gate_old, gate_new)
        audit_robustness(name, dates, close, regimes, whips, gate_old, gate_new)


if __name__ == '__main__':
    main()
