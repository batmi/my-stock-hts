"""pandas 판이 바뀌어도 판단 값이 바뀌지 않아야 한다.

[2026-10-24] 하한만 걸린 requirements 로 Python 3.11+ 에 새로 설치하면 pandas 3.x 가 들어온다(운영 라즈베리파이는
Python 3.13, 개발기 venv 는 pandas 2.3.3). 전체 테스트 5,919건은 pandas 3.1 에서도 통과했지만, 한 자리는
**값이** 달라졌다 — Series.pct_change 의 fill_method 기본값이 'pad' → None 으로 바뀌어, 중간 결측이 있으면
pandas 3 는 그 자리와 다음 자리를 NaN 으로 둔다. ATR 손절 캡 배율(indicators.vol_regime_ratio)은 그 수익률의
rolling std 를 쓰므로, 결측 하나가 창 하나(60봉) 내내 배율을 1.0(고정 캡)으로 만든다. 실매매 상관 필터의 수익률도
pct_change 로 구한다. 감사·검증은 모두 pandas 2 의미(앞 값으로 채움)로 이뤄졌으므로 그 의미를 코드에 명시해 고정한다.
"""
import os
import re

import numpy as np
import pandas as pd

from core import indicators

ROOT = os.path.join(os.path.dirname(os.path.abspath(__file__)), os.pardir)
_SCAN = ("modules", "api", "core", "brokers", "tools")


def _py_files():
    yield os.path.join(ROOT, "main.py")
    yield os.path.join(ROOT, "config.py")
    for base in _SCAN:
        for dirpath, _dirs, files in os.walk(os.path.join(ROOT, base)):
            for f in files:
                if f.endswith(".py"):
                    yield os.path.join(dirpath, f)


def test_pct_change_는_결측_처리를_명시한다():
    """기본값에 기대면 pandas 2 와 3 이 다른 값을 낸다 — fill_method 를 적는다(가격은 ffill 뒤 None)."""
    bad = []
    for p in _py_files():
        for i, line in enumerate(open(p, encoding="utf-8"), 1):
            code = line.split("#", 1)[0]
            if re.search(r"\.pct_change\(", code) and "fill_method" not in code:
                bad.append(f"{os.path.relpath(p, ROOT)}:{i}")
    assert not bad, f"fill_method 없이 pct_change 를 부른다(pandas 판에 따라 값이 다르다): {bad}"


def test_명시형은_pandas_판과_무관하게_앞값_채움과_같다():
    s = pd.Series([np.nan, 100.0, 110.0, np.nan, 121.0, 133.1])
    got = s.ffill().pct_change(fill_method=None).round(4).tolist()
    assert np.isnan(got[0]) and np.isnan(got[1])
    assert got[2:] == [0.1, 0.0, 0.1, 0.1]


def test_ATR_캡_배율은_결측이_있어도_앞값으로_채운_시계열과_같다():
    rng = np.random.default_rng(20261024)
    close = pd.Series(100 * np.cumprod(1 + rng.normal(0, 0.012, 400)))
    gapped = close.copy()
    gapped.iloc[[120, 260, 261]] = np.nan          # 지수 종가 결측(휴장 정렬·조회 누락)
    a = indicators.vol_regime_ratio(gapped, window=60, ref_min=250)
    b = indicators.vol_regime_ratio(gapped.ffill(), window=60, ref_min=250)
    pd.testing.assert_series_equal(a.reset_index(drop=True), b.reset_index(drop=True))
    #  결측 뒤 창 하나 내내 1.0 으로 꺼지지 않는다(pandas 3 기본값이던 시절의 증상)
    assert (a.iloc[262:322] != 1.0).any()
