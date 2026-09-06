"""db_manager 가 스스로 '위험하다'고 적어 둔 조합들을 실제로 막았는가.

[배경] 2026-09-03 배치는 조회 실패가 흔적 없이 사라지는 것을 고치면서, 파일 머리에
위험한 조합을 이름으로 적고 **고치는 것은 미뤄** 뒀다:

    · get_pending_reserved_orders() → [] : 예약 손절 감시 전체가 조용히 멈춘다
    · check_trade_exists() → False       : 체결 행을 한 번 더 INSERT 한다(이중 계상)
    · get_all_stock_strategies() → []    : 개별 룰이 사라지고 전역값으로 판정한다
    · get_all_trailing_stops() → {}      : 트레일링 앵커를 잃는다
  "반환 계약을 바꾸면 호출부 전체를 손봐야 하므로 동작은 그대로 두고 흔적만 남긴다."

이 파일은 그 미뤄 둔 절반을 고정한다. 반환 계약은 그대로 두고(표시·리포트 호출부가
15곳이 넘는다), **판정에 쓰는 자리만** strict 로 갈라 받는다.
"""
import ast
import inspect

import pytest

from modules import db_manager


class _Boom:
    def cursor(self):
        raise RuntimeError("database is locked")


@pytest.fixture
def broken_db(monkeypatch):
    monkeypatch.setattr(type(db_manager.db), '_get_conn', lambda self: _Boom())
    yield


_STRICT_READERS = ("get_pending_reserved_orders", "get_all_stock_strategies",
                   "get_all_trailing_stops", "get_trades")


@pytest.mark.parametrize("name", _STRICT_READERS)
def test_strict_는_실패를_올린다(broken_db, name):
    with pytest.raises(Exception):
        getattr(db_manager.db, name)(strict=True)


@pytest.mark.parametrize("name", _STRICT_READERS)
def test_기본값은_종전_계약을_지킨다(broken_db, name):
    """표시·리포트 호출부는 빈 값으로 흘러도 손해가 없다 — 계약을 통째로 바꾸지 않는다."""
    out = getattr(db_manager.db, name)()
    assert out in ([], {}) or out == [], f"{name} 의 기본 반환이 바뀌었다: {out!r}"


@pytest.mark.parametrize("name", _STRICT_READERS)
def test_모든_열쇠_리더가_strict_를_받는다(name):
    """목록의 이름이 어긋나면 위 검사들이 조용히 무의미해진다."""
    f = getattr(db_manager.db, name, None)
    assert f is not None, f"{name} 이 없다 — 목록이 낡았다"
    assert "strict" in inspect.signature(f).parameters


# ─────────────────────────── 판정 경로가 실제로 쓰는가 ───────────────────────────

def _calls_strict(fn, reader):
    """그 함수 안의 reader 호출이 **전부** strict=True 인가.

    [주의] '하나라도'로 세면 안 된다 — 같은 함수가 그 조회를 두 번 부르는 경우가 있고
    (예약 감시는 권리조정 점검 앞뒤로 두 번 읽는다), 한쪽만 고쳐도 검사가 통과해
    나머지 한쪽의 구멍을 덮는다. 실제로 이 파일을 쓰면서 그 상태를 한 번 만들었다.
    """
    tree = ast.parse(inspect.getsource(fn).lstrip())
    calls = [n for n in ast.walk(tree)
             if isinstance(n, ast.Call) and getattr(n.func, "attr", None) == reader]
    if not calls:
        return False
    return all(any(k.arg == "strict" and getattr(k.value, "value", None) is True
                   for k in c.keywords) for c in calls)


def test_예약_감시는_strict_로_읽는다():
    """빈 목록을 '예약 없음'으로 읽으면 그 주기의 손절·익절이 통째로 멈춘다."""
    from modules.reserved_order_monitor import ReservedOrderMonitor
    assert _calls_strict(ReservedOrderMonitor._check_orders, "get_pending_reserved_orders")

    src = inspect.getsource(ReservedOrderMonitor._check_orders)
    assert "_pending_fail_streak" in src and "alert_delivered" in src, \
        "연속 실패가 사람에게 닿지 않는다 — 감시가 멈춘 채 조용해진다"


def test_매수_경로는_룰을_strict_로_읽고_실패하면_보류한다():
    from modules.auto_trade.trader import AutoTrader
    assert _calls_strict(AutoTrader._check_buy_conditions, "get_all_stock_strategies")
    assert "매수 보류: 종목별 개별 룰" in inspect.getsource(AutoTrader._check_buy_conditions)


def test_매도_경로는_룰을_못_읽어도_멈추지_않는다():
    """매도 검사를 거르면 손절·트레일링이 통째로 꺼진다 — 룰을 잃는 것보다 비싸다.

    대신 룰이 빠진 사실은 반드시 남는다(룰은 손절을 조이는 방향만 허용되므로,
    룰을 잃는 것은 곧 그 종목의 손절선이 넓어지는 것이다).
    """
    from modules.auto_trade.trader import AutoTrader
    src = inspect.getsource(AutoTrader._check_sell_conditions)
    assert _calls_strict(AutoTrader._check_sell_conditions, "get_all_stock_strategies")
    assert "custom_rules = []" in src, "실패 시 매도 검사를 중단한다면 그게 더 위험하다"
    assert "넓어집니다" in src, "무엇을 잃는지 말하지 않으면 로그가 아니다"


def test_트레일링_앵커를_못_읽으면_남긴다():
    """빈 dict 는 '앵커가 하나도 없다' — 고점을 모르면 트레일링이 발동할 수 없다."""
    from modules.auto_trade import engine
    src = inspect.getsource(engine)
    i = src.index("get_all_trailing_stops(strict=True)")
    tail = src[i:i + 600]
    assert "무장 해제" in tail, "앵커 상실을 로그로 남기지 않는다"
