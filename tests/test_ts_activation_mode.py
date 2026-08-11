"""TS 발동 방식(손익분기 연동) 회귀 테스트.

[왜 이렇게 정했나] 고정 +10%는 종목 변동성을 무시한다. 관심종목 41개 기준 ATR%가
2.39~7.08%로 3배 차이라, 같은 '+10%'가 KT에겐 2.09R·현대오토에버에겐 0.71R을 뜻한다.
10년(2016-08~2026-08) 실측에서 고정 상향(20·40%)은 2년씩 5구간 중 최소 2구간에서
현행보다 나빴고 40%는 약세 구간에서 TS이익비중이 0%까지 무너졌다. 손익분기 연동만
5구간 어디서도 지지 않았다(config.TRAILING_STOP_ACTIVATION_RATE 주석 참조).

[여기서 지키는 것]
  1) 발동선은 '되돌림 한 번(ATR×배수)을 맞고도 본전'인 MFE — 매수가 기준으로 환산한다.
     고점 기준으로 환산하면 고점이 오를수록 문턱이 낮아져(자기참조) 사실상 고정 10%와
     같아진다. 실측에서 구간5 수익승이 23/30 → 14/30으로 무너진 실패 모드다.
  2) 실매매(engine)와 백테스트(portfolio_backtest·backtest)가 같은 식을 쓴다.
"""
import os
import sys

import pytest

sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import config
from modules.auto_trade.engine import (breakeven_activation_rate, compute_trailing_stop,
                                       ts_activation_atr_mult,
                                       ts_activation_label)


# 발동선은 콜백 하한·ATR 배수·ATR 손절 사용 여부에 함께 의존한다. config.SELL_STRATEGY는
#  전역 공유 객체라 다른 테스트가 이 값들을 바꿔둔 채 넘어오면 여기 기대값이 깨진다
#  (실제로 TRAILING_ATR_MULTIPLIER가 바뀐 상태에서 무장 판정이 뒤집혔다).
#  → 계산에 쓰이는 값을 픽스처에서 전부 고정한다. 의존 관계를 문서화하는 효과도 있다.
_TS_ENV = {
    "TRAILING_STOP_CALLBACK_RATE": 5.0,
    "TRAILING_ATR_MULTIPLIER": 3.5,
    "TRAILING_STOP_ACTIVATION_RATE": 10.0,
    "USE_ATR_STOP": True,
    "TS_MAX_GIVEBACK_RATIO": 0.0,
    # 2026-08-11 분리: 발동선은 이 배수를, 콜백은 위 TRAILING_ATR_MULTIPLIER를 쓴다.
    "TS_ACTIVATION_ATR_MULTIPLIER": 3.0,
    "TS_ACTIVATION_MAX_RATE": 25.0,
}


def _pin(mode):
    saved = {k: config.SELL_STRATEGY.get(k) for k in (*_TS_ENV, "TS_ACTIVATION_MODE")}
    config.SELL_STRATEGY.update(_TS_ENV)
    config.SELL_STRATEGY["TS_ACTIVATION_MODE"] = mode
    return saved


def _restore(saved):
    for k, v in saved.items():
        if v is None:
            config.SELL_STRATEGY.pop(k, None)
        else:
            config.SELL_STRATEGY[k] = v


@pytest.fixture
def breakeven_mode():
    saved = _pin("breakeven")
    yield
    _restore(saved)


@pytest.fixture
def no_act_cap():
    """발동선 상한 캡을 끈 상태. 캡에 가려지는 산식 성질을 검증할 때 쓴다."""
    saved = config.SELL_STRATEGY.get("TS_ACTIVATION_MAX_RATE")
    config.SELL_STRATEGY["TS_ACTIVATION_MAX_RATE"] = 0.0
    yield
    config.SELL_STRATEGY["TS_ACTIVATION_MAX_RATE"] = saved


@pytest.fixture
def fixed_mode():
    saved = _pin("fixed")
    yield
    _restore(saved)


@pytest.fixture(autouse=True)
def _pin_for_pure_functions(request):
    """픽스처를 쓰지 않는 순수 함수 테스트에도 같은 환경을 보장한다."""
    if "breakeven_mode" in request.fixturenames or "fixed_mode" in request.fixturenames:
        yield
        return
    saved = {k: config.SELL_STRATEGY.get(k) for k in _TS_ENV}
    config.SELL_STRATEGY.update(_TS_ENV)
    yield
    _restore(saved)


# ------------------------------------------------------------------ 발동선 산식

def test_발동선은_변동성이_클수록_높다(no_act_cap):
    """같은 규칙이 저변동주는 일찍·고변동주는 늦게 무장시킨다.

    상한 캡은 이 성질을 가리므로(고변동이 캡에 붙어 평평해진다) 여기서는 꺼두고 잰다 —
    캡 자체는 아래 test_발동선_상한_캡이 따로 검증한다.
    """
    buy = 100_000
    mult = ts_activation_atr_mult()
    low = breakeven_activation_rate(2.39 / 100 * buy, buy)     # KT급
    mid = breakeven_activation_rate(4.56 / 100 * buy, buy)     # 중앙값
    high = breakeven_activation_rate(7.08 / 100 * buy, buy)    # 현대오토에버급
    assert low < mid < high
    # 값 자체는 발동 전용 배수에 비례한다 — 배수를 바꾸면 같이 움직여야 정상이다.
    for atr_pct, got in ((2.39, low), (4.56, mid), (7.08, high)):
        cb = atr_pct / 100 * mult
        assert got == pytest.approx(cb / (1 - cb) * 100, rel=1e-9)


def test_발동선은_되돌림_한_번을_맞고도_본전인_지점이다(no_act_cap):
    """정의 그대로: 고점 × (1 - 콜백) ≥ 매수가 가 되는 최소 MFE.

    [주의] 여기서 쓰는 '콜백'은 청산선 폭(TRAILING_ATR_MULTIPLIER)이 아니라 **발동 전용
    배수**다. 2026-08-11에 두 축을 분리했다 — 발동만 앞당기고 청산선 폭은 유지하기 위해서다.
    """
    buy, atr = 100_000, 4_560.0
    cb = atr * ts_activation_atr_mult() / buy   # 매수가 기준 되돌림 폭
    act = breakeven_activation_rate(atr, buy)
    high = buy * (1 + act / 100)
    assert high * (1 - cb) == pytest.approx(buy, rel=1e-9)


def test_발동선_상한_캡(monkeypatch):
    """캡은 고ATR 종목만 자르고 평시 종목은 건드리지 않는다."""
    buy = 100_000
    monkeypatch.setitem(config.SELL_STRATEGY, "TS_ACTIVATION_MAX_RATE", 25.0)
    assert breakeven_activation_rate(9.52 / 100 * buy, buy) == pytest.approx(25.0)
    low = breakeven_activation_rate(2.39 / 100 * buy, buy)
    assert low < 25.0                       # 저변동 종목은 캡에 닿지 않는다
    monkeypatch.setitem(config.SELL_STRATEGY, "TS_ACTIVATION_MAX_RATE", 0.0)
    assert breakeven_activation_rate(2.39 / 100 * buy, buy) == pytest.approx(low)  # 해제 시 불변


def test_발동_배수는_콜백_배수와_분리된다(monkeypatch):
    """[회귀 방지] 콜백 배수를 움직여도 발동선은 따라가지 않아야 한다.

    둘이 한 키로 묶여 있던 시절에는 '발동을 앞당기자'가 곧 '청산선을 좁히자'가 되어
    fat-tail이 무너졌다(수익승 0/15). 분리가 풀리면 그 실패로 되돌아간다.
    """
    buy, atr = 100_000, 4_560.0
    monkeypatch.setitem(config.SELL_STRATEGY, "TS_ACTIVATION_MAX_RATE", 0.0)
    monkeypatch.setitem(config.SELL_STRATEGY, "TS_ACTIVATION_ATR_MULTIPLIER", 3.0)
    base = breakeven_activation_rate(atr, buy)
    monkeypatch.setitem(config.SELL_STRATEGY, "TRAILING_ATR_MULTIPLIER", 5.0)
    assert breakeven_activation_rate(atr, buy) == pytest.approx(base)
    # 미설정(0)이면 종전처럼 콜백 배수를 따른다(되돌리기 경로)
    monkeypatch.setitem(config.SELL_STRATEGY, "TS_ACTIVATION_ATR_MULTIPLIER", 0.0)
    assert breakeven_activation_rate(atr, buy) > base


def test_발동선은_고점이_올라도_움직이지_않는다():
    """[실패 모드 차단] 고점 기준으로 환산하면 문턱이 따라 내려가 무장이 즉시 이뤄진다."""
    buy, atr = 100_000, 4_560.0
    base = breakeven_activation_rate(atr, buy)
    # 고점이 얼마나 올랐든 발동선은 매수가와 ATR만의 함수여야 한다
    assert breakeven_activation_rate(atr, buy) == base


def test_ATR_없으면_콜백_하한으로_계산한다():
    """ATR 미확보 시에도 고정 %로 되돌아가지 않는다."""
    cb = config.SELL_STRATEGY.get("TRAILING_STOP_CALLBACK_RATE", 5.0)
    act = breakeven_activation_rate(0, 100_000)
    assert act == pytest.approx(cb / (100 - cb) * 100)


def test_매수가가_없으면_고정값으로_되돌린다():
    fixed = config.SELL_STRATEGY.get("TRAILING_STOP_ACTIVATION_RATE", 10.0)
    assert breakeven_activation_rate(1000, 0) == fixed
    assert breakeven_activation_rate(1000, None) == fixed


# ------------------------------------------------------------------ 무장 판정

def test_손실_구간에서는_무장하지_않는다(breakeven_mode):
    """현행 고정 10%는 청산선이 매수가보다 아래인 상태로 무장한다 — 그 상태를 없앤다."""
    ind = {"atr": 4_560.0}
    r = compute_trailing_stop(highest_price=110_000, buy_price=100_000,
                              current_price=110_000, ind=ind)
    assert r["armed"] is False
    assert r["stop_price"] < 100_000          # 무장했다면 손실 구간에서 털렸을 자리

    r = compute_trailing_stop(highest_price=120_000, buy_price=100_000,
                              current_price=120_000, ind=ind)
    assert r["armed"] is True
    assert r["stop_price"] > 100_000          # 무장 시점엔 이미 본전 위


def test_고정_방식은_종전대로_동작한다(fixed_mode):
    ind = {"atr": 4_560.0}
    r = compute_trailing_stop(highest_price=110_000, buy_price=100_000,
                              current_price=110_000, ind=ind)
    assert r["armed"] is True                 # +10% 도달 → 무장
    assert r["activation"] == config.SELL_STRATEGY["TRAILING_STOP_ACTIVATION_RATE"]


def test_청산_판정은_무장_이후에만_난다(breakeven_mode):
    ind = {"atr": 4_560.0}
    # 아직 무장 전이면 아무리 떨어져도 트레일링으로는 팔지 않는다(손절이 담당)
    r = compute_trailing_stop(highest_price=110_000, buy_price=100_000,
                              current_price=80_000, ind=ind)
    assert r["armed"] is False and r["triggered"] is False


# ------------------------------------------------------------------ 표시

def test_표시_문구가_방식을_따른다(breakeven_mode):
    assert ts_activation_label() == "손익분기"
    assert ts_activation_label(19.0) == "손익분기(≈+19.0%)"


def test_표시_문구는_고정_방식에서_수치를_쓴다(fixed_mode):
    assert ts_activation_label() == f"+{config.SELL_STRATEGY['TRAILING_STOP_ACTIVATION_RATE']}%"


# ------------------------------------------------------------------ 경로 정합성

def test_백테스트가_실매매와_같은_발동선을_쓴다(breakeven_mode):
    """[SSOT] 두 구현이 어긋나면 백테스트 수치가 실매매를 설명하지 못한다."""
    from modules.portfolio_backtest import decide_sell

    buy, atr = 100_000.0, 4_560.0
    act = breakeven_activation_rate(atr, buy)
    cfg = {"use_atr": True, "use_time_stop": False, "ts_act": 10.0,
           "ts_callback": config.SELL_STRATEGY["TRAILING_STOP_CALLBACK_RATE"],
           "ts_atr_mult": config.SELL_STRATEGY["TRAILING_ATR_MULTIPLIER"],
           "ts_breakeven": True, "sell_score_limit": 0.0}

    # 무장 직전(MFE < 발동선): 큰 폭으로 밀려도 트레일링 청산이 아니다
    high = buy * (1 + (act - 2) / 100)
    sell, reason = decide_sell(price=high * 0.7, high=high, avg=buy, sl_rate=0,
                               atr_applied=False, is_bep=False, holding_days=1,
                               state="매수", state_reason="", raw_score=9.0, sell_check=9.0,
                               ema60=0, atr=atr, cfg=cfg)
    assert reason != "트레일링스탑"

    # 무장 이후 + 콜백 초과: 트레일링 청산
    high = buy * (1 + (act + 5) / 100)
    sell, reason = decide_sell(price=high * 0.7, high=high, avg=buy, sl_rate=0,
                               atr_applied=False, is_bep=False, holding_days=1,
                               state="매수", state_reason="", raw_score=9.0, sell_check=9.0,
                               ema60=0, atr=atr, cfg=cfg)
    assert sell and reason == "트레일링스탑"


def test_기본값은_손익분기_연동이다():
    """10년 실측으로 채택한 기본값 — 되돌리려면 근거가 필요하다.

    실행 중 바뀐 전역값이 아니라 클래스 기본값을 본다(다른 테스트의 오염과 무관하게).
    """
    defaults = config.GlobalSettings().SELL_STRATEGY
    assert defaults.get("TS_ACTIVATION_MODE") == "breakeven"


# ------------------------------------------------- 이익 보호선 (기본 OFF)

def test_profit_lock_only_above_min_mfe():
    """MIN_MFE 아래에서는 아예 선이 생기지 않는다 — BEP와 갈리는 지점이다.

    본전 청산은 낮은 MFE에서 손절선을 끌어올려 눌림에 털렸다(2026-08-04 실측으로 OFF).
    이 선이 같은 실수를 반복하지 않으려면 문턱 아래에서 None이어야 한다.
    """
    from modules.auto_trade.engine import profit_lock_stop_rate

    assert profit_lock_stop_rate(24.9, 25.0, 0.5) is None
    assert profit_lock_stop_rate(0, 25.0, 0.5) is None
    assert profit_lock_stop_rate(-10, 25.0, 0.5) is None
    assert profit_lock_stop_rate(25.0, 25.0, 0.5) == pytest.approx(12.5)


def test_profit_lock_always_leaves_room_to_run():
    """켜진 뒤에도 최고 이익의 giveback만큼은 계속 내준다 (추세에 남기는 여유)."""
    from modules.auto_trade.engine import profit_lock_stop_rate

    for mfe in (30.0, 50.0, 108.0):
        rate = profit_lock_stop_rate(mfe, 25.0, 0.5)
        assert rate == pytest.approx(mfe * 0.5)
        assert 0 < rate < mfe, "보호선이 고점에 붙으면 추세를 끊는다"

    # 반납 비율이 클수록 선이 낮다 = 더 많이 내준다
    assert (profit_lock_stop_rate(50.0, 25.0, 0.65)
            < profit_lock_stop_rate(50.0, 25.0, 0.35))


def test_profit_lock_is_off_by_default():
    """실측에서 전 조합이 열위였다 (config.SELL_STRATEGY 주석 참조). 기본은 OFF여야 한다."""
    assert config.SELL_STRATEGY.get("PROFIT_LOCK_USE") is False


def test_backtest_and_live_share_the_profit_lock_formula():
    """백테스트가 실매매와 다른 식을 쓰면 이 축으로 정한 파라미터의 근거가 무너진다."""
    from modules.auto_trade.engine import profit_lock_stop_rate
    from modules.portfolio_backtest import decide_sell

    cfg = {"profit_lock_use": True, "profit_lock_min_mfe": 25.0,
           "profit_lock_giveback": 0.5, "ts_act": 999.0}   # TS는 무장 못 하게 막아둔다
    avg, high = 100.0, 160.0
    lock = profit_lock_stop_rate((high - avg) / avg * 100, 25.0, 0.5)   # +30%

    kw = dict(high=high, avg=avg, sl_rate=-15.0, atr_applied=True, is_bep=False,
              holding_days=5, state="상승", state_reason="", raw_score=6.0,
              sell_check=False, ema60=90.0, atr=3.0, cfg=cfg)
    assert decide_sell(price=avg * (1 + lock / 100) + 0.5, **kw) == (False, "")
    assert decide_sell(price=avg * (1 + lock / 100) - 0.5, **kw) == (True, "이익보호")
