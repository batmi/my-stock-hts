import pytest
from unittest.mock import patch, MagicMock
from modules import analysis, trading, market
import config
import pandas as pd
from datetime import datetime, timezone

# --- analysis.py coverage ---
@patch('pandas.ExcelWriter')
@patch('rich.prompt.Prompt.ask', return_value='y')
@patch('modules.analysis._get_master_stock_list')
@patch('modules.analysis._analyze_stock_worker')
@patch('modules.analysis.api.get_current_price_data')
def test_save_all_market_analysis_flow(mock_cp, mock_worker, mock_master, mock_ask, mock_writer):
    """전체 시장 종목 엑셀 저장, 셀 렌더링 커버리지"""
    mock_master.return_value = [{'code': '005930', 'name': '삼성전자'}]
    
    # 워커 응답 모킹 (모든 필드 채움)
    mock_worker.return_value = {
        'code': '005930', 'name': '삼성전자', 'price': 60000, 'w52_pos': 50.0,
        'score': 8.5, 'state': '매수', 'state_reason': '이유',
        'rsi': 50.0, 'adx': 20.0, 'cci': 100.0, 'psar': 50000,
        'macd': 10, 'macd_signal': 5, 'obv_trend': True, 'vol_strength': 100.0,
        'is_custom_rule': False
    }
    
    mock_cp.return_value = {'rt_cd': '0', 'output': {'bstp_kor_isnm': '전기전자'}}
    
    with patch('config.console.print'):
        analysis.save_all_market_analysis()
        
    assert mock_writer.call_count > 0

@patch('rich.prompt.Prompt.ask')
def test_get_analysis_params_edge_cases(mock_ask):
    """파라미터 입력 메뉴 예외 케이스 처리 (취소, 잘못된 타입)"""
    # 숫자가 아닌 값 입력 -> 기본값 유지됨
    # b 입력 -> 취소
    mock_ask.side_effect = ["invalid", "b"]
    
    with patch('config.console.print'):
        res = analysis.get_analysis_params()
        assert res is None

# --- trading.py coverage ---
@patch('rich.prompt.Prompt.ask')
@patch('modules.trading.show_open_orders')
@patch('modules.trading.api.revise_cancel_order')
def test_modify_order_cancel_overseas(mock_revise, mock_show, mock_ask):
    """해외 주식 취소 주문 분기 테스트"""
    mock_show.return_value = [{
        'odno': '12345', 'pdno': 'AAPL', 'prdt_name': 'Apple', 
        'nccs_qty': '10', '_origin': 'US', 'ovrs_excg_cd': 'NAS', 'sll_buy_dvsn_cd': '01'
    }]
    
    # 주문선택(1) -> 취소(2) -> 수량(10) -> 확인(y)
    mock_ask.side_effect = ["1", "2", "10", "y"]
    mock_revise.return_value = {'rt_cd': '0', 'output': {'ODNO': '54321'}}
    
    with patch('config.console.print'):
        trading.modify_order()
        
    mock_revise.assert_called_once()
    args, kwargs = mock_revise.call_args
    assert args[0] == "overseas"
    assert args[1] == "cancel"

@patch('rich.prompt.Prompt.ask')
@patch('modules.trading.show_open_orders')
@patch('modules.trading.api.revise_cancel_order')
def test_modify_order_error_40330000(mock_revise, mock_show, mock_ask):
    """정정/취소 주문 시 KIS 40330000 (기체결/기취소) 에러 처리 로직 커버리지"""
    mock_show.return_value = [{
        'odno': '12345', 'pdno': '005930', 'prdt_name': '삼성전자', 
        'rmn_qty': '10', '_origin': 'KR', 'sll_buy_dvsn_cd': '02'
    }]
    
    # 1 -> 2(취소) -> 0(전량) -> y
    mock_ask.side_effect = ["1", "2", "0", "y"]
    
    # 이미 체결/취소된 경우 에러 응답
    mock_revise.return_value = {'rt_cd': '1', 'msg_cd': '40330000', 'msg1': '기체결/기취소'}
    config.session.is_simulation = True
    
    with patch('config.console.print') as mock_print, \
         patch('modules.db_manager.db.insert_trade') as mock_insert:
        trading.modify_order()
        
        # 40330000 일 때 안내 메시지가 출력되고 DB 정리(더미 이력 생성)를 시도해야 함
        assert any("이미 체결되었거나 취소된 주문" in str(c) for c in mock_print.call_args_list)
        mock_insert.assert_called()

# --- market.py coverage ---
@patch('modules.market.api.get_yf_fast_info')
@patch('modules.market.analysis.get_domestic_index_data')
def test_process_index_worker_futures_proxy(mock_dom, mock_fi):
    """해외 지수 마이크로 캐시를 통한 미국채 금리 추정(선물 적용) 분기 테스트"""
    mock_dom.return_value = None
    
    # 선물 fast_info
    def fast_info_side_effect(ticker):
        if ticker == "ZF=F": # 5년물 선물
            return {'last_price': 100.0, 'regular_market_previous_close': 99.0}
        return {'last_price': 4.0, 'regular_market_previous_close': 3.9}
        
    mock_fi.side_effect = fast_info_side_effect
    df_empty = pd.DataFrame()
    
    with patch('modules.market.datetime') as mock_dt:
        mock_dt.now.return_value = datetime(2023, 1, 1, 10, 0, tzinfo=timezone.utc)
        
        res = market._process_index_worker("미국채 5년물 금리", "^FVX", df_empty, df_empty)
        assert res['status'] == 'success'
        assert "선물적용" in res['row_data'][0]