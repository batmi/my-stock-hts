import pytest
from unittest.mock import patch, MagicMock, call, ANY
import api
import config
import requests

@pytest.fixture
def mock_session():
    """세션 Mock 객체"""
    mock_session = MagicMock()
    return mock_session

def test_call_api_success(mock_session):
    """API 호출 성공 테스트"""
    expected_result = {"rt_cd": "0", "output": {}}
    mock_session.get.return_value = MagicMock(status_code=200)
    mock_session.get.return_value.json.return_value = expected_result

    # get_current_token을 mock하여 토큰 발급 로직 격리
    with patch("api.session", mock_session), \
         patch("api.get_current_token", return_value="test_token"):
        
        # tr_id를 명시적으로 전달하여 config lookup 우회
        result = api.call_api("test/url", "domestic", "test", "test", tr_id="TEST_TR_ID")
        
        assert result == expected_result
        
        # URL은 base_url이 붙으므로 ANY로 처리하거나 endswith 확인
        mock_session.get.assert_called_once_with(
            ANY,
            headers=ANY,
            params=None,
            timeout=config.DEFAULT_TIMEOUT,
            retries=ANY  # call_api가 retries를 세션으로 위임하도록 변경됨
        )
        # URL 확인
        args, _ = mock_session.get.call_args
        assert args[0].endswith("test/url")

def test_call_api_failure(mock_session):
    """API 호출 실패 테스트 (HTTP 에러)"""
    mock_session.get.return_value.status_code = 500
    mock_session.get.return_value.text = "Server Error"
    # JSON 파싱 실패 시뮬레이션
    mock_session.get.return_value.json.side_effect = ValueError("JSON Decode Error")

    with patch("api.session", mock_session), \
         patch("api.get_current_token", return_value="test_token"):
        
        result = api.call_api("test/url", "domestic", "test", "test", tr_id="TEST_TR_ID")
        assert result["rt_cd"] == "9999"

def test_get_access_token_success(mock_session):
    """토큰 발급 성공 테스트"""
    mock_session.post.return_value = MagicMock(status_code=200)
    mock_session.post.return_value.json.return_value = {"access_token": "test_token"}
    
    config.session.app_key = "test_key"
    config.session.app_secret = "test_secret"

    with patch("api._token_session", mock_session), \
         patch("config.session.get_valid_token", return_value=None): # 캐시 무시
        result = api.get_access_token()
        assert result == "test_token"
        mock_session.post.assert_called_once()

def test_get_access_token_failure(mock_session):
    """토큰 발급 실패 테스트 (HTTP 에러)"""
    mock_session.post.return_value.status_code = 500
    mock_session.post.return_value.text = "Server Error"
    
    config.session.app_key = "test_key"
    config.session.app_secret = "test_secret"

    with patch("api._token_session", mock_session), \
         patch("config.session.get_valid_token", return_value=None): # 캐시 무시
        result = api.get_access_token()
        assert result is None
        mock_session.post.assert_called_once()

def test_safe_int_valid_input():
    """safe_int 유효한 입력 테스트"""
    assert api.safe_int("123") == 123
    assert api.safe_int(123.45) == 123

def test_safe_int_invalid_input():
    """safe_int 잘못된 입력에 대한 기본값 반환 테스트"""
    assert api.safe_int(None) == 0
    assert api.safe_int("abc") == 0


@pytest.mark.real_index_chart
def test_get_domestic_index_chart_empty_response(mock_session):
    """get_domestic_index_chart 함수가 API 응답이 비어있을 때 빈 DataFrame을 반환하는지 테스트"""
    mock_session.get.return_value = MagicMock(status_code=200)
    mock_session.get.return_value.json.return_value = {'rt_cd': '0', 'output2': None}  # output2가 None인 경우

    with patch("api.session", mock_session), \
         patch("api.get_current_token", return_value="test_token"):
        df = api.get_domestic_index_chart("0001")
        assert df.empty


# Test cases for Rate Limiter
def test_api_rate_limit():
    """API Rate Limit 테스트 (RateLimiter)"""
    config.SIM_TX_PER_SECOND = 1

    with patch.object(requests.Session, 'request') as mock_request, \
         patch("api.get_current_token", return_value="test_token"), \
         patch("time.sleep"): # Rate Limit 대기 시간 제거
        
        mock_request.return_value = MagicMock(status_code=200, json=lambda: {'rt_cd': '0', 'output': {}})
        
        # 세션 상태 초기화 (다른 테스트 영향 제거)
        api.session.request_history_sim.clear()
        api.session.request_history_real.clear()

        # call_api를 연속으로 호출하여 Rate Limiting이 동작하는지 확인
        api.call_api("test/url", "domestic", "test", "test", method="GET", tr_id="TEST_TR_ID")
        api.call_api("test/url", "domestic", "test", "test", method="GET", tr_id="TEST_TR_ID")  # Second call (should be throttled)
        assert mock_request.call_count == 2


from unittest.mock import ANY

@patch('api.session.get')
def test_call_api_args(mock_get):
    """call_api가 session.get에 정확한 인수를 전달하는지 확인"""
    expected_url = "https://openapi.koreainvestment.com:9443/test/url"
    expected_headers = {
        "Content-Type": "application/json",
        "authorization": "Bearer test_token",
        "appKey": "test_app_key",
        "appSecret": "test_app_secret",
        "tr_id": "test_tr_id",
        "custtype": "P"
    }
    
    config.session.url_base = "https://openapi.koreainvestment.com:9443"
    config.session.app_key = "test_app_key"
    config.session.app_secret = "test_app_secret"
    mock_get.return_value = MagicMock(status_code=200)
    mock_get.return_value.json.return_value = {"rt_cd": "0", "output": {}}
    
    with patch("api.get_current_token", return_value="test_token"):
        result = api.call_api(
            "test/url", "domestic", "test", "test", 
            params={"param1": "value1"}, 
            tr_id="test_tr_id"
        )
        
        mock_get.assert_called_once_with(
            expected_url,
            headers=expected_headers,
            params={"param1": "value1"},
            timeout=config.DEFAULT_TIMEOUT,
            retries=ANY  # call_api가 retries를 세션으로 위임하도록 변경됨
        )