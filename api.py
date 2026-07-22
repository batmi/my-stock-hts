# api.py
import requests
import logging
import json
import math
import time
import random
import sys
import ssl
import urllib3
import re
import os
import threading
import concurrent.futures
import sqlite3
import pickle
import caching
from contextlib import closing
import yfinance as yf
import pandas as pd
from datetime import datetime, timedelta, timezone
from requests.adapters import HTTPAdapter
from urllib3.poolmanager import PoolManager
from urllib3.util.retry import Retry
from collections import deque
import config
import context # [추가] 상태 관리 모듈
import constants
import toss_api  # [추가] 토스증권(mode 3) 클라이언트
from modules.executors import tg_sender_executor

logger = logging.getLogger(__name__)

# [추가] yfinance 호출 직렬화용 전역 락.
#  yfinance는 종목 timezone을 ~/.cache(또는 ~/Library/Caches)/py-yfinance 아래 SQLite에
#  캐싱하는데, 여러 스레드가 동시에 yfinance를 호출하면 이 캐시 DB에 동시 쓰기가 발생해
#  OperationalError('database is locked')로 다운로드가 실패한다(특히 모의투자처럼 해외 지수
#  fallback 의존도가 높을 때 빈번). 해외 yfinance 진입점을 이 락으로 직렬화해 경합을 차단한다.
#  (국내 지수의 KIS 조회는 락 대상이 아니므로 병렬성은 유지된다)
_YF_LOCK = threading.Lock()

# [추가] yfinance 자체 ERROR 로그('Failed download' 등)는 락 직렬화로 빈도가 급감하며,
#  남는 일시적 실패는 우리 쪽에서 빈 DataFrame 감지 후 재시도/폴백으로 처리하므로
#  라이브러리 로그 레벨을 CRITICAL로 올려 노이즈를 억제한다.
try:
    logging.getLogger("yfinance").setLevel(logging.CRITICAL)
except Exception:
    pass

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

# [추가] 국내 ETF/ETN 판정용 캐시 및 브랜드/키워드 목록
#  - 관심목록(etfs_kr)에 없더라도 보유 중인 ETF/ETN을 식별하기 위함.
#  - 1GB 라즈베리파이 운영 및 모의투자 API 한계를 고려해 매 주기 API 호출 대신
#    관심목록 + 종목명 브랜드/키워드 휴리스틱으로 판정하고 코드 단위로 캐시한다.
_ETF_ETN_CACHE = {}
_KR_ETF_BRANDS = (
    "KODEX", "TIGER", "KBSTAR", "ARIRANG", "KOSEF", "HANARO", "ACE", "SOL",
    "PLUS", "RISE", "TIMEFOLIO", "KOACT", "WOORI", "BNK", "FOCUS", "TREX",
    "KCGI", "VITA", "KINDEX", "에셋플러스", "마이다스", "히어로즈", "마이티",
)
_KR_ETF_ETN_KEYWORDS = ("레버리지", "인버스", "ETN", "ETF", "선물")

def is_domestic_etf_etn(code, name=""):
    """국내 보유/관심 종목이 ETF/ETN인지 판정한다.
    1) 관심목록(etfs_kr) 등록 여부, 2) 종목명 브랜드 프리픽스/키워드 휴리스틱.
    결과는 코드 단위로 캐시한다. (해외 종목은 호출 측에서 사전 제외)"""
    if not code:
        return False
    if code in _ETF_ETN_CACHE:
        return _ETF_ETN_CACHE[code]

    result = False
    try:
        # 1) 관심목록(국내 ETF)에 등록된 경우
        sd = getattr(config.session, 'stock_data', None) if config.session else None
        etfs = sd.get('etfs_kr', []) if sd else []
        if any(e.get('code') == code for e in etfs):
            result = True
        else:
            # 2) 종목명 기반 휴리스틱 (브랜드 프리픽스 또는 ETF/ETN/레버리지 등 키워드)
            nm = (name or "").upper().replace(" ", "")
            if nm and (any(nm.startswith(b) for b in _KR_ETF_BRANDS)
                       or any(k in nm for k in _KR_ETF_ETN_KEYWORDS)):
                result = True
    except Exception:
        result = False

    _ETF_ETN_CACHE[code] = result
    return result

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
        
    # [추가] 토스 모드일 경우 토스 API의 market-calendar 이용
    if config.session.is_toss:
        import toss_api
        try:
            today_formatted = datetime.now().strftime("%Y-%m-%d")
            res = toss_api.get_market_calendar("KR", today_formatted)
            if res and res.get('today'):
                is_holiday = not bool(res['today'].get('integrated'))
                _HOLIDAY_CACHE[today_str] = is_holiday
                return is_holiday
        except Exception as e:
            logger.debug(f"Toss market-calendar error: {e}")
            pass
            
    # 실전투자 모드일 경우에만 API 우선 조회 시도
    if not config.session.is_simulation and not config.session.is_toss:
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
            
    # [추가] 토스 모드일 경우 토스 API의 market-calendar 이용
    if config.session.is_toss:
        import toss_api
        try:
            today_formatted = datetime.now().strftime("%Y-%m-%d")
            res = toss_api.get_market_calendar("US", today_formatted)
            if res and res.get('today'):
                today_info = res['today']
                has_market = any(k in today_info and today_info[k] is not None for k in ['dayMarket', 'preMarket', 'regularMarket', 'afterMarket'])
                is_holiday = not has_market
                _HOLIDAY_CACHE[cache_key] = is_holiday
                return is_holiday
        except Exception as e:
            logger.debug(f"Toss US market-calendar error: {e}")
            pass
            
    # 실전투자 모드일 경우에만 API 우선 조회 시도
    if not config.session.is_simulation and not config.session.is_toss:
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

# (계산일 YYYYMMDD, country) -> 직전 거래일 YYYYMMDD. 하루 2건 수준이라 상한 불필요.
_TRADING_DAY_CACHE = {}

def _is_closed_day(dt, country):
    """해당 일자가 주말·휴장일이면 True. 오늘(KR)은 실시간 캘린더(토스/KIS API) 판정을
    우선해 holidays 라이브러리에 없는 임시휴장까지 반영하고, 그 외 일자는 라이브러리로 판정한다."""
    if dt.weekday() > 4:
        return True
    d_str = dt.strftime('%Y%m%d')
    if country == 'KR' and d_str == datetime.now().strftime('%Y%m%d'):
        try:
            return bool(is_holiday_today())
        except Exception:
            pass
    return get_holiday_name(d_str, country=country) is not None

def last_trading_day(dt, country='KR'):
    """dt(datetime)부터 주말·공휴일(휴장일)을 거슬러 올라간 가장 가까운 거래일(YYYYMMDD)을 반환한다."""
    for _ in range(15):  # 최장 연휴(추석 등)+주말 연속 상한
        if not _is_closed_day(dt, country):
            break
        dt -= timedelta(days=1)
    return dt.strftime('%Y%m%d')

def now_us_eastern():
    """미국 동부시간(ET) 현재 시각(naive datetime). 서머타임(DST) 자동 판별.
    (trading.py 주문 세션 판별과 동일 규칙 — 3월 둘째 일요일 ~ 11월 첫째 일요일)"""
    now_utc = datetime.now(timezone.utc).replace(tzinfo=None)
    year = now_utc.year
    march_first = datetime(year, 3, 1)
    march_second_sunday = march_first + timedelta(days=(6 - march_first.weekday()) % 7 + 7)
    dst_start_utc = march_second_sunday.replace(hour=7, minute=0, second=0)
    nov_first = datetime(year, 11, 1)
    nov_first_sunday = nov_first + timedelta(days=(6 - nov_first.weekday()) % 7)
    dst_end_utc = nov_first_sunday.replace(hour=6, minute=0, second=0)
    is_dst = dst_start_utc <= now_utc < dst_end_utc
    return now_utc - timedelta(hours=4 if is_dst else 5)

# ==========================================================
# [미국 주간거래(데이마켓)] 거래소 코드 매핑 및 세션 판정
# ==========================================================
# KIS는 미국 야간 ATS 세션(ET 20:00~04:00 = KST 09:00~17:00, 서머타임 기준)을
# '주간거래(데이마켓)'로 부르며 정규장과 다른 거래소 코드로 시세를 제공한다.
# 이 코드로 조회하지 않으면 세션 내내 직전 정규장 마감가가 그대로 굳는다
# (실측 2026-07-22 ET 02:50: MU NAS $970.82 +12.17%[전일 마감 동결] vs BAQ $949.00 -2.25%[라이브]).
# 주문 경로(modules/trading.py)는 이미 ord_dvsn '31'로 데이마켓을 인지하고 있었으므로,
# '주문은 되는데 가격은 못 보는' 비대칭을 해소한다.
US_DAY_MARKET_EXCD = {"NAS": "BAQ", "NASD": "BAQ",
                      "NYS": "BAY", "NYSE": "BAY",
                      "AMS": "BAA", "AMEX": "BAA"}
# 주간거래 코드는 exchange_cache/stock.json에 저장하면 안 되므로(정규장 조회가 깨진다) 역매핑을 둔다.
US_REGULAR_EXCD = {"BAQ": "NAS", "BAY": "NYS", "BAA": "AMS"}


def us_day_market_session():
    """미국 주간거래(데이마켓) 세션이 열려 있으면 그 세션의 '거래일'(YYYYMMDD), 아니면 None.

    야간 ATS 세션은 ET 20:00에 시작해 다음날 ET 04:00에 끝나고 '다음 거래일' 세션으로 귀속된다
    (ET 21:00 07/21의 체결은 07/22 세션 — KIS 주간거래 응답의 base가 07/21 정규장 종가인 것과 정합).
    세션 귀속일이 실제 거래일일 때만 열린 것으로 보므로, 금요일 밤(→토요일 귀속)·토요일 새벽은
    자동으로 닫힘 처리된다.
      ET 20:00~24:00 → 귀속일 = 다음 날 / ET 00:00~04:00 → 귀속일 = 당일
    """
    et = now_us_eastern()
    if et.hour >= 20:
        target = et + timedelta(days=1)
    elif et.hour < 4:
        target = et
    else:
        return None
    try:
        if _is_closed_day(target, 'US'):
            return None
    except Exception:
        return None
    return target.strftime('%Y%m%d')


def us_excd_candidates(cached_ex=None):
    """미국 시세 조회용 거래소 코드 후보(시도 순서).

    주간거래 세션 중에는 주간 코드를 먼저 시도하고, 값이 없으면 정규 코드로 폴백한다
    (세션 중이라도 해당 종목에 체결이 없으면 주간 코드는 빈 응답을 준다).
    캐시된 거래소가 있으면 그 코드(및 대응 주간 코드)를 최우선으로 두어 호출 수를 줄인다.
    """
    regular = []
    if cached_ex:
        regular.append(cached_ex)
    for e in ("NAS", "NYS", "AMS", "NASD", "NYSE", "AMEX"):
        if e not in regular:
            regular.append(e)

    if not us_day_market_session():
        return regular

    day = []
    for e in regular:
        d = US_DAY_MARKET_EXCD.get(e)
        if d and d not in day:
            day.append(d)
    return day + regular


def market_today(is_overseas=False):
    """실시간 현재가 반영 시 '당일' 판정에 쓰는 시장 기준일(YYYYMMDD 문자열).

    국내는 시스템 로컬(KST), 해외(미국)는 동부시간(ET, 서머타임 자동판별) 기준이며,
    주말·공휴일(휴장일)이면 직전 거래일까지 되돌려 반환한다. 비거래일에 현재가(=최종 종가)로
    '가짜 당일 봉'이 추가되어 등락폭/등락률이 0으로 계산되는 문제를 막는다.

    [주간거래] 데이마켓 세션 중에는 그 세션의 귀속 거래일을 돌려준다. ET 20:00~24:00 구간에서
    ET 달력일(=직전 정규장일)을 그대로 쓰면 주간거래 체결가가 '직전 정규장 봉'을 덮어써
    확정된 과거 봉이 오염되므로, 세션 귀속일 기준으로 새 봉을 추가하게 한다.
    """
    if is_overseas:
        day_session = us_day_market_session()
        if day_session:
            return day_session

    dt = datetime.now() if not is_overseas else now_us_eastern()
    country = 'US' if is_overseas else 'KR'
    key = (dt.strftime('%Y%m%d'), country)
    hit = _TRADING_DAY_CACHE.get(key)
    if hit:
        return hit
    res = last_trading_day(dt, country)
    _TRADING_DAY_CACHE[key] = res
    return res

def _is_screen_output_allowed():
    """화면 출력 허용 여부 확인 (텔레그램 봇 스레드 차단) — context 공용 판정으로 위임"""
    return context.is_screen_output_allowed()

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

def fetch_yfinance_data(tickers, period=None, start=None, end=None, interval="1d", group_by='column', _retried=False, threads=False):
    """yfinance 데이터 조회 통합 함수.

    [DB Lock 대응]
    - 전역 _YF_LOCK으로 호출을 직렬화하여 tz 캐시(SQLite) 동시 접근 경합을 차단한다.
    - 최신 yfinance는 'database is locked'를 예외로 던지지 않고 내부에서 삼킨 뒤
      빈 DataFrame을 반환하므로(예외 핸들러 우회), 결과가 비어 있으면 캐시를 정리하고
      1회 재시도한다. (_retried 플래그로 무한 재귀 방지)

    threads: 다중 티커 요청 시 yfinance 내부 병렬 다운로드 허용 여부.
      _YF_LOCK이 '외부 호출자 간' 경합은 계속 직렬화하므로, 시장 지수처럼 티커가 많은
      일괄 조회는 True로 켜면 티커당 순차 왕복(N회)이 병렬로 줄어 수 배 빨라진다.
      (결과 데이터는 동일. 빈 응답/DB Lock 재시도 시엔 안전하게 순차(False)로 폴백)
    """
    try:
        with _YF_LOCK:
            df = yf.download(tickers, period=period, start=start, end=end, interval=interval, group_by=group_by, progress=False, threads=threads)

        # [추가] 빈 결과(= tz 캐시 lock 등으로 인한 조용한 실패 가능성) → 캐시 정리 후 1회 재시도
        if not _retried and (df is None or getattr(df, 'empty', True)):
            if _is_screen_output_allowed() and config.SCREEN_DEBUG_LEVEL in ["TRACE", "DEBUG"]:
                config.console.print(f"[dim yellow]yfinance 빈 응답({tickers}). 캐시 정리 후 1회 재시도합니다.[/dim yellow]")
            clear_yfinance_cache()
            time.sleep(0.5)  # 파일 잠금 해제 대기
            return fetch_yfinance_data(tickers, period, start, end, interval, group_by, _retried=True)
        return df
    except Exception as e:
        err_msg = str(e).lower()
        if not _retried and ("database" in err_msg or "lock" in err_msg):
            if _is_screen_output_allowed() and config.SCREEN_DEBUG_LEVEL in ["TRACE", "DEBUG"]:
                config.console.print(f"[dim yellow]yfinance DB Lock 감지: {e}. 캐시 정리 후 재시도합니다.[/dim yellow]")
            clear_yfinance_cache()
            time.sleep(0.5) # 파일 잠금 해제 대기
            return fetch_yfinance_data(tickers, period, start, end, interval, group_by, _retried=True)
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

# [리팩토링] 텔레그램 발신 계층은 modules/telegram_notify.py 로 분리되었다.
# 기존 호출부(api.send_telegram_message 등) 호환을 위한 재수출(re-export).
from modules.telegram_notify import (_get_telegram_footer, send_telegram_message,
                                     send_telegram_photo)

# ==========================================================
# [추가] 실시간 단건 API용 초단기 마이크로 캐시 (Micro-Cache)
# 화면 렌더링 중 발생하는 동일 종목의 동시다발적 중복 호출 방지 (TTL: 3~10초)
# ==========================================================
# [메모리] 항목 상한(라즈베리파이 OOM 방어). 초과 시 가장 오래된 항목부터 제거한다.
# 전체 종목(코스피+코스닥 ~2800)을 스캔해도 종목당 cp_/vol_/ob_/yf_fi_ 정도라 여유 있게 잡는다.
_MICRO_CACHE_MAX = 6000
_MICRO_CACHE = caching.TTLCache(max_size=_MICRO_CACHE_MAX)
_MICRO_CACHE_LOCK = _MICRO_CACHE._lock  # 하위 호환 별칭 (기존 코드/테스트의 with 락 사용처)

def _get_micro_cache(key, ttl=60.0): # [수정] 잦은 중복 호출 방지를 위해 기본 TTL 상향
    return _MICRO_CACHE.get(key, ttl)

def _evict_oldest(cache, max_size, time_key):
    """딕셔너리 캐시가 상한을 넘으면 가장 오래된 항목들을 제거해 90% 수준으로 낮춘다.
    (eviction 빈도를 줄이기 위해 한 번에 여유분까지 비운다. 호출자가 락을 보유한 상태여야 한다)"""
    if len(cache) <= max_size:
        return
    drop = len(cache) - int(max_size * 0.9)
    for k in sorted(cache, key=lambda k: cache[k].get(time_key, 0))[:drop]:
        cache.pop(k, None)

def _set_micro_cache(key, data):
    _MICRO_CACHE.set(key, data)  # 상한 초과 시 자동 eviction (TTLCache 내장)

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

                is_extended = False
                if pd.notna(post_close) and post_close > 0:
                    close_p = post_close
                    is_extended = True
                elif pd.notna(pre_close) and pre_close > 0:
                    close_p = pre_close
                    is_extended = True

                prev_close = None
                if pd.notna(row.get('close')) and pd.notna(change_abs):
                    prev_close = row.get('close') - change_abs

                data = {
                    'last_price': close_p,
                    'regular_market_previous_close': prev_close,
                    'last_volume': row.get('volume', 0),
                    'year_high': row.get('High.52Week'),
                    'src': 'tv',            # [추가] 소스 구분 (해외주식 현재가 폴백은 TV만 허용)
                    'is_extended': is_extended  # [추가] 장외(프리/애프터) 세션 가격 여부
                }
                _set_micro_cache(cache_key, data)
                return data
        except Exception:
            pass

    # 2. yfinance Fallback
    try:
        # [수정] tz 캐시(SQLite) 동시 접근 경합 방지를 위해 yfinance 호출을 전역 락으로 직렬화
        with _YF_LOCK:
            fi = yf.Ticker(code).fast_info
            
        # [수정] regular_market_previous_close가 없는 지수(달러인덱스 등)를 위한 Fallback
        prev_close = getattr(fi, 'regular_market_previous_close', None)
        if prev_close is None or pd.isna(prev_close):
            prev_close = getattr(fi, 'previous_close', None)
                
        data = {
            'last_price': getattr(fi, 'last_price', None),
                'regular_market_previous_close': prev_close,
            'last_volume': getattr(fi, 'last_volume', 0),
            'year_high': getattr(fi, 'year_high', None),
            'src': 'yf',           # [추가] yfinance fast_info는 정규장가만 제공 (장외 시세 병합에 사용 금지)
            'is_extended': False
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
# [메모리] 차트 캐시는 DataFrame을 보관해 항목당 비용이 크다. 라즈베리파이 OOM 방어를 위해
# 항목 수를 제한하고, 초과 시 가장 오래된 항목부터 제거한다. (전체 시장 스캔 시 무제한 누적 방지)
_CHART_CACHE_MAX = 600

# [영속] 일봉 차트 디스크 캐시(SQLite). 일봉은 하루 1회만 바뀌므로, 같은 거래일 동안은
# 재시작 후에도 네트워크 재조회 없이 디스크에서 즉시 복원한다(시작 버스트·반복 조회 절감).
# 메모리 캐시(_CHART_CACHE) 미스 시 디스크를 확인하고, 과거일자 항목은 자동 정리해 크기를 제한한다.
_CHART_DISK_LOCK = threading.RLock()
_chart_disk_pruned_date = None

def _chart_disk_path():
    base = getattr(config, 'DATA_DIR', None) or getattr(config, 'JSON_DIR', '.')
    try:
        os.makedirs(base, exist_ok=True)
    except Exception:
        pass
    return os.path.join(base, 'chart_cache.db')

def _chart_disk_get(cache_key, today_str):
    """디스크 일봉 캐시에서 '오늘자' DataFrame을 복원한다(없거나 비활성/오류 시 None)."""
    if getattr(config, 'CHART_DISK_CACHE', True) is False:
        return None
    try:
        with _CHART_DISK_LOCK, closing(sqlite3.connect(_chart_disk_path())) as conn:
            conn.execute("CREATE TABLE IF NOT EXISTS chart_cache (cache_key TEXT PRIMARY KEY, trade_date TEXT, df_blob BLOB, ts REAL)")
            row = conn.execute("SELECT df_blob FROM chart_cache WHERE cache_key=? AND trade_date=?", (cache_key, today_str)).fetchone()
            if row and row[0]:
                df = pickle.loads(row[0])
                if df is not None and not df.empty:
                    return df
    except Exception as e:
        logger.debug(f"[ChartDisk] get 실패({cache_key}): {e}")
    return None

def _chart_disk_set(cache_key, df, today_str):
    """디스크 일봉 캐시에 '오늘자' DataFrame을 저장하고, 과거일자 항목은 하루 1회 정리한다."""
    if getattr(config, 'CHART_DISK_CACHE', True) is False:
        return
    global _chart_disk_pruned_date
    try:
        blob = pickle.dumps(df)
        with _CHART_DISK_LOCK, closing(sqlite3.connect(_chart_disk_path())) as conn, conn:
            conn.execute("CREATE TABLE IF NOT EXISTS chart_cache (cache_key TEXT PRIMARY KEY, trade_date TEXT, df_blob BLOB, ts REAL)")
            conn.execute("INSERT OR REPLACE INTO chart_cache (cache_key, trade_date, df_blob, ts) VALUES (?,?,?,?)",
                         (cache_key, today_str, blob, time.time()))
            # 거래일이 바뀌면 과거일자 항목 일괄 정리(디스크 무제한 누적 방지)
            if _chart_disk_pruned_date != today_str:
                conn.execute("DELETE FROM chart_cache WHERE trade_date != ?", (today_str,))
                _chart_disk_pruned_date = today_str
    except Exception as e:
        logger.debug(f"[ChartDisk] set 실패({cache_key}): {e}")

def _chart_disk_delete(cache_key):
    """디스크 일봉 캐시에서 특정 키를 제거한다(수정주가 감지 시 오염 항목 파기용).
    메모리만 지우면 다음 호출에서 디스크의 옛 df가 재적재→재파기가 반복되므로 함께 지운다."""
    if getattr(config, 'CHART_DISK_CACHE', True) is False:
        return
    try:
        with _CHART_DISK_LOCK, closing(sqlite3.connect(_chart_disk_path())) as conn, conn:
            conn.execute("CREATE TABLE IF NOT EXISTS chart_cache (cache_key TEXT PRIMARY KEY, trade_date TEXT, df_blob BLOB, ts REAL)")
            conn.execute("DELETE FROM chart_cache WHERE cache_key=?", (cache_key,))
    except Exception as e:
        logger.debug(f"[ChartDisk] delete 실패({cache_key}): {e}")
# [추가] 캐시 오버레이(get_current_price_data) 재진입 방지용 가드.
# 액면분할 보정 경로(get_current_price_data → get_chart_data → 오버레이 → get_current_price_data)에서
# 무한 재귀가 발생하지 않도록, 오버레이 진행 중 같은 스레드의 재진입 시 과거봉 캐시만 반환한다.
_OVERLAY_GUARD = threading.local()

def _chart_disk_clear():
    """디스크 일봉 캐시(SQLite)를 전부 비운다. (수동 전체 갱신/테스트 격리용)"""
    if getattr(config, 'CHART_DISK_CACHE', True) is False:
        return
    try:
        with _CHART_DISK_LOCK, closing(sqlite3.connect(_chart_disk_path())) as conn, conn:
            conn.execute("CREATE TABLE IF NOT EXISTS chart_cache (cache_key TEXT PRIMARY KEY, trade_date TEXT, df_blob BLOB, ts REAL)")
            conn.execute("DELETE FROM chart_cache")
    except Exception as e:
        logger.debug(f"[ChartDisk] clear 실패: {e}")

def clear_chart_cache():
    """모든 차트 데이터 캐시 초기화 (수동 갱신용). 디스크 영속 캐시도 함께 비운다."""
    with _CHART_CACHE_LOCK:
        _CHART_CACHE.clear()
    _chart_disk_clear()
    if _is_screen_output_allowed():
        config.console.print("[bold green]차트 데이터 캐시(메모리)가 전체 초기화되었습니다.[/bold green]")
    logger.info("[Cache] 차트 데이터 캐시 수동 초기화")

def _chart_cache_key(code, is_overseas, is_index):
    """차트 캐시 키 (브로커 네임스페이스 포함).

    같은 종목이라도 브로커별로 일봉 종가가 다르다: **TOSS 국내 일봉=NXT 연장 종가,
    KIS 일봉=KRX 정규장 종가**. 브로커를 키에 넣지 않으면 mode 전환 시(예: 맥북에서
    mode 2 → mode 3) 한쪽 데이터가 다른 쪽 캐시로 새어들어 등락률/EMA가 어긋난다.
    모의(mode1)·실전(mode2)은 둘 다 KIS/KRX라 'K'로 공유한다.

    TOSS는 'T2' — 일봉 이상치 보정(_toss_sanitize_daily_ohlc) 도입 시 네임스페이스를 올려
    이미 저장된 오염 캐시(메모리/디스크)가 재사용되지 않게 한다.
    """
    broker = 'T2' if getattr(config.session, 'is_toss', False) else 'K'
    return f"{broker}_{code}_{is_overseas}_{is_index}"


def _get_cached_chart(code, is_overseas, is_index, fetch_func, realtime_overlay=True):
    """캐시된 차트를 반환하되, 당일 최신 캔들은 실시간 현재가로 덮어씌워 반환합니다.

    realtime_overlay=False면 캐시 적중 시 현재가 API 오버레이를 생략하고 과거봉 캐시를 그대로 반환한다.
    (호출자가 자체적으로 당일 캔들을 실시간 갱신하는 경우, 중복 현재가 호출을 막아 TPS 부담을 줄인다.)
    """
    ttl_minutes = getattr(config, 'CHART_CACHE_TTL_MINUTES', 360)
    if ttl_minutes <= 0:
        return fetch_func()

    now = datetime.now()
    today_str = now.strftime("%Y-%m-%d")
    cache_key = _chart_cache_key(code, is_overseas, is_index)

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

    # [영속] 메모리 미스 시 디스크(오늘자) 캐시를 확인해 네트워크 재조회를 피한다(재시작 내성).
    if cached is None and not is_index:
        disk_df = _chart_disk_get(cache_key, today_str)
        if disk_df is not None:
            cached = {'df': disk_df, 'timestamp': now, 'date': today_str}
            with _CHART_CACHE_LOCK:
                _CHART_CACHE[cache_key] = cached
                _evict_oldest(_CHART_CACHE, _CHART_CACHE_MAX, 'timestamp')

    if cached:
        df = cached['df'].copy()
        # [추가] 오버레이 불필요(호출자 자체 갱신) 시 과거봉 캐시만 즉시 반환 → 중복 현재가 호출 제거
        if not realtime_overlay:
            return df
        # [추가] 재진입(분할보정 경로 등) 감지 시 오버레이/재조회 없이 과거봉 캐시만 반환 → 무한재귀 차단
        if getattr(_OVERLAY_GUARD, 'active', False):
            return df
        _OVERLAY_GUARD.active = True
        try:
            # 시장 기준일(주말·휴장일이면 직전 거래일). 달력 날짜를 쓰면 비거래일에
            # 최종 종가로 '가짜 당일 봉'이 추가되어 등락률이 0으로 계산된다.
            today_ymd = market_today(is_overseas)
            last_date = str(df.iloc[-1]['date'])
            
            curr, open_p, high_p, low_p, vol, prev = 0, 0, 0, 0, 0, 0

            def _safe_float(val):
                if val is None: return 0.0
                s = str(val).strip().replace(',', '')
                if not s: return 0.0
                try: return float(s)
                except Exception: return 0.0

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
                        # 전일종가는 base 필드만 신뢰한다. (curr - diff) 방식은 last가 장외
                        # 실시간가로 덮어써지거나(KIS 프리/애프터) diff 미제공(토스) 시 어긋나
                        # 아래 수정주가 검증이 오탐→캐시 파기·재조회를 반복한다. base 없으면 0(검증 스킵).
                        prev = _safe_float(out.get('base', 0))
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
                    _chart_disk_delete(cache_key)
                    # 아래 공통 재조회 경로로 합류시켜 새 데이터가 메모리·디스크에 재캐싱되게 한다.
                    cached = None
                else:
                    # 3. 실시간 가격 패치(Patch)
                    # last_date > today_ymd(캔들 소스가 기준일보다 앞선 경우: KST 장중의 국내지수를
                    # ET 기준일로 본 경우 등)에도 최신 봉을 덮어쓴다. 새 행 추가는 기준일이
                    # 거래일로 보정되어 있으므로 실제 개장일에만 일어난다.
                    if last_date >= today_ymd:
                        # 당일 봉 덮어쓰기 (고가/저가는 캐시된 데이터와 비교하여 최대/최소 유지)
                        old_high = float(df.iloc[-1]['high'])
                        old_low = float(df.iloc[-1]['low'])
                        high_p = max(old_high, high_p, curr)
                        low_p = min(old_low, low_p, curr) if low_p > 0 else min(old_low, curr)
                        # 현재가 API가 시가/거래량을 안 주는 경우(토스 등)는 캐시된 당일 봉 값을 보존(0 덮어쓰기 방지)
                        if open_p <= 0: open_p = float(df.iloc[-1]['open'])
                        if vol <= 0: vol = float(df.iloc[-1]['volume'])
                        df.loc[df.index[-1], ['open', 'high', 'low', 'close', 'volume']] = [open_p, high_p, low_p, curr, vol]
                    else:
                        # 오늘 날짜 행이 없으면 새로 추가 (시가/고가/저가 미제공 시 현재가로 근사)
                        if open_p <= 0: open_p = curr
                        if high_p <= 0: high_p = curr
                        if low_p <= 0: low_p = curr
                        new_row = pd.DataFrame([{'date': today_ymd, 'open': open_p, 'high': high_p, 'low': low_p, 'close': curr, 'volume': vol}])
                        df = pd.concat([df, new_row], ignore_index=True)

                    return df
        except Exception as e:
            logger.debug(f"[Cache] Update failed for {code}: {e}")
        finally:
            _OVERLAY_GUARD.active = False
        # 오버레이 실패(현재가 미확보·예외) 시 전체 재조회 대신 캐시된 과거봉을 그대로 반환한다.
        # 과거봉은 불변이므로 당일 봉만 잠시 덜 신선할 뿐, 무거운 전체 재다운로드보다 낫다.
        # (수정주가 파기 시에만 cached=None으로 아래 재조회 경로를 탄다)
        if cached is not None:
            return df

    df = fetch_func()
    if df is not None and not df.empty:
        with _CHART_CACHE_LOCK:
            _CHART_CACHE[cache_key] = {
                'df': df.copy(),
                'timestamp': now,
                'date': today_str
            }
            _evict_oldest(_CHART_CACHE, _CHART_CACHE_MAX, 'timestamp')
        # [영속] 일봉(비지수)만 디스크에 저장해 재시작/반복 조회 시 네트워크 호출을 줄인다.
        if not is_index:
            _chart_disk_set(cache_key, df, today_str)
    return df

def prefetch_multiple_current_prices(codes, is_overseas=False, include_investor=True, progress_updater=None, prefer_ws=False, skip_if_fresh_sec=None):
    """[최적화] 다중 종목 실시간 데이터 일괄 조회 (Micro-Cache 사전 예열)

    prefer_ws=True면 WS 실시간 피드가 이미 신선한 현재가를 보유한 종목은 현재가 REST 예열을
    생략한다(모의투자 2 TPS 절감). 시스템 트레이딩처럼 이후 경로가 현재가 값만 필요한 곳에서 쓴다.
    skip_if_fresh_sec(해외 전용): 전 종목의 fast_info 마이크로 캐시가 지정 초 이내로 신선하면
    TV 재조회 자체를 생략한다. 백그라운드 워머(OverviewWarmer)가 방금 예열한 경우 임계경로의
    네트워크 왕복을 제거하는 용도이며, 워머 자신은 이 인자를 쓰면 안 된다(갱신이 영구 생략됨).
    """
    if not codes: return

    if is_overseas:
        # 0. [최적화] 워머가 예열해 둔 캐시가 전 종목 신선하면 라이브 조회 생략
        if skip_if_fresh_sec:
            if all(_get_micro_cache(f"yf_fi_{c}", ttl=skip_if_fresh_sec) for c in codes):
                if progress_updater:
                    for _ in codes: progress_updater()
                return

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
                    is_extended = False
                    if pd.notna(post_close) and post_close > 0:
                        close_p = post_close
                        is_extended = True
                    elif pd.notna(pre_close) and pre_close > 0:
                        close_p = pre_close
                        is_extended = True

                    if pd.isna(close_p): continue

                    prev_close = None
                    if pd.notna(row.get('close')) and pd.notna(change_abs):
                        prev_close = row.get('close') - change_abs

                    data = {
                        'last_price': close_p,
                        'regular_market_previous_close': prev_close,
                        'last_volume': row.get('volume', 0),
                        'year_high': row.get('High.52Week'),
                        'src': 'tv',
                        'is_extended': is_extended
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
        # WS가 신선한 현재가를 이미 가진 종목은 현재가 REST 예열을 생략(TPS 절감)하기 위한 피드 핸들
        _ws_feed = None
        if prefer_ws and getattr(config, 'USE_WEBSOCKET', True) and not config.session.is_toss:
            try:
                import realtime
                _ws_feed = realtime.get_feed()
            except Exception:
                _ws_feed = None
        _ws_ttl = getattr(config, 'WS_DATA_TTL_SEC', 3.0)

        def fetch_worker(code):
            ws_has_price = False
            if _ws_feed is not None:
                try:
                    p = _ws_feed.get_price(code, max_age=_ws_ttl)
                    ws_has_price = bool(p and p > 0)
                except Exception:
                    ws_has_price = False
            if not ws_has_price:
                try: get_current_price_data(code, False)
                except Exception: pass
            if include_investor:
                try: get_investor_trend(code)
                except Exception: pass
            # 체결강도는 get_realtime_vol_strength가 내부적으로 WS를 먼저 확인하므로 WS 커버 시 REST 미발생
            try: get_realtime_vol_strength(code)
            except Exception: pass

        max_w = 4 if config.session.is_simulation else 10
        with concurrent.futures.ThreadPoolExecutor(max_workers=max_w) as executor:
            futures = [executor.submit(fetch_worker, c) for c in codes]
            for future in concurrent.futures.as_completed(futures):
                if progress_updater: progress_updater()

def prefetch_watchlists_async():
    """백그라운드에서 관심 종목의 차트 데이터를 캐싱(Warming)합니다."""
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

_OVERVIEW_WARMER_STARTED = False

def start_overview_warmer():
    """[최적화] 해외 종목 시세/시장 지수를 백그라운드에서 주기적으로 마이크로 캐시에 예열한다.

    '시장 지수 조회'/'종목 시세 분석'(해외) 개요 화면이 임계경로에서 무거운 조회 없이
    예열된 캐시를 즉시 읽도록 하여 체감 지연을 줄인다. (국내 종목 현재가·체결강도는 KRX/NXT
    시간대 무관하게 매 실행마다 라이브로 조회하므로 예열하지 않는다.)
    모의투자(2 TPS)는 시스템 트레이딩과 TPS를 다투므로 기본 비활성화한다.
    """
    global _OVERVIEW_WARMER_STARTED
    if _OVERVIEW_WARMER_STARTED:
        return None
    if not getattr(config, 'OVERVIEW_WARM_ENABLED', True):
        return None
    if config.session.is_toss:
        return None  # 토스 모드는 별도 캐시 경로 사용
    if config.session.is_simulation and not getattr(config, 'OVERVIEW_WARM_ON_SIMULATION', False):
        logger.info("[Warm] 모의투자: 개요 백그라운드 예열 비활성(시스템 트레이딩 TPS 보호)")
        return None

    interval = max(5, int(getattr(config, 'OVERVIEW_WARM_INTERVAL_SEC', 15)))

    def worker():
        logger.info(f"[Warm] 개요 백그라운드 예열 시작 (주기 {interval}s)")
        while True:
            try:
                stock_data = config.session.stock_data or {}
                ovs_codes = []
                seen = set()
                for key in ["stocks_us", "etfs_us"]:
                    for s in stock_data.get(key, []):
                        c = s.get('code')
                        if c and c not in seen:
                            seen.add(c); ovs_codes.append(c)

                # 해외: TradingView 일괄(HTTP 1회, TPS 무관) 예열은 저비용이므로 항상 수행
                if ovs_codes:
                    try:
                        prefetch_multiple_current_prices(ovs_codes, is_overseas=True)
                    except Exception as e:
                        logger.debug(f"[Warm] 해외 예열 오류: {e}")

                # 시장 지수(메뉴1): 해외/지표 지수의 fast_info 예열. 60초 TTL이 네트워크를 자체 제한하므로
                # 매 사이클 호출해도 대부분 캐시 적중이라 저비용이다. (국내 지수는 KIS 경로라 제외)
                try:
                    from modules import market as _market
                    _domestic = {"코스피", "코스닥", "코스피200", "코스닥150"}
                    idx_tickers = [tk for nm, tk in _market.ALL_INDICES if nm not in _domestic]
                    if idx_tickers:
                        with concurrent.futures.ThreadPoolExecutor(max_workers=4) as _ex:
                            list(_ex.map(lambda tk: get_yf_fast_info(tk), idx_tickers))
                except Exception as e:
                    logger.debug(f"[Warm] 지수 예열 오류: {e}")
            except Exception as e:
                logger.error(f"[Warm] 개요 예열 루프 오류: {e}")
            time.sleep(interval)

    t = threading.Thread(target=worker, daemon=True, name="OverviewWarmer")
    t.start()
    _OVERVIEW_WARMER_STARTED = True
    return t

class ThrottledSession(requests.Session):
    def __init__(self):
        super().__init__()
        self.request_history_sim = deque()
        self.request_history_real = deque()
        self.lock = threading.Lock() # [추가] Rate Limit 계산 동기화를 위한 락
        # [#7] 실전 실효 TPS를 적응형으로 운행한다(AIMD).
        #  - 시작값: 명목 한도 × REAL_TPS_SAFETY(=0.9 마진). 성공이 누적되면 마진을 조금씩 줄여(가산 증가)
        #    실효 TPS를 점진 상향하고, EGW00201(초당 거래건수 초과)이 나면 곱셈 감소로 즉시 물러난다.
        #  - [REAL_TPS_SAFETY_MIN, REAL_TPS_SAFETY_MAX] 범위 내에서 적정 TPS로 자가 수렴한다.
        self.adaptive_limit_real = None

    def _real_tps_bounds(self):
        nominal = config.REAL_TX_PER_SECOND
        lo = nominal * getattr(config, 'REAL_TPS_SAFETY_MIN', 0.85)
        hi = nominal * getattr(config, 'REAL_TPS_SAFETY_MAX', 0.98)
        start = nominal * getattr(config, 'REAL_TPS_SAFETY', 0.9)
        return lo, hi, start

    def _tps_on_success_real(self):
        """실전 성공(레이트리밋 아님) 시 실효 TPS를 가산 증가(마진 축소)시킨다."""
        with self.lock:
            lo, hi, start = self._real_tps_bounds()
            cur = self.adaptive_limit_real if self.adaptive_limit_real is not None else start
            self.adaptive_limit_real = min(hi, cur + getattr(config, 'TPS_ADAPT_STEP', 0.05))

    def _tps_on_rate_limit_real(self):
        """실전 EGW00201(초당 거래건수 초과) 시 실효 TPS를 곱셈 감소(마진 확대)시킨다."""
        with self.lock:
            lo, hi, start = self._real_tps_bounds()
            cur = self.adaptive_limit_real if self.adaptive_limit_real is not None else start
            self.adaptive_limit_real = max(lo, cur * getattr(config, 'TPS_ADAPT_BACKOFF', 0.9))

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
                            # [수정] 명목 한도(target_limit)에 내부 안전계수를 곱해 '실효 한도'로 운행한다.
                            #  - 명목 한도에 정확히 붙이면 클라이언트 윈도우와 KIS 서버 1초 카운터의
                            #    경계가 충돌해 EGW00201이 상시 발생하므로, 약간의 마진을 둔다.
                            #  - config 설정값(REAL_TX_PER_SECOND 등)은 그대로 두고 로직 내부에서만 보정.
                            if is_sim_server:
                                effective_limit = target_limit  # 모의(2 TPS)는 기존 동작 유지
                                min_interval = (1.0 / target_limit) * 1.2
                            else:
                                # [#7] 적응형 실효 한도(AIMD). 미초기화 시 시작 마진(REAL_TPS_SAFETY)으로 출발.
                                if self.adaptive_limit_real is None:
                                    self.adaptive_limit_real = target_limit * getattr(config, 'REAL_TPS_SAFETY', 0.9)
                                effective_limit = max(1.0, self.adaptive_limit_real)
                                min_interval = 1.0 / effective_limit

                            # 1. 윈도우 기반 한도 체크 (Burst 방어)
                            window_size = 1.5 if is_sim_server else 1.1

                            while history and history[0] <= now - window_size:
                                history.popleft()

                            # 2. 최소 간격 체크 (고르게 분산)
                            time_since_last = now - history[-1] if history else float('inf')

                            if len(history) < effective_limit and time_since_last >= min_interval:
                                history.append(now)
                                current_tps = len(history)
                                break # 락 해제 후 전송 진행
                            else:
                                wait_from_window = (history[0] + window_size) - now if len(history) >= effective_limit else 0
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
                        body_preview = response.text[:500]
                        # [수정] EGW00201/EGW00215(초당 거래건수 초과)는 스로틀 백오프로
                        #        재시도되어 정상 복구되는 흐름이므로 ERROR가 아닌 DEBUG로 강등.
                        #        (Status 500으로 내려오지만 실제 장애가 아니라 Rate Limit임)
                        if 'EGW00201' in body_preview or 'EGW00215' in body_preview:
                            logger.debug(f"[Rate Limit] TPS 초과 응답 → 스로틀 백오프 후 재시도. URL: {url}")
                            if is_real_server:
                                self._tps_on_rate_limit_real()  # [#7] 실효 TPS 곱셈 감소
                        else:
                            logger.error(f"⚠️ [HTTP Error] URL: {url} | Status: {response.status_code} | Body: {body_preview}")
                    except Exception: pass
                
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

                            # [#7] 실전 정상 응답이면 실효 TPS를 가산 증가(마진 축소)시킨다.
                            if is_real_server and rt_cd == '0':
                                self._tps_on_success_real()

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
                                    if is_real_server:
                                        self._tps_on_rate_limit_real()  # [#7] 실효 TPS 곱셈 감소
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
                    # [수정] Rate Limit(EGW00201/EGW00215)은 정상적인 백오프 재시도 흐름이므로
                    #        DEBUG로 강등하여 로그 노이즈를 줄인다. (진짜 오류만 WARNING 유지)
                    if 'EGW00201' in str(e) or 'EGW00215' in str(e):
                        logger.debug(msg)
                    else:
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
            if config.session.is_toss:
                import toss_api
                if not toss_api.get_access_token(force_refresh=True):
                    success = False
                    fail_reason = "토스 API 토큰 발급 실패"
            elif config.session.is_simulation:
                if not get_access_token(force_refresh=True):
                    success = False
                    fail_reason = "모의투자 토큰 발급 실패 (API 서버 응답 없음 또는 점검 중)"
            else:
                if not get_real_access_token(force_refresh=True):
                    success = False
                    fail_reason = "한투증권 토큰 발급 실패 (API 서버 응답 없음 또는 점검 중)"
                
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
                except Exception: pass

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
        # [추가] 모의투자 서버는 응답이 느려 짧은 timeout에서 ReadTimeout이 빈발하므로,
        #  모의투자일 때는 최소 timeout을 보장한다. (DEFAULT_TIMEOUT보다 크게 늘리지는 않음)
        if config.session.is_simulation:
            sim_min = getattr(config, 'SIM_MIN_QUOTE_TIMEOUT', 5)
            if timeout < sim_min: timeout = sim_min
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
                # [Fix] 그동안 retries가 session에 전달되지 않아 ThrottledSession이 항상
                #  config.MAX_RETRIES로 재시도했다. retries=0(빠른 실패) 의도가 무력화되어
                #  네트워크 장애 시 불필요하게 수십 초 블로킹되던 문제를 바로잡는다.
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

def _resample_weekly(df):
    """일봉 DataFrame(date=YYYYMMDD 문자열)을 주봉으로 리샘플링한다(주 마감=금요일 기준).
    KIS 네이티브 주봉이 없는 경로(토스 개별종목)에서 사용한다.
    OHLCV 집계: 시가=주 첫 거래일, 고가=최댓값, 저가=최솟값, 종가=주 마지막, 거래량=합계."""
    if df is None or df.empty:
        return df if df is not None else pd.DataFrame()
    d = df.copy()
    d['_dt'] = pd.to_datetime(d['date'].astype(str), format='%Y%m%d', errors='coerce')
    d = d.dropna(subset=['_dt']).sort_values('_dt').set_index('_dt')
    agg = {'open': 'first', 'high': 'max', 'low': 'min', 'close': 'last', 'volume': 'sum'}
    w = d.resample('W-FRI').agg(agg).dropna(subset=['close'])
    w = w.reset_index()
    w['date'] = w['_dt'].dt.strftime('%Y%m%d')  # 주봉 라벨 = 해당 주 마감(금)일
    return w[['date', 'open', 'high', 'low', 'close', 'volume']].reset_index(drop=True)

def _fetch_kis_weekly_domestic(code, lookback_days=1100):
    """KIS 국내 주봉(FID_PERIOD_DIV_CODE='W'). 날짜 구간을 뒤로 페이징하며 ~3년치를 모은다."""
    now = datetime.now()
    today = now.strftime("%Y%m%d")
    start_date_origin = (now - timedelta(days=lookback_days)).strftime("%Y%m%d")
    url_path = constants.API_URLS["DOMESTIC"]["QUOTATIONS"]["CHART"]
    all_items = []
    current_end_date = today
    retry_count = 0
    while retry_count < 10:
        params = {"FID_COND_MRKT_DIV_CODE": "J", "FID_INPUT_ISCD": code,
                  "FID_INPUT_DATE_1": start_date_origin, "FID_INPUT_DATE_2": current_end_date,
                  "FID_PERIOD_DIV_CODE": "W", "FID_ORG_ADJ_PRC": "0"}
        data = call_api(url_path, "domestic", "quotations", "chart", params=params, timeout=3)
        if data.get('rt_cd') == '0':
            items = data.get('output2')
            if items:
                all_items.extend(items)
                temp_dates = sorted([x['stck_bsop_date'] for x in items if x.get('stck_bsop_date')])
                if not temp_dates or temp_dates[0] <= start_date_origin:
                    break
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

    if not all_items:
        return pd.DataFrame()
    df = pd.DataFrame(all_items).drop_duplicates(subset=['stck_bsop_date'])
    df = df[df['stck_bsop_date'] >= start_date_origin]
    df = df[['stck_bsop_date', 'stck_clpr', 'stck_oprc', 'stck_hgpr', 'stck_lwpr', 'acml_vol']].copy()
    df.columns = ['date', 'close', 'open', 'high', 'low', 'volume']
    df = df.astype({'close': float, 'open': float, 'high': float, 'low': float, 'volume': float})
    return df.sort_values('date', ascending=True).reset_index(drop=True).tail(160)

def _fetch_kis_weekly_overseas(code, lookback_days=1100):
    """KIS 해외 주봉(GUBN='1'). 거래소 후보를 순회하며 날짜 구간을 뒤로 페이징한다."""
    now = datetime.now()
    today = now.strftime("%Y%m%d")
    start_date_origin = (now - timedelta(days=lookback_days)).strftime("%Y%m%d")
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
        while retry_count < 10:
            params = {"AUTH": "", "EXCD": excd, "SYMB": code, "GUBN": "1", "BYMD": next_bymd, "MODP": "1", "KEYB": code}
            data = call_api(url_path, "overseas", "quotations", "chart", params=params, timeout=3)
            if data.get('rt_cd') == '0':
                items = data.get('output2')
                if items:
                    if not all_items and cached_ex != excd:
                        config.session.update_cache_and_save(code, excd)
                    all_items.extend(items)
                    last = items[-1]['xymd']
                    if last <= start_date_origin:
                        break
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
            for c in ['close', 'open', 'high', 'low', 'volume']: df[c] = df[c].astype(float)
            return df.sort_values('date', ascending=True).reset_index(drop=True).tail(160)
    return pd.DataFrame()

def _get_weekly_chart_data(code, is_overseas):
    """주봉 차트 데이터. KIS 네이티브 주봉(국내 W / 해외 GUBN=1)으로 ~3년치를 조회하고,
    KIS 주봉이 없는 경로(지수·환율·원자재는 yfinance 1wk, 토스 개별종목은 일봉 리샘플링)로 보강한다."""
    is_index = (code.startswith('^') or code.endswith('=F') or code.endswith('=X')
                or code == 'DX-Y.NYB' or '-USD' in code or code.endswith('.SS') or code.endswith('.IL'))
    if is_index:
        def _fetch_yf_index_weekly():
            try:
                df = fetch_yfinance_data(code, period="5y", interval="1wk")
                if df is None or df.empty:
                    return pd.DataFrame()
                flat_cols = [str(col[0]).lower() if isinstance(col, tuple) else str(col).lower() for col in df.columns]
                df.columns = flat_cols
                df.reset_index(inplace=True)
                df.columns = [str(c).lower() for c in df.columns]
                df = df.loc[:, ~df.columns.duplicated()].copy()
                cols = ['date', 'open', 'high', 'low', 'close', 'volume']
                for c in cols:
                    if c not in df.columns: df[c] = 0
                df = df[cols].copy()
                df['date'] = df['date'].apply(lambda x: x.strftime('%Y%m%d') if hasattr(x, 'strftime') else str(x).replace('-', '')[:8])
                return df.sort_values('date', ascending=True).reset_index(drop=True).tail(160)
            except Exception as e:
                logger.debug(f"yfinance weekly index fetch error: {e}")
                return pd.DataFrame()

        # [최적화] 지수 주봉도 메모리 캐싱한다. 일봉과 키가 겹치지 않게 '_W' 접미사를 붙이고,
        # 당일 '일봉' 패치용 오버레이는 주봉 캔들(주 시작일 date)에 맞지 않으므로 끈다.
        return _get_cached_chart(f"{code}_W", is_overseas=True, is_index=True,
                                 fetch_func=_fetch_yf_index_weekly, realtime_overlay=False)

    # 토스 개별종목: 토스 캔들은 일/분봉만 제공 → 일봉을 주 단위로 리샘플링
    if config.session.is_toss:
        return _resample_weekly(get_chart_data(code, is_overseas, 'daily'))

    if not is_overseas:
        return _fetch_kis_weekly_domestic(code)
    return _fetch_kis_weekly_overseas(code)

def get_chart_data(code, is_overseas=False, period_type='daily', realtime=True):
    """
    기술적 분석을 위한 차트 데이터를 조회합니다.
    period_type: 'weekly' (주봉), 'daily' (일봉), 'hourly' (시봉), 'intraday' (분봉)
    realtime=False: 일봉 캐시 적중 시 현재가 오버레이를 생략한다(호출자가 직접 당일 캔들을 갱신하는 대량 조회용).
    """
    if period_type == 'weekly':
        return _get_weekly_chart_data(code, is_overseas)

    # [추가] 토스: yfinance 대상(지수/원자재/환율 등)이 아닌 개별 종목은 토스 캔들로 조회
    _yf_index = (code.startswith('^') or code.endswith('=F') or code.endswith('=X')
                 or code == 'DX-Y.NYB' or '-USD' in code or code.endswith('.SS') or code.endswith('.IL'))
    if config.session.is_toss and not _yf_index:
        # [최적화] 일봉은 KIS 경로와 동일하게 _get_cached_chart로 6시간/디스크 캐싱한다.
        #  과거 일봉은 불변이므로 반복 조회(메뉴2 등) 시 토스 캔들 페이지네이션(최대 4콜)을 제거한다.
        #  당일 봉은 오버레이가 실시간 현재가로 갱신한다. 시봉/분봉은 KIS와 동일하게 캐시 제외.
        if period_type == 'daily':
            return _get_cached_chart(
                code, is_overseas, is_index=False,
                fetch_func=lambda: _toss_daily_chart_with_tv_fallback(code, is_overseas),
                realtime_overlay=realtime,
            )
        return _toss_chart_data(code, period_type, is_overseas)

    if period_type == 'intraday':
        return _get_intraday_chart_data(code, is_overseas)
    
    if period_type == 'hourly':
        return _get_hourly_chart_data(code, is_overseas)

    now = datetime.now()
    today = now.strftime("%Y%m%d")
    start_date_origin = (now - timedelta(days=config.INDICATOR_PARAMS["CHART_LOOKBACK_DAYS"])).strftime("%Y%m%d")
    
    is_index = (code.startswith('^') or code.endswith('=F') or code.endswith('=X') or code == 'DX-Y.NYB' or '-USD' in code or code.endswith('.SS') or code.endswith('.IL'))
    if is_index:
        def _fetch_yf_index_daily():
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

        # [최적화] 지수/원자재/환율 일봉도 종목과 동일하게 메모리 캐싱한다(is_index=True → 디스크 제외).
        # 모두 yfinance 티커이므로 당일 봉 오버레이가 fast_info 경로를 타도록 is_overseas=True로 고정한다.
        return _get_cached_chart(code, is_overseas=True, is_index=True,
                                 fetch_func=_fetch_yf_index_daily, realtime_overlay=realtime)

    if not is_overseas:
        # [최적화] 일봉 과거 데이터는 불변이므로 _get_cached_chart로 캐싱(당일 봉만 실시간 오버레이).
        # 반복 조회(메뉴2 등) 시 250봉 페이지네이션(~3콜/종목)을 제거한다.
        def _fetch_domestic_daily():
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

        return _get_cached_chart(code, is_overseas=False, is_index=False, fetch_func=_fetch_domestic_daily, realtime_overlay=realtime)

    else:
        def _fetch_overseas_daily():
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
            return pd.DataFrame()

        return _get_cached_chart(code, is_overseas=True, is_index=False, fetch_func=_fetch_overseas_daily, realtime_overlay=realtime)

def _get_intraday_yfinance(code, is_overseas):
    """yfinance 1분봉 폴백. 해외/지수, 또는 국내라도 장전 등으로 KIS 당일분봉이 빌 때 사용.
    5일치를 받아 최근 390개(≈정규장 1세션)만 유지 → 장전이면 직전 거래일 세션이 된다."""
    try:
        # 국내 종목이 폴백을 탈 경우를 대비해 stock.json을 참조해 정확한 티커(.KS / .KQ) 생성
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

            cols = ['date', 'open', 'high', 'low', 'close', 'volume']
            for c in cols:
                if c not in df.columns: df[c] = 0

            df = df[cols].copy().sort_values('date', ascending=True)

            # 최근 390개 (약 6시간 30분 = 1일 장 운영 시간) 데이터만 유지
            if len(df) > 390:
                df = df.iloc[-390:]

            return df.reset_index(drop=True)
    except Exception as e:
        logger.error(f"[API] yfinance 분봉 조회 실패: {e}")
    return pd.DataFrame()


def _get_intraday_chart_data(code, is_overseas):
    """분봉(1분) 데이터 조회 (KIS API 사용, 해외/지수는 yfinance Fallback)"""

    # 1. KIS API 미지원 대상 확인 (해외주식, 지수 등)
    use_fallback = is_overseas
    if code.startswith('^') or (code.startswith('0001') and len(code) == 4):
        use_fallback = True

    # Fallback 로직 (yfinance)
    if use_fallback:
        return _get_intraday_yfinance(code, is_overseas)

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
        # 장 시작 전/휴장 등으로 당일 분봉이 없으면 빈 값 반환 (호출부에서 장전 안내 처리)
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


# KIS 지수 코드 → yfinance 티커 (토스 모드 폴백용)
_INDEX_KIS_TO_YF = {"0001": "^KS11", "1001": "^KQ11", "2001": "^KS200", "2203": "^KQ150"}


def get_domestic_index_chart(code):
    """업종/지수 기간별 시세(일봉) 조회 (KIS API, 토스 모드는 yfinance 폴백)"""
    # [추가] 토스: KIS 미사용. 지수 KIS 코드를 yfinance 티커로 매핑하여 조회.
    if config.session.is_toss:
        yf_ticker = _INDEX_KIS_TO_YF.get(str(code))
        if not yf_ticker:
            return pd.DataFrame()
        try:
            df = get_chart_data(yf_ticker, is_overseas=True)
            return df if df is not None else pd.DataFrame()
        except Exception as e:
            logger.debug(f"[Toss] 지수 차트 yfinance 조회 실패({code}): {e}")
            return pd.DataFrame()

    def fetch_func():
        # 지수/업종 차트 조회 URL 및 TR_ID (실전/모의 동일)
        url_path = constants.API_URLS["DOMESTIC"]["QUOTATIONS"]["INDEX_CHART"]
        tr_id = "FHKUP03500100"

        now = datetime.now()
        today = now.strftime("%Y%m%d")
        start_date = (now - timedelta(days=730)).strftime("%Y%m%d") # 2년치 조회

        def _fetch_pages(api_caller, log_fail=True):
            """기간 분할 페이지네이션 공통 루프 (api_caller: params → 응답 dict)"""
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
                data = api_caller(params)
                if data.get('rt_cd') == '0':
                    # 빈 행/None 응답 방어
                    items = [it for it in (data.get('output2') or []) if it.get('stck_bsop_date')]
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
                    if not all_items and log_fail:
                        logger.warning(f"[API] 지수({code}) 조회 실패: {data.get('msg1')} (Code: {data.get('msg_cd')})")
                    break
            return all_items

        # 모의서버 업종 TR은 'MCI전송 오류(OPSQ0008)' 등으로 간헐 실패한다. 모의 모드에서
        # 실전 서버는 사용하지 않으며(운영 방침), 실패 시 상위 폴백 체인(tvDatafeed→yfinance)이
        # 코스피·코스닥·코스피200·코스닥150을 받쳐준다 (VKOSPI는 폴백이 없어 모드 1 목록 제외).
        all_items = _fetch_pages(
            lambda p: call_api(url_path, "domestic", "quotations", "index_chart", params=p, tr_id=tr_id, retries=0)
        )

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
    """업종/지수 현재가 조회 (KIS API, 토스 모드는 yfinance 폴백)"""
    # [추가] 토스: KIS 미사용. yfinance fast_info로 현재가/전일종가 조회 후 KIS 형태로 반환.
    if config.session.is_toss:
        yf_ticker = _INDEX_KIS_TO_YF.get(str(code))
        if not yf_ticker:
            return {'rt_cd': '9999'}
        try:
            fi = get_yf_fast_info(yf_ticker)
            if not fi:
                return {'rt_cd': '9999'}
            curr = fi.get('last_price')
            prev = fi.get('regular_market_previous_close')
            if curr is None:
                return {'rt_cd': '9999'}
            return {'rt_cd': '0', 'output': {
                'bstp_nmix_prpr': str(curr),
                'bstp_nmix_prdy_clpr': str(prev if prev is not None else curr),
            }}
        except Exception as e:
            logger.debug(f"[Toss] 지수 현재가 yfinance 조회 실패({code}): {e}")
            return {'rt_cd': '9999'}

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

# ==========================================================
# [추가] 코스피200 선물 (주간 F / 야간 CM) 시세 — KIS 전용
#  - 종목코드는 주간/야간 공통(예: A01609), 시장분류코드로 세션을 가른다.
#  - 야간(KRX 야간파생시장, 18:00~익일 06:00)은 FID_COND_MRKT_DIV_CODE='CM'.
# ==========================================================
def get_k200_futures_front_code(now=None):
    """코스피200 선물 근월물 종목코드(예: 'A01609')를 계산한다.

    결제월은 3/6/9/12월, 만기(최종거래일)는 결제월 두 번째 목요일.
    만기일 주간장 마감(15:45) 이후에는 차근월물로 롤오버한다.
    (마스터파일 fo_cme_code.mst의 코드 체계: 'A01' + 연도 끝자리 + 결제월 2자리)
    """
    if now is None:
        now = datetime.now()
    d = now.date()
    y, m = d.year, d.month
    for _ in range(8):
        qm = ((m - 1) // 3 + 1) * 3  # 3/6/9/12월로 올림
        first_wd = datetime(y, qm, 1).weekday()
        expiry = datetime(y, qm, 1 + (3 - first_wd) % 7 + 7).date()  # 두 번째 목요일
        if d < expiry or (d == expiry and now.hour < 16):
            return f"A01{y % 10}{qm:02d}"
        # 만기 경과 → 다음 분기월로 이동
        m = qm + 1
        if m > 12:
            m = 1
            y += 1
    return None

def _call_k200_futures_api(url_path, action, tr_id, params):
    """국내선물옵션 시세 TR 호출 (조회 전용, 실전 모드 전용).

    모의투자 서버는 선물 TR을 지원하지 않는다(현재가 HTTP 500, 차트 'MCI전송 오류').
    모의 모드에서 실전 서버를 사용하지 않는다는 운영 방침에 따라 우회 없이 실패를 반환하며,
    코스피200선물 지수는 표시 계층(market)에서 모드 1/토스 시 목록에서 제외된다.
    """
    if config.session.is_simulation:
        return {'rt_cd': '9999', 'msg1': '모의투자 서버는 국내선물옵션 시세 TR을 지원하지 않습니다'}
    return call_api(url_path, "domestic", "quotations", action, params=params, tr_id=tr_id, retries=0)

def get_k200_futures_quote(mrkt_div_code, iscd):
    """코스피200 선물 현재가/전일대비/등락률 조회 (FHMIF10000000).

    mrkt_div_code: 'F'(주간) / 'CM'(야간). 야간 등락률은 주간 종가 대비(KIS 제공값 그대로).
    성공 시 {'current','diff','rate'} dict, 실패 시 None.
    """
    if config.session.is_toss:
        return None
    url = constants.API_URLS["DOMESTIC"]["QUOTATIONS"]["FUT_PRICE"]
    params = {"FID_COND_MRKT_DIV_CODE": mrkt_div_code, "FID_INPUT_ISCD": iscd}
    data = _call_k200_futures_api(url, "fut_price", "FHMIF10000000", params)
    if data.get('rt_cd') == '0':
        out = data.get('output1') or {}
        try:
            current = float(out.get('futs_prpr'))
            diff = float(out.get('futs_prdy_vrss'))
            rate = float(out.get('futs_prdy_ctrt'))
            if current > 0:
                return {'current': current, 'diff': diff, 'rate': rate}
        except (TypeError, ValueError):
            pass
    else:
        logger.debug(f"[API] K200선물 시세({mrkt_div_code}/{iscd}) 조회 실패: {data.get('msg1')}")
    return None

def get_k200_futures_chart(mrkt_div_code, iscd):
    """코스피200 선물 일봉 조회 (FHKIF03020100, 1콜 최대 100건 → 기간 분할로 최대 ~300봉).

    mrkt_div_code: 'F'(주간) / 'CM'(야간). 반환 스키마는 지수 차트와 동일:
    ['date','open','high','low','close','volume'] (오름차순, attrs['source']='KIS').
    근월물 상장기간이 짧으면 확보되는 만큼만 반환한다.
    """
    if config.session.is_toss:
        return pd.DataFrame()

    url_path = constants.API_URLS["DOMESTIC"]["QUOTATIONS"]["FUT_CHART"]
    now = datetime.now()
    all_items = []
    current_end_date = now.strftime("%Y%m%d")
    retry_count = 0
    while len(all_items) < 300 and retry_count < 10:
        params = {
            "FID_COND_MRKT_DIV_CODE": mrkt_div_code,
            "FID_INPUT_ISCD": iscd,
            "FID_INPUT_DATE_1": (now - timedelta(days=730)).strftime("%Y%m%d"),
            "FID_INPUT_DATE_2": current_end_date,
            "FID_PERIOD_DIV_CODE": "D"
        }
        data = _call_k200_futures_api(url_path, "fut_chart", "FHKIF03020100", params)
        if data.get('rt_cd') == '0':
            items = data.get('output2', [])
            # 빈 행(과거 미상장 구간) 제거
            items = [it for it in items if it.get('stck_bsop_date')]
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
                logger.warning(f"[API] K200선물 차트({mrkt_div_code}/{iscd}) 조회 실패: {data.get('msg1')} (Code: {data.get('msg_cd')})")
            break

    if not all_items:
        return pd.DataFrame()

    df = pd.DataFrame(all_items)
    df.drop_duplicates(subset=['stck_bsop_date'], inplace=True)
    rename_map = {
        'stck_bsop_date': 'date',
        'futs_prpr': 'close',
        'futs_oprc': 'open',
        'futs_hgpr': 'high',
        'futs_lwpr': 'low',
        'acml_vol': 'volume'
    }
    df.rename(columns=rename_map, inplace=True)
    for col in ['close', 'open', 'high', 'low', 'volume']:
        if col in df.columns:
            df[col] = pd.to_numeric(df[col], errors='coerce')
        else:
            df[col] = 0.0
    df = df[['date', 'open', 'high', 'low', 'close', 'volume']].dropna(subset=['close'])
    df = df.sort_values('date', ascending=True).reset_index(drop=True)
    df.attrs['source'] = 'KIS'
    return df

def _nxt_quote_window():
    """NXT(대체거래소) 보조 시세 조회가 의미있는 시간대인지 판단한다(TPS 절감용 시간대 게이트).

    정규장(09:00~15:30)에는 KRX가 대표가이고 NXT와 사실상 동일하므로, 종목당 NXT 보조
    호출을 생략해 전역 TPS 부담을 줄인다(분석 속도 개선). KRX가 닫혀 NXT 시세가 유일하게
    유효한 NXT 단독 거래시간(프리 08:00~09:00, 애프터 15:30~20:00)에만 조회한다.
    그 외 시간(야간)·휴장일은 NXT가 닫혀 빈 응답이므로 생략한다.
    """
    try:
        if is_holiday_today():
            return False
    except Exception:
        pass
    now = datetime.now().strftime("%H%M")
    return ("0800" <= now < "0900") or ("1530" <= now <= "2000")

def fetch_nxt_price(code):
    """NXT(대체거래소) 현재가만 단독 조회한다. (모의투자/오류/미체결 시 0 반환)

    base 현재가를 이미 확보한 경로(개요 테이블 등)에서 NXT 시세만 추가로 병합할 때
    사용한다. 모의투자(VTS)는 NXT 미지원이라 ReadTimeout 방지를 위해 조회를 건너뛴다.
    """
    if config.session.is_simulation:
        return 0
    try:
        nxt_url = constants.API_URLS["DOMESTIC"]["QUOTATIONS"]["PRICE"]
        # [수정] retries=1: 장전(08:00~09:00) 오버뷰 팬아웃 중 EGW00201(초당 거래건수 초과)에 걸리면
        #  call_api의 스로틀 백오프 재시도가 작동해 회복되도록 한다(retries=0이면 즉시 0→KRX 전일종가
        #  폴백→등락률 0% stale). nxtSupported=false 종목은 rt_cd 0·stck_prpr 0 정상응답이라 무관.
        nxt_res = call_api(nxt_url, "domestic", "quotations", "price", params={"fid_cond_mrkt_div_code": "NX", "fid_input_iscd": code}, timeout=2, retries=1)
        if nxt_res and nxt_res.get('rt_cd') == '0' and nxt_res.get('output'):
            nxt_price = nxt_res['output'].get('stck_prpr')
            if nxt_price and safe_int(nxt_price) > 0:
                return safe_int(nxt_price)
    except Exception as e:
        logger.debug(f"[API] NXT(대체거래소) 시세 조회 오류 (NX 코드 시도): {e}")
    return 0

# ==========================================================
# NXT(대체거래소) 마지막 종가 기억 — 야간/주말/휴장 시 현재가 표시용 (실전 전용)
#  거래시간(프리 08:00~09:00, 애프터 15:30~20:00) 동안 받은 NXT 현재가를 보관했다가,
#  거래가 없는 시간대(야간 20:00~익일 08:00 / 주말 / 휴장일)에는 KRX 정규장 종가 대신
#  '마지막 NXT 종가'를 현재가로 노출한다(다음 거래일 개장 전까지). 디스크에 영속하여 재시작에도 보존.
#  모의투자(VTS)는 NXT 미지원이므로 이 경로를 타지 않는다(항상 KRX 종가).
# ==========================================================
_nxt_last_close = {}
_nxt_last_close_lock = threading.RLock()
_nxt_last_close_loaded = False
_nxt_last_close_dirty = False
_nxt_last_close_saved_at = 0.0
_NXT_RECALL_MAX_AGE_DAYS = 5   # 연휴 고려: 마지막 NXT 종가를 최대 5일까지 유효로 인정

def _nxt_close_file():
    base = getattr(config, 'DATA_DIR', None) or getattr(config, 'JSON_DIR', '.')
    return os.path.join(base, 'nxt_last_close.json')

def _nxt_load_last_close():
    global _nxt_last_close_loaded
    if _nxt_last_close_loaded:
        return
    _nxt_last_close_loaded = True
    try:
        path = _nxt_close_file()
        if os.path.exists(path):
            with open(path, 'r', encoding='utf-8') as f:
                data = json.load(f)
            if isinstance(data, dict):
                with _nxt_last_close_lock:
                    _nxt_last_close.update(data)
    except Exception as e:
        logger.debug(f"[NXT] 마지막 종가 캐시 로드 실패: {e}")

def _nxt_save_last_close(force=False):
    """디스크 쓰기를 60초 throttle 한다(SD카드 보호). force=True면 즉시 저장."""
    global _nxt_last_close_dirty, _nxt_last_close_saved_at
    if not _nxt_last_close_dirty:
        return
    now = time.time()
    if not force and (now - _nxt_last_close_saved_at) < 60:
        return
    try:
        with _nxt_last_close_lock:
            snapshot = dict(_nxt_last_close)
        with open(_nxt_close_file(), 'w', encoding='utf-8') as f:
            json.dump(snapshot, f)
        _nxt_last_close_dirty = False
        _nxt_last_close_saved_at = now
    except Exception as e:
        logger.debug(f"[NXT] 마지막 종가 캐시 저장 실패: {e}")

def _nxt_remember_close(code, price):
    """거래시간에 받은 NXT 현재가를 '마지막 종가'로 기억한다."""
    global _nxt_last_close_dirty
    try:
        p = int(price)
    except (TypeError, ValueError):
        return
    if p <= 0:
        return
    _nxt_load_last_close()
    with _nxt_last_close_lock:
        _nxt_last_close[code] = {'price': p, 'date': datetime.now().strftime('%Y%m%d')}
    _nxt_last_close_dirty = True
    _nxt_save_last_close()

def _nxt_recalled_close(code):
    """야간/주말/휴장 시 보여줄 NXT 마지막 종가. 너무 오래된(>5일) 값은 폐기(0 반환)."""
    _nxt_load_last_close()
    with _nxt_last_close_lock:
        e = _nxt_last_close.get(code)
    if not e:
        return 0
    try:
        d = datetime.strptime(e.get('date', ''), '%Y%m%d')
        if (datetime.now() - d).days > _NXT_RECALL_MAX_AGE_DAYS:
            return 0
        return int(e.get('price', 0))
    except Exception:
        return 0

def _nxt_quote_phase():
    """실전 NXT 시세 처리 단계를 '한 번의 휴장 판정'으로 결정한다(중복 휴장조회 방지).
       'active'   : NXT 거래시간(프리 08:00~09:00 / 애프터 15:30~20:00) → 라이브 NXT 사용
       'offhours' : 야간(20:00~익일 08:00)·주말·휴장 → 라이브 NXT 시도 후 없으면 마지막 종가
       'skip'     : 정규장(09:00~15:30) 등 → KRX 대표가만 사용
    """
    try:
        holiday = is_holiday_today()   # 주말·공휴일 포함
    except Exception:
        holiday = False
    now = datetime.now().strftime("%H%M")
    if not holiday and (("0800" <= now < "0900") or ("1530" <= now <= "2000")):
        return 'active'
    if holiday or now >= "2000" or now < "0800":
        return 'offhours'
    return 'skip'

# [최적화] 관심종목 멀티시세 세션 비활성 플래그 (TR 미지원 서버에서 1회 실패 후 재시도 방지)
_MULTI_PRICE_DISABLED = False
_MULTI_PRICE_DISABLED_AT = 0.0
# [Fix] 멀티시세 배치 실패 시 '세션 영구 비활성' → '쿨다운 일시 비활성'.
#  EGW00201(초당 거래건수 초과)·타임아웃 같은 일시 오류 1회로 세션 내내 배치가 꺼지면,
#  장전/장후(NXT)엔 현재가만 전일 종가로 굳고(등락률 0%) 체결강도는 별도 콜이라 신선하게
#  갱신되는 비대칭 stale이 생긴다(실측: 현대건설 강도 132%·등락률 0%). 쿨다운 후 재시도해
#  일시 오류에서 자동 복구한다. (TR 미지원 환경이어도 쿨다운당 1콜 낭비에 그침)
_MULTI_PRICE_RETRY_COOLDOWN_SEC = 600

def get_multi_current_prices(codes, market_div="J"):
    """[최적화] 관심종목(멀티종목) 시세조회(FHKST11300006)로 국내 현재가를 30종목/1콜 일괄 수집.

    종목당 1콜씩 나가던 현재가 REST를 N/30콜로 줄여 TPS 소모를 대폭 절감한다
    (모의투자 2 TPS 환경에서 특히 효과 큼). 응답 필드를 개별 현재가 API(output) 이름으로
    정규화해 반환하므로 호출측은 기존 필드명 그대로 사용한다.
      stck_prpr←inter2_prpr, prdy_vrss←inter2_prdy_vrss, stck_oprc/hgpr/lwpr←inter2_*,
      stck_sdpr←inter2_sdpr, stck_prdy_clpr←inter2_prdy_clpr,
      rprs_mrkt_kor_name←kospi_kosdaq_cls_name (prdy_ctrt/prdy_vrss_sign/acml_vol는 동일명)
    52주 고저(w52_*)는 이 TR이 제공하지 않으므로 '_src'='multi' 마커를 남기고,
    호출측(_analyze_table_row)이 차트(250봉)로 보강한다.

    반환: {code: 정규화 output dict}. TR 미지원(모의 등)·오류 시 None을 반환하며,
    쿨다운(_MULTI_PRICE_RETRY_COOLDOWN_SEC) 동안 비활성화되어 호출측이 종목별 조회로 폴백한다.
    (쿨다운 경과 후 자동 재시도 — 일시 오류로 세션 전체가 영구 비활성되지 않도록)
    """
    global _MULTI_PRICE_DISABLED, _MULTI_PRICE_DISABLED_AT
    if _MULTI_PRICE_DISABLED:
        if time.time() - _MULTI_PRICE_DISABLED_AT < _MULTI_PRICE_RETRY_COOLDOWN_SEC:
            return None
        _MULTI_PRICE_DISABLED = False  # 쿨다운 경과 → 재시도 허용
    if not codes or config.session.is_toss:
        return None
    if not getattr(config, 'USE_MULTI_PRICE', True):
        return None
    result = {}
    try:
        for i in range(0, len(codes), 30):
            chunk = codes[i:i + 30]
            params = {}
            for j, c in enumerate(chunk, start=1):
                params[f"FID_COND_MRKT_DIV_CODE_{j}"] = market_div
                params[f"FID_INPUT_ISCD_{j}"] = c
            res = call_api("uapi/domestic-stock/v1/quotations/intstock-multprice",
                           "domestic", "quotations", "multi_price", params=params,
                           tr_id="FHKST11300006", timeout=5, retries=1)
            if not res or res.get('rt_cd') != '0':
                raise RuntimeError(f"rt_cd={res.get('rt_cd') if res else None} msg={res.get('msg1', '') if res else ''}")
            outputs = res.get('output') or res.get('output1') or []
            for row in outputs:
                code = str(row.get('inter_shrn_iscd', '')).strip()
                prpr = str(row.get('inter2_prpr', '')).strip()
                if not code or not prpr:
                    continue
                result[code] = {
                    '_src': 'multi',
                    'stck_prpr': prpr,
                    'prdy_vrss': row.get('inter2_prdy_vrss', '0'),
                    'prdy_vrss_sign': row.get('prdy_vrss_sign', ''),
                    'prdy_ctrt': row.get('prdy_ctrt', '0'),
                    'acml_vol': row.get('acml_vol', '0'),
                    'stck_oprc': row.get('inter2_oprc', '0'),
                    'stck_hgpr': row.get('inter2_hgpr', '0'),
                    'stck_lwpr': row.get('inter2_lwpr', '0'),
                    'stck_sdpr': row.get('inter2_sdpr', '0'),
                    'stck_prdy_clpr': row.get('inter2_prdy_clpr', '0'),
                    'rprs_mrkt_kor_name': row.get('kospi_kosdaq_cls_name', ''),
                }
        if not result:
            raise RuntimeError("응답에 유효 종목 없음")

        # [보강] 실전 응답에서 kospi_kosdaq_cls_name이 빈 값으로 오는 경우가 실측 확인되어,
        # 관심목록(stock.json)의 exchange 정보로 시장구분을 보강한다.
        # (시장 국면 보정에서 코스닥 종목이 KOSPI로 오분류되는 것 방지)
        try:
            exch_map = {}
            sd = getattr(config.session, 'stock_data', None) or {}
            for key in ("stocks_kr", "etfs_kr"):
                for s in sd.get(key, []):
                    if s.get('code') and s.get('exchange'):
                        exch_map[s['code']] = str(s['exchange']).upper()
            for c, out in result.items():
                if not out.get('rprs_mrkt_kor_name'):
                    out['rprs_mrkt_kor_name'] = exch_map.get(c, '')
        except Exception:
            pass

        return result
    except Exception as e:
        _MULTI_PRICE_DISABLED = True
        _MULTI_PRICE_DISABLED_AT = time.time()
        logger.info(f"[MultiPrice] 관심종목 멀티시세 일시 비활성({_MULTI_PRICE_RETRY_COOLDOWN_SEC}s): {e} → 종목별 현재가 조회로 폴백")
        return None

# [최적화] NXT 멀티시세 비활성 플래그 (KRX 'J' 멀티시세와 분리 — NX 미지원이 J를 끄지 않도록)
#  [Fix] J와 동일하게 쿨다운 일시 비활성: 장전/장후 EGW00201 1회로 NX 병합이 세션 내내 꺼지면
#  현재가가 KRX(전일 종가)로 굳어 '강도만 신선한' stale 증상이 재발하므로 쿨다운 후 재시도한다.
_MULTI_PRICE_NXT_DISABLED = False
_MULTI_PRICE_NXT_DISABLED_AT = 0.0

def _fetch_multi_nxt_raw(codes):
    """NXT(NX) 멀티시세 배치 → {code: {'prpr':int,'vol':int}}. 미지원/오류 시 빈 dict(쿨다운 비활성)."""
    global _MULTI_PRICE_NXT_DISABLED, _MULTI_PRICE_NXT_DISABLED_AT
    if _MULTI_PRICE_NXT_DISABLED:
        if time.time() - _MULTI_PRICE_NXT_DISABLED_AT < _MULTI_PRICE_RETRY_COOLDOWN_SEC:
            return {}
        _MULTI_PRICE_NXT_DISABLED = False  # 쿨다운 경과 → 재시도 허용
    if not codes or config.session.is_simulation:
        return {}
    out = {}
    try:
        for i in range(0, len(codes), 30):
            chunk = codes[i:i + 30]
            params = {}
            for j, c in enumerate(chunk, start=1):
                params[f"FID_COND_MRKT_DIV_CODE_{j}"] = "NX"
                params[f"FID_INPUT_ISCD_{j}"] = c
            res = call_api("uapi/domestic-stock/v1/quotations/intstock-multprice",
                           "domestic", "quotations", "multi_price", params=params,
                           tr_id="FHKST11300006", timeout=5, retries=1)
            if not res or res.get('rt_cd') != '0':
                raise RuntimeError(f"rt_cd={res.get('rt_cd') if res else None} msg={res.get('msg1', '') if res else ''}")
            for row in (res.get('output') or res.get('output1') or []):
                c = str(row.get('inter_shrn_iscd', '')).strip()
                p = safe_int(row.get('inter2_prpr'))
                if c and p > 0:  # nxtSupported=false 종목은 prpr 0 → 제외(KRX 값 유지)
                    out[c] = {'prpr': p, 'vol': safe_int(row.get('acml_vol'))}
        return out
    except Exception as e:
        _MULTI_PRICE_NXT_DISABLED = True
        _MULTI_PRICE_NXT_DISABLED_AT = time.time()
        logger.info(f"[MultiPrice] NXT 멀티시세 일시 비활성({_MULTI_PRICE_RETRY_COOLDOWN_SEC}s): {e} → NXT 병합 생략(KRX 대표가 사용)")
        return {}

def get_multi_current_prices_nxt(codes):
    """KRX(J) 멀티시세에 NXT(NX) 멀티시세를 병합해 반환(장전 08:00~09:00·장후 15:30~20:00용).

    종목별 fetch_nxt_price(NX 단건) 팬아웃이 EGW00201(초당 거래건수 초과)을 유발해 현재가가
    전일종가로 stale 폴백되던 문제를, NX도 30종목/1콜 배치로 바꿔 콜 수를 대폭 줄인다.
    NX에 살아있는 체결가가 있으면 그 값으로 stck_prpr을 교체(등락률은 표시부가 stck_sdpr
    기준으로 재계산). NX 미지원/실패 시 KRX 결과만 반환(세션 내 NXT 병합 자동 비활성).
    """
    base = get_multi_current_prices(codes)  # KRX 'J' (실패 시 None → 종목별 폴백)
    if not base:
        return base
    nxt = _fetch_multi_nxt_raw(codes)
    if not nxt:
        return base
    for c, o in base.items():
        n = nxt.get(c)
        if n and n['prpr'] > 0:
            o['stck_prpr'] = str(n['prpr'])
            if n['vol'] > 0:
                o['acml_vol'] = str(n['vol'])
    return base

def _overseas_tv_fallback_price(code, fast_info_ttl=3.0):
    """해외 현재가: KIS/토스 조회 실패 시 TradingView 시세로 폴백한다.
    성공 시 KIS get_current_price_data 형태({rt_cd:'0', output:{...}})를, 실패 시 None을 반환한다.
    가격·등락률 일관성을 위해 diff/rate는 전일 종가(base) 기준으로 함께 재계산해 채운다.
    (yfinance는 장외가 미제공이라 src=='tv'만 채택. mode 1/2/3 공통 폴백 경로)
    """
    try:
        fi = get_yf_fast_info(code, ttl=fast_info_ttl)
        if fi and fi.get('src') == 'tv' and fi.get('last_price'):
            last_v = float(fi['last_price'])
            prev_v = fi.get('regular_market_previous_close')
            if last_v > 0:
                out_o = {'last': str(last_v), '_src': 'tv_fallback'}
                if prev_v is not None and float(prev_v) > 0:
                    prev_v = float(prev_v)
                    out_o['base'] = str(prev_v)
                    out_o['diff'] = str(round(last_v - prev_v, 4))
                    out_o['rate'] = str(round((last_v - prev_v) / prev_v * 100, 2))
                return {'rt_cd': '0', 'output': out_o}
    except Exception as e:
        logger.debug(f"[API] 해외 현재가 TV 폴백 실패({code}): {e}")
    return None


def get_current_price_data(code, is_overseas, include_nxt=True, cache_ttl=3.0, fast_info_ttl=3.0):
    """현재가 조회. include_nxt=False면 NXT(대체거래소) 보조 호출을 생략한다.
    (대량 개요 조회 시 종목당 1콜을 줄여 전역 TPS 부담을 낮춘다. 주문/상세 경로는 기본값 True 유지)
    cache_ttl: 캐시 재사용 허용 시간(초). 개요/예열 경로는 더 큰 값으로 백그라운드 예열 데이터를 재사용한다.
    fast_info_ttl(해외 전용): KIS 조회 실패 시 TV 폴백에 쓰는 fast_info 캐시 허용 시간(초).
      개요(대량) 경로는 TV 일괄 예열 캐시를 재사용하도록 크게(예: 30초) 주고,
      주문/개별 분석 경로는 기본 3초로 실시간성을 유지한다.
      (해외 현재가는 KIS last/diff/rate를 1차 신뢰하며, TV는 KIS 실패 시에만 사용. yfinance 미사용)
    """
    if config.session.is_toss:
        return _toss_current_price_data(code, is_overseas)
    # NXT 포함 여부에 따라 캐시를 분리하여 주문 경로(NXT 포함)와 개요 경로(NXT 미포함)가 섞이지 않게 한다.
    cache_key = f"cp_{code}_{is_overseas}" if include_nxt else f"cp_{code}_{is_overseas}_nonxt"
    cached = _get_micro_cache(cache_key, ttl=cache_ttl) # [수정] 실시간 시세 반영을 위해 캐시 유지 시간을 3초로 단축
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
            # [수정] 모의투자(VTS)는 NXT 미지원이라 fetch_nxt_price가 0을 반환한다.
            # [최적화] 정규장(09:00~15:30)엔 KRX가 대표가이므로 NXT 보조호출을 생략(_nxt_quote_window)해
            #  종목당 호출을 절반으로 줄인다. NXT 단독시간(프리/애프터)에만 NXT를 조회한다.
            out = res.get('output', {})
            # 모의투자(VTS)는 NXT 미지원 → 항상 KRX 종가. 실전만 NXT 병합/회상.
            if include_nxt and not config.session.is_simulation:
                phase = _nxt_quote_phase()
                if phase in ('active', 'offhours'):
                    # 거래시간이든 야간이든 KIS 라이브 NXT가를 먼저 시도한다.
                    nxt_price = fetch_nxt_price(code)
                    if nxt_price > 0:
                        out['ats_prpr'] = str(nxt_price)
                        _nxt_remember_close(code, nxt_price)         # 받은 값은 항상 기억
                    elif phase == 'offhours':
                        # 야간에 KIS가 NXT를 안 주면 기억한 마지막 NXT 종가를 노출(다음 개장 전까지)
                        recalled = _nxt_recalled_close(code)
                        if recalled > 0:
                            out['ats_prpr'] = str(recalled)

            _set_micro_cache(cache_key, res)
        return res
    
    if is_overseas:
        cached_ex = config.session.exchange_cache.get(code)
        # [주간거래] 데이마켓 세션 중이면 주간 거래소 코드(BAQ/BAY/BAA)를 먼저 시도한다.
        #  이 분기가 없으면 세션 내내 직전 정규장 마감가가 그대로 굳는다.
        exchanges = us_excd_candidates(cached_ex)

        for excd in exchanges:
            params = {"AUTH": "", "EXCD": excd, "SYMB": code}
            data = call_api(constants.API_URLS["OVERSEAS"]["QUOTATIONS"]["PRICE"], "overseas", "quotations", "price", params=params, timeout=3)
            if data.get('rt_cd') == '0':
                if float(data.get('output', {}).get('last', 0) or 0) > 0:
                    # [주간거래] 캐시·stock.json에는 항상 '정규장' 코드를 저장한다.
                    #  주간 코드(BAQ 등)가 저장되면 정규장 시간대 조회와 주문 경로가 깨진다.
                    reg_excd = US_REGULAR_EXCD.get(excd, excd)
                    if cached_ex != reg_excd: config.session.update_cache_and_save(code, reg_excd)

                    # [수정] 장외(프리/애프터) 시세: KIS 응답을 1차 신뢰한다.
                    #  KIS 현재체결가의 last/diff/rate는 프리·애프터장에도 갱신되므로, 기존의
                    #  TV/yfinance 덮어쓰기(특히 yfinance fast_info는 정규장가만 제공)가 오히려
                    #  신선한 KIS 가격을 정지시키고 등락률과 불일치를 만들던 문제를 제거.
                    #  단, last만 동결되고 rate는 갱신되는 비정합 응답에 대비해 KIS 자체 필드로 역산 보정한다.
                    try:
                        out_o = data['output']
                        base_v = float(out_o.get('base', 0) or 0)
                        rate_v = float(out_o.get('rate', 0) or 0)
                        last_v = float(out_o.get('last', 0) or 0)
                        if base_v > 0 and rate_v != 0:
                            expected = base_v * (1 + rate_v / 100.0)
                            # 0.1% 이상 괴리 = last 동결 감지 (rate 반올림 오차 최대 0.005%의 20배 여유)
                            if abs(last_v - expected) / base_v > 0.001:
                                out_o['last'] = str(round(expected, 4))
                                logger.debug(f"[API] {code} 해외 last 정합성 보정: {last_v} -> {expected:.4f} (base {base_v}, rate {rate_v}%)")
                    except Exception as e:
                        logger.debug(f"[API] 해외 last 정합성 보정 오류({code}): {e}")

                    _set_micro_cache(cache_key, data)
                    return data

        # [폴백] KIS 전 거래소 조회 실패 시에만 TradingView 시세로 대체한다.
        #  (yfinance는 장외가 미제공이라 사용하지 않음. fast_info_ttl: 개요 경로는 예열 캐시 재사용)
        res_tv = _overseas_tv_fallback_price(code, fast_info_ttl)
        if res_tv is not None:
            _set_micro_cache(cache_key, res_tv)
            return res_tv

        res_err = {'rt_cd': '9999'}
        return res_err
    return {'rt_cd': '9999'}

def get_current_price(code, is_overseas):
    """현재가 단일 값 조회 (실패 시 0 반환)"""
    # [WS] 실시간 피드에 신선한 현재가가 있으면 REST 호출 없이 즉시 반환(TPS 절감).
    #  미구독/끊김/정규장 외(KRX 정지)면 None → 아래 REST 경로로 자동 폴백한다.
    if not is_overseas and getattr(config, 'USE_WEBSOCKET', True) and not config.session.is_toss:
        try:
            import realtime
            p = realtime.get_feed().get_price(code, max_age=getattr(config, 'WS_DATA_TTL_SEC', 3.0))
            if p and p > 0:
                return p
        except Exception:
            pass
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
    if config.session.is_toss:
        return _toss_order_book(code)
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

def is_toss_ask_bid_window():
    """[토스] 매도잔량비 유효 시간창(NXT 운영시간 08:00~20:00, 휴장일 제외) 여부.

    KIS 모드의 체결강도 표시 시간창과 동일. 표시 경로(테이블 헤더/셀)와
    get_ask_bid_ratio 게이트가 공유해 '값 미제공 시 컬럼 표기 자체를 생략'을 일관 처리한다.
    """
    try:
        if is_holiday_today():
            return False
    except Exception:
        pass
    _now_hhmm = datetime.now().strftime("%H%M")
    return "0800" <= _now_hhmm <= "2000"


def get_ask_bid_ratio(code, is_overseas=False):
    """매도/매수 총잔량 비율(비대칭성)만 필요한 수급 게이트용 헬퍼.

    10호가 상세가 필요없는 경로(매수후보·매도조건 분석의 ask_bid_ratio)에서 사용한다.
    WS 실시간 호가 총잔량이 신선하면 REST 없이 즉시 계산 → 종목당 호가 REST 1콜을 절감한다.
    미구독/끊김/해외/토스면 REST(get_order_book) out1 총잔량으로 자동 폴백한다.

    반환: float 비율(매도/매수). 매수잔량 0·매도만 존재 시 99.9. 데이터 없으면 None.
    """
    # [토스] 매도잔량비 유효 시간창 게이트 — KIS 모드의 체결강도 표시와 동일하게
    #  NXT 운영시간(프리 08:00 개장 ~ 애프터 20:00 마감, 휴장일 제외)에만 유효값을 반환한다.
    #  그 외 시간대의 토스 호가는 마지막 스냅샷(동결)이라 수급 지표로서 의미가 없어
    #  None(표시 경로에서는 매도비 표기 자체를 생략)으로 처리한다. 자동매매는 어차피 이 시간창 안에서만 돌므로 영향 없음.
    if config.session.is_toss and not is_overseas and not is_toss_ask_bid_window():
        return None

    # [WS] 국내주식 실시간 호가 총잔량 우선 사용(REST 절감)
    if not is_overseas and getattr(config, 'USE_WEBSOCKET', True) and not config.session.is_toss:
        try:
            import realtime
            ob = realtime.get_feed().get_orderbook(code, max_age=getattr(config, 'WS_DATA_TTL_SEC', 3.0))
            if ob:
                ta = ob.get('total_ask') or 0
                tb = ob.get('total_bid') or 0
                if tb > 0:
                    return ta / tb
                if ta > 0:
                    return 99.9
                # 둘 다 0이면 유효 데이터 없음으로 보고 REST로 폴백
        except Exception:
            pass

    # [폴백] REST 호가창 out1 총잔량
    ob_data = get_order_book(code, is_overseas)
    if ob_data and ob_data.get('rt_cd') == '0':
        out1 = ob_data.get('output1', {})
        total_ask = safe_int(out1.get('total_askp_rsqn'))
        total_bid = safe_int(out1.get('total_bidp_rsqn'))
        if total_bid > 0:
            return total_ask / total_bid
        if total_ask > 0:
            return 99.9
    return None

def get_investor_trend(code, market_div="J"):
    # [추가] 토스 미제공: 투자자 매매동향 없음
    if config.session.is_toss:
        return []
    cache_key = f"inv_{code}_{market_div}"
    cached = _get_micro_cache(cache_key, ttl=300.0) # [수정] 수급 정보는 장중 잠정치가 천천히 갱신되는 일단위 집계라 5분 캐시로 REST/TPS 절감
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
    # [추가] 토스 미제공: 외국인 소진율 없음 (KIS로 누수되지 않도록 차단)
    if config.session.is_toss:
        return []
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

def get_realtime_vol_strength(code, is_overseas=False, exchange_code=None, include_nxt=True, cache_ttl=3.0):
    # [추가] 토스 미제공: 체결강도 없음
    if config.session.is_toss: return None
    if is_overseas: return None

    # [WS] 실시간 피드에 신선한 체결강도(H0STCNT0)가 있으면 REST 호출 없이 즉시 반환(TPS 절감).
    if getattr(config, 'USE_WEBSOCKET', True):
        try:
            import realtime
            v = realtime.get_feed().get_vol_strength(code, max_age=getattr(config, 'WS_DATA_TTL_SEC', 3.0))
            if v is not None and v > 0:
                return v
        except Exception:
            pass

    # NXT 포함 여부에 따라 캐시 분리 (대량 개요 조회는 NXT 생략하여 종목당 1콜 절감)
    cache_key = f"vol_{code}" if include_nxt else f"vol_{code}_nonxt"
    cached = _get_micro_cache(cache_key, ttl=cache_ttl) # [수정] 체결강도의 실시간성 확보를 위해 캐시 유지 시간을 3초로 단축
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
                        # [수정] 체결강도 0은 해당 거래소 당일 무거래(NXT 단독시간대엔 KRX(J)가 닫혀 항상 0)를
                        #  의미하는 무효값이므로 채택하지 않는다. 실제값은 아래 NXT(NX) 조회가 채운다.
                        #  (0을 그대로 채택→캐시하면 NXT 조회 실패 시 [0%]로 오표시되는 문제를 차단)
                        if valid_val > 0:
                            final_vol = valid_val
                    except Exception as e:
                        if config.FILE_DEBUG_LEVEL in ["DEBUG", "TRACE"]: logger.debug(f"[VOL_STRENGTH_ERROR] [{code}] Parse Error: {e}")
                        pass
            # [수정] rt_cd=0 정상 응답이면 값이 0(무거래)이어도 재시도는 무의미하므로 즉시 종료한다.
            #  (NXT 단독시간대에 J 조회를 3회 반복하면 EGW00201 스로틀만 악화 → 팬아웃 지연)
            break
        elif data.get('msg_cd') == 'EGW00201': time.sleep(0.2)
        else: time.sleep(0.2)
            
    # [추가] NXT(대체거래소) 체결강도 조회 및 병합 (NX 코드 사용)
    # [수정] 모의투자(VTS)는 NXT 미지원 → NX 조회 스킵 (불필요한 ReadTimeout 방지)
    try:
        # [수정] NX 폴백 조건을 '시간대 게이트'에서 'J 무효(final_vol is None)일 때'로 확장한다.
        #  기존엔 프리/애프터(_nxt_quote_window)에만 NX를 조회했으나, 정규장 개장 직후(09:00~09:0X)
        #  KRX(J) 체결강도(tday_rltv)가 잠시 0으로 나오는 전이 구간에서 시간대 게이트가 이미 닫혀
        #  NX 폴백이 생략돼 None→[0%]로 오표시됐다(EGW00201로 J가 스로틀 실패해도 동일).
        #  - phase=='active'(프리/애프터): 기존과 동일하게 항상 조회(J는 항상 0이라 어차피 None).
        #  - phase=='skip'(정규장 09:00~15:30): J가 유효값(>0)을 준 대다수 종목은 final_vol이
        #    채워져 NX를 호출하지 않으므로 TPS 절감은 유지되고, J가 0/실패한 종목만 NX로 보강한다.
        #  - phase=='offhours'(야간·휴장): NXT 미개장 → 조회 생략.
        _nxt_phase = _nxt_quote_phase()
        if (include_nxt and not config.session.is_simulation
                and (_nxt_phase == 'active' or (_nxt_phase == 'skip' and final_vol is None))):
            # [수정] retries=1: NXT 단독시간대엔 NX가 유일한 유효 체결강도 소스인데, 개요 팬아웃(다워커
            #  동시호출) 중 EGW00201(초당 거래건수 초과)에 걸려 retries=0으로 즉시 실패하면 J의 0으로
            #  폴백돼 [0%]로 오표시된다(간헐적·종목마다 뒤바뀜). call_api의 스로틀 백오프로 회복시킨다.
            #  (fetch_nxt_price의 동일 EGW00201 대응과 일관)
            nxt_data = call_api(constants.API_URLS["DOMESTIC"]["QUOTATIONS"]["VOL_STRENGTH"], "domestic", "quotations", "vol_strength", params={"FID_COND_MRKT_DIV_CODE": "NX", "FID_INPUT_ISCD": code}, timeout=2, retries=1)
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

def _tv_overseas_fundamentals(code):
    """[토스] 해외 종목/ETF의 PER/PBR/상장주수를 TradingView 스캐너로 조회한다.

    반환은 KIS 상세(fetch_overseas_detail_price) 형태의 부분 dict: {'perx','pbrx','shar'}
    (미확보 필드는 생략). 실패/미매칭 시 {}.
      - 기본 스캐너 쿼리는 type=stock(공통주·DR)만 반환하므로 filter2(type 제한)를 제거해
        ETF(fund)도 매칭한다. (라이브러리 내부 키라 미존재 시 pop은 무해한 no-op)
      - ETF는 total_shares_outstanding이 비어 있어 aum/nav로 상장주수를 역산한다.
      - PBR은 TradingView 표시 기준과 동일한 price_book_fq(직전 분기)를 사용한다.
      - PER은 KIS와 동일 방식으로 채운다: price_earnings_ttm이 있으면 그대로,
        없으면(적자 기업은 EPS<0이라 TV가 None 반환) 주가/|EPS|로 직접 계산한다.
        (KIS는 적자 종목도 EPS 절댓값 기준 양수 PER을 표기 — 역산으로 확인)
    """
    try:
        from tradingview_screener import Query, Column
        q = (Query()
             .select('close', 'price_earnings_ttm', 'price_book_fq',
                     'earnings_per_share_diluted_ttm', 'earnings_per_share_basic_ttm',
                     'earnings_per_share_diluted_fy',
                     'total_shares_outstanding', 'aum', 'nav', 'type')
             .set_markets('america')
             .where(Column('name') == code))
        q.query.pop('filter2', None)  # type=stock 제한 제거 → ETF/fund 포함
        _, df = q.limit(1).get_scanner_data()
    except Exception as e:
        logger.debug(f"[API] 해외 펀더멘털 TV 조회 실패({code}): {e}")
        return {}
    if df is None or df.empty:
        return {}
    row = df.iloc[0]
    out = {}
    per = row.get('price_earnings_ttm')
    if pd.notna(per):
        out['perx'] = f"{float(per):.2f}"
    else:
        # 적자 기업: TV는 PER을 None으로 주므로 KIS와 동일하게 주가/|EPS|로 계산.
        # EPS는 희석(TTM) → 기본(TTM) → 희석(FY) 순으로 사용 가능한 값을 채택.
        close_v = row.get('close')
        eps = None
        for f in ('earnings_per_share_diluted_ttm', 'earnings_per_share_basic_ttm',
                  'earnings_per_share_diluted_fy'):
            v = row.get(f)
            if pd.notna(v) and float(v) != 0:
                eps = float(v)
                break
        if pd.notna(close_v) and eps:
            out['perx'] = f"{float(close_v) / abs(eps):.2f}"
    pbr = row.get('price_book_fq')
    if pd.notna(pbr):
        out['pbrx'] = f"{float(pbr):.2f}"
    shar = row.get('total_shares_outstanding')
    if pd.isna(shar) or not shar:
        aum, nav = row.get('aum'), row.get('nav')
        if pd.notna(aum) and pd.notna(nav) and float(nav) > 0:
            shar = float(aum) / float(nav)  # ETF: 순자산/기준가로 상장주수 역산
    if pd.notna(shar) and shar:
        out['shar'] = float(shar)
    return out


def fetch_overseas_detail_price(code, excd):
    # [토스] 해외 상세(PER/PBR/상장주수)는 토스 미제공 → TradingView 스캐너로 보강한다.
    # (52주 위치는 가격 기반이라 호출부에서 토스 캔들로 별도 산출)
    if config.session.is_toss:
        cache_key = f"detail_{code}"
        cached = _get_micro_cache(cache_key, ttl=300.0)  # 펀더멘털은 일단위 변동 → 5분 캐시
        if cached is not None:
            return cached
        data = _tv_overseas_fundamentals(code)
        _set_micro_cache(cache_key, data)
        return data
    cache_key = f"detail_{code}"
    cached = _get_micro_cache(cache_key, ttl=60.0) # [수정] 상세 정보 유지 시간 연장
    if cached is not None: return cached

    exchanges = []
    if excd: exchanges.append(excd)
    for e in ["NASD", "NAS", "NYSE", "NYS", "AMEX", "AMS"]:
        if e not in exchanges: exchanges.append(e)

    # [주간거래] 데이마켓 세션 중에는 주간 코드를 먼저 시도한다.
    #  이 TR의 52주 고저·PER/PBR은 정규/주간이 동일하고 last만 라이브로 갱신되므로,
    #  주간 코드를 쓰지 않으면 52주 위치가 직전 정규장 종가 기준으로 계산된다.
    if us_day_market_session():
        day = []
        for e in exchanges:
            d = US_DAY_MARKET_EXCD.get(e)
            if d and d not in day:
                day.append(d)
        exchanges = day + exchanges

    for target_excd in exchanges:
        params = {"AUTH": "", "EXCD": target_excd, "SYMB": code}
        data = call_api(constants.API_URLS["OVERSEAS"]["QUOTATIONS"]["DETAIL"], "overseas", "quotations", "detail", params=params, timeout=3)
        if data.get('rt_cd') == '0':
            output = data.get('output', {})
            if output.get('h52p') and float(output.get('h52p')) > 0:
                # [주간거래] 캐시·stock.json에는 항상 '정규장' 코드를 저장한다(주간 코드가 박히면
                #  정규장 시간대 조회·주문 경로가 깨진다). update_cache_and_save는 파일에 영속된다.
                reg_excd = US_REGULAR_EXCD.get(target_excd, target_excd)
                if reg_excd != excd: config.session.update_cache_and_save(code, reg_excd)
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
    if config.session.is_toss:
        return _toss_buyable_qty(stock_code, price, "KRW")
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
    if config.session.is_toss:
        return _toss_sellable_qty(stock_code)
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
    if config.session.is_toss:
        return _toss_buyable_qty(stock_code, price, "USD")
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
    if config.session.is_toss:
        return _toss_sellable_qty(stock_code)
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

def resolve_overseas_exchange(code):
    """해외 티커의 KIS 거래소 코드(NAS/NYS/AMS)를 판별해 캐시·stock.json에 저장한다.

    KIS 모드는 시세 응답 기반(find_best_exchange_code)을 쓰고, 토스 모드는 KIS API를
    쓸 수 없어 TradingView 스캐너의 거래소 접두사(NASDAQ/NYSE/AMEX)로 판별한다.
    (스캐너 기본 필터는 ETF(type=fund)를 제외하므로 필터를 직접 구성한다)
    실패 시 None — 표시는 '-' 유지.
    """
    cached = config.session.exchange_cache.get(code)
    if cached:
        return cached
    if not config.session.is_toss:
        return find_best_exchange_code(code)
    try:
        from tradingview_screener import Query
        q = Query().set_markets('america').select('close')
        q.query['filter'] = [{'left': 'name', 'operation': 'equal', 'right': code}]
        q.query.pop('filter2', None)
        _, df = q.get_scanner_data()
        if df is not None and not df.empty:
            tv_ex = str(df.iloc[0]['ticker']).split(':')[0]
            excd = {"NASDAQ": "NAS", "NYSE": "NYS", "AMEX": "AMS"}.get(tv_ex)
            if excd:
                config.session.update_cache_and_save(code, excd)
                return excd
    except Exception as e:
        logger.debug(f"[TV] 거래소 판별 실패({code}): {e}")
    return None

def find_best_exchange_code(stock_code):
    # [추가] 토스: 주문 시 거래소 코드가 불필요(토스 내부 라우팅). 기본값 반환.
    if config.session.is_toss:
        return "NAS"
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

# =========================================================================
# [추가] 토스증권(mode 3) 어댑터
#   토스 응답을 KIS 화면 코드가 기대하는 형태(output1/output2 등)로 변환한다.
#   토스가 제공하지 않는 필드는 0/공란으로 채운다.
# =========================================================================
def _toss_int(v, default=0):
    try:
        if v is None: return default
        return int(float(str(v).replace(',', '')))
    except Exception:
        return default


def _toss_float(v, default=0.0):
    try:
        if v is None: return default
        return float(str(v).replace(',', ''))
    except Exception:
        return default


def _toss_cached_daily_chart(code):
    """기준가 판별용 국내 일봉을 '캐시 우선'으로 조회한다 (메모리→디스크→최초 1회 네트워크).

    _toss_base_price는 현재가 조회마다 불리므로, 차트는 오늘자 캐시를 재사용하고
    캐시가 전혀 없을 때만(하루 최대 1회) 오버레이 없는 과거봉을 새로 받는다.
    """
    today_str = datetime.now().strftime("%Y-%m-%d")
    ck = _chart_cache_key(code, False, False)  # 브로커 네임스페이스(T_/K_)로 KIS↔TOSS 교차오염 차단
    with _CHART_CACHE_LOCK:
        c = _CHART_CACHE.get(ck)
        if c and c.get('date') == today_str:
            return c['df']
    df = _chart_disk_get(ck, today_str)
    if df is not None:
        return df
    if config.session.is_toss:
        try:
            return get_chart_data(code, False, 'daily', False)
        except Exception as e:
            logger.debug(f"[Toss] 기준가 판별용 일봉 조회 실패({code}): {e}")
    return None


# --- KRX 정규장 마감가(15:30) 캡처·저장 (mode 3 등락률 기준가) ---
#  TOSS는 전일 KRX 정규장 종가 필드를 안 준다. 역산·yfinance는 불안정해 폐기했고, 대신
#  '거래일 마감 후 정규장 분봉의 15:30 종가'(=KRX 마감가)를 그날 1회 캡처해 영속 저장한다.
#  역산·계산 없이 '저장된 값을 그대로' 읽어 다음 거래일의 등락률 기준가로 쓴다(HTS와 일치).
#  캡처 못한 날(앱 미구동 등)은 전일 NXT 종가(일봉 직전 캔들)로 안전 폴백한다.
_toss_krx_close_store = None
_toss_krx_close_lock = threading.Lock()


def _toss_krx_close_path():
    return os.path.join(config.JSON_DIR, "toss_krx_close.json")


def _toss_krx_close_load_locked():
    global _toss_krx_close_store
    if _toss_krx_close_store is None:
        try:
            with open(_toss_krx_close_path(), 'r', encoding='utf-8') as f:
                _toss_krx_close_store = json.load(f)
            if not isinstance(_toss_krx_close_store, dict):
                _toss_krx_close_store = {}
        except Exception:
            _toss_krx_close_store = {}
    return _toss_krx_close_store


#  저장 형식: {code: {date: {"c": 종가, "s": 출처}}} — 출처 'yf'는 KRX 종가로 검증된 값,
#  'cap'은 분봉 캡처값(주식은 NXT 혼입으로 부정확, 아래 _toss_capture_krx_close 주석 참조).
#  구버전은 종가만 실수로 저장했으므로, 태그 없는 값은 'cap'으로 간주해 신뢰하지 않는다.
def _toss_krx_close_unpack(v):
    """저장값 → (종가, 출처). 구형식(실수)은 ('cap')으로 본다."""
    if isinstance(v, dict):
        try:
            c = float(v.get("c") or 0)
        except Exception:
            return None, None
        return (c if c > 0 else None), (v.get("s") or "cap")
    try:
        c = float(v) if v else 0
    except Exception:
        return None, None
    return (c if c > 0 else None), "cap"


def _toss_krx_close_trusted(code, source):
    """이 출처의 저장값을 KRX 정규장 종가로 믿어도 되는가.

    'yf'는 일봉(KRX 기준) 조회로 얻은 값이라 신뢰한다. 'cap'(분봉 캡처)은 ETF/ETN만
    신뢰한다 — ETF는 NXT에서 거래되지 않아 분봉이 KRX 단독이기 때문이다(실측 2026-07-16:
    ETF 15/15 정확, 주식 0/10 정확).
    """
    if source == "yf":
        return True
    try:
        return bool(is_domestic_etf_etn(code))
    except Exception:
        return False


def _toss_krx_close_get(code, date_str, trusted_only=False):
    """저장된 KRX 정규장 마감가 조회 (없으면 None).

    trusted_only=True면 KRX 종가로 믿을 수 있는 값만 돌려준다(_toss_krx_close_trusted).
    """
    if not date_str:
        return None
    with _toss_krx_close_lock:
        store = _toss_krx_close_load_locked()
        try:
            close, source = _toss_krx_close_unpack((store.get(code) or {}).get(date_str))
        except Exception:
            return None
    if close is None:
        return None
    if trusted_only and not _toss_krx_close_trusted(code, source):
        return None
    return close


def _toss_krx_close_put(code, date_str, close, source="cap"):
    """KRX 정규장 마감가 저장 (종목당 최근 10거래일 유지, 원자적 저장).

    source: 'yf'(일봉 조회로 검증) / 'cap'(분봉 캡처). 검증값은 캡처값을 덮어쓰지만,
    반대로 캡처값이 검증값을 덮지는 않는다(오염된 값으로의 퇴행 방지).
    """
    if not date_str or not close or close <= 0:
        return
    with _toss_krx_close_lock:
        store = _toss_krx_close_load_locked()
        per = store.setdefault(code, {})
        if not isinstance(per, dict):
            per = {}
            store[code] = per
        cur_close, cur_source = _toss_krx_close_unpack(per.get(date_str))
        if cur_close is not None:
            if cur_source == "yf" and source != "yf":
                return                       # 검증값을 캡처값으로 되돌리지 않는다
            if cur_close == close and cur_source == source:
                return
        per[date_str] = {"c": float(close), "s": source}
        for k in sorted(per.keys())[:-10]:
            del per[k]
        try:
            tmp = _toss_krx_close_path() + ".tmp"
            with open(tmp, 'w', encoding='utf-8') as f:
                json.dump(store, f, ensure_ascii=False)
            os.replace(tmp, _toss_krx_close_path())
        except Exception as e:
            logger.debug(f"[Toss] KRX 마감가 저장 실패: {e}")


def _toss_after_krx_close():
    """오늘이 거래일이고 KRX 정규장 마감(15:35+, 종가 확정 여유) 이후면 True."""
    now = datetime.now()
    return market_today(False) == now.strftime('%Y%m%d') and now.strftime('%H%M') >= '1535'


def _before_nxt_premarket_open():
    """다음 NXT(대체거래소) 프리마켓 개장(08:00) 전이면 True.

    NXT 미지원(또는 장전 무체결) 종목의 '마지막 정규장 등락률'은 다음 NXT 개장(08:00)
    전까지 유지한다. 휴장일(주말·공휴일)은 하루 종일 개장 전이므로 항상 True이고,
    거래일엔 08:00 전까지만 True다. 08:00 이후엔 NXT 거래시간(프리 08~09시)이라
    체결이 시작되므로, 무체결 종목은 0%로 노출한다. (토스/KIS 공통 판정)
    """
    now = datetime.now()
    if market_today(False) != now.strftime('%Y%m%d'):
        return True  # 휴장일 — 다음 거래일 NXT 개장까지 유지
    return now.strftime('%H%M') < '0800'


def _toss_before_nxt_open():
    """(호환 별칭) 토스 경로에서 쓰는 장전(NXT 개장 전) 판정. _before_nxt_premarket_open 위임."""
    return _before_nxt_premarket_open()


def _before_krx_regular_open():
    """오늘이 거래일이고 KRX 정규장 개장(09:00) 전이면 True.

    국내 지수는 NXT 연장거래가 없어 09:00 개장 전까지 현재가=전일 종가로 등락률이 0%가 된다.
    이 구간엔 직전 정규장 최종 등락률(전일 vs 전전일)을 대신 표시하기 위한 판정.
    """
    now = datetime.now()
    return market_today(False) == now.strftime('%Y%m%d') and now.strftime('%H%M') < '0900'


def _toss_capture_krx_close(code):
    """마감 후, 오늘 정규장 분봉의 마지막(15:30) 종가를 하루 1회 저장한다.

    ETF/ETN은 이 값이 곧 KRX 마감가다. 그러나 **주식은 KRX 마감가가 아니다** —
    NXT(넥스트레이드)가 정규장 시간대에도 KRX와 병행 체결되고 토스 분봉은 두 거래소를
    섞어 주므로, 마지막 봉 종가가 KRX 종가 단일가와 어긋난다. ETF는 NXT 미거래라 오염이 없다.
    실측(2026-07-16, KIS 일봉 대조): ETF 15/15 정확, 주식은 52종목 중 26종목이 불일치
    (중앙값 0.45%, 최대 1.89%). 시간 필터로는 고칠 수 없어 'cap' 태그로 저장하고,
    주식 기준가로는 _toss_yf_krx_close(일봉=KRX 기준) 값을 우선한다.

    이미 오늘분이 저장돼 있으면(store 적중) 재조회하지 않아 마감 직후 1회 버스트로 끝난다.
    """
    if not (config.session.is_toss and _toss_after_krx_close()):
        return
    today = datetime.now().strftime('%Y%m%d')
    if _toss_krx_close_get(code, today):  # 이미 캡처됨 → 네트워크 재조회 없음
        return
    try:
        df = _toss_chart_data(code, period_type='intraday', is_overseas=False)
        if df is None or len(df) == 0:
            return
        close = _toss_float(df.iloc[-1]['close'])  # 정규장 필터 → 마지막 봉 = 15:30
        if close > 0:
            _toss_krx_close_put(code, today, close, source="cap")
    except Exception as e:
        logger.debug(f"[Toss] KRX 마감가 캡처 실패({code}): {e}")


# --- 랭킹 basePrice = 전일 KRX 정규장 종가(기준가) 라이브 조회 (1순위 소스) ---
#  /api/v1/rankings 의 price.basePrice(MARKET_* 타입)는 '전일 기준가'(=HTS 등락률 기준가)다.
#  거래대금+거래량 상위 각 100종목을 하루 1회 받아 {symbol: basePrice} 맵을 만든다(대형주 커버).
#  basePrice는 전일 종가라 장중 불변 → 거래일 단위 캐시. 랭킹 밖(중소형) 종목은 없음→하위순위로.
_toss_rank_base_map = None      # {code: basePrice}
_toss_rank_base_day = None      # 캐시 유효 거래일(YYYYMMDD)
_toss_rank_lock = threading.Lock()


def _toss_ranking_base(code):
    """랭킹 basePrice(전일 KRX 정규장 종가). 랭킹 상위 종목만 존재, 없으면 None."""
    global _toss_rank_base_map, _toss_rank_base_day
    if not config.session.is_toss:
        return None
    today = datetime.now().strftime('%Y%m%d')
    with _toss_rank_lock:
        if _toss_rank_base_map is not None and _toss_rank_base_day == today:
            return _toss_rank_base_map.get(code)
        # 하루 1회 적재 (거래대금·거래량 상위 각 100)
        mp = {}
        for rank_type in ("MARKET_TRADING_AMOUNT", "MARKET_TRADING_VOLUME"):
            try:
                res = toss_api.get_rankings(rank_type=rank_type, market_country="KR",
                                            duration="realtime", count=100)
            except toss_api.TossApiError as e:
                logger.debug(f"[Toss] 랭킹({rank_type}) 조회 실패: {e}")
                continue
            for item in (res or {}).get('rankings', []) or []:
                sym = item.get('symbol')
                bp = _toss_float(((item.get('price') or {}).get('basePrice')))
                if sym and bp > 0:
                    mp.setdefault(sym, bp)
        # 조회 자체가 전부 실패하면(빈 맵) 캐시를 세팅하지 않아 다음 호출에서 재시도한다.
        if mp:
            _toss_rank_base_map, _toss_rank_base_day = mp, today
        return mp.get(code)


# --- yfinance 국내 일봉 = KRX 정규장 종가 (3순위 소스, 과거 소급 조회 가능) ---
#  캡처(2순위)는 '그날 15:35 이후 프로그램이 mode 3로 떠 있어야' 저장되므로, 하루라도 안 띄우면
#  그날의 KRX 종가가 영구히 비어 다음 날 기준가가 NXT 종가로 폴백된다(실측 2026-07-22 한미약품:
#  KIS 기준가 372,500[KRX] vs 토스 371,500[NXT] → 등락률 -1.21% vs -0.94%).
#  yfinance 국내 일봉 종가는 KRX 정규장 기준이라 KIS 기준가와 일치하고(실측 07-21 = 372,500),
#  인증 없이 과거 아무 날짜나 조회되므로 이 커버리지 구멍을 메운다.
_toss_yf_base_miss = {}          # {(code, date): 마지막 실패 시각} — 실패 재조회 폭주 방지
_toss_yf_base_lock = threading.Lock()
_TOSS_YF_BASE_RETRY_SEC = 1800   # 실패 후 재시도 쿨다운(초)


def _toss_yf_krx_close(code, ref_date):
    """yfinance 국내 일봉에서 ref_date(YYYYMMDD)의 KRX 정규장 종가를 얻는다. 없으면 None.

    조회 성공 시 캡처 저장소에 넣어 다음부터는 2순위에서 즉시 끝나게 한다(네트워크 1회로 종료).
    실패는 쿨다운 캐시로 묶어 주기적 시세 갱신마다 yfinance를 두드리지 않게 한다.
    """
    if not code or not ref_date:
        return None
    key = (code, ref_date)
    with _toss_yf_base_lock:
        last = _toss_yf_base_miss.get(key)
        if last and (time.time() - last) < _TOSS_YF_BASE_RETRY_SEC:
            return None
    try:
        # 국내는 KOSPI(.KS)/KOSDAQ(.KQ) 접미사를 모두 시도한다(거래소 정보가 없어도 동작).
        start = (datetime.strptime(ref_date, "%Y%m%d") - timedelta(days=7)).strftime("%Y-%m-%d")
        end = (datetime.strptime(ref_date, "%Y%m%d") + timedelta(days=1)).strftime("%Y-%m-%d")
        for suffix in (".KS", ".KQ"):
            ticker = f"{code}{suffix}"
            df = fetch_yfinance_data(ticker, start=start, end=end)
            if df is None or getattr(df, 'empty', True):
                continue
            if isinstance(df.columns, pd.MultiIndex):
                try:
                    df = df.xs(ticker, axis=1, level=1)
                except Exception:
                    pass
            df.columns = [str(c).lower() for c in df.columns]
            if 'close' not in df.columns:
                continue
            for idx, val in df['close'].items():
                d = idx.strftime('%Y%m%d') if hasattr(idx, 'strftime') else str(idx).replace('-', '')[:8]
                if d == ref_date:
                    close = float(val)
                    if close > 0:
                        # 검증값으로 저장 → 다음부터 2순위(신뢰 조회)에서 즉시 종료
                        _toss_krx_close_put(code, ref_date, close, source="yf")
                        return close
    except Exception as e:
        logger.debug(f"[Toss] yfinance 기준가 조회 실패({code} {ref_date}): {e}")
    with _toss_yf_base_lock:
        _toss_yf_base_miss[key] = time.time()
    return None


def _toss_base_price(code, chart_df=None):
    """국내 등락률 기준가. 역산·계산 없이 '확보된 값을 그대로' 쓴다(단락 평가).

    우선순위(위에서 값이 나오면 즉시 반환, 아래는 실행 안 함):
      1) 랭킹 basePrice — 거래대금/거래량 상위(대형주)의 전일 KRX 정규장 종가(라이브, HTS 일치)
      2) 저장된 값 중 '검증된' 것 — yfinance로 확인된 값, 또는 ETF의 분봉 캡처값
      3) yfinance 일봉 종가 — KRX 정규장 기준. 확보 즉시 검증값으로 저장돼 다음부터 2)에서 끝난다
      4) 저장된 분봉 캡처값(주식) — NXT 혼입으로 부정확하나 NXT 종가보다는 가깝다
      5) 폴백: 전일 NXT(대체거래소) 종가 = 일봉 직전 거래일 캔들 종가

    주식의 분봉 캡처값을 2)에서 쓰지 않는 이유는 _toss_capture_krx_close 주석 참조 —
    NXT가 정규장 시간대에 병행 체결돼 토스 분봉 종가가 KRX 단일가와 어긋난다. 캡처값을
    믿으면 3)의 yfinance가 영원히 호출되지 않아 오차가 고정된다(2026-07-22 실측 6종목).
    4)·5)는 KRX 기준 HTS 등락률과 소폭 다를 수 있다(기동 시 안내).
    TOSS는 전일 KRX 종가 필드를 직접 주지 않는다.

    ref_date(전일 종가의 거래일)는 일봉 '날짜'만으로 결정한다:
      마지막 캔들일이 오늘보다 과거면 그 캔들(=장중), 오늘이면 그 직전 캔들(=마감 후).

    chart_df: 호출자가 일봉을 이미 들고 있으면 전달(재조회 방지). 미전달 시 캐시 우선 조회.
    """
    today = datetime.now().strftime('%Y%m%d')

    # 1) 랭킹 basePrice (대형주, 라이브·HTS 일치) — 있으면 여기서 종료
    rb = _toss_ranking_base(code)
    if rb:
        return rb

    try:
        df = chart_df if chart_df is not None else _toss_cached_daily_chart(code)
        if df is None or len(df) < 2:
            return None
        last_date = str(df.iloc[-1]['date']).replace('-', '')[:8]
        prev_date = str(df.iloc[-2]['date']).replace('-', '')[:8]
        ref_date = last_date if last_date < today else prev_date

        # 2) 저장된 값 중 검증된 것만 (yfinance 확인분 또는 ETF 캡처) — 네트워크 없음
        trusted = _toss_krx_close_get(code, ref_date, trusted_only=True)
        if trusted:
            return trusted

        # 3) yfinance 일봉 종가 (KRX 정규장 기준). 성공 시 검증값으로 적재되어 2)에서 종료된다.
        yf_close = _toss_yf_krx_close(code, ref_date)
        if yf_close:
            return yf_close

        # 4) 저장된 분봉 캡처값 (주식: NXT 혼입으로 부정확) — yfinance 실패 시 근사 폴백
        stored = _toss_krx_close_get(code, ref_date)
        if stored:
            return stored

        # 5) 폴백: 전일 NXT 종가 (일봉 직전 캔들 종가)
        row = df.iloc[-1] if last_date < today else df.iloc[-2]
        base = _toss_float(row['close'])
        return base if base > 0 else None
    except Exception as e:
        logger.debug(f"[Toss] 기준가 산출 실패({code}): {e}")
        return None


def _toss_prev_prev_close(code):
    """전전일(직전 정규장의 하루 전) 종가. 프리마켓 '최종 등락률' 기준가로 쓴다.

    NXT 미지원 종목은 개장 전 체결이 없어 현재가=전일 종가=기준가 → 등락률 0%가 된다.
    이때 '기준가'를 전전일 종가로 바꿔 직전 정규장의 최종 등락률(전일 vs 전전일)을
    개장 전까지 유지 표시한다. 일봉 마지막 캔들이 전일(과거)이면 그 직전(-2)이 전전일,
    이미 오늘 캔들이 있으면(-3)이 전전일이다.
    """
    today = datetime.now().strftime('%Y%m%d')
    try:
        df = _toss_cached_daily_chart(code)
        if df is None or len(df) < 2:
            return None
        last_date = str(df.iloc[-1]['date']).replace('-', '')[:8]
        idx = -2 if last_date < today else -3
        if len(df) < abs(idx):
            return None
        pp = _toss_float(df.iloc[idx]['close'])
        return pp if pp > 0 else None
    except Exception as e:
        logger.debug(f"[Toss] 전전일 종가 산출 실패({code}): {e}")
        return None


def _toss_krw_deposit():
    """토스 매수 가능 금액(KRW, 현금)을 예수금 근사치로 사용한다."""
    try:
        bp = toss_api.get_buying_power("KRW")
        return _toss_int(bp.get('cashBuyingPower')) if bp else 0
    except toss_api.TossApiError as e:
        logger.debug(f"[Toss] buying-power 조회 실패: {e}")
        return 0


def _toss_domestic_balance():
    """토스 보유주식(KR) → KIS get_domestic_balance (output1, output2) 형태."""
    try:
        ov = toss_api.get_holdings()
    except toss_api.TossApiError as e:
        logger.error(f"[Toss] 보유주식 조회 실패: {e}")
        return None, None

    items = (ov or {}).get('items', []) or []
    output1 = []
    tot_eval = 0
    for it in items:
        if it.get('marketCountry') != 'KR':
            continue
        mv = it.get('marketValue', {}) or {}
        pl = it.get('profitLoss', {}) or {}
        evlu_amt = _toss_int(mv.get('amount'))
        tot_eval += evlu_amt
        output1.append({
            'pdno': it.get('symbol', ''),
            'prdt_name': it.get('name', ''),
            'hldg_qty': str(_toss_int(it.get('quantity'))),
            'pchs_avg_pric': str(_toss_float(it.get('averagePurchasePrice'))),
            'evlu_amt': str(evlu_amt),
            'evlu_pfls_amt': str(_toss_int(pl.get('amount'))),
            'evlu_pfls_rt': str(round(_toss_float(pl.get('rate')) * 100, 2)),
            'prpr': str(_toss_int(it.get('lastPrice'))),
        })

    deposit = _toss_krw_deposit()
    summary = {
        'tot_evlu_amt': str(tot_eval + deposit),
        'scts_evlu_amt': str(tot_eval),
        'dnca_tot_amt': str(deposit),
        'nxdy_excc_amt': str(deposit),
        'prvs_rcdl_excc_amt': str(deposit),
        'bfdy_sll_amt': '0', 'bfdy_buy_amt': '0',
        'bfdy_tlex_amt': '0', 'thdt_tlex_amt': '0',
    }
    logger.info(f"[Toss] 잔고 조회 결과: 종목수={len(output1)}, 총평가금(주식)={tot_eval:,}원")
    return output1, [summary]


def _toss_overseas_balance():
    """토스 보유주식(US) → KIS get_overseas_balance 형태(list)."""
    try:
        ov = toss_api.get_holdings()
    except toss_api.TossApiError as e:
        logger.error(f"[Toss] 해외 보유주식 조회 실패: {e}")
        return []

    items = (ov or {}).get('items', []) or []
    out = []
    for it in items:
        if it.get('marketCountry') != 'US':
            continue
        pl = it.get('profitLoss', {}) or {}
        qty = _toss_float(it.get('quantity'))
        out.append({
            'ovrs_pdno': it.get('symbol', ''),
            'ovrs_item_name': it.get('name', ''),
            'prdt_name': it.get('name', ''),
            'ovrs_cblc_qty': str(qty),
            'ord_psbl_qty': str(qty),
            'pchs_avg_pric': str(_toss_float(it.get('averagePurchasePrice'))),
            'frcr_evlu_pfls_amt': str(_toss_float(pl.get('amount'))),
            'evlu_pfls_rt': str(round(_toss_float(pl.get('rate')) * 100, 2)),
            'now_pric2': str(_toss_float(it.get('lastPrice'))),
            'ovrs_now_pric': str(_toss_float(it.get('lastPrice'))),  # [추가] 화면 코드가 읽는 현재가 필드 (KIS 호환)
            '_exchange': it.get('market', 'NASD'),
        })
    return out


def _toss_daily_chart_with_tv_fallback(code, is_overseas):
    """토스 일봉을 조회하되, 해외 종목에서 토스 캔들이 비었거나 지표 계산에 부족하면
    TradingView(tvDatafeed) 일봉으로 폴백한다.

    SK hynix(ADR) 등 일부 해외 종목/ETF는 토스 캔들이 비어(또는 EMA120 산출에 못 미쳐)
    EMA/RSI/CCI/ADX가 표에서 '-'로 비는데, 이때 TV 시세로 보강한다. 국내 종목은 폴백하지 않는다.
    (tradingview_screener(스캐너)는 스냅샷만 제공하므로 시계열은 OHLC를 주는 tvDatafeed로 조회)
    """
    df = _toss_chart_data(code, 'daily', is_overseas)
    if not is_overseas:
        return df
    have = 0 if (df is None or df.empty) else len(df)
    # 120봉 미만이면 EMA(120)가 계산되지 않아 지표가 빈다 → TV 폴백 시도.
    if have >= 120:
        return df
    try:
        from modules import analysis as _analysis
        tv_df = _analysis.fetch_overseas_daily_via_tvdatafeed(code)
    except Exception as e:
        logger.debug(f"[API] 토스 해외 일봉 TV 폴백 실패({code}): {e}")
        tv_df = None
    # 토스가 일부라도 줬으면 더 긴 쪽을 채택(TV도 실패하면 기존 토스 결과 유지).
    if tv_df is not None and not tv_df.empty and len(tv_df) > have:
        return tv_df
    return df


# 국내 일봉 이상치 판정 폭. 국내 가격제한폭(±30%)보다 낮게 잡아 '제한폭에 붙은 가짜 값'만 걸러낸다.
# 실측(2026-07-22) 정상 봉의 전일종가 대비 고가 괴리는 최대 +15.6%였고, 오염 값은 -30%/-30%/+27%로
# 뚜렷이 갈렸다. 18%는 그 사이의 여유 있는 경계다.
_TOSS_DAILY_OUTLIER_GUARD = 0.18


def _toss_sanitize_daily_ohlc(df, code=""):
    """토스 국내 일봉의 상·하한가 이상치를 제거한다.

    토스 캔들은 NXT 프리마켓(08:00) 개장 직후의 상·하한가 호가 체결을 일봉 시가/고가/저가에
    그대로 섞는다. 실측 사례(전일 종가 대비 정확히 ±30% = 가격제한폭):
      KT 2025-09-03      시가·저가 36,500 (KRX 실제 52,000 / 종가 52,500)
      HD현대중공업 09-29  저가 344,500     (KRX 실제 486,500)
      삼성SDI 2026-04-22  시가·고가 820,000 (KRX 실제 686,000)
    이 값이 52주 밴드는 물론 ATR·변동성·샹들리에 트레일링 스톱까지 오염시킨다.

    판정: '종가는 전일 종가 근처인데 시가/고가/저가만 제한폭에 붙은' 봉만 손본다. 진짜 상·하한가
    급등락일은 종가도 함께 크게 움직이므로 게이트에 걸려 원본이 보존된다(예: -29.95% 하한가 마감).
    보정: 오염된 값은 임의의 클램프 값으로 바꾸지 않고 그 봉의 실제 체결가(시가/종가)로 접는다.
    (가짜 극값이 52주 고·저점으로 남지 않게 하는 것이 목적이며, 밴드를 넓히는 방향으로는 절대
     보정하지 않는다.)
    """
    if df is None or df.empty or len(df) < 2:
        return df
    g = _TOSS_DAILY_OUTLIER_GUARD
    fixed = 0
    try:
        io, ih, il, ic = (df.columns.get_loc(c) for c in ('open', 'high', 'low', 'close'))
        prev_c = float(df.iat[0, ic] or 0)
        for i in range(1, len(df)):
            o = float(df.iat[i, io] or 0)
            h = float(df.iat[i, ih] or 0)
            l = float(df.iat[i, il] or 0)
            c = float(df.iat[i, ic] or 0)
            if prev_c > 0 and c > 0 and abs(c / prev_c - 1) <= g:
                hi_lim, lo_lim = prev_c * (1 + g), prev_c * (1 - g)
                dirty = False
                if o > hi_lim or o < lo_lim:      # 시가 오염 → 그 봉의 신뢰 가능한 값(종가)으로 대체
                    o, dirty = c, True
                if h > hi_lim:
                    h, dirty = max(o, c), True
                if l < lo_lim:
                    l, dirty = min(o, c), True
                if dirty:
                    # 정합성: 고가/저가는 시가·종가를 반드시 포함해야 한다.
                    h, l = max(h, o, c), min(l, o, c)
                    df.iat[i, io], df.iat[i, ih], df.iat[i, il] = o, h, l
                    fixed += 1
            prev_c = c if c > 0 else prev_c
        if fixed:
            logger.debug(f"[Toss] 일봉 이상치 보정({code}): {fixed}봉")
    except Exception as e:
        logger.debug(f"[Toss] 일봉 이상치 보정 실패({code}): {e}")
    return df


def _toss_chart_data(code, period_type='daily', is_overseas=False):
    """토스 캔들 → KIS get_chart_data 형태의 DataFrame.
    columns=['date','open','high','low','close','volume'] (date=YYYYMMDD, 오름차순).

    일봉은 52주 위치/EMA120 정확도를 위해 nextBefore 커서로 ~250개 이상 모은다
    (토스 캔들은 호출당 최대 200개). 분봉은 단일 호출(200개).
    """
    if period_type == 'hourly':
        # 토스는 1분/일봉만 제공 → 시봉 미지원
        return pd.DataFrame()
    interval = '1m' if period_type == 'intraday' else '1d'
    # 일봉: 52주(≈250거래일) 확보.
    # 분봉: KIS와 동일하게 "당일 정규장(09:00~15:30)"만 표시한다. 토스 1분봉은 NXT(대체거래소)
    # 연장시간(08:00~20:00)까지 포함하므로, 장후(NXT 20:00)에 조회해도 당일 09:00까지 닿도록
    # 하루 분량(≈720)을 커서로 모은다. (토스 count 최대 200 → 400은 [400] invalid-request)
    target = 260 if interval == '1d' else 720
    max_pages = 4 if interval == '1d' else 5
    per_call = 200

    candles = []
    before = None
    prev_cursor = None
    page_log = []  # [진단] 분봉 페이징 추적
    try:
        for _ in range(max_pages):
            res = toss_api.get_candles(code, interval=interval, count=per_call, before=before)
            batch = (res or {}).get('candles', []) or []
            nb = (res or {}).get('nextBefore')
            if interval == '1m':
                _tsb = [str(c.get('timestamp', '')) for c in batch if c.get('timestamp')]
                page_log.append(
                    f"before={before} → {len(batch)}건"
                    f"[{min(_tsb) if _tsb else '-'}~{max(_tsb) if _tsb else '-'}] nextBefore={nb}")
            if not batch:
                break
            candles.extend(batch)
            if len(candles) >= target:
                break
            # 다음 페이지 커서: nextBefore 우선, 없으면 이번 배치의 가장 오래된 timestamp로 폴백.
            # (분봉은 nextBefore가 1페이지 후 끊기는 경우가 있어 09:00까지 못 가는 문제를 보완)
            oldest_ts = min((str(c.get('timestamp', '')) for c in batch if c.get('timestamp')),
                            default=None)
            before = nb or oldest_ts
            if not before or before == prev_cursor:  # 커서가 더 진행 못하면 중단(무한루프 방지)
                break
            prev_cursor = before
    except toss_api.TossApiError as e:
        cand_err = e
        logger.debug(f"[Toss] 캔들 조회 실패({code}): {e}")
    else:
        cand_err = None

    if interval == '1m' and (config.FILE_DEBUG_LEVEL == "DEBUG" or cand_err):
        # [진단] 페이지별 건수/시간범위/커서 + 에러를 1줄로 남긴다(에러는 항상 기록).
        logger.warning(f"[Toss] 분봉({code}) 페이징 {len(page_log)}회: "
                       + (" || ".join(page_log) if page_log else "(요청 결과 없음)")
                       + (f" | ERROR={cand_err}" if cand_err else ""))

    if not candles:
        # [진단] 일봉이 통째로 비면 화면엔 '차트 없음'·지표 전체 '-'로만 나타나 원인이 안 남는다.
        #  (토큰 만료·레이트리밋 등) 실패 사유를 항상 로그에 남긴다. 분봉은 위에서 이미 기록.
        if interval == '1d':
            logger.warning(f"[Toss] 일봉({code}) 조회 결과 없음"
                           + (f" | ERROR={cand_err}" if cand_err else " (요청은 성공했으나 캔들 0건)"))
        return pd.DataFrame()
    rows = []
    for c in candles:
        ts = str(c.get('timestamp', ''))
        if interval == '1d':
            date = ts[:10].replace('-', '')  # YYYYMMDD 문자열 (KIS 일봉과 동일)
        else:
            # 분봉: KIS(_get_intraday_chart_data)와 동일하게 Timestamp로 보관해야
            # chart.format_date가 strftime 경로를 타 'MM-DD HH:MM' 라벨을 동일하게 출력한다.
            # (12자리 문자열로 두면 format_date 어느 분기에도 안 걸려 raw 숫자가 X축에 찍힘)
            date = pd.to_datetime(ts, errors='coerce')
            if pd.notna(date) and getattr(date, 'tzinfo', None) is not None:
                date = date.tz_convert('Asia/Seoul').tz_localize(None)
        rows.append({
            'date': date,
            'open': _toss_float(c.get('openPrice')),
            'high': _toss_float(c.get('highPrice')),
            'low': _toss_float(c.get('lowPrice')),
            'close': _toss_float(c.get('closePrice')),
            'volume': _toss_float(c.get('volume')),
        })
    df = pd.DataFrame(rows).drop_duplicates(subset=['date'])
    df = df.sort_values('date', ascending=True).reset_index(drop=True)
    if interval == '1d':
        if not is_overseas:
            # NXT 프리마켓 상·하한가 체결이 섞인 봉을 먼저 정제한다(tail 이전 = 첫 봉도 전일 종가 확보).
            # 해외는 가격제한폭이 없어 '제한폭에 붙은 값' 판정이 성립하지 않으므로 제외.
            df = _toss_sanitize_daily_ohlc(df, code)
        df = df.tail(250).reset_index(drop=True)
        # 국내 일봉 종가는 NXT 연장(~20:00) 체결까지 포함한 값 그대로 둔다.
        # (과거엔 직전 봉 종가를 KRX 기준가로 역산 보정했으나 역산 로직을 폐기 —
        #  mode 3 등락률은 전일 NXT 종가 기준으로 계산하며, 기동 시 안내한다.)
        return df

    # 분봉: KIS 당일분봉과 동일하게 "당일(가장 최근 거래일)의 정규장(09:00~15:30)"만 표시.
    # 토스의 시간외/NXT 연장(08:00~/~20:00) 캔들과 날짜 교차를 모두 제거한다.
    # 장전에 조회하면 당일 정규장 데이터가 없어 빈 값이 되고(→ 호출부에서 장전 안내),
    # 장중이면 09:00~현재, 장후면 09:00~15:30 전체가 된다.
    last_day = df['date'].dt.normalize().max()
    hh, mm = df['date'].dt.hour, df['date'].dt.minute
    in_session = (hh >= 9) & ((hh < 15) | ((hh == 15) & (mm <= 30)))
    return df[(df['date'].dt.normalize() == last_day) & in_session].reset_index(drop=True)


def _toss_current_price_data(code, is_overseas):
    """토스 현재가 → KIS get_current_price_data 형태({rt_cd,output})."""
    try:
        row = toss_api.get_price(code)
    except toss_api.TossApiError as e:
        logger.debug(f"[Toss] 현재가 조회 실패({code}): {e}")
        row = None
    # [폴백] 토스 해외 현재가 조회 실패 시 TradingView 시세로 대체한다(mode 1/2와 동일 경로).
    #  국내는 KRX/NXT 전용이라 TV 폴백 대상이 아니다.
    price = (row or {}).get('lastPrice', '0')
    if is_overseas and (not row or _toss_float(price) <= 0):
        res_tv = _overseas_tv_fallback_price(code)
        if res_tv is not None:
            return res_tv
    if not row:
        return {'rt_cd': '9999'}
    # 국내(stck_prpr)/해외(last) 양쪽 경로를 모두 채운다. 외인비율 등은 토스 미제공.
    output = {
        'stck_prpr': str(_toss_int(price)),
        'last': str(_toss_float(price)),
    }
    # [추가] 국내: 기준가 대비 전일대비/등락률 필드를 채운다. 기준가=저장된 KRX 정규장 마감가
    # (있으면 HTS 일치), 없으면 전일 NXT 종가로 폴백. 마감 후엔 오늘 KRX 마감가를 1회 캡처해 저장.
    if not is_overseas:
        _toss_capture_krx_close(code)  # 마감(15:35+) 후 오늘 KRX 마감가 1회 저장(이미 있으면 즉시 반환)
        p = _toss_float(price)

        # [모드 정합성] ETF/ETN은 마감 후 현재가를 'KRX 정규장 종가'로 고정한다.
        #  ETF는 NXT 연장거래 대상이 아니라 15:30 이후 체결은 전부 KRX 시간외단일가(16:00~18:00)다.
        #  KIS 경로(mode 1/2)는 시간외단일가를 별도 TR(FHPST02300000)로만 제공해 반영하지 않으므로
        #  정규장 종가를 보여주는데, 토스 lastPrice는 시간외 체결을 그대로 반영해 두 모드가 어긋났다.
        #  (실측 2026-07-22 16:07 KODEX 코스닥150: KIS·HTS 12,525 / 토스 12,530 = 시간외 체결가.
        #   같은 시각 시간외 거래량이 있는 ETF만 값이 갈리고, 거래가 없던 ETF는 정확히 일치했다.)
        #  일봉 종가와 지표(EMA/RSI/CCI·52주 위치)의 기준은 정규장 종가이고 시간외단일가는 거래량이
        #  극히 적어(수 주~수십 주) 노이즈에 가까우므로, 마감 후에는 캡처해 둔 KRX 마감가로 고정한다.
        #  ※ 일반 주식은 NXT 연장가를 mode 1/2도 함께 노출하므로(get_multi_current_prices_nxt)
        #    여기서 고정하면 오히려 어긋난다 → ETF/ETN에만 적용한다.
        if _toss_after_krx_close() and is_domestic_etf_etn(code):
            krx_close = _toss_krx_close_get(code, datetime.now().strftime('%Y%m%d'))
            if krx_close and krx_close > 0:
                p = float(krx_close)
                output['stck_prpr'] = str(int(round(p)))
                output['last'] = str(p)

        base = _toss_base_price(code)
        if base and base > 0 and p > 0:
            output['stck_sdpr'] = str(int(base))
            output['prdy_vrss'] = str(int(round(p - base)))
            output['prdy_ctrt'] = str(round((p - base) / base * 100, 2))
            # [마감 후 최종 등락률 유지] NXT 미지원 종목은 체결이 없어 현재가(lastPrice)=전일 종가
            #  =기준가 → 등락률 0%가 된다. 다음날 NXT 개장(08:00) 전까지는 기준가를 전전일 종가로
            #  대체해 직전 정규장 최종 등락률(전일 vs 전전일)을 유지한다. 08:00(NXT 프리마켓) 이후엔
            #  대체하지 않아 KRX 개장 전까지 약 1시간은 0%로 노출된다.
            if int(round(p)) == int(round(base)) and _toss_before_nxt_open():
                pp = _toss_prev_prev_close(code)
                if pp and pp > 0:
                    output['stck_sdpr'] = str(int(pp))
                    output['prdy_vrss'] = str(int(round(p - pp)))
                    output['prdy_ctrt'] = str(round((p - pp) / pp * 100, 2))
    return {'rt_cd': '0', 'output': output}


def _toss_order_book(code):
    """토스 호가 → KIS get_order_book 형태({rt_cd,output1})."""
    try:
        ob = toss_api.get_orderbook(code)
    except toss_api.TossApiError as e:
        logger.debug(f"[Toss] 호가 조회 실패({code}): {e}")
        return {'rt_cd': '9999'}
    if not ob:
        return {'rt_cd': '9999'}
    asks = ob.get('asks', []) or []   # 낮은 가격순(최우선 매도호가가 [0])
    bids = ob.get('bids', []) or []   # 높은 가격순(최우선 매수호가가 [0])
    out1 = {}
    total_ask = 0
    total_bid = 0
    for i in range(10):
        a = asks[i] if i < len(asks) else {}
        b = bids[i] if i < len(bids) else {}
        av = _toss_int(a.get('volume'))
        bv = _toss_int(b.get('volume'))
        total_ask += av
        total_bid += bv
        out1[f'askp{i+1}'] = str(_toss_int(a.get('price')))
        out1[f'bidp{i+1}'] = str(_toss_int(b.get('price')))
        out1[f'askp_rsqn{i+1}'] = str(av)
        out1[f'bidp_rsqn{i+1}'] = str(bv)
    out1['total_askp_rsqn'] = str(total_ask)
    out1['total_bidp_rsqn'] = str(total_bid)
    return {'rt_cd': '0', 'output1': out1}


# -------------------------------------------------------------------------
# [추가] 토스 주문(메뉴 8) 어댑터: 미체결 조회 / 주문 / 정정·취소 / 주문가능수량
# -------------------------------------------------------------------------
def _toss_name_map(symbols):
    """심볼 목록 → {symbol: name} (종목명 표시용). 실패 시 빈 dict."""
    syms = [s for s in dict.fromkeys(symbols) if s]
    if not syms:
        return {}
    try:
        rows = toss_api.get_stocks(syms)
    except toss_api.TossApiError as e:
        logger.debug(f"[Toss] 종목명 조회 실패: {e}")
        return {}
    return {r.get('symbol'): r.get('name', '') for r in (rows or []) if r.get('symbol')}


def _toss_open_orders(market):
    """토스 미체결(OPEN) 주문 → KIS 미체결 형태.
    market: 'domestic'(KRW) | 'overseas'(USD)."""
    try:
        res = toss_api.get_orders(status="OPEN")
    except toss_api.TossApiError as e:
        logger.error(f"[Toss] 미체결 조회 실패: {e}")
        return []

    orders = (res or {}).get('orders', []) or []
    want_krw = (market == 'domestic')
    picked = []
    for o in orders:
        is_krw = (o.get('currency') == 'KRW')
        if is_krw != want_krw:
            continue
        picked.append(o)

    name_map = _toss_name_map([o.get('symbol') for o in picked])
    out = []
    for o in picked:
        symbol = o.get('symbol', '')
        side = o.get('side')  # BUY / SELL
        sll_buy_cd = '02' if side == 'BUY' else '01'  # KIS: 01=매도, 02=매수
        sll_buy_name = '매수' if side == 'BUY' else '매도'
        qty = _toss_int(o.get('quantity'))
        filled = _toss_int((o.get('execution') or {}).get('filledQuantity'))
        rmn = max(qty - filled, 0)
        # orderedAt: '2026-03-29T09:30:00+09:00' → ord_dt(YYYYMMDD) / ord_tmd(HHMMSS)
        ts = str(o.get('orderedAt', ''))
        ord_dt = ts[:10].replace('-', '') if len(ts) >= 10 else ''
        ord_tmd = ts[11:19].replace(':', '') if len(ts) >= 19 else ''
        name = name_map.get(symbol) or symbol

        if want_krw:
            out.append({
                'odno': o.get('orderId', ''),
                'pdno': symbol,
                'prdt_name': name,
                'sll_buy_dvsn_cd': sll_buy_cd,
                'sll_buy_dvsn_cd_name': sll_buy_name,
                'ord_qty': str(qty),
                'ord_unpr': str(_toss_int(o.get('price'))),
                'rmn_qty': str(rmn),
                'ord_tmd': ord_tmd,
                '_toss_order_type': o.get('orderType', 'LIMIT'),
            })
        else:
            out.append({
                'odno': o.get('orderId', ''),
                'pdno': symbol,
                'prdt_name': name,
                'sll_buy_dvsn_cd': sll_buy_cd,
                'ft_ord_qty': str(qty),
                'nccs_qty': str(rmn),
                'ft_ord_unpr3': str(_toss_float(o.get('price'))),
                'ord_unpr': str(_toss_float(o.get('price'))),
                'ord_dt': ord_dt,
                'ord_tmd': ord_tmd,
                'ovrs_excg_cd': o.get('_market', 'NASD'),
                '_toss_order_type': o.get('orderType', 'LIMIT'),
            })
    logger.info(f"[Toss] 미체결({market}) 조회 결과: {len(out)}건")
    return out


def _toss_today_closed_orders():
    """오늘(KST) 종료된(CLOSED) 토스 주문 목록(페이지네이션). 2초 마이크로 캐시.
    체결 감시(ConclusionMonitor)가 국내/해외 두 번 호출하므로 캐시로 중복 호출을 줄인다."""
    cache_key = "toss_closed_today"
    cached = _get_micro_cache(cache_key, ttl=2.0)
    if cached is not None:
        return cached

    today = datetime.now().strftime("%Y-%m-%d")

    def _fetch(use_range):
        out = []
        cursor = None
        for _ in range(10):  # 최대 10페이지(=1000건) 방어
            kwargs = {"status": "CLOSED", "limit": 100}
            if cursor:
                kwargs["cursor"] = cursor
            if use_range:
                kwargs["from_date"] = today
                kwargs["to_date"] = today
            res = toss_api.get_orders(**kwargs)
            out.extend((res or {}).get('orders', []) or [])
            if not (res or {}).get('hasNext'):
                break
            cursor = (res or {}).get('nextCursor')
            if not cursor:
                break
        return out

    orders = []
    try:
        orders = _fetch(use_range=True)
    except toss_api.TossApiError as e:
        # from/to 형식 등으로 실패하면 기간 없이 재조회 후 클라이언트 필터에 의존
        logger.debug(f"[Toss] 체결이력 기간조회 실패, 전체조회로 폴백: {e}")
        try:
            orders = _fetch(use_range=False)
        except toss_api.TossApiError as e2:
            logger.error(f"[Toss] 당일 체결이력 조회 실패: {e2}")
            orders = []

    # 오늘(KST) filledAt(없으면 orderedAt) 기준으로 한 번 더 필터(방어)
    todays = []
    for o in orders:
        ts = str((o.get('execution') or {}).get('filledAt') or o.get('orderedAt') or '')
        if ts[:10] == today:
            todays.append(o)
    _set_micro_cache(cache_key, todays)
    return todays


def _toss_history_item(o, overseas, name_map):
    """토스 CLOSED 주문 1건 → KIS 당일 체결이력 항목."""
    symbol = o.get('symbol', '')
    side = o.get('side')
    sll_buy_cd = '02' if side == 'BUY' else '01'
    sll_buy_name = '매수' if side == 'BUY' else '매도'
    qty = _toss_int(o.get('quantity'))
    ex = o.get('execution') or {}
    filled = _toss_int(ex.get('filledQuantity'))
    status = str(o.get('status', ''))
    is_canceled = bool(o.get('canceledAt')) or status in ('CANCELED', 'EXPIRED', 'REJECTED')
    cncl = max(qty - filled, 0) if is_canceled else 0
    avg = _toss_float(ex.get('averageFilledPrice'))
    ts = str(ex.get('filledAt') or o.get('orderedAt') or '')
    ord_dt = ts[:10].replace('-', '') if len(ts) >= 10 else ''
    ord_tmd = ts[11:19].replace(':', '') if len(ts) >= 19 else ''
    name = name_map.get(symbol) or symbol

    item = {
        'odno': o.get('orderId', ''),
        'pdno': symbol,
        'prdt_name': name,
        'sll_buy_dvsn_cd': sll_buy_cd,
        'sll_buy_dvsn_cd_name': sll_buy_name,
        'ord_dt': ord_dt,
        'ord_tmd': ord_tmd,
        'cncl_cfrm_qty': str(cncl),
    }
    if overseas:
        # 해외는 ft_* 필드 존재로 판별되므로 국내 항목에는 절대 넣지 않는다.
        item.update({
            'ovrs_item_name': name,
            'ft_ord_qty': str(qty),
            'ft_ccld_qty': str(filled),
            'nccs_qty': '0',  # CLOSED 주문은 잔량 0
            'ft_ccld_unpr3': str(avg),
        })
    else:
        item.update({
            'ord_qty': str(qty),
            'tot_ccld_qty': str(filled),
            'rmn_qty': '0',
            'avg_prvs': str(avg),
        })
    return item


def _toss_today_history(overseas):
    """KIS get_today_history / get_overseas_today_history 토스 어댑터."""
    orders = _toss_today_closed_orders()
    want_krw = not overseas
    picked = [o for o in orders if (o.get('currency') == 'KRW') == want_krw]
    name_map = _toss_name_map([o.get('symbol') for o in picked])
    items = [_toss_history_item(o, overseas, name_map) for o in picked]
    logger.info(f"[Toss] 당일 체결이력({'해외' if overseas else '국내'}) 조회 결과: {len(items)}건")
    if overseas:
        return {'rt_cd': '0', 'output': items}
    return {'rt_cd': '0', 'output1': items}


def _toss_place_order(market, action, code, qty, price, ord_dvsn):
    """토스 주문 생성 → KIS place_order 형태({rt_cd,msg1,output:{ODNO}})."""
    side = 'BUY' if action == 'buy' else 'SELL'
    # KIS ord_dvsn: '01'=국내 시장가 → 토스 MARKET, 그 외 지정가
    is_market = (market == 'domestic' and str(ord_dvsn) == '01')
    order_type = 'MARKET' if is_market else 'LIMIT'
    order_price = None if is_market else price
    try:
        r = toss_api.create_order(
            symbol=code, side=side, order_type=order_type,
            quantity=qty, price=order_price,
        )
        odno = (r or {}).get('orderId', '')
        logger.info(f"[Toss] 주문 접수: {side} {code} {qty}주 @{order_price} ({order_type}) → {odno}")
        return {'rt_cd': '0', 'msg_cd': '0000', 'msg1': '주문 접수 완료',
                'output': {'ODNO': odno, 'KRX_FWDG_ORD_ORGNO': '', 'ORD_TMD': ''}}
    except toss_api.TossApiError as e:
        logger.error(f"[Toss] 주문 실패: {e}")
        return {'rt_cd': '1', 'msg_cd': str(e.code or ''), 'msg1': str(e.message or e), 'output': {}}


def _toss_revise_cancel(market, action, org_no, code, qty, price, ord_dvsn):
    """토스 정정/취소 → KIS revise_cancel_order 형태({rt_cd,msg1,output:{ODNO}})."""
    try:
        if action == 'cancel':
            r = toss_api.cancel_order(org_no)
        else:  # modify (정정)
            is_market = (market == 'domestic' and str(ord_dvsn) == '01')
            order_type = 'MARKET' if is_market else 'LIMIT'
            # 토스 정정은 수량이 필수(누락 시 '주문 수량이 유효하지 않습니다' 오류).
            # KIS의 0=전량정정 sentinel이 들어오면 미체결 잔량을 조회해 실제 수량으로 명시한다.
            mod_qty = qty if (qty and int(qty) > 0) else None
            if mod_qty is None:
                try:
                    od = toss_api.get_order(org_no) or {}
                    rem = _toss_int(od.get('quantity')) - _toss_int((od.get('execution') or {}).get('filledQuantity'))
                    if rem > 0:
                        mod_qty = rem
                except toss_api.TossApiError as e:
                    logger.debug(f"[Toss] 정정 잔량 조회 실패({org_no}): {e}")
            r = toss_api.modify_order(
                org_no, order_type=order_type,
                quantity=mod_qty, price=(None if is_market else price),
            )
        odno = (r or {}).get('orderId', '')
        logger.info(f"[Toss] {action} 완료: 원주문={org_no} → 신규={odno}")
        return {'rt_cd': '0', 'msg_cd': '0000', 'msg1': f'{action} 완료',
                'output': {'ODNO': odno, 'KRX_FWDG_ORD_ORGNO': ''}}
    except toss_api.TossApiError as e:
        logger.error(f"[Toss] {action} 실패: {e}")
        return {'rt_cd': '1', 'msg_cd': str(e.code or ''), 'msg1': str(e.message or e), 'output': {}}


def _toss_buyable_qty(code, price, currency="KRW"):
    """토스 매수가능수량 = 매수가능현금 / 단가."""
    try:
        bp = toss_api.get_buying_power(currency)
    except toss_api.TossApiError as e:
        logger.debug(f"[Toss] 매수가능금액 조회 실패({code}): {e}")
        return 0
    cash = _toss_float((bp or {}).get('cashBuyingPower'))
    if price and float(price) > 0:
        return int(cash / float(price))
    return 0


def _toss_sellable_qty(code):
    """토스 매도가능수량."""
    try:
        sq = toss_api.get_sellable_quantity(code)
    except toss_api.TossApiError as e:
        logger.debug(f"[Toss] 매도가능수량 조회 실패({code}): {e}")
        return 0
    return _toss_int((sq or {}).get('sellableQuantity'))


def get_domestic_balance(cano=None, acnt_prdt_cd=None, retries=None):
    """국내 주식 잔고 조회"""
    if config.session.is_toss:
        return _toss_domestic_balance()
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
    
    # [추가] 실패 시 로그 출력 (네트워크 장애와 API 논리 오류를 구분)
    if data.get('msg_cd') == 'NETERR':
        msg = f"잔고 조회 실패(일시적 네트워크/서버 장애): {data.get('msg1')}"
    else:
        msg = f"잔고 조회 실패: {data.get('msg1')} ({data.get('msg_cd')})"
    logger.debug(f"{msg}")
    if hasattr(context, 'SYSTEM_LOGGER') and context.SYSTEM_LOGGER:
        context.SYSTEM_LOGGER(f"[API] {msg}")

    return None, None

def get_overseas_balance(cano=None, acnt_prdt_cd=None, retries=None):
    """해외 주식 잔고 조회"""
    if config.session.is_toss:
        return _toss_overseas_balance()
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
    # [추가] 토스: 금일 손익 요약 미제공 → 빈 값
    if config.session.is_toss:
        return {'rt_cd': '0', 'output2': []}
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
    # [추가] 토스: CLOSED 주문 이력에서 당일 국내 체결을 KIS 형태로 변환
    if config.session.is_toss:
        return _toss_today_history(overseas=False)
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
    # [추가] 토스: CLOSED 주문 이력에서 당일 해외 체결을 KIS 형태로 변환
    if config.session.is_toss:
        return _toss_today_history(overseas=True)
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
    """국내주식 미체결 내역 조회 (모의/실전/토스 분기 처리)"""
    if config.session.is_toss:
        return _toss_open_orders('domestic')
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
    if config.session.is_toss:
        return _toss_open_orders('overseas')
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
    if config.session.is_toss:
        return _toss_place_order(market, action, code, qty, price, ord_dvsn)
    cano, acnt = _prepare_account_params(None, None)
    
    if market == "domestic":
        url_path = constants.API_URLS["DOMESTIC"]["TRADING"][action.upper()]
        category = "trade"
            
        data = {
            "CANO": cano, "ACNT_PRDT_CD": acnt, 
            "PDNO": code, "ORD_DVSN": ord_dvsn, 
            "ORD_QTY": str(qty), "ORD_UNPR": str(price)
        }
        
        # [추가] 모의투자가 아닐 경우 거래소 코드 적용
        # NXT 거래 가능 종목은 SOR(최적주문집행, KRX+NXT 통합 라우팅), 미지원 종목(ETF 등)은
        # KRX로 지정한다. NXT 미지원 종목에 SOR을 쓰면 APBK3026(종목정보 없음) 오류가 발생한다.
        if not config.session.is_simulation:
            data["EXCG_ID_DVSN_CD"] = "SOR" if is_nxt_tradeable(code) else "KRX"
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
    if config.session.is_toss:
        return _toss_revise_cancel(market, action, org_no, code, qty, price, ord_dvsn)
    cano, acnt = _prepare_account_params(None, None)
    
    if market == "domestic":
        url_path = constants.API_URLS["DOMESTIC"]["TRADING"]["REVISE_CANCEL"]
        category = "modify"
            
        qty_all_yn = "Y" if qty == 0 else "N" # 0이면 전량으로 간주 (호출부 로직에 따름)
        data = {"CANO": cano, "ACNT_PRDT_CD": acnt, "KRX_FWDG_ORD_ORGNO": "", "ORGN_ODNO": org_no, "ORD_DVSN": ord_dvsn, "RVSE_CNCL_DVSN_CD": type_cd, "ORD_QTY": str(qty), "ORD_UNPR": str(price), "QTY_ALL_ORD_YN": qty_all_yn}
        
        # [추가] 모의투자가 아닐 경우 거래소 코드 적용 (NXT 미지원 종목은 KRX, place_order와 동일)
        if not config.session.is_simulation:
            data["EXCG_ID_DVSN_CD"] = "SOR" if is_nxt_tradeable(code) else "KRX"
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
    """예수금 및 자산 현황 조회 (모의/실전/토스 자동 분기)"""
    # [추가] 토스: 매수가능금액(현금)을 예수금으로 사용. D+1/D+2 구분은 제공되지 않음.
    if config.session.is_toss:
        dep = _toss_krw_deposit()
        return {"deposit": dep, "foreign_deposit": 0, "withdraw": dep,
                "d2_deposit": dep, "order_possible": dep, "d2_real": dep}

    cano, acnt_prdt_cd = _prepare_account_params(cano, acnt_prdt_cd)
    res = {"deposit": 0, "foreign_deposit": 0, "withdraw": 0, "d2_deposit": 0, "order_possible": 0, "d2_real": 0}
    success = False # [추가] 조회 성공 여부 플래그

    if config.session.is_simulation:
        # [수정] 모의투자: 주식잔고조회(VTTC8434R)를 우선 사용하여 예수금 확인 (더 안정적)
        # skip_balance_check가 True이면(이미 외부에서 조회했다면) 건너뜀
        holdings, summary_list = ([], []) if skip_balance_check else get_domestic_balance(cano, acnt_prdt_cd, retries=retries)

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
        data_order = get_deposit(cano, acnt_prdt_cd, retries=retries)
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
    if config.session.is_toss:
        try:
            import toss_api
            res = toss_api.get_price("005930")
            if res is not None:
                return True
        except Exception as e:
            logger.debug(f"check_server_health (toss) error: {e}")
        return False
        
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


# [리팩토링] OpenDART 연동 계층은 modules/dart_api.py 로 분리되었다. (재수출)
from modules.dart_api import (DART_BASE_URL, call_dart, get_dart_corp_map,
                              get_dart_dividend, get_dart_acc_month, get_dart_disclosures,
                              get_dart_insider_trades, get_dart_major_holdings,
                              get_dart_financials, get_dart_paid_increase_detail,
                              get_dart_bond_issue_detail, get_dart_document_text,
                              get_dart_earnings_brief,
                              get_dart_treasury_decisions, get_dart_free_increase_detail,
                              get_dart_capital_reduction_detail, get_dart_financial_index,
                              get_dart_dividend_decision, get_dart_shares_outstanding,
                              DART_INDEX_CLASSES)
