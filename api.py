# api.py
import requests
import logging
import json
import time
import sys
import ssl
import urllib3
import re
import os
import threading
import yfinance as yf
import pandas as pd
from datetime import datetime, timedelta
from requests.adapters import HTTPAdapter
from urllib3.poolmanager import PoolManager
from urllib3.util.retry import Retry
from collections import deque
import config
import constants

ssl._create_default_https_context = ssl._create_unverified_context
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

logger = logging.getLogger(__name__)

def clear_yfinance_cache():
    """yfinance 캐시 파일(.sqlite)을 강제로 삭제하여 DB Lock 문제를 해결합니다."""
    if config.SCREEN_DEBUG_LEVEL in ["TRACE", "DEBUG"]:
        config.console.print("[dim cyan][DEBUG] yfinance 캐시 정리 시도...[/dim cyan]")
    
    possible_paths = [
        os.path.join(os.path.expanduser("~"), ".cache", "py-yfinance"),
        os.path.join(os.environ.get("LOCALAPPDATA", ""), "py-yfinance"),
        os.path.join(os.path.expanduser("~"), "Library", "Caches", "py-yfinance")
    ]
    
    deleted_count = 0
    for c_path in possible_paths:
        if os.path.exists(c_path):
            try:
                for f in os.listdir(c_path):
                    if f.endswith('.sqlite') or f.endswith('.sqlite-journal'):
                        try:
                            os.remove(os.path.join(c_path, f))
                            deleted_count += 1
                        except Exception: pass
            except Exception: pass
    
    if config.SCREEN_DEBUG_LEVEL == "DEBUG" and deleted_count > 0:
        config.console.print(f"[dim cyan][DEBUG] 캐시 파일 {deleted_count}개 삭제 완료[/dim cyan]")

def fetch_yfinance_data(tickers, period=None, start=None, end=None, interval="1d", group_by='column'):
    """yfinance 데이터 조회 통합 함수 (DB Lock 발생 시 자동 캐시 정리 후 재시도)"""
    try:
        return yf.download(tickers, period=period, start=start, end=end, interval=interval, group_by=group_by, progress=False, threads=False)
    except Exception as e:
        if "database" in str(e).lower():
            clear_yfinance_cache()
            return yf.download(tickers, period=period, start=start, end=end, interval=interval, group_by=group_by, progress=False, threads=False)
        raise e

class TLSAdapter(HTTPAdapter):
    def init_poolmanager(self, connections, maxsize, block=False):
        self.poolmanager = PoolManager(num_pools=connections, maxsize=maxsize, block=block, ssl_version=ssl.PROTOCOL_TLSv1_2)

def _get_telegram_footer():
    """텔레그램 메시지용 계좌 정보 꼬리말 생성"""
    if not config.TELEGRAM_BOT_TOKEN:
        return

    cano = config.session.cano
    acc_label = "모의" if config.session.is_simulation else "실전"

    # 시스템 트레이딩 컨텍스트(AUTO 계좌) 확인
    if not config.session.is_simulation and getattr(config.trade_context, 'use_auto_account', False) and config.session.auto_cano:
        cano = config.session.auto_cano
        acc_label = "자동"

    instance_name = config.TELEGRAM_INSTANCE_NAME
    return f"[{instance_name} | {acc_label} {cano}]"

def send_telegram_message(message):
    """텔레그램 메시지 전송 (시스템 트레이딩 알림용)"""
    if not config.TELEGRAM_BOT_TOKEN or not config.TELEGRAM_CHAT_ID:
        return

    account_info = _get_telegram_footer()
    
    # [추가] rich 라이브러리 색상 태그 제거 (텔레그램 전송용)
    # 예: [red]텍스트[/] -> 텍스트. 소문자로 시작하는 태그만 제거하여 [시스템] 등은 유지
    clean_message = re.sub(r'\[/?[a-z]+(?:[\s=][^\]]*)?\]', '', message)

    # [추가] HTML 이스케이프 처리 (HTML 파싱 모드 사용 시 필수)
    clean_message = clean_message.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")

    # [수정] 종목 코드에 링크 자동 적용 (네이버 증권)
    # 패턴: 괄호 안의 6자리 숫자/영문(국내) 또는 영문 대문자(해외) -> 예: (005930), (0080G0), (AAPL)
    def add_stock_link(match):
        code = match.group(1)
        
        # 1. 국내 주식 (6자리)
        if len(code) == 6:
            # 국내 주식: 네이버 증권 모바일 (최신 URL 구조 적용)
            url = f"https://m.stock.naver.com/domestic/stock/{code}"
        # 2. 해외 주식
        else:
            # 거래소 정보 확인 (config.session.exchange_cache 활용)
            exchange = config.session.exchange_cache.get(code, "")
            suffix = ""
            
            # 네이버 증권 해외주식 거래소 접미사 매핑
            if exchange in ["NAS", "NASD"]: suffix = ".O"   # NASDAQ
            elif exchange in ["NYS", "NYSE"]: suffix = ".N" # NYSE
            elif exchange in ["AMS", "AMEX"]: suffix = ".A" # AMEX
            
            if suffix:
                url = f"https://m.stock.naver.com/worldstock/stock/{code}{suffix}"
            else:
                # 거래소 정보가 없거나 매핑되지 않으면 검색 페이지로 연결
                url = f"https://m.stock.naver.com/worldstock/search?query={code}"
                
        return f'(<a href="{url}">{code}</a>)'
    
    clean_message = re.sub(r'\(([0-9A-Z]{6}|[A-Z]{1,5})\)', add_stock_link, clean_message)

    # [수정] 계좌 정보를 메시지 가장 마지막에 추가 (가독성을 위해 한 줄 공백 추가)
    final_msg = f"{clean_message.rstrip()}\n\n{account_info}"

    # [수정] 전송 메시지 로그 기록 (시스템 로그로 변경)
    log_content = final_msg.replace('\n', ' | ')
    logger.info(f"[Telegram] 메시지 발송: {log_content}")

    url = f"https://api.telegram.org/bot{config.TELEGRAM_BOT_TOKEN}/sendMessage"
    # [수정] HTML 파싱 모드 활성화 및 링크 미리보기 비활성화
    data = {"chat_id": config.TELEGRAM_CHAT_ID, "text": final_msg, "parse_mode": "HTML", "disable_web_page_preview": True}
    
    # [추가] 화면 디버그 로그 (요청)
    if config.SCREEN_DEBUG_LEVEL in ["TRACE", "DEBUG"]:
        config.console.print(f"[dim cyan][TRACE] REQ (TELEGRAM) | POST {url}[/dim cyan]")
        if config.SCREEN_DEBUG_LEVEL == "DEBUG":
            config.console.print(f"[dim cyan]  > Message: {message.replace(chr(10), ' ')}[/dim cyan]")

    # [수정] 재시도 로직 추가 (최대 3회)
    max_retries = 3
    for attempt in range(max_retries):
        try:
            # 매매 로직 지연 방지를 위해 짧은 타임아웃 설정 (재시도 시 조금씩 증가)
            current_timeout = 1 + (attempt * 0.5)
            res = requests.post(url, data=data, timeout=current_timeout)
            
            # [추가] 화면 디버그 로그 (응답)
            if config.SCREEN_DEBUG_LEVEL in ["TRACE", "DEBUG"]:
                config.console.print(f"[dim magenta][TRACE] RES (TELEGRAM) Status:{res.status_code} ({attempt+1}/{max_retries})[/dim magenta]")
                if config.SCREEN_DEBUG_LEVEL == "DEBUG" and res.status_code != 200:
                     config.console.print(f"[dim red]  > Error: {res.text}[/dim red]")
            
            if res.status_code == 200:
                logger.info("[Telegram] 전송 성공")
                return # 성공 시 함수 종료
            else:
                logger.error(f"[Telegram] 전송 실패({attempt+1}/{max_retries}) Status: {res.status_code}, Msg: {res.text}")
        except Exception as e:
            # [추가] 화면 디버그 로그 (예외)
            if config.SCREEN_DEBUG_LEVEL in ["TRACE", "DEBUG"]:
                config.console.print(f"[dim red][TRACE] ERR (TELEGRAM) {str(e)} ({attempt+1}/{max_retries})[/dim red]")

            logger.error(f"[Telegram] 전송 중 오류 발생({attempt+1}/{max_retries}): {str(e)}")
        
        # 마지막 시도가 아니면 대기
        if attempt < max_retries - 1:
            time.sleep(1)
            
    logger.error("[Telegram] 최종 전송 실패")

_last_alert_time = 0 # [추가] 텔레그램 알림 스로틀링용

class ThrottledSession(requests.Session):
    def __init__(self):
        super().__init__()
        self.last_request_time_sim = 0
        self.last_request_time_real = 0
        self.request_history = deque()
        self.lock = threading.Lock() # [추가] Rate Limit 계산 동기화를 위한 락

    def _get_current_tps(self):
        now = time.time()
        while self.request_history and self.request_history[0] < now - 1.0:
            self.request_history.popleft()
        return len(self.request_history)

    def request(self, method, url, *args, **kwargs):
        global _last_alert_time
        is_real_server = "openapi.koreainvestment.com" in url and "openapivts" not in url
        is_sim_server = "openapivts.koreainvestment.com" in url
        
        target_limit = 0
        last_time = 0
        server_type = "EXTERNAL"
        
        # [수정] 락을 사용하여 Rate Limit 계산 및 대기 로직을 원자적으로 처리
        with self.lock:
            if is_real_server:
                target_limit = config.REAL_TX_PER_SECOND
                last_time = self.last_request_time_real
                server_type = "REAL"
            elif is_sim_server:
                target_limit = config.SIM_TX_PER_SECOND
                last_time = self.last_request_time_sim
                server_type = "SIMULATION"

            if target_limit > 0:
                min_interval = (1.0 / target_limit) * 1.05
                elapsed = time.time() - last_time
                if elapsed < min_interval:
                    wait_time = min_interval - elapsed
                    time.sleep(wait_time)

            # [수정] 요청 전송 직전에 시간 갱신 (멀티스레드 환경에서 중복 전송 방지)
            now_req = time.time()
            if is_real_server:
                self.last_request_time_real = now_req
            elif is_sim_server:
                self.last_request_time_sim = now_req

            self.request_history.append(now_req)
            
        current_tps = self._get_current_tps()

        if config.SCREEN_DEBUG_LEVEL in ["TRACE", "DEBUG"] and (is_sim_server or is_real_server):
            config.console.print(f"[dim cyan][TRACE] REQ ({server_type}) TPS:{current_tps:.1f} | {method} {url}[/dim cyan]")
            if config.SCREEN_DEBUG_LEVEL == "DEBUG":
                if kwargs.get('params'): config.console.print(f"[dim cyan]  > Params: {kwargs['params']}[/dim cyan]")
                if kwargs.get('data'): config.console.print(f"[dim cyan]  > Body Data: {kwargs['data']}[/dim cyan]")
                if kwargs.get('json'): config.console.print(f"[dim cyan]  > JSON Data: {kwargs['json']}[/dim cyan]")

        # [변경] 재시도 로직 추가 (MAX_RETRIES 사용)
        max_retries = config.MAX_RETRIES
        
        for attempt in range(max_retries + 1):
            try:
                if 'timeout' not in kwargs:
                    kwargs['timeout'] = config.DEFAULT_TIMEOUT
                
                response = super().request(method, url, *args, **kwargs)

                if config.SCREEN_DEBUG_LEVEL in ["TRACE", "DEBUG"] and (is_sim_server or is_real_server):
                    rt_cd = "-"
                    msg_cd = "-"
                    desc = "정상"
                    res_data = None
                    try:
                        res_data = response.json()
                        rt_cd = res_data.get('rt_cd') or "-"
                        msg_cd = res_data.get('msg_cd') or "-"
                        if msg_cd == 'OPSQ2000': desc = "서버 지연"
                        elif msg_cd in ['EGW00123', 'EGW00121']: desc = "토큰 만료"
                        elif rt_cd != '0' and rt_cd != '-': desc = "오류 발생"
                    except: pass
                    
                    url_tail = url.split('/')[-1].split('?')[0]
                    config.console.print(f"[dim magenta][TRACE] RES ({server_type}) Status:{response.status_code} RT_CD:{rt_cd} MSG_CD:{msg_cd} ({desc}) | {url_tail}[/dim magenta]")
                    
                    if config.SCREEN_DEBUG_LEVEL == "DEBUG" and res_data:
                        config.console.print(f"[dim magenta]  > Response Data: {json.dumps(res_data, ensure_ascii=False, indent=2)}[/dim magenta]")

                try:
                    if response.text and response.text.startswith('{'):
                        res_json = response.json()
                        msg_cd = res_json.get('msg_cd')
                        
                        if msg_cd == 'OPSQ2000':
                            # [개선] 허위 에러 메시지 설명 추가
                            msg1 = res_json.get('msg1', '')
                            note = ""
                            if "INVALID_CHECK_ACNO" in msg1:
                                note = " (※ 서버 내부 오류)"
                                
                                # [Fix] 계좌번호 오류 메시지가 떴지만, 일시적인 세션/토큰 꼬임일 수 있으므로
                                # 즉시 실패 처리하지 않고 토큰을 강제로 갱신하여 복구를 시도합니다.
                                if attempt < max_retries:
                                    # [개선] 토큰이 방금 발급된 것이라면(60초 이내), 재발급해도 소용없으므로 대기만 수행
                                    token_key = "SIMULATION" if is_sim_server else "REAL"
                                    if is_real_server and getattr(config.trade_context, 'use_auto_account', False):
                                        token_key = "AUTO"
                                        
                                    if config.session.is_token_recently_issued(token_key, seconds=60):
                                        logger.warning(f"⚠️ 계좌번호 인식 오류(OPSQ2000). 토큰은 최신입니다. 잠시 대기 후 재시도합니다...")
                                        time.sleep(2.0)
                                        continue

                                    logger.warning(f"⚠️ 계좌번호 인식 오류(OPSQ2000) 감지. 토큰 세션 재설정을 시도합니다...")
                                    
                                    new_token = None
                                    if is_sim_server:
                                        new_token = get_access_token(force_refresh=True)
                                    elif is_real_server:
                                        if getattr(config.trade_context, 'use_auto_account', False):
                                            new_token = get_auto_access_token(force_refresh=True)
                                        else:
                                            new_token = get_real_access_token(force_refresh=True)
                                    
                                    if new_token:
                                        # 갱신된 토큰으로 헤더 교체
                                        if 'headers' in kwargs:
                                            kwargs['headers']['authorization'] = f"Bearer {new_token}"
                                            kwargs['headers']['Authorization'] = f"Bearer {new_token}"
                                        else:
                                            kwargs['headers'] = {"authorization": f"Bearer {new_token}"}
                                        
                                        # 1초 대기 후 재요청 (서버 동기화 시간 고려)
                                        time.sleep(1.0)
                                        response = super().request(method, url, *args, **kwargs)
                                        return response

                            # [수정] 고정 대기 대신 지수 백오프(Exponential Backoff) 적용
                            wait_time = config.RETRY_DELAY_SERVER * (2 ** attempt)
                            msg = f"⚠️ KIS 서버 처리 지연(OPSQ2000) 발생{note}. 서버 상태가 불안정할 수 있습니다. {wait_time:.1f}초 대기 후 재시도..."
                            logger.warning(msg)
                            # [추가] 시스템 트레이딩 로그 기록
                            if config.SYSTEM_LOGGER: config.SYSTEM_LOGGER(f"[API] {msg}")
                            
                            # [추가] 텔레그램 긴급 알림 (5분 간격 제한)
                            if time.time() - _last_alert_time > 300:
                                send_telegram_message(f"⚠️ [서버 경고] KIS 서버 처리 지연(OPSQ2000).\n(자동 재시도 중...)")
                                _last_alert_time = time.time()
                            
                            time.sleep(wait_time)
                            
                            # [개선] 재시도 루프 활용 (단발성 재시도가 아닌 루프 continue)
                            if attempt < max_retries:
                                continue

                        elif msg_cd in ['EGW00123', 'EGW00121']:
                            logger.warning(f"토큰 만료 감지(Code: {msg_cd}). 토큰을 갱신합니다...")
                            
                            new_token = None
                            if is_sim_server:
                                new_token = get_access_token(force_refresh=True)
                            elif is_real_server:
                                if getattr(config.trade_context, 'use_auto_account', False):
                                    new_token = get_auto_access_token(force_refresh=True)
                                else:
                                    new_token = get_real_access_token(force_refresh=True)
                            
                            if new_token:
                                if 'headers' in kwargs:
                                    kwargs['headers']['authorization'] = f"Bearer {new_token}"
                                    kwargs['headers']['Authorization'] = f"Bearer {new_token}"
                                else:
                                    kwargs['headers'] = {"authorization": f"Bearer {new_token}"}
                                response = super().request(method, url, *args, **kwargs)
                        
                        # [추가] Rate Limit 초과 (EGW00201) 적응형 재시도
                        elif msg_cd == 'EGW00201':
                            if attempt < max_retries:
                                # Exponential Backoff: 0.5초 -> 1.0초 -> 2.0초 ...
                                wait_time = 0.5 * (2 ** attempt)
                                msg = f"초당 전송건수 초과(EGW00201). {wait_time:.1f}초 대기 후 재시도합니다 ({attempt+1}/{max_retries})..."
                                logger.warning(msg)
                                # [추가] 시스템 트레이딩 로그 기록
                                if config.SYSTEM_LOGGER: config.SYSTEM_LOGGER(f"[API] {msg}")
                                
                                time.sleep(wait_time)
                                continue
                except Exception: pass
                
                return response

            except Exception as e:
                # 연결 끊김 에러 체크
                err_str = str(e)
                is_disconnect = "Connection aborted" in err_str or "RemoteDisconnected" in err_str or "timed out" in err_str
                
                # 재시도 가능한 에러이고 횟수가 남았으면 대기 후 재시도
                if is_disconnect and attempt < max_retries:
                    wait_time = 0.5 * (2 ** attempt)
                    msg = f"⚠️ KIS 서버 연결 끊김(불안정) 감지. {wait_time}초 후 재시도합니다 ({attempt+1}/{max_retries})..."
                    if config.SCREEN_DEBUG_LEVEL in ["TRACE", "DEBUG"]:
                        config.console.print(f"[dim yellow][TRACE] {msg}[/dim yellow]")
                    # [추가] 시스템 트레이딩 로그 기록
                    if config.SYSTEM_LOGGER: config.SYSTEM_LOGGER(f"[API] {msg}")
                    
                    # [추가] 텔레그램 긴급 알림 (5분 간격 제한)
                    if time.time() - _last_alert_time > 300:
                        send_telegram_message(f"⚠️ [서버 경고] KIS 서버 연결 불안정.\n내용: {str(e)}\n(자동 재시도 중...)")
                        _last_alert_time = time.time()
                    
                    time.sleep(wait_time)
                    continue
                
                # 재시도 불가능하거나 횟수 초과 시 에러 발생
                logger.error(f"Request Failed: {str(e)}")
                raise e

session = ThrottledSession()

# [수정] 연결 끊김(RemoteDisconnected) 등 네트워크 레벨 에러 자동 재시도 설정
retry_strategy = Retry(
    total=3,
    backoff_factor=0.5,
    status_forcelist=[500, 502, 503, 504],
    allowed_methods=["GET", "POST"],
    raise_on_status=False
)
session.mount('https://', TLSAdapter(max_retries=retry_strategy))

def get_current_token():
    # [추가] 시스템 트레이딩 컨텍스트 확인
    if getattr(config.trade_context, 'use_auto_account', False) and not config.session.is_simulation:
        return get_auto_access_token()
        
    if config.session.is_simulation:
        return get_access_token()
    else:
        return get_real_access_token()

def get_access_token(force_refresh=False):
    # [Fix] 토큰 갱신 경합 방지 (Thread-Safe)
    with config.TOKEN_REFRESH_LOCK:
        return _get_access_token_internal(force_refresh)

def _get_access_token_internal(force_refresh=False):
    if not force_refresh:
        token = config.session.get_valid_token("SIMULATION")
        if token:
            logger.debug("모의 캐시 토큰 사용")
            return token

    # [추가] 키 누락 시 조기 리턴 (불필요한 서버 요청 방지)
    if not config.session.app_key or not config.session.app_secret:
        logger.error("모의투자 API Key 또는 Secret이 설정되지 않았습니다. (환경변수 SIM_APP_KEY, SIM_APP_SECRET 확인 필요)")
        return None

    headers = {"content-type": "application/json"}
    # [수정] session에서 키 사용
    body = {"grant_type": "client_credentials", "appkey": config.session.app_key, "appsecret": config.session.app_secret}
    url = f"{config.session.url_base}/oauth2/tokenP"
    
    try:
        logger.info("모의투자 토큰 신규 발급 요청...")
        res = session.post(url, headers=headers, data=json.dumps(body), verify=False)
        res_json = res.json()
        
        if 'access_token' in res_json:
            token = res_json['access_token']
            expired = res_json.get('access_token_token_expired')
            config.session.set_token("SIMULATION", token, expired)
            logger.info("[green]모의투자 토큰 발급 완료[/green]")
            return token
        
        elif res_json.get('error_code') == 'EGW00133':
            logger.warning("빈도 제한. 캐시 재확인.")
            # 디스크에서 다시 로드 시도
            token = config.session.get_valid_token("SIMULATION", force_disk_reload=True)
            if token:
                logger.info("[green]캐시된 유효 토큰을 복구했습니다.[/green]")
                return token
            return None
        else:
            logger.error(f"토큰 발급 실패: {res.text}")
            return None
            
    except Exception as e:
        logger.error(f"접속 오류: {str(e)}")
        return None

def get_real_access_token(force_refresh=False):
    # [Fix] 토큰 갱신 경합 방지 (Thread-Safe)
    with config.TOKEN_REFRESH_LOCK:
        return _get_real_access_token_internal(force_refresh)

def _get_real_access_token_internal(force_refresh=False):
    if not config.session.real_app_key: return None

    if not force_refresh:
        token = config.session.get_valid_token("REAL")
        if token:
            logger.debug("실전 캐시 토큰 사용")
            return token

    headers = {"content-type": "application/json"}
    # [수정] session에서 키 사용
    body = {"grant_type": "client_credentials", "appkey": config.session.real_app_key, "appsecret": config.session.real_app_secret}
    url = f"{config.REAL_URL}/oauth2/tokenP"
    
    try:
        logger.info("실전투자 토큰 신규 발급 요청...")
        res = session.post(url, headers=headers, data=json.dumps(body), verify=False)
        
        if res.status_code == 200:
            res_json = res.json()
            token = res_json['access_token']
            expired = res_json.get('access_token_token_expired')
            config.session.set_token("REAL", token, expired)
            logger.info("[green]실전투자 토큰 발급 완료[/green]")
            return token
        
        else:
            try:
                err_json = res.json()
                if err_json.get('error_code') == 'EGW00133':
                    logger.warning("실전 토큰 발급 빈도 제한(EGW00133). 캐시를 재확인합니다.")
                    token = config.session.get_valid_token("REAL", force_disk_reload=True)
                    if token:
                        logger.info("[green]캐시된 유효 토큰을 복구했습니다.[/green]")
                        return token
                    return None
            except: pass
            
            logger.error(f"실전 토큰 발급 실패: {res.text}")
            
    except Exception as e:
        logger.error(f"실전 토큰 발급 중 오류: {e}")
        pass
        
    return None

def get_auto_access_token(force_refresh=False):
    # [Fix] 토큰 갱신 경합 방지 (Thread-Safe)
    with config.TOKEN_REFRESH_LOCK:
        return _get_auto_access_token_internal(force_refresh)

def _get_auto_access_token_internal(force_refresh=False):
    """시스템 트레이딩 전용 계좌 토큰 발급"""
    # [추가] 실전투자 계좌와 자동매매 계좌의 AppKey가 동일한 경우, 실전투자 토큰을 공유 사용
    # (동일한 Key로 짧은 시간 내 중복 토큰 발급 요청 시 EGW00133 에러 발생 방지)
    if config.session.auto_app_key and config.session.real_app_key and \
       config.session.auto_app_key == config.session.real_app_key:
        return get_real_access_token(force_refresh)

    if not config.session.auto_app_key: return None

    if not force_refresh:
        token = config.session.get_valid_token("AUTO")
        if token:
            logger.debug("자동매매 캐시 토큰 사용")
            return token

    headers = {"content-type": "application/json"}
    # [수정] session에서 키 사용
    body = {"grant_type": "client_credentials", "appkey": config.session.auto_app_key, "appsecret": config.session.auto_app_secret}
    url = f"{config.REAL_URL}/oauth2/tokenP"
    
    try:
        logger.info("자동매매용 토큰 신규 발급 요청...")
        res = session.post(url, headers=headers, data=json.dumps(body), verify=False)
        
        if res.status_code == 200:
            res_json = res.json()
            token = res_json['access_token']
            expired = res_json.get('access_token_token_expired')
            config.session.set_token("AUTO", token, expired)
            logger.info("[green]자동매매용 토큰 발급 완료[/green]")
            return token
        else:
            try:
                err_json = res.json()
                if err_json.get('error_code') == 'EGW00133':
                    logger.warning("자동매매 토큰 발급 빈도 제한(EGW00133). 캐시를 재확인합니다.")
                    token = config.session.get_valid_token("AUTO", force_disk_reload=True)
                    if token:
                        logger.info("[green]캐시된 유효 토큰을 복구했습니다.[/green]")
                        return token
                    return None
            except: pass
            
            logger.error(f"자동매매 토큰 발급 실패: {res.text}")
    except Exception as e:
        logger.error(f"자동매매 토큰 발급 중 오류: {e}")
        
    return None

def safe_int(value):
    try:
        if value is None: return 0
        s_val = str(value).strip().replace(',', '')
        if not s_val: return 0
        return int(float(s_val))
    except Exception: return 0

def call_api(url_path, market, category, action, params=None, data=None, method="GET", timeout=None, retries=None, tr_id=None):
    """
    통합 API 호출 함수
    constants.TR_ID_CONFIG를 사용하여 TR_ID를 자동으로 조회하고 요청을 수행합니다.
    retries: 실패(예외 발생) 시 재시도 횟수. None일 경우 config.MAX_RETRIES 값을 따릅니다.
    """
    # [추가] 시스템 트레이딩 우선순위 락 처리
    # RLock을 사용하여 시스템 트레이딩 스레드는 중복 획득 허용, 메인 스레드는 대기
    # [수정] 성능 최적화: 단순 조회(GET)는 락 없이 병렬 처리 허용, 주문(POST)만 동기화
    use_lock = (method != "GET")
    if use_lock:
        config.SYSTEM_TRADING_LOCK.acquire()

    try:
        if timeout is None: timeout = config.DEFAULT_TIMEOUT
        if retries is None: retries = config.MAX_RETRIES
        
        token_to_use = get_current_token()
        base_url = config.session.url_base if config.session.is_simulation else config.REAL_URL
        
        # [수정] 컨텍스트에 따라 키 선택
        use_auto = getattr(config.trade_context, 'use_auto_account', False)
        if use_auto and not config.session.is_simulation:
            key = config.session.auto_app_key
            secret = config.session.auto_app_secret
            if not key: # Fallback
                key = config.session.real_app_key; secret = config.session.real_app_secret
        else:
            key = config.session.app_key if config.session.is_simulation else config.session.real_app_key
            secret = config.session.app_secret if config.session.is_simulation else config.session.real_app_secret

        env_key = "sim" if config.session.is_simulation else "real"
        if tr_id is None:
            try:
                tr_id = constants.TR_ID_CONFIG[market][category][action][env_key]
            except KeyError:
                return {'rt_cd': '9999', 'msg1': f'TR_ID not found for {market}.{category}.{action}'}

        headers = {
            "Content-Type": "application/json",
            "authorization": f"Bearer {token_to_use}",
            "appKey": key,
            "appSecret": secret,
            "tr_id": tr_id
        }
        
        full_url = f"{base_url}/{url_path}"
        
        last_error = None
        for attempt in range(retries + 1):
            try:
                if method == "GET":
                    res = session.get(full_url, headers=headers, params=params, verify=False, timeout=timeout)
                else:
                    res = session.post(full_url, headers=headers, data=json.dumps(data) if data else None, verify=False, timeout=timeout)
                
                res_json = res.json()
                
                
                return res_json
            except Exception as e:
                last_error = e
                if attempt < retries:
                    wait_time = 0.5 * (2 ** attempt)
                    time.sleep(wait_time)
                    continue
        
        return {'rt_cd': '9999', 'msg1': str(last_error)}
    finally:
        if use_lock:
            config.SYSTEM_TRADING_LOCK.release()

def get_stock_name_by_code(code, is_overseas):
    final_name = None
    if not is_overseas:
        url = f"https://finance.naver.com/item/main.naver?code={code}"
        try:
            headers = {'User-Agent': 'Mozilla/5.0'}
            r = session.get(url, headers=headers, verify=False, timeout=3)
            m_og = re.search(r'meta property="og:title" content="(.*?)"', r.text)
            if m_og:
                raw_title = m_og.group(1).strip()
                if "페이지를 찾을 수 없습니다" not in raw_title:
                    clean_name = re.sub(r'\s*\(\d{6}\)', '', raw_title)
                    clean_name = re.sub(r'\s*[:|-]\s*(Npay|네이버|Naver|금융|증권).*', '', clean_name, flags=re.IGNORECASE)
                    final_name = clean_name.strip()
                if final_name in ["Npay 증권", "네이버 페이 증권", "증권", "금융", "네이버 금융"]: final_name = None
            else: final_name = code
        except Exception: final_name = code
    else:
        try:
            with open(os.devnull, 'w') as fnull:
                old_stderr = sys.stderr; sys.stderr = fnull
                try:
                    ticker = yf.Ticker(code); info = ticker.info
                    if info: final_name = info.get('longName') or info.get('shortName')
                except: pass
                finally: sys.stderr = old_stderr
        except Exception: pass
    if not final_name and code: return code
    return final_name

def get_chart_data(code, is_overseas=False):
    """
    기술적 분석을 위한 차트 데이터를 조회합니다.
    현재 로직은 '일봉(Daily)' 데이터를 기준으로 하며, 장 중에는 당일의 실시간 시세가 반영된 일봉 데이터가 포함됩니다.
    """
    now = datetime.now()
    today = now.strftime("%Y%m%d")
    start_date_origin = (now - timedelta(days=config.INDICATOR_PARAMS["CHART_LOOKBACK_DAYS"])).strftime("%Y%m%d")
    
    is_index = (code.startswith('^') or code.endswith('=F') or code.endswith('=X') or code == 'DX-Y.NYB')
    if is_index:
        try:
            df = fetch_yfinance_data(code, period="2y")
            if df is None or df.empty: return pd.DataFrame()
            if isinstance(df.columns, pd.MultiIndex):
                try: df.columns = df.columns.get_level_values(0)
                except: pass
            df.reset_index(inplace=True)
            df.rename(columns={'Date': 'date', 'Close': 'close', 'High': 'high', 'Low': 'low', 'Open': 'open', 'Volume': 'volume'}, inplace=True)
            cols = ['date', 'close', 'high', 'low', 'volume']
            for c in cols:
                if c not in df.columns: df[c] = 0
            df = df[cols].copy()
            df['date'] = df['date'].apply(lambda x: x.strftime('%Y%m%d'))
            df = df[df['date'] >= start_date_origin]
            return df.sort_values('date', ascending=True).reset_index(drop=True)
        except Exception: return pd.DataFrame()

    if not is_overseas:
        url_path = "uapi/domestic-stock/v1/quotations/inquire-daily-itemchartprice"
        all_items = []
        current_end_date = today
        current_start_date = start_date_origin
        
        for i in range(5):
            params = {"FID_COND_MRKT_DIV_CODE": "J", "FID_INPUT_ISCD": code, "FID_INPUT_DATE_1": current_start_date, "FID_INPUT_DATE_2": current_end_date, "FID_PERIOD_DIV_CODE": "D", "FID_ORG_ADJ_PRC": "1"}
            page_success = False
            for retry in range(3):
                data = call_api(url_path, "domestic", "quotations", "chart", params=params, timeout=3)
                if data.get('rt_cd') == '0':
                    items = data.get('output2')
                    if items:
                        all_items.extend(items)
                        temp_dates = sorted([x['stck_bsop_date'] for x in items])
                        current_end_date = (datetime.strptime(temp_dates[0], "%Y%m%d") - timedelta(days=1)).strftime("%Y%m%d")
                        page_success = True
                    else: page_success = True
                    break
                elif data.get('msg_cd') == 'EGW00201': time.sleep(0.5)
                else: time.sleep(0.2)
            if page_success and len(all_items) >= 250: break
            
        if not all_items: return pd.DataFrame()
        df = pd.DataFrame(all_items).drop_duplicates(subset=['stck_bsop_date'])
        df = df[df['stck_bsop_date'] >= start_date_origin]
        df = df[['stck_bsop_date', 'stck_clpr', 'stck_oprc', 'stck_hgpr', 'stck_lwpr', 'acml_vol']].copy()
        df.columns = ['date', 'close', 'open', 'high', 'low', 'volume']
        df = df.astype({'close': float, 'open': float, 'high': float, 'low': float, 'volume': float})
        return df.sort_values('date', ascending=True).reset_index(drop=True).tail(250)
    
    else:
        cached_ex = config.session.exchange_cache.get(code)
        exchanges = []
        if cached_ex: exchanges.append(cached_ex)
        for e in ["NAS", "NYS", "AMS", "NASD", "NYSE", "AMEX"]:
            if e not in exchanges: exchanges.append(e)
            
        url_path = "uapi/overseas-price/v1/quotations/dailyprice"
        
        for excd in exchanges:
            all_items = []
            next_bymd = today
            page_success = False
            
            for i in range(4):
                params = {"AUTH": "", "EXCD": excd, "SYMB": code, "GUBN": "0", "BYMD": next_bymd, "MODP": "1", "KEYB": code}
                sub_success = False
                for retry in range(2):
                    data = call_api(url_path, "overseas", "quotations", "chart", params=params, timeout=3)
                    if data.get('rt_cd') == '0':
                        items = data.get('output2')
                        if items:
                            if not all_items: 
                                if cached_ex != excd: config.session.update_cache_and_save(code, excd)
                            all_items.extend(items)
                            last = items[-1]['xymd']
                            next_bymd = (datetime.strptime(last, "%Y%m%d") - timedelta(days=1)).strftime("%Y%m%d")
                            sub_success = True
                        else: sub_success = True
                        break
                    elif data.get('msg_cd') == 'EGW00201': time.sleep(0.5)
                    else: time.sleep(0.1)
                if not sub_success: break
                if len(all_items) >= 250: break
            
            if all_items:
                df = pd.DataFrame(all_items).drop_duplicates(subset=['xymd'])
                df.rename(columns={'xymd': 'date', 'clos': 'close', 'open': 'open', 'high': 'high', 'low': 'low'}, inplace=True)
                if 'tvol' in df.columns: df['volume'] = df['tvol']
                elif 'tovol' in df.columns: df['volume'] = df['tovol']
                elif 'vol' in df.columns: df['volume'] = df['vol']
                else: df['volume'] = 0
                df = df[df['date'] >= start_date_origin]
                numeric_cols = ['close', 'open', 'high', 'low', 'volume']
                for c in numeric_cols: df[c] = df[c].astype(float)
                return df.sort_values('date', ascending=True).reset_index(drop=True).tail(250)
        return None

def get_domestic_index_chart(code):
    """업종/지수 기간별 시세(일봉) 조회 (KIS API)"""
    # 지수/업종 차트 조회 URL 및 TR_ID (실전/모의 동일)
    url_path = "uapi/domestic-stock/v1/quotations/inquire-daily-indexchartprice"
    tr_id = "FHKST03010200"
    
    now = datetime.now()
    today = now.strftime("%Y%m%d")
    start_date = (now - timedelta(days=730)).strftime("%Y%m%d") # 2년치 조회
    
    params = {
        "FID_COND_MRKT_DIV_CODE": "U", # U: 업종(Index)
        "FID_INPUT_ISCD": code,        # 0001(KOSPI), 1001(KOSDAQ)
        "FID_INPUT_DATE_1": start_date,
        "FID_INPUT_DATE_2": today,
        "FID_PERIOD_DIV_CODE": "D"     # D: 일봉
    }
    
    data = call_api(url_path, "domestic", "quotations", "index_chart", params=params, tr_id=tr_id)
    
    if data.get('rt_cd') == '0':
        items = data.get('output2', [])
        if items:
            df = pd.DataFrame(items)
            # 컬럼 매핑 (KIS API 응답 -> 내부 표준)
            # stck_bsop_date:일자, bstp_nmix_prpr:현재가(종가), bstp_nmix_hgpr:고가, bstp_nmix_lwpr:저가, acml_vol:거래량
            df = df[['stck_bsop_date', 'bstp_nmix_prpr', 'bstp_nmix_hgpr', 'bstp_nmix_lwpr', 'acml_vol']].copy()
            df.columns = ['date', 'close', 'high', 'low', 'volume']
            df = df.astype({'close': float, 'high': float, 'low': float, 'volume': float})
            return df.sort_values('date', ascending=True).reset_index(drop=True)
            
    return pd.DataFrame()

def get_current_price_data(code, is_overseas):
    if not is_overseas:
        return call_api("uapi/domestic-stock/v1/quotations/inquire-price", "domestic", "quotations", "price", params={"fid_cond_mrkt_div_code": "J", "fid_input_iscd": code}, timeout=3)
    
    if is_overseas:
        cached_ex = config.session.exchange_cache.get(code)
        exchanges = []
        if cached_ex: exchanges.append(cached_ex)
        for e in ["NAS", "NYS", "AMS", "NASD", "NYSE", "AMEX"]:
            if e not in exchanges: exchanges.append(e)
        
        for excd in exchanges:
            params = {"AUTH": "", "EXCD": excd, "SYMB": code}
            data = call_api("uapi/overseas-price/v1/quotations/price", "overseas", "quotations", "price", params=params, timeout=3)
            if data.get('rt_cd') == '0':
                if float(data.get('output', {}).get('last', 0) or 0) > 0:
                    if cached_ex != excd: config.session.update_cache_and_save(code, excd)
                    return data
        return {'rt_cd': '9999'}
    return {'rt_cd': '9999'}

def get_current_price(code, is_overseas):
    """현재가 단일 값 조회 (실패 시 0 반환)"""
    data = get_current_price_data(code, is_overseas)
    if data.get('rt_cd') == '0':
        output = data.get('output', {})
        if is_overseas:
            try: return float(output.get('last', 0))
            except: return 0.0
        else:
            return safe_int(output.get('stck_prpr'))
    return 0

def get_investor_trend(code):
    data = call_api("uapi/domestic-stock/v1/quotations/inquire-investor", "domestic", "quotations", "investor", params={"FID_COND_MRKT_DIV_CODE": "J", "FID_INPUT_ISCD": code})
    if data.get('rt_cd') == '0': return data.get('output', [])
    return []

def get_realtime_vol_strength(code, is_overseas=False, exchange_code=None):
    if is_overseas: return None
    
    for _ in range(3):
        data = call_api("uapi/domestic-stock/v1/quotations/inquire-ccnl", "domestic", "quotations", "vol_strength", params={"FID_COND_MRKT_DIV_CODE": "J", "FID_INPUT_ISCD": code}, timeout=2)
        if data.get('rt_cd') == '0':
            items = data.get('output', [])
            if items and items[0].get('tday_rltv'): return float(str(items[0].get('tday_rltv')).replace(',', ''))
        elif data.get('msg_cd') == 'EGW00201': time.sleep(0.2)
        else: time.sleep(0.2)
    return None

def fetch_overseas_detail_price(code, excd):
    exchanges = []
    if excd: exchanges.append(excd)
    for e in ["NASD", "NAS", "NYSE", "NYS", "AMEX", "AMS"]:
        if e not in exchanges: exchanges.append(e)

    for target_excd in exchanges:
        params = {"AUTH": "", "EXCD": target_excd, "SYMB": code}
        data = call_api("uapi/overseas-price/v1/quotations/price-detail", "overseas", "quotations", "detail", params=params, timeout=3)
        if data.get('rt_cd') == '0':
            output = data.get('output', {})
            if output.get('h52p') and float(output.get('h52p')) > 0:
                if target_excd != excd: config.session.update_cache_and_save(code, target_excd)
                return output
    return {}

def fetch_domestic_period_price(code):
    today = datetime.now().strftime("%Y%m%d")
    past = (datetime.now() - timedelta(days=100)).strftime("%Y%m%d")
    
    params = {"FID_COND_MRKT_DIV_CODE": "J", "FID_INPUT_ISCD": code, "FID_INPUT_DATE_1": past, "FID_INPUT_DATE_2": today, "FID_PERIOD_DIV_CODE": "D", "FID_ORG_ADJ_PRC": "1"}
    data = call_api("uapi/domestic-stock/v1/quotations/inquire-daily-itemchartprice", "domestic", "quotations", "chart", params=params)
    if data.get('rt_cd') == '0': return data.get('output2', [])
    return []

def fetch_overseas_period_price(code, excd):
    today = datetime.now().strftime("%Y%m%d")
    
    target_exchanges = []
    if excd: target_exchanges.append(excd)
    for e in ["NASD", "NAS", "NYSE", "NYS", "AMEX", "AMS"]:
        if e not in target_exchanges: target_exchanges.append(e)
    
    for target_excd in target_exchanges:
        params = {"AUTH": "", "EXCD": target_excd, "SYMB": code, "GUBN": "0", "BYMD": today, "MODP": "1", "KEYB": code}
        data = call_api("uapi/overseas-price/v1/quotations/dailyprice", "overseas", "quotations", "chart", params=params, timeout=5)
        if data.get('rt_cd') == '0':
            items = data.get('output2')
            if items:
                if target_excd != excd: config.session.update_cache_and_save(code, target_excd)
                df = pd.DataFrame(items).drop_duplicates(subset=['xymd'])
                df.rename(columns={'xymd': 'date', 'clos': 'close', 'tovol': 'volume', 'high': 'high', 'low': 'low'}, inplace=True)
                if 'volume' not in df.columns:
                    if 'tvol' in df.columns: df['volume'] = df['tvol']
                    else: df['volume'] = 0
                df = df.astype({'close': float, 'high': float, 'low': float, 'volume': float})
                return df.sort_values('date', ascending=True).reset_index(drop=True).tail(250)
    return None

def fetch_buyable_quantity(stock_code, price):
    # [수정] 컨텍스트에 따른 계좌번호 선택
    cano = config.session.cano
    acnt_prdt_cd = config.session.acnt_prdt_cd
    if not config.session.is_simulation and getattr(config.trade_context, 'use_auto_account', False) and config.session.auto_cano:
        cano = config.session.auto_cano
        acnt_prdt_cd = config.session.auto_acnt_prdt_cd

    params = {"CANO": cano, "ACNT_PRDT_CD": acnt_prdt_cd, "PDNO": stock_code, "ORD_UNPR": str(price), "ORD_DVSN": "00" if price > 0 else "01", "CMA_EVLU_AMT_ICLD_YN": "N", "OVRS_ICLD_YN": "N", "CRDT_TYPE": "00"}
    data = call_api("uapi/domestic-stock/v1/trading/inquire-psbl-order", "domestic", "inquiry", "buyable", params=params, timeout=5)
    if data.get('rt_cd') == '0':
        out = data.get('output', {})
        api_qty = safe_int(out.get('ord_psbl_qty')) or safe_int(out.get('nrcvb_buy_qty')) or safe_int(out.get('max_buy_qty'))
        if price > 0:
            cash = safe_int(out.get('ord_psbl_cash'))
            return min(api_qty, int(cash / price))
        return api_qty
    return 0

def fetch_sellable_quantity(stock_code):
    # [수정] 컨텍스트에 따른 계좌번호 선택
    cano = config.session.cano
    acnt_prdt_cd = config.session.acnt_prdt_cd
    if not config.session.is_simulation and getattr(config.trade_context, 'use_auto_account', False) and config.session.auto_cano:
        cano = config.session.auto_cano
        acnt_prdt_cd = config.session.auto_acnt_prdt_cd

    params = {"CANO": cano, "ACNT_PRDT_CD": acnt_prdt_cd, "AFHR_FLPR_YN": "N", "OFL_YN": "N", "INQR_DVSN": "02", "UNPR_DVSN": "01", "FUND_STTL_ICLD_YN": "N", "FNCG_AMT_AUTO_RDPT_YN": "N", "PRCS_DVSN": "00", "CTX_AREA_FK100": "", "CTX_AREA_NK100": ""}
    data = call_api("uapi/domestic-stock/v1/trading/inquire-psbl-sell", "domestic", "inquiry", "sellable", params=params)
    if data.get('rt_cd') == '0':
        for item in data.get('output1', []):
            if item.get('pdno') == stock_code: return safe_int(item.get('ord_psbl_qty'))
    return 0

def fetch_overseas_buyable_quantity(stock_code, price, excd):
    trade_excd = excd
    if excd == "NAS": trade_excd = "NASD"
    elif excd == "NYS": trade_excd = "NYSE"
    elif excd == "AMS": trade_excd = "AMEX"
    
    # [수정] 컨텍스트에 따른 계좌번호 선택
    cano = config.session.cano
    acnt_prdt_cd = config.session.acnt_prdt_cd
    if not config.session.is_simulation and getattr(config.trade_context, 'use_auto_account', False) and config.session.auto_cano:
        cano = config.session.auto_cano
        acnt_prdt_cd = config.session.auto_acnt_prdt_cd
        
    params = {"CANO": cano, "ACNT_PRDT_CD": acnt_prdt_cd, "OVRS_EXCG_CD": trade_excd, "OVRS_ORD_UNPR": str(price), "ITEM_CD": stock_code}
    data = call_api("uapi/overseas-stock/v1/trading/inquire-psamount", "overseas", "inquiry", "buyable", params=params)
    if data.get('rt_cd') == '0':
        out = data.get('output', {})
        return safe_int(out.get('ovrs_ord_psbl_qty')) or safe_int(out.get('ord_psbl_qty'))
    return 0

def fetch_overseas_sellable_quantity(stock_code, excd):
    trade_excds = []
    primary_excd = excd
    if excd == "NAS": primary_excd = "NASD"
    elif excd == "NYS": primary_excd = "NYSE"
    elif excd == "AMS": primary_excd = "AMEX"
    if config.session.is_simulation:
        trade_excds.append(primary_excd)
        for e in ["NASD", "NYSE", "AMEX"]:
            if e != primary_excd: trade_excds.append(e)
    else: trade_excds = ["NASD"]
    
    # [수정] 컨텍스트에 따른 계좌번호 선택
    cano = config.session.cano
    acnt_prdt_cd = config.session.acnt_prdt_cd
    if not config.session.is_simulation and getattr(config.trade_context, 'use_auto_account', False) and config.session.auto_cano:
        cano = config.session.auto_cano
        acnt_prdt_cd = config.session.auto_acnt_prdt_cd

    for target_excd in trade_excds:
        params = {"CANO": cano, "ACNT_PRDT_CD": acnt_prdt_cd, "OVRS_EXCG_CD": target_excd, "TR_CRCY_CD": "USD", "CTX_AREA_FK100": "", "CTX_AREA_NK100": "", "CTX_AREA_FK200": "", "CTX_AREA_NK200": ""}
        data = call_api("uapi/overseas-stock/v1/trading/inquire-balance", "overseas", "inquiry", "sellable", params=params)
        if data.get('rt_cd') == '0':
            for item in data.get('output1', []):
                if item.get('ovrs_pdno') == stock_code:
                    qty = safe_int(item.get('ord_psbl_qty'))
                    if qty > 0: return qty
    return 0

def find_best_exchange_code(stock_code):
    token_to_use = get_current_token()
    cached = config.session.exchange_cache.get(stock_code)
    if cached: return cached

    for excd in ["NAS", "NYS", "AMS"]:
        params = {"AUTH": "", "EXCD": excd, "SYMB": stock_code}
        data = call_api("uapi/overseas-price/v1/quotations/price", "overseas", "quotations", "price", params=params)
        if data.get('rt_cd') == '0' and float(str(data.get('output', {}).get('last', '0')).strip() or 0) > 0:
            config.session.update_cache_and_save(stock_code, excd)
            return excd
    return None

def _prepare_account_params(cano, acnt_prdt_cd):
    """계좌 파라미터 준비 및 컨텍스트 설정 (내부 헬퍼)"""
    # 인자가 없으면 현재 설정/컨텍스트 값 사용
    if not cano:
        if not config.session.is_simulation and getattr(config.trade_context, 'use_auto_account', False) and config.session.auto_cano:
            cano = config.session.auto_cano
            acnt_prdt_cd = config.session.auto_acnt_prdt_cd
        else:
            cano = config.session.cano
            acnt_prdt_cd = config.session.acnt_prdt_cd
    
    # 요청 계좌가 자동매매 계좌와 일치하면 컨텍스트 전환 (토큰/Key 변경)
    if not config.session.is_simulation and cano == config.session.auto_cano and config.session.auto_app_key:
        config.trade_context.use_auto_account = True
    elif not config.session.is_simulation and cano == config.session.cano:
        config.trade_context.use_auto_account = False
        
    return cano, acnt_prdt_cd

def get_domestic_balance(cano=None, acnt_prdt_cd=None):
    """국내 주식 잔고 조회"""
    cano, acnt_prdt_cd = _prepare_account_params(cano, acnt_prdt_cd)
    params = {"CANO": cano, "ACNT_PRDT_CD": acnt_prdt_cd, "AFHR_FLPR_YN": "N", "OFL_YN": "N", "INQR_DVSN": "02", "UNPR_DVSN": "01", "FUND_STTL_ICLD_YN": "N", "FNCG_AMT_AUTO_RDPT_YN": "N", "PRCS_DVSN": "00", "CTX_AREA_FK100": "", "CTX_AREA_NK100": ""}
    data = call_api("uapi/domestic-stock/v1/trading/inquire-balance", "domestic", "inquiry", "balance", params=params)
    
    # [추가] OPSQ2001 에러(INQR_DVSN 관련) 발생 시 '01'(대출일별)로 재시도
    if data.get('msg_cd') == 'OPSQ2001':
        logger.warning("[API] 잔고 조회 '02' 실패(OPSQ2001). '01'로 재시도합니다.")
        params["INQR_DVSN"] = "01"
        data = call_api("uapi/domestic-stock/v1/trading/inquire-balance", "domestic", "inquiry", "balance", params=params)

    if data.get('rt_cd') == '0':
        output1 = data.get('output1', [])
        output2 = data.get('output2', [])
        
        # [디버깅] 잔고 조회 결과 로그 출력
        count = len(output1)
        summary_eval = 0
        if output2:
            summary_tmp = output2[0] if isinstance(output2, list) and output2 else (output2 if isinstance(output2, dict) else {})
            summary_eval = safe_int(summary_tmp.get('scts_evlu_amt'))
            
        logger.info(f"[API] 잔고 조회 결과: 종목수={count}, 총평가금={summary_eval:,}원 (RT_CD={data.get('rt_cd')})")
        
        # [보정] 리스트는 비어있는데 총 평가금액이 있는 경우 (데이터 불일치), 조회 구분 변경 시도
        if not output1 and output2:
            summary = output2[0] if isinstance(output2, list) and output2 else (output2 if isinstance(output2, dict) else {})
            total_eval = safe_int(summary.get('scts_evlu_amt'))
            
            if total_eval > 0 and params["INQR_DVSN"] == "02":
                logger.info(f"[API] 잔고 불일치 감지(평가금:{total_eval}, 종목수:0). INQR_DVSN='01'로 재조회 시도.")
                params["INQR_DVSN"] = "01"
                retry_data = call_api("uapi/domestic-stock/v1/trading/inquire-balance", "domestic", "inquiry", "balance", params=params)
                if retry_data.get('rt_cd') == '0':
                    return retry_data.get('output1', []), retry_data.get('output2', [])
        
        return output1, output2
    
    # [추가] 실패 시 로그 출력
    msg = f"잔고 조회 실패: {data.get('msg1')} ({data.get('msg_cd')})"
    logger.debug(f"{msg}")
    if config.SYSTEM_LOGGER:
        config.SYSTEM_LOGGER(f"[API] {msg}")
        
    return [], []

def get_overseas_balance(cano=None, acnt_prdt_cd=None):
    """해외 주식 잔고 조회"""
    cano, acnt_prdt_cd = _prepare_account_params(cano, acnt_prdt_cd)
    target_exchanges = ["NASD", "NYSE", "AMEX"]
    all_holdings = []
    
    for exc in target_exchanges:
        if config.session.is_simulation: time.sleep(0.2)
        params = {"CANO": cano, "ACNT_PRDT_CD": acnt_prdt_cd, "OVRS_EXCG_CD": exc, "TR_CRCY_CD": "USD", "CTX_AREA_FK100": "", "CTX_AREA_NK100": "", "CTX_AREA_FK200": "", "CTX_AREA_NK200": ""}
        data = call_api("uapi/overseas-stock/v1/trading/inquire-balance", "overseas", "inquiry", "balance", params=params)
        
        # Rate Limit 발생 시 잠시 대기 후 재시도 (call_api 내부 재시도와 별개로 루프 내 처리)
        if data.get('msg_cd') == 'EGW00201':
            time.sleep(0.5)
            data = call_api("uapi/overseas-stock/v1/trading/inquire-balance", "overseas", "inquiry", "balance", params=params)

        if data.get('rt_cd') == '0':
            for item in data.get('output1', []):
                if '_exchange' not in item: item['_exchange'] = exc
                all_holdings.append(item)
                
    return all_holdings

def get_today_profit_summary(cano=None, acnt_prdt_cd=None):
    """금일 투자 손익 요약 조회"""
    cano, acnt_prdt_cd = _prepare_account_params(cano, acnt_prdt_cd)
    today = datetime.now().strftime("%Y%m%d")
    params = {
        "CANO": cano, "ACNT_PRDT_CD": acnt_prdt_cd,
        "INQR_STRT_DT": today, "INQR_END_DT": today,
        "SLL_BUY_DVSN_CD": "00", "INQR_DVSN": "00", 
        "PDNO": "", "CTX_AREA_FK100": "", "CTX_AREA_NK100": "",
        "AFHR_FLPR_YN": "N", "OFL_YN": "N", "UNPR_DVSN": "01",          
        "FUND_STTL_ICLD_YN": "N", "FNCG_AMT_AUTO_RDPT_YN": "N", "PRCS_DVSN": "00",
        "COST_ICLD_YN": "Y" 
    }
    return call_api("uapi/domestic-stock/v1/trading/inquire-period-profit", "domestic", "inquiry", "profit", params=params)

def get_today_history(cano=None, acnt_prdt_cd=None):
    """금일 체결 내역 조회"""
    cano, acnt_prdt_cd = _prepare_account_params(cano, acnt_prdt_cd)
    today = datetime.now().strftime("%Y%m%d")
    params = {"CANO": cano, "ACNT_PRDT_CD": acnt_prdt_cd, "INQR_STRT_DT": today, "INQR_END_DT": today, "SLL_BUY_DVSN_CD": "00", "INQR_DVSN": "00", "PDNO": "", "CCLD_DVSN": "01", "ORD_GNO_BRNO": "", "ODNO": "", "INQR_DVSN_3": "00", "INQR_DVSN_1": "", "CTX_AREA_FK100": "", "CTX_AREA_NK100": ""}
    return call_api("uapi/domestic-stock/v1/trading/inquire-daily-ccld", "domestic", "inquiry", "history", params=params)

def get_unfilled_orders(cano=None, acnt_prdt_cd=None):
    """미체결 내역 조회 (국내주식) - get_domestic_open_orders의 Alias"""
    return get_domestic_open_orders(cano, acnt_prdt_cd)

def get_domestic_open_orders(cano=None, acnt_prdt_cd=None):
    """국내주식 미체결 내역 조회 (모의/실전 분기 처리)"""
    cano, acnt_prdt_cd = _prepare_account_params(cano, acnt_prdt_cd)
    
    if config.session.is_simulation:
        # 모의투자: 주식일별체결조회(CCLD_DVSN=02:미체결)
        today = datetime.now().strftime("%Y%m%d")
        params = {
            "CANO": cano, "ACNT_PRDT_CD": acnt_prdt_cd, 
            "INQR_STRT_DT": today, "INQR_END_DT": today,
            "SLL_BUY_DVSN_CD": "00", "INQR_DVSN": "00",
            "PDNO": "", "CCLD_DVSN": "02",
            "ORD_GNO_BRNO": "", "ODNO": "", "INQR_DVSN_3": "00", 
            "INQR_DVSN_1": "", "CTX_AREA_FK100": "", "CTX_AREA_NK100": ""
        }
        res = call_api("uapi/domestic-stock/v1/trading/inquire-daily-ccld", "domestic", "inquiry", "history", params=params)
        if res.get('rt_cd') == '0':
            return res.get('output1', [])
    else:
        # 실전투자: 주식정정취소가능주문조회
        params = {
            "CANO": cano, "ACNT_PRDT_CD": acnt_prdt_cd, 
            "CTX_AREA_FK100": "", "CTX_AREA_NK100": "", 
            "INQR_DVSN_1": "0", "INQR_DVSN_2": "0"
        }
        res = call_api("uapi/domestic-stock/v1/trading/inquire-psbl-rvsecncl", "domestic", "inquiry", "unfilled", params=params)
        if res.get('rt_cd') == '0':
            return res.get('output', [])
    return []

def get_overseas_open_orders(cano=None, acnt_prdt_cd=None):
    """해외주식 미체결 내역 조회"""
    cano, acnt_prdt_cd = _prepare_account_params(cano, acnt_prdt_cd)
    all_orders = []
    target_exchanges = ["NASD", "NYSE", "AMEX"] if config.session.is_simulation else ["NASD"]
    
    for exc in target_exchanges:
        params = {
            "CANO": cano, "ACNT_PRDT_CD": acnt_prdt_cd, 
            "OVRS_EXCG_CD": exc, "SORT_SQN": "DS", 
            "CTX_AREA_FK100": "", "CTX_AREA_NK100": "", "CTX_AREA_FK200": "", "CTX_AREA_NK200": ""
        }
        res = call_api("uapi/overseas-stock/v1/trading/inquire-nccs", "overseas", "inquiry", "open_orders", params=params)
        if res.get('rt_cd') == '0':
            orders = res.get('output', [])
            if orders:
                for o in orders:
                    if not o.get('ovrs_excg_cd'): o['ovrs_excg_cd'] = exc
                all_orders.extend(orders)
    return all_orders

def place_order(market, action, code, qty, price, ord_dvsn, exchange_code=None):
    """
    주문 전송 통합 함수
    market: "domestic" or "overseas"
    action: "buy" or "sell"
    """
    cano, acnt = _prepare_account_params(None, None)
    
    if market == "domestic":
        url_path = "uapi/domestic-stock/v1/trading/order-cash"
        data = {
            "CANO": cano, "ACNT_PRDT_CD": acnt, 
            "PDNO": code, "ORD_DVSN": ord_dvsn, 
            "ORD_QTY": str(qty), "ORD_UNPR": str(price)
        }
    else: # overseas
        url_path = "uapi/overseas-stock/v1/trading/order"
        data = {
            "CANO": cano, "ACNT_PRDT_CD": acnt, 
            "OVRS_EXCG_CD": exchange_code, "PDNO": code, 
            "ORD_QTY": str(qty), "OVRS_ORD_UNPR": str(price), 
            "ORD_SVR_DVSN_CD": "0", "ORD_DVSN": ord_dvsn
        }

    return call_api(url_path, market, "trade", action, data=data, method="POST")

def revise_cancel_order(market, action, org_no, code, qty, price, type_cd, ord_dvsn, exchange_code=None):
    """
    정정/취소 통합 함수
    action: "modify" (정정) or "cancel" (취소)
    type_cd: "01"(정정), "02"(취소) - API 스펙상 구분 코드
    """
    cano, acnt = _prepare_account_params(None, None)
    
    if market == "domestic":
        url_path = "uapi/domestic-stock/v1/trading/order-rvsecncl"
        qty_all_yn = "Y" if qty == 0 else "N" # 0이면 전량으로 간주 (호출부 로직에 따름)
        data = {"CANO": cano, "ACNT_PRDT_CD": acnt, "KRX_FWDG_ORD_ORGNO": "", "ORGN_ODNO": org_no, "ORD_DVSN": ord_dvsn, "RVSE_CNCL_DVSN_CD": type_cd, "ORD_QTY": str(qty), "ORD_UNPR": str(price), "QTY_ALL_ORD_YN": qty_all_yn}
    else: # overseas
        url_path = "uapi/overseas-stock/v1/trading/order-rvsecncl"
        data = {"CANO": cano, "ACNT_PRDT_CD": acnt, "OVRS_EXCG_CD": exchange_code, "PDNO": code, "ORGN_ODNO": org_no, "RVSE_CNCL_DVSN_CD": type_cd, "ORD_QTY": str(qty), "OVRS_ORD_UNPR": str(price)}
    
    # action 파라미터는 TR_ID 조회를 위해 사용됨 (modify/cancel)
    return call_api(url_path, market, "modify", action, data=data, method="POST")

def get_deposit(cano=None, acnt_prdt_cd=None):
    """예수금(주문가능현금) 조회 (국내/모의)"""
    cano, acnt_prdt_cd = _prepare_account_params(cano, acnt_prdt_cd)
    params = {
        "CANO": cano, "ACNT_PRDT_CD": acnt_prdt_cd, 
        "PDNO": "005930", "ORD_UNPR": "0", "ORD_DVSN": "01", 
        "CMA_EVLU_AMT_ICLD_YN": "Y", "OVRS_ICLD_YN": "Y", "CRDT_TYPE": "00"
    }
    return call_api("uapi/domestic-stock/v1/trading/inquire-psbl-order", "domestic", "inquiry", "deposit", params=params)

def get_foreign_deposit(cano=None, acnt_prdt_cd=None):
    """외화 예수금 등 실전투자 계좌 잔고 상세 조회"""
    cano, acnt_prdt_cd = _prepare_account_params(cano, acnt_prdt_cd)
    params = {
        "CANO": cano, "ACNT_PRDT_CD": acnt_prdt_cd, 
        "TR_CONT": "", "INQR_DVSN_1": "", "TR_CRCY_CD": "", "PDNO": "", 
        "ORD_UNPR": "", "ORD_QTY": "", "ORD_DVSN": "00", 
        "CMA_EVLU_AMT_ICLD_YN": "Y", "OVRS_ICLD_YN": "Y", 
        "CTX_AREA_FK100": "", "CTX_AREA_NK100": "", 
        "BSPR_BF_DT_APLY_YN": "N"
    }
    return call_api("uapi/domestic-stock/v1/trading/inquire-account-balance", "domestic", "inquiry", "deposit", params=params)

def get_deposit_balance(cano=None, acnt_prdt_cd=None):
    """예수금 및 자산 현황 조회 (모의/실전 자동 분기)"""
    cano, acnt_prdt_cd = _prepare_account_params(cano, acnt_prdt_cd)
    res = {"deposit": 0, "foreign_deposit": 0, "withdraw": 0, "d2_deposit": 0}
    success = False # [추가] 조회 성공 여부 플래그

    if config.session.is_simulation:
        # [수정] 모의투자: 주식잔고조회(VTTC8434R)를 우선 사용하여 예수금 확인 (더 안정적)
        holdings, summary_list = get_domestic_balance(cano, acnt_prdt_cd)
        
        if summary_list and len(summary_list) > 0:
            summary = summary_list[0]
            res['deposit'] = int(float(summary.get('dnca_tot_amt', 0)))
            res['d2_deposit'] = int(float(summary.get('prvs_rcdl_excc_amt', 0)))
            res['withdraw'] = res['d2_deposit']
            success = True
        
        # 잔고조회에서 예수금을 못 가져왔거나 0인 경우, 기존 방식(주문가능금액) 시도
        if res['deposit'] == 0:
            data = get_deposit(cano, acnt_prdt_cd)
            if data.get('rt_cd') == '0':
                output = data.get('output', {})
                cash = safe_int(output.get('ord_psbl_cash'))
                res['deposit'] = cash
                res['withdraw'] = cash
                res['d2_deposit'] = cash
                success = True
            else:
                # [수정] 서버 장애(OPSQ2000)로 인한 조회 실패 시 로그 레벨 완화 및 D+2 예수금 활용
                logger.warning(f"모의투자 예수금 조회 실패: {data.get('msg1')} ({data.get('msg_cd')})")
                # 잔고 조회(get_domestic_balance)에서 가져온 d2_deposit이 있다면 이를 deposit으로 대체 사용
                if res['d2_deposit'] > 0:
                    res['deposit'] = res['d2_deposit']
                    success = True
    else:
        data = get_foreign_deposit(cano, acnt_prdt_cd)
        if data.get('rt_cd') == '0' and data.get('output2'):
            out2 = data['output2'][0] if isinstance(data['output2'], list) else data['output2']
            res['foreign_deposit'] = int(float(out2.get('frcr_evlu_tota', 0)))
            res['deposit'] = int(float(out2.get('dnca_tot_amt', 0)))
            res['d2_deposit'] = int(float(out2.get('prvs_rcdl_excc_amt', 0)))
            res['withdraw'] = res['d2_deposit']
            success = True
        else:
            logger.error(f"실전투자 예수금 조회 실패: {data.get('msg1')} ({data.get('msg_cd')})")
            
    return res if success else None # [수정] 실패 시 None 반환

def check_server_health():
    """서버 상태 점검 (삼성전자 현재가 조회)"""
    try:
        # 타임아웃 5초, 재시도 0회로 설정하여 빠르게 확인
        res = call_api("uapi/domestic-stock/v1/quotations/inquire-price", "domestic", "quotations", "price", 
                       params={"fid_cond_mrkt_div_code": "J", "fid_input_iscd": "005930"}, 
                       timeout=5, retries=0)
        if res and res.get('rt_cd') == '0':
            return True
    except:
        pass
    return False

def send_telegram_photo(file_path, caption=None):
    """텔레그램 사진 전송"""
    if not config.TELEGRAM_BOT_TOKEN or not config.TELEGRAM_CHAT_ID:
        return False

    if not os.path.exists(file_path):
        logger.error(f"[Telegram] 전송할 파일이 없습니다: {file_path}")
        return False

    account_info = _get_telegram_footer()
    final_caption = f"{caption}\n\n{account_info}" if caption else account_info

    url = f"https://api.telegram.org/bot{config.TELEGRAM_BOT_TOKEN}/sendPhoto"
    chat_id = config.TELEGRAM_CHAT_ID
    
    logger.info(f"[Telegram] 사진 전송 시작: {os.path.basename(file_path)}")

    max_retries = 3
    for attempt in range(max_retries):
        try:
            with open(file_path, 'rb') as f:
                data = {"chat_id": chat_id, "caption": final_caption}
                files = {"photo": (os.path.basename(file_path), f, 'image/png')}
                
                # 이미지 전송은 시간이 더 걸릴 수 있으므로 타임아웃을 넉넉하게 설정
                res = requests.post(url, data=data, files=files, timeout=30)
            
            if res.status_code == 200:
                logger.info("[Telegram] 사진 전송 성공")
                return True
            else:
                logger.error(f"[Telegram] 사진 전송 실패({attempt+1}/{max_retries}) Status: {res.status_code}, Msg: {res.text}")
                
        except Exception as e:
            if config.SCREEN_DEBUG_LEVEL in ["TRACE", "DEBUG"]:
                config.console.print(f"[dim red][TRACE] ERR (TELEGRAM PHOTO) {str(e)}[/dim red]")
            logger.error(f"[Telegram] 사진 전송 중 오류({attempt+1}/{max_retries}): {str(e)}")
        
        if attempt < max_retries - 1:
            time.sleep(1)
            
    return False
