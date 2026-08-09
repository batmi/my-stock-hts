"""ATR 손절 캡을 지수 변동성 국면에 따라 넓히는 장치.

[왜 필요했나] 고정 -15%는 평시엔 이상치만 잘라내지만 고변동 국면에서는 상시 구속해
ATR 적응 손절을 사실상 고정 손절로 만든다. 실측(2026-08-09, 41종목·10년 KRX 확정 일봉)
캡 구속률이 10년간 연 0.2~3.9%였는데 2026-07에 66.4%였다. 그러면 변동성 상위 =
대개 모멘텀 상위 종목의 청산선이 노이즈 안으로 들어온다 — 추세추종에서 가장 비싼 쪽이다.

[무엇을 지켜야 하나]
  · 평시(배율 1.0)에는 종전과 **완전히 같아야** 한다. 이 장치의 채택 근거가 '평시 무해'다.
  · 배율이 커지면 캡이 넓어지고, 상·하한 밖으로 발산하지 않아야 한다.
  · 실매매와 백테스트가 같은 배율에서 같은 손절률을 내야 한다(파라미터 검증의 전제).
  · 배율 산출이 미래를 보면 안 된다.
  · 무엇 하나 실패해도 고정 캡으로 되돌아가야 한다(fail-safe) — 손절은 꺼지면 안 된다.
"""
import numpy as np
import pandas as pd
import pytest

import config
import indicators
from modules import backtest, portfolio_backtest as pbt
from modules.auto_trade import engine

BASE = -15.0


@pytest.fixture(autouse=True)
def _reset():
    saved = {k: config.SELL_STRATEGY.get(k) for k in
             ("ATR_CAP_DYNAMIC", "ATR_CAP_VOL_POWER", "ATR_CAP_FLOOR", "ATR_CAP_CEIL",
              "MAX_ATR_STOP_LOSS_RATE", "ATR_STOP_MULTIPLIER")}
    engine.set_vol_regime_ratio(1.0)
    yield
    config.SELL_STRATEGY.update({k: v for k, v in saved.items() if v is not None})
    engine.set_vol_regime_ratio(1.0)


# ---------------------------------------------------------------------------
# 캡 산식
# ---------------------------------------------------------------------------
def test_calm_regime_is_identical_to_the_old_fixed_cap():
    """[핵심] 배율 1.0에서 종전과 한 치도 달라지면 안 된다 — '평시 무해'가 채택 근거다."""
    assert engine.effective_atr_stop_cap(1.0) == BASE


def test_cap_widens_as_the_market_gets_rougher():
    caps = [engine.effective_atr_stop_cap(r) for r in (1.0, 1.5, 2.0, 3.0)]
    assert caps == sorted(caps, reverse=True), caps      # 점점 더 음수(= 넓어짐)
    assert caps[-1] < BASE


def test_calm_market_tightens_the_cap():
    """지수가 평시보다 잔잔하면 캡도 좁아진다 — 한 방향으로만 움직이면 국면 적응이 아니다."""
    assert engine.effective_atr_stop_cap(0.5) > BASE


def test_cap_never_escapes_its_bounds():
    floor = config.SELL_STRATEGY["ATR_CAP_FLOOR"]
    ceil = config.SELL_STRATEGY["ATR_CAP_CEIL"]
    for r in (0.001, 0.4, 1.0, 3.0, 1000.0):
        assert floor <= engine.effective_atr_stop_cap(r) <= ceil


def test_square_root_keeps_the_widening_gentle():
    """power=0.5. 선형(1.0)이면 고변동 국면에서 캡이 하한까지 가 사실상 해제된다."""
    config.SELL_STRATEGY["ATR_CAP_VOL_POWER"] = 0.5
    gentle = engine.effective_atr_stop_cap(3.0)
    config.SELL_STRATEGY["ATR_CAP_VOL_POWER"] = 1.0
    aggressive = engine.effective_atr_stop_cap(3.0)
    assert aggressive < gentle < BASE


# ---------------------------------------------------------------------------
# fail-safe — 손절은 어떤 경우에도 꺼지면 안 된다
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("bad", [None, 0, -1.0, float("nan"), float("inf"), "x"])
def test_bad_ratio_falls_back_to_the_fixed_cap(bad):
    assert engine.effective_atr_stop_cap(bad) == BASE


def test_switch_off_restores_the_fixed_cap():
    config.SELL_STRATEGY["ATR_CAP_DYNAMIC"] = False
    assert engine.effective_atr_stop_cap(3.0) == BASE


def test_cap_zero_means_no_cap_at_all():
    """MAX_ATR_STOP_LOSS_RATE=0은 '캡 미사용'이다. 동적화가 이 뜻을 바꾸면 안 된다."""
    config.SELL_STRATEGY["MAX_ATR_STOP_LOSS_RATE"] = 0.0
    assert engine.effective_atr_stop_cap(3.0) == 0.0


def test_set_vol_regime_ratio_ignores_garbage():
    engine.set_vol_regime_ratio(2.0)
    for bad in (None, 0, -1, float("nan"), "x"):
        engine.set_vol_regime_ratio(bad)
        assert engine.get_vol_regime_ratio() == 2.0


# ---------------------------------------------------------------------------
# 손절률에 실제로 반영되는가
# ---------------------------------------------------------------------------
def test_atr_stop_rate_follows_the_dynamic_cap():
    config.SELL_STRATEGY["ATR_STOP_MULTIPLIER"] = 2.0
    # 산식값 -40%. 평시 캡(-15%)에도, 배율 3.0의 캡(-25.98%)에도 걸릴 만큼 넓다.
    atr, price = 20_000.0, 100_000.0
    assert engine.atr_stop_rate(atr, price, vol_ratio=1.0) == pytest.approx(BASE)
    wide = engine.atr_stop_rate(atr, price, vol_ratio=3.0)
    assert wide < BASE
    assert wide == pytest.approx(engine.effective_atr_stop_cap(3.0))


def test_regime_only_relaxes_what_the_cap_was_cutting():
    """산식값이 두 캡 사이에 있으면, 넓어진 캡에서는 산식값이 그대로 살아난다.
    이것이 이 장치의 목적이다 — 캡을 푸는 게 아니라 'ATR이 정한 폭을 돌려주는' 것."""
    config.SELL_STRATEGY["ATR_STOP_MULTIPLIER"] = 2.0
    atr, price = 12_000.0, 100_000.0                      # 산식값 -24%
    assert engine.atr_stop_rate(atr, price, vol_ratio=1.0) == pytest.approx(BASE)   # 잘림
    assert engine.atr_stop_rate(atr, price, vol_ratio=3.0) == pytest.approx(-24.0)  # 살아남


def test_uncapped_stop_is_untouched_by_the_regime():
    """캡에 안 걸리는 좁은 손절폭은 국면과 무관해야 한다 — 캡은 상한이지 배율이 아니다."""
    config.SELL_STRATEGY["ATR_STOP_MULTIPLIER"] = 2.0
    atr, price = 1_000.0, 100_000.0           # 산식값 -2%
    assert (engine.atr_stop_rate(atr, price, vol_ratio=1.0)
            == engine.atr_stop_rate(atr, price, vol_ratio=3.0))


def test_explicit_max_cap_still_wins():
    """호출부가 캡을 명시하면 그것을 쓴다(개별 룰 등의 통로를 막지 않는다)."""
    assert engine.atr_stop_rate(12_000, 100_000, max_cap=-9.0, vol_ratio=3.0) == pytest.approx(-9.0)


# ---------------------------------------------------------------------------
# 실매매 ↔ 백테스트 패리티
# ---------------------------------------------------------------------------
def test_live_and_backtest_agree_on_the_same_ratio():
    """두 경로가 갈리면 이 설정을 정한 백테스트가 실거래에 옮겨가지 않는다."""
    config.SELL_STRATEGY["ATR_STOP_MULTIPLIER"] = 2.0
    day = "20260731"
    for ratio in (0.6, 1.0, 1.4, 2.0, 3.0):
        backtest._VOL_REGIME_STATE["by_date"] = {day: ratio}
        engine.set_vol_regime_ratio(ratio)
        for atr, price in ((12_000, 100_000), (3_000, 50_000), (900, 20_000)):
            bt = pbt._atr_stop_rate(atr, price, 2.0, day)
            live = engine.atr_stop_rate(atr, price, atr_mult=2.0)
            assert bt == pytest.approx(live), (ratio, atr, price, bt, live)
    backtest._VOL_REGIME_STATE["by_date"] = {}


def test_backtest_without_a_ratio_uses_the_fixed_cap():
    """배율 조회 실패(빈 dict)는 고정 캡으로 — 시뮬레이션이 멈추면 안 된다."""
    backtest._VOL_REGIME_STATE["by_date"] = {}
    engine.set_vol_regime_ratio(1.0)
    assert pbt._atr_stop_rate(12_000, 100_000, 2.0, "20260731") == pytest.approx(BASE)


# ---------------------------------------------------------------------------
# 배율 산출
# ---------------------------------------------------------------------------
def test_ratio_does_not_peek_into_the_future():
    """뒤쪽 값을 바꿔도 앞쪽 배율은 변하면 안 된다."""
    rng = np.random.default_rng(20260809)
    close = pd.Series(100 * np.exp(np.cumsum(rng.normal(0, 0.01, 900))))
    a = indicators.vol_regime_ratio(close)
    tampered = close.copy()
    tampered.iloc[700:] *= np.linspace(1.0, 2.5, len(tampered) - 700)   # 미래를 크게 흔든다
    b = indicators.vol_regime_ratio(tampered)
    assert np.allclose(a.iloc[:700], b.iloc[:700])


def test_ratio_is_one_during_warmup():
    """표본이 모자란 구간은 1.0 = 고정 캡. 근거 없는 값으로 손절폭을 흔들면 안 된다."""
    close = pd.Series(np.linspace(100, 110, 120))
    assert (indicators.vol_regime_ratio(close) == 1.0).all()


def test_ratio_rises_when_volatility_rises():
    rng = np.random.default_rng(7)
    calm = rng.normal(0, 0.005, 700)
    storm = rng.normal(0, 0.025, 200)
    close = pd.Series(100 * np.exp(np.cumsum(np.concatenate([calm, storm]))))
    r = indicators.vol_regime_ratio(close)
    assert r.iloc[-1] > 2.0, r.iloc[-1]
    assert r.iloc[650] < 1.5


def test_ratio_is_bounded():
    rng = np.random.default_rng(11)
    close = pd.Series(100 * np.exp(np.cumsum(
        np.concatenate([rng.normal(0, 0.001, 600), rng.normal(0, 0.2, 300)]))))
    r = indicators.vol_regime_ratio(close)
    lo = config.SELL_STRATEGY["ATR_CAP_RATIO_MIN"]
    hi = config.SELL_STRATEGY["ATR_CAP_RATIO_MAX"]
    assert r.min() >= lo and r.max() <= hi
