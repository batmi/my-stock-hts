import pytest
import pandas as pd
from unittest.mock import patch, MagicMock
from datetime import datetime, timedelta
import sys
import os

# 프로젝트 루트 경로 추가
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import config
from modules.telegram_bot import TelegramCommander
from modules.reserved_order_monitor import ReservedOrderMonitor
from modules.auto_trade import AutoTrader, ConclusionMonitor

@pytest.fixture(autouse=True)
def setup_teardown():
    """매 테스트마다 config 설정을 강제 초기화하여 독립성을 보장합니다."""
    original_is_sim = config.session.is_simulation
    config.session.is_simulation = True
    
    yield
    
    config.session.is_simulation = original_is_sim

@patch('modules.telegram_bot.TelegramCommander._get_refined_trades_cached')
def test_telegram_cmd_stats(mock_get_trades):
    """TelegramBot: /stats 명령어 (종목별 성과 분석) 테스트"""
    cmd = TelegramCommander()
    
    # 가상의 체결된 매매 기록 생성
    mock_get_trades.return_value = [
        {'code': '005930', 'name': '삼성전자', 'type': 'buy', 'time': '2023-10-01 10:00:00', 'price': 70000, 'qty': 10, 'order_status': '체결'},
        {'code': '005930', 'name': '삼성전자', 'type': 'sell', 'time': '2023-10-02 10:00:00', 'price': 71000, 'qty': 10, 'profit_amt': 10000, 'profit_rate': 1.4, 'reason': '익절', 'order_status': '체결'}
    ]
    
    res = cmd._cmd_stats([])
    assert "종목별 성과 분석" in res
    assert "삼성전자 (005930)" in res
    assert "총손익: +10,000원" in res
    assert "익절" in res

@patch('modules.telegram_bot.bot_executor.submit')
@patch('modules.telegram_bot.TelegramCommander._send_reply')
def test_telegram_cmd_curate(mock_send_reply, mock_submit):
    """TelegramBot: /curate 명령어 (주도주 큐레이션) 테스트"""
    cmd = TelegramCommander()
    cmd._cmd_curate([])
    mock_send_reply.assert_called_with("⏳ [AI 종목 큐레이션] 실시간 시장 매크로 데이터 및 뉴스를 분석하여 주도주를 발굴 중입니다. 잠시만 기다려주세요...")
    mock_submit.assert_called_once()

@patch('modules.telegram_bot.bot_executor.submit')
@patch('modules.telegram_bot.TelegramCommander._send_reply')
def test_telegram_cmd_scan(mock_send_reply, mock_submit):
    """TelegramBot: /scan 명령어 (트레이딩뷰 스크리너) 테스트"""
    cmd = TelegramCommander()
    cmd._cmd_scan([])
    mock_send_reply.assert_called_with("⏳ [TradingView Screener] 시장을 스캔 중입니다. 잠시만 기다려주세요...")
    mock_submit.assert_called_once()

@patch('modules.telegram_bot.bot_executor.submit')
@patch('modules.telegram_bot.TelegramCommander._send_reply')
def test_telegram_cmd_briefing_closing(mock_send_reply, mock_submit):
    """TelegramBot: /briefing, /closing 명령어 테스트"""
    cmd = TelegramCommander()
    
    cmd._cmd_briefing([])
    mock_send_reply.assert_any_call("⏳ [AI 시황 브리핑] 실시간 글로벌 마켓 데이터를 수집하고 AI 시황 브리핑을 작성 중입니다. 잠시만 기다려주세요...")
    
    cmd._cmd_closing([])
    mock_send_reply.assert_any_call("⏳ [AI 장 마감 종합 브리핑] 오늘 시장 흐름, 보유 종목 특이사항 및 당일 매매 내역을 종합 분석 중입니다. 잠시만 기다려주세요...")
    assert mock_submit.call_count == 2

@patch('modules.reserved_order_monitor.db_manager.db')
@patch('modules.reserved_order_monitor.api')
def test_reserved_order_expire(mock_api, mock_db):
    """ReservedOrderMonitor: 유효기간 만료 주문(EXPIRED) 처리 테스트"""
    monitor = ReservedOrderMonitor()
    
    past_date = (datetime.now() - timedelta(days=1)).strftime("%Y%m%d")
    
    # 만료된 날짜를 가진 가상의 예약 주문
    mock_db.get_pending_reserved_orders.return_value = [
        {'id': 1, 'code': '005930', 'name': '삼성전자', 'condition_type': 'LIMIT', 'target_price': 70000, 'order_type': 'buy', 'market': 'KR', 'expire_dt': past_date, 'qty': 10}
    ]
    
    monitor._check_orders()
    
    # 만료된 주문은 상태가 EXPIRED로 업데이트되고 기록되어야 함
    mock_db.update_reserved_order_status.assert_called_with(1, 'EXPIRED')
    mock_db.insert_trade.assert_called()
    mock_api.send_telegram_message.assert_called()

@patch('modules.reserved_order_monitor.db_manager.db')
@patch('modules.reserved_order_monitor.api.get_current_price')
@patch('modules.reserved_order_monitor.api.domestic_trading_session_open', return_value=True)
def test_reserved_order_trailing_buy(mock_session_open, mock_get_price, mock_db):
    """ReservedOrderMonitor: 트레일링 매수 조건(TRAILING_BUY) 테스트"""
    monitor = ReservedOrderMonitor()

    # 바닥 대비 3% 반등을 기다리는 트레일링 매수 주문
    mock_db.get_pending_reserved_orders.return_value = [
        {'id': 2, 'code': '005930', 'name': '삼성전자', 'condition_type': 'TRAILING_BUY', 'target_price': 3.0, 'order_type': 'buy', 'market': 'KR', 'lowest_price': 10000, 'qty': 10, 'order_price': 0}
    ]

    def _mock_strftime(fmt):
        if fmt == '%H%M': return '1000'
        if fmt == '%Y%m%d%H%M': return '209912311000'
        return '20991231'

    # Case 1: 가격이 더 내려갔을 때 (최저가 10,000 -> 9,000원 갱신)
    mock_get_price.return_value = 9000
    with patch('modules.reserved_order_monitor.datetime') as mock_dt:
        mock_dt.now.return_value.strftime.side_effect = _mock_strftime
        monitor._check_orders()
    mock_db.update_reserved_order_lowest.assert_called_with(2, 9000)

    # Case 2: 가격이 바닥에서 3% 이상 반등했을 때 (9,000원에서 9,300원으로 반등)
    mock_db.get_pending_reserved_orders.return_value = [
        {'id': 2, 'code': '005930', 'name': '삼성전자', 'condition_type': 'TRAILING_BUY', 'target_price': 3.0, 'order_type': 'buy', 'market': 'KR', 'lowest_price': 9000, 'qty': 10, 'order_price': 0}
    ]
    mock_get_price.return_value = 9300

    with patch('modules.reserved_order_monitor.datetime') as mock_dt:
        mock_dt.now.return_value.strftime.side_effect = _mock_strftime
        with patch.object(monitor, '_execute_order') as mock_exec:
            monitor._check_orders()
            mock_exec.assert_called_once()

@patch('modules.reserved_order_monitor.db_manager.db')
@patch('modules.reserved_order_monitor.api.get_current_price')
@patch('modules.reserved_order_monitor.api.domestic_trading_session_open', return_value=True)
def test_reserved_order_trailing_sell(mock_session_open, mock_get_price, mock_db):
    """ReservedOrderMonitor: 트레일링 매도 조건(TRAILING_SELL) 테스트"""
    monitor = ReservedOrderMonitor()

    # 고점 대비 5% 하락을 기다리는 트레일링 매도 주문
    mock_db.get_pending_reserved_orders.return_value = [
        {'id': 3, 'code': '005930', 'name': '삼성전자', 'condition_type': 'TRAILING_SELL', 'target_price': 5.0, 'order_type': 'sell', 'market': 'KR', 'highest_price': 10000, 'qty': 10, 'order_price': 0}
    ]

    def _mock_strftime(fmt):
        if fmt == '%H%M': return '1000'
        if fmt == '%Y%m%d%H%M': return '209912311000'
        return '20991231'

    # Case 1: 가격이 올랐을 때 (최고가 10,000 -> 11,000원 갱신)
    mock_get_price.return_value = 11000
    with patch('modules.reserved_order_monitor.datetime') as mock_dt:
        mock_dt.now.return_value.strftime.side_effect = _mock_strftime
        monitor._check_orders()
    mock_db.update_reserved_order_highest.assert_called_with(3, 11000)

    # Case 2: 가격이 고점에서 5% 이상 하락했을 때 (11,000원에서 10,400원으로 하락)
    mock_db.get_pending_reserved_orders.return_value = [
        {'id': 3, 'code': '005930', 'name': '삼성전자', 'condition_type': 'TRAILING_SELL', 'target_price': 5.0, 'order_type': 'sell', 'market': 'KR', 'highest_price': 11000, 'qty': 10, 'order_price': 0}
    ]
    mock_get_price.return_value = 10400

    with patch('modules.reserved_order_monitor.datetime') as mock_dt:
        mock_dt.now.return_value.strftime.side_effect = _mock_strftime
        with patch.object(monitor, '_execute_order') as mock_exec:
            monitor._check_orders()
            mock_exec.assert_called_once()

@patch('modules.auto_trade.api.get_domestic_balance')
@patch('modules.auto_trade.db_manager.db')
def test_simulation_conclusions_by_balance(mock_db, mock_dom_bal):
    """ConclusionMonitor: 모의투자 잔고 기반 API 체결 누락 보정 테스트"""
    trader = AutoTrader()
    trader.order_manager.pending_orders = {'005930': {'ODNO123': 'ORDER_SENT'}}
    
    monitor = ConclusionMonitor()
    
    # 잔고에 수량이 10주 입고된 것으로 모킹
    mock_dom_bal.return_value = ([{'pdno': '005930', 'hldg_qty': '10'}], None)
    mock_db.get_trade_by_odno.return_value = {'type': 'buy', 'qty': '10', 'price': '70000', 'code': '005930', 'name': '삼성전자'}
    
    with patch.object(monitor, '_handle_simulation_fill') as mock_handle_fill:
        monitor._check_simulation_conclusions_by_balance('12345678', '01')
        
        # 잔고 수량(10주)이 주문 수량(10주) 이상이므로 체결로 간주하고 핸들러가 호출되어야 함
        mock_handle_fill.assert_called_once()