"""매도 사유 태그: **실매매가 내는 사유가 표에 빠져 있으면 안 된다**.

[왜 이 파일이 있나 · 2026-09-07]
core/trade_tags 는 '어휘가 하나 늘 때 한쪽이 빠지지 않게' 만든 단일 소스다. 그런데
표 자신이 빠져 있었다 — engine.analyze_sell 이 실제로 내보내는 사유 넷
(수익보존·이익보호·본전청산·하락반전)이 전부 빈 태그가 됐다.

빈 태그는 비어 있는 채로 끝나지 않는다. 소비자(modules/account.py 매매내역)는
태그를 붙인 뒤 `if not reason_display.startswith("[")` 로 그 자리를 **상태 태그**
(기간만료·발동실패·예약취소)로 채운다. 청산 사유 자리에 주문 상태가 앉는 것이다 —
2026-08-26 피라미딩 태그 누락 때 접수 상태 태그가 그 자리를 차지했던 것과 같은 모양이고,
그 사고 때문에 이 모듈이 생겼다.

사람이 매번 대조할 일이 아니라서, 정본 목록(portfolio_backtest.EXIT_REASONS)이
**하나도 빠짐없이** 태그를 받는지 여기서 센다. 예외 목록은 두지 않는다 — 예외 목록은
낡고, 낡은 예외가 바로 이번에 빠진 넷이다.
"""
import pytest

from core import trade_tags as tags
from modules.portfolio_backtest import EXIT_REASONS


#  engine.analyze_sell 이 실제로 조립하는 모양(접미 괄호까지 붙인 형태).
#  파일 위치는 2026-09-07 기준 modules/auto_trade/engine.py 의 해당 줄.
LIVE_SELL_REASONS = {
    "반익절(12.0%)": "반익절",
    "익절(15%)": "익절",
    "수익보존(목표돌파후 하락, 8.2%)": "이익보호",
    "이익보호(8.2%, 고점 12.0%)": "이익보호",
    "본전청산(1.2%)": "본전청산",
    "손절(-5.0%)": "손절",
    "ATR손절(-2.1%)": "ATR손절",
    "시간청산(15일경과, 상방모멘텀 상실)": "시간청산",
    "RSI과열(기준:75)": "과열매도",
    "RSI과열(슈퍼모멘텀, 기준:78)": "과열매도",
    "하락반전(방어적 반매도, 수익률:+6.0%)": "하락반전",
    "트레일링스탑(고점대비 -3.5%)": "트레일링스탑",
    "점수하락(3.2 < 4.0)": "추세이탈",
    "매도진입(역배열) [점수:2.0, RSI:38]": "추세이탈",
}


@pytest.mark.parametrize("reason", sorted(EXIT_REASONS))
def test_정본_청산_어휘는_모두_태그를_받는다(reason):
    """예외 없이 전부 — 여기에 예외를 만들면 그 예외가 다음 구멍이 된다."""
    assert tags.classify_sell_reason(reason), \
        f"정본 어휘 '{reason}' 에 태그가 없다 — 화면에서 그 자리를 상태 태그가 채운다"


@pytest.mark.parametrize("reason,expected", sorted(LIVE_SELL_REASONS.items()))
def test_실매매_사유가_의도한_태그로_분류된다(reason, expected):
    assert tags.classify_sell_reason(reason) == expected


def test_빈_태그는_상태_태그에게_자리를_내준다():
    """왜 빈 태그가 그냥 비어 있는 것이 아닌지 — 소비자 쪽 규칙을 여기 못 박는다."""
    reason = tags.apply_sell_tag("본전청산(1.2%)")
    assert reason.startswith("[본전청산]")
    # 소비자(account.py)는 아래 조건으로 상태 태그를 덧붙인다.
    assert reason.startswith("["), "태그가 없으면 이 자리를 기간만료/발동실패가 채운다"


def test_새_규칙이_기존_분류를_가리지_않는다():
    """추가한 규칙이 위에 놓이면서 먼저 걸리는 것이 없어야 한다."""
    assert tags.classify_sell_reason("반익절(10%)") == "반익절"
    assert tags.classify_sell_reason("익절(20%)") == "익절"
    assert tags.classify_sell_reason("ATR손절(-2%)") == "ATR손절"
    assert tags.classify_sell_reason("손절(-5%)") == "손절"
    assert tags.classify_sell_reason("시간청산(15일)") == "시간청산"


def test_해당_없는_사유는_여전히_빈_문자열이다():
    """모르는 것을 아무 태그나 붙여 채우면 그게 더 나쁘다."""
    assert tags.classify_sell_reason("알 수 없는 사유") == ""
    assert tags.classify_sell_reason("") == ""
    assert tags.classify_sell_reason(None) == ""
