"""매도를 결정했는데 팔 수 없는 포지션이 조용히 방치되지 않는가.

[사고 시나리오] 시스템이 손절을 결정했는데 증권사가 매도가능수량 0을 준다. 종전에는
로그 한 줄만 남기고 끝나서, 매 주기 반복되는데도 운영자는 아무것도 몰랐다.

자기 미체결 주문은 그 앞의 is_pending 검사에서 이미 걸러진다. 따라서 여기까지 왔다면
남는 원인은 **거래정지·상장폐지·HTS에서 직접 낸 매도 주문에 물량이 묶임**이다. 전부
시스템이 스스로 빠져나올 수 없는 상태이므로, 추세추종 원칙("탈출 전략이 없다면
포지션을 잡지 마라")에 따라 운영자에게 넘겨야 한다.

[오경보를 막아야 하는 이유] 미체결 취소 직후 한 주기 정도는 정상적으로 0이 된다.
즉시 알리면 평범한 운영 중에도 경보가 울려 진짜 신호가 묻힌다. 연속 관측될 때만 알린다.
"""
from unittest.mock import patch

import pandas as pd
import pytest

from modules.auto_trade import AutoTrader
from modules.auto_trade.engine import NO_SELLABLE_ALERT_CYCLES, UNMANAGED_NO_SELLABLE

CODE, NAME = "005930", "삼성전자"
BUY, NOW = 100_000, 88_000       # -12% — 손절 기준(-7%) 아래


@pytest.fixture
def trader():
    AutoTrader._instance = None
    t = AutoTrader()
    t.is_running = True
    t.market_index_status = {}
    t.market_status_notified = {}
    t.unmanaged_stop_notified = {}
    t.no_sellable_streak = {}
    return t


def _holding(qty=10, price=NOW):
    return [{'pdno': CODE, 'prdt_name': NAME, 'hldg_qty': str(qty), 'ord_psbl_qty': str(qty),
             'pchs_avg_pric': str(BUY), 'pchs_amt': str(BUY * qty), 'prpr': str(price),
             'evlu_amt': str(price * qty), 'evlu_pfls_amt': str((price - BUY) * qty),
             'evlu_pfls_rt': f"{(price - BUY) / BUY * 100:.2f}"}]


_DEFAULT = object()   # [주의] 빈 잔고([])는 falsy라 `holdings or _holding()`으로 쓰면
                      #  '청산됨' 시나리오가 조용히 기본 잔고로 바뀐다(실제로 겪었다).


def _cycle(trader, sellable, holdings=_DEFAULT):
    """한 주기 매도 판정 → (경보 mock, 주문 전송 mock)."""
    holdings = _holding() if holdings is _DEFAULT else holdings
    df = pd.DataFrame({'close': [NOW], 'high': [NOW], 'low': [NOW],
                       'open': [NOW], 'volume': [1000]})
    with patch('modules.auto_trade.api.send_telegram_message'), \
         patch('modules.auto_trade.load_restricted_stocks', return_value={}), \
         patch('modules.auto_trade.api.fetch_sellable_quantity', return_value=sellable), \
         patch('modules.auto_trade.api.get_chart_data', return_value=df), \
         patch('modules.auto_trade.api.get_price_limits', return_value=(0, 0)), \
         patch('modules.auto_trade.DefaultStrategy.analyze_sell') as mock_analyze, \
         patch.object(trader.order_manager, 'is_pending', return_value=False), \
         patch.object(trader.order_manager, 'send_order', return_value='1') as mock_send, \
         patch.object(trader, '_alert_unmanaged_stop') as mock_alert:
        mock_analyze.return_value = {'action': 'sell', 'reason': 'ATR손절', 'score': 1.0,
                                     'state': '매도', 'ind': {'rsi': 20, 'adx': 30, 'cci': -200}}
        trader._check_sell_conditions(holdings, is_market_open=True)
    return mock_alert, mock_send


# ---------------------------------------------------------------------------
# 오경보 방지
# ---------------------------------------------------------------------------
def test_single_transient_zero_does_not_alert(trader):
    """미체결 취소 직후 한 주기 0은 정상이다. 곧바로 알리면 안 된다."""
    alert, send = _cycle(trader, 0)
    assert not alert.called
    assert not send.called, "팔 수 없는데 주문이 나갔다"


def test_recovery_resets_the_streak(trader):
    """중간에 한 번이라도 팔 수 있으면 연속이 끊긴다."""
    for _ in range(NO_SELLABLE_ALERT_CYCLES - 1):
        _cycle(trader, 0)
    _cycle(trader, 10)                      # 회복 — 정상 매도
    assert trader.no_sellable_streak.get(CODE) is None

    alert, _ = _cycle(trader, 0)            # 다시 0이어도 1회차일 뿐
    assert not alert.called


def test_partial_sellable_also_resets(trader):
    """일부라도 팔렸으면 '못 빠져나오는 상태'가 아니다."""
    _cycle(trader, 0)
    _cycle(trader, 3)                       # 10주 중 3주만 가능 → 수량 조정 후 주문
    assert trader.no_sellable_streak.get(CODE) is None


def test_streak_is_dropped_when_position_closes(trader):
    """청산된 종목의 횟수를 남기면, 재매수 후 첫 일시적 0에서 곧바로 오경보가 난다."""
    for _ in range(NO_SELLABLE_ALERT_CYCLES - 1):
        _cycle(trader, 0)
    assert trader.no_sellable_streak.get(CODE)

    _cycle(trader, 0, holdings=[])          # 보유 없음(청산됨)
    assert CODE not in trader.no_sellable_streak

    alert, _ = _cycle(trader, 0)            # 재매수 후 첫 0
    assert not alert.called


# ---------------------------------------------------------------------------
# 지속되면 반드시 알린다
# ---------------------------------------------------------------------------
def test_persistent_zero_alerts(trader):
    """거래정지처럼 계속되는 상태는 운영자에게 넘겨야 한다."""
    alerts = []
    for _ in range(NO_SELLABLE_ALERT_CYCLES):
        alert, _ = _cycle(trader, 0)
        alerts.append(alert.called)

    assert alerts[:-1] == [False] * (NO_SELLABLE_ALERT_CYCLES - 1), "너무 일찍 알렸다"
    assert alerts[-1] is True, (
        "시스템이 스스로 청산할 수 없는 포지션이 로그만 남기고 방치된다")


def test_alert_carries_the_right_reason(trader):
    """사유가 뭉뚱그려지면 운영자가 조치를 오판한다(설정 문제 ≠ 시장 문제)."""
    for _ in range(NO_SELLABLE_ALERT_CYCLES - 1):
        _cycle(trader, 0)
    alert, _ = _cycle(trader, 0)
    assert UNMANAGED_NO_SELLABLE in str(alert.call_args)


def test_alert_message_says_sell_failed_not_excluded(trader):
    """문구 검증 — 이 경우는 '제외'가 아니라 '시도했는데 실패'다."""
    t = trader
    item = _holding()[0]
    with patch('modules.auto_trade.alert_delivered', return_value=True) as tg:
        t._alert_unmanaged_stop(CODE, NAME, item, UNMANAGED_NO_SELLABLE, None)
    body = str(tg.call_args)
    assert "매도 실패" in body
    assert "제외되어" not in body, "시도했는데 못 판 상황을 '대상에서 제외'로 알리면 오해한다"


def test_excluded_reason_keeps_original_wording(trader):
    """대조군 — 기존 사유(트레이딩 제한 등)의 문구는 그대로여야 한다."""
    from modules.auto_trade.engine import UNMANAGED_RESTRICTED
    with patch('modules.auto_trade.alert_delivered', return_value=True) as tg:
        trader._alert_unmanaged_stop(CODE, NAME, _holding()[0], UNMANAGED_RESTRICTED, None)
    body = str(tg.call_args)
    assert "자동매도 제외 종목" in body and "제외되어" in body
