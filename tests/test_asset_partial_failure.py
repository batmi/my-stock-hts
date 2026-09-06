"""자산 집계가 **일부만** 실패했을 때, 그 값을 온전한 것처럼 쓰지 않는가.

[왜 중요한가] 총자산은 일일 손실 한도의 분모이자 포지션 사이징의 기준이고,
그날의 기준선으로 저장되면 **하루 종일 고정된다**. 그런데 get_asset_status_data 의 네
구간은 각자 예외를 삼키고 넘어가므로, 한 구간이 실패해도 tot_asset 은 숫자로 나온다 —
그저 그만큼 작을 뿐이다. 실측(주식비중 36% 계좌, 국내 잔고 조회만 실패):

    정상          : 총자산 10,000,000 (주식 3,600,000 + 현금 6,400,000)
    국내잔고 실패 : 총자산  6,400,000 (주식 0 + 현금 6,400,000)   → -36.0%

기존 방어 둘(차단기의 '비정상 급감', is_plausible_baseline)은 **직전 대비 0.5배**를
본다. 이 시스템의 노출 상한이 40%라(4슬롯·균등배분) 주식 평가액이 통째로 빠져도 그
문턱에 영영 닿지 않는다 — 실측에서 둘 다 통과했다. 그래서 비율이 아니라 **결손 사실**을
전한다([[unknown-vs-empty]]).
"""
from unittest.mock import patch

import pytest

import config
from modules import account

HOLD = [{'pdno': '005930', 'hldg_qty': '50', 'pchs_avg_pric': '70000',
         'evlu_amt': '3600000', 'evlu_pfls_amt': '100000'}]
SUM = [{'tot_evlu_amt': '10000000', 'nxdy_excc_amt': '6400000',
        'prvs_rcdl_excc_amt': '6400000', 'dnca_tot_amt': '6400000'}]
DEP = {'deposit': 6400000, 'd2_deposit': 6400000, 'withdraw': 6400000,
       'order_possible': 6400000, 'd2_real': 6400000, 'foreign_deposit': 0}


@pytest.fixture(autouse=True)
def _mode(monkeypatch):
    monkeypatch.setattr(config.session, 'is_toss', False, raising=False)
    monkeypatch.setattr(config.session, 'is_paper', False, raising=False)


def _run(balance=None, deposit=None, overseas=None):
    balance = balance or {'return_value': (HOLD, SUM)}
    deposit = deposit or {'return_value': DEP}
    overseas = overseas or {'return_value': []}
    with patch('modules.account.api.get_domestic_balance', **balance), \
         patch('modules.account.api.get_deposit_balance', **deposit), \
         patch('modules.account.fetch_overseas_balance', **overseas), \
         patch('modules.account.fetch_today_profit_summary',
               return_value={'buy_amt': 0, 'sell_amt': 0, 'total_cost': 0, 'realized_pl': 0}), \
         patch('modules.account.fetch_today_history',
               return_value={'buy_total': 0, 'sell_total': 0}), \
         patch('modules.account.db_manager.db.get_trades', return_value=[]):
        return account.get_asset_status_data("12345678", "01")


def test_온전한_집계는_결손을_남기지_않는다():
    """대조군 — 정상 경로에서 결손이 뜨면 호출부가 늘 대안 계산으로 빠진다."""
    res = _run()
    assert res['degraded'] == [], res['degraded']
    assert res['tot_asset'] == 10_000_000


@pytest.mark.parametrize("kwargs, label", [
    ({"balance": {'side_effect': RuntimeError("KIS 잔고 조회 실패")}}, "국내잔고"),
    ({"balance": {'return_value': (None, None)}}, "국내잔고"),
    ({"deposit": {'side_effect': RuntimeError("예수금 조회 실패")}}, "예수금"),
    ({"deposit": {'return_value': None}}, "예수금"),
    ({"overseas": {'side_effect': RuntimeError("해외 잔고 폭발")}}, "해외잔고"),
    ({"overseas": {'return_value': None}}, "해외잔고"),
])
def test_구간이_실패하면_결손으로_남는다(kwargs, label):
    res = _run(**kwargs)
    assert label in res['degraded'], \
        f"{label} 구간이 실패했는데 반환값이 온전한 척한다: {res['degraded']}"


def test_결손이_기존_문턱에는_걸리지_않는다():
    """이 파일이 존재하는 이유 — 비율 문턱이 이 시스템에서 도달 불가능하다."""
    from modules.auto_trade.common import is_plausible_baseline
    ok = _run()
    bad = _run(balance={'side_effect': RuntimeError("KIS 잔고 조회 실패")})
    drop = bad['tot_asset'] / ok['tot_asset']
    assert 0.5 < drop < 1.0, f"실측 전제가 바뀌었다: {drop:.2f}배"
    assert is_plausible_baseline("TEST-1", bad['tot_asset'],
                                 last_known=ok['tot_asset']) is True, \
        "비율 가드가 이것을 잡는다면 결손 표식이 필요 없다 — 전제를 다시 확인하라"


def test_해외_수량_필드가_둘_다_비어도_해외분만_비운다():
    """`float(A or B)` 는 A 가 빈 문자열이면 B 로 넘어가지만 B 도 비면 그대로 터진다.
    그러면 바깥 except 로 튀어 **해외분 전체**가 총자산에서 사라진다."""
    res = _run(overseas={'return_value': [
        {'ovrs_cblc_qty': '', 'ord_psbl_qty': '', 'pchs_avg_pric': '',
         'frcr_evlu_pfls_amt': ''},
        {'ovrs_cblc_qty': '10', 'ord_psbl_qty': '10', 'pchs_avg_pric': '100',
         'frcr_evlu_pfls_amt': '50'},
    ]})
    assert "해외잔고" not in res['degraded'], \
        f"빈 수량 한 건이 해외 합산을 통째로 날렸다: {res['degraded']}"
    assert res['ovrs_eval_krw'] > 0, "멀쩡한 해외 보유분까지 사라졌다"
