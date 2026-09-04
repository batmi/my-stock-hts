"""겹쳐 거는 예약이 기존 예약을 조용히 지우지 않게 한다.

같은 계좌·종목의 예약은 하나가 발동하면 나머지가 일괄 취소된다
(db.cancel_other_reserved_orders). 그래서 이미 손절 예약이 걸린 종목에 익절을 하나 더
걸면, 익절이 나가는 순간 손절이 사라진다. 그게 OCO 의 설계지만, **모르고** 겹쳐 걸면
설계가 아니라 사고다. 등록 마법사에는 안내가 있었고 OCO 단축 경로에는 없었다 —
OCO 는 한 번에 두 건을 넣으므로 겹칠 여지가 더 크다.
"""
import inspect

import pytest

from modules import trading


ROWS = [
    {'id': 11, 'code': '005930', 'cano': '1111', 'acnt': '01', 'order_type': 'sell',
     'qty': 10, 'condition_type': 'STOP', 'target_price': 60000, 'target_time': '',
     'composite_json': None},
    {'id': 12, 'code': '005930', 'cano': '1111', 'acnt': '02', 'order_type': 'sell',
     'qty': 10, 'condition_type': 'STOP', 'target_price': 60000, 'target_time': '',
     'composite_json': None},
    {'id': 13, 'code': '000660', 'cano': '1111', 'acnt': '01', 'order_type': 'buy',
     'qty': 5, 'condition_type': 'BREAKOUT', 'target_price': 200000, 'target_time': '',
     'composite_json': None},
]


@pytest.fixture
def shown(monkeypatch):
    out = []
    monkeypatch.setattr(trading.db_manager.db, 'get_pending_reserved_orders',
                        lambda: list(ROWS), raising=False)
    monkeypatch.setattr(trading.config.console, 'print',
                        lambda *a, **k: out.append(" ".join(str(x) for x in a)))
    return out


def test_an_overlapping_reservation_is_shown(shown):
    trading._warn_existing_reserved('005930', '1111', '01', False)
    body = "\n".join(shown)
    assert "ID 11" in body, body
    assert "자동 취소" in body, "무엇이 일어나는지 말하지 않으면 안내가 아니다"


def test_the_scope_matches_what_actually_gets_canceled(shown):
    """일괄 취소는 (cano, acnt, code) 로 좁혀 지운다 — 안내도 같은 범위여야 한다.

    계좌번호만 보면 상품코드가 다른 계좌의 예약까지 '사라진다'고 겁을 준다.
    """
    trading._warn_existing_reserved('005930', '1111', '01', False)
    body = "\n".join(shown)
    assert "ID 12" not in body, "다른 계좌(acnt)의 예약까지 끌어왔다"
    assert "ID 13" not in body, "다른 종목의 예약까지 끌어왔다"


def test_nothing_is_said_when_there_is_no_overlap(shown):
    trading._warn_existing_reserved('123456', '1111', '01', False)
    assert not shown


def test_a_lookup_failure_does_not_block_registration(shown, monkeypatch):
    """조회가 실패해도 등록은 막지 않되, 확인하지 못했다는 사실은 밝힌다."""
    def boom():
        raise RuntimeError("DB 잠김")
    monkeypatch.setattr(trading.db_manager.db, 'get_pending_reserved_orders',
                        boom, raising=False)
    trading._warn_existing_reserved('005930', '1111', '01', False)
    assert "확인하지 못했" in "\n".join(shown)


@pytest.mark.parametrize("fn", [trading._register_oco_orders, trading.register_reserved_order])
def test_both_entry_paths_warn(fn):
    """두 경로가 같은 안내를 쓴다 — 한쪽에만 있으면 다른 쪽이 조용해진다."""
    assert "_warn_existing_reserved" in inspect.getsource(fn)
