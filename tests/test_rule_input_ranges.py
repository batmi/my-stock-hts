"""개별 종목 룰 입력에도 전역과 같은 범위 검증이 걸려야 한다.

2026-09-04 감사: 전역 설정은 2026-07-26 감사에서 두 겹으로 막혔다 —
범위 검증(settings._RANGE_RULES)과 잠금(settings.BACKTESTED_HIDDEN_KEYS).
그 잠금 주석에 당시 실측이 그대로 적혀 있다: "부호를 +로 잘못 넣어도 통과하던
자리다(실측: +5.0 입력 수용)".

그런데 **개별 종목 룰**은 같은 다이얼을 전역보다 우선해 덮어쓰면서
(engine.build_sell_thresholds) 검증이 하나도 없었다. 잠근 문 옆의 잠기지 않은 문이다.
"""
import pytest

from modules.auto_trade import engine, menu
from modules import settings


# --------------------------------------------------------------------------
# 결함 재현 — 왜 막아야 하는가
# --------------------------------------------------------------------------
def _rule(**over):
    base = {'code': '005930', 'use_atr_stop': 0, 'stop_loss': -7.0, 'take_profit': 0,
            'sell_score': 4.0, 'take_profit_rsi': 0, 'ts_activation': 10.0,
            'ts_callback': 5.0, 'time_stop_days': 15, 'buy_score': 7.0,
            'buy_rsi': 70.0, 'half_take_profit_use': 0, 'weights': None}
    base.update(over)
    return base


def test_positive_stop_loss_reaches_the_engine_when_atr_is_off():
    """룰이 ATR 손절을 끄면 부호 오입력이 그대로 판정에 들어간다."""
    th = engine.build_sell_thresholds(rule=_rule(stop_loss=7.0),
                                      buy_trades=[{'qty': 10, 'stop_loss_rate': -8.0}])
    assert th["STOP_LOSS_RATE"] == 7.0


def test_positive_stop_loss_also_reaches_it_without_buy_records():
    """HTS 직접 매수처럼 매수 기록이 없으면 ATR 손절이 켜져 있어도 룰 값이 그대로 쓰인다."""
    th = engine.build_sell_thresholds(rule=_rule(use_atr_stop=1, stop_loss=7.0), buy_trades=[])
    assert th["STOP_LOSS_RATE"] == 7.0


def test_atr_record_protects_the_normal_path():
    """대조군 — 매수 기록이 있고 ATR 손절이 켜져 있으면 기록값이 지킨다(이 경로는 안전했다)."""
    th = engine.build_sell_thresholds(rule=_rule(use_atr_stop=1, stop_loss=7.0),
                                      buy_trades=[{'qty': 10, 'stop_loss_rate': -8.0}])
    assert th["STOP_LOSS_RATE"] == -8.0


# --------------------------------------------------------------------------
# 범위 검증
# --------------------------------------------------------------------------
@pytest.mark.parametrize("key,bad", [
    ("stop_loss", 7.0),         # 부호 오입력 — 수익 포지션을 '손절'로 청산한다
    ("stop_loss", -80.0),
    ("sell_score", 99.0),       # 점수가 늘 그 아래라 보유 전 종목 즉시 청산
    ("sell_score", -1.0),
    ("buy_score", 0.0),         # 매수 게이트 소멸
    ("buy_score", 20.0),        # 도달 불가
    ("buy_rsi", 30.0),
    ("take_profit", -30.0),
    ("take_profit_rsi", 150.0),
    ("ts_activation", 999.0),   # 트레일링 사실상 비활성
    ("ts_activation", -5.0),
    ("ts_callback", -5.0),
    ("atr_stop_multiplier", -2.0),
    ("time_stop_days", -3),
])
def test_bad_values_are_rejected(key, bad):
    assert menu.rule_range_error(key, bad) is not None, f"{key}={bad} 가 통과했다"


@pytest.mark.parametrize("key,good", [
    ("stop_loss", -7.0), ("stop_loss", 0.0),
    ("sell_score", 4.0), ("sell_score", 0.0),
    ("buy_score", 7.0), ("buy_rsi", 70.0),
    ("take_profit", 0.0), ("take_profit", 30.0),
    ("take_profit_rsi", 0.0), ("take_profit_rsi", 75.0),
    ("ts_activation", 10.0), ("ts_callback", 3.5),
    ("atr_stop_multiplier", 2.0), ("atr_stop_multiplier", 0.0),
    ("time_stop_days", 15), ("time_stop_days", 0),
    ("buy_vol_strength", 100.0), ("buy_ask_bid_ratio", 1.2),
])
def test_normal_tuning_is_not_blocked(key, good):
    assert menu.rule_range_error(key, good) is None, f"{key}={good} 가 막혔다"


def test_existing_value_is_not_trapped():
    """옛 설정이 범위 밖이면 Enter 만 눌러도 거부돼 메뉴를 못 빠져나간다."""
    assert menu.rule_range_error("stop_loss", 7.0) is not None
    assert menu.rule_range_error("stop_loss", 7.0, current=7.0) is None


def test_unknown_key_passes_through():
    assert menu.rule_range_error("memo", "아무 말") is None
    assert menu.rule_range_error("weights", None) is None


def test_non_numeric_is_not_a_range_error():
    assert menu.rule_range_error("stop_loss", "abc") is None


# --------------------------------------------------------------------------
# 두 문이 다시 갈라지지 않게
# --------------------------------------------------------------------------
_SHARED = {
    "buy_score": "BUY_SCORE",
    "buy_rsi": "BUY_RSI_MAX",
    "buy_vol_strength": "BUY_VOL_STRENGTH",
    "buy_ask_bid_ratio": "BUY_ASK_BID_RATIO",
    "ts_activation": "TRAILING_STOP_ACTIVATION_RATE",
}


@pytest.mark.parametrize("rule_key,cfg_key", sorted(_SHARED.items()))
def test_shared_dials_use_the_same_bounds(rule_key, cfg_key):
    """전역과 개별 룰이 같은 다이얼이면 폭도 같아야 한다 — 한쪽만 조이면 우회로가 된다."""
    lo, hi, _allow_zero, _why = menu._RULE_RANGES[rule_key]
    g_lo, g_hi, _ = settings._RANGE_RULES[cfg_key]
    assert (lo, hi) == (g_lo, g_hi), f"{rule_key} 와 {cfg_key} 의 허용 폭이 다르다"


def test_every_overriding_dial_has_a_range():
    """build_sell_thresholds 가 전역을 덮어쓰는 룰 필드는 전부 범위가 있어야 한다."""
    import inspect
    import re

    src = inspect.getsource(engine.build_sell_thresholds)
    overriding = set(re.findall(r"_rv\('(\w+)'", src)) | set(re.findall(r"rule_value\(rule, '(\w+)'", src))
    #  숫자가 아닌 것·구조적인 것은 범위 대상이 아니다.
    exempt = {"weights", "half_take_profit_use", "use_atr_stop"}
    missing = sorted(k for k in overriding - exempt if k not in menu._RULE_RANGES)
    assert not missing, f"전역을 덮어쓰는데 범위가 없는 룰 필드: {missing}"


def test_rule_input_loops_until_valid():
    """ask_val 이 잘못된 값에서 그냥 통과하지 않고 다시 묻는지 — 소스로 고정한다."""
    import inspect

    src = inspect.getsource(menu._input_and_save_rule)
    assert "rule_range_error(" in src, "룰 입력이 범위 검증을 거치지 않는다"
    assert "while True:" in src, "범위를 벗어나면 다시 물어야 한다"
