import pytest
from unittest.mock import patch, MagicMock
import pandas as pd
import os
import sys
import time

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
    
    # plt.subplots가 (fig, [ax1, ax_ha, ax6, ax2, ax3, ax4, ax5])를 반환하도록 설정
    #  (하이킨 아시 패널 추가로 서브플롯 6개 → 7개. chart.py의 언패킹 개수와 반드시 일치해야 함)
    mock_fig = MagicMock()
    mock_axes = [MagicMock() for _ in range(7)]
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

@patch('modules.chart._is_before_krx_open', return_value=True)
@patch('modules.chart.config.console.print')
@patch('modules.chart.plt')
@patch('modules.chart.api.get_chart_data')
def test_generate_visual_chart_intraday_premarket_message(mock_get_data, mock_plt, mock_print, mock_pre):
    """국내 분봉 + 장전(빈 데이터): '장 시작 후 확인' 안내(일반 실패 메시지 아님)."""
    mock_get_data.return_value = pd.DataFrame()
    chart.generate_visual_chart("005930", "삼성전자", False, period_type='intraday')
    printed = " ".join(str(c) for c in mock_print.call_args_list)
    assert "장 시작" in printed and "09:00" in printed
    assert "데이터를 불러올 수 없습니다" not in printed

@patch('modules.chart._is_before_krx_open', return_value=False)
@patch('modules.chart.config.console.print')
@patch('modules.chart.plt')
@patch('modules.chart.api.get_chart_data')
def test_generate_visual_chart_intraday_empty_after_open_generic(mock_get_data, mock_plt, mock_print, mock_pre):
    """장 시작 후 분봉이 비면 일반 실패 메시지(장전 안내 아님)."""
    mock_get_data.return_value = pd.DataFrame()
    chart.generate_visual_chart("005930", "삼성전자", False, period_type='intraday')
    printed = " ".join(str(c) for c in mock_print.call_args_list)
    assert "데이터를 불러올 수 없습니다" in printed

@patch('modules.chart.api.get_chart_data')
def test_generate_visual_chart_toss_hourly_blocked(mock_get_data):
    """토스 모드 + 시봉: 데이터 조회 없이 안내 후 종료(미제공 명시)."""
    config.session.is_toss = True
    try:
        chart.generate_visual_chart("005930", "삼성전자", False, period_type='hourly')
    finally:
        config.session.is_toss = False
    # 데이터 조회 자체를 시도하지 않아야 함 (matplotlib 적재도 회피)
    assert not mock_get_data.called

@patch('modules.chart.plt')
@patch('modules.chart.api.get_chart_data')
def test_generate_visual_chart_exception(mock_get_data, mock_plt):
    """차트 생성 중 예외 발생 시 처리 테스트"""
    mock_get_data.side_effect = Exception("API Error")
    
    # 예외가 전파되는지 확인 (상위 호출자에서 처리)
    with pytest.raises(Exception, match="API Error"):
        chart.generate_visual_chart("005930", "삼성전자", False)

# ==========================================================
# [메모리] 렌더 직렬화 — 두 스레드가 동시에 그리면 피크가 합산된다
# ==========================================================
def test_render_lock_serializes_concurrent_renders():
    """_serialized_render 로 감싼 구간에는 한 번에 하나만 들어간다."""
    import threading

    inside = []
    overlap = []

    @chart._serialized_render
    def fake_render():
        inside.append(1)
        if len(inside) > 1:
            overlap.append(1)
        time.sleep(0.05)
        inside.pop()

    threads = [threading.Thread(target=fake_render) for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert not overlap, "렌더 구간에 두 스레드가 동시에 들어갔다"


def test_render_entrypoints_are_serialized():
    """차트 진입점 두 개가 실제로 락에 싸여 있는지(데코레이터 유실 방지)."""
    assert hasattr(chart.generate_visual_chart, '__wrapped__')
    assert hasattr(chart.generate_monte_carlo_histogram, '__wrapped__')
