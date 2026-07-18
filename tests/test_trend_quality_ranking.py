"""
추세 품질(회귀 모멘텀 = 연환산 기울기 × R²) 지표 및 매수 후보 랭킹 테스트.

변경 사항:
  1. indicators.get_trend_quality — 최근 TREND_QUALITY_LOOKBACK일 로그 종가 선형회귀로
     '연환산 기울기 × R²'(Clenow 모멘텀)를 계산. 매끄러운 추세일수록 높은 값.
  2. 매수 후보 우선순위 — 게이트(BUY_SCORE)와 랭킹을 분리.
     candidate_priority_key: 추세 품질 → 점수 → 52주 위치 → 체결강도 순.
     trend_quality=None(이력 부족)은 최하순위.
"""

import numpy as np
import pandas as pd
import pytest

import indicators
from modules.auto_trade.trader import candidate_priority_key


# ─────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────

def make_df(closes):
    closes = pd.Series(closes, dtype=float)
    return pd.DataFrame({
        'date': [f"2025{i:04d}" for i in range(len(closes))],
        'open': closes, 'high': closes * 1.01, 'low': closes * 0.99,
        'close': closes, 'volume': [1000.0] * len(closes),
    })


LOOKBACK = 90


# ─────────────────────────────────────────────
# get_trend_quality
# ─────────────────────────────────────────────

class TestGetTrendQuality:
    def test_smooth_uptrend_positive_and_matches_formula(self):
        # 일 0.2% 완전 지수 성장 → R²=1, 품질 = 연환산 수익률
        daily = 0.002
        closes = 100 * np.exp(daily * np.arange(LOOKBACK))
        q = indicators.get_trend_quality(make_df(closes), lookback=LOOKBACK)
        expected = (np.exp(daily * 252) - 1) * 100  # ≈ 65.5%
        assert q == pytest.approx(expected, rel=0.01)

    def test_choppy_trend_scores_lower_than_smooth(self):
        # 시작·끝이 같아도 급등락(노이즈)이 크면 R² 하락 → 품질 열위
        rng = np.random.default_rng(42)
        daily = 0.002
        smooth = 100 * np.exp(daily * np.arange(LOOKBACK))
        noise = rng.normal(0, 0.05, LOOKBACK)
        noise -= noise.mean()  # 총수익률은 유사하게 유지
        choppy = smooth * np.exp(noise)
        q_smooth = indicators.get_trend_quality(make_df(smooth), lookback=LOOKBACK)
        q_choppy = indicators.get_trend_quality(make_df(choppy), lookback=LOOKBACK)
        assert q_smooth > q_choppy

    def test_downtrend_negative(self):
        closes = 100 * np.exp(-0.002 * np.arange(LOOKBACK))
        q = indicators.get_trend_quality(make_df(closes), lookback=LOOKBACK)
        assert q < 0

    def test_flat_price_zero(self):
        q = indicators.get_trend_quality(make_df([100.0] * LOOKBACK), lookback=LOOKBACK)
        assert q == 0.0

    def test_insufficient_history_returns_none(self):
        closes = 100 * np.exp(0.002 * np.arange(LOOKBACK - 1))
        assert indicators.get_trend_quality(make_df(closes), lookback=LOOKBACK) is None

    def test_uses_config_lookback_default(self):
        # lookback 미지정 시 config 기본값(90) 사용 — 90봉이면 계산 가능해야 함
        import config
        lb = config.INDICATOR_PARAMS.get("TREND_QUALITY_LOOKBACK", 90)
        closes = 100 * np.exp(0.001 * np.arange(lb))
        assert indicators.get_trend_quality(make_df(closes)) is not None

    def test_invalid_close_returns_none(self):
        closes = [100.0] * LOOKBACK
        closes[10] = 0.0  # log(0) 방어
        assert indicators.get_trend_quality(make_df(closes), lookback=LOOKBACK) is None


# ─────────────────────────────────────────────
# candidate_priority_key (게이트/랭킹 분리)
# ─────────────────────────────────────────────

class TestCandidatePriorityKey:
    def test_trend_quality_beats_higher_score(self):
        # 점수가 낮아도 추세 품질이 높으면 우선 (게이트 통과 후에는 품질이 1순위)
        strong = {'score': 7.0, 'trend_quality': 80.0, 'w52_pos': 60.0, 'vol_strength': 100.0}
        weak = {'score': 9.0, 'trend_quality': 10.0, 'w52_pos': 95.0, 'vol_strength': 150.0}
        ranked = sorted([weak, strong], key=candidate_priority_key)
        assert ranked[0] is strong

    def test_score_breaks_quality_tie(self):
        a = {'score': 8.0, 'trend_quality': 50.0, 'w52_pos': 50.0, 'vol_strength': 100.0}
        b = {'score': 7.0, 'trend_quality': 50.0, 'w52_pos': 90.0, 'vol_strength': 150.0}
        ranked = sorted([b, a], key=candidate_priority_key)
        assert ranked[0] is a

    def test_none_quality_ranks_last(self):
        # 이력 부족(None)은 음수 품질보다도 뒤 (검증 불가 종목 후순위)
        no_hist = {'score': 9.5, 'trend_quality': None, 'w52_pos': 99.0, 'vol_strength': 200.0}
        negative = {'score': 7.0, 'trend_quality': -5.0, 'w52_pos': 30.0, 'vol_strength': 90.0}
        ranked = sorted([no_hist, negative], key=candidate_priority_key)
        assert ranked[-1] is no_hist

    def test_w52_then_vol_strength_tiebreak(self):
        a = {'score': 7.0, 'trend_quality': 50.0, 'w52_pos': 90.0, 'vol_strength': 100.0}
        b = {'score': 7.0, 'trend_quality': 50.0, 'w52_pos': 80.0, 'vol_strength': 150.0}
        c = {'score': 7.0, 'trend_quality': 50.0, 'w52_pos': 90.0, 'vol_strength': 120.0}
        ranked = sorted([b, a, c], key=candidate_priority_key)
        assert ranked == [c, a, b]

    def test_missing_optional_fields_do_not_crash(self):
        a = {'score': 7.0}
        b = {'score': 8.0, 'trend_quality': 1.0}
        ranked = sorted([a, b], key=candidate_priority_key)
        assert ranked[0] is b


# ─────────────────────────────────────────────
# analyze_buy 결과에 trend_quality 포함 (라이브 경로 연결 확인)
# ─────────────────────────────────────────────

def test_analyze_buy_includes_trend_quality(monkeypatch):
    from modules.auto_trade.engine import DefaultStrategy
    from modules import analysis

    monkeypatch.setattr(analysis, 'check_smart_money_turnaround', lambda code, ov: (False, ""))

    n = 300
    closes = 100 * np.exp(0.002 * np.arange(n))
    df = make_df(closes)
    strategy = DefaultStrategy()
    result = strategy.analyze_buy("005930", "테스트", df, float(closes[-1]))
    assert result is not None
    assert 'trend_quality' in result
    assert result['trend_quality'] is not None and result['trend_quality'] > 0
