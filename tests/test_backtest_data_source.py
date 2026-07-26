"""백테스트 데이터 소스 정합성 + 손절 부재 시 매수 가드.

- 국내 백테스트는 모드와 무관하게 KRX 공식(pykrx/FDR)을 1순위로 쓴다.
  실매매·화면 지표(토스=KRX 공식, KIS 일봉=KRX 정규장 기준)와 같은 기준이라
  검증한 전략과 실행하는 전략이 다른 데이터 위에 서지 않고, 모드를 바꿔도 결과가 같다.
  폴백: yfinance → 차트 API(250봉 상한 → 절단 경고)
- "탈출 전략이 없다면 포지션을 잡지 마라": 손절 기준이 아예 없으면 매수하지 않는다.
"""
from unittest.mock import MagicMock, patch

import pandas as pd
import pytest

import config
from modules import backtest, krx_daily


def _daily(n=300):
    idx = pd.bdate_range('2024-01-02', periods=n)
    return pd.DataFrame({
        'date': idx.strftime('%Y%m%d'),
        'open': [10000 + i for i in range(n)],
        'high': [10100 + i for i in range(n)],
        'low': [9900 + i for i in range(n)],
        'close': [10050 + i for i in range(n)],
        'volume': [1000 + i for i in range(n)],
    })


@pytest.fixture
def toss_mode():
    old = config.session.is_toss
    config.session.is_toss = True
    yield
    config.session.is_toss = old


@pytest.fixture
def kis_mode():
    old = config.session.is_toss
    config.session.is_toss = False
    yield
    config.session.is_toss = old


# ---------------------------------------------------------
# 국내 — 모드 무관 KRX 공식 소스
# ---------------------------------------------------------
def test_toss_backtest_uses_krx_official_source(toss_mode):
    df = _daily()
    with patch.object(krx_daily, 'get_daily', return_value=df) as krx, \
         patch.object(backtest.api, 'fetch_yfinance_data') as yf, \
         patch.object(backtest.api, 'get_chart_data') as chart:
        out = backtest.get_backtest_data('005930', False, 1000)

    assert out is df
    yf.assert_not_called()          # yfinance로 새면 실매매와 다른 데이터로 검증하게 된다
    chart.assert_not_called()


def test_toss_backtest_requests_warmup_period(toss_mode):
    """지표 워밍업(52주) 포함 기간을 요청해야 한다 — 요청일수 + 400."""
    with patch.object(krx_daily, 'get_daily', return_value=_daily()) as krx, \
         patch.object(backtest.api, 'fetch_yfinance_data'), \
         patch.object(backtest.api, 'get_chart_data'):
        backtest.get_backtest_data('005930', False, 1000)
    assert krx.call_args.kwargs['lookback_days'] == 1400


def test_backtest_falls_back_to_yfinance_when_krx_fails(toss_mode):
    """KRX 실패 시 yfinance로 폴백한다."""
    yf_df = _daily().set_index('date')
    with patch.object(krx_daily, 'get_daily', return_value=None), \
         patch.object(backtest.api, 'fetch_yfinance_data', return_value=yf_df) as yf:
        backtest.get_backtest_data('005930', False, 1000)
    yf.assert_called()


def test_backtest_survives_krx_exception(toss_mode):
    yf_df = _daily().set_index('date')
    with patch.object(krx_daily, 'get_daily', side_effect=RuntimeError('boom')), \
         patch.object(backtest.api, 'fetch_yfinance_data', return_value=yf_df) as yf:
        backtest.get_backtest_data('005930', False, 1000)
    yf.assert_called()


# ---------------------------------------------------------
# KIS 모드 / 해외
# ---------------------------------------------------------
def test_kis_mode_backtest_also_uses_krx_source(kis_mode):
    """mode 1/2도 KRX 공식을 쓴다 — KIS 일봉이 KRX 정규장 기준이라 같은 값이고,
    KIS 분석 경로(_fetch_domestic_daily)는 250봉 상한이라 장기 백테스트에 못 쓴다."""
    df = _daily()
    with patch.object(krx_daily, 'get_daily', return_value=df) as krx, \
         patch.object(backtest.api, 'fetch_yfinance_data') as yf:
        out = backtest.get_backtest_data('005930', False, 1000)
    assert out is df
    krx.assert_called_once()
    yf.assert_not_called()


def test_both_modes_return_identical_source(toss_mode):
    """모드를 바꿔도 같은 소스를 타야 백테스트 결과가 흔들리지 않는다."""
    df = _daily()
    with patch.object(krx_daily, 'get_daily', return_value=df):
        a = backtest.get_backtest_data('005930', False, 1000)
        config.session.is_toss = False
        b = backtest.get_backtest_data('005930', False, 1000)
    assert a is b is df


def test_overseas_backtest_never_uses_krx_source(toss_mode):
    yf_df = _daily().set_index('date')
    with patch.object(krx_daily, 'get_daily') as krx, \
         patch.object(backtest.api, 'fetch_yfinance_data', return_value=yf_df):
        backtest.get_backtest_data('AAPL', True, 1000)
    krx.assert_not_called()


# ---------------------------------------------------------
# 캐시 — 짧게 캐시된 시계열이 긴 요청에 재사용되면 안 된다
# ---------------------------------------------------------
def test_short_cached_lookback_is_not_reused_for_longer_request():
    """차트 경로(730일)가 먼저 캐시하면 백테스트(수년)가 잘린 시계열을 받는 문제 방지."""
    krx_daily.clear_cache()
    krx_daily._import_done, krx_daily._pykrx, krx_daily._fdr = True, object(), object()
    try:
        norm = krx_daily._normalize(
            pd.DataFrame({'시가': [1], '고가': [2], '저가': [1], '종가': [2], '거래량': [1]},
                         index=pd.to_datetime(['2026-07-24'])), 'pykrx')
        with patch.object(krx_daily, '_fetch_pykrx', return_value=norm) as m:
            krx_daily.get_daily('005930', lookback_days=730)     # 차트 경로
            krx_daily.get_daily('005930', lookback_days=3650)    # 백테스트 — 재조회돼야 한다
            assert m.call_count == 2
            krx_daily.get_daily('005930', lookback_days=730)     # 더 긴 캐시본은 재사용
            assert m.call_count == 2
    finally:
        krx_daily.clear_cache()


# ---------------------------------------------------------
# 손절 가드 — "탈출 전략이 없다면 포지션을 잡지 마라"
# ---------------------------------------------------------
@pytest.fixture
def trader():
    from modules.auto_trade import AutoTrader
    t = AutoTrader()
    t.is_running = True
    return t


def _cand():
    return [{'code': '005930', 'name': '삼성전자', 'price': 50000, 'score': 9.0,
             'rsi': 50, 'adx': 30, 'cci': 100, 'is_custom_rule': False, 'atr': 0}]


def test_buy_blocked_when_no_stop_loss_defined(trader):
    """ATR 손절 OFF + 고정 손절 0 → 청산 기준도 손실액 상한도 없으므로 매수하지 않는다."""
    with patch.dict(config.SELL_STRATEGY, {'STOP_LOSS_RATE': 0, 'USE_ATR_STOP': False}), \
         patch('modules.auto_trade.api.place_order') as place, \
         patch('modules.auto_trade.api.fetch_buyable_quantity', return_value=10), \
         patch('modules.auto_trade.db_manager.db.insert_trade'), \
         patch('modules.auto_trade.api.send_telegram_message'):
        trader._execute_buy_orders(_cand(), 1_000_000, 0.25, 0, 5)
    place.assert_not_called()


def test_buy_proceeds_with_fixed_stop_loss(trader):
    """고정 손절이 살아 있으면 정상 매수된다(가드가 과잉 차단하지 않는다)."""
    with patch.dict(config.SELL_STRATEGY, {'STOP_LOSS_RATE': -7.0, 'USE_ATR_STOP': False}), \
         patch('modules.auto_trade.api.place_order',
               return_value={'rt_cd': '0', 'output': {'ODNO': '1'}}) as place, \
         patch('modules.auto_trade.api.fetch_buyable_quantity', return_value=10), \
         patch('modules.auto_trade.db_manager.db.insert_trade'), \
         patch('modules.auto_trade.api.send_telegram_message'):
        trader._execute_buy_orders(_cand(), 1_000_000, 0.25, 0, 5)
    place.assert_called()


def test_buy_blocked_when_rule_disables_stop_loss(trader):
    """개별 종목 룰로 손절을 꺼도 동일하게 차단된다."""
    cands = _cand()
    cands[0]['rule'] = {'stop_loss': 0, 'use_atr_stop': 0}
    with patch.dict(config.SELL_STRATEGY, {'STOP_LOSS_RATE': -7.0, 'USE_ATR_STOP': True}), \
         patch('modules.auto_trade.api.place_order') as place, \
         patch('modules.auto_trade.api.fetch_buyable_quantity', return_value=10), \
         patch('modules.auto_trade.db_manager.db.insert_trade'), \
         patch('modules.auto_trade.api.send_telegram_message'):
        trader._execute_buy_orders(cands, 1_000_000, 0.25, 0, 5)
    place.assert_not_called()


def test_atr_stop_satisfies_the_guard(trader):
    """고정 손절이 0이어도 ATR 손절이 계산되면 진입한다."""
    cands = _cand()
    cands[0]['atr'] = 1500
    with patch.dict(config.SELL_STRATEGY, {'STOP_LOSS_RATE': 0, 'USE_ATR_STOP': True}), \
         patch('modules.auto_trade.api.place_order',
               return_value={'rt_cd': '0', 'output': {'ODNO': '1'}}) as place, \
         patch('modules.auto_trade.api.fetch_buyable_quantity', return_value=10), \
         patch('modules.auto_trade.db_manager.db.insert_trade'), \
         patch('modules.auto_trade.api.send_telegram_message'):
        trader._execute_buy_orders(cands, 1_000_000, 0.25, 0, 5)
    place.assert_called()


# ---------------------------------------------------------
# 무언의 절단 방지 — 차트 API(250봉 상한) 폴백 시 경고
# ---------------------------------------------------------
def test_warns_when_fallback_data_is_truncated(toss_mode):
    """차트 API는 250봉 상한이라 다년 요청이 조용히 1년으로 잘린다 → 반드시 알린다."""
    short = _daily(250)
    with patch.object(krx_daily, 'get_daily', return_value=None), \
         patch.object(backtest.api, 'fetch_yfinance_data', return_value=pd.DataFrame()), \
         patch.object(backtest.api, 'get_chart_data', return_value=short), \
         patch.object(config.console, 'print') as p:
        backtest.get_backtest_data('005930', False, 2000)
    out = ' '.join(str(c.args[0]) for c in p.call_args_list if c.args)
    assert '250봉 상한' in out and 'yellow' in out


def test_no_warning_when_coverage_is_sufficient(toss_mode):
    full = _daily(300)          # 약 430일치(영업일 300)
    with patch.object(krx_daily, 'get_daily', return_value=None), \
         patch.object(backtest.api, 'fetch_yfinance_data', return_value=pd.DataFrame()), \
         patch.object(backtest.api, 'get_chart_data', return_value=full), \
         patch.object(config.console, 'print') as p:
        backtest.get_backtest_data('005930', False, 100)
    assert not [c for c in p.call_args_list if c.args and '250봉' in str(c.args[0])]


# ---------------------------------------------------------
# 기본 분석 기간 (365일 → 730일 → 1095일, 2026-07-26)
#  승률 24%·대박 fat-tail 구조라 표본 부족이 결과를 가장 크게 왜곡한다.
#  실측(30종목): 종목당 청산이 365일 5.9건 → 730일 11.3건 → 1095일 16.2건.
#  pykrx/FDR이 3년치를 절단 없이 커버하고 비용도 30종목 5.2s→5.5s로 무시할 만하다.
# ---------------------------------------------------------

def test_backtest_default_period_is_three_years():
    import inspect

    from modules import backtest as bt
    src = inspect.getsource(bt.run_backtest)
    assert 'days = 1095' in src
    assert 'default="1095"' in src
    assert 'days = 365' not in src
    assert 'days = 730' not in src


def test_walk_forward_default_period_is_three_years():
    import inspect

    from modules import backtest as bt
    sig = inspect.signature(bt.run_walk_forward)
    assert sig.parameters['days'].default == 1095


def test_walk_forward_floor_raised_to_three_years():
    """WF는 OOS 폴드당 표본을 확보해야 하므로 짧은 설정을 1095일로 자동 상향한다."""
    import inspect

    from modules import backtest as bt
    src = inspect.getsource(bt.run_backtest)
    assert 'if days < 1095:' in src
    assert 'if days < 730:' not in src
