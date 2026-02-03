# config.py
import os
import sys
from rich.console import Console
from rich.prompt import Prompt
import json

console = Console()

# ==========================================================
# [설정] 디버그 로그 레벨 설정 (OFF / DEBUG / TRACE)
# ==========================================================
# OFF   : 로그 출력 없음
# TRACE : 한 줄 요약 로그 (TPS, URL, 상태코드)
# DEBUG : 상세 로그 (모든 요청/응답 파라미터 및 데이터 포함)
DEBUG_LEVEL = "OFF"

# ==========================================================
# [설정] 트랜잭션 속도 제한 (Rate Limiting)
# ==========================================================
SIM_TX_PER_SECOND = 2     # 모의투자 서버 최대 TPS: 2
REAL_TX_PER_SECOND = 20   # 실전투자 서버 최대 TPS: 20

# ==========================================================
# [설정] 파일 경로 관리
# ==========================================================
# 종목 분석 대상 목록을 저장하는 JSON 파일의 경로입니다.
# (기본값: stock.json)
STOCK_DATA_FILE = "stock.json"

# API 접속 토큰을 캐싱하여 재사용하기 위한 파일 경로입니다.
TOKEN_CACHE_FILE = "token_cache.json"

# ==========================================================
# [설정] 시스템 및 네트워크 정책
# ==========================================================
DEFAULT_TIMEOUT = 10      # API 요청 타임아웃 (초)
RETRY_DELAY_SERVER = 5.0  # 서버 지연(OPSQ2000) 발생 시 재시도 대기 시간 (초)

# ==========================================================
# [추가] API 요청 중 연결 끊김(RemoteDisconnected 등) 발생 시 재시도 횟수
# 0으로 설정하면 재시도하지 않으며, 1로 설정하면 실패 시 1회 재시도합니다.
# ==========================================================
MAX_RETRIES = 1

# ==========================================================
# [설정] 기술적 분석 지표 파라미터
# ==========================================================
INDICATOR_PARAMS = {
    "RSI_PERIOD": 14,             # RSI 계산 기간
    "ADX_PERIOD": 14,             # ADX 계산 기간
    "CCI_WINDOW": 20,             # CCI 계산 기간
    "CHART_LOOKBACK_DAYS": 730    # 차트 데이터 조회 기간 (2년)
}

# ----------------------------------------------------------
# 전역 변수 초기화
# ----------------------------------------------------------
APP_KEY = ""
APP_SECRET = ""
CANO = ""
ACNT_PRDT_CD = ""
URL_BASE = ""
IS_SIMULATION = False

# [데이터 조회 가속용] 실전투자 변수
REAL_APP_KEY = ""
REAL_APP_SECRET = ""

# 서버 URL 상수 정의
SIM_URL = "https://openapivts.koreainvestment.com:29443"
REAL_URL = "https://openapi.koreainvestment.com:9443"

# 거래소 정보 캐싱
EXCHANGE_CACHE = {}

# 종목 리스트 데이터
STOCK_CONFIG_DATA = {}

def initialize_environment():
    global APP_KEY, APP_SECRET, CANO, ACNT_PRDT_CD, URL_BASE, IS_SIMULATION
    global REAL_APP_KEY, REAL_APP_SECRET
    
    console.print("\n[bold]접속할 서버를 선택하세요:[/bold]")
    console.print("[1] 모의투자 (Simulation)")
    console.print("[2] 실전투자 (Real Trading)")
    
    try:
        console.print()
        choice = Prompt.ask("선택 [dim](종료: q)[/dim]", choices=["1", "2", "q", "Q"], default="1")
    except KeyboardInterrupt:
        console.print("\n[yellow]프로그램을 종료합니다.[/yellow]\n")
        sys.exit()
    
    if choice.lower() == 'q':
        console.print("\n[yellow]프로그램을 종료합니다.[/yellow]\n")
        sys.exit()
    
    acc_num_input = ""
    
    if choice == "1":
        APP_KEY = os.environ.get("SIM_APP_KEY")
        APP_SECRET = os.environ.get("SIM_APP_SECRET")
        acc_num_input = os.environ.get("SIM_ACC_NUM")
        URL_BASE = SIM_URL
        IS_SIMULATION = True
        console.print()
        console.print("[green]모의투자 서버 환경을 로드했습니다.[/green]")
        
    else:
        REAL_APP_KEY = os.environ.get("REAL_APP_KEY")
        REAL_APP_SECRET = os.environ.get("REAL_APP_SECRET")
        APP_KEY = REAL_APP_KEY
        APP_SECRET = REAL_APP_SECRET
        acc_num_input = os.environ.get("REAL_ACC_NUM")
        URL_BASE = REAL_URL
        IS_SIMULATION = False
        console.print()
        console.print("[bold red]실전투자 서버 환경을 로드했습니다. (실제 자산 거래 주의)[/bold red]")

    if not all([APP_KEY, APP_SECRET, acc_num_input]):
        console.print("[bold red]오류: 해당 환경의 필수 환경변수(KEY, SECRET, ACC_NUM)가 설정되지 않았습니다.[/bold red]")
        sys.exit()

    clean_acc = acc_num_input.strip().replace('-', '')
    if len(clean_acc) == 8:
        CANO = clean_acc
        ACNT_PRDT_CD = "01" 
    elif len(clean_acc) == 10:
        CANO = clean_acc[:8]
        ACNT_PRDT_CD = clean_acc[8:]
    else:
        console.print(f"[bold red]오류: 계좌번호 형식이 올바르지 않습니다. ({acc_num_input})[/bold red]")
        sys.exit()

def load_stock_config(filename=None):
    global STOCK_CONFIG_DATA
    if filename is None:
        filename = STOCK_DATA_FILE

    default_config = {
        "stocks_kr": [{"name": "삼성전자", "code": "005930"}],
        "etfs_kr": [{"name": "ACE KRX금현물", "code": "411060"}],
        "stocks_us": [{"name": "Apple Inc.", "code": "AAPL"}],
        "etfs_us": [{"name": "Invesco QQQ Trust", "code": "QQQ"}]   
    }

    try:
        with open(filename, 'r', encoding='utf-8') as f:
            config_data = json.load(f)
            if 'etfs' in config_data and 'etfs_kr' not in config_data: config_data['etfs_kr'] = config_data.pop('etfs')
            
            for group in ['stocks_us', 'etfs_us']:
                for item in config_data.get(group, []):
                    if 'exchange' in item:
                        EXCHANGE_CACHE[item['code']] = item['exchange']
            
            STOCK_CONFIG_DATA = config_data
            return
            
    except FileNotFoundError:
        console.print(f"[yellow]'{filename}' 파일이 없습니다. 기본 설정을 사용합니다.[/yellow]")
        STOCK_CONFIG_DATA = default_config
    except json.JSONDecodeError:
        console.print(f"[bold red]'{filename}' 파일 형식이 잘못되었습니다. 기본 설정을 사용합니다.[/bold red]")
        STOCK_CONFIG_DATA = default_config

def save_stock_config(config_data, filename=None):
    if filename is None:
        filename = STOCK_DATA_FILE
    try:
        with open(filename, 'w', encoding='utf-8') as f:
            json.dump(config_data, f, ensure_ascii=False, indent=2)
    except Exception as e:
        console.print(f"[bold red]저장 실패: {str(e)}[/bold red]")

def update_cache_and_save(code, excd):
    if EXCHANGE_CACHE.get(code) != excd:
        EXCHANGE_CACHE[code] = excd
    
    updated = False
    target_groups = ['stocks_us', 'etfs_us']
    
    for group in target_groups:
        if group in STOCK_CONFIG_DATA:
            for item in STOCK_CONFIG_DATA[group]:
                if item['code'] == code:
                    if item.get('exchange') != excd:
                        item['exchange'] = excd
                        updated = True
    
    if updated:
        save_stock_config(STOCK_CONFIG_DATA)

