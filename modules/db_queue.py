# modules/db_queue.py
import queue
import threading
import logging
import time

#  한 건의 DB 작업을 기다리는 시한(초).
OP_TIMEOUT_SEC = 30


class DBOperationUnknown(Exception):
    """시한 안에 결과를 받지 못했다 — **실패가 아니라 '모른다'** 다.

    [왜 별도 예외인가 · 2026-09-07] 종전에는 그냥 `Exception("... Timeout")` 이었다.
     읽는 사람도 잡는 코드도 그것을 '기록되지 않았다'로 읽는다. 그런데 시한이 지나도
     **작업은 큐에 그대로 남아 곧 실행된다** — 취소하지 않기 때문이다.
     실측(시한 0.2초 · 작업 1초): 호출부는 예외를 받았고, 1.5초 뒤 원장에는 그 행이
     들어가 있었다. 즉 '실패했다'는 말이 사실이 아니다.

     이 시스템은 주문 응답 유실에 대해 이미 같은 규칙을 세워 뒀다 — 재전송하지 말고
     당일 주문내역으로 대사하라([[order-timeout-no-resend]], api/http.py 의 '결과 불명').
     DB 한 층 아래에서 그 규칙이 깨져 있었다. 보상 조치(수동 재입력·재삽입)는
     **중복 원장**을 만들고, 원장의 중복은 손익·평단·트레일링 기준을 통째로 흔든다.

     취소하지 않는 이유: 쓰기를 버리는 쪽이 더 비싸다. 체결 기록·트레일링 최고가 갱신이
     사라지면 그 종목의 손절 기준이 붙을 자리를 잃는다. 늦더라도 반영되는 편이 낫다 —
     대신 늦게 끝났다는 사실을 로그로 남긴다(DBWorker 의 지각 완료 경고).
    """

from core import context

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
                if task is None:  # 종료 시그널 — 앞의 작업을 모두 처리한 뒤에 도달한다
                    self._running = False
                    self._queue.task_done()
                    break

                method_name, args, kwargs, result_queue, use_auto = task
                _queued_at = getattr(result_queue, '_dbq_queued_at', None)
                
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
                    
                    #  [지각 완료 · 2026-09-07] 호출부는 OP_TIMEOUT_SEC 이 지나면 '모른다'로
                    #   돌아간다. 그 뒤에 이 작업이 끝나면 아무 데도 흔적이 없어, 호출부가
                    #   받은 예외만 남고 실제로는 반영된 상태가 된다. 지각을 로그로 남긴다.
                    if _queued_at is not None and (time.time() - _queued_at) > OP_TIMEOUT_SEC:
                        logger.warning(
                            f"[DBQueue] '{method_name}' 가 시한({OP_TIMEOUT_SEC}s)을 넘겨 "
                            f"{time.time() - _queued_at:.1f}초 만에 완료됐습니다 — 호출부는 "
                            f"이미 '결과 불명'으로 돌아갔습니다. **이 작업은 반영됐습니다** "
                            f"(같은 내용을 다시 넣으면 중복이 됩니다).")
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
        """남은 작업을 **먼저 처리하고** 종료한다.

        [Fix 2026-09-05] 종전에는 `_running = False` 를 먼저 세웠다. 그러면 루프가 다음
         바퀴에서 조건에 걸려 끊기므로, 큐에 남아 있던 작업이 **처리되지 않은 채 사라진다**.
         종료 화면은 그동안 "DB 작업 큐 처리 및 종료 [완료]" 라고 말하고 있었다 —
         비우지 않은 것을 비웠다고 알린 셈이다.

         종료 신호(None)는 이미 쌓인 작업들 **뒤에** 들어가므로, 그것을 만났다는 것은
         앞의 작업을 전부 처리했다는 뜻이다. 플래그는 그때 내린다.
        """
        self._queue.put(None)

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

    def _require_worker(self, what):
        """워커가 살아 있는지 먼저 본다. 죽었으면 **곧바로** 실패한다.

        [왜 필요한가 · 2026-09-06] 종전에는 워커 생사와 무관하게 큐에 넣고 결과를
         기다렸다. 워커가 없으면 아무도 꺼내지 않으므로 호출부는 **30초를 꼬박 기다린
         뒤에야** 실패를 안다(실측: 30.0초). 매매 루프에서 30초는 치명적이다 — 보유
         종목마다 이 대기가 붙으면 한 주기가 분 단위로 늘어나 손절·트레일링 판정이
         통째로 밀린다. 게다가 그 사이 큐에는 실행되지 않을 작업이 쌓인다.
         워커는 종료(shutdown) 뒤에도 이 상태가 되므로 가정이 아니다.
        """
        if not self._worker.is_alive():
            raise RuntimeError(
                f"DB 워커가 살아 있지 않습니다 — '{what}' 를 수행할 수 없습니다"
                f"(종료 중이거나 워커 스레드가 죽었습니다). 대기하지 않고 즉시 알립니다.")

    def execute_custom(self, func, *args, **kwargs):
        """
        임의의 함수를 DB 전담 워커 스레드에서 실행하도록 위임합니다.
        직접 sqlite3 연결이 필요하거나 여러 쿼리를 하나의 트랜잭션으로 묶어야 할 때 사용합니다.
        """
        self._require_worker("__CUSTOM__")
        result_queue = queue.Queue()
        result_queue._dbq_queued_at = time.time()
        # 특수 메서드명 __CUSTOM__을 사용하여 메인 큐에 적재
        self._queue.put(("__CUSTOM__", (func, args, kwargs), {}, result_queue,
                         getattr(context.trade_context, 'use_auto_account', False)))
        
        try:
            # 워커 스레드가 작업을 마치고 결과를 돌려줄 때까지 대기 (최대 30초)
            status, res = result_queue.get(timeout=OP_TIMEOUT_SEC)
            if status == "ERROR":
                raise res
            return res
        except queue.Empty:
            logger.error(f"[DBQueue] 트랜잭션 작업이 시한({OP_TIMEOUT_SEC}s) 안에 끝나지 "
                         f"않았습니다 — 취소하지 않으므로 **나중에 반영될 수 있습니다**")
            raise DBOperationUnknown(
                f"DB 트랜잭션 결과 불명 — 시한 {OP_TIMEOUT_SEC}s 초과. 작업은 취소되지 "
                f"않아 곧 반영될 수 있습니다. 같은 내용을 다시 넣지 마십시오(중복).")

    def __getattr__(self, name):
        """
        실제 DB 객체(DBManager)의 메서드 호출을 동적으로 가로챕니다.
        (예: db.insert_trade() 호출 시 이 함수가 작동)
        """
        attr = getattr(self._real_db, name)
        
        if callable(attr):
            def wrapper(*args, **kwargs):
                self._require_worker(name)
                # 호출한 스레드가 결과를 돌려받을 1회용 큐 생성
                result_queue = queue.Queue()
                result_queue._dbq_queued_at = time.time()
                # (메서드명, 인자, 키워드인자, 결과큐, 계좌컨텍스트)를 메인 작업 큐에 전달.
                #  계좌 컨텍스트는 **호출 스레드에서** 읽어야 한다 — 워커 스레드에서 읽으면
                #  항상 기본값(수동 계좌)이다(DBWorker.run 주석 참조).
                self._queue.put((name, args, kwargs, result_queue,
                                 getattr(context.trade_context, 'use_auto_account', False)))
                
                try:
                    # 30초 타임아웃 설정 (무한 대기 방지)
                    status, res = result_queue.get(timeout=OP_TIMEOUT_SEC)
                    if status == "ERROR":
                        raise res
                    return res
                except queue.Empty:
                    logger.error(f"[DBQueue] '{name}' 이 시한({OP_TIMEOUT_SEC}s) 안에 끝나지 "
                                 f"않았습니다 — 취소하지 않으므로 **나중에 반영될 수 있습니다**")
                    raise DBOperationUnknown(
                        f"DB '{name}' 결과 불명 — 시한 {OP_TIMEOUT_SEC}s 초과. 작업은 "
                        f"취소되지 않아 곧 반영될 수 있습니다. 같은 내용을 다시 넣지 "
                        f"마십시오(중복).")
            return wrapper
        return attr

    def stop(self, timeout=5.0):
        """워커를 정리한다. **큐를 다 비웠으면 True.**

        비우지 못한 채 시간이 다하면 그 사실을 로그로 드러낸다 — 조용히 넘기면 종료
        직전의 체결 기록·트레일링 최고가 갱신이 사라진 것을 아무도 모른다. 이어지는
        VACUUM 이 아직 쓰고 있는 워커와 부딪히는 것도 이 신호로 설명된다.
        """
        if not self._worker.is_alive():
            return True
        self._worker.stop()
        self._worker.join(timeout=timeout)
        left = self._queue.qsize()
        if self._worker.is_alive() or left:
            logger.warning(f"[DBQueue] 종료 시한({timeout:.0f}s) 안에 큐를 비우지 못했습니다 "
                           f"— 남은 작업 {left}건. 그 기록은 저장되지 않았습니다.")
            return False
        return True

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

def shutdown(timeout=5.0):
    """시스템 종료 시 호출되어 Proxy를 안전하게 끕니다. 큐를 다 비웠으면 True."""
    global _proxy_instance
    if _proxy_instance:
        return _proxy_instance.stop(timeout=timeout)
    return True
