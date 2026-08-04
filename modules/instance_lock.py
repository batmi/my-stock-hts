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

    def __init__(self, account_key):
        # 계좌번호에 경로 구분자가 섞여도 파일명이 깨지지 않게 한다.
        safe = "".join(c if c.isalnum() or c in "-_" else "_" for c in str(account_key or "default"))
        self.account_key = account_key
        self.path = os.path.join(_lock_dir(), f"autotrade_{safe}.lock")
        self._fd = None
        self.holder = ""

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
        os.write(fd, f"pid={os.getpid()} account={self.account_key}".encode('utf-8'))
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
