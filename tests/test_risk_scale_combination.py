"""감속 축을 **합치는 법**을 바꿀 수 있게 하되, 기본값은 한 칸도 달라지지 않게 한다.

[배경 · 2026-09-08] 네 축(국면·휩소율·시장필터·드로다운)은 서로 80% 넘게 겹친다
(실측 KOSPI: P(휩소|시장필터) 87.5% · 3축 이상 동시 발동 36.1%). 곱으로 합치면 같은
정보가 여러 번 과세되고, 결합 배수와 향후 지수 수익률이 단조가 아니다 — 가장 좋은
구간(0.60~0.80, +2.14%)에서 노출을 크게 깎는다.

그래서 `min` 결합과 `하한`을 **감사 도구가 실험할 수 있게** 열었다. 다만 이것은
계측기 옵션이지 설정 변경이 아니다. **기본값이 조금이라도 움직이면 지금까지의 모든
감사 수치가 다른 계측기에서 나온 것이 된다.** 그 회귀를 여기서 막는다.

축을 아예 빼는 방향은 이미 반증돼 있다(상수 배수 대조군은 수익 절반 — 146.5%→71.0%).
→ [[drawdown-lever-backtest]] · [[audit-scale-fn-contamination]]
"""
import pytest

from tools.audit_drawdown_axis import make_scale_fn

DD = (90, 5.0, 0.9, 10.0, 0.8)      # 현행 RISK_SCALING_PARAMS 와 같은 모양


def _run(fn, equities):
    """(날짜, 자산) 열을 흘려 넣고 마지막 배수를 돌려준다."""
    out = None
    for i, eq in enumerate(equities):
        out = fn(f"2026010{i + 1}", eq)
    return out


def test_the_default_is_still_a_product():
    """시장 배수 × 드로다운 배수 — 종전 그대로."""
    fn = make_scale_fn({"20260101": 0.6, "20260102": 0.6}, DD)
    # 자산이 100 → 88 이면 -12% 라 2단(0.8)이 걸린다. 0.6 × 0.8 = 0.48
    assert _run(fn, [100.0, 88.0]) == pytest.approx(0.48)


def test_min_listens_to_the_worst_axis_only():
    fn = make_scale_fn({"20260101": 0.6, "20260102": 0.6}, DD, combine="min")
    assert _run(fn, [100.0, 88.0]) == pytest.approx(0.6), \
        "min 은 곱하지 않는다 — 가장 나쁜 축 하나만 듣는다"


def test_min_still_takes_the_drawdown_when_it_is_worse():
    fn = make_scale_fn({"20260101": 0.95, "20260102": 0.95}, DD, combine="min")
    assert _run(fn, [100.0, 88.0]) == pytest.approx(0.8)


def test_a_floor_stops_the_multiplier_from_sinking():
    fn = make_scale_fn({"20260101": 0.6, "20260102": 0.6}, DD, floor=0.60)
    assert _run(fn, [100.0, 88.0]) == pytest.approx(0.60), "하한이 곱을 막지 못했다"


def test_a_floor_never_raises_a_healthy_multiplier():
    """하한은 바닥일 뿐 — 멀쩡한 배수를 끌어올리면 노출이 늘어난다."""
    fn = make_scale_fn({"20260101": 1.0, "20260102": 1.0}, DD, floor=0.60)
    assert _run(fn, [100.0, 100.0]) == pytest.approx(1.0)


def test_turning_the_market_axis_off_still_works_with_the_new_options():
    fn = make_scale_fn({"20260101": 0.5}, DD, use_market=False, combine="min")
    assert _run(fn, [100.0]) == pytest.approx(1.0), "축을 껐는데 시장 배수가 샜다"


def test_the_market_series_defaults_to_product(monkeypatch):
    """국면·휩소 결합도 기본은 곱이다(계열을 만드는 쪽)."""
    import numpy as np

    import tools.audit_drawdown_axis as ax

    import pandas as pd
    idx = pd.to_datetime(["2026-01-01", "2026-01-02"])
    monkeypatch.setattr(ax, "load_index", lambda t, s: (idx, np.array([1.0, 1.0])))
    monkeypatch.setattr(ax, "regime_series", lambda i, c: (None, None))
    monkeypatch.setattr(ax, "regime_scale", lambda r: np.array([0.6, 0.6]))
    monkeypatch.setattr(ax, "whipsaw_scale", lambda w: np.array([0.85, 0.85]))

    prod = ax.market_scale_by_date(["20260101"], 10)
    mn = ax.market_scale_by_date(["20260101"], 10, combine="min")
    assert prod["20260101"] == pytest.approx(0.51), "기본 결합이 곱이 아니다"
    assert mn["20260101"] == pytest.approx(0.6)
