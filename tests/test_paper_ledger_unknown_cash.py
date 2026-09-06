"""가상 원장은 '못 읽은 예수금'을 0원으로 읽고 덮어쓰지 않는다.

[왜] paper_broker._get_state 는 예외를 통째로 삼키고 default 를 돌려줬다. 그 값이
 그대로 원장에 **다시 써지는** 자리가 셋 있었다 — 매도(new_cash = 예수금 + 매도대금),
 개설(seed 가 None 이면 계좌를 다시 연다), 입출금(cash/seed 에 더한다).
 실측(50주 왕복, DB 잠금 1회): 예수금 6,499,381 → 3,841,619 (정상 10,349,381),
 그런데 주문 응답은 rt_cd '0'. 가상 계좌의 자산곡선은 드로다운 → 리스크 한도 →
 매매 강도로 이어지므로 어긋난 원장은 관찰 결과 자체를 못 쓰게 만든다.
 [[unknown-vs-empty]] · [[daily-asset-baseline-transfers]]
"""
import os
import tempfile

import pytest

import api
import config
from modules import db_manager, paper_broker


@pytest.fixture
def paper(monkeypatch):
    tmpdir = tempfile.mkdtemp()
    path = os.path.join(tmpdir, "paper_ledger.db")
    original_path = db_manager.db.db_path
    monkeypatch.setattr(config, 'PAPER_DB_FILE_PATH', path, raising=False)
    monkeypatch.setattr(config, 'PAPER_SEED_CAPITAL', 10_000_000, raising=False)
    monkeypatch.setattr(config.session, 'is_paper', True, raising=False)
    db_manager.db.switch_path(path)
    paper_broker.init_tables()
    monkeypatch.setattr(paper_broker, '_current_price',
                        lambda code, fallback=0.0: {'005930': 70000}.get(code, fallback or 0))
    yield paper_broker
    db_manager.db.close_all_connections()
    db_manager.db.switch_path(original_path)


def _break_state_read(monkeypatch, key):
    """paper_state 의 특정 키 읽기만 실패시킨다(DB 잠금 소진과 같은 자리)."""
    real = db_manager.db.execute_query

    def flaky(self, q, p=(), fetch=None):
        if fetch == 'one' and 'paper_state' in q and p and p[0] == key:
            raise Exception("database is locked")
        return real(q, p, fetch)

    monkeypatch.setattr(type(db_manager.db), 'execute_query', flaky)
    return lambda: monkeypatch.setattr(
        type(db_manager.db), 'execute_query',
        lambda self, q, p=(), fetch=None: real(q, p, fetch))


def test_예수금을_못_읽으면_매도가_원장을_덮지_않는다(paper, monkeypatch):
    api.place_order("domestic", "buy", "005930", 50, 70000, "00")
    before = paper_broker.get_cash()
    assert before > 0

    restore = _break_state_read(monkeypatch, 'cash')
    res = api.place_order("domestic", "sell", "005930", 50, 77000, "00")
    restore()

    # 주문은 거부되어야 한다 — '성공'으로 답하면 트레이더가 청산됐다고 믿는다.
    assert res['rt_cd'] != '0'
    assert '예수금' in res['msg1']
    # 원장은 손대지 않았다: 예수금·포지션 그대로.
    assert paper_broker.get_cash() == before
    assert [p['qty'] for p in paper_broker.get_positions()] == [50]


def test_예수금을_못_읽으면_매수도_나가지_않는다(paper, monkeypatch):
    """0원은 '예수금 부족'으로도 읽힌다 — 거부 이유가 사실과 달라선 안 된다."""
    restore = _break_state_read(monkeypatch, 'cash')
    res = api.place_order("domestic", "buy", "005930", 10, 70000, "00")
    restore()
    assert res['rt_cd'] != '0'
    assert '읽지 못했' in res['msg1'], f"부족으로 둔갑했다: {res['msg1']}"


def test_시드를_못_읽으면_계좌를_다시_열지_않는다(paper, monkeypatch):
    """재기동 때마다 부르는 경로다. 조용한 초기화보다 시끄러운 정지가 낫다."""
    api.place_order("domestic", "buy", "005930", 50, 70000, "00")
    before = paper_broker.get_cash()

    restore = _break_state_read(monkeypatch, 'seed')
    with pytest.raises(Exception):
        paper_broker.init_tables()
    restore()

    assert paper_broker.get_cash() == before
    assert paper_broker.get_seed() == 10_000_000


def test_입출금은_잔액을_못_읽으면_반영하지_않는다(paper, monkeypatch):
    api.place_order("domestic", "buy", "005930", 50, 70000, "00")
    before_cash, before_seed = paper_broker.get_cash(), paper_broker.get_seed()

    restore = _break_state_read(monkeypatch, 'cash')
    ok, msg = paper_broker.adjust_seed(1_000_000)
    restore()

    assert ok is False and '읽지 못했' in msg
    assert paper_broker.get_cash() == before_cash
    assert paper_broker.get_seed() == before_seed


def test_표시_경로는_못_읽어도_죽지_않는다(paper, monkeypatch):
    """원장을 고쳐 쓰지 않는 자리까지 막으면 화면이 통째로 사라진다."""
    restore = _break_state_read(monkeypatch, 'cash')
    try:
        assert paper_broker.get_cash() == 0.0          # 기본값으로 넘어간다
    finally:
        restore()
    restore = _break_state_read(monkeypatch, 'started_at')
    try:
        assert paper_broker._get_state('started_at', '-') == '-'
    finally:
        restore()
