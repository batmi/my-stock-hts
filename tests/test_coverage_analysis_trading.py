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

# --- market.py coverage ---
@patch('modules.market.analysis.get_us_treasury_spot_data', return_value=None)
@patch('modules.market.api.get_yf_fast_info')
@patch('modules.market.analysis.get_domestic_index_data')
def test_process_index_worker_futures_proxy(mock_dom, mock_fi, mock_treasury_spot):
    """해외 지수 마이크로 캐시를 통한 미국채 금리 추정(선물 적용) 분기 테스트
    (미국채 현물(TVC) 조회는 실패로 모킹해 선물 프록시 폴백 분기를 검증)"""
    mock_dom.return_value = None
    
    # 선물 fast_info
    def fast_info_side_effect(ticker):
        if ticker == "ZF=F": # 5년물 선물
            return {'last_price': 100.0, 'regular_market_previous_close': 99.0}
        return {'last_price': 4.0, 'regular_market_previous_close': 3.9}
        
    mock_fi.side_effect = fast_info_side_effect
    df_empty = pd.DataFrame({'close': [100.0]*60, 'open': [100.0]*60, 'high': [100.0]*60, 'low': [100.0]*60, 'volume': [1000]*60}, index=pd.date_range('2023-01-01', periods=60))
    
    with patch('modules.market.datetime') as mock_dt:
        mock_dt.now.return_value = datetime(2023, 1, 1, 10, 0, tzinfo=timezone.utc)
        
        res = market._process_index_worker("미국채 5년물 금리", "^FVX", df_empty, df_empty)
        assert res['status'] == 'success'
        assert "(F)" in res['row_data'][0]


@patch('modules.market.api.get_yf_fast_info', return_value=None)
@patch('modules.market.analysis.get_domestic_index_data')
def test_process_index_worker_domestic_trailing_nan_close(mock_dom, mock_fi):
    """토스 코스피/코스닥: yfinance 최신행 close=NaN이어도 마지막 유효 종가로 지수·등락률 표시."""
    n = 130
    closes = [6700.0 + i for i in range(n)]
    closes[-1] = float('nan')  # yfinance ^KS11 최신 거래일 close 미집계(NaN) 재현
    df = pd.DataFrame({
        'date': pd.date_range('2026-01-01', periods=n),
        'open': [6700.0 + i for i in range(n)],
        'high': [6700.0 + i for i in range(n)],
        'low': [6700.0 + i for i in range(n)],
        'close': closes,
        'volume': [1000] * n,
    })
    df.attrs['source'] = 'YFINANCE'
    mock_dom.return_value = df

    res = market._process_index_worker("코스피", "^KS11", pd.DataFrame(), pd.DataFrame())
    assert res['status'] == 'success'
    # 지수/등락폭이 '-'가 아니라 마지막 유효 종가(6828)로 채워진다
    assert '-[/]' not in res['row_data'][1]
    assert '6,828' in res['row_data'][1]
    # 등락률은 마지막 유효 종가(6828) vs 직전(6827) 기준으로 산출된다(0%/'-' 아님)
    assert '+0.01%' in res['row_data'][2]


@patch('modules.market.api.get_yf_fast_info', return_value=None)
@patch('modules.market.analysis.get_domestic_index_data')
def test_process_index_worker_obv_rendered_when_volume_present(mock_dom, mock_fi):
    """지수 df에 거래량이 있으면(yfinance 보강) OBV 컬럼이 '-'가 아니라 값으로 렌더된다."""
    n = 130
    df = pd.DataFrame({
        'date': pd.date_range('2026-01-01', periods=n),
        'open': [6700.0 + i for i in range(n)],
        'high': [6700.0 + i for i in range(n)],
        'low': [6700.0 + i for i in range(n)],
        'close': [6700.0 + i for i in range(n)],
        'volume': [10000 + i for i in range(n)],  # tvDatafeed+yfinance 거래량 보강 재현
    })
    df.attrs['source'] = 'TVDATAFEED'
    mock_dom.return_value = df

    res = market._process_index_worker("코스피", "^KS11", pd.DataFrame(), pd.DataFrame())
    assert res['status'] == 'success'
    # row_data[12] = OBV 컬럼. 거래량이 있으므로 '-'가 아닌 값(예: '..K'/'..M')이어야 한다.
    obv_cell = res['row_data'][12]
    assert '-[/dim]' not in obv_cell and obv_cell.strip() not in ('-', '[dim]-[/dim]')


@patch('modules.market.api.get_yf_fast_info', return_value=None)
@patch('modules.market.analysis.get_domestic_index_data')
def test_process_index_worker_obv_dash_when_volume_zero(mock_dom, mock_fi):
    """거래량이 0(tvDatafeed 단독, 보강 실패)이면 OBV는 '-'로 표시된다(데이터 한계)."""
    n = 130
    df = pd.DataFrame({
        'date': pd.date_range('2026-01-01', periods=n),
        'open': [6700.0 + i for i in range(n)],
        'high': [6700.0 + i for i in range(n)],
        'low': [6700.0 + i for i in range(n)],
        'close': [6700.0 + i for i in range(n)],
        'volume': [0] * n,
    })
    df.attrs['source'] = 'TVDATAFEED'
    mock_dom.return_value = df

    res = market._process_index_worker("코스닥150", "^KQ150", pd.DataFrame(), pd.DataFrame())
    assert res['status'] == 'success'
    assert '-' in res['row_data'][12]  # OBV '-'