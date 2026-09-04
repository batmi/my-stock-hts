"""가격제한폭(상·하한가) 밖 주문으로 손절이 통째로 거부되지 않는가.

[사고 시나리오] 주문가는 '현재가 ± 슬리피지'로 만든다. 평소엔 문제없지만 종목이
하한가에 락되면 매도 지정가가 제한폭 **밖**으로 나가 접수 자체가 거부된다.
  전일 종가 100,000 → 하한가 70,000 → 우리 매도가 69,900 → 거부
하필 -30% 폭락일, 손절이 가장 필요한 날에 주문이 나가지 않는다. 게다가 실패해도
상태가 정리되어 다음 주기에 같은 값으로 재시도하므로 하루 종일 거부만 반복하고
포지션은 방치된다(알림만 100건 넘게 쌓인다).

[백테스트로 못 잡는 이유] 백테스트는 종가로 체결을 가정할 뿐 '주문이 거부됐다'는
상태가 없다. 실계좌에서만 드러난다.

[설계 의도] 체결을 보장하려는 게 아니다 — 하한가엔 매도 잔량이 쌓여 있다. 다만
**대기열에 들어가는 것과 접수조차 안 되는 것은 다르다**. 락이 풀리는 순간을 잡으려면
주문이 걸려 있어야 한다.
"""
import time
from unittest.mock import MagicMock, patch

import pytest

from core import utils
from modules.auto_trade import AutoTrader

CODE = "005930"
UPPER, LOWER = 130_000, 70_000      # 전일 종가 100,000원 기준 ±30%


# ===========================================================================
# 1. 클램프 순수 함수
# ===========================================================================
def test_sell_below_lower_limit_is_pulled_up():
    """하한가 아래로 내려간 매도가를 하한가로 되돌린다(핵심 사고 경로)."""
    assert utils.clamp_to_price_limit(69_900, UPPER, LOWER) == LOWER


def test_buy_above_upper_limit_is_pulled_down():
    assert utils.clamp_to_price_limit(130_300, UPPER, LOWER) == UPPER


def test_price_inside_band_is_untouched():
    assert utils.clamp_to_price_limit(100_000, UPPER, LOWER) == 100_000


@pytest.mark.parametrize("upper,lower", [(0, 0), (0, LOWER), (UPPER, 0)])
def test_missing_limits_fail_open(upper, lower):
    """한도를 못 구했으면 건드리지 않는다 — 잘못된 한도로 주문가를 흔드는 쪽이 더 위험하다."""
    got = utils.clamp_to_price_limit(69_900, upper, lower)
    assert got == (LOWER if lower else 69_900)


def test_non_numeric_and_zero_are_passthrough():
    assert utils.clamp_to_price_limit(0, UPPER, LOWER) == 0
    assert utils.clamp_to_price_limit(None, UPPER, LOWER) is None


# ===========================================================================
# 2. 트레이더 배선
# ===========================================================================
@pytest.fixture
def trader():
    AutoTrader._instance = None
    return AutoTrader()


def test_trader_clamps_sell_price_at_limit_down(trader):
    with patch('modules.auto_trade.api.get_price_limits', return_value=(UPPER, LOWER)):
        assert trader._clamp_order_price(CODE, 69_900) == LOWER


def test_trader_clamps_buy_price_at_limit_up(trader):
    with patch('modules.auto_trade.api.get_price_limits', return_value=(UPPER, LOWER)):
        assert trader._clamp_order_price(CODE, 130_300) == UPPER


def test_trader_skips_clamp_when_limits_unavailable(trader):
    """토스(관찰) 모드 등 한도를 못 주는 경로에서는 원래 값을 그대로 쓴다."""
    with patch('modules.auto_trade.api.get_price_limits', return_value=(0, 0)):
        assert trader._clamp_order_price(CODE, 69_900) == 69_900


def test_trader_survives_limit_lookup_failure(trader):
    """한도 조회가 터져도 주문 경로 자체는 죽으면 안 된다."""
    with patch('modules.auto_trade.api.get_price_limits', side_effect=RuntimeError("API down")):
        assert trader._clamp_order_price(CODE, 69_900) == 69_900


# ===========================================================================
# 3. 배선 — 실제 매도 경로가 클램프를 거치는가
# ===========================================================================
#  [왜 따로] 위 2절은 _clamp_order_price 를 직접 부른다. 그것만 있으면 매도 경로에서
#  호출을 빼먹어도 전부 통과한다. 실제로 나가는 주문가로 확인한다.
def test_sell_path_sends_order_inside_price_band(trader):
    import pandas as pd

    crash = LOWER          # 하한가에 락된 상태
    holdings = [{'pdno': CODE, 'prdt_name': '삼성전자', 'hldg_qty': '10', 'ord_psbl_qty': '10',
                 'pchs_avg_pric': '100000', 'pchs_amt': '1000000', 'prpr': str(crash),
                 'evlu_amt': str(crash * 10), 'evlu_pfls_amt': '-300000',
                 'evlu_pfls_rt': '-30.0'}]

    trader.is_running = True
    trader.market_index_status = {}
    trader.market_status_notified = {}
    df = pd.DataFrame({'close': [crash], 'high': [crash], 'low': [crash],
                       'open': [crash], 'volume': [1000]})
    with patch('modules.auto_trade.api.send_telegram_message'), \
         patch('modules.auto_trade.load_restricted_stocks', return_value={}), \
         patch('modules.auto_trade.api.fetch_sellable_quantity', return_value=10), \
         patch('modules.auto_trade.api.get_chart_data', return_value=df), \
         patch('modules.auto_trade.api.get_price_limits', return_value=(UPPER, LOWER)), \
         patch('modules.auto_trade.DefaultStrategy.analyze_sell') as mock_analyze, \
         patch.object(trader.order_manager, 'is_pending', return_value=False), \
         patch.object(trader.order_manager, 'send_order', return_value='1') as mock_send:
        mock_analyze.return_value = {'action': 'sell', 'reason': 'ATR손절', 'score': 1.0,
                                     'state': '매도', 'ind': {'rsi': 20, 'adx': 30, 'cci': -200}}
        trader._check_sell_conditions(holdings, is_market_open=True)

    assert mock_send.called, "손절 주문 자체가 나가지 않았다 — 하네스 전제가 깨졌다"
    sent = mock_send.call_args.kwargs.get('price')
    assert sent >= LOWER, (
        f"하한가({LOWER:,}) 아래 {sent:,}원으로 주문했다 — 접수 거부되어 손절이 실패한다")


# ===========================================================================
# 4. 주문 실패 알림 쿨다운
# ===========================================================================
#  억제하는 것은 알림뿐이고 재시도가 아니다 — 제한폭이 풀리면 체결돼야 하므로
#  주문 시도는 계속한다. 같은 원인이 반복될 때만 조용해진다.
@pytest.fixture
def om(trader):
    trader.order_manager.order_fail_alerted = {}
    #  알림은 전달을 확인한 뒤에 쿨다운을 찍는다(common.alert_delivered). 테스트 환경은
    #  토큰이 비어 있어 '텔레그램 미구성 = 전달 성공'으로 흘러 종전과 같은 쿨다운이 된다.
    return trader.order_manager


def test_same_failure_alerts_only_once(om):
    assert om._alert_order_fail(CODE, 'sell', '40310000', 'msg') is True
    for _ in range(5):
        assert om._alert_order_fail(CODE, 'sell', '40310000', 'msg') is False, (
            "하한가 락 하루면 3분마다 같은 실패가 반복돼 알림이 100건 넘게 쌓인다")


def test_different_cause_alerts_immediately(om):
    om._alert_order_fail(CODE, 'sell', '40310000', 'msg')      # 제한폭 초과
    assert om._alert_order_fail(CODE, 'sell', '40240000', 'msg') is True, (
        "원인이 바뀌면 새로 생긴 문제다 — 쿨다운을 기다리면 안 된다")


def test_different_stock_alerts_immediately(om):
    om._alert_order_fail(CODE, 'sell', '40310000', 'msg')
    assert om._alert_order_fail("000660", 'sell', '40310000', 'msg') is True


def test_buy_and_sell_are_tracked_separately(om):
    om._alert_order_fail(CODE, 'sell', '40310000', 'msg')
    assert om._alert_order_fail(CODE, 'buy', '40310000', 'msg') is True


def test_cooldown_expires(om):
    om._alert_order_fail(CODE, 'sell', '40310000', 'msg')
    key = (CODE, 'sell', '40310000')
    #  값의 의미가 '보낸 시각' → '다음 알림 가능 시각'으로 바뀌었다(전달 확인 뒤 기록).
    om.order_fail_alerted[key] = time.time() - 1
    assert om._alert_order_fail(CODE, 'sell', '40310000', 'msg') is True


def test_successful_order_resets_suppression(om, trader):
    """접수에 성공한 뒤 다시 실패하면 '새로 생긴 문제'다. 즉시 알려야 한다."""
    om._alert_order_fail(CODE, 'sell', '40310000', 'msg')
    assert om._alert_order_fail(CODE, 'sell', '40310000', 'msg') is False

    ok = {'rt_cd': '0', 'output': {'ODNO': '0001'}}
    with patch('modules.auto_trade.api.place_order', return_value=ok), \
         patch('modules.auto_trade.api.send_telegram_message'), \
         patch('modules.auto_trade.api.get_current_price', return_value=100_000), \
         patch('modules.auto_trade.register_system_odno'), \
         patch('modules.auto_trade.db_manager.db.insert_trade'), \
         patch('modules.auto_trade.db_manager.db.update_highest_price'), \
         patch('modules.auto_trade.ConclusionMonitor', return_value=MagicMock()), \
         patch('time.sleep'):
        om.send_order(CODE, 1, 'sell', name="삼성전자", price=100_000)

    assert om._alert_order_fail(CODE, 'sell', '40310000', 'msg') is True


def test_rejected_order_still_retries_next_cycle(om):
    """[중요] 억제는 알림에만 걸린다 — 종목이 pending에 묶여 재시도가 막히면 안 된다."""
    fail = {'rt_cd': '1', 'msg_cd': '40310000', 'msg1': '주문가격이 가격제한폭을 벗어났습니다'}
    #  발송은 alert_delivered 를 거친다 — 전달을 확인한 뒤에 쿨다운을 찍기 때문이다.
    with patch('modules.auto_trade.api.place_order', return_value=fail), \
         patch('modules.auto_trade.alert_delivered', return_value=True) as tg:
        for _ in range(3):
            om.send_order(CODE, 1, 'sell', name="삼성전자", price=69_900)

    assert not om.is_pending(CODE), "거부된 주문이 pending으로 남으면 다음 주기 손절이 막힌다"
    assert tg.call_count == 1, f"같은 실패를 {tg.call_count}번 알렸다"
