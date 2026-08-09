"""자동매매 단일 실행 보장 — 같은 계좌로 엔진이 두 개 뜨는 것을 막는다.

[왜 필요한가] pending_orders(미체결 추적)는 프로세스 메모리에 있다. 엔진이 둘이면
서로의 주문을 모르고, 같은 종목에 각자 매수를 낸다. 재기동 복구(restore_pending_orders)
로도 못 막는다 — 둘 다 거래소 미체결 목록을 보고 '내 주문이 살아 있구나'로 읽기 때문에
상대의 주문을 자기 것으로 착각한다. 잔고 기반 보유 종목 수 게이트도 마찬가지로 무력하다.

실제로 생기는 경로: SSH 세션 두 개에서 각각 실행, 재기동 중 구프로세스 잔존, cron 중복.

[왜 flock인가] PID 파일은 프로세스가 kill -9 로 죽거나 OOM 킬되면 낡은 PID가 남아
'실행 중'으로 오판한다(라즈베리파이 OOM 환경에서 특히). flock은 파일 디스크립터가
닫히면 커널이 자동으로 푼다 — 비정상 종료여도 잠금이 남지 않는다.

[범위] 계좌 단위로 잠근다. 다른 계좌를 동시에 돌리는 것은 막지 않는다.
"""
import hashlib
import logging
import os

import config

logger = logging.getLogger(__name__)

try:
    import fcntl
except ImportError:      # Windows — 배포 대상(라즈베리파이·macOS)이 아니다
    fcntl = None


def _lock_dir():
    base = os.path.dirname(os.path.abspath(getattr(config, 'DB_FILE_PATH', '') or '')) or '.'
    path = os.path.join(base, 'locks')
    os.makedirs(path, exist_ok=True)
    return path


class InstanceLock:
    """계좌 단위 배타 잠금. with 문 없이 acquire/release 로 수명을 직접 관리한다."""

    prefix = "autotrade"    # 잠금 파일 접두어(=잠금의 범위). 하위 클래스가 바꾼다.
    label = "account"       # 잠금 파일에 남길 키 이름(진단용)

    def __init__(self, account_key):
        self.account_key = account_key
        self.path = os.path.join(_lock_dir(), self._lock_name(account_key))
        self._fd = None
        self.holder = ""

    def _lock_name(self, key):
        # 키에 경로 구분자가 섞여도 파일명이 깨지지 않게 한다.
        safe = "".join(c if c.isalnum() or c in "-_" else "_" for c in str(key or "default"))
        return f"{self.prefix}_{safe}.lock"

    def acquire(self):
        """잠금 획득 성공 여부. 실패 시 self.holder 에 선점자 정보가 담긴다."""
        if fcntl is None:
            logger.warning("[InstanceLock] 이 플랫폼은 flock 미지원 — 중복 실행 검사를 건너뜁니다.")
            return True
        if self._fd is not None:
            return True         # 같은 프로세스가 이미 쥐고 있다

        fd = os.open(self.path, os.O_RDWR | os.O_CREAT, 0o644)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except (OSError, IOError):
            # 선점자가 있다. 파일 내용으로 누구인지 알려 준다(잠금은 이미 상대가 쥐었다).
            try:
                self.holder = os.read(fd, 256).decode('utf-8', 'replace').strip()
            except Exception:
                self.holder = ""
            os.close(fd)
            return False

        os.ftruncate(fd, 0)
        os.write(fd, f"pid={os.getpid()} {self.label}={self.account_key}".encode('utf-8'))
        try:
            os.fsync(fd)
        except OSError:
            pass                # 내용은 진단용 — 동기화 실패가 잠금을 무효로 만들지는 않는다
        self._fd = fd
        return True

    def release(self):
        if self._fd is None:
            return
        fd, self._fd = self._fd, None
        try:
            fcntl.flock(fd, fcntl.LOCK_UN)
        except Exception:
            pass
        finally:
            try:
                os.close(fd)    # 파일은 지우지 않는다 — 지우는 순간과 남의 open 이 겹치면
            except Exception:   #  서로 다른 inode를 잠가 배타성이 깨진다.
                pass


# ==========================================================
# [추가 2026-08-09] 앱키 단위 중복 프로세스 감지
# ==========================================================
# EGW00201(초당 거래건수 초과) 진단에서, '같은 앱키를 쓰는 다른 프로세스'는 1순위
#  후보였는데도 확인할 방법이 없었다. 위 InstanceLock 은 자동매매 엔진이 계좌 단위로만
#  잡으므로, 조회 전용 인스턴스를 하나 더 띄우면 아무 잠금도 걸리지 않는다.
#  실측 로그에도 25분간 6회 재시작(2026-08-07)처럼 겹칠 여지가 충분한 흔적이 있었다.
#
# [왜 계좌가 아니라 앱키인가] KIS의 TPS(20)·웹소켓 동시 연결(1)·토큰 발급(1분 1회)
#  제약은 전부 앱키 단위다. 계좌가 달라도 앱키가 같으면 유량을 함께 쓴다.
#  (mode 4 가상투자가 VIRT_APP_KEY로 키를 분리하는 이유와 같은 제약이다)
#
# [왜 차단이 아니라 감지가 기본인가] 조회 인스턴스를 하나 더 띄우는 것은 정상 작업
#  흐름이고, 실주문 중복은 이미 계좌 단위 InstanceLock 이 막는다. 여기서 필요한 것은
#  EGW00201 이 떴을 때 '다른 프로세스 때문인가'를 로그만 보고 판정할 근거다.
#  차단이 필요하면 config.APPKEY_DUP_ABORT 를 켠다.
APPKEY_DUPLICATE = False   # 같은 앱키를 쓰는 다른 프로세스가 있는가
APPKEY_HOLDER = ""         # 있다면 그 프로세스 정보(pid=…)
APPKEY_DUP_LABEL = ""      # 중복이 걸린 키의 이름(수동/자동매매)
_APPKEY_LOCKS = []         # 프로세스 수명 동안 fd 를 살려 둔다(GC 되면 잠금이 풀린다).
                           #  수동 키와 자동매매 키를 따로 잠그므로 리스트다 — 단일 변수로
                           #  두면 두 번째 호출이 첫 잠금을 덮어써 조용히 풀린다.


def _appkey_fingerprint(app_key):
    """앱키를 파일명에 그대로 쓰지 않는다 — 잠금 파일명은 평문으로 남는다."""
    raw = str(app_key or "").strip().encode('utf-8')
    return hashlib.sha256(raw).hexdigest()[:12] if raw else "empty"


class AppKeyLock(InstanceLock):
    """앱키 단위 배타 잠금. 계좌 잠금과 파일이 겹치지 않게 접두어만 다르다."""

    prefix = "appkey"
    label = "appkey"

    def __init__(self, app_key):
        super().__init__(_appkey_fingerprint(app_key))


def guard_appkey(app_key, label="수동"):
    """앱키 잠금을 잡고 중복 여부를 모듈 전역에 기록한다. (중복이면 False)

    반환값은 '이 프로세스가 유일한가'다. 잠금 객체는 전역에 붙들어 프로세스가 살아 있는
    동안 유지한다 — 해제는 프로세스 종료 시 커널이 한다(비정상 종료도 동일).

    label은 어느 키에서 중복이 났는지 로그에 남기기 위한 것이다. 수동 계좌와 자동매매
    계좌가 서로 다른 앱키를 쓰면 유량 예산도 키마다 따로 잡히므로(api.ThrottledSession의
    앱키별 버킷), '어느 쪽 키가 중복인가'가 곧 '어느 트래픽이 영향을 받는가'다.
    """
    global APPKEY_DUPLICATE, APPKEY_HOLDER, APPKEY_DUP_LABEL, _APPKEY_LOCKS

    if not app_key:
        return True
    try:
        lock = AppKeyLock(app_key)
        if lock.acquire():
            _APPKEY_LOCKS.append(lock)
            return True
    except Exception as e:
        # 잠금 장치가 고장 났다고 프로그램을 막지는 않는다(보조 진단 장치다).
        logger.debug(f"[AppKeyLock] 검사 실패 — 건너뜁니다: {e}")
        return True

    APPKEY_DUPLICATE, APPKEY_HOLDER = True, (lock.holder or "unknown")
    APPKEY_DUP_LABEL = label
    logger.warning(
        f"[AppKeyLock] 같은 {label} 앱키를 쓰는 다른 프로세스가 이미 실행 중입니다 ({APPKEY_HOLDER}). "
        f"KIS의 TPS(20)·웹소켓(1)·토큰 발급 제약은 앱키 단위라 두 프로세스가 유량을 나눠 쓰게 되며, "
        f"EGW00201(초당 거래건수 초과)의 직접 원인이 됩니다.")
    return False


def appkey_duplicate_note():
    """진단 로그에 붙일 한 줄. api.py의 TPS 경고가 그대로 인용한다."""
    if APPKEY_DUPLICATE:
        return f"중복 프로세스 감지됨({APPKEY_DUP_LABEL}키, {APPKEY_HOLDER})"
    return "중복 프로세스 없음"
