"""체결 중복 검사가 **실패했을 때** 같은 체결을 한 번 더 적는가.

[배경 · 2026-09-06 감사] check_trade_exists 는 조회 실패를 False('없다')로 돌려줬다.
호출부는 전부 `if not check_trade_exists(...): insert_trade(...)` 모양이라, DB 가 잠깐
열리지 않으면 **같은 체결이 두 번 적힌다.** 중복 체결 행은
  · 실현손익을 이중 계상하고 (성과·드로다운·세금 추정이 전부 어긋난다)
  · 매매일지 웹서버로 그대로 전송되며
  · 되돌릴 방법이 없다 (지우는 경로가 없다)

반대 방향(모르는데 '이미 있다'로 보고 건너뜀)은 스스로 낫는다 — 이 경로들은 매 주기
같은 체결 내역을 다시 훑는다. 그래서 **모르면 적지 않는다.**

한편 api/orders._odno_known_to_db 에는 "확인 못 하면 '이미 아는 주문'으로 보수적 판정"
이라는 분기가 이미 있었는데, check_trade_exists 가 예외를 삼켜 **그 분기에 영영 닿지
않았다** — 선언된 의도를 한 조각이 무력화한 자리다.
"""
import sqlite3
from unittest.mock import patch

import pytest

from modules import db_manager


def _db():
    return getattr(db_manager.db, "_real_db", db_manager.db)


class _Broken:
    def cursor(self):
        raise sqlite3.OperationalError("database is locked")


# ── DB 계층 ──────────────────────────────────────────

def test_a_failed_check_raises_instead_of_saying_not_found(monkeypatch):
    monkeypatch.setattr(type(_db()), "_get_conn", lambda self, *a, **k: _Broken())
    with pytest.raises(Exception):
        _db().check_trade_exists("O1", "체결", on_date="2026-09-06")


def test_a_genuine_absence_is_still_false():
    assert _db().check_trade_exists("NO-SUCH-ODNO", "체결",
                                    on_date="2026-09-06") is False


# ── 주문 대사 — 죽어 있던 보수적 분기가 살아났는가 ──

def test_order_reconcile_treats_an_unknown_check_as_known(monkeypatch):
    """확인 못 한 주문번호를 '모르는 주문'이라 답하면 결과 불명 주문을 함부로 이어받는다."""
    from api import orders as _orders

    monkeypatch.setattr(type(_db()), "_get_conn", lambda self, *a, **k: _Broken())
    assert _orders._odno_known_to_db("O1") is True


def test_order_reconcile_still_answers_false_when_the_db_is_fine():
    from api import orders as _orders
    assert _orders._odno_known_to_db("NO-SUCH-ODNO") is False


# ── 복원 경로 ────────────────────────────────────────

def test_backfill_skips_a_fill_it_cannot_verify():
    from modules import holdings_backfill

    with patch('modules.holdings_backfill.db_manager.db.check_trade_exists',
               side_effect=sqlite3.OperationalError("boom")):
        assert holdings_backfill._exists("O1", "2026-09-06") is True


def test_backfill_still_records_a_genuinely_new_fill():
    from modules import holdings_backfill
    assert holdings_backfill._exists("NO-SUCH-ODNO", "2026-09-06") is False


# ── 체결 감지 경로 — 실제로 두 번 적히는가 ────────────

import config                                             # noqa: E402
from modules.auto_trade import AutoTrader, ConclusionMonitor  # noqa: E402


@pytest.fixture(autouse=True)
def _reset_singletons():
    AutoTrader._instance = None
    ConclusionMonitor._instance = None
    yield


def _fill_item(odno="FILL-1"):
    return {'odno': odno, 'pdno': '005930', 'prdt_name': '삼성전자',
            'ord_qty': '10', 'tot_ccld_qty': '10', 'cncl_cfrm_qty': '0',
            'rmn_qty': '0', 'avg_prvs': '70000', 'ord_dt': '20260906',
            'ord_tmd': '100000', 'sll_buy_dvsn_cd_name': '매수'}


def _run_fill_cycle(check_kwargs):
    monitor = ConclusionMonitor()
    with patch('modules.auto_trade.api.get_today_history',
               return_value={'rt_cd': '0', 'output1': [_fill_item()]}), \
         patch('modules.auto_trade.api.get_overseas_today_history',
               return_value={'rt_cd': '0', 'output1': []}), \
         patch('modules.auto_trade.db_manager.db.get_trade_by_odno', return_value=None), \
         patch('modules.auto_trade.db_manager.db.get_reserved_order_by_odno', return_value=None),\
         patch('modules.auto_trade.db_manager.db.check_trade_exists', **check_kwargs), \
         patch('modules.auto_trade.db_manager.db.insert_trade') as ins, \
         patch('modules.auto_trade.api.send_telegram_message'):
        monitor._check_conclusions(initial=False)
    return ins


def test_a_fill_is_not_recorded_when_the_duplicate_check_fails():
    """[핵심] 중복 여부를 모르면 적지 않는다 — 다음 주기가 같은 체결을 다시 본다."""
    ins = _run_fill_cycle({'side_effect': sqlite3.OperationalError("locked")})
    assert not [c for c in ins.call_args_list if "체결" in str(c)], \
        "중복 여부를 모르는 채로 체결을 적었다 — 실현손익이 이중 계상된다"


def test_a_genuinely_new_fill_is_still_recorded():
    """대조군 — 확인이 되면 종전대로 적는다."""
    ins = _run_fill_cycle({'return_value': False})
    assert [c for c in ins.call_args_list if "체결" in str(c)], \
        "정상 경로에서 체결이 기록되지 않았다"


def test_an_already_recorded_fill_is_not_recorded_twice():
    ins = _run_fill_cycle({'return_value': True})
    assert not [c for c in ins.call_args_list if "체결" in str(c)]
