"""'보냈다'는 표시는 실제 전달을 확인한 뒤에만 남긴다.

[배경] 공시·캘린더 알림은 같은 건을 두 번 보내지 않으려고 DB(notified_disclosures)에
표시를 남긴다. 그런데 api.send_telegram_message 는 기본이 **비동기**라 스레드/큐에
넘기고 즉시 돌아온다 — 예외를 던질 일이 없다. 그것을 try 로 감싸고 성공으로 간주했으니,
라즈베리파이의 네트워크가 끊긴 동안 접수된 공시는 아무 데도 도착하지 않은 채
'발송 완료'로 굳었다. 공시는 하루 30분 폴링, 캘린더는 하루 한 번이라 한 번 굳으면 끝이다.
"""
import pytest

import api
import config
from modules.manage import disclosure


@pytest.fixture
def telegram_off(monkeypatch):
    """토큰이 없는 상태 — 어떤 경로로도 전달될 수 없다."""
    monkeypatch.setattr(config, "TELEGRAM_BOT_TOKEN", "", raising=False)
    monkeypatch.setattr(config, "TELEGRAM_CHAT_ID", "", raising=False)


def test_sync_send_reports_delivery_failure(telegram_off):
    """sync=True 는 전달 여부를 돌려준다 — 비동기는 알 수 없으므로 None."""
    assert api.send_telegram_message("보낼 수 없는 메시지", sync=True) is False
    assert api.send_telegram_message("비동기") is None


def test_sync_send_reports_success(monkeypatch):
    monkeypatch.setattr(config, "TELEGRAM_BOT_TOKEN", "T", raising=False)
    monkeypatch.setattr(config, "TELEGRAM_CHAT_ID", "C", raising=False)

    class _Res:
        status_code = 200
        text = "ok"

    monkeypatch.setattr("modules.telegram_notify.requests.post", lambda *a, **k: _Res())
    assert api.send_telegram_message("전달됨", sync=True) is True


def _one_event():
    return [{"rcept_no": "20260904000001", "code": "005930", "name": "삼성전자",
             "date": "20260904", "category": "상장위험", "level": 2, "icon": "🔴",
             "report_nm": "테스트 공시"}]


@pytest.fixture
def stub_disclosure(monkeypatch):
    """공시 1건이 수집된 상태로 고정하고, DB 표시를 메모리로 가로챈다."""
    monkeypatch.setattr(config, "DART_API_KEY", "KEY", raising=False)
    monkeypatch.setattr(disclosure, "_kr_watchlist", lambda: [("005930", "삼성전자")])
    monkeypatch.setattr(disclosure, "collect_disclosures",
                        lambda c, n, days, lvl: _one_event())
    monkeypatch.setattr(disclosure, "_detail_eligible", lambda e: False)

    marked = set()
    from modules import db_manager
    monkeypatch.setattr(db_manager.db, "is_disclosure_notified", lambda r: r in marked)
    monkeypatch.setattr(db_manager.db, "mark_disclosure_notified", lambda r: marked.add(r))
    return marked


def test_failed_send_is_not_marked_as_notified(stub_disclosure, monkeypatch):
    """전달에 실패하면 표시하지 않는다 — 다음 주기에 다시 시도할 수 있어야 한다."""
    monkeypatch.setattr(disclosure.api, "send_telegram_message",
                        lambda msg, **kw: False)

    assert disclosure.check_and_alert_disclosures() == 0
    assert stub_disclosure == set(), "못 간 알림이 '보냈다'로 굳었다"


def test_delivered_send_is_marked_once(stub_disclosure, monkeypatch):
    """전달되면 표시하고, 다음 주기에는 다시 보내지 않는다."""
    calls = []

    def _send(msg, **kw):
        calls.append(kw.get("sync"))
        return True

    monkeypatch.setattr(disclosure.api, "send_telegram_message", _send)

    assert disclosure.check_and_alert_disclosures() == 1
    assert stub_disclosure == {"20260904000001"}
    assert calls == [True], "전달을 확인하려면 동기로 보내야 한다"

    assert disclosure.check_and_alert_disclosures() == 0     # 중복 발송 없음
    assert len(calls) == 1
