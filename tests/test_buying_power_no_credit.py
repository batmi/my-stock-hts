"""매수여력이 '미수 없는 금액'으로 산출되는지 검증한다.

[왜 중요한가] 이 시스템은 자본대비 리스크·변동성 한도로 포지션 크기를 정한다. 그 한도의
분모가 '증권사가 허용하는 최대 매수여력'이면 한도가 의미를 잃는다 — 신용·대용 여력까지
끌어 쓰면 자본 대비 노출이 설계값을 넘고, 미수가 나면 연체이자와 반대매매가 붙어
**손절 규칙 바깥에서** 포지션이 정리된다. 추세추종에서 청산 시점을 시스템이 통제하지
못하는 상태가 가장 나쁘다.

KIS 매수가능조회(TTTC8908R) 응답 필드:
  - nrcvb_buy_amt / nrcvb_buy_qty : 미수 없는 매수금액·수량
  - ord_psbl_amt  / ord_psbl_qty  : 주문가능금액·수량(계좌에 신용·대용 여력이 있으면 포함)

실측(2026-08-09) 결과 운용 계좌들은 ord_psbl_* 자체가 응답에 없어 이미 폴백으로 안전했다.
그건 우연이므로 선호 순서를 뒤집어 명시적으로 만들었다. 이 테스트가 그 순서를 고정한다.
"""
import pytest

import api
import config


@pytest.fixture
def real_mode(monkeypatch):
    monkeypatch.setattr(config.session, 'is_toss', False, raising=False)
    monkeypatch.setattr(config.session, 'is_paper', False, raising=False)
    monkeypatch.setattr(config.session, 'cano', "11111111", raising=False)
    monkeypatch.setattr(config.session, 'acnt_prdt_cd', "01", raising=False)
    monkeypatch.setattr(config.session, 'auto_cano', "", raising=False)


def test_order_possible_prefers_the_credit_free_amount(real_mode, monkeypatch):
    """신용 포함 금액(ord_psbl_amt)이 더 커도 미수 없는 금액을 쓴다."""
    monkeypatch.setattr(api, 'get_deposit', lambda *a, **k: {
        'rt_cd': '0',
        'output': {'ord_psbl_amt': '50000000',      # 신용·대용 포함
                   'nrcvb_buy_amt': '10000000'},    # 미수 없는 금액
    })
    monkeypatch.setattr(api, 'get_domestic_balance', lambda *a, **k: ([], []))

    res = api.get_deposit_balance("11111111", "01")
    assert res['order_possible'] == 10_000_000, (
        f"매수여력이 {res['order_possible']:,}원으로 잡혔다 — 신용 여력까지 끌어 쓰면 "
        "자본대비 리스크 한도가 설계값을 넘고 미수·반대매매 위험이 생긴다")


def test_order_possible_falls_back_when_credit_free_field_is_absent(real_mode, monkeypatch):
    """미수없는 필드가 응답에 없으면 주문가능금액으로 폴백한다(거래 중단 방지)."""
    monkeypatch.setattr(api, 'get_deposit', lambda *a, **k: {
        'rt_cd': '0', 'output': {'ord_psbl_amt': '7000000'},
    })
    monkeypatch.setattr(api, 'get_domestic_balance', lambda *a, **k: ([], []))

    res = api.get_deposit_balance("11111111", "01")
    assert res['order_possible'] == 7_000_000


def test_buyable_quantity_prefers_the_credit_free_quantity(real_mode, monkeypatch):
    """수량 경로도 같은 순서를 따른다."""
    monkeypatch.setattr(api, 'call_api', lambda *a, **k: {
        'rt_cd': '0',
        'output': {'ord_psbl_qty': '100',       # 신용 포함
                   'nrcvb_buy_qty': '20',       # 미수 없음
                   'max_buy_qty': '100',
                   'ord_psbl_cash': '99999999'},  # 현금 상한에 걸리지 않게 크게
    })
    qty = api.fetch_buyable_quantity("005930", 70000)
    assert qty == 20, f"미수 없는 수량 20주여야 하는데 {qty}주가 나왔다"


def test_buyable_quantity_is_still_capped_by_actual_cash(real_mode, monkeypatch):
    """현금 상한은 그대로 적용된다(두 방어선이 함께 걸린다)."""
    monkeypatch.setattr(api, 'call_api', lambda *a, **k: {
        'rt_cd': '0',
        'output': {'nrcvb_buy_qty': '20', 'ord_psbl_cash': '350000'},
    })
    qty = api.fetch_buyable_quantity("005930", 70000)
    assert qty == 5, f"현금 350,000원 / 70,000원 = 5주여야 하는데 {qty}주가 나왔다"
