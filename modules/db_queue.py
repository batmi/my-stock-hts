import queue
import threading
import logging
import time

logger = logging.getLogger(__name__)

class DBProxy:
    """
    기존 DB 객체를 감싸서 모든 메서드 호출을 큐에 넣어 
    단일 워커 스레드에서 순차적으로 처리하는 프록시 클래스입니다.
    """
    def __init__(self, real_db):
        self._real_db = real_db
        self._queue = queue.Queue()
        self._running = True
        self._thread = threading.Thread(target=self._worker_loop, daemon=True, name="DBWorker")
        self._thread.start()

    def execute_custom(self, func, *args, **kwargs):
        """
        임의의 함수를 DB 워커 스레드에서 실행합니다.
        직접 sqlite3 연결이 필요한 로직을 큐에 태우기 위해 사용합니다.
        """
        result_queue = queue.Queue()
        # 특수 메서드명 __CUSTOM__ 사용
        self._queue.put(("__CUSTOM__", (func, args, kwargs), {}, result_queue))
        
        try:
            # 30초 타임아웃 설정으로 무한 대기 방지
            status, res = result_queue.get(timeout=30)
            if status == "ERROR":
                raise res
            return res
        except queue.Empty:
            logger.error("[DBQueue] Custom operation timed out (30s)")
            raise Exception("DB Operation Timeout")

    def _worker_loop(self):
        """큐에서 작업을 꺼내 순차적으로 실행하는 워커 스레드"""
        while self._running:
            try:
                task = self._queue.get(timeout=0.5)
                method_name, args, kwargs, result_queue = task
                
                # 큐 사이즈 모니터링
                q_size = self._queue.qsize()
                if q_size > 10:
                    logger.warning(f"[DBQueue] High load: {q_size} tasks waiting.")

                try:
                    if method_name == "__CUSTOM__":
                        # 커스텀 함수 실행
                        func, f_args, f_kwargs = args
                        result = func(*f_args, **f_kwargs)
                    else:
                        # 실제 DB 객체의 메서드 호출
                        method = getattr(self._real_db, method_name)
                        result = method(*args, **kwargs)
                    
                    if result_queue:
                        result_queue.put(("OK", result))
                except Exception as e:
                    logger.error(f"[DBQueue] Error executing '{method_name}': {e}", exc_info=True)
                    if result_queue:
                        result_queue.put(("ERROR", e))
                finally:
                    self._queue.task_done()
            except queue.Empty:
                continue
            except Exception as e:
                logger.error(f"[DBQueue] Worker loop critical error: {e}", exc_info=True)

    def __getattr__(self, name):
        attr = getattr(self._real_db, name)
        
        if callable(attr):
            def wrapper(*args, **kwargs):
                result_queue = queue.Queue()
                self._queue.put((name, args, kwargs, result_queue))
                
                try:
                    # 30초 타임아웃 설정
                    status, res = result_queue.get(timeout=30)
                    if status == "ERROR":
                        raise res
                    return res
                except queue.Empty:
                    logger.error(f"[DBQueue] Method '{name}' timed out (30s)")
                    raise Exception(f"DB Method '{name}' Timeout")
            return wrapper
        return attr

    def stop(self):
        self._running = False
        if self._thread.is_alive():
            self._thread.join(timeout=2.0)

_proxy_instance = None

def install_proxy(db_manager_module):
    global _proxy_instance
    if _proxy_instance is None:
        real_db = db_manager_module.db
        _proxy_instance = DBProxy(real_db)
        db_manager_module.db = _proxy_instance
        logger.info("[DBQueue] DB Proxy installed successfully.")

def shutdown():
    global _proxy_instance
    if _proxy_instance:
        _proxy_instance.stop()
