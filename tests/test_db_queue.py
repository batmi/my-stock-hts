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
    """DB 작업 타임아웃 발생 시 예외 처리 테스트.

    [2026-09-06] 종전에는 proxy.stop() 으로 워커를 죽여 타임아웃을 흉내 냈다. 그런데
     '워커가 없다'와 '워커는 있는데 응답이 없다'는 서로 다른 상황이고, 전자는 이제
     30초를 기다리지 않고 즉시 실패한다(test_db_queue_shutdown.py). 타임아웃 경로를
     시험하려면 **워커를 살려 둔 채** 결과 대기만 막아야 한다.
    """
    real_db = MockDB()
    proxy = db_queue.DBProxy(real_db)
    try:
        # queue.Queue.get을 모킹하여 즉시 Empty 예외 발생 (타임아웃 시뮬레이션)
        # 이렇게 하면 30초를 기다리지 않고 바로 타임아웃 예외 발생 경로를 테스트할 수 있음
        with patch('queue.Queue.get', side_effect=queue.Empty):
            #  [2026-09-07] 시한 초과는 '실패'가 아니라 '결과 불명'이다 — 작업을 취소하지
            #   않으므로 나중에 반영될 수 있다. 예외 종류로 그것을 구분한다.
            with pytest.raises(db_queue.DBOperationUnknown, match="결과 불명"):
                proxy.dummy_method()
    finally:
        proxy.stop()

def test_execute_custom_timeout():
    """execute_custom 타임아웃 테스트 (워커는 살아 있다 — 위 주석 참조)"""
    real_db = MockDB()
    proxy = db_queue.DBProxy(real_db)
    try:
        with patch('queue.Queue.get', side_effect=queue.Empty):
            with pytest.raises(db_queue.DBOperationUnknown, match="결과 불명"):
                proxy.execute_custom(lambda: None)
    finally:
        proxy.stop()

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
