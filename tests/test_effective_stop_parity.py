"""'손절선'이 경보·취소 경로와 매도 엔진에서 같은 값인가.

[왜] 손절률의 수량가중평균이 세 곳에 복제돼 있었다.

  ① engine.build_sell_thresholds  — 실제 매도 판정이 쓰는 값(SSOT)
  ② trader._effective_stop_loss_rate — '손절 보호'(미체결 매수 즉시 취소)
  ③ trader._alert_unmanaged_stop 안의 인라인 복사본 — 미관리 포지션 이탈 경보

②③은 ①이 보는 것을 보지 않았다: ATR 손절 사용 여부, 개별 룰의 손절 조이기, 매수
기록이 없는 포지션의 ATR 복원(fallback). 그래서 시스템이 손절해 주지 않는 포지션의
**마지막 안전망**인 경보와, 손절 상황에서 미체결 매수를 거둬들이는 보호 장치가 매도
엔진과 다른 선을 보고 판단했다 — 조용히 늦거나 조용히 빨라지는 종류의 어긋남이다.

게다가 ②의 가중평균 루프는 테스트가 한 번도 밟은 적이 없었다(전체 스위트 기준).
분할 매수·피라미딩으로 매수 기록이 여러 건인 포지션에서만 의미가 생기는 코드인데,
정작 그 상황이 검증되지 않았다.

지금은 ②가 ①에 위임하고 ③은 ②를 부른다. 이 파일은 그 위임이 유지되는지를
'값 자체'로 고정한다 — 다시 복제되면 격자에서 갈라진다.
"""
import pytest
from unittest.mock import patch

import config
from modules.auto_trade import AutoTrader, engine


ATR_ON = {"USE_ATR_STOP": True, "STOP_LOSS_RATE": -7.0}
ATR_OFF = {"USE_ATR_STOP": False, "STOP_LOSS_RATE": -7.0}


@pytest.fixture
def trader():
    AutoTrader._instance = None
    return AutoTrader()


def _trades(*pairs):
    """(수량, 손절률) 목록을 매수 기록 형태로."""
    return [{'qty': q, 'stop_loss_rate': s} for q, s in pairs]


# ───────────────────────── 값 자체 ─────────────────────────

def test_weighted_average_across_split_buys(trader):
    """[핵심] 분할 매수·피라미딩 포지션의 손절선은 수량가중평균이다.

    10주 -5% + 30주 -9% → (10×-5 + 30×-9) / 40 = -8.0
    (종전에는 이 경로를 밟는 테스트가 하나도 없었다)
    """
    with patch.dict(config.SELL_STRATEGY, ATR_ON):
        assert trader._effective_stop_loss_rate(_trades((10, -5.0), (30, -9.0))) == pytest.approx(-8.0)


def test_records_without_a_rate_do_not_drag_the_average(trader):
    """손절률이 없는(0/None) 기록은 분모에서도 빠져야 한다 — 0으로 세면 선이 얕아진다."""
    with patch.dict(config.SELL_STRATEGY, ATR_ON):
        got = trader._effective_stop_loss_rate(
            _trades((10, -8.0), (30, 0.0)) + [{'qty': 5, 'stop_loss_rate': None}])
    assert got == pytest.approx(-8.0)


def test_a_tighter_rule_wins(trader):
    """개별 룰이 더 타이트하면 경보·취소도 그 선을 봐야 한다(매도 엔진과 같은 규약)."""
    with patch.dict(config.SELL_STRATEGY, ATR_ON):
        got = trader._effective_stop_loss_rate(_trades((10, -8.0)), rule={'stop_loss': -3.0})
    assert got == pytest.approx(-3.0)


def test_a_wider_rule_is_refused(trader):
    """룰이 더 넓으면 거부한다 — 포지션 크기가 기록값을 전제로 계산돼 있다."""
    with patch.dict(config.SELL_STRATEGY, ATR_ON):
        got = trader._effective_stop_loss_rate(_trades((10, -8.0)), rule={'stop_loss': -12.0})
    assert got == pytest.approx(-8.0)


def test_atr_stop_off_ignores_the_recorded_rate(trader):
    """[핵심] ATR 손절을 끄면 기록값이 아니라 고정 손절률이 실제 판정선이다.

    복제본은 이 설정을 보지 않아, 매도 엔진은 -7%로 판정하는데 경보는 -12%(기록값)를
    보는 식으로 갈렸다.
    """
    with patch.dict(config.SELL_STRATEGY, ATR_OFF):
        assert trader._effective_stop_loss_rate(_trades((10, -12.0))) == pytest.approx(-7.0)


def test_no_records_falls_back_to_the_restored_atr_rate(trader):
    """HTS 직접 매수처럼 기록이 없는 포지션 — 매도 엔진은 진입 봉에서 복원한 값을 쓴다."""
    with patch.dict(config.SELL_STRATEGY, ATR_ON):
        assert trader._effective_stop_loss_rate([], fallback_atr_rate=-11.0) == pytest.approx(-11.0)


def test_stop_disabled_reports_none(trader):
    """고정 손절 0(미사용) + 기록 없음 = 기준 자체가 없다 → None(경보·취소 모두 보류)."""
    with patch.dict(config.SELL_STRATEGY, {"USE_ATR_STOP": False, "STOP_LOSS_RATE": 0}):
        assert trader._effective_stop_loss_rate([]) is None


# ───────────────────── 매도 엔진과의 대조(격자) ─────────────────────

@pytest.mark.parametrize("sell_cfg", [ATR_ON, ATR_OFF], ids=["atr_on", "atr_off"])
@pytest.mark.parametrize("rule", [None, {'stop_loss': -3.0}, {'stop_loss': -12.0}],
                         ids=["no_rule", "tight_rule", "wide_rule"])
@pytest.mark.parametrize("trades", [[], _trades((10, -8.0)), _trades((10, -5.0), (30, -9.0))],
                         ids=["no_trades", "one_trade", "split_buys"])
@pytest.mark.parametrize("fallback", [None, -11.0], ids=["no_fallback", "fallback"])
def test_it_always_equals_what_the_sell_engine_uses(trader, sell_cfg, rule, trades, fallback):
    """[핵심] 어떤 조합에서도 경보·취소가 보는 선 == 매도 판정이 받는 선.

    (36개 조합. 다시 복제하면 여기서 갈라진다)
    """
    with patch.dict(config.SELL_STRATEGY, sell_cfg):
        got = trader._effective_stop_loss_rate(trades, rule=rule, fallback_atr_rate=fallback)
        thr = engine.build_sell_thresholds(rule=rule, buy_trades=trades,
                                           fallback_atr_rate=fallback)
    engine_sl = thr.get("STOP_LOSS_RATE", config.SELL_STRATEGY["STOP_LOSS_RATE"])
    expected = engine_sl if engine_sl < 0 else None
    assert got == (pytest.approx(expected) if expected is not None else None)
