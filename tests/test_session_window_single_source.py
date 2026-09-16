"""세션 경계(휴게 15:30~16:00)는 api.sessions 정본 하나만 쓴다 — 리터럴 재도입 가드.

[왜 · 2026-09-16] KRX 애프터마켓 도입 때 "15:30~20:00 = NXT 전용" 하드코딩 6곳을 걷어내고
 `domestic_break_window`·`krx_after_window`·`nxt_order_window` 를 정본으로 세웠다
 ([[krx-after-market-policy]]: 한 덩어리 시각 비교를 다시 넣지 말 것). 그런데 자동매매 운용
 시간(common.is_system_market_open)과 예약 주문 안내(trading.register_reserved_order)가
 휴게 구간을 "1530"/"1600" 리터럴로 다시 비교하고 있었다. 오늘은 값이 같아 무해하지만,
 정본(KRX_BREAK_WINDOW)을 고치는 날 이 두 자리만 옛 경계로 남는다 — 그것이 하드코딩을 걷어낸
 이유 자체다. 가드가 없으면 같은 일이 세 번째로 일어난다.
"""
import inspect
import re

from modules import trading
from modules.auto_trade import common

_BREAK_LITERAL = re.compile(r'"(1530|1559|1600)"\s*[<>=]|[<>=]\s*"(1530|1559|1600)"')


def _code_lines(fn):
    for line in inspect.getsource(fn).splitlines():
        code = line.split("#", 1)[0]
        if code.strip():
            yield code


def test_자동매매_운용시간이_휴게를_정본으로_판정한다():
    src = inspect.getsource(common.is_system_market_open)
    assert "domestic_break_window(_now)" in src
    #  종가 단일가(1520~1530)·NXT 프리 정지(0850~0900) 리터럴은 이 함수 고유의 회피 구간이라
    #   그대로 두되, 휴게 경계(1530~1600)를 다시 손으로 비교하면 안 된다.
    hits = [c for c in _code_lines(common.is_system_market_open)
            if _BREAK_LITERAL.search(c) and "1520" not in c]
    assert not hits, f"휴게 경계를 리터럴로 다시 비교한다: {hits}"


def test_예약주문_안내가_휴게를_정본으로_판정한다():
    src = inspect.getsource(trading.register_reserved_order)
    assert "domestic_break_window(_now)" in src
    hits = [c for c in _code_lines(trading.register_reserved_order)
            if _BREAK_LITERAL.search(c) and "2000" not in c]
    assert not hits, f"휴게 경계를 리터럴로 다시 비교한다: {hits}"


def test_정본_경계와_옛_리터럴이_같은_구간을_말한다():
    """가드가 거는 값(1530~1559)이 정본과 어긋나면 이 파일이 거짓을 지킨다."""
    from api import sessions
    assert sessions.KRX_BREAK_WINDOW == ("1530", "1559")
