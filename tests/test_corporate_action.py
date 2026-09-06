"""액면분할·무상증자가 트레일링 스탑을 오발동시키지 않는가.

[왜 실계좌에서만 터지는가] trailing_stops.highest_price 는 원시 가격이고, 갱신은
'더 높을 때만'인 단조 증가다(db_manager.update_highest_price). 5:1 분할이 나면 증권사는
매입평균단가를 1/5로 조정하는데 우리 고점만 분할 전 값으로 남는다. 그러면
compute_trailing_stop 이 고점 대비 -80%를 보고 즉시 청산을 때린다.

백테스트 데이터(yfinance·pykrx)는 수정주가라 이 경로가 아예 존재하지 않는다. 즉
**백테스트를 아무리 돌려도 이 사고는 재현되지 않는다** — 코드로만 막을 수 있다.

[오탐이 더 위험한 방향] 평단이 바뀌었다는 것만으로 보정하면, HTS 에서 비싸게 추가
매수한 경우(평단↑)에 고점이 위로 조정되어 **없던 청산이 생긴다**. 그래서 판정은
'매입금액(= 수량 × 평단)이 보존되는가'를 필수 조건으로 둔다. 이 파일의 절반은 그
오탐을 막는 테스트다.
"""
from unittest.mock import patch

import pytest

from modules import db_manager
from modules.auto_trade import AutoTrader
from modules.auto_trade.engine import compute_trailing_stop, detect_corporate_action

CODE, NAME = "005930", "삼성전자"


# ===========================================================================
# 1. 판정 순수 함수
# ===========================================================================
def test_no_reference_yet_does_nothing():
    """기준값이 없으면(최초 관측·컬럼 이관 직후) 이번 주기는 기록만 한다."""
    assert detect_corporate_action(0.0, 0.0, 100_000, 1_000_000) == (1.0, "")


def test_unchanged_position_does_nothing():
    assert detect_corporate_action(100_000, 1_000_000, 100_000, 1_000_000) == (1.0, "")


def test_five_to_one_split_is_detected():
    """5:1 분할 — 수량 10주→50주, 평단 100,000→20,000, 매입금액 그대로."""
    ratio, reason = detect_corporate_action(100_000, 1_000_000, 20_000, 1_000_000)
    assert ratio == pytest.approx(0.2)
    assert "분할" in reason


def test_bonus_issue_is_detected():
    """무상증자 100% — 권리락으로 평단이 절반이 된다."""
    ratio, _ = detect_corporate_action(100_000, 1_000_000, 50_000, 1_000_000)
    assert ratio == pytest.approx(0.5)


def test_reverse_split_is_detected():
    """액면병합 1:5 — 평단이 5배가 된다. 방향만 반대일 뿐 같은 문제다."""
    ratio, reason = detect_corporate_action(20_000, 1_000_000, 100_000, 1_000_000)
    assert ratio == pytest.approx(5.0)
    assert "병합" in reason


def test_odd_lot_cash_settlement_still_detected():
    """분할 단주는 현금 정산되어 매입금액이 아주 조금 준다. 허용 오차 안이면 잡아야 한다."""
    ratio, _ = detect_corporate_action(100_000, 1_000_000, 20_000, 995_000)   # -0.5%
    assert ratio == pytest.approx(0.2)


# --- 오탐 방지 (이쪽이 더 위험하다) ---------------------------------------

def test_pyramiding_buy_is_not_a_split():
    """피라미딩 증액 — 평단도 매입금액도 오른다. 보정하면 안 된다."""
    assert detect_corporate_action(100_000, 1_000_000, 110_000, 1_650_000)[0] == 1.0


def test_manual_high_priced_buy_is_not_a_split():
    """HTS 수동 추가 매수(더 비싸게) — 평단만 보고 판정하면 고점이 위로 조정돼
    '없던 청산'이 생긴다. 매입금액이 늘었으므로 매수로 판정해야 한다."""
    ratio, _ = detect_corporate_action(100_000, 1_000_000, 120_000, 2_400_000)
    assert ratio == 1.0, "수동 매수가 분할로 오인되면 고점이 올라가 즉시 청산된다"


def test_partial_sell_is_not_a_split():
    """부분 매도 — 평단은 그대로고 매입금액만 준다."""
    assert detect_corporate_action(100_000, 1_000_000, 100_000, 400_000)[0] == 1.0


# ===========================================================================
# 2. 실제로 강제 청산을 막는가 (이 수정의 존재 이유)
# ===========================================================================
def _ts(highest, buy, current):
    """ATR 동적 콜백을 배제하고 기본 콜백만으로 판정한다(재현성 확보)."""
    return compute_trailing_stop(highest, buy, current, ind=None,
                                 ts_activation=10.0, ts_callback=5.0, use_atr_stop=False)


def test_split_without_fix_forces_liquidation():
    """[회귀 근거] 보정이 없으면 분할 다음 주기에 반드시 청산된다."""
    #  분할 전: 평단 90,000 · 고점 105,000 · 현재가 100,000 → 아직 청산 아님
    assert _ts(105_000, 90_000, 100_000)['triggered'] is False
    #  5:1 분할 후: 평단·현재가만 1/5이 되고 고점은 그대로 남는다
    stale = _ts(105_000, 18_000, 20_000)
    assert stale['triggered'] is True, "이 테스트가 실패하면 사고 시나리오 자체가 바뀐 것이다"
    assert stale['drop_rate'] > 75


def test_split_with_fix_keeps_position():
    """고점을 같은 배율로 보정하면 분할 전과 같은 판정이 나온다."""
    fixed = _ts(105_000 * 0.2, 18_000, 20_000)
    assert fixed['triggered'] is False
    assert fixed['drop_rate'] == pytest.approx(_ts(105_000, 90_000, 100_000)['drop_rate'])


# ===========================================================================
# 3. 트레이더 통합 — DB 최고가가 실제로 내려가는가
# ===========================================================================
@pytest.fixture
def trader():
    AutoTrader._instance = None
    t = AutoTrader()
    db_manager.db.delete_trailing_stop(CODE)
    yield t
    db_manager.db.delete_trailing_stop(CODE)


def _item(qty, avg, pchs_amt=None):
    return {'pdno': CODE, 'hldg_qty': str(qty), 'pchs_avg_pric': str(avg),
            'pchs_amt': str(pchs_amt if pchs_amt is not None else int(qty * avg))}


def _apply(trader, qty, avg, highest, pchs_amt=None):
    #  [2026-09-06] 권리 조정 알림은 alert_delivered 를 지난다 — 취소는 되돌릴 수 없고
    #   본문이 "조정 후 가격 기준으로 다시 설정해 주세요"라고 사람의 조치를 요구하므로,
    #   전달 여부를 확인해야 한다(api.send_telegram_message 는 비동기라 실패해도 조용하다).
    with patch('modules.auto_trade.alert_delivered', return_value=True) as tg:
        out = trader._apply_corporate_action(CODE, NAME, _item(qty, avg, pchs_amt),
                                             float(avg), float(highest))
    return out, tg


def test_trader_rescales_stored_highest_on_split(trader):
    db_manager.db.update_highest_price(CODE, 105_000)
    _apply(trader, 10, 90_000, 105_000)              # 1주기: 기준값 기록만

    out, tg = _apply(trader, 50, 18_000, 105_000)    # 2주기: 5:1 분할
    assert out == pytest.approx(21_000)              # 105,000 × 0.2
    assert db_manager.db.get_highest_price(CODE) == pytest.approx(21_000)
    assert tg.called, "권리 조정은 사용자가 알아야 한다(수량·평단이 통째로 바뀐다)"
    assert trader.trailing_stop_cache[CODE] == pytest.approx(21_000)


def test_trader_ignores_normal_buy(trader):
    """매수로 평단이 올라도 최고가는 건드리지 않는다."""
    db_manager.db.update_highest_price(CODE, 105_000)
    _apply(trader, 10, 90_000, 105_000)

    out, tg = _apply(trader, 15, 100_000, 105_000)   # 매입원가 90만 → 150만
    assert out == pytest.approx(105_000)
    assert db_manager.db.get_highest_price(CODE) == pytest.approx(105_000)
    assert not tg.called


def test_reference_tracks_latest_position(trader):
    """기준값은 매 주기 최신으로 옮겨야 다음 비교가 성립한다.

    옮기지 않으면 매수 다음 주기에 '평단은 바뀌었는데 매입금액은 그대로'가 되어
    정상 매수가 뒤늦게 분할로 오인된다.
    """
    db_manager.db.update_highest_price(CODE, 105_000)
    _apply(trader, 10, 90_000, 105_000)
    _apply(trader, 15, 100_000, 105_000)             # 매수
    out, tg = _apply(trader, 15, 100_000, 105_000)   # 그대로 유지된 다음 주기
    assert out == pytest.approx(105_000)
    assert not tg.called
    assert db_manager.db.get_position_ref(CODE) == (pytest.approx(100_000),
                                                    pytest.approx(1_500_000))


def test_missing_pchs_amt_falls_back_to_qty_times_avg(trader):
    """실전 잔고·토스 어댑터는 pchs_amt를 0으로 준다. 복원하지 못하면 판정이 죽는다."""
    db_manager.db.update_highest_price(CODE, 105_000)
    _apply(trader, 10, 90_000, 105_000, pchs_amt=0)
    out, tg = _apply(trader, 50, 18_000, 105_000, pchs_amt=0)
    assert out == pytest.approx(21_000)
    assert tg.called


def test_failure_does_not_block_sell_analysis(trader):
    """보정이 실패해도 매도 분석은 계속돼야 한다 — 손절이 막히는 쪽이 더 위험하다."""
    with patch.object(db_manager.db, 'get_position_ref', side_effect=RuntimeError("DB down")):
        out, _tg = _apply(trader, 50, 18_000, 105_000)
    assert out == pytest.approx(105_000)


# ---------------------------------------------------------------------------
#  환산 실패 (2026-09-05)
#
#  rescale_highest_price 는 DB 잠금이 5회 소진되면 None 을 돌려준다. 종전에는 그 None 을
#  조용히 넘기고 **기준값은 조정 후 값으로 옮겼다**. 그러면 다음 주기의
#  detect_corporate_action 이 배율 1.0 을 보고 분할을 **다시는 감지하지 못한다** —
#  앵커는 조정 전 값(105,000)으로 영구히 남고, 5:1 분할이면 청산선이 현재가의 4배쯤에
#  서므로 그 종목은 트레일링 즉시 발동으로 시장가 강제 청산된다. 이 함수가 존재하는
#  이유가 바로 그것을 막는 것이다.
def test_환산_실패하면_기준값을_옮기지_않는다(trader):
    db_manager.db.update_highest_price(CODE, 105_000)
    _apply(trader, 10, 90_000, 105_000)                     # 1주기: 기준값 기록
    ref_before = db_manager.db.get_position_ref(CODE)

    with patch.object(db_manager.db, 'rescale_highest_price', return_value=None):
        out, tg = _apply(trader, 50, 18_000, 105_000)       # 2주기: 분할, 환산 실패

    assert db_manager.db.get_position_ref(CODE) == ref_before, (
        "환산에 실패했는데 기준값을 옮겼다 — 다음 주기에 분할을 다시 감지하지 못한다")
    assert out == pytest.approx(105_000), "앵커는 조정 전 값 그대로여야 한다(거짓 보고 금지)"
    assert tg.called, "환산 실패는 사용자가 알아야 한다"


def test_다음_주기가_환산을_다시_시도한다(trader):
    """기준값을 안 옮겼으므로 같은 배율이 다시 감지되어야 한다."""
    db_manager.db.update_highest_price(CODE, 105_000)
    _apply(trader, 10, 90_000, 105_000)

    with patch.object(db_manager.db, 'rescale_highest_price', return_value=None):
        _apply(trader, 50, 18_000, 105_000)

    out, tg = _apply(trader, 50, 18_000, 105_000)           # 3주기: 이번엔 성공
    assert out == pytest.approx(21_000), "재시도가 이루어지지 않았다"
    assert db_manager.db.get_highest_price(CODE) == pytest.approx(21_000)


def test_환산_실패는_알림에_적힌다(trader):
    db_manager.db.update_highest_price(CODE, 105_000)
    _apply(trader, 10, 90_000, 105_000)
    with patch.object(db_manager.db, 'rescale_highest_price', return_value=None):
        _out, tg = _apply(trader, 50, 18_000, 105_000)
    body = tg.call_args[0][0]
    assert "환산 실패" in body, f"실패가 알림에 안 보인다:\n{body}"
    assert "105,000" in body, "어느 값이 남았는지 알려야 조치할 수 있다"


def test_앵커가_없으면_실패가_아니다(trader):
    """환산할 앵커 자체가 없는 것(highest=0)은 실패가 아니다 — 기준값은 옮겨야 한다."""
    _apply(trader, 10, 90_000, 0)
    _apply(trader, 50, 18_000, 0)                           # 분할, 앵커 없음
    assert db_manager.db.get_position_ref(CODE) == (pytest.approx(18_000),
                                                    pytest.approx(900_000))


# ===========================================================================
# 4. 예약 주문 — 환산하지 않고 취소한다
# ===========================================================================
#  예약 주문의 목표가는 운영자가 조정 전 가격을 보고 직접 정한 값이라, 기계적으로
#  환산해도 의도한 자리가 아니다. 그대로 두면 STOP·LIMIT은 이미 도달한 것처럼,
#  TRAILING은 폭락한 것처럼 보여 어느 쪽이든 즉시 오발동한다. 취소하고 알린다.
def _reserve(code=CODE, order_type='sell', condition='STOP', target=95_000, qty=10,
             status='PENDING'):
    db_manager.db.execute_query(
        "INSERT INTO reserved_orders (cano, acnt, market, order_type, code, name, qty, "
        "order_price, condition_type, target_price, status) "
        "VALUES ('1','01','KR',?,?,?,?,0,?,?,?)",
        (order_type, code, NAME, qty, condition, target, status))


@pytest.fixture
def clean_reserves():
    db_manager.db.execute_query("DELETE FROM reserved_orders WHERE code IN (?, ?)",
                                (CODE, "000660"))
    yield
    db_manager.db.execute_query("DELETE FROM reserved_orders WHERE code IN (?, ?)",
                                (CODE, "000660"))


def _statuses(code=CODE):
    rows = db_manager.db.execute_query(
        "SELECT status FROM reserved_orders WHERE code=?", (code,), fetch='all') or []
    return sorted(r[0] for r in rows)


def test_split_cancels_pending_reserved_orders(trader, clean_reserves):
    """분할이 감지되면 그 종목의 대기 예약 주문을 전부 취소한다."""
    _reserve(condition='STOP', target=95_000)
    _reserve(condition='TRAILING_SELL', target=5)
    db_manager.db.update_highest_price(CODE, 105_000)
    _apply(trader, 10, 90_000, 105_000)

    _out, tg = _apply(trader, 50, 18_000, 105_000)
    assert _statuses() == ['CANCELED', 'CANCELED'], (
        "조정 전 목표가가 살아남으면 분할 직후 즉시 오발동한다")
    body = str(tg.call_args)
    assert "예약" in body and "다시 설정" in body, "무엇이 왜 취소됐는지 알려야 다시 걸 수 있다"


def test_normal_buy_leaves_reserved_orders_alone(trader, clean_reserves):
    """대조군 — 평범한 추가 매수는 예약 주문을 건드리지 않는다."""
    _reserve(condition='STOP', target=95_000)
    db_manager.db.update_highest_price(CODE, 105_000)
    _apply(trader, 10, 90_000, 105_000)

    _apply(trader, 15, 100_000, 105_000)             # 매입금액 90만 → 150만
    assert _statuses() == ['PENDING']


def test_other_stocks_reserves_are_untouched(trader, clean_reserves):
    """분할은 한 종목의 사건이다. 다른 종목 예약까지 취소하면 안 된다."""
    _reserve(condition='STOP', target=95_000)
    _reserve(code="000660", condition='STOP', target=200_000)
    db_manager.db.update_highest_price(CODE, 105_000)
    _apply(trader, 10, 90_000, 105_000)

    _apply(trader, 50, 18_000, 105_000)
    assert _statuses(CODE) == ['CANCELED']
    assert _statuses("000660") == ['PENDING']


def test_already_triggered_reserve_is_not_touched(trader, clean_reserves):
    """이미 발동된 주문은 대상이 아니다(PENDING만 취소).

    [중요] 발동분만 단독으로 두면 조회 단계에서 걸러져 UPDATE 자체가 실행되지 않아,
    'PENDING 조건 없이 전부 취소'하는 결함을 놓친다(변이 검증에서 실제로 통과했다).
    대기 1건 + 발동 1건이 섞인 현실적인 상태로 확인한다.
    """
    _reserve(condition='STOP', target=95_000, status='TRIGGERED')
    _reserve(condition='LIMIT', target=98_000, status='PENDING')
    db_manager.db.update_highest_price(CODE, 105_000)
    _apply(trader, 10, 90_000, 105_000)

    _apply(trader, 50, 18_000, 105_000)
    assert _statuses() == ['CANCELED', 'TRIGGERED'], "발동이 끝난 주문의 이력까지 덮어썼다"


def test_cancellation_is_recorded_in_trade_history(trader, clean_reserves):
    """거래내역에도 남겨야 나중에 '왜 사라졌지'를 추적할 수 있다."""
    _reserve(condition='STOP', target=95_000)
    db_manager.db.update_highest_price(CODE, 105_000)
    _apply(trader, 10, 90_000, 105_000)

    with patch.object(db_manager.db, 'insert_trade') as ins:
        _apply(trader, 50, 18_000, 105_000)
    assert ins.called
    assert "권리 조정" in str(ins.call_args)


def test_split_without_reserves_still_rescales(trader, clean_reserves):
    """예약이 없어도 최고가 보정은 그대로 동작해야 한다."""
    db_manager.db.update_highest_price(CODE, 105_000)
    _apply(trader, 10, 90_000, 105_000)
    out, tg = _apply(trader, 50, 18_000, 105_000)
    assert out == pytest.approx(21_000)
    assert tg.called


# ===========================================================================
# 5. 배선 — 실제 매도 경로가 보정된 값을 쓰는가
# ===========================================================================
#  [왜 따로 두는가] 위 3절은 _apply_corporate_action 을 직접 부른다. 그것만 있으면
#  _check_sell_conditions 에서 호출을 빼먹어도 전부 통과한다 — 변이 검증에서 실제로
#  그렇게 새어나갔다(호출부 삭제 시 16/16 통과). 판정에 넘어가는 값으로 확인한다.
def test_sell_path_feeds_corrected_highest_into_analysis(trader):
    import pandas as pd

    db_manager.db.update_highest_price(CODE, 105_000)
    db_manager.db.update_position_ref(CODE, 90_000, 900_000)     # 분할 전 기준값

    #  5:1 분할 후 잔고: 수량 ×5, 평단 ÷5, 매입금액 유지
    holdings = [{'pdno': CODE, 'prdt_name': NAME, 'hldg_qty': '50', 'ord_psbl_qty': '50',
                 'pchs_avg_pric': '18000', 'pchs_amt': '900000', 'prpr': '20000',
                 'evlu_amt': '1000000', 'evlu_pfls_amt': '100000', 'evlu_pfls_rt': '11.11'}]

    trader.is_running = True
    trader.market_index_status = {}
    trader.market_status_notified = {}
    df = pd.DataFrame({'close': [20000], 'high': [20000], 'low': [20000],
                       'open': [20000], 'volume': [1000]})
    with patch('modules.auto_trade.api.send_telegram_message'), \
         patch('modules.auto_trade.alert_delivered', return_value=True), \
         patch('modules.auto_trade.load_restricted_stocks', return_value={}), \
         patch('modules.auto_trade.api.fetch_sellable_quantity', return_value=50), \
         patch('modules.auto_trade.api.get_chart_data', return_value=df), \
         patch('modules.auto_trade.DefaultStrategy.analyze_sell') as mock_analyze, \
         patch.object(trader.order_manager, 'is_pending', return_value=False), \
         patch.object(trader.order_manager, 'send_order', return_value='1'):
        mock_analyze.return_value = {'action': 'hold', 'reason': '', 'score': 5.0,
                                     'state': '보유', 'ind': {'rsi': 50, 'adx': 20, 'cci': 0}}
        trader._check_sell_conditions(holdings, is_market_open=True)

    assert mock_analyze.called, "매도 판정 자체가 돌지 않았다 — 하네스 전제가 깨졌다"
    passed = mock_analyze.call_args.kwargs.get('highest_price')
    assert passed == pytest.approx(21_000), (
        f"매도 판정에 분할 전 고점이 그대로 넘어갔다({passed}) — 즉시 강제 청산된다")


import contextlib as _contextlib


@_contextlib.contextmanager
def _broken_db():
    """**기준값 SELECT 하나만** 실패시킨다.

    연결을 통째로 깨면 뒤이은 update_position_ref 도 함께 막혀, 옛 결함('기준값이
    조정 후 값으로 덮인다')이 재현되지 않는다 — 물기 시험에서 실제로 그랬다.
    쓰기는 살려 두어야 이 테스트가 무엇인가를 시험한다.

    연결은 **클래스**에 패치한다(tests/test_db_patch_scope_guard.py 참조).
    """
    import sqlite3

    real = getattr(db_manager.db, '_real_db', db_manager.db)
    orig = type(real)._get_conn

    class _Cursor:
        def __init__(self, inner):
            self._inner = inner

        def execute(self, sql, *a, **k):
            if "ref_avg_price" in sql and sql.strip().upper().startswith("SELECT"):
                raise sqlite3.OperationalError("database is locked")
            return self._inner.execute(sql, *a, **k)

        def __getattr__(self, name):
            return getattr(self._inner, name)

    class _Conn:
        def __init__(self, inner):
            self._inner = inner

        def cursor(self):
            return _Cursor(self._inner.cursor())

        def __getattr__(self, name):
            return getattr(self._inner, name)

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(type(real), "_get_conn",
                   lambda self, *a, **k: _Conn(orig(self, *a, **k)))
        yield


# ---------------------------------------------------------------------------
#  기준값 **조회** 실패 (2026-09-06)
#
#  환산 실패는 위에서 막았는데, 그 앞단인 '기준값을 읽지 못한 경우'는 열려 있었다.
#  get_position_ref 가 조회 실패를 (0.0, 0.0) 으로 돌려줬고, detect_corporate_action 은
#  그것을 "기준이 없다(최초 관측) — 이번 주기는 기록만 한다"로 읽는다. 그 '기록'이
#  기준값을 **조정 후 값으로 덮는다** — 결과는 환산 실패와 똑같다. 다시는 감지하지 못한다.
def test_기준값_조회_실패는_기준값을_옮기지_않는다(trader):
    db_manager.db.update_highest_price(CODE, 105_000)
    _apply(trader, 10, 90_000, 105_000)                     # 1주기: 기준값 기록
    ref_before = db_manager.db.get_position_ref(CODE)
    assert ref_before[0] > 0, "시나리오 전제가 성립하지 않는다"

    #  [중요] 메서드가 아니라 **DB 연결**을 깨뜨린다. 메서드를 예외로 갈아끼우면
    #   옛 동작((0,0) 반환)을 재현하지 못해 이 테스트가 아무것도 시험하지 않는다.
    with _broken_db():
        out, _tg = _apply(trader, 50, 18_000, 105_000)      # 2주기: 분할, 조회 실패

    assert db_manager.db.get_position_ref(CODE) == ref_before, (
        "기준값을 읽지 못했는데 기준값을 옮겼다 — 다음 주기에 분할을 다시 감지하지 못한다")
    assert out == pytest.approx(105_000), "앵커는 건드리지 않아야 한다"


def test_조회가_회복되면_같은_분할을_다시_감지한다(trader):
    """기준값을 안 옮겼으므로 다음 주기에 그대로 잡혀야 한다."""
    db_manager.db.update_highest_price(CODE, 105_000)
    _apply(trader, 10, 90_000, 105_000)

    with _broken_db():
        _apply(trader, 50, 18_000, 105_000)

    out, tg = _apply(trader, 50, 18_000, 105_000)           # 조회 회복
    assert out == pytest.approx(21_000), "회복 후에도 분할을 감지하지 못했다"
    assert tg.called


def test_조회_실패는_읽기_계약에서도_구분된다():
    """DB 계층 — 실패와 '기록 없음'이 같은 (0,0) 이면 위 판정이 성립할 수 없다."""
    import sqlite3

    real = getattr(db_manager.db, '_real_db', db_manager.db)
    assert real.get_position_ref("NO-SUCH-CODE") == (0.0, 0.0), "기록 없음은 (0,0) 이다"

    class _Broken:
        def cursor(self):
            raise sqlite3.OperationalError("database is locked")

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(type(real), "_get_conn", lambda self, *a, **k: _Broken())
        with pytest.raises(Exception):
            real.get_position_ref(CODE)
