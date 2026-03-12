import pytest
from unittest.mock import patch, MagicMock
import pandas as pd
from modules import analysis
import config

@patch('urllib.request.urlretrieve', side_effect=Exception("Download failed"))
def test_get_master_stock_list_download_fail(mock_retrieve):
    """마스터 파일 다운로드 실패 시 예외 발생 테스트"""
    with patch('os.path.exists', return_value=False):
        with patch('os.makedirs'):  # [Fix] FileExistsError 방지
            # 함수 내부에서 예외를 처리하고 빈 리스트를 반환함
            result = analysis._get_master_stock_list("KOSPI")
            assert result == []

@patch('modules.analysis._get_master_stock_list', return_value=[])
def test_save_all_market_analysis_no_data(mock_get_master):
    """분석할 데이터가 없을 때 엑셀 저장 함수 동작 테스트"""
    with patch('pandas.ExcelWriter') as mock_excel:
        with patch('rich.prompt.Prompt.ask', return_value='y'):
            analysis.save_all_market_analysis()
            # 데이터가 없으므로 ExcelWriter가 호출되지 않아야 함
            mock_excel.assert_not_called()

def test_diagnose_group_stocks_empty():
    """분석 대상 종목이 없을 때의 동작 테스트"""
    original_stock_data = config.session.stock_data
    config.session.stock_data = {'stocks_kr': [], 'etfs_kr': []}
    
    with patch('config.console.print') as mock_print:
        analysis.diagnose_group_stocks()
        # "등록된 국내 종목이 없습니다" 메시지 확인
        assert any("등록된 국내 종목이 없습니다" in str(call) for call in mock_print.call_args_list)
    
    config.session.stock_data = original_stock_data

def test_print_period_price_common_empty():
    """기간별 시세 출력 시 데이터가 없을 때 테스트"""
    with patch('api.get_chart_data', return_value=pd.DataFrame()):
        with patch('config.console.print') as mock_print:
            analysis._print_period_price_common("005930", False)
            # 테이블이 출력되지 않아야 함 (호출 횟수로 간접 확인)
            assert mock_print.call_count == 0