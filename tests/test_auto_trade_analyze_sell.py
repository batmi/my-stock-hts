"""DefaultStrategy.analyze_sell 의 매도 판정 분기 커버리지 테스트.

기존 테스트는 df=None 으로만 호출해 df 블록(지표 기반 분기)이 미커버였다.
실제 df + 지표/상태 mock으로 추세이탈·RSI과열·방어적반매도·시간청산 경로를 검증한다.
"""
import pytest
from unittest.mock import patch
import numpy as np
import pandas as pd

from modules.auto_trade import DefaultStrategy
import config


@pytest.fixture(autouse=True)
def _restore_sell_strategy():
    """[격리] 이 파일은 config.SELL_STRATEGY(모듈 전역)를 직접 덮어쓴다.

    되돌리지 않으면 같은 워커에서 뒤에 도는 테스트가 오염된 청산 다이얼을 본다 —
    실제로 test_exit_parity 가 TIME_STOP_* 잔재 때문에 8건 실패했다(2026-08-26 확인).
    """
    import copy
    saved = copy.deepcopy(config.SELL_STRATEGY)
    yield
    config.SELL_STRATEGY.clear()
    config.SELL_STRATEGY.update(saved)


@pytest.fixture
def strategy():
    return DefaultStrategy()


@pytest.fixture
def df_up():
    """우상향 60일 차트(지표 계산용)."""
    n = 60
    close = np.linspace(9000, 11000, n)
    return pd.DataFrame({
        'date': pd.date_range('2024-01-01', periods=n),
        'open': close * 0.99,
        'high': close * 1.02,
        'low': close * 0.98,
        'close': close,
        'volume': np.random.randint(1000, 5000, n),
    })


# analyze_sell이 ind.get(...)으로 참조하는 키들의 기본 지표값
def _ind(**over):
    base = {'rsi': 50.0, 'adx': 30.0, 'cci': 0.0, 'psar': 9000.0,
            'ema_5': 9500.0, 'ema_20': 9800.0, 'atr': 100.0}
    base.update(over)
    return base


@patch('modules.auto_trade.analysis.check_smart_money_turnaround', return_value=(False, ""))
@patch('modules.auto_trade.analysis.calculate_score')
@patch('modules.auto_trade.analysis.classify_stock_state')
@patch('modules.auto_trade.indicators.calculate_indicators')
def test_analyze_sell_trend_break_state(mock_ind, mock_cls, mock_score, mock_sm, strategy, df_up):
    """추세 붕괴(state='매도') 시 '매도진입' 사유로 전량 매도한다."""
    mock_ind.return_value = _ind()
    mock_cls.return_value = ("매도", "", "추세붕괴")
    mock_score.return_value = (2.0, [])

    res = strategy.analyze_sell(
        "005930", "삼성전자", df_up, current_price=10000, buy_price=10000, profit_rate=0.0,
        thresholds={"TAKE_PROFIT_RATE": 50.0, "STOP_LOSS_RATE": -20.0, "SELL_SCORE": 3.0},
    )
    assert res['action'] == 'sell'
    assert '매도진입' in res['reason']


@patch('modules.auto_trade.analysis.check_smart_money_turnaround', return_value=(False, ""))
@patch('modules.auto_trade.analysis.calculate_score')
@patch('modules.auto_trade.analysis.classify_stock_state')
@patch('modules.auto_trade.indicators.calculate_indicators')
def test_analyze_sell_trend_break_score(mock_ind, mock_cls, mock_score, mock_sm, strategy, df_up):
    """상태는 보유이나 점수가 매도 기준 미만이면 '추세이탈'로 매도한다."""
    mock_ind.return_value = _ind()
    mock_cls.return_value = ("보유", "", "")
    mock_score.return_value = (2.0, [])  # SELL_SCORE(3.0) 미만

    res = strategy.analyze_sell(
        "005930", "삼성전자", df_up, current_price=10000, buy_price=10000, profit_rate=0.0,
        thresholds={"TAKE_PROFIT_RATE": 50.0, "STOP_LOSS_RATE": -20.0, "SELL_SCORE": 3.0},
    )
    assert res['action'] == 'sell'
    assert '추세이탈' in res['reason']


@patch('modules.auto_trade.analysis.check_smart_money_turnaround', return_value=(False, ""))
@patch('modules.auto_trade.analysis.calculate_score')
@patch('modules.auto_trade.analysis.classify_stock_state')
@patch('modules.auto_trade.indicators.calculate_indicators')
def test_analyze_sell_rsi_overheated(mock_ind, mock_cls, mock_score, mock_sm, strategy, df_up):
    """RSI가 과열 기준을 초과하면 'RSI과열'로 익절 매도한다."""
    mock_ind.return_value = _ind(rsi=90.0)
    mock_cls.return_value = ("보유", "", "")
    mock_score.return_value = (9.0, [])  # 추세이탈/방어 회피용 고점수

    res = strategy.analyze_sell(
        "005930", "삼성전자", df_up, current_price=10000, buy_price=10000, profit_rate=0.0,
        thresholds={"TAKE_PROFIT_RATE": 50.0, "STOP_LOSS_RATE": -20.0,
                    "TAKE_PROFIT_RSI": 75.0, "SELL_SCORE": 3.0},
    )
    assert res['action'] == 'sell'
    assert 'RSI과열' in res['reason']


@patch('modules.auto_trade.analysis.check_smart_money_turnaround', return_value=(False, ""))
@patch('modules.auto_trade.analysis.calculate_score')
@patch('modules.auto_trade.analysis.classify_stock_state')
@patch('modules.auto_trade.indicators.calculate_indicators')
def test_analyze_sell_defensive_half(mock_ind, mock_cls, mock_score, mock_sm, strategy, df_up):
    """수익 구간에서 하락 반전(PSAR·EMA5 이탈) 시 방어적 반매도(50%)한다."""
    config.SELL_STRATEGY["DEFENSIVE_HALF_SELL_USE"] = True
    # 현재가가 psar/ema_5보다 낮아 하락 반전으로 판정되도록 지표 설정
    mock_ind.return_value = _ind(rsi=50.0, psar=11000.0, ema_5=11000.0)
    mock_cls.return_value = ("보유", "", "")
    mock_score.return_value = (9.0, [])

    res = strategy.analyze_sell(
        "005930", "삼성전자", df_up, current_price=10000, buy_price=9500, profit_rate=5.0,
        thresholds={"TAKE_PROFIT_RATE": 50.0, "STOP_LOSS_RATE": -20.0, "SELL_SCORE": 3.0},
        already_half_sold=False,
    )
    assert res['action'] == 'sell'
    assert res['sell_ratio'] == 0.5
    assert '하락반전' in res['reason']


@patch('modules.auto_trade.analysis.check_smart_money_turnaround', return_value=(False, ""))
@patch('modules.auto_trade.analysis.calculate_score')
@patch('modules.auto_trade.analysis.classify_stock_state')
@patch('modules.auto_trade.indicators.calculate_indicators')
def test_analyze_sell_time_stop(mock_ind, mock_cls, mock_score, mock_sm, strategy, df_up):
    """보유 기간 초과 + 수익 미달 시 '시간청산'으로 매도한다."""
    config.SELL_STRATEGY["TIME_STOP_USE"] = True
    config.SELL_STRATEGY["TIME_STOP_MIN_PROFIT_RATE"] = 3.0
    mock_ind.return_value = _ind()
    mock_cls.return_value = ("보유", "", "")  # 매수 상태가 아니어야 유예 없이 청산
    mock_score.return_value = (9.0, [])

    res = strategy.analyze_sell(
        "005930", "삼성전자", df_up, current_price=10000, buy_price=10000, profit_rate=0.0,
        thresholds={"TAKE_PROFIT_RATE": 50.0, "STOP_LOSS_RATE": -20.0,
                    "SELL_SCORE": 3.0, "TIME_STOP_DAYS": 10},
        holding_days=15,
    )
    assert res['action'] == 'sell'
    assert '시간청산' in res['reason']


@patch('modules.auto_trade.analysis.check_smart_money_turnaround', return_value=(False, ""))
@patch('modules.auto_trade.analysis.calculate_score')
@patch('modules.auto_trade.analysis.classify_stock_state')
@patch('modules.auto_trade.indicators.calculate_indicators')
def test_analyze_sell_break_even(mock_ind, mock_cls, mock_score, mock_sm, strategy, df_up):
    """고점에서 BEP 활성화 기준을 넘긴 뒤 본전 부근으로 밀리면 '본전청산'한다.

    [2026-08-04] BEP는 기본 OFF가 됐다(추세추종 원칙 — config.SELL_STRATEGY 주석 참조).
    기능 자체는 남아 있으므로 토글을 명시적으로 켜서 검증한다.
    """
    mock_ind.return_value = _ind()
    mock_cls.return_value = ("보유", "", "")
    mock_score.return_value = (9.0, [])

    base = {"TAKE_PROFIT_RATE": 50.0, "STOP_LOSS_RATE": -20.0, "SELL_SCORE": 3.0,
            "BREAK_EVEN_PROFIT_RATE": 7.0, "BREAK_EVEN_STOP_RATE": 0.5}

    # 고점(highest)에서 +8% 도달(BEP 활성), 현재가는 본전(+0%) → 본전청산
    res = strategy.analyze_sell(
        "005930", "삼성전자", df_up, current_price=10000, buy_price=10000, profit_rate=0.0,
        thresholds={**base, "USE_BREAK_EVEN_STOP": True}, highest_price=10800,
    )
    assert res['action'] == 'sell'
    assert '본전청산' in res['reason']

    # [대조군] 토글이 꺼져 있으면(기본값) 같은 입력에서 청산하지 않는다.
    off = strategy.analyze_sell(
        "005930", "삼성전자", df_up, current_price=10000, buy_price=10000, profit_rate=0.0,
        thresholds={**base, "USE_BREAK_EVEN_STOP": False}, highest_price=10800,
    )
    assert '본전청산' not in off['reason'], "BEP를 껐는데 본전청산이 나왔다"


@patch('modules.auto_trade.analysis.check_smart_money_turnaround', return_value=(False, ""))
@patch('modules.auto_trade.analysis.calculate_score')
@patch('modules.auto_trade.analysis.classify_stock_state')
@patch('modules.auto_trade.indicators.calculate_indicators')
def test_analyze_sell_time_stop_grace(mock_ind, mock_cls, mock_score, mock_sm, strategy, df_up):
    """매수 상태 + 상방 모멘텀 유지 시 시간청산을 유예(보유)한다."""
    config.SELL_STRATEGY["TIME_STOP_USE"] = True
    config.SELL_STRATEGY["TIME_STOP_MIN_PROFIT_RATE"] = 3.0
    mock_ind.return_value = _ind()
    mock_cls.return_value = ("매수", "", "")  # 매수 상태 → 모멘텀 체크 후 유예 가능
    mock_score.return_value = (9.0, [])

    # df_up은 우상향이라 최근 5일 고점 >= 최근 10일 고점 → 상방 모멘텀 유지 → 유예
    res = strategy.analyze_sell(
        "005930", "삼성전자", df_up, current_price=10000, buy_price=10000, profit_rate=0.0,
        thresholds={"TAKE_PROFIT_RATE": 50.0, "STOP_LOSS_RATE": -20.0,
                    "SELL_SCORE": 3.0, "TIME_STOP_DAYS": 10},
        holding_days=15,
    )
    assert res['action'] == 'hold'


# ==========================================================
# analyze_buy 매수 판정 분기
# ==========================================================
def test_analyze_buy_none_df(strategy):
    """df가 없으면 None을 반환한다."""
    assert strategy.analyze_buy("005930", "삼성전자", None, 10000) is None


@patch('modules.auto_trade.analysis.check_smart_money_turnaround', return_value=(False, ""))
@patch('modules.auto_trade.analysis.calculate_score')
@patch('modules.auto_trade.analysis.classify_stock_state')
@patch('modules.auto_trade.indicators.calculate_indicators')
def test_analyze_buy_signal(mock_ind, mock_cls, mock_score, mock_sm, strategy, df_up):
    """매수 상태 + 체결강도/호가비 충족 시 'buy'."""
    mock_ind.return_value = _ind()
    mock_cls.return_value = ("매수", "", "조건충족")
    mock_score.return_value = (8.0, [])

    res = strategy.analyze_buy(
        "005930", "삼성전자", df_up, 10000, vol_strength=150.0, ask_bid_ratio=2.0,
        thresholds={"BUY_VOL_STRENGTH": 100.0, "BUY_ASK_BID_RATIO": 1.0},
    )
    assert res['action'] == 'buy'
    assert res['state'] == "매수"


@patch('modules.auto_trade.analysis.check_smart_money_turnaround', return_value=(False, ""))
@patch('modules.auto_trade.analysis.calculate_score')
@patch('modules.auto_trade.analysis.classify_stock_state')
@patch('modules.auto_trade.indicators.calculate_indicators')
def test_analyze_buy_weak_volume(mock_ind, mock_cls, mock_score, mock_sm, strategy, df_up):
    """체결강도 미달 시 'wait'(사유 기록)."""
    mock_ind.return_value = _ind()
    mock_cls.return_value = ("매수", "", "조건충족")
    mock_score.return_value = (8.0, [])

    res = strategy.analyze_buy(
        "005930", "삼성전자", df_up, 10000, vol_strength=50.0, ask_bid_ratio=2.0,
        thresholds={"BUY_VOL_STRENGTH": 100.0, "BUY_ASK_BID_RATIO": 1.0},
    )
    assert res['action'] == 'wait'
    assert "체결" in res['vol_reject_reason']


@patch('modules.auto_trade.analysis.check_smart_money_turnaround', return_value=(False, ""))
@patch('modules.auto_trade.analysis.calculate_score')
@patch('modules.auto_trade.analysis.classify_stock_state')
@patch('modules.auto_trade.indicators.calculate_indicators')
def test_analyze_buy_ask_bid_reject(mock_ind, mock_cls, mock_score, mock_sm, strategy, df_up):
    """체결강도는 충분하나 호가 매도비 미달(가짜 체결강도) 시 'wait'."""
    mock_ind.return_value = _ind()
    mock_cls.return_value = ("매수", "", "조건충족")
    mock_score.return_value = (8.0, [])

    # auto_adjust: min_ask_bid = 1.0 * (150/100) = 1.5 → ask_bid_ratio 0.5는 미달
    res = strategy.analyze_buy(
        "005930", "삼성전자", df_up, 10000, vol_strength=150.0, ask_bid_ratio=0.5,
        thresholds={"BUY_VOL_STRENGTH": 100.0, "BUY_ASK_BID_RATIO": 1.0,
                    "AUTO_ADJUST_ASK_BID_RATIO": True},
    )
    assert res['action'] == 'wait'
    assert "매도비" in res['vol_reject_reason']


@patch('modules.auto_trade.analysis.check_smart_money_turnaround', return_value=(False, ""))
@patch('modules.auto_trade.analysis.calculate_score')
@patch('modules.auto_trade.analysis.classify_stock_state')
@patch('modules.auto_trade.indicators.calculate_indicators')
def test_analyze_buy_mean_reversion(mock_ind, mock_cls, mock_score, mock_sm, strategy, df_up):
    """역매수 상태는 별도 체결강도 기준(MR_VOL_STRENGTH)을 충족하면 'buy'."""
    mock_ind.return_value = _ind()
    mock_cls.return_value = ("역매수", "", "역추세 반등")
    mock_score.return_value = (7.0, [])

    res = strategy.analyze_buy(
        "005930", "삼성전자", df_up, 10000, vol_strength=150.0, ask_bid_ratio=3.0,
        thresholds={"MR_VOL_STRENGTH": 120.0, "BUY_ASK_BID_RATIO": 1.0},
    )
    assert res['action'] == 'buy'
    assert res['state'] == "역매수"


# ==========================================================
# 체결강도 미확인(None) 시 fail-closed 보류 (2026-07-27)
#  정규장에 KRX(J) 체결강도를 못 구하면 NXT(NX) 값으로 대체하지 않고 None을 넘기므로,
#  게이트가 이를 '충족'으로 통과시키지 않고 보류하는지 검증한다.
# ==========================================================
@patch('modules.auto_trade.analysis.check_smart_money_turnaround', return_value=(False, ""))
@patch('modules.auto_trade.analysis.calculate_score')
@patch('modules.auto_trade.analysis.classify_stock_state')
@patch('modules.auto_trade.indicators.calculate_indicators')
def test_analyze_buy_vol_unknown_holds(mock_ind, mock_cls, mock_score, mock_sm, strategy, df_up, monkeypatch):
    """국내 종목: 체결강도 미확인(None)이면 매수하지 않고 보류한다."""
    mock_ind.return_value = _ind()
    mock_cls.return_value = ("매수", "", "조건충족")
    mock_score.return_value = (8.0, [])
    monkeypatch.setattr(config.session, 'is_toss', False, raising=False)

    res = strategy.analyze_buy(
        "005930", "삼성전자", df_up, 10000, vol_strength=None, ask_bid_ratio=2.0,
        thresholds={"BUY_VOL_STRENGTH": 100.0, "BUY_ASK_BID_RATIO": 1.0},
    )
    assert res['action'] == 'wait'
    assert "미확인" in res['vol_reject_reason']


@patch('modules.auto_trade.analysis.check_smart_money_turnaround', return_value=(False, ""))
@patch('modules.auto_trade.analysis.calculate_score')
@patch('modules.auto_trade.analysis.classify_stock_state')
@patch('modules.auto_trade.indicators.calculate_indicators')
def test_analyze_buy_vol_unknown_gate_off(mock_ind, mock_cls, mock_score, mock_sm, strategy, df_up, monkeypatch):
    """체결강도 기준을 끈 경우(0)에는 미확인이어도 종전대로 통과한다."""
    mock_ind.return_value = _ind()
    mock_cls.return_value = ("매수", "", "조건충족")
    mock_score.return_value = (8.0, [])
    monkeypatch.setattr(config.session, 'is_toss', False, raising=False)

    res = strategy.analyze_buy(
        "005930", "삼성전자", df_up, 10000, vol_strength=None, ask_bid_ratio=2.0,
        thresholds={"BUY_VOL_STRENGTH": 0.0, "BUY_ASK_BID_RATIO": 0.0},
    )
    assert res['action'] == 'buy'


@patch('modules.auto_trade.analysis.check_smart_money_turnaround', return_value=(False, ""))
@patch('modules.auto_trade.analysis.calculate_score')
@patch('modules.auto_trade.analysis.classify_stock_state')
@patch('modules.auto_trade.indicators.calculate_indicators')
def test_analyze_buy_vol_unknown_overseas_passes(mock_ind, mock_cls, mock_score, mock_sm, strategy, df_up, monkeypatch):
    """해외 종목은 KIS가 체결강도를 제공하지 않으므로 미확인이어도 보류하지 않는다."""
    mock_ind.return_value = _ind()
    mock_cls.return_value = ("매수", "", "조건충족")
    mock_score.return_value = (8.0, [])
    monkeypatch.setattr(config.session, 'is_toss', False, raising=False)

    res = strategy.analyze_buy(
        "AAPL", "애플", df_up, 10000, vol_strength=None, ask_bid_ratio=2.0,
        thresholds={"BUY_VOL_STRENGTH": 100.0, "BUY_ASK_BID_RATIO": 1.0},
    )
    assert res['action'] == 'buy'


@patch('modules.auto_trade.analysis.check_smart_money_turnaround', return_value=(False, ""))
@patch('modules.auto_trade.analysis.calculate_score')
@patch('modules.auto_trade.analysis.classify_stock_state')
@patch('modules.auto_trade.indicators.calculate_indicators')
def test_analyze_buy_uses_same_w52_for_state_and_score(mock_ind, mock_cls, mock_score, mock_sm,
                                                      strategy, df_up):
    """[SSOT] 52주 위치는 상태 분류와 점수 계산에 같은 값이 들어가야 한다.

    calculate_score에 w52_pos를 넘기지 않으면 내부 폴백(_w52_band, 365 달력일)이 쓰이는데,
    바로 위 classify_stock_state에는 tail(250 거래일)로 계산한 값을 넘기고 있어 같은 시점에
    52주 위치가 두 개 존재하게 된다. 그러면 가격 모멘텀 팩터(+0.5, 임계 80%)가 상태와 점수에서
    다르게 매겨지고, 백테스트(rolling 250 거래일)와의 판정도 어긋난다.
    (실측: 20종목 6069건 대조에서 2건이 이 원인으로 0.5점 갈렸다 —
     tools/audit_live_backtest_parity.py)
    """
    mock_ind.return_value = _ind()
    mock_cls.return_value = ("매수", "", "조건충족")
    mock_score.return_value = (8.0, [])

    strategy.analyze_buy(
        "005930", "삼성전자", df_up, 10000, vol_strength=150.0, ask_bid_ratio=2.0,
        thresholds={"BUY_VOL_STRENGTH": 100.0, "BUY_ASK_BID_RATIO": 1.0},
    )

    w52_state = mock_cls.call_args.kwargs.get('w52_pos')
    w52_score = mock_score.call_args.kwargs.get('w52_pos')
    assert w52_state is not None, "classify_stock_state에 w52_pos가 전달되지 않았다"
    assert w52_score is not None, \
        "calculate_score에 w52_pos가 전달되지 않았다 — _w52_band(365일) 폴백으로 갈라진다"
    assert w52_score == pytest.approx(w52_state)


# ==========================================================
# [판정이 성립했을 때만 판다] (2026-09-06)
# ==========================================================
#  `if df is not None and not df.empty:` 는 **그릇**만 본다. 프레임이 있어도 내용이 못
#  쓸 수 있다 — 마지막 봉이 결측이거나(yfinance 가 최신 종가를 자주 비운다) 상장 초기라
#  60일선이 아직 없거나. 그때 calculate_score 는 '모름'이 아니라 숫자를 돌려주고
#  (실측: 지표 전무 → 1.0점), ema_60 이 None 이라 structure_broken 도 True 가 되어
#  점수매도가 **전량 청산**을 낸다.
#
#  백테스트에는 이 팔이 없다 — 그쪽 EMA60 은 ewm(adjust=False) 라 결측이면 NaN 이고
#  None 이 아니다. 즉 `is None` 팔은 실매매에만 있고 한 번도 측정된 적이 없다.

@patch('modules.auto_trade.analysis.check_smart_money_turnaround', return_value=(False, ""))
@patch('modules.auto_trade.analysis.calculate_score')
@patch('modules.auto_trade.analysis.classify_stock_state')
@patch('modules.auto_trade.indicators.calculate_indicators')
def test_판정_불가면_점수매도를_하지_않는다(mock_ind, mock_cls, mock_score, mock_sm, strategy, df_up):
    """상태 '-'(데이터 부족)는 '약한 종목'이 아니다."""
    mock_ind.return_value = {}                    # 지표를 하나도 못 구했다
    mock_cls.return_value = ("-", "[dim]", "데이터 부족")
    mock_score.return_value = (1.0, [])           # 그래도 숫자는 나온다

    res = strategy.analyze_sell(
        "005930", "삼성전자", df_up, current_price=10000, buy_price=10000, profit_rate=0.0,
        thresholds={"TAKE_PROFIT_RATE": 50.0, "STOP_LOSS_RATE": -20.0, "SELL_SCORE": 3.0},
    )
    assert res['action'] != 'sell', (
        f"아무것도 못 쟀는데 팔았다: {res.get('reason')}")


@patch('modules.auto_trade.analysis.check_smart_money_turnaround', return_value=(False, ""))
@patch('modules.auto_trade.analysis.calculate_score')
@patch('modules.auto_trade.analysis.classify_stock_state')
@patch('modules.auto_trade.indicators.calculate_indicators')
def test_판정_불가여도_매도_상태는_그대로_판다(mock_ind, mock_cls, mock_score, mock_sm, strategy, df_up):
    """'매도' 상태는 자체 조건이 엄격하다 — 그 길까지 막으면 청산이 늦는다."""
    mock_ind.return_value = _ind()
    mock_cls.return_value = ("매도", "", "추세붕괴")
    mock_score.return_value = (9.0, [])           # 점수는 높아도 상태가 매도면 판다

    res = strategy.analyze_sell(
        "005930", "삼성전자", df_up, current_price=10000, buy_price=10000, profit_rate=0.0,
        thresholds={"TAKE_PROFIT_RATE": 50.0, "STOP_LOSS_RATE": -20.0, "SELL_SCORE": 3.0},
    )
    assert res['action'] == 'sell'


def test_마지막_봉이_결측인_프레임은_실제로_팔지_않는다(strategy):
    """목 없이 진짜 경로로 — 실측으로 'sell' 이 나오던 자리다.

    종전 사유 문자열: "추세이탈(-/점수하락+60일선이탈) [점수:1.0, RSI:-, ADX:-, CCI:-]"
    아무것도 못 쟀다고 적으면서 팔았다.
    """
    n = 200
    close = pd.Series(np.linspace(69000.0, 70000.0, n))
    df = pd.DataFrame({
        'date': [f"2026{(i % 12) + 1:02d}{(i % 28) + 1:02d}" for i in range(n)],
        'open': close, 'high': close * 1.01, 'low': close * 0.99,
        'close': close, 'volume': 1000.0,
    })
    df.loc[df.index[-1], ['open', 'high', 'low', 'close']] = np.nan

    res = strategy.analyze_sell(
        "005930", "삼성전자", df, current_price=70000.0, buy_price=69000.0,
        profit_rate=1.45, holding_days=3, highest_price=70500.0)
    assert res['action'] != 'sell', f"결측 프레임으로 청산했다: {res.get('reason')}"


def test_그_팔이_백테스트에는_없다는_사실을_못박는다():
    """실매매에만 있는 경로가 다시 생기면 알아야 한다 — 백테스트가 못 재는 매도는 위험하다."""
    import inspect
    from modules import backtest

    src = inspect.getsource(backtest)
    assert "df['EMA60'] = df['close'].ewm(span=60, adjust=False).mean()" in src, (
        "백테스트 EMA60 산출이 바뀌었다 — 결측이 None 으로 오면 이 축의 전제가 달라진다")

    n = 5
    frame = pd.DataFrame({'close': np.linspace(1.0, 2.0, n)})
    frame['EMA60'] = frame['close'].ewm(span=60, adjust=False).mean()
    assert frame.iloc[0].get('EMA60') is not None, (
        "백테스트의 EMA60 은 0번 봉부터 값이 있다 — `is None` 팔은 그쪽에서 죽은 코드다")
