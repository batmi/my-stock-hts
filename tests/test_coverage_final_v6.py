# tests/test_coverage_final_v6.py
import pytest
from unittest.mock import patch, MagicMock, ANY
import pandas as pd
import api
import config
import context
from modules import analysis, auto_trade, market, account, db_manager, manage, backtest, settings
import utils
import json
import os
import sqlite3
from datetime import datetime

@pytest.fixture(autouse=True)
def cleanup_db_connection():
    yield
    db_manager.db.close_connection()

# --- utils.py coverage ---
def test_get_tick_size_comprehensive():
    """호가 단위 테스트 (전 구간)"""
    # 코스피/코스닥 호가 단위
    assert utils.get_tick_size(1000, False) == 1
    assert utils.get_tick_size(4000, False) == 5
    assert utils.get_tick_size(15000, False) == 10
    assert utils.get_tick_size(45000, False) == 50
    assert utils.get_tick_size(150000, False) == 100
    assert utils.get_tick_size(450000, False) == 500
    assert utils.get_tick_size(1000000, False) == 1000
    
    # 해외
    assert utils.get_tick_size(100, True) == 0.01
    
    # 잘못된 입력
    assert utils.get_tick_size("invalid", False) == 0

def test_adjust_to_tick_comprehensive():
    """호가 보정 테스트"""
    assert utils.adjust_to_tick(1234, False) == 1234
    assert utils.adjust_to_tick(1234.5, False) == 1234
    assert utils.adjust_to_tick(4567, False) == 4565 # 5단위
    assert utils.adjust_to_tick(150.1234, True) == 150.12

# --- api.py coverage ---
@patch('api.session.get')
def test_get_stock_name_domestic_parsing(mock_get):
    """국내 종목명 파싱 테스트"""
    mock_resp = MagicMock()
    mock_resp.text = '<meta property="og:title" content="삼성전자 : 네이버 금융">'
    mock_get.return_value = mock_resp
    assert api.get_stock_name_by_code("005930", False) == "삼성전자"

    mock_resp.text = '<meta property="og:title" content="SK하이닉스(000660) : 네이버 금융">'
    assert api.get_stock_name_by_code("000660", False) == "SK하이닉스"

@patch('api.yf.Ticker')
def test_get_stock_name_overseas_yf(mock_ticker):
    """해외 종목명 yfinance 조회 테스트"""
    mock_inst = MagicMock()
    mock_inst.info = {'longName': 'Apple Inc.'}
    mock_ticker.return_value = mock_inst
    assert api.get_stock_name_by_code("AAPL", True) == "Apple Inc."
    
    mock_inst.info = {'shortName': 'Tesla'} # longName 없을 때
    assert api.get_stock_name_by_code("TSLA", True) == "Tesla"

@patch('api.call_api')
def test_fetch_domestic_period_price_empty(mock_call):
    """기간별 시세 조회 빈 응답 처리"""
    mock_call.return_value = {'rt_cd': '0', 'output2': []}
    assert api.fetch_domestic_period_price("005930") == []

@patch('api.call_api')
def test_fetch_overseas_period_price_fail(mock_call):
    """해외 기간별 시세 조회 실패 처리"""
    mock_call.return_value = {'rt_cd': '1'}
    assert api.fetch_overseas_period_price("AAPL", "NAS") is None

@patch('api.session.post')
def test_get_auto_access_token_shared_key(mock_post):
    """자동매매 키가 실전 키와 같을 때 토큰 공유 테스트"""
    config.session.auto_app_key = "SAME_KEY"
    config.session.real_app_key = "SAME_KEY"
    
    # get_real_access_token이 호출되어야 함
    with patch('api.get_real_access_token') as mock_real:
        mock_real.return_value = "REAL_TOKEN"
        token = api.get_auto_access_token()
        assert token == "REAL_TOKEN"
        mock_real.assert_called()

@patch('api.session.post')
def test_get_auto_access_token_separate_key(mock_post):
    """자동매매 키가 다를 때 별도 발급 테스트"""
    config.session.auto_app_key = "AUTO_KEY"
    config.session.real_app_key = "REAL_KEY"
    
    mock_post.return_value.status_code = 200
    mock_post.return_value.json.return_value = {'access_token': 'AUTO_TOKEN', 'access_token_token_expired': '2099-01-01 00:00:00'}
    
    token = api._get_auto_access_token_internal(force_refresh=True)
    assert token == "AUTO_TOKEN"

# --- modules/analysis.py coverage ---
def test_calculate_score_defaults():
    """점수 계산 기본값 처리"""
    # 필수 인자만 전달, 나머지 None
    score, details = analysis.calculate_score(10000, None, None, None, None, None, None, None, False)
    assert score == 0
    assert len(details) == 0

def test_classify_stock_state_macd_dead():
    """MACD 데드크로스 상태 분류"""
    # MACD < Signal -> 주의
    state, color, reason = analysis.classify_stock_state(
        10000, 10000, 10000, 10000, 9000, 50, 50, 20, 0, True, macd=10, macd_signal=20
    )
    assert state == "주의"
    assert "MACD 데드크로스" in reason

@patch('modules.analysis.api.get_domestic_index_chart')
@patch('modules.analysis.api.get_chart_data')
def test_get_market_regime_kosdaq(mock_yf, mock_kis):
    """코스닥 시장 국면 판단"""
    # KIS API Fail -> yfinance Fallback
    mock_kis.return_value = pd.DataFrame()
    
    # yfinance Data
    dates = pd.date_range(end=datetime.now(), periods=60)
    df = pd.DataFrame({'close': [1000]*60, 'high': [1010]*60, 'low': [990]*60, 'volume': [1000]*60}, index=dates)
    mock_yf.return_value = df
    
    regime, adj = analysis.get_market_regime("KOSDAQ")
    assert regime in ["Bull", "Bear", "Sideways"]

@patch('modules.analysis.api.get_chart_data')
@patch('modules.analysis.api.get_realtime_vol_strength')
def test_analyze_stock_worker_custom_rule(mock_vol, mock_chart):
    """개별 룰이 적용된 분석 워커 테스트"""
    # [수정] 지표 계산(EMA120 등)을 위해 충분한 데이터 제공 (30 -> 150)
    mock_chart.return_value = pd.DataFrame({
        'close': [10000]*150, 'high': [10000]*150, 
        'low': [10000]*150, 'open': [10000]*150, 
        'volume': [1000]*150
    })
    mock_vol.return_value = 100.0
    
    stock = {'code': '005930', 'name': 'Samsung', 'is_custom_rule': True}
    params = {'BUY_VOL_STRENGTH': 200.0} # 높은 체결강도 요구
    
    # 체결강도(100) < 요구(200) -> 관망 상태 예상
    res = analysis._analyze_stock_worker(stock, params)
    
    assert res is not None
    assert res['is_custom_rule'] is True
    # state_reason에 체결강도 미달 포함 여부 확인
    if res['state_reason']:
        assert "체결강도 미달" in res['state_reason'] or res['state'] == "관망"

# --- modules/auto_trade.py coverage ---
def test_risk_manager_volatility_scaling():
    """변동성 타겟팅 스케일링 계산"""
    trader = MagicMock()
    trader.initial_asset = 10_000_000
    rm = auto_trade.RiskManager(trader)
    
    # ATR이 매우 낮음 -> 비중 확대 (Max 2.0배 제한 확인)
    config.USE_VOLATILITY_TARGETING = True
    config.TARGET_VOLATILITY = 0.2
    config.VOLATILITY_SCALING_MAX = 2.0
    
    # Price 10000, ATR 10 -> Volatility approx 1.6% -> Scale very high -> Capped at 2.0
    amt = rm.allocate_budget(10_000_000, 0.1, atr=10, current_price=10000)
    assert amt == 2_000_000 # 100만 * 2.0

def test_order_manager_register():
    """수동 주문 등록 테스트"""
    trader = MagicMock()
    om = auto_trade.OrderManager(trader)
    om.register_manual_order("005930", "12345")
    assert om.is_pending("005930")
    assert om.pending_orders["005930"]["12345"] == auto_trade.OrderStatus.ORDER_SENT

@patch('sqlite3.connect')
def test_enrich_rules_with_weights(mock_connect):
    """룰 가중치 병합 테스트"""
    mock_conn = MagicMock()
    mock_cursor = MagicMock()
    mock_connect.return_value.__enter__.return_value = mock_conn
    mock_conn.cursor.return_value = mock_cursor
    
    # Mock DB response
    mock_cursor.fetchall.return_value = [
        {'code': '005930', 'weights': '{"TREND": 5.0}'}
    ]
    
    rules = [{'code': '005930', 'name': 'Samsung'}]
    enriched = auto_trade._enrich_rules_with_weights(rules)
    
    assert enriched[0]['weights']['TREND'] == 5.0

@patch('modules.auto_trade.api.get_domestic_balance')
def test_get_holdings_message(mock_balance):
    """보유 종목 메시지 생성 테스트"""
    trader = auto_trade.AutoTrader()
    
    # 1. 보유 종목 있음
    mock_balance.return_value = ([{'prdt_name': 'Samsung', 'hldg_qty': '10', 'prpr': '60000', 'evlu_amt': '600000', 'evlu_pfls_rt': '10.0', 'evlu_pfls_amt': '50000'}], None)
    msg = trader._get_holdings_message("12345678")
    assert "Samsung" in msg
    assert "10주" in msg
    
    # 2. 보유 종목 없음
    mock_balance.return_value = ([], None)
    msg_empty = trader._get_holdings_message("12345678")
    assert "없음" in msg_empty

def test_get_prev_rsi():
    """전일 RSI 계산 테스트"""
    trader = auto_trade.AutoTrader()
    
    # 1. 데이터 부족
    assert trader._get_prev_rsi(pd.DataFrame()) is None
    
    # 2. 데이터 충분
    df = pd.DataFrame({'close': [100 + i for i in range(20)]})
    rsi = trader._get_prev_rsi(df)
    assert rsi is not None

@patch('modules.auto_trade.api.check_server_health')
@patch('modules.auto_trade.api.send_telegram_message')
def test_wait_for_server_recovery_loop(mock_tg, mock_health):
    """서버 복구 대기 루프 테스트"""
    trader = auto_trade.AutoTrader()
    trader.is_running = True
    
    # False -> True
    mock_health.side_effect = [False, True]
    
    with patch('time.sleep'):
        trader._wait_for_server_recovery()
        
    assert mock_tg.call_count >= 1

# --- modules/manage.py coverage ---
@patch('modules.manage.api.get_current_price_data')
@patch('modules.manage.api.get_chart_data')
def test_show_extended_info_domestic_full(mock_chart, mock_price):
    """국내 주식 상세 정보 출력 (전체 데이터)"""
    mock_price.return_value = {'rt_cd': '0', 'output': {'stck_prpr': '10000'}}
    # [수정] 출력에 필요한 컬럼(open, high, low, volume) 추가
    mock_chart.return_value = pd.DataFrame({
        'close': [10000]*20, 
        'date': pd.date_range(end=datetime.now(), periods=20),
        'open': [10000]*20, 'high': [10000]*20,
        'low': [10000]*20, 'volume': [1000]*20
    })
    
    with patch('config.console.print'):
        manage.show_extended_info("005930", False, basic_output={'stck_prpr': '10000'})

@patch('modules.manage.api.fetch_overseas_detail_price')
@patch('modules.manage.api.get_chart_data')
def test_show_extended_info_overseas_full(mock_chart, mock_detail):
    """해외 주식 상세 정보 출력 (전체 데이터)"""
    mock_detail.return_value = {'last': '150.00', 'rsym': 'AAPL'}
    # [수정] 출력에 필요한 컬럼(open, high, low, volume) 추가
    mock_chart.return_value = pd.DataFrame({
        'close': [150]*20, 
        'date': pd.date_range(end=datetime.now(), periods=20),
        'open': [150]*20, 'high': [150]*20,
        'low': [150]*20, 'volume': [1000]*20
    })
    
    with patch('config.console.print'):
        manage.show_extended_info("AAPL", True)

# --- modules/market.py coverage ---
@patch('modules.market.api.fetch_yfinance_data')
@patch('modules.market.yf.Tickers')
def test_show_market_indices_fast_info_fail(mock_tickers, mock_fetch):
    """fast_info 실패 시 DataFrame Fallback 테스트"""
    # fast_info raise Exception
    mock_tickers.return_value.tickers.__getitem__.side_effect = Exception("Fast Info Error")
    
    # DataFrame Data
    dates = pd.date_range(end=datetime.now(), periods=5)
    df = pd.DataFrame(100, index=dates, columns=['Close', 'Open', 'High', 'Low', 'Volume'])
    # MultiIndex Mock
    cols = pd.MultiIndex.from_product([['^KS11'], df.columns])
    data = pd.DataFrame(100, index=dates, columns=cols)
    mock_fetch.return_value = data
    
    with patch('config.console.print'):
        market.show_market_indices()

# --- modules/settings.py coverage ---
@patch('rich.prompt.Prompt.ask')
def test_modify_log_settings(mock_ask):
    """로그 설정 변경 테스트"""
    mock_ask.side_effect = ["1", "DEBUG", "q"]
    with patch('config.setup_logging'):
        settings.modify_log_settings()
        assert config.SCREEN_DEBUG_LEVEL == "DEBUG"

# --- modules/db_manager.py coverage ---
def test_db_update_highest_price_lock():
    """DB 락 발생 시 재시도 로직 테스트 (Mocking)"""
    db = db_manager.DBManager()
    with patch.object(db, '_get_conn') as mock_conn:
        mock_cursor = MagicMock()
        mock_conn.return_value.cursor.return_value = mock_cursor
        
        # First call raises OperationalError (locked), Second call succeeds
        mock_cursor.execute.side_effect = [sqlite3.OperationalError("database is locked"), None]
        
        with patch('time.sleep'):
            db.update_highest_price("005930", 80000)
            
        assert mock_cursor.execute.call_count == 2

def test_db_check_trade_exists_debug():
    """check_trade_exists 디버그 로그 테스트"""
    db = db_manager.DBManager()
    config.SCREEN_DEBUG_LEVEL = "DEBUG"
    
    with patch.object(db, '_get_conn') as mock_conn:
        mock_cursor = MagicMock()
        mock_conn.return_value.cursor.return_value = mock_cursor
        mock_cursor.fetchone.return_value = [1] # Exists
        
        with patch('config.console.print') as mock_print:
            exists = db.check_trade_exists("123", "체결")
            assert exists is True
            assert mock_print.called

    config.SCREEN_DEBUG_LEVEL = "OFF"

# --- modules/backtest.py coverage ---
@patch('modules.backtest.api.fetch_yfinance_data')
def test_get_backtest_data_multi_index(mock_fetch):
    """yfinance 멀티인덱스 컬럼 처리 테스트"""
    # MultiIndex DataFrame Mock (Price, Ticker) structure for group_by='column'
    dates = pd.date_range(end=datetime.now(), periods=5, name='Date')
    # Level 0: Price, Level 1: Ticker
    cols = pd.MultiIndex.from_product([['Close', 'Open', 'High', 'Low', 'Volume'], ['AAPL']])
    df = pd.DataFrame(100, index=dates, columns=cols)
    mock_fetch.return_value = df
    
    # Mock fallback to avoid network call
    with patch('modules.backtest.api.get_chart_data', return_value=pd.DataFrame()):
        res = backtest.get_backtest_data("AAPL", True, 5)
        
    assert res is not None
    assert not res.empty
    assert 'close' in res.columns

# --- modules/auto_trade.py additional ---

@patch('modules.auto_trade.api.fetch_sellable_quantity')
@patch('modules.auto_trade.api.get_chart_data')
@patch('modules.auto_trade.DefaultStrategy.analyze_sell')
@patch('modules.auto_trade.api.send_telegram_message')
@patch('modules.auto_trade.db_manager.db.insert_trade')
@patch('modules.auto_trade.load_restricted_stocks', return_value={})
@patch('modules.auto_trade.db_manager.db.get_highest_price', return_value=0)
@patch('modules.auto_trade.db_manager.db.update_highest_price')
@patch('modules.auto_trade.db_manager.db.delete_trailing_stop')
def test_check_sell_conditions_atr_stop(mock_del, mock_upd, mock_get_high, mock_load, mock_insert, mock_tg, mock_analyze, mock_chart, mock_qty):
    """ATR 손절 로직 테스트"""
    trader = auto_trade.AutoTrader()
    trader.is_running = True
    
    holdings = [{
        'pdno': '005930', 'prdt_name': 'Samsung', 'ord_psbl_qty': '10',
        'evlu_pfls_rt': '-5.0', 'prpr': '60000', 'pchs_avg_pric': '63000',
        'evlu_pfls_amt': '-30000'
    }]
    
    mock_qty.return_value = 10
    mock_chart.return_value = pd.DataFrame({'close': [60000]*20, 'high': [61000]*20, 'low': [59000]*20, 'open': [60000]*20, 'volume': [1000]*20})
    
    # ATR 손절 활성화
    config.SELL_STRATEGY["USE_ATR_STOP"] = True
    
    # Mock DB latest buy trade to have stop_loss_rate
    with patch('modules.auto_trade.db_manager.db.get_latest_buy_trade', return_value={'stop_loss_rate': -4.0}):
        # analyze_sell returns sell due to stop loss
        mock_analyze.return_value = {
            'action': 'sell', 'reason': '손절', 'score': 4.0, 'state': '매도', 'ind': {}
        }
        
        with patch.object(trader.order_manager, 'is_pending', return_value=False):
            with patch.object(trader.order_manager, 'send_order', return_value='12345') as mock_send:
                trader._check_sell_conditions(holdings, is_market_open=True)
                
                mock_send.assert_called()
                # Verify reason contains ATR손절
                args, kwargs = mock_send.call_args
                reason = kwargs.get('reason')
                assert "ATR손절" in reason

def test_check_buy_conditions_low_cash_limit():
    """최소 예수금 미달 테스트"""
    trader = auto_trade.AutoTrader()
    trader.is_running = True
    trader.consecutive_errors = 0
    
    config.session.stock_data = {"stocks_kr": [{"code": "005930", "name": "Samsung"}]}
    
    with patch.object(trader, 'log') as mock_log:
        # Cash 500 < 1000
        trader._check_buy_conditions([], {'d2_deposit': 500}, is_market_open=True)
        assert any("예수금 부족" in str(c) for c in mock_log.call_args_list)

def test_check_buy_conditions_max_holdings_limit():
    """최대 보유 종목 수 제한 테스트"""
    trader = auto_trade.AutoTrader()
    trader.is_running = True
    trader.consecutive_errors = 0
    
    config.session.stock_data = {"stocks_kr": [{"code": "005930", "name": "Samsung"}]}
    config.SYSTEM_MAX_HOLDINGS = 2
    
    # Holdings 2
    holdings = [{'pdno': '1'}, {'pdno': '2'}]
    
    with patch.object(trader, 'log') as mock_log:
        trader._check_buy_conditions(holdings, {'d2_deposit': 1000000}, is_market_open=True)
        assert any("최대 보유 종목 수" in str(c) for c in mock_log.call_args_list)

# --- api.py additional ---

@patch('api.call_api')
def test_get_domestic_balance_fail(mock_call):
    """국내 잔고 조회 실패 테스트"""
    mock_call.return_value = {'rt_cd': '1', 'msg1': 'Error'}
    
    h, s = api.get_domestic_balance("12345678", "01")
    assert h is None
    assert s is None

@patch('api.call_api')
def test_get_deposit_balance_fail(mock_call):
    """예수금 조회 실패 테스트"""
    mock_call.return_value = {'rt_cd': '1', 'msg1': 'Error'}
    
    res = api.get_deposit_balance("12345678", "01")
    assert res is None