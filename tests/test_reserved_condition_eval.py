"""예약 주문 발동 조건 평가 — 실주문을 내보내는 판정이 실제로 맞는가.

[왜 이 파일인가] 예약 주문의 발동은 곧 **실주문**이다(시드를 쓰거나 포지션을 청산한다).
그런데 조건 평가기(`_eval_atomic`)는 전체 스위트에서 39%만 실행됐고, 조건 8종 가운데
대부분이 한 번도 평가되지 않았다(2026-08-30 커버리지 실측). 경계 하나가 뒤집혀도
(>= vs >, 오늘 봉 포함 여부, 매수/매도 방향) 붉어지지 않는 상태였다.

[구조] 종전에는 같은 조건이 두 벌로 평가됐다 — 단일 예약(SCORE/RSI/EMA)은
`_check_orders` 안의 인라인 비교, 복합(COMPOSITE) 서브조건은 `_eval_atomic`.
지금은 단일도 `_eval_atomic`을 부른다. 여기서 고정하는 것은 그 하나의 평가기다.

[fail-closed] 지표·차트를 못 구했으면 발동시키지 않는다. 예약 주문에서 '모름'을
'충족'으로 읽으면 조용한 오발주가 된다.
"""
import json

import numpy as np
import pandas as pd
import pytest
from unittest.mock import patch

from core import indicators
from modules.reserved_order_monitor import ReservedOrderMonitor


@pytest.fixture
def monitor():
    m = ReservedOrderMonitor()
    m.is_running = False
    m.chart_cache = {}
    return m


def _df(n=200, drift=0.001, last_high=None):
    dates = pd.date_range("2024-01-01", periods=n).strftime("%Y%m%d")
    close = 1000 * np.exp(np.arange(n) * drift)
    df = pd.DataFrame({"date": dates, "close": close, "open": close,
                       "high": close * 1.01, "low": close * 0.99, "volume": 1000})
    if last_high is not None:
        df.loc[df.index[-2], 'high'] = last_high      # 직전 봉 고가를 못 박는다
    return df


def _ev(monitor, ctype, value, ctx):
    """평가 결과를 파이썬 bool 로 정규화한다.

    비교 대상이 numpy 스칼라(판다스 열에서 온 고가·EMA)면 numpy.bool_ 이 돌아온다.
    호출부는 if 로만 쓰므로 동작에 차이가 없지만, 테스트에서 `is True` 로 못 박으려면
    정규화가 필요하다.
    """
    return bool(monitor._eval_atomic(ctype, value, ctx))


def _ctx(monitor=None, price=1200.0, df=None, with_ind=True, order_type='buy', now="1200"):
    df = _df() if df is None else df
    ind = indicators.calculate_indicators(df) if with_ind else None
    return {'curr_price': price, 'df': df, 'ind': ind, 'code': '005930',
            'is_overseas': False, 'now_hhmm': now, 'order_type': order_type}


# ───────────────────────── 가격·시각 (지표 불필요) ─────────────────────────

@pytest.mark.parametrize("ctype,value,price,expected", [
    ('PRICE_UP', 1000, 1000, True),     # 경계 포함
    ('PRICE_UP', 1000, 999, False),
    ('PRICE_DOWN', 1000, 1000, True),   # 경계 포함
    ('PRICE_DOWN', 1000, 1001, False),
])
def test_price_conditions(monitor, ctype, value, price, expected):
    assert _ev(monitor, ctype, value, _ctx(price=price)) is expected


@pytest.mark.parametrize("now,value,expected", [
    ("1500", "1500", True),    # 경계 포함
    ("1459", "1500", False),
    ("1501", "1500", True),
])
def test_time_after(monitor, now, value, expected):
    assert _ev(monitor, 'TIME_AFTER', value, _ctx(now=now)) is expected


def test_time_after_requires_zero_padded_hhmm(monitor):
    """[규약 고정] 시각 비교는 문자열이다 — 등록이 HHMM 4자리를 보장해야 한다.

    '930'처럼 앞자리 0이 빠지면 '0930' >= '930' 이 False라 **영원히 발동하지 않는다**.
    등록 화면이 4자리를 강제하므로 지금은 안전하지만, 그 검증이 느슨해지면 조용히
    죽는 조건이 되므로 의존 관계를 여기 남긴다.
    """
    assert _ev(monitor, 'TIME_AFTER', "930", _ctx(now="0930")) is False
    assert _ev(monitor, 'TIME_AFTER', "0930", _ctx(now="0930")) is True


def test_time_after_without_a_value_never_fires(monitor):
    assert _ev(monitor, 'TIME_AFTER', None, _ctx()) is False


# ───────────────────────── 신고가 ─────────────────────────

def test_new_high_ignores_todays_own_bar(monitor):
    """오늘 봉을 기준에 넣으면 자기 자신을 넘어야 하는 모순이 된다 — 직전까지와 비교한다."""
    df = _df(last_high=10_000)                       # 직전 봉 고가 10,000
    assert _ev(monitor, 'NEW_HIGH', 20, _ctx(price=9_999, df=df)) is False
    assert _ev(monitor, 'NEW_HIGH', 20, _ctx(price=10_000, df=df)) is True


def test_new_high_lookback_window_matters(monitor):
    """룩백 밖의 옛 고가는 기준이 아니다(52주 신고가와 사상 최고가의 차이)."""
    df = _df()
    df.loc[df.index[5], 'high'] = 1e6                # 아주 오래전 초고가
    price = float(df['high'].iloc[-30:-1].max()) + 1
    assert _ev(monitor, 'NEW_HIGH', 20, _ctx(price=price, df=df)) is True
    assert _ev(monitor, 'NEW_HIGH', 0, _ctx(price=price, df=df)) is False   # 사상 기준


def test_new_high_needs_enough_bars(monitor):
    assert _ev(monitor, 'NEW_HIGH', 20, _ctx(df=_df(n=10))) is False


# ───────────────────────── 지표 조건 ─────────────────────────

def test_indicator_conditions_are_fail_closed_without_indicators(monitor):
    """[핵심] 차트를 못 구하면 어떤 지표 조건도 발동하지 않는다."""
    ctx = _ctx(with_ind=False)
    for ctype, value in [('STATE', '강매수'), ('SCORE_UP', 0), ('SCORE_DOWN', 99),
                         ('RSI_UP', 0), ('RSI_DOWN', 99), ('EMA_UP', 20),
                         ('EMA_DOWN', 20), ('ATR_BREAKOUT', 0.1)]:
        assert _ev(monitor, ctype, value, ctx) is False, f"{ctype}이 지표 없이 발동했다"


@pytest.mark.parametrize("ctype,rsi,value,expected", [
    ('RSI_UP', 70.0, 70, True), ('RSI_UP', 69.9, 70, False),
    ('RSI_DOWN', 30.0, 30, True), ('RSI_DOWN', 30.1, 30, False),
])
def test_rsi_conditions(monitor, ctype, rsi, value, expected):
    ctx = _ctx()
    ctx['ind']['rsi'] = rsi
    assert _ev(monitor, ctype, value, ctx) is expected


def test_rsi_unknown_is_not_a_trigger(monitor):
    ctx = _ctx()
    ctx['ind']['rsi'] = None
    assert _ev(monitor, 'RSI_UP', 0, ctx) is False


@pytest.mark.parametrize("period", [5, 20, 60, 120])
def test_ema_keys_exist_for_every_selectable_period(monitor, period):
    """등록 화면이 고르게 하는 기간(5·20·60·120)의 지표 키가 실제로 있어야 한다.

    키 이름이 바뀌면 조건이 '조용히 영영 발동하지 않는' 상태가 된다.
    """
    ctx = _ctx()
    assert ctx['ind'].get(f'ema_{period}') is not None
    ev = ctx['ind'][f'ema_{period}']
    assert _ev(monitor, 'EMA_UP', period, _ctx(price=ev + 1)) is True
    assert _ev(monitor, 'EMA_DOWN', period, _ctx(price=ev - 1)) is True


def test_ema_with_an_unknown_period_does_not_fire(monitor):
    assert _ev(monitor, 'EMA_UP', 7, _ctx()) is False


def test_atr_breakout_direction_depends_on_the_order_side(monitor):
    """매수는 전일 종가 + ATR×k 상향, 매도는 − ATR×k 하향이다 — 방향이 뒤집히면 정반대 주문이 나간다."""
    df = _df()
    ind = indicators.calculate_indicators(df)
    prev_close = float(df['close'].iloc[-2])
    atr = ind['atr']
    up, down = prev_close + atr * 1.0, prev_close - atr * 1.0

    assert _ev(monitor, 'ATR_BREAKOUT', 1.0, _ctx(price=up, df=df)) is True
    assert _ev(monitor, 'ATR_BREAKOUT', 1.0, _ctx(price=up - 1, df=df)) is False
    assert _ev(monitor, 'ATR_BREAKOUT', 1.0,
                                _ctx(price=down, df=df, order_type='sell')) is True
    assert _ev(monitor, 'ATR_BREAKOUT', 1.0,
                                _ctx(price=down + 1, df=df, order_type='sell')) is False


def test_atr_breakout_without_a_multiplier_does_not_fire(monitor):
    assert _ev(monitor, 'ATR_BREAKOUT', 0, _ctx()) is False


def test_score_and_state_use_the_shared_engines(monitor):
    """점수·상태는 자동매매와 같은 엔진을 쓴다(예약만 다른 기준으로 판단하면 안 된다)."""
    ctx = _ctx()
    with patch('modules.reserved_order_monitor.analysis.check_smart_money_turnaround',
               return_value=(False, "")), \
         patch('modules.reserved_order_monitor.analysis.calculate_score',
               return_value=(8.4, {})) as score, \
         patch('modules.reserved_order_monitor.analysis.classify_stock_state',
               return_value=("강매수", "", {})):
        assert _ev(monitor, 'SCORE_UP', 8.0, ctx) is True
        assert _ev(monitor, 'SCORE_DOWN', 8.0, ctx) is False
        assert _ev(monitor, 'STATE', '강매수', ctx) is True
        assert _ev(monitor, 'STATE', '매수', ctx) is False
    assert score.call_count == 1, "같은 주기에 점수를 다시 계산했다(레이트리밋 낭비)"


def test_smart_money_is_cached_within_one_evaluation(monitor):
    ctx = _ctx()
    with patch('modules.reserved_order_monitor.analysis.check_smart_money_turnaround',
               return_value=(True, "")) as sm:
        assert _ev(monitor, 'SMART_MONEY', None, ctx) is True
        assert _ev(monitor, 'SMART_MONEY', None, ctx) is True
    assert sm.call_count == 1


def test_an_unknown_condition_never_fires(monitor):
    """모르는 조건은 발동하지 않는다 — 저장된 조건이 코드보다 새로워도 오발주가 없다."""
    assert _ev(monitor, 'SOMETHING_NEW', 1, _ctx()) is False


# ───────────────────────── 복합(AND) ─────────────────────────

def _order(subs):
    return {'composite_json': json.dumps(subs), 'code': '005930'}


def test_composite_requires_every_sub_condition(monitor):
    ctx = _ctx(price=1200)
    ok, reason = monitor._eval_composite(
        _order([{'type': 'PRICE_UP', 'value': 1000},
                {'type': 'TIME_AFTER', 'value': '1100'}]), ctx)
    assert ok and "복합조건 충족" in reason and "AND" in reason

    ok, _ = monitor._eval_composite(
        _order([{'type': 'PRICE_UP', 'value': 1000},
                {'type': 'TIME_AFTER', 'value': '1300'}]), ctx)
    assert ok is False, "서브조건 하나가 미충족인데 발동했다"


def test_composite_with_an_unknown_sub_condition_does_not_fire(monitor):
    ok, _ = monitor._eval_composite(
        _order([{'type': 'PRICE_UP', 'value': 1}, {'type': 'NOPE', 'value': 1}]), _ctx())
    assert ok is False


@pytest.mark.parametrize("raw", [None, "", "[]", "{not json}"])
def test_composite_without_usable_conditions_does_not_fire(monitor, raw):
    ok, _ = monitor._eval_composite({'composite_json': raw, 'code': '005930'}, _ctx())
    assert ok is False
