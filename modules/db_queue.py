# modules/db_queue.py
import queue
import threading
import logging
import time

import context

logger = logging.getLogger(__name__)

# =========================================================================
# [Queue Proxy 아키텍처 핵심 모듈]
# 다중 스레드 환경(자동매매, 텔레그램 봇, 스케줄러 등)에서 로컬 SQLite 데이터베이스에
# 동시에 쓰기(Write) 작업을 시도할 때 발생하는 'database is locked' 에러를 원천 차단합니다.
# 모든 DB 작업 요청을 Queue에 담아 단일 워커(Single Worker) 스레드가 순차적으로 처리하도록 라우팅합니다.
# =========================================================================

class DBWorker(threading.Thread):
    """
    DB 작업을 순차적으로 처리하는 전담 워커 스레드입니다.
    오직 이 스레드만이 실제 SQLite DB에 접근하므로, 락(Lock) 충돌이 발생하지 않습니다.
    """
    def __init__(self, task_queue, real_db):
        super().__init__(daemon=True, name="DBWorker")
        self._queue = task_queue
        self._real_db = real_db
        self._running = True

    def run(self):
        """큐에서 대기 중인 작업을 하나씩 꺼내어 순차적으로 실행합니다."""
        while self._running:
            try:
                # timeout을 주어 주기적으로 _running 상태를 체크할 수 있게 함
                task = self._queue.get(timeout=0.5)
                if task is None:  # 종료 시그널 수신 시 루프 탈출
                    break

                method_name, args, kwargs, result_queue, use_auto = task
                
                # 큐에 작업이 많이 쌓이면 병목 현상 경고 로깅
                q_size = self._queue.qsize()
                if q_size > 10:
                    logger.warning(f"[DBQueue] High load: {q_size} tasks waiting.")

                # [계좌 컨텍스트 전파] DBManager는 기록 대상 계좌를 thread-local
                #  (context.trade_context.use_auto_account)에서 읽는다. 그런데 실제 실행은
                #  이 워커 스레드에서 일어나고 thread-local은 상속되지 않으므로, 감싸지 않으면
                #  자동매매가 낸 주문이 전부 '수동 계좌' 기록으로 남는다.
                #  주문은 자동 계좌로 나가는데 기록만 수동 계좌에 쌓이면, 계좌로 필터하는
                #  조회(get_trades(account=...))가 빈 결과를 돌려주고 평단·트레일링 최고가·
                #  손절 기준이 붙을 자리를 잃는다. 호출 스레드의 값을 그대로 복원한다.
                context.trade_context.use_auto_account = use_auto
                try:
                    # 커스텀 함수 실행 로직 (트랜잭션 단위 작업 등)
                    if method_name == "__CUSTOM__":
                        func, f_args, f_kwargs = args
                        result = func(*f_args, **f_kwargs)
                    # 일반 DBManager 메서드 실행 로직
                    else:
                        method = getattr(self._real_db, method_name)
                        result = method(*args, **kwargs)
                    
                    # 작업 결과를 호출자(Proxy)의 1회용 결과 큐로 전달하여 대기 해제
                    if result_queue:
                        result_queue.put(("OK", result))
                except Exception as e:
                    logger.error(f"[DBQueue] Error executing '{method_name}': {e}", exc_info=True)
                    # 에러 발생 시에도 호출자가 무한 대기하지 않도록 에러 전송
                    if result_queue:
                        result_queue.put(("ERROR", e))
                finally:
                    # 큐의 해당 작업이 완료되었음을 알림 (task_done)
                    self._queue.task_done()
            except queue.Empty:
                # timeout 발생 시 다시 while 루프 조건 검사
                continue
            except Exception as e:
                logger.error(f"[DBQueue] Worker loop critical error: {e}", exc_info=True)

    def stop(self):
        """워커 스레드를 안전하게 종료합니다."""
        self._running = False
        self._queue.put(None) # 대기 중인 get()을 즉시 깨우기 위해 None(종료 시그널) 전송

class DBProxy:
    """
    기존 DBManager 객체를 감싸는(Wrap) 프록시 클래스입니다.
    다른 모듈들은 이 Proxy를 실제 DB 객체로 인식하고 사용하지만,
    내부적으로는 모든 호출을 가로채어 큐(Queue)에 넣고 결과를 기다립니다.
    """
    def __init__(self, real_db):
        self._real_db = real_db
        self._queue = queue.Queue()
        self._worker = DBWorker(self._queue, self._real_db)
        self._worker.start()

    def execute_custom(self, func, *args, **kwargs):
        """
        임의의 함수를 DB 전담 워커 스레드에서 실행하도록 위임합니다.
        직접 sqlite3 연결이 필요하거나 여러 쿼리를 하나의 트랜잭션으로 묶어야 할 때 사용합니다.
        """
        result_queue = queue.Queue()
        # 특수 메서드명 __CUSTOM__을 사용하여 메인 큐에 적재
        self._queue.put(("__CUSTOM__", (func, args, kwargs), {}, result_queue,
                         getattr(context.trade_context, 'use_auto_account', False)))
        
        try:
            # 워커 스레드가 작업을 마치고 결과를 돌려줄 때까지 대기 (최대 30초)
            status, res = result_queue.get(timeout=30)
            if status == "ERROR":
                raise res
            return res
        except queue.Empty:
            logger.error("[DBQueue] Custom operation timed out (30s)")
            raise Exception("DB Operation Timeout")

    def __getattr__(self, name):
        """
        실제 DB 객체(DBManager)의 메서드 호출을 동적으로 가로챕니다.
        (예: db.insert_trade() 호출 시 이 함수가 작동)
        """
        attr = getattr(self._real_db, name)
        
        if callable(attr):
            def wrapper(*args, **kwargs):
                # 호출한 스레드가 결과를 돌려받을 1회용 큐 생성
                result_queue = queue.Queue()
                # (메서드명, 인자, 키워드인자, 결과큐, 계좌컨텍스트)를 메인 작업 큐에 전달.
                #  계좌 컨텍스트는 **호출 스레드에서** 읽어야 한다 — 워커 스레드에서 읽으면
                #  항상 기본값(수동 계좌)이다(DBWorker.run 주석 참조).
                self._queue.put((name, args, kwargs, result_queue,
                                 getattr(context.trade_context, 'use_auto_account', False)))
                
                try:
                    # 30초 타임아웃 설정 (무한 대기 방지)
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
        """시스템 종료 시 DB 워커 스레드를 정리합니다."""
        if self._worker.is_alive():
            self._worker.stop()
            self._worker.join(timeout=2.0)

_proxy_instance = None

def install_proxy(db_manager_module):
    """
    db_manager 모듈의 전역 db 객체를 Proxy 객체로 바꿔치기(Monkey Patching)합니다.
    프로그램 초기 구동 시 단 한 번만 호출되어야 합니다.
    """
    global _proxy_instance
    if _proxy_instance is None:
        real_db = db_manager_module.db
        _proxy_instance = DBProxy(real_db)
        db_manager_module.db = _proxy_instance
        logger.info("[DBQueue] DB Proxy installed successfully.")

def shutdown():
    """시스템 종료 시 호출되어 Proxy를 안전하게 끕니다."""
    global _proxy_instance
    if _proxy_instance:
        _proxy_instance.stop()
