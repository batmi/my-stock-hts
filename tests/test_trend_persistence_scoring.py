"""
추세 지속 이력 가점 + 다중 기간 모멘텀 정합 게이트 테스트.

변경 사항 (Trend 팩터 총점 4.0 유지):
  1. MA 군집 상한 2.5 → 2.0, 절감분 0.5는 '추세 지속 이력' 가점으로 이관
     - 최근 TREND_PERSIST_LOOKBACK(120)일 중 종가>60일선 비율 ≥ TREND_PERSIST_MIN(70%) → +0.5
     - 갓 골든크로스한 미검증 추세와 장기 지속 추세를 점수로 구분
  2. 가격 모멘텀(6개월+52주 위치) 가점에 다중 기간 정합 게이트
     - 1개월(21일)·3개월(63일) 수익률이 명시적으로 음수면 가점 보류 (식어가는 추세 차단)
     - 단기 수익률 미상(None/NaN)이면 게이트 미적용 (fail-open, 기존 호출자 호환)
"""

import numpy as np
import pandas as pd
import pytest

from modules import analysis


def base_kwargs(**over):
    """스코어링 호출 공통 인자 (스칼라 경로) — 필요한 항목만 덮어쓴다."""
    kw = dict(price=100.0, ema20=90.0, ema60=80.0, ema120=70.0, ema_5=95.0)
    kw.update(over)
    return kw


def score_of(**over):
    score, details = analysis.calculate_score(**base_kwargs(**over))
    return score, details


# ─────────────────────────────────────────────
# 1. 추세 지속 이력 가점 & MA 상한 축소
# ─────────────────────────────────────────────

class TestTrendPersistence:
    def test_persist_bonus_granted(self):
        s_with, d_with = score_of(trend_persist=90.0)
        s_without, _ = score_of(trend_persist=None)
        assert s_with == pytest.approx(s_without + 0.5)
        assert any("추세 지속" in d for d in d_with)

    def test_persist_below_threshold_no_bonus(self):
        s_low, d_low = score_of(trend_persist=50.0)
        s_none, _ = score_of(trend_persist=None)
        assert s_low == s_none
        assert not any("추세 지속" in d for d in d_low)

    def test_persist_nan_no_bonus(self):
        s_nan, _ = score_of(trend_persist=float('nan'))
        s_none, _ = score_of(trend_persist=None)
        assert s_nan == s_none

    def test_ma_cluster_cap_is_2_0(self):
        """정배열 만점 종목도 MA 군집 합산은 2.0으로 캡 (지속 이력 없이는 TREND 만점 불가)"""
        _, details = score_of()
        cap_lines = [d for d in details if "[상한]" in d]
        assert cap_lines and "2.00" in cap_lines[0]

    def test_full_trend_score_requires_persistence(self):
        """MA 만점 + MACD/SAR 만점 + 지속 이력까지 갖추면 TREND 4.0 회복"""
        common = dict(sar=95.0, macd=1.0, macd_signal=0.5, macd_hist=0.5, prev_macd_hist=0.0)
        s_persist, _ = score_of(trend_persist=90.0, **common)
        s_no_persist, _ = score_of(trend_persist=None, **common)
        assert s_persist - s_no_persist == pytest.approx(0.5)

    def test_df_path_computes_persistence(self):
        """df 경로: 장기 상승(120일 이상 60일선 위) 종목은 지속 가점을 자동 산출"""
        n = 300
        closes = 100 * np.exp(0.002 * np.arange(n))
        df = pd.DataFrame({
            'date': [f"2025{i:04d}" for i in range(n)],
            'open': closes, 'high': closes * 1.01, 'low': closes * 0.99,
            'close': closes, 'volume': [1000.0] * n,
        })
        ind = {'ema_20': closes[-1] * 0.98, 'ema_60': closes[-1] * 0.95, 'ema_120': closes[-1] * 0.9,
               'psar': None, 'rsi': None, 'adx': None, 'cci': None, 'obv_trend': False,
               'macd': None, 'macd_signal': None, 'plus_di': None, 'minus_di': None}
        _, details = analysis.calculate_score(df=df, ind=ind)
        assert any("추세 지속" in d for d in details)


# ─────────────────────────────────────────────
# 2. 다중 기간 모멘텀 정합 게이트
# ─────────────────────────────────────────────

class TestMomentumAlignment:
    MOM = dict(mom_ret=30.0, w52_pos=90.0)

    def test_aligned_momentum_grants_bonus(self):
        s_all, d_all = score_of(mom_ret_1m=5.0, mom_ret_3m=10.0, **self.MOM)
        assert any(d.startswith("가격 모멘텀:") for d in d_all)

    def test_negative_1m_blocks_bonus(self):
        s_neg, d_neg = score_of(mom_ret_1m=-3.0, mom_ret_3m=10.0, **self.MOM)
        s_pos, _ = score_of(mom_ret_1m=5.0, mom_ret_3m=10.0, **self.MOM)
        assert s_pos - s_neg == pytest.approx(0.5)
        assert any("가격 모멘텀 보류" in d for d in d_neg)

    def test_negative_3m_blocks_bonus(self):
        s_neg, d_neg = score_of(mom_ret_1m=5.0, mom_ret_3m=-1.0, **self.MOM)
        assert any("가격 모멘텀 보류" in d for d in d_neg)
        assert not any(d.startswith("가격 모멘텀:") for d in d_neg)

    def test_unknown_short_momentum_fail_open(self):
        """단기 수익률 미상(None) 시 기존 동작 유지 — 가점 부여"""
        _, d = score_of(mom_ret_1m=None, mom_ret_3m=None, **self.MOM)
        assert any(d2.startswith("가격 모멘텀:") for d2 in d)

    def test_nan_short_momentum_fail_open(self):
        _, d = score_of(mom_ret_1m=float('nan'), mom_ret_3m=float('nan'), **self.MOM)
        assert any(d2.startswith("가격 모멘텀:") for d2 in d)

    def test_df_path_blocks_cooling_trend(self):
        """df 경로: 6개월 누적은 양수지만 최근 1개월 급락한 '식어가는 추세'는 가점 보류"""
        n = 300
        closes = 100 * np.exp(0.004 * np.arange(n))  # 강한 상승
        closes[-21:] = closes[-22] * np.exp(-0.01 * np.arange(1, 22))  # 최근 1개월 급락
        df = pd.DataFrame({
            'date': [f"2025{i:04d}" for i in range(n)],
            'open': closes, 'high': closes * 1.01, 'low': closes * 0.99,
            'close': closes, 'volume': [1000.0] * n,
        })
        price = float(closes[-1])
        ind = {'ema_20': price * 0.98, 'ema_60': price * 0.95, 'ema_120': price * 0.9,
               'psar': None, 'rsi': None, 'adx': None, 'cci': None, 'obv_trend': False,
               'macd': None, 'macd_signal': None, 'plus_di': None, 'minus_di': None}
        _, details = analysis.calculate_score(df=df, ind=ind, w52_pos=85.0)
        assert not any(d.startswith("가격 모멘텀:") for d in details)


# ─────────────────────────────────────────────
# 3. 백테스트 사전계산 컬럼 패리티
# ─────────────────────────────────────────────

def test_backtest_precompute_columns():
    from modules import backtest as bt
    n = 300
    closes = 100 * np.exp(0.002 * np.arange(n))
    df = pd.DataFrame({
        'date': [f"2025{i:04d}" for i in range(n)],
        'open': closes, 'high': closes * 1.01, 'low': closes * 0.99,
        'close': closes, 'volume': [1000.0] * n,
    })
    out = bt.compute_price_indicators(df.copy())
    for col in ['MOM_RET_1M', 'MOM_RET_3M', 'TREND_PERSIST']:
        assert col in out.columns, f"{col} 사전계산 누락"
    # 상시 상승 종목: 말미 행에서 지속 비율 100%, 단기 모멘텀 양수
    assert out['TREND_PERSIST'].iloc[-1] == pytest.approx(100.0)
    assert out['MOM_RET_1M'].iloc[-1] > 0
    # 워밍업 구간(120일 미만)은 NaN → 가점 비활성
    assert pd.isna(out['TREND_PERSIST'].iloc[50])
