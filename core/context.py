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


# ==========================================================
# [상태 캐시] 종목 기술적 상태 스냅샷 (텔레그램 /stocks 연동용)
# ==========================================================
#  시스템 트레이딩(자동 주기)과 운영자 수동 조회(메뉴 2)가 같은 저장소에 쓴다.
#  둘 다 analysis.classify_stock_state()의 결과이므로 값의 의미가 동일하고,
#  사용자에게 필요한 구분은 '누가 계산했나'가 아니라 '언제 것인가'다 → 조회 시각만 노출한다.
#
#  src는 표시하지 않고 '지우기 규칙'에만 쓴다. 시스템은 분석 불가 종목에
#  set_stock_state(code, None)로 자기 값을 지우는데(NXT 시간대 ETF 등), 이때 더 신선한
#  수동 스냅샷까지 함께 날아가면 정작 시스템이 못 보는 종목의 상태가 사라진다.
#
#  프로세스 재시작이면 어차피 무효인 값이라 메모리에만 둔다(라즈베리파이 SD 쓰기 절약).
STOCK_STATE_LOCK = threading.RLock()
_STOCK_STATE = {}   # code -> {'state': str, 'ts': datetime, 'token': str, 'src': 'auto'|'manual'}


def _is_overseas_code(code):
    """국내 6자리 코드가 아니면 해외로 본다(trader/engine과 동일 판정)."""
    c = str(code or "")
    return not (len(c) == 6 and c[0].isdigit() and c.isalnum())


def _session_token(code):
    """해당 종목이 속한 시장의 현재 세션 토큰. 판정 실패 시 None(만료 검사 생략)."""
    try:
        import api      # 지연 임포트 — api가 context를 임포트하므로 순환을 피한다
        return api.market_session_token(_is_overseas_code(code))
    except Exception:   # noqa: BLE001
        return None


def set_stock_state(code, state, src='auto'):
    """종목의 기술적 상태를 현재 세션 스냅샷으로 기록한다."""
    if not code or not state:
        return
    from datetime import datetime as _dt
    with STOCK_STATE_LOCK:
        _STOCK_STATE[code] = {'state': state, 'ts': _dt.now(),
                              'token': _session_token(code), 'src': src}


def clear_stock_state(code, src='auto'):
    """이 출처가 기록한 상태를 지운다. 다른 출처의 스냅샷은 건드리지 않는다."""
    if not code:
        return
    with STOCK_STATE_LOCK:
        entry = _STOCK_STATE.get(code)
        if entry and entry.get('src') == src:
            _STOCK_STATE.pop(code, None)


def get_stock_state(code):
    """(상태, 'HH:MM') 튜플. 없거나 세션이 지나 만료됐으면 None.

    만료된 항목은 조회 시점에 함께 폐기한다(별도 청소 주기 불필요).
    """
    with STOCK_STATE_LOCK:
        entry = _STOCK_STATE.get(code)
        if not entry:
            return None
        token = _session_token(code)
        if token is not None and entry.get('token') is not None and entry['token'] != token:
            _STOCK_STATE.pop(code, None)
            return None
        return (entry['state'], entry['ts'].strftime('%H:%M'))


def prune_stock_states(valid_codes):
    """관심종목·보유종목에서 빠진 코드의 스냅샷을 정리한다."""
    try:
        valid = set(valid_codes or ())
    except TypeError:
        return
    with STOCK_STATE_LOCK:
        for k in [k for k in _STOCK_STATE if k not in valid]:
            _STOCK_STATE.pop(k, None)


def stock_state_count():
    """보관 중인 스냅샷 수 (만료 여부는 보지 않음)."""
    with STOCK_STATE_LOCK:
        return len(_STOCK_STATE)
