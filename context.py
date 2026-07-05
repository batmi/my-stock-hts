# context.py
import threading

# ==========================================================
# [상태] 런타임 전역 상태 관리 (Runtime State)
# ==========================================================
# config.py에서 분리된 런타임 변수들입니다.

# 시스템 트레이딩 로거 (AutoTrader 실행 시 할당됨)
SYSTEM_LOGGER = None

# 락 (Locks) - 스레드 간 동기화
SYSTEM_TRADING_LOCK = threading.RLock() # 시스템 트레이딩 우선순위 처리
TOKEN_REFRESH_LOCK = threading.RLock()  # 토큰 갱신 경합 방지

# 스레드별 컨텍스트 (API 호출 시 계좌 분리용)
trade_context = threading.local()

# 전역 상태 변수
MAIN_THREAD_ID = None       # 메인 스레드 ID
TOKEN_EXPIRED = False       # 토큰 만료 플래그
USER_ACTION_BREADCRUMB = [] # 사용자 입력 경로 추적 (로깅용)

# [추가] API 호출 우선순위 제어 (시스템 트레이딩 우대)
SYSTEM_API_WAIT_COUNT = 0   # API 호출 대기 중인 시스템 스레드 수
API_PRIORITY_CONDITION = threading.Condition() # 우선순위 제어용 조건 변수

# [추가] 토큰 갱신 및 에러 알림 쿨타임 제어
LAST_TOKEN_REFRESH_ATTEMPT = 0
LAST_TOKEN_REFRESH_ALERT = 0


def is_screen_output_allowed():
    """화면 출력 허용 여부 확인 (텔레그램 봇 스레드에서는 터미널 출력 차단).

    api/db_manager 등에 흩어져 있던 동일 판정의 단일 출처(Single Source of Truth).
    """
    return threading.current_thread().name != "TelegramBot"
