"""미체결 조회 실패는 '미체결 없음'이 아니다.

2026-09-05 감사: 중복 주문을 막는 게이트가 이 구분에 기대고 있었다 —

  · OrderManager.restore_pending_orders : None 이면 False 를 돌려 기동 직후
    pending_restore_ok 를 내린다(신규 매수·피라미딩 보류).
  · manage_unfilled_orders : `unfilled_list is not None` 일 때만 그 게이트를 푼다.

둘 다 None 을 기다리는데 `get_domestic_open_orders` 는 **절대 None 을 돌려주지 않았다**
(KIS 는 rt_cd != '0' 에서 [], 토스는 예외에서 []). 그래서 **게이트가 첫 주기에 무조건
풀렸다** — 조회가 실패하는 동안에도 시스템은 '미체결 없음'으로 믿고 신규 주문을 낸다.
거래소에 이미 걸린 주문을 못 본 채 두 번째를 내는 자리다
([[order-timeout-no-resend]] 와 같은 규약).
"""
from unittest.mock import patch

import pytest

import api
import config


@pytest.fixture(autouse=True)
def _live_mode(monkeypatch):
    monkeypatch.setattr(config.session, 'is_paper', False, raising=False)
    monkeypatch.setattr(config.session, 'is_toss', False, raising=False)


def test_kis_failure_returns_unknown(monkeypatch):
    monkeypatch.setattr(api, '_prepare_account_params', lambda c, a: ("1", "01"))
    with patch.object(api, 'call_api', return_value={'rt_cd': '1', 'msg_cd': 'X', 'msg1': '실패'}):
        assert api.get_domestic_open_orders() is None


def test_kis_success_returns_the_list(monkeypatch):
    monkeypatch.setattr(api, '_prepare_account_params', lambda c, a: ("1", "01"))
    rows = [{'odno': '1', 'pdno': '005930', 'rmn_qty': '10'}]
    with patch.object(api, 'call_api', return_value={'rt_cd': '0', 'output': rows}):
        assert api.get_domestic_open_orders() == rows


def test_empty_is_still_a_real_answer(monkeypatch):
    """빈 목록은 '미체결 없음'이라는 정상 응답이다 — None 과 구분된다."""
    monkeypatch.setattr(api, '_prepare_account_params', lambda c, a: ("1", "01"))
    with patch.object(api, 'call_api', return_value={'rt_cd': '0', 'output': []}):
        assert api.get_domestic_open_orders() == []


def test_toss_failure_returns_unknown(monkeypatch):
    monkeypatch.setattr(config.session, 'is_toss', True, raising=False)
    from api import toss as at
    with patch.object(at.toss_api, 'get_orders',
                      side_effect=at.toss_api.TossApiError("x", "boom")):
        assert api.get_domestic_open_orders() is None


def test_paper_mode_still_returns_empty_list(monkeypatch):
    """관찰 모드는 즉시 전량 체결이라 미체결이 **정말로** 없다 — 실패가 아니다."""
    monkeypatch.setattr(config.session, 'is_paper', True, raising=False)
    assert api.get_domestic_open_orders() == []


def test_overseas_all_exchanges_failing_is_unknown(monkeypatch):
    monkeypatch.setattr(api, '_prepare_account_params', lambda c, a: ("1", "01"))
    with patch.object(api, 'call_api', return_value={'rt_cd': '1', 'msg1': '실패'}):
        assert api.get_overseas_open_orders() is None


def test_overseas_partial_failure_returns_what_was_read(monkeypatch):
    """일부 거래소만 실패하면 읽은 만큼 돌려준다 — 그쪽 주문은 실제로 확인됐다."""
    monkeypatch.setattr(api, '_prepare_account_params', lambda c, a: ("1", "01"))
    seq = iter([{'rt_cd': '0', 'output': [{'odno': '1', 'pdno': 'AAPL'}]},
                {'rt_cd': '1', 'msg1': '실패'},
                {'rt_cd': '1', 'msg1': '실패'}])
    with patch.object(api, 'call_api', side_effect=lambda *a, **k: next(seq)):
        out = api.get_overseas_open_orders()
    assert out is not None and len(out) == 1


# --------------------------------------------------------------------------
# 게이트가 실제로 내려가는가
# --------------------------------------------------------------------------
def test_restore_gate_drops_on_unknown(trader_om):
    with patch('modules.auto_trade.api.get_domestic_open_orders', return_value=None):
        assert trader_om.restore_pending_orders() is False


def test_restore_gate_holds_on_empty(trader_om):
    with patch('modules.auto_trade.api.get_domestic_open_orders', return_value=[]):
        assert trader_om.restore_pending_orders() is True


@pytest.fixture
def trader_om():
    from modules import auto_trade
    auto_trade.AutoTrader._instance = None
    t = auto_trade.AutoTrader()
    yield t.order_manager
    auto_trade.AutoTrader._instance = None


def test_manage_unfilled_does_not_release_the_gate_on_unknown(trader_om):
    """조회 실패면 pending_restore_ok 를 풀면 안 된다 — 이 함수가 게이트의 해제자다."""
    trader_om.trader.pending_restore_ok = False
    with patch('modules.auto_trade.api.get_unfilled_orders', return_value=None), \
         patch.object(trader_om.trader, 'is_market_open', return_value=True):
        trader_om.manage_unfilled_orders()
    assert trader_om.trader.pending_restore_ok is False, \
        "조회 실패인데 '미체결 현황을 안다'로 풀렸다"


def test_manage_unfilled_releases_the_gate_on_empty(trader_om):
    trader_om.trader.pending_restore_ok = False
    with patch('modules.auto_trade.api.get_unfilled_orders', return_value=[]), \
         patch.object(trader_om.trader, 'is_market_open', return_value=True):
        trader_om.manage_unfilled_orders()
    assert trader_om.trader.pending_restore_ok is True
