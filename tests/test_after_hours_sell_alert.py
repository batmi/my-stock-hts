"""장 마감 후 감지된 매도 신호가 조용히 묻히지 않는가.

[사고 시나리오] 15:20 이후 청산 신호(손절·트레일링)가 뜬다. 마감이라 주문은 못 낸다.
종전에는 `[장마감] 매도 신호 감지 (주문 미전송)` 로그 한 줄만 남고 끝났다. 청산이 다음
개장까지 밀리는데 운영자는 그 사실을 모른다 — 손절이면 하룻밤 갭이 그대로 손실이다.

[스팸을 막아야 하는 이유] 마감 뒤에도 분석 주기는 계속 돈다. 매 주기 알리면 같은
신호가 수십 번 울려 진짜 신호가 묻힌다. 세션당 한 번만 보내되, 사유가 바뀌면 다시
알린다(트레일링과 손절은 운영자가 할 판단이 다르다).
"""
from unittest.mock import patch

import pandas as pd
import pytest

from modules.auto_trade import AutoTrader

CODE, NAME = "005930", "삼성전자"
BUY, NOW = 100_000, 88_000


@pytest.fixture
def trader():
    AutoTrader._instance = None
    t = AutoTrader()
    t.is_running = True
    t.market_index_status = {}
    t.market_status_notified = {}
    t.unmanaged_stop_notified = {}
    t.no_sellable_streak = {}
    t.after_hours_sell_notified = {}
    return t


def _holding(qty=10, price=NOW):
    return [{'pdno': CODE, 'prdt_name': NAME, 'hldg_qty': str(qty), 'ord_psbl_qty': str(qty),
             'pchs_avg_pric': str(BUY), 'pchs_amt': str(BUY * qty), 'prpr': str(price),
             'evlu_amt': str(price * qty), 'evlu_pfls_amt': str((price - BUY) * qty),
             'evlu_pfls_rt': f"{(price - BUY) / BUY * 100:.2f}"}]


_DEFAULT = object()


def _cycle(trader, is_market_open=False, reason='ATR손절', holdings=_DEFAULT):
    """한 주기 매도 판정 → (텔레그램 mock, 주문 전송 mock)."""
    holdings = _holding() if holdings is _DEFAULT else holdings
    df = pd.DataFrame({'close': [NOW], 'high': [NOW], 'low': [NOW],
                       'open': [NOW], 'volume': [1000]})
    with patch('modules.auto_trade.api.send_telegram_message') as mock_tg, \
         patch('modules.auto_trade.load_restricted_stocks', return_value={}), \
         patch('modules.auto_trade.api.fetch_sellable_quantity', return_value=10), \
         patch('modules.auto_trade.api.get_chart_data', return_value=df), \
         patch('modules.auto_trade.api.get_price_limits', return_value=(0, 0)), \
         patch('modules.auto_trade.DefaultStrategy.analyze_sell') as mock_analyze, \
         patch.object(trader.order_manager, 'is_pending', return_value=False), \
         patch.object(trader.order_manager, 'send_order', return_value='1') as mock_send:
        mock_analyze.return_value = {'action': 'sell', 'reason': reason, 'score': 1.0,
                                     'state': '매도', 'ind': {'rsi': 20, 'adx': 30, 'cci': -200}}
        trader._check_sell_conditions(holdings, is_market_open=is_market_open)
    return mock_tg, mock_send


def _bodies(mock_tg):
    return [c.args[0] for c in mock_tg.call_args_list if c.args]


def _alerts(mock_tg):
    return [b for b in _bodies(mock_tg) if "장마감 후 매도 신호" in b]


# ---------------------------------------------------------------------------
# 알린다
# ---------------------------------------------------------------------------
def test_after_hours_sell_signal_is_notified(trader):
    """마감 뒤 청산 신호는 주문이 안 나가므로 반드시 알려야 한다."""
    tg, send = _cycle(trader)
    assert not send.called, "장 마감인데 주문이 나갔다"

    alerts = _alerts(tg)
    assert len(alerts) == 1, f"알림이 없다: {_bodies(tg)}"
    body = alerts[0]
    assert NAME in body and CODE in body
    assert "ATR손절" in body
    # 확정이 아니라는 사실을 반드시 적어야 한다 — 개장 때 다시 판정한다
    assert "다시 판정" in body


def test_open_market_does_not_use_this_alert(trader):
    """장중에는 주문이 실제로 나가므로 이 알림이 울리면 안 된다."""
    tg, send = _cycle(trader, is_market_open=True)
    assert send.called
    assert not _alerts(tg)


# ---------------------------------------------------------------------------
# 스팸 방지
# ---------------------------------------------------------------------------
def test_repeated_cycles_do_not_spam(trader):
    """마감 뒤에도 주기는 계속 돈다. 같은 신호를 매번 알리면 안 된다."""
    total = 0
    for _ in range(5):
        tg, _s = _cycle(trader)
        total += len(_alerts(tg))
    assert total == 1, f"같은 신호가 {total}번 울렸다"


def test_changed_reason_alerts_again(trader):
    """손절과 트레일링은 운영자가 할 판단이 다르다 — 사유가 바뀌면 다시 알린다."""
    _cycle(trader, reason='ATR손절')
    tg, _s = _cycle(trader, reason='트레일링스탑')
    assert len(_alerts(tg)) == 1


def test_market_open_rearms_the_throttle(trader):
    """개장 주기를 지나면 스로틀이 풀려, 그날 마감 뒤 다시 알린다."""
    _cycle(trader)                                   # 마감 — 1회 알림
    _cycle(trader, is_market_open=True)              # 개장 — 스로틀 해제
    tg, _s = _cycle(trader)                          # 다시 마감
    assert len(_alerts(tg)) == 1


def test_throttle_is_dropped_when_position_closes(trader):
    """청산된 종목의 스로틀을 남기면, 재매수 후 같은 사유의 신호가 묻힌다."""
    _cycle(trader)
    assert CODE in trader.after_hours_sell_notified

    _cycle(trader, holdings=[])                      # 보유 없음
    assert CODE not in trader.after_hours_sell_notified

    tg, _s = _cycle(trader)                          # 재매수 후 같은 사유
    assert len(_alerts(tg)) == 1


# ---------------------------------------------------------------------------
# 스캔 게이트 — 마감 후 신호를 '감지하는' 경로가 실제로 도는가
# ---------------------------------------------------------------------------
#  [배경] 메인 루프는 마감과 동시에 분석을 통째로 멈춘다(트래픽 절감). 그래서 이 알림을
#  붙여도 감지 자체가 일어나지 않아 영영 울리지 않는다 — 실제로 그 상태였다.
#  마감 후 1회 스캔이 이 공백을 메운다. 게이트가 깨지면 알림 전체가 죽는다.
import modules.auto_trade as at_pkg


def _scan(trader, hhmm="1600", holiday=False, holdings=_DEFAULT, enabled=True):
    from datetime import datetime as _dt

    class _Now(_dt):
        @classmethod
        def now(cls, tz=None):
            return _dt(2026, 8, 10, int(hhmm[:2]), int(hhmm[2:]))

    holdings = _holding() if holdings is _DEFAULT else holdings
    with patch.object(at_pkg.trader, 'datetime', _Now), \
         patch('modules.auto_trade.api.is_holiday_today', return_value=holiday), \
         patch('modules.auto_trade.api.get_domestic_balance', return_value=(holdings, {})), \
         patch.object(at_pkg.config, 'AFTER_HOURS_SELL_ALERT', enabled), \
         patch.object(trader, '_check_sell_conditions') as mock_check:
        trader._scan_after_hours_sell_signals("12345678")
    return mock_check


def test_scan_runs_after_close_and_never_orders(trader):
    """종가 확정 후에는 돌아야 한다. 그리고 주문 경로가 아님을 인자로 못박는다."""
    check = _scan(trader)
    assert check.called, "마감 후 스캔이 돌지 않았다 — 알림이 영영 울리지 않는다"
    assert check.call_args.kwargs["is_market_open"] is False


def test_scan_waits_for_the_closing_auction(trader):
    """15:20~15:30 종가 단일가 중에는 일봉이 아직 확정 전이다."""
    assert not _scan(trader, hhmm="1525").called


def test_scan_skips_holidays(trader):
    """휴장일에는 판정할 새 봉이 없다."""
    assert not _scan(trader, hhmm="1600", holiday=True).called


def test_scan_runs_once_per_trading_day(trader):
    """마감 후에도 루프는 계속 돈다. 매 주기 스캔하면 잔고 조회만 낭비된다."""
    assert _scan(trader).called
    assert not _scan(trader).called


def test_scan_skips_when_no_holdings(trader):
    """보유가 없으면 청산할 것도 없다."""
    assert not _scan(trader, holdings=[]).called


def test_scan_can_be_disabled(trader):
    assert not _scan(trader, enabled=False).called
