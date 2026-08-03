"""관찰(페이퍼 트레이딩) 모드 검증.

핵심 불변식 두 가지를 지킨다.
 1) 관찰 모드에서는 **실주문이 절대 나가지 않는다** (api 최상단 하드 가드).
 2) 관찰 모드가 아닐 때는 기존 동작이 **하나도 바뀌지 않는다** (가로채기 누수 없음).
"""
import os
import tempfile
from unittest.mock import patch

import pytest

import api
import config
from modules import db_manager, paper_broker


@pytest.fixture
def paper(monkeypatch):
    """임시 DB에 가상 계좌를 열고 관찰 모드로 전환한다."""
    tmpdir = tempfile.mkdtemp()
    path = os.path.join(tmpdir, "paper_test.db")
    original_path = db_manager.db.db_path
    monkeypatch.setattr(config, 'PAPER_DB_FILE_PATH', path, raising=False)
    monkeypatch.setattr(config, 'PAPER_SEED_CAPITAL', 5_000_000, raising=False)
    monkeypatch.setattr(config.session, 'is_paper', True, raising=False)
    db_manager.db.switch_path(path)
    paper_broker.init_tables()
    # 현재가는 네트워크 없이 고정
    monkeypatch.setattr(paper_broker, '_current_price',
                        lambda code, fallback=0.0: {'005930': 70000, '000660': 180000}.get(code, fallback or 0))
    yield paper_broker
    db_manager.db.close_all_connections()
    db_manager.db.switch_path(original_path)


def test_seed_and_initial_state(paper):
    """개설 시 시드 전액이 현금이고 포지션은 없다."""
    assert paper.get_cash() == 5_000_000
    assert paper.get_seed() == 5_000_000
    assert paper.get_positions() == []


def test_buy_sell_roundtrip_updates_cash_and_positions(paper):
    """매수→매도 왕복에서 현금·포지션·손익이 일관되게 갱신된다."""
    res = api.place_order("domestic", "buy", "005930", 10, 70000, "00")
    assert res['rt_cd'] == '0' and res['output']['ODNO']
    assert paper.get_cash() == pytest.approx(5_000_000 - 700_000 * (1 + paper.BUY_FEE_RATE))
    pos = paper.get_positions()
    assert len(pos) == 1 and pos[0]['qty'] == 10 and pos[0]['avg_price'] == 70000

    res = api.place_order("domestic", "sell", "005930", 10, 77000, "00")
    assert res['rt_cd'] == '0'
    assert paper.get_positions() == []
    fill = paper.get_fills()[-1]
    assert fill['type'] == '매도'
    # 손익 = (77000-70000)*10 - 매도수수료
    assert fill['profit_amt'] == pytest.approx(70_000 - 770_000 * paper.SELL_FEE_RATE)


def test_pyramiding_updates_average_price(paper):
    """추가 매수 시 평단이 수량가중으로 갱신된다(피라미딩 경로)."""
    api.place_order("domestic", "buy", "005930", 10, 70000, "00")
    api.place_order("domestic", "buy", "005930", 5, 76000, "00")
    pos = paper.get_positions()[0]
    assert pos['qty'] == 15
    assert pos['avg_price'] == pytest.approx((70000 * 10 + 76000 * 5) / 15)


def test_rejects_insufficient_cash_and_qty(paper):
    """예수금·보유수량을 넘어서는 주문은 거부된다(가상 계좌가 음수로 가지 않는다)."""
    res = api.place_order("domestic", "buy", "005930", 10_000, 70000, "00")
    assert res['rt_cd'] == '1' and '예수금 부족' in res['msg1']
    assert paper.get_cash() == 5_000_000

    api.place_order("domestic", "buy", "005930", 1, 70000, "00")
    res = api.place_order("domestic", "sell", "005930", 5, 70000, "00")
    assert res['rt_cd'] == '1' and '보유수량 부족' in res['msg1']
    assert paper.get_positions()[0]['qty'] == 1


def test_balance_shape_matches_kis(paper):
    """잔고 응답이 KIS 스키마와 같아야 기존 25개 호출부가 수정 없이 동작한다."""
    api.place_order("domestic", "buy", "005930", 10, 70000, "00")
    holdings, summary = api.get_domestic_balance()
    h = holdings[0]
    for key in ('pdno', 'prdt_name', 'hldg_qty', 'ord_psbl_qty', 'pchs_avg_pric',
                'pchs_amt', 'prpr', 'evlu_amt', 'evlu_pfls_amt', 'evlu_pfls_rt'):
        assert key in h
    assert int(h['hldg_qty']) == 10
    s = summary[0]
    for key in ('dnca_tot_amt', 'prvs_rcdl_excc_amt', 'scts_evlu_amt',
                'tot_evlu_amt', 'pchs_amt_smtl', 'evlu_pfls_smtl_amt'):
        assert key in s
    assert int(s['scts_evlu_amt']) == 700_000
    dep = api.get_deposit_balance()
    assert dep['order_possible'] == int(paper.get_cash())


def test_no_real_order_can_escape(paper):
    """[하드 가드] 관찰 모드에서는 어떤 경로로도 실주문 API가 호출되지 않는다."""
    with patch('api.call_api') as mock_call, patch('api._toss_place_order') as mock_toss:
        api.place_order("domestic", "buy", "005930", 1, 70000, "00")
        api.place_order("domestic", "sell", "005930", 1, 70000, "00")
        api.revise_cancel_order("domestic", "cancel", "1", "005930", 1, 0, "02", "00")
        api.get_domestic_balance()
        api.get_deposit_balance()
        api.get_domestic_open_orders()
        mock_call.assert_not_called()
        mock_toss.assert_not_called()


def test_overseas_order_blocked(paper):
    """해외 주문은 관찰 모드 범위 밖이므로 거부한다(실주문으로 새지 않게)."""
    res = api.place_order("overseas", "buy", "AAPL", 1, 200, "00", exchange_code="NASD")
    assert res['rt_cd'] == '1' and '해외' in res['msg1']


def test_unfilled_always_empty(paper):
    """즉시 체결 모델이므로 미체결은 항상 비어 있다.

    [2026-08-04] 이 테스트는 원래 응답 봉투 dict({'rt_cd','output'})를 단언해,
    '주문 dict의 리스트'라는 실제 계약과 어긋난 형태를 고정하고 있었다. 그 탓에
    mode 4에서 호출부가 봉투를 순회하며 키 문자열을 원소로 받아 매 주기 터졌다
    ("미체결 관리 중 오류: 'str' object has no attribute 'get'").
    계약대로 리스트를 단언한다. 상세는 tests/test_open_orders_contract.py 참조.
    """
    res = api.get_domestic_open_orders()
    assert isinstance(res, list) and res == []


def test_performance_metrics(paper):
    """성과 지표(PF·승률·연속손실)가 체결 내역과 일치한다."""
    api.place_order("domestic", "buy", "005930", 10, 70000, "00")
    api.place_order("domestic", "sell", "005930", 10, 77000, "00")   # 이익
    api.place_order("domestic", "buy", "000660", 5, 180000, "00")
    api.place_order("domestic", "sell", "000660", 5, 170000, "00")   # 손실
    perf = paper.get_performance()
    assert perf['sell_count'] == 2
    assert perf['win'] == 1 and perf['loss'] == 1
    assert perf['win_rate'] == pytest.approx(50.0)
    assert perf['pf'] > 0
    assert perf['max_loss_streak'] == 1


def test_deposit_moves_seed_and_cash_together(paper):
    """입금은 시드(투입원금)와 현금을 함께 늘린다 — 수익률 분모가 어긋나면 안 된다."""
    api.place_order("domestic", "buy", "005930", 10, 70000, "00")
    before_return = paper.get_performance()['total_return']

    ok, _ = paper.adjust_seed(2_000_000)
    assert ok
    assert paper.get_seed() == 7_000_000
    # 입금 직후 수익률은 변하지 않아야 한다(현금만 늘려 수익률이 좋아 보이는 착시 방지)
    assert paper.get_performance()['total_return'] == pytest.approx(before_return, abs=0.5)
    # 포지션은 유지
    assert len(paper.get_positions()) == 1


def test_withdraw_limited_by_cash(paper):
    """출금은 가상 현금 범위 안에서만 가능하다(보유 주식은 인출 대상 아님)."""
    api.place_order("domestic", "buy", "005930", 10, 70000, "00")
    cash = paper.get_cash()
    ok, msg = paper.adjust_seed(-int(cash) - 1)
    assert not ok and "초과" in msg
    assert paper.get_cash() == cash

    ok, _ = paper.adjust_seed(-1_000_000)
    assert ok
    assert paper.get_cash() == pytest.approx(cash - 1_000_000)
    assert paper.get_seed() == 4_000_000


def test_reset_clears_everything(paper):
    """초기화하면 포지션·체결·자산곡선이 지워지고 시드가 복원된다."""
    api.place_order("domestic", "buy", "005930", 10, 70000, "00")
    paper.snapshot_equity()
    paper.reset(3_000_000)
    assert paper.get_cash() == 3_000_000
    assert paper.get_seed() == 3_000_000
    assert paper.get_positions() == []
    assert paper.get_fills() == []
    assert paper.get_equity_curve() == []


def test_equity_snapshot_is_one_row_per_day(paper):
    """같은 날 여러 번 스냅샷해도 하루 1행만 남는다(주기마다 호출되므로)."""
    api.place_order("domestic", "buy", "005930", 10, 70000, "00")
    paper.snapshot_equity()
    paper.snapshot_equity()
    curve = paper.get_equity_curve()
    assert len(curve) == 1
    assert curve[0]['total'] == pytest.approx(paper.get_cash() + 700_000)


def test_inactive_mode_does_not_intercept(monkeypatch):
    """[회귀 방지] 관찰 모드가 아니면 가로채기가 전혀 개입하지 않는다."""
    monkeypatch.setattr(config.session, 'is_paper', False, raising=False)
    monkeypatch.setattr(config.session, 'is_toss', False, raising=False)
    assert api._paper_active() is False
    with patch('api.call_api', return_value={'rt_cd': '0', 'output': {'ODNO': 'REAL1'}}) as mock_call:
        res = api.place_order("domestic", "buy", "005930", 1, 70000, "00")
        mock_call.assert_called_once()
        assert res['output']['ODNO'] == 'REAL1'


def test_journal_sync_disabled_in_paper_mode(monkeypatch):
    """가상 체결이 매매일지 웹 연동으로 새어나가지 않는다."""
    tmpdir = tempfile.mkdtemp()
    original_path = db_manager.db.db_path
    monkeypatch.setattr(config, 'PAPER_DB_FILE_PATH', os.path.join(tmpdir, "p.db"), raising=False)
    monkeypatch.setattr(config.settings, 'JOURNAL_SYNC_USE', True, raising=False)
    try:
        config.session._activate_paper_mode()
        assert config.settings.JOURNAL_SYNC_USE is False
    finally:
        db_manager.db.close_all_connections()
        db_manager.db.switch_path(original_path)
