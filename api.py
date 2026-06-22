# api.py
import requests
import logging
import json
import time
import random
import sys
import ssl
import urllib3
import re
import os
import threading
import concurrent.futures
import yfinance as yf
import pandas as pd
from datetime import datetime, timedelta
from requests.adapters import HTTPAdapter
from urllib3.poolmanager import PoolManager
from urllib3.util.retry import Retry
from collections import deque
import config
import context # [추가] 상태 관리 모듈
import constants
from modules.executors import tg_sender_executor

logger = logging.getLogger(__name__)

# [추가] 대체거래소(NXT) 관련 마스터 파일 캐시
_NXT_TRADEABLE_CACHE = set()
_NXT_MASTER_LOADED = False
_NXT_MASTER_LOCK = threading.RLock()

def load_nxt_master():
    """KIS API의 NXT 종목 마스터 파일을 다운로드하여 거래 가능 종목 코드를 추출합니다."""
    global _NXT_MASTER_LOADED
    with _NXT_MASTER_LOCK:
        if _NXT_MASTER_LOADED: return
        
        try:
            # NXT 마스터 파일 다운로드 및 파싱을 시도합니다.
            base_url = config.session.url_base if config.session.is_simulation else config.REAL_URL
            # KIS OpenAPI 대체거래소 종목정보 다운로드 API 경로
            url_path = "uapi/domestic-stock/v1/quotations/nxt-master" 
            
            token = get_current_token()
            key = config.session.app_key if config.session.is_simulation else config.session.real_app_key
            secret = config.session.app_secret if config.session.is_simulation else config.session.real_app_secret
            
            headers = {
                "authorization": f"Bearer {token}",
                "appKey": key,
                "appSecret": secret,
                "tr_id": "CTCA0703C", # 대체거래소 마스터 조회 TR_ID
                "custtype": "P"
            }
            
            res = requests.get(f"{base_url}/{url_path}", headers=headers, timeout=5)
            if res.status_code == 200:
                # 마스터 파일 파싱 (한 줄씩 파이프(|) 구분되어 있다고 가정)
                lines = res.text.splitlines()
                for line in lines:
                    parts = line.split('|')
                    if len(parts) > 0 and len(parts[0]) == 6 and parts[0][0].isdigit():
                        _NXT_TRADEABLE_CACHE.add(parts[0])
                logger.info(f"NXT 거래 가능 종목 마스터 파일 로드 완료 ({len(_NXT_TRADEABLE_CACHE)}종목)")
        except Exception as e:
            logger.debug(f"NXT 마스터 파일 로드 실패 (Fallback 동적 조회 사용): {e}")
        finally:
            _NXT_MASTER_LOADED = True

def is_nxt_tradeable(code):
    """NXT 거래 대상 종목 여부를 확인합니다."""
    if not _NXT_MASTER_LOADED:
        load_nxt_master()
    
    # 1. 마스터 파일이 정상 로드되어 캐시에 종목이 있는 경우
    if _NXT_TRADEABLE_CACHE:
        return code in _NXT_TRADEABLE_CACHE
        
    # 2. 마스터 로드에 실패했거나 미지원 상태일 경우, 종목 그룹으로 확인 (ETF는 무조건 불가)
    # 안전장치로 일단 일반 주식은 모두 통과시킵니다 (오류로 매매 못하는 것 방지)
    return True

# [추가] 휴장일 캐시
_HOLIDAY_CACHE = {}

def check_holiday(date_str):
    """한국투자증권 휴장일 조회 API 호출"""
    url_path = "uapi/domestic-stock/v1/quotations/chk-holiday"
    tr_id = "CTCA0903R"
    params = {"BASS_DT": date_str, "CTX_AREA_NK": "", "CTX_AREA_FK": ""}
    
    res = call_api(url_path, "domestic", "quotations", "chk_holiday", params=params, tr_id=tr_id, retries=1)
    
    if res and res.get('rt_cd') == '0':
        output = res.get('output', [])
        if output:
            for day_info in output:
                if day_info.get('bass_dt') == date_str:
                    opnd_yn = day_info.get('opnd_yn', 'Y')
                    bzdy_yn = day_info.get('bzdy_yn', 'Y')
                    if opnd_yn == 'N' or bzdy_yn == 'N': return True
                    return False
    return None

def is_holiday_today():
    """오늘이 주말 또는 공휴일(휴장일)인지 확인합니다."""
    today_str = datetime.now().strftime("%Y%m%d")
    if today_str in _HOLIDAY_CACHE: return _HOLIDAY_CACHE[today_str]
    
    if datetime.now().weekday() > 4:
        _HOLIDAY_CACHE[today_str] = True
        return True
        
    # 실전투자 모드일 경우에만 API 우선 조회 시도
    if not config.session.is_simulation:
        res = check_holiday(today_str)
        if res is not None:
            _HOLIDAY_CACHE[today_str] = res
            return res
            
    # 모의투자이거나 API 호출이 실패(장애 등)한 경우 holidays 라이브러리로 자체 판단
    is_holiday = get_holiday_name(today_str, country='KR') is not None
    _HOLIDAY_CACHE[today_str] = is_holiday
    
    return is_holiday

def check_us_holiday(date_str):
    """한국투자증권 해외주식(미국) 휴장일 조회 API 호출"""
    url_path = "uapi/overseas-stock/v1/quotations/chk-holiday"
    tr_id = "CTCA0904R"
    params = {"BASS_DT": date_str, "CTX_AREA_NK": "", "CTX_AREA_FK": "", "NATN_CD": "840"}
    
    res = call_api(url_path, "overseas", "quotations", "chk_holiday", params=params, tr_id=tr_id, retries=1)
    
    if res and res.get('rt_cd') == '0':
        output = res.get('output', [])
        if output:
            for day_info in output:
                if day_info.get('bass_dt') == date_str:
                    opnd_yn = day_info.get('opnd_yn', 'Y')
                    bzdy_yn = day_info.get('bzdy_yn', 'Y')
                    if opnd_yn == 'N' or bzdy_yn == 'N': return True
                    return False
    return None

def is_us_holiday_today():
    """오늘이 주말 또는 미국 공휴일(휴장일)인지 확인합니다."""
    today_str = datetime.now().strftime("%Y%m%d")
    cache_key = f"US_{today_str}"
    if cache_key in _HOLIDAY_CACHE: return _HOLIDAY_CACHE[cache_key]
    
    if datetime.now().weekday() > 4:
        _HOLIDAY_CACHE[cache_key] = True
        return True
            
    # 실전투자 모드일 경우에만 API 우선 조회 시도
    if not config.session.is_simulation:
        res = check_us_holiday(today_str)
        if res is not None:
            _HOLIDAY_CACHE[cache_key] = res
            return res
            
    # 모의투자이거나 API 호출이 실패(장애 등)한 경우 holidays 라이브러리로 자체 판단
    is_holiday = get_holiday_name(today_str, country='US') is not None
    _HOLIDAY_CACHE[cache_key] = is_holiday
    
    return is_holiday

def get_holiday_name(date_str, country='KR'):
    """holidays 라이브러리를 이용하여 공휴일 이름을 반환합니다."""
    try:
        import holidays
        dt = datetime.strptime(date_str, "%Y%m%d").date()
        
        if country == 'KR':
            h_cal = holidays.KR()
            h_cal[dt.replace(month=5, day=1)] = "근로자의 날" # 법정공휴일이 아닌 근로자의 날 강제 추가
            h_cal[dt.replace(month=12, day=31)] = "연말 폐장일" # 한국거래소(KRX) 연말 휴장일 강제 추가
            name = h_cal.get(dt)
            return name
        elif country == 'US':
            h_cal = holidays.US(observed=True)
            name = h_cal.get(dt)
            return name
    except Exception as e:
        logger.debug(f"get_holiday_name error: {e}")
        return None
    return None

def _is_screen_output_allowed():
    """화면 출력 허용 여부 확인 (텔레그램 봇 스레드 차단)"""
    return threading.current_thread().name != "TelegramBot"

def clear_yfinance_cache():
    """yfinance 캐시 파일(.sqlite)을 강제로 삭제하여 DB Lock 문제를 해결합니다."""
    if _is_screen_output_allowed() and config.SCREEN_DEBUG_LEVEL in ["TRACE", "DEBUG"]:
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
                        except Exception as e:
                            logger.debug(f"clear_yfinance_cache file remove error: {e}")
            except Exception as e:
                logger.debug(f"clear_yfinance_cache directory access error: {e}")
    
    if _is_screen_output_allowed() and config.SCREEN_DEBUG_LEVEL == "DEBUG" and deleted_count > 0:
        config.console.print(f"[dim cyan][DEBUG] 캐시 파일 {deleted_count}개 삭제 완료[/dim cyan]")

def fetch_yfinance_data(tickers, period=None, start=None, end=None, interval="1d", group_by='column'):
    """yfinance 데이터 조회 통합 함수 (DB Lock 발생 시 자동 캐시 정리 후 재시도)"""
    try:
        return yf.download(tickers, period=period, start=start, end=end, interval=interval, group_by=group_by, progress=False, threads=False)
    except Exception as e:
        err_msg = str(e).lower()
        if "database" in err_msg or "lock" in err_msg:
            if _is_screen_output_allowed() and config.SCREEN_DEBUG_LEVEL in ["TRACE", "DEBUG"]:
                config.console.print(f"[dim yellow]yfinance DB Lock 감지: {e}. 캐시 정리 후 재시도합니다.[/dim yellow]")
            clear_yfinance_cache()
            time.sleep(0.5) # 파일 잠금 해제 대기
            return fetch_yfinance_data(tickers, period, start, end, interval, group_by)
        raise e

class TLSAdapter(HTTPAdapter):
    def init_poolmanager(self, connections, maxsize, block=False):
        # urllib3 메이저 버전 확인을 통한 명시적 분기 (지연 평가로 인한 에러 방지)
        urllib3_version = int(urllib3.__version__.split('.')[0])
        
        if urllib3_version >= 2:
            # urllib3 v2.x
            self.poolmanager = PoolManager(num_pools=connections, maxsize=maxsize, block=block, ssl_minimum_version=ssl.TLSVersion.TLSv1_2)
        else:
            # urllib3 v1.x
            self.poolmanager = PoolManager(num_pools=connections, maxsize=maxsize, block=block, ssl_version=ssl.PROTOCOL_TLSv1_2)

def _get_telegram_footer():
    """텔레그램 메시지용 계좌 정보 꼬리말 생성"""
    if not config.TELEGRAM_BOT_TOKEN:
        return

    cano = config.session.cano
    acc_label = "모의" if config.session.is_simulation else "실전"

    # 시스템 트레이딩 컨텍스트(AUTO 계좌) 확인
    if not config.session.is_simulation and getattr(context.trade_context, 'use_auto_account', False) and config.session.auto_cano:
        cano = config.session.auto_cano
        acc_label = "자동"

    instance_name = config.TELEGRAM_INSTANCE_NAME
    return f"[{instance_name} | {acc_label} {cano}]"

def send_telegram_message(message, reply_markup=None, is_urgent=False, sync=False):
    """텔레그램 메시지 전송 (시스템 트레이딩 알림용)"""
    if not config.TELEGRAM_BOT_TOKEN or not config.TELEGRAM_CHAT_ID:
        return

    account_info = _get_telegram_footer()
    
    # [추가] 마크다운 링크 패턴([text](url))을 임시 토큰으로 변환 (Rich 태그 제거 및 이스케이프 영향 방지)
    link_map = {}
    def _stash_link(match):
        token = f"__LINK_{len(link_map)}__"
        link_map[token] = (match.group(1), match.group(2)) # text, url
        return token
    
    clean_message = re.sub(r'\[([^\]]+)\]\(([^)]+)\)', _stash_link, message)

    # [추가] rich 라이브러리 색상 태그 제거 (텔레그램 전송용)
    # 예: [red]텍스트[/] -> 텍스트. 소문자로 시작하는 태그만 제거하여 [시스템] 등은 유지
    clean_message = re.sub(r'\[/?[a-z]+(?:[\s=][^\]]*)?\]', '', clean_message)

    # [추가] HTML 이스케이프 처리 (HTML 파싱 모드 사용 시 필수)
    clean_message = clean_message.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")

    # [추가] AI가 무작위로 생성하는 마크다운 굵게(**) 기호 일괄 제거
    clean_message = clean_message.replace("**", "")

    # [추가] 마크다운 헤더(#) 및 수평선(---) 기호 일괄 제거
    clean_message = re.sub(r'^#{1,6}\s*', '', clean_message, flags=re.MULTILINE)
    clean_message = re.sub(r'^[-*_]{3,}\s*$', '', clean_message, flags=re.MULTILINE)

    # [추가] 마크다운 링크 복원 (HTML <a> 태그로 변환)
    for token, (text, url) in link_map.items():
        # 텍스트 부분도 이스케이프 처리
        safe_text = text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
        clean_message = clean_message.replace(token, f'<a href="{url}">{safe_text}</a>')

    # [수정] 종목 코드에 링크 자동 적용 (네이버 증권)
    # 패턴: 괄호 안의 6자리 숫자/영문(국내) 또는 영문 대문자(해외) -> 예: (005930), (0080G0), (AAPL)
    def add_stock_link(match):
        code = match.group(1)
        
        # [추가] 일반 영문 단어나 보조지표명 등이 해외 티커로 오인되어 링크되는 현상 방지
        exclude_words = {"ON", "OFF", "RSI", "MACD", "ATR", "SMA", "EMA", "CCI", "ADX", "SAR", "OBV", "ETF", "TS", "RUN", "STOP", "WAIT"}
        if code in exclude_words:
            return f"({code})"

        # 1. 국내 주식 (6자리)
        if len(code) == 6:
            # [수정] 국내 주식: 트레이딩뷰 심볼 오버뷰 페이지 (유료/앱 설치 팝업 우회)
            url = f"https://kr.tradingview.com/symbols/KRX-{code}/"
        # 2. 해외 주식
        else:
            # 거래소 정보 확인 (config.session.exchange_cache 활용)
            exchange = config.session.exchange_cache.get(code, "")
            tv_exchange = ""
            
            # 트레이딩뷰 해외주식 거래소 접미사 매핑
            if exchange in ["NAS", "NASD"]: tv_exchange = "NASDAQ"
            elif exchange in ["NYS", "NYSE"]: tv_exchange = "NYSE"
            elif exchange in ["AMS", "AMEX"]: tv_exchange = "AMEX"
            
            if tv_exchange:
                url = f"https://kr.tradingview.com/symbols/{tv_exchange}-{code}/"
            else:
                # 거래소 정보가 없으면 티커만으로 접근 (트레이딩뷰가 자동 라우팅)
                url = f"https://kr.tradingview.com/symbols/{code}/"
                
        return f'(<a href="{url}">{code}</a>)'
    
    clean_message = re.sub(r'\(([0-9A-Z]{6}|[A-Z]{1,5})\)', add_stock_link, clean_message)

    # [수정] 계좌 정보를 메시지 가장 마지막에 추가 (가독성을 위해 한 줄 공백 추가)
    final_msg = f"{clean_message.rstrip()}\n\n{account_info}"

    # [수정] 전송 메시지 로그 기록 (시스템 로그로 변경)
    log_content = final_msg.replace('\n', ' | ')
    logger.info(f"[Telegram] 메시지 발송: {log_content}")

    # [추가] 4000자 분할 로직 (긴 메시지 자동 분할 전송)
    MAX_LEN = 4000
    msg_chunks = []
    if len(final_msg) <= MAX_LEN:
        msg_chunks.append(final_msg)
    else:
        lines = final_msg.split('\n')
        current_chunk = ""
        for line in lines:
            if len(current_chunk) + len(line) + 1 > MAX_LEN:
                if current_chunk:
                    msg_chunks.append(current_chunk.strip())
                    current_chunk = line + "\n"
                else:
                    msg_chunks.append(line[:MAX_LEN])
                    current_chunk = line[MAX_LEN:] + "\n"
            else:
                current_chunk += line + "\n"
        if current_chunk:
            msg_chunks.append(current_chunk.strip())

    def _send_task():
        url = f"https://api.telegram.org/bot{config.TELEGRAM_BOT_TOKEN}/sendMessage"
        
        # [추가] 화면 디버그 로그 (요청)
        if _is_screen_output_allowed() and config.SCREEN_DEBUG_LEVEL in ["TRACE", "DEBUG"]:
            config.console.print(f"[dim cyan][TRACE] REQ (TELEGRAM) | POST {url}[/dim cyan]")
            if config.SCREEN_DEBUG_LEVEL == "DEBUG":
                config.console.print(f"[dim cyan]  > Message: {message.replace(chr(10), ' ')}[/dim cyan]")

        # [수정] 재시도 로직 추가 (최대 3회)
        max_retries = 3
        for i, chunk in enumerate(msg_chunks):
            data = {"chat_id": config.TELEGRAM_CHAT_ID, "text": chunk, "parse_mode": "HTML", "disable_web_page_preview": True}
            
            if reply_markup and i == len(msg_chunks) - 1:
                data["reply_markup"] = json.dumps(reply_markup)

            success_chunk = False
            for attempt in range(max_retries):
                try:
                    current_timeout = 1 + (attempt * 0.5)
                    res = requests.post(url, data=data, timeout=current_timeout)
                    
                    if _is_screen_output_allowed() and config.SCREEN_DEBUG_LEVEL in ["TRACE", "DEBUG"]:
                        config.console.print(f"[dim magenta][TRACE] RES (TELEGRAM) Status:{res.status_code} Chunk {i+1}/{len(msg_chunks)} ({attempt+1}/{max_retries})[/dim magenta]")
                        if config.SCREEN_DEBUG_LEVEL == "DEBUG" and res.status_code != 200:
                             config.console.print(f"[dim red]  > Error: {res.text}[/dim red]")
                    
                    if res.status_code == 200:
                        logger.info(f"[Telegram] 전송 성공 (Chunk {i+1}/{len(msg_chunks)})")
                        success_chunk = True
                        break
                    else:
                        logger.error(f"[Telegram] 전송 실패 (Chunk {i+1}/{len(msg_chunks)}, {attempt+1}/{max_retries}) Status: {res.status_code}, Msg: {res.text}")
                except Exception as e:
                    # [추가] 네트워크 오류 등 긴 에러 메시지 축약
                    error_msg = str(e)
                    if "Network is unreachable" in error_msg:
                        error_msg = "네트워크 통신 불가 (Network is unreachable)"
                    elif "Max retries exceeded" in error_msg:
                        error_msg = "서버 접속 지연 (Connection Timeout)"

                    if _is_screen_output_allowed() and config.SCREEN_DEBUG_LEVEL in ["TRACE", "DEBUG"]:
                        config.console.print(f"[dim red][TRACE] ERR (TELEGRAM) {error_msg} ({attempt+1}/{max_retries})[/dim red]")
                    logger.error(f"[Telegram] 전송 중 오류 발생 (Chunk {i+1}/{len(msg_chunks)}, {attempt+1}/{max_retries}): {error_msg}")
                
                if attempt < max_retries - 1:
                    # [수정] 네트워크 단절 시 복구될 시간을 벌기 위해 점진적 대기 (1초 -> 2초 -> 4초)
                    time.sleep(2 ** attempt)
                    
            if not success_chunk:
                logger.error(f"[Telegram] 최종 전송 실패 (Chunk {i+1}/{len(msg_chunks)})")

    # [수정] 긴급 발송 여부에 따라 큐(Queue) 대기열 우회 처리
    if sync:
        _send_task()
    elif is_urgent:
        threading.Thread(target=_send_task, daemon=True, name="TgUrgentSender").start()
    else:
        # 핵심 매매 로직 블로킹 방지를 위해 스레드 풀로 위임 (비동기 전송)
        tg_sender_executor.submit(_send_task)

_last_alert_time = 0 # [추가] 텔레그램 알림 스로틀링용

# ==========================================================
# [추가] 실시간 단건 API용 초단기 마이크로 캐시 (Micro-Cache)
# 화면 렌더링 중 발생하는 동일 종목의 동시다발적 중복 호출 방지 (TTL: 3~10초)
# ==========================================================
_MICRO_CACHE = {}
_MICRO_CACHE_LOCK = threading.RLock()

def _get_micro_cache(key, ttl=60.0): # [수정] 잦은 중복 호출 방지를 위해 기본 TTL 상향
    with _MICRO_CACHE_LOCK:
        item = _MICRO_CACHE.get(key)
        if item and time.time() - item['time'] < ttl:
            return item['data']
    return None

def _set_micro_cache(key, data):
    with _MICRO_CACHE_LOCK:
        _MICRO_CACHE[key] = {'time': time.time(), 'data': data}

# [추가] yfinance 특수 티커를 TradingView 티커로 완벽히 1:1 매핑
YF_TO_TV_EXACT = {
    # [수정] 미국 3대 지수는 TradingView(15분 지연) 대신 yfinance(실시간)를 사용하도록 매핑 해제
    # "^IXIC": "NASDAQ:IXIC",        # 나스닥
    # "^GSPC": "SP:SPX",             # S&P 500
    # "^DJI": "DJ:DJI",              # 다우존스
    # "^RUT": "RUSSELL:RUT",         # 러셀 2000
    "KRW=X": "FX_IDC:USDKRW",      # 원/달러 환율
    "DX-Y.NYB": "TVC:DXY",         # 달러인덱스
    "CL=F": "NYMEX:CL1!",          # WTI 원유
    "BZ=F": "NYMEX:BRN1!",         # 브렌트유
    "GC=F": "COMEX:GC1!",          # 금
    "SI=F": "COMEX:SI1!",          # 은
    "HG=F": "COMEX:HG1!",          # 구리
    "NG=F": "NYMEX:NG1!",          # 천연가스
    "ZW=F": "CBOT:ZW1!",           # 밀
    "^VIX": "CBOE:VIX",            # 변동성 지수
    "BTC-USD": "CRYPTO:BTCUSD",    # 비트코인
    "ETH-USD": "CRYPTO:ETHUSD",    # 이더리움
    "^TNX": "TVC:US10Y",           # 미국채 10년물
    "^FVX": "TVC:US05Y",           # 미국채 5년물
    "^TYX": "TVC:US30Y",           # 미국채 30년물
    "ZT=F": "CBOT:ZT1!",           # 미국채 2년물 선물
    "ZF=F": "CBOT:ZF1!",           # 미국채 5년물 선물
    "ZN=F": "CBOT:ZN1!",           # 미국채 10년물 선물
    "ZB=F": "CBOT:ZB1!"            # 미국채 30년물 선물
}

def get_yf_fast_info(code, ttl=60.0):
    """TV 단건 조회 + yf_fast_info Fallback (캐싱 포함)"""
    cache_key = f"yf_fi_{code}"
    cached = _get_micro_cache(cache_key, ttl=ttl) # [수정] 상황에 맞게 TTL 조절 가능토록 인자 추가
    if cached: return cached

    tv_exact_symbol = YF_TO_TV_EXACT.get(code)
    is_special_ticker = any(c in code for c in ['^', '=', '-', '.'])
    
    # 1. TradingView Screener 우선 조회 (일반 미국 주식 또는 TV 매핑이 존재하는 지수)
    if not is_special_ticker or tv_exact_symbol:
        try:
            from tradingview_screener import Query, Column
            if tv_exact_symbol:
                _, df = Query().select('close', 'change_abs', 'volume', 'High.52Week', 'premarket_close', 'postmarket_close').get_tickers([tv_exact_symbol])
            else:
                _, df = Query().set_markets('america').select('close', 'change_abs', 'volume', 'High.52Week', 'premarket_close', 'postmarket_close').where(Column('name') == code).limit(1).get_scanner_data()
                
            if df is not None and not df.empty:
                row = df.iloc[0]
                close_p = row.get('close')
                change_abs = row.get('change_abs')
                
                # [추가] 장외(프리/애프터마켓) 가격이 존재할 경우 실시간 가격으로 우선 적용
                pre_close = row.get('premarket_close')
                post_close = row.get('postmarket_close')
                
                if pd.notna(post_close) and post_close > 0:
                    close_p = post_close
                elif pd.notna(pre_close) and pre_close > 0:
                    close_p = pre_close
                
                prev_close = None
                if pd.notna(row.get('close')) and pd.notna(change_abs):
                    prev_close = row.get('close') - change_abs
                    
                data = {
                    'last_price': close_p,
                    'regular_market_previous_close': prev_close,
                    'last_volume': row.get('volume', 0),
                    'year_high': row.get('High.52Week')
                }
                _set_micro_cache(cache_key, data)
                return data
        except Exception:
            pass

    # 2. yfinance Fallback
    try:
        time.sleep(0.05) # 야후 API 동시 호출 차단 완화용 미세 지연
        fi = yf.Ticker(code).fast_info
            
        # [수정] regular_market_previous_close가 없는 지수(달러인덱스 등)를 위한 Fallback
        prev_close = getattr(fi, 'regular_market_previous_close', None)
        if prev_close is None or pd.isna(prev_close):
            prev_close = getattr(fi, 'previous_close', None)
                
        data = {
            'last_price': getattr(fi, 'last_price', None),
                'regular_market_previous_close': prev_close,
            'last_volume': getattr(fi, 'last_volume', 0),
            'year_high': getattr(fi, 'year_high', None)
        }
        _set_micro_cache(cache_key, data)
        return data
    except Exception as e:
        logger.debug(f"get_yf_fast_info Error ({code}): {e}")
        return None

# ==========================================================
# [추가] 차트 데이터 인메모리 캐싱 시스템 (하이브리드 패치)
# ==========================================================
_CHART_CACHE = {}
_CHART_CACHE_LOCK = threading.RLock()

def clear_chart_cache():
    """모든 차트 데이터 캐시 초기화 (수동 갱신용)"""
    with _CHART_CACHE_LOCK:
        _CHART_CACHE.clear()
    if _is_screen_output_allowed():
        config.console.print("[bold green]차트 데이터 캐시(메모리)가 전체 초기화되었습니다.[/bold green]")
    logger.info("[Cache] 차트 데이터 캐시 수동 초기화")

def _get_cached_chart(code, is_overseas, is_index, fetch_func):
    """캐시된 차트를 반환하되, 당일 최신 캔들은 실시간 현재가로 덮어씌워 반환합니다."""
    ttl_minutes = getattr(config, 'CHART_CACHE_TTL_MINUTES', 60)
    if ttl_minutes <= 0:
        return fetch_func()

    now = datetime.now()
    today_str = now.strftime("%Y-%m-%d")
    cache_key = f"{code}_{is_overseas}_{is_index}"

    with _CHART_CACHE_LOCK:
        cached = _CHART_CACHE.get(cache_key)
        if cached:
            # 날짜 변경선 감지 (자정이 지나면 무효화)
            if cached['date'] != today_str:
                del _CHART_CACHE[cache_key]
                cached = None
            # TTL 감지
            elif (now - cached['timestamp']).total_seconds() > (ttl_minutes * 60):
                del _CHART_CACHE[cache_key]
                cached = None

    if cached:
        df = cached['df'].copy()
        try:
            today_ymd = now.strftime("%Y%m%d")
            last_date = str(df.iloc[-1]['date'])
            
            curr, open_p, high_p, low_p, vol, prev = 0, 0, 0, 0, 0, 0

            def _safe_float(val):
                if val is None: return 0.0
                s = str(val).strip().replace(',', '')
                if not s: return 0.0
                try: return float(s)
                except: return 0.0

            # 1. 가장 가벼운 현재가 API로 오늘 데이터만 가져오기
            if is_index and not is_overseas:
                res = get_domestic_index_price(code)
                if res and res.get('rt_cd') == '0':
                    out = res.get('output', {})
                    curr = _safe_float(out.get('bstp_nmix_prpr', 0))
                    prev = _safe_float(out.get('bstp_nmix_prdy_clpr', 0))
                    open_p, high_p, low_p = curr, curr, curr # 지수는 당일 고가/저가를 주지 않으므로 근사치 사용
                    vol = 0
            elif is_index and is_overseas:
                # yfinance 단건 현재가 빠른 조회
                try:
                    fi = get_yf_fast_info(code)
                    if fi:
                        curr = _safe_float(fi['last_price'])
                        prev = _safe_float(fi['regular_market_previous_close'])
                        open_p, high_p, low_p = curr, curr, curr
                        vol = _safe_float(fi['last_volume'])
                except Exception as e:
                    logger.debug(f"[Cache] yfinance fast_info error for {code}: {e}")
                    pass
            else:
                cp_data = get_current_price_data(code, is_overseas)
                if cp_data and cp_data.get('rt_cd') == '0':
                    out = cp_data.get('output', {})
                    if is_overseas:
                        curr = _safe_float(out.get('last', 0))
                        open_p = _safe_float(out.get('open', 0)) if out.get('open') else curr
                        high_p = _safe_float(out.get('high', 0)) if out.get('high') else curr
                        low_p = _safe_float(out.get('low', 0)) if out.get('low') else curr
                        vol = _safe_float(out.get('tvol', 0) or out.get('vol', 0))
                        diff = _safe_float(out.get('diff', 0))
                        prev = curr - diff
                    else:
                        curr = _safe_float(out.get('stck_prpr', 0))
                        open_p = _safe_float(out.get('stck_oprc', 0))
                        high_p = _safe_float(out.get('stck_hgpr', 0))
                        low_p = _safe_float(out.get('stck_lwpr', 0))
                        vol = _safe_float(out.get('acml_vol', 0))
                        prev = _safe_float(out.get('stck_prdy_clpr', 0))

            if curr > 0:
                # 2. 정합성(수정주가) 검증 로직: 전일 종가가 1.5% 이상 차이나면 오염된 캐시로 판단하고 파기
                target_prev = float(df.iloc[-2]['close']) if len(df) >= 2 else 0
                if last_date < today_ymd: target_prev = float(df.iloc[-1]['close'])
                
                if target_prev > 0 and prev > 0 and abs(target_prev - prev) / target_prev > 0.015:
                    if config.FILE_DEBUG_LEVEL == "DEBUG": logger.debug(f"[Cache] {code} 수정주가 감지 (캐시:{target_prev} != 실시간:{prev}). 파기 후 재조회.")
                    with _CHART_CACHE_LOCK:
                        if cache_key in _CHART_CACHE: del _CHART_CACHE[cache_key]
                    return fetch_func()

                # 3. 실시간 가격 패치(Patch)
                if last_date == today_ymd:
                    # 오늘 날짜 행 덮어쓰기 (고가/저가는 캐시된 데이터와 비교하여 최대/최소 유지)
                    old_high = float(df.iloc[-1]['high'])
                    old_low = float(df.iloc[-1]['low'])
                    high_p = max(old_high, high_p, curr)
                    low_p = min(old_low, low_p, curr) if low_p > 0 else min(old_low, curr)
                    df.loc[df.index[-1], ['open', 'high', 'low', 'close', 'volume']] = [open_p, high_p, low_p, curr, vol]
                else:
                    # 오늘 날짜 행이 없으면 새로 추가
                    new_row = pd.DataFrame([{'date': today_ymd, 'open': open_p, 'high': high_p, 'low': low_p, 'close': curr, 'volume': vol}])
                    df = pd.concat([df, new_row], ignore_index=True)
                
                return df
        except Exception as e:
            logger.debug(f"[Cache] Update failed for {code}: {e}")
            pass # 실패 시 그냥 원본 받아옴
            
    df = fetch_func()
    if df is not None and not df.empty:
        with _CHART_CACHE_LOCK:
            _CHART_CACHE[cache_key] = {
                'df': df.copy(),
                'timestamp': now,
                'date': today_str
            }
    return df

def prefetch_multiple_current_prices(codes, is_overseas=False, include_investor=True, progress_updater=None):
    """[최적화] 다중 종목 실시간 데이터 일괄 조회 (Micro-Cache 사전 예열)"""
    if not codes: return
    
    if is_overseas:
        # 1. TradingView 일괄 조회 (가장 빠름, 단 1회의 HTTP 요청으로 모두 해결)
        tv_success_codes = set()
        try:
            from tradingview_screener import Query
            _, df = Query().set_markets('america').select('name', 'close', 'change_abs', 'volume', 'High.52Week', 'premarket_close', 'postmarket_close').get_tickers(codes)
            
            if df is not None and not df.empty:
                for _, row in df.iterrows():
                    ticker = row.get('ticker')
                    if not ticker: continue
                    
                    close_p = row.get('close')
                    change_abs = row.get('change_abs')
                    
                    pre_close = row.get('premarket_close')
                    post_close = row.get('postmarket_close')
                    if pd.notna(post_close) and post_close > 0:
                        close_p = post_close
                    elif pd.notna(pre_close) and pre_close > 0:
                        close_p = pre_close
                        
                    if pd.isna(close_p): continue
                    
                    prev_close = None
                    if pd.notna(row.get('close')) and pd.notna(change_abs):
                        prev_close = row.get('close') - change_abs
                        
                    data = {
                        'last_price': close_p,
                        'regular_market_previous_close': prev_close,
                        'last_volume': row.get('volume', 0),
                        'year_high': row.get('High.52Week')
                    }
                    _set_micro_cache(f"yf_fi_{ticker}", data)
                    tv_success_codes.add(ticker)
                    if progress_updater: progress_updater()
        except Exception as e:
            logger.debug(f"TV Screener prefetch error: {e}")
            pass
            
        # 2. TV 조회에 실패한 종목들만 yfinance 병렬 워커로 Fallback
        remaining_codes = [c for c in codes if c not in tv_success_codes]
        if remaining_codes:
            def fetch_yf_worker(code):
                try: get_yf_fast_info(code)
                except Exception: pass
                if progress_updater: progress_updater()

            max_w = 4 if config.session.is_simulation else 5
            with concurrent.futures.ThreadPoolExecutor(max_workers=max_w) as executor:
                futures = [executor.submit(fetch_yf_worker, c) for c in remaining_codes]
                concurrent.futures.wait(futures)
    else:
        def fetch_worker(code):
            try: get_current_price_data(code, False)
            except: pass
            if include_investor:
                try: get_investor_trend(code)
                except: pass
            try: get_realtime_vol_strength(code)
            except: pass
            
        max_w = 4 if config.session.is_simulation else 10
        with concurrent.futures.ThreadPoolExecutor(max_workers=max_w) as executor:
            futures = [executor.submit(fetch_worker, c) for c in codes]
            for future in concurrent.futures.as_completed(futures):
                if progress_updater: progress_updater()

def prefetch_watchlists_async():
    """백그라운드에서 관심 종목의 차트 데이터를 캐싱(Warming)합니다."""
    # [진단] HTS_NO_PREFETCH=1 이면 예열을 통째로 건너뛴다 (메모리 폭증 원인 A/B 테스트용)
    if os.environ.get("HTS_NO_PREFETCH", "").strip().lower() in ("1", "true", "yes", "on"):
        logger.info("[Cache] HTS_NO_PREFETCH=1: 관심종목 예열을 건너뜁니다.")
        return None

    def worker():
        try:
            # [수정] 1. 글로벌 지수 데이터 백그라운드 예열 (로직 개선)
            logger.info("[Cache] 글로벌 지수 데이터 백그라운드 예열 시작")
            from modules import market, analysis
            
            # 국내 지수 이름 집합
            domestic_indices_names = { "코스피": "KOSPI", "코스피200": "KOSPI200", "코스닥": "KOSDAQ", "코스닥150": "KOSDAQ150" }
            
            def _prefetch_worker(name, ticker):
                try:
                    if name in domestic_indices_names:
                        # 국내 지수는 KIS API 우선 조회 로직을 태움
                        analysis.get_domestic_index_data(domestic_indices_names[name])
                    else:
                        # 해외 지수는 yfinance 조회 로직을 태움 (내부 캐싱 활용)
                        get_chart_data(ticker, is_overseas=True)
                except Exception as e:
                    logger.debug(f"[Cache] Index pre-fetch failed for {name}: {e}")

            # 병렬 실행
            with concurrent.futures.ThreadPoolExecutor(max_workers=5) as executor:
                futures = [executor.submit(_prefetch_worker, name, ticker) for name, ticker in market.ALL_INDICES]
                concurrent.futures.wait(futures)

            import config
            stocks = []
            for key in ["stocks_kr", "etfs_kr", "stocks_us", "etfs_us"]:
                stocks.extend([(s['code'], 'us' in key) for s in config.session.stock_data.get(key, [])])

            if not stocks: return
            
            # 중복 제거
            unique_stocks = []
            seen = set()
            for c, ovs in stocks:
                if c not in seen:
                    seen.add(c)
                    unique_stocks.append((c, ovs))
            
            logger.info(f"[Cache] 백그라운드 예열(Warming) 시작: 총 {len(unique_stocks)}종목")
            
            # 모의투자는 시스템 트레이딩 API 호출 방해를 피하기 위해 여유를 둠 (실전:0.1초, 모의:1.0초)
            delay = 1.0 if config.session.is_simulation else 0.1
            
            for code, is_overseas in unique_stocks:
                try:
                    # 캐시 적중 시 API 호출 생략 처리 로직은 _get_cached_chart 안에 이미 포함됨
                    get_chart_data(code, is_overseas=is_overseas)
                except Exception as e:
                    logger.debug(f"[Cache] 예열 중 오류({code}): {e}")
                
                time.sleep(delay)
                
            logger.info("[Cache] 백그라운드 예열 완료")
        except Exception as e:
            logger.error(f"[Cache] 예열 워커 오류: {e}")

    t = threading.Thread(target=worker, daemon=True, name="CacheWarmer")
    t.start()
    return t # [수정] 테스트 코드에서 제어할 수 있도록 스레드 객체 반환

class ThrottledSession(requests.Session):
    def __init__(self):
        super().__init__()
        self.request_history_sim = deque()
        self.request_history_real = deque()
        self.lock = threading.Lock() # [추가] Rate Limit 계산 동기화를 위한 락

    def request(self, method, url, *args, **kwargs):
        is_real_server = "openapi.koreainvestment.com" in url and "openapivts" not in url
        is_sim_server = "openapivts.koreainvestment.com" in url
        
        # [수정] 재시도 횟수 설정 (kwargs에서 전달받거나 config 기본값 사용)
        max_retries = kwargs.pop('retries', config.MAX_RETRIES)
        if max_retries is None: max_retries = config.MAX_RETRIES
        
        # [추가] 모의투자의 엄격한 TPS 제어로 인한 빈번한 차단을 방지하기 위해 기본 재시도 횟수 +1 추가
        if is_sim_server and max_retries == config.MAX_RETRIES:
            max_retries += 1
        
        response = None
        
        for attempt in range(max_retries + 1):
            target_limit = 0
            server_type = "EXTERNAL"
            wait_time = 0
            current_tps = 0
            
            # [Fix] 스케줄링 지연으로 인해 스레드들이 동시에 깨어나 융단 폭격을 하는 현상을 
            # 원천 차단하기 위해, 예약 방식에서 폴링/토큰 획득 방식으로 TPS 제어 재설계
            if is_real_server or is_sim_server:
                while True:
                    wait_time = 0
                    with self.lock:
                        now = time.time()
                        target_limit = config.REAL_TX_PER_SECOND if is_real_server else config.SIM_TX_PER_SECOND
                        history = self.request_history_real if is_real_server else self.request_history_sim
                        server_type = "REAL" if is_real_server else "SIMULATION"
                        
                        if target_limit > 0:
                            # 1. 윈도우 기반 한도 체크 (Burst 방어)
                            window_size = 1.5 if is_sim_server else 1.1
                            
                            while history and history[0] <= now - window_size:
                                history.popleft()
                                
                            # 2. 최소 간격 체크 (고르게 분산)
                            min_interval = (1.0 / target_limit)
                            if is_sim_server: min_interval *= 1.2
                            else: min_interval *= 1.05

                            time_since_last = now - history[-1] if history else float('inf')

                            if len(history) < target_limit and time_since_last >= min_interval:
                                history.append(now)
                                current_tps = len(history)
                                break # 락 해제 후 전송 진행
                            else:
                                wait_from_window = (history[0] + window_size) - now if len(history) >= target_limit else 0
                                wait_from_interval = min_interval - time_since_last
                                wait_time = max(wait_from_window, wait_from_interval)
                                if wait_time <= 0: wait_time = 0.05
                        else:
                            break # 한도 미설정 시 즉시 통과
                            
                    # 전송 권한을 얻지 못한 경우 락을 반환하고 대기한 후 다시 락을 잡아 권한 획득 시도
                    time.sleep(wait_time)

            if _is_screen_output_allowed() and config.SCREEN_DEBUG_LEVEL in ["TRACE", "DEBUG"] and (is_sim_server or is_real_server):
                config.console.print(f"[dim cyan][TRACE] REQ ({server_type}) TPS:{current_tps:.1f} | {method} {url}[/dim cyan]")
                if config.SCREEN_DEBUG_LEVEL == "DEBUG":
                    if kwargs.get('params'): config.console.print(f"[dim cyan]  > Params: {kwargs['params']}[/dim cyan]")
                    if kwargs.get('data'): config.console.print(f"[dim cyan]  > Body Data: {kwargs['data']}[/dim cyan]")
                    if kwargs.get('json'): config.console.print(f"[dim cyan]  > JSON Data: {kwargs['json']}[/dim cyan]")

            try:
                if 'timeout' not in kwargs:
                    kwargs['timeout'] = config.DEFAULT_TIMEOUT
                
                response = super().request(method, url, *args, **kwargs)

                if _is_screen_output_allowed() and config.SCREEN_DEBUG_LEVEL in ["TRACE", "DEBUG"] and (is_sim_server or is_real_server):
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
                    except Exception as e:
                        logger.debug(f"API response logging json parse error: {e}")
                    
                    url_tail = url.split('/')[-1].split('?')[0]
                    config.console.print(f"[dim magenta][TRACE] RES ({server_type}) Status:{response.status_code} RT_CD:{rt_cd} MSG_CD:{msg_cd} ({desc}) | {url_tail}[/dim magenta]")
                    
                    if config.SCREEN_DEBUG_LEVEL == "DEBUG" and res_data:
                        config.console.print(f"[dim magenta]  > Response Data: {json.dumps(res_data, ensure_ascii=False, indent=2)}[/dim magenta]")

                # [수정] 통합 재시도 로직 (모든 에러 상황 처리)
                should_retry = False
                retry_reason = ""

                # 1. HTTP Status 확인
                if response.status_code != 200:
                    # [수정] HTTP 에러는 재시도하지 않고 로그만 기록 (연결 끊김은 except 블록에서 처리됨)
                    try:
                        logger.error(f"⚠️ [HTTP Error] URL: {url} | Status: {response.status_code} | Body: {response.text[:500]}")
                    except: pass
                
                # 2. API 응답 코드 확인
                if not should_retry:
                    # [수정] OAuth 토큰 발급 요청 등은 rt_cd 구조가 다르므로 검사 제외
                    if "oauth2" in url:
                        pass
                    elif is_sim_server or is_real_server:
                        try:
                            res_json = response.json()
                            rt_cd = res_json.get('rt_cd')
                            msg_cd = res_json.get('msg_cd')
                            msg1 = res_json.get('msg1', '')

                            # [추가] 지수 조회 API(실전)의 빈 응답 이슈 예외 처리
                            # 실전투자 서버에서 지수 조회 시 rt_cd가 없거나 빈 값으로 오는 경우가 있음 -> 에러 로그 제외하고 Fallback 유도
                            if "inquire-daily-indexchartprice" in url and (not rt_cd or rt_cd != '0'):
                                return response

                            # 토큰 만료 처리 (특수 케이스: 갱신 후 재시도)
                            if msg_cd in ['EGW00123', 'EGW00121']:
                                # [수정] 자동 갱신 로직 삭제. 만료 플래그만 설정하고 예외 발생시킴.
                                logger.error(f"토큰 만료 감지(Code: {msg_cd}). 메인 스레드에 갱신을 요청합니다.")
                                context.TOKEN_EXPIRED = True
                                raise Exception(f"Token Expired ({msg_cd})")
                            
                            # 그 외 모든 API 에러 (성공이 아닌 경우)
                            elif rt_cd is not None and rt_cd != '0':
                                rt_disp = rt_cd if rt_cd else "(Empty)"
                                msg_disp = msg_cd if msg_cd else "(Empty)"
                                msg1_disp = msg1 if msg1 else "(Empty)"
                                
                                # EGW00201: 전체 API 초당 거래건수 초과
                                # EGW00215: 원장(계좌/주문) API 초당 거래건수 초과
                                if msg_cd == 'EGW00201' or (msg_cd == 'EGW00215' and 'inquire' in url):
                                    should_retry = True
                                    retry_reason = f"Rate Limit Exceeded ({msg_cd}): {msg1_disp}"
                                elif msg_cd == 'EGW00215' and 'inquire' not in url:
                                    # 주문과 같이 상태 변화가 있는 API는 중복 방지를 위해 재시도하지 않음
                                    req_body = kwargs.get('data', '')
                                    logger.error(f"⚠️ [ORDER_FAIL] [API Error] URL: {url} | RT_CD: {rt_disp} | MSG_CD: {msg_disp} | MSG: {msg1_disp} | REQ: {req_body}")
                                elif msg_cd == 'MCA00124' and 'chk-holiday' in url and is_sim_server:
                                    # 모의투자 서버에서 휴장일 조회 미지원 에러 로그 무시 (모의투자 모드에서만 동작하도록 명확화)
                                    pass
                                else:
                                    # 단순 조회(GET) 요청이면서 일시적인 API 서버/MCI 오류인 경우 안전하게 재시도 처리
                                    if method == 'GET' and ('MCI' in msg1_disp or '게이트웨이' in msg1_disp):
                                        should_retry = True
                                        retry_reason = f"KIS Server Intermittent Error ({msg_cd}): {msg1_disp}"
                                    else:
                                        req_body = kwargs.get('data', '')
                                        # 조회 API와 주문 API를 구분하여 에러 로그 출력
                                        if method == 'GET' or 'inquire' in url:
                                            logger.error(f"⚠️ [API Error] URL: {url} | RT_CD: {rt_disp} | MSG_CD: {msg_disp} | MSG: {msg1_disp}")
                                        else:
                                            logger.error(f"⚠️ [ORDER_FAIL] [API Error] URL: {url} | RT_CD: {rt_disp} | MSG_CD: {msg_disp} | MSG: {msg1_disp} | REQ: {req_body}")

                        except Exception as e:
                            # JSON 파싱 실패 등
                            if not str(e).startswith("Token Expired"):
                                # [수정] 파싱 에러는 재시도하지 않음
                                # should_retry = True
                                # retry_reason = f"Response Parsing Error: {e}"
                                # [추가] 파싱 에러 상세 로깅
                                logger.error(f"⚠️ [Parsing Error] URL: {url} | Error: {e} | Body: {response.text[:500]}")
                            else:
                                raise e # 토큰 만료 예외는 그대로 전달
                
                if not should_retry:
                    return response
                
                # 재시도 대상이면 예외를 발생시켜 아래 except 블록에서 처리
                raise Exception(retry_reason)

            except Exception as e:
                # [Fix] 토큰 만료 예외는 내부 재시도(ThrottledSession)를 하지 않고 즉시 상위(call_api)로 전파
                if "Token Expired" in str(e):
                    raise e

                # [수정] 모든 예외(연결 끊김, API 에러 등)에 대해 백오프 후 재시도
                if attempt < max_retries:
                    base_delay = getattr(config, 'RETRY_DELAY_SERVER', 1.0)
                    # [수정] 동시에 여러 스레드가 깨어나는 것을 방지하기 위해 랜덤 지터(Jitter) 추가
                    jitter = random.uniform(0.1, 0.5)
                    wait_time = (base_delay * (2 ** attempt)) + jitter
                    
                    msg = f"⚠️ API 요청 실패. {wait_time:.1f}초 후 재시도합니다. 사유: {str(e)}"
                    logger.warning(msg)
                    
                    if _is_screen_output_allowed() and config.SCREEN_DEBUG_LEVEL in ["TRACE", "DEBUG"]:
                        config.console.print(f"[dim yellow][TRACE] {msg}[/dim yellow]")
                    
                    time.sleep(wait_time)
                    continue
                
                # [수정] 최종 실패 시 로그 출력 및 마지막 응답/예외 반환
                logger.error(f"⚠️ API 요청 최종 실패. 사유: {str(e)}")
                if response is not None:
                    return response
                raise e
        
        return response

session = ThrottledSession()

# [수정] 연결 끊김(RemoteDisconnected) 등 '네트워크 레벨' 에러만 어댑터에서 자동 재시도.
# [중요] HTTP 5xx(특히 EGW00201/EGW00215는 Status 500으로 내려옴)는 어댑터 재시도 대상에서 제외한다.
#  - 어댑터 레벨 재시도는 ThrottledSession의 TPS 게이트를 거치지 않고 super().request() 내부에서
#    연사되므로, 모의투자(2 TPS) 서버에서는 재시도 자체가 초당 거래건수 초과(EGW00201)를 유발한다.
#  - 5xx/Rate-Limit 재시도는 TPS 게이트 + 지수 백오프를 갖춘 앱 레벨(ThrottledSession)에서만 처리한다.
retry_strategy = Retry(
    total=2,
    backoff_factor=0.5,
    status_forcelist=[],
    allowed_methods=["GET", "POST"],
    raise_on_status=False
)

# [수정] 실전투자(20 TPS) 병렬 처리를 위해 커넥션 풀 크기 확장 (기본 10 -> 30)
# 동시에 많은 네트워크 요청이 발생해도 커넥션 대기 없이 즉시 처리 가능
session.mount('https://', TLSAdapter(max_retries=retry_strategy, pool_connections=30, pool_maxsize=30))

# [추가] 토큰 발급 전용 세션 (강화된 재시도 로직)
def _create_token_session():
    """토큰 발급 전용 requests 세션을 생성합니다. (강화된 재시도 로직 포함)"""
    session = requests.Session()
    # KIS API 서버의 일시적 장애(5xx 에러) 및 네트워크 오류에 대응하기 위한 재시도 전략
    retry_strategy = Retry(
        total=5,  # 총 5회 재시도
        backoff_factor=1, # 실패 시 대기 시간 (1s, 2s, 4s, 8s, 16s...)
        status_forcelist=[429, 500, 502, 503, 504], # 재시도할 HTTP 상태 코드
        allowed_methods=["POST"], # POST 요청에 대해서도 재시도
        raise_on_status=False
    )
    adapter = HTTPAdapter(max_retries=retry_strategy)
    session.mount("https://", adapter)
    return session

_token_session = _create_token_session()


def get_current_token():
    # [추가] 시스템 트레이딩 컨텍스트 확인
    if getattr(context.trade_context, 'use_auto_account', False) and not config.session.is_simulation:
        return get_auto_access_token()
        
    if config.session.is_simulation:
        return get_access_token()
    else:
        return get_real_access_token()

def get_access_token(force_refresh=False):
    # [Fix] 토큰 갱신 경합 방지 (Thread-Safe)
    with context.TOKEN_REFRESH_LOCK:
        return _get_access_token_internal(force_refresh)

def check_and_refresh_token_if_expired():
    """토큰 만료 플래그 확인 및 갱신 (메인 스레드/로그 뷰어 등에서 주기적 호출)"""
    if context.TOKEN_EXPIRED:
        now = time.time()
        # 60초 쿨타임 적용 (로그 뷰어 등에서 무한 루프 호출 시 API 및 텔레그램 도배 방지)
        if getattr(context, 'LAST_TOKEN_REFRESH_ATTEMPT', 0) and now - context.LAST_TOKEN_REFRESH_ATTEMPT < 60:
            return
            
        context.LAST_TOKEN_REFRESH_ATTEMPT = now

        if _is_screen_output_allowed() and config.SCREEN_DEBUG_LEVEL != "OFF":
            config.console.print("\n[bold yellow]토큰 만료가 감지되었습니다. 토큰 갱신을 시도합니다...[/bold yellow]")
        
        success = True
        fail_reason = "Unknown Error"

        try:
            if config.session.is_simulation:
                if not get_access_token(force_refresh=True):
                    success = False
                    fail_reason = "모의투자 토큰 발급 실패 (API 서버 응답 없음 또는 점검 중)"
            else:
                if not get_real_access_token(force_refresh=True):
                    success = False
                    fail_reason = "실전투자 토큰 발급 실패 (API 서버 응답 없음 또는 점검 중)"
                
                if success and config.session.auto_app_key:
                    if not get_auto_access_token(force_refresh=True):
                        success = False
                        fail_reason = "자동매매 토큰 발급 실패 (API 서버 응답 없음 또는 점검 중)"
            
            if success:
                context.TOKEN_EXPIRED = False
                if _is_screen_output_allowed() and config.SCREEN_DEBUG_LEVEL != "OFF":
                    config.console.print("[bold green]토큰 갱신 완료. 시스템을 정상적으로 계속 사용합니다.[/bold green]\n")
                
                # [추가] 토큰 갱신 지연 알림이 발송된 적이 있다면, 복구 알림 전송
                if getattr(context, 'LAST_TOKEN_REFRESH_ALERT', 0) > 0:
                    try:
                        send_telegram_message("✅ [시스템 복구] API 토큰이 정상적으로 갱신되었습니다.\n시스템을 계속 운영합니다.")
                        # 복구 알림 후에는 다시 지연 알림을 보낼 수 있도록 초기화
                        context.LAST_TOKEN_REFRESH_ALERT = 0
                    except Exception as e:
                        logger.debug(f"Token refresh recovery telegram send error: {e}")
            else:
                raise Exception(fail_reason)

        except Exception as e:
            if _is_screen_output_allowed() and config.SCREEN_DEBUG_LEVEL != "OFF":
                config.console.print(f"[bold red]토큰 갱신 실패: {e}[/bold red]")
                config.console.print("[dim]서버 점검 등 일시적 오류일 수 있으므로 잠시 후 자동으로 다시 시도합니다.[/dim]")
            
            # [수정] 텔레그램 알림 전송 쿨타임 적용 (1시간당 1회 제한으로 알림 폭탄 방지)
            if now - getattr(context, 'LAST_TOKEN_REFRESH_ALERT', 0) > 3600:
                try:
                    send_telegram_message(f"🚨 [시스템 경고] API 토큰 갱신 지연\n\n사유: {str(e)}\n\n(한국투자증권 서버 정기 점검 시간일 수 있습니다. 시스템은 멈추지 않고 1분 간격으로 토큰 발급을 계속 재시도합니다.)")
                    context.LAST_TOKEN_REFRESH_ALERT = now
                except: pass

def _fetch_and_set_token(token_type, force_refresh=False):
    """
    지정된 유형의 액세스 토큰을 발급받고 세션에 저장합니다.

    Args:
        token_type (str): "SIMULATION", "REAL", "AUTO" 중 하나
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

    if token_type == "SIMULATION":
        app_key = config.session.app_key
        app_secret = config.session.app_secret
        url = f"{config.SIM_URL}/oauth2/tokenP"
    elif token_type == "REAL":
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
                logger.info(f"[green]{token_type} 토큰 발급 완료[/green]")
                return token
            else:
                logger.error(f"토큰 발급 응답 오류: {res.text}")
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
            return None

    except requests.exceptions.RequestException as e:
        logger.error(f"{token_type} 토큰 발급 중 네트워크 오류: {e}")
        return None
    except Exception as e:
        logger.error(f"{token_type} 토큰 발급 중 오류: {e}")
        return None

def _get_access_token_internal(force_refresh=False):
    # [수정] 백그라운드 스레드에서도 토큰이 없거나 만료된 경우 발급 허용
    # (자정 이후 세션 만료 등으로 인한 재발급 필요성 대응)
    return _fetch_and_set_token("SIMULATION", force_refresh)

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
        if retries is None: retries = config.MAX_RETRIES
        
        # [추가] 토큰 만료 시 재시도를 위한 루프 (최대 1회 재시도)
        for attempt in range(2):
            try:
                token_to_use = get_current_token()
                base_url = config.session.url_base if config.session.is_simulation else config.REAL_URL
                
                # [수정] 컨텍스트에 따라 키 선택
                use_auto = getattr(context.trade_context, 'use_auto_account', False)
                if use_auto and not config.session.is_simulation:
                    key = config.session.auto_app_key
                    secret = config.session.auto_app_secret
                    if not key: # Fallback
                        key = config.session.real_app_key; secret = config.session.real_app_secret
                else:
                    key = config.session.app_key if config.session.is_simulation else config.session.real_app_key
                    secret = config.session.app_secret if config.session.is_simulation else config.session.real_app_secret

                env_key = "sim" if config.session.is_simulation else "real"
                current_tr_id = tr_id
                if current_tr_id is None:
                    try:
                        current_tr_id = constants.TR_ID_CONFIG[market][category][action][env_key]
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
                if _is_screen_output_allowed() and config.SCREEN_DEBUG_LEVEL == "DEBUG":
                    masked_headers = headers.copy()
                    if 'authorization' in masked_headers:
                        masked_headers['authorization'] = masked_headers['authorization'][:20] + "..."
                    if 'appSecret' in masked_headers:
                        masked_headers['appSecret'] = "********"
                    config.console.print(f"[dim cyan]  > Headers: {masked_headers}[/dim cyan]")
                
                full_url = f"{base_url}/{url_path}"
                
                # [수정] 재시도 로직을 session.request로 위임 (retries 인자 전달)
                if method == "GET":
                    res = session.get(full_url, headers=headers, params=params, timeout=timeout, retries=retries)
                else:
                    res = session.post(full_url, headers=headers, data=json.dumps(data) if data else None, timeout=timeout, retries=retries)
                
                return res.json()
            except Exception as e:
                # [추가] 토큰 만료 예외 감지 시 갱신 후 재시도
                if "Token Expired" in str(e) and attempt == 0:
                    logger.warning(f"[API] 토큰 만료 감지({str(e)}). 갱신 후 재시도합니다.")
                    new_token = None
                    if getattr(context.trade_context, 'use_auto_account', False) and not config.session.is_simulation:
                        new_token = get_auto_access_token(force_refresh=True)
                    elif config.session.is_simulation:
                        new_token = get_access_token(force_refresh=True)
                    else:
                        new_token = get_real_access_token(force_refresh=True)
                    
                    if new_token:
                        context.TOKEN_EXPIRED = False
                        
                    continue
                
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

def get_stock_name_by_code(code, is_overseas):
    final_name = None
    if not is_overseas:
        url = f"https://finance.naver.com/item/main.naver?code={code}"
        try:
            headers = {'User-Agent': 'Mozilla/5.0'}
            r = session.get(url, headers=headers, timeout=3)
            m_og = re.search(r'meta property="og:title" content="(.*?)"', r.text)
            if m_og:
                raw_title = m_og.group(1).strip()
                if "페이지를 찾을 수 없습니다" not in raw_title:
                    clean_name = re.sub(r'\s*\(\d{6}\)', '', raw_title)
                    clean_name = re.sub(r'\s*[:|-]\s*(Npay|네이버|Naver|금융|증권).*', '', clean_name, flags=re.IGNORECASE)
                    final_name = clean_name.strip()
                if final_name in ["Npay 증권", "네이버 페이 증권", "증권", "금융", "네이버 금융"]: final_name = None
            else: final_name = code
        except Exception as e:
            logger.debug(f"Naver stock name parsing error: {e}")
            final_name = code
    else:
        # 1. TradingView Screener 우선 조회 (속도 개선)
        try:
            from tradingview_screener import Query, Column
            count, df = Query().set_markets('america').select('description').where(Column('name') == code).limit(1).get_scanner_data()
            if count > 0 and not df.empty:
                final_name = df.iloc[0]['description']
        except Exception as e:
            logger.debug(f"TV screener stock name fetch error: {e}")
        
        if not final_name:
            # 2. yfinance Fallback
            try:
                with open(os.devnull, 'w') as fnull:
                    old_stderr = sys.stderr; sys.stderr = fnull
                    try:
                        ticker = yf.Ticker(code); info = ticker.info
                        if info: final_name = info.get('longName') or info.get('shortName')
                    except Exception as e:
                        logger.debug(f"yf.Ticker info fetch error: {e}")
                    finally: sys.stderr = old_stderr
            except Exception as e:
                logger.debug(f"yf.Ticker outer block error: {e}")
            
    if not final_name and code: return code
    return final_name

def _get_hourly_chart_data(code, is_overseas):
    """시봉(1시간) 데이터 조회 (yfinance 전용)"""
    targets = []
    if is_overseas:
        targets.append(code)
    else:
        # config 데이터(stock.json)를 참조하여 정확한 거래소 티커 1개만 구성
        market_suffix = None
        for key in ["stocks_kr", "etfs_kr"]:
            for item in config.session.stock_data.get(key, []):
                if item.get('code') == code and "exchange" in item:
                    if item['exchange'].upper() == "KOSDAQ":
                        market_suffix = ".KQ"
                    elif item['exchange'].upper() == "KOSPI":
                        market_suffix = ".KS"
                    break
            if market_suffix: break
            
        if market_suffix:
            targets.append(f"{code}{market_suffix}")
        else:
            # 시장 정보를 모를 경우 (직접 입력 등) 기존처럼 둘 다 시도
            targets.append(f"{code}.KS")
            targets.append(f"{code}.KQ")
    
    for t in targets:
        try:
            # 1시간 간격, 최근 3개월 (지표 계산을 위해 충분한 데이터 확보)
            df = fetch_yfinance_data(t, period="3mo", interval="1h")
            if not df.empty:
                # 1. 컬럼 평탄화 및 튜플 방어
                flat_cols = []
                for col in df.columns:
                    if isinstance(col, tuple):
                        flat_cols.append(str(col[0]).lower())
                    else:
                        flat_cols.append(str(col).lower())
                df.columns = flat_cols
                
                # 2. 인덱스 리셋 (Datetime을 컬럼으로)
                df.reset_index(inplace=True)
                
                # 3. 모든 컬럼명을 소문자로 강제 변환
                df.columns = [str(c).lower() for c in df.columns]
                df = df.loc[:, ~df.columns.duplicated()].copy() # 중복 컬럼 제거 방어 로직 추가
                if 'datetime' in df.columns: df.rename(columns={'datetime': 'date'}, inplace=True)
                
                cols = ['date', 'open', 'high', 'low', 'close', 'volume']
                for c in cols:
                    if c not in df.columns: df[c] = 0
                
                df = df[cols].copy()
                # 시간대 변환 (UTC -> KST)
                if pd.api.types.is_datetime64_any_dtype(df['date']):
                    if df['date'].dt.tz is None:
                        df['date'] = df['date'].dt.tz_localize('UTC').dt.tz_convert('Asia/Seoul')
                    else:
                        df['date'] = df['date'].dt.tz_convert('Asia/Seoul')

                return df.sort_values('date', ascending=True).reset_index(drop=True)
        except Exception as e:
            logger.debug(f"yfinance hourly fetch failed for {t}: {e}")
            pass
            
    return pd.DataFrame()

def get_chart_data(code, is_overseas=False, period_type='daily'):
    """
    기술적 분석을 위한 차트 데이터를 조회합니다.
    period_type: 'daily' (일봉), 'hourly' (시봉), 'intraday' (분봉)
    """
    if period_type == 'intraday':
        return _get_intraday_chart_data(code, is_overseas)
    
    if period_type == 'hourly':
        return _get_hourly_chart_data(code, is_overseas)

    now = datetime.now()
    today = now.strftime("%Y%m%d")
    start_date_origin = (now - timedelta(days=config.INDICATOR_PARAMS["CHART_LOOKBACK_DAYS"])).strftime("%Y%m%d")
    
    is_index = (code.startswith('^') or code.endswith('=F') or code.endswith('=X') or code == 'DX-Y.NYB' or '-USD' in code or code.endswith('.SS') or code.endswith('.IL'))
    if is_index:
        try:
            df = fetch_yfinance_data(code, period="2y")
            if df is None or df.empty: return pd.DataFrame()
                
            # 1. 컬럼 평탄화 및 튜플 방어
            flat_cols = []
            for col in df.columns:
                if isinstance(col, tuple):
                    flat_cols.append(str(col[0]).lower())
                else:
                    flat_cols.append(str(col).lower())
            df.columns = flat_cols
            
            df.reset_index(inplace=True)
            df.columns = [str(c).lower() for c in df.columns]
            df = df.loc[:, ~df.columns.duplicated()].copy() # 중복 컬럼 제거 방어 로직 추가
            
            cols = ['date', 'open', 'high', 'low', 'close', 'volume']
            for c in cols:
                if c not in df.columns: df[c] = 0
            df = df[cols].copy()
            df['date'] = df['date'].apply(lambda x: x.strftime('%Y%m%d') if hasattr(x, 'strftime') else str(x).replace('-', '')[:8])
            df = df[df['date'] >= start_date_origin]
            return df.sort_values('date', ascending=True).reset_index(drop=True).tail(250)
        except Exception as e:
            logger.debug(f"yfinance 2y index fetch error: {e}")
            return pd.DataFrame()

    if not is_overseas:
        url_path = constants.API_URLS["DOMESTIC"]["QUOTATIONS"]["CHART"]
        all_items = []
        current_end_date = today
        current_start_date = start_date_origin
        
        retry_count = 0
        while len(all_items) < 250 and retry_count < 10:
            params = {"FID_COND_MRKT_DIV_CODE": "J", "FID_INPUT_ISCD": code, "FID_INPUT_DATE_1": current_start_date, "FID_INPUT_DATE_2": current_end_date, "FID_PERIOD_DIV_CODE": "D", "FID_ORG_ADJ_PRC": "0"}
            data = call_api(url_path, "domestic", "quotations", "chart", params=params, timeout=3)
            if data.get('rt_cd') == '0':
                items = data.get('output2')
                if items:
                    all_items.extend(items)
                    temp_dates = sorted([x['stck_bsop_date'] for x in items])
                    current_end_date = (datetime.strptime(temp_dates[0], "%Y%m%d") - timedelta(days=1)).strftime("%Y%m%d")
                else:
                    break
                retry_count += 1
            elif data.get('msg_cd') == 'EGW00201': 
                time.sleep(0.5)
                retry_count += 1
            else: 
                time.sleep(0.2)
                break
            
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
            
        url_path = constants.API_URLS["OVERSEAS"]["QUOTATIONS"]["CHART"]
        
        for excd in exchanges:
            all_items = []
            next_bymd = today
            
            retry_count = 0
            while len(all_items) < 250 and retry_count < 10:
                params = {"AUTH": "", "EXCD": excd, "SYMB": code, "GUBN": "0", "BYMD": next_bymd, "MODP": "1", "KEYB": code}
                data = call_api(url_path, "overseas", "quotations", "chart", params=params, timeout=3)
                if data.get('rt_cd') == '0':
                    items = data.get('output2')
                    if items:
                        if not all_items: 
                            if cached_ex != excd: config.session.update_cache_and_save(code, excd)
                        all_items.extend(items)
                        last = items[-1]['xymd']
                        next_bymd = (datetime.strptime(last, "%Y%m%d") - timedelta(days=1)).strftime("%Y%m%d")
                    else:
                        break
                    retry_count += 1
                elif data.get('msg_cd') == 'EGW00201': 
                    time.sleep(0.5)
                    retry_count += 1
                else: 
                    time.sleep(0.1)
                    break
            
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

def _get_intraday_chart_data(code, is_overseas):
    """분봉(1분) 데이터 조회 (KIS API 사용, 해외/지수는 yfinance Fallback)"""
    
    # 1. KIS API 미지원 대상 확인 (해외주식, 지수 등)
    use_fallback = is_overseas
    if code.startswith('^') or (code.startswith('0001') and len(code) == 4):
        use_fallback = True

    # Fallback 로직 (yfinance)
    if use_fallback:
        try:
            # [수정] 국내 종목이 Fallback을 탈 경우를 대비하여 stock.json을 참조해 정확한 티커(.KS / .KQ) 생성
            target_ticker = code
            if not is_overseas and not code.startswith('^'):
                market_suffix = None
                for key in ["stocks_kr", "etfs_kr"]:
                    for item in config.session.stock_data.get(key, []):
                        if item.get('code') == code and "exchange" in item:
                            if item['exchange'].upper() == "KOSDAQ":
                                market_suffix = ".KQ"
                            elif item['exchange'].upper() == "KOSPI":
                                market_suffix = ".KS"
                            break
                    if market_suffix: break
                
                if market_suffix:
                    target_ticker = f"{code}{market_suffix}"
                else:
                    target_ticker = f"{code}.KS" # 기본값 코스피

            logger.info(f"[API] '{target_ticker}' yfinance 분봉 조회 시도 (Fallback)...")
            # yfinance는 1분봉 최대 7일, 5분봉 최대 60일 지원
            # [수정] 5일치를 가져와서 최근 390개(약 1일 거래시간)만 추출하여 차트 여백 최소화
            df = fetch_yfinance_data(target_ticker, period="5d", interval="1m")
            if df is not None and not df.empty:
                # 1. 컬럼 평탄화 및 튜플 방어
                flat_cols = []
                for col in df.columns:
                    if isinstance(col, tuple):
                        flat_cols.append(str(col[0]).lower())
                    else:
                        flat_cols.append(str(col).lower())
                df.columns = flat_cols
                
                df.reset_index(inplace=True)
                
                # 2. 소문자 변환
                df.columns = [str(c).lower() for c in df.columns]
                df = df.loc[:, ~df.columns.duplicated()].copy() # 중복 컬럼 제거 방어 로직 추가
                if 'datetime' in df.columns: df.rename(columns={'datetime': 'date'}, inplace=True)

                # [추가] yfinance 시간대 변환 (UTC/현지시간 -> 한국 시간 KST)
                if 'date' in df.columns and pd.api.types.is_datetime64_any_dtype(df['date']):
                    if df['date'].dt.tz is not None:
                        df['date'] = df['date'].dt.tz_convert('Asia/Seoul')
                    else:
                        pass

                cols = ['date', 'open', 'high', 'low', 'close', 'volume']
                for c in cols:
                    if c not in df.columns: df[c] = 0
                    
                df = df[cols].copy().sort_values('date', ascending=True)
                
                # 최근 390개 (약 6시간 30분 = 1일 장 운영 시간) 데이터만 유지
                if len(df) > 390:
                    df = df.iloc[-390:]
                
                return df.reset_index(drop=True)
                return df
        except Exception as e:
            logger.error(f"[API] yfinance 분봉 조회 실패: {e}")
        return pd.DataFrame()
    
    # 국내 주식 KIS API 1분봉 조회
    # URL: /uapi/domestic-stock/v1/quotations/inquire-time-itemchartprice
    # TR_ID: FHKST03010200
    url_path = constants.API_URLS["DOMESTIC"]["QUOTATIONS"]["TIME_CHART"]
    tr_id = "FHKST03010200"
    
    all_items = []
    current_time_key = "" # 빈 문자열 = 현재시간(최신)
    
    # [수정] 하루치(381분) 전체 조회를 위해 반복 횟수 및 최대 건수 상향 (3회 -> 20회)
    for i in range(20):
        params = {
            "FID_ETC_CLS_CODE": "",
            "FID_COND_MRKT_DIV_CODE": "J",
            "FID_INPUT_ISCD": code,
            "FID_INPUT_HOUR_1": current_time_key,
            "FID_PW_DIV_CODE": "1", # 1분봉
            "FID_PW_DATA_INCU_YN": "N" # [추가] 필수 입력 필드 (데이터 포함 여부)
        }
        
        # [디버그] 요청 상세 로그 (태그 포함)
        logger.debug(f"[API_DEBUG] 분봉 조회 요청({i+1}): {code} | TimeKey: {current_time_key} | Params: {params}")
        
        # [추가] 모의투자 환경일 경우 TR ID 변경 (실전: FHKST03010200, 모의: 없음/지원안함 가능성 체크)
        # KIS 모의투자 API 문서를 보면 주식분봉조회는 실전/모의 동일하게 FHKST03010200을 사용하는 경우가 많으나,
        # 모의투자 서버의 경우 데이터가 없거나 다른 TR일 수 있음. 일단 실전용 TR 사용.
        res = call_api(url_path, "domestic", "quotations", "time_chart", params=params, tr_id=tr_id)
        
        if res.get('rt_cd') == '0':
            items = res.get('output2', [])
            if items:
                all_items.extend(items)
                # 다음 페이징을 위해 마지막 데이터의 시간 사용
                last_item = items[-1]
                current_time_key = last_item.get('stck_cntg_hour')
                
                # [수정] 하루 장 운영 시간(09:00~15:30) 커버를 위해 420건으로 상향
                if len(all_items) >= 420: break
            else:
                logger.debug(f"[API_DEBUG] 분봉 조회 결과 없음 (반복 중단): {res.get('msg1')}")
                break
        else:
            logger.error(f"[API] 분봉 조회 실패: {res.get('msg1')} (Code: {res.get('msg_cd')})")
            break
            
        time.sleep(0.1) # Rate Limit
    
    if not all_items:
        return pd.DataFrame()
        
    df = pd.DataFrame(all_items)
    
    # 컬럼 매핑 및 정제
    # stck_bsop_date: 일자, stck_cntg_hour: 시간
    # stck_prpr: 현재가, stck_oprc: 시가, stck_hgpr: 고가, stck_lwpr: 저가, cntg_vol: 체결량
    cols_map = {
        'stck_bsop_date': 'date_str',
        'stck_cntg_hour': 'time_str',
        'stck_prpr': 'close',
        'stck_oprc': 'open',
        'stck_hgpr': 'high',
        'stck_lwpr': 'low',
        'cntg_vol': 'volume'
    }
    
    # 필요한 컬럼만 존재 시 이름 변경
    df.rename(columns=cols_map, inplace=True)
    df = df[list(cols_map.values())].copy()
    
    # 날짜+시간 병합 (YYYYMMDD + HHMMSS)
    df['date'] = pd.to_datetime(df['date_str'] + df['time_str'], format='%Y%m%d%H%M%S')
    
    # 수치형 변환
    numeric_cols = ['close', 'open', 'high', 'low', 'volume']
    for c in numeric_cols:
        df[c] = pd.to_numeric(df[c])
        
    df = df[['date', 'open', 'high', 'low', 'close', 'volume']]
    
    # 시간순 정렬 (과거 -> 현재)
    return df.sort_values('date', ascending=True).reset_index(drop=True)


def get_domestic_index_chart(code):
    """업종/지수 기간별 시세(일봉) 조회 (KIS API)"""
    def fetch_func():
        # 지수/업종 차트 조회 URL 및 TR_ID (실전/모의 동일)
        url_path = constants.API_URLS["DOMESTIC"]["QUOTATIONS"]["INDEX_CHART"]
        tr_id = "FHKUP03500100" 
        
        now = datetime.now()
        today = now.strftime("%Y%m%d")
        start_date = (now - timedelta(days=730)).strftime("%Y%m%d") # 2년치 조회
        
        all_items = []
        current_end_date = today
        
        retry_count = 0
        while len(all_items) < 300 and retry_count < 10:
            params = {
                "FID_COND_MRKT_DIV_CODE": "U",
                "FID_INPUT_ISCD": code,        
                "FID_INPUT_DATE_1": start_date,
                "FID_INPUT_DATE_2": current_end_date,
                "FID_PERIOD_DIV_CODE": "D"     
            }
            
            data = call_api(url_path, "domestic", "quotations", "index_chart", params=params, tr_id=tr_id, retries=0)
            
            if data.get('rt_cd') == '0':
                items = data.get('output2', [])
                if items:
                    all_items.extend(items)
                    last_date = items[-1]['stck_bsop_date']
                    current_end_date = (datetime.strptime(last_date, "%Y%m%d") - timedelta(days=1)).strftime("%Y%m%d")
                    retry_count += 1
                    time.sleep(0.1) 
                else:
                    break
            elif data.get('msg_cd') == 'EGW00201':
                time.sleep(0.5)
                retry_count += 1
            else:
                if not all_items:
                    logger.warning(f"[API] 지수({code}) 조회 실패: {data.get('msg1')} (Code: {data.get('msg_cd')})")
                break

        if all_items:
            df = pd.DataFrame(all_items)
            df.drop_duplicates(subset=['stck_bsop_date'], inplace=True)
            df = df[['stck_bsop_date', 'bstp_nmix_prpr', 'bstp_nmix_oprc', 'bstp_nmix_hgpr', 'bstp_nmix_lwpr', 'acml_vol']].copy()
            df.columns = ['date', 'close', 'open', 'high', 'low', 'volume']
            df = df.astype({'close': float, 'open': float, 'high': float, 'low': float, 'volume': float})
            return df.sort_values('date', ascending=True).reset_index(drop=True)

        return pd.DataFrame()

    return _get_cached_chart(code, is_overseas=False, is_index=True, fetch_func=fetch_func)

def get_domestic_index_price(code):
    """업종/지수 현재가 조회 (KIS API)"""
    cache_key = f"idx_price_{code}"
    cached = _get_micro_cache(cache_key)
    if cached: return cached

    url = constants.API_URLS["DOMESTIC"]["QUOTATIONS"]["INDEX_PRICE"]
    params = {
        "FID_COND_MRKT_DIV_CODE": "U",
        "FID_INPUT_ISCD": code
    }
    res = call_api(url, "domestic", "quotations", "index_price", params=params)
    if res.get('rt_cd') == '0':
        _set_micro_cache(cache_key, res)
    return res

def get_current_price_data(code, is_overseas):
    cache_key = f"cp_{code}_{is_overseas}"
    cached = _get_micro_cache(cache_key, ttl=3.0) # [수정] 실시간 시세 반영을 위해 캐시 유지 시간을 3초로 단축
    if cached: return cached

    if not is_overseas:
        res = call_api(constants.API_URLS["DOMESTIC"]["QUOTATIONS"]["PRICE"], "domestic", "quotations", "price", params={"fid_cond_mrkt_div_code": "J", "fid_input_iscd": code}, timeout=3)
        if res.get('rt_cd') == '0':
            # [추가] 액면분할 종목의 52주 고가/저가가 KIS API에서 원주가로 반환되는 스펙 한계 보정
            try:
                out = res['output']
                curr = float(out.get('stck_prpr', 0))
                w52h = float(out.get('w52_hgpr', 0))
                # 52주 고점과 현재가가 2.5배 이상 차이나면(액면분할 의심), 
                # 차트 데이터를 조회하여 52주 최고/최저가를 수정주가 기준으로 덮어씌움
                if curr > 0 and w52h > 0 and (w52h / curr) > 2.5:
                    df = get_chart_data(code, is_overseas=False)
                    if df is not None and not df.empty:
                        recent_df = df.tail(250)
                        real_h52 = recent_df['high'].max()
                        real_l52 = recent_df['low'].min()
                        if real_h52 > 0 and real_l52 > 0:
                            out['w52_hgpr'] = str(int(real_h52))
                            out['w52_lwpr'] = str(int(real_l52))
            except Exception as e:
                logger.debug(f"[API] 52주 고가 보정 중 오류: {e}")

            # [추가] NXT(대체거래소) 시세 조회 및 병합 (NX 코드 사용)
            out = res.get('output', {})
            try:
                nxt_url = constants.API_URLS["DOMESTIC"]["QUOTATIONS"]["PRICE"]
                nxt_res = call_api(nxt_url, "domestic", "quotations", "price", params={"fid_cond_mrkt_div_code": "NX", "fid_input_iscd": code}, timeout=2, retries=0)
                if nxt_res and nxt_res.get('rt_cd') == '0' and nxt_res.get('output'):
                    nxt_price = nxt_res['output'].get('stck_prpr')
                    if nxt_price and safe_int(nxt_price) > 0:
                        out['ats_prpr'] = str(nxt_price)
            except Exception as e:
                logger.debug(f"[API] NXT(대체거래소) 시세 조회 오류 (NX 코드 시도): {e}")

            _set_micro_cache(cache_key, res)
        return res
    
    if is_overseas:
        cached_ex = config.session.exchange_cache.get(code)
        exchanges = []
        if cached_ex: exchanges.append(cached_ex)
        for e in ["NAS", "NYS", "AMS", "NASD", "NYSE", "AMEX"]:
            if e not in exchanges: exchanges.append(e)
        
        for excd in exchanges:
            params = {"AUTH": "", "EXCD": excd, "SYMB": code}
            data = call_api(constants.API_URLS["OVERSEAS"]["QUOTATIONS"]["PRICE"], "overseas", "quotations", "price", params=params, timeout=3)
            if data.get('rt_cd') == '0':
                if float(data.get('output', {}).get('last', 0) or 0) > 0:
                    if cached_ex != excd: config.session.update_cache_and_save(code, excd)
                    
                    # [추가] 미국 주식 프리/애프터마켓 시세 실시간 반영 로직
                    # KIS 정규장 종가(last) 대신 TradingView/yfinance의 실시간 장외 최신가로 덮어쓰기
                    try:
                        fi = get_yf_fast_info(code, ttl=3.0) # [수정] 실시간 시세 갱신을 위해 TTL을 3초로 단축
                        if fi and fi.get('last_price'):
                            global_rt_price = float(fi['last_price'])
                            kis_regular_price = float(data['output'].get('last', 0))
                            
                            # KIS 종가와 글로벌 실시간 가격이 다르면 장외 거래 시세로 간주하여 반영
                            if global_rt_price > 0 and abs(global_rt_price - kis_regular_price) > 0.0001:
                                data['output']['last'] = str(global_rt_price)
                    except Exception as e:
                        logger.debug(f"[API] 해외주식 장외 시세 병합 오류: {e}")
                        
                    _set_micro_cache(cache_key, data)
                    return data
        res_err = {'rt_cd': '9999'}
        return res_err
    return {'rt_cd': '9999'}

def get_current_price(code, is_overseas):
    """현재가 단일 값 조회 (실패 시 0 반환)"""
    data = get_current_price_data(code, is_overseas)
    if data.get('rt_cd') == '0':
        output = data.get('output', {})
        if is_overseas:
            try:
                return float(output.get('last', 0))
            except Exception as e:
                logger.debug(f"get_current_price float cast error: {e}")
                return 0.0
        else:
            ats_val = output.get('ats_prpr')
            if ats_val and safe_int(ats_val) > 0:
                return safe_int(ats_val)
            return safe_int(output.get('stck_prpr'))
    return 0

def get_order_book(code, is_overseas=False):
    """호가창 데이터 조회 (최대 10호가)"""
    cache_key = f"ob_{code}_{is_overseas}"
    cached = _get_micro_cache(cache_key, ttl=2.0)
    if cached: return cached

    if not is_overseas:
        url_path = "uapi/domestic-stock/v1/quotations/inquire-asking-price-exp-ccn"
        params = {"FID_COND_MRKT_DIV_CODE": "J", "FID_INPUT_ISCD": code}
        res = call_api(url_path, "domestic", "quotations", "order_book", params=params, tr_id="FHKST01010200", timeout=3)
        if res.get('rt_cd') == '0':
            _set_micro_cache(cache_key, res)
        return res
    else:
        cached_ex = config.session.exchange_cache.get(code)
        exchanges = []
        if cached_ex: exchanges.append(cached_ex)
        for e in ["NASD", "NAS", "NYSE", "NYS", "AMEX", "AMS"]:
            if e not in exchanges: exchanges.append(e)
        
        url_path = "uapi/overseas-price/v1/quotations/inquire-asking-price"
        for excd in exchanges:
            params = {"AUTH": "", "EXCD": excd, "SYMB": code}
            res = call_api(url_path, "overseas", "quotations", "order_book", params=params, tr_id="HHDFS76200200", timeout=3)
            if res.get('rt_cd') == '0':
                out = res.get('output1', {})
                if out and (float(out.get('pask1', 0)) > 0 or float(out.get('pbid1', 0)) > 0):
                    if cached_ex != excd: config.session.update_cache_and_save(code, excd)
                    _set_micro_cache(cache_key, res)
                    return res
        return {'rt_cd': '9999'}

def get_investor_trend(code, market_div="J"):
    cache_key = f"inv_{code}_{market_div}"
    cached = _get_micro_cache(cache_key, ttl=60.0) # [수정] 수급 정보 유지 시간 연장
    if cached is not None: return cached

    # [수정] 업종(지수)인 경우 별도 TR_ID(FHPTJ04040000) 및 URL 사용
    action = "investor"
    url = constants.API_URLS["DOMESTIC"]["QUOTATIONS"]["INVESTOR"]
    params = {"FID_COND_MRKT_DIV_CODE": market_div, "FID_INPUT_ISCD": code}

    if market_div == "U":
        # 1. 일별 추이 조회 (FHPTJ04040000) 시도
        action = "index_investor"
        url = "uapi/domestic-stock/v1/quotations/inquire-investor-daily-by-market"
        
        today = datetime.now().strftime("%Y%m%d")
        start_date = (datetime.now() - timedelta(days=7)).strftime("%Y%m%d") # 기간 확대
        params.update({
            "FID_INPUT_DATE_1": start_date,
            "FID_INPUT_DATE_2": today,
            "FID_PERIOD_DIV_CODE": "D"
        })
        
        if config.FILE_DEBUG_LEVEL == "DEBUG":
            logger.debug(f"[API] get_investor_trend(Daily) Req: {code}, Params={params}")

        data = call_api(url, "domestic", "quotations", action, params=params)
        
        if data.get('rt_cd') == '0':
            output = data.get('output', [])
            if output:
                _set_micro_cache(cache_key, output)
                return output
            
        # 2. 실패/빈값 시 현재가 투자자 조회 (FHKUP01010900) Fallback
        if config.FILE_DEBUG_LEVEL == "DEBUG":
            logger.debug(f"[API] get_investor_trend(Daily) Empty/Fail. Trying Current Trend Fallback.")
            
        action = "index_investor_current"
        url = constants.API_URLS["DOMESTIC"]["QUOTATIONS"]["INDEX_INVESTOR_CURRENT"]
        params = {"FID_COND_MRKT_DIV_CODE": market_div, "FID_INPUT_ISCD": code}

    # 주식(J)이거나 업종(U) Fallback 실행
    data = call_api(url, "domestic", "quotations", action, params=params)
    
    if data.get('rt_cd') == '0':
        output = data.get('output', [])
        # [수정] output 키가 없거나 비어있을 경우 output1, output2 등 대체 키 확인 (지수 조회 시 필드명이 다를 수 있음)
        if not output: output = data.get('output1', [])
        if not output: output = data.get('output2', [])
        
        # [추가] output이 dict인 경우 list로 변환 (market.py 호환성)
        if isinstance(output, dict):
            output = [output]
        
        _set_micro_cache(cache_key, output)
        return output
            
    return []

def get_daily_foreign_rate(code):
    """주식 일자별 시세 (최근 30일, 외인소진율 포함) 조회"""
    url = constants.API_URLS["DOMESTIC"]["QUOTATIONS"]["DAILY_PRICE"]
    params = {
        "FID_COND_MRKT_DIV_CODE": "J",
        "FID_INPUT_ISCD": code,
        "FID_PERIOD_DIV_CODE": "D",
        "FID_ORG_ADJ_PRC": "0"
    }
    data = call_api(url, "domestic", "quotations", "daily_price", params=params, tr_id="FHKST01010400", timeout=3)
    if data.get('rt_cd') == '0':
        return data.get('output', [])
    return []

def get_realtime_vol_strength(code, is_overseas=False, exchange_code=None):
    if is_overseas: return None

    cache_key = f"vol_{code}"
    cached = _get_micro_cache(cache_key, ttl=3.0) # [수정] 체결강도의 실시간성 확보를 위해 캐시 유지 시간을 3초로 단축
    if cached is not None: return cached
    
    final_vol = None
    
    for attempt in range(3):
        # [수정] Timeout을 2초에서 3초로 늘려 로그에 나타난 ReadTimeoutError 빈도 완화
        data = call_api(constants.API_URLS["DOMESTIC"]["QUOTATIONS"]["VOL_STRENGTH"], "domestic", "quotations", "vol_strength", params={"FID_COND_MRKT_DIV_CODE": "J", "FID_INPUT_ISCD": code}, timeout=3, retries=0)
        if data.get('rt_cd') == '0':
            items = data.get('output', [])
            if items:
                # [추가] 체결강도 HTS 괴리 분석을 위한 원본(Raw) 데이터 정밀 추적 로그
                if config.FILE_DEBUG_LEVEL in ["DEBUG", "TRACE"]:
                    logger.debug(f"[VOL_STRENGTH_RAW_DATA] [{code}] Attempt {attempt+1} | Raw Output[0]: {json.dumps(items[0], ensure_ascii=False)}")
                    
                tday_rltv = items[0].get('tday_rltv')
                if tday_rltv and str(tday_rltv).strip():
                    try:
                        valid_val = float(str(tday_rltv).replace(',', ''))
                        if config.FILE_DEBUG_LEVEL in ["DEBUG", "TRACE"]:
                            logger.debug(f"[VOL_STRENGTH_PARSED] [{code}] Extracted Value: {valid_val}%")
                        final_vol = valid_val
                    except Exception as e:
                        if config.FILE_DEBUG_LEVEL in ["DEBUG", "TRACE"]: logger.debug(f"[VOL_STRENGTH_ERROR] [{code}] Parse Error: {e}")
                        pass
        elif data.get('msg_cd') == 'EGW00201': time.sleep(0.2)
        else: time.sleep(0.2)
        
        if final_vol is not None:
            break
            
    # [추가] NXT(대체거래소) 체결강도 조회 및 병합 (NX 코드 사용)
    try:
        nxt_data = call_api(constants.API_URLS["DOMESTIC"]["QUOTATIONS"]["VOL_STRENGTH"], "domestic", "quotations", "vol_strength", params={"FID_COND_MRKT_DIV_CODE": "NX", "FID_INPUT_ISCD": code}, timeout=2, retries=0)
        if nxt_data and nxt_data.get('rt_cd') == '0':
            nxt_items = nxt_data.get('output', [])
            if nxt_items:
                nxt_tday_rltv = nxt_items[0].get('tday_rltv')
                if nxt_tday_rltv and str(nxt_tday_rltv).strip():
                    nxt_vol = float(str(nxt_tday_rltv).replace(',', ''))
                    if nxt_vol > 0:
                        final_vol = nxt_vol
    except Exception as e:
        logger.debug(f"[API] NXT(대체거래소) 체결강도 조회 오류 (NX 코드 시도): {e}")

    if final_vol is not None:
        _set_micro_cache(cache_key, final_vol)
        return final_vol
        
    return None

def fetch_overseas_detail_price(code, excd):
    cache_key = f"detail_{code}"
    cached = _get_micro_cache(cache_key, ttl=60.0) # [수정] 상세 정보 유지 시간 연장
    if cached is not None: return cached

    exchanges = []
    if excd: exchanges.append(excd)
    for e in ["NASD", "NAS", "NYSE", "NYS", "AMEX", "AMS"]:
        if e not in exchanges: exchanges.append(e)

    for target_excd in exchanges:
        params = {"AUTH": "", "EXCD": target_excd, "SYMB": code}
        data = call_api(constants.API_URLS["OVERSEAS"]["QUOTATIONS"]["DETAIL"], "overseas", "quotations", "detail", params=params, timeout=3)
        if data.get('rt_cd') == '0':
            output = data.get('output', {})
            if output.get('h52p') and float(output.get('h52p')) > 0:
                if target_excd != excd: config.session.update_cache_and_save(code, target_excd)
                _set_micro_cache(cache_key, output)
                return output
    return {}

def fetch_domestic_period_price(code, days=100):
    """국내 주식 기간별 시세 조회 (기본 100일, 단일 호출 최대 약 100건 반환)"""
    today = datetime.now().strftime("%Y%m%d")
    past = (datetime.now() - timedelta(days=days)).strftime("%Y%m%d")
    
    params = {"FID_COND_MRKT_DIV_CODE": "J", "FID_INPUT_ISCD": code, "FID_INPUT_DATE_1": past, "FID_INPUT_DATE_2": today, "FID_PERIOD_DIV_CODE": "D", "FID_ORG_ADJ_PRC": "0"}
    data = call_api(constants.API_URLS["DOMESTIC"]["QUOTATIONS"]["CHART"], "domestic", "quotations", "chart", params=params)
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
        data = call_api(constants.API_URLS["OVERSEAS"]["QUOTATIONS"]["CHART"], "overseas", "quotations", "chart", params=params, timeout=5)
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
    if not config.session.is_simulation and getattr(context.trade_context, 'use_auto_account', False) and config.session.auto_cano:
        cano = config.session.auto_cano
        acnt_prdt_cd = config.session.auto_acnt_prdt_cd

    params = {"CANO": cano, "ACNT_PRDT_CD": acnt_prdt_cd, "PDNO": stock_code, "ORD_UNPR": str(price), "ORD_DVSN": "00" if price > 0 else "01", "CMA_EVLU_AMT_ICLD_YN": "N", "OVRS_ICLD_YN": "N", "CRDT_TYPE": "00"}
    data = call_api(constants.API_URLS["DOMESTIC"]["INQUIRY"]["BUYABLE"], "domestic", "inquiry", "buyable", params=params, timeout=5)
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
    if not config.session.is_simulation and getattr(context.trade_context, 'use_auto_account', False) and config.session.auto_cano:
        cano = config.session.auto_cano
        acnt_prdt_cd = config.session.auto_acnt_prdt_cd

    params = {"CANO": cano, "ACNT_PRDT_CD": acnt_prdt_cd, "AFHR_FLPR_YN": "N", "OFL_YN": "N", "INQR_DVSN": "01", "UNPR_DVSN": "01", "FUND_STTL_ICLD_YN": "N", "FNCG_AMT_AUTO_RDPT_YN": "N", "PRCS_DVSN": "00", "CTX_AREA_FK100": "", "CTX_AREA_NK100": ""}
    data = call_api(constants.API_URLS["DOMESTIC"]["INQUIRY"]["SELLABLE"], "domestic", "inquiry", "sellable", params=params)
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
    if not config.session.is_simulation and getattr(context.trade_context, 'use_auto_account', False) and config.session.auto_cano:
        cano = config.session.auto_cano
        acnt_prdt_cd = config.session.auto_acnt_prdt_cd
        
    params = {"CANO": cano, "ACNT_PRDT_CD": acnt_prdt_cd, "OVRS_EXCG_CD": trade_excd, "OVRS_ORD_UNPR": str(price), "ITEM_CD": stock_code}
    data = call_api(constants.API_URLS["OVERSEAS"]["INQUIRY"]["BUYABLE"], "overseas", "inquiry", "buyable", params=params)
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
    
    # [수정] 실전/모의 모두 모든 거래소 확인 (종목별 상장 거래소가 다를 수 있음)
    trade_excds = []
    trade_excds.append(primary_excd)
    for e in ["NASD", "NYSE", "AMEX"]:
        if e != primary_excd: trade_excds.append(e)
    
    # [수정] 컨텍스트에 따른 계좌번호 선택
    cano = config.session.cano
    acnt_prdt_cd = config.session.acnt_prdt_cd
    if not config.session.is_simulation and getattr(context.trade_context, 'use_auto_account', False) and config.session.auto_cano:
        cano = config.session.auto_cano
        acnt_prdt_cd = config.session.auto_acnt_prdt_cd

    for target_excd in trade_excds:
        params = {"CANO": cano, "ACNT_PRDT_CD": acnt_prdt_cd, "OVRS_EXCG_CD": target_excd, "TR_CRCY_CD": "USD", "CTX_AREA_FK100": "", "CTX_AREA_NK100": "", "CTX_AREA_FK200": "", "CTX_AREA_NK200": ""}
        data = call_api(constants.API_URLS["OVERSEAS"]["INQUIRY"]["BALANCE"], "overseas", "inquiry", "sellable", params=params)
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
        data = call_api(constants.API_URLS["OVERSEAS"]["QUOTATIONS"]["PRICE"], "overseas", "quotations", "price", params=params)
        if data.get('rt_cd') == '0' and float(str(data.get('output', {}).get('last', '0')).strip() or 0) > 0:
            config.session.update_cache_and_save(stock_code, excd)
            return excd
    return None

def _prepare_account_params(cano, acnt_prdt_cd):
    """계좌 파라미터 준비 및 컨텍스트 설정 (내부 헬퍼)"""
    # 인자가 없으면 현재 설정/컨텍스트 값 사용
    if not cano:
        if not config.session.is_simulation and getattr(context.trade_context, 'use_auto_account', False) and config.session.auto_cano:
            cano = config.session.auto_cano
            acnt_prdt_cd = config.session.auto_acnt_prdt_cd
        else:
            cano = config.session.cano
            acnt_prdt_cd = config.session.acnt_prdt_cd
    
    # 요청 계좌가 자동매매 계좌와 일치하면 컨텍스트 전환 (토큰/Key 변경)
    if not config.session.is_simulation and cano == config.session.auto_cano and config.session.auto_app_key:
        context.trade_context.use_auto_account = True
    elif not config.session.is_simulation and cano == config.session.cano:
        context.trade_context.use_auto_account = False
        
    return cano, acnt_prdt_cd

def get_domestic_balance(cano=None, acnt_prdt_cd=None, retries=None):
    """국내 주식 잔고 조회"""
    cano, acnt_prdt_cd = _prepare_account_params(cano, acnt_prdt_cd)
    
    # [수정] 조회 구분: 모의투자는 '02'(종목별), 실전투자는 '01'(대출일별 - API 제한 대응)
    inqr_dvsn = "02" if config.session.is_simulation else "01"
    
    params = {"CANO": cano, "ACNT_PRDT_CD": acnt_prdt_cd, "AFHR_FLPR_YN": "N", "OFL_YN": "N", "INQR_DVSN": inqr_dvsn, "UNPR_DVSN": "01", "FUND_STTL_ICLD_YN": "N", "FNCG_AMT_AUTO_RDPT_YN": "N", "PRCS_DVSN": "00", "CTX_AREA_FK100": "", "CTX_AREA_NK100": ""}
    data = call_api(constants.API_URLS["DOMESTIC"]["INQUIRY"]["BALANCE"], "domestic", "inquiry", "balance", params=params, retries=retries)

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
        
        return output1, output2
    
    # [추가] 실패 시 로그 출력
    msg = f"잔고 조회 실패: {data.get('msg1')} ({data.get('msg_cd')})"
    logger.debug(f"{msg}")
    if hasattr(context, 'SYSTEM_LOGGER') and context.SYSTEM_LOGGER:
        context.SYSTEM_LOGGER(f"[API] {msg}")
        
    return None, None

def get_overseas_balance(cano=None, acnt_prdt_cd=None, retries=None):
    """해외 주식 잔고 조회"""
    cano, acnt_prdt_cd = _prepare_account_params(cano, acnt_prdt_cd)
    target_exchanges = ["NASD", "NYSE", "AMEX"]
    all_holdings = []
    
    for exc in target_exchanges:
        if config.session.is_simulation: time.sleep(0.2)
        params = {"CANO": cano, "ACNT_PRDT_CD": acnt_prdt_cd, "OVRS_EXCG_CD": exc, "TR_CRCY_CD": "USD", "CTX_AREA_FK100": "", "CTX_AREA_NK100": "", "CTX_AREA_FK200": "", "CTX_AREA_NK200": ""}
        data = call_api(constants.API_URLS["OVERSEAS"]["INQUIRY"]["BALANCE"], "overseas", "inquiry", "balance", params=params, retries=retries)
        
        # Rate Limit 발생 시 잠시 대기 후 재시도 (call_api 내부 재시도와 별개로 루프 내 처리)
        if data.get('msg_cd') == 'EGW00201':
            time.sleep(0.5)
            data = call_api(constants.API_URLS["OVERSEAS"]["INQUIRY"]["BALANCE"], "overseas", "inquiry", "balance", params=params)

        if data.get('rt_cd') == '0':
            for item in data.get('output1', []):
                if '_exchange' not in item: item['_exchange'] = exc
                all_holdings.append(item)
                
    return all_holdings

def get_today_profit_summary(cano=None, acnt_prdt_cd=None, target_date=None):
    """금일 투자 손익 요약 조회"""
    # [수정] 모의투자 서버는 기간별 손익 조회(TTTC8494R/VTTC8494R)를 지원하지 않음 (OPSQ0002 에러 발생)
    # 따라서 모의투자일 경우 API 호출을 생략하고 빈 값 반환하여 에러 로그 방지
    if config.session.is_simulation:
        return {'rt_cd': '0', 'output2': []}

    cano, acnt_prdt_cd = _prepare_account_params(cano, acnt_prdt_cd)
    today = target_date if target_date else datetime.now().strftime("%Y%m%d")
    params = {
        "CANO": cano, "ACNT_PRDT_CD": acnt_prdt_cd,
        "INQR_STRT_DT": today, "INQR_END_DT": today,
        "SLL_BUY_DVSN_CD": "00", "INQR_DVSN": "00", 
        "PDNO": "", "CTX_AREA_FK100": "", "CTX_AREA_NK100": "",
        "AFHR_FLPR_YN": "N", "OFL_YN": "N", "UNPR_DVSN": "01",          
        "FUND_STTL_ICLD_YN": "N", "FNCG_AMT_AUTO_RDPT_YN": "N", "PRCS_DVSN": "00",
        "COST_ICLD_YN": "Y" 
    }
    return call_api(constants.API_URLS["DOMESTIC"]["INQUIRY"]["PROFIT"], "domestic", "inquiry", "profit", params=params)

def get_today_history(cano=None, acnt_prdt_cd=None, retries=None, target_date=None):
    """금일 체결 내역 조회"""
    cano, acnt_prdt_cd = _prepare_account_params(cano, acnt_prdt_cd)
    today = target_date if target_date else datetime.now().strftime("%Y%m%d")
    
    # [수정] 주식일별주문체결조회 (inquire-daily-ccld) 사용
    # 실전: TTTC8001R, 모의: VTTC8001R
    url = constants.API_URLS["DOMESTIC"]["INQUIRY"]["HISTORY"]
    tr_id = "VTTC8001R" if config.session.is_simulation else "TTTC8001R"
    
    params = {
        "CANO": cano,
        "ACNT_PRDT_CD": acnt_prdt_cd,
        "INQR_STRT_DT": today,
        "INQR_END_DT": today,
        "SLL_BUY_DVSN_CD": "00",
        "INQR_DVSN": "00",
        "PDNO": "",
        "CCLD_DVSN": "00", # [수정] 00: 전체 조회 (취소/미체결 포함)
        "ORD_GNO_BRNO": "",
        "ODNO": "",
        "INQR_DVSN_3": "00",
        "INQR_DVSN_1": "",
        "CTX_AREA_FK100": "",
        "CTX_AREA_NK100": ""
    }
    
    return call_api(url, "domestic", "inquiry", "history", params=params, retries=retries, tr_id=tr_id)

def get_overseas_today_history(cano=None, acnt_prdt_cd=None, retries=None, target_date=None):
    """금일 해외주식 체결 내역 조회"""
    cano, acnt_prdt_cd = _prepare_account_params(cano, acnt_prdt_cd)
    today = target_date if target_date else datetime.now().strftime("%Y%m%d")
    
    url = constants.API_URLS["OVERSEAS"]["INQUIRY"]["HISTORY"]
    tr_id = "VTTS3035R" if config.session.is_simulation else "TTTS3035R"
    
    # [Fix] OVRS_EXCG_CD는 필수 입력값이므로, 거래소별로 순회하며 조회 후 병합
    all_trades = []
    final_res = {'rt_cd': '0', 'output': []} # 성공 시 반환할 기본 구조

    for excg_cd in ["NASD", "NYSE", "AMEX"]:
        params = {
            "CANO": cano,
            "ACNT_PRDT_CD": acnt_prdt_cd,
            "OVRS_EXCG_CD": excg_cd, # [Fix] 거래소 코드 추가
            "PDNO": "%",
            "ORD_STRT_DT": today,
            "ORD_END_DT": today,
            "SLL_BUY_DVSN": "00",
            "CCLD_NCCS_DVSN": "00", # [수정] 00: 전체 조회 (취소/미체결 포함)
            "CTX_AREA_FK200": "",
            "CTX_AREA_NK200": "",
            "ORD_DT": "",
            "ORD_GNO_BRNO": "",
            "ODNO": ""
        }
        
        # [Fix] 모의투자 환경에서 SORT_SQN 누락 시 에러(OPSQ2001) 발생 대응
        params["SORT_SQN"] = "01" if not config.session.is_simulation else ""
        
        res = call_api(url, "overseas", "inquiry", "history", params=params, retries=retries, tr_id=tr_id)
        if res.get('rt_cd') == '0':
            all_trades.extend(res.get('output', []))
        elif res.get('rt_cd') != '1': # '1'은 데이터 없음이므로 에러가 아님
            return res # 실제 에러 발생 시 즉시 반환

    final_res['output'] = all_trades
    return final_res

def get_unfilled_orders(cano=None, acnt_prdt_cd=None):
    """미체결 내역 조회 (국내주식) - get_domestic_open_orders의 Alias"""
    return get_domestic_open_orders(cano, acnt_prdt_cd)

def get_domestic_open_orders(cano=None, acnt_prdt_cd=None):
    """국내주식 미체결 내역 조회 (모의/실전 분기 처리)"""
    cano, acnt_prdt_cd = _prepare_account_params(cano, acnt_prdt_cd)
    
    if config.session.is_simulation:
        # [수정] 모의투자: 주식일별주문체결조회(VTTC8001R) 사용하여 미체결(02) 조회
        # 모의투자 환경에서 주식정정취소가능주문조회(VTTC8036R) 미지원 이슈 대응
        # [주의] 모의투자 API 버그로 인해 실제 미체결이 있어도 데이터가 반환되지 않는 경우가 많음
        #       -> AutoTrader의 manage_unfilled_orders에서 로컬 상태 기반 강제 취소 로직으로 보완 중
        url = constants.API_URLS["DOMESTIC"]["INQUIRY"]["HISTORY"]
        tr_id = "VTTC8001R"
        today = datetime.now().strftime("%Y%m%d")
        
        params = {
            "CANO": cano,
            "ACNT_PRDT_CD": acnt_prdt_cd,
            "INQR_STRT_DT": today,
            "INQR_END_DT": today,
            "SLL_BUY_DVSN_CD": "00",
            "INQR_DVSN": "00",
            "PDNO": "",
            "CCLD_DVSN": "02", # [수정] 모의투자는 02(미체결)로 조회해야 정상 반환됨
            "ORD_GNO_BRNO": "",
            "ODNO": "",
            "INQR_DVSN_3": "00",
            "INQR_DVSN_1": "",
            "CTX_AREA_FK100": "",
            "CTX_AREA_NK100": ""
        }
        
        res = call_api(url, "domestic", "inquiry", "history", params=params, tr_id=tr_id)
        
        if res.get('rt_cd') == '0':
            # [추가] 전체 내역 중 미체결 잔량이 있는 주문만 필터링
            all_orders = res.get('output1', [])
            unfilled = []
            for order in all_orders:
                if int(order.get('rmn_qty', 0)) > 0:
                    unfilled.append(order)
            return unfilled
        return []

    else:
        # [수정] 실전투자: 주식정정취소가능주문조회(TTTC8036R) 사용
        url = constants.API_URLS["DOMESTIC"]["INQUIRY"]["OPEN_ORDERS"]
        tr_id = "TTTC8036R"
        
        params = {
            "CANO": cano, "ACNT_PRDT_CD": acnt_prdt_cd, 
            "CTX_AREA_FK100": "", "CTX_AREA_NK100": "", 
            "INQR_DVSN_1": "0", "INQR_DVSN_2": "0"
        }
        
        res = call_api(url, "domestic", "inquiry", "open_orders", params=params, tr_id=tr_id)
        
        if res.get('rt_cd') == '0':
            return res.get('output', [])
            
    return []

def get_overseas_open_orders(cano=None, acnt_prdt_cd=None):
    """해외주식 미체결 내역 조회"""
    cano, acnt_prdt_cd = _prepare_account_params(cano, acnt_prdt_cd)
    all_orders = []
    # [수정] 실전 투자 시에도 모든 거래소 조회 (NYSE, AMEX 누락 방지)
    # 단, API 호출 횟수가 늘어나므로 Rate Limit 주의 필요
    target_exchanges = ["NASD", "NYSE", "AMEX"]
    
    for exc in target_exchanges:
        params = {
            "CANO": cano, "ACNT_PRDT_CD": acnt_prdt_cd,
            "OVRS_EXCG_CD": exc, 
            "CTX_AREA_FK100": "", "CTX_AREA_NK100": "", "CTX_AREA_FK200": "", "CTX_AREA_NK200": ""
        }
        # [Fix] 모의투자 서버는 SORT_SQN 파라미터가 없으면 에러가 발생할 수 있으므로 빈 값으로 전송
        params["SORT_SQN"] = "01" if not config.session.is_simulation else ""
            
        res = call_api(constants.API_URLS["OVERSEAS"]["INQUIRY"]["OPEN_ORDERS"], "overseas", "inquiry", "open_orders", params=params)
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
        url_path = constants.API_URLS["DOMESTIC"]["TRADING"][action.upper()]
        category = "trade"
            
        data = {
            "CANO": cano, "ACNT_PRDT_CD": acnt, 
            "PDNO": code, "ORD_DVSN": ord_dvsn, 
            "ORD_QTY": str(qty), "ORD_UNPR": str(price)
        }
        
        # [추가] 모의투자가 아닐 경우 SOR (최적주문집행) 거래소 코드 적용
        if not config.session.is_simulation:
            data["EXCG_ID_DVSN_CD"] = "SOR"
    else: # overseas
        # [Fix] 해외 주문 시 거래소 코드 보정 (3자리 -> 4자리)
        trade_excd = exchange_code
        if exchange_code == "NAS": trade_excd = "NASD"
        elif exchange_code == "NYS": trade_excd = "NYSE"
        elif exchange_code == "AMS": trade_excd = "AMEX"

        url_path = constants.API_URLS["OVERSEAS"]["TRADING"]["ORDER"]
        category = "trade"
        data = {
            "CANO": cano, "ACNT_PRDT_CD": acnt, 
            "OVRS_EXCG_CD": trade_excd, "PDNO": code, 
            "ORD_QTY": str(qty), "OVRS_ORD_UNPR": str(price), 
            "ORD_SVR_DVSN_CD": "0", "ORD_DVSN": ord_dvsn
        }

    return call_api(url_path, market, category, action, data=data, method="POST")

def revise_cancel_order(market, action, org_no, code, qty, price, type_cd, ord_dvsn, exchange_code=None):
    """
    정정/취소 통합 함수
    action: "modify" (정정) or "cancel" (취소)
    type_cd: "01"(정정), "02"(취소) - API 스펙상 구분 코드
    """
    cano, acnt = _prepare_account_params(None, None)
    
    if market == "domestic":
        url_path = constants.API_URLS["DOMESTIC"]["TRADING"]["REVISE_CANCEL"]
        category = "modify"
            
        qty_all_yn = "Y" if qty == 0 else "N" # 0이면 전량으로 간주 (호출부 로직에 따름)
        data = {"CANO": cano, "ACNT_PRDT_CD": acnt, "KRX_FWDG_ORD_ORGNO": "", "ORGN_ODNO": org_no, "ORD_DVSN": ord_dvsn, "RVSE_CNCL_DVSN_CD": type_cd, "ORD_QTY": str(qty), "ORD_UNPR": str(price), "QTY_ALL_ORD_YN": qty_all_yn}
        
        # [추가] 모의투자가 아닐 경우 SOR (최적주문집행) 거래소 코드 적용
        if not config.session.is_simulation:
            data["EXCG_ID_DVSN_CD"] = "SOR"
    else: # overseas
        # [Fix] 해외 주문 정정/취소 시 거래소 코드 보정
        trade_excd = exchange_code
        if exchange_code == "NAS": trade_excd = "NASD"
        elif exchange_code == "NYS": trade_excd = "NYSE"
        elif exchange_code == "AMS": trade_excd = "AMEX"

        url_path = constants.API_URLS["OVERSEAS"]["TRADING"]["REVISE_CANCEL"]
        data = {"CANO": cano, "ACNT_PRDT_CD": acnt, "OVRS_EXCG_CD": trade_excd, "PDNO": code, "ORGN_ODNO": org_no, "RVSE_CNCL_DVSN_CD": type_cd, "ORD_QTY": str(qty), "OVRS_ORD_UNPR": str(price)}
        category = "modify"
    
    # action 파라미터는 TR_ID 조회를 위해 사용됨 (modify/cancel)
    return call_api(url_path, market, category, action, data=data, method="POST")

def get_deposit(cano=None, acnt_prdt_cd=None, retries=None):
    """예수금(주문가능현금) 조회 (국내/모의)"""
    cano, acnt_prdt_cd = _prepare_account_params(cano, acnt_prdt_cd)
    params = {
        "CANO": cano, "ACNT_PRDT_CD": acnt_prdt_cd, 
        "PDNO": "005930", "ORD_UNPR": "0", "ORD_DVSN": "01", 
        "CMA_EVLU_AMT_ICLD_YN": "Y", "OVRS_ICLD_YN": "Y", "CRDT_TYPE": "00"
    }
    # [수정] TR_ID 명시적 지정 (로그상 CTRP6548R이 호출되고 있어 TTTC8908R로 교정)
    tr_id = "VTTC8908R" if config.session.is_simulation else "TTTC8908R"
    return call_api(constants.API_URLS["DOMESTIC"]["INQUIRY"]["BUYABLE"], "domestic", "inquiry", "deposit", params=params, retries=retries, tr_id=tr_id)

def get_foreign_deposit(cano=None, acnt_prdt_cd=None, retries=None):
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
    return call_api(constants.API_URLS["DOMESTIC"]["INQUIRY"]["DEPOSIT"], "domestic", "inquiry", "deposit", params=params, retries=retries)

def get_deposit_balance(cano=None, acnt_prdt_cd=None, skip_balance_check=False, retries=None):
    """예수금 및 자산 현황 조회 (모의/실전 자동 분기)"""
    cano, acnt_prdt_cd = _prepare_account_params(cano, acnt_prdt_cd)
    res = {"deposit": 0, "foreign_deposit": 0, "withdraw": 0, "d2_deposit": 0, "order_possible": 0, "d2_real": 0}
    success = False # [추가] 조회 성공 여부 플래그

    if config.session.is_simulation:
        # [수정] 모의투자: 주식잔고조회(VTTC8434R)를 우선 사용하여 예수금 확인 (더 안정적)
        # skip_balance_check가 True이면(이미 외부에서 조회했다면) 건너뜀
        try:
            import mem_diag; mem_diag.log_event("deposit:before-balance-call")
        except Exception: pass
        holdings, summary_list = ([], []) if skip_balance_check else get_domestic_balance(cano, acnt_prdt_cd, retries=retries)
        try:
            import mem_diag; mem_diag.log_event("deposit:after-balance-call")
        except Exception: pass

        if summary_list and len(summary_list) > 0:
            summary = summary_list[0]
            res['deposit'] = int(float(summary.get('dnca_tot_amt', 0)))
            res['d2_deposit'] = int(float(summary.get('prvs_rcdl_excc_amt', 0)))
            
            # [추가] 모의투자에서 D+2 예수금이 0인 경우 일반 예수금으로 대체 (데이터 누락 대응)
            if res['d2_deposit'] == 0 and res['deposit'] > 0:
                res['d2_deposit'] = res['deposit']
                
            res['withdraw'] = res['d2_deposit']
            
            # [수정] 모의투자는 D+2 예수금을 주문가능금액으로 설정 (별도 API 호출 생략 시)
            res['order_possible'] = res['d2_deposit']
            success = True
        
        # [수정] 모의투자도 주문가능금액 상세 조회 (VTTC8908R) 수행하여 정확한 값(nrcvb_buy_amt) 확인
        try:
            import mem_diag; mem_diag.log_event("deposit:before-get_deposit-call")
        except Exception: pass
        data_order = get_deposit(cano, acnt_prdt_cd, retries=retries)
        try:
            import mem_diag; mem_diag.log_event("deposit:after-get_deposit-call")
        except Exception: pass
        if data_order.get('rt_cd') == '0':
            output = data_order.get('output', {})
            # 모의투자는 nrcvb_buy_amt(미수없는매수금액) 필드가 실질적인 주문가능금액
            ord_psbl = safe_int(output.get('nrcvb_buy_amt')) or safe_int(output.get('ord_psbl_amt'))
            if ord_psbl > 0:
                res['order_possible'] = ord_psbl
            
            cash = safe_int(output.get('ord_psbl_cash'))
            if cash > 0:
                res['withdraw'] = cash
                # 잔고조회에서 값을 못 가져왔다면 채워넣기
                if res['deposit'] == 0: res['deposit'] = cash
                if res['d2_deposit'] == 0: res['d2_deposit'] = cash
            
            success = True
        elif not success:
            # 잔고조회도 실패하고 주문가능금액 조회도 실패한 경우
            logger.warning(f"모의투자 예수금 조회 실패: {data_order.get('msg1')} ({data_order.get('msg_cd')})")
            # 잔고 조회(get_domestic_balance)에서 가져온 d2_deposit이 있다면 이를 deposit으로 대체 사용
            if res['d2_deposit'] > 0:
                res['deposit'] = res['d2_deposit']
                logger.info(f"[API] 모의투자 예수금 조회 폴백: D+2 예수금({res['d2_deposit']:,}원)을 사용합니다.")
                success = True
    else:
        # [수정] 실전투자: 주문가능금액(get_deposit)과 계좌잔고(get_foreign_deposit) 모두 조회하여 병합
        # 1. 주문가능금액 조회 (주문가능금액, 출금가능금액)
        data_order = get_deposit(cano, acnt_prdt_cd, retries=retries)
        
        if data_order.get('rt_cd') == '0':
            out = data_order.get('output', {})
            # [수정] 실전투자 주문가능금액: ord_psbl_amt가 없으면 nrcvb_buy_amt(미수없는매수금액) 사용
            res['order_possible'] = safe_int(out.get('ord_psbl_amt')) or safe_int(out.get('nrcvb_buy_amt'))
            logger.info(f"[API] 주문가능금액 조회 성공: {res['order_possible']:,}원 (TR_ID: TTTC8908R)")
            res['withdraw'] = safe_int(out.get('ord_psbl_cash')) # 출금가능은 현금 기준
            # 예수금 정보가 없을 경우 주문가능현금으로 대체
            res['deposit'] = safe_int(out.get('ord_psbl_cash'))
            success = True
        else:
            logger.warning(f"[API] 주문가능금액 조회 실패: {data_order.get('msg1')} (Code: {data_order.get('msg_cd')})")

        # 2. 주식 잔고 조회 (예수금, D+2 가수도) - get_domestic_balance 활용
        # get_foreign_deposit 대신 더 안정적인 get_domestic_balance 사용
        holdings, summary_list = get_domestic_balance(cano, acnt_prdt_cd, retries=retries)
        if summary_list and len(summary_list) > 0:
            summary = summary_list[0]
            res['deposit'] = int(float(summary.get('dnca_tot_amt', 0))) # 예수금 (우선)
            res['d2_real'] = int(float(summary.get('prvs_rcdl_excc_amt', 0))) # D+2 가수도 (우선)
            
            # [추가] Fallback: 주문가능금액 조회 실패 시 D+2 가수도 사용
            if res['order_possible'] == 0:
                res['order_possible'] = res['d2_real']
            
            # [추가] Fallback: 출금가능금액 조회 실패 시 예수금 사용
            if res['withdraw'] == 0:
                res['withdraw'] = res['deposit']
                
            success = True

        # 3. 외화 잔고 조회 (보조)
        data_foreign = get_foreign_deposit(cano, acnt_prdt_cd, retries=retries)
        if data_foreign.get('rt_cd') == '0' and data_foreign.get('output2'):
            out2 = data_foreign['output2'][0] if isinstance(data_foreign['output2'], list) else data_foreign['output2']
            res['foreign_deposit'] = int(float(out2.get('frcr_evlu_tota', 0)))
            
            # [추가] 계좌잔고평가 API의 D+2 가수도금(prvs_rcdl_excc_amt)이 더 정확할 수 있음 (매도 대금 반영 등)
            d2_account_val = int(float(out2.get('prvs_rcdl_excc_amt', 0)))
            if d2_account_val > res['d2_real']:
                res['d2_real'] = d2_account_val
                
            # [추가] 예수금도 확인하여 더 큰 값 사용
            deposit_account_val = int(float(out2.get('dnca_tot_amt', 0)))
            if deposit_account_val > res['deposit']:
                res['deposit'] = deposit_account_val
            
        # D+2 예수금(매수여력) 결정: 주문가능금액이 있으면 그것을, 없으면 D+2 잔고를 사용
        if res['order_possible'] > 0:
            res['d2_deposit'] = res['order_possible']
        elif res['d2_real'] > 0:
            res['d2_deposit'] = res['d2_real']
            
    return res if success else None # [수정] 실패 시 None 반환

def check_server_health():
    """서버 상태 점검 (삼성전자 현재가 조회)"""
    try:
        # 타임아웃 5초, 재시도 0회로 설정하여 빠르게 확인
        res = call_api(constants.API_URLS["DOMESTIC"]["QUOTATIONS"]["PRICE"], "domestic", "quotations", "price", 
                       params={"fid_cond_mrkt_div_code": "J", "fid_input_iscd": "005930"}, 
                       timeout=5, retries=0)
        if res and res.get('rt_cd') == '0':
            return True
    except Exception as e:
        logger.debug(f"check_server_health error: {e}")
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
            # [추가] 네트워크 오류 등 긴 에러 메시지 축약
            error_msg = str(e)
            if "Network is unreachable" in error_msg:
                error_msg = "네트워크 통신 불가 (Network is unreachable)"
            elif "Max retries exceeded" in error_msg:
                error_msg = "서버 접속 지연 (Connection Timeout)"

            if _is_screen_output_allowed() and config.SCREEN_DEBUG_LEVEL in ["TRACE", "DEBUG"]:
                config.console.print(f"[dim red][TRACE] ERR (TELEGRAM PHOTO) {error_msg}[/dim red]")
            logger.error(f"[Telegram] 사진 전송 중 오류({attempt+1}/{max_retries}): {error_msg}")
        
        if attempt < max_retries - 1:
            time.sleep(2 ** attempt)
            
    return False


# ==========================================================
# [추가] OpenDART (전자공시) 연동 - 국내 배당/실적 조회
# ==========================================================
DART_BASE_URL = "https://opendart.fss.or.kr/api"
_dart_corp_map_cache = None  # 프로세스 메모리 캐시


def call_dart(endpoint, params, timeout=10):
    """OpenDART OpenAPI 공통 호출 래퍼.

    반환: 성공 시 응답 JSON의 'list'(없으면 dict 전체), 실패/데이터없음 시 None.
    status: 000=정상, 013=데이터없음, 020/021=한도초과/오류.
    """
    if not config.DART_API_KEY:
        return None
    try:
        p = dict(params)
        p["crtfc_key"] = config.DART_API_KEY
        res = requests.get(f"{DART_BASE_URL}/{endpoint}", params=p, timeout=timeout)
        data = res.json()
        status = data.get("status")
        if status == "000":
            return data.get("list", data)
        if status == "013":  # 조회된 데이터 없음 (정상 케이스)
            return None
        logger.warning(f"[DART] {endpoint} 응답 코드 {status}: {data.get('message')}")
        return None
    except Exception as e:
        logger.error(f"[DART] {endpoint} 호출 오류: {e}")
        return None


def get_dart_corp_map(force_refresh=False):
    """종목코드(6자리) -> DART 고유번호(corp_code, 8자리) 매핑.

    corpCode.xml(ZIP) 1회 다운로드 후 json 파일로 캐시(30일 TTL).
    """
    global _dart_corp_map_cache
    if _dart_corp_map_cache is not None and not force_refresh:
        return _dart_corp_map_cache

    if not config.DART_API_KEY:
        return {}

    cache_path = os.path.join(config.JSON_DIR, "dart_corp_map.json")

    # 파일 캐시 확인 (30일 이내면 재사용)
    if not force_refresh and os.path.exists(cache_path):
        try:
            age_days = (time.time() - os.path.getmtime(cache_path)) / 86400.0
            if age_days < 30:
                with open(cache_path, "r", encoding="utf-8") as f:
                    _dart_corp_map_cache = json.load(f)
                return _dart_corp_map_cache
        except Exception:
            pass

    # 신규 다운로드 (ZIP 안에 CORPCODE.xml)
    try:
        import zipfile, io
        import xml.etree.ElementTree as ET
        res = requests.get(f"{DART_BASE_URL}/corpCode.xml",
                           params={"crtfc_key": config.DART_API_KEY}, timeout=20)
        zf = zipfile.ZipFile(io.BytesIO(res.content))
        root = ET.fromstring(zf.read(zf.namelist()[0]))

        corp_map = {}
        for item in root.iter("list"):
            stock_code = (item.findtext("stock_code") or "").strip()
            corp_code = (item.findtext("corp_code") or "").strip()
            if stock_code and corp_code:  # 상장사만 (비상장은 stock_code 공란)
                corp_map[stock_code] = corp_code

        if corp_map:
            _dart_corp_map_cache = corp_map
            try:
                with open(cache_path, "w", encoding="utf-8") as f:
                    json.dump(corp_map, f)
            except Exception as e:
                logger.warning(f"[DART] corp_map 캐시 저장 실패: {e}")
            return corp_map
    except Exception as e:
        logger.error(f"[DART] corp_map 다운로드 오류: {e}")

    return _dart_corp_map_cache or {}


def get_dart_dividend(stock_code, year=None, reprt_code="11011"):
    """국내 종목의 '배당에 관한 사항' 조회 (정기보고서 기준).

    반환: {'주당배당금': float, '시가배당률': float, '결산월': str, 'year': str} 또는 None.
    reprt_code: 11011=사업보고서(연간), 11012=반기, 11013=1분기, 11014=3분기.
    """
    if year is None:
        # 사업보고서는 다음 해 3월경 공시되므로 직전 회계연도를 우선 조회
        year = datetime.now().year - 1

    corp = get_dart_corp_map().get(stock_code)
    if not corp:
        return None

    rows = call_dart("alotMatter.json", {
        "corp_code": corp, "bsns_year": str(year), "reprt_code": reprt_code
    })
    if not rows or not isinstance(rows, list):
        return None

    def _to_num(s):
        try:
            return float(str(s).replace(",", "").strip())
        except Exception:
            return 0.0

    result = {"year": str(year), "주당배당금": 0.0, "시가배당률": 0.0}
    for row in rows:
        se = (row.get("se") or "").strip()          # 항목명
        val = row.get("thstrm")                       # 당기 값
        # 주당 현금배당금(원) / 현금배당수익률(%) 추출 (보통주 기준)
        if "주당 현금배당금" in se or ("주당배당금" in se and "현금" in se):
            num = _to_num(val)
            if num > result["주당배당금"]:
                result["주당배당금"] = num
        elif "현금배당수익률" in se or "시가배당" in se:
            num = _to_num(val)
            if num > result["시가배당률"]:
                result["시가배당률"] = num

    if result["주당배당금"] <= 0 and result["시가배당률"] <= 0:
        return None
    return result


_dart_acc_month_cache = {}  # 종목코드 -> 결산월


def get_dart_acc_month(stock_code):
    """종목의 결산월('12' 등) 조회 (company.json). 프로세스 메모리 캐시."""
    if stock_code in _dart_acc_month_cache:
        return _dart_acc_month_cache[stock_code]

    acc = None
    corp = get_dart_corp_map().get(stock_code)
    if corp:
        data = call_dart("company.json", {"corp_code": corp})
        if isinstance(data, dict):
            acc = (data.get("acc_mt") or "").strip() or None
    _dart_acc_month_cache[stock_code] = acc
    return acc


def get_dart_disclosures(stock_code, days=30, pblntf_ty=None, page_count=100):
    """종목의 최근 공시 목록 조회 (list.json).

    반환: [{rcept_no, report_nm, flr_nm, rcept_dt, rm, corp_name}, ...] (최신순). 실패 시 [].
    pblntf_ty: 공시유형 코드(A정기/B주요사항/C발행/D지분 등). None이면 전체.
    """
    corp = get_dart_corp_map().get(stock_code)
    if not corp:
        return []
    end = datetime.now()
    bgn = end - timedelta(days=int(days))
    params = {
        "corp_code": corp,
        "bgn_de": bgn.strftime("%Y%m%d"),
        "end_de": end.strftime("%Y%m%d"),
        "page_count": str(page_count),
        "sort": "date", "sort_mth": "desc",
    }
    if pblntf_ty:
        params["pblntf_ty"] = pblntf_ty
    rows = call_dart("list.json", params)
    if not rows or not isinstance(rows, list):
        return []
    out = []
    for r in rows:
        out.append({
            "rcept_no": r.get("rcept_no", ""),
            "report_nm": (r.get("report_nm") or "").strip(),
            "flr_nm": (r.get("flr_nm") or "").strip(),
            "rcept_dt": (r.get("rcept_dt") or "").strip(),
            "rm": (r.get("rm") or "").strip(),
            "corp_name": (r.get("corp_name") or "").strip(),
        })
    return out
