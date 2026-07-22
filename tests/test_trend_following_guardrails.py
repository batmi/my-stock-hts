"""추세추종 원칙 가드레일 테스트.

책의 원칙 중 코드가 위배하던 세 지점을 바로잡은 동작을 고정한다.

1) "손절을 하지 않으면 언젠가는 계좌가 심각한 타격을 입는다"
   → 일일 손실 한도 초과는 '방어 모드'(신규 매수만 중단)이며, 매도·손절 감시는 계속 돌아야 한다.
2) "대체 무슨 일이 벌어지고 있는지 모르겠다면, 아무것도 하지 마라"
   → 지수 판단 불가(데이터 장애) 시 매수 게이트는 fail-closed(보류)여야 한다.
3) "탈출 전략이 없다면 포지션을 잡지 마라"
   → 자동 매도에서 제외된 포지션(수동 홀딩·ETF)이 손절선을 이탈하면 최소한 경보는 나가야 한다.
"""
import time
import pytest
from unittest.mock import patch, MagicMock

from modules.auto_trade import AutoTrader
import config


@pytest.fixture
def trader():
    t = AutoTrader()
    t.order_manager.pending_orders.clear()
    t.consecutive_errors = 0
    t.buy_halted = False
    t.buy_halt_reason = ""
    t.buy_halt_date = None
    t.market_index_status = {}
    t.unmanaged_stop_notified = {}
    return t


# ==========================================================
# 1. 방어 모드 — 신규 매수만 중단, 청산은 유지
# ==========================================================

def test_halt_buys_blocks_new_entries_only(trader):
    """방어 모드에서는 매수 검사가 즉시 반환되고, 매도 검사는 영향받지 않는다."""
    with patch('modules.auto_trade.api.send_telegram_message'):
        assert trader.halt_buys("일일 손실 한도 초과") is True
    assert trader.buy_halted is True

    # 매수 경로: 후보 분석에 진입하지 않고 즉시 반환되어야 한다
    with patch.object(trader, '_analyze_candidates') as mock_analyze:
        trader._check_buy_conditions([], {'d2_deposit': 10_000_000}, is_market_open=True)
        mock_analyze.assert_not_called()

    # 매도 경로: 방어 모드와 무관하게 정상 동작해야 한다 (보유 없음 → 히트 0으로 정상 종료)
    trader._check_sell_conditions([], is_market_open=True, rules_map={}, restricted_stocks={})
    assert trader.portfolio_heat_amt == 0.0


def test_halt_buys_is_idempotent_within_same_day(trader):
    """같은 날 재호출하면 False를 돌려주어 알림·로그가 반복되지 않는다."""
    with patch('modules.auto_trade.api.send_telegram_message') as mock_tg:
        assert trader.halt_buys("한도 초과", notify_msg="알림") is True
        assert trader.halt_buys("한도 초과", notify_msg="알림") is False
        assert mock_tg.call_count == 1


def test_resume_buys_clears_halt(trader):
    """방어 모드 해제 시 매수 경로가 다시 열린다."""
    with patch('modules.auto_trade.api.send_telegram_message'):
        trader.halt_buys("한도 초과")
    assert trader.resume_buys("테스트") is True
    assert trader.buy_halted is False
    assert trader.resume_buys("테스트") is False  # 이미 해제됨


def test_pyramiding_blocked_while_halted(trader):
    """방어 모드에서는 피라미딩(노출 확대)도 보류된다."""
    with patch('modules.auto_trade.api.send_telegram_message'):
        trader.halt_buys("한도 초과")

    with patch.object(trader.strategy, 'analyze_pyramid') as mock_pyr:
        trader._try_pyramid_buy('005930', '삼성전자', 10, 10000, 15.0,
                                {'state': '매수', 'score': 8.0, 'ind': {}}, None, True)
        mock_pyr.assert_not_called()


# ==========================================================
# 2. 시장 필터 fail-closed — 판단 불가 시 매수 보류
# ==========================================================

def test_index_status_unknown_on_fetch_failure(trader):
    """지수 조회 실패 시 is_healthy=False + unknown=True로 기록된다 (fail-closed)."""
    with patch('modules.auto_trade.analysis.get_domestic_index_data', return_value=None), \
         patch('modules.auto_trade.api.send_telegram_message'):
        trader._update_market_indices_status(notify=False)

    for m in ("KOSPI", "KOSDAQ"):
        assert trader.market_index_status[m]['is_healthy'] is False
        assert trader.market_index_status[m]['unknown'] is True


def test_index_status_unknown_on_exception(trader):
    """지수 조회가 예외를 던져도 매수를 허용하지 않는다."""
    with patch('modules.auto_trade.analysis.get_domestic_index_data', side_effect=RuntimeError("네트워크 장애")), \
         patch('modules.auto_trade.api.send_telegram_message'):
        trader._update_market_indices_status(notify=False)

    assert trader.market_index_status["KOSPI"]['unknown'] is True
    assert trader.market_index_status["KOSPI"]['is_healthy'] is False


@patch('modules.auto_trade.analysis.get_market_regime', return_value=("Sideways", 0.0))
def test_buy_candidate_skipped_when_index_unknown(mock_regime, trader):
    """지수 판단 불가 상태에서는 매수 후보가 시장 필터로 보류된다."""
    config.USE_MARKET_FILTER = True
    trader.is_running = True
    trader.market_index_status = {"KOSPI": {"is_healthy": False, "unknown": True, "current": 0}}
    trader.stock_market_map = {'005930': 'KOSPI'}

    res = trader._analyze_candidate_worker(
        item={'code': '005930', 'name': '삼성전자', 'group': 'stocks_kr'},
        holding_codes=set(), rules_map={}, restricted_stocks={},
        market_regime_adj={}, safe_delay=0, reentry_hurdles={},
        holdings_dfs={}, holding_groups_map={},
    )
    assert res is not None and res['type'] == 'market_skip'


@patch('modules.auto_trade.analysis.get_market_regime', return_value=("Sideways", 0.0))
def test_buy_candidate_skipped_when_index_status_missing(mock_regime, trader):
    """상태 캐시가 아예 없어도(첫 주기 전) 매수를 허용하지 않는다."""
    config.USE_MARKET_FILTER = True
    trader.is_running = True
    trader.market_index_status = {}
    trader.stock_market_map = {'005930': 'KOSPI'}

    res = trader._analyze_candidate_worker(
        item={'code': '005930', 'name': '삼성전자', 'group': 'stocks_kr'},
        holding_codes=set(), rules_map={}, restricted_stocks={},
        market_regime_adj={}, safe_delay=0, reentry_hurdles={},
        holdings_dfs={}, holding_groups_map={},
    )
    assert res is not None and res['type'] == 'market_skip'


def test_pyramiding_blocked_when_index_unknown(trader):
    """피라미딩도 신규 매수와 동일하게 지수 판단 불가 시 보류된다."""
    config.USE_MARKET_FILTER = True
    config.ANALYSIS_THRESHOLDS["PYRAMIDING_REQUIRE_HEALTHY_MARKET"] = True
    trader.market_index_status = {"KOSPI": {"is_healthy": False, "unknown": True, "current": 0}}
    trader.stock_market_map = {'005930': 'KOSPI'}

    with patch.object(trader.strategy, 'analyze_pyramid', return_value=(True, "피라미딩 1차")), \
         patch('modules.auto_trade.api.fetch_buyable_quantity') as mock_qty:
        trader._try_pyramid_buy('005930', '삼성전자', 10, 10000, 15.0,
                                {'state': '매수', 'score': 8.0, 'ind': {}}, None, True)
        mock_qty.assert_not_called()  # 증액 수량 조회까지 가지 않고 보류


# ==========================================================
# 3. 자동매도 제외 포지션의 손절선 이탈 경보
# ==========================================================

def _holding(profit_rate):
    return {
        'pdno': '069500', 'prdt_name': 'KODEX 200', 'hldg_qty': '10',
        'ord_psbl_qty': '10', 'evlu_pfls_rt': str(profit_rate),
        'prpr': '9000', 'pchs_avg_pric': '10000',
        'evlu_amt': '90000', 'evlu_pfls_amt': '-10000',
    }


def test_unmanaged_stop_alert_fires_below_stop(trader):
    """손절선 이탈 시 텔레그램 경보를 보낸다."""
    config.SELL_STRATEGY["STOP_LOSS_RATE"] = -7.0
    with patch('modules.auto_trade.api.send_telegram_message') as mock_tg:
        trader._alert_unmanaged_stop('069500', 'KODEX 200', _holding(-10.0), 'ETF 자동매매 제외 설정')
        mock_tg.assert_called_once()
        body = mock_tg.call_args[0][0]
        assert '손절선 이탈' in body
        assert 'KODEX 200' in body


def test_unmanaged_stop_alert_silent_above_stop(trader):
    """손절선 위에서는 경보하지 않는다."""
    config.SELL_STRATEGY["STOP_LOSS_RATE"] = -7.0
    with patch('modules.auto_trade.api.send_telegram_message') as mock_tg:
        trader._alert_unmanaged_stop('069500', 'KODEX 200', _holding(-3.0), 'ETF 자동매매 제외 설정')
        mock_tg.assert_not_called()


def test_unmanaged_stop_alert_throttled_24h(trader):
    """같은 종목의 반복 경보는 24시간 스로틀된다."""
    config.SELL_STRATEGY["STOP_LOSS_RATE"] = -7.0
    with patch('modules.auto_trade.api.send_telegram_message') as mock_tg:
        trader._alert_unmanaged_stop('069500', 'KODEX 200', _holding(-10.0), 'ETF')
        trader._alert_unmanaged_stop('069500', 'KODEX 200', _holding(-12.0), 'ETF')
        assert mock_tg.call_count == 1


def test_unmanaged_stop_alert_rearms_after_recovery(trader):
    """손절선 위로 회복하면 스로틀이 풀려 재이탈 시 다시 알린다."""
    config.SELL_STRATEGY["STOP_LOSS_RATE"] = -7.0
    with patch('modules.auto_trade.api.send_telegram_message') as mock_tg:
        trader._alert_unmanaged_stop('069500', 'KODEX 200', _holding(-10.0), 'ETF')
        trader._alert_unmanaged_stop('069500', 'KODEX 200', _holding(-2.0), 'ETF')   # 회복
        trader._alert_unmanaged_stop('069500', 'KODEX 200', _holding(-11.0), 'ETF')  # 재이탈
        assert mock_tg.call_count == 2


def test_unmanaged_stop_uses_recorded_stop_loss_rate(trader):
    """매수 기록의 실제 손절률(ATR 손절)이 있으면 그것을 기준으로 판정한다."""
    config.SELL_STRATEGY["STOP_LOSS_RATE"] = -7.0
    trades = [{'qty': 10, 'stop_loss_rate': -12.0}]  # ATR 손절 -12%

    with patch('modules.auto_trade.api.send_telegram_message') as mock_tg:
        # -10%는 전역 기준(-7%)은 넘었지만 실제 손절선(-12%)은 아직 이탈 전 → 경보 없음
        trader._alert_unmanaged_stop('069500', 'KODEX 200', _holding(-10.0), 'ETF', trades)
        mock_tg.assert_not_called()

        trader._alert_unmanaged_stop('069500', 'KODEX 200', _holding(-13.0), 'ETF', trades)
        mock_tg.assert_called_once()


def test_unmanaged_stop_silent_when_stop_disabled(trader):
    """손절 기준 자체가 없으면(0=미사용) 경보 기준도 없다."""
    config.SELL_STRATEGY["STOP_LOSS_RATE"] = 0.0
    with patch('modules.auto_trade.api.send_telegram_message') as mock_tg:
        trader._alert_unmanaged_stop('069500', 'KODEX 200', _holding(-50.0), 'ETF')
        mock_tg.assert_not_called()
    config.SELL_STRATEGY["STOP_LOSS_RATE"] = -7.0
