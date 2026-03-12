import pytest
from unittest.mock import patch, MagicMock
import queue
import time
from modules import db_queue

class MockDB:
    def error_method(self):
        raise ValueError("Simulated DB Error")
    def dummy_method(self):
        return "success"
    def echo(self, val):
        return val

@pytest.fixture
def db_proxy_setup():
    """DBProxy 테스트 환경 설정"""
    real_db = MockDB()
    proxy = db_queue.DBProxy(real_db)
    yield proxy, real_db
    proxy.stop()

def test_normal_method_call(db_proxy_setup):
    """일반 메서드 호출 성공 테스트"""
    proxy, _ = db_proxy_setup
    assert proxy.dummy_method() == "success"
    assert proxy.echo(123) == 123

def test_execute_custom_success(db_proxy_setup):
    """execute_custom 성공 테스트"""
    proxy, _ = db_proxy_setup
    def add(a, b):
        return a + b
    
    result = proxy.execute_custom(add, 10, 20)
    assert result == 30

def test_execute_custom_exception(db_proxy_setup):
    """execute_custom 예외 발생 테스트"""
    proxy, _ = db_proxy_setup
    def fail():
        raise KeyError("Custom Fail")
    
    with pytest.raises(KeyError, match="Custom Fail"):
        proxy.execute_custom(fail)

def test_db_proxy_timeout():
    """DB 작업 타임아웃 발생 시 예외 처리 테스트"""
    real_db = MockDB()
    proxy = db_queue.DBProxy(real_db)
    # 워커를 정지시켜 큐 처리가 안되게 함 (하지만 timeout=30초를 기다려야 하므로 get을 모킹해야 함)
    proxy.stop()
    
    # queue.Queue.get을 모킹하여 즉시 Empty 예외 발생 (타임아웃 시뮬레이션)
    # 이렇게 하면 30초를 기다리지 않고 바로 타임아웃 예외 발생 경로를 테스트할 수 있음
    with patch('queue.Queue.get', side_effect=queue.Empty):
        with pytest.raises(Exception, match="DB Method 'dummy_method' Timeout"):
            proxy.dummy_method()

def test_execute_custom_timeout():
    """execute_custom 타임아웃 테스트"""
    real_db = MockDB()
    proxy = db_queue.DBProxy(real_db)
    proxy.stop()
    
    with patch('queue.Queue.get', side_effect=queue.Empty):
        with pytest.raises(Exception, match="DB Operation Timeout"):
            proxy.execute_custom(lambda: None)

def test_install_proxy_and_shutdown():
    """install_proxy 및 shutdown 함수 테스트"""
    # 모의 db_manager 모듈 생성
    class MockDBModule:
        pass
        
    mock_module = MockDBModule()
    mock_module.db = MockDB()
    
    # install_proxy 호출 (전역 변수 _proxy_instance 설정됨)
    db_queue.install_proxy(mock_module)
    
    # 프록시가 설치되었는지 확인
    assert isinstance(mock_module.db, db_queue.DBProxy)
    assert db_queue._proxy_instance is not None
    
    # shutdown 호출
    db_queue.shutdown()
    
    # 워커가 종료되었는지 확인
    assert not db_queue._proxy_instance._worker.is_alive()
    
    # 테스트 격리를 위해 전역 변수 초기화
    db_queue._proxy_instance = None

def test_db_worker_exception(db_proxy_setup):
    """DB 워커가 작업 중 예외 발생 시 처리 테스트"""
    proxy, _ = db_proxy_setup
    
    with pytest.raises(ValueError, match="Simulated DB Error"):
        proxy.error_method()
