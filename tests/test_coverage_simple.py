import pytest
from unittest.mock import patch, MagicMock, mock_open
import pandas as pd
import os
import json
from modules import manage, analysis, auto_trade, market, settings
import api
from core import utils
import config

# --- modules/manage.py ---
def test_show_extended_info_domestic():
    """국내 주식 상세 정보 출력 테스트"""
    code = "005930"
    basic_output = {
        "rprs_mrkt_kor_name": "KOSPI",
        "stck_prpr": "60000",
        "prdy_vrss": "1000",
        "prdy_ctrt": "1.5",
        "acml_vol": "1000000",
        "per": "10.5",
        "pbr": "1.2",
        "d250_hgpr": "80000",
        "d250_hgpr_date": "20230101"
    }
    
    # Mock get_chart_data to return empty or valid df
    with patch('modules.manage.api.get_chart_data') as mock_chart:
        mock_chart.return_value = pd.DataFrame({
            'date': pd.date_range(start='20230101', periods=10),
            'close': [60000]*10,
            'open': [60000]*10, 'high': [61000]*10, 'low': [59000]*10, 'volume': [1000]*10
        })
        with patch('config.console.print') as mock_print:
            manage.show_extended_info(code, False, basic_output)
            assert mock_print.call_count > 0

def test_show_extended_info_overseas():
    """해외 주식 상세 정보 출력 테스트"""
    code = "AAPL"
    # Mock fetch_overseas_detail_price
    with patch('modules.manage.api.fetch_overseas_detail_price') as mock_detail:
        mock_detail.return_value = {
            "rsym": "AAPL", "last": "150.00", "diff": "2.00", "rate": "1.5",
            "tvol": "5000000", "perx": "25.0", "pbrx": "10.0", "h52p": "180.00"
        }
        with patch('modules.manage.api.get_chart_data') as mock_chart:
            mock_chart.return_value = pd.DataFrame({
                'date': pd.date_range(start='20230101', periods=10),
                'close': [150.00]*10,
                'open': [150.00]*10, 'high': [155.00]*10, 'low': [145.00]*10, 'volume': [1000]*10
            })
            with patch('config.console.print') as mock_print:
                manage.show_extended_info(code, True)
                assert mock_print.call_count > 0

# --- modules/analysis.py ---
@patch('os.makedirs')
@patch('urllib.request.urlretrieve')
@patch('zipfile.ZipFile')
@patch('os.path.exists')
@patch('os.path.getsize')
@patch('builtins.open', new_callable=mock_open)
def test_get_master_stock_list(mock_file, mock_getsize, mock_exists, mock_zip, mock_retrieve, mock_makedirs):
    """마스터 파일 다운로드 및 파싱 테스트"""
    mock_exists.return_value = False # Force download
    mock_getsize.return_value = 100
    
    # Mock zip extraction
    mock_zip_instance = MagicMock()
    mock_zip.return_value.__enter__.return_value = mock_zip_instance
    
    # Mock file content (CP949 encoded)
    # Format: Code(9) ... Name(40) ...
    line_data = b'005930   ' + b' ' * 12 + '삼성전자'.encode('cp949') + b' ' * 30
    mock_file.return_value.__enter__.return_value = [line_data]
    
    result = analysis._get_master_stock_list("KOSPI")
    
    assert len(result) == 1
    assert result[0]['code'] == '005930'
    assert result[0]['name'] == '삼성전자'

@patch('modules.analysis.api.get_chart_data')
@patch('modules.analysis.indicators.calculate_indicators')
@patch('modules.analysis.classify_stock_state')
def test_analyze_stock_worker_insufficient_data(mock_classify, mock_calc, mock_chart):
    """데이터 부족으로 분석 불가 시 None 반환 테스트"""
    mock_chart.return_value = pd.DataFrame({'close': [10000]})
    mock_calc.return_value = {}
    mock_classify.return_value = ("-", "[dim]", "데이터 부족")
    
    stock = {'code': '005930', 'name': 'Samsung'}
    result = analysis._analyze_stock_worker(stock)
    assert result is not None
    assert 'error' in result

# --- modules/auto_trade.py ---
def test_get_stock_market_type_cached():
    """시장 구분 캐시 테스트"""
    trader = auto_trade.AutoTrader()
    trader.stock_market_map['005930'] = 'KOSPI'
    
    assert trader._get_stock_market_type('005930') == 'KOSPI'

@patch('modules.auto_trade.api.get_current_price_data')
def test_get_stock_market_type_api(mock_api):
    """시장 구분 API 조회 테스트"""
    trader = auto_trade.AutoTrader()
    if '000660' in trader.stock_market_map:
        del trader.stock_market_map['000660']
        
    mock_api.return_value = {
        'rt_cd': '0',
        'output': {'rprs_mrkt_kor_name': '유가증권'}
    }
    
    assert trader._get_stock_market_type('000660') == 'KOSPI'
    assert trader.stock_market_map['000660'] == 'KOSPI'

def test_check_buy_conditions_low_cash():
    """예수금 부족 시 매수 중단 테스트"""
    trader = auto_trade.AutoTrader()
    trader.is_running = True
    trader.consecutive_errors = 0 # 상태 초기화
    
    # 매수 대상 종목이 있어야 로직이 진행됨
    config.session.stock_data = {"stocks_kr": [{"code": "005930", "name": "Samsung"}]}
    
    holdings = []
    deposit_res = {'d2_deposit': 500} # Less than 1000
    
    with patch.object(trader, 'log') as mock_log:
        trader._check_buy_conditions(holdings, deposit_res)
        # Should log "매수 스킵: 예수금 부족"
        assert any("예수금 부족" in str(call) for call in mock_log.call_args_list)

# --- api.py ---
@patch('api.get_current_price_data')
def test_get_current_price_fail(mock_data):
    """현재가 조회 실패 시 0 반환 테스트"""
    mock_data.return_value = {'rt_cd': '1'}
    assert api.get_current_price("005930", False) == 0

# --- utils.py ---
def test_get_tr_id_missing():
    """TR_ID 설정 누락 시 빈 문자열 반환 테스트"""
    assert utils.get_tr_id("invalid", "category", "action") == ""

# --- modules/settings.py ---
@patch('rich.prompt.Prompt.ask')
def test_edit_config_table_bool_toggle(mock_ask):
    """설정 변경 테이블 - 불리언 토글 테스트"""
    # 1번 선택 -> y(변경) -> q(종료)
    mock_ask.side_effect = ["1", "y", "q"]
    
    test_config = {"VALUE": True}
    
    items = [
        {"desc": "테스트 항목", "help": "설명", "name": "VALUE", "type": "bool",
         "get": lambda: test_config["VALUE"], "set": lambda v: test_config.update({"VALUE": v})}
    ]
    
    with patch('config.console.print'):
        settings._edit_config_table("Test Title", items)
        
    assert test_config["VALUE"] is False