"""예약 주문만 걸린 **미보유** 종목의 권리 조정을 잡는가.

보유 종목은 잔고의 평단·매입금액 변화로 잡는다(engine.detect_corporate_action).
예약 주문만 있는 종목은 보유분이 없어 그 근거가 통째로 없다. 그런데 방치하면 더 나쁘다.
5:1 분할이 나면 목표가는 이미 도달한 것처럼 보여서, 운영자가 의도한 적 없는 가격에
매수·매도가 곧바로 나간다.

[판정 근거] 거래소는 권리 조정 시 **과거 시세를 소급 수정**한다. 어제 종가로 적어 둔
값과 오늘 조회한 같은 날짜의 값을 맞대면, 그 차이가 곧 조정 배율이다.

[왜 '전일 대비 ±30% 초과'가 아닌가]
  · 거래소가 권리락일에 기준가를 미리 조정하므로 전일대비는 정상 범위로 보인다.
  · 30% 무상증자는 -23% — 가격제한폭 안이라 정상 등락과 구분되지 않는다.
"""
import pandas as pd
import pytest
from unittest.mock import patch

import config
from modules import db_manager
from modules.auto_trade.engine import detect_retro_price_adjustment
from modules.reserved_order_monitor import ReservedOrderMonitor

CODE = "005930"
NAME = "삼성전자"
TODAY = "20260804"
PREV = "20260803"
OLDER = "20260731"


def _df(rows):
    """rows: [(date, close)]"""
    return pd.DataFrame({'date': [r[0] for r in rows],
                         'close': [float(r[1]) for r in rows],
                         'open': [float(r[1]) for r in rows],
                         'high': [float(r[1]) for r in rows],
                         'low': [float(r[1]) for r in rows],
                         'volume': [1000.0] * len(rows)})


@pytest.fixture
def monitor():
    ReservedOrderMonitor._instance = None
    m = ReservedOrderMonitor()
    m.corp_checked_at.clear()
    yield m
    m.corp_checked_at.clear()


def _order(code=CODE, market="KR", oid=1):
    return {'id': oid, 'code': code, 'name': NAME, 'market': market, 'order_type': 'buy',
            'qty': 10, 'order_price': 500000, 'condition_type': 'LIMIT', 'target_price': 480000}


# ───────────────────────── 판정 산식 ─────────────────────────

def test_split_is_detected_from_the_retroactive_change():
    """[핵심] 같은 날짜의 과거 종가가 1/5이 됐으면 5:1 분할이다."""
    ratio, reason = detect_retro_price_adjustment(500_000, 100_000)
    assert ratio == pytest.approx(0.2)
    assert "액면분할" in reason


def test_a_bonus_issue_below_the_price_limit_is_still_caught():
    """[핵심] 30% 무상증자는 -23% — 가격제한폭 안이라 점프 감지로는 못 잡는다.

    소급 수정 비교는 조정 폭과 무관하게 잡는다. 이 테스트가 방식 선택의 근거다.
    """
    ratio, reason = detect_retro_price_adjustment(13_000, 10_000)
    assert abs(ratio - 1) < 0.30, "전제: 이 조정은 가격제한폭(±30%) 안이다"
    assert ratio != 1.0 and "무상증자" in reason, "제한폭 안의 조정을 놓쳤다"


def test_a_merge_is_labelled_as_a_merge():
    ratio, reason = detect_retro_price_adjustment(1_000, 10_000)
    assert ratio == pytest.approx(10.0) and "액면병합" in reason


def test_rounding_noise_is_not_a_corporate_action():
    """수정주가는 반올림되므로 정확히 같지 않다 — 1% 안은 조정이 아니다."""
    assert detect_retro_price_adjustment(50_000, 50_200)[0] == 1.0


def test_missing_reference_never_alarms():
    """기준이 없으면(최초 관측) 판정하지 않는다 — 첫 관측이 경보가 되면 안 된다."""
    assert detect_retro_price_adjustment(0, 100_000)[0] == 1.0
    assert detect_retro_price_adjustment(100_000, 0)[0] == 1.0


# ───────────────────────── 감시 배선 ─────────────────────────

def test_first_observation_only_records(monitor):
    """최초 관측은 기록만 한다 — 비교 대상이 없으므로 취소가 나가면 안 된다."""
    with patch.object(db_manager.db, 'get_corp_action_ref', return_value=("", 0.0, "")), \
         patch.object(db_manager.db, 'save_corp_action_ref') as save, \
         patch.object(monitor, '_cancel_on_corp_action') as cancel, \
         patch('modules.reserved_order_monitor.api.get_chart_data',
               return_value=_df([(OLDER, 500_000), (PREV, 510_000), (TODAY, 505_000)])):
        monitor._check_one_corp_action(CODE, TODAY, "kis")

    assert not cancel.called, "기준도 없는데 예약 주문을 취소했다"
    save.assert_called_once_with(CODE, PREV, 510_000.0, "kis")


def test_todays_bar_is_never_used_as_the_reference(monitor):
    """오늘 봉은 장중에 계속 변한다 — 기준으로 쓰면 매시간 '조정'이 감지된다."""
    with patch.object(db_manager.db, 'get_corp_action_ref', return_value=("", 0.0, "")), \
         patch.object(db_manager.db, 'save_corp_action_ref') as save, \
         patch('modules.reserved_order_monitor.api.get_chart_data',
               return_value=_df([(PREV, 510_000), (TODAY, 505_000)])):
        monitor._check_one_corp_action(CODE, TODAY, "kis")
    assert save.call_args[0][1] == PREV, "오늘 봉을 기준으로 잡았다"


def test_a_split_cancels_the_reserved_orders(monitor):
    """[핵심] 소급 수정이 확인되면 예약 주문을 취소한다."""
    #  어제 500,000원으로 기록해 뒀는데, 오늘 조회하니 같은 날짜가 100,000원이다.
    with patch.object(db_manager.db, 'get_corp_action_ref',
                      return_value=(PREV, 500_000.0, "kis")), \
         patch.object(db_manager.db, 'save_corp_action_ref') as save, \
         patch.object(monitor, '_cancel_on_corp_action') as cancel, \
         patch('modules.reserved_order_monitor.api.get_chart_data',
               return_value=_df([(PREV, 100_000), (TODAY, 98_000)])):
        monitor._check_one_corp_action(CODE, TODAY, "kis")

    assert cancel.called, "5:1 분할인데 예약 주문을 그대로 뒀다"
    assert cancel.call_args[0][1] == pytest.approx(0.2)
    # 감지 후에도 기준은 조정 후 값으로 옮겨야 매 시간 같은 경보가 반복되지 않는다.
    assert save.called and save.call_args[0][2] == 100_000.0


def test_an_ordinary_price_move_does_not_cancel_anything(monitor):
    """대조군 — 정상 등락은 과거 종가를 바꾸지 않는다.

    오늘 가격이 30% 빠져도 어제 봉의 종가는 그대로다. 이걸 취소하면 오탐이다.
    """
    with patch.object(db_manager.db, 'get_corp_action_ref',
                      return_value=(PREV, 500_000.0, "kis")), \
         patch.object(db_manager.db, 'save_corp_action_ref'), \
         patch.object(monitor, '_cancel_on_corp_action') as cancel, \
         patch('modules.reserved_order_monitor.api.get_chart_data',
               return_value=_df([(PREV, 500_000), (TODAY, 350_000)])):
        monitor._check_one_corp_action(CODE, TODAY, "kis")
    assert not cancel.called, "정상 급락을 권리 조정으로 오인했다"


def test_a_source_switch_refreshes_instead_of_alarming(monitor):
    """일봉 출처가 바뀌면 판정하지 않는다 — 수정주가 반영이 달라 오탐이 난다."""
    with patch.object(db_manager.db, 'get_corp_action_ref',
                      return_value=(PREV, 500_000.0, "toss")), \
         patch.object(db_manager.db, 'save_corp_action_ref') as save, \
         patch.object(monitor, '_cancel_on_corp_action') as cancel, \
         patch('modules.reserved_order_monitor.api.get_chart_data',
               return_value=_df([(PREV, 100_000), (TODAY, 98_000)])):
        monitor._check_one_corp_action(CODE, TODAY, "kis")   # 출처가 kis 로 바뀐 상태
    assert not cancel.called, "출처가 다른 값끼리 비교해 취소했다"
    assert save.called, "기준을 새 출처로 옮기지 않으면 영영 판정하지 못한다"


def test_a_reference_date_missing_from_the_window_refreshes(monitor):
    """기준일이 조회 구간 밖이면(장기 중단) 판정하지 않고 기준만 새로 잡는다."""
    with patch.object(db_manager.db, 'get_corp_action_ref',
                      return_value=("20200101", 500_000.0, "kis")), \
         patch.object(db_manager.db, 'save_corp_action_ref') as save, \
         patch.object(monitor, '_cancel_on_corp_action') as cancel, \
         patch('modules.reserved_order_monitor.api.get_chart_data',
               return_value=_df([(PREV, 100_000), (TODAY, 98_000)])):
        monitor._check_one_corp_action(CODE, TODAY, "kis")
    assert not cancel.called and save.called


# ───────────────────────── 스윕 / 순서 ─────────────────────────

def test_overseas_orders_are_skipped(monitor):
    """해외는 일봉 수정주가 반영 방식이 달라 같은 비교가 성립하지 않는다."""
    with patch.object(monitor, '_check_one_corp_action') as one:
        monitor._guard_corporate_actions([_order(market="US")])
    assert not one.called, "해외 종목까지 국내 방식으로 판정했다"


def test_each_code_is_checked_at_most_once_per_interval(monitor):
    """감시 스레드는 10초 주기다 — 매번 일봉을 조회하면 API를 태운다."""
    with patch.object(monitor, '_check_one_corp_action') as one:
        for _ in range(5):
            monitor._guard_corporate_actions([_order()])
    assert one.call_count == 1, f"주기당 1회여야 하는데 {one.call_count}회 조회했다"


def test_the_guard_runs_before_any_trigger_evaluation(monitor):
    """[배선·순서] 취소가 발동 판정보다 **앞서야** 한다.

    조정 직후의 목표가는 이미 도달한 것처럼 보인다. 순서가 뒤집히면 취소하기 전에
    오발동이 먼저 나가서, 취소는 아무것도 구하지 못한다.
    """
    calls = []

    def _guard(orders):
        calls.append("guard")

    with patch.object(monitor, '_guard_corporate_actions', side_effect=_guard), \
         patch.object(db_manager.db, 'get_pending_reserved_orders', return_value=[_order()]), \
         patch('modules.reserved_order_monitor.api.get_current_price',
               side_effect=lambda *a, **k: calls.append("price") or 100_000), \
         patch('modules.reserved_order_monitor.api.domestic_trading_session_open',
               return_value=True), \
         patch.object(config.session, 'is_toss', False):
        monitor._check_orders()

    assert calls and calls[0] == "guard", f"발동 판정이 권리 조정 점검보다 앞섰다: {calls}"
