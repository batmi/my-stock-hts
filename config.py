# config.py
import os
import threading
from rich.console import Console
import logging
from datetime import datetime, timedelta
from logging.handlers import RotatingFileHandler, TimedRotatingFileHandler
from session import SessionManager

console = Console()

# ==========================================================
# [설정] 스크린 디버그 로그 레벨 설정 (OFF / DEBUG / TRACE)
# ==========================================================
# OFF   : 로그 출력 없음
# TRACE : [TRACE] 로그 화면 출력
# DEBUG : [TRACE] 및 [DEBUG] 로그 화면 출력
SCREEN_DEBUG_LEVEL = "OFF"

# ==========================================================
# [설정] 파일 로그 레벨 설정 (DEBUG / INFO / WARNING / ERROR / CRITICAL)
# ==========================================================
FILE_DEBUG_LEVEL = "WARNING"

# ==========================================================
# [설정] 텔레그램 설정 (Telegram Configuration)
# ==========================================================
# 보안을 위해 소스 코드에 직접 입력하기보다 환경 변수 사용을 권장합니다.
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")
TELEGRAM_INSTANCE_NAME = "HTS"
TELEGRAM_POLLING_TIMEOUT = 10
ENABLE_TELEGRAM = True

# ==========================================================
# [설정] 트랜잭션 속도 제한 (Rate Limiting)
# ==========================================================
SIM_TX_PER_SECOND = 2     # 모의투자 서버 최대 TPS: 2
REAL_TX_PER_SECOND = 20   # 실전투자 서버 최대 TPS: 20

# ==========================================================
# [설정] 파일 경로 관리
# ==========================================================
# 프로젝트 루트 디렉토리 및 서브 디렉토리 정의
BASE_DIR = os.path.dirname(os.path.abspath(__file__))

DB_DIR = os.path.join(BASE_DIR, "db")
CHART_DIR = os.path.join(BASE_DIR, "chart")
DATA_DIR = os.path.join(BASE_DIR, "data")
JSON_DIR = os.path.join(BASE_DIR, "json")
LOG_DIR = os.path.join(BASE_DIR, "logs")

# 디렉토리 자동 생성
for d in [DB_DIR, CHART_DIR, DATA_DIR, JSON_DIR, LOG_DIR]:
    if not os.path.exists(d):
        try: os.makedirs(d)
        except Exception: pass

# 종목 분석 대상 목록을 저장하는 JSON 파일의 경로입니다.
# (기본값: json/stock.json)
STOCK_DATA_FILE = os.path.join(JSON_DIR, "stock.json")

# API 접속 토큰을 캐싱하여 재사용하기 위한 파일 경로입니다.
TOKEN_CACHE_FILE = os.path.join(JSON_DIR, "token_cache.json")

# [추가] 거래 내역 및 스냅샷을 저장할 SQLite DB 파일 경로
DB_FILE_PATH = os.path.join(DB_DIR, "trade_history.db")

# ==========================================================
# [설정] 데이터 보존 및 관리 정책
# ==========================================================
# [추가] DB 데이터 보존 기간 (일 단위)
# 설정된 기간보다 오래된 거래 내역은 프로그램 종료 시 자동으로 삭제됩니다.
# (기본값: 365일, 0으로 설정 시 자동 삭제 안 함)

DB_DATA_RETENTION_DAYS = 365

# [추가] 로그 파일 보존 기간 (일 단위)
# 설정된 기간보다 오래된 로그 파일은 자동매매 시작 시 자동으로 삭제됩니다.
# (기본값: 30일, 0으로 설정 시 자동 삭제 안 함)
LOG_RETENTION_DAYS = 30

# ==========================================================
# [설정] 시스템 및 네트워크 정책
# ==========================================================
DEFAULT_TIMEOUT = 10      # API 요청 타임아웃 (초)
RETRY_DELAY_SERVER = 1.0  # 서버 에러 발생 시 재시도 대기 시간 (초) (기본값 1.0)

# ==========================================================
# [추가] API 요청 중 연결 끊김(RemoteDisconnected 등) 발생 시 재시도 횟수
# 0으로 설정하면 재시도하지 않으며, 1로 설정하면 실패 시 1회 재시도합니다.
# 재시도 시 RETRY_DELAY_SERVER를 이용한 백오프 방식으로 대기 후 재시도합니다.
# ==========================================================
MAX_RETRIES = 2

# ==========================================================
# [설정] 기본 환율 (Fallback)
# ==========================================================
# 실시간 환율 조회(yfinance) 실패 시 / 백테스팅 시 사용할 기본 환율입니다.
DEFAULT_EXCHANGE_RATE = 1450.00

# ==========================================================
# [설정] 시스템 트레이딩 (System Trading)
# ==========================================================
SYSTEM_TRADING_INTERVAL = 180  # 자동매매 모니터링 주기 (초)
# - 너무 짧으면(예: 10초) API 호출 제한(Rate Limit)에 걸릴 수 있습니다.
# - 너무 길면(예: 300초) 급변하는 시세에 대응하기 어렵습니다.
SYSTEM_TRADING_LOG_DIR = LOG_DIR # 시스템 트레이딩 로그 저장 디렉토리
# (파일명은 system_trade_YYYY-MM-DD.log 형태로 자동 생성됩니다)

# [종목당 최대 투자 비중]
# 전체 자산 대비 한 종목에 투자할 최대 비중입니다. (기본값: 0.5 = 50%)
# - 리스크 기반 포지션 사이징(SYSTEM_RISK_PER_TRADE)과 함께 사용될 경우,
#   두 방식 중 '더 적은 금액'이 최종 투자 금액으로 결정됩니다. (이중 안전장치: 몰빵 방지 + 리스크 관리)
# - 만약 리스크 기반 사이징만 전적으로 따르고 싶다면 이 값을 1.0(100%)으로 설정하세요.
SYSTEM_INVEST_PER_STOCK = 0.5

# [최대 보유 종목 수]
# 포트폴리오에 담을 수 있는 최대 종목 개수입니다. (기본값: 5)
SYSTEM_MAX_HOLDINGS = 5

USE_MARKET_FILTER = True       # [추가] 장세 판단 필터 사용 여부 (코스피 지수 추세 확인)
MARKET_FILTER_MA = 20          # [추가] 시장 필터링 기준 이동평균선 (일)
SYSTEM_MAX_CONSECUTIVE_ERRORS = 5  # [안전장치] 연속 에러 5회 발생 시 자동 중단
SYSTEM_DAILY_LOSS_LIMIT = 10.0     # [안전장치] 일일 손실률 10.0% 도달 시 자동 중단 (0.0이면 미사용)
SYSTEM_RISK_PER_TRADE = 5.0        # [안전장치] 1회 매매 시 계좌 대비 최대 허용 손실률 (%) (0.0이면 미사용)

# [설정] 변동성 타겟팅 (Volatility Targeting)
USE_VOLATILITY_TARGETING = True    # 변동성 타겟팅 사용 여부
TARGET_VOLATILITY = 0.30           # 목표 연간 변동성 (30%)
                                   # - 0.10 ~ 0.15: 보수적/안정적 (생존 우선, MDD 최소화)
                                   # - 0.15 ~ 0.20: 중립적 (시장 수익률 추구)
                                   # - 0.25 ~ 0.30: 적극적 (고수익 추구, 변동성 허용) -> 현재 설정
VOLATILITY_SCALING_MAX = 2.0       # 최대 확대 배수 (2배) - 변동성이 낮을 때 포지션 확대 제한
VOLATILITY_SCALING_MIN = 0.3       # 최소 축소 배수 (0.3배) - 변동성이 높을 때 최소 포지션 유지

# [추가] 슬리피지 비율 (Slippage Rate)
# 매수/매도 주문 시 현재가 대비 불리한 가격으로 주문을 내어 체결 확률을 높이고,
# 백테스팅 시 실제 체결 오차를 반영하기 위한 비율입니다.
# (기본값: 0.001 = 0.1%)
SLIPPAGE_RATE = 0.001

SYSTEM_TRADING_START_TIME = "0915" # 거래 시작 시간 (HHMM) - 장 시작 후 안정화 대기
SYSTEM_TRADING_END_TIME = "1515"   # 거래 종료 시간 (HHMM) - 장 마감 전 정리

# [추가] 체결 감시 모니터링 주기 (초)
# 1. 집중 감시 주기: 주문 발생 직후 체결 확인 주기 (기본값: 5초)
CONCLUSION_CHECK_INTERVAL = 5

# 2. 대기 모드 주기: 주문이 없는 평상시 확인 주기 (기본값: 300초 = 5분)
# (0으로 설정하면 평상시에는 아예 확인하지 않습니다. 외부 HTS 주문 감지 불필요 시 0 권장)
CONCLUSION_CHECK_IDLE_INTERVAL = 300

# 3. 집중 감시 유지 시간: 주문 후 짧은 주기로 확인할 시간 (기본값: 100초)
CONCLUSION_CHECK_ACTIVE_DURATION = 100

# [추가] 미체결 주문 자동 취소 대기 시간 (초)
# 지정가 주문 후 이 시간이 지나도 체결되지 않으면 주문을 취소하여 현금을 확보합니다. (기본값: 600초 = 10분)
UNFILLED_ORDER_CANCEL_SECONDS = 600

# ==========================================================
# [설정] 종목 분석 및 상태 분류 임계값
# ==========================================================
ANALYSIS_THRESHOLDS = {
    # [매수 기준 점수]
    # 기술적 분석 지표(이평선, SAR, RSI, ADX, CCI, OBV 등)를 종합하여 산출된 점수입니다.
    # 총점 만점은 지표 조합에 따라 다르지만 보통 10점 내외입니다.
    # 이 점수 이상일 때 '매수' 상태로 분류합니다. (기본값: 8.0)
    # - 값을 낮추면(예: 6~7) 매수 신호가 자주 발생하지만, 속임수(False Signal) 가능성이 높아집니다.
    # - 값을 높이면(예: 9) 신호 빈도는 줄어들지만, 확실한 상승 추세에서만 진입합니다.
    "BUY_SCORE": 8.0,

    # [상승 추세 기준 점수]
    # 매수 기준에는 미치지 못하지만, 상승 흐름이 있다고 판단하는 점수입니다. (기본값: 6)
    "RISE_SCORE": 6.0,
    
    # [RSI 과열 기준]
    # 매수 점수를 충족하더라도, RSI가 이 값 이상이면 '과열'로 판단하여 매수 추천에서 제외합니다.
    # (기본값: 65 - 상승 여력이 남아있는 구간에서만 진입하기 위함)
    "BUY_RSI_MAX": 65,
    
    # [체결강도 기준]
    # 매수 시점의 체결강도가 이 값 이상이어야 진입합니다. (기본값: 100.0)
    # 100% 이상은 매수세가 매도세보다 강함을 의미합니다.
    "BUY_VOL_STRENGTH": 100.0,

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

    # [점수 하락 매도 기준]
    # 종목의 종합 점수가 이 값 미만으로 떨어지면 추세가 꺾인 것으로 보고 매도합니다.
    # (기본값: 5)
    # - "위험" 상태가 아니더라도 점수가 이 값 미만이면 매도합니다.
    # - "주의"나 "관망" 상태라도 점수가 이 값 이상이면 보유합니다.
    "SELL_SCORE": 5.0,

    # [트레일링 스탑 (Trailing Stop)]
    # 수익을 극대화하기 위해 주가가 상승함에 따라 익절 라인을 함께 올리는 전략입니다.
    # 1. 발동 조건: 수익률이 이 값 이상 도달해야 트레일링 스탑 감시가 시작됩니다. (기본값: 10.0%)
    #    (※ 팁: 이 값을 '익절 기준'보다 낮게 잡으면 추세 추종형, 높게 잡으면 목표 달성형이 됩니다.)
    "TRAILING_STOP_ACTIVATION_RATE": 10.0,
    
    # 2. 매도 조건: 최고가 대비 이 비율만큼 하락하면 이익 실현 매도를 수행합니다. (기본값: 3.0%)
    "TRAILING_STOP_CALLBACK_RATE": 3.0,

    # [ATR 기반 손절 설정]
    # 고정 손절률(STOP_LOSS_RATE) 대신 ATR(변동성)을 기반으로 손절폭을 동적으로 설정합니다.
    # True로 설정 시, 매수 시점의 ATR * Multiplier 만큼의 비율이 손절률로 적용됩니다.
    "USE_ATR_STOP": True,          # ATR 기반 손절 사용 여부 (기본값: True - 권장)
    "ATR_STOP_MULTIPLIER": 2.0     # ATR 배수 (기본값: 2.0 - 보통 1.5~3.0 사용)
}

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
    "RSI_LOWER": 30,               # 과매도 기준선 (이 값 이하면 매수 고려)

    # [ATR] Average True Range (평균 진폭)
    # - 변동성 지표로, 손절폭 계산이나 변동성 타겟팅에 사용됩니다.
    # - 기간이 짧을수록 최근 변동성을 민감하게 반영합니다.
    "ATR_PERIOD": 14               # ATR 계산 기간 (기본 14일)
}


# ==========================================================
# [추가] 시스템 트레이딩 로거 (AutoTrader 실행 시 함수 할당)
SYSTEM_LOGGER = None

# ==========================================================
# [추가] 시스템 트레이딩 우선순위 처리를 위한 락 (RLock 사용)
SYSTEM_TRADING_LOCK = threading.RLock()

# ==========================================================
# [추가] 토큰 갱신 경합 방지를 위한 락 (API 모듈에서 사용)
TOKEN_REFRESH_LOCK = threading.RLock()

# ==========================================================
# [추가] 스레드별 컨텍스트 관리 (API 호출 시 계좌 분리용)
trade_context = threading.local()

# ==========================================================
# [추가] 토큰 관리 및 스레드 제어용 전역 변수
MAIN_THREAD_ID = None
TOKEN_EXPIRED = False

# ==========================================================
# [추가] 사용자 입력 경로 추적용 (Breadcrumb)
USER_ACTION_BREADCRUMB = []

session = SessionManager()

# 서버 URL 상수 정의
SIM_URL = "https://openapivts.koreainvestment.com:29443"
REAL_URL = "https://openapi.koreainvestment.com:9443"

# [추가] 로그 파일명 변경을 위한 Namer 함수
def _log_namer(name):
    """
    로테이션된 로그 파일명을 '파일명_YYYYMMDD.log' 형태로 변경
    예: mystock.log.20260218 -> mystock_20260218.log
    """
    try:
        base, date_part = name.rsplit('.', 1) # logs/mystock.log, 20260218
        dir_name = os.path.dirname(base)
        file_name = os.path.basename(base) # mystock.log
        root, ext = os.path.splitext(file_name) # mystock, .log
        return os.path.join(dir_name, f"{root}_{date_part}{ext}")
    except:
        return name

# [추가] 로깅 설정 초기화 함수
def setup_logging():
    # 기존 핸들러 제거
    root = logging.getLogger()
    if root.handlers:
        for handler in root.handlers:
            root.removeHandler(handler)

    # 로깅 활성화
    logging.disable(logging.NOTSET)
    

    if not os.path.exists(LOG_DIR):
        try: os.makedirs(LOG_DIR)
        except: pass

    # [수정] 오래된 로그 파일 정리 (핸들러가 관리하지 않는 과거 패턴 파일만 정리)
    try:
        if LOG_RETENTION_DAYS > 0:
            cutoff_date = datetime.now().date() - timedelta(days=LOG_RETENTION_DAYS)
            for filename in os.listdir(LOG_DIR):
                file_path = os.path.join(LOG_DIR, filename)
                
                # 기존 system_trade_YYYY-MM-DD.log 정리 (새로운 autotrade.log는 핸들러가 관리)
                if filename.startswith("system_trade_") and filename.endswith(".log"):
                    try:
                        date_part = filename.replace("system_trade_", "").replace(".log", "")
                        file_date = datetime.strptime(date_part, "%Y-%m-%d").date()
                        if file_date < cutoff_date:
                            os.remove(file_path)
                    except: pass
    except: pass

    log_filename = "mystock.log" # [수정] 고정 파일명 사용
    log_filepath = os.path.join(LOG_DIR, log_filename)

    # [수정] TimedRotatingFileHandler 적용 (매일 자정 로테이션, mystock_YYYYMMDD.log 백업)
    file_handler = TimedRotatingFileHandler(
        log_filepath, when='midnight', interval=1, backupCount=LOG_RETENTION_DAYS, encoding='utf-8'
    )
    file_handler.suffix = "%Y%m%d"
    file_handler.namer = _log_namer

    # [수정] 로그 포맷에 파일명과 라인 번호 추가
    file_handler.setFormatter(logging.Formatter('%(asctime)s.%(msecs)03d [%(levelname)s] [%(threadName)s] %(filename)s:%(lineno)d - %(message)s', datefmt='%H:%M:%S'))
    
    # 파일 로그 레벨 설정
    level_name = FILE_DEBUG_LEVEL.upper()
    numeric_level = getattr(logging, level_name, logging.INFO)

    logging.basicConfig(level=numeric_level, handlers=[file_handler], force=True)

# [추가] 시스템 트레이딩 전용 로거 설정 함수
def get_autotrade_logger():
    """시스템 트레이딩 로그(autotrade.log)를 위한 로거 반환"""
    logger = logging.getLogger("autotrade")
    
    # [수정] 기존 핸들러가 있다면 모두 제거하고 새로 설정 (파일 생성 보장 및 중복 방지)
    if logger.hasHandlers():
        for h in list(logger.handlers):
            logger.removeHandler(h)
        
    logger.setLevel(logging.INFO)
    logger.propagate = False # 루트 로거로 전파 방지
    
    # [추가] 로그 디렉토리 확인 및 생성 (파일 생성 에러 방지)
    if not os.path.exists(LOG_DIR):
        try: os.makedirs(LOG_DIR)
        except: pass

    log_filename = "autotrade.log"
    log_filepath = os.path.join(LOG_DIR, log_filename)
    
    # 매일 자정 로테이션, autotrade_YYYYMMDD.log 백업
    handler = TimedRotatingFileHandler(
        log_filepath, when='midnight', interval=1, backupCount=LOG_RETENTION_DAYS, encoding='utf-8', delay=False
    )
    handler.suffix = "%Y%m%d"
    handler.namer = _log_namer
    
    # 메시지만 출력 (타임스탬프는 AutoTrader가 메시지에 포함해서 보냄)
    handler.setFormatter(logging.Formatter('%(message)s'))
    
    logger.addHandler(handler)
    return logger

# [추가] 동적 설정 로드 함수 (사용자가 변경한 설정을 덮어씌움)
def load_dynamic_config():
    import json
    config_path = os.path.join(JSON_DIR, "dynamic_config.json")
    if os.path.exists(config_path):
        try:
            with open(config_path, 'r', encoding='utf-8') as f:
                data = json.load(f)
            
            if "ANALYSIS_THRESHOLDS" in data:
                ANALYSIS_THRESHOLDS.update(data["ANALYSIS_THRESHOLDS"])
            
            if "SELL_STRATEGY" in data:
                SELL_STRATEGY.update(data["SELL_STRATEGY"])
                
            if "INDICATOR_PARAMS" in data:
                INDICATOR_PARAMS.update(data["INDICATOR_PARAMS"])
            
            global SYSTEM_INVEST_PER_STOCK, SYSTEM_TRADING_INTERVAL, SYSTEM_DAILY_LOSS_LIMIT, USE_MARKET_FILTER, MARKET_FILTER_MA
            global CONCLUSION_CHECK_INTERVAL, CONCLUSION_CHECK_IDLE_INTERVAL, CONCLUSION_CHECK_ACTIVE_DURATION
            global ENABLE_TELEGRAM
            global TELEGRAM_INSTANCE_NAME, TELEGRAM_POLLING_TIMEOUT
            global SCREEN_DEBUG_LEVEL, FILE_DEBUG_LEVEL
            global SYSTEM_MAX_CONSECUTIVE_ERRORS, SYSTEM_TRADING_START_TIME, SYSTEM_TRADING_END_TIME, SYSTEM_MAX_TRADES_PER_DAY, SYSTEM_RISK_PER_TRADE
            global USE_VOLATILITY_TARGETING, TARGET_VOLATILITY, VOLATILITY_SCALING_MAX, VOLATILITY_SCALING_MIN
            global UNFILLED_ORDER_CANCEL_SECONDS
            global SLIPPAGE_RATE
            
            if "SYSTEM_INVEST_PER_STOCK" in data: SYSTEM_INVEST_PER_STOCK = data["SYSTEM_INVEST_PER_STOCK"]
            if "SYSTEM_MAX_HOLDINGS" in data: SYSTEM_MAX_HOLDINGS = data["SYSTEM_MAX_HOLDINGS"]
            if "SYSTEM_TRADING_INTERVAL" in data: SYSTEM_TRADING_INTERVAL = data["SYSTEM_TRADING_INTERVAL"]
            if "SYSTEM_DAILY_LOSS_LIMIT" in data: SYSTEM_DAILY_LOSS_LIMIT = data["SYSTEM_DAILY_LOSS_LIMIT"]
            
            if "USE_MARKET_FILTER" in data: USE_MARKET_FILTER = data["USE_MARKET_FILTER"]
            if "MARKET_FILTER_MA" in data: MARKET_FILTER_MA = data["MARKET_FILTER_MA"]
            
            if "CONCLUSION_CHECK_INTERVAL" in data: CONCLUSION_CHECK_INTERVAL = data["CONCLUSION_CHECK_INTERVAL"]
            if "CONCLUSION_CHECK_IDLE_INTERVAL" in data: CONCLUSION_CHECK_IDLE_INTERVAL = data["CONCLUSION_CHECK_IDLE_INTERVAL"]
            if "CONCLUSION_CHECK_ACTIVE_DURATION" in data: CONCLUSION_CHECK_ACTIVE_DURATION = data["CONCLUSION_CHECK_ACTIVE_DURATION"]
            if "UNFILLED_ORDER_CANCEL_SECONDS" in data: UNFILLED_ORDER_CANCEL_SECONDS = data["UNFILLED_ORDER_CANCEL_SECONDS"]
            
            if "ENABLE_TELEGRAM" in data: ENABLE_TELEGRAM = data["ENABLE_TELEGRAM"]
            if "TELEGRAM_INSTANCE_NAME" in data: TELEGRAM_INSTANCE_NAME = data["TELEGRAM_INSTANCE_NAME"]
            if "TELEGRAM_POLLING_TIMEOUT" in data: TELEGRAM_POLLING_TIMEOUT = data["TELEGRAM_POLLING_TIMEOUT"]
            
            if "SCREEN_DEBUG_LEVEL" in data: SCREEN_DEBUG_LEVEL = data["SCREEN_DEBUG_LEVEL"]
            if "FILE_DEBUG_LEVEL" in data: FILE_DEBUG_LEVEL = data["FILE_DEBUG_LEVEL"]
            
            if "SYSTEM_MAX_CONSECUTIVE_ERRORS" in data: SYSTEM_MAX_CONSECUTIVE_ERRORS = data["SYSTEM_MAX_CONSECUTIVE_ERRORS"]
            if "SYSTEM_TRADING_START_TIME" in data: SYSTEM_TRADING_START_TIME = data["SYSTEM_TRADING_START_TIME"]
            if "SYSTEM_TRADING_END_TIME" in data: SYSTEM_TRADING_END_TIME = data["SYSTEM_TRADING_END_TIME"]
            if "SYSTEM_RISK_PER_TRADE" in data: SYSTEM_RISK_PER_TRADE = data["SYSTEM_RISK_PER_TRADE"]
            if "USE_VOLATILITY_TARGETING" in data: USE_VOLATILITY_TARGETING = data["USE_VOLATILITY_TARGETING"]
            if "TARGET_VOLATILITY" in data: TARGET_VOLATILITY = data["TARGET_VOLATILITY"]
            if "VOLATILITY_SCALING_MAX" in data: VOLATILITY_SCALING_MAX = data["VOLATILITY_SCALING_MAX"]
            if "VOLATILITY_SCALING_MIN" in data: VOLATILITY_SCALING_MIN = data["VOLATILITY_SCALING_MIN"]
            
            if "SLIPPAGE_RATE" in data: SLIPPAGE_RATE = data["SLIPPAGE_RATE"]

        except Exception as e:
            print(f"[Config] 동적 설정 로드 실패: {e}")

# 모듈 로드 시 자동 실행
load_dynamic_config()
