"""잔고 어댑터는 매도 판정이 읽는 필드를 반드시 채워야 한다.

2026-09-05 감사 · 실사용 모드(토스)에서 발견:
 `api.toss._toss_domestic_balance` 가 `ord_psbl_qty` 를 채우지 않았다. 매도 워커
 (trader._sell_worker)는 그 값으로 게이트한다 —

     qty = api.safe_int(item.get('ord_psbl_qty'))
     if qty <= 0:  → '[분석스킵] 주문 가능 수량 0' 으로 return

 즉 **토스 모드의 국내 보유 종목 전부가 손절·트레일링 판정에서 빠져 무방비였다.**
 같은 파일의 해외 어댑터(_toss_overseas_balance)에는 그 키가 있었다 — 한쪽만 빠진
 형태라 눈에 띄지 않았다.

이 파일은 어댑터가 늘어나도 같은 구멍이 다시 나지 않게 계약을 고정한다.
"""
from unittest.mock import patch

import pytest

import api
from api import toss as at


#  매도 판정(_sell_worker)과 미관리 경보(_alert_unmanaged_stop)가 읽는 국내 잔고 필드.
#  하나라도 비면 그 종목은 시스템이 지켜 주지 못한다.
_DOMESTIC_REQUIRED = (
    'pdno',            # 종목코드
    'prdt_name',       # 종목명
    'hldg_qty',        # 보유수량
    'ord_psbl_qty',    # 주문가능수량 — 0이면 매도 판정 자체를 건너뛴다
    'pchs_avg_pric',   # 매입평단 (손절선 계산)
    'prpr',            # 현재가 (트리거)
    'evlu_pfls_rt',    # 평가손익률 (손절·트레일링 판정)
    'evlu_amt',
    'evlu_pfls_amt',
)


def _toss_item(country='KR', qty=10):
    return {'marketCountry': country, 'symbol': '005930', 'name': '삼성전자',
            'quantity': qty, 'averagePurchasePrice': 70000, 'lastPrice': 60000,
            'market': 'NASD',
            'marketValue': {'amount': 600000},
            'profitLoss': {'amount': -100000, 'rate': -0.1429}}


@pytest.fixture
def toss_holdings():
    def _run(items):
        with patch.object(at.toss_api, 'get_holdings', return_value={'items': items}), \
             patch.object(at, '_toss_krw_deposit', return_value=0):
            return at._toss_domestic_balance()
    return _run


def test_toss_domestic_balance_has_every_field_the_sell_path_reads(toss_holdings):
    out1, _ = toss_holdings([_toss_item()])
    assert out1, "보유 종목이 잔고에서 사라졌다"
    missing = [k for k in _DOMESTIC_REQUIRED if k not in out1[0]]
    assert not missing, f"매도 판정이 읽는 필드가 없다: {missing}"


def test_sell_gate_sees_a_positive_quantity(toss_holdings):
    """이 값이 0이면 손절·트레일링이 통째로 꺼진다 — 결함의 핵심."""
    out1, _ = toss_holdings([_toss_item(qty=10)])
    assert api.safe_int(out1[0].get('ord_psbl_qty')) == 10


def test_zero_holding_still_reports_zero(toss_holdings):
    """실제로 0주면 0이어야 한다 — 무조건 채우는 것이 아니다."""
    out1, _ = toss_holdings([_toss_item(qty=0)])
    assert api.safe_int(out1[0].get('ord_psbl_qty')) == 0


def test_overseas_adapter_keeps_its_field():
    """대조군 — 해외 어댑터는 종전부터 채우고 있었다."""
    with patch.object(at.toss_api, 'get_holdings',
                      return_value={'items': [_toss_item(country='US')]}):
        rows = at._toss_overseas_balance()
    assert rows and api.safe_int(rows[0]['ord_psbl_qty']) == 10


def test_paper_adapter_fills_it_too():
    """관찰모드(가상투자) 어댑터도 같은 계약을 지켜야 한다."""
    import inspect

    from modules import paper_broker
    src = inspect.getsource(paper_broker)
    assert "'ord_psbl_qty'" in src, "가상 잔고에 주문가능수량이 없으면 매도 판정이 꺼진다"


def test_sell_worker_still_gates_on_this_field():
    """게이트가 이 필드를 읽는다는 사실 자체를 고정한다 — 계약의 반대편이다."""
    import inspect

    from modules import auto_trade
    src = inspect.getsource(auto_trade.AutoTrader._check_sell_conditions)
    assert "item.get('ord_psbl_qty')" in src
    assert "주문 가능 수량 0" in src


# --------------------------------------------------------------------------
# 매도가능수량 — 조회 실패와 '못 판다'를 가른다
# --------------------------------------------------------------------------
#  같은 부류의 두 번째 결함(2026-09-05): KIS 경로는 조회 실패를 None 으로 돌려주도록
#  이미 고쳐져 있었는데(fetch_sellable_quantity 주석) 토스 어댑터만 0을 돌려줬다.
#  호출부는 0을 '팔 수 없는 상태'로 읽어 **매도를 중단**한다 — 일시적 조회 실패가
#  손절을 거른다. 추세추종에서 못 파는 비용은 못 사는 비용보다 훨씬 크다.
def test_toss_sellable_failure_is_unknown_not_zero():
    with patch.object(at.toss_api, 'get_sellable_quantity',
                      side_effect=at.toss_api.TossApiError("x", "boom")):
        assert at._toss_sellable_qty("005930") is None, (
            "조회 실패를 0으로 답하면 호출부가 매도를 중단한다")


def test_toss_sellable_empty_response_is_unknown():
    with patch.object(at.toss_api, 'get_sellable_quantity', return_value=None):
        assert at._toss_sellable_qty("005930") is None


def test_toss_sellable_real_zero_is_zero():
    """진짜 0주면 0이어야 한다 — 실패와 구분되는 값이다."""
    with patch.object(at.toss_api, 'get_sellable_quantity',
                      return_value={'sellableQuantity': 0}):
        assert at._toss_sellable_qty("005930") == 0


def test_toss_sellable_normal_value():
    with patch.object(at.toss_api, 'get_sellable_quantity',
                      return_value={'sellableQuantity': 7}):
        assert at._toss_sellable_qty("005930") == 7


def test_sell_worker_treats_unknown_as_hold_quantity():
    """None 이면 잔고 수량으로 진행한다 — 이 계약이 있어야 위 수정이 뜻을 가진다."""
    import inspect

    from modules import auto_trade
    src = inspect.getsource(auto_trade.AutoTrader._check_sell_conditions)
    assert "if real_qty is None:" in src
    assert "조회 실패를 '매도 불가'로 읽지 않는다" in src
