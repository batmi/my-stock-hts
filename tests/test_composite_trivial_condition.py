"""복합(AND) 예약 조건에 '언제나 참'인 서브조건을 넣을 수 없다 (감사 2026-09-06, 배치 62).

[무엇이 걸려 있는가] 단일 조건 등록 경로에는 '등록 즉시 발동'을 되묻는 가드가 있다
(_rsv_immediate_trigger — "방향을 반대로 적은 실수가 즉시 체결로 이어지는 유일한 경로").
그런데 **복합 서브조건 경로에는 그것이 통째로 없었고**, 범위 검사도 없었다. 같은 파일
안에서 두 경로가 갈라져 있었다.

그래서 이런 입력이 그대로 등록된다:
    목표 퀀트 점수 0 + '점수 이상'  →  `score >= 0`   — 언제나 참
    목표 RSI 0 + 'RSI 이상'        →  `rsi >= 0`     — 언제나 참
    목표가 0 + '가격 이상'          →  `price >= 0`   — 언제나 참

복합은 AND 라 이 하나로 곧장 발주되지는 않는다. 문제는 **사용자가 걸었다고 믿는 조건
하나가 조용히 사라진다**는 것이다. 두 조건짜리 복합이라면 사실상 단일 조건이 되어,
의도한 것보다 훨씬 이른 시점에 실주문이 나간다. 화면에는 아무 표시도 없다.

같은 파일의 트레일링 폭(`0 < pct <= 50`)·ATR 배수(`0 < k <= 5`)·시각(HHMM 범위)
프롬프트는 이미 같은 범위 검사를 갖고 있다 — 이 셋만 빠져 있었다.
"""
from unittest.mock import patch

import pytest

from modules import trading


# ══════════════════════════════════════════════════════════════════════
# 판정 헬퍼
# ══════════════════════════════════════════════════════════════════════

@pytest.mark.parametrize("ctype,value,why", [
    ('SCORE_UP', 0, "점수 ≥ 0"),
    ('SCORE_UP', -1, "음수 점수"),
    ('RSI_UP', 0, "RSI ≥ 0"),
    ('RSI_DOWN', 100, "RSI ≤ 100"),
    ('RSI_DOWN', 150, "RSI ≤ 150"),
    ('PRICE_UP', 0, "가격 ≥ 0"),
])
def test_언제나_참인_서브조건을_잡아낸다(ctype, value, why):
    assert trading._rsv_trivial_sub(ctype, value), f"{why} 를 놓쳤다"


@pytest.mark.parametrize("ctype,value", [
    ('SCORE_UP', 8.0), ('SCORE_DOWN', 4.0),
    ('RSI_UP', 70), ('RSI_DOWN', 35),
    ('PRICE_UP', 50000), ('PRICE_DOWN', 45000),
    ('SMART_MONEY', None), ('STATE', '강매수'),
])
def test_정상_조건은_통과시킨다(ctype, value):
    """엄격해지느라 정상 입력을 막으면 그것이 더 나쁘다."""
    assert trading._rsv_trivial_sub(ctype, value) is None


# ══════════════════════════════════════════════════════════════════════
# 입력 프롬프트
# ══════════════════════════════════════════════════════════════════════

def _ask(answers):
    it = iter(answers)
    return lambda *a, **k: next(it)


def test_점수_0은_서브조건으로_추가되지_않는다(monkeypatch):
    monkeypatch.setattr(trading.Prompt, 'ask', _ask(["0", "1"]))   # 점수 0 / '이상'
    monkeypatch.setattr(trading.config.console, 'print', lambda *a, **k: None)
    assert trading._prompt_sub_condition("1", 50000, False) is None


def test_점수_8은_정상_추가된다(monkeypatch):
    monkeypatch.setattr(trading.Prompt, 'ask', _ask(["8.0", "1"]))
    monkeypatch.setattr(trading.config.console, 'print', lambda *a, **k: None)
    sub = trading._prompt_sub_condition("1", 50000, False)
    assert sub == {"type": "SCORE_UP", "value": 8.0, "_label": "점수≥8.0"}


def test_RSI_0_이상은_추가되지_않는다(monkeypatch):
    monkeypatch.setattr(trading.Prompt, 'ask', _ask(["0", "1"]))
    monkeypatch.setattr(trading.config.console, 'print', lambda *a, **k: None)
    assert trading._prompt_sub_condition("2", 50000, False) is None


def test_목표가_0_이상은_추가되지_않는다(monkeypatch):
    monkeypatch.setattr(trading.Prompt, 'ask', _ask(["0", "1"]))
    monkeypatch.setattr(trading.config.console, 'print', lambda *a, **k: None)
    assert trading._prompt_sub_condition("6", 50000, False) is None


def test_이미_충족된_목표가는_되묻고_거절하면_추가하지_않는다(monkeypatch):
    """단일 조건 경로와 같은 가드다 — 방향을 반대로 고른 실수를 잡는다.
    기준가 50,000원인데 '40,000원 이상'은 등록 시점부터 이미 참이다."""
    monkeypatch.setattr(trading.Prompt, 'ask', _ask(["40000", "1", "n"]))
    monkeypatch.setattr(trading.config.console, 'print', lambda *a, **k: None)
    assert trading._prompt_sub_condition("6", 50000, False) is None


def test_되물음에_동의하면_그대로_추가한다(monkeypatch):
    """의도적으로 그렇게 걸 수도 있다 — 판단은 사람에게 남긴다."""
    monkeypatch.setattr(trading.Prompt, 'ask', _ask(["40000", "1", "y"]))
    monkeypatch.setattr(trading.config.console, 'print', lambda *a, **k: None)
    sub = trading._prompt_sub_condition("6", 50000, False)
    assert sub and sub["type"] == "PRICE_UP" and sub["value"] == 40000.0
