# config.py
import os
import sys
import threading
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

# [추가] 거래 내역 및 스냅샷을 저장할 SQLite DB 파일 경로
DB_FILE_PATH = "trade_history.db"

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
# [설정] 기본 환율 (Fallback)
# ==========================================================
# 실시간 환율 조회(yfinance) 실패 시 / 백테스팅 시 사용할 기본 환율입니다.
DEFAULT_EXCHANGE_RATE = 1450.00

# ==========================================================
# [설정] 기술적 분석 지표 파라미터
# ==========================================================
INDICATOR_PARAMS = {
    # [데이터 조회 기간]
    # 120일 이동평균선이나 EMA(지수이동평균)의 정확한 계산을 위해 충분한 과거 데이터가 필요합니다.
    # 화면에는 1년치만 보여도, 내부 계산의 정확도를 위해 2년(730일) 데이터를 조회합니다.
    "CHART_LOOKBACK_DAYS": 730,    # 일봉 데이터 조회 기간 (일 단위)

    # [SAR] Parabolic SAR (추세 반전 지표)
    # - 가속변수(AF) 값이 클수록 점이 주가에 바짝 붙어 움직이며 반전 신호가 빨리 나옵니다.
    # - 단기 매매나 타이트한 익절(Trailing Stop)을 원하면 값을 키우세요 (예: 0.02 -> 0.03~0.05).
    # - 잦은 매매 신호를 피하고 추세를 길게 보려면 값을 유지하거나 줄이세요.
    "SAR_AF_START": 0.02,          # 가속변수(AF) 초기값 (기본 0.02)
    "SAR_AF_STEP": 0.02,           # 추세가 지속될 때마다 더해지는 가속 증가값 (기본 0.02)
    "SAR_AF_MAX": 0.2,             # 가속변수가 커질 수 있는 최대 한계값 (기본 0.2)

    # [ADX] Average Directional Movement Index (추세 강도)
    # - 추세의 '강도'를 측정하는 지표입니다. (방향 아님)
    # - 기간을 줄이면 최근 변동성을 더 빠르게 반영합니다.
    "ADX_PERIOD": 14,              # ADX 계산 기간 (기본 14일)

    # [CCI] Commodity Channel Index (과매수/과매도)
    # - 현재 주가가 이동평균과 얼마나 떨어져 있는지를 측정합니다.
    # - 기간을 줄이면(예: 14) 지표가 민감하게 움직여 단기 매매 신호를 자주 줍니다.
    # - 기간을 늘리면(예: 20~40) 큰 추세의 흐름을 파악하기 좋습니다.
    "CCI_WINDOW": 20,              # CCI 계산 기간 (기본 20일)
    "CCI_UPPER": 100,              # 과매수 기준선 (+100 이상이면 과열)
    "CCI_LOWER": -100,             # 과매도 기준선 (-100 이하면 침체)

    # [MACD] Moving Average Convergence Divergence (추세 및 모멘텀)
    # - 단기 이평선과 장기 이평선의 차이를 이용해 매매 신호를 포착합니다.
    # - (12, 26, 9)는 전 세계 표준 설정값입니다.
    # - 단기 매매용 설정 예시: (5, 20, 5) -> 신호가 매우 빨라지지만 속임수(False Signal)도 많아집니다.
    # - 장기 투자용 설정 예시: (24, 52, 18) -> 신호는 늦지만 큰 추세를 확인하기 좋습니다.
    "MACD_FAST": 12,               # 단기 지수이동평균 기간 (Fast EMA)
    "MACD_SLOW": 26,               # 장기 지수이동평균 기간 (Slow EMA)
    "MACD_SIGNAL": 9,              # 시그널(MACD선의 이동평균) 기간

    # [OBV] On-Balance Volume (거래량 추세)
    # - 거래량은 주가에 선행한다는 전제로, 주가 상승일 거래량은 더하고 하락일은 뺍니다.
    # - OBV 값이 이 이동평균선 위에 있으면 매집(상승) 추세로 봅니다.
    "OBV_MA_PERIOD": 5,            # OBV의 추세 방향을 판단할 이동평균 기간 (짧을수록 민감)

    # [RSI] Relative Strength Index (상대 강도 지수)
    # - 주가의 상승폭과 하락폭을 비교해 과매수/과매도를 판단합니다.
    # - 기간을 줄이면(예: 9) 민감해져서 매매 신호가 자주 발생합니다 (단타용).
    # - 기간을 늘리면(예: 25) 신호 빈도는 줄지만 신뢰도가 높아집니다 (스윙/중장기용).
    "RSI_PERIOD": 14,              # RSI 계산 기간 (기본 14일)
    "RSI_SIGNAL": 14,              # RSI의 이동평균선(시그널) 계산 기간 (참고용)
    "RSI_UPPER": 70,               # 과매수 기준선 (이 값 이상이면 매도 고려)
    "RSI_MID": 50,                 # 기준선 (50 이상이면 강세, 이하면 약세)
    "RSI_LOWER": 30                # 과매도 기준선 (이 값 이하면 매수 고려)
}

# ==========================================================
# [설정] 종목 분석 및 상태 분류 임계값
# ==========================================================
ANALYSIS_THRESHOLDS = {
    # [매수 기준 점수]
    # 기술적 분석 지표(이평선, SAR, RSI, ADX, CCI, OBV 등)를 종합하여 산출된 점수입니다.
    # 총점 만점은 지표 조합에 따라 다르지만 보통 10점 내외입니다.
    # 이 점수 이상일 때 '매수' 상태로 분류합니다. (기본값: 8)
    # - 값을 낮추면(예: 6~7) 매수 신호가 자주 발생하지만, 속임수(False Signal) 가능성이 높아집니다.
    # - 값을 높이면(예: 9) 신호 빈도는 줄어들지만, 확실한 상승 추세에서만 진입합니다.
    "BUY_SCORE": 8,

    # [상승 추세 기준 점수]
    # 매수 기준에는 미치지 못하지만, 상승 흐름이 있다고 판단하는 점수입니다. (기본값: 6)
    "RISE_SCORE": 6,
    
    # [RSI 과열 기준]
    # 매수 점수를 충족하더라도, RSI가 이 값 이상이면 '과열'로 판단하여 매수 추천에서 제외합니다.
    # (기본값: 60 - 상승 여력이 남아있는 구간에서만 진입하기 위함)
    "BUY_RSI_MAX": 60,

    # [이격도 경고 기준]
    # 20일 이동평균선 대비 현재가 비율(%) 기준
    "DISPARITY_UPPER": 110, # 단기 과열 (110% 이상)
    "DISPARITY_LOWER": 90   # 과매도 (90% 이하)
}

# ==========================================================
# [설정] 매도 전략 임계값 (Backtest & Trading)
# ==========================================================
SELL_STRATEGY = {
    # [손절매 기준 (Stop Loss)]
    # 진입가 대비 손실률이 이 값 이하가 되면 즉시 매도하여 손실을 제한합니다.
    # (기본값: -7.0%)
    # - 값을 줄이면(예: -3.0) 손실은 줄지만, 일시적 하락에 잦은 손절이 발생할 수 있습니다.
    # - 값을 늘리면(예: -10.0) 버티는 힘은 커지지만, 큰 손실로 이어질 위험이 있습니다.
    "STOP_LOSS_RATE": -7.0,

    # [익절 기준 (Take Profit)]
    # 진입가 대비 수익률이 이 값 이상이 되면 이익을 실현합니다.
    # (기본값: 30.0%)
    # - 트레일링 스탑 발동 여부와 관계없이, 이 수익률에 도달하면 즉시 이익을 실현합니다.
    # - 즉, 트레일링 스탑은 수익 보전용으로, 이 값은 최종 목표 수익률로 작동합니다.
    "TAKE_PROFIT_RATE": 30.0,

    # [익절 RSI 기준]
    # 수익률 조건을 만족하지 않더라도, RSI가 이 값 이상이면 과열로 판단하여 이익 실현합니다.
    # (기본값: 75)
    # - RSI 70~80 구간은 강력한 매수세가 있지만, 곧 조정이 올 가능성이 높은 구간입니다.
    "TAKE_PROFIT_RSI": 75,
    
    # [트레일링 스탑 (Trailing Stop)]
    # 수익을 극대화하기 위해 주가가 상승함에 따라 익절 라인을 함께 올리는 전략입니다.
    # 1. 발동 조건: 수익률이 이 값 이상 도달해야 트레일링 스탑 감시가 시작됩니다. (기본값: 10.0%)
    #    (※ 팁: 이 값을 '익절 기준'보다 낮게 잡으면 추세 추종형, 높게 잡으면 목표 달성형이 됩니다.)
    "TRAILING_STOP_ACTIVATION_RATE": 10.0,
    
    # 2. 매도 조건: 최고가 대비 이 비율만큼 하락하면 이익 실현 매도를 수행합니다. (기본값: 3.0%)
    "TRAILING_STOP_CALLBACK_RATE": 3.0,
    
    # [점수 하락 매도 기준]
    # 종목의 종합 점수가 이 값 미만으로 떨어지면 추세가 꺾인 것으로 보고 매도합니다.
    # (기본값: 5)
    # - "위험" 상태가 아니더라도 점수가 이 값 미만이면 매도합니다.
    # - "주의"나 "관망" 상태라도 점수가 이 값 이상이면 보유합니다.
    "SELL_SCORE": 5
}

# ==========================================================
# [설정] 시스템 트레이딩 (System Trading)
# ==========================================================
SYSTEM_TRADING_INTERVAL = 180  # 자동매매 모니터링 주기 (초)
# - 너무 짧으면(예: 10초) API 호출 제한(Rate Limit)에 걸릴 수 있습니다.
# - 너무 길면(예: 300초) 급변하는 시세에 대응하기 어렵습니다.
SYSTEM_TRADING_LOG_DIR = "logs" # 시스템 트레이딩 로그 저장 디렉토리
# (파일명은 system_trade_YYYY-MM-DD.log 형태로 자동 생성됩니다)
SYSTEM_INVEST_PER_STOCK = 0.5  # 종목당 투자 비중 (최초 자산의 50%씩 분산 투자)
SYSTEM_MAX_CONSECUTIVE_ERRORS = 5  # [안전장치] 연속 에러 5회 발생 시 자동 중단
SYSTEM_DAILY_LOSS_LIMIT = 10.0     # [안전장치] 일일 손실률 10.0% 도달 시 자동 중단 (0.0이면 미사용)
SYSTEM_TRADING_START_TIME = "0915" # 거래 시작 시간 (HHMM) - 장 시작 후 안정화 대기
SYSTEM_TRADING_END_TIME = "1515"   # 거래 종료 시간 (HHMM) - 장 마감 전 정리

# [추가] 시스템 트레이딩 로거 (AutoTrader 실행 시 함수 할당)
SYSTEM_LOGGER = None

# [추가] 시스템 트레이딩 우선순위 처리를 위한 락 (RLock 사용)
SYSTEM_TRADING_LOCK = threading.RLock()

# [추가] 스레드별 컨텍스트 관리 (API 호출 시 계좌 분리용)
trade_context = threading.local()

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

# [추가] 시스템 트레이딩 전용 변수 (실전 모드에서 분리 운용 시 사용)
AUTO_APP_KEY = ""
AUTO_APP_SECRET = ""
AUTO_CANO = ""
AUTO_ACNT_PRDT_CD = ""

# [추가] 텔레그램 알림 설정
TELEGRAM_BOT_TOKEN = ""
TELEGRAM_CHAT_ID = ""

# 서버 URL 상수 정의
SIM_URL = "https://openapivts.koreainvestment.com:29443"
REAL_URL = "https://openapi.koreainvestment.com:9443"

# 거래소 정보 캐싱
EXCHANGE_CACHE = {}

# 종목 리스트 데이터
STOCK_CONFIG_DATA = {}

def initialize_environment():
    global APP_KEY, APP_SECRET, CANO, ACNT_PRDT_CD, URL_BASE, IS_SIMULATION
    global REAL_APP_KEY, REAL_APP_SECRET, AUTO_APP_KEY, AUTO_APP_SECRET, TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID
    global AUTO_CANO, AUTO_ACNT_PRDT_CD
    
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
    auto_acc_num = None
    
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
        
        # [추가] 시스템 트레이딩 전용 계좌 정보 로드
        AUTO_APP_KEY = os.environ.get("AUTO_APP_KEY")
        AUTO_APP_SECRET = os.environ.get("AUTO_APP_SECRET")
        auto_acc_num = os.environ.get("AUTO_ACC_NUM")
        
        URL_BASE = REAL_URL
        IS_SIMULATION = False
        console.print()
        console.print("[bold red]실전투자 서버 환경을 로드했습니다. (실제 자산 거래 주의)[/bold red]")

    if not all([APP_KEY, APP_SECRET, acc_num_input]):
        console.print("[bold red]오류: 해당 환경의 필수 환경변수(KEY, SECRET, ACC_NUM)가 설정되지 않았습니다.[/bold red]")
        sys.exit()

    # [추가] 텔레그램 설정 로드 (선택 사항)
    TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")
    TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID")

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

    # [추가] 자동매매 계좌번호 파싱
    if auto_acc_num:
        clean_auto = auto_acc_num.strip().replace('-', '')
        if len(clean_auto) == 8:
            AUTO_CANO = clean_auto
            AUTO_ACNT_PRDT_CD = "01"
        elif len(clean_auto) == 10:
            AUTO_CANO = clean_auto[:8]
            AUTO_ACNT_PRDT_CD = clean_auto[8:]

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
