"""토큰 경보의 스로틀은 **전달을 확인한 뒤에** 찍는다.

[왜] check_and_refresh_token_if_expired 의 두 알림은 send_telegram_message 를 직접 불렀다.
 그 함수는 기본이 비동기라 예외를 던지지 않는다 — 네트워크가 끊겨 있어도 감싼 try 는
 아무것도 잡지 못하고 스로틀이 '보냈다'로 굳는다(alert_delivered 를 만든 바로 그 이유,
 2026-09-04). 여기서 놓치면:
   · 지연 경보(1시간 스로틀) — 토큰이 없으면 주문도 조회도 못 나가는데 그 한 시간이
     통째로 침묵이 된다.
   · 복구 알림 — 표식(LAST_TOKEN_REFRESH_ALERT)만 지워지고 알림은 안 가서, 운영자는
     **장애가 아직 계속되는 줄** 안다.
"""
import time
from unittest.mock import patch

import pytest

import api
import config
from api import auth
from core import context


@pytest.fixture(autouse=True)
def clean(monkeypatch):
    monkeypatch.setattr(context, 'TOKEN_EXPIRED', True, raising=False)
    monkeypatch.setattr(context, 'LAST_TOKEN_REFRESH_ATTEMPT', 0, raising=False)
    monkeypatch.setattr(context, 'LAST_TOKEN_REFRESH_ALERT', 0, raising=False)
    monkeypatch.setattr(config.session, 'is_toss', False, raising=False)
    monkeypatch.setattr(config.session, 'auto_app_key', '', raising=False)
    monkeypatch.setattr(api, '_is_screen_output_allowed', lambda: False, raising=False)
    yield


def _run(token_ok, delivered):
    sent = []
    with patch.object(api, 'get_real_access_token', return_value=token_ok, create=True), \
         patch.object(api, 'alert_delivered',
                      side_effect=lambda m, urgent=False: (sent.append(m), delivered)[1],
                      create=True), \
         patch.object(api, 'send_telegram_message',
                      side_effect=lambda *a, **k: sent.append(a[0] if a else ''),
                      create=True):
        auth.check_and_refresh_token_if_expired()
    return sent


def test_지연_경보를_못_보내면_스로틀을_찍지_않는다():
    sent = _run(token_ok=False, delivered=False)
    assert sent, "경보 시도조차 없었다"
    assert context.LAST_TOKEN_REFRESH_ALERT == 0, \
        "못 보냈는데 1시간 스로틀이 찍혔다 — 그 사이 주문이 안 나가는 것을 아무도 모른다"


def test_지연_경보가_전달되면_스로틀을_찍는다():
    _run(token_ok=False, delivered=True)
    assert context.LAST_TOKEN_REFRESH_ALERT > 0


def test_복구_알림을_못_보내면_표식을_지우지_않는다(monkeypatch):
    """표식만 지우면 운영자는 장애가 계속되는 줄 안다."""
    monkeypatch.setattr(context, 'LAST_TOKEN_REFRESH_ALERT', time.time() - 10, raising=False)
    before = context.LAST_TOKEN_REFRESH_ALERT
    _run(token_ok="TOKEN", delivered=False)
    assert context.LAST_TOKEN_REFRESH_ALERT == before, "알림은 못 갔는데 표식이 지워졌다"


def test_복구_알림이_전달되면_표식을_지운다(monkeypatch):
    monkeypatch.setattr(context, 'LAST_TOKEN_REFRESH_ALERT', time.time() - 10, raising=False)
    _run(token_ok="TOKEN", delivered=True)
    assert context.LAST_TOKEN_REFRESH_ALERT == 0
    assert context.TOKEN_EXPIRED is False


def test_api_층이_alert_delivered_를_내보낸다():
    """재수출이 빠지면 위 자리들이 AttributeError 로 조용히 except 에 먹힌다."""
    assert callable(getattr(api, 'alert_delivered', None))
