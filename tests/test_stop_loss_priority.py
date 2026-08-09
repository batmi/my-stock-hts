"""손절률 우선순위와 '가동 중 설정 변경'이 보유 포지션에 미치는 범위를 고정한다.

[배경] 포지션 크기는 진입 시점의 손절폭을 전제로 계산된다(리스크 = 크기 × 손절폭).
따라서 사후에 손절을 **넓히면** 그 포지션의 실제 손실이 사이징이 가정한 상한을 넘어
자본대비 리스크 한도가 명목만 남는다. 반대로 **조이는** 것은 손실 상한이 줄어들 뿐이라
한도를 깨지 않는다.

그래서 개별 룰의 stop_loss는 '기록값보다 타이트할 때만' 적용한다. 종전에는 ATR 기록값이
룰을 무조건 덮어써서 운용자가 룰로 손절을 조여도 조용히 무시됐다(룰에서 use_atr_stop을
함께 꺼야만 반영 — 발견하기 어렵다). docstring은 '개별 룰 최우선'이라 코드와 반대였다.
"""
import pytest

import config
from modules.auto_trade import engine

ENTRY_TRADE = [{'qty': '10', 'stop_loss_rate': -12.0}]   # 진입 시 ATR 손절 -12%


@pytest.fixture(autouse=True)
def base_config(monkeypatch):
    ss = dict(config.SELL_STRATEGY)
    ss.update({"USE_ATR_STOP": True, "STOP_LOSS_RATE": -7.0})
    monkeypatch.setattr(config, 'SELL_STRATEGY', ss, raising=False)
    yield ss


def _sl(**kw):
    return engine.build_sell_thresholds(**kw).get("STOP_LOSS_RATE")


def test_recorded_atr_stop_is_used_without_a_rule():
    assert _sl(buy_trades=ENTRY_TRADE) == -12.0


def test_a_tighter_rule_stop_is_honoured():
    """운용자가 그 종목만 빨리 자르겠다고 지정하면 받아들인다."""
    assert _sl(rule={'stop_loss': -5.0}, buy_trades=ENTRY_TRADE) == -5.0


def test_a_wider_rule_stop_is_refused():
    """넓히는 지시는 거부한다 — 사이징이 가정한 손실 상한을 넘게 된다."""
    assert _sl(rule={'stop_loss': -20.0}, buy_trades=ENTRY_TRADE) == -12.0, (
        "룰이 손절을 넓히는 것을 허용하면 그 포지션의 실제 손실이 진입 시 계산한 "
        "리스크 한도를 초과한다")


def test_equal_rule_stop_changes_nothing():
    assert _sl(rule={'stop_loss': -12.0}, buy_trades=ENTRY_TRADE) == -12.0


def test_rule_applies_when_atr_stop_is_off_in_the_rule():
    """룰이 ATR 손절을 끄면 기록값을 쓰지 않으므로 룰 값이 그대로 쓰인다."""
    assert _sl(rule={'stop_loss': -5.0, 'use_atr_stop': False}, buy_trades=ENTRY_TRADE) == -5.0


def test_no_buy_record_uses_the_rule_stop(base_config):
    """매수 기록이 없으면(HTS 직매수) 룰의 손절이 그대로 쓰인다."""
    assert _sl(rule={'stop_loss': -5.0}, buy_trades=[]) == -5.0


def test_without_rule_or_record_the_key_is_left_to_the_global_fallback(base_config):
    """룰도 기록도 없으면 임계값 dict에 키를 넣지 않는다.

    analyze_sell이 thresholds.get("STOP_LOSS_RATE", config.SELL_STRATEGY[...])로
    전역값을 읽는 구조라, 여기서 굳이 복사해 두면 전역 변경이 반영되는 시점이
    두 곳으로 갈린다. 키 부재가 곧 '전역을 따른다'는 뜻이다.
    """
    assert _sl(buy_trades=[]) is None
    th = engine.build_sell_thresholds(buy_trades=[])
    assert th.get("STOP_LOSS_RATE", base_config["STOP_LOSS_RATE"]) == -7.0


# ─────────────── 가동 중 설정 변경의 반영 범위 ───────────────

def test_global_stop_change_does_not_move_an_open_position(base_config):
    """전역 손절률을 바꿔도 이미 보유한 포지션의 손절은 진입 시 값 그대로다.

    포지션 크기가 그 값을 전제로 계산됐기 때문이다. 운용자가 '전역을 조였는데 왜
    그대로냐'고 오해할 수 있는 지점이라 동작을 명시적으로 고정해 둔다.
    (그 종목만 조이려면 개별 룰의 stop_loss를 쓴다 — 위 테스트 참조)
    """
    base_config["STOP_LOSS_RATE"] = -3.0
    assert _sl(buy_trades=ENTRY_TRADE) == -12.0


def test_time_stop_change_applies_to_open_positions_immediately(base_config):
    """시간 청산일 등 나머지 임계값은 다음 주기부터 즉시 반영된다."""
    base_config["TIME_STOP_DAYS"] = 3
    th = engine.build_sell_thresholds(buy_trades=ENTRY_TRADE)
    assert th["TIME_STOP_DAYS"] == 3
