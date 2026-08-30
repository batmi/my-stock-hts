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
from core import trading_cost


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

    [2026-08-20] 지정가 주문은 주문가 그대로 체결된다 — 주문가가 이미
    adjust_to_tick(현재가 × (1 + SLIPPAGE_RATE))이라 백테스트 체결가와 같은 자리다.
    """
    buy_fill = paper_broker.fill_price(70000, 'buy')
    res = api.place_order("domestic", "buy", "005930", 10, 70000, "00")
    assert res['rt_cd'] == '0' and res['output']['ODNO']
    assert paper.get_cash() == pytest.approx(
        5_000_000 - buy_fill * 10 - trading_cost.buy_fee(buy_fill * 10))
    pos = paper.get_positions()
    assert len(pos) == 1 and pos[0]['qty'] == 10 and pos[0]['avg_price'] == pytest.approx(buy_fill)

    sell_fill = paper_broker.fill_price(77000, 'sell')
    res = api.place_order("domestic", "sell", "005930", 10, 77000, "00")
    assert res['rt_cd'] == '0'
    assert paper.get_positions() == []
    fill = paper.get_fills()[-1]
    assert fill['type'] == '매도'
    # 손익 = 총손익 − 왕복 비용(매수 수수료 + 매도 수수료·세)
    expected, _ = trading_cost.net_realized_profit(buy_fill, sell_fill, 10)
    assert fill['profit_amt'] == pytest.approx(expected)


def test_limit_fill_matches_backtest_execution_price(paper):
    """지정가 체결가가 **백테스트 체결가와 같은 자리**여야 한다 (슬리피지 이중 부과 금지).

    [배경] 주문가는 호출부가 adjust_to_tick(현재가 × (1 ± SLIPPAGE_RATE))로 만든다
     — '체결 확률 확보'용 버퍼이지 비용 모델이 아니다. 2026-08-10~08-20 사이의
     가상 브로커는 그 위에 슬리피지를 한 번 더 얹어 편도 0.4%를 물렸고, 백테스트는
     편도 0.2%였다. 관찰모드가 전략이 아니라 비용 모델 때문에 뒤처지면 mode 1로
     실매매와 백테스트를 견주는 일 자체가 불가능해진다.
    """
    from core import utils
    slip = config.SLIPPAGE_RATE
    assert slip > 0, "슬리피지가 0이면 이 테스트가 아무것도 지키지 못한다"

    for current, action in ((70000, 'buy'), (70000, 'sell')):
        raw = current * (1 + slip) if action == 'buy' else current * (1 - slip)
        order_price = int(utils.adjust_to_tick(raw, is_overseas=False))   # 실매매 주문가
        backtest_px = int(utils.adjust_to_tick(raw, is_overseas=False))   # 백테스트 체결가
        assert paper_broker.fill_price(order_price, action) == pytest.approx(backtest_px)


def test_market_fill_applies_slippage_and_tick(paper):
    """시장가만 슬리피지를 얹고, 그 결과는 실재하는 호가여야 한다."""
    from core import utils
    px = paper_broker.fill_price(70000, 'buy', market=True)
    assert px == pytest.approx(
        utils.adjust_to_tick(trading_cost.apply_slippage(70000, 'buy'), is_overseas=False))
    tick = utils.get_tick_size(px, False)
    assert px % tick == 0, f"호가 단위에 맞지 않는 체결가: {px} (호가 {tick})"


def test_limit_order_is_not_slipped_twice(paper):
    """주문 경로 전체(api.place_order → 원장)에서도 이중 부과가 없다."""
    api.place_order("domestic", "buy", "005930", 1, 70140, "00")
    assert paper.get_fills()[-1]['price'] == pytest.approx(70140)


def test_reported_profit_nets_out_both_legs(paper):
    """보고 손익에서 매수 수수료도 빠져야 한다 — 한쪽만 빼면 근소한 손실이 '승'이 된다."""
    api.place_order("domestic", "buy", "005930", 10, 70000, "00")
    api.place_order("domestic", "sell", "005930", 10, 77000, "00")
    fill = paper.get_fills()[-1]

    buy_fill = paper_broker.fill_price(70000, 'buy')
    sell_fill = paper_broker.fill_price(77000, 'sell')
    gross = (sell_fill - buy_fill) * 10
    sell_only = gross - trading_cost.sell_fee(sell_fill * 10)
    assert fill['profit_amt'] < sell_only, "매수 수수료가 빠지지 않았다"


def test_pyramiding_updates_average_price(paper):
    """추가 매수 시 평단이 수량가중으로 갱신된다(피라미딩 경로)."""
    api.place_order("domestic", "buy", "005930", 10, 70000, "00")
    api.place_order("domestic", "buy", "005930", 5, 76000, "00")
    b1 = paper_broker.fill_price(70000, 'buy')
    b2 = paper_broker.fill_price(76000, 'buy')
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
    mode 1에서 호출부가 봉투를 순회하며 키 문자열을 원소로 받아 매 주기 터졌다
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


def test_deposit_shifts_drawdown_baseline(paper):
    """입출금은 일일 자산 이력을 함께 평행이동한다 — 가짜 드로다운을 남기지 않는다.

    [사고 2026-08-23] adjust_seed 가 현금·시드만 옮기고 기준선을 두던 시절, 가상계좌에
     1,000만원이 들어왔다 나간 흔적 한 줄(20,028,670원)이 daily_asset_history 에 남았다.
     이 값이 HWM 으로 잡혀 드로다운 49.5% → 리스크 스케일 x0.8 → 히트 캡 10%가 8%로
     묶였고, 휩소율 x0.85 가 겹친 실효 캡 6.8%를 실제 오픈 리스크 6.85%가 넘겨 신규
     매수·피라미딩이 통째로 막혔다. 룩백이 90일이라 한 줄이 석 달을 간다.

    [왜 삭제가 아니라 이동인가] 지우면 드로다운 기준 자체가 사라져 한도가 조용히 열린다.
     이동은 곡선의 모양을 보존하고, 반대 방향 입출금에 그대로 되돌아온다(아래에서 확인).
    """
    key = paper._account_key()
    db = db_manager.db
    db.save_daily_asset("2026-08-21", key, 5_000_000)
    db.save_daily_asset("2026-08-22", key, 5_100_000)

    ok, _ = paper.adjust_seed(2_000_000)
    assert ok
    # 과거 고점이 '입금 후 자본' 기준으로 올라온다 → 현재 700만원 대비 드로다운 없음
    assert db.get_max_daily_asset("2026-08-01", key) == pytest.approx(7_100_000)

    # 같은 돈을 도로 빼면 원래 곡선으로 돌아온다 — 가짜 고점이 남지 않는 것이 요점이다.
    ok, _ = paper.adjust_seed(-2_000_000)
    assert ok
    assert db.get_max_daily_asset("2026-08-01", key) == pytest.approx(5_100_000)


def test_deposit_shifts_the_daily_loss_baseline(paper, tmp_path, monkeypatch):
    """입출금은 **일일 손실 한도의 분모**(오늘 시작 자산)도 함께 옮긴다.

    이걸 빼면 500만으로 시작한 날 200만을 넣는 순간 자산이 +40%로 읽혀 한도가 헐거워지고,
    반대로 출금하면 매매와 무관하게 방어 모드가 걸린다. 종전에는 자산 이력(HWM) 이동만
    테스트가 있었고, 세 기준선 중 나머지 둘은 검증되지 않았다(2026-08-30 커버리지 실측).
    """
    from core import jsonio
    from modules.auto_trade import common as at_common

    state = tmp_path / "daily_asset_state.json"
    key = paper._account_key()
    jsonio.save_json(str(state), {"accounts": {key: 5_000_000}})
    monkeypatch.setattr(at_common, 'DAILY_STATE_FILE', str(state), raising=False)

    assert paper.adjust_seed(2_000_000)[0]
    assert jsonio.load_json(str(state))["accounts"][key] == 7_000_000

    assert paper.adjust_seed(-2_000_000)[0]
    assert jsonio.load_json(str(state))["accounts"][key] == 5_000_000, \
        "반대 방향 입출금에 기준선이 되돌아오지 않았다"


def test_deposit_shifts_the_running_traders_baseline(paper, tmp_path, monkeypatch):
    """실행 중인 트레이더의 메모리 기준선·HWM 캐시도 재기동 없이 따라와야 한다.

    HWM 캐시는 하루 1회만 갱신되므로, 여기서 옮기지 않으면 입출금한 그날 하루는
    옛 고점으로 드로다운이 계산된다 — 사고 2026-08-23과 같은 계열의 오차다.
    """
    from modules.auto_trade import AutoTrader
    from modules.auto_trade import common as at_common
    monkeypatch.setattr(at_common, 'DAILY_STATE_FILE', str(tmp_path / "s.json"), raising=False)

    AutoTrader._instance = None
    t = AutoTrader()
    t.initial_asset = 5_000_000
    t.baseline_principal = 5_000_000
    t._hwm_cache = 5_100_000

    assert paper.adjust_seed(1_000_000)[0]
    assert t.initial_asset == 6_000_000
    assert t.baseline_principal == 6_000_000
    assert t._hwm_cache == pytest.approx(6_100_000)


def test_an_unmeasured_baseline_is_left_alone(paper, tmp_path, monkeypatch):
    """0(미설정)은 건드리지 않는다 — 다음 측정 때 새 자산으로 잡히는 것이 맞다."""
    from modules.auto_trade import AutoTrader
    from modules.auto_trade import common as at_common
    monkeypatch.setattr(at_common, 'DAILY_STATE_FILE', str(tmp_path / "s.json"), raising=False)

    AutoTrader._instance = None
    t = AutoTrader()
    t.initial_asset = 0
    t.baseline_principal = 0
    t._hwm_cache = 0.0

    assert paper.adjust_seed(1_000_000)[0]
    assert (t.initial_asset, t.baseline_principal, t._hwm_cache) == (0, 0, 0.0)


def test_equity_snapshot_freezes_seed_of_that_day(paper):
    """스냅샷은 **그 시점 시드**를 함께 굳힌다 — 누적 수익률의 분모가 흔들리면 안 된다.

    시드는 입출금으로 변한다. 나중에 현재 시드로 과거 행을 나누면 입출금 전 구간의
    수익률이 통째로 틀어진다 — daily_asset_history 에서 겪은 것과 같은 계열의 함정이다
    (그쪽은 HWM 이 오염돼 가짜 드로다운이 됐다).
    """
    api.place_order("domestic", "buy", "005930", 10, 70000, "00")
    paper.snapshot_equity()
    assert paper.get_equity_curve()[-1]["seed"] == pytest.approx(5_000_000)

    # 입금해도 **이미 찍힌 행의 분모는 그대로**여야 한다.
    paper.adjust_seed(2_000_000)
    assert paper.get_equity_curve()[-1]["seed"] == pytest.approx(5_000_000)


def test_daily_ledger_tracks_holdings_realized_and_events(paper):
    """날짜별 체결 요약 — 보유 수·실현손익·매매 이벤트를 한 원장에서 되짚는다.

    자산 곡선이 "이 날 왜 움직였나"에 답하려면 셋이 함께 있어야 한다. 실측 2026-08-26:
    변동 -0.62%였지만 실현손익 -102,356원 · 평가는 +40,220 상승이었다 — 손절이 나간
    날과 시장이 나빴던 날은 완전히 다른 사건인데, 실현손익 없이는 구분되지 않는다.
    """
    api.place_order("domestic", "buy", "005930", 10, 70000, "00")
    api.place_order("domestic", "buy", "000660", 5, 180000, "00")
    day = list(paper.daily_ledger().values())[-1]
    assert day["holdings"] == 2
    assert day["realized"] == 0.0            # 매수만으로는 실현손익이 없다
    assert day["events"] == ["+삼성전자", "+SK하이닉스"] or len(day["events"]) == 2

    # 전량 매도하면 보유에서 빠지고 실현손익이 잡힌다.
    api.place_order("domestic", "sell", "005930", 10, 77000, "00")
    day = list(paper.daily_ledger().values())[-1]
    assert day["holdings"] == 1
    assert day["realized"] > 0               # 70,000 → 77,000 이익 청산
    assert day["events"][-1].startswith("-")


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
    from core import context
    from modules import telegram_notify

    monkeypatch.setattr(config, 'TELEGRAM_BOT_TOKEN', "test_token", raising=False)
    monkeypatch.setattr(config, 'TELEGRAM_INSTANCE_NAME', "TEST", raising=False)
    for attr, val in (('cano', 'PAPER'), ('acnt_prdt_cd', ''),
                      ('auto_cano', 'PAPER'), ('auto_acnt_prdt_cd', ''),
                      ('is_toss', False),
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


def test_journal_sync_follows_toggle_in_paper_mode(monkeypatch):
    """가상투자 기동이 매매일지 스위치를 임의로 내리지 않는다 (설정이 정한다)."""
    tmpdir = tempfile.mkdtemp()
    original_path = db_manager.db.db_path
    monkeypatch.setattr(config, 'PAPER_DB_FILE_PATH', os.path.join(tmpdir, "p.db"), raising=False)
    monkeypatch.setattr(config.settings, 'JOURNAL_SYNC_USE', True, raising=False)
    try:
        config.session._activate_paper_mode()
        assert config.settings.JOURNAL_SYNC_USE is True
    finally:
        db_manager.db.close_all_connections()
        db_manager.db.switch_path(original_path)
