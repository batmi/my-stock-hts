# api.py
import requests
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
from collections import deque
import config
import constants

ssl._create_default_https_context = ssl._create_unverified_context
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

class TLSAdapter(HTTPAdapter):
    def init_poolmanager(self, connections, maxsize, block=False):
        self.poolmanager = PoolManager(num_pools=connections, maxsize=maxsize, block=block, ssl_version=ssl.PROTOCOL_TLSv1_2)

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
                min_interval = (1.0 / target_limit) * 1.01
                elapsed = time.time() - last_time
                if elapsed < min_interval:
                    wait_time = min_interval - elapsed
                    time.sleep(wait_time)

            self.request_history.append(time.time())
            
        current_tps = self._get_current_tps()

        if config.DEBUG_LEVEL in ["TRACE", "DEBUG"] and (is_sim_server or is_real_server):
            config.console.print(f"[dim cyan][TRACE] REQ ({server_type}) TPS:{current_tps:.1f} | {method} {url}[/dim cyan]")
            if config.DEBUG_LEVEL == "DEBUG":
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

                if config.DEBUG_LEVEL in ["TRACE", "DEBUG"] and (is_sim_server or is_real_server):
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
                    
                    if config.DEBUG_LEVEL == "DEBUG" and res_data:
                        config.console.print(f"[dim magenta]  > Response Data: {json.dumps(res_data, ensure_ascii=False, indent=2)}[/dim magenta]")

                try:
                    if response.text and response.text.startswith('{'):
                        res_json = response.json()
                        msg_cd = res_json.get('msg_cd')
                        
                        if msg_cd == 'OPSQ2000':
                            msg = f"서버 동기화 지연 감지(OPSQ2000). {config.RETRY_DELAY_SERVER}초 대기 후 재시도합니다..."
                            if config.DEBUG_LEVEL != "OFF":
                                config.console.print(f"[bold yellow]{msg}[/bold yellow]")
                            # [추가] 시스템 트레이딩 로그 기록
                            if config.SYSTEM_LOGGER: config.SYSTEM_LOGGER(f"[API] {msg}")
                            
                            time.sleep(config.RETRY_DELAY_SERVER)
                            response = super().request(method, url, *args, **kwargs)

                        elif msg_cd in ['EGW00123', 'EGW00121']:
                            if config.DEBUG_LEVEL != "OFF":
                                config.console.print(f"[bold yellow]토큰 만료 감지(Code: {msg_cd}). 토큰을 갱신합니다...[/bold yellow]")
                            
                            new_token = None
                            if is_sim_server:
                                new_token = get_access_token(force_refresh=True)
                            elif is_real_server:
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
                                if config.DEBUG_LEVEL != "OFF":
                                    config.console.print(f"[bold yellow]{msg}[/bold yellow]")
                                # [추가] 시스템 트레이딩 로그 기록
                                if config.SYSTEM_LOGGER: config.SYSTEM_LOGGER(f"[API] {msg}")
                                
                                time.sleep(wait_time)
                                continue
                except Exception: pass
                
                # 성공 시 시간 기록 후 반환
                # [수정] 락 안에서 시간 업데이트
                with self.lock:
                    now_final = time.time()
                    if is_real_server: self.last_request_time_real = now_final
                    elif is_sim_server: self.last_request_time_sim = now_final
                
                return response

            except Exception as e:
                # 연결 끊김 에러 체크
                err_str = str(e)
                is_disconnect = "Connection aborted" in err_str or "RemoteDisconnected" in err_str or "timed out" in err_str
                
                # 재시도 가능한 에러이고 횟수가 남았으면 대기 후 재시도
                if is_disconnect and attempt < max_retries:
                    wait_time = 0.5
                    msg = f"서버 연결 끊김 감지. {wait_time}초 후 재시도합니다 ({attempt+1}/{max_retries})..."
                    if config.DEBUG_LEVEL != "OFF":
                        config.console.print(f"[dim][yellow][TRACE] {msg}[/yellow][dim]")
                    # [추가] 시스템 트레이딩 로그 기록
                    if config.SYSTEM_LOGGER: config.SYSTEM_LOGGER(f"[API] {msg}")
                    
                    time.sleep(wait_time)
                    continue
                
                # 재시도 불가능하거나 횟수 초과 시 에러 발생
                if config.DEBUG_LEVEL != "OFF":
                    config.console.print(f"[bold red][DEBUG] Request Failed: {str(e)}[/bold red]")
                raise e

session = ThrottledSession()
session.mount('https://', TLSAdapter())

# 전역 토큰 변수
SIM_ACCESS_TOKEN = ""
REAL_ACCESS_TOKEN = "" 
AUTO_ACCESS_TOKEN = "" # [추가] 시스템 트레이딩 전용 토큰

def load_token_cache():
    try:
        if not os.path.exists(config.TOKEN_CACHE_FILE): return {}
        with open(config.TOKEN_CACHE_FILE, 'r', encoding='utf-8') as f:
            return json.load(f)
    except Exception: return {}

def save_token_cache(cache_data):
    try:
        with open(config.TOKEN_CACHE_FILE, 'w', encoding='utf-8') as f:
            json.dump(cache_data, f, ensure_ascii=False, indent=2)
    except Exception: pass

def check_token_validity(token_info):
    if not token_info: return False
    expired_str = token_info.get('token_expired')
    access_token = token_info.get('access_token')
    if not expired_str or not access_token: return False
    
    try:
        expired_dt = datetime.strptime(expired_str, "%Y-%m-%d %H:%M:%S")
        if datetime.now() < (expired_dt - timedelta(minutes=1)):
            return True
    except: return False
    return False

def get_current_token():
    # [추가] 시스템 트레이딩 컨텍스트 확인
    if getattr(config.trade_context, 'use_auto_account', False) and not config.IS_SIMULATION:
        return get_auto_access_token()
        
    if config.IS_SIMULATION:
        return get_access_token()
    else:
        return get_real_access_token()

def get_access_token(force_refresh=False):
    global SIM_ACCESS_TOKEN
    
    if not force_refresh and SIM_ACCESS_TOKEN:
        return SIM_ACCESS_TOKEN

    cache = load_token_cache()
    sim_token_info = cache.get("SIMULATION")
    
    if not force_refresh and check_token_validity(sim_token_info):
        SIM_ACCESS_TOKEN = sim_token_info['access_token']
        if config.DEBUG_LEVEL != "OFF":
            config.console.print(f"[dim][TRACE] 모의 캐시 토큰 사용 ({sim_token_info.get('token_expired')})[/dim]")
        return SIM_ACCESS_TOKEN

    headers = {"content-type": "application/json"}
    body = {"grant_type": "client_credentials", "appkey": config.APP_KEY, "appsecret": config.APP_SECRET}
    url = f"{config.URL_BASE}/oauth2/tokenP"
    
    try:
        config.console.print("[dim]모의투자 토큰 신규 발급 요청...[/dim]")
        res = requests.post(url, headers=headers, data=json.dumps(body), verify=False)
        res_json = res.json()
        
        if 'access_token' in res_json:
            SIM_ACCESS_TOKEN = res_json['access_token']
            expired = res_json.get('access_token_token_expired')
            
            cache = load_token_cache()
            cache["SIMULATION"] = {
                "access_token": SIM_ACCESS_TOKEN,
                "token_expired": expired,
                "issued_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            }
            save_token_cache(cache)
            config.console.print("[green]모의투자 토큰 발급 완료[/green]")
            return SIM_ACCESS_TOKEN
        
        elif res_json.get('error_code') == 'EGW00133':
            if config.DEBUG_LEVEL != "OFF": config.console.print("[yellow]빈도 제한. 캐시 재확인.[/yellow]")
            cache = load_token_cache()
            sim_token_info = cache.get("SIMULATION")
            if check_token_validity(sim_token_info):
                SIM_ACCESS_TOKEN = sim_token_info['access_token']
                config.console.print("[green]캐시된 유효 토큰을 복구했습니다.[/green]")
                return SIM_ACCESS_TOKEN
            return None
        else:
            config.console.print(f"[bold red]토큰 발급 실패: {res.text}[/bold red]")
            return None
            
    except Exception as e:
        config.console.print(f"[bold red]접속 오류: {str(e)}[/bold red]")
        return None

def get_real_access_token(force_refresh=False):
    global REAL_ACCESS_TOKEN
    
    if not config.REAL_APP_KEY: return None

    if not force_refresh and REAL_ACCESS_TOKEN: 
        return REAL_ACCESS_TOKEN

    cache = load_token_cache()
    real_token_info = cache.get("REAL")

    if not force_refresh and check_token_validity(real_token_info):
        REAL_ACCESS_TOKEN = real_token_info['access_token']
        if config.DEBUG_LEVEL != "OFF":
            config.console.print(f"[dim][TRACE] 실전 캐시 토큰 사용 ({real_token_info.get('token_expired')})[/dim]")
        return REAL_ACCESS_TOKEN

    headers = {"content-type": "application/json"}
    body = {"grant_type": "client_credentials", "appkey": config.REAL_APP_KEY, "appsecret": config.REAL_APP_SECRET}
    url = f"{config.REAL_URL}/oauth2/tokenP"
    
    try:
        config.console.print("[dim]실전투자 토큰 신규 발급 요청...[/dim]")
        res = requests.post(url, headers=headers, data=json.dumps(body), verify=False)
        
        if res.status_code == 200:
            res_json = res.json()
            REAL_ACCESS_TOKEN = res_json['access_token']
            expired = res_json.get('access_token_token_expired')
            
            cache = load_token_cache()
            cache["REAL"] = {
                "access_token": REAL_ACCESS_TOKEN,
                "token_expired": expired,
                "issued_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            }
            save_token_cache(cache)
            config.console.print("[green]실전투자 토큰 발급 완료[/green]")
            return REAL_ACCESS_TOKEN
        
        else:
            try:
                err_json = res.json()
                if err_json.get('error_code') == 'EGW00133':
                    if config.DEBUG_LEVEL != "OFF": config.console.print("[yellow]실전 토큰 발급 빈도 제한(EGW00133). 캐시를 재확인합니다.[/yellow]")
                    cache = load_token_cache()
                    real_token_info = cache.get("REAL")
                    if check_token_validity(real_token_info):
                        REAL_ACCESS_TOKEN = real_token_info['access_token']
                        config.console.print("[green]캐시된 유효 토큰을 복구했습니다.[/green]")
                        return REAL_ACCESS_TOKEN
                    return None
            except: pass
            
            config.console.print(f"[red]실전 토큰 발급 실패: {res.text}[/red]")
            
    except Exception as e:
        config.console.print(f"[red]실전 토큰 발급 중 오류: {e}[/red]")
        pass
        
    return None

def get_auto_access_token(force_refresh=False):
    """시스템 트레이딩 전용 계좌 토큰 발급"""
    global AUTO_ACCESS_TOKEN
    
    if not config.AUTO_APP_KEY: return None

    if not force_refresh and AUTO_ACCESS_TOKEN: 
        return AUTO_ACCESS_TOKEN

    cache = load_token_cache()
    auto_token_info = cache.get("AUTO")

    if not force_refresh and check_token_validity(auto_token_info):
        AUTO_ACCESS_TOKEN = auto_token_info['access_token']
        if config.DEBUG_LEVEL != "OFF":
            config.console.print(f"[dim][TRACE] 자동매매 캐시 토큰 사용 ({auto_token_info.get('token_expired')})[/dim]")
        return AUTO_ACCESS_TOKEN

    headers = {"content-type": "application/json"}
    body = {"grant_type": "client_credentials", "appkey": config.AUTO_APP_KEY, "appsecret": config.AUTO_APP_SECRET}
    url = f"{config.REAL_URL}/oauth2/tokenP"
    
    try:
        config.console.print("[dim]자동매매용 토큰 신규 발급 요청...[/dim]")
        res = requests.post(url, headers=headers, data=json.dumps(body), verify=False)
        
        if res.status_code == 200:
            res_json = res.json()
            AUTO_ACCESS_TOKEN = res_json['access_token']
            expired = res_json.get('access_token_token_expired')
            
            cache = load_token_cache()
            cache["AUTO"] = {
                "access_token": AUTO_ACCESS_TOKEN,
                "token_expired": expired,
                "issued_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            }
            save_token_cache(cache)
            config.console.print("[green]자동매매용 토큰 발급 완료[/green]")
            return AUTO_ACCESS_TOKEN
    except Exception as e:
        config.console.print(f"[red]자동매매 토큰 발급 중 오류: {e}[/red]")
        
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
    config.SYSTEM_TRADING_LOCK.acquire()

    try:
        if timeout is None: timeout = config.DEFAULT_TIMEOUT
        if retries is None: retries = config.MAX_RETRIES
        
        token_to_use = get_current_token()
        base_url = config.URL_BASE if config.IS_SIMULATION else config.REAL_URL
        
        # [수정] 컨텍스트에 따라 키 선택
        use_auto = getattr(config.trade_context, 'use_auto_account', False)
        if use_auto and not config.IS_SIMULATION:
            key = config.AUTO_APP_KEY
            secret = config.AUTO_APP_SECRET
            if not key: # Fallback
                key = config.REAL_APP_KEY; secret = config.REAL_APP_SECRET
        else:
            key = config.APP_KEY if config.IS_SIMULATION else config.REAL_APP_KEY
            secret = config.APP_SECRET if config.IS_SIMULATION else config.REAL_APP_SECRET

        env_key = "sim" if config.IS_SIMULATION else "real"
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
                return res.json()
            except Exception as e:
                last_error = e
                if attempt < retries:
                    time.sleep(0.5)
                    continue
        
        return {'rt_cd': '9999', 'msg1': str(last_error)}
    finally:
        config.SYSTEM_TRADING_LOCK.release()

def get_stock_name_by_code(code, is_overseas):
    final_name = None
    if not is_overseas:
        url = f"https://finance.naver.com/item/main.naver?code={code}"
        with config.console.status(f"[bold green]네이버에서 종목명({code}) 조회 중...[/]"):
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
        with config.console.status(f"[bold green]yfinance에서 종목명({code}) 조회 중...[/]"):
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
            df = yf.download(code, period="2y", progress=False)
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
        df = df[['stck_bsop_date', 'stck_clpr', 'stck_hgpr', 'stck_lwpr', 'acml_vol']].copy()
        df.columns = ['date', 'close', 'high', 'low', 'volume']
        df = df.astype({'close': float, 'high': float, 'low': float, 'volume': float})
        return df.sort_values('date', ascending=True).reset_index(drop=True).tail(250)
    
    else:
        cached_ex = config.EXCHANGE_CACHE.get(code)
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
                                if cached_ex != excd: config.update_cache_and_save(code, excd)
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
                df.rename(columns={'xymd': 'date', 'clos': 'close', 'high': 'high', 'low': 'low'}, inplace=True)
                if 'tvol' in df.columns: df['volume'] = df['tvol']
                elif 'tovol' in df.columns: df['volume'] = df['tovol']
                elif 'vol' in df.columns: df['volume'] = df['vol']
                else: df['volume'] = 0
                df = df[df['date'] >= start_date_origin]
                numeric_cols = ['close', 'high', 'low', 'volume']
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
        cached_ex = config.EXCHANGE_CACHE.get(code)
        exchanges = []
        if cached_ex: exchanges.append(cached_ex)
        for e in ["NAS", "NYS", "AMS", "NASD", "NYSE", "AMEX"]:
            if e not in exchanges: exchanges.append(e)
        
        for excd in exchanges:
            params = {"AUTH": "", "EXCD": excd, "SYMB": code}
            data = call_api("uapi/overseas-price/v1/quotations/price", "overseas", "quotations", "price", params=params, timeout=3)
            if data.get('rt_cd') == '0':
                if float(data.get('output', {}).get('last', 0) or 0) > 0:
                    if cached_ex != excd: config.update_cache_and_save(code, excd)
                    return data
        return {'rt_cd': '9999'}
    return {'rt_cd': '9999'}

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
                if target_excd != excd: config.update_cache_and_save(code, target_excd)
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
                if target_excd != excd: config.update_cache_and_save(code, target_excd)
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
    cano = config.CANO
    acnt_prdt_cd = config.ACNT_PRDT_CD
    if not config.IS_SIMULATION and getattr(config.trade_context, 'use_auto_account', False) and config.AUTO_CANO:
        cano = config.AUTO_CANO
        acnt_prdt_cd = config.AUTO_ACNT_PRDT_CD

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
    cano = config.CANO
    acnt_prdt_cd = config.ACNT_PRDT_CD
    if not config.IS_SIMULATION and getattr(config.trade_context, 'use_auto_account', False) and config.AUTO_CANO:
        cano = config.AUTO_CANO
        acnt_prdt_cd = config.AUTO_ACNT_PRDT_CD

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
    cano = config.CANO
    acnt_prdt_cd = config.ACNT_PRDT_CD
    if not config.IS_SIMULATION and getattr(config.trade_context, 'use_auto_account', False) and config.AUTO_CANO:
        cano = config.AUTO_CANO
        acnt_prdt_cd = config.AUTO_ACNT_PRDT_CD
        
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
    if config.IS_SIMULATION:
        trade_excds.append(primary_excd)
        for e in ["NASD", "NYSE", "AMEX"]:
            if e != primary_excd: trade_excds.append(e)
    else: trade_excds = ["NASD"]
    
    # [수정] 컨텍스트에 따른 계좌번호 선택
    cano = config.CANO
    acnt_prdt_cd = config.ACNT_PRDT_CD
    if not config.IS_SIMULATION and getattr(config.trade_context, 'use_auto_account', False) and config.AUTO_CANO:
        cano = config.AUTO_CANO
        acnt_prdt_cd = config.AUTO_ACNT_PRDT_CD

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
    cached = config.EXCHANGE_CACHE.get(stock_code)
    if cached: return cached

    for excd in ["NAS", "NYS", "AMS"]:
        params = {"AUTH": "", "EXCD": excd, "SYMB": stock_code}
        data = call_api("uapi/overseas-price/v1/quotations/price", "overseas", "quotations", "price", params=params)
        if data.get('rt_cd') == '0' and float(str(data.get('output', {}).get('last', '0')).strip() or 0) > 0:
            config.update_cache_and_save(stock_code, excd)
            return excd
    return None

def get_domestic_balance():
    """국내 주식 잔고 조회 (시스템 트레이딩용, account 모듈 대체)"""
    cano = config.CANO
    acnt_prdt_cd = config.ACNT_PRDT_CD
    if not config.IS_SIMULATION and getattr(config.trade_context, 'use_auto_account', False) and config.AUTO_CANO:
        cano = config.AUTO_CANO
        acnt_prdt_cd = config.AUTO_ACNT_PRDT_CD

    params = {"CANO": cano, "ACNT_PRDT_CD": acnt_prdt_cd, "AFHR_FLPR_YN": "N", "OFL_YN": "N", "INQR_DVSN": "02", "UNPR_DVSN": "01", "FUND_STTL_ICLD_YN": "N", "FNCG_AMT_AUTO_RDPT_YN": "N", "PRCS_DVSN": "00", "CTX_AREA_FK100": "", "CTX_AREA_NK100": ""}
    data = call_api("uapi/domestic-stock/v1/trading/inquire-balance", "domestic", "inquiry", "balance", params=params)
    
    # [추가] OPSQ2001 에러(INQR_DVSN 관련) 발생 시 '01'(대출일별)로 재시도
    if data.get('msg_cd') == 'OPSQ2001':
        if config.DEBUG_LEVEL != "OFF":
            config.console.print("[dim yellow][API] 잔고 조회 '02' 실패(OPSQ2001). '01'로 재시도합니다.[/dim yellow]")
        params["INQR_DVSN"] = "01"
        data = call_api("uapi/domestic-stock/v1/trading/inquire-balance", "domestic", "inquiry", "balance", params=params)

    if data.get('rt_cd') == '0':
        return data.get('output1', []), data.get('output2', [])
    
    # [추가] 실패 시 로그 출력
    msg = f"잔고 조회 실패: {data.get('msg1')} ({data.get('msg_cd')})"
    if config.DEBUG_LEVEL != "OFF":
        config.console.print(f"[dim red][DEBUG] {msg}[/dim red]")
    if config.SYSTEM_LOGGER:
        config.SYSTEM_LOGGER(f"[API] {msg}")
        
    return [], []

def get_unfilled_orders(cano=None, acnt_prdt_cd=None):
    """미체결 내역 조회 (국내주식)"""
    if not cano: cano = config.CANO
    if not acnt_prdt_cd: acnt_prdt_cd = config.ACNT_PRDT_CD
    
    # 시스템 트레이딩 컨텍스트 확인
    if not config.IS_SIMULATION and getattr(config.trade_context, 'use_auto_account', False) and config.AUTO_CANO:
        cano = config.AUTO_CANO
        acnt_prdt_cd = config.AUTO_ACNT_PRDT_CD

    # 미체결 조회 TR (주식정정취소가능주문조회)
    url = f"{config.URL_BASE}/uapi/domestic-stock/v1/trading/inquire-psbl-rvsecncl"
    tr_id = utils.get_tr_id("domestic", "inquiry", "unfilled") # constants에 매핑 필요, 없으면 아래 로직으로 대체
    
    # TR ID 하드코딩 (안전장치)
    if not tr_id:
        tr_id = "VTTC8036R" if config.IS_SIMULATION else "TT800103R" # 주식정정취소가능주문조회

    headers = _get_headers(tr_id, cano)
    
    params = {
        "CANO": cano, "ACNT_PRDT_CD": acnt_prdt_cd,
        "CTX_AREA_FK100": "", "CTX_AREA_NK100": "",
        "INQR_DVSN_1": "0", # 0:전체, 1:현금, 2:융자
        "INQR_DVSN_2": "0"  # 0:전체, 1:매도, 2:매수
    }
    
    try:
        res = session.get(url, headers=headers, params=params, verify=False, timeout=5)
        data = res.json()
        if data['rt_cd'] == '0':
            return data.get('output', [])
    except Exception: pass
    return []

def cancel_order(odno, code, qty, is_buy):
    """주문 취소 실행"""
    # 컨텍스트에 따른 계좌 선택
    cano = config.CANO; acnt = config.ACNT_PRDT_CD
    if not config.IS_SIMULATION and getattr(config.trade_context, 'use_auto_account', False) and config.AUTO_CANO:
        cano = config.AUTO_CANO; acnt = config.AUTO_ACNT_PRDT_CD

    tr_id = utils.get_tr_id("domestic", "trade", "cancel")
    url = f"{config.URL_BASE}/uapi/domestic-stock/v1/trading/order-rvsecncl"
    headers = _get_headers(tr_id, cano)
    
    data = {"CANO": cano, "ACNT_PRDT_CD": acnt, "KRX_FWDG_ORD_ORGNO": "", "ORGN_ODNO": odno, "ORD_DVSN": "00", "RVSE_CNCL_DVSN_CD": "02", "ORD_QTY": str(qty), "ORD_UNPR": "0", "QTY_ALL_ORD_YN": "Y"}
    
    res = session.post(url, headers=headers, data=json.dumps(data), verify=False)
    return res.json()

def send_telegram_message(message):
    """텔레그램 메시지 전송 (시스템 트레이딩 알림용)"""
    if not config.TELEGRAM_BOT_TOKEN or not config.TELEGRAM_CHAT_ID:
        return

    # [추가] 계좌 정보 식별 및 메시지 헤더 추가
    cano = config.CANO
    acc_label = "모의" if config.IS_SIMULATION else "실전"

    # 시스템 트레이딩 컨텍스트(AUTO 계좌) 확인
    if not config.IS_SIMULATION and getattr(config.trade_context, 'use_auto_account', False) and config.AUTO_CANO:
        cano = config.AUTO_CANO
        acc_label = "자동"

    # [수정] 인스턴스 이름 및 계좌번호 표시
    instance_name = getattr(config, 'TELEGRAM_INSTANCE_NAME', 'HTS')
    account_info = f"[{instance_name} | {acc_label} {cano}]"

    # [추가] rich 라이브러리 색상 태그 제거 (텔레그램 전송용)
    # 예: [red]텍스트[/] -> 텍스트. 소문자로 시작하는 태그만 제거하여 [시스템] 등은 유지
    clean_message = re.sub(r'\[/?[a-z]+(?:[\s=][^\]]*)?\]', '', message)

    # [수정] 계좌 정보를 메시지 가장 마지막에 추가
    final_msg = f"{clean_message}\n{account_info}"

    # [추가] 전송 메시지 로그 기록
    if config.SYSTEM_LOGGER:
        # 로그 파일 가독성을 위해 줄바꿈을 구분자로 치환하여 기록
        log_content = final_msg.replace('\n', ' | ')
        config.SYSTEM_LOGGER(f"[Telegram] 메시지 발송: {log_content}")

    url = f"https://api.telegram.org/bot{config.TELEGRAM_BOT_TOKEN}/sendMessage"
    data = {"chat_id": config.TELEGRAM_CHAT_ID, "text": final_msg}
    
    # [추가] 화면 디버그 로그 (요청)
    if config.DEBUG_LEVEL in ["TRACE", "DEBUG"]:
        config.console.print(f"[dim cyan][TRACE] REQ (TELEGRAM) | POST {url}[/dim cyan]")
        if config.DEBUG_LEVEL == "DEBUG":
            config.console.print(f"[dim cyan]  > Message: {message.replace(chr(10), ' ')}[/dim cyan]")

    # [수정] 재시도 로직 추가 (최대 3회)
    max_retries = 3
    for attempt in range(max_retries):
        try:
            # 매매 로직 지연 방지를 위해 짧은 타임아웃 설정 (재시도 시 조금씩 증가)
            current_timeout = 1 + (attempt * 0.5)
            res = requests.post(url, data=data, timeout=current_timeout)
            
            # [추가] 화면 디버그 로그 (응답)
            if config.DEBUG_LEVEL in ["TRACE", "DEBUG"]:
                status_color = "magenta" if res.status_code == 200 else "red"
                config.console.print(f"[dim {status_color}][TRACE] RES (TELEGRAM) Status:{res.status_code} ({attempt+1}/{max_retries})[/dim {status_color}]")
                if config.DEBUG_LEVEL == "DEBUG" and res.status_code != 200:
                     config.console.print(f"[dim red]  > Error: {res.text}[/dim red]")
            
            if res.status_code == 200:
                if config.SYSTEM_LOGGER:
                    config.SYSTEM_LOGGER("[Telegram] 전송 성공")
                return # 성공 시 함수 종료
            else:
                if config.SYSTEM_LOGGER:
                    config.SYSTEM_LOGGER(f"[Telegram] 전송 실패({attempt+1}/{max_retries}) Status: {res.status_code}, Msg: {res.text}")
        except Exception as e:
            # [추가] 화면 디버그 로그 (예외)
            if config.DEBUG_LEVEL in ["TRACE", "DEBUG"]:
                config.console.print(f"[dim red][TRACE] ERR (TELEGRAM) {str(e)} ({attempt+1}/{max_retries})[/dim red]")

            if config.SYSTEM_LOGGER:
                config.SYSTEM_LOGGER(f"[Telegram] 전송 중 오류 발생({attempt+1}/{max_retries}): {str(e)}")
        
        # 마지막 시도가 아니면 대기
        if attempt < max_retries - 1:
            time.sleep(1)
            
    if config.SYSTEM_LOGGER:
        config.SYSTEM_LOGGER("[Telegram] 최종 전송 실패")
