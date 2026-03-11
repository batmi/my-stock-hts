import pytest
from unittest.mock import MagicMock, patch
import queue
import threading
import time
from modules import db_queue

class MockDB:
    """테스트용 모의 DB 클래스"""
    def echo(self, msg):
        return msg
    
    def add(self, a, b):
        return a + b
        
    def error(self):
        raise ValueError("DB Error Simulated")

@pytest.fixture
def db_proxy_setup():
    """DBProxy 테스트 환경 설정"""
    # 원본 객체
    real_db = MockDB()
    # 프록시 생성
    proxy = db_queue.DBProxy(real_db)
    # 큐 교체 (테스트 제어용)
    test_queue = queue.Queue()
    proxy._queue = test_queue
    
    return proxy, test_queue, real_db

def test_db_proxy_method_call(db_proxy_setup):
    """일반 메서드 호출이 큐를 통해 전달되고 결과가 반환되는지 테스트"""
    proxy, task_queue, real_db = db_proxy_setup
    
    result_box = {}
    
    def caller():
        result_box['value'] = proxy.echo("Hello")
        
    t = threading.Thread(target=caller)
    t.start()
    
    # 큐에서 작업 가져오기 (타임아웃 설정으로 무한 대기 방지)
    try:
        # task_queue.get() -> (method_name, args, kwargs, result_queue)
        item = task_queue.get(timeout=2)
    except queue.Empty:
        pytest.fail("큐에 작업이 들어오지 않았습니다.")
        
    name, args, kwargs, result_queue = item
    
    assert name == "echo"
    assert args == ("Hello",)
    
    # 워커 로직 시뮬레이션: 실제 메서드 호출 후 결과 전송
    ret = real_db.echo(*args, **kwargs)
    result_queue.put(("OK", ret))
    
    t.join(timeout=2)
    assert result_box['value'] == "Hello"

def test_db_proxy_exception_propagation(db_proxy_setup):
    """예외 발생 시 프록시 호출자에게 예외가 전파되는지 테스트"""
    proxy, task_queue, real_db = db_proxy_setup
    
    result_box = {}
    
    def caller():
        try:
            proxy.error()
        except Exception as e:
            result_box['error'] = e
            
    t = threading.Thread(target=caller)
    t.start()
    
    try:
        item = task_queue.get(timeout=2)
    except queue.Empty:
        pytest.fail("큐에 작업이 들어오지 않았습니다.")
        
    name, args, kwargs, result_queue = item
    assert name == "error"
    
    # 워커 로직 시뮬레이션: 예외 발생 및 전송
    try:
        real_db.error()
    except Exception as e:
        result_queue.put(("ERROR", e))
        
    t.join(timeout=2)
    assert isinstance(result_box.get('error'), ValueError)
    assert str(result_box['error']) == "DB Error Simulated"

def test_execute_custom(db_proxy_setup):
    """execute_custom 메서드 동작 테스트"""
    proxy, task_queue, real_db = db_proxy_setup
    
    def my_func(x, y):
        return x * y
        
    result_box = {}
    def caller():
        result_box['value'] = proxy.execute_custom(my_func, 3, 4)
        
    t = threading.Thread(target=caller)
    t.start()
    
    try:
        item = task_queue.get(timeout=2)
    except queue.Empty:
        pytest.fail("큐에 작업이 들어오지 않았습니다.")
        
    # execute_custom은 ("__CUSTOM__", (func, args, kwargs), {}, result_queue) 형태로 넣음
    name, args_tuple, kwargs, result_queue = item
    
    assert name == "__CUSTOM__"
    func, f_args, f_kwargs = args_tuple
    
    assert func == my_func
    assert f_args == (3, 4)
    
    # 워커 시뮬레이션
    res = func(*f_args, **f_kwargs)
    result_queue.put(("OK", res))
    
    t.join(timeout=2)
    assert result_box['value'] == 12

def test_worker_shutdown():
    """Worker 종료(Shutdown) 테스트"""
    real_db = MockDB()
    task_queue = queue.Queue()
    worker = db_queue.DBWorker(task_queue, real_db)
    worker.start()
    
    assert worker.is_alive()
    
    # Shutdown 시그널 전송 (None)
    task_queue.put(None)
    
    # 종료 대기
    worker.join(timeout=2)
    assert not worker.is_alive()

