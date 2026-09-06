"""체결 한 건의 오류가 그 계좌의 대사를 통째로 끊지 않는가.

[왜 중요한가] 체결 기록은 손절선·진입일·트레일링 앵커·실현손익이 붙는 **근거**다.
기록이 없으면 그 포지션은 관리 밖으로 떨어진다. 그런데 종전에는 응답 한 건이 깨지면
그 계좌의 **다른 종목 체결까지** 사라졌고, 같은 응답이 매 주기 다시 오므로 그 상태가
영구히 반복됐다. 예약 주문 감시에서 같은 모양을 고쳤는데 체결 쪽이 남아 있었다.
"""
from datetime import datetime
from unittest.mock import patch

import pytest

import config
from modules import db_manager
from modules.auto_trade.conclusion import ConclusionMonitor

_EMPTY = {"rt_cd": "0", "msg_cd": "", "output": []}
_ORD_DT = datetime.now().strftime("%Y%m%d")
BROKEN, GOOD = "0000A01", "0000A02"


def _item(odno, code, name, avg):
    return {"odno": odno, "pdno": code, "prdt_name": name,
            "ord_qty": "10", "tot_ccld_qty": "10", "cncl_cfrm_qty": "0", "rmn_qty": "0",
            "avg_prvs": avg, "sll_buy_dvsn_cd_name": "매수", "sll_buy_dvsn_cd": "02",
            "ord_dt": _ORD_DT, "ord_tmd": "091500"}


@pytest.fixture
def monitor(monkeypatch):
    monkeypatch.setattr(config.session, 'cano', "12345678", raising=False)
    monkeypatch.setattr(config.session, 'acnt_prdt_cd', "01", raising=False)
    monkeypatch.setattr(config.session, 'is_toss', False, raising=False)
    monkeypatch.setattr(config.session, 'is_paper', False, raising=False)
    monkeypatch.setattr(config.session, 'auto_cano', "", raising=False)
    monkeypatch.setattr(config.session, 'auto_acnt_prdt_cd', "", raising=False)
    ConclusionMonitor._instance = None
    m = ConclusionMonitor()
    m.order_status = {}
    m.cancel_status = {}
    yield m
    ConclusionMonitor._instance = None
    conn = db_manager.db._get_conn()
    conn.cursor().execute("DELETE FROM trades WHERE odno IN (?, ?)", (BROKEN, GOOD))
    conn.commit()


#  '한 건이 깨진다'를 만들기 위한 주입(_poll 의 boom_odno). 특정 주문번호에서만 던지게
#  해서 **어떤** 예외든 그 건에서 멈추고 나머지는 계속 도는지를 본다 — 원인은 avg_prvs
#  하나가 아니다. 응답 필드에는 언제든 예상 밖 값이 온다.


def _poll(monitor, payload, boom_odno=None):
    import modules.auto_trade.conclusion as C
    with patch('modules.auto_trade.api.get_today_history', return_value=payload), \
         patch('modules.auto_trade.api.get_overseas_today_history', return_value=_EMPTY), \
         patch('modules.auto_trade.api.send_telegram_message'), \
         patch('modules.auto_trade.api.get_current_price_data', return_value={"rt_cd": "1"}), \
         patch('modules.auto_trade.api.get_domestic_balance', return_value=(None, None)), \
         patch.object(ConclusionMonitor, '_send_trading_autopsy', lambda *a, **k: None), \
         patch('modules.auto_trade.api.get_chart_data', return_value=None):
        if boom_odno is not None:
            import modules.auto_trade.conclusion as C
            real = C._odno_scope_date
            def fake(item, *a, **k):
                if isinstance(item, dict) and item.get('odno') == boom_odno:
                    raise RuntimeError("이 건에서 터진다")
                return real(item, *a, **k)
            with patch.object(C, '_odno_scope_date', fake):
                return monitor._check_conclusions(initial=False)
        return monitor._check_conclusions(initial=False)


def _recorded(odno):
    return [r for r in (db_manager.db.get_trades(limit=300) or [])
            if str(r.get('odno')) == odno and r.get('order_status') == "체결"]


def test_빈_단가는_예외가_아니라_0으로_읽는다(monitor):
    """같은 응답의 수량은 전부 safe_int 로 받는데 단가만 맨 float() 였다.

    증권사는 값이 없을 때 빈 문자열을 준다 — float('') 는 ValueError 이고,
    dict.get 의 기본값 0 은 **키가 없을 때만** 쓰인다.
    """
    payload = {"rt_cd": "0", "msg_cd": "", "output2": {},
               "output1": [_item(GOOD, "000660", "SK하이닉스", "")]}
    limited, has_error = _poll(monitor, payload)
    assert has_error is False, "빈 단가 하나로 대사가 실패로 떨어진다"


def test_한_건이_깨져도_다른_종목의_체결은_기록된다(monitor):
    """이것이 이 파일의 요점이다 — 남의 종목 체결이 사라지면 그 포지션은 무방비다."""
    payload = {"rt_cd": "0", "msg_cd": "", "output2": {}, "output1": [
        _item(BROKEN, "005930", "삼성전자", "70000"),
        _item(GOOD, "000660", "SK하이닉스", "180000"),
    ]}
    _poll(monitor, payload, boom_odno=BROKEN)
    assert _recorded(GOOD), \
        "앞 건의 오류로 뒤 종목의 체결이 통째로 사라졌다 — 손절선이 붙을 근거가 없어진다"


def test_깨진_건은_조용히_넘기지_않는다(monitor):
    """건너뛰되 세어야 한다 — 연속 에러가 Kill Switch(is_healthy)에 실린다."""
    payload = {"rt_cd": "0", "msg_cd": "", "output2": {},
               "output1": [_item(BROKEN, "005930", "삼성전자", "70000")]}
    limited, has_error = _poll(monitor, payload, boom_odno=BROKEN)
    assert has_error is True, "처리하지 못한 체결이 있는데 '이상 없음'으로 답한다"


def test_예수금_조회는_외화_한_필드로_무너지지_않는다(monkeypatch):
    """3단계(외화)는 '보조' 조회다. 여기서 던지면 1·2단계에서 이미 받아 둔
    주문가능금액·예수금까지 함께 버려진다 — 매수 여력을 모르는 채 주기가 지나간다."""
    import api as api_mod
    from api import orders as orders_mod

    monkeypatch.setattr(orders_mod, 'get_deposit', lambda *a, **k: {
        'rt_cd': '0', 'output': {'nrcvb_buy_amt': '9000000', 'ord_psbl_cash': '9000000'}})
    monkeypatch.setattr(api_mod, 'get_domestic_balance', lambda *a, **k: (
        [], [{'dnca_tot_amt': '9000000', 'prvs_rcdl_excc_amt': '9000000'}]))
    #  외화 보유가 없으면 증권사는 빈 문자열을 준다.
    monkeypatch.setattr(orders_mod, 'get_foreign_deposit', lambda *a, **k: {
        'rt_cd': '0', 'output2': [{'frcr_evlu_tota': '', 'prvs_rcdl_excc_amt': '',
                                   'dnca_tot_amt': ''}]})

    res = orders_mod.get_deposit_balance("12345678", "01")
    assert res is not None, "외화 한 필드 때문에 예수금 조회가 통째로 실패했다"
    assert res['order_possible'] == 9000000, res
    assert res['foreign_deposit'] == 0, res
