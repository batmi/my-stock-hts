"""종목 상태 스냅샷 공용 캐시 테스트 (텔레그램 /stocks 연동).

시스템 트레이딩과 운영자 수동 조회(메뉴 2)가 같은 저장소를 쓴다. 둘 다
analysis.classify_stock_state()의 결과라 값의 의미가 같으므로, 사용자에게 필요한 구분은
'누가 계산했나'가 아니라 '언제 것인가'다 → 출처는 숨기고 조회 시각만 노출한다.

출처(src)는 지우기 규칙에만 쓴다. 시스템은 분석 불가 종목에 set_stock_state(code, None)로
자기 값을 지우는데(NXT 시간대 ETF 등), 이때 더 신선한 수동 스냅샷까지 날아가면 정작
시스템이 보지 못하는 종목의 상태가 사라진다.
"""
from datetime import datetime
from unittest.mock import patch

import pandas as pd
import pytest

import api
from core import context
from modules.auto_trade import AutoTrader


class _FrozenDatetime(datetime):
    _now = None

    @classmethod
    def now(cls, tz=None):
        return cls._now


@pytest.fixture(autouse=True)
def _clean_state():
    context.prune_stock_states(())
    yield
    context.prune_stock_states(())


def _kr_at(dt):
    """국내 세션 판정을 특정 시각으로 고정한다."""
    _FrozenDatetime._now = dt
    return (patch.object(api, 'datetime', _FrozenDatetime),
            patch.object(api, 'is_holiday_today', lambda: False))


# ==========================================================
# 저장 / 조회 / 만료
# ==========================================================

def test_set_and_get_returns_state_with_time():
    a, b = _kr_at(datetime(2026, 7, 28, 10, 0))
    with a, b:
        context.set_stock_state("069500", "매도", src='manual')
        got = context.get_stock_state("069500")
    assert got is not None
    state, at = got
    assert state == "매도"
    assert len(at) == 5 and at[2] == ":"      # HH:MM


def test_snapshot_expires_when_session_changes():
    """정규장에 찍은 상태는 애프터마켓으로 넘어가면 폐기된다.
    세션이 바뀌면 확정 종가·일봉이 바뀌어 같은 종목의 상태 판정도 달라진다."""
    a, b = _kr_at(datetime(2026, 7, 28, 10, 0))
    with a, b:
        context.set_stock_state("069500", "매도", src='manual')

    a, b = _kr_at(datetime(2026, 7, 28, 16, 0))
    with a, b:
        assert context.get_stock_state("069500") is None


def test_snapshot_expires_next_day_in_same_phase():
    """같은 단계('장 마감')가 이어져도 날짜가 바뀌면 폐기된다 — HH:MM만 보여주므로
    묵은 값이 남으면 어제 것인지 오늘 것인지 구분되지 않는다."""
    a, b = _kr_at(datetime(2026, 7, 28, 22, 0))
    with a, b:
        context.set_stock_state("069500", "매도", src='manual')

    a, b = _kr_at(datetime(2026, 7, 29, 22, 0))
    with a, b:
        assert context.get_stock_state("069500") is None


def test_us_day_market_snapshot_survives_et_midnight():
    """미국 데이마켓은 ET 자정을 넘겨 이어진다 — 달력일로 만료시키면 세션 한복판에 사라진다."""
    with patch.object(api, 'now_us_eastern', lambda: datetime(2026, 7, 22, 22, 0)), \
         patch.object(api, 'us_day_market_session', lambda: "20260723"):
        context.set_stock_state("AAPL", "강매수", src='manual')

    with patch.object(api, 'now_us_eastern', lambda: datetime(2026, 7, 23, 2, 0)), \
         patch.object(api, 'us_day_market_session', lambda: "20260723"):
        got = context.get_stock_state("AAPL")
    assert got is not None and got[0] == "강매수"


def test_get_state_missing_code():
    assert context.get_stock_state("000000") is None


# ==========================================================
# 출처별 지우기
# ==========================================================

def test_auto_clear_keeps_manual_snapshot():
    """시스템이 분석을 스킵해도(NXT 시간대 ETF 등) 수동 스냅샷은 남아야 한다."""
    a, b = _kr_at(datetime(2026, 7, 28, 16, 0))
    with a, b:
        context.set_stock_state("069500", "매도", src='manual')
        context.clear_stock_state("069500", src='auto')
        assert context.get_stock_state("069500")[0] == "매도"


def test_auto_clear_removes_own_snapshot():
    a, b = _kr_at(datetime(2026, 7, 28, 10, 0))
    with a, b:
        context.set_stock_state("005930", "매수", src='auto')
        context.clear_stock_state("005930", src='auto')
        assert context.get_stock_state("005930") is None


def test_newer_write_overwrites_regardless_of_source():
    """더 최근에 계산한 값이 항상 이긴다 — 출처는 우선순위가 아니다."""
    a, b = _kr_at(datetime(2026, 7, 28, 10, 0))
    with a, b:
        context.set_stock_state("005930", "관심", src='manual')
        context.set_stock_state("005930", "매수", src='auto')
        assert context.get_stock_state("005930")[0] == "매수"
        context.set_stock_state("005930", "주의", src='manual')
        assert context.get_stock_state("005930")[0] == "주의"


def test_prune_drops_codes_outside_watchlist():
    a, b = _kr_at(datetime(2026, 7, 28, 10, 0))
    with a, b:
        context.set_stock_state("005930", "매수", src='auto')
        context.set_stock_state("069500", "매도", src='manual')
        context.prune_stock_states({"005930"})
        assert context.get_stock_state("005930") is not None
        assert context.get_stock_state("069500") is None


# ==========================================================
# AutoTrader 위임 (기존 인터페이스 유지)
# ==========================================================

def test_trader_set_stock_state_delegates_to_context():
    trader = AutoTrader()
    a, b = _kr_at(datetime(2026, 7, 28, 10, 0))
    with a, b:
        trader.set_stock_state("005930", "매수")
        assert context.get_stock_state("005930")[0] == "매수"
        assert trader.stock_state_cache.get("005930") == "매수"
        trader.set_stock_state("005930", None)
        assert context.get_stock_state("005930") is None


# ==========================================================
# 텔레그램 /stocks 출력
# ==========================================================

def _monitoring_list():
    from modules.telegram_bot import TelegramCommander
    bot = TelegramCommander()
    with patch('modules.telegram_bot.auto_trade.get_restricted_stocks', return_value={}), \
         patch('modules.telegram_bot.db_manager.db.get_all_stock_strategies', return_value=[]):
        return bot._get_monitoring_list()


def test_stocks_shows_state_with_time(monkeypatch):
    import config as _config
    monkeypatch.setattr(_config.session, 'stock_data',
                        {"stocks_kr": [{"name": "삼성전자", "code": "005930"}],
                         "etfs_kr": [{"name": "KODEX 200", "code": "069500"}]},
                        raising=False)
    a, b = _kr_at(datetime(2026, 7, 28, 16, 0))
    with a, b:
        context.set_stock_state("005930", "매수", src='auto')
        context.set_stock_state("069500", "매도", src='manual')
        msg = _monitoring_list()

    # 출처와 무관하게 같은 형태 — 이모지 + 상태 + 조회 시각 (본문에 출처 표기 없음)
    body = msg.split("ℹ️")[0]
    assert "🔴 매수 " in body
    assert "🔵 매도 " in body
    assert "수동" not in body and "시스템" not in body


def test_stocks_footer_when_system_stopped_but_states_exist(monkeypatch):
    """시스템 정지 중이어도 수동 조회 상태가 있으면 '표시되지 않는다'는 안내는 틀린 말이다."""
    import config as _config
    monkeypatch.setattr(_config.session, 'stock_data',
                        {"stocks_kr": [{"name": "삼성전자", "code": "005930"}]},
                        raising=False)
    trader = AutoTrader()
    monkeypatch.setattr(trader, 'is_running', False, raising=False)

    a, b = _kr_at(datetime(2026, 7, 28, 16, 0))
    with a, b:
        context.set_stock_state("005930", "매수", src='manual')
        msg = _monitoring_list()

    assert "🔴 매수 " in msg
    assert "현재 상태가 표시되지 않습니다" not in msg
    assert "시스템 트레이딩 중지 중" in msg


def test_stocks_footer_when_no_state_at_all(monkeypatch):
    import config as _config
    monkeypatch.setattr(_config.session, 'stock_data',
                        {"stocks_kr": [{"name": "삼성전자", "code": "005930"}]},
                        raising=False)
    msg = _monitoring_list()
    assert "조회된 상태가 없습니다" in msg


# ==========================================================
# 매수/매도 경로 임계값 일치 (BUY_RSI_MAX 누락 회귀)
# ==========================================================

_RULE = {
    'code': '005930', 'buy_score': 7.0, 'buy_rsi': 55.0, 'sell_score': 4.0,
    'take_profit': 10.0, 'stop_loss': -5.0, 'take_profit_rsi': 75.0,
    'weights': None, 'ts_activation': 10.0, 'ts_callback': 5.0,
    'time_stop_days': 20, 'half_take_profit_use': False, 'use_atr_stop': False,
    'buy_vol_strength': 100.0, 'buy_ask_bid_ratio': 1.0,
}


@patch('modules.auto_trade.api.fetch_sellable_quantity', return_value=10)
@patch('modules.auto_trade.api.get_chart_data')
@patch('modules.auto_trade.DefaultStrategy.analyze_sell', return_value=None)
@patch('modules.auto_trade.analysis.get_market_regime', return_value=("중립", 0.0))
@patch('modules.auto_trade.api.prefetch_multiple_current_prices')
def test_sell_path_passes_rule_buy_rsi_max(mock_pre, mock_regime, mock_sell, mock_chart, mock_qty):
    """개별 룰의 RSI 상한이 매도 경로의 상태 재판정에도 전달돼야 한다.

    analyze_sell도 classify_stock_state로 상태를 다시 매기는데(engine.py), thresholds에
    BUY_RSI_MAX가 없으면 전역값으로 폴백해 매수 경로·메뉴 2 화면과 상태가 갈렸다.
    """
    mock_chart.return_value = pd.DataFrame({
        'close': [60000], 'high': [60000], 'low': [60000], 'open': [60000], 'volume': [1000]})
    holdings = [{'pdno': '005930', 'prdt_name': '삼성전자', 'ord_psbl_qty': '10',
                 'evlu_pfls_rt': '5.0', 'prpr': '60000', 'pchs_avg_pric': '55000',
                 'evlu_pfls_amt': '50000'}]

    trader = AutoTrader()
    trader.is_running = True
    with patch.object(trader.order_manager, 'is_pending', return_value=False):
        trader._check_sell_conditions(holdings, is_market_open=True,
                                      rules_map={'005930': _RULE}, restricted_stocks={})

    assert mock_sell.called, "analyze_sell이 호출되지 않아 임계값을 확인할 수 없다"
    thresholds = mock_sell.call_args.kwargs.get('thresholds')
    assert thresholds is not None
    assert thresholds.get("BUY_RSI_MAX") == _RULE['buy_rsi']
    # 매수 경로와 동일한 키를 준다 (classify_stock_state가 읽는 값들)
    assert thresholds.get("BUY_SCORE") == _RULE['buy_score']
