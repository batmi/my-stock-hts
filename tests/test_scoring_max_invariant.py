"""스코어링 만점 불변식 — 총점은 항상 10.0이어야 한다.

[왜 테스트로 고정하는가] 만점이 10.0이라는 것은 주석이 아니라 **설계 계약**이다.
 BUY_SCORE(7.0)·RISE_SCORE(6.0)·SELL_SCORE(4.0)·SUPER_MOMENTUM_SCORE(8.0)가 전부 이
 10점 위의 절대값이라, 항목을 하나 더해 만점이 10.5가 되는 순간 **같은 BUY_SCORE가 더
 헐거운 문턱**으로 바뀐다. 그러면 그 문턱들로 정한 모든 실증(2026-08-12 스코어링 전수
 검증 포함)이 조용히 무효가 된다.

 그래서 새 항목은 언제나 기존 슬롯을 **교체**해야 하고, 예산을 늘려서는 안 된다.
 사람이 기억해서 지킬 규칙이 아니므로 여기서 잡는다.
"""
import numpy as np
import pytest

import config
from modules import analysis


def _all_items_fire():
    """모든 가점 항목이 동시에 켜지고 감점은 하나도 없는 입력.

    추세: 정배열 1.0 + EMA20위 0.5 + EMA5>20 0.5 = 2.0(MA 상한) + 지속이력 0.5
          + MACD 0선 0.5 + MACD 확산 0.5 + SAR 0.5
    모멘텀: RSI 강세 0.5 + 모멘텀 확장 0.5 + CCI 상승 0.5 + DMI 0.5 + 가격 모멘텀 0.5
    강도: ADX 0.5 + 거래량 0.5 + 수급 0.5     시너지: 추세 시작 1.0 + 모멘텀 폭발 1.0
    """
    return dict(
        price=120.0, ema20=100.0, ema60=95.0, ema120=90.0, ema_5=110.0,
        sar=80.0,                      # 주가 > SAR
        rsi=70.0,                      # 강세(>=50) + 확장(60~80)
        adx=30.0, cci=120.0, prev_cci=100.0,
        obv_trend=True, smart_money=True,
        macd=2.0, macd_signal=1.0,     # 0선 위 + 골든(감점 없음)
        macd_hist=1.0, prev_macd_hist=0.5,   # 확산(0선 위) → 시너지 조건도 충족
        plus_di=30.0, minus_di=10.0,   # +DI 우위(감점 없음)
        vol_spike=True, vol_trend=True,
        w52_pos=95.0, mom_ret=40.0, mom_ret_1m=5.0, mom_ret_3m=15.0,
        trend_persist=95.0,
    )


def test_모든_항목이_켜지면_정확히_10점이다():
    score, details = analysis.calculate_score(**_all_items_fire())
    assert score == pytest.approx(10.0), f"만점이 10.0이 아니다: {score}\n" + "\n".join(details)


def test_어떤_입력도_10점을_넘지_못한다():
    """무작위 입력 전수 탐색 — 항목이 늘거나 슬롯이 겹치면 여기서 먼저 터진다."""
    rng = np.random.default_rng(20260812)
    worst = 0.0
    for _ in range(3000):
        price = float(rng.uniform(50, 200))
        kw = dict(
            price=price,
            ema20=float(rng.uniform(50, 200)), ema60=float(rng.uniform(50, 200)),
            ema120=float(rng.uniform(50, 200)), ema_5=float(rng.uniform(50, 200)),
            sar=float(rng.uniform(50, 200)),
            rsi=float(rng.uniform(0, 100)), adx=float(rng.uniform(0, 60)),
            cci=float(rng.uniform(-250, 250)), prev_cci=float(rng.uniform(-250, 250)),
            obv_trend=bool(rng.integers(0, 2)), smart_money=bool(rng.integers(0, 2)),
            macd=float(rng.uniform(-5, 5)), macd_signal=float(rng.uniform(-5, 5)),
            macd_hist=float(rng.uniform(-3, 3)), prev_macd_hist=float(rng.uniform(-3, 3)),
            plus_di=float(rng.uniform(0, 50)), minus_di=float(rng.uniform(0, 50)),
            vol_spike=bool(rng.integers(0, 2)), vol_trend=bool(rng.integers(0, 2)),
            w52_pos=float(rng.uniform(0, 100)), mom_ret=float(rng.uniform(-50, 100)),
            mom_ret_1m=float(rng.uniform(-30, 30)), mom_ret_3m=float(rng.uniform(-40, 60)),
            trend_persist=float(rng.uniform(0, 100)),
        )
        score, _ = analysis.calculate_score(**kw)
        worst = max(worst, score)
        assert score <= 10.0 + 1e-9, f"10점을 넘었다: {score}\n{kw}"
    assert worst > 7.0, f"무작위 표본이 고득점 구간에 닿지 못했다(탐색 부족): 최대 {worst}"


def test_팩터_예산_합이_10이다():
    """가중치 4개의 합 = 만점. 여기가 10이 아니면 문턱(BUY_SCORE 등)의 의미가 달라진다."""
    w = config.SCORING_WEIGHTS
    total = w["TREND"] + w["MOMENTUM"] + w["STRENGTH"] + w["SYNERGY"]
    assert total == pytest.approx(10.0), f"팩터 예산 합이 10.0이 아니다: {total}"


def test_감점은_점수를_음수로_만들지_않는다():
    kw = _all_items_fire()
    kw.update(macd=-2.0, macd_signal=-1.0, macd_hist=-1.0, prev_macd_hist=-0.5,
              plus_di=10.0, minus_di=30.0, rsi=20.0, cci=-200.0, adx=10.0,
              price=60.0, ema20=100.0, ema60=110.0, ema120=120.0, ema_5=90.0,
              sar=130.0, obv_trend=False, smart_money=False, vol_spike=False,
              vol_trend=False, w52_pos=5.0, mom_ret=-30.0, trend_persist=5.0)
    score, _ = analysis.calculate_score(**kw)
    assert 0.0 <= score <= 10.0
