"""AI 매매 복기에 지어낸 점수를 주지 않는다 (감사 2026-09-06, 배치 69).

[무엇이 걸려 있는가] TRADING_AUTOPSY_PROMPT 는 매도가 끝날 때마다 AI 에게
"진입 당시 퀀트 점수: {buy_score}" 를 사실로 제시하고, 그 점수를 근거로 프로세스를
판정하게 한다. 그런데 호출부가 `last_buy.get('score', 0)` 이었다 —
trades 테이블의 컬럼은 **strategy_score** 다(get_latest_buy_trade 는 SELECT *).

`.get()` 은 예외를 내지 않으므로 **모든 거래가 0점으로** 들어갔다. 프롬프트는
"0점 (10점 만점)"이라고 단정하고, 같은 프롬프트가 바로 아래에서
"결과가 나빴다는 이유만으로 없는 실패 요인을 지어내지 마세요" 라고 적는다 —
지어낸 실패 요인을 **입력으로 준** 셈이다.

모르면 모른다고 적는다. 0점은 '최악의 진입'이라는 강한 주장이다([[unknown-vs-empty]]).
"""
from unittest.mock import patch

import pytest

from modules import prompts
from modules.auto_trade import conclusion


def _monitor():
    return conclusion.ConclusionMonitor.__new__(conclusion.ConclusionMonitor)


def _run(last_buy, monkeypatch):
    captured = {}

    def _autopsy(code, name, buy_time, buy_score, sell_reason, profit_rate, holding_days):
        captured['buy_score'] = buy_score
        return None

    monkeypatch.setattr(conclusion.db_manager.db, 'get_latest_buy_trade',
                        lambda code: last_buy)
    from modules import theme_analysis
    monkeypatch.setattr(theme_analysis, 'generate_trading_autopsy', _autopsy)

    _monitor()._send_trading_autopsy('005930', '삼성전자',
                                     {'reason': '손절', 'profit_rate': -7.0})
    return captured


_BUY = {'time': '2026-09-01 09:30:00', 'strategy_score': 8.5}


def test_실제_컬럼명으로_점수를_읽는다(monkeypatch):
    """trades 의 컬럼은 'score' 가 아니라 'strategy_score' 다."""
    assert _run(_BUY, monkeypatch)['buy_score'] == 8.5


def test_옛_키는_더_이상_읽지_않는다(monkeypatch):
    """'score' 키만 있는 행이 와도 그것을 점수로 삼지 않는다(그런 컬럼은 없다)."""
    got = _run({'time': '2026-09-01 09:30:00', 'score': 9.9}, monkeypatch)['buy_score']
    assert got is None, f"존재하지 않는 컬럼을 읽었다: {got}"


@pytest.mark.parametrize("row", [
    None,
    {'time': '2026-09-01 09:30:00'},
    {'time': '2026-09-01 09:30:00', 'strategy_score': None},
    {'time': '2026-09-01 09:30:00', 'strategy_score': ''},
])
def test_점수를_모르면_0점이라고_말하지_않는다(row, monkeypatch):
    """0점은 '최악의 진입'이라는 강한 주장이다 — AI 가 그 위에서 판정한다."""
    from modules import theme_analysis
    got = _run(row, monkeypatch)['buy_score']
    assert theme_analysis._fmt_autopsy_score(got) == "알 수 없음"
    assert got in (None, ""), f"모르는 점수를 {got!r} 로 메웠다"


def test_프롬프트가_점수를_그대로_받는다():
    """'{buy_score}점 (10점 만점)' 이면 '알 수 없음점' 이 된다 — 문구는 호출부가 만든다."""
    assert '{buy_score}점' not in prompts.TRADING_AUTOPSY_PROMPT
    assert '{buy_score}' in prompts.TRADING_AUTOPSY_PROMPT

    body = prompts.TRADING_AUTOPSY_PROMPT.format(
        name='삼성전자', code='005930', buy_time='2026-09-01 09:30:00',
        buy_score='알 수 없음', holding_days=3, profit_rate=-7.0, sell_reason='손절')
    assert '퀀트 점수: 알 수 없음' in body


@pytest.mark.parametrize("raw,expect", [
    (8.5, "8.5점 (10점 만점)"),
    (0.0, "0.0점 (10점 만점)"),      # 진짜 0점은 그대로 적는다
    (None, "알 수 없음"),
    ("", "알 수 없음"),
    ("알 수 없음", "알 수 없음"),
    ("abc", "알 수 없음"),
])
def test_점수_문구는_프롬프트_옆에서_만든다(raw, expect):
    """'{buy_score}점' 을 템플릿에 두면 모르는 경우가 '알 수 없음점' 이 된다."""
    from modules import theme_analysis
    assert theme_analysis._fmt_autopsy_score(raw) == expect
