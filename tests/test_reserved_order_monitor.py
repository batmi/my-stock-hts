import pytest
import os
import sys
from unittest.mock import patch, MagicMock
from datetime import datetime, timedelta
import pandas as pd

sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from modules.reserved_order_monitor import ReservedOrderMonitor


@pytest.fixture(autouse=True)
def _domestic_market_open():
    """예약 주문 감시는 국내 시장 개장(NXT 포함) 중에만 발동한다.

    아래 테스트들은 정규장(12:00) 시나리오이므로 개장으로 고정한다.
    (실제 시각에 따라 결과가 달라지는 플래키를 막는다)
    """
    with patch('modules.reserved_order_monitor.api.domestic_trading_session_open', return_value=True):
        yield


@pytest.fixture
def monitor():
    """싱글톤 인스턴스 초기화용 피스처"""
    m = ReservedOrderMonitor()
    m.is_running = False
    m.chart_cache = {}
    return m

def test_monitor_start_stop(monitor):
    """모니터 스레드 시작 및 중지 테스트"""
    monitor.start()
    assert monitor.is_running is True
    monitor.stop()
    assert monitor.is_running is False

@patch('modules.reserved_order_monitor.db_manager.db.get_pending_reserved_orders')
@patch('modules.reserved_order_monitor.db_manager.db.update_reserved_order_status')
@patch('modules.reserved_order_monitor.api.send_telegram_message')
def test_check_orders_expire(mock_tg, mock_update, mock_get_orders, monitor):
    """기간 만료된 예약 주문 처리 테스트"""
    past_date = (datetime.now() - timedelta(days=1)).strftime("%Y%m%d")
    mock_get_orders.return_value = [
        {'id': 1, 'code': '005930', 'condition_type': 'LIMIT', 'target_price': 80000, 
         'order_type': 'sell', 'market': 'KR', 'name': '삼성전자', 'qty': 10, 'order_price': 0, 'expire_dt': past_date}
    ]
    
    monitor._check_orders()
    mock_update.assert_called_with(1, 'EXPIRED')
    mock_tg.assert_called_once()

@patch('modules.reserved_order_monitor.db_manager.db.get_pending_reserved_orders')
@patch('modules.reserved_order_monitor.api.get_current_price')
@patch('modules.reserved_order_monitor.ReservedOrderMonitor._execute_order')
def test_check_orders_limit_trigger(mock_execute, mock_get_price, mock_get_orders, monitor):
    """지정가 도달 조건(LIMIT) 트리거 테스트 (매도)"""
    mock_get_orders.return_value = [
        {'id': 2, 'code': '000660', 'condition_type': 'LIMIT', 'target_price': 150000, 
         'order_type': 'sell', 'market': 'KR', 'name': 'SK하이닉스', 'qty': 10, 'order_price': 150000, 'expire_dt': '20991231'}
    ]
    # 현재가가 목표가보다 높으므로 매도 조건 충족
    mock_get_price.return_value = 155000
    
    with patch('modules.reserved_order_monitor.datetime') as mock_dt:
        mock_dt.now.return_value.strftime.side_effect = lambda x: '1000' if x == '%H%M' else '20991231'
        monitor._check_orders()
        
    mock_execute.assert_called_once()
    assert "지정가 도달" in mock_execute.call_args[0][1]

@patch('modules.reserved_order_monitor.db_manager.db.get_pending_reserved_orders')
@patch('modules.reserved_order_monitor.ReservedOrderMonitor._execute_order')
def test_check_orders_time_trigger(mock_execute, mock_get_orders, monitor):
    """특정 시간(TIME) 도달 트리거 테스트"""
    mock_get_orders.return_value = [
        {'id': 3, 'code': '005930', 'condition_type': 'TIME', 'target_time': '1520', 
         'order_type': 'buy', 'market': 'KR', 'name': '삼성전자', 'qty': 10, 'order_price': 0, 'expire_dt': '20991231', 'target_price': 0.0}
    ]
    
    with patch('modules.reserved_order_monitor.datetime') as mock_dt:
        mock_dt.now.return_value.strftime.side_effect = lambda x: '1521' if x == '%H%M' else '20991231'
        monitor._check_orders()
        
    mock_execute.assert_called_once()
    assert "지정 시간" in mock_execute.call_args[0][1]

@patch('modules.reserved_order_monitor.db_manager.db.get_pending_reserved_orders')
@patch('modules.reserved_order_monitor.api.get_current_price')
@patch('modules.reserved_order_monitor.db_manager.db.update_reserved_order_lowest')
@patch('modules.reserved_order_monitor.ReservedOrderMonitor._execute_order')
def test_check_orders_trailing_buy(mock_execute, mock_update_low, mock_get_price, mock_get_orders, monitor):
    """트레일링 매수 조건 트리거 및 최저가 갱신 테스트"""
    mock_get_orders.return_value = [
        {'id': 4, 'code': 'AAPL', 'condition_type': 'TRAILING_BUY', 'target_price': 3.0, 'lowest_price': 100.0,
         'order_type': 'buy', 'market': 'US', 'name': 'Apple', 'qty': 5, 'order_price': 0, 'expire_dt': '20991231'}
    ]
    
    with patch('modules.reserved_order_monitor.datetime') as mock_dt:
        # 미국 주식은 08:00~16:00 사이에 감시를 생략하므로, 밤 10시(2200)로 모킹
        mock_dt.now.return_value.strftime.side_effect = lambda x: '2200' if x == '%H%M' else '20991231'
        
        # 1. 가격 하락 -> 최저가 갱신 트리거 안됨
        mock_get_price.return_value = 95.0
        monitor._check_orders()
        mock_update_low.assert_called_with(4, 95.0)
        mock_execute.assert_not_called()

        # 2. 가격 상승 (95.0 대비 3% 이상 반등) -> 98.0
        mock_get_orders.return_value[0]['lowest_price'] = 95.0
        mock_get_price.return_value = 98.0
        monitor._check_orders()
        mock_execute.assert_called_once()
        assert "반등 매수" in mock_execute.call_args[0][1]

@patch('modules.reserved_order_monitor.api.place_order')
@patch('modules.reserved_order_monitor.db_manager.db.update_reserved_order_status')
@patch('modules.reserved_order_monitor.api.send_telegram_message')
def test_execute_order_success(mock_tg, mock_update, mock_place, monitor):
    """_execute_order 성공 로직 검증"""
    order = {
        'id': 5, 'cano': '12345678', 'acnt': '01', 'code': '005930', 'market': 'KR', 'order_type': 'buy', 'name': '삼성전자', 'qty': 10, 'order_price': 80000
    }
    mock_place.return_value = {'rt_cd': '0', 'output': {'ODNO': '123456'}}
    
    monitor._execute_order(order, "테스트 사유")
    
    mock_place.assert_called_once_with('domestic', 'buy', '005930', 10, '80000', '00')
    mock_update.assert_any_call(5, 'TRIGGERED', '123456')
    mock_tg.assert_called_once()

# ---------------------------------------------------------------------------
# 원장에 적히는 것이 '실제로 발주한 것'인가
#
# [왜] 예약 매도는 발주 직전에 매도가능수량과 대사해 수량을 줄인다(_reconcile_sell_qty).
# 그런데 거래내역(trades)에는 등록 시점의 order['qty']와 order_price(시장가면 0)를
# 적고 있었다 — 텔레그램 알림은 대사된 수량을 쓰므로 원장과 알림이 서로 달랐다.
# 원장은 사후에 손익·체결을 대조하는 유일한 근거라 '주문한 적 없는 수량'이 남으면 안 된다.
# ---------------------------------------------------------------------------

def _execute(monitor, order, sellable=None, price=0.0):
    """_execute_order 를 한 번 태우고 (insert_trade Mock, place_order Mock)을 돌려준다."""
    with patch('modules.reserved_order_monitor.db_manager.db.update_reserved_order_status'), \
         patch('modules.reserved_order_monitor.db_manager.db.cancel_other_reserved_orders',
               return_value=[]), \
         patch('modules.reserved_order_monitor.db_manager.db.insert_trade') as insert, \
         patch('modules.reserved_order_monitor.analysis.get_snapshot', return_value=None), \
         patch('modules.reserved_order_monitor.api.send_telegram_message'), \
         patch('modules.reserved_order_monitor.api.fetch_sellable_quantity',
               return_value=sellable), \
         patch('modules.reserved_order_monitor.api.get_current_price', return_value=price), \
         patch('modules.auto_trade.AutoTrader'), \
         patch('modules.auto_trade.ConclusionMonitor'), \
         patch('modules.reserved_order_monitor.api.place_order',
               return_value={'rt_cd': '0', 'output': {'ODNO': '123456'}}) as place:
        monitor._execute_order(order, "테스트 사유")
    return insert, place


def test_ledger_records_the_reconciled_sell_quantity(monitor):
    """외부 매매로 보유가 줄면 원장도 줄어든 수량을 적어야 한다."""
    order = {'id': 7, 'cano': '12345678', 'acnt': '01', 'code': '005930', 'market': 'KR',
             'order_type': 'sell', 'name': '삼성전자', 'qty': 10, 'order_price': 80000,
             'condition_type': 'PRICE_BELOW'}
    insert, place = _execute(monitor, order, sellable=4)
    assert place.call_args[0][3] == 4, "대조 조건이 깨졌다 — 발주 수량이 축소되지 않았다"
    assert insert.call_args[0][3] == 4, "원장에 주문한 적 없는 수량(등록 수량)이 남았다"


def test_ledger_records_the_price_actually_ordered_for_a_market_order(monitor):
    """시장가(등록가 0)로 현재가를 계산해 발주했으면 그 단가가 원장에 남아야 한다."""
    order = {'id': 8, 'cano': '12345678', 'acnt': '01', 'code': 'AAPL', 'market': 'US',
             'order_type': 'buy', 'name': 'AAPL', 'qty': 3, 'order_price': 0,
             'condition_type': 'PRICE_ABOVE'}
    insert, place = _execute(monitor, order, price=200.0)
    sent = float(place.call_args[0][4])
    assert sent > 0, "대조 조건이 깨졌다 — 시장가 단가가 계산되지 않았다"
    assert float(insert.call_args[0][4]) == pytest.approx(sent), "원장 단가가 0으로 남았다"
