import pytest
from unittest.mock import patch, MagicMock
from modules.auto_trade import ConclusionMonitor, OrderManager, AutoTrader, OrderStatus
import config
from datetime import datetime

@pytest.fixture(autouse=True)
def reset_singleton():
    """싱글톤 인스턴스 초기화"""
    AutoTrader._instance = None
    ConclusionMonitor._instance = None
    yield

@pytest.fixture
def monitor():
    return ConclusionMonitor()

@patch('modules.auto_trade.api.get_today_history')
@patch('modules.auto_trade.db_manager.db')
@patch('modules.auto_trade.api.send_telegram_message')
def test_check_conclusions_filled(mock_tg, mock_db, mock_get_history, monitor):
    """체결 확인 로직 테스트"""
    # Mock API response (Filled)
    mock_get_history.return_value = {
        'rt_cd': '0',
        'output1': [{
            'odno': '12345', 'pdno': '005930', 'prdt_name': 'Samsung',
            'ord_qty': '10', 'tot_ccld_qty': '10', 'avg_prvs': '60000',
            'sll_buy_dvsn_cd_name': '매수'
        }]
    }
    
    # Mock DB
    mock_db.check_trade_exists.return_value = False # New trade
    
    # Run
    monitor._check_conclusions(initial=False)
    
    # Verify
    mock_db.insert_trade.assert_called()
    mock_tg.assert_called()
    assert "[매수 체결]" in mock_tg.call_args[0][0]

@patch('modules.auto_trade.api.get_unfilled_orders')
@patch('modules.auto_trade.api.revise_cancel_order')
def test_manage_unfilled_orders_cancel(mock_cancel, mock_get_unfilled):
    """오래된 미체결 주문 취소 테스트"""
    trader = AutoTrader()
    
    # Mock Unfilled Orders (Old)
    mock_get_unfilled.return_value = [{
        'odno': '12345', 'pdno': '005930', 'prdt_name': 'Samsung',
        'rmn_qty': '10', 'ord_tmd': '090000' # 09:00:00
    }]
    
    # Mock Cancel Response
    mock_cancel.return_value = {'rt_cd': '0'}
    
    # Set current time to 10:00:00 (1 hour later)
    class FakeDatetime(datetime):
        @classmethod
        def now(cls):
            return cls(2023, 1, 2, 10, 0, 0)

    with patch('modules.auto_trade.datetime', FakeDatetime):
        with patch('modules.auto_trade.db_manager.db.get_trade_by_odno', return_value={'type': 'buy'}):
            with patch.object(trader, 'is_market_open', return_value=True):
                # Set threshold to 10 seconds
                config.UNFILLED_ORDER_CANCEL_SECONDS = 10
            
                trader.order_manager.manage_unfilled_orders()

        
    mock_cancel.assert_called()

# ---- WS 체결통보 확정 라벨링 ----
from modules.auto_trade import _norm_odno


def test_norm_odno_strips_zero_padding():
    # 발주 ODNO와 WS 체결통보 주문번호의 0 패딩 차이를 흡수해야 한다
    assert _norm_odno("0000013727") == _norm_odno("13727")
    assert _norm_odno("13727") == "13727"
    assert _norm_odno("CANCEL_99") == "CANCEL_99"  # 비숫자는 원문 유지


def test_ws_exec_notice_records_fill_and_ignores_rejected(monitor):
    with patch.object(monitor, 'check_now'):
        # 실제 체결(is_fill) → 기록
        monitor._on_ws_exec_notice({'is_fill': True, 'rejected': False,
                                    'odno': '0000013727', 'price': 28200.0, 'qty': 33})
        assert _norm_odno('0000013727') in monitor.ws_confirmed_fills
        # 거부 통보 → 무시
        monitor._on_ws_exec_notice({'is_fill': False, 'rejected': True,
                                    'odno': '0000099999', 'price': 0, 'qty': 0})
        assert _norm_odno('0000099999') not in monitor.ws_confirmed_fills


@patch('modules.auto_trade.api.get_current_price_data', return_value={'rt_cd': '1'})
@patch('modules.auto_trade.api.get_current_price', return_value=0)
@patch('modules.auto_trade.db_manager.db')
@patch('modules.auto_trade.api.send_telegram_message')
def test_handle_simulation_fill_ws_confirmed_drops_estimate(mock_tg, mock_db, mock_cp, mock_cpd, monitor):
    mock_db.check_trade_exists.return_value = False
    mock_db.get_all_stock_strategies.return_value = []
    odno = '0000013727'
    # WS 체결통보 수신(실제 체결가 28,200)
    with patch.object(monitor, 'check_now'):
        monitor._on_ws_exec_notice({'is_fill': True, 'rejected': False,
                                    'odno': odno, 'price': 28200.0, 'qty': 33})
    trade = {'type': 'sell', 'name': '대한항공', 'price': 0, 'reason': '사용자 수동 주문',
             'profit_amt': -34650, 'profit_rate': -3.58}
    monitor._handle_simulation_fill(MagicMock(), trade, odno, '003490', 33, '잔고 감소 확인')

    # DB: '체결(추정)'이 아닌 '체결'로 저장
    assert mock_db.insert_trade.call_args.kwargs.get('order_status') == '체결'
    # 알림: (추정) 문구 없음 + 실제 체결가 사용
    msg = mock_tg.call_args[0][0]
    assert '[매도 체결]' in msg and '추정' not in msg
    assert '28,200원(체결가)' in msg


@patch('modules.auto_trade.api.get_current_price_data', return_value={'rt_cd': '1'})
@patch('modules.auto_trade.api.get_current_price', return_value=28000)
@patch('modules.auto_trade.db_manager.db')
@patch('modules.auto_trade.api.send_telegram_message')
def test_handle_simulation_fill_without_ws_keeps_estimate(mock_tg, mock_db, mock_cp, mock_cpd, monitor):
    mock_db.check_trade_exists.return_value = False
    mock_db.get_all_stock_strategies.return_value = []
    trade = {'type': 'sell', 'name': '대한항공', 'price': 0, 'reason': '사용자 수동 주문',
             'profit_amt': -34650, 'profit_rate': -3.58}
    # WS 기록 없음 → 기존 추정 라벨 유지
    monitor._handle_simulation_fill(MagicMock(), trade, '0000013727', '003490', 33, '잔고 감소 확인')

    assert mock_db.insert_trade.call_args.kwargs.get('order_status') == '체결(추정)'
    msg = mock_tg.call_args[0][0]
    assert '[매도 체결(추정)]' in msg
    assert '(추정체결가)' in msg
