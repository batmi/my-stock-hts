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

