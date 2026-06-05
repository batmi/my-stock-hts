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
        # Progress 바 등 내부 출력은 무시하고, Table 객체 출력이 없는지 확인
        with patch('rich.console.Console.print') as mock_print:
            # Progress 바 생성을 위한 console 접근도 모킹해야 함
            with patch('config.console', MagicMock()):
                analysis._print_period_price_common("005930", False)
                
                # 테이블 객체가 print 되었는지 확인
                # mock_print.call_args_list를 순회하며 Table 인스턴스가 있는지 확인
                # 데이터가 없으면 Table이 생성되어 print되지 않음
                assert not any(isinstance(arg[0], analysis.Table) for call in mock_print.call_args_list for arg in call.args if arg)

def test_calculate_score_perfect_10_points():
    """새롭게 개선된 상호 배타적 OR 조건을 통해 10.0점 만점이 계산되는지 검증"""
    # Trend (4.0) + Momentum (2.5) + Strength (1.5) + Synergy (2.0) = 10.0
    score, details = analysis.calculate_score(
        price=10000,
        ema20=9000, ema60=8000, ema120=7000, # 정배열 (+1.0), 현재가>20선 (+0.5)
        sar=8000,                            # 주가>SAR (+0.5)
        rsi=65,                              # 50~75 강세 (+0.5), >=60 확장 (+0.5) [NEW OR]
        adx=25,                              # ADX >= 20 (+0.5)
        cci=100,                             # >0 상승 (+0.5), >=50 심화 (+0.5) [NEW OR]
        obv_trend=True,                      # OBV 상승 (+0.5)
        macd=100, macd_signal=50,            # 골든크로스 (+0.5)
        ema_5=9500,                          # 5>20선 (+0.5), 5>20>60 급등 (+0.5) [NEW OR]
        macd_hist=50, prev_macd_hist=30,     # 히스토그램 개선 (+0.5)
        prev_cci=80,
        vol_spike=False, vol_trend=True,     # 거래량 추세 상승 (+0.5) [NEW OR]
        smart_money=False,
        plus_di=30, minus_di=15,             # +DI > -DI (+0.5)
        weights={"TREND": 4.0, "MOMENTUM": 2.5, "STRENGTH": 1.5, "SYNERGY": 2.0} # 타 테스트 오염 방지
    )
    
    assert score == 10.0
    assert len(details) == 17 # 각 가산점에 대한 상세 내역 개수 확인