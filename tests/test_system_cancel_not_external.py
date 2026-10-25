"""시스템·사용자가 낸 취소를 체결 감시가 '앱(MTS)/HTS 외부 취소'로 읽지 않는가.

[배경 · 2026-10-25] ConclusionMonitor 는 거래소 취소를 발견하면 그날 그 주문의 취소 이력을
DB 에서 찾는다. 없으면 '외부 취소'로 보고 ① 운영자에게 알리고 ② 원장에 `취소(외부)` 행을
하나 더 남긴다. 두 군데가 어긋나 있었다.
  · 손절 보호(_cancel_pending_buy_on_stop_loss)는 미체결 매수를 직접 취소하면서 취소 이력을
    남기지 않았다 — 시간 초과 자동 취소(engine)는 남긴다.
  · 이력이 있어도 사유 문구의 낱말('수동'·'초과'·'타임아웃'·'외부')로 다시 걸렀는데, 메뉴
    수동 취소의 사유는 '사용자 취소'라 걸리지 않았다. 2026-06-23 운영 원장에 실측: 메뉴로
    취소한 매도 3건이 저마다 `매도취소(외부)` 행을 하나씩 더 달고 있다.
"""
from datetime import datetime
from unittest.mock import MagicMock, patch

import pytest

from modules import db_manager
from modules.auto_trade import AutoTrader, ConclusionMonitor
from modules.auto_trade.common import OrderStatus

CODE = "005930"
NAME = "삼성전자"
ODNO = "0000777001"


def _db():
    return getattr(db_manager.db, "_real_db", db_manager.db)


def _today():
    return datetime.now().strftime("%Y-%m-%d")


@pytest.fixture(autouse=True)
def _reset_singletons():
    AutoTrader._instance = None
    ConclusionMonitor._instance = None
    yield
    AutoTrader._instance = None
    ConclusionMonitor._instance = None


@pytest.fixture
def trader():
    t = AutoTrader()
    t.is_running = True
    t.market_index_status = {CODE: {}}
    t.order_manager.pending_orders = {CODE: {ODNO: OrderStatus.ORDER_SENT}}
    return t


def _holding():
    """손절선(-7%)을 크게 이탈한 체결분."""
    return {'pdno': CODE, 'prdt_name': NAME, 'hldg_qty': '30', 'ord_psbl_qty': '30',
            'evlu_pfls_rt': '-14.3', 'prpr': '60000', 'pchs_avg_pric': '70000',
            'evlu_pfls_amt': '-300000'}


def _stop_loss_cancel(trader, cancel_res, insert=None):
    """손절 보호를 한 번 돌린다(주문 종류 조회는 '매수 10주')."""
    patches = [
        patch('modules.auto_trade.db_manager.db.get_trade_by_odno',
              return_value={'type': '매수', 'qty': 10}),
        patch('modules.auto_trade.api.revise_cancel_order', return_value=cancel_res),
        patch('modules.auto_trade.api.send_telegram_message'),
    ]
    if insert is not None:
        patches.append(patch('modules.auto_trade.db_manager.db.insert_trade', insert))
    for p in patches:
        p.start()
    try:
        trader._cancel_pending_buy_on_stop_loss(CODE, NAME, _holding())
    finally:
        for p in reversed(patches):
            p.stop()


# ─────────────────────────────────────────────
# ① 손절 보호가 취소 이력을 남기는가
# ─────────────────────────────────────────────

def test_손절_보호_취소는_취소_이력을_남긴다(trader):
    _stop_loss_cancel(trader, {'rt_cd': '0', 'output': {'ODNO': '0000777099'}})

    rec = _db().get_cancel_record_by_org_odno(ODNO, on_date=_today())
    assert rec, "손절 보호가 낸 취소의 이력이 없다 — 체결 감시가 외부 취소로 읽는다"
    assert "손절 보호" in rec["reason"]

    row = _db().get_trade_by_odno("0000777099", on_date=_today())
    assert row, "취소 이력 행을 취소 주문번호로 찾을 수 없다"
    assert row["type"] == "매수취소(자동)", "시간 초과 자동 취소와 같은 종류로 남겨야 화면·집계가 같게 본다"
    assert row["order_status"] == "취소"
    assert int(row["qty"]) == 10


def test_취소_응답에_주문번호가_없으면_합성_키로_남긴다(trader):
    _stop_loss_cancel(trader, {'rt_cd': '0', 'output': {}})
    assert _db().get_trade_by_odno(f"CANCEL_{ODNO}", on_date=_today()), \
        "취소 이력 행의 키는 없으면 합성값으로 남긴다(engine 과 같은 규칙)"


def test_취소에_실패하면_이력을_남기지_않는다(trader):
    ins = MagicMock()
    _stop_loss_cancel(trader, {'rt_cd': '1', 'msg1': '주문 불가'}, insert=ins)
    ins.assert_not_called()


def test_이력은_알림보다_먼저_남긴다(trader):
    """체결 감시는 다른 스레드다 — 알림을 보내는 사이에 감시가 돌면 이력이 아직 없다."""
    order = []
    with patch('modules.auto_trade.db_manager.db.get_trade_by_odno',
               return_value={'type': '매수', 'qty': 10}), \
         patch('modules.auto_trade.api.revise_cancel_order',
               return_value={'rt_cd': '0', 'output': {'ODNO': '9'}}), \
         patch('modules.auto_trade.db_manager.db.insert_trade',
               side_effect=lambda *a, **k: order.append('insert')), \
         patch('modules.auto_trade.api.send_telegram_message',
               side_effect=lambda *a, **k: order.append('telegram')):
        trader._cancel_pending_buy_on_stop_loss(CODE, NAME, _holding())
    assert order == ['insert', 'telegram']


def test_이력_기록이_실패해도_알림과_로그는_나간다(trader):
    logged = []
    with patch.object(trader, 'log', side_effect=lambda m, *a, **k: logged.append(m)), \
         patch('modules.auto_trade.db_manager.db.get_trade_by_odno',
               return_value={'type': '매수', 'qty': 10}), \
         patch('modules.auto_trade.api.revise_cancel_order',
               return_value={'rt_cd': '0', 'output': {'ODNO': '9'}}), \
         patch('modules.auto_trade.db_manager.db.insert_trade',
               side_effect=RuntimeError("database is locked")), \
         patch('modules.auto_trade.api.send_telegram_message') as tg:
        trader._cancel_pending_buy_on_stop_loss(CODE, NAME, _holding())
    assert any("즉시 취소했습니다" in m for m in logged)
    tg.assert_called_once()


# ─────────────────────────────────────────────
# ② 체결 감시가 이력이 있는 취소를 외부 취소로 읽지 않는가
# ─────────────────────────────────────────────

def _cancel_item():
    return {'odno': ODNO, 'pdno': CODE, 'prdt_name': NAME,
            'ord_qty': '10', 'tot_ccld_qty': '0', 'cncl_cfrm_qty': '10',
            'rmn_qty': '0', 'avg_prvs': '70000',
            'ord_dt': datetime.now().strftime("%Y%m%d"),
            'sll_buy_dvsn_cd_name': '매수'}


def _monitor_cycle(lookup=None):
    """취소 1건을 흘려보내고 (텔레그램 mock, insert_trade mock) 을 돌려준다.

    lookup 이 None 이면 취소 이력 조회는 진짜 DB 를 본다.
    """
    monitor = ConclusionMonitor()
    patches = [
        patch('modules.auto_trade.api.get_today_history',
              return_value={'rt_cd': '0', 'output1': [_cancel_item()]}),
        patch('modules.auto_trade.api.get_overseas_today_history',
              return_value={'rt_cd': '0', 'output1': []}),
        patch('modules.auto_trade.db_manager.db.get_trade_by_odno', return_value=None),
        patch('modules.auto_trade.db_manager.db.get_reserved_order_by_odno', return_value=None),
        patch('modules.auto_trade.db_manager.db.check_trade_exists', return_value=False),
    ]
    if lookup is not None:
        patches.append(patch('modules.auto_trade.db_manager.db.get_cancel_record_by_org_odno',
                             **lookup))
    ins = patch('modules.auto_trade.db_manager.db.insert_trade')
    tg = patch('modules.auto_trade.api.send_telegram_message')
    for p in patches:
        p.start()
    ins_m, tg_m = ins.start(), tg.start()
    try:
        monitor._check_conclusions(initial=False)
    finally:
        tg.stop()
        ins.stop()
        for p in reversed(patches):
            p.stop()
    return tg_m, ins_m


def _reported_external(tg, ins):
    return (bool([c for c in tg.call_args_list if "외부 취소" in str(c)]),
            bool([c for c in ins.call_args_list if "(외부)" in str(c)]))


@pytest.mark.parametrize("reason", [
    "사용자 취소",                    # 메뉴 수동 취소(modules/trading.py) — 2026-06-23 실측
    "손절 보호 (자동 취소)",          # 손절 보호
    "미체결 시간 초과 (자동 취소)",   # 시간 초과 자동 취소(engine) — 종전에도 걸렀다
    "외부/사후 취소 감지 (전량 취소)",  # 이 감시가 이미 알린 외부 취소
])
def test_우리가_남긴_취소_이력이면_외부_취소로_알리지_않는다(reason):
    tg, ins = _monitor_cycle({'return_value': {'id': 1, 'reason': reason}})
    assert _reported_external(tg, ins) == (False, False), \
        f"사유 '{reason}' 의 취소를 외부 취소로 알리고 원장에 한 번 더 남겼다"


def test_이력이_없으면_종전대로_외부_취소로_알린다():
    """대조군 — 진짜 외부 취소는 그대로 알린다."""
    tg, ins = _monitor_cycle({'return_value': None})
    assert _reported_external(tg, ins) == (True, True)


def test_손절_보호_취소가_끝까지_외부_취소로_읽히지_않는다(trader):
    """종단 — 손절 보호가 남긴 진짜 DB 행을 체결 감시가 진짜 조회로 찾는다."""
    _stop_loss_cancel(trader, {'rt_cd': '0', 'output': {'ODNO': '0000777098'}})
    tg, ins = _monitor_cycle()
    assert _reported_external(tg, ins) == (False, False)
