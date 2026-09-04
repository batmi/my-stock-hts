import copy

import pytest
from unittest.mock import patch, MagicMock
from modules.auto_trade import AutoTrader
import config
import pandas as pd


@pytest.fixture(autouse=True)
def _restore_config_dicts():
    """이 파일은 config 전역 dict 를 **제자리에서** 고친다(임계값 고정). 되돌리지 않으면
    같은 워커의 뒤 테스트가 오염된 값을 본다.

    실제로 test_exit_parity 가 이것 때문에 깨졌다: BUY_RSI_MAX 70→65 가 남으면
    module 스코프로 미리 계산해 둔 상태(precompute_status)와 그 뒤 실매매 경로가 서로
    다른 임계값을 보게 되어, 경계에 있던 판정 2건이 '보유' ↔ '점수하락'으로 뒤집힌다.
    xdist 분배에 따라 앞뒤 순서가 바뀌므로 증상이 파일 하나 추가만으로 나타났다 사라진다.
    """
    saved_thresholds = copy.deepcopy(config.ANALYSIS_THRESHOLDS)
    saved_sell = copy.deepcopy(config.SELL_STRATEGY)
    yield
    config.ANALYSIS_THRESHOLDS.clear()
    config.ANALYSIS_THRESHOLDS.update(saved_thresholds)
    config.SELL_STRATEGY.clear()
    config.SELL_STRATEGY.update(saved_sell)


@pytest.fixture
def trader():
    t = AutoTrader()
    t.order_manager.pending_orders.clear() # 상태 초기화
    t.consecutive_errors = 0
    return t

@pytest.fixture
def mock_chart_df():
    df = pd.DataFrame({
        'date': pd.date_range(start='2023-01-01', periods=100),
        'close': [10000] * 100,
        'high': [10500] * 100,
        'low': [9500] * 100,
        'open': [10000] * 100,
        'volume': [1000] * 100
    })
    return df

@patch('modules.auto_trade.analysis.classify_stock_state', return_value=("매수", "[red]", "조건 충족"))
@patch('modules.auto_trade.analysis.calculate_score', return_value=(9.0, []))
@patch('time.sleep')
@patch('api.get_investor_trend', return_value=[])
@patch('api.get_current_price_data', return_value={'rt_cd': '0', 'output': {}})
#  실시간 현재가가 없으면 매수는 보류된다(진입가·손절폭·수량이 함께 어긋나므로).
#  이 테스트는 매수 집행 경로를 보는 것이라 정상 시세를 명시한다.
@patch('api.get_current_price', return_value=10000.0)
@patch('modules.auto_trade.analysis.get_market_regime', return_value=("Sideways", 0.0))
@patch('modules.auto_trade.api.prefetch_multiple_current_prices')
@patch('modules.auto_trade.load_restricted_stocks', return_value={})
@patch('api.get_chart_data')
@patch('api.get_realtime_vol_strength')
@patch('api.get_order_book')
@patch('api.fetch_buyable_quantity')
@patch('api.place_order')
@patch('core.indicators.calculate_indicators')
@patch('modules.auto_trade.db_manager.db.get_all_stock_strategies', return_value=[])
def test_check_buy_conditions(mock_get_rules, mock_calc, mock_place, mock_qty, mock_ob, mock_vol, mock_get_chart, mock_restricted, mock_prefetch, mock_regime, mock_price, mock_cp, mock_inv, mock_sleep, mock_score, mock_classify, trader, mock_chart_df):
    """매수 조건 점검 및 주문 실행 테스트"""
    # Setup
    trader.is_running = True
    trader.buy_halted = False
    # [필수] 시장 필터는 fail-closed다 — 지수 상태가 없으면 '판단 불가'로 신규 매수가 보류되므로
    #  매수 경로를 검증하려면 정상(healthy) 지수 상태를 명시적으로 세팅해야 한다.
    trader.market_index_status = {
        "KOSPI": {"is_healthy": True, "unknown": False, "current": 2500.0},
        "KOSDAQ": {"is_healthy": True, "unknown": False, "current": 800.0},
    }
    config.session.stock_data = {"stocks_kr": [{"code": "005930", "name": "Samsung"}]}
    
    # Mocks
    mock_get_chart.return_value = mock_chart_df
    mock_vol.return_value = 150.0
    mock_ob.return_value = {'rt_cd': '0', 'output1': {'total_askp_rsqn': '200', 'total_bidp_rsqn': '100'}}
    mock_qty.return_value = 10
    mock_place.return_value = {'rt_cd': '0', 'output': {'ODNO': '12345'}}
    
    # 테스트 간섭 방지를 위해 명시적 임계값 고정
    config.ANALYSIS_THRESHOLDS["BUY_SCORE"] = 7.0
    config.ANALYSIS_THRESHOLDS["BUY_RSI_MAX"] = 65.0
    config.ANALYSIS_THRESHOLDS["BUY_ASK_BID_RATIO"] = 1.2

    # 매수 신호가 나오도록 지표 설정
    mock_calc.return_value = {
        'ema_5': 10000, 'ema_20': 9000, 'ema_60': 8000, 'ema_120': 7000,
        'rsi': 60, 'adx': 30, 'cci': 100, 'obv': 1000, 'obv_trend': True,
        'psar': 9000, 'macd': 50, 'macd_signal': 40, 'macd_hist': 10, 'prev_macd_hist': 5,
        'atr': 100,
        'plus_di': 30, 'minus_di': 10 # 추가 점수 확보용 DMI
    }
    
    # 테스트 속도를 위해 분석 대상 종목 수를 1개로 제한
    trader.consecutive_errors = 0
    
    # 예수금 Mock
    deposit_res = {'d2_deposit': 1000000}
    
    # 실행
    with patch('modules.auto_trade.db_manager.db.insert_trade'), \
         patch('modules.auto_trade.api.send_telegram_message'):
        trader._check_buy_conditions([], deposit_res, is_market_open=True)
        
    # 주문 함수 호출 확인
    mock_place.assert_called()
    args, _ = mock_place.call_args
    assert args[2] == "005930" # code
    assert args[1] == "buy" # type

@patch('modules.auto_trade.analysis.get_market_regime', return_value=("Sideways", 0.0))
@patch('modules.auto_trade.api.prefetch_multiple_current_prices')
@patch('modules.auto_trade.load_restricted_stocks', return_value={})
@patch('api.get_chart_data')
@patch('api.place_order')
@patch('core.indicators.calculate_indicators')
@patch('api.fetch_sellable_quantity', return_value=10)
@patch('modules.auto_trade.db_manager.db.get_all_stock_strategies', return_value=[])
def test_check_sell_conditions(mock_get_rules, mock_sell_qty, mock_calc, mock_place, mock_get_chart, mock_restricted, mock_prefetch, mock_regime, trader, mock_chart_df):
    """매도 조건 점검 및 주문 실행 테스트"""
    # Setup
    mock_sell_qty.return_value = 10  # 명시적 반환값 설정
    trader.is_running = True
    holdings = [{
        'pdno': '005930', 'prdt_name': 'Samsung', 'ord_psbl_qty': '10',
        'evlu_pfls_rt': '5.0', 'prpr': '10000', 'pchs_avg_pric': '9500',
        'evlu_pfls_amt': '5000'
    }]
    
    # Mocks
    mock_get_chart.return_value = mock_chart_df
    mock_place.return_value = {'rt_cd': '0', 'output': {'ODNO': '12345'}}
    
    # 테스트 간섭 방지를 위해 명시적 임계값 고정 (다른 테스트가 값을 바꿔도 영향받지 않도록 방어)
    config.SELL_STRATEGY["TAKE_PROFIT_RSI"] = 75.0
    config.SELL_STRATEGY["SUPER_TAKE_PROFIT_RSI"] = 85.0

    # 매도 신호(RSI 과열)가 나오도록 지표 설정
    mock_calc.return_value = {
        'ema_5': 10000, 'ema_20': 9000, 'ema_60': 8000, 'ema_120': 7000,
        'rsi': 90, # Overbought (상향된 기준치를 무조건 초과하도록 90으로 설정)
        'adx': 30, 'cci': 100, 'obv': 1000, 'obv_trend': True,
        'psar': 9000, 'macd': 50, 'macd_signal': 40, 'macd_hist': 10,
        'atr': 100,
        'plus_di': 30, 'minus_di': 10
    }
    
    # 실행
    with patch('modules.auto_trade.db_manager.db.insert_trade'), \
         patch('modules.auto_trade.api.send_telegram_message'), \
         patch('modules.auto_trade.db_manager.db.get_highest_price', return_value=0), \
         patch('modules.auto_trade.db_manager.db.update_highest_price'), \
         patch('modules.auto_trade.db_manager.db.delete_trailing_stop'):
        trader._check_sell_conditions(holdings, is_market_open=True)
        
    # 주문 함수 호출 확인
    mock_get_chart.assert_called()
    mock_calc.assert_called()
    mock_sell_qty.assert_called()
    mock_place.assert_called()
    
    args, _ = mock_place.call_args
    assert args[2] == "005930" # code
    assert args[1] == "sell" # type