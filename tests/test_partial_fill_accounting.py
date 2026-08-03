"""부분체결이 여러 폴링 주기에 걸칠 때 체결 수량이 정확히 기록되는가.

관찰 모드(mode 4)는 '즉시 전량 체결'로 모델링하므로 이 경로가 한 번도 실행되지 않는다.
실계좌에서 처음 겪는 구간이라 별도로 검증한다.

체결 감시(ConclusionMonitor._check_conclusions)는 KIS 일별주문체결 API의 **누적**
체결수량(tot_ccld_qty)을 폴링한다. 한 주문이 30주 → 100주로 나뉘어 체결되면 두 주기에
걸쳐 관측되는데, 이때 trades 테이블에 최종 100주가 남아야 한다.

여기가 틀리면 성과 지표(PF·승률·누적손익), 손절률 수량가중평균, 매매일지 전송이 모두
어긋난다. 파라미터를 전부 백테스트로 정하는 시스템에서 체결 기록의 오차는 그대로
판단 근거의 오차가 된다.
"""
import pytest
from unittest.mock import patch

import config
from modules import db_manager
from modules.auto_trade import ConclusionMonitor

ODNO = "0000123456"
CODE = "005930"
NAME = "삼성전자"


def _history(ord_qty, ccld_qty, rmn_qty, avg_price=70000):
    """KIS 일별주문체결(국내) 응답 한 건. tot_ccld_qty는 누적 체결수량이다."""
    return {
        "rt_cd": "0", "msg_cd": "",
        "output1": [{
            "odno": ODNO, "pdno": CODE, "prdt_name": NAME,
            "ord_qty": str(ord_qty), "tot_ccld_qty": str(ccld_qty),
            "cncl_cfrm_qty": "0", "rmn_qty": str(rmn_qty),
            "avg_prvs": str(avg_price), "sll_buy_dvsn_cd_name": "매수",
            "sll_buy_dvsn_cd": "02", "ord_dt": "20260804", "ord_tmd": "091500",
        }],
        "output2": {},
    }


_EMPTY = {"rt_cd": "0", "msg_cd": "", "output": []}


@pytest.fixture
def monitor(monkeypatch):
    monkeypatch.setattr(config.session, 'cano', "12345678", raising=False)
    monkeypatch.setattr(config.session, 'acnt_prdt_cd', "01", raising=False)
    monkeypatch.setattr(config.session, 'is_simulation', False, raising=False)
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


def _filled_qty_in_db():
    """trades 테이블에 기록된 이 주문의 '체결' 수량 합계."""
    rows = db_manager.db.get_trades(limit=200) or []
    return sum(int(r['qty']) for r in rows
               if str(r.get('odno')) == ODNO and r.get('order_status') == "체결")


def _poll(monitor, payload):
    with patch('modules.auto_trade.api.get_today_history', return_value=payload), \
         patch('modules.auto_trade.api.get_overseas_today_history', return_value=_EMPTY), \
         patch('modules.auto_trade.api.send_telegram_message'), \
         patch('modules.auto_trade.api.get_current_price_data', return_value={"rt_cd": "1"}), \
         patch('modules.auto_trade.api.get_chart_data', return_value=None):
        monitor._check_conclusions(initial=False)


def test_single_poll_full_fill_is_recorded(monitor):
    """대조군 — 한 주기에 전량 체결되면 그대로 100주가 기록된다."""
    _poll(monitor, _history(ord_qty=100, ccld_qty=100, rmn_qty=0))
    assert _filled_qty_in_db() == 100


def test_partial_fill_across_polls_records_total(monitor):
    """부분체결(30 → 100)이 두 주기에 걸쳐도 최종 100주가 남아야 한다."""
    _poll(monitor, _history(ord_qty=100, ccld_qty=30, rmn_qty=70))
    assert _filled_qty_in_db() == 30, "1차 부분체결이 기록되지 않았다"

    _poll(monitor, _history(ord_qty=100, ccld_qty=100, rmn_qty=0))
    assert _filled_qty_in_db() == 100, (
        "부분체결 증분이 유실됐다 — 성과 지표·손절률 가중평균·매매일지가 모두 어긋난다")


def test_three_step_partial_fill(monitor):
    """3단계(20 → 60 → 100)로 나뉘어도 최종 수량이 맞아야 한다."""
    for ccld, rmn in ((20, 80), (60, 40), (100, 0)):
        _poll(monitor, _history(ord_qty=100, ccld_qty=ccld, rmn_qty=rmn))
    assert _filled_qty_in_db() == 100


def test_repeated_poll_of_same_state_does_not_duplicate(monitor):
    """같은 누적 상태를 다시 폴링해도 수량이 부풀지 않아야 한다(중복 방지)."""
    _poll(monitor, _history(ord_qty=100, ccld_qty=100, rmn_qty=0))
    _poll(monitor, _history(ord_qty=100, ccld_qty=100, rmn_qty=0))
    assert _filled_qty_in_db() == 100, "같은 체결이 두 번 적재됐다"


def test_accepted_row_keeps_ordered_quantity(monitor):
    """'접수' 행은 주문 수량을 보존해야 한다 — 체결 누적 갱신이 원 주문을 덮으면 안 된다.

    같은 odno로 '접수'와 '체결' 행이 함께 존재한다. 수량 갱신이 두 행을 모두 건드리면
    '100주 주문 중 30주 체결'이라는 사실이 사라져 미체결·취소 추적이 무너진다.
    """
    db_manager.db.insert_trade("매수", CODE, NAME, 100, 70000.0, ODNO,
                               order_status="접수", reason="테스트 접수")

    # 끝까지 전량 체결되지 않는 경우로 본다. 최종 체결량이 주문량과 같으면 두 행이 같은
    # 값이 되어, 접수 행을 덮어쓰는 버그가 있어도 드러나지 않는다.
    _poll(monitor, _history(ord_qty=100, ccld_qty=20, rmn_qty=80))
    _poll(monitor, _history(ord_qty=100, ccld_qty=30, rmn_qty=70))

    rows = db_manager.db.get_trades(limit=200) or []
    mine = [r for r in rows if str(r.get('odno')) == ODNO]
    accepted = [r for r in mine if r.get('order_status') == "접수"]
    filled = [r for r in mine if r.get('order_status') == "체결"]

    assert accepted and int(accepted[0]['qty']) == 100, \
        "접수 행의 주문 수량이 체결 수량으로 덮어써졌다 — 미체결·취소 추적이 무너진다"
    assert filled and sum(int(r['qty']) for r in filled) == 30
