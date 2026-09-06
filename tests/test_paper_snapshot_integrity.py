"""가상 자산 스냅샷은 만들 수 없는 값을 굳히지 않는다.

[왜] paper_equity 는 자산곡선·MDD의 원본이고, MDD 는 리스크 한도로 이어져 매매 강도를
 바꾼다. 예수금 조회가 한 번 실패하면 get_domestic_balance 가 현금 0으로 답했고,
 그 값이 그날 행에 그대로 굳었다(같은 날 행은 INSERT OR REPLACE 라 **정상 행을 덮는다**).
 실측(주식 3,500,000 · 현금 6,499,381, 잠금 1회):
     정상   total 9,999,381
     실패   total 3,500,000  (-65.0%)  ← seed 는 설정 기본값으로 채워졌다
 실계좌 경로에는 구간 실패를 표시하는 기계가 이미 있다(account.get_asset_status_data 의
 degraded). 가상 계좌도 그것을 타려면 조회 실패가 **예외로** 올라가야 한다.
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
    path = os.path.join(tmpdir, "paper_snap.db")
    orig = db_manager.db.db_path
    monkeypatch.setattr(config, 'PAPER_DB_FILE_PATH', path, raising=False)
    monkeypatch.setattr(config, 'PAPER_SEED_CAPITAL', 10_000_000, raising=False)
    monkeypatch.setattr(config.session, 'is_paper', True, raising=False)
    db_manager.db.switch_path(path)
    paper_broker.init_tables()
    monkeypatch.setattr(paper_broker, '_current_price', lambda c, fallback=0.0: 70000)
    monkeypatch.setattr(paper_broker, '_krx_settled_close', lambda c: 0.0)
    yield paper_broker
    db_manager.db.close_all_connections()
    db_manager.db.switch_path(orig)


def _break_cash_read(monkeypatch):
    real = db_manager.db.execute_query

    def flaky(self, q, p=(), fetch=None):
        if fetch == 'one' and 'paper_state' in q and p and p[0] == 'cash':
            raise Exception("database is locked")
        return real(q, p, fetch)

    monkeypatch.setattr(type(db_manager.db), 'execute_query', flaky)
    return lambda: monkeypatch.setattr(
        type(db_manager.db), 'execute_query',
        lambda self, q, p=(), fetch=None: real(q, p, fetch))


def test_예수금을_못_읽으면_스냅샷이_정상행을_덮지_않는다(paper, monkeypatch):
    api.place_order("domestic", "buy", "005930", 50, 70000, "00")
    assert paper_broker.snapshot_equity() in (True, False)
    good = paper_broker.get_equity_curve()[-1]
    assert good['total'] > 9_000_000

    restore = _break_cash_read(monkeypatch)
    ok = paper_broker.snapshot_equity()
    restore()

    assert ok is False
    assert paper_broker.get_equity_curve()[-1] == good, "가짜 낙폭이 자산 이력에 굳었다"


def test_잔고_예수금_조회는_실패를_0원으로_답하지_않는다(paper, monkeypatch):
    """실계좌 경로의 degraded 표시를 타려면 예외로 올라가야 한다."""
    api.place_order("domestic", "buy", "005930", 50, 70000, "00")
    restore = _break_cash_read(monkeypatch)
    try:
        with pytest.raises(Exception):
            paper_broker.get_domestic_balance()
        with pytest.raises(Exception):
            paper_broker.get_deposit_balance()
    finally:
        restore()


def test_가로채기_계층이_예외를_삼키지_않는다(paper, monkeypatch):
    """api 층이 여기서 예외를 잡아 0으로 바꾸면 위 방어가 통째로 무의미해진다."""
    api.place_order("domestic", "buy", "005930", 50, 70000, "00")
    restore = _break_cash_read(monkeypatch)
    try:
        with pytest.raises(Exception):
            api.get_domestic_balance()
        with pytest.raises(Exception):
            api.get_deposit_balance()
    finally:
        restore()
