"""
RSI 스코어링 경계값 조정 및 역매수 수급 확인 조건 추가에 대한 테스트.

변경 사항:
  1. SCORE_RSI_REBOUND 30 → 40
     - RSI 40~49: 상승 여력 구간 (+0.5) — 초기 매수 진입 여지
     - RSI  < 40: 스코어링 점수 없음 (역매수 전용 영역으로 분리)
  2. classify_stock_state 역매수 조건에 수급 확인 추가
     - OBV 상승(obv_trend=True) 또는 스마트머니 유입(smart_money=True) 필수
     - 미충족 시 데드캣 바운스로 간주하여 역매수 신호 차단
"""

import pytest
import pandas as pd
from unittest.mock import patch

from modules import analysis
import config


# ─────────────────────────────────────────────
# Fixtures
# ─────────────────────────────────────────────

@pytest.fixture
def mr_thresholds():
    return {"USE_MEAN_REVERSION": True, "MR_RSI_MAX": 40.0, "MR_DISPARITY_MAX": 90.0}


@pytest.fixture
def yangbong_df():
    """양봉 캔들 DataFrame (종가 > 시가)"""
    return pd.DataFrame({'close': [8500.0], 'open': [8400.0]})


@pytest.fixture
def mr_base_args(mr_thresholds):
    """역매수 기본 조건을 모두 충족하는 인수 집합
    - 이격도 85% (8500/10000), RSI 35↑30, 양봉"""
    return dict(
        price=8500, ema20=10000, ema60=11000, ema120=12000,
        sar=9000, rsi=35.0, prev_rsi=30.0,
        adx=20, cci=-120,
        thresholds=mr_thresholds,
    )


# ─────────────────────────────────────────────
# Issue 1: RSI 스코어링 경계값 (30 → 40)
# ─────────────────────────────────────────────

class TestRsiScoreBoundary:
    """SCORE_RSI_REBOUND 변경(30→40) 이후의 calculate_score RSI 기여값 검증"""

    def test_config_score_rsi_rebound_is_40(self):
        """설정값 자체가 40으로 적용되어 있는지 확인"""
        assert config.INDICATOR_PARAMS.get('SCORE_RSI_REBOUND') == 40

    def test_rsi_40_at_lower_boundary_gets_score(self):
        """RSI 40: 하한 경계값 포함(≥40) — 상승 여력 구간 +0.5"""
        with patch.dict(config.INDICATOR_PARAMS, {'SCORE_RSI_REBOUND': 40}):
            score, details = analysis.calculate_score(price=10000, rsi=40.0)
        assert score == pytest.approx(0.5)
        assert any("상승 여력 구간" in d for d in details)

    def test_rsi_45_in_zone_gets_score(self):
        """RSI 45: 구간 내 — 상승 여력 구간 +0.5"""
        with patch.dict(config.INDICATOR_PARAMS, {'SCORE_RSI_REBOUND': 40}):
            score, details = analysis.calculate_score(price=10000, rsi=45.0)
        assert score == pytest.approx(0.5)
        assert any("상승 여력 구간" in d for d in details)

    def test_rsi_49_upper_boundary_in_zone_gets_score(self):
        """RSI 49: 상한 경계값 미만(<50) — 상승 여력 구간 +0.5"""
        with patch.dict(config.INDICATOR_PARAMS, {'SCORE_RSI_REBOUND': 40}):
            score, details = analysis.calculate_score(price=10000, rsi=49.0)
        assert score == pytest.approx(0.5)
        assert any("상승 여력 구간" in d for d in details)

    def test_rsi_39_just_below_boundary_gets_zero(self):
        """RSI 39: 40 미만 — 역매수 전용 영역, 스코어링 기여 없음"""
        with patch.dict(config.INDICATOR_PARAMS, {'SCORE_RSI_REBOUND': 40}):
            score, details = analysis.calculate_score(price=10000, rsi=39.0)
        assert score == pytest.approx(0.0)
        assert not any("RSI" in d for d in details)

    def test_rsi_30_old_threshold_now_gets_zero(self):
        """RSI 30: 구 기준(30)에서는 +0.5였으나 현재는 0점 (역매수 전용 영역)"""
        with patch.dict(config.INDICATOR_PARAMS, {'SCORE_RSI_REBOUND': 40}):
            score, details = analysis.calculate_score(price=10000, rsi=30.0)
        assert score == pytest.approx(0.0)
        assert not any("RSI" in d for d in details)

    def test_rsi_50_enters_강세구간(self):
        """RSI 50: 강세 구간 진입 — +0.5, 레이블 구분 확인"""
        with patch.dict(config.INDICATOR_PARAMS, {'SCORE_RSI_REBOUND': 40}):
            score, details = analysis.calculate_score(price=10000, rsi=50.0)
        assert score == pytest.approx(0.5)
        assert any("강세 구간" in d for d in details)
        assert not any("상승 여력 구간" in d for d in details)

    def test_rsi_60_gets_full_momentum_score(self):
        """RSI 60: 강세 구간(+0.5) + 모멘텀 확장(+0.5) = 1.0"""
        with patch.dict(config.INDICATOR_PARAMS,
                        {'SCORE_RSI_REBOUND': 40, 'SCORE_RSI_STRONG': 60, 'SCORE_RSI_MID': 50}):
            score, details = analysis.calculate_score(price=10000, rsi=60.0)
        assert score == pytest.approx(1.0)
        assert any("강세 구간" in d for d in details)
        assert any("모멘텀 확장" in d for d in details)

    def test_boundary_step_39_vs_40(self):
        """경계 전후 계단 확인: RSI 39 → 0점, RSI 40 → 0.5점"""
        with patch.dict(config.INDICATOR_PARAMS, {'SCORE_RSI_REBOUND': 40}):
            score_below, _ = analysis.calculate_score(price=10000, rsi=39.0)
            score_at, _    = analysis.calculate_score(price=10000, rsi=40.0)
        assert score_below == pytest.approx(0.0)
        assert score_at    == pytest.approx(0.5)
        assert score_at > score_below

    def test_label_is_상승여력구간_not_반등시도(self):
        """레이블이 '반등 시도'에서 '상승 여력 구간'으로 변경되었는지 확인"""
        with patch.dict(config.INDICATOR_PARAMS, {'SCORE_RSI_REBOUND': 40}):
            _, details = analysis.calculate_score(price=10000, rsi=45.0)
        detail_str = " ".join(details)
        assert "상승 여력 구간" in detail_str
        assert "반등 시도" not in detail_str


# ─────────────────────────────────────────────
# Issue 2: 역매수 수급 확인 조건 (OBV or SM)
# ─────────────────────────────────────────────

class TestMrBuyingConfirmation:
    """역매수 신호에 OBV/스마트머니 수급 확인 조건 추가 검증"""

    def test_mr_blocked_without_obv_or_sm(self, mr_base_args, yangbong_df):
        """OBV 미상승 + 스마트머니 미유입 → 역매수 차단 (데드캣 바운스 방어)"""
        state, _, _ = analysis.classify_stock_state(
            **mr_base_args, obv_trend=False, smart_money=False, df=yangbong_df
        )
        assert state != "역매수"

    def test_mr_fires_with_obv_trend_only(self, mr_base_args, yangbong_df):
        """OBV 상승 단독으로 역매수 발동"""
        state, _, _ = analysis.classify_stock_state(
            **mr_base_args, obv_trend=True, smart_money=False, df=yangbong_df
        )
        assert state == "역매수"

    def test_mr_fires_with_smart_money_only(self, mr_base_args, yangbong_df):
        """스마트머니 유입 단독으로 역매수 발동"""
        state, _, _ = analysis.classify_stock_state(
            **mr_base_args, obv_trend=False, smart_money=True, df=yangbong_df
        )
        assert state == "역매수"

    def test_mr_fires_with_both_obv_and_sm(self, mr_base_args, yangbong_df):
        """OBV + 스마트머니 동시 확인 시 역매수 발동"""
        state, _, _ = analysis.classify_stock_state(
            **mr_base_args, obv_trend=True, smart_money=True, df=yangbong_df
        )
        assert state == "역매수"

    def test_mr_reason_includes_수급확인(self, mr_base_args, yangbong_df):
        """역매수 발동 사유 메시지에 '수급 확인' 포함"""
        _, _, reason = analysis.classify_stock_state(
            **mr_base_args, obv_trend=True, smart_money=False, df=yangbong_df
        )
        assert "수급 확인" in reason

    # ----- 기존 필터 조건이 수급과 무관하게 차단하는지 교차 검증 -----

    def test_mr_blocked_rsi_falling_despite_obv(self, mr_base_args, yangbong_df):
        """RSI 하락 중이면 OBV가 있어도 역매수 차단"""
        args = {**mr_base_args, 'rsi': 30.0, 'prev_rsi': 35.0}
        state, _, _ = analysis.classify_stock_state(
            **args, obv_trend=True, smart_money=False, df=yangbong_df
        )
        assert state != "역매수"

    def test_mr_blocked_disparity_too_high_despite_obv(self, mr_base_args):
        """이격도 96% (기준 90% 초과) — OBV 있어도 역매수 차단"""
        df_high = pd.DataFrame({'close': [9600.0], 'open': [9400.0]})
        args = {**mr_base_args, 'price': 9600}
        state, _, _ = analysis.classify_stock_state(
            **args, obv_trend=True, smart_money=False, df=df_high
        )
        assert state != "역매수"

    def test_mr_blocked_rsi_above_mr_max_despite_obv(self, mr_base_args, yangbong_df):
        """RSI 41 > MR_RSI_MAX(40) — OBV 있어도 역매수 차단"""
        args = {**mr_base_args, 'rsi': 41.0, 'prev_rsi': 38.0}
        state, _, _ = analysis.classify_stock_state(
            **args, obv_trend=True, smart_money=False, df=yangbong_df
        )
        assert state != "역매수"

    def test_mr_blocked_no_yangbong_despite_obv(self, mr_base_args):
        """음봉(종가 < 시가) — OBV 있어도 역매수 차단"""
        df_eum = pd.DataFrame({'close': [8300.0], 'open': [8500.0]})
        state, _, _ = analysis.classify_stock_state(
            **mr_base_args, obv_trend=True, smart_money=False, df=df_eum
        )
        assert state != "역매수"

    def test_absolute_defense_overrides_mr_and_obv(self, mr_base_args, yangbong_df):
        """절대 방어 필터(ADX 45+, -DI 우위)는 OBV가 있어도 역매수보다 우선 적용"""
        args = {**mr_base_args, 'adx': 50, 'plus_di': 10.0, 'minus_di': 30.0}
        state, _, _ = analysis.classify_stock_state(
            **args, obv_trend=True, smart_money=False, df=yangbong_df
        )
        assert state == "매도"

    def test_mr_disabled_globally_ignores_obv(self, yangbong_df):
        """USE_MEAN_REVERSION=False 설정 시 역매수 경로 자체 비활성화"""
        thresholds = {"USE_MEAN_REVERSION": False, "MR_RSI_MAX": 40.0, "MR_DISPARITY_MAX": 90.0}
        state, _, _ = analysis.classify_stock_state(
            price=8500, ema20=10000, ema60=11000, ema120=12000,
            sar=9000, rsi=35.0, prev_rsi=30.0, adx=20, cci=-120,
            obv_trend=True, smart_money=False,
            thresholds=thresholds, df=yangbong_df
        )
        assert state != "역매수"
