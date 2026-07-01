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
    # [설정] 현재 활성화된 전략 프리셋 (Active Preset)
    # ==========================================================
    ACTIVE_PRESET: str = "default"

    # ==========================================================
    # [설정] 스크린 디버그 로그 레벨 설정 (OFF / DEBUG / TRACE)
    # ==========================================================
    # OFF   : 로그 출력 없음
    # ERROR : [ERROR] 시스템 에러 및 정지 로그 화면 출력
    # TRACE : [TRACE] 로그 화면 출력
    # DEBUG : [TRACE], [DEBUG] 및 [ERROR] 로그 화면 출력
    SCREEN_DEBUG_LEVEL: str = "ERROR"

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
    AUTO_DISCLOSURE_ALERT_USE: bool = True  # 관심종목 중대 공시 텔레그램 알림 (기본 ON)
    # [추가] 시장정지 알림: KIS는 서킷브레이커(CB)+VI, 토스는 VI만 (사이드카는 REST 미지원으로 제외)
    MARKET_HALT_ALERT_USE: bool = True      # 서킷브레이커/VI 시장정지 텔레그램 알림 (기본 ON)
    MARKET_HALT_CB_INTERVAL: int = 20       # CB 점검 주기(초) - KIS 전용
    MARKET_HALT_VI_INTERVAL: int = 60       # VI 점검 주기(초) - 보유+관심종목
    MARKET_HALT_VI_MAX_CODES: int = 40      # VI 점검 종목 수 상한 (라즈베리파이 부하 방어)
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
    # 전체 자산 대비 한 종목에 투자할 최대 비중입니다. (기본값: 0.2 = 20%)
    # - [정합성] SYSTEM_MAX_HOLDINGS(기본 5종목)와 곱했을 때 1.0(100%)을 넘지 않도록 0.2로 설정합니다.
    #   (0.3 × 5 = 1.5 → 앞순위 종목이 현금을 과다 소진해 뒷순위 슬롯이 굶는 문제를 방지하기 위함)
    #   균등비중 운용을 원하면 (1.0 / SYSTEM_MAX_HOLDINGS) 값으로 맞추세요.
    # - 리스크 기반 포지션 사이징(SYSTEM_RISK_PER_TRADE)과 함께 사용될 경우,
    #   두 방식 중 '더 적은 금액'이 최종 투자 금액으로 결정됩니다. (이중 안전장치: 몰빵 방지 + 리스크 관리)
    # - 만약 리스크 기반 사이징만 전적으로 따르고 싶다면 이 값을 1.0(100%)으로 설정하세요.
    SYSTEM_INVEST_PER_STOCK: float = Field(default=0.2, gt=0.0, le=1.0)

    # [최대 보유 종목 수]
    # 포트폴리오에 담을 수 있는 최대 종목 개수입니다. (기본값: 5)
    SYSTEM_MAX_HOLDINGS: int = Field(default=5, gt=0)

    # [추가] 자동매매 대상에 ETF 포함 여부
    # 기본값: False (관심종목 중 국내 주식만 시스템 트레이딩 대상으로 함)
    SYSTEM_INCLUDE_ETF: bool = False

    # [추가] 실시간 시세 WebSocket 사용 여부 (KIS mode 1/2)
    # 기본값: True. 켜면 보유/관심 종목 현재가·체결강도를 WebSocket push로 받아 REST/TPS 부담을 줄인다.
    # 미구독/끊김/비활성 시 자동으로 기존 REST 조회로 폴백한다. (토스 mode 3는 공식 미지원→REST 유지)
    USE_WEBSOCKET: bool = True

    USE_MARKET_FILTER: bool = True          # 장세 판단 필터 사용 여부 (코스피 지수 추세 확인)
    MARKET_FILTER_MA: int = Field(default=30, gt=0)              # 시장 필터링 기준 단순이동평균선 (SMA, 일)
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

    SYSTEM_TRADING_START_TIME: str = "0800" # 거래 시작 시간 (HHMM) - 장 시작 후 안정화 대기
    SYSTEM_TRADING_END_TIME: str = "2000"   # 거래 종료 시간 (HHMM) - 장 마감 전 정리

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
    # (기본값: 360분, 0으로 설정 시 캐시 미사용)
    CHART_CACHE_TTL_MINUTES: int = Field(default=360, ge=0)

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
        "BUY_SCORE": 7.0,

        # [상승 추세 기준 점수]
        # 매수 기준에는 미치지 못하지만, 상승 흐름이 있다고 판단하는 점수입니다. (기본값: 6)
        "RISE_SCORE": 6.0,

        # [관심(태동) 신호 최소 개수]
        # '상승'(추세 정렬 완성)에는 이르지 못했으나, 추세 전환 초기 신호가 포착되어
        # 모니터링/수동 스윙 대상으로 표시하는 '관심' 상태의 진입 조건입니다.
        # 아래 7가지 초기 상승신호(단기 골든크로스/MACD 히스토그램 개선/+DI 우위/RSI 50상향/
        # CCI 개선/수급 유입/60일선 근접) 중 이 개수 이상을 충족하고, 위험형 신호
        # (SAR 매도·RSI 과열침체·MACD 데드크로스·-DI 우위 등)가 없으면 '관심'으로 분류합니다.
        # - 값을 낮추면(예: 2) 더 이른 단계에서 많이 포착되나 속임수 신호가 늘어납니다.
        # - 값을 높이면(예: 4~5) 신호 빈도는 줄고 신뢰도는 높아집니다. (기본값: 3 / 0이면 미사용)
        "INTEREST_SIGNAL_MIN": 3,
        # [관심 60일선 근접 허용 비율]
        # 현재가가 60일선의 이 비율 이상이면(아직 60일선 아래여도) '60일선 돌파 시도'
        # 초기신호로 인정합니다. (기본값: 0.97 = 60일선의 97% 이상 근접)
        "INTEREST_MA60_NEAR": 0.97,

        # [RSI 과열 기준]
        # 매수 점수를 충족하더라도, RSI가 이 값 이상이면 '과열'로 판단하여 매수 추천에서 제외합니다.
        # (기본값: 70 - 추세추종 기조상 강한 추세는 RSI가 오래 과매수에 머무르므로 65→70으로 완화.
        #  과도한 고점매수는 슈퍼모멘텀 게이팅[SUPER_BUY_RSI_MAX 75]과 추세악화 감점으로 별도 방어)
        "BUY_RSI_MAX": 70,
        
        # [체결강도 기준]
        # 매수 시점의 체결강도가 이 값 이상이어야 진입합니다. (기본값: 100.0)
        # 100% 이상은 매수세가 매도세보다 강함을 의미합니다.
        "BUY_VOL_STRENGTH": 100.0,

        # [추가] 가짜 체결강도 방어 (호가창 매도잔량 비대칭성)
        # 매도 잔량이 매수 잔량보다 이 비율(배) 이상 많아야 진짜 상승 에너지로 판단합니다.
        # (기본값: 1.0배 / 0으로 설정 시 미사용)
        "BUY_ASK_BID_RATIO": 1.0,

        # [추가] 매도잔량비 자동 연동 옵션
        # 체결강도 기준이 100%를 초과/미달할 경우 그 비율만큼 매도잔량비 허들을 비례 조정합니다.
        # (단, 최저 설정값은 1.0으로 제한됨)
        "AUTO_ADJUST_ASK_BID_RATIO": True,

        # [추가] 역추세 (낙폭과대) 매수 설정 (Mean Reversion)
        # 하락장이나 급락 구간에서 지표가 과매도에 도달한 후 반등하는 시점을 포착합니다.
        # [기조] 역추세는 추세추종과 상반된 엣지이므로 성과 귀인을 명확히 하기 위해 기본 비활성화합니다.
        #        (별도 sleeve로 분리 검증하거나, 추세추종 검증 완료 후 설정에서 다시 켤 수 있습니다.)
        "USE_MEAN_REVERSION": False,     # 역추세 매수 사용 여부 (기본 비활성)
        "MR_RSI_MAX": 40.0,              # 진입 허용 최대 RSI (과매도 기준)
        "MR_DISPARITY_MAX": 90.0,        # 20일선 대비 이격도 (90% 이하일 때만 진입)
    "MR_VOL_STRENGTH": 120.0,        # 바닥권 매수세 확인을 위한 높은 체결강도 기준 (투매 방어)

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
        "TREND": 4.0,        # 추세 팩터 (이평선, MACD, SAR)
        "MOMENTUM": 2.5,     # 모멘텀 팩터 (RSI, CCI)
        "STRENGTH": 1.5,     # 강도/수급 팩터 (ADX, OBV)
        "SYNERGY": 2.0,      # 시너지 가산점 (지표 간 동조화)
    }

    # ==========================================================
    # [추가] 적응형 임계값 및 시장 국면 설정 (Adaptive Thresholds)
    # ==========================================================
    # 시장 국면(강세/약세/횡보)에 따라 매수 기준 점수를 동적으로 조절합니다.
    MARKET_REGIME_PARAMS: dict = {
        "USE_ADAPTIVE_THRESHOLD": True,  # 적응형 임계값 사용 여부
        "BULL_SCORE_ADJ": -0.5,          # 강세장: 기준 완화 (예: 8.0 -> 7.5)
        "BEAR_SCORE_ADJ": 0.5,           # 약세장: 기준 강화 (예: 8.0 -> 8.5)
        "SIDEWAYS_SCORE_ADJ": 0.0,       # 횡보장: 기준 유지
        "REGIME_MA_PERIOD": 20,          # 추세 판단용 지수이동평균선 (EMA, 일)
        "REGIME_ADX_THRESHOLD": 20       # 추세장/횡보장 구분 ADX 기준
    }

    # ==========================================================
    # [설정] 매도 전략 임계값 (Backtest & Trading)
    # ==========================================================
    SELL_STRATEGY: dict = {
        # [추세추종 기조] 청산의 주(主) 수단은 '트레일링 스탑'입니다. 고정 익절은 추세추종에서
        # 수익의 fat-tail(드물게 나오는 +100%~ 종목)을 잘라내므로, 보조적 상한선 역할로만 둡니다.
        "TAKE_PROFIT_RATE": 50.0,           # [익절 기준] 진입가 대비 이 값 이상 도달 시 즉시 매도 (상한선 역할로 상향, 0이면 미사용)
        "HALF_TAKE_PROFIT_USE": True,       # [반익절] 목표 익절률의 절반(=25%)에 도달하면 50% 분할 매도 여부
        "DEFENSIVE_HALF_SELL_USE": True,    # [방어적 반매도] 하락 반전(SAR 매도 + 5일선 이탈) 시 50% 수익실현
        "STOP_LOSS_RATE": -7.0,             # [손절 기준] 진입가 대비 이 값 이하로 하락 시 즉시 매도
        "USE_ATR_STOP": True,               # ATR 기반 동적 손절 사용 여부
        "ATR_STOP_MULTIPLIER": 2.0,         # ATR 기반 손절 적용 배수
        "MAX_ATR_STOP_LOSS_RATE": -15.0,    # 동적 손절의 최대 한계선
        "BREAK_EVEN_PROFIT_RATE": 5.0,      # [본전 청산] 최고 수익률이 이 값에 도달하면 손절선 상향 (ATR 사용 시 동적 연동)
        "BREAK_EVEN_STOP_RATE": 0.5,        # [본전 청산] 손절선을 이 값(+0.5%)으로 끌어올림
        "TIME_STOP_USE": True,              # [시간 청산] 사용 여부
        "TIME_STOP_DAYS": 20,               # 보유 제한 기간 (일) - 추세 전개에 충분한 시간 부여 (추세추종 기조로 10→20 완화)
        "TIME_STOP_MIN_PROFIT_RATE": 3.0,   # 이 기간 내에 달성해야 할 최소 수익률 (%)
        "MR_GRACE_LOSS_RATE": -7.0,         # 역매수로 진입 시 유예기간 중 최대 허용 손실률
        "SELL_SCORE": 5.0,                  # [추세 이탈 매도] 종합 점수가 이 값 미만으로 떨어지면 매도
        "TAKE_PROFIT_RSI": 85.0,            # 과열 매도 기준 RSI
        "SUPER_TAKE_PROFIT_RSI": 90.0,      # 슈퍼 모멘텀 상태 시 상향 적용되는 매도 기준 RSI (추세 장기 추종)
        "TRAILING_STOP_ACTIVATION_RATE": 10.0, # [트레일링 스탑] 감시 시작 수익률 (주청산 수단으로 일찍 활성화: 15→10)
        "TRAILING_STOP_CALLBACK_RATE": 4.0     # [트레일링 스탑] 최고가 대비 이탈률(매도 조건)
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
        "ATR_PERIOD": 14,              # ATR 계산 기간
        "TREND_PERIOD": 60,            # [추가] 상승/하락 추세선 기간 일수
        "BOX_PERIOD": 20,              # [추가] 박스권 설정 기간 일수
        "BOX_VALUE_AREA_PCT": 50.0,    # [추가] 박스권 매물대 %
        "MOMENTUM_LOOKBACK": 126,      # [추가] 가격 모멘텀(절대 모멘텀) 산정 룩백 기간 (약 6개월=126거래일)
        "MOMENTUM_W52_NEAR": 80,       # [추가] 가격 모멘텀 가점 기준 52주 위치(%) (신고가 근접도)
        "EMA_SHORT": 5,                # [추가] 단기 이평선(EMA) 기간 (Early 추세용)
        "VOLUME_MA_PERIOD": 20,        # [추가] 거래량 이동평균 기간 (Volume Spike용)
        "VOLUME_SPIKE_RATIO": 2.0,     # [추가] 거래량 폭발 기준 (200% 이상)
        "SCORE_RSI_MID": 50,           # 스코어링 강세 기준 RSI
        "SCORE_RSI_STRONG": 60,        # 스코어링 모멘텀 확장 기준 RSI
        "SCORE_RSI_OVERHEAT": 80,      # [추가] 모멘텀 확장 가점 동결 기준 RSI (과열 구간 고점매수 방지)
        "SCORE_RSI_REBOUND": 40,       # 스코어링 상승 여력 구간 하한 RSI (MR_RSI_MAX=40과 동일 경계: 40 이상이면 여력 점수, 40 미만은 역매수 영역)
        "SCORE_ADX_MIN": 20,           # 스코어링 추세 기준 ADX
        "SCORE_CCI_STRONG": 0,         # 스코어링 추세 기준 CCI
        "SCORE_CCI_MOMENTUM": 50       # 스코어링 모멘텀 심화 기준 CCI
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
GEMINI_MODEL = os.getenv("GEMINI_MODEL", "gemini-3.1-flash-lite")
GEMINI_API_VERSION = os.getenv("GEMINI_API_VERSION", "v1beta") # 사용 가능한 버전: v1beta, v1

# ==========================================================
# [설정] OpenDART (전자공시) API 설정 - 국내 배당/실적 조회용
# ==========================================================
DART_API_KEY = os.getenv("DART_API_KEY", "")  # https://opendart.fss.or.kr 발급 (무료, 40자리)

# ==========================================================
# [설정] 시장 지수 그룹 (Market Index Groups)
# ==========================================================
INDICES_GROUPS = {
    "1": {"name": "국내 지수 (Domestic Indices)", "indices": ["코스피", "코스피200", "코스닥", "코스닥150"]},
    "2": {"name": "미국 지수 (US Indices)", "indices": ["나스닥 선물", "나스닥", "S&P500", "다우존스", "러셀2000"]},
    "3": {"name": "섹터 및 지표 (Sectors & Indicators)", "indices": ["London - Samsung GDR", "SOX (반도체)", "DRG (제약)", "NBI (바이오)", "BKX (은행)", "DJT (운송)", "DJU (유틸/전력)", "XAL (항공)", "XOI (에너지)", "HUI (금광)", "VIX (변동성)", "MSCI 전세계", "MSCI 선진국", "MSCI 신흥국"]},
    "4": {"name": "금리 및 환율 (Rates & FX)", "indices": ["달러인덱스", "달러환율", "미국채 5년물 금리", "미국채 10년물 금리", "미국채 30년물 금리"]},
    "5": {"name": "글로벌 지수 (Global Indices)", "indices": ["Japan - 닛케이", "Taiwan - 대만가권", "Hong Kong - 항셍", "China - 상해종합", "UK - FTSE 100", "France - CAC 40", "Germany - DAX 40", "Europe - STOXX 50"]},
    "6": {"name": "원자재 (Commodities)", "indices": ["금", "은", "구리", "브랜트유", "WTI 원유", "가솔린 RBOB", "천연가스", "밀"]},
    "7": {"name": "암호화폐 (Cryptocurrency)", "indices": ["비트코인", "이더리움", "솔라나", "리플"]}
}
# ==========================================================
# [설정] 트랜잭션 속도 제한 (Rate Limiting)
# ==========================================================
SIM_TX_PER_SECOND = 2     # 모의투자 서버 최대 TPS: 2
REAL_TX_PER_SECOND = 20   # 실전투자 서버 최대 TPS: 20 (KIS 명목 한도)

# [추가] 내부 TPS 안전계수 (Safety Margin)
# 위 *_TX_PER_SECOND는 운영자가 선언하는 '명목 한도'로 그대로 두고,
# 스로틀 로직 내부에서만 이 비율을 곱한 '실효 한도'로 운행한다.
# 이유: 명목 한도(예: 20)에 정확히 붙여 운행하면 클라이언트 1.1초 윈도우와
#       KIS 서버 1초 카운터의 경계가 충돌하여 EGW00201(초당 거래건수 초과)이
#       상시 발생한다. 약 10% 마진(실효 18 TPS)이면 20건이 ~1.06초에 분산되어
#       서버 1초 윈도우 기준 한도 미만으로 유지되며, 재시도 폭주가 사라져
#       오히려 실효 처리량이 안정된다. (1.0 = 마진 없음 / 값을 낮출수록 보수적)
REAL_TPS_SAFETY = 0.9     # 실전투자 실효 한도 '시작값' = 20 * 0.9 = 18 TPS (#7 적응형의 출발점)

# [#7] 적응형 동적 TPS 마진 (AIMD: 가산 증가·곱셈 감소)
# - 시작은 REAL_TPS_SAFETY(0.9 마진=18 TPS). 성공이 누적되면 마진을 조금씩 줄여 실효 TPS를 점진
#   상향(MAX까지)하고, EGW00201(초과)이 나면 즉시 곱셈 감소로 물러나 적정 TPS로 자가 수렴한다.
REAL_TPS_SAFETY_MIN = 0.85   # 백오프 하한 (= 20 * 0.85 = 17 TPS). 과도한 하강 방지
REAL_TPS_SAFETY_MAX = 0.98   # 상향 상한 (= 20 * 0.98 = 19.6 TPS). 명목 한도 직전까지만 도전
TPS_ADAPT_STEP = 0.05        # 성공 1건당 실효 TPS 가산 증가폭 (TPS). 작게 둬 완만히 상승
TPS_ADAPT_BACKOFF = 0.9      # EGW00201 발생 시 실효 TPS 곱셈 감소율 (×0.9)

def analysis_max_workers():
    """[#9] 종목 단위 외부 병렬 분석(KIS 경로)의 워커 수를 TPS에 정합시킨다.

    게이트(실효 TPS)보다 훨씬 많은 워커는 게이트 앞에서 대기만 하며 라즈베리파이 메모리를
    낭비한다. 동시에 in-flight 시킬 종목 수를 TPS에 비례해 제한한다.
    (모의 2 TPS→2, 실전 20 TPS→약 5). 토스는 별도 한도라 보수적으로 4를 쓴다.
    """
    try:
        if session.is_toss:
            return 4
        if session.is_simulation:
            return 2
        return max(3, min(6, int(REAL_TX_PER_SECOND / 4)))
    except Exception:
        return 4

# [추가] 토스증권 Open API 기본 호출 한도 (그룹별 토큰버킷이나, 보수적 단일 간격으로 운영)
# 토스 한도는 10/s → 최대치(10)로 운영한다.
TOSS_TX_PER_SECOND = 10

# ==========================================================
# [설정] 시세 개요 백그라운드 예열 (Overview Warmer)
# ==========================================================
# 해외 종목 시세/시장 지수를 백그라운드에서 주기적으로 마이크로 캐시에 채워,
# '시장 지수 조회'/'종목 시세 분석'(해외) 진입 시 임계경로 조회 없이 즉시 표시되게 한다.
#  - 국내 종목 현재가/체결강도는 KRX/NXT 무관하게 매 실행마다 라이브로 조회하므로 예열하지 않는다.
#  - 모의투자(2 TPS)는 예열이 시스템 트레이딩 호출과 TPS를 다투므로 기본 비활성화한다(실전만 ON).
OVERVIEW_WARM_ENABLED = True          # 백그라운드 예열 사용 여부 (실전 계좌에서만 자동 동작)
OVERVIEW_WARM_INTERVAL_SEC = 15       # 예열 주기(초). 해외 시세/지수 fast_info 예열에 사용
OVERVIEW_WARM_ON_SIMULATION = False   # 모의투자에서도 예열할지 (기본 OFF: 시스템 트레이딩 보호)

# ==========================================================
# [설정] 실시간 시세 WebSocket (KIS 실전/모의)
# ==========================================================
# KIS는 단일 WS 연결당 41건(종목×TR) 등록 제한 + approval_key당 동시 연결 1개 제한이 있다.
# 보유종목 우선 구독 + 관심종목 로테이션으로 운용하고, 미구독/끊김 시 기존 REST로 자동 폴백한다.
# 토스(mode 3)는 공식 WS 미지원이라 REST 폴링을 유지한다(추후 WS 공개 시 어댑터 교체).
# (USE_WEBSOCKET 사용 여부 토글은 메뉴 0에서 변경 가능하도록 GlobalSettings(Pydantic) 필드로 둔다)
WS_MAX_REGISTRATIONS = 41         # KIS 단일 연결 등록 한도(종목×TR)
# 호가(H0STASP0) 구독 여부. SubscriptionManager.plan()이 현재가(H0STCNT0)를 먼저 등록해 종목
# 커버리지를 최대(=한도)로 확보한 뒤, 남는 등록 슬롯에만 호가를 best-effort로 얹으므로 현재가
# 커버리지는 절반으로 줄지 않는다. WS 호가 캐시는 api.get_ask_bid_ratio()의 수급 게이트가 소비하며,
# 이를 켜면 매수후보/매도조건 분석에서 종목당 호가 REST 1콜을 절감한다(특히 모의투자 2 TPS에서 체감 큼).
WS_SUBSCRIBE_ORDERBOOK = True
WS_DATA_TTL_SEC = 3.0             # WS 캐시 신선도(초): 이 시간 이내면 REST 대신 WS 값을 사용
WS_ROTATE_INTERVAL_SEC = 30       # 41건 초과분(관심종목) 구독 로테이션 주기(초)
WS_RECONNECT_BACKOFF_SEC = 5      # 연결 실패/끊김 시 재연결 대기(초)

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

# [추가] 사용자가 커스텀한 전략 프리셋 값을 저장할 파일 경로
PRESETS_FILE = os.path.join(JSON_DIR, "presets.json")

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
# [추가] 모의투자(VTS) 서버는 실전 대비 응답이 느려 시세/차트의 짧은 timeout(2~3초)에서
#  ReadTimeout이 빈발한다. 모의투자일 때는 호출별 timeout을 이 값 이상으로 보장한다.
SIM_MIN_QUOTE_TIMEOUT = 5  # 모의투자 시 최소 보장 timeout (초)
RETRY_DELAY_SERVER = 1.0  # 서버 에러 발생 시 재시도 대기 시간 (초) (기본값 1.0)

# ==========================================================
# [추가] API 요청 중 연결 끊김(RemoteDisconnected 등) 발생 시 재시도 횟수
# 0으로 설정하면 재시도하지 않으며, 1로 설정하면 실패 시 1회 재시도합니다.
# 재시도 시 RETRY_DELAY_SERVER를 이용한 백오프 방식으로 대기 후 재시도합니다.
# ==========================================================
MAX_RETRIES = 4

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

# [추가] 토스증권 Open API 서버 URL
TOSS_URL = "https://openapi.tossinvest.com"

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
    except Exception:
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
    # [수정] removeHandler만 하면 FileHandler가 파일을 연 채 분리되어 ResourceWarning
    #        (unclosed file)이 발생하므로, 제거 전에 close()로 파일 핸들을 확실히 닫는다.
    if logger.hasHandlers():
        for h in list(logger.handlers):
            try:
                h.close()
            except Exception:
                pass
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

                # [마이그레이션] 구버전 가격모멘텀(MOMENTUM_PRICE) 가중치는 제거하고 추세(TREND)로 흡수
                #   (총점 10점 유지: 추세4/모멘텀2.5/강도1.5/시너지2.0 체계로 복귀)
                sw = current_dict.get("SCORING_WEIGHTS", {})
                if "MOMENTUM_PRICE" in sw:
                    sw["TREND"] = round(sw.get("TREND", 4.0) + sw.pop("MOMENTUM_PRICE", 0.0), 2)

                current_dict.update(data)
                settings = GlobalSettings(**current_dict)
        except Exception as e:
            print(f"[Config] 동적 설정 로드 실패: {e}")

# [추가] 커스텀 변경된 설정 내역 추출
def get_custom_settings():
    default_settings = getattr(GlobalSettings(), 'model_dump', GlobalSettings().dict)()
    current_settings = getattr(settings, 'model_dump', settings.dict)()
    
    changed_items = {}
    for k, v in current_settings.items():
        if k in ["ACTIVE_PRESET"]: continue
        default_v = default_settings.get(k)
        if isinstance(v, dict) and isinstance(default_v, dict):
            for sub_k, sub_v in v.items():
                sub_default = default_v.get(sub_k)
                if sub_v != sub_default:
                    changed_items[f"{k}.{sub_k}"] = {"parent": k, "key": sub_k, "default": sub_default, "current": sub_v}
        else:
            if v != default_v:
                changed_items[k] = {"parent": None, "key": k, "default": default_v, "current": v}
    return changed_items

# [추가] 지정된 커스텀 설정을 초기화하고 다시 로드
def reset_custom_settings(keys_to_reset):
    global settings
    import json
    config_path = os.path.join(JSON_DIR, "dynamic_config.json")
    
    if not os.path.exists(config_path):
        return

    with _settings_lock:
        try:
            with open(config_path, 'r', encoding='utf-8') as f:
                data = json.load(f)
                
            for key_path in keys_to_reset:
                if '.' in key_path:
                    parent, child = key_path.split('.', 1)
                    if parent in data and child in data[parent]:
                        del data[parent][child]
                        if not data[parent]:
                            del data[parent]
                else:
                    if key_path in data:
                        del data[key_path]

            with open(config_path, 'w', encoding='utf-8') as f:
                json.dump(data, f, indent=4, ensure_ascii=False)
            
            # 초기 상태를 베이스로 새 설정 덮어씌우기
            settings = GlobalSettings()
            load_dynamic_config()
        except Exception as e:
            print(f"[Config] 설정 초기화 중 오류: {e}")

# [추가] 모든 커스텀 설정 삭제 및 시스템 기본값으로 완전 초기화
def reset_all_settings():
    global settings
    import os
    config_path = os.path.join(JSON_DIR, "dynamic_config.json")
    if os.path.exists(config_path):
        try: os.remove(config_path)
        except Exception: pass

    with _settings_lock:
        settings = GlobalSettings()
        
        # [추가] 파이썬 클래스 딕셔너리의 메모리 참조 오염을 방지하기 위해 
        # 초기화 시 하드코딩된 순수 기본값으로 강제 복원
        settings.ANALYSIS_THRESHOLDS = {
            "BUY_SCORE": 7.0, "RISE_SCORE": 6.0, "INTEREST_SIGNAL_MIN": 3, "INTEREST_MA60_NEAR": 0.97,
            "BUY_RSI_MAX": 70, "BUY_VOL_STRENGTH": 100.0,
            "BUY_ASK_BID_RATIO": 1.0, "AUTO_ADJUST_ASK_BID_RATIO": True, "USE_MEAN_REVERSION": False,
            "MR_RSI_MAX": 40.0, "MR_DISPARITY_MAX": 90.0, "MR_VOL_STRENGTH": 120.0,
            "DISPARITY_UPPER": 110, "DISPARITY_LOWER": 90, "SUPER_MOMENTUM_USE": True,
            "SUPER_MOMENTUM_SCORE": 8.5, "SUPER_MOMENTUM_W52_POS": 90.0, "SUPER_BUY_RSI_MAX": 75.0
        }
        settings.SELL_STRATEGY = {
            "TAKE_PROFIT_RATE": 50.0, "HALF_TAKE_PROFIT_USE": True, "DEFENSIVE_HALF_SELL_USE": True,
            "STOP_LOSS_RATE": -7.0, "USE_ATR_STOP": True, "ATR_STOP_MULTIPLIER": 2.0,
            "MAX_ATR_STOP_LOSS_RATE": -15.0, "BREAK_EVEN_PROFIT_RATE": 5.0, "BREAK_EVEN_STOP_RATE": 0.5,
            "TIME_STOP_USE": True, "TIME_STOP_DAYS": 20, "TIME_STOP_MIN_PROFIT_RATE": 3.0,
            "MR_GRACE_LOSS_RATE": -7.0, "SELL_SCORE": 5.0, "TAKE_PROFIT_RSI": 85.0,
            "SUPER_TAKE_PROFIT_RSI": 90.0, "TRAILING_STOP_ACTIVATION_RATE": 10.0, "TRAILING_STOP_CALLBACK_RATE": 4.0
        }
        settings.SCORING_WEIGHTS = {
            "TREND": 4.0, "MOMENTUM": 2.5, "STRENGTH": 1.5, "SYNERGY": 2.0
        }
        settings.MARKET_REGIME_PARAMS = {
            "USE_ADAPTIVE_THRESHOLD": True, "BULL_SCORE_ADJ": -0.5, "BEAR_SCORE_ADJ": 0.5,
            "SIDEWAYS_SCORE_ADJ": 0.0, "REGIME_MA_PERIOD": 20, "REGIME_ADX_THRESHOLD": 20
        }
        settings.INDICATOR_PARAMS = {
            "CHART_LOOKBACK_DAYS": 730, "SAR_AF_START": 0.02, "SAR_AF_STEP": 0.02, "SAR_AF_MAX": 0.2,
            "ADX_PERIOD": 14, "CCI_WINDOW": 20, "CCI_UPPER": 100, "CCI_LOWER": -100,
            "MACD_FAST": 12, "MACD_SLOW": 26, "MACD_SIGNAL": 9, "OBV_MA_PERIOD": 5,
            "RSI_PERIOD": 14, "RSI_SIGNAL": 14, "RSI_UPPER": 70, "RSI_MID": 50, "RSI_LOWER": 30,
            "ATR_PERIOD": 14, "TREND_PERIOD": 60, "BOX_PERIOD": 20, "BOX_VALUE_AREA_PCT": 50.0, "EMA_SHORT": 5, "VOLUME_MA_PERIOD": 20, "VOLUME_SPIKE_RATIO": 2.0,
            "SCORE_RSI_MID": 50, "SCORE_RSI_STRONG": 60, "SCORE_RSI_OVERHEAT": 80, "SCORE_RSI_REBOUND": 40,
            "SCORE_ADX_MIN": 20, "SCORE_CCI_STRONG": 0, "SCORE_CCI_MOMENTUM": 50
        }
        
        import sys
        current_module = sys.modules[__name__]
        settings_keys = getattr(settings, 'model_dump', settings.dict)().keys()
        
        for key in settings_keys:
            if key in current_module.__dict__:
                del current_module.__dict__[key]

# [추가] 설정 항목에 대한 한글 설명 매핑 (UI 출력용)
CONFIG_DESCRIPTIONS = {
    "ACTIVE_PRESET": "현재 활성화된 전략 프리셋",
    "SCREEN_DEBUG_LEVEL": "터미널 디버그 출력 레벨",
    "CLEAR_SCREEN_ON_MENU": "메뉴 이동 시 터미널 클리어",
    "FILE_DEBUG_LEVEL": "로그 파일 저장 레벨",
    "ENABLE_TELEGRAM": "텔레그램 알림 기능 활성화 여부",
    "AUTO_MORNING_BRIEFING_USE": "매일 글로벌 매크로 시황 전송 여부",
    "AUTO_MORNING_BRIEFING_TIME": "장전 AI 브리핑 발송 시각",
    "MARKET_HALT_ALERT_USE": "서킷브레이커/VI 시장정지 알림 여부 (KIS:CB+VI, 토스:VI)",
    "MARKET_HALT_CB_INTERVAL": "서킷브레이커 점검 주기(초)",
    "MARKET_HALT_VI_INTERVAL": "VI 점검 주기(초)",
    "MARKET_HALT_VI_MAX_CODES": "VI 점검 종목 수 상한",
    "TELEGRAM_INSTANCE_NAME": "알림 메시지 머리말 (인스턴스 식별)",
    "TELEGRAM_POLLING_TIMEOUT": "봇 명령어 수신 대기 시간",
    "SYSTEM_TRADING_INTERVAL": "자동매매 루프 실행 간격 (초)",
    "SYSTEM_INVEST_PER_STOCK": "전체 자산 대비 한 종목 투자 비율",
    "SYSTEM_MAX_HOLDINGS": "포트폴리오에 담을 수 있는 최대 종목 수",
    "SYSTEM_INCLUDE_ETF": "자동매매 대상에 ETF 포함 여부",
    "USE_MARKET_FILTER": "지수 하락 시 신규 매수 보류 여부",
    "MARKET_FILTER_MA": "시장 추세 판단용 단순이동평균선 (일)",
    "SYSTEM_MAX_CONSECUTIVE_ERRORS": "시스템 중단 연속 에러 임계값",
    "SYSTEM_DAILY_LOSS_LIMIT": "자산 보호를 위한 비상 정지 기준 손실률",
    "SYSTEM_RISK_PER_TRADE": "1회 매매 시 계좌 대비 최대 허용 손실률",
    "USE_VOLATILITY_TARGETING": "변동성(ATR) 타겟팅 비중 조절 사용 여부",
    "TARGET_VOLATILITY": "목표 연간 변동성",
    "VOLATILITY_SCALING_MAX": "변동성 비중 확대 최대 배수",
    "VOLATILITY_SCALING_MIN": "변동성 비중 축소 최소 배수",
    "SLIPPAGE_RATE": "주문가 보정 및 백테스트 슬리피지 비율",
    "SYSTEM_TRADING_START_TIME": "거래 시작 시간 (HHMM)",
    "SYSTEM_TRADING_END_TIME": "거래 종료 시간 (HHMM)",
    "CONCLUSION_CHECK_INTERVAL": "주문 직후 집중 체결 확인 주기 (초)",
    "CONCLUSION_CHECK_IDLE_INTERVAL": "평상시 체결 확인 주기 (초)",
    "CONCLUSION_CHECK_ACTIVE_DURATION": "주문 후 짧은 주기 모니터링 시간 (초)",
    "UNFILLED_ORDER_CANCEL_SECONDS": "미체결 주문 자동 취소 대기 시간 (초)",
    "CHART_CACHE_TTL_MINUTES": "차트 데이터 메모리 캐시 시간 (분)",
    "USE_CORRELATION_FILTER": "상관계수 필터링 (중복 테마 매수 방지) 사용",
    "CORRELATION_THRESHOLD": "동조화 판단 상관계수 임계값",
    "BUY_SCORE": "매수 기준 종합 점수",
    "RISE_SCORE": "상승 추세 판단 기준 점수",
    "INTEREST_SIGNAL_MIN": "관심(태동) 분류를 위한 최소 초기신호 개수",
    "INTEREST_MA60_NEAR": "관심 60일선 근접 인정 비율(0.97=97% 근접)",
    "BUY_RSI_MAX": "매수 진입 허용 최고 RSI (과열 방지)",
    "BUY_VOL_STRENGTH": "매수 최소 체결강도 기준",
    "BUY_ASK_BID_RATIO": "매수 최소 매도잔량 비대칭성 비율",
    "AUTO_ADJUST_ASK_BID_RATIO": "매도잔량비 체결강도 비례 자동 조정 여부",
    "USE_MEAN_REVERSION": "낙폭과대 역추세 매수 사용 여부",
    "MR_RSI_MAX": "역추세 매수 최대 허용 RSI",
    "MR_DISPARITY_MAX": "역추세 매수 20일선 대비 최대 이격도",
    "MR_VOL_STRENGTH": "역추세 매수 최소 체결강도 기준",
    "DISPARITY_UPPER": "20일선 대비 단기 과열 이격도 상한",
    "DISPARITY_LOWER": "20일선 대비 과매도 이격도 하한",
    "SUPER_MOMENTUM_USE": "주도주 슈퍼 모멘텀 전략 사용 여부",
    "SUPER_MOMENTUM_SCORE": "슈퍼 모멘텀 발동 최소 점수",
    "SUPER_MOMENTUM_W52_POS": "슈퍼 모멘텀 발동 최소 52주 고점 위치",
    "SUPER_BUY_RSI_MAX": "슈퍼 모멘텀 발동 시 완화되는 매수 진입 RSI",
    "SUPER_TAKE_PROFIT_RSI": "슈퍼 모멘텀 발동 시 상향되는 매도 RSI",
    "TREND_PERIOD": "상승/하락 추세선 기간 일수",
    "BOX_PERIOD": "박스권 설정 기간 일수",
    "BOX_VALUE_AREA_PCT": "박스권 매물대 % (집중도)",
    "TREND": "추세 팩터 가중치 (이평선, MACD, SAR)",
    "MOMENTUM": "모멘텀 팩터 가중치 (RSI, CCI, DMI)",
    "STRENGTH": "수급/강도 팩터 가중치 (ADX, OBV)",
    "SYNERGY": "시너지 가산점 가중치",
    "USE_ADAPTIVE_THRESHOLD": "시장 국면에 따른 매수 점수 동적 조절 여부",
    "BULL_SCORE_ADJ": "강세장 점수 보정값",
    "BEAR_SCORE_ADJ": "약세장 점수 보정값",
    "SIDEWAYS_SCORE_ADJ": "횡보장 점수 보정값",
    "REGIME_MA_PERIOD": "시장 국면 판단용 EMA 기간",
    "REGIME_ADX_THRESHOLD": "추세장/횡보장 구분 ADX 기준",
    "TAKE_PROFIT_RATE": "목표 익절 수익률",
    "HALF_TAKE_PROFIT_USE": "목표 익절률의 절반 도달 시 50% 분할 매도 여부",
    "DEFENSIVE_HALF_SELL_USE": "하락 반전 시 50% 방어적 수익실현 여부",
    "STOP_LOSS_RATE": "고정 손절 수익률",
    "USE_ATR_STOP": "ATR 기반 동적 손절 사용 여부",
    "ATR_STOP_MULTIPLIER": "ATR 기반 손절 배수",
    "MAX_ATR_STOP_LOSS_RATE": "동적 손절 최대 허용 한계선",
    "BREAK_EVEN_PROFIT_RATE": "본전 청산 발동을 위한 최고 수익률",
    "BREAK_EVEN_STOP_RATE": "본전 청산 시 끌어올릴 새로운 손절률",
    "TIME_STOP_USE": "장기 보유 종목 시간 청산 여부",
    "TIME_STOP_DAYS": "시간 청산 제한 보유 일수",
    "TIME_STOP_MIN_PROFIT_RATE": "시간 청산 회피 최소 달성 수익률",
    "MR_GRACE_LOSS_RATE": "역매수 유예기간 중 최대 허용 손실률",
    "SELL_SCORE": "추세 이탈 매도 기준 점수",
    "TAKE_PROFIT_RSI": "과열 매도 기준 RSI",
    "TRAILING_STOP_ACTIVATION_RATE": "트레일링 스탑 감시 시작 수익률",
    "TRAILING_STOP_CALLBACK_RATE": "최고가 대비 트레일링 스탑 매도 이탈률",
    "CHART_LOOKBACK_DAYS": "차트 데이터 조회 기간 (일)",
    "SAR_AF_START": "파라볼릭 SAR 가속변수 시작값",
    "SAR_AF_STEP": "파라볼릭 SAR 가속변수 증가값",
    "SAR_AF_MAX": "파라볼릭 SAR 가속변수 최대값",
    "ADX_PERIOD": "ADX 계산 기간",
    "CCI_WINDOW": "CCI 계산 기간",
    "CCI_UPPER": "CCI 과매수 기준선",
    "CCI_LOWER": "CCI 과매도 기준선",
    "MACD_FAST": "MACD 단기선 (Fast EMA)",
    "MACD_SLOW": "MACD 장기선 (Slow EMA)",
    "MACD_SIGNAL": "MACD 시그널선 기간",
    "OBV_MA_PERIOD": "OBV 이동평균 기간",
    "RSI_PERIOD": "RSI 계산 기간",
    "RSI_SIGNAL": "RSI 시그널(이동평균) 기간",
    "RSI_UPPER": "RSI 과매수 기준선",
    "RSI_MID": "RSI 중간 기준선",
    "RSI_LOWER": "RSI 과매도 기준선",
    "ATR_PERIOD": "ATR 계산 기간",
    "EMA_SHORT": "단기 이평선(EMA) 기간",
    "VOLUME_MA_PERIOD": "거래량 이동평균 기간",
    "VOLUME_SPIKE_RATIO": "거래량 폭발 기준 비율",
    "SCORE_RSI_MID": "스코어링 강세 기준 RSI",
    "SCORE_RSI_STRONG": "스코어링 모멘텀 확장 기준 RSI",
    "SCORE_RSI_REBOUND": "스코어링 상승 여력 구간 하한 RSI (초기 매수 진입 여지 구간)",
    "SCORE_ADX_MIN": "스코어링 추세 기준 ADX",
    "SCORE_CCI_STRONG": "스코어링 추세 기준 CCI",
    "SCORE_CCI_MOMENTUM": "스코어링 모멘텀 심화 기준 CCI"
}

# 모듈 로드 시 자동 실행
load_dynamic_config()

# 기존 코드 호환용 PEP 562 모듈 레벨 __getattr__ 래퍼
def __getattr__(name):
    if hasattr(settings, name):
        with _settings_lock:
            return getattr(settings, name)
    raise AttributeError(f"module '{__name__}' has no attribute '{name}'")
