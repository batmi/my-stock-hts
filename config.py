# config.py
import os
import re
from rich.console import Console
import logging
from datetime import datetime, timedelta
from logging.handlers import TimedRotatingFileHandler
from session import SessionManager
from dotenv import load_dotenv
from pydantic import BaseModel, Field
import threading

# .env 파일 로드 (환경 변수 우선순위: 시스템 환경변수 > .env 파일)
load_dotenv()

console = Console()

class GlobalSettings(BaseModel):
    """동적으로 변경 가능한 전역 설정값들을 관리하는 Pydantic 모델"""

    # ==========================================================
    # [설정] 스크린 디버그 로그 레벨 설정 (OFF / DEBUG / TRACE)
    # ==========================================================
    # OFF   : 로그 출력 없음
    # TRACE : [TRACE] 로그 화면 출력
    # DEBUG : [TRACE] 및 [DEBUG] 로그 화면 출력
    SCREEN_DEBUG_LEVEL: str = "OFF"

    # ==========================================================
    # [설정] 화면 자동 지우기 (Clear Screen)
    # ==========================================================
    # 메뉴 이동 시 이전 터미널 내용을 지우고 깔끔하게 유지할지 여부입니다.
    CLEAR_SCREEN_ON_MENU: bool = False

    # ==========================================================
    # [설정] 파일 로그 레벨 설정 (DEBUG / INFO / WARNING / ERROR / CRITICAL)
    # ==========================================================
    FILE_DEBUG_LEVEL: str = "WARNING"

    # ==========================================================
    # [설정] 텔레그램 설정 (Telegram Configuration)
    # ==========================================================
    ENABLE_TELEGRAM: bool = True
    AUTO_MORNING_BRIEFING_USE: bool = False
    AUTO_MORNING_BRIEFING_TIME: str = "0830"
    TELEGRAM_INSTANCE_NAME: str = "HTS"
    TELEGRAM_POLLING_TIMEOUT: int = Field(default=10, gt=0)

    # ==========================================================
    # [설정] 시스템 트레이딩 (System Trading)
    # ==========================================================
    # 자동매매 모니터링 주기 (초)
    # - 너무 짧으면(예: 10초) API 호출 제한(Rate Limit)에 걸릴 수 있습니다.
    # - 너무 길면(예: 300초) 급변하는 시세에 대응하기 어렵습니다.
    SYSTEM_TRADING_INTERVAL: int = Field(default=180, gt=0)

    # [종목당 최대 투자 비중]
    # 전체 자산 대비 한 종목에 투자할 최대 비중입니다. (기본값: 0.1 = 10%)
    # - 리스크 기반 포지션 사이징(SYSTEM_RISK_PER_TRADE)과 함께 사용될 경우,
    #   두 방식 중 '더 적은 금액'이 최종 투자 금액으로 결정됩니다. (이중 안전장치: 몰빵 방지 + 리스크 관리)
    # - 만약 리스크 기반 사이징만 전적으로 따르고 싶다면 이 값을 1.0(100%)으로 설정하세요.
    SYSTEM_INVEST_PER_STOCK: float = Field(default=0.2, gt=0.0, le=1.0)

    # [최대 보유 종목 수]
    # 포트폴리오에 담을 수 있는 최대 종목 개수입니다. (기본값: 10)
    SYSTEM_MAX_HOLDINGS: int = Field(default=10, gt=0)

    # [추가] 시스템 트레이딩 매수 대상에 ETF 포함 여부
    # 기본값: False (국내 주식만 대상으로 함)
    SYSTEM_INCLUDE_ETF: bool = False

    USE_MARKET_FILTER: bool = True          # 장세 판단 필터 사용 여부 (코스피 지수 추세 확인)
    MARKET_FILTER_MA: int = Field(default=50, gt=0)              # 시장 필터링 기준 단순이동평균선 (SMA, 일)
                                            #   KIS API는 약 50일치 데이터만 제공할 수 있습니다.
                                            #   60일 이상 설정 시 yfinance 데이터로 자동 대체됩니다.
    SYSTEM_MAX_CONSECUTIVE_ERRORS: int = Field(default=5, ge=1)  # [안전장치] 연속 에러 5회 발생 시 자동 중단
    SYSTEM_DAILY_LOSS_LIMIT: float = Field(default=10.0, ge=0.0)   # [안전장치] 일일 손실률 10.0% 도달 시 자동 중단 (0.0이면 미사용)
    SYSTEM_RISK_PER_TRADE: float = Field(default=5.0, ge=0.0)      # [안전장치] 1회 매매 시 계좌 대비 최대 허용 손실률 (%) (0.0이면 미사용)

    # [설정] 변동성 타겟팅 (Volatility Targeting)
    USE_VOLATILITY_TARGETING: bool = True   # 변동성 타겟팅 사용 여부
    TARGET_VOLATILITY: float = Field(default=0.30, gt=0.0)         # 목표 연간 변동성 (30%)
                                            # - 0.10 ~ 0.15: 보수적/안정적 (생존 우선, MDD 최소화)
                                            # - 0.15 ~ 0.20: 중립적 (시장 수익률 추구)
                                            # - 0.25 ~ 0.30: 적극적 (고수익 추구, 변동성 허용) -> 현재 설정
    VOLATILITY_SCALING_MAX: float = Field(default=2.0, gt=0.0)     # 최대 확대 배수 (2배) - 변동성이 낮을 때 포지션 확대 제한
    VOLATILITY_SCALING_MIN: float = Field(default=0.5, ge=0.0)     # 최소 축소 배수 (0.5배) - 변동성이 높을 때 최소 포지션 유지 (너무 적은 금액 매수 방지)

    # [추가] 슬리피지 비율 (Slippage Rate)
    # 매수/매도 주문 시 현재가 대비 불리한 가격으로 주문을 내어 체결 확률을 높이고,
    # 백테스팅 시 실제 체결 오차를 반영하기 위한 비율입니다.
    # (기본값: 0.002 = 0.2%, 0으로 설정 시 미사용)
    #
    # [케이스별 추천 설정]
    # 1. 대형주/ETF (유동성 풍부): 0.001 ~ 0.002 (0.1% ~ 0.2%) - *권장*
    # 2. 중소형주/코스닥 (일반): 0.003 ~ 0.005 (0.3% ~ 0.5%)
    # 3. 급등주/변동성 장세: 0.005 ~ 0.010 (0.5% ~ 1.0%) - 체결 최우선
    SLIPPAGE_RATE: float = Field(default=0.002, ge=0.0)

    SYSTEM_TRADING_START_TIME: str = "0920" # 거래 시작 시간 (HHMM) - 장 시작 후 안정화 대기
    SYSTEM_TRADING_END_TIME: str = "1510"   # 거래 종료 시간 (HHMM) - 장 마감 전 정리

    # [추가] 체결 감시 모니터링 주기 (초)
    # 1. 집중 감시 주기: 주문 발생 직후 체결 확인 주기 (기본값: 5초)
    CONCLUSION_CHECK_INTERVAL: int = Field(default=5, gt=0)

    # 2. 대기 모드 주기: 주문이 없는 평상시 확인 주기 (기본값: 300초 = 5분)
    # (0으로 설정하면 평상시에는 아예 확인하지 않습니다. 외부 HTS 주문 감지 불필요 시 0 권장)
    CONCLUSION_CHECK_IDLE_INTERVAL: int = Field(default=300, ge=0)

    # 3. 집중 감시 유지 시간: 주문 후 짧은 주기로 확인할 시간 (기본값: 60초)
    CONCLUSION_CHECK_ACTIVE_DURATION: int = Field(default=60, gt=0)

    # [추가] 미체결 주문 자동 취소 대기 시간 (초)
    # 지정가 주문 후 이 시간이 지나도 체결되지 않으면 주문을 취소하여 현금을 확보합니다. (기본값: 120초 = 2분)
    UNFILLED_ORDER_CANCEL_SECONDS: int = Field(default=120, gt=0)

    # [추가] 차트 데이터 메모리 캐시 TTL (분)
    # 일봉 데이터 조회 시 불필요한 네트워크 통신을 줄이고 시스템 전체의 응답 속도를 높입니다.
    # 당일 현재가는 실시간으로 갱신되며, 과거 데이터만 캐싱됩니다. 자정(날짜 변경선)이 지나면 자동 무효화됩니다.
    # (기본값: 180분, 0으로 설정 시 캐시 미사용)
    CHART_CACHE_TTL_MINUTES: int = Field(default=180, ge=0)

    # ==========================================================
    # [설정] 상관계수 필터링 (Pearson Correlation Filter)
    # ==========================================================
    # 보유 중인 종목과 주가 수익률의 상관관계가 높은(비슷하게 움직이는) 종목을
    # 매수 대상에서 제외하여 포트폴리오 다각화를 유도합니다.
    USE_CORRELATION_FILTER: bool = True
    CORRELATION_THRESHOLD: float = Field(default=0.7, ge=-1.0, le=1.0)        # 상관계수 임계값 (0.7 이상이면 유사한 흐름으로 판단)

    # ==========================================================
    # [설정] 종목 분석 및 상태 분류 임계값
    # ==========================================================
    ANALYSIS_THRESHOLDS: dict = {
        # [매수 기준 점수]
        # 기술적 분석 지표(이평선, SAR, RSI, ADX, CCI, OBV 등)를 종합하여 산출된 점수입니다.
        # 총점 만점은 지표 조합에 따라 다르지만 보통 10점 내외입니다.
        # 이 점수 이상일 때 '매수' 상태로 분류합니다. (기본값: 7.5)
        # - 값을 낮추면(예: 6~7) 매수 신호가 자주 발생하지만, 속임수(False Signal) 가능성이 높아집니다.
        # - 값을 높이면(예: 9) 신호 빈도는 줄어들지만, 확실한 상승 추세에서만 진입합니다.
        "BUY_SCORE": 7.5,

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

        # [추가] 역추세 (낙폭과대) 매수 설정 (Mean Reversion)
        # 하락장이나 급락 구간에서 지표가 과매도에 도달한 후 반등하는 시점을 포착합니다.
        "USE_MEAN_REVERSION": True,      # 역추세 매수 사용 여부
        "MR_RSI_MAX": 40.0,              # 진입 허용 최대 RSI (과매도 기준)
        "MR_DISPARITY_MAX": 90.0,        # 20일선 대비 이격도 (90% 이하일 때만 진입)
        "MR_VOL_STRENGTH": 120.0,        # 바닥권 반등 시 강한 매수세를 확인하기 위한 높은 체결강도 기준

        # [이격도 경고 기준]
        # 20일 이동평균선 대비 현재가 비율(%) 기준
        "DISPARITY_UPPER": 110,          # 단기 과열 (110% 이상)
        "DISPARITY_LOWER": 90,           # 과매도 (90% 이하)

        # [추가] 슈퍼 모멘텀 (RSI 유연화) 설정
        # 강력한 주도주(신고가 랠리)의 경우 매수 및 매도 RSI 허용치를 완화하여 추세를 길게 추종합니다.
        "SUPER_MOMENTUM_USE": True,       # 슈퍼 모멘텀 전략 사용 여부
        "SUPER_MOMENTUM_SCORE": 8.5,      # 발동 조건: 종합 점수 8.5점 이상 (및 52주 고점 90% 이상)
        "SUPER_MOMENTUM_W52_POS": 90.0,   # 발동 조건: 52주 고점 위치 90% 이상
        "SUPER_BUY_RSI_MAX": 75.0         # 발동 시 완화되는 매수 진입 최고 RSI (기본 65 -> 75)
    }

    # ==========================================================
    # [추가] 스코어링 모델 가중치 (Scoring Weights)
    # ==========================================================
    # 각 팩터별 배점을 설정합니다. (총점 10점 만점 기준)
    # ※ 주의: 이 값은 해당 팩터의 '만점'을 의미합니다.
    #         값을 변경하면 평가 항목 수가 바뀌는 것이 아니라,
    #         각 세부 항목의 배점이 비율에 맞춰 자동으로 스케일링됩니다.
    SCORING_WEIGHTS: dict = {
        "TREND": 4.0,       # 추세 팩터 (이평선, MACD, SAR)
        "MOMENTUM": 2.5,    # 모멘텀 팩터 (RSI, CCI)
        "STRENGTH": 1.5,    # 강도/수급 팩터 (ADX, OBV)
        "SYNERGY": 2.0      # 시너지 가산점 (지표 간 동조화)
    }

    # ==========================================================
    # [추가] 적응형 임계값 및 시장 국면 설정 (Adaptive Thresholds)
    # ==========================================================
    # 시장 국면(강세/약세/횡보)에 따라 매수 기준 점수를 동적으로 조절합니다.
    MARKET_REGIME_PARAMS: dict = {
        "USE_ADAPTIVE_THRESHOLD": True,  # 적응형 임계값 사용 여부
        "BULL_SCORE_ADJ": -1.0,          # 강세장: 기준 완화 (예: 8.0 -> 7.0)
        "BEAR_SCORE_ADJ": 1.0,           # 약세장: 기준 강화 (예: 8.0 -> 9.0)
        "SIDEWAYS_SCORE_ADJ": 0.0,       # 횡보장: 기준 유지
        "REGIME_MA_PERIOD": 20,          # 추세 판단용 지수이동평균선 (EMA, 일)
        "REGIME_ADX_THRESHOLD": 20       # 추세장/횡보장 구분 ADX 기준
    }

    # ==========================================================
    # [설정] 매도 전략 임계값 (Backtest & Trading)
    # ==========================================================
    SELL_STRATEGY: dict = {
        "STOP_LOSS_RATE": -7.0,             # [손절 기준] 진입가 대비 이 값 이하로 하락 시 즉시 매도
        "TAKE_PROFIT_RATE": 30.0,           # [익절 기준] 진입가 대비 이 값 이상 도달 시 즉시 매도
        "HALF_TAKE_PROFIT_USE": True,       # [반익절] 목표 익절률의 절반에 도달하면 50% 분할 매도 여부
        "TIME_STOP_USE": True,              # [시간 청산] 사용 여부
        "TIME_STOP_DAYS": 10,               # 보유 제한 기간 (일)
        "TIME_STOP_MIN_PROFIT_RATE": 3.0,   # 이 기간 내에 달성해야 할 최소 수익률 (%)
        "MR_GRACE_LOSS_RATE": -5.0,         # 역매수로 진입 시 유예기간 중 최대 허용 손실률
        "TAKE_PROFIT_RSI": 80,              # 과열 매도 기준 RSI
        "SUPER_TAKE_PROFIT_RSI": 85.0,      # 슈퍼 모멘텀 상태 시 상향 적용되는 매도 기준 RSI
        "SELL_SCORE": 5.0,                  # [추세 이탈 매도] 종합 점수가 이 값 미만으로 떨어지면 매도
        "TRAILING_STOP_ACTIVATION_RATE": 15.0, # [트레일링 스탑] 감시 시작 수익률
        "TRAILING_STOP_CALLBACK_RATE": 4.0,    # [트레일링 스탑] 최고가 대비 이탈률(매도 조건)
        "USE_ATR_STOP": True,               # ATR 기반 동적 손절 사용 여부
        "ATR_STOP_MULTIPLIER": 2.0,         # ATR 기반 손절 적용 배수
        "MAX_ATR_STOP_LOSS_RATE": -15.0     # 동적 손절의 최대 한계선
    }

    # ==========================================================
    # [설정] 기술적 분석 지표 파라미터
    # ==========================================================
    INDICATOR_PARAMS: dict = {
        "CHART_LOOKBACK_DAYS": 730,    # 차트 데이터 조회 기간 (일봉, 보통 2년치 조회)
        "SAR_AF_START": 0.02,          # Parabolic SAR (가속변수 시작값)
        "SAR_AF_STEP": 0.02,           # Parabolic SAR (가속변수 증가값)
        "SAR_AF_MAX": 0.2,             # Parabolic SAR (가속변수 최대값)
        "ADX_PERIOD": 14,              # ADX 계산 기간
        "CCI_WINDOW": 20,              # CCI 계산 기간
        "CCI_UPPER": 100,              # CCI 과매수 기준선
        "CCI_LOWER": -100,             # CCI 과매도 기준선
        "MACD_FAST": 12,               # MACD 단기선 (Fast EMA)
        "MACD_SLOW": 26,               # MACD 장기선 (Slow EMA)
        "MACD_SIGNAL": 9,              # MACD 시그널선 기간
        "OBV_MA_PERIOD": 5,            # OBV 이동평균 기간 (수급 추세 판단용)
        "RSI_PERIOD": 14,              # RSI 계산 기간
        "RSI_SIGNAL": 14,              # RSI 시그널(이동평균) 기간
        "RSI_UPPER": 70,               # RSI 과매수 기준선
        "RSI_MID": 50,                 # RSI 중간 기준선
        "RSI_LOWER": 30,               # RSI 과매도 기준선
        "ATR_PERIOD": 14               # ATR 계산 기간
    }

_settings_lock = threading.RLock()
settings = GlobalSettings()

# ==========================================================
# [설정] 텔레그램 설정 (Telegram Configuration)
# ==========================================================
# 보안을 위해 소스 코드에 직접 입력하기보다 환경 변수 사용을 권장합니다.
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")

# ==========================================================
# [설정] Google Gemini API 설정 (무료 대안)
# ==========================================================
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "")
GEMINI_MODEL = os.getenv("GEMINI_MODEL", "gemini-3.1-flash-lite-preview")
GEMINI_API_VERSION = os.getenv("GEMINI_API_VERSION", "v1beta") # 사용 가능한 버전: v1beta, v1

# ==========================================================
# [설정] 시장 지수 그룹 (Market Index Groups)
# ==========================================================
INDICES_GROUPS = {
    "1": {"name": "국내 지수 (Domestic Indices)", "indices": ["코스피", "코스피200", "코스닥", "코스닥150"]},
    "2": {"name": "글로벌 지수 (Global Indices)", "indices": ["나스닥 선물", "나스닥", "S&P500", "다우존스", "러셀2000", "Japan - 닛케이", "Taiwan - 대만가권", "Hong Kong - 항셍", "China - 상해종합", "Germany - 닥스40", "Europe - 스톡스50"]},
    "3": {"name": "섹터 및 주요 지표 (Sectors & Key Indicators)", "indices": ["London - Samsung GDR", "SOX (반도체)", "QGRD (스마트그리드)", "NBI (바이오)", "VIX (변동성)", "MSCI 신흥국", "하이일드 채권"]},
    "4": {"name": "금리 및 환율 (Rates & FX)", "indices": ["달러인덱스", "달러환율", "미국채 2년물 선물", "미국채 5년물 금리", "미국채 10년물 금리", "미국채 30년물 금리"]},
    "5": {"name": "원자재 (Commodities)", "indices": ["금", "은", "구리", "브랜트유", "WTI 원유", "가솔린 RBOB", "천연가스", "밀"]},
    "6": {"name": "암호화폐 (Cryptocurrency)", "indices": ["비트코인", "이더리움", "솔라나", "리플"]}
}
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
SYSTEM_TRADING_LOG_DIR = LOG_DIR # 시스템 트레이딩 로그 저장 디렉토리

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

# [추가] 커스텀 로그 핸들러 클래스 (커스텀 Namer 사용 시 자동 삭제 버그 수정)
class CustomTimedRotatingFileHandler(TimedRotatingFileHandler):
    """커스텀 namer(_log_namer)를 사용할 때 내장 getFilesToDelete가 파일을 인식하지 못해 삭제되지 않는 문제를 해결한 클래스"""
    def getFilesToDelete(self):
        dirName, baseName = os.path.split(self.baseFilename)
        fileNames = os.listdir(dirName)
        result = []
        
        root, ext = os.path.splitext(baseName)
        prefix = root + "_" # 예: mystock_
        date_pattern = re.compile(r'^\d{8}$') # %Y%m%d
        
        for fileName in fileNames:
            if fileName.startswith(prefix) and fileName.endswith(ext):
                date_part = fileName[len(prefix):-len(ext)]
                if date_pattern.match(date_part):
                    result.append(os.path.join(dirName, fileName))
        
        if len(result) < self.backupCount:
            result = []
        else:
            result.sort()
            # 지정된 보존 개수를 초과하는 가장 오래된 파일들을 반환
            result = result[:len(result) - self.backupCount]
        return result

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
        try:
            os.makedirs(LOG_DIR)
        except Exception as e:
            print(f"[Config] LOG_DIR creation error: {e}")

    # [수정] 오래된 로그 파일 정리 (과거 패턴 및 핸들러 버그로 누적된 파일 강제 정리)
    try:
        if LOG_RETENTION_DAYS > 0:
            cutoff_date = datetime.now().date() - timedelta(days=LOG_RETENTION_DAYS)
            for filename in os.listdir(LOG_DIR):
                file_path = os.path.join(LOG_DIR, filename)
                
                try:
                    # 1. 기존 system_trade_YYYY-MM-DD.log 정리
                    if filename.startswith("system_trade_") and filename.endswith(".log"):
                        date_part = filename.replace("system_trade_", "").replace(".log", "")
                        file_date = datetime.strptime(date_part, "%Y-%m-%d").date()
                        if file_date < cutoff_date:
                            os.remove(file_path)
                    
                    # 2. 누적된 mystock_YYYYMMDD.log 및 autotrade_YYYYMMDD.log 강제 정리
                    elif (filename.startswith("mystock_") or filename.startswith("autotrade_")) and filename.endswith(".log"):
                        match = re.search(r'_(\d{8})\.log$', filename)
                        if match:
                            date_part = match.group(1)
                            file_date = datetime.strptime(date_part, "%Y%m%d").date()
                            if file_date < cutoff_date:
                                os.remove(file_path)
                except Exception as e:
                    print(f"[Config] Old log file remove error ({filename}): {e}")
    except Exception as e:
        print(f"[Config] Old log cleanup process error: {e}")

    log_filename = "mystock.log" # [수정] 고정 파일명 사용
    log_filepath = os.path.join(LOG_DIR, log_filename)

    # [수정] CustomTimedRotatingFileHandler 적용 (백업 파일 삭제 버그 수정)
    file_handler = CustomTimedRotatingFileHandler(
        log_filepath, when='midnight', interval=1, backupCount=LOG_RETENTION_DAYS, encoding='utf-8'
    )
    file_handler.suffix = "%Y%m%d"
    file_handler.namer = _log_namer

    # [수정] 로그 포맷에 파일명과 라인 번호 추가
    file_handler.setFormatter(logging.Formatter('%(asctime)s.%(msecs)03d [%(levelname)s] [%(threadName)s] %(filename)s:%(lineno)d - %(message)s', datefmt='%H:%M:%S'))
    
    # 파일 로그 레벨 설정
    level_name = settings.FILE_DEBUG_LEVEL.upper()
    numeric_level = getattr(logging, level_name, logging.INFO)

    logging.basicConfig(level=numeric_level, handlers=[file_handler], force=True)
    
    # [추가] 외부 라이브러리 로그 레벨 조정 (노이즈 감소)
    for lib in ["httpcore", "httpx", "urllib3", "google", "google.genai", "mistune", "markdown_it", "yfinance", "peewee"]:
        logging.getLogger(lib).setLevel(logging.WARNING)
        
    logging.info(f"=== 로깅 시스템 설정 갱신 (현재 파일 로그 레벨: {level_name}) ===")

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
        try:
            os.makedirs(LOG_DIR)
        except Exception as e:
            print(f"[Config] Autotrade LOG_DIR creation error: {e}")

    log_filename = "autotrade.log"
    log_filepath = os.path.join(LOG_DIR, log_filename)
    
    # [수정] CustomTimedRotatingFileHandler 적용 (백업 파일 삭제 버그 수정)
    handler = CustomTimedRotatingFileHandler(
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
    global settings
    import json
    config_path = os.path.join(JSON_DIR, "dynamic_config.json")
    
    if os.path.exists(config_path):
        try:
            with open(config_path, 'r', encoding='utf-8') as f:
                data = json.load(f)
            
            with _settings_lock:
                current_dict = getattr(settings, 'model_dump', settings.dict)()
                
                for key in ["ANALYSIS_THRESHOLDS", "SELL_STRATEGY", "INDICATOR_PARAMS", "SCORING_WEIGHTS", "MARKET_REGIME_PARAMS"]:
                    if key in data:
                        current_dict[key].update(data[key])
                        del data[key]
                
                current_dict.update(data)
                settings = GlobalSettings(**current_dict)
        except Exception as e:
            print(f"[Config] 동적 설정 로드 실패: {e}")

# 모듈 로드 시 자동 실행
load_dynamic_config()

# 기존 코드 호환용 PEP 562 모듈 레벨 __getattr__ 래퍼
def __getattr__(name):
    if hasattr(settings, name):
        with _settings_lock:
            return getattr(settings, name)
    raise AttributeError(f"module '{__name__}' has no attribute '{name}'")
