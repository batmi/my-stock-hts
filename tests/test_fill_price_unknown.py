"""체결 수량은 있는데 단가를 못 읽으면 0원으로 적지 않는다.

[왜] 체결 감시는 응답의 체결 단가를 그대로 원장에 적었다. 그 필드가 비거나 없거나
 null 이면 `safe_float(..., default=0.0)` 이 0 으로 접는다 — 그러면 **100주가 0원에
 체결된 행**이 남는다. 실측(같은 100주 전량 체결 응답, 단가 필드만 바꿈):
     정상 70000 → ('체결','100','70000.0')  /  빈 문자열·필드 누락·null → ('체결','100','0.0')
 0원 체결은 평단·실현손익·손절선·트레일링 앵커의 근거를 통째로 무너뜨리고,
 이어지는 update_trade(price=...) 가 **접수 행의 주문가까지** 0으로 덮는다.
 그리고 그대로 매매일지로 나간다.

 가정이 아니다 — 이 파일 자신이 avg_prvs 가 빈 문자열인 실측을 2026-09-06 에 적어 뒀고,
 토스 어댑터의 매도가능수량도 같은 모양이었다([[toss-balance-sell-gate]]).

[어떻게 고쳤나] 같은 복구가 **취소 경로에는 이미 있었다**(price_val — 단가를 못 읽으면
 원 주문가로 메운다). 정작 체결 경로만 날것을 쓰고 있었다. 그 복구를 체결에도 쓴다.
 원 주문 기록이 없는 외부 주문은 메울 근거가 없으므로 그대로 적되(포지션은 실재한다 —
 원장에 없으면 손절·트레일링 감시 대상도 못 된다) 조용히 넘기지 않는다.
"""
import logging
from datetime import datetime
from unittest.mock import patch

import pytest

import config
from modules import db_manager
from modules.auto_trade import ConclusionMonitor

ODNO, CODE, NAME = "0000777777", "005930", "삼성전자"
_ORD_DT = datetime.now().strftime("%Y%m%d")
_EMPTY = {"rt_cd": "0", "msg_cd": "", "output": []}
_MISSING = object()
ORDER_PRICE = 71000


def _history(avg_prvs, ccld=100):
    item = {"odno": ODNO, "pdno": CODE, "prdt_name": NAME,
            "ord_qty": "100", "tot_ccld_qty": str(ccld), "cncl_cfrm_qty": "0",
            "rmn_qty": str(100 - ccld), "sll_buy_dvsn_cd_name": "매수",
            "sll_buy_dvsn_cd": "02", "ord_dt": _ORD_DT, "ord_tmd": "091500"}
    if avg_prvs is not _MISSING:
        item["avg_prvs"] = avg_prvs
    return {"rt_cd": "0", "msg_cd": "", "output1": [item], "output2": {}}


@pytest.fixture
def monitor(monkeypatch):
    for a, v in (('cano', "12345678"), ('acnt_prdt_cd', "01"), ('is_toss', False),
                 ('is_paper', False), ('auto_cano', ""), ('auto_acnt_prdt_cd', "")):
        monkeypatch.setattr(config.session, a, v, raising=False)
    ConclusionMonitor._instance = None
    m = ConclusionMonitor()
    m.order_status = {}
    m.cancel_status = {}
    yield m
    ConclusionMonitor._instance = None


def _poll(monitor, payload):
    with patch('modules.auto_trade.api.get_today_history', return_value=payload), \
         patch('modules.auto_trade.api.get_overseas_today_history', return_value=_EMPTY), \
         patch('modules.auto_trade.api.send_telegram_message'), \
         patch('modules.auto_trade.api.get_current_price_data', return_value={"rt_cd": "1"}), \
         patch('modules.auto_trade.api.get_domestic_balance', return_value=(None, None)), \
         patch.object(ConclusionMonitor, '_send_trading_autopsy', lambda *a, **k: None), \
         patch('modules.auto_trade.api.get_chart_data', return_value=None):
        monitor._check_conclusions(initial=False)


def _place_order_row():
    db_manager.db.insert_trade("매수(AUTO)", CODE, NAME, 100, ORDER_PRICE, ODNO,
                               order_status="접수", reason="[추세매수] 조건 만족")


def _rows(status=None):
    return [r for r in (db_manager.db.get_trades(limit=200) or [])
            if str(r.get('odno')) == ODNO and (status is None or r.get('order_status') == status)]


def _price_of(status):
    rows = _rows(status)
    return float(rows[0]['price']) if rows else None


BROKEN = [("빈 문자열", ""), ("필드 누락", _MISSING), ("null", None)]


@pytest.mark.parametrize("label,avg", BROKEN)
def test_단가를_못_읽으면_원_주문가로_메운다(monitor, label, avg):
    _place_order_row()
    _poll(monitor, _history(avg))
    assert _price_of("체결") == pytest.approx(ORDER_PRICE), \
        f"{label}: 0원 체결이 원장에 남았다"


@pytest.mark.parametrize("label,avg", BROKEN)
def test_접수_행의_주문가를_0으로_덮지_않는다(monitor, label, avg):
    """이 갱신의 목적은 '시장가 접수 단가를 체결가로 채우는 것'이다.
    모르는 값으로 아는 값을 지우면 그 반대가 된다."""
    _place_order_row()
    _poll(monitor, _history(avg))
    assert _price_of("접수") == pytest.approx(ORDER_PRICE)


def test_정상_응답은_체결가를_그대로_쓴다(monitor):
    """대조군 — 복구가 정상 값을 가로채면 안 된다(체결가 ≠ 주문가)."""
    _place_order_row()
    _poll(monitor, _history("70000"))
    assert _price_of("체결") == pytest.approx(70000)
    assert _price_of("접수") == pytest.approx(70000), "시장가 접수 단가 채우기가 죽었다"


def test_메울_근거가_없으면_시끄럽게_적는다(monitor, caplog):
    """외부 주문은 접수 행이 없다 — 그래도 행은 적는다(원장에 없으면 손절 감시도 못 한다)."""
    with caplog.at_level(logging.ERROR, logger="modules.auto_trade.conclusion"):
        _poll(monitor, _history(""))
    assert _rows("체결"), "포지션은 실재하는데 원장에서 통째로 빠졌다"
    loud = [r.message for r in caplog.records if "단가 0원" in r.message]
    assert loud, f"0원 기록이 조용히 지나갔다: {[r.message for r in caplog.records]}"


def test_부분체결_누적_갱신도_복구값을_쓴다(monitor):
    _place_order_row()
    _poll(monitor, _history("", ccld=30))
    assert _price_of("체결") == pytest.approx(ORDER_PRICE)
    _poll(monitor, _history("", ccld=100))
    rows = _rows("체결")
    assert len(rows) == 1 and int(rows[0]['qty']) == 100
    assert float(rows[0]['price']) == pytest.approx(ORDER_PRICE)
