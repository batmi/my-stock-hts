import time
import types

import pytest
from unittest.mock import patch, MagicMock
from modules import market
import yfinance as yf
import pandas as pd

@patch('modules.market.Table')
@patch('modules.market.api.fetch_yfinance_data')
def test_show_market_indices_core_error(mock_prefetch, mock_table):
    """지수 분석 중 일반 예외 발생 시 처리 테스트"""
    mock_prefetch.return_value = pd.DataFrame()
    # 테이블 생성 단계에서 예외 발생 시뮬레이션
    mock_table.side_effect = Exception("Forced Error")
    
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
    
        # 사용자 입력: 그룹 선택(9:전체) -> 재시도(y) -> 메인화면(q)
    mock_ask.side_effect = ['9', 'y', 'q']
    
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
    
    # 무한 루프 방지를 위해 sleep에서 예외 발생.
    #  [범위 · 2026-08-29] `patch('time.sleep', ...)` 도, `patch('modules.market.time.sleep', ...)`
    #   도 **프로세스 전역**이다 — 둘 다 같은 time 모듈 객체의 속성을 바꾸기 때문이다.
    #   그러면 그 순간 잠들어 있던 배경 스레드(전역 스레드 풀·스케줄러 등)에도
    #   KeyboardInterrupt 가 꽂혀, 실제로 xdist 워커가 여기서 크래시해 스위트 전체가
    #   중단됐다(`worker 'gwN' crashed` → 절반만 실행되고 끝난다).
    #   market 의 네임스페이스에 있는 `time` **이름만** 바꿔 끼워 진짜로 좁힌다.
    fake_time = types.SimpleNamespace(now=time.time, strptime=time.strptime)
    fake_time.sleep = MagicMock(side_effect=KeyboardInterrupt)
    with patch.object(market, 'time', fake_time):
        market.show_market_indices()
        
    assert mock_core.called