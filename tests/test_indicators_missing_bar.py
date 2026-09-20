"""calculate_indicators 는 창 안의 결측 봉을 만나도 NaN 을 내보내지 않는다 — 모르면 None.

[왜 · 2026-09-20] 마지막 봉 결측은 09-05 에 막았지만(전부 None), rolling 창 안의 결측은
CCI 만 NaN 으로 새어 나왔다(실측: 200봉 중 5봉 전 결측 → cci·prev_cci NaN). NaN 은 모든
비교에서 False 라 점수만 조용히 깎이고 화면에는 'nan' 이 찍힌다.
"""
import math

import numpy as np
import pandas as pd

from core import indicators


def _frame(n=200, seed=1):
    rng = np.random.default_rng(seed)
    c = 100 + np.cumsum(rng.normal(0, 1, n))
    return pd.DataFrame({'open': c, 'high': c + 1, 'low': c - 1, 'close': c,
                         'volume': rng.integers(1000, 5000, n)})


def _nan_keys(ind):
    return sorted(k for k, v in ind.items()
                  if isinstance(v, (float, np.floating)) and math.isnan(v))


def test_a_missing_bar_inside_the_cci_window_gives_none_not_nan():
    df = _frame()
    df.loc[195, ['high', 'low', 'close']] = np.nan
    ind = indicators.calculate_indicators(df)
    assert _nan_keys(ind) == []
    assert ind['cci'] is None and ind.get('prev_cci') is None
    # 결측을 건너뛰는 지표는 여전히 값을 낸다 — 통째로 버리는 게 아니다.
    assert ind['rsi'] is not None and ind['ema_20'] is not None


def test_a_clean_frame_is_untouched():
    ind = indicators.calculate_indicators(_frame())
    assert _nan_keys(ind) == []
    assert ind['cci'] is not None and isinstance(ind['cci'], float)


def test_a_missing_last_bar_still_returns_the_empty_contract():
    df = _frame()
    df.loc[199, 'close'] = np.nan
    ind = indicators.calculate_indicators(df)
    assert ind['rsi'] is None and ind['cci'] is None and _nan_keys(ind) == []
