import pytest
from unittest.mock import patch, MagicMock, ANY
from datetime import datetime
import config
from modules import auto_trade
import api

@pytest.fixture
def reset_autotrader():
    """AutoTrader 싱글톤 인스턴스 초기화"""
    auto_trade.AutoTrader._instance = None
    yield
    auto_trade.AutoTrader._instance = None

@patch('modules.auto_trade.api.place_order')
@patch('modules.auto_trade.api.fetch_buyable_quantity')
@patch('modules.auto_trade.api.get_current_price')
@patch('modules.auto_trade.utils.adjust_to_tick')
def test_buy_slippage(mock_adjust, mock_get_price, mock_fetch_qty, mock_place, reset_autotrader):
    """
    [매수 슬리피지 테스트]
    현재가 10,000원, 슬리피지 0.2% 설정 시
    주문 가격이 10,020원으로 계산되어 전송되는지 확인
    """
    # 설정: 슬리피지 0.2%
    config.SLIPPAGE_RATE = 0.002
    config.SYSTEM_RISK_PER_TRADE = 0 # 리스크 관리 비활성화 (단순화)
    config.USE_VOLATILITY_TARGETING = False # 변동성 타겟팅 비활성화
    
    trader = auto_trade.AutoTrader()
    trader.is_running = True # [수정] 실행 상태 활성화 (로직 진입 조건)
    
    # Mock 설정
    current_price = 10000
    expected_price = 10020 # 10000 * (1 + 0.002)
    
    # adjust_to_tick은 입력값을 그대로 정수로 반환하도록 설정
    mock_adjust.side_effect = lambda x, is_overseas: int(round(x))
    mock_fetch_qty.return_value = 10 # 매수 가능 수량
    mock_place.return_value = {'rt_cd': '0', 'output': {'ODNO': '12345'}}
    
    # 테스트 데이터 (매수 후보)
    candidates = [{
        'code': '005930',
        'name': '삼성전자',
        'price': current_price,
        'score': 9.0,
        'rsi': 50,
        'adx': 30,
        'cci': 100,
        'vol_strength': 120
    }]
    
    avail_cash = 1000000 # 예수금 충분
    invest_ratio = 0.5
    
    # 실행
    trader._execute_buy_orders(candidates, avail_cash, invest_ratio, 0, 5)
    
    # 검증
    mock_place.assert_called_once()
    args, _ = mock_place.call_args
    
    # place_order(market, action, code, qty, price, ord_dvsn)의 5번째 인자(price) 확인
    actual_price = int(args[4])
    assert actual_price == expected_price, f"매수 주문 가격이 슬리피지 적용가({expected_price})와 다릅니다: {actual_price}"

@patch('modules.auto_trade.api.place_order')
@patch('modules.auto_trade.api.fetch_sellable_quantity')
@patch('modules.auto_trade.api.get_chart_data')
@patch('modules.auto_trade.utils.adjust_to_tick')
def test_sell_slippage(mock_adjust, mock_chart, mock_fetch_qty, mock_place, reset_autotrader):
    """
    [매도 슬리피지 테스트]
    현재가 10,000원, 슬리피지 0.2% 설정 시
    주문 가격이 9,980원으로 계산되어 전송되는지 확인
    """
    # 설정: 슬리피지 0.2%
    config.SLIPPAGE_RATE = 0.002
    
    trader = auto_trade.AutoTrader()
    trader.is_running = True # [수정] 실행 상태 활성화 (로직 진입 조건)
    
    # Mock 설정
    current_price = 10000
    expected_price = 9980 # 10000 * (1 - 0.002)
    
    mock_adjust.side_effect = lambda x, is_overseas: int(round(x))
    mock_fetch_qty.return_value = 10 # 매도 가능 수량
    mock_place.return_value = {'rt_cd': '0', 'output': {'ODNO': '67890'}}
    mock_chart.return_value = None 
    
    # Strategy Mocking (무조건 매도 신호 발생)
    trader.strategy = MagicMock()
    trader.strategy.analyze_sell.return_value = {
        'action': 'sell',
        'reason': '테스트 매도',
        'score': 0,
        'state': '매도',
        'ind': {}
    }
    
    # 테스트 데이터 (보유 종목)
    holdings = [{
        'pdno': '005930', 'prdt_name': '삼성전자', 'ord_psbl_qty': '10',
        'evlu_pfls_rt': '5.0', 'prpr': str(current_price), 'pchs_avg_pric': '9500',
        'evlu_pfls_amt': '5000'
    }]
    
    # 실행
    trader._check_sell_conditions(holdings, is_market_open=True)
    
    # 검증
    mock_place.assert_called_once()
    args, _ = mock_place.call_args
    
    actual_price = int(args[4])
    assert actual_price == expected_price, f"매도 주문 가격이 슬리피지 적용가({expected_price})와 다릅니다: {actual_price}"

@patch('modules.auto_trade.api.get_unfilled_orders')
@patch('modules.auto_trade.api.revise_cancel_order')
@patch('modules.auto_trade.datetime')
def test_unfilled_order_cancel(mock_datetime, mock_revise, mock_get_unfilled, reset_autotrader):
    """
    [미체결 주문 취소 테스트]
    취소 대기 시간 120초(2분) 설정 시,
    3분 경과한 주문은 취소하고 1분 경과한 주문은 유지하는지 확인
    """
    config.UNFILLED_ORDER_CANCEL_SECONDS = 120
    trader = auto_trade.AutoTrader()
    
    # 현재 시간 고정: 12시 00분 00초
    now = datetime(2023, 1, 1, 12, 0, 0)
    mock_datetime.now.return_value = now
    mock_datetime.strptime = datetime.strptime
    
    # 미체결 주문 데이터 Mocking
    orders = [
        {'odno': '1001', 'pdno': '005930', 'prdt_name': '삼성전자', 'rmn_qty': '10', 'ord_tmd': '115700'}, # 3분 전 (취소 대상)
        {'odno': '1002', 'pdno': '000660', 'prdt_name': 'SK하이닉스', 'rmn_qty': '5', 'ord_tmd': '115900'}  # 1분 전 (유지 대상)
    ]
    mock_get_unfilled.return_value = orders
    mock_revise.return_value = {'rt_cd': '0', 'msg1': '정상'}
    
    # 실행
    trader.order_manager.manage_unfilled_orders()
    
    # 검증: 주문번호 1001만 취소 요청되었는지 확인
    assert mock_revise.call_count == 1
    args, _ = mock_revise.call_args
    assert args[2] == '1001', "3분 경과한 주문(1001)이 취소되어야 합니다."