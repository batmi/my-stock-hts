"""마감 후 청산 신호 스캔의 하루-1회 게이트는 '일이 끝난 것'을 보고 찍는다.

[왜] `_scan_after_hours_sell_signals` 는 잔고를 조회하기 **전에** after_hours_scan_date 를
 찍었다. 그래서
   · 잔고 조회가 실패하면(None) `if not holdings: return` 에 걸려 '보유 없음'과 같은
     자리에서 조용히 빠졌고, 그 거래일의 마감 후 점검은 통째로 사라졌다.
   · 판정 도중 예외가 나도 마찬가지였다(바깥 except 는 debug 한 줄).
 하필 _alert_after_hours_sell 은 '전달을 확인한 뒤에 기록한다 — 실패하면 다음 주기에
 다시 시도'로 고쳐 둔 자리다. 그 재시도가 이 바깥 게이트에 막혀 죽은 코드였다.

 이 스캔은 종가가 확정된 뒤 손절·트레일링선 이탈을 알리는 **유일한** 경로다
 (마감과 함께 분석이 멈춘다). 놓치면 하룻밤 갭이 그대로 손실이 된다.
 [[unknown-vs-empty]]
"""
from datetime import datetime
from unittest.mock import patch

import pytest

import config
from modules.auto_trade import AutoTrader

TODAY = datetime.now().strftime("%Y%m%d")


@pytest.fixture
def trader():
    AutoTrader._instance = None
    t = AutoTrader()
    t.is_running = True
    t.after_hours_scan_date = None
    t.after_hours_sell_notified = {}
    return t


def _run(trader, balance, checked=None, rules=None):
    """스캔 한 주기. balance 는 api.get_domestic_balance 의 (holdings, summary)."""
    calls = []

    def fake_check(_self, holdings, **kw):    # 클래스에 붙이므로 self 가 온다
        calls.append(holdings)
        if checked:
            checked(holdings)

    with patch('modules.auto_trade.trader.api.is_holiday_today', return_value=False), \
         patch('modules.auto_trade.trader.api.krx_last_settled_day', return_value=TODAY), \
         patch('modules.auto_trade.trader.api.get_domestic_balance', return_value=balance), \
         patch('modules.auto_trade.trader.db_manager.db.get_all_stock_strategies',
               return_value=(rules if rules is not None else [])), \
         patch('modules.auto_trade.trader.get_restricted_stocks', return_value={}), \
         patch.object(config, 'AFTER_HOURS_SELL_ALERT', True, create=True), \
         patch.object(config, 'AFTER_HOURS_SELL_ALERT_TIME', "0000", create=True), \
         patch.object(type(trader), '_check_sell_conditions', fake_check):
        trader._scan_after_hours_sell_signals("12345678")
    return calls


HOLDING = [{'pdno': '005930', 'prdt_name': '삼성전자', 'hldg_qty': '10',
            'pchs_avg_pric': '100000', 'prpr': '88000'}]


def test_잔고를_못_읽으면_오늘을_소비하지_않는다(trader):
    """조회 실패(None)는 '보유 없음'이 아니다."""
    calls = _run(trader, (None, None))
    assert calls == [], "판정이 돌 리 없다"
    assert trader.after_hours_scan_date is None, "실패했는데 오늘 점검이 끝난 것으로 굳었다"

    # 다음 주기에 실제로 다시 본다.
    calls = _run(trader, (HOLDING, {}))
    assert len(calls) == 1
    assert trader.after_hours_scan_date == TODAY


def test_판정이_던져도_오늘을_소비하지_않는다(trader):
    def boom(_h):
        raise RuntimeError("차트 조회 실패")

    _run(trader, (HOLDING, {}), checked=boom)
    assert trader.after_hours_scan_date is None

    calls = _run(trader, (HOLDING, {}))
    assert len(calls) == 1 and trader.after_hours_scan_date == TODAY


def test_보유가_정말_없는_날은_오늘을_찍는다(trader):
    """빈 목록은 실패가 아니다 — 매 주기 다시 조회하면 마감 후 트래픽만 는다."""
    calls = _run(trader, ([], {}))
    assert calls == []
    assert trader.after_hours_scan_date == TODAY

    calls = _run(trader, (HOLDING, {}))
    assert calls == [], "이미 끝난 날인데 다시 돌았다"


def test_정상_스캔은_하루_한_번만_돈다(trader):
    assert len(_run(trader, (HOLDING, {}))) == 1
    assert trader.after_hours_scan_date == TODAY
    assert _run(trader, (HOLDING, {})) == []
