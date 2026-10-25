"""경보 스로틀은 **전달을 확인한 뒤에** 찍어야 한다.

2026-09-04 감사: 매매 경보 넷(손절선 이탈·장마감 매도 신호·지수 판단보류·주문 실패)이
모두 보내기 **전에** 스로틀을 찍었다. `api.send_telegram_message` 는 기본이 비동기라
실패해도 예외를 던지지 않으므로 호출부의 try 는 아무것도 잡지 못한다 — 네트워크가 끊긴
동안의 경보가 전부 '보냈다'로 굳는다. 같은 결함을 캘린더·공시 알림은 이미 고쳤는데
(modules/manage/events.py) 매매 쪽만 남아 있었다.

특히 손절선 이탈 경보는 스스로 '시스템이 손절해 주지 않는 포지션의 마지막 안전망'이라고
적어 둔 알림이고, 한 번 놓치면 손절선 아래에서 24시간 침묵한다.
"""
import time
from unittest.mock import patch

import pytest

import config
from modules import auto_trade
from modules.auto_trade import common


@pytest.fixture
def trader():
    auto_trade.AutoTrader._instance = None
    t = auto_trade.AutoTrader()
    t.is_running = True
    t.unmanaged_stop_notified = {}
    t.after_hours_sell_notified = {}
    t.market_status_notified = {}
    yield t
    auto_trade.AutoTrader._instance = None


def _holding(rate=-12.0):
    return {'pdno': '005930', 'prdt_name': '삼성전자', 'hldg_qty': '10',
            'evlu_pfls_rt': str(rate), 'evlu_amt': '1000000', 'evlu_pfls_amt': '-120000',
            'pchs_avg_pric': '10000', 'prpr': '8800'}


# --------------------------------------------------------------------------
# alert_delivered 자체
# --------------------------------------------------------------------------
def test_unconfigured_telegram_counts_as_delivered(monkeypatch):
    """토큰이 없으면 '전송 실패'가 아니라 '알림 수단 없음'이다 — 로그가 알림 역할을 한다."""
    monkeypatch.setattr(config, 'TELEGRAM_BOT_TOKEN', '', raising=False)
    monkeypatch.setattr(config, 'TELEGRAM_CHAT_ID', '', raising=False)
    with patch('modules.telegram_notify.send_telegram_message') as send:
        assert common.alert_delivered("x") is True
    send.assert_not_called()


def test_configured_telegram_is_sent_synchronously(monkeypatch):
    """전달 여부를 알아야 하므로 동기 전송이어야 한다(비동기는 None 을 준다)."""
    monkeypatch.setattr(config, 'TELEGRAM_BOT_TOKEN', 'tok', raising=False)
    monkeypatch.setattr(config, 'TELEGRAM_CHAT_ID', 'chat', raising=False)
    with patch('modules.telegram_notify.send_telegram_message', return_value=True) as send:
        assert common.alert_delivered("x") is True
    assert send.call_args.kwargs.get('sync') is True


def test_send_failure_is_reported(monkeypatch):
    monkeypatch.setattr(config, 'TELEGRAM_BOT_TOKEN', 'tok', raising=False)
    monkeypatch.setattr(config, 'TELEGRAM_CHAT_ID', 'chat', raising=False)
    with patch('modules.telegram_notify.send_telegram_message', return_value=False):
        assert common.alert_delivered("x") is False
    with patch('modules.telegram_notify.send_telegram_message', side_effect=OSError("down")):
        assert common.alert_delivered("x") is False


# --------------------------------------------------------------------------
# 손절선 이탈 경보 (마지막 안전망)
# --------------------------------------------------------------------------
def test_unmanaged_stop_retries_when_undelivered(trader):
    """전송 실패면 24시간이 아니라 짧게 재시도한다 — 침묵도 도배도 아니게."""
    with patch.object(trader, '_effective_stop_loss_rate', return_value=-7.0), \
         patch('modules.auto_trade.alert_delivered', return_value=False) as send:
        trader._alert_unmanaged_stop('005930', '삼성전자', _holding(), "ETF 제외")
        assert send.call_count == 1
        until = trader.unmanaged_stop_notified['005930']
        assert until - time.time() <= common.ALERT_RETRY_SEC + 1

        #  재시도 창이 지나면 다시 시도한다.
        trader.unmanaged_stop_notified['005930'] = time.time() - 1
        trader._alert_unmanaged_stop('005930', '삼성전자', _holding(), "ETF 제외")
        assert send.call_count == 2


def test_unmanaged_stop_throttles_a_day_when_delivered(trader):
    with patch.object(trader, '_effective_stop_loss_rate', return_value=-7.0), \
         patch('modules.auto_trade.alert_delivered', return_value=True) as send:
        trader._alert_unmanaged_stop('005930', '삼성전자', _holding(), "ETF 제외")
        trader._alert_unmanaged_stop('005930', '삼성전자', _holding(), "ETF 제외")
    assert send.call_count == 1
    assert trader.unmanaged_stop_notified['005930'] - time.time() > 86000


def test_unmanaged_stop_recovery_clears_the_throttle(trader):
    """손절선 위로 회복하면 스로틀을 푼다(재이탈 시 즉시 알림)."""
    with patch.object(trader, '_effective_stop_loss_rate', return_value=-7.0), \
         patch('modules.auto_trade.alert_delivered', return_value=True):
        trader._alert_unmanaged_stop('005930', '삼성전자', _holding(-12.0), "ETF 제외")
        assert '005930' in trader.unmanaged_stop_notified
        trader._alert_unmanaged_stop('005930', '삼성전자', _holding(+3.0), "ETF 제외")
    assert '005930' not in trader.unmanaged_stop_notified


# --------------------------------------------------------------------------
# 장마감 매도 신호 · 지수 판단보류
# --------------------------------------------------------------------------
def test_after_hours_sell_not_latched_when_undelivered(trader):
    item = _holding()
    with patch('modules.auto_trade.alert_delivered', return_value=False):
        trader._alert_after_hours_sell('005930', '삼성전자', item, "손절(-12.0%)", 8800, 8780, 10)
    assert '005930' not in trader.after_hours_sell_notified

    with patch('modules.auto_trade.alert_delivered', return_value=True):
        trader._alert_after_hours_sell('005930', '삼성전자', item, "손절(-12.0%)", 8800, 8780, 10)
    assert trader.after_hours_sell_notified['005930'] == "손절(-12.0%)"


def test_market_unknown_latch_waits_for_delivery(trader):
    """래치는 회복 때만 풀린다 — 전송 실패로 걸리면 운영자가 끝까지 모른다."""
    with patch('modules.auto_trade.alert_delivered', return_value=False):
        trader._notify_market_unknown("KOSDAQ")
    assert not trader.market_status_notified.get("KOSDAQ")

    with patch('modules.auto_trade.alert_delivered', return_value=True):
        trader._notify_market_unknown("KOSDAQ")
    #  [2026-10-25] 무엇을 알렸는지 남긴다 — 약세 판정("down")과 구분해야 회복 문구·하락 알림이 맞게 나간다.
    assert trader.market_status_notified["KOSDAQ"] == "unknown"


def _index_status(trader, monkeypatch, healthy):
    """지수 판정만 바꿔 _update_market_indices_status 한 바퀴 — 알림 문구를 모은다."""
    sent = []
    det = {"ready": False}
    monkeypatch.setattr("modules.auto_trade.market_risk.analysis.get_domestic_index_data", lambda m: object())
    monkeypatch.setattr("modules.auto_trade.market_filter_status",
                        lambda m, df=None: {"unknown": False, "is_healthy": healthy, "filter": det})
    monkeypatch.setattr("modules.auto_trade.market_risk.api.send_telegram_message", lambda m, *a, **k: sent.append(m))
    monkeypatch.setattr(type(trader), 'log', lambda self, *a, **k: None, raising=False)
    trader._update_market_indices_status()
    return sent


def test_판단_보류_뒤_이평선_아래로_돌아오면_하락을_알린다(trader, monkeypatch):
    """종전에는 판단 보류 표시가 하락 알림을 막아, 실제로 약세인데 운용자는 '데이터 장애'로만 알고 있었다."""
    trader.market_status_notified = {"KOSPI": "unknown", "KOSDAQ": "unknown"}
    sent = _index_status(trader, monkeypatch, healthy=False)
    assert sum("📉 [시장 감지]" in m for m in sent) == 2, sent
    assert trader.market_status_notified == {"KOSPI": "down", "KOSDAQ": "down"}


def test_판단_보류_뒤_이평선_위로_돌아오면_회복이_아니라_판단_재개다(trader, monkeypatch):
    """떨어진 적이 없는데 '이평선을 회복했습니다'가 나갔다."""
    trader.market_status_notified = {"KOSPI": "unknown", "KOSDAQ": "down"}
    sent = _index_status(trader, monkeypatch, healthy=True)
    assert any(m.startswith("✅ [판단 재개] KOSPI") for m in sent), sent
    assert any(m.startswith("📈 [시장 회복] KOSDAQ") for m in sent), sent
    assert not any("KOSPI" in m and "회복했습니다" in m for m in sent)
    assert trader.market_status_notified == {"KOSPI": False, "KOSDAQ": False}


def test_약세로_알린_뒤의_판단_보류는_다시_알리지_않는다(trader):
    """매수는 이미 막혀 있다 — 데이터가 오락가락할 때 알림이 쏟아지지 않게 한다."""
    trader.market_status_notified = {"KOSDAQ": "down"}
    with patch('modules.auto_trade.alert_delivered', return_value=True) as sent:
        trader._notify_market_unknown("KOSDAQ")
    assert not sent.called and trader.market_status_notified["KOSDAQ"] == "down"


# --------------------------------------------------------------------------
# 주문 실패 알림
# --------------------------------------------------------------------------
def test_order_fail_alert_retries_when_undelivered(trader):
    om = trader.order_manager
    om.order_fail_alerted = {}
    with patch('modules.auto_trade.alert_delivered', return_value=False):
        assert om._alert_order_fail('005930', 'sell', '40310000', 'msg') is False
        key = ('005930', 'sell', '40310000')
        assert om.order_fail_alerted[key] - time.time() <= common.ALERT_RETRY_SEC + 1
    with patch('modules.auto_trade.alert_delivered', return_value=True):
        om.order_fail_alerted[key] = time.time() - 1
        assert om._alert_order_fail('005930', 'sell', '40310000', 'msg') is True
        assert om.order_fail_alerted[key] - time.time() > om.ORDER_FAIL_ALERT_COOLDOWN - 5


def test_no_trading_alert_stamps_before_sending():
    """스로틀을 먼저 찍고 나중에 보내는 형태가 다시 생기지 않게 막는다."""
    import inspect

    for fn in (auto_trade.AutoTrader._alert_unmanaged_stop,
               auto_trade.AutoTrader._alert_after_hours_sell,
               auto_trade.AutoTrader._notify_market_unknown,
               auto_trade.OrderManager._alert_order_fail):
        src = inspect.getsource(fn)
        assert "alert_delivered" in src, f"{fn.__name__} 이 전달 확인을 거치지 않는다"
        assert "api.send_telegram_message" not in src, (
            f"{fn.__name__} 이 전달 확인 없이 직접 보낸다")
