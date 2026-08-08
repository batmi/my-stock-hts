"""설정 가드레일 테스트 (2026-07-26 설정 감사 후속).

감사 배경: 편집 가능 71개 항목 중 범위 검증이 걸린 것은 SYSTEM_INVEST_PER_STOCK 하나뿐이라
SYSTEM_RISK_PER_TRADE=50, BUY_RSI_MAX=0, STOP_LOSS_RATE=+5 같은 값이 전부 수용됐다.
30종목×3년 실측으로 해롭거나 무효인 다이얼은 봉인하고, 남는 항목에 범위·조합 검증을 붙였다.
"""
import pytest

import config
from modules import settings as S


def _editable():
    builders = [S._entry_strategy_items, S._sell_strategy_items, S._indicator_items,
                S._risk_portfolio_items, S._trading_cycle_items]
    return {it['name']: it for b in builders for it in b()}


# ==========================================================
# D. 봉인 — 실측상 추세추종을 훼손하거나 무효인 다이얼
# ==========================================================

NEWLY_SEALED = [
    "USE_ATR_STOP",                 # 끄면 >50% 대박 12→4건 (fat-tail 3분의 1 절단)
    "ATR_STOP_MULTIPLIER",          # 2.0→1.0에서 PF 1.74→1.60, MDD 악화
    "MAX_ATR_STOP_LOSS_RATE",       # -15→-1%에서 거래 2배 churn, PF 1.91→1.56
    "STOP_LOSS_RATE",               # ATR 잠금 후엔 폴백 전용 (부호 오입력 통로)
    "TRAILING_STOP_CALLBACK_RATE",  # 죽은 다이얼 — 5→2%로 낮춰도 거래 486건 불변
    "SUPER_MOMENTUM_USE",           # BUY_RSI_MAX와 조합 시 매매 0건
    "TS_ACTIVATION_MODE",           # fixed로 되돌리면 41종목 중 40개가 매수가 아래에서 무장
]


@pytest.mark.parametrize("key", NEWLY_SEALED)
def test_key_is_sealed(key):
    assert key in S.ANTI_TREND_HIDDEN_KEYS, f"{key} 봉인이 풀렸습니다"


@pytest.mark.parametrize("key", NEWLY_SEALED)
def test_sealed_key_not_editable(key):
    assert key not in _editable(), f"{key}가 편집 목록에 노출돼 있습니다"


def test_no_hidden_key_leaks_into_editable():
    """봉인 키가 어떤 편집 경로로도 새어나오지 않아야 한다."""
    sealed = S.ANTI_TREND_HIDDEN_KEYS | S.BACKTESTED_HIDDEN_KEYS
    assert not (sealed & set(_editable()))


def test_sealed_values_remain_at_verified_defaults():
    """봉인은 '현재 값을 고정'하는 것이므로 검증된 기본값이어야 의미가 있다."""
    assert config.SELL_STRATEGY["USE_ATR_STOP"] is True
    assert config.SELL_STRATEGY["ATR_STOP_MULTIPLIER"] == 2.0
    assert config.SELL_STRATEGY["MAX_ATR_STOP_LOSS_RATE"] == -15.0
    assert config.ANALYSIS_THRESHOLDS["SUPER_MOMENTUM_USE"] is True
    assert config.SELL_STRATEGY["TS_ACTIVATION_MODE"] == "breakeven"


def test_ts_activation_rate_editable_only_when_it_governs(monkeypatch):
    """TS 발동 수익률은 fixed일 때만 편집 목록에 있다.

    breakeven에서는 ATR 산출 실패 시의 폴백일 뿐인데 편집 가능하게 두면 화면의 값과
    실제 발동선이 어긋난다(STOP_LOSS_RATE가 같은 이유로 봉인됐다).
    """
    monkeypatch.setitem(config.SELL_STRATEGY, "TS_ACTIVATION_MODE", "breakeven")
    assert "TRAILING_STOP_ACTIVATION_RATE" in S.anti_trend_hidden_keys()
    assert "TRAILING_STOP_ACTIVATION_RATE" not in _editable()

    monkeypatch.setitem(config.SELL_STRATEGY, "TS_ACTIVATION_MODE", "fixed")
    assert "TRAILING_STOP_ACTIVATION_RATE" not in S.anti_trend_hidden_keys()
    assert "TRAILING_STOP_ACTIVATION_RATE" in _editable()


# ==========================================================
# A. 범위 검증
# ==========================================================

@pytest.mark.parametrize("name,bad", [
    ("SYSTEM_RISK_PER_TRADE", 50.0),        # 감사 시 실제로 수용되던 값
    ("SYSTEM_MAX_PORTFOLIO_RISK", 100.0),
    ("SYSTEM_MAX_HOLDINGS", 50),
    ("BUY_SCORE", 0.0),
    ("BUY_RSI_MAX", 0.0),
    ("BUY_RSI_MAX", 50.0),                  # 수익 반토막 구간
    ("TRAILING_STOP_ACTIVATION_RATE", 999.0),
    ("RSI_PERIOD", 1),
    ("MACD_FAST", 0),
    ("CHART_LOOKBACK_DAYS", 100),           # 250봉 미달
])
def test_out_of_range_rejected(name, bad):
    assert S._range_error(name, bad) is not None, f"{name}={bad} 가 통과했습니다"


@pytest.mark.parametrize("name,good", [
    ("SYSTEM_RISK_PER_TRADE", 4.0),
    ("SYSTEM_MAX_PORTFOLIO_RISK", 10.0),
    ("SYSTEM_MAX_HOLDINGS", 4),
    ("BUY_SCORE", 7.0),
    ("BUY_RSI_MAX", 70.0),
    ("TRAILING_STOP_ACTIVATION_RATE", 10.0),
    ("RSI_PERIOD", 14),
    ("MACD_FAST", 12),
    ("CHART_LOOKBACK_DAYS", 730),
])
def test_current_defaults_pass(name, good):
    assert S._range_error(name, good) is None, f"기본값 {name}={good} 이 거부됐습니다"


def test_every_numeric_editable_item_has_a_range():
    """숫자형 편집 항목은 전부 중앙 규칙표나 자체 validator 중 하나를 가져야 한다."""
    missing = [n for n, it in _editable().items()
               if it.get('type') in ('int', 'float')
               and n not in S._RANGE_RULES and 'validator' not in it]
    assert not missing, f"범위 검증이 없는 숫자 항목: {missing}"


def test_range_error_message_names_the_bound():
    msg = S._range_error("SYSTEM_RISK_PER_TRADE", 50.0)
    assert "SYSTEM_RISK_PER_TRADE" in msg and "0 ~ 10" in msg


def test_unknown_key_is_not_blocked():
    assert S._range_error("SOME_UNKNOWN_KEY", 999999) is None


# ==========================================================
# B. 조합(교차) 검증
# ==========================================================

@pytest.fixture
def restore_config():
    t = dict(config.ANALYSIS_THRESHOLDS)
    s = dict(config.SELL_STRATEGY)
    i = dict(config.INDICATOR_PARAMS)
    yield
    config.ANALYSIS_THRESHOLDS.clear(); config.ANALYSIS_THRESHOLDS.update(t)
    config.SELL_STRATEGY.clear(); config.SELL_STRATEGY.update(s)
    config.INDICATOR_PARAMS.clear(); config.INDICATOR_PARAMS.update(i)


def test_shipped_defaults_have_no_conflicts(restore_config, monkeypatch):
    """출하 기본값 조합은 경고가 없어야 한다.

    주변 상태(다른 테스트가 남긴 전역 설정)에 기대지 않도록 config.py 정본 기본값을
    명시적으로 세운 뒤 검사한다 — xdist 병렬 실행에서 전역 dict 오염에 흔들리지 않게.
    """
    config.ANALYSIS_THRESHOLDS.update({
        "BUY_SCORE": 7.0, "RISE_SCORE": 6.0, "BUY_RSI_MAX": 70.0,
        "SUPER_MOMENTUM_USE": True, "SUPER_MOMENTUM_SCORE": 8.0,
        "DISPARITY_UPPER": 110.0, "DISPARITY_LOWER": 90.0,
    })
    config.SELL_STRATEGY.update({
        "TRAILING_STOP_ACTIVATION_RATE": 10.0, "USE_ATR_STOP": True,
    })
    config.INDICATOR_PARAMS.update({
        "MACD_FAST": 12, "MACD_SLOW": 26,
        "RSI_LOWER": 30, "RSI_MID": 50, "RSI_UPPER": 70,
        "CCI_LOWER": -100, "CCI_UPPER": 100,
        "CHART_LOOKBACK_DAYS": 730,
    })
    monkeypatch.setattr(config.settings, "SYSTEM_RISK_PER_TRADE", 4.0, raising=False)
    monkeypatch.setattr(config.settings, "SYSTEM_MAX_PORTFOLIO_RISK", 10.0, raising=False)
    monkeypatch.setattr(config.settings, "SYSTEM_MAX_HOLDINGS", 4, raising=False)

    assert S.check_config_conflicts() == []


def test_zero_trade_combo_is_detected(restore_config):
    """폐지된 횡보 프리셋의 자기모순 — 개별 값은 정상 범위라 조합으로만 잡힌다."""
    config.ANALYSIS_THRESHOLDS["BUY_RSI_MAX"] = 50.0
    config.ANALYSIS_THRESHOLDS["SUPER_MOMENTUM_USE"] = False
    warns = S.check_config_conflicts()
    assert any("0건" in w for w in warns), warns


def test_macd_inversion_detected(restore_config):
    config.INDICATOR_PARAMS["MACD_FAST"] = 26
    config.INDICATOR_PARAMS["MACD_SLOW"] = 12
    assert any("MACD" in w for w in S.check_config_conflicts())


def test_rsi_band_order_detected(restore_config):
    config.INDICATOR_PARAMS["RSI_UPPER"] = 20
    assert any("RSI" in w for w in S.check_config_conflicts())


def test_cci_band_order_detected(restore_config):
    config.INDICATOR_PARAMS["CCI_LOWER"] = 200
    assert any("CCI" in w for w in S.check_config_conflicts())


def test_no_exit_path_detected(restore_config):
    config.SELL_STRATEGY["TRAILING_STOP_ACTIVATION_RATE"] = 999.0
    config.SELL_STRATEGY["USE_ATR_STOP"] = False
    assert any("청산 수단" in w for w in S.check_config_conflicts())


def test_risk_exceeds_portfolio_limit_detected(restore_config, monkeypatch):
    monkeypatch.setattr(config.settings, "SYSTEM_RISK_PER_TRADE", 20.0, raising=False)
    monkeypatch.setattr(config.settings, "SYSTEM_MAX_PORTFOLIO_RISK", 10.0, raising=False)
    assert any("총 오픈 리스크" in w for w in S.check_config_conflicts())


def test_conflict_check_never_raises(restore_config):
    """설정이 어떤 쓰레기 값이어도 점검 자체가 죽으면 안 된다."""
    config.ANALYSIS_THRESHOLDS["BUY_RSI_MAX"] = None
    config.INDICATOR_PARAMS["MACD_FAST"] = None
    try:
        S.check_config_conflicts()
    except Exception as e:
        pytest.fail(f"조합 점검이 예외를 던졌습니다: {e}")


# ==========================================================
# C. 죽은 다이얼 — 실제 규칙을 화면에 드러내는지
# ==========================================================

def test_ts_callback_display_states_effective_rule():
    """TS 하락 감지율은 하한일 뿐이므로 'max(설정값, ATR×배수)'가 화면에 보여야 한다."""
    import inspect
    src = inspect.getsource(S.view_system_config)
    assert "실효 콜백 = max(" in src


def test_atr_stop_display_survives_sealing():
    """봉인해도 '무엇으로 도는지'는 읽기 전용으로 계속 보여야 한다."""
    import inspect
    src = inspect.getsource(S.view_system_config)
    assert "ATR 손절 (변동성 기반)" in src
    assert "조정 잠금" in src
