"""관찰 모드(mode 1)에서 현재가 조회가 실패하면 손절이 조용히 멈추는가.

paper_broker._current_price 는 조회 실패 시 **평단**으로 폴백한다. 평가금이 0으로
무너져 자산곡선이 망가지는 것을 막으려는 의도였지만, 이 값이 그대로 매매 판정에 쓰인다.

평단으로 폴백하면 prpr == pchs_avg_pric 이 되어 수익률이 정확히 0.00% 로 계산된다.
_sell_worker 의 방어선은 `current_price <= 0` 하나뿐이라 이 값은 그대로 통과하고,
실제로 -20% 하락한 포지션이 '본전에서 쉬고 있는 정상 포지션'으로 보인다. 손절도
트레일링도 발동하지 않으며 로그에도 이상이 남지 않는다.

토스 401·레이트리밋 같은 일시적 조회 실패는 실제로 관측된 적이 있고(2026-08-04 로그),
mode 1 는 지금 장기 검증에 쓰이는 모드다. 이 구간이 길어지면 청산됐어야 할 포지션이
계속 살아남아 검증 결과(자산곡선·MDD·승률) 자체가 왜곡된다.
"""
import os
import tempfile
from unittest.mock import patch

import pytest

import api
import config
from modules import db_manager, paper_broker
from core import trading_cost

CODE = "005930"
BUY_PRICE = 70000
CRASH_PRICE = 56000  # 지정가 기준 -20%


def _crash_rate():
    """체결가 기준 하락률. 지정가 주문은 주문가 그대로 체결된다(2026-08-20 — 주문가에
    이미 슬리피지 버퍼가 들어 있어 가상 브로커가 또 얹지 않는다). 이 파일의 관심사는
    '조회 실패 시 하락분이 장부에서 사라지는가'이지 특정 소수점이 아니므로,
    기대값을 실제 체결가에서 도출한다."""
    fill = paper_broker.fill_price(BUY_PRICE, 'buy')
    return (CRASH_PRICE - fill) / fill * 100


@pytest.fixture
def paper(monkeypatch):
    """임시 DB에 가상 계좌를 열고 관찰 모드로 전환한다(_current_price 는 실물 사용)."""
    tmpdir = tempfile.mkdtemp()
    path = os.path.join(tmpdir, "paper_stale_test.db")
    original_path = db_manager.db.db_path
    monkeypatch.setattr(config, 'PAPER_DB_FILE_PATH', path, raising=False)
    monkeypatch.setattr(config, 'PAPER_SEED_CAPITAL', 5_000_000, raising=False)
    monkeypatch.setattr(config.session, 'is_paper', True, raising=False)
    db_manager.db.switch_path(path)
    paper_broker.init_tables()
    paper_broker.reset_price_cache()
    yield paper_broker
    paper_broker.reset_price_cache()
    db_manager.db.close_all_connections()
    db_manager.db.switch_path(original_path)


def _buy(price=BUY_PRICE, qty=10):
    with patch('api.get_current_price', return_value=price):
        res = api.place_order("domestic", "buy", CODE, qty, price, "00")
    assert res['rt_cd'] == '0'


def _balance_row(price_side_effect):
    with patch('api.get_current_price', side_effect=price_side_effect):
        output1, _ = paper_broker.get_domestic_balance()
    return output1[0]


# ─────────────────────────────── 정상 경로 ───────────────────────────────

def test_live_price_is_reflected(paper):
    """대조군 — 조회가 되면 실제 현재가와 수익률이 그대로 반영된다."""
    _buy()
    row = _balance_row(lambda *a, **k: CRASH_PRICE)
    assert int(row['prpr']) == CRASH_PRICE
    assert float(row['evlu_pfls_rt']) == pytest.approx(_crash_rate(), abs=0.01)
    assert not row.get('_price_stale')


# ─────────────────────────── 조회 실패 = 판정 불가 ───────────────────────────

def test_fetch_failure_is_marked_stale(paper):
    """조회 실패로 폴백한 값은 '판정 불가'로 표시돼야 한다.

    표시가 없으면 평단 폴백이 수익률 0.00%로 위장돼 손절이 영구히 유예된다.
    """
    _buy()
    row = _balance_row(Exception("Toss 401"))
    assert row.get('_price_stale') is True, (
        "현재가 조회에 실패했는데 정상 시세와 구분되지 않는다 — "
        "평단 폴백이 수익률 0.00%가 되어 손절이 발동하지 않는다")


def test_fetch_returning_zero_is_marked_stale(paper):
    """예외가 아니라 0/None 을 돌려주는 실패도 같게 취급한다."""
    _buy()
    assert _balance_row(lambda *a, **k: 0).get('_price_stale') is True
    assert _balance_row(lambda *a, **k: None).get('_price_stale') is True


def test_stale_row_does_not_look_like_breakeven(paper):
    """폴백 값이 '본전(0.00%)'으로 보이면 안 된다 — 손절 판정이 무력화된다."""
    _buy()
    row = _balance_row(Exception("Toss 401"))
    if not row.get('_price_stale'):
        pytest.fail("stale 표시가 없어 0.00% 위장을 막을 수단이 없다")


# ─────────────────────── 마지막 정상가로 폴백 (자산곡선 보존) ───────────────────────

def test_falls_back_to_last_known_price_not_cost(paper):
    """폴백은 평단이 아니라 **마지막 정상 시세**여야 한다.

    평단으로 되돌리면 하락분이 장부에서 사라져 자산곡선·MDD가 실제보다 좋아 보인다.
    관찰 모드의 존재 이유가 성과 계측이므로 이 왜곡은 그대로 결론을 바꾼다.
    """
    _buy()
    row = _balance_row(lambda *a, **k: CRASH_PRICE)   # 마지막 정상가 = 56000
    assert int(row['prpr']) == CRASH_PRICE

    row = _balance_row(Exception("Toss 401"))
    assert int(row['prpr']) == CRASH_PRICE, (
        f"조회 실패 시 평단({BUY_PRICE})으로 되돌아갔다 — 하락분이 자산곡선에서 사라진다")
    assert float(row['evlu_pfls_rt']) == pytest.approx(_crash_rate(), abs=0.01)


def test_cost_fallback_only_when_no_known_price(paper):
    """마지막 정상가가 없으면(구동 직후 조회 실패) 평단 폴백을 유지한다.

    평가금이 0으로 무너져 자산 스냅샷이 망가지는 것을 막는 원래 의도는 살린다.
    """
    _buy()
    row = _balance_row(Exception("Toss 401"))
    # 폴백값은 '평단'이다 — 지정가 주문은 주문가가 곧 체결가다(2026-08-20).
    assert int(row['prpr']) == int(paper_broker.fill_price(BUY_PRICE, 'buy'))
    assert row.get('_price_stale') is True


def test_equity_snapshot_survives_fetch_failure(paper):
    """조회가 실패해도 자산 스냅샷은 기록돼야 한다(0원으로 무너지지 않는다)."""
    _buy()
    with patch('api.get_current_price', side_effect=Exception("Toss 401")):
        paper_broker.snapshot_equity()
    curve = paper_broker.get_equity_curve()
    assert curve and curve[-1]['total'] > 0


# ─────────────────── 트레이더가 stale 표시를 실제로 존중하는가 ───────────────────
#  브로커가 표시만 하고 트레이더가 무시하면 아무것도 달라지지 않는다.

import pandas as pd
from modules.auto_trade import AutoTrader


@pytest.fixture
def trader():
    t = AutoTrader()
    t.is_running = True
    t.market_index_status = {}
    t.market_status_notified = {}
    return t


def _stale_holding(stale):
    return [{'pdno': CODE, 'prdt_name': '삼성전자', 'ord_psbl_qty': '10',
             'evlu_pfls_rt': '-20.0', 'prpr': str(CRASH_PRICE),
             'pchs_avg_pric': str(BUY_PRICE), 'evlu_pfls_amt': '-140000',
             '_price_stale': stale}]


def _run_sell(trader, holdings, analyze_result=None):
    """(analyze_sell 호출 여부, 주문 전송 mock) — 판정이 돌았는지 본다."""
    df = pd.DataFrame({'close': [CRASH_PRICE], 'high': [CRASH_PRICE], 'low': [CRASH_PRICE],
                       'open': [CRASH_PRICE], 'volume': [1000]})
    with patch('modules.auto_trade.api.send_telegram_message'), \
         patch('modules.auto_trade.load_restricted_stocks', return_value={}), \
         patch('modules.auto_trade.api.fetch_sellable_quantity', return_value=10), \
         patch('modules.auto_trade.api.get_chart_data', return_value=df), \
         patch('modules.auto_trade.DefaultStrategy.analyze_sell') as mock_analyze, \
         patch.object(trader.order_manager, 'is_pending', return_value=False), \
         patch.object(trader.order_manager, 'send_order', return_value='1') as mock_send:
        mock_analyze.return_value = analyze_result or {
            'action': 'sell', 'reason': '손절', 'score': 1.0, 'state': '매도',
            'ind': {'rsi': 30, 'adx': 20, 'cci': -100}}
        trader._check_sell_conditions(holdings, is_market_open=True)
    return mock_analyze, mock_send


def test_trader_skips_judgment_on_stale_price(trader):
    """stale 표시가 붙은 종목은 매도 판정 자체를 하지 않는다."""
    mock_analyze, mock_send = _run_sell(trader, _stale_holding(True))
    assert not mock_analyze.called, (
        "시세 조회에 실패한 값으로 손절을 판정했다 — 옛 가격으로 '아직 괜찮다'는 답이 나온다")
    assert not mock_send.called, "판정 불가인데 주문이 전송됐다"


def test_trader_alerts_on_stale_price(trader):
    """판정을 보류하는 대신, 지켜주지 못한다는 사실을 운영자에게 알린다."""
    with patch.object(trader, '_alert_unmanaged_stop') as mock_alert:
        _run_sell(trader, _stale_holding(True))
    assert mock_alert.called, "판정을 건너뛰면서 경보도 없으면 포지션이 조용히 방치된다"
    assert "조회 실패" in str(mock_alert.call_args)


def test_trader_judges_normally_when_not_stale(trader):
    """대조군 — stale이 아니면 같은 데이터로 정상 판정·주문이 나가야 한다."""
    mock_analyze, mock_send = _run_sell(trader, _stale_holding(False))
    assert mock_analyze.called
    assert mock_send.called
