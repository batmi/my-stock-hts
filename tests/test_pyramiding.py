from modules.auto_trade import DefaultStrategy
import config
import pytest
from unittest.mock import patch


@pytest.fixture
def strategy():
    return DefaultStrategy()


PYRAMID_ON = {
    "PYRAMIDING_USE": True,
    "PYRAMIDING_PROFIT_TRIGGER": 10.0,
    "PYRAMIDING_RATIO": 0.5,
    "PYRAMIDING_MAX_COUNT": 1,
}


def test_pyramid_triggers_on_profit_and_buy_state(strategy):
    """수익률 트리거 이상 + 매수 신호 유지 시 증액"""
    with patch.dict(config.ANALYSIS_THRESHOLDS, PYRAMID_ON):
        ok, reason = strategy.analyze_pyramid(profit_rate=12.0, state="매수", score=8.0, pyramid_count=0)
        assert ok is True
        assert "피라미딩 1차" in reason


def test_pyramid_blocked_below_trigger(strategy):
    """수익률이 트리거 미만이면 증액하지 않음 (손실 종목은 절대 증액 금지 = 물타기 방지)"""
    with patch.dict(config.ANALYSIS_THRESHOLDS, PYRAMID_ON):
        ok, _ = strategy.analyze_pyramid(profit_rate=9.9, state="매수", score=8.0, pyramid_count=0)
        assert ok is False
        ok, _ = strategy.analyze_pyramid(profit_rate=-5.0, state="매수", score=8.0, pyramid_count=0)
        assert ok is False


def test_pyramid_blocked_without_buy_state(strategy):
    """추세 유지(매수/강매수) 상태가 아니면 수익 중이어도 증액하지 않음"""
    with patch.dict(config.ANALYSIS_THRESHOLDS, PYRAMID_ON):
        for state in ["상승", "관망", "관심", "매도"]:
            ok, _ = strategy.analyze_pyramid(profit_rate=15.0, state=state, score=7.0, pyramid_count=0)
            assert ok is False, f"state={state}에서 증액되면 안 됨"


def test_pyramid_max_count_limit(strategy):
    """최대 증액 횟수 도달 시 추가 증액 금지"""
    with patch.dict(config.ANALYSIS_THRESHOLDS, PYRAMID_ON):
        ok, _ = strategy.analyze_pyramid(profit_rate=20.0, state="강매수", score=9.0, pyramid_count=1)
        assert ok is False


def test_pyramid_disabled(strategy):
    """PYRAMIDING_USE=False면 어떤 조건에서도 증액하지 않음"""
    with patch.dict(config.ANALYSIS_THRESHOLDS, {**PYRAMID_ON, "PYRAMIDING_USE": False}):
        ok, _ = strategy.analyze_pyramid(profit_rate=20.0, state="강매수", score=9.0, pyramid_count=0)
        assert ok is False


def test_pyramid_count_in_reason(strategy):
    """MAX_COUNT를 늘리면 차수가 사유에 누적 표기됨 (DB 사유 파싱으로 횟수 추적)"""
    with patch.dict(config.ANALYSIS_THRESHOLDS, {**PYRAMID_ON, "PYRAMIDING_MAX_COUNT": 2}):
        ok, reason = strategy.analyze_pyramid(profit_rate=25.0, state="강매수", score=9.0, pyramid_count=1)
        assert ok is True
        assert "피라미딩 2차" in reason
