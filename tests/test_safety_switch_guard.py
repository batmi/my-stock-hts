"""실계좌 시작 시 '꺼진 안전장치' 경고.

[왜 이 테스트인가] 가상 검증 중에는 매매를 강제로 발생시키려고 시장 필터를 끄는 일이
실제로 있다. dynamic_config.json은 모드별로 나뉘지 않으므로 그 설정이 실전으로 그대로
넘어간다 — 하필 그 필터가 하락장을 위한 장치다. 주석은 가드가 아니라서 코드로 옮겼다.
"""
import pytest

import config
from modules import settings


@pytest.fixture(autouse=True)
def all_switches_on(monkeypatch):
    for key, _label, _impact in settings.SAFETY_SWITCHES:
        monkeypatch.setattr(config.settings, key, True, raising=False)
    for key, _label, _impact in settings.SAFETY_LIMITS:
        monkeypatch.setattr(config.settings, key, 10.0, raising=False)


def test_nothing_to_warn_when_everything_is_on():
    assert settings.safety_switch_warnings() == []


def test_market_filter_off_is_reported(monkeypatch):
    monkeypatch.setattr(config.settings, "USE_MARKET_FILTER", False, raising=False)
    labels = [lab for lab, _ in settings.safety_switch_warnings()]
    assert "시장 필터" in labels


@pytest.mark.parametrize("key", [k for k, _l, _i in settings.SAFETY_SWITCHES])
def test_every_declared_switch_is_actually_checked(monkeypatch, key):
    """선언만 해 놓고 안 보는 항목이 있으면 경고가 조용히 비어 버린다."""
    monkeypatch.setattr(config.settings, key, False, raising=False)
    assert settings.safety_switch_warnings(), f"{key} 를 꺼도 경고가 없다"


@pytest.mark.parametrize("key", [k for k, _l, _i in settings.SAFETY_LIMITS])
def test_zero_limit_counts_as_disabled(monkeypatch, key):
    """0은 '미사용' 센티널이다 — 스위치가 아니라 값으로 기능이 꺼진다."""
    monkeypatch.setattr(config.settings, key, 0.0, raising=False)
    assert settings.safety_switch_warnings(), f"{key}=0 인데 경고가 없다"


def test_warning_prints_and_notifies(monkeypatch, capsys):
    monkeypatch.setattr(config.settings, "USE_MARKET_FILTER", False, raising=False)
    sent = []
    import api
    monkeypatch.setattr(api, "send_telegram_message", lambda m, **kw: sent.append(m))

    n = settings.warn_if_safety_switches_off()

    assert n == 1
    out = capsys.readouterr().out
    assert "안전장치가 꺼진 상태" in out and "시장 필터" in out
    assert sent and "시장 필터" in sent[0]


def test_warning_does_not_change_any_setting(monkeypatch):
    """경고만 한다 — 값을 되돌리면 사용자가 의도적으로 끈 설정이 조용히 뒤집힌다."""
    monkeypatch.setattr(config.settings, "USE_MARKET_FILTER", False, raising=False)
    import api
    monkeypatch.setattr(api, "send_telegram_message", lambda m, **kw: None)
    settings.warn_if_safety_switches_off()
    assert config.settings.USE_MARKET_FILTER is False


def test_silent_when_all_clear(monkeypatch, capsys):
    import api
    monkeypatch.setattr(api, "send_telegram_message",
                        lambda m, **kw: pytest.fail("정상인데 알림을 보냈다"))
    assert settings.warn_if_safety_switches_off() == 0
    assert capsys.readouterr().out.strip() == ""


def test_telegram_failure_does_not_break_startup(monkeypatch):
    """알림이 실패해도 기동은 계속돼야 한다 — 경고 수단이 기동을 막으면 본말전도다."""
    monkeypatch.setattr(config.settings, "USE_MARKET_FILTER", False, raising=False)
    import api

    def _boom(*a, **kw):
        raise RuntimeError("telegram down")

    monkeypatch.setattr(api, "send_telegram_message", _boom)
    assert settings.warn_if_safety_switches_off() == 1
