"""토큰과 API 호출 진입점.

접근 토큰 발급·갱신·만료 확인과, 그 위에서 도는 공용 호출 함수(call_api)를 담는다.
실전/자동매매/모의 계좌의 토큰이 앱키 단위로 따로 관리되므로 진입점이 여러 개다.
"""
import json
import logging
import threading
import time
import requests
from requests.adapters import HTTPAdapter
import config
from core import constants
from core import context
from brokers import toss_api

#  로거 이름은 분해 전(api.py)과 같은 'api' 로 둔다 — 로그 필터·레벨 설정이 이름을 보므로
#  서브모듈마다 다른 이름을 쓰면 기존 설정이 조용히 빗나간다.
logger = logging.getLogger("api")

def _api():
    """패키지 네임스페이스(api)를 돌려준다 — 다른 계층의 이름은 반드시 이걸 통해 부른다.

    분해 전에는 전부 한 모듈이었으므로 테스트의 patch.object(api, 'X') 가 모든 호출부에
    걸렸다. 서브모듈이 상대 모듈을 직접 import 하면 그 patch 가 닿지 않는다 —
    같은 규약을 쓰는 modules/auto_trade 의 _pkg() 와 같은 이유다.
    """
    import api
    return api

# [추가] 토큰 발급 전용 세션 (강화된 재시도 로직)
def _create_token_session():
    """토큰 발급 전용 requests 세션을 생성합니다. (강화된 재시도 로직 포함)"""
    session = requests.Session()
    # KIS API 서버의 일시적 장애(5xx 에러) 및 네트워크 오류에 대응하기 위한 재시도 전략
    # [수정 2026-08-09] 500을 재시도 목록에서 뺀다. KIS는 EGW00201(초당 거래건수 초과)을
    #  HTTP 500으로 내려주므로, 토큰 발급이 레이트리밋에 걸리면 이 어댑터가 TPS 게이트
    #  밖에서 최대 6연사한다 — 한도를 풀어야 할 상황에서 오히려 부하를 6배로 키운다.
    #  진짜 게이트웨이 장애(502/503/504)와 429는 그대로 재시도한다.
    retry_strategy = _api().GatedRetry(
        total=5,  # 총 5회 재시도
        backoff_factor=1, # 실패 시 대기 시간 (1s, 2s, 4s, 8s, 16s...)
        status_forcelist=[429, 502, 503, 504], # 재시도할 HTTP 상태 코드
        allowed_methods=["POST"], # POST 요청에 대해서도 재시도
        raise_on_status=False
    )
    adapter = HTTPAdapter(max_retries=retry_strategy)
    session.mount("https://", adapter)
    return session

_token_session = _create_token_session()


def get_current_token():
    # [추가] 시스템 트레이딩 컨텍스트 확인
    if getattr(context.trade_context, 'use_auto_account', False):
        return get_auto_access_token()
        
    return get_real_access_token()

def check_and_refresh_token_if_expired():
    """토큰 만료 플래그 확인 및 갱신 (메인 스레드/로그 뷰어 등에서 주기적 호출)"""
    if context.TOKEN_EXPIRED:
        now = time.time()
        # 60초 쿨타임 적용 (로그 뷰어 등에서 무한 루프 호출 시 API 및 텔레그램 도배 방지)
        if getattr(context, 'LAST_TOKEN_REFRESH_ATTEMPT', 0) and now - context.LAST_TOKEN_REFRESH_ATTEMPT < 60:
            return
            
        context.LAST_TOKEN_REFRESH_ATTEMPT = now

        if _api()._is_screen_output_allowed() and config.SCREEN_DEBUG_LEVEL != "OFF":
            config.console.print("\n[bold yellow]토큰 만료가 감지되었습니다. 토큰 갱신을 시도합니다...[/bold yellow]")
        
        success = True
        fail_reason = "Unknown Error"

        try:
            if config.session.is_toss:
                from brokers import toss_api
                if not toss_api.get_access_token(force_refresh=True):
                    success = False
                    fail_reason = "토스 API 토큰 발급 실패"
            #  토스 모드에는 KIS 키가 없다. 여기서 함께 부르면 반드시 실패해
            #  '한투증권 토큰 발급 실패' 경고가 나간다 — 있지도 않은 장애를 알리는 셈이다.
            elif not get_real_access_token(force_refresh=True):
                success = False
                fail_reason = "한투증권 토큰 발급 실패 (API 서버 응답 없음 또는 점검 중)"
                
            if success and config.session.auto_app_key:
                if not get_auto_access_token(force_refresh=True):
                    success = False
                    fail_reason = "자동매매 토큰 발급 실패 (API 서버 응답 없음 또는 점검 중)"
            
            if success:
                context.TOKEN_EXPIRED = False
                if _api()._is_screen_output_allowed() and config.SCREEN_DEBUG_LEVEL != "OFF":
                    config.console.print("[bold green]토큰 갱신 완료. 시스템을 정상적으로 계속 사용합니다.[/bold green]\n")
                
                # [추가] 토큰 갱신 지연 알림이 발송된 적이 있다면, 복구 알림 전송
                if getattr(context, 'LAST_TOKEN_REFRESH_ALERT', 0) > 0:
                    try:
                        _api().send_telegram_message("✅ [시스템 복구] API 토큰이 정상적으로 갱신되었습니다.\n시스템을 계속 운영합니다.")
                        # 복구 알림 후에는 다시 지연 알림을 보낼 수 있도록 초기화
                        context.LAST_TOKEN_REFRESH_ALERT = 0
                    except Exception as e:
                        logger.debug(f"Token refresh recovery telegram send error: {e}")
            else:
                raise Exception(fail_reason)

        except Exception as e:
            if _api()._is_screen_output_allowed() and config.SCREEN_DEBUG_LEVEL != "OFF":
                config.console.print(f"[bold red]토큰 갱신 실패: {e}[/bold red]")
                config.console.print("[dim]서버 점검 등 일시적 오류일 수 있으므로 잠시 후 자동으로 다시 시도합니다.[/dim]")
            
            # [수정] 텔레그램 알림 전송 쿨타임 적용 (1시간당 1회 제한으로 알림 폭탄 방지)
            if now - getattr(context, 'LAST_TOKEN_REFRESH_ALERT', 0) > 3600:
                try:
                    _api().send_telegram_message(f"🚨 [시스템 경고] API 토큰 갱신 지연\n\n사유: {str(e)}\n\n(한국투자증권 서버 정기 점검 시간일 수 있습니다. 시스템은 멈추지 않고 1분 간격으로 토큰 발급을 계속 재시도합니다.)")
                    context.LAST_TOKEN_REFRESH_ALERT = now
                except Exception: pass

def _fetch_and_set_token(token_type, force_refresh=False):
    """
    지정된 유형의 액세스 토큰을 발급받고 세션에 저장합니다.

    Args:
        token_type (str): "REAL", "AUTO" 중 하나
        force_refresh (bool): True이면 강제로 토큰을 재발급합니다.

    Returns:
        str or None: 발급된 액세스 토큰 또는 실패 시 None
    """
    if not force_refresh:
        token = config.session.get_valid_token(token_type)
        if token:
            logger.debug(f"{token_type} 캐시 토큰 사용")
            return token
        logger.info(f"[Token] 유효한 {token_type} 토큰이 없어 신규 발급을 진행합니다.")

    # [추가] EGW00133(토큰 발급 1분당 1회 제한) 예방.
    #  KIS는 같은 AppKey로 1분 내 재발급을 거부하므로, force_refresh로 강제 갱신을
    #  요청하더라도 최근 1분 내 발급된 '유효' 토큰이 있으면 어차피 재발급이 불가하다.
    #  이 경우 불필요한 발급 시도/EGW00133 로그를 피하기 위해 기존 토큰을 재사용한다.
    #  (만료 임박/만료 토큰은 get_valid_token이 None을 반환하므로 정상적으로 재발급되어
    #   force_refresh 본래 목적은 유지된다. 시작 시 여러 경로의 동시 강제 갱신 경합 차단)
    if config.session.is_token_recently_issued(token_type, seconds=60):
        cached = config.session.get_valid_token(token_type, force_disk_reload=True)
        if cached:
            logger.debug(f"{token_type} 최근(1분 내) 발급 토큰 재사용 — 재발급 생략 (EGW00133 예방)")
            return cached

    if token_type == "REAL":
        app_key = config.session.real_app_key
        app_secret = config.session.real_app_secret
        url = f"{config.REAL_URL}/oauth2/tokenP"
    elif token_type == "AUTO":
        if config.session.auto_app_key and config.session.real_app_key and \
           config.session.auto_app_key == config.session.real_app_key:
            return _fetch_and_set_token("REAL", force_refresh)
        app_key = config.session.auto_app_key
        app_secret = config.session.auto_app_secret
        url = f"{config.REAL_URL}/oauth2/tokenP"
    else:
        logger.error(f"잘못된 토큰 유형: {token_type}")
        return None

    if not app_key or not app_secret:
        logger.error(f"{token_type} API Key 또는 Secret이 설정되지 않았습니다.")
        return None

    headers = {"content-type": "application/json"}
    body = {"grant_type": "client_credentials", "appkey": app_key, "appsecret": app_secret}

    try:
        logger.info(f"{token_type} 토큰 신규 발급 요청...")
        res = _token_session.post(url, headers=headers, data=json.dumps(body), timeout=15)
        if res is None:
            raise Exception(f"{token_type} 토큰 발급 응답 없음 (최대 재시도 초과)")

        if res.status_code == 200:
            res_json = res.json()
            if 'access_token' in res_json:
                token = res_json['access_token']
                expired = res_json.get('access_token_token_expired')
                config.session.set_token(token_type, token, expired)
                config.set_last_token_error(None)
                logger.info(f"[green]{token_type} 토큰 발급 완료[/green]")
                return token
            else:
                logger.error(f"토큰 발급 응답 오류: {res.text}")
                config.set_last_token_error('AUTH')
                return None
        else:
            try:
                res_json = res.json()
                if res_json.get('error_code') == 'EGW00133':
                    logger.warning(f"{token_type} 토큰 발급 빈도 제한(EGW00133). 캐시를 재확인합니다.")
                    token = config.session.get_valid_token(token_type, force_disk_reload=True)
                    if token:
                        logger.info("[green]캐시된 유효 토큰을 복구했습니다.[/green]")
                        return token
            except Exception as e:
                logger.debug(f"Token fetch EGW00133 fallback error: {e}")
            logger.error(f"{token_type} 토큰 발급 실패 (Status: {res.status_code}): {res.text}")
            # [수정] 한투(KIS)는 개인 계정에 IP 화이트리스트 개념이 없다(포털 실측 확인).
            #  403 + EGW00103('유효하지 않은 AppKey')은 키 불일치·만료·모의투자 만기 등
            #  '키 자체' 문제이므로 AUTH로 분류한다. (응답에 IP 차단 문구가 명시된 경우에만 IP_BLOCKED)
            _txt = res.text.lower()
            if 'ip' in _txt and 'allow' in _txt:
                config.set_last_token_error('IP_BLOCKED')
            else:
                config.set_last_token_error('AUTH')
            return None

    except requests.exceptions.RequestException as e:
        # [추가] 연결 거부/타임아웃 등은 네트워크 장애로 분류(고정 IP 미등록 시에도 발생 가능).
        logger.error(f"{token_type} 토큰 발급 중 네트워크 오류: {e}")
        config.set_last_token_error('NETWORK')
        return None
    except Exception as e:
        logger.error(f"{token_type} 토큰 발급 중 오류: {e}")
        return None

def get_real_access_token(force_refresh=False):
    # [Fix] 토큰 갱신 경합 방지 (Thread-Safe)
    with context.TOKEN_REFRESH_LOCK:
        return _get_real_access_token_internal(force_refresh)

def _get_real_access_token_internal(force_refresh=False):
    # [수정] 백그라운드 스레드에서도 토큰이 없거나 만료된 경우 발급 허용
    return _fetch_and_set_token("REAL", force_refresh)

def get_auto_access_token(force_refresh=False):
    # [Fix] 토큰 갱신 경합 방지 (Thread-Safe)
    with context.TOKEN_REFRESH_LOCK:
        return _get_auto_access_token_internal(force_refresh)

def _get_auto_access_token_internal(force_refresh=False):
    # [추가] 실전투자 계좌와 자동매매 계좌의 AppKey가 동일한 경우, 실전투자 토큰을 공유 사용
    # (동일한 Key로 짧은 시간 내 중복 토큰 발급 요청 시 EGW00133 에러 발생 방지)
    return _fetch_and_set_token("AUTO", force_refresh)

def safe_int(value):
    try:
        if value is None: return 0
        s_val = str(value).strip().replace(',', '')
        if not s_val: return 0
        return int(float(s_val))
    except Exception as e:
        logger.debug(f"safe_int parse error: {e}")
        return 0

def call_api(url_path, market, category, action, params=None, data=None, method="GET", timeout=None, retries=None, tr_id=None):
    """
    통합 API 호출 함수
    constants.TR_ID_CONFIG를 사용하여 TR_ID를 자동으로 조회하고 요청을 수행합니다.
    retries: 실패(예외 발생) 시 재시도 횟수. None일 경우 config.MAX_RETRIES 값을 따릅니다.
    """
    # [추가] 시스템 트레이딩 우선순위 락 처리
    # RLock을 사용하여 시스템 트레이딩 스레드는 중복 획득 허용, 메인 스레드는 대기
    # [수정] 성능 최적화: 단순 조회(GET)는 락 없이 병렬 처리 허용, 주문(POST)만 동기화
    # [변경] 계좌 관련 조회(잔고, 체결, 가능수량 등)도 락을 적용하여 OPSQ2000(동시성 오류) 방지
    # 시세 조회(quotations) 등은 병렬 처리 유지
    is_account_related = "trading" in url_path or "balance" in action or "ccld" in action or "psbl" in action or "nccs" in action
    use_lock = (method != "GET") or is_account_related
    
    # [추가] API 호출 우선순위 제어 (시스템 트레이딩 스레드 우대)
    # AutoTrader, ConclusionMonitor, TelegramBot 스레드가 API를 호출하려 하면,
    # 일반 사용자(MainThread)의 API 호출은 잠시 대기하여 시스템 반응성을 확보함
    current_thread_name = threading.current_thread().name
    is_priority_thread = (
        current_thread_name in ["AutoTrader", "ConclusionMonitor", "TelegramBot"] or
        getattr(context.trade_context, 'is_system_trading', False)
    )
    
    if is_priority_thread:
        with context.API_PRIORITY_CONDITION:
            context.SYSTEM_API_WAIT_COUNT += 1
    else:
        with context.API_PRIORITY_CONDITION:
            if context.SYSTEM_API_WAIT_COUNT > 0:
                # 시스템 트레이딩 작업이 있으면 최대 1초간 양보 (Starvation 방지)
                context.API_PRIORITY_CONDITION.wait(timeout=1.0)
    
    if use_lock:
        context.SYSTEM_TRADING_LOCK.acquire()

    try:
        if timeout is None: timeout = config.DEFAULT_TIMEOUT
        # [추가] 모의투자 서버는 응답이 느려 짧은 timeout에서 ReadTimeout이 빈발하므로,
        #  모의투자일 때는 최소 timeout을 보장한다. (DEFAULT_TIMEOUT보다 크게 늘리지는 않음)
        if retries is None: retries = config.MAX_RETRIES
        
        # [추가] 토큰 만료 시 재시도를 위한 루프 (최대 1회 재시도)
        for attempt in range(2):
            try:
                token_to_use = get_current_token()
                base_url = config.REAL_URL
                
                # [수정] 컨텍스트에 따라 키 선택
                use_auto = getattr(context.trade_context, 'use_auto_account', False)
                if use_auto:
                    key = config.session.auto_app_key
                    secret = config.session.auto_app_secret
                    if not key: # Fallback
                        key = config.session.real_app_key; secret = config.session.real_app_secret
                else:
                    key = config.session.real_app_key
                    secret = config.session.real_app_secret

                current_tr_id = tr_id
                if current_tr_id is None:
                    try:
                        current_tr_id = constants.TR_ID_CONFIG[market][category][action]
                    except KeyError:
                        return {'rt_cd': '9999', 'msg1': f'TR_ID not found for {market}.{category}.{action}'}

                headers = {
                    "Content-Type": "application/json",
                    "authorization": f"Bearer {token_to_use}",
                    "appKey": key,
                    "appSecret": secret,
                    "tr_id": current_tr_id,
                    "custtype": "P"  # [추가] 개인고객(P) / 법인고객(B) 명시 (API 엄격화 대비)
                }
                
                # [추가] 디버그 모드일 때 헤더 정보 출력 (민감정보 마스킹)
                if _api()._is_screen_output_allowed() and config.SCREEN_DEBUG_LEVEL == "DEBUG":
                    masked_headers = headers.copy()
                    if 'authorization' in masked_headers:
                        masked_headers['authorization'] = masked_headers['authorization'][:20] + "..."
                    if 'appSecret' in masked_headers:
                        masked_headers['appSecret'] = "********"
                    config.console.print(f"[dim cyan]  > Headers: {masked_headers}[/dim cyan]")
                
                full_url = f"{base_url}/{url_path}"
                
                # [수정] 재시도 로직을 session.request로 위임 (retries 인자 전달)
                # [Fix] 그동안 retries가 session에 전달되지 않아 ThrottledSession이 항상
                #  config.MAX_RETRIES로 재시도했다. retries=0(빠른 실패) 의도가 무력화되어
                #  네트워크 장애 시 불필요하게 수십 초 블로킹되던 문제를 바로잡는다.
                if method == "GET":
                    res = _api().session.get(full_url, headers=headers, params=params, timeout=timeout, retries=retries)
                else:
                    res = _api().session.post(full_url, headers=headers, data=json.dumps(data) if data else None, timeout=timeout, retries=retries)
                
                return res.json()
            except Exception as e:
                # [추가] 토큰 만료 예외 감지 시 갱신 후 재시도
                if "Token Expired" in str(e) and attempt == 0:
                    logger.warning(f"[API] 토큰 만료 감지({str(e)}). 갱신 후 재시도합니다.")
                    #  [Fix 2026-09-05] 마지막 줄이 분기 **밖**에 있어, 자동 계좌 토큰이
                    #   만료되면 자동 토큰을 갱신한 **뒤 수동 토큰까지 강제 재발급**했다.
                    #   두 가지가 나빴다:
                    #    · KIS 는 앱키당 1분에 한 번만 발급하고 발급 시 이전 토큰을 무효화한다.
                    #      멀쩡한 수동 토큰을 버리고 그 1분 예산까지 태우므로, 곧바로 수동
                    #      토큰이 진짜 만료되면 재발급이 거부된다.
                    #    · new_token 이 항상 수동 토큰이라, 자동 갱신이 실패하고 수동만
                    #      성공해도 TOKEN_EXPIRED 를 내려 '정상'으로 표시했다.
                    #   (scheduler._heartbeat_context · trader 관제 표시에 이어 같은 모양의
                    #    세 번째 자리다 — 분기 밖의 마지막 줄을 의심할 것.)
                    if getattr(context.trade_context, 'use_auto_account', False):
                        new_token = get_auto_access_token(force_refresh=True)
                    else:
                        new_token = get_real_access_token(force_refresh=True)

                    if new_token:
                        context.TOKEN_EXPIRED = False

                    continue

                # [추가] 네트워크 레벨 장애(연결 거부/타임아웃 등)를 API 논리 오류와 구분한다.
                #  msg_cd='NETERR'로 표시하여 상위 로직/로그가 "일시적 서버·네트워크 장애"임을
                #  명확히 인지하도록 한다. (토큰 만료·잔고 부족 등 진짜 오류와 혼동 방지)
                if isinstance(e, (requests.exceptions.ConnectionError, requests.exceptions.Timeout)):
                    return {'rt_cd': '9999', 'msg_cd': 'NETERR', 'msg1': f'네트워크 연결 실패: {str(e)}'}

                return {'rt_cd': '9999', 'msg1': str(e)}
    finally:
        if use_lock:
            context.SYSTEM_TRADING_LOCK.release()
            
        # [추가] 우선순위 카운트 감소 및 대기 스레드 알림
        if is_priority_thread:
            with context.API_PRIORITY_CONDITION:
                context.SYSTEM_API_WAIT_COUNT -= 1
                if context.SYSTEM_API_WAIT_COUNT <= 0:
                    context.SYSTEM_API_WAIT_COUNT = 0
                    context.API_PRIORITY_CONDITION.notify_all()
