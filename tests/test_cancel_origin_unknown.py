"""취소 이력 **조회 실패**를 '취소 이력 없음'으로 읽어 자기 취소를 '외부 취소'로 단정하는가.

[배경 · 2026-09-06 감사] ConclusionMonitor 는 거래소 취소를 발견하면 그것이 우리 시스템이
낸 취소(미체결 시간 초과·수동 등)인지 DB 에서 확인한다. 아니면 '외부 취소'로 보고
  ① 운영자에게 "앱(MTS)/HTS 외부 취소" 알림을 보내고
  ② 매매 원장에 `취소(외부)` 행을 하나 더 남긴다.

그런데 get_cancel_record_by_org_odno 는 조회 실패를 **None(=이력 없음)** 으로 삼켰다.
그래서 DB 가 잠깐 열리지 않으면, 시스템이 방금 자기 손으로 낸 취소가 '외부 취소'가 된다 —
사람이 하지 않은 일을 했다고 알리고, 원장에는 같은 취소가 두 행으로 남는다.
([[unknown-vs-empty]] · '조회 실패 ≠ 없음')
"""
import sqlite3
from unittest.mock import patch

import pytest

from modules import db_manager


ODNO = "TESTCAN-1"


def _db():
    return getattr(db_manager.db, "_real_db", db_manager.db)


class _BrokenConn:
    def cursor(self):
        raise sqlite3.OperationalError("database is locked")


def test_a_failed_lookup_is_not_reported_as_no_record(monkeypatch):
    """[핵심] 조회가 실패했으면 '이력 없음'과 같은 답을 주면 안 된다."""
    db = _db()
    #  클래스에 건다 — 인스턴스에 걸면 복원 시 속성이 남아 뒤 테스트의 패치를 가린다.
    monkeypatch.setattr(type(db), "_get_conn", lambda self, *a, **k: _BrokenConn())
    with pytest.raises(Exception):
        db.get_cancel_record_by_org_odno(ODNO)


def test_no_record_is_still_none():
    """대조군 — 진짜로 이력이 없으면 종전대로 None 이다."""
    assert _db().get_cancel_record_by_org_odno("NO-SUCH-ODNO") is None


def test_an_existing_record_is_returned():
    db = _db()
    db.insert_trade("매수취소(자동)", "005930", "삼성전자", 10, 0, f"CANCEL_{ODNO}",
                    org_odno=ODNO, reason="미체결 시간 초과 (자동 취소)",
                    order_status="취소")
    rec = db.get_cancel_record_by_org_odno(ODNO)
    assert rec and "초과" in rec["reason"]


# ─────────────────────────────────────────────
# 호출부 — 조회 실패가 가짜 '외부 취소'를 만드는가
# ─────────────────────────────────────────────

import config                                        # noqa: E402
from modules.auto_trade import AutoTrader, ConclusionMonitor  # noqa: E402


@pytest.fixture(autouse=True)
def _reset_singletons():
    AutoTrader._instance = None
    ConclusionMonitor._instance = None
    yield


def _cancel_item():
    return {'odno': ODNO, 'pdno': '005930', 'prdt_name': '삼성전자',
            'ord_qty': '10', 'tot_ccld_qty': '0', 'cncl_cfrm_qty': '10',
            'rmn_qty': '0', 'avg_prvs': '70000', 'ord_dt': '20260906',
            'sll_buy_dvsn_cd_name': '매수'}


def _run_cancel_cycle(lookup):
    """취소 1건을 흘려보내고 (텔레그램 mock, insert_trade mock) 을 돌려준다."""
    monitor = ConclusionMonitor()
    with patch('modules.auto_trade.api.get_today_history',
               return_value={'rt_cd': '0', 'output1': [_cancel_item()]}), \
         patch('modules.auto_trade.api.get_overseas_today_history',
               return_value={'rt_cd': '0', 'output1': []}), \
         patch('modules.auto_trade.db_manager.db.get_trade_by_odno', return_value=None), \
         patch('modules.auto_trade.db_manager.db.get_reserved_order_by_odno', return_value=None),\
         patch('modules.auto_trade.db_manager.db.check_trade_exists', return_value=False), \
         patch('modules.auto_trade.db_manager.db.get_cancel_record_by_org_odno', **lookup), \
         patch('modules.auto_trade.db_manager.db.insert_trade') as ins, \
         patch('modules.auto_trade.api.send_telegram_message') as tg:
        monitor._check_conclusions(initial=False)
    return tg, ins


def test_a_lookup_failure_does_not_produce_a_false_external_cancel():
    """[핵심] 조회가 실패했을 뿐인데 '외부 취소'를 알리고 원장에 남기면 안 된다."""
    tg, ins = _run_cancel_cycle({'side_effect': sqlite3.OperationalError("locked")})

    assert not [c for c in tg.call_args_list if "외부 취소" in str(c)], \
        "조회 실패를 '외부 취소'로 단정해 알렸다"
    assert not [c for c in ins.call_args_list if "(외부)" in str(c)], \
        "조회 실패인데 원장에 `취소(외부)` 행을 남겼다"


def test_a_real_external_cancel_is_still_reported():
    """대조군 — 진짜로 이력이 없으면 종전대로 외부 취소로 알린다."""
    tg, ins = _run_cancel_cycle({'return_value': None})
    assert [c for c in tg.call_args_list if "외부 취소" in str(c)], \
        "진짜 외부 취소까지 조용해졌다"


def test_our_own_timeout_cancel_is_not_reported_as_external():
    """대조군 — 시스템이 낸 취소는 종전대로 알리지 않는다."""
    tg, ins = _run_cancel_cycle(
        {'return_value': {'id': 1, 'reason': '미체결 시간 초과 (자동 취소)'}})
    assert not [c for c in tg.call_args_list if "외부 취소" in str(c)]
