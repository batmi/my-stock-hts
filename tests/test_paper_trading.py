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
from modules import db_manager, paper_broker, trading_cost


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
    """매수→매도 왕복에서 현금·포지션·손익이 일관되게 갱신된다.

    [2026-08-10] 체결가에 슬리피지가 붙는다. 종전에는 관찰모드만 '지정가 그대로 체결'이라
    백테스트보다 구조적으로 유리했고, 두 성과를 직접 비교할 수 없었다.
    """
    buy_fill = trading_cost.apply_slippage(70000, 'buy')
    res = api.place_order("domestic", "buy", "005930", 10, 70000, "00")
    assert res['rt_cd'] == '0' and res['output']['ODNO']
    assert paper.get_cash() == pytest.approx(
        5_000_000 - buy_fill * 10 - trading_cost.buy_fee(buy_fill * 10))
    pos = paper.get_positions()
    assert len(pos) == 1 and pos[0]['qty'] == 10 and pos[0]['avg_price'] == pytest.approx(buy_fill)

    sell_fill = trading_cost.apply_slippage(77000, 'sell')
    res = api.place_order("domestic", "sell", "005930", 10, 77000, "00")
    assert res['rt_cd'] == '0'
    assert paper.get_positions() == []
    fill = paper.get_fills()[-1]
    assert fill['type'] == '매도'
    # 손익 = 총손익 − 왕복 비용(매수 수수료 + 매도 수수료·세)
    expected, _ = trading_cost.net_realized_profit(buy_fill, sell_fill, 10)
    assert fill['profit_amt'] == pytest.approx(expected)


def test_reported_profit_nets_out_both_legs(paper):
    """보고 손익에서 매수 수수료도 빠져야 한다 — 한쪽만 빼면 근소한 손실이 '승'이 된다."""
    api.place_order("domestic", "buy", "005930", 10, 70000, "00")
    api.place_order("domestic", "sell", "005930", 10, 77000, "00")
    fill = paper.get_fills()[-1]

    buy_fill = trading_cost.apply_slippage(70000, 'buy')
    sell_fill = trading_cost.apply_slippage(77000, 'sell')
    gross = (sell_fill - buy_fill) * 10
    sell_only = gross - trading_cost.sell_fee(sell_fill * 10)
    assert fill['profit_amt'] < sell_only, "매수 수수료가 빠지지 않았다"


def test_pyramiding_updates_average_price(paper):
    """추가 매수 시 평단이 수량가중으로 갱신된다(피라미딩 경로)."""
    api.place_order("domestic", "buy", "005930", 10, 70000, "00")
    api.place_order("domestic", "buy", "005930", 5, 76000, "00")
    b1 = trading_cost.apply_slippage(70000, 'buy')
    b2 = trading_cost.apply_slippage(76000, 'buy')
    pos = paper.get_positions()[0]
    assert pos['qty'] == 15
    assert pos['avg_price'] == pytest.approx((b1 * 10 + b2 * 5) / 15)


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


def _count(table):
    row = db_manager.db.execute_query(f"SELECT COUNT(*) FROM {table}", fetch='one')
    return int(row[0]) if row else 0


def _seed_trade_history():
    """매매 기록·포지션 파생 상태를 페이퍼 DB에 심는다."""
    db_manager.db.execute_query(
        "INSERT INTO trades (time, type, code, name, qty, price, odno, account, is_sim) "
        "VALUES ('2026-08-05 09:00:00','매수','005930','삼성전자','10','70000','X1','PAPER-',0)")
    db_manager.db.execute_query(
        "INSERT OR REPLACE INTO trailing_stops (code, highest_price, update_time) "
        "VALUES ('005930', 88000, '2026-08-05')")
    db_manager.db.execute_query(
        "INSERT OR REPLACE INTO half_tp_status (code, update_time) VALUES ('005930','2026-08-05')")


def test_reset_clears_trade_history(paper):
    """초기화하면 매매 기록(trades)과 포지션 파생 상태까지 함께 지워진다.

    남겨 두면 5-4 트레이딩 평가가 지워진 계좌의 과거 청산을 계속 집계하고,
    트레일링 최고가는 같은 종목 재진입 시 그대로 되살아난다.
    """
    _seed_trade_history()
    assert _count("trades") == 1

    paper.reset(3_000_000)

    assert _count("trades") == 0
    assert _count("trailing_stops") == 0
    assert _count("half_tp_status") == 0


def test_reset_never_touches_real_db(paper, monkeypatch):
    """[안전장치] 열려 있는 DB가 가상투자 파일이 아니면 매매 기록에 손대지 않는다.

    trades·trailing_stops는 실계좌 DB에도 같은 이름으로 있다. 경로 확인 없이 지우면
    초기화 한 번이 실계좌 매매 기록을 통째로 날린다.
    """
    _seed_trade_history()
    monkeypatch.setattr(config, 'PAPER_DB_FILE_PATH', "/nonexistent/real.db", raising=False)

    assert paper.reset(3_000_000) is False

    assert _count("trades") == 1              # 실계좌 기록이라 가정 — 보존
    assert _count("trailing_stops") == 1
    assert paper.get_cash() == 3_000_000      # 가상 계좌 자체는 정상 초기화


def test_sellable_quantity_uses_virtual_position(paper, monkeypatch):
    """[회귀 방지] 매도 가능 수량은 가상 보유분으로 답한다 — 실계좌 API를 부르면 안 된다.

    가로채지 않으면 CANO='PAPER'로 실계좌를 조회해 INVALID_CHECK_ACNO가 나고 0을
    돌려준다. 트레이더는 그것을 '팔 수 없는 상태'로 읽어 매도를 중단하므로
    손절·트레일링·점수매도가 전부 죽는다(청산 검증 자체가 성립하지 않는다).
    """
    def _boom(*a, **k):
        raise AssertionError("관찰 모드에서 실계좌 API를 호출했다")
    monkeypatch.setattr(api, 'call_api', _boom)

    api.place_order("domestic", "buy", "005930", 7, 70000, "00")
    assert api.fetch_sellable_quantity("005930") == 7
    assert api.fetch_sellable_quantity("000660") == 0   # 미보유


def test_buyable_quantity_uses_virtual_cash(paper, monkeypatch):
    """[회귀 방지] 매수 가능 수량은 가상 현금으로 답한다.

    신규 매수는 예수금 폴백이 있어 살아남지만 피라미딩 경로에는 폴백이 없어
    0을 받으면 '예수금 부족'으로 영구히 보류된다 — 증액이 한 번도 발동하지 못한다.
    """
    def _boom(*a, **k):
        raise AssertionError("관찰 모드에서 실계좌 API를 호출했다")
    monkeypatch.setattr(api, 'call_api', _boom)

    qty = api.fetch_buyable_quantity("005930", 70000)
    assert qty == int(paper.get_cash() * 0.998 / 70000) > 0
    assert api.fetch_buyable_quantity("005930", 0) == 0   # 가격 0은 계산 불가


def test_paper_footer_shows_virt_account(paper, monkeypatch):
    """가상투자 꼬리말은 'PAPER + 실제 계좌번호'로 어느 계좌 앞 인스턴스인지 밝힌다.

    session.cano 는 안전장치로 'PAPER' 문자열이라 계좌번호로 쓸 수 없다. 표시 전용
    VIRT_ACC_NUM 을 따로 읽으며, 스레드(자동/수동)에 따라 달라지지 않아야 한다 —
    꼬리말은 주문 출처가 아니라 계좌를 가리키는 자리다.
    """
    import context
    from modules import telegram_notify

    monkeypatch.setattr(config, 'TELEGRAM_BOT_TOKEN', "test_token", raising=False)
    monkeypatch.setattr(config, 'TELEGRAM_INSTANCE_NAME', "TEST", raising=False)
    for attr, val in (('cano', 'PAPER'), ('acnt_prdt_cd', ''),
                      ('auto_cano', 'PAPER'), ('auto_acnt_prdt_cd', ''),
                      ('is_simulation', False), ('is_toss', False),
                      ('virt_cano', '43486025'), ('virt_acnt_prdt_cd', '01')):
        monkeypatch.setattr(config.session, attr, val, raising=False)

    _prev = getattr(context.trade_context, 'use_auto_account', False)
    try:
        for auto in (True, False):
            context.trade_context.use_auto_account = auto
            assert telegram_notify._get_telegram_footer() == "[TEST | PAPER 43486025-01]"

        # VIRT_ACC_NUM 미설정이면 번호 없이 라벨만 남는다(종전 표기와 같다).
        monkeypatch.setattr(config.session, 'virt_cano', '', raising=False)
        monkeypatch.setattr(config.session, 'virt_acnt_prdt_cd', '', raising=False)
        assert telegram_notify._get_telegram_footer() == "[TEST | PAPER]"
    finally:
        context.trade_context.use_auto_account = _prev


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
