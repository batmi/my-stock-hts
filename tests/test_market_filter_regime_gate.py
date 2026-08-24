"""시장 필터의 '확정 Bear 해제'(MARKET_FILTER_RELEASE_ON_BEAR) 동작 검증.

배경: 시장 필터(SMA80±1%)와 국면(EMA9/41)은 같은 지수 종가에서 나오는데 각각 독립으로
작동한다. 게이트 차단일을 국면별로 쪼개면 PendDown·PendUp은 차단이 옳지만 확정 Bear는
이미 -5% 하락을 소화한 반등 구간(향후 20일 +2.48%/+2.33%)이라 차단이 손해였다.
플래그를 켜면 Bear 구간에서만 차단을 해제한다. 근거는 config.py 주석 참조.
기본값은 OFF(기존 동작 유지)여야 한다.
"""
import numpy as np
import pandas as pd
import pytest

import config
from core import indicators
from modules import analysis


def _confirmed_bear():
    """확정 하락(Bear) — 교차 후 -5% 넘게 진행. 밴드도 이탈해 게이트가 차단 상태다."""
    return pd.Series(np.linspace(200.0, 90.0, 300))


def _pending_up():
    """장기 하락 뒤 반등 초입(PendUp) — 밴드는 아직 이탈이지만 확정 Bear는 아니다."""
    return pd.Series(np.concatenate([np.linspace(232.0, 50.0, 500),
                                     np.linspace(50.0, 58.0, 15)]))


def test_default_is_off():
    """실계좌 반영 전 포트폴리오 백테스트로 확인해야 하므로 기본값은 반드시 OFF."""
    assert getattr(config, 'MARKET_FILTER_RELEASE_ON_BEAR', False) is False


def test_band_only_behavior_unchanged_when_flag_off():
    for close in (_confirmed_bear(), _pending_up()):
        assert indicators.get_market_filter_blocked(close, 80, 1.0, release_on_bear=False).iloc[-1]


def test_gate_released_in_confirmed_bear():
    close = _confirmed_bear()
    assert indicators.get_regime_series(close)['regime'][-1] == "Bear"
    assert not indicators.get_market_filter_blocked(close, 80, 1.0, release_on_bear=True).iloc[-1]


def test_gate_kept_in_pending_up():
    """PendUp은 실측상 차단이 옳은 구간이므로 해제되면 안 된다."""
    close = _pending_up()
    assert indicators.get_regime_series(close)['regime'][-1] == "PendUp"
    assert indicators.get_market_filter_blocked(close, 80, 1.0, release_on_bear=True).iloc[-1]


def test_unknown_regime_stays_blocked():
    """국면 판정 불가(데이터 부족)는 해제하지 않는다 — fail-closed."""
    close = pd.Series(np.linspace(100.0, 70.0, 30))   # EMA 느린 기간(41)에 미달
    assert indicators.get_regime_series(close)['regime'][-1] == "Sideways"
    assert indicators.get_market_filter_blocked(close, 20, 1.0, release_on_bear=True).iloc[-1]


def test_flag_read_from_config(monkeypatch):
    close = _confirmed_bear()
    monkeypatch.setattr(config.settings, 'MARKET_FILTER_RELEASE_ON_BEAR', True)
    assert not indicators.get_market_filter_blocked(close, 80, 1.0).iloc[-1]
    monkeypatch.setattr(config.settings, 'MARKET_FILTER_RELEASE_ON_BEAR', False)
    assert indicators.get_market_filter_blocked(close, 80, 1.0).iloc[-1]


@pytest.mark.parametrize("close", [_confirmed_bear(), _pending_up()])
def test_regime_series_is_single_source_of_truth(close):
    """classify_regime_from_df는 get_regime_series의 마지막 원소와 같아야 한다."""
    ser = indicators.get_regime_series(close)
    one = analysis.classify_regime_from_df(pd.DataFrame({'close': close}))
    assert one['regime'] == ser['regime'][-1]
    assert one['moved_pct'] == pytest.approx(float(ser['moved_pct'][-1]))
    assert one['segments'] == int(ser['segments'][-1])


def test_regime_series_is_causal():
    """각 시점의 판정은 이후 데이터에 영향받지 않아야 한다(미래 참조 금지)."""
    close = _pending_up()
    full = indicators.get_regime_series(close)['regime']
    for cut in (120, 300, 480):
        assert indicators.get_regime_series(close.iloc[:cut])['regime'][-1] == full[cut - 1]
