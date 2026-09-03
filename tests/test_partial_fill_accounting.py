"""부분체결이 여러 폴링 주기에 걸칠 때 체결 수량이 정확히 기록되는가.

관찰 모드(mode 1)는 '즉시 전량 체결'로 모델링하므로 이 경로가 한 번도 실행되지 않는다.
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
    # [테스트 격리] 매도 체결은 매매 부검(AI)을 **데몬 스레드**로 띄운다. 그 스레드는
    #  테스트보다 오래 살아서, conftest 의 네트워크 차단이 풀린 뒤(세션 종료 시점)
    #  실제 KIS·Gemini 로 나갈 수 있다. 실측: 이 파일에 매도 검사를 더하자
    #  inquire-balance 가 실 서버 응답(OPSQ2000)을 받아 왔다. 스레드를 띄우는 지점 자체를
    #  막는다 — 부검은 이 파일이 검증하는 대상이 아니다.
    with patch('modules.auto_trade.api.get_today_history', return_value=payload), \
         patch('modules.auto_trade.api.get_overseas_today_history', return_value=_EMPTY), \
         patch('modules.auto_trade.api.send_telegram_message'), \
         patch('modules.auto_trade.api.get_current_price_data', return_value={"rt_cd": "1"}), \
         patch('modules.auto_trade.api.get_domestic_balance', return_value=(None, None)), \
         patch.object(ConclusionMonitor, '_send_trading_autopsy', lambda *a, **k: None), \
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


# ---------------------------------------------------------------- 실현손익
# 위 검사는 전부 **매수** 주문이다. 매수는 실현손익이 없어 _recalc_realized 가 곧바로
# 빠져나가므로, 손익이 누적을 따라오는지는 한 번도 확인되지 않았다.
#
# [무엇이 틀렸었나 · 2026-09-03] 부분체결 갱신 분기가 수량·단가만 고치고 profit_amt 는
# **첫 관측 시점의 수량으로 계산된 값** 그대로 두었다. 실측: 30주 관측 후 100주 체결 시
# 실현손익이 70% 과소 기록. 이 값은 성과 지표에서 끝나지 않는다 —
# db.get_realized_profit_between 을 지나 **입출금 판정**까지 가므로, 적게 센 만큼이
# 가짜 입금으로 둔갑해 자산 기준선이 밀린다(daily-asset-baseline-transfers).
#
# 관찰 모드는 즉시 전량 체결이라 이 경로를 밟지 않는다. 실계좌 자동매매를 시작하는
# 순간 처음 나타나는 자리다.
SELL_ODNO = "0000654321"
BUY_PRICE = 70000.0
SELL_PRICE = 77000.0


def _sell_history(ord_qty, ccld_qty, rmn_qty, avg_price=SELL_PRICE):
    return {
        "rt_cd": "0", "msg_cd": "",
        "output1": [{
            "odno": SELL_ODNO, "pdno": CODE, "prdt_name": NAME,
            "ord_qty": str(ord_qty), "tot_ccld_qty": str(ccld_qty),
            "cncl_cfrm_qty": "0", "rmn_qty": str(rmn_qty),
            "avg_prvs": str(avg_price), "sll_buy_dvsn_cd_name": "매도",
            "sll_buy_dvsn_cd": "01", "ord_dt": "20260903", "ord_tmd": "091500",
        }],
        "output2": {},
    }


def _sell_row():
    for r in (db_manager.db.get_trades(limit=300) or []):
        if str(r.get('odno')) == SELL_ODNO and r.get('order_status') == "체결":
            return r
    return None


@pytest.fixture
def sell_order():
    """매도 접수 기록을 심는다 — buy_price 가 있어야 손익을 다시 계산할 수 있다."""
    db_manager.db.insert_trade("sell(AUTO)", CODE, NAME, 100, str(SELL_PRICE), SELL_ODNO,
                               order_status="접수", reason="트레일링스탑",
                               buy_price=BUY_PRICE, profit_amt=0, profit_rate=0.0)
    yield


def test_partial_sell_profit_follows_the_accumulated_quantity(monitor, sell_order):
    """분할 매도의 실현손익이 최종 체결 수량 기준으로 남아야 한다."""
    from core import trading_cost

    _poll(monitor, _sell_history(ord_qty=100, ccld_qty=30, rmn_qty=70))
    first = _sell_row()
    assert first is not None, "1차 부분체결이 기록되지 않았다"
    expected_30 = int(trading_cost.net_realized_profit(BUY_PRICE, SELL_PRICE, 30)[0])
    assert abs(int(first['profit_amt']) - expected_30) <= 1, (
        f"1차 손익이 30주 기준이 아니다: {first['profit_amt']}")

    _poll(monitor, _sell_history(ord_qty=100, ccld_qty=100, rmn_qty=0))
    final = _sell_row()
    expected_100 = int(trading_cost.net_realized_profit(BUY_PRICE, SELL_PRICE, 100)[0])
    assert int(final['qty']) == 100
    assert abs(int(final['profit_amt']) - expected_100) <= 1, (
        f"실현손익이 첫 관측 수량에 굳었다: {final['profit_amt']} (기대 {expected_100}). "
        f"이 값은 성과 지표뿐 아니라 입출금 판정까지 간다")


def test_partial_sell_profit_rate_is_quantity_independent(monitor, sell_order):
    """수익률은 수량과 무관하므로 두 시점이 같아야 한다 — 산식이 바뀌면 여기서 걸린다."""
    _poll(monitor, _sell_history(ord_qty=100, ccld_qty=30, rmn_qty=70))
    rate_30 = float(_sell_row()['profit_rate'])
    _poll(monitor, _sell_history(ord_qty=100, ccld_qty=100, rmn_qty=0))
    rate_100 = float(_sell_row()['profit_rate'])
    assert abs(rate_30 - rate_100) < 0.01, f"{rate_30} vs {rate_100}"


def test_buy_order_profit_is_left_alone(monitor):
    """매수는 실현손익이 없다 — 갱신이 0이 아닌 값을 밀어 넣으면 안 된다."""
    _poll(monitor, _history(ord_qty=100, ccld_qty=30, rmn_qty=70))
    _poll(monitor, _history(ord_qty=100, ccld_qty=100, rmn_qty=0))
    rows = [r for r in (db_manager.db.get_trades(limit=300) or [])
            if str(r.get('odno')) == ODNO and r.get('order_status') == "체결"]
    assert rows, "체결 행이 없다"
    assert not int(rows[0]['profit_amt'] or 0), f"매수에 손익이 붙었다: {rows[0]['profit_amt']}"
