import pytest
from unittest.mock import patch, MagicMock
import pandas as pd
import os
import sys

# 프로젝트 루트 경로 추가
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from modules import chart
import config

@pytest.fixture
def sample_chart_df():
    """차트 생성용 가상 데이터 (60일치)"""
    dates = pd.date_range(start="2023-01-01", periods=60)
    df = pd.DataFrame({
        'date': dates,
        'open': [10000 + i*10 for i in range(60)],
        'high': [10500 + i*10 for i in range(60)],
        'low': [9500 + i*10 for i in range(60)],
        'close': [10200 + i*10 for i in range(60)],
        'volume': [100000 for _ in range(60)]
    })
    return df

@patch('modules.chart.plt')
@patch('modules.chart.api.get_chart_data')
def test_generate_visual_chart_success(mock_get_data, mock_plt, sample_chart_df, tmp_path):
    """차트 생성 성공 테스트"""
    # Mock 설정
    mock_get_data.return_value = sample_chart_df
    
    # plt.subplots가 (fig, [ax1, ax2, ax3, ax4, ax5])를 반환하도록 설정
    mock_fig = MagicMock()
    mock_axes = [MagicMock() for _ in range(5)]
    mock_plt.subplots.return_value = (mock_fig, mock_axes)
    
    # 차트 저장 경로를 임시 디렉토리로 변경 (config.CHART_DIR 모킹)
    with patch('config.CHART_DIR', str(tmp_path)):
        # 함수 실행
        chart.generate_visual_chart("005930", "삼성전자", False)
        
        # 검증
        # generate_visual_chart는 내부적으로 period_type='daily'를 사용하여 호출함
        mock_get_data.assert_called_with("005930", False, 'daily')
        
        # 플롯 관련 함수 호출 확인
        assert mock_plt.subplots.called
        assert mock_plt.savefig.called
        assert mock_plt.close.called
        
        # 저장된 파일명 확인 (args[0] of savefig)
        args, _ = mock_plt.savefig.call_args
        assert "005930" in args[0]
        assert ".png" in args[0]

@patch('modules.chart.api.get_chart_data')
@patch('modules.chart.plt')
def test_generate_visual_chart_no_data(mock_plt, mock_get_data):
    """데이터가 없을 때 차트 생성 중단 테스트"""
    # 데이터 없음 (None 또는 빈 DataFrame)
    mock_get_data.return_value = pd.DataFrame()
    
    chart.generate_visual_chart("005930", "삼성전자", False)
    
    # 플롯을 그리지 않아야 함
    assert not mock_plt.figure.called
    assert not mock_plt.savefig.called

@patch('modules.chart.plt')
@patch('modules.chart.api.get_chart_data')
def test_generate_visual_chart_exception(mock_get_data, mock_plt):
    """차트 생성 중 예외 발생 시 처리 테스트"""
    mock_get_data.side_effect = Exception("API Error")
    
    # 예외가 전파되는지 확인 (상위 호출자에서 처리)
    with pytest.raises(Exception, match="API Error"):
        chart.generate_visual_chart("005930", "삼성전자", False)