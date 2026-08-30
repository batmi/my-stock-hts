import pytest
import concurrent.futures
import threading
from unittest.mock import patch
from pydantic import ValidationError

import config
import api
from modules import prompts
from core import executors

# ==========================================================
# 2. 전역 변수(Global) 기반 설정 관리 개선 (Pydantic & Thread-Safety)
# ==========================================================
def test_global_settings_pydantic_validation():
    """Pydantic BaseModel을 통한 설정값 타입 및 제약 조건(Validation) 검증"""
    # 정상적인 인스턴스화
    settings = config.GlobalSettings(SYSTEM_MAX_HOLDINGS=10)
    assert settings.SYSTEM_MAX_HOLDINGS == 10

    # 제약 조건 위반 (gt=0 인데 0 이하의 값 주입)
    with pytest.raises(ValidationError):
        config.GlobalSettings(SYSTEM_MAX_HOLDINGS=-5)
        
    with pytest.raises(ValidationError):
        config.GlobalSettings(SYSTEM_INVEST_PER_STOCK=1.5) # le=1.0 제약 위반

def test_load_dynamic_config_thread_safety():
    """동적 설정 로드 시 RLock을 통해 Thread-safe하게 동작하는지 검증

    [주의] 이 테스트는 진짜로 load_dynamic_config()를 돌린다 — 주입한 값이 settings와
     config 모듈 속성 양쪽에 그대로 남는다. 되돌리지 않으면 뒤 테스트가 상한 20을 본다:
     실제로 test_audit_trade_mechanics 의 슬롯 상한 검증이 조용히 무력화됐다(6 > 4 를
     잡아야 하는데 6 < 20 이라 통과). 점검 도구 테스트의 실패 형태가 '항상 통과'라
     증상이 보이지도 않는다. 그래서 여기서 되돌린다.
    """
    saved = config.settings.SYSTEM_MAX_HOLDINGS
    had_attr = 'SYSTEM_MAX_HOLDINGS' in config.__dict__
    try:
        with patch('config._settings_lock') as mock_lock:
            # 빈 JSON 데이터로 강제 업데이트 시도
            with patch('builtins.open'), patch('json.load', return_value={"SYSTEM_MAX_HOLDINGS": 20}), patch('os.path.exists', return_value=True):
                config.load_dynamic_config()

            # 락(RLock)을 획득하고 해제했는지 확인
            mock_lock.__enter__.assert_called()
            mock_lock.__exit__.assert_called()
    finally:
        config.settings.SYSTEM_MAX_HOLDINGS = saved
        if had_attr:
            config.__dict__['SYSTEM_MAX_HOLDINGS'] = saved
        else:
            config.__dict__.pop('SYSTEM_MAX_HOLDINGS', None)

# ==========================================================
# 3. AI 프롬프트 관리의 외부화 (Prompts Separation)
# ==========================================================
def test_prompts_externalization_and_formatting():
    """분리된 prompts 모듈의 프롬프트 템플릿 변수 바인딩(Formatting) 무결성 검증"""
    # 프롬프트 상수가 존재하는지 확인
    assert hasattr(prompts, "MARKET_TRENDS_PROMPT")
    assert hasattr(prompts, "STOCK_ANALYSIS_PROMPT")
    
    # KeyError가 발생하지 않고 정상 포매팅되는지 확인 (필수 파라미터 누락 방지)
    formatted = prompts.MARKET_TRENDS_PROMPT.format(now="2023-12-01 10:00:00", macro_context="Test Context")
    assert "Test Context" in formatted
    
    formatted_analysis = prompts.STOCK_ANALYSIS_PROMPT.format(
        now="2023-12-01 10:00:00", name="삼성전자", code="005930", tech_info_str="Test Tech"
    )
    assert "삼성전자(005930)" in formatted_analysis

# ==========================================================
# 4. 스레드 풀(ThreadPool) 생성 오버헤드 최소화
# ==========================================================
def test_global_executors_reusability():
    """전역 스레드 풀 객체가 정상적으로 생성되고 재사용 가능한지 검증"""
    # tg_sender_executor가 ThreadPoolExecutor 인스턴스인지 확인
    assert isinstance(executors.tg_sender_executor, concurrent.futures.ThreadPoolExecutor)
    
    # 작업을 Submit하여 정상 실행되는지 확인
    def sample_task():
        return threading.current_thread().name
        
    future = executors.tg_sender_executor.submit(sample_task)
    result_thread_name = future.result(timeout=2)
    
    assert result_thread_name is not None

# ==========================================================
# 5. except Exception: pass (Silent Failure) 지양 로깅
# ==========================================================
@patch('api.logger.debug')
@patch('api.yf.Ticker')
def test_silent_failure_logging_prevention(mock_ticker, mock_debug):
    """예외 발생 시 pass하지 않고 logger.debug를 호출하여 원인을 추적하는지 검증"""
    # 강제로 예외 발생
    mock_ticker.side_effect = Exception("Intentional YFinance Error")
    
    api.get_yf_fast_info("INVALID_TICKER")
    
    # 에러 메시지가 로깅되었는지 확인 (에러가 은폐되지 않음)
    assert mock_debug.call_count > 0
    assert any("Intentional YFinance Error" in str(call.args[0]) for call in mock_debug.call_args_list)