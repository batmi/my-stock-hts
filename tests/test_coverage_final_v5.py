import pytest
from unittest.mock import patch, MagicMock
import config
from modules import auto_trade, analysis, db_manager
import api
import requests

@pytest.fixture(autouse=True)
def cleanup_db_connection():
    yield
    db_manager.db.close_connection()

# --- AutoTrade Tests ---

@patch('modules.auto_trade.api.get_chart_data')
@patch('modules.auto_trade.api.get_realtime_vol_strength')
@patch('modules.auto_trade.DefaultStrategy.analyze_buy')
def test_analyze_candidates_market_filter(mock_analyze, mock_vol, mock_chart):
    """시장 필터링 동작 테스트"""
    trader = auto_trade.AutoTrader()
    trader.is_running = True
    trader.market_index_status = {"KOSPI": {"is_healthy": False, "current": 2000}}
    
    config.USE_MARKET_FILTER = True
    config.session.stock_data = {"stocks_kr": [{"code": "005930", "name": "Samsung"}]}
    
    # Mocking _get_stock_market_type to return KOSPI
    with patch.object(trader, '_get_stock_market_type', return_value="KOSPI"):
        candidates = trader._analyze_candidates([{"code": "005930", "name": "Samsung"}], set(), {})
        
    assert len(candidates) == 0
    assert trader.skipped_by_market_filter_count == 1

@patch('modules.auto_trade.api.get_chart_data')
@patch('modules.auto_trade.api.get_realtime_vol_strength')
@patch('modules.auto_trade.DefaultStrategy.analyze_buy')
def test_analyze_candidates_holding_skip(mock_analyze, mock_vol, mock_chart):
    """보유 종목 스킵 테스트"""
    trader = auto_trade.AutoTrader()
    trader.is_running = True
    
    targets = [{"code": "005930", "name": "Samsung"}]
    holding_codes = {"005930"}
    
    candidates = trader._analyze_candidates(targets, holding_codes, {})
    assert len(candidates) == 0

@patch('modules.auto_trade.api.fetch_buyable_quantity')
@patch('modules.auto_trade.api.place_order')
def test_execute_buy_orders_last_slot(mock_place, mock_qty):
    """마지막 슬롯 전액 투자 테스트"""
    trader = auto_trade.AutoTrader()
    trader.is_running = True
    
    candidates = [{'code': '005930', 'name': 'Samsung', 'price': 10000, 'score': 9.0, 'rsi': 50, 'adx': 30, 'cci': 100}]
    avail_cash = 1000000
    
    mock_qty.return_value = 100
    mock_place.return_value = {'rt_cd': '0', 'output': {'ODNO': '12345'}}
    
    # Config: Volatility Targeting OFF, Risk Per Trade 0 -> Full allocation, Slippage 0
    with patch('config.USE_VOLATILITY_TARGETING', False), \
         patch('config.SYSTEM_RISK_PER_TRADE', 0), \
         patch('config.SLIPPAGE_RATE', 0):
        with patch('modules.auto_trade.db_manager.db.insert_trade'), \
             patch('modules.auto_trade.api.send_telegram_message'):
            # max_holdings=5, current=4 -> remaining=1 (Last slot)
            trader._execute_buy_orders(candidates, avail_cash, 0.1, 4, 5)
            
        # Should try to buy with full cash (approx)
        # 1,000,000 / 10,000 = 100 shares
        args, _ = mock_place.call_args
        assert int(args[3]) == 100 # qty

# --- Analysis Tests ---

def test_calculate_score_full_trend():
    """모든 추세 조건 만족 시 점수 계산"""
    # EMA 정배열, MACD 골든, SAR 상승, RSI 강세, ADX 강세, OBV 상승
    score, details = analysis.calculate_score(
        price=10000, ema20=9000, ema60=8000, ema120=7000,
        sar=9000, rsi=60, adx=30, cci=150, obv_trend=True,
        macd=50, macd_signal=40
    )
    # 4.0(Trend) + 2.5(Mom) + 1.5(Str) + 2.0(Syn) = 10.0
    assert score >= 9.5 # Floating point margin

def test_classify_stock_state_caution_conditions():
    """주의 상태의 다양한 조건 테스트"""
    # 1. 60일선 이탈
    s1, _, _ = analysis.classify_stock_state(9500, 10000, 10000, 9000, 9000, 50, 50, 20, 0, True)
    assert s1 == "주의"
    
    # 2. SAR 매도
    s2, _, _ = analysis.classify_stock_state(10000, 9000, 8000, 7000, 11000, 50, 50, 20, 0, True)
    assert s2 == "주의"
    
    # 3. RSI 과열
    s3, _, _ = analysis.classify_stock_state(10000, 9000, 8000, 7000, 9000, 85, 80, 20, 0, True)
    assert s3 == "주의"
    
    # 4. RSI 침체
    s4, _, _ = analysis.classify_stock_state(10000, 9000, 8000, 7000, 9000, 25, 30, 20, 0, True)
    assert s4 == "주의"

# --- API Tests ---
@patch('requests.Session.request')
@patch('api.get_current_token', return_value="test_token")
def test_call_api_network_error_retry(mock_token, mock_req):
    """네트워크 에러 시 재시도 로직 테스트 (ThrottledSession)"""
    # 1. ConnectionError -> 2. Success
    mock_req.side_effect = [requests.exceptions.ConnectionError("Fail"), MagicMock(status_code=200, json=lambda: {'rt_cd': '0'})]
    
    with patch('time.sleep'):
        # api.call_api는 자체적으로 광범위한 예외 처리를 가지고 있어,
        # ThrottledSession 내부의 재시도 로직을 직접 테스트하기 어렵습니다.
        # 따라서 재시도 로직이 포함된 ThrottledSession.request를 직접 호출하여 테스트합니다.
        res = api.session.request("GET", "https://test.com/test", retries=1)

        # 성공적인 응답(MagicMock)이 반환되었는지 확인
        assert res.status_code == 200
        assert res.json()['rt_cd'] == '0'
        assert mock_req.call_count == 2