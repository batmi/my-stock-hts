"""텔레그램 원격 제한 명령(/addrestrict·/delrestrict)의 계좌 스코프 검증.

[왜 중요한가] 제한 목록에는 성격이 다른 두 종류가 섞여 있다.

  · 전역 차단 — 운용자가 "이 종목은 매매하지 마라"고 지정한 것(/addrestrict).
  · 계좌별 수동매매 보호 — 운용자가 **그 계좌에서 직접 매수**해 시스템이 자동으로
    등록한 것. 시스템이 제 손절 기준으로 운용자의 포지션을 청산하지 않게 막는다.

/delrestrict는 앞의 것만 풀어야 한다. 종전 구현은 종목 항목을 통째로 삭제해서,
운용자가 무관한 전역 차단을 푸는 순간 뒤의 보호까지 조용히 걷어냈다. 그 뒤 시스템은
운용자가 직접 산 포지션을 자기 ATR 손절로 팔아버린다 — 운용자는 그런 지시를 한 적이
없고, 알림도 '제한 해제 완료' 한 줄뿐이라 알아챌 방법도 없다.
"""
import pytest

import config
from modules import auto_trade
from modules.auto_trade import common

AUTO = ("44048158", "01")
CODE, NAME = "005930", "삼성전자"


@pytest.fixture
def bot(tmp_path, monkeypatch):
    monkeypatch.setattr(common, 'RESTRICTED_FILE', str(tmp_path / "r.json"), raising=False)
    from modules import telegram_bot
    b = telegram_bot.TelegramCommander.__new__(telegram_bot.TelegramCommander)
    monkeypatch.setattr(b, '_resolve_stock', lambda kw: (CODE, NAME, False), raising=False)
    return b


def test_delrestrict_keeps_the_account_scoped_manual_hold_protection(bot):
    """전역 차단만 풀고, 계좌별 수동매매 보호는 남긴다."""
    auto_trade.add_restricted_stock(CODE, NAME, "수동매매", cano=AUTO[0], acnt=AUTO[1])
    auto_trade.add_restricted_stock(CODE, NAME, "텔레그램 원격 차단")

    reply = bot._cmd_delrestrict([CODE])

    assert CODE in auto_trade.get_restricted_stocks(*AUTO), (
        "계좌별 수동매매 보호까지 함께 지워졌다 — 시스템이 운용자의 수동 매수분을 "
        "제 손절 기준으로 청산하게 된다")
    assert "계좌별 제한은 그대로" in reply, f"남은 제한을 운용자에게 알리지 않았다: {reply}"


def test_delrestrict_removes_the_entry_when_nothing_remains(bot):
    """남은 사유가 없으면 항목 자체를 지운다(잔여물 방지)."""
    auto_trade.add_restricted_stock(CODE, NAME, "텔레그램 원격 차단")

    reply = bot._cmd_delrestrict([CODE])

    assert CODE not in auto_trade.load_restricted_stocks()
    assert "해제 완료" in reply
    assert "계좌별 제한은 그대로" not in reply


def test_delrestrict_reports_when_the_stock_is_not_restricted(bot):
    reply = bot._cmd_delrestrict([CODE])
    assert "제한 목록에 없습니다" in reply


def test_addrestrict_registers_a_global_block(bot):
    """/addrestrict는 계좌를 가리지 않는 전역 차단이어야 한다(모든 계좌에 적용)."""
    bot._cmd_addrestrict([CODE, "어닝쇼크"])

    entry = auto_trade.load_restricted_stocks()[CODE]
    assert "어닝쇼크" in entry.get('memo', ''), "전역 사유로 등록되지 않았다"
    assert not entry.get('accounts'), "전역 차단인데 특정 계좌에만 걸렸다"
    assert CODE in auto_trade.get_restricted_stocks(*AUTO)


def test_addrestrict_then_delrestrict_is_a_round_trip(bot):
    """운용자가 걸고 푸는 왕복이 잔여물 없이 끝난다."""
    bot._cmd_addrestrict([CODE])
    assert CODE in auto_trade.get_restricted_stocks(*AUTO)

    bot._cmd_delrestrict([CODE])
    assert CODE not in auto_trade.get_restricted_stocks(*AUTO)
    assert CODE not in auto_trade.load_restricted_stocks()


# ─────────────────── 정지 시 미체결 주문 고지 ───────────────────

def test_stop_message_warns_about_pending_orders(monkeypatch):
    """정지 시 거래소에 남은 미체결 주문을 운용자에게 알린다.

    시스템을 꺼도 주문은 살아 있다. 정지 뒤 체결되면 손절·트레일링 감시가 없는
    포지션이 되는데, '최종 보유 종목'은 정지 시점 잔고라 "없음"으로 끝난다 —
    운용자가 무감시 포지션의 존재를 알 방법이 없었다.
    """
    import threading
    from modules import auto_trade as at

    trader = at.AutoTrader()
    om = trader.order_manager
    with om._lock:
        saved = dict(om.pending_orders)
        om.pending_orders = {"005930": {"ODNO1": 1}, "000660": {"ODNO2": 1, "ODNO3": 1}}
    try:
        sent = []
        monkeypatch.setattr(at.api, 'send_telegram_message', lambda m, **k: sent.append(m))
        # stop()은 is_running=False면 즉시 반환한다 — 가동 중 정지를 재현한다.
        monkeypatch.setattr(trader, 'is_running', True, raising=False)
        monkeypatch.setattr(trader, 'thread', None, raising=False)

        trader.stop(use_status=False)

        body = "\n".join(sent)
        assert "미체결 주문 3건 잔존" in body, f"미체결 고지가 없다: {body[:400]}"
        assert "005930" in body and "000660" in body
    finally:
        with om._lock:
            om.pending_orders = saved
