import pytest
from unittest.mock import patch, MagicMock
from modules import market
import yfinance as yf

@patch('modules.market.yf.Tickers')
def test_show_market_indices_core_ticker_fail(mock_tickers):
    """yf.Tickers 객체 생성 실패 시 예외 처리 테스트"""
    # Tickers 객체 생성 시 예외 발생
    mock_tickers.side_effect = Exception("Failed to create Tickers")
    
    with patch('config.console.print') as mock_print:
        market._show_market_indices_core()
        # 예외가 내부에서 catch되고 에러 메시지가 출력되어야 함
        assert any("오류 발생" in str(c) for c in mock_print.call_args_list)

@patch('modules.market._show_market_indices_core')
@patch('rich.prompt.Prompt.ask')
def test_show_market_indices_retry_on_fail(mock_ask, mock_core):
    """조회 실패한 지수 재시도 로직 테스트"""
    # 첫 번째 호출에서는 'VIX' 조회를 실패했다고 가정
    mock_core.side_effect = [
        ['VIX (변동성)'], # 1. 첫 호출: 실패 목록 반환
        [],            # 2. 재시도 호출: 성공 (빈 목록 반환)
        []             # 3. 추가 호출 대비 (안전장치)
    ]
    
    # 사용자 입력: 그룹 선택(8:전체) -> 재시도(y) -> 반복조회(n)
    mock_ask.side_effect = ['8', 'y', 'n']
    
    market.show_market_indices()
    
    # _show_market_indices_core가 2번 호출되었는지 확인
    assert mock_core.call_count >= 2

@patch('modules.market._show_market_indices_core')
@patch('rich.prompt.Prompt.ask')
def test_show_market_indices_auto_refresh(mock_ask, mock_core):
    """반복 조회(@ 입력) 테스트"""
    # 1@ 입력 -> 반복 조회 모드 활성화 -> KeyboardInterrupt로 루프 탈출
    mock_ask.return_value = "1@"
    mock_core.return_value = []
    
    # 무한 루프 방지를 위해 time.sleep에서 예외 발생
    with patch('time.sleep', side_effect=KeyboardInterrupt):
        market.show_market_indices()
        
    assert mock_core.called