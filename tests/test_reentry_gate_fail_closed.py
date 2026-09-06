"""재진입 방어의 입력이 되는 조회가 실패하면, 그 주기의 매수를 미루는가.

[왜 중요한가] `get_trades(오늘)` 는 게이트 **두 개**의 입력이다.

  ① 재진입 체결강도 허들
  ② '손절가보다 비싸게 되사지 않는다'

 종전에는 조회가 실패해도 빈 목록이 돌아와 "오늘 판 종목이 없다"로 읽혔고, 두 게이트가
 **동시에** 사라졌다. 그 게이트가 막으려던 것이 2026-08-05 실측의 손절·재매수 반복
 루프다 — 체결강도 허들은 재진입할 때마다 갱신되어 스스로 세운 허들을 스스로 넘었고
 (103.1% → 127.3% → 127.5%), 매 주기 손절과 재매수를 되풀이하며 왕복 스프레드만큼
 실현 손실이 누적됐다. 조회 한 번이 실패하면 정확히 그 상태로 돌아간다.

 진입을 한 주기 미루는 것은 되돌릴 수 있고, 방어 없이 낸 재매수는 되돌릴 수 없다.
"""
import inspect

import pytest

from modules import db_manager


class _Boom:
    def cursor(self):
        raise RuntimeError("database is locked")


def test_기본값은_종전대로_빈_목록이다(monkeypatch):
    """화면·리포트는 빈 목록으로 흘러도 손해가 없다 — 계약을 통째로 바꾸지 않는다."""
    monkeypatch.setattr(type(db_manager.db), '_get_conn', lambda self: _Boom())
    assert db_manager.db.get_trades(limit=10) == []


def test_strict_는_실패를_올린다(monkeypatch):
    monkeypatch.setattr(type(db_manager.db), '_get_conn', lambda self: _Boom())
    with pytest.raises(Exception):
        db_manager.db.get_trades(limit=10, strict=True)


def test_정상_조회는_strict_에서도_그대로다():
    """대조군 — strict 가 정상 경로를 바꾸지 않는다."""
    a = db_manager.db.get_trades(limit=5)
    b = db_manager.db.get_trades(limit=5, strict=True)
    assert [r.get('id') for r in a] == [r.get('id') for r in b]


def test_매수_경로는_strict_로_읽고_실패하면_보류한다():
    """[가드] 인자를 안 넘기면 위 계약이 있어도 없는 것과 같다."""
    from modules.auto_trade.trader import AutoTrader

    src = inspect.getsource(AutoTrader._check_buy_conditions)
    head = src[:src.index("sold_today")]
    assert "strict=True" in head, \
        "당일 매매 이력을 strict 로 읽지 않는다 — 조회 실패가 '판 종목 없음'이 된다"
    assert "매수 보류" in src and "재진입 방어" in src, \
        "실패했을 때 매수를 미루지 않거나, 미룬 사실을 남기지 않는다"


def test_보류는_매수만이다():
    """매도 검사까지 멈추면 손절·트레일링이 함께 꺼진다 — 훨씬 비싸다.

    주석이 아니라 **실제 호출**을 본다(코드에는 그 관계를 설명하는 주석이 있다).
    """
    import ast
    from modules.auto_trade.trader import AutoTrader

    fn = ast.parse(inspect.getsource(AutoTrader._check_buy_conditions).lstrip()).body[0]
    called = {getattr(n.func, "attr", None) for n in ast.walk(fn) if isinstance(n, ast.Call)}
    assert "_check_sell_conditions" not in called, \
        "매수 경로가 매도 검사를 부르고 있다면 이 보류가 청산까지 멈춘다"
