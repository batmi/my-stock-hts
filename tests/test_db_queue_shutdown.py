"""DB 작업 큐는 **비우고 나서** 닫는다.

[왜 이 파일이 있나 · 2026-09-05]
모든 DB 쓰기는 단일 워커 스레드로 직렬화된다('database is locked' 방지). 그 워커의
종료가 이렇게 생겼었다:

    def stop(self):
        self._running = False      # ← 먼저 내리고
        self._queue.put(None)      # ← 그 다음 신호

    while self._running:           # ← 다음 바퀴에서 곧바로 끊긴다
        task = self._queue.get(timeout=0.5)

플래그를 먼저 내리므로 **큐에 남은 작업이 처리되지 않은 채 루프가 끊긴다.** 그런데 종료
화면은 "[3/4] DB 작업 큐 처리 및 종료 [완료]" 라고 말했다 — 비우지 않은 것을 비웠다고
알린 셈이다. 종료 직전에 들어온 체결 기록(insert_trade)·트레일링 최고가 갱신이 사라진다.

종료 신호는 이미 쌓인 작업 **뒤에** 들어가므로, 그것을 만났다는 것이 곧 '앞을 다 처리했다'
는 뜻이다. 플래그는 그때 내린다. 그래도 시한 안에 못 비우면 **그 사실을 드러낸다** —
조용히 넘기면 사라진 기록을 아무도 모르고, 뒤이은 VACUUM 이 아직 쓰고 있는 워커와
부딪히는 것도 설명되지 않는다.
"""
import queue
import threading
import time

from modules import db_queue


class _SlowDB:
    """호출을 기록하는 가짜 DB. 각 호출이 조금 걸린다(큐가 쌓이도록)."""

    def __init__(self, delay=0.02):
        self.calls = []
        self.delay = delay
        self._lock = threading.Lock()

    def write(self, n):
        time.sleep(self.delay)
        with self._lock:
            self.calls.append(n)
        return n


def _worker_with_queue(real_db):
    q = queue.Queue()
    w = db_queue.DBWorker(q, real_db)
    w.start()
    return q, w


def test_종료_전에_쌓인_작업을_모두_처리한다():
    """[핵심] 대기 중인 쓰기가 종료 신호에 밀려 사라지면 안 된다."""
    db = _SlowDB()
    q, w = _worker_with_queue(db)
    try:
        for i in range(20):
            q.put(("write", (i,), {}, None, False))
        w.stop()                      # 남은 19건이 처리되어야 한다
        w.join(timeout=10)
        assert not w.is_alive(), "워커가 종료되지 않았다"
    finally:
        w._running = False
    assert sorted(db.calls) == list(range(20)), (
        f"{20 - len(db.calls)}건이 처리되지 않고 사라졌다: {sorted(db.calls)}")


def test_종료_신호를_만나야_루프가_끝난다():
    """플래그를 먼저 내리는 옛 방식으로 되돌아가지 않았는지 구조로 못박는다."""
    import ast
    import inspect
    import textwrap

    fn = ast.parse(textwrap.dedent(inspect.getsource(db_queue.DBWorker.stop))).body[0]
    #  주석·독스트링이 아니라 **실행되는 문장**만 본다(이 파일의 설명 문장이 걸리지 않게).
    stmts = [n for n in fn.body if not (isinstance(n, ast.Expr)
                                        and isinstance(n.value, ast.Constant)
                                        and isinstance(n.value.value, str))]
    code = "\n".join(ast.unparse(n) for n in stmts)
    assert "_running = False" not in code, (
        f"stop() 이 큐를 비우기 전에 루프를 끊는다 — 대기 중인 쓰기가 사라진다:\n{code}")
    assert "put(None)" in code, code


def test_비우지_못하면_그_사실을_돌려준다():
    """조용히 '완료'라고 말하지 않는다 — 화면 문구가 이 반환값을 읽는다."""
    db = _SlowDB(delay=0.5)           # 한 건에 0.5초 — 시한 안에 못 비운다
    proxy = db_queue.DBProxy.__new__(db_queue.DBProxy)
    proxy._real_db = db
    proxy._queue = queue.Queue()
    proxy._worker = db_queue.DBWorker(proxy._queue, db)
    proxy._worker.start()
    try:
        for i in range(20):
            proxy._queue.put(("write", (i,), {}, None, False))
        assert proxy.stop(timeout=0.3) is False, "못 비웠는데 비웠다고 답했다"
    finally:
        proxy._worker._running = False
        proxy._queue.put(None)
        proxy._worker.join(timeout=5)


def test_다_비우면_True():
    db = _SlowDB(delay=0.001)
    proxy = db_queue.DBProxy.__new__(db_queue.DBProxy)
    proxy._real_db = db
    proxy._queue = queue.Queue()
    proxy._worker = db_queue.DBWorker(proxy._queue, db)
    proxy._worker.start()
    for i in range(5):
        proxy._queue.put(("write", (i,), {}, None, False))
    assert proxy.stop(timeout=10) is True
    assert sorted(db.calls) == [0, 1, 2, 3, 4]


def test_계좌_컨텍스트가_워커로_전달된다():
    """DB 기록의 계좌 귀속은 호출 스레드의 thread-local 에서 온다."""
    from core import context

    seen = {}

    class _DB:
        def write(self, n):
            seen['use_auto'] = getattr(context.trade_context, 'use_auto_account', False)
            return n

    q, w = _worker_with_queue(_DB())
    try:
        rq = queue.Queue()
        q.put(("write", (1,), {}, rq, True))
        assert rq.get(timeout=5)[0] == "OK"
        assert seen['use_auto'] is True, "워커가 계좌 컨텍스트를 물려받지 못했다"
    finally:
        w.stop()
        w.join(timeout=5)


# ══════════════════════════════════════════════════════════════════════
# 워커가 없으면 기다리지 않는다 (감사 2026-09-06)
# ══════════════════════════════════════════════════════════════════════

class _PingDB:
    def ping(self):
        return "pong"

    def boom(self):
        raise RuntimeError("db error")


def test_워커가_죽었으면_30초_기다리지_않고_즉시_실패한다():
    """[왜] 종전에는 워커 생사와 무관하게 큐에 넣고 결과를 기다렸다. 아무도 꺼내지
    않으므로 호출부는 **30초를 꼬박 기다린 뒤에야** 실패를 안다(실측 30.0초).

    매매 루프에서 30초는 치명적이다 — 보유 종목마다 이 대기가 붙으면 한 주기가 분
    단위로 늘어나 손절·트레일링 판정이 통째로 밀린다. 워커는 종료(shutdown) 뒤에도
    이 상태가 되므로 가정이 아니다.
    """
    proxy = db_queue.DBProxy(_PingDB())
    assert proxy.ping() == "pong"

    proxy.stop(timeout=2.0)
    assert not proxy._worker.is_alive()

    t0 = time.time()
    try:
        proxy.ping()
        raised = None
    except Exception as e:      # noqa: BLE001
        raised = e
    elapsed = time.time() - t0

    assert raised is not None, "워커가 없는데 호출이 성공한 것처럼 돌아왔다"
    assert elapsed < 1.0, f"즉시 실패해야 하는데 {elapsed:.1f}초를 기다렸다"
    assert "워커" in str(raised)


def test_execute_custom도_같은_규칙을_따른다():
    proxy = db_queue.DBProxy(_PingDB())
    proxy.stop(timeout=2.0)

    t0 = time.time()
    try:
        proxy.execute_custom(lambda: 1)
        raised = None
    except Exception as e:      # noqa: BLE001
        raised = e
    assert raised is not None and time.time() - t0 < 1.0


def test_살아있는_워커의_정상_경로는_그대로다():
    """빠르게 실패하려다 정상 경로를 막으면 안 된다."""
    proxy = db_queue.DBProxy(_PingDB())
    try:
        assert proxy.ping() == "pong"
        assert proxy.execute_custom(lambda x: x * 2, 21) == 42
        # 워커 안에서 난 예외는 종전대로 호출부로 올라온다.
        try:
            proxy.boom()
            raised = None
        except Exception as e:      # noqa: BLE001
            raised = e
        assert isinstance(raised, RuntimeError) and "db error" in str(raised)
    finally:
        proxy.stop(timeout=2.0)
