# modules/db_manager.py
import sqlite3
import json
import logging
import threading
import os
import shutil
import time
from datetime import datetime, timedelta
import config
from core import context
import atexit

logger = logging.getLogger(__name__)


class DBManager:
    def __init__(self):
        self.db_path = config.DB_FILE_PATH
        self.lock = threading.RLock() # 스레드 간 동기화를 위한 락 (RLock으로 변경)
        self.local = threading.local() # 스레드별 로컬 저장소
        # [추가] 전체 스레드 연결 추적 레지스트리 {thread_id: connection}.
        #  스레드 로컬 연결은 그 스레드가 직접 닫지 않으면 메인에서 정리할 수 없어
        #  ResourceWarning(unclosed database)을 남긴다. 모든 연결을 추적해
        #  close_all_connections()로 일괄 정리할 수 있게 한다.
        #  (sqlite3.Connection은 weakref 불가하므로 thread_id 키의 일반 dict 사용)
        self._all_conns = {}
        self._all_conns_lock = threading.Lock()
        # [추가] 연결 세대. close_all_connections/switch_path가 증가시키면 각 스레드는
        #  다음 사용 시 자신의 캐시된 연결이 낡았음을 알고 새로 연다.
        #  (이게 없으면 다른 스레드의 thread-local이 '이미 닫힌 연결'을 계속 들고 있다가
        #   ProgrammingError: Cannot operate on a closed database 로 터진다)
        self._generation = 0
        # [안전장치] 쓰기 실패 이력. 종전에는 실패해도 console.print 한 줄이 전부였고
        #  (그마저 SCREEN_DEBUG_LEVEL=OFF면 안 나온다) 호출부는 반환값이 없어 실패를
        #  알 수 없었다. 인메모리 캐시는 갱신되므로 **그 세션 동안은 정상으로 보이고,
        #  재기동해야 소실이 드러난다** — 트레일링 최고가가 그렇게 사라지면 청산선이
        #  통째로 어긋난다. 라즈베리파이 SD카드 가득 참·I/O 오류가 실제 발생 조건이다.
        self._write_failures = {'count': 0, 'last_op': '', 'last_error': '',
                                'last_at': None, 'recent': []}
        self._wf_lock = threading.Lock()
        self._init_db()

    #  유실 이력 보관 상한(1GB 라즈베리파이 — 무한히 쌓으면 안 된다)
    WRITE_FAILURE_KEEP = 20

    def _note_write_failure(self, op, detail, error):
        """쓰기 실패를 로그 파일과 카운터에 남긴다.

        console.print 만으로는 안 된다 — 헤드리스 운영이라 보는 사람이 없고,
        로그 파일에도 안 남아 사후 추적이 불가능하다.
        """
        logger.error(f"[DB] 쓰기 실패 {op}({detail}): {error}")
        with self._wf_lock:
            self._write_failures['count'] += 1
            self._write_failures['last_op'] = op
            self._write_failures['last_error'] = str(error)[:200]
            self._write_failures['last_at'] = time.time()
            self._write_failures['recent'].append(
                (datetime.now().strftime("%H:%M:%S"), op, str(detail)))
            del self._write_failures['recent'][:-self.WRITE_FAILURE_KEEP]

    def get_write_failures(self):
        with self._wf_lock:
            return dict(self._write_failures, recent=list(self._write_failures['recent']))

    def reset_write_failures(self):
        with self._wf_lock:
            self._write_failures.update({'count': 0, 'last_op': '', 'last_error': '',
                                         'last_at': None, 'recent': []})

    def disk_free_mb(self):
        """DB가 있는 파티션의 남은 공간(MB). 알 수 없으면 -1."""
        try:
            usage = shutil.disk_usage(os.path.dirname(os.path.abspath(self.db_path)) or '.')
            return usage.free / (1024 * 1024)
        except Exception:
            return -1.0

    def __del__(self):
        """객체 소멸 시 모든 연결 종료"""
        try:
            self.close_all_connections()
        except Exception: pass

    def close_connection(self):
        """현재 스레드의 DB 연결을 명시적으로 종료"""
        try:
            if hasattr(self.local, 'conn') and self.local.conn:
                self.local.conn.close()
                self.local.conn = None
                with self._all_conns_lock:
                    self._all_conns.pop(threading.get_ident(), None)
        except Exception: pass

    def close_all_connections(self):
        """모든 스레드에서 생성된 DB 연결을 닫는다.

        백그라운드 워커 스레드가 직접 close_connection()을 호출하지 못한 채
        종료되면 그 스레드 로컬 연결이 GC 시점까지 열린 채로 남아
        ResourceWarning을 유발한다. 테스트 정리(conftest) 및 프로그램 종료 시
        호출하여 모든 추적 연결을 일괄 정리한다.
        (check_same_thread=False로 생성하므로 다른 스레드에서 close 가능)
        """
        with self._all_conns_lock:
            conns = list(self._all_conns.values())
            self._all_conns.clear()
        self._generation += 1   # 다른 스레드가 닫힌 연결을 재사용하지 않도록 무효화
        for c in conns:
            try:
                c.close()
            except Exception:
                pass
        try:
            if hasattr(self.local, 'conn'):
                self.local.conn = None
        except Exception:
            pass

    def _get_conn(self):
        """스레드별 DB 연결 객체 반환 (없으면 생성)

        세대(_generation)가 바뀌었으면 캐시된 연결이 닫혔거나 다른 DB 파일을 가리키므로
        버리고 새로 연다. 이 검사가 없으면 close_all_connections/switch_path 이후
        다른 스레드가 닫힌 연결을 계속 사용하게 된다.
        """
        gen = self._generation
        if (not hasattr(self.local, 'conn') or self.local.conn is None
                or getattr(self.local, 'gen', None) != gen):
            self.local.gen = gen
            # [수정] check_same_thread=False: 스레드 로컬 구조상 연결은 한 스레드만
            #  사용하므로 동시성 위험은 없으며, 정리(close)를 메인/정리 스레드에서
            #  수행할 수 있도록 스레드 검사를 끈다.
            conn = sqlite3.connect(self.db_path, timeout=60, check_same_thread=False)
            conn.execute("PRAGMA journal_mode=WAL;") # WAL 모드 설정
            conn.row_factory = sqlite3.Row
            self.local.conn = conn
            with self._all_conns_lock:
                self._all_conns[threading.get_ident()] = conn
        return self.local.conn

    def _is_screen_output_allowed(self):
        return threading.current_thread().name != "TelegramBot"

    def switch_path(self, new_path):
        """DB 파일을 통째로 교체한다 (관찰 모드 전용).

        db 인스턴스는 모듈 import 시점에 생성되므로 세션 모드가 정해지기 전에
        실계좌 경로로 고정된다. 관찰 모드는 실계좌와 **파일을 분리**해야 하므로
        (trailing_stops·half_tp_status가 code를 PK로 써서 같은 파일을 공유하면
         실계좌 포지션의 최고가가 페이퍼 포지션에 섞인다) 세션 초기화 시 이 함수로 갈아끼운다.
        열려 있는 모든 스레드 연결을 닫고 새 경로로 테이블을 재생성한다.
        """
        if not new_path or new_path == self.db_path:
            return
        self.close_all_connections()
        with self.lock:
            self.db_path = new_path
            self._generation += 1
        self._init_db()
        logger.info(f"[DB] 데이터베이스 경로 전환: {new_path}")

    def get_connection(self):
        """현재 스레드의 연결을 반환한다(외부 모듈이 직접 커서를 쓸 때)."""
        return self._get_conn()

    def execute_query(self, query, params=(), fetch=None):
        """범용 쿼리 실행 헬퍼. fetch: None(쓰기) / 'one' / 'all'.

        기존 메서드들과 동일하게 락 + 잠김 재시도를 적용한다.
        """
        with self.lock:
            for attempt in range(5):
                try:
                    conn = self._get_conn()
                    cursor = conn.cursor()
                    cursor.execute(query, params)
                    if fetch == 'one':
                        return cursor.fetchone()
                    if fetch == 'all':
                        return cursor.fetchall()
                    conn.commit()
                    return None
                except sqlite3.OperationalError as e:
                    if "locked" in str(e) and attempt < 4:
                        time.sleep(0.5)
                        continue
                    logger.error(f"[DB] execute_query 실패: {e} | {query[:80]}")
                    raise
        return None

    def _init_db(self):
        """DB 초기화 (테이블 생성 등) - 메인 스레드에서 한 번만 실행"""
        with self.lock:
            conn = None
            try:
                conn = sqlite3.connect(self.db_path, timeout=60)
                conn.execute("PRAGMA journal_mode=WAL;")
                cursor = conn.cursor()
                
                # 거래 내역 테이블 생성
                cursor.execute('''
                    CREATE TABLE IF NOT EXISTS trades (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        time TEXT,
                        type TEXT,
                        code TEXT,
                        name TEXT,
                        qty TEXT,
                        price TEXT,
                        odno TEXT,
                        org_odno TEXT,
                        account TEXT,
                        is_sim INTEGER,
                        snapshot TEXT
                    )
                ''')

                # [최적화] 자동매매 주기의 배치 조회(WHERE code IN ...)가 풀스캔하지 않도록 종목코드 인덱스
                #  (라즈베리파이 SD카드 SQLite I/O 절감. 기존 DB에도 IF NOT EXISTS로 안전 적용)
                cursor.execute("CREATE INDEX IF NOT EXISTS idx_trades_code ON trades(code)")

                # 트레일링 스탑 추적 테이블 생성
                cursor.execute('''
                    CREATE TABLE IF NOT EXISTS trailing_stops (
                        code TEXT PRIMARY KEY,
                        highest_price REAL,
                        update_time TEXT,
                        ref_avg_price REAL DEFAULT 0.0,
                        ref_pchs_amt REAL DEFAULT 0.0,
                        pyramid_count INTEGER DEFAULT 0
                    )
                ''')
                
                # [추가] 종목별 개별 트레이딩 룰 테이블 생성
                cursor.execute('''
                    CREATE TABLE IF NOT EXISTS stock_strategies (
                        code TEXT PRIMARY KEY,
                        name TEXT,
                        buy_score REAL,
                        buy_rsi REAL,
                        buy_vol_strength REAL,
                        sell_score REAL,
                        stop_loss REAL,
                        take_profit REAL,
                        take_profit_rsi REAL,
                        ts_activation REAL,
                        ts_callback REAL,
                        updated_at TEXT,
                        memo TEXT,
                        weights TEXT,
                        invest_ratio REAL,
                        time_stop_days INTEGER,
                        use_atr_stop INTEGER,
                        atr_stop_multiplier REAL,
                        half_take_profit_use INTEGER
                    )
                ''')
                
                # [Fix: Point 2] 반익절(Half TP) 상태 추적 테이블 생성
                cursor.execute('''
                    CREATE TABLE IF NOT EXISTS half_tp_status (
                        code TEXT PRIMARY KEY,
                        update_time TEXT
                    )
                ''')
                
                # [추가] 일별 자산 스냅샷 테이블 생성
                cursor.execute('''
                    CREATE TABLE IF NOT EXISTS daily_asset_history (
                        date TEXT,
                        account TEXT,
                        asset REAL,
                        PRIMARY KEY (date, account)
                    )
                ''')

                # [추가] 공시 알림 중복방지 테이블 (접수번호 기준)
                cursor.execute('''
                    CREATE TABLE IF NOT EXISTS notified_disclosures (
                        rcept_no TEXT PRIMARY KEY,
                        notified_at TEXT
                    )
                ''')

                # [추가] 매매일지 웹서버 전송 대기열 (Outbox 패턴)
                #  체결 기록과 '같은 트랜잭션'으로 적재해 두고 백그라운드 워커가 배치 전송한다.
                #  체결 처리 루프에서 직접 HTTP를 때리면 네트워크 지연에 매매가 묶이고,
                #  전송 실패 시 그 기록이 그대로 유실된다. 큐에 남겨야 재부팅·단절 후에도 복구된다.
                cursor.execute('''
                    CREATE TABLE IF NOT EXISTS journal_outbox (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        exec_id TEXT UNIQUE,
                        payload TEXT,
                        created_at TEXT,
                        attempts INTEGER DEFAULT 0,
                        last_attempt_at TEXT,
                        last_error TEXT,
                        synced_at TEXT,
                        remote_id TEXT,
                        dead_at TEXT,
                        reject_count INTEGER DEFAULT 0,
                        is_backlog INTEGER DEFAULT 0
                    )
                ''')
                # dead_at      : 서버가 반복 거절한 행을 대기열에서 뺀다. 지우지 않고 표시만
                #                하는 이유 — 무엇이 왜 못 나갔는지 last_error 와 함께 남아야
                #                운용자가 원인을 찾는다.
                # reject_count : '서버가 이 건을 명시적으로 거절한' 횟수만 센다. attempts 와
                #                반드시 분리해야 한다 — attempts 로 포기 판정을 하면 웹서버가
                #                반나절만 죽어 있어도 대기열 전체가 통째로 폐기된다.
                cursor.execute("PRAGMA table_info(journal_outbox)")
                outbox_columns = [info[1] for info in cursor.fetchall()]
                # is_backlog  : 재동기화처럼 뒤늦게 밀어 넣은 행. 정렬에서 뒤로 보내
                #               실시간 체결이 대량 backlog 뒤에 줄 서지 않게 한다.
                for col, dtype in (("dead_at", "TEXT"),
                                   ("reject_count", "INTEGER DEFAULT 0"),
                                   ("is_backlog", "INTEGER DEFAULT 0")):
                    if col not in outbox_columns:
                        try:
                            cursor.execute(f"ALTER TABLE journal_outbox ADD COLUMN {col} {dtype}")
                        except Exception as e:
                            config.console.print(
                                f"[red][DB] journal_outbox 컬럼 추가 실패({col}): {e}[/red]")
                # 전송 대기 행 조회는 synced_at·dead_at 이 모두 NULL 인 것만 보고,
                # is_backlog 순으로 실시간 체결을 먼저 집는다.
                #  CREATE INDEX IF NOT EXISTS 는 '이름'만 보므로, 정의를 바꿀 때는
                #  이름에 버전을 올려야 한다. 같은 이름을 쓰면 기존 DB 는 옛 정의를
                #  그대로 들고 있게 되어 새 정렬이 인덱스를 타지 못한다.
                for stale in ('idx_journal_outbox_pending', 'idx_journal_outbox_queue'):
                    cursor.execute(f"DROP INDEX IF EXISTS {stale}")
                cursor.execute(
                    "CREATE INDEX IF NOT EXISTS idx_journal_outbox_queue_v2 "
                    "ON journal_outbox(synced_at, dead_at, is_backlog, id)")
                
                # [추가] 예약 주문 테이블 생성
                cursor.execute('''
                    CREATE TABLE IF NOT EXISTS reserved_orders (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        cano TEXT, acnt TEXT, market TEXT,
                        order_type TEXT, code TEXT, name TEXT,
                        qty INTEGER, order_price REAL,
                        condition_type TEXT, target_price REAL, target_time TEXT,
                        status TEXT DEFAULT 'PENDING', odno TEXT,
                        fail_reason TEXT,
                        expire_dt TEXT,
                        lowest_price REAL DEFAULT 0.0,
                        highest_price REAL DEFAULT 0.0,
                        composite_json TEXT,
                        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                    )
                ''')

                # [추가] 권리 조정 감시 기준점.
                #  예약 주문이 걸린 **미보유** 종목은 잔고가 없어 평단 비교(engine.detect_
                #  corporate_action)를 쓸 수 없다. 대신 '우리가 기록해 둔 과거 종가'와
                #  '오늘 조회한 같은 날짜의 종가'를 맞대 본다. 권리 조정이 나면 거래소가
                #  과거 시세를 소급 수정하므로 그 차이가 곧 조정 배율이다.
                cursor.execute('''
                    CREATE TABLE IF NOT EXISTS corp_action_refs (
                        code TEXT PRIMARY KEY,
                        ref_date TEXT,
                        ref_close REAL DEFAULT 0.0,
                        source TEXT,
                        updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                    )
                ''')

                # [추가 2026-08-19] 신호 원장 — 매수 신호가 게이트에서 어떻게 됐는가.
                #  [왜 필요한가] 실매매에만 있는 게이트(체결강도·매도잔량비·재진입 차단)는
                #   일봉 백테스트에 아예 없어 이 기계의 운영 기록으로만 셀 수 있다. 그런데
                #   그 기록이 로그 **문자열**뿐이라 두 가지가 걸렸다.
                #    ① 30일 뒤 지워진다 — config가 "3개월쯤 쌓여야 답할 수 있다"고 적어 둔
                #       바로 그 증거를. 그래서 감사 창이 영원히 18거래일에 묶여 있었다.
                #    ② 파싱이 위험하다 — `[매도비:3.92]`(정보 표기)와 `매도비:3.92<1.0`
                #       (차단)을 혼동해 차단율을 1.3% → 75%로 잘못 보고한 적이 있다.
                #  [왜 하루 1행인가] 주기마다 1행이면 44종목×약 390주기 = 하루 17,000행이라
                #   파이3 SD카드에 부담이다. 감사가 묻는 것은 "그날 그 신호가 한 번도 못
                #   뚫었는가(완전 차단), 일부만 막혔는가(부분 차단)"이므로 **(일자, 종목)당
                #   1행에 주기 수를 세면 충분**하다. 하루 최대 44행 → 1년에 1.5MB 남짓.
                #  [주의] 매수 상태였던 주기만 센다. 애초에 신호가 아니었던 것까지 세면
                #   차단율의 분모가 부풀어 기회비용을 과대평가한다.
                cursor.execute('''
                    CREATE TABLE IF NOT EXISTS signal_ledger (
                        date TEXT,
                        code TEXT,
                        -- 실전/모의는 같은 DB 파일을 쓰고 trades는 is_sim으로 가른다.
                        --  원장도 같이 갈라야 한다 — 재진입 차단·상관 차단은 그 계좌의
                        --  보유 상태에 따라 달라지므로, 섞으면 차단율이 무엇을 뜻하는지
                        --  알 수 없게 된다(관찰모드는 DB 파일 자체가 분리된다).
                        is_sim INTEGER DEFAULT 0,
                        name TEXT,
                        cycles INTEGER DEFAULT 0,
                        passed INTEGER DEFAULT 0,
                        blocked_vol INTEGER DEFAULT 0,
                        blocked_abr INTEGER DEFAULT 0,
                        blocked_hold INTEGER DEFAULT 0,
                        blocked_corr INTEGER DEFAULT 0,
                        blocked_rs INTEGER DEFAULT 0,
                        blocked_tq INTEGER DEFAULT 0,
                        blocked_reentry INTEGER DEFAULT 0,
                        blocked_other INTEGER DEFAULT 0,
                        max_score REAL DEFAULT 0.0,
                        max_vol REAL,
                        min_abr REAL,
                        last_state TEXT,
                        updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                        PRIMARY KEY (date, code, is_sim)
                    )
                ''')

                # [마이그레이션 2026-08-19] is_sim 없이 만들어진 원장을 옮긴다.
                #  PK가 (date, code) → (date, code, is_sim)로 바뀌므로 ALTER로는 안 되고
                #  테이블을 다시 만들어야 한다. 기존 행은 전부 실전(0)으로 본다 —
                #  원장은 2026-08-19에 신설됐고 그 전에는 모드 구분 없이 쌓인 적이 없다.
                cursor.execute("PRAGMA table_info(signal_ledger)")
                _led_cols = [c[1] for c in cursor.fetchall()]
                if _led_cols and "is_sim" not in _led_cols:
                    cursor.execute("ALTER TABLE signal_ledger RENAME TO signal_ledger_old")
                    cursor.execute('''
                        CREATE TABLE signal_ledger (
                            date TEXT, code TEXT, is_sim INTEGER DEFAULT 0, name TEXT,
                            cycles INTEGER DEFAULT 0, passed INTEGER DEFAULT 0,
                            blocked_vol INTEGER DEFAULT 0, blocked_abr INTEGER DEFAULT 0,
                            blocked_hold INTEGER DEFAULT 0, blocked_corr INTEGER DEFAULT 0,
                            blocked_rs INTEGER DEFAULT 0, blocked_tq INTEGER DEFAULT 0,
                            blocked_reentry INTEGER DEFAULT 0, blocked_other INTEGER DEFAULT 0,
                            max_score REAL DEFAULT 0.0, max_vol REAL, min_abr REAL,
                            last_state TEXT,
                            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                            PRIMARY KEY (date, code, is_sim)
                        )
                    ''')
                    cursor.execute('''
                        INSERT INTO signal_ledger
                            (date, code, is_sim, name, cycles, passed, blocked_vol, blocked_abr,
                             blocked_hold, blocked_corr, blocked_rs, blocked_tq, blocked_reentry,
                             blocked_other, max_score, max_vol, min_abr, last_state, updated_at)
                        SELECT date, code, 0, name, cycles, passed, blocked_vol, blocked_abr,
                               blocked_hold, blocked_corr, blocked_rs, blocked_tq, blocked_reentry,
                               blocked_other, max_score, max_vol, min_abr, last_state, updated_at
                        FROM signal_ledger_old
                    ''')
                    cursor.execute("DROP TABLE signal_ledger_old")
                    print("[DB] 신호 원장에 계좌 구분(is_sim) 추가됨")

                # 컬럼 확장 (마이그레이션)
                cursor.execute("PRAGMA table_info(trades)")
                columns = [info[1] for info in cursor.fetchall()]
                
                new_columns = {
                    "profit_amt": "INTEGER DEFAULT 0",
                    "profit_rate": "REAL DEFAULT 0.0",
                    "reason": "TEXT",
                    "strategy_score": "REAL DEFAULT 0",
                    "order_status": "TEXT DEFAULT '접수'",
                    "stop_loss_rate": "REAL DEFAULT 0.0",
                    # [비용] 매도 시점의 매입평균가. 체결 확인 단계에서 '실제 체결가' 기준으로
                    #  실현손익을 다시 계산하려면 매입가가 있어야 한다(주문 시점 추정치가 아니라).
                    "buy_price": "REAL DEFAULT 0.0",
                }
                
                for col, dtype in new_columns.items():
                    if col not in columns:
                        try:
                            cursor.execute(f"ALTER TABLE trades ADD COLUMN {col} {dtype}")
                            if self._is_screen_output_allowed() and config.SCREEN_DEBUG_LEVEL != "OFF":
                                config.console.print(f"[dim green][DB] 컬럼 추가됨: {col}[/dim green]")
                        except Exception as e:
                            config.console.print(f"[red][DB] 컬럼 추가 실패({col}): {e}[/red]")

                # stock_strategies 테이블 컬럼 확장 (memo 추가)
                cursor.execute("PRAGMA table_info(stock_strategies)")
                strat_columns = [info[1] for info in cursor.fetchall()]
                
                new_strat_columns = {
                    "buy_vol_strength": "REAL", "memo": "TEXT", "weights": "TEXT",
                    "invest_ratio": "REAL", "time_stop_days": "INTEGER",
                    "use_atr_stop": "INTEGER", "atr_stop_multiplier": "REAL",
                    "half_take_profit_use": "INTEGER"
                }
                for col, dtype in new_strat_columns.items():
                    if col not in strat_columns:
                        try:
                            cursor.execute(f"ALTER TABLE stock_strategies ADD COLUMN {col} {dtype}")
                            if self._is_screen_output_allowed() and config.SCREEN_DEBUG_LEVEL != "OFF":
                                config.console.print(f"[dim green][DB] stock_strategies 테이블에 {col} 컬럼 추가 완료[/dim green]")
                        except Exception as e:
                            config.console.print(f"[red][DB] stock_strategies 컬럼 추가 실패({col}): {e}[/red]")

                # [추가] trailing_stops 컬럼 확장 — 코퍼레이트 액션(액면분할·무상증자) 탐지용.
                #  최고가는 원시 가격이라 분할이 일어나면 조정 전 값으로 남는다. 직전 주기의
                #  매입평균단가·매입금액을 함께 들고 있어야 '평단만 바뀌고 매입금액은 그대로'인
                #  분할을 매수·매도와 구분할 수 있다(engine.detect_corporate_action 주석 참조).
                cursor.execute("PRAGMA table_info(trailing_stops)")
                ts_columns = [info[1] for info in cursor.fetchall()]
                # [추가] pyramid_count — 증액 횟수를 자유 텍스트 사유가 아니라 여기 둔다.
                #  (사유 파싱은 기록이 유실되면 0으로 읽혀 상한을 넘겨 증액된다)
                if "pyramid_count" not in ts_columns:
                    try:
                        cursor.execute("ALTER TABLE trailing_stops ADD COLUMN pyramid_count INTEGER DEFAULT 0")
                        if self._is_screen_output_allowed() and config.SCREEN_DEBUG_LEVEL != "OFF":
                            config.console.print("[dim green][DB] trailing_stops 테이블에 pyramid_count 컬럼 추가 완료[/dim green]")
                    except Exception as e:
                        config.console.print(f"[red][DB] trailing_stops 컬럼 추가 실패(pyramid_count): {e}[/red]")
                for col in ("ref_avg_price", "ref_pchs_amt"):
                    if col not in ts_columns:
                        try:
                            cursor.execute(f"ALTER TABLE trailing_stops ADD COLUMN {col} REAL DEFAULT 0.0")
                            if self._is_screen_output_allowed() and config.SCREEN_DEBUG_LEVEL != "OFF":
                                config.console.print(f"[dim green][DB] trailing_stops 테이블에 {col} 컬럼 추가 완료[/dim green]")
                        except Exception as e:
                            config.console.print(f"[red][DB] trailing_stops 컬럼 추가 실패({col}): {e}[/red]")

                # reserved_orders 테이블 컬럼 확장 (fail_reason 추가)
                cursor.execute("PRAGMA table_info(reserved_orders)")
                ro_columns = [info[1] for info in cursor.fetchall()]
                if "fail_reason" not in ro_columns:
                    try:
                        cursor.execute("ALTER TABLE reserved_orders ADD COLUMN fail_reason TEXT")
                        if self._is_screen_output_allowed() and config.SCREEN_DEBUG_LEVEL != "OFF":
                            config.console.print("[dim green][DB] reserved_orders 테이블에 fail_reason 컬럼 추가 완료[/dim green]")
                    except Exception as e:
                        config.console.print(f"[red][DB] reserved_orders 컬럼 추가 실패(fail_reason): {e}[/red]")

                if "expire_dt" not in ro_columns:
                    try:
                        cursor.execute("ALTER TABLE reserved_orders ADD COLUMN expire_dt TEXT")
                        if self._is_screen_output_allowed() and config.SCREEN_DEBUG_LEVEL != "OFF":
                            config.console.print("[dim green][DB] reserved_orders 테이블에 expire_dt 컬럼 추가 완료[/dim green]")
                    except Exception as e:
                        config.console.print(f"[red][DB] reserved_orders 컬럼 추가 실패(expire_dt): {e}[/red]")

                if "lowest_price" not in ro_columns:
                    try:
                        cursor.execute("ALTER TABLE reserved_orders ADD COLUMN lowest_price REAL DEFAULT 0.0")
                        if self._is_screen_output_allowed() and config.SCREEN_DEBUG_LEVEL != "OFF":
                            config.console.print("[dim green][DB] reserved_orders 테이블에 lowest_price 컬럼 추가 완료[/dim green]")
                    except Exception as e:
                        config.console.print(f"[red][DB] reserved_orders 컬럼 추가 실패(lowest_price): {e}[/red]")

                if "highest_price" not in ro_columns:
                    try:
                        cursor.execute("ALTER TABLE reserved_orders ADD COLUMN highest_price REAL DEFAULT 0.0")
                        if self._is_screen_output_allowed() and config.SCREEN_DEBUG_LEVEL != "OFF":
                            config.console.print("[dim green][DB] reserved_orders 테이블에 highest_price 컬럼 추가 완료[/dim green]")
                    except Exception as e:
                        config.console.print(f"[red][DB] reserved_orders 컬럼 추가 실패(highest_price): {e}[/red]")

                # [추가] 복합(AND) 조건 예약 주문용 컬럼 (서브 조건 리스트를 JSON으로 저장)
                if "composite_json" not in ro_columns:
                    try:
                        cursor.execute("ALTER TABLE reserved_orders ADD COLUMN composite_json TEXT")
                        if self._is_screen_output_allowed() and config.SCREEN_DEBUG_LEVEL != "OFF":
                            config.console.print("[dim green][DB] reserved_orders 테이블에 composite_json 컬럼 추가 완료[/dim green]")
                    except Exception as e:
                        config.console.print(f"[red][DB] reserved_orders 컬럼 추가 실패(composite_json): {e}[/red]")

                conn.commit()
            except Exception as e:
                if self._is_screen_output_allowed() and config.SCREEN_DEBUG_LEVEL != "OFF":
                    config.console.print(f"[red][DB] Init Error: {e}[/red]")
            finally:
                # [수정] 예외 발생 시에도 연결을 확실히 닫아 ResourceWarning(unclosed database) 방지
                if conn is not None:
                    conn.close()

    def _enqueue_journal(self, cursor, trade):
        """매매일지 웹서버 전송 대기열에 적재 (호출자의 트랜잭션 안에서 실행).

        연동이 꺼져 있거나 어떤 이유로든 실패해도 **거래 기록 저장은 절대 방해하지 않는다.**
        일지 전송은 부가 기능이고 거래 기록이 본질이므로, 여기서 예외를 올리면 안 된다.
        """
        try:
            from modules import journal_sync
            journal_sync.enqueue(cursor, trade)
        except Exception as e:
            # 거래 기록은 그대로 저장되지만 이 체결은 일지로 나가지 않는다 —
            # 조용히 넘기면 나중에 누락 원인을 못 찾으므로 반드시 남긴다.
            logger.warning(f"[Journal] 전송 대기열 적재 실패 (거래 기록은 정상 저장됨): {e}")

    def insert_trade(self, type_str, code, name, qty, price, odno, org_odno=None, snapshot=None, profit_amt=0, profit_rate=0.0, reason=None, score=0, order_status="접수", custom_time=None, stop_loss_rate=0.0, buy_price=0.0):
        """거래 내역 및 스냅샷 저장"""
        # 쓰기 작업은 락으로 보호하여 순차 처리 (SQLite 특성상 안전)
        with self.lock:
            for attempt in range(5):
                try:
                    if self._is_screen_output_allowed() and config.SCREEN_DEBUG_LEVEL == "DEBUG":
                        config.console.print(f"[dim cyan][DB][{threading.get_ident()}] insert_trade 요청 ({attempt+1}/5): {type_str} {name}({code})[/dim cyan]")

                    conn = self._get_conn()
                    cursor = conn.cursor()
                    
                    now_str = custom_time if custom_time else datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                    
                    acc_no = f"{config.session.cano}-{config.session.acnt_prdt_cd}"
                    if getattr(context.trade_context, 'use_auto_account', False) and config.session.auto_cano:
                        acc_no = f"{config.session.auto_cano}-{config.session.auto_acnt_prdt_cd}"
                    
                    is_sim = 0
                    snapshot_json = json.dumps(snapshot, ensure_ascii=False) if snapshot else "{}"
                    
                    cursor.execute('''
                        INSERT INTO trades (time, type, code, name, qty, price, odno, org_odno, account, is_sim, snapshot, profit_amt, profit_rate, reason, strategy_score, order_status, stop_loss_rate, buy_price)
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ''', (now_str, type_str, code, name, str(qty), str(price), odno, org_odno, acc_no, is_sim, snapshot_json, profit_amt, profit_rate, reason, score, order_status, stop_loss_rate, buy_price))

                    # [추가] 매매일지 웹서버 전송 대기열 적재.
                    #  거래 기록과 같은 트랜잭션에서 처리해야 '기록은 남았는데 전송 큐엔 없는'
                    #  틈이 생기지 않는다. 전송 자체는 백그라운드 워커가 담당한다.
                    self._enqueue_journal(cursor, {
                        'time': now_str, 'type': type_str, 'code': code, 'name': name,
                        'qty': qty, 'price': price, 'odno': odno, 'org_odno': org_odno,
                        'account': acc_no, 'is_sim': is_sim, 'profit_amt': profit_amt,
                        'profit_rate': profit_rate, 'reason': reason,
                        'strategy_score': score, 'order_status': order_status,
                        'stop_loss_rate': stop_loss_rate, 'buy_price': buy_price,
                    })

                    conn.commit()
                    
                    if self._is_screen_output_allowed() and config.SCREEN_DEBUG_LEVEL == "DEBUG":
                        config.console.print(f"[dim green][DB][{threading.get_ident()}] 거래 내역 저장 완료 (ODNO: {odno})[/dim green]")
                    return True

                except sqlite3.OperationalError as e:
                    if "locked" in str(e) and attempt < 4:
                        time.sleep(0.5)
                        continue
                    if self._is_screen_output_allowed() and config.SCREEN_DEBUG_LEVEL != "OFF":
                        config.console.print(f"[red][DB] Insert Error: {e}[/red]")
                    self._note_write_failure("거래 기록", f"{type_str} {code} No.{odno}", e)
                    return False
                except Exception as e:
                    if self._is_screen_output_allowed() and config.SCREEN_DEBUG_LEVEL != "OFF":
                        config.console.print(f"[red][DB] Insert Error: {e}[/red]")
                    self._note_write_failure("거래 기록", f"{type_str} {code} No.{odno}", e)
                    return False
            return False

    def get_trades(self, limit=None, start_date=None, end_date=None, code=None, is_auto=False, is_sim=None, order_status=None, account=None):
        """거래 내역 조회"""
        # 읽기 작업은 락 없이 수행 가능 (WAL 모드 덕분)
        try:
            conn = self._get_conn()
            cursor = conn.cursor()
            
            query = "SELECT * FROM trades WHERE 1=1"
            params = []
            
            if start_date:
                query += " AND time >= ?"
                params.append(f"{start_date} 00:00:00")
            if end_date:
                query += " AND time <= ?"
                params.append(f"{end_date} 23:59:59")
            if code:
                query += " AND (code LIKE ? OR name LIKE ?)"
                params.append(f"%{code}%")
                params.append(f"%{code}%")
            
            if is_auto:
                query += " AND type LIKE '%(AUTO)%'"
            
            if is_sim is not None:
                query += " AND is_sim = ?"
                params.append(1 if is_sim else 0)
            
            # [추가] 계좌 번호 필터링
            if account:
                query += " AND account = ?"
                params.append(account)
            
            # [추가] 주문 상태 필터링
            if order_status:
                query += " AND order_status = ?"
                params.append(order_status)
            
            query += " ORDER BY id DESC"
            
            if limit:
                query += " LIMIT ?"
                params.append(limit)
            
            cursor.execute(query, params)
            rows = cursor.fetchall()
            return [dict(row) for row in rows]
        except Exception as e:
            if self._is_screen_output_allowed() and config.SCREEN_DEBUG_LEVEL != "OFF":
                config.console.print(f"[red][DB] Select Error: {e}[/red]")
            return []
            
    def delete_trade_by_id(self, trade_id):
        """특정 ID의 거래 내역을 삭제"""
        with self.lock:
            for attempt in range(5):
                try:
                    conn = self._get_conn()
                    cursor = conn.cursor()
                    cursor.execute("DELETE FROM trades WHERE id = ?", (trade_id,))
                    deleted = cursor.rowcount
                    conn.commit()
                    return deleted > 0
                except sqlite3.OperationalError as e:
                    if "locked" in str(e) and attempt < 4:
                        time.sleep(0.5)
                        continue
                    return False
                except Exception:
                    return False
    
    def update_trade(self, odno, price=None, qty=None, profit_amt=None, profit_rate=None,
                     order_status=None, where_status=None):
        """주문번호(odno)를 기준으로 거래 내역 업데이트

        where_status: 지정하면 그 상태의 행만 갱신한다. 같은 odno로 '접수'와 '체결' 행이
          함께 존재하므로, 체결 수량 누적 갱신처럼 한쪽만 고쳐야 할 때 쓴다.
          (지정하지 않으면 종전대로 해당 odno의 모든 행을 갱신한다)
        """
        with self.lock:
            for attempt in range(5):
                try:
                    conn = self._get_conn()
                    cursor = conn.cursor()
                    
                    updates = []
                    params = []
                    
                    if price is not None: updates.append("price = ?"); params.append(str(price))
                    if qty is not None: updates.append("qty = ?"); params.append(str(qty))
                    if profit_amt is not None: updates.append("profit_amt = ?"); params.append(profit_amt)
                    if profit_rate is not None: updates.append("profit_rate = ?"); params.append(profit_rate)
                    if order_status is not None: updates.append("order_status = ?"); params.append(order_status)
                    
                    if updates:
                        params.append(odno)
                        where = "odno = ?"
                        if where_status is not None:
                            where += " AND order_status = ?"
                            params.append(where_status)
                        cursor.execute(f"UPDATE trades SET {', '.join(updates)} WHERE {where}", params)
                        conn.commit()
                    break
                except sqlite3.OperationalError as e:
                    if "locked" in str(e) and attempt < 4:
                        time.sleep(0.5)
                        continue
                    if self._is_screen_output_allowed() and config.SCREEN_DEBUG_LEVEL != "OFF":
                        config.console.print(f"[red][DB] Update Error: {e}[/red]")
                    break
                except Exception as e:
                    if self._is_screen_output_allowed() and config.SCREEN_DEBUG_LEVEL != "OFF":
                        config.console.print(f"[red][DB] Update Error: {e}[/red]")
                    break
    
    def check_trade_exists(self, odno, order_status):
        """특정 주문번호와 상태를 가진 거래 내역 존재 여부 확인"""
        try:
            conn = self._get_conn()
            cursor = conn.cursor()
            cursor.execute("SELECT count(*) FROM trades WHERE odno = ? AND order_status = ?", (odno, order_status))
            cnt = cursor.fetchone()[0]
            
            if self._is_screen_output_allowed() and config.SCREEN_DEBUG_LEVEL == "DEBUG" and cnt > 0:
                config.console.print(f"[dim yellow][DB] check_trade_exists: {odno} ({order_status}) -> 존재함[/dim yellow]")
            return cnt > 0
        except Exception: return False
            
    def get_original_order_type(self, odno):
        """주문번호로 원 주문(접수 상태)의 유형 조회"""
        try:
            conn = self._get_conn()
            cursor = conn.cursor()
            # [수정] 원본 주문 조회 시 '접수'뿐만 아니라 '정정', '취소' 상태도 조회 허용
            cursor.execute("SELECT type FROM trades WHERE odno = ? AND order_status IN ('접수', '정정', '취소') ORDER BY id DESC LIMIT 1", (odno,))
            row = cursor.fetchone()
            return row[0] if row else None
        except Exception: return None

    def get_trade_by_odno(self, odno):
        """주문번호로 원 주문(접수) 내역 조회"""
        try:
            conn = self._get_conn()
            cursor = conn.cursor()
            # [수정] 정정 주문에 의해 새로 생성된 odno도 찾을 수 있도록 상태 범위 확장
            cursor.execute("SELECT * FROM trades WHERE odno = ? AND order_status IN ('접수', '정정', '취소') ORDER BY id DESC LIMIT 1", (odno,))
            row = cursor.fetchone()
            return dict(row) if row else None
        except Exception: return None

    def get_cancel_record_by_org_odno(self, odno):
        """원주문번호(org_odno)로 가장 최근 취소 이력 1건 조회 (외부/사후 취소 중복 판별용)"""
        try:
            conn = self._get_conn()
            cursor = conn.cursor()
            cursor.execute(
                "SELECT id, reason FROM trades WHERE org_odno = ? AND order_status IN ('취소', '취소(추정)') ORDER BY id DESC LIMIT 1",
                (odno,)
            )
            row = cursor.fetchone()
            return dict(row) if row else None
        except Exception: return None

    def get_reserved_order_by_odno(self, odno):
        """주문번호(odno)로 발동된 예약 주문 1건 조회"""
        try:
            conn = self._get_conn()
            cursor = conn.cursor()
            cursor.execute("SELECT * FROM reserved_orders WHERE odno = ?", (str(odno),))
            row = cursor.fetchone()
            return dict(row) if row else None
        except Exception: return None

    def get_buy_trades_for_current_holding(self, code):
        """
        현재 보유 수량에 해당하는 매수 거래 내역들을 조회합니다.
        (마지막 매도 이후의 모든 매수 내역)
        """
        try:
            conn = self._get_conn()
            cursor = conn.cursor()
            
            # 1. 해당 종목의 마지막 매도 시점 조회
            cursor.execute("SELECT time FROM trades WHERE code = ? AND (type LIKE '%sell%' OR type LIKE '%매도%') ORDER BY id DESC LIMIT 1", (code,))
            last_sell_time = cursor.fetchone()
            
            # 2. 마지막 매도 이후의 모든 매수 내역 조회
            query = "SELECT * FROM trades WHERE code = ? AND (type LIKE '%buy%' OR type LIKE '%매수%')"
            params = [code]
            
            if last_sell_time:
                query += " AND time > ?"
                params.append(last_sell_time['time'])
            
            cursor.execute(query, params)
            rows = cursor.fetchall()
            return [dict(row) for row in rows]
        except Exception:
            return []

    def get_buy_trades_for_current_holdings(self, codes):
        """[배치] 여러 종목의 현재 보유분 매수 내역을 일괄 조회합니다. {code: [trades]}

        get_buy_trades_for_current_holding과 동일한 결과를 종목 수와 무관하게
        쿼리 2회로 반환한다 (자동매매 매도 분석 주기의 DB I/O 절감).
        """
        codes = [c for c in dict.fromkeys(codes or []) if c]
        result = {c: [] for c in codes}
        if not codes: return result
        try:
            conn = self._get_conn()
            cursor = conn.cursor()
            ph = ",".join("?" * len(codes))

            # 1. 종목별 마지막 매도 시각 일괄 조회
            cursor.execute(f"""
                SELECT code, time FROM trades
                WHERE id IN (
                    SELECT MAX(id) FROM trades
                    WHERE code IN ({ph}) AND (type LIKE '%sell%' OR type LIKE '%매도%')
                    GROUP BY code
                )""", codes)
            last_sell = {row['code']: row['time'] for row in cursor.fetchall()}

            # 2. 매수 내역 일괄 조회 후 종목별 '마지막 매도 이후' 필터링
            cursor.execute(f"SELECT * FROM trades WHERE code IN ({ph}) AND (type LIKE '%buy%' OR type LIKE '%매수%')", codes)
            for row in cursor.fetchall():
                t = dict(row)
                ls = last_sell.get(t['code'])
                if ls is not None and not (t.get('time') and t['time'] > ls):
                    continue
                result[t['code']].append(t)
            return result
        except Exception:
            return result

    def get_position_entry_dates(self, codes):
        """[배치] 현재 보유 포지션의 진입일을 일괄 조회합니다. {code: 'YYYY-MM-DD'}

        get_position_entry_info의 진입일만 추린 편의 함수.
        """
        return {c: v['date'] for c, v in self.get_position_entry_info(codes).items() if v.get('date')}

    def get_position_entry_info(self, codes):
        """[배치] 진입일과 재생 결과 수량을 함께 돌려줍니다.
        {code: {'date': 'YYYY-MM-DD'|None, 'qty': int}}

        진입일 = 누적 보유수량이 0에서 1 이상으로 바뀐 '마지막' 시점.
        부분 매도(반익절)는 포지션을 끊지 않으므로 '마지막 매도 이후 첫 매수'로는 진입일을
        구할 수 없고, 분할 매수·피라미딩의 '최근 매수'를 쓰면 1주만 더 담아도 보유일수가
        0으로 리셋된다. 그래서 체결 내역을 시간순으로 재생해 수량 흐름으로 판정한다.

        trades에는 접수·정정·취소 행이 섞여 있어 체결(order_status='체결') 행만 집계한다.

        [중요] qty(재생 수량)를 함께 주는 이유 — 이 DB는 증권사 이력의 부분 사본이다.
        시스템을 쓰기 전부터 들고 있던 포지션은 첫 기록이 '0 → 1 이상'처럼 보여 진입일이
        DB 최초 기록일로 굳는다(실측: 228주 보유인데 DB엔 2주만 기록 → 진입일 오판).
        호출부가 재생 수량과 실제 잔고 수량을 비교해 이력 절단을 판별할 수 있게 한다.
        """
        codes = [c for c in dict.fromkeys(codes or []) if c]
        if not codes:
            return {}

        result = {}
        try:
            conn = self._get_conn()
            cursor = conn.cursor()
            ph = ",".join("?" * len(codes))
            cursor.execute(f"""
                SELECT code, time, type, qty FROM trades
                WHERE code IN ({ph}) AND order_status = '체결'
                ORDER BY code, time ASC""", codes)

            running = {}
            for row in cursor.fetchall():
                t = dict(row)
                code = t['code']
                type_str = str(t.get('type') or '')
                # [Fix] 정정 주문의 '체결' 행은 진짜 체결이다(접수 → 정정 → 체결로 기록된다).
                #  종전에는 이 행까지 버려서 정정된 매도가 수량 흐름에서 빠졌고, 전량 청산한
                #  포지션이 계속 보유 중으로 남아 진입일이 옛 날짜로 굳었다.
                #  order_status='체결'로 이미 걸렀으므로 취소 행만 방어적으로 제외한다.
                if '취소' in type_str:
                    continue

                try:
                    qty = int(float(t.get('qty') or 0))
                except (TypeError, ValueError):
                    continue
                if qty <= 0:
                    continue

                is_buy = ('매수' in type_str) or ('buy' in type_str.lower())
                is_sell = ('매도' in type_str) or ('sell' in type_str.lower())
                if not (is_buy or is_sell):
                    continue

                prev = running.get(code, 0)
                if is_buy:
                    if prev <= 0:
                        # 수량이 0에서 양수로 바뀌는 순간 = 이번 포지션의 진입
                        result[code] = str(t.get('time') or '')[:10] or None
                    running[code] = prev + qty
                else:
                    running[code] = max(0, prev - qty)
                    if running[code] == 0:
                        result.pop(code, None)   # 전량 청산 — 다음 매수가 새 진입

            return {c: {'date': result.get(c), 'qty': q}
                    for c, q in running.items() if q > 0}
        except Exception as e:
            logger.debug(f"진입일 조회 실패: {e}")
            return {}

    def get_latest_buy_trades(self, codes):
        """[배치] 여러 종목의 최근 매수 내역을 일괄 조회합니다. {code: trade|None 미포함}

        get_latest_buy_trade의 3단계 우선순위(ATR 손절률 보존 원본 → 접수 원본 → 체결확인 더미)를
        동일하게 적용하되 쿼리 1회로 처리한다.
        """
        codes = [c for c in dict.fromkeys(codes or []) if c]
        result = {}
        if not codes: return result
        try:
            conn = self._get_conn()
            cursor = conn.cursor()
            ph = ",".join("?" * len(codes))
            cursor.execute(f"SELECT * FROM trades WHERE code IN ({ph}) AND (type LIKE '%buy%' OR type LIKE '%매수%') ORDER BY id DESC", codes)

            tiers = {}  # code -> [원본(ATR), 원본, 아무거나] 각 티어별 최신(id 최대) 1건
            for row in cursor.fetchall():
                t = dict(row)
                code = t['code']
                slot = tiers.setdefault(code, [None, None, None])
                if slot[0] is not None:
                    continue  # 최우선 티어가 찼으면 이후 행은 모두 더 오래된 것
                reason = t.get('reason')
                is_origin = (reason is None or not str(reason).startswith('체결 확인'))
                try:
                    has_sl = float(t.get('stop_loss_rate') or 0.0) != 0.0
                except (TypeError, ValueError):
                    has_sl = False
                if is_origin and has_sl and slot[0] is None: slot[0] = t
                if is_origin and slot[1] is None: slot[1] = t
                if slot[2] is None: slot[2] = t

            for code, slot in tiers.items():
                picked = slot[0] or slot[1] or slot[2]
                if picked: result[code] = picked
            return result
        except Exception:
            return result

    def get_latest_buy_trade(self, code):
        """특정 종목의 가장 최근 매수 내역 조회 (ATR 손절률 확인용)"""
        try:
            conn = self._get_conn()
            cursor = conn.cursor()
            # [수정] ATR 손절률이 누락되지 않은 원본 매수 내역을 우선 조회합니다. (체결 확인용 더미 레코드 제외)
            cursor.execute("SELECT * FROM trades WHERE code = ? AND (type LIKE '%buy%' OR type LIKE '%매수%') AND stop_loss_rate != 0.0 AND (reason IS NULL OR reason NOT LIKE '체결 확인%') ORDER BY id DESC LIMIT 1", (code,))
            row = cursor.fetchone()
            
            # 만약 0이 아닌 내역이 없다면(고정 손절 등), 기본 최신 매수 내역을 조회합니다.
            if not row:
                cursor.execute("SELECT * FROM trades WHERE code = ? AND (type LIKE '%buy%' OR type LIKE '%매수%') AND (reason IS NULL OR reason NOT LIKE '체결 확인%') ORDER BY id DESC LIMIT 1", (code,))
                row = cursor.fetchone()

            # [추가] 그래도 없으면, '접수' 원본 없이 '체결 확인' 레코드만 존재하는 경우
            #        (수동/외부 매수 등)이므로 체결 확인 더미라도 조회하여
            #        매수 시각(holding_days)·사유를 확보합니다. (시간청산 등 정상 동작 보장)
            if not row:
                cursor.execute("SELECT * FROM trades WHERE code = ? AND (type LIKE '%buy%' OR type LIKE '%매수%') ORDER BY id DESC LIMIT 1", (code,))
                row = cursor.fetchone()

            return dict(row) if row else None
        except Exception: return None
            
    def update_highest_price(self, code, price):
        """트레일링 스탑용 최고가 갱신. 성공 여부를 돌려준다.

        [왜 반환값이 필요한가] 실패해도 호출부는 인메모리 캐시를 갱신하고 넘어간다.
        그래서 그 세션 동안은 정상으로 보이고, **재기동해야 소실이 드러난다** —
        그 시점엔 최고가가 옛 값이라 트레일링 청산선이 통째로 어긋나 있다.
        """
        with self.lock:
            for attempt in range(5):
                try:
                    if self._is_screen_output_allowed() and config.SCREEN_DEBUG_LEVEL == "DEBUG":
                        config.console.print(f"[dim cyan][DB][{threading.get_ident()}] update_highest_price 요청 ({attempt+1}/5): {code} {price}[/dim cyan]")

                    conn = self._get_conn()
                    cursor = conn.cursor()
                    now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                    
                    # UPSERT 구문 사용 (SQLite 3.24+)
                    cursor.execute('''
                        INSERT INTO trailing_stops (code, highest_price, update_time)
                        VALUES (?, ?, ?)
                        ON CONFLICT(code) DO UPDATE SET
                        highest_price = excluded.highest_price,
                        update_time = excluded.update_time
                        WHERE excluded.highest_price > trailing_stops.highest_price
                    ''', (code, price, now_str))
                    
                    conn.commit()
                    return True
                except sqlite3.OperationalError as e:
                    if "locked" in str(e) and attempt < 4:
                        time.sleep(0.5)
                        continue
                    if self._is_screen_output_allowed() and config.SCREEN_DEBUG_LEVEL != "OFF":
                        config.console.print(f"[red][DB] Trailing Stop Update Error: {e}[/red]")
                    self._note_write_failure("트레일링 최고가", f"{code} {price}", e)
                    return False
                except Exception as e:
                    if self._is_screen_output_allowed() and config.SCREEN_DEBUG_LEVEL != "OFF":
                        config.console.print(f"[red][DB] Trailing Stop Update Error: {e}[/red]")
                    self._note_write_failure("트레일링 최고가", f"{code} {price}", e)
                    return False
            return False

    def get_highest_price(self, code):
        """종목의 기록된 최고가 조회"""
        try:
            conn = self._get_conn()
            cursor = conn.cursor()
            cursor.execute("SELECT highest_price FROM trailing_stops WHERE code = ?", (code,))
            row = cursor.fetchone()
            return row[0] if row else None
        except Exception: return None

    def get_position_ref(self, code):
        """코퍼레이트 액션 판정용 기준값 조회 → (평단, 매입금액). 기록이 없으면 (0.0, 0.0)."""
        try:
            conn = self._get_conn()
            cursor = conn.cursor()
            cursor.execute("SELECT ref_avg_price, ref_pchs_amt FROM trailing_stops WHERE code = ?",
                           (code,))
            row = cursor.fetchone()
            return (float(row[0] or 0.0), float(row[1] or 0.0)) if row else (0.0, 0.0)
        except Exception:
            return (0.0, 0.0)

    def update_position_ref(self, code, avg_price, pchs_amt):
        """코퍼레이트 액션 판정용 기준값 갱신.

        최고가와 달리 **단조 조건이 없다** — 평단은 매수로 오르고 매도·분할로 내리므로
        직전 주기 값을 그대로 덮어써야 다음 주기의 비교 기준이 된다.
        """
        with self.lock:
            for attempt in range(5):
                try:
                    conn = self._get_conn()
                    cursor = conn.cursor()
                    now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                    cursor.execute('''
                        INSERT INTO trailing_stops (code, highest_price, update_time,
                                                    ref_avg_price, ref_pchs_amt)
                        VALUES (?, 0.0, ?, ?, ?)
                        ON CONFLICT(code) DO UPDATE SET
                        ref_avg_price = excluded.ref_avg_price,
                        ref_pchs_amt = excluded.ref_pchs_amt
                    ''', (code, now_str, float(avg_price), float(pchs_amt)))
                    conn.commit()
                    return True
                except sqlite3.OperationalError as e:
                    if "locked" in str(e) and attempt < 4:
                        time.sleep(0.5)
                        continue
                    self._note_write_failure("권리조정 기준값", f"{code} {avg_price}", e)
                    return False
                except Exception as e:
                    self._note_write_failure("권리조정 기준값", f"{code} {avg_price}", e)
                    return False
            return False

    def rescale_highest_price(self, code, ratio):
        """분할·무상증자 비율만큼 최고가를 재조정한다.

        update_highest_price는 '더 높을 때만' 반영하는 단조 갱신이라 하향이 불가능하다.
        분할은 정확히 그 하향이 필요한 유일한 경우이므로 별도 경로를 둔다.
        """
        if not ratio or ratio <= 0:
            return None
        with self.lock:
            for attempt in range(5):
                try:
                    conn = self._get_conn()
                    cursor = conn.cursor()
                    now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                    cursor.execute("SELECT highest_price FROM trailing_stops WHERE code = ?", (code,))
                    row = cursor.fetchone()
                    if not row or not row[0]:
                        return None
                    new_price = float(row[0]) * float(ratio)
                    cursor.execute(
                        "UPDATE trailing_stops SET highest_price = ?, update_time = ? WHERE code = ?",
                        (new_price, now_str, code))
                    conn.commit()
                    return new_price
                except sqlite3.OperationalError as e:
                    if "locked" in str(e) and attempt < 4:
                        time.sleep(0.5)
                        continue
                    return None
                except Exception:
                    return None

    def get_all_trailing_stops(self):
        """모든 종목의 트레일링 스탑 기준가 조회 (시스템 시작 시 캐시 로드용)"""
        try:
            conn = self._get_conn()
            cursor = conn.cursor()
            cursor.execute("SELECT code, highest_price FROM trailing_stops")
            return {row[0]: row[1] for row in cursor.fetchall()}
        except Exception: return {}

    def delete_trailing_stop(self, code):
        """매도 후 트레일링 스탑 정보 삭제"""
        with self.lock:
            for attempt in range(5):
                try:
                    conn = self._get_conn()
                    cursor = conn.cursor()
                    cursor.execute("DELETE FROM trailing_stops WHERE code = ?", (code,))
                    conn.commit()
                    break
                except sqlite3.OperationalError as e:
                    if "locked" in str(e) and attempt < 4:
                        time.sleep(0.5)
                        continue
                    break
                except Exception: break

    def insert_half_tp(self, code):
        """반익절 상태 저장"""
        with self.lock:
            for attempt in range(5):
                try:
                    conn = self._get_conn()
                    cursor = conn.cursor()
                    now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                    cursor.execute('''
                        INSERT OR REPLACE INTO half_tp_status (code, update_time)
                        VALUES (?, ?)
                    ''', (code, now_str))
                    conn.commit()
                    break
                except sqlite3.OperationalError: time.sleep(0.5); continue
                except Exception: break

    def delete_half_tp(self, code):
        """반익절 상태 삭제"""
        with self.lock:
            for attempt in range(5):
                try:
                    conn = self._get_conn()
                    cursor = conn.cursor()
                    cursor.execute("DELETE FROM half_tp_status WHERE code = ?", (code,))
                    conn.commit()
                    break
                except sqlite3.OperationalError: time.sleep(0.5); continue
                except Exception: break

    def get_all_half_tp(self):
        """모든 반익절 상태 종목 조회 (시스템 시작 시 캐시 로드용)"""
        try:
            conn = self._get_conn()
            cursor = conn.cursor()
            cursor.execute("SELECT code FROM half_tp_status")
            return set(row[0] for row in cursor.fetchall())
        except Exception: return set()

    def is_disclosure_notified(self, rcept_no):
        """공시 접수번호가 이미 알림 발송됐는지 확인"""
        try:
            conn = self._get_conn()
            cursor = conn.cursor()
            cursor.execute("SELECT 1 FROM notified_disclosures WHERE rcept_no = ?", (rcept_no,))
            return cursor.fetchone() is not None
        except Exception: return False

    def mark_disclosure_notified(self, rcept_no):
        """공시 접수번호를 알림 발송됨으로 기록"""
        with self.lock:
            for attempt in range(5):
                try:
                    conn = self._get_conn()
                    cursor = conn.cursor()
                    now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                    cursor.execute("INSERT OR REPLACE INTO notified_disclosures (rcept_no, notified_at) VALUES (?, ?)", (rcept_no, now_str))
                    conn.commit()
                    break
                except sqlite3.OperationalError: time.sleep(0.5); continue
                except Exception: break

    def save_stock_strategy(self, code, name, strategy):
        """종목별 매매 전략 저장"""
        with self.lock:
            conn = self._get_conn()
            cursor = conn.cursor()
            now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            memo = strategy.get('memo', '')
            weights = strategy.get('weights')
            weights_json = json.dumps(weights) if weights else None
            buy_vol_strength = strategy.get('buy_vol_strength', 100.0)
            
            invest_ratio = strategy.get('invest_ratio')
            time_stop_days = strategy.get('time_stop_days')
            use_atr_stop = strategy.get('use_atr_stop')
            atr_stop_multiplier = strategy.get('atr_stop_multiplier')
            half_take_profit_use = strategy.get('half_take_profit_use', 1)
            
            cursor.execute('''
                INSERT OR REPLACE INTO stock_strategies 
                (code, name, buy_score, buy_rsi, buy_vol_strength, sell_score, stop_loss, take_profit, take_profit_rsi, ts_activation, ts_callback, updated_at, memo, weights, invest_ratio, time_stop_days, use_atr_stop, atr_stop_multiplier, half_take_profit_use)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ''', (code, name, 
                  strategy['buy_score'], strategy['buy_rsi'], buy_vol_strength,
                  strategy['sell_score'], strategy['stop_loss'], 
                  strategy['take_profit'], strategy['take_profit_rsi'], 
                  strategy['ts_activation'], strategy['ts_callback'], 
                  now, memo, weights_json, invest_ratio, time_stop_days, use_atr_stop, atr_stop_multiplier, half_take_profit_use))
            conn.commit()

    def get_stock_strategy(self, code):
        """특정 종목의 매매 전략 조회"""
        try:
            conn = self._get_conn()
            cursor = conn.cursor()
            cursor.execute("SELECT * FROM stock_strategies WHERE code = ?", (code,))
            row = cursor.fetchone()
            return dict(row) if row else None
        except Exception: return None

    def get_all_stock_strategies(self):
        """모든 종목별 매매 전략 조회"""
        try:
            conn = self._get_conn()
            cursor = conn.cursor()
            cursor.execute("SELECT * FROM stock_strategies ORDER BY updated_at DESC")
            rows = cursor.fetchall()
            return [dict(row) for row in rows]
        except Exception: return []

    def delete_stock_strategy(self, code):
        """종목별 매매 전략 삭제"""
        with self.lock:
            conn = self._get_conn()
            cursor = conn.cursor()
            cursor.execute("DELETE FROM stock_strategies WHERE code = ?", (code,))
            conn.commit()

    # ------------------------------------------------------------------
    # 신호 원장 (signal_ledger)
    # ------------------------------------------------------------------
    #  결과 분류(outcome) → 컬럼. 로그 문자열을 파싱하지 않고 판정 지점이 직접 넘긴다.
    _LEDGER_COLS = {
        "passed": "passed",                 # 게이트를 모두 통과해 매수 후보가 됨
        "gate_vol": "blocked_vol",          # 체결강도 미달
        "gate_abr": "blocked_abr",          # 매도잔량비 미달
        "gate_hold": "blocked_hold",        # 체결강도 미확인(보류)
        "corr": "blocked_corr",             # 상관관계
        "rs": "blocked_rs",                 # 상대강도
        "tq": "blocked_tq",                 # 추세품질 상한
        "reentry": "blocked_reentry",       # 당일 재진입 차단
        "other": "blocked_other",
    }

    def record_signal_ledger(self, date_str, rows):
        """한 주기의 매수 신호 판정을 (일자, 종목) 단위로 누적한다.

        rows: [{'code','name','outcome','score','vol','abr','state'}, ...]
        주기마다 한 번 호출한다 — 종목마다 쓰면 파이3에서 쓰기가 주기당 수십 번이 된다.
        실패해도 매매에 영향을 주지 않는다(계측은 매매를 막지 않는다).
        """
        if not rows:
            return
        # 모의투자 원장은 실전과 섞이면 안 된다(같은 DB 파일을 쓴다). trades.is_sim과 같은 기준.
        is_sim = 0
        payload = []
        for r in rows:
            col = self._LEDGER_COLS.get(r.get("outcome"), "blocked_other")
            counts = {c: 0 for c in self._LEDGER_COLS.values()}
            counts[col] = 1
            payload.append((
                date_str, r.get("code"), is_sim, r.get("name"),
                counts["passed"], counts["blocked_vol"], counts["blocked_abr"],
                counts["blocked_hold"], counts["blocked_corr"], counts["blocked_rs"],
                counts["blocked_tq"], counts["blocked_reentry"], counts["blocked_other"],
                float(r.get("score") or 0.0), r.get("vol"), r.get("abr"), r.get("state"),
            ))
        sql = '''
            INSERT INTO signal_ledger
                (date, code, is_sim, name, cycles, passed, blocked_vol, blocked_abr, blocked_hold,
                 blocked_corr, blocked_rs, blocked_tq, blocked_reentry, blocked_other,
                 max_score, max_vol, min_abr, last_state, updated_at)
            VALUES (?, ?, ?, ?, 1, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
            ON CONFLICT(date, code, is_sim) DO UPDATE SET
                cycles          = cycles + 1,
                passed          = passed + excluded.passed,
                blocked_vol     = blocked_vol + excluded.blocked_vol,
                blocked_abr     = blocked_abr + excluded.blocked_abr,
                blocked_hold    = blocked_hold + excluded.blocked_hold,
                blocked_corr    = blocked_corr + excluded.blocked_corr,
                blocked_rs      = blocked_rs + excluded.blocked_rs,
                blocked_tq      = blocked_tq + excluded.blocked_tq,
                blocked_reentry = blocked_reentry + excluded.blocked_reentry,
                blocked_other   = blocked_other + excluded.blocked_other,
                max_score       = MAX(max_score, excluded.max_score),
                -- NULL은 '못 쟀다'이지 0이 아니다. 한쪽만 값이 있으면 그 값을 남긴다.
                max_vol = CASE WHEN excluded.max_vol IS NULL THEN max_vol
                               WHEN max_vol IS NULL THEN excluded.max_vol
                               ELSE MAX(max_vol, excluded.max_vol) END,
                min_abr = CASE WHEN excluded.min_abr IS NULL THEN min_abr
                               WHEN min_abr IS NULL THEN excluded.min_abr
                               ELSE MIN(min_abr, excluded.min_abr) END,
                last_state = excluded.last_state,
                updated_at = CURRENT_TIMESTAMP
        '''
        with self.lock:
            try:
                conn = self._get_conn()
                conn.executemany(sql, payload)   # 주기당 트랜잭션 1회
                conn.commit()
            except Exception as e:
                logger.warning(f"[Ledger] 신호 원장 기록 실패 (매매에는 영향 없음): {e}")

    def get_signal_ledger(self, start_date=None, end_date=None, code=None, is_sim=None):
        """신호 원장 조회. 감사 도구가 로그 파싱 대신 쓰는 경로.

        is_sim: None이면 실전·모의를 모두 준다(계좌별로 보고 싶으면 0/1을 준다).
         두 모드를 섞어 세면 재진입·상관 차단율이 무엇을 뜻하는지 알 수 없게 되므로,
         감사 도구는 기본적으로 실전(0)만 본다.
        """
        sql = "SELECT * FROM signal_ledger WHERE 1=1"
        params = []
        if is_sim is not None:
            sql += " AND is_sim = ?"; params.append(1 if is_sim else 0)
        if start_date:
            sql += " AND date >= ?"; params.append(start_date)
        if end_date:
            sql += " AND date <= ?"; params.append(end_date)
        if code:
            sql += " AND code = ?"; params.append(code)
        sql += " ORDER BY date, code"
        try:
            cursor = self._get_conn().cursor()
            cursor.execute(sql, params)
            return [dict(r) for r in cursor.fetchall()]
        except Exception as e:
            logger.warning(f"[Ledger] 신호 원장 조회 실패: {e}")
            return []

    def cleanup_old_data(self, days_to_keep):
        """보존 기간이 지난 오래된 거래 내역 삭제"""
        if days_to_keep <= 0: return
        
        try:
            conn = self._get_conn()
            cursor = conn.cursor()
            
            # SQLite의 date 함수를 사용하여 기준일 계산
            cursor.execute(f"DELETE FROM trades WHERE time < date('now', '-{days_to_keep} days')")
            deleted_count = cursor.rowcount
            conn.commit()
            
            if self._is_screen_output_allowed() and deleted_count > 0 and config.SCREEN_DEBUG_LEVEL != "OFF":
                config.console.print(f"[dim yellow][DB] 오래된 거래 내역 {deleted_count}건을 정리했습니다. ({days_to_keep}일 이전)[/dim yellow]")
        except Exception as e:
            if self._is_screen_output_allowed() and config.SCREEN_DEBUG_LEVEL != "OFF":
                config.console.print(f"[red][DB] Cleanup Error: {e}[/red]")

    def run_vacuum(self):
        """DB 최적화 (VACUUM) 실행 - 프로그램 종료 시 호출"""
        try:
            # 별도 연결 생성하여 실행 (스레드 로컬 연결 간섭 방지)
            # [수정] VACUUM은 트랜잭션 내에서 실행할 수 없으므로 isolation_level=None (Auto-commit) 설정
            conn = sqlite3.connect(self.db_path, timeout=60, isolation_level=None)
            if self._is_screen_output_allowed() and config.SCREEN_DEBUG_LEVEL != "OFF":
                config.console.print("[dim cyan][DB] 데이터베이스 정리 및 최적화(VACUUM) 수행 중...[/dim cyan]")
            
            # 1. 오래된 데이터 삭제 (설정된 기간 기준)
            retention_days = getattr(config, 'DB_DATA_RETENTION_DAYS', 365)
            if retention_days > 0:
                conn.execute(f"DELETE FROM trades WHERE time < date('now', '-{retention_days} days')")

            # 신호 원장은 감사 증거라 거래 내역보다 훨씬 오래 남긴다(하루 최대 44행).
            ledger_days = getattr(config, 'SIGNAL_LEDGER_RETENTION_DAYS', 1095)
            if ledger_days > 0:
                conn.execute("DELETE FROM signal_ledger WHERE date < ?",
                             ((datetime.now() - timedelta(days=ledger_days)).strftime("%Y%m%d"),))
            
            # 2. VACUUM 실행 (공간 회수)
            conn.execute("VACUUM;")
            conn.close()
            
            if self._is_screen_output_allowed() and config.SCREEN_DEBUG_LEVEL != "OFF":
                config.console.print("[dim green][DB] 데이터베이스 최적화 완료[/dim green]")
        except Exception as e:
            if self._is_screen_output_allowed() and config.SCREEN_DEBUG_LEVEL != "OFF":
                config.console.print(f"[red][DB] VACUUM Error: {e}[/red]")

    def check_integrity(self):
        """(정상여부, 메시지). 자동매매 시작 전 DB가 성한지 확인한다.

        [왜] 이 파일에는 평단·트레일링 최고가·손절 기준이 들어 있다. 잔고는 증권사에
        있지만 '어디서 자를지'는 여기에만 있다 — 깨지면 보유 포지션의 청산 기준을 잃는다.
        운영이 라즈베리파이 SD카드라 전원 차단으로 인한 손상이 실제 위험이다.

        integrity_check는 전체 페이지를 훑으므로 시작 시 1회만 부른다.
        """
        try:
            conn = sqlite3.connect(self.db_path, timeout=60)
            try:
                row = conn.execute("PRAGMA integrity_check;").fetchone()
            finally:
                conn.close()
            result = (row[0] if row else "").strip()
            if result.lower() == "ok":
                return True, "ok"
            return False, result or "빈 응답"
        except Exception as e:
            # 열지도 못했다 = 파일이 없거나 손상됐다. 정상으로 볼 수 없다.
            return False, f"검사 실패: {e}"

    def backup(self, keep=7):
        """DB를 백업 디렉터리에 하루 1회 복제하고 오래된 것을 지운다. (백업 경로 또는 None)

        [왜 sqlite3 backup API인가] 파일 복사(cp)는 WAL 모드에서 안전하지 않다 —
        -wal 파일에만 있는 최신 커밋을 놓치거나, 복사 도중의 쓰기가 섞여 깨진 사본이
        나온다. conn.backup()은 SQLite가 페이지 단위로 일관된 스냅샷을 뜬다.
        """
        try:
            src_dir = os.path.dirname(os.path.abspath(self.db_path))
            bdir = os.path.join(src_dir, "backups")
            os.makedirs(bdir, exist_ok=True)
            base = os.path.splitext(os.path.basename(self.db_path))[0]
            dest = os.path.join(bdir, f"{base}_{datetime.now().strftime('%Y%m%d')}.db")
            if os.path.exists(dest):
                return dest         # 오늘 것은 이미 떴다

            src = sqlite3.connect(self.db_path, timeout=60)
            dst = sqlite3.connect(dest)
            try:
                src.backup(dst)
            finally:
                dst.close()
                src.close()

            # 회전: 최신 keep개만 남긴다(SD카드 용량이 넉넉하지 않다).
            olds = sorted(f for f in os.listdir(bdir)
                          if f.startswith(base + "_") and f.endswith(".db"))
            for f in olds[:-keep] if keep > 0 else []:
                try:
                    os.remove(os.path.join(bdir, f))
                except OSError:
                    pass
            logger.info(f"[DB] 백업 완료: {dest}")
            return dest
        except Exception as e:
            logger.error(f"[DB] 백업 실패: {e}")
            return None

    def save_daily_asset(self, date_str, account, asset_value):
        """일일 총 자산 스냅샷 저장"""
        with self.lock:
            for attempt in range(5):
                try:
                    conn = self._get_conn()
                    cursor = conn.cursor()
                    cursor.execute('''
                        INSERT OR REPLACE INTO daily_asset_history (date, account, asset)
                        VALUES (?, ?, ?)
                    ''', (date_str, account, asset_value))
                    conn.commit()
                    break
                except sqlite3.OperationalError as e:
                    if "locked" in str(e) and attempt < 4:
                        time.sleep(0.5)
                        continue
                    break
                except Exception:
                    break

    def shift_daily_assets(self, account, amount):
        """계좌의 일일 자산 스냅샷 전체를 amount만큼 평행이동한다. (외부 입출금 보정)

        [왜 필요한가] 이 표는 드로다운 기반 리스크 스케일링의 HWM 분모다. 입금으로
        자산이 뛴 날이 고점으로 박히면, 그 돈을 다시 빼도 고점은 남아 **매매와 무관한
        가짜 드로다운**이 룩백 기간(DD_LOOKBACK_DAYS) 내내 리스크 한도를 축소한다.
        실측 2026-08-23: 가상계좌에 1,000만원이 들어왔다 나간 흔적 한 줄(20,028,670원)이
        드로다운을 49.5%로 만들어 히트 캡을 10% → 8%로 묶었다.

        [왜 삭제가 아니라 이동인가] 지우면 드로다운 기준 자체가 사라져 한도가 조용히
        열린다(데이터가 없을수록 열리는 구조). 과거 곡선을 현재 자본 기준으로 옮기면
        곡선의 '모양'이 보존되고, 반대 방향 입출금이 오면 그대로 되돌아온다.

        반환: 이동한 행 수 (실패 시 0)
        """
        if not amount:
            return 0
        with self.lock:
            try:
                conn = self._get_conn()
                cursor = conn.cursor()
                # 자산이 음수가 되는 이동은 기록을 망가뜨린다 — 0에서 끊는다.
                cursor.execute("UPDATE daily_asset_history SET asset = MAX(0, asset + ?) "
                               "WHERE account = ?", (amount, account))
                conn.commit()
                return cursor.rowcount or 0
            except Exception as e:
                logger.warning(f"[DB] 일일 자산 스냅샷 이동 실패: {e}")
                return 0

    def get_daily_asset(self, start_date, account):
        """특정 날짜 이후의 가장 오래된 자산 스냅샷 조회 (기초 자산용)"""
        try:
            conn = self._get_conn()
            cursor = conn.cursor()
            cursor.execute("SELECT asset FROM daily_asset_history WHERE account = ? AND date >= ? ORDER BY date ASC LIMIT 1", (account, start_date))
            row = cursor.fetchone()
            return row[0] if row else None
        except Exception:
            return None

    def get_last_daily_asset(self, account, before_date):
        """before_date 이전의 가장 최근 일일 시작자산. 없으면 None.

        오늘 기준선이 그럴듯한 값인지 대조하는 데 쓴다 — 직전 영업일 대비 반토막 이하면
        시세 결손으로 예수금만 잡힌 응답을 의심한다.
        """
        try:
            cursor = self._get_conn().cursor()
            cursor.execute("SELECT asset FROM daily_asset_history WHERE account = ? AND date < ? "
                           "AND asset > 0 ORDER BY date DESC LIMIT 1", (account, before_date))
            row = cursor.fetchone()
            return row[0] if row else None
        except Exception:
            return None

    def get_max_daily_asset(self, start_date, account):
        """특정 날짜 이후의 자산 고점(HWM) 조회 (드로다운 기반 리스크 스케일링용)"""
        try:
            conn = self._get_conn()
            cursor = conn.cursor()
            cursor.execute("SELECT MAX(asset) FROM daily_asset_history WHERE account = ? AND date >= ?", (account, start_date))
            row = cursor.fetchone()
            return row[0] if row and row[0] else None
        except Exception:
            return None
            
    def insert_reserved_order(self, cano, acnt, market, order_type, code, name, qty, order_price, condition_type, target_price, target_time, expire_dt=None, composite_json=None):
        with self.lock:
            conn = self._get_conn()
            cursor = conn.cursor()
            cursor.execute('''
                INSERT INTO reserved_orders (cano, acnt, market, order_type, code, name, qty, order_price, condition_type, target_price, target_time, expire_dt, composite_json)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ''', (cano, acnt, market, order_type, code, name, qty, order_price, condition_type, target_price, target_time, expire_dt, composite_json))
            conn.commit()

    def get_pending_reserved_orders(self):
        try:
            conn = self._get_conn()
            cursor = conn.cursor()
            cursor.execute("SELECT * FROM reserved_orders WHERE status = 'PENDING'")
            return [dict(row) for row in cursor.fetchall()]
        except Exception: return []

    def get_completed_reserved_orders(self, start_date=None, keyword=None):
        """발동 완료되거나 취소된 예약 주문 내역 조회 (히스토리용)"""
        try:
            conn = self._get_conn()
            cursor = conn.cursor()
            q = "SELECT * FROM reserved_orders WHERE status != 'PENDING'"
            params = []
            
            if start_date:
                q += " AND created_at >= ?"
                params.append(start_date + " 00:00:00")
            if keyword:
                q += " AND (code LIKE ? OR name LIKE ?)"
                params.append(f"%{keyword}%")
                params.append(f"%{keyword}%")
                
            cursor.execute(q, params)
            return [dict(row) for row in cursor.fetchall()]
        except Exception: return []

    def update_reserved_order_status(self, order_id, status, odno=None, fail_reason=None):
        with self.lock:
            conn = self._get_conn()
            cursor = conn.cursor()
            if odno: cursor.execute("UPDATE reserved_orders SET status=?, odno=? WHERE id=?", (status, odno, order_id))
            elif fail_reason: cursor.execute("UPDATE reserved_orders SET status=?, fail_reason=? WHERE id=?", (status, fail_reason, order_id))
            else: cursor.execute("UPDATE reserved_orders SET status=? WHERE id=?", (status, order_id))
            conn.commit()
            
    def update_reserved_order_fields(self, order_id, **fields):
        """대기 중인 예약 주문의 편집 가능한 항목만 갱신한다 (PENDING 한정).

        [왜 화이트리스트인가] 조건 종류(condition_type)나 종목을 여기서 바꾸면
        누적 상태(lowest_price/highest_price)의 의미가 달라진다 — 트레일링 최저점을
        그대로 둔 채 조건만 바꾸면 등록한 적 없는 기준으로 발동한다.
        조건 자체를 바꾸려면 취소 후 재등록하는 것이 맞다.
        """
        allowed = {'qty', 'order_price', 'target_price', 'expire_dt'}
        sets, vals = [], []
        for k, v in fields.items():
            if k in allowed:
                sets.append(f"{k}=?")
                vals.append(v)
        if not sets:
            return False
        vals.append(order_id)
        with self.lock:
            conn = self._get_conn()
            cursor = conn.cursor()
            cursor.execute(
                f"UPDATE reserved_orders SET {', '.join(sets)} WHERE id=? AND status='PENDING'",
                tuple(vals))
            conn.commit()
            return cursor.rowcount > 0

    def update_reserved_order_lowest(self, order_id, price):
        """트레일링 매수용 최저점 추적 업데이트"""
        with self.lock:
            conn = self._get_conn()
            conn.cursor().execute("UPDATE reserved_orders SET lowest_price=? WHERE id=?", (price, order_id))
            conn.commit()
            
    def update_reserved_order_highest(self, order_id, price):
        """트레일링 매도용 최고점 추적 업데이트"""
        with self.lock:
            conn = self._get_conn()
            conn.cursor().execute("UPDATE reserved_orders SET highest_price=? WHERE id=?", (price, order_id))
            conn.commit()
            
    def cancel_reserved_orders_on_corp_action(self, code, reason):
        """권리 조정(액면분할·무상증자)이 감지된 종목의 대기 예약 주문을 전부 취소한다.

        예약 주문의 기준값은 전부 **조정 전 가격**이다 — 목표가(STOP·LIMIT·BREAKOUT)도,
        추적 극값(TRAILING_*의 highest/lowest)도 그렇다. 분할이 나면 목표가는 즉시
        도달한 것처럼 보이고 추적 극값은 폭락한 것처럼 보여, 어느 쪽이든 곧바로 오발동한다.
        보정할 방법이 없지는 않으나 **운영자가 의도한 가격 자체가 무의미해진 상황**이므로,
        시스템이 임의로 환산하지 않고 취소한 뒤 다시 걸도록 알린다.

        계좌로 좁히지 않는다 — 권리 조정은 계좌가 아니라 종목의 사건이다.
        반환: 취소된 주문 목록(알림 문구 구성용). 없으면 빈 리스트.
        """
        with self.lock:
            for attempt in range(5):
                try:
                    conn = self._get_conn()
                    cursor = conn.cursor()
                    cursor.execute(
                        "SELECT id, name, order_type, qty, condition_type, target_price, "
                        "order_price FROM reserved_orders WHERE code=? AND status='PENDING'",
                        (code,))
                    rows = [dict(r) for r in cursor.fetchall()]
                    if not rows:
                        return []
                    cursor.execute(
                        "UPDATE reserved_orders SET status='CANCELED', fail_reason=? "
                        "WHERE code=? AND status='PENDING'", (reason, code))
                    conn.commit()
                    return rows
                except sqlite3.OperationalError as e:
                    if "locked" in str(e) and attempt < 4:
                        time.sleep(0.5)
                        continue
                    break
                except Exception:
                    break
            return []

    def get_pyramid_count(self, code):
        """증액(피라미딩) 횟수. 조회 실패는 -1 — '0회'와 구분해야 한다.

        [왜 구분하는가] 호출부는 횟수를 모를 때 증액을 **보류**해야 한다. 모르는 것을
        0으로 읽으면 상한을 넘겨 계속 증액되고, 한 종목이 계좌를 삼킨다.
        """
        try:
            cur = self._get_conn().cursor()
            cur.execute("SELECT pyramid_count FROM trailing_stops WHERE code=?", (code,))
            row = cur.fetchone()
            return int(row["pyramid_count"] or 0) if row else 0
        except Exception as e:
            logger.error(f"[DB] 증액 횟수 조회 실패({code}): {e}")
            return -1

    def bump_pyramid_count(self, code, expected):
        """증액 횟수를 expected+1로 올린다. 성공 여부를 돌려준다.

        주문을 내기 **전에** 호출한다 — 기록이 안 되면 주문도 내지 않는다. 반대 순서면
        '주문은 나갔는데 횟수는 그대로'가 되어 다음 주기에 같은 증액이 또 나간다.
        기록만 되고 주문이 실패하면 증액 기회를 하나 잃을 뿐이라, 이쪽이 안전한 방향이다.
        """
        with self.lock:
            for attempt in range(5):
                try:
                    conn = self._get_conn()
                    now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                    conn.execute(
                        "INSERT INTO trailing_stops (code, highest_price, update_time, pyramid_count) "
                        "VALUES (?, 0.0, ?, ?) "
                        "ON CONFLICT(code) DO UPDATE SET pyramid_count=excluded.pyramid_count",
                        (code, now_str, int(expected) + 1))
                    conn.commit()
                    return True
                except sqlite3.OperationalError as e:
                    if "locked" in str(e) and attempt < 4:
                        time.sleep(0.5)
                        continue
                    self._note_write_failure("증액 횟수", f"{code} → {int(expected) + 1}", e)
                    return False
                except Exception as e:
                    self._note_write_failure("증액 횟수", f"{code} → {int(expected) + 1}", e)
                    return False
            return False

    def get_corp_action_ref(self, code):
        """(기준일, 기준종가, 출처). 기록이 없으면 ("", 0.0, "")."""
        with self.lock:
            try:
                cur = self._get_conn().cursor()
                cur.execute("SELECT ref_date, ref_close, source FROM corp_action_refs WHERE code=?",
                            (code,))
                row = cur.fetchone()
                if not row:
                    return "", 0.0, ""
                return row['ref_date'] or "", float(row['ref_close'] or 0.0), row['source'] or ""
            except Exception:
                return "", 0.0, ""

    def save_corp_action_ref(self, code, ref_date, ref_close, source):
        """권리 조정 감시 기준점을 갱신한다(무조건 덮어쓴다)."""
        with self.lock:
            for attempt in range(5):
                try:
                    conn = self._get_conn()
                    conn.execute(
                        "INSERT INTO corp_action_refs (code, ref_date, ref_close, source, updated_at) "
                        "VALUES (?, ?, ?, ?, CURRENT_TIMESTAMP) "
                        "ON CONFLICT(code) DO UPDATE SET ref_date=excluded.ref_date, "
                        "ref_close=excluded.ref_close, source=excluded.source, "
                        "updated_at=CURRENT_TIMESTAMP",
                        (code, ref_date, float(ref_close), source))
                    conn.commit()
                    return True
                except sqlite3.OperationalError as e:
                    if "locked" in str(e) and attempt < 4:
                        time.sleep(0.5)
                        continue
                    break
                except Exception:
                    break
            return False

    def cancel_reserved_sell_orders(self, cano, acnt, code):
        """특정 계좌/종목의 대기 중인 예약 매도 주문을 일괄 취소 처리 (전량 매도 시)"""
        with self.lock:
            for attempt in range(5):
                try:
                    conn = self._get_conn()
                    cursor = conn.cursor()
                    cursor.execute('''
                        UPDATE reserved_orders 
                        SET status='CANCELED', fail_reason='수동/자동 전량 매도로 인한 예약 자동 취소' 
                        WHERE cano=? AND acnt=? AND code=? AND order_type='sell' AND status='PENDING'
                    ''', (cano, acnt, code))
                    updated = cursor.rowcount
                    conn.commit()
                    return updated
                except sqlite3.OperationalError as e:
                    if "locked" in str(e) and attempt < 4:
                        time.sleep(0.5)
                        continue
                    break
                except Exception:
                    break
            return 0
            
    def cancel_reserved_buy_orders(self, cano, acnt, code):
        """특정 계좌/종목의 대기 중인 예약 매수 주문을 일괄 취소 처리 (신규 매수 시 중복 방지)"""
        with self.lock:
            for attempt in range(5):
                try:
                    conn = self._get_conn()
                    cursor = conn.cursor()
                    cursor.execute('''
                        UPDATE reserved_orders 
                        SET status='CANCELED', fail_reason='수동/자동 매수로 인한 예약 매수 자동 취소' 
                        WHERE cano=? AND acnt=? AND code=? AND order_type='buy' AND status='PENDING'
                    ''', (cano, acnt, code))
                    updated = cursor.rowcount
                    conn.commit()
                    return updated
                except sqlite3.OperationalError: time.sleep(0.5); continue
                except Exception: break
            return 0

    def cancel_other_reserved_orders(self, triggered_id, cano, acnt, code,
                                     reason='동일 종목의 다른 예약 매매 발동으로 인한 자동 취소'):
        """특정 예약 주문이 발동되었을 때, 동일 계좌/종목의 나머지 대기 중인 예약 주문을 일괄 취소

        reason: 취소 사유. 발동 외의 일괄 취소(보유 소멸 등)에서 사유가 '다른 예약 발동'으로
                잘못 남으면 나중에 이력만 보고는 왜 취소됐는지 알 수 없다.
        """
        with self.lock:
            for attempt in range(5):
                try:
                    conn = self._get_conn()
                    cursor = conn.cursor()
                    cursor.execute('''
                        SELECT * FROM reserved_orders 
                        WHERE cano=? AND acnt=? AND code=? AND id != ? AND status='PENDING'
                    ''', (cano, acnt, code, triggered_id))
                    targets = [dict(row) for row in cursor.fetchall()]
                    
                    if targets:
                        cursor.execute('''
                            UPDATE reserved_orders 
                            SET status='CANCELED', fail_reason=? 
                            WHERE cano=? AND acnt=? AND code=? AND id != ? AND status='PENDING'
                        ''', (reason, cano, acnt, code, triggered_id))
                        conn.commit()
                    return targets
                except sqlite3.OperationalError as e:
                    if "locked" in str(e) and attempt < 4:
                        time.sleep(0.5)
                        continue
                    break
                except Exception:
                    break
            return []

# 전역 인스턴스
db = DBManager()
atexit.register(db.run_vacuum)
