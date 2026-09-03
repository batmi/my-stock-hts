"""백테스트가 밟지 않는 진입 게이트를 결과 옆에 밝히는가.

[배경] `UNMODELED_SELL_TOGGLES`(청산)는 전부 기본 OFF라 '켜면 갈라진다'는 예방 경고였다.
진입 게이트 셋 — 체결강도 · 호가잔량비 · 개장 직후 보류 — 은 성격이 반대다.
**기본값이 켜짐**이라 지금 이 순간에도 갈라져 있는데 아무 말도 하지 않았다.
체결강도·호가잔량비는 일봉에 존재하지 않는 값이고(실시간 체결·호가창), 개장 직후 보류는
종가 모델에 시각이 없어 밟을 수 없다.

[크기] 라즈베리파이 관찰 모드(mode 1) 신호 원장 2026-08-19~09-01: 매수 상태였던
(일,종목) 52건 중 15건(28.8%)이 완전 차단. 그 기계는 시장 필터를 끄고 돌던 중이라
실전 비율과 같다고 볼 수는 없지만, 자릿수는 무시할 크기가 아니다.
"""
import pytest

import config
from modules import portfolio_backtest as pbt


@pytest.fixture
def thresholds(monkeypatch):
    """ANALYSIS_THRESHOLDS 와 개장 지연을 원하는 값으로 세운다."""
    def _apply(vol, abr, delay_use=True, delay_min=30):
        at = dict(getattr(config, 'ANALYSIS_THRESHOLDS', {}) or {})
        at.update({"BUY_VOL_STRENGTH": vol, "BUY_ASK_BID_RATIO": abr})
        monkeypatch.setattr(config, 'ANALYSIS_THRESHOLDS', at, raising=False)
        monkeypatch.setattr(config, 'SYSTEM_ENTRY_OPEN_DELAY_USE', delay_use, raising=False)
        monkeypatch.setattr(config, 'SYSTEM_ENTRY_OPEN_DELAY_MINUTES', delay_min, raising=False)
    return _apply


def test_default_settings_are_reported_as_unmodeled(thresholds):
    """기본값(체결강도 100 · 호가잔량비 1.0 · 개장 지연 30분)이면 셋 다 나온다."""
    thresholds(100.0, 1.0)
    names = pbt.unmodeled_entry_features()
    assert names == ["체결강도 게이트", "호가잔량비 게이트", "개장 직후 진입 보류"], names


def test_turning_a_gate_off_removes_it(thresholds):
    """끈 게이트는 빠진다 — 0 은 '미사용'이라는 config 규약을 그대로 따른다."""
    thresholds(0, 1.0)
    assert "체결강도 게이트" not in pbt.unmodeled_entry_features()
    thresholds(100.0, 0)
    assert "호가잔량비 게이트" not in pbt.unmodeled_entry_features()


def test_open_delay_needs_both_switch_and_minutes(thresholds):
    """분이 0이면 스위치가 켜져 있어도 무동작이다 — 없는 게이트를 알리면 안 된다."""
    thresholds(0, 0, delay_use=True, delay_min=0)
    assert pbt.unmodeled_entry_features() == []
    thresholds(0, 0, delay_use=False, delay_min=30)
    assert pbt.unmodeled_entry_features() == []


def test_everything_off_says_nothing(thresholds, monkeypatch):
    """전부 끄면 아무 말도 하지 않는다 — 경고가 늘 떠 있으면 아무도 안 읽는다."""
    thresholds(0, 0, delay_use=False)
    said = []
    monkeypatch.setattr(pbt, '_announce', lambda msg, loud=True: said.append(msg))
    pbt.warn_if_unmodeled(where="테스트")
    assert not [m for m in said if "진입 게이트" in m], said


def test_warning_comes_out_of_the_same_gate_as_the_sell_side(thresholds, monkeypatch):
    """매도 측과 같은 문에서 함께 나온다 — 호출부를 새로 만들 필요가 없어야 한다.

    warn_if_unmodeled 는 prepare_universe 가 부르고, 메뉴 백테스트와 감사 도구가
    전부 그 문을 지난다. 진입 경고를 따로 부르게 하면 그 순간 또 빠지는 곳이 생긴다.
    """
    thresholds(100.0, 1.0)
    said = []
    monkeypatch.setattr(pbt, '_announce', lambda msg, loud=True: said.append((msg, loud)))

    returned = pbt.warn_if_unmodeled(where="테스트")

    entry_msgs = [m for m, _ in said if "진입 게이트" in m]
    assert entry_msgs, said
    assert "체결강도 게이트" in entry_msgs[0] and "호가잔량비 게이트" in entry_msgs[0]
    # 거의 항상 뜨는 알림이라 조용한 톤이어야 한다(경보로 내면 진짜 경보가 묻힌다)
    assert all(loud is False for m, loud in said if "진입 게이트" in m)
    # 반환값 규약은 그대로 '청산 목록' — 종전 호출부가 깨지면 안 된다
    assert returned == pbt.unmodeled_sell_features()


def test_prepare_universe_is_still_the_single_gate():
    """warn_if_unmodeled 호출부가 prepare_universe 하나로 유지되는지 확인한다."""
    src = open(pbt.__file__.replace('.pyc', '.py'), encoding='utf-8').read()
    code = "\n".join(l for l in src.split("\n") if not l.strip().startswith("#"))
    assert code.count("warn_if_unmodeled(") >= 2, "정의 + 호출이 있어야 한다"
