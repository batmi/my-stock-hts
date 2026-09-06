"""반복 오류 경보와 방어 모드 발동 알림도 전달을 확인한 뒤에 표시한다.

[왜]
 · 반복 오류 경보 — 쿨다운을 **보내기 전에** 찍었다. 본문이 "신규 매수는 계속되므로
   원인 확인이 필요합니다" 라, 못 닿으면 그 쿨다운 동안 매수가 계속 나가는데 아무도 모른다.
 · 방어 모드 발동 알림 — 이 알림은 **하루에 한 번**만 나간다(같은 날 재발동은
   `buy_halted and buy_halt_date == today` 가 막는다). 한 번 못 닿으면 그날은 끝이다.
   상태(buy_halted)는 이미 뒤집혔으므로 되돌리지 않되, 못 닿은 사실은 남겨야 한다.
"""
import logging
import time

import pytest

from modules.auto_trade import AutoTrader
from modules.auto_trade import trader as tr


@pytest.fixture
def trader(monkeypatch):
    AutoTrader._instance = None
    t = AutoTrader()
    t._code_error_alerted_at = 0.0
    t.buy_halted = False
    t.buy_halt_date = None
    monkeypatch.setattr(type(t), 'log', lambda self, *a, **k: None, raising=False)
    yield t
    AutoTrader._instance = None


def _patch(monkeypatch, delivered):
    #  _errors_are_not_the_server 는 서버가 정상일 때만 경보 자리로 간다.
    monkeypatch.setattr(tr.api, 'check_server_health', lambda *a, **k: True, raising=False)
    sent = []
    monkeypatch.setattr(tr._pkg(), 'alert_delivered',
                        lambda m, urgent=False: (sent.append(m), delivered)[1], raising=False)
    return sent


# ─────────────────────────── 반복 오류 경보
def test_반복오류_경보를_못_보내면_쿨다운을_찍지_않는다(trader, monkeypatch):
    sent = _patch(monkeypatch, delivered=False)
    trader._errors_are_not_the_server("타입 오류")
    assert sent, "경보 시도조차 없었다"
    assert trader._code_error_alerted_at == 0.0, "못 닿았는데 쿨다운이 찍혔다"


def test_반복오류_경보가_전달되면_쿨다운을_찍는다(trader, monkeypatch):
    _patch(monkeypatch, delivered=True)
    trader._errors_are_not_the_server("타입 오류")
    assert trader._code_error_alerted_at > 0


def test_쿨다운_안에는_다시_보내지_않는다(trader, monkeypatch):
    sent = _patch(monkeypatch, delivered=True)
    trader._errors_are_not_the_server("타입 오류")
    trader._errors_are_not_the_server("타입 오류")
    assert len(sent) == 1


# ─────────────────────────── 방어 모드 발동
def test_방어모드는_알림_실패와_무관하게_발동한다(trader, monkeypatch):
    """상태를 되돌리면 안 된다 — 신규 매수를 막는 것이 이 함수의 목적이다."""
    _patch(monkeypatch, delivered=False)
    assert trader.halt_buys("일일 손실 한도 도달", notify_msg="⚠️ 방어 모드") is True
    assert trader.buy_halted is True


def test_방어모드_알림을_못_보내면_그_사실이_남는다(trader, monkeypatch, caplog):
    """이 알림은 하루 한 번뿐이라 한 번 놓치면 그날은 끝이다."""
    _patch(monkeypatch, delivered=False)
    with caplog.at_level(logging.WARNING, logger="modules.auto_trade.trader"):
        trader.halt_buys("일일 손실 한도 도달", notify_msg="⚠️ 방어 모드")
    assert any("닿지 않았습니다" in r.message for r in caplog.records), \
        f"조용히 지나갔다: {[r.message for r in caplog.records]}"


def test_전달되면_조용하다(trader, monkeypatch, caplog):
    _patch(monkeypatch, delivered=True)
    with caplog.at_level(logging.WARNING, logger="modules.auto_trade.trader"):
        trader.halt_buys("일일 손실 한도 도달", notify_msg="⚠️ 방어 모드")
    assert not [r for r in caplog.records if "닿지 않았습니다" in r.message]
