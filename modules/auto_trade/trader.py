# modules/auto_trade/trader.py
"""AutoTrader: 시스템 트레이딩 메인 루프 (분석→매수/매도→리포트)

기존 modules/auto_trade.py 에서 분해. 외부 인터페이스는 패키지(__init__)가 재수출한다.
"""
import threading
import concurrent.futures
import logging
import time
import requests
import json
from core import jsonio
import os
import sqlite3 # [추가] DB 직접 접근용
from datetime import datetime, timedelta
from collections import Counter
from rich.prompt import Prompt
from rich.markup import escape
from rich.table import Table
from rich import box
from rich.progress import Progress, SpinnerColumn, BarColumn, TextColumn, TimeRemainingColumn
import config
from core import context # [추가]
import api
from core import utils
from core import indicators
from modules import analysis, account # [수정] account 모듈 재사용
import math # [추가] math 모듈
from modules import db_manager # [추가] DB 매니저
from core import trading_cost # [추가] 거래비용 단일 계산
from modules import holdings_backfill # [추가] 기동 시 외부 체결 동기화
from modules import chart # [추가] 차트 모듈
from modules import instance_lock # [추가] 자동매매 단일 실행 보장
from modules import telegram_notify # [추가] 알림 발신 상태 조회
import re # [추가] 정규식 모듈
import pandas as pd

from modules.auto_trade.engine import (ANCHOR_RESTORE_INTERVAL_SEC, DefaultStrategy,
                                       NO_SELLABLE_ALERT_CYCLES, OrderManager,
                                       RiskManager, STUCK_PENDING_ALERT_CYCLES,
                                       UNMANAGED_ANALYSIS_ERROR,
                                       UNMANAGED_BAD_PRICE, UNMANAGED_ETF,
                                       UNMANAGED_NO_SELLABLE, UNMANAGED_RESTRICTED,
                                       UNMANAGED_STALE_PRICE, UNMANAGED_STUCK_PENDING)
from modules.auto_trade.common import (_enrich_rules_with_weights, _get_trade_account, entry_open_delay_remaining,
    trade_account_key, get_mystock_log_tail, get_restricted_stocks, is_plausible_baseline, is_single_price_break, is_system_market_open, load_daily_initial_asset, load_daily_principal, save_daily_initial_asset)

console = config.console

logger = logging.getLogger(__name__)

#  디스크 여유가 이보다 적으면 경고한다. DB·백업·차트 캐시·로그가 같은 SD카드를 쓰므로
#  여유가 이 정도로 줄면 곧 쓰기가 실패한다.
DISK_FREE_WARN_MB = 200.0
#  같은 DB 쓰기 실패를 다시 알리기까지의 간격(초). 디스크가 차면 매 주기 실패하므로
#  억제하지 않으면 알림이 도배되어 정작 중요한 경보가 묻힌다.
DB_WRITE_FAIL_ALERT_COOLDOWN = 1800.0
# [입출금 자동 조정 상한] 기준 자산은 계좌 차단기의 분모이자 사이징의 기준이라, 오탐 한 번이
#  두 장치를 동시에 틀어 놓는다. 추정 금액이 기준 자산의 이 비율(또는 아래 절대 하한)을 넘으면
#  자동 반영하지 않고 사람에게 넘긴다 — 반영하지 않으면 기준이 옛 값(더 작은 쪽)으로 남아
#  차단기가 더 일찍 걸리므로, '안 고치는 쪽'이 안전한 방향이다.

#  계좌 차단기(일일 손실 한도) 점검이 이만큼 연속 실패하면 알린다. 한 번은 일시적
#  데이터 결손일 수 있지만, 연속 실패는 '차단기가 꺼져 있다'는 뜻이다.
CIRCUIT_BREAKER_ALERT_FAILS = 3
#  '서버는 정상인데 코드가 터진다'를 다시 알리기까지의 간격(초). 이 상태는 대기로
#  숨겨지지 않고 매 주기 반복되므로, 억제하지 않으면 알림이 도배된다.
CODE_ERROR_ALERT_COOLDOWN = 1800.0

#  [오프라인 입출금 확정 문턱] 원금 대조에는 잔돈이 남는다 — 매수 수수료는 실현손익에
#   들어가지 않고, 세금·배당도 원 단위로 어긋난다. 그 잡음을 입출금으로 확정하면
#   (출금 방향일 때) 자산 고점이 낮아져 리스크 한도가 조용히 열린다.
#   그래서 절대 하한(장중 감지와 같은 5만원)과 계좌 규모 대비 비율 중 **작은 쪽**을 쓴다.
#   비율을 함께 두는 이유: 잡음은 계좌 규모에 비례하는데 5만원 고정이면 소액 계좌에서
#   전 재산이 빠져나가도(실제 사례: 10,027원 계좌의 1만원 출금 → 가짜 드로다운 99.7%)
#   문턱을 못 넘는다.
OFFLINE_TRANSFER_ABS_MIN = 50_000.0
OFFLINE_TRANSFER_RATIO = 0.005
OFFLINE_TRANSFER_FLOOR = 100.0


def _pkg():
    """패키지(modules.auto_trade) 네임스페이스 접근자.

    분해 전에는 모듈 전역 조회였던 상호 호출을 패키지 속성 조회로 유지해,
    테스트의 patch('modules.auto_trade.X') 가 분해 전과 동일하게 내부 호출에도
    적용되도록 한다. (지연 import라 순환 없음)
    """
    import modules.auto_trade as _at
    return _at


#  휴장 판정은 달력일 기준이라 자정에 뒤집힌다(일 23:59 'holiday' → 월 00:00 'closed').
#  둘 다 거래가 없는 같은 상태인데 문자열만 달라 자정마다 '시장 상태 변경' 알림이 나갔다
#  (실측 2026-08-03 00:00 "장 마감 · KRX 종가"). 알림 비교에서는 한 상태로 묶는다.
_IDLE_SESSION_PHASES = frozenset({'closed', 'holiday'})


def session_phase_key(phase):
    """세션 전환 알림용 비교 키. 거래 없는 단계(마감·휴장)는 하나로 접는다."""
    return 'idle' if phase in _IDLE_SESSION_PHASES else phase


def candidate_priority_key(c):
    """[추세추종] 매수 후보 우선순위 정렬 키 — 게이트(매수 점수 통과)와 랭킹을 분리한다.

    점수 1순위, 추세 품질(회귀 모멘텀 = 연환산 기울기 × R²)은 그 **동점을 가르는** 2순위다.
    이력 부족(None)은 검증 불가로 보아 동점 안에서 최하순위. (3순위 이하: 52주 위치 → 체결강도)

    [실증 2026-08-12] 종전에는 추세 품질이 1순위였다. "점수는 이진 신호 합산이라 동점이
     흔하고 추세의 강도·지속성을 구분하지 못한다"는 것이 근거였는데, 동점이 흔하다는 관찰은
     맞았지만 **1순위를 통째로 넘기는 것은 실측상 열위**였다.
     (tools/audit_scoring_weights.py --only C · tools/audit_entry_gate_parity.py,
      10년 15회 × 25종목 짝비교)

       · 추세품질 1순위(종전) : 하위 4구간 19/60 · 게이트 반영 후 20/60 · 고변동 3-0-12
                                10년 수익 384.9 → 286.8, 고변동 상위10% 141.8 → 62.8
       · 점수 1순위 + 추세품질 동점가름(현행) : 하위 4구간 33/60 (채택 기준 31/60 통과)
                                MAR승 34/60 · 꼬리승 34/60 · MDD 동일(-29.9)

     추세 품질을 1순위로 두면 슬롯 경쟁에서 점수가 **한 번도** 주인을 가르지 못한다 —
     추세 품질은 연속값이라 동점이 0%라서, 2순위인 점수는 죽은 조항이었다. 그 상태로
     10년 수익의 4분의 1과 고변동 구간의 fat-tail 절반을 잃고 있었다.
     반대로 점수를 1순위로 두면 경쟁일의 25~32%가 동점이 되는데, 종전 백테스트는 그
     동점을 관심종목 등록 순서로 갈랐다. 그 임의 상수 자리에 추세 품질을 넣은 것이
     지금 이 순서다 — 두 측정(상관관계 게이트 OFF/ON)에서 모두 기준을 넘겼다.

    [남은 한계] 체결강도·호가비 게이트는 실시간 체결 데이터라 백테스트로 재현할 수 없어
     실매매의 후보 풀은 측정보다 좁다. 또 후보가 남은 슬롯보다 많았던 날은 전체 거래일의
     11.0%뿐이다 — 이 순위 축은 드물게 개입하고 크게 남기므로, 표본이 얇다는 점을 감안해
     다른 축보다 신뢰구간을 넓게 잡을 것.
    """
    tq = c.get('trend_quality')
    return (-c['score'],
            -(tq if tq is not None else float('-inf')),
            -(c.get('w52_pos') or 0.0), -(c.get('vol_strength') or 0.0))


def index_source_note(stat):
    """지수 상태에 붙일 출처 꼬리표. 최후 폴백일 때만 표시한다.

    지수는 KRX 확정 봉 위에 KIS·토스·tvDatafeed·yfinance 중 하나를 얹어 만든다
    (analysis._fetch_domestic_index_data). 평상시 출처까지 화면에 늘어놓으면 읽는 데
    방해가 되지만, **최후 폴백(yfinance)** 은 다르다 — 최신 거래일 종가를 결측으로 주는
    일이 잦아 시장 필터가 어긋났을 때 가장 먼저 의심할 자리다. 그때만 밝힌다.
    """
    src = (stat or {}).get('source') or "" if isinstance(stat, dict) else ""
    return " [dim](yfinance 폴백)[/]" if "YFINANCE" in str(src).upper() else ""


class AutoTrader:
    _instance = None
    
    def __new__(cls):
        if cls._instance is None:
            cls._instance = super(AutoTrader, cls).__new__(cls)
            cls._instance._lock = threading.RLock() # [추가] 스레드 동기화 락
            cls._instance.is_running = False
            cls._instance.thread = None
            cls._instance.logs = []
            cls._instance.trade_history = []
            cls._instance.trade_records = []
            cls._instance.start_time = None
            cls._instance.consecutive_errors = 0
            # [안전장치] 직전 주기의 보유 종목 수. 잔고가 갑자기 0건이 되면
            #  한 주기 재확인 후에만 수용한다 (_run_loop 참조).
            cls._instance.last_holdings_count = 0
            # 운영 관제용 수명주기 정보. /health는 외부 API를 추가 호출하지 않고
            # 이 값을 읽어 현재 루프의 생존성·최근 장애를 보여준다.
            cls._instance.last_cycle_at = None
            # [계측] 주기 소요 시간(초). 실제 청산 감시 간격 = 이 값 + SYSTEM_TRADING_INTERVAL
            cls._instance.last_cycle_secs = None
            cls._instance.cycle_secs_history = []   # 최근 값만 유지(라즈베리파이 메모리 고려)
            cls._instance.cycle_secs_peak = 0.0
            cls._instance.last_success_at = None
            cls._instance.waiting_for_server = False   # 서버 복구 대기(의도된 멈춤)
            cls._instance.last_error_at = None
            cls._instance.last_error_message = ""
            cls._instance.initial_asset = 0
            cls._instance.baseline_principal = 0   # [추가] 입금 자동감지용 기준 원금(현금+매입원가-실현손익). initial_asset(총자산)과 별개.
            cls._instance.was_market_open = None
            cls._instance.trailing_stop_cache = {} # [추가] 트레일링 스탑 메모리 캐시 (DB 부하 감소용)
            cls._instance.market_status_notified = {} # [수정] 시장 상태 알림 플래그 (시장별 관리)
            cls._instance.market_index_status = {}    # [추가] 지수 상태 캐시
            cls._instance.stock_market_map = {}       # [추가] 종목별 시장 구분 캐시
            # [이동] 분석된 종목 상태 캐시는 context._STOCK_STATE로 옮겼다 (수동 조회와 공용).
            #  읽기는 stock_state_cache 프로퍼티가 그대로 제공한다.
            cls._instance.skipped_by_market_filter_count = {"KOSPI": 0, "KOSDAQ": 0} # [추가] 시장 필터링 보류 종목 수
            cls._instance.current_total_asset = 0     # [리스크 스케일링] 최근 조회된 현재 평가자산 (히트 캡 기준자산·드로다운 계산용)
            cls._instance.risk_scale = 1.0            # [리스크 스케일링] 계좌 단위 배수 = 열위 시장 기준 (히트 캡용, 1.0=축소 없음)
            cls._instance.risk_scale_reason = ""      # [리스크 스케일링] 현재 배수의 사유 (로그 표시용)
            cls._instance.risk_scale_by_market = {}   # [리스크 스케일링] 시장별 배수 {KOSPI: x, KOSDAQ: y} — 종목 사이징용
            cls._instance.risk_scale_reason_by_market = {}
            cls._instance.strategy = DefaultStrategy() # [추가] 전략 인스턴스
            cls._instance.last_log_date = datetime.now().date() # [추가] 로그 파일 날짜 추적용
            cls._instance.initial_holdings = None # [추가] 초기 조회 잔고 캐시
            cls._instance.initial_summary = None  # [추가] 초기 조회 요약 캐시
            cls._instance.file_logger = config.get_autotrade_logger() # [추가] 파일 로거 초기화
            cls._instance.restricted_notified = {} # [추가] 거래 제한 알림 스로틀링 (종목별 타임스탬프)
            cls._instance.order_manager = OrderManager(cls._instance) # [추가] 주문 매니저
            cls._instance.risk_manager = RiskManager(cls._instance)   # [추가] 리스크 매니저
            cls._instance.half_tp_cache = set()       # [추가] 반익절 실행 여부 추적 캐시
            cls._instance.portfolio_heat_amt = 0.0    # [추가] 포트폴리오 히트(총 오픈 리스크, 원) 주기별 스냅샷
            cls._instance.portfolio_heat_unknown = False  # 산출 실패 여부 — '0(없음)'과 '못 셈'을 가른다
            # 보유 종목별 '직전 주기 매도 판정이 실제로 쓴' 손절률·ATR — 오픈 리스크
            #  산출이 역산 근사 대신 이 실측값을 쓴다(engine.compute_portfolio_heat live_map).
            cls._instance.holding_risk_cache = {}
            cls._instance.last_emergency_alert_time = 0 # [추가] 긴급 알림 쿨타임용 타임스탬프
            cls._instance.last_wait_alert_time = 0    # [추가] 대기 모드 진입 알림 쿨타임 (진입/복구 반복 시 스팸 방지)
            cls._instance._wait_alert_sent = False    # [추가] 진입 알림 발송 여부 (복구 알림과 짝 맞춤)
            # [안전장치] 방어 모드 — 신규 매수(피라미딩 포함)만 중단하고 매도·손절 감시는 계속 돌린다.
            #  일일 손실 한도 초과 시 시스템 전체를 정지하던 기존 동작은, 정작 손절이 가장 필요한
            #  순간에 청산 엔진을 꺼버려 보유 포지션이 손절선 아래로 방치되는 문제가 있었다.
            cls._instance.buy_halted = False          # 방어 모드 활성 여부
            cls._instance.buy_halt_reason = ""        # 방어 모드 사유 (상태 표시용)
            cls._instance.buy_halt_date = None        # 방어 모드 발동 일자 (날짜 변경 시 자동 해제)
            cls._instance.buy_halt_kind = None        # 발동 원인 종류('daily_loss' 등) — 정정 시 재평가용
            cls._instance.net_transfer_today = 0      # 오늘 누적 순입출금(파생값) — effective_baseline 보정용
            cls._instance.unmanaged_stop_notified = {} # [안전장치] 자동매도 제외 포지션의 손절선 이탈 경보 스로틀 {code: ts}
            # [안전장치] '매도 결정했는데 매도가능수량 0'이 연속 몇 주기 관측됐는가 {code: 횟수}.
            #  미체결 취소 직후의 일시적 0과, 거래정지처럼 지속되는 상태를 구분하기 위한 값이다.
            cls._instance.no_sellable_streak = {}
            # 대기 주문에 묶여 매도 판정에서 빠진 연속 주기 수 {code: n}
            cls._instance.stuck_pending_streak = {}
            # 매도 판정 밖에서 앵커만 되짚는 경로(ETF)의 종목별 스로틀 {code: ts}
            cls._instance._anchor_restore_at = {}
            # [관측성] 장 마감 후 감지된 매도 신호의 알림 스로틀 {code: 사유}.
            #  마감 뒤에는 주문을 낼 수 없어 로그 한 줄만 남았다 — 청산이 하루 밀리는데
            #  운영자가 그 사실을 모른다. 장이 열리면 비워서 다음 마감 때 다시 알린다.
            cls._instance.after_hours_sell_notified = {}
            # 마감 후 청산 신호 스캔을 수행한 날짜(YYYYMMDD). 거래일당 1회로 묶는다.
            cls._instance.after_hours_scan_date = None
            # [관찰 모드] 마감 스냅샷을 찍은 날짜(YYYYMMDD). 거래일당 1회.
            cls._instance.paper_closing_snapshot_date = None
            # [안전장치] 거래소 미체결 현황을 파악했는가. initialize()의 재기동 복구가
            #  성공하면 True. False인 동안은 신규 매수를 보류한다(중복 주문 방지).
            cls._instance.pending_restore_ok = True
            # [안전장치] 계좌 단위 자동매매 배타 잠금(같은 계좌 이중 실행 방지). start에서 획득.
            cls._instance.instance_lock = None
            # [안전장치] 계좌 차단기(일일 손실 한도) 마지막 정상 수행 시각·연속 실패 횟수.
            #  차단기가 안 도는 것을 아무도 모르는 상태가 가장 나쁘다.
            cls._instance.circuit_breaker_ran_at = 0.0
            cls._instance.circuit_breaker_fails = 0
            # [안전장치] 서버는 정상인데 루프가 터진 횟수. 킬스위치가 '서버 장애 대기'로
            #  오판해 매도 감시까지 멈추는 것을 막은 횟수이기도 하다.
            cls._instance.code_error_streaks = 0
            cls._instance._code_error_alerted_at = 0.0

            cls._instance.initialized = False # [추가] 초기화 상태 플래그
            cls._instance.last_session_phase = None # [추가] 시장 세션 상태 변경 추적용
            # [추가] 로그 디렉토리 확인 및 생성
            log_dir = getattr(config, 'SYSTEM_TRADING_LOG_DIR', 'logs')
            if not os.path.exists(log_dir):
                try:
                    os.makedirs(log_dir)
                except Exception as e:
                    console.print(f"[red]로그 디렉토리 생성 실패: {e}[/red]")
        return cls._instance

    def __init__(self):
        # [추가] 로거 초기화 보장 (인스턴스 호출 시마다 확인)
        # 로거 객체가 없거나 핸들러가 연결되지 않은 경우 재설정
        if not getattr(self, 'file_logger', None) or not self.file_logger.handlers:
            self.file_logger = config.get_autotrade_logger()

    def set_stock_state(self, code, state):
        """종목의 기술적 상태 캐시 업데이트 (텔레그램 /stocks 연동용)

        [변경] 저장소를 context로 옮겨 수동 조회(메뉴 2) 결과와 한 곳에서 관리한다.
         값의 의미가 같으므로(둘 다 classify_stock_state 결과) 출처는 표시하지 않고,
         지우기 범위를 가르는 데만 쓴다 — 여기서 None을 넘겨도 더 신선한 수동 스냅샷은
         남겨야 시스템이 분석하지 않는 종목(NXT 시간대 ETF 등)의 상태가 보인다.
        """
        if state:
            context.set_stock_state(code, state, src='auto')
        else:
            context.clear_stock_state(code, src='auto')

    @property
    def stock_state_cache(self):
        """[호환] 종목→상태 문자열 맵. 실제 저장소는 context._STOCK_STATE."""
        with context.STOCK_STATE_LOCK:
            return {c: e['state'] for c, e in context._STOCK_STATE.items()}

    def _refine_trade_records(self, records):
        """거래 내역 중복 제거 및 우선순위 적용 (전략 사유 > 체결 확인)"""
        unique_records = {}
        
        for r in records:
            odno = r.get('odno')
            # odno가 없으면 고유 키 생성하여 포함
            if not odno:
                key = f"NO_ODNO_{r.get('time', '')}_{r.get('code', '')}_{r.get('type', '')}_{len(unique_records)}"
                unique_records[key] = r
                continue

            # [수정] KIS 주문번호(odno)는 영업일 단위로 채번되어 날짜가 다르면 재사용된다.
            #  키를 odno만 쓰면 서로 다른 날짜의 거래가 같은 odno로 병합되어 한쪽이
            #  소실(누락)되므로, (거래일 + odno)를 키로 사용해 날짜 충돌을 막는다.
            #  (같은 날의 접수→체결 병합은 그대로 유지된다)
            date_key = str(r.get('time', ''))[:10]
            key = f"{date_key}_{odno}"

            if key not in unique_records:
                unique_records[key] = dict(r) # 복사본 저장
            else:
                existing = unique_records[key]
                
                # 새 레코드(r)가 더 최신 정보(체결 등)를 담고 있을 때 병합
                if float(r.get('price', 0)) > 0 and float(existing.get('price', 0)) <= 0:
                    existing['price'] = r['price']
                    
                if r.get('profit_amt'):
                    existing['profit_amt'] = r['profit_amt']
                if r.get('profit_rate'):
                    existing['profit_rate'] = r['profit_rate']
                    
                old_reason = str(existing.get('reason', ''))
                new_reason = str(r.get('reason', ''))
                
                if "체결 확인" in old_reason and "체결 확인" not in new_reason:
                    existing['reason'] = new_reason
                elif "체결 확인" not in old_reason and "체결 확인" in new_reason:
                    pass # 기존 구체적 사유 유지
                else:
                    existing['reason'] = new_reason # 최신 사유로 덮어씀

                existing['time'] = r.get('time', existing.get('time'))
                if r.get('order_status'):
                    existing['order_status'] = r['order_status']

                # [추가] 주문 출처 꼬리표(type_full)는 '접수' 원본이 정확하다. 레코드는 시간
                #  오름차순으로 들어오므로 먼저 자리잡은 값(=접수)을 유지하고, 비어 있을 때만 채운다.
                #  (체결 확인 시점에 원주문 조회가 실패하면 그 레코드에는 (외부)가 붙는다)
                if not existing.get('type_full') and r.get('type_full'):
                    existing['type_full'] = r['type_full']
        
        return list(unique_records.values())

    def update_order_status(self, code, odno, status):
        """체결 모니터에서 호출하여 주문 상태 업데이트"""
        self.order_manager.update_order_status(code, odno, status)

    def initialize(self):
        """
        자동매매 시작에 필요한 모든 초기화 작업을 병렬로 수행합니다.
        (자산 조회, DB 캐시 로드, 초기 자산 설정 등)
        """
        if self.initialized:
            return True

        with Progress(
            SpinnerColumn(),
            TextColumn("[progress.description]{task.description}"),
            BarColumn(),
            TextColumn("[progress.percentage]{task.percentage:>3.0f}%"),
            console=console,
            transient=True,
            disable=not api._is_screen_output_allowed() # [추가] 텔레그램 스레드 등 백그라운드에서는 상태바 숨김
        ) as progress:
            # [수정] 모의투자는 예수금을 잔고 summary에서 유도하므로 작업 2개(잔고/DB), 실전은 3개(+예수금)
            _init_total = 3
            task = progress.add_task("[cyan]자동매매 세션 초기화 중...[/cyan]", total=_init_total)
            
            target_cano = config.session.auto_cano
            acnt = config.session.auto_acnt_prdt_cd
            
            with utils.AccountContext(target_cano):
                # 병렬 실행을 위한 변수
                results = {}

                def _fetch_balance():
                    progress.update(task, description="[cyan]잔고/평가금 조회...[/cyan]")
                    holdings, summary = api.get_domestic_balance(target_cano, acnt)
                    progress.advance(task)
                    return "balance", (holdings, summary)

                def _fetch_deposit():
                    progress.update(task, description="[cyan]예수금 상세 조회...[/cyan]")
                    deposit_res = api.get_deposit_balance(target_cano, acnt)
                    progress.advance(task)
                    return "deposit", deposit_res

                def _load_db_caches():
                    progress.update(task, description="[cyan]DB 캐시 로드...[/cyan]")
                    ts_cache = db_manager.db.get_all_trailing_stops()
                    half_cache = db_manager.db.get_all_half_tp()
                    # [재기동 복구] 거래소에 살아 있는 미체결 주문을 메모리 추적에 되살린다.
                    #  이걸 안 하면 첫 주기에 같은 종목으로 두 번째 주문이 나간다.
                    #  DB 캐시 작업에 얹어 시작 시 API 호출이 몰리지 않게 한다(라즈베리파이 OOM).
                    ok = self.order_manager.restore_pending_orders(target_cano, acnt)
                    progress.advance(task)
                    return "caches", (ts_cache, half_cache, ok)


                with concurrent.futures.ThreadPoolExecutor(max_workers=3, thread_name_prefix="at_init") as executor:
                    # [수정] 모의투자는 잔고 summary에 예수금이 포함되어 있어 별도 예수금 API 호출이 불필요.
                    # 초기화 시 중복 잔고조회(get_domestic_balance)+예수금조회가 2-TPS 경합으로 재시도
                    # 폭주를 일으켜 메모리가 폭증하던 문제를 제거한다. (실전만 별도 예수금 조회 수행)
                    futures = [executor.submit(_fetch_balance), executor.submit(_load_db_caches)]
                    futures.append(executor.submit(_fetch_deposit))
                    for future in concurrent.futures.as_completed(futures):
                        key, value = future.result()
                        results[key] = value

                # 결과 처리
                holdings, summary = results.get("balance", (None, None))
                ts_cache, half_cache, pending_restored = results.get("caches", ({}, set(), False))
                # 조회 실패는 '미체결 없음'과 다르다 — 모르는 상태에서 신규 매수를 더 내지
                # 않는다. 매도는 막지 않는다(청산 경로는 어떤 경우에도 열려 있어야 한다).
                self.pending_restore_ok = bool(pending_restored)

                deposit_res = results.get("deposit")

                if holdings is None or deposit_res is None:
                    raise Exception("자산/예수금 조회 실패 (API 응답 없음)")

                # [기동 동기화] 시스템이 꺼져 있던 동안의 외부 체결을 DB에 채운다.
                #  ConclusionMonitor의 실시간 감지는 '오늘'만 본다(get_today_history). 운용자가
                #  NXT 애프터나 비운용일에 HTS로 매매하면 그 기록은 영구 누락됐다.
                #  보유수량 역산이라 얼마나 오래 꺼져 있었든 필요한 만큼만 조회한다.
                #  잔고가 확정된 뒤에 돌린다 — 위 병렬 블록 안에서는 잔고가 아직 없다.
                self._sync_external_fills(target_cano, acnt, holdings)

                self.trailing_stop_cache = ts_cache
                self.half_tp_cache = half_cache
                self.initial_holdings = holdings
                self.initial_summary = summary
                
                # [수정] 초기 총자산 산출.
                # 기존에는 get_asset_status_data를 재호출했으나, 이는 startup 직후 잔고조회를
                # 또 수행(+해외잔고/체결내역)하여 짧은 시간에 KIS 호출이 몰리고, 그 중 일부가
                # 재시도(타임아웃/EGW00201) 경로로 빠지며 네이티브 메모리가 폭증(OOM)하는 원인이었다.
                # 모의투자는 이미 조회한 잔고 summary의 총평가금(tot_evlu_amt)으로 유도하여
                # 추가 KIS 호출을 제거한다. (실전만 해외자산 포함 통합 조회 유지)
                asset_data = account.get_asset_status_data(target_cano, acnt)
                tot_asset = asset_data.get('tot_asset', 0) if asset_data else 0

                if tot_asset > 0:
                    account_key = f"{target_cano}-{acnt}"
                    saved_initial = load_daily_initial_asset(account_key)
                    if saved_initial > 0:
                        self.initial_asset = saved_initial
                        # [오프라인 입출금] 기준 원금도 함께 복원한다. 복원하지 않으면 아래
                        #  감시 루프가 기준 원금을 **입출금 이후 상태로** 새로 잡아 차이가
                        #  0이 되고, 프로그램이 꺼진 사이의 입출금이 영영 감지되지 않는다.
                        #  그러면 시작 자산만 옛 값으로 남아(출금이면 높게) 차단기가 헛발동하고
                        #  사이징 기준도 부푼 채로 하루가 간다.
                        self.baseline_principal = load_daily_principal(account_key)
                    elif is_plausible_baseline(account_key, tot_asset):
                        self.initial_asset = tot_asset
                        save_daily_initial_asset(account_key, self.initial_asset)
                        db_manager.db.save_daily_asset(datetime.now().strftime("%Y-%m-%d"), account_key, self.initial_asset)
                    else:
                        # [안전장치] 직전 영업일 대비 반토막 이하 — 시세 결손으로 예수금만 잡힌
                        #  응답을 의심한다. 이 값을 기준선으로 박으면 차단기의 분모가 작아져
                        #  손실률이 종일 큰 양수로 계산되고, 보호 장치가 조용히 사라진다.
                        #  저장하지 않고 0으로 둔다 — 사이징은 예수금 기준으로 폴백하고,
                        #  차단기가 꺼진 사실은 아래 알림으로 드러난다.
                        self.initial_asset = 0
                        last = db_manager.db.get_last_daily_asset(
                            account_key, datetime.now().strftime("%Y-%m-%d"))
                        warn = (f"⚠️ [시작 자산 이상] 조회값 {tot_asset:,}원이 직전 기록"
                                f" {int(last or 0):,}원 대비 지나치게 작습니다.\n"
                                f"시세 결손(주식 평가액 0 수신)이 의심되어 오늘 기준 자산으로"
                                f" 삼지 않습니다.\n"
                                f"계좌 차단기(일일 손실 한도)가 동작하지 않으니 확인해 주세요.")
                        self.log(warn)
                        api.send_telegram_message(warn)
                else:
                    self.initial_asset = 0

                self.initialized = True
                return True
        return False

    def _trade_account_key(self):
        return trade_account_key()

    def _acquire_instance_lock(self):
        """이 계좌의 자동매매 배타 잠금을 잡는다. 실패하면 시작하지 않는다.

        선점자가 있다는 것은 '다른 프로세스가 이미 이 계좌로 매매 중'이라는 뜻이다.
        그대로 시작하면 서로의 미체결을 모른 채 같은 종목에 각자 주문을 낸다.
        """
        try:
            lock = instance_lock.InstanceLock(self._trade_account_key())
            if lock.acquire():
                self.instance_lock = lock
                return True
        except Exception as e:
            # 잠금 장치 자체가 고장 났다고 매매를 막지는 않는다(잠금은 보조 안전장치다).
            self.log(f"[중복 실행 검사] 잠금 처리 실패 — 검사를 건너뜁니다: {e}")
            return True

        holder = f" ({lock.holder})" if lock.holder else ""
        msg = (f"자동매매를 시작할 수 없습니다 — 같은 계좌({self._trade_account_key()})로 "
               f"이미 다른 프로세스가 매매 중입니다{holder}.")
        self.log(f"[중복 실행 차단] {msg}")
        if api._is_screen_output_allowed():
            console.print(f"\n[bold red]{msg}[/bold red]")
            console.print("[dim]두 인스턴스가 동시에 돌면 서로의 미체결 주문을 몰라 "
                          "같은 종목에 중복 주문이 나갑니다.[/dim]")
        api.send_telegram_message(f"⛔ [중복 실행 차단] {msg}")
        return False

    def _release_instance_lock(self):
        lock = getattr(self, 'instance_lock', None)
        if lock is not None:
            self.instance_lock = None
            try:
                lock.release()
            except Exception as e:
                logger.debug(f"[InstanceLock] 해제 실패: {e}")

    def _check_db_health(self):
        """DB 무결성 확인 + 당일 백업. 무결성이 깨졌으면 매매를 시작하지 않는다.

        [왜 fail-closed 인가] 이 DB에는 평단·트레일링 최고가·손절 기준이 들어 있다.
        잔고는 증권사에 있으니 '무엇을 들고 있는지'는 복구되지만, '어디서 자를지'는
        여기에만 있다. 손상된 채로 돌리면 트레일링 최고가가 사라져 청산이 어긋나고,
        그 오류는 조용히 손실로만 나타난다. 멈추는 쪽이 낫다.

        백업은 실패해도 막지 않는다 — 백업이 없다고 지금 매매가 틀리지는 않는다.
        """
        ok, detail = db_manager.db.check_integrity()
        if not ok:
            msg = (f"DB 무결성 검사 실패 — 자동매매를 시작하지 않습니다.\n"
                   f"경로: {db_manager.db.db_path}\n결과: {detail}")
            self.log(f"[DB 이상] {msg}")
            if api._is_screen_output_allowed():
                console.print(f"\n[bold red]{msg}[/bold red]")
                console.print("[dim]db/backups 의 최근 백업으로 복구한 뒤 다시 시작하세요. "
                              "(평단·트레일링 최고가·손절 기준이 이 파일에만 있습니다)[/dim]")
            api.send_telegram_message(f"⛔ [DB 이상] {msg}")
            return False

        path = db_manager.db.backup()
        if path:
            self.log(f"[DB 백업] {os.path.basename(path)}")
        else:
            # 백업 실패로 매매를 막지는 않되, 조용히 넘기지도 않는다.
            self.log("[DB 백업] 실패 — 백업 없이 진행합니다(운영자 확인 필요)")

        # [안전장치] 디스크가 차면 쓰기가 실패한다 — 트레일링 최고가·거래 기록이 사라진다.
        #  **막지는 않는다.** 손상과 달리 지금 읽는 값은 옳고, 여기서 멈추면 보유 포지션의
        #  손절 감시까지 함께 멈춘다. 대신 크게 알린다.
        free_mb = db_manager.db.disk_free_mb()
        if 0 <= free_mb < DISK_FREE_WARN_MB:
            msg = (f"디스크 여유 공간 부족: {free_mb:,.0f}MB — DB 쓰기가 실패하면 "
                   f"트레일링 최고가·거래 기록이 사라집니다(매매는 계속합니다).")
            self.log(f"[디스크 경고] {msg}")
            if api._is_screen_output_allowed():
                console.print(f"\n[bold yellow]⚠ {msg}[/bold yellow]")
            api.send_telegram_message(f"⚠️ [디스크 경고] {msg}")
        return True

    def _check_db_write_failures(self):
        """새 쓰기 실패가 생겼으면 알린다(같은 원인 반복은 억제).

        DB 계층은 알림을 보내지 않는다(계층 분리). 대신 카운터를 여기서 읽어 올린다.
        """
        try:
            h = db_manager.db.get_write_failures()
        except Exception:
            return
        seen = getattr(self, '_db_write_fail_seen', 0)
        if h['count'] <= seen:
            return
        self._db_write_fail_seen = h['count']

        now = time.time()
        if now - getattr(self, '_db_write_fail_alerted_at', 0.0) < DB_WRITE_FAIL_ALERT_COOLDOWN:
            return
        self._db_write_fail_alerted_at = now

        free_mb = db_manager.db.disk_free_mb()
        msg = (f"DB 쓰기 실패 누적 {h['count']}건 (최근: {h['last_op']} — {h['last_error']})\n"
               f"디스크 여유 {free_mb:,.0f}MB\n"
               f"트레일링 최고가가 저장되지 않으면 재기동 후 청산선이 어긋납니다.")
        self.log(f"[DB 쓰기 실패] {msg}")
        api.send_telegram_message(f"⚠️ [DB 쓰기 실패] {msg}")

    def start(self, interactive=True):
        if self.is_running:
            if interactive:
                console.print("\n[yellow]이미 자동매매가 실행 중입니다.[/yellow]")
            return
        
        self.log("━━━ 자동매매 시스템 시작 프로세스 진입 ━━━")

        # [안전장치] 시작 시 방어 모드는 초기화한다. 손실 한도 조건이 여전히 유효하면
        #  첫 주기의 check_loss_limit이 즉시 재발동시키므로 안전 수준은 유지된다.
        if getattr(self, 'buy_halted', False):
            self.resume_buys(reason="시스템 재시작")

        if config.session.is_toss:
            # [추가] 토스: 단일 계좌 + 토스 API 사용. 별도 KIS AUTO 계좌가 필요 없다.
            if not config.session.toss_app_key or not config.session.toss_app_secret or not config.session.cano:
                if api._is_screen_output_allowed():
                    console.print("[bold red]오류: 토스 시스템 트레이딩을 실행하려면 토스 API 설정이 필요합니다.[/bold red]")
                    console.print("[dim]환경 변수 TOSS_APP_KEY, TOSS_APP_SECRET, TOSS_ACC_NUM을 설정해주세요.[/dim]")
                return

            # [안내] 토스는 주식계좌를 하나만 내준다. 한투 실전처럼 자동매매 전용 계좌로
            #  갈라 둘 수 없으므로, 시스템 트레이딩도 수동 주문과 같은 계좌에서 돈다.
            #  운용자가 알아야 할 사실이라(예수금·보유수량을 수동 매매와 나눠 쓴다)
            #  시작 확인 앞에서 명시한다.
            if interactive:
                console.print("\n[bold magenta]!!! 경고: 토스증권 실계좌에서 시스템 트레이딩을 시작합니다 !!![/bold magenta]")
                console.print(f"운용 계좌: [bold yellow]{config.session.cano}[/bold yellow] (토스증권, 실제 자산 거래)")
                console.print("[dim]토스증권은 주식계좌를 하나만 제공합니다 — 시스템 트레이딩이 수동 주문과 "
                              "같은 계좌를 사용합니다(자동매매 전용 계좌 없음).[/dim]")
                console.print("[dim]따라서 수동으로 낸 주문·보유 종목이 자동매매의 예수금과 슬롯을 함께 씁니다.[/dim]")
                utils.print_breadcrumb()
                if Prompt.ask("위 계좌로 실제 매매가 수행됩니다. 진행하시겠습니까?", choices=["y", "n"], default="n") != "y":
                    console.print("[yellow]시작을 취소했습니다.[/yellow]")
                    return
            else:
                if api._is_screen_output_allowed():
                    console.print("[bold cyan][시스템 명령] 토스증권 자동매매를 시작합니다(수동 주문과 같은 계좌).[/bold cyan]")
        elif config.session.is_paper:
            if interactive:
                virt_acc_str = os.environ.get("VIRT_ACC_NUM", "")
                display_acc = virt_acc_str.replace("PAPER-", "") if virt_acc_str.startswith("PAPER-") else virt_acc_str
                console.print(f"\n운용 계좌: [bold yellow]PAPER | {display_acc}[/bold yellow]")
                utils.print_breadcrumb()
                if Prompt.ask("위 계좌로 매매가 수행됩니다. 진행하시겠습니까?", choices=["y", "n"], default="n") != "y":
                    console.print("[yellow]시작을 취소했습니다.[/yellow]")
                    return
            else:
                if api._is_screen_output_allowed():
                    console.print("[bold cyan][시스템 명령] 가상 투자 자동매매를 시작합니다.[/bold cyan]")
        else:
            if not config.session.auto_app_key or not config.session.auto_cano:
                if api._is_screen_output_allowed():
                    console.print("[bold red]오류: 실전 투자 모드에서 시스템 트레이딩을 실행하려면 별도의 자동매매 계좌 설정이 필요합니다.[/bold red]")
                    console.print("[dim]환경 변수 AUTO_APP_KEY, AUTO_APP_SECRET, AUTO_ACC_NUM을 설정해주세요.[/dim]")
                return

            if interactive:
                console.print("\n[bold red]!!! 경고: 실전 투자 모드에서 시스템 트레이딩을 시작합니다 !!![/bold red]")
                console.print(f"운용 계좌: [bold yellow]{config.session.auto_cano}-{config.session.auto_acnt_prdt_cd}[/bold yellow] (시스템 트레이딩 전용)")
                utils.print_breadcrumb()
                if Prompt.ask("위 계좌로 실제 매매가 수행됩니다. 진행하시겠습니까?", choices=["y", "n"], default="n") != "y":
                    console.print("[yellow]시작을 취소했습니다.[/yellow]")
                    return
            else:
                if api._is_screen_output_allowed():
                    console.print("[bold cyan][시스템 명령] 실전 투자 자동매매를 시작합니다.[/bold cyan]")

        # [안전장치] 같은 계좌로 엔진이 두 개 뜨면 서로의 미체결 주문을 모른다 —
        #  각자 같은 종목에 매수를 내고, 재기동 복구도 이걸 못 막는다(둘 다 거래소
        #  미체결을 자기 주문으로 읽는다). 매매를 시작하기 전에 잠근다.
        if not self._acquire_instance_lock():
            return

        # [안전장치] 손절 기준이 든 DB가 깨졌으면 매매하지 않는다(잠금은 되돌린다).
        if not self._check_db_health():
            self._release_instance_lock()
            return

        try:
            # [수정] 초기화 로직 분리
            if not self.initialized:
                if not self.initialize():
                    self._release_instance_lock()
                    self.log("초기화 실패로 자동매매를 시작할 수 없습니다.")
                    if api._is_screen_output_allowed():
                        console.print("[bold red]시스템 초기화에 실패하여 자동매매를 시작할 수 없습니다.[/bold red]")
                        if config.SCREEN_DEBUG_LEVEL in ["ERROR", "TRACE", "DEBUG"]:
                            console.print("[bold red][ERROR] 시스템 초기화 실패[/bold red]")
                    return

            self.is_running = True
            self.start_time = datetime.now()
            self.consecutive_errors = 0
            self.was_market_open = self.is_market_open()
            self._first_loop_flag = True
            self.market_status_notified = {}
            context.SYSTEM_LOGGER = self.log

            self.thread = threading.Thread(target=self._run_loop, daemon=True, name="AutoTrader")
            self.thread.start()

            if api._is_screen_output_allowed():
                console.print("\n[green]자동매매 시스템이 시작되었습니다. (백그라운드)[/green]")
            self.log("시스템 시작")
            
            # [추가] 장 마감 상태에서 시작했을 경우 명확한 안내 메시지 출력
            if not self.was_market_open:
                self.log("━" * 85)
                if is_single_price_break():
                    self.log("⏸️ [휴게 시간 대기] 현재는 단일가 매매 동기화 시간입니다. 거래 재개 시 자동으로 매매가 개시됩니다.")
                elif api.is_holiday_today() or datetime.now().weekday() > 4:
                    self.log("💤 [휴장일 대기] 오늘은 주말 또는 공휴일입니다. 다음 거래일에 자동으로 매매가 개시됩니다.")
                else:
                    self.log("💤 [장 마감 대기] 현재는 거래 시간이 아닙니다. 장 시작 시 자동으로 매매가 개시됩니다.")
                self.log("━" * 85)
            
            # [수정] 시작 메시지 생성 로직은 초기화 시 저장된 데이터 활용
            holdings = self.initial_holdings
            summary = self.initial_summary
            deposit = 0
            if self.initial_asset > 0:
                if summary:
                    deposit = api.safe_int(summary[0].get('prvs_rcdl_excc_amt', 0))
                    if deposit == 0:
                        deposit = api.safe_int(summary[0].get('dnca_tot_amt', 0))
            
            msg = f"🟢 [시스템 시작] 자동매매가 시작되었습니다.\n"
            msg += f"초기 자산: {self.initial_asset:,}원"
            msg += f"\n현재 예수금: {deposit:,}원"
            
            # [복원] 상세 자산 현황 추가
            stock_eval_amt = 0
            if summary and len(summary) > 0:
                s_data = summary[0]
                stock_eval_amt = api.safe_int(s_data.get('scts_evlu_amt'))
                total_profit = api.safe_int(s_data.get('evlu_pfls_smtl_amt'))
                
                tot_pchs = api.safe_int(s_data.get('pchs_amt_smtl'))
                if tot_pchs == 0 and holdings:
                    tot_pchs = sum(int(int(h['hldg_qty']) * float(h['pchs_avg_pric'])) for h in holdings if int(h.get('hldg_qty', 0)) > 0)
                
                rate = (total_profit / tot_pchs * 100) if tot_pchs > 0 else 0.0
                msg += f"\n증권 평가 자산: {stock_eval_amt:,}원"
                msg += f"\n증권 평가 손익: {total_profit:+,}원 ({rate:+.2f}%)"

            # [복원] 전략 설정 요약 정보 추가
            buy_score = config.ANALYSIS_THRESHOLDS["BUY_SCORE"]
            buy_rsi = config.ANALYSIS_THRESHOLDS["BUY_RSI_MAX"]
            buy_vol = config.ANALYSIS_THRESHOLDS["BUY_VOL_STRENGTH"]
            sl = config.SELL_STRATEGY["STOP_LOSS_RATE"]
            tp = config.SELL_STRATEGY["TAKE_PROFIT_RATE"]
            ts_act = _pkg().ts_activation_label()
            ts_call = config.SELL_STRATEGY.get("TRAILING_STOP_CALLBACK_RATE", 5.0)
            sell_score = config.SELL_STRATEGY["SELL_SCORE"]
            tp_rsi = config.SELL_STRATEGY["TAKE_PROFIT_RSI"]
            invest_ratio_str = config.format_invest_ratio()

            use_half_tp = config.SELL_STRATEGY.get("HALF_TAKE_PROFIT_USE", False)
            use_atr_stop = config.SELL_STRATEGY.get("USE_ATR_STOP", True)
            atr_mult = config.SELL_STRATEGY.get("ATR_STOP_MULTIPLIER", 2.0)
            use_time_stop = config.SELL_STRATEGY.get("TIME_STOP_USE", True)
            time_stop_days = config.SELL_STRATEGY["TIME_STOP_DAYS"]

            msg += "\n\n⚙️ [적용 전략]"
            if config.session.is_toss:
                # 토스는 체결강도 미제공 → 매도잔량비 게이트로 대체
                buy_abr = config.ANALYSIS_THRESHOLDS.get("BUY_ASK_BID_RATIO", 1.0)
                msg += f"\n• 매수: {buy_score}점↑ & RSI {buy_rsi}↓ & 매도잔량비 {buy_abr}배↑"
            else:
                msg += f"\n• 매수: {buy_score}점↑ & RSI {buy_rsi}↓ & 체결강도 {buy_vol}%↑"
            # [수정] 0=미사용 규칙(RSI 과열·고정 익절)은 조건을 표시하지 않는다
            #  ("RSI 0.0 초과"/"익절 +0.0%"처럼 OFF 규칙이 활성으로 보이던 표시 모순 해소)
            msg += f"\n• 매도: {sell_score}점 미만+60일선 이탈"
            if tp_rsi > 0:
                msg += f" / RSI {tp_rsi} 초과"

            if tp > 0:
                tp_str = f"+{tp}%"
                if use_half_tp:
                    half_tp_rate = config.SELL_STRATEGY.get("HALF_TAKE_PROFIT_RATE", tp / 2.0)
                    tp_str += f" (반익절 +{half_tp_rate:.1f}%)"
            else:
                tp_str = "미사용 (추세추종: TS 주청산)"
            
            if use_atr_stop:
                sl_str = f"ATR 동적손절 (x{atr_mult})"
            else:
                sl_str = f"고정 {sl}%"
            
            msg += f"\n• 익절: {tp_str}"
            msg += f"\n• 손절: {sl_str}"
            msg += f"\n• 트레일링: {ts_act} 도달 후 -{ts_call}%"
            if use_time_stop:
                msg += f"\n• 시간청산: {time_stop_days}일 경과"
            msg += f"\n• 비중: 종목당 {invest_ratio_str}"
                
            # [복원] 보유 종목 현황 추가
            valid_holdings = [h for h in holdings if int(h.get('hldg_qty', 0)) > 0] if holdings else []

            if valid_holdings:
                from modules import account
                analysis_results = account.run_holding_analysis(valid_holdings, [], _pkg().get_restricted_stocks(*_get_trade_account()),
                                                           account=trade_account_key())
                msg += "\n\n" + _pkg().format_holdings_block(valid_holdings, analysis_results=analysis_results)
            else:
                msg += "\n\n📋 [보유 종목] 없음"
                if stock_eval_amt > 0:
                    msg += " (⚠️ 평가금액 존재 - API 데이터 불일치)"

            target_cano = config.session.auto_cano
            with utils.AccountContext(target_cano):
                from modules.telegram_bot import TelegramCommander
                reply_markup = TelegramCommander()._get_default_keyboard()
                api.send_telegram_message(msg, reply_markup=reply_markup)

            # 초기화에 사용된 데이터는 비워줌
            self.initial_holdings = None
            self.initial_summary = None
            self.initialized = False

        except Exception as e:
            # 매매 루프가 뜨지 못했으면 잠금을 붙들고 있을 이유가 없다.
            # (붙들면 다음 실행 시도가 '이미 실행 중'으로 거부된다)
            if not self.is_running:
                self._release_instance_lock()
            logger.error(f"자동매매 시작 실패: {e}")
            if api._is_screen_output_allowed():
                console.print(f"[bold red]자동매매 시작 실패: {e}[/bold red]")
                if config.SCREEN_DEBUG_LEVEL in ["ERROR", "TRACE", "DEBUG"]:
                    console.print(f"[bold red][ERROR] 자동매매 시작 실패: {e}[/bold red]")

            if self.initial_asset > 0:
                target_cano = config.session.auto_cano
                acnt = config.session.auto_acnt_prdt_cd
                account_key = f"{target_cano}-{acnt}"
                saved_msg = " (당일 기준 복원)" if load_daily_initial_asset(account_key) > 0 else " (당일 기준 저장)"
                self.log(f"시스템 시작 자산: {self.initial_asset:,}원{saved_msg}")

    def stop(self, use_status=True):
        if not self.is_running:
            if use_status:
                console.print("\n[yellow]실행 중인 자동매매가 없습니다.[/yellow]")
            return
            
        def _stop_logic():
            self.is_running = False
            _pkg().ConclusionMonitor().stop() # [추가] 체결 감시 모니터 종료
            if self.thread and self.thread is not threading.current_thread():
                self.thread.join(timeout=15) # [수정] 타임아웃 연장 (종목 분석 등 백그라운드 스레드 정상 종료 대기)
            # 루프가 멈춘 뒤에 잠금을 푼다 — 먼저 풀면 정지 중에 다른 인스턴스가 끼어든다.
            self._release_instance_lock()

        if use_status:
            with Progress(
                SpinnerColumn(),
                TextColumn("[progress.description]{task.description}"),
                BarColumn(),
                console=console,
                transient=True
            ) as progress:
                progress.add_task("[cyan]시스템 중단 요청 처리 중...[/cyan]", total=None)
                _stop_logic()
        else:
            _stop_logic()

        if self.thread and self.thread.is_alive() and self.thread is not threading.current_thread():
            if use_status:
                console.print("\n[bold red]경고: 시스템 트레이딩 스레드가 응답하지 않습니다. (DB/API 작업 지연)[/bold red]")
                console.print("[dim]강제로 중단 절차를 진행합니다. 일부 데이터가 누락될 수 있습니다.[/dim]")

        if use_status:
            console.print("\n[red]자동매매 시스템이 중단되었습니다.[/red]")
            # 정상 정지 완료는 ERROR가 아님 → 진단(TRACE/DEBUG)에서만 중립 색으로 표기
            if config.SCREEN_DEBUG_LEVEL in ["TRACE", "DEBUG"]:
                console.print("[dim]시스템 트레이딩 정지 완료[/dim]")
            
        self.log("시스템 중단")
        
        # [수정] 텔레그램 전송 시 AUTO 계좌 정보가 포함되도록 컨텍스트 설정
        msg = f"⚪️ [시스템 종료] 자동매매가 종료되었습니다.\n시작 자산: {self.initial_asset:,}원"
        
        # [수정] 스레드가 종료된 경우에만 자산 및 보유 종목 조회 (락 충돌 방지)
        if not self.thread or not self.thread.is_alive():
            # [추가] 종료 시 최종 자산 현황 요약 전송
            deposit = 0
            stock_eval = 0
            final_asset = 0
            is_data_valid = False # [추가] 데이터 유효성 플래그

            target_cano = config.session.auto_cano
            with utils.AccountContext(target_cano):
                try:
                    # 1. 예수금 조회
                    acnt = config.session.auto_acnt_prdt_cd
                    res = api.get_deposit_balance(target_cano, acnt)
                    if res:
                        # [수정] 자산 계산 시 D+2 예수금(가수도금) 사용 (매도 대금 포함) - start()와 통일
                        # [Fix] 주문가능금액(d2_deposit)이 아닌 실제 D+2 가수도금(d2_real)을 사용하여 50원 오차 등 왜곡 방지
                        d2_val = res.get('d2_real', 0)
                        if d2_val == 0:
                            d2_val = res.get('d2_deposit', 0)
                        deposit = d2_val + res.get('foreign_deposit', 0)
                        is_data_valid = True
                    else:
                        deposit = 0

                    # 2. 잔고 및 평가금 조회
                    holdings, summary = api.get_domestic_balance(target_cano, acnt)
                    
                    tot_profit = 0
                    tot_pchs = 0
                    unmanaged_count = 0     # 정지로 감시가 끊기는 포지션 수

                    if holdings:
                        valid_holdings = [h for h in holdings if int(h.get('hldg_qty', 0)) > 0]
                        unmanaged_count = len(valid_holdings)
                        stock_eval = sum(int(h['evlu_amt']) for h in valid_holdings)
                        tot_profit = sum(int(h['evlu_pfls_amt']) for h in valid_holdings)
                        tot_pchs = sum(int(int(h['hldg_qty']) * float(h['pchs_avg_pric'])) for h in valid_holdings)
                    elif summary and len(summary) > 0:
                        stock_eval = api.safe_int(summary[0].get('scts_evlu_amt', 0))
                        tot_profit = api.safe_int(summary[0].get('evlu_pfls_smtl_amt', 0))
                        tot_pchs = api.safe_int(summary[0].get('pchs_amt_smtl', 0))

                    if is_data_valid:
                        final_asset = deposit + stock_eval
                        # 기준선은 입출금 보정을 담아 float 로 온다. 금액 표시는 원 단위이므로
                        #  여기서 정수로 되돌린다 — 안 그러면 '+100,000.0원'이 나간다.
                        _base = self.daily_pnl_base()
                        profit = int(round(final_asset - _base))
                        profit_rate = 0.0 if _base <= 0 else (profit / _base) * 100
                        stock_rate = (tot_profit / tot_pchs * 100) if tot_pchs > 0 else 0.0

                        msg += f"\n최종 예수금: {deposit:,}원"
                        msg += f"\n증권 평가 자산: {stock_eval:,}원"
                        msg += f"\n증권 평가 손익: {tot_profit:+,}원 ({stock_rate:+.2f}%)"
                        msg += f"\n금일 최종 손익: {profit:+,}원 ({profit_rate:+.2f}%)"
                    else:
                        msg += "\n(⚠️ 종료 시 자산 정보 조회 실패 - 서버 응답 없음)"

                    # [안전장치] 정지는 매도 감시 루프까지 함께 끈다. 이 코드베이스는 같은
                    #  이유로 일일 손실 한도 초과와 Kill Switch를 '정지'에서 '방어 모드'로
                    #  바꿨다(engine.check_loss_limit 주석: "정지는 포지션을 청산하지 않고
                    #  매도 감시 루프까지 함께 끄기 때문에 무방비 상태가 된다").
                    #  명시적 정지까지 막을 일은 아니다 — 다만 무엇을 껐는지는 알려야 한다.
                    #  종전 종료 알림은 자산만 보고하고 이 사실을 말하지 않았다.
                    #  종목 목록은 붙이지 않는다 — 같은 알림 아래에 '최종 보유 종목 현황'이
                    #  수익률까지 담아 그대로 이어지므로 두 번 나열하는 셈이었다.
                    if unmanaged_count:
                        msg += (f"\n\n⛔ 보유 {unmanaged_count}종목의 "
                                f"손절·트레일링 감시가 함께 멈춥니다.")

                    # [추가] 금일 매매 요약 집계
                    buy_cnt = 0
                    sell_cnt = 0
                    best_stock = None
                    worst_stock = None
                    max_p = 0
                    min_p = 0
                    try:
                        today_str = datetime.now().strftime("%Y-%m-%d")
                        target_account = (f"{config.session.auto_cano}-{config.session.auto_acnt_prdt_cd}" if config.session.auto_cano else None)
                        today_trades = db_manager.db.get_trades(start_date=today_str, end_date=today_str, is_sim=False, account=target_account)
                        
                        today_trades_parsed = []
                        for r in reversed(today_trades):
                            simple_type = "buy" if "매수" in r['type'] or "buy" in r['type'].lower() else "sell"
                            parsed_r = dict(r)
                            parsed_r['type'] = simple_type
                            today_trades_parsed.append(parsed_r)
                            
                        today_trades_refined = self._refine_trade_records(today_trades_parsed)
                        # [추가] 체결된 내역만 당일 매매 요약에 포함
                        today_trades_refined = [r for r in today_trades_refined if "체결" in r.get('order_status', '')]
                        
                        buy_cnt = len([x for x in today_trades_refined if x['type'] == 'buy'])
                        sell_cnt = len([x for x in today_trades_refined if x['type'] == 'sell'])
                        
                        stock_profits = {}
                        for t in today_trades_refined:
                            if t['type'] == 'sell':
                                code = t.get('code', 'unknown')
                                name = t.get('name', 'Unknown')
                                p_amt = int(float(t.get('profit_amt') or 0))
                                if code not in stock_profits:
                                    stock_profits[code] = {'name': name, 'profit': 0}
                                stock_profits[code]['profit'] += p_amt
                                
                        for code, info in stock_profits.items():
                            if info['profit'] > max_p:
                                best_stock = info
                                max_p = info['profit']
                            if info['profit'] < min_p:
                                worst_stock = info
                                min_p = info['profit']
                    except Exception as e:
                        self.log(f"종료 시 매매 요약 조회 실패: {e}")
                        
                    msg += f"\n오늘 매매 요약: 매수 {buy_cnt}건 / 매도 {sell_cnt}건"
                    if best_stock:
                        msg += f"\n최고 수익: {best_stock['name']} (+{max_p:,}원)"
                    if worst_stock:
                        msg += f"\n최대 손실: {worst_stock['name']} ({min_p:,}원)"

                    # [수정] 보유수량 0 초과인 종목만 필터링
                    valid_holdings = [h for h in holdings if int(h.get('hldg_qty', 0)) > 0] if holdings else []

                    if valid_holdings:
                        from modules import account
                        analysis_results = account.run_holding_analysis(valid_holdings, [], _pkg().get_restricted_stocks(*_get_trade_account()),
                                                           account=trade_account_key())
                        msg += "\n\n" + _pkg().format_holdings_block(valid_holdings, title="최종 보유 종목 현황", analysis_results=analysis_results)
                    else:
                        msg += "\n\n📋 [최종 보유 종목] 없음"
                        if not is_data_valid:
                            msg += " (조회 실패 가능성 있음)"
                except Exception as e:
                    self.log(f"종료 시 자산/잔고 조회 실패: {e}")
                    msg += "\n(자산 조회 실패)"
        else:
            msg += "\n(시스템 응답 지연으로 최종 자산 정보 생략)"
            self.log("스레드 종료 지연으로 최종 자산/잔고 조회 생략")

        # [운영 안전] 미체결 주문은 시스템을 꺼도 거래소에 살아 있다. 정지 뒤에 체결되면
        #  손절·트레일링 감시가 전혀 없는 포지션이 된다. 위 '최종 보유 종목'은 정지 시점의
        #  잔고라 그 주문을 보여주지 못하고 "보유 없음"으로 끝나므로, 운용자는 무감시
        #  포지션이 생겼다는 것을 알 방법이 없다. 정지 시점에 명시적으로 알린다.
        try:
            pending = []
            om = getattr(self, 'order_manager', None)
            if om is not None:
                with om._lock:
                    for _code, _orders in (om.pending_orders or {}).items():
                        if _orders:
                            pending.append((_code, len(_orders)))
            if pending:
                total = sum(n for _, n in pending)
                detail = ", ".join(f"{c}({n}건)" for c, n in pending)
                msg += (f"\n\n⚠️ [미체결 주문 {total}건 잔존] {detail}\n"
                        f"시스템을 꺼도 거래소의 주문은 살아 있습니다. 정지 후 체결되면 "
                        f"손절·트레일링 감시가 없는 포지션이 됩니다 — 증권사 앱에서 "
                        f"취소하거나 시스템을 다시 켜 주십시오.")
        except Exception as e:
            self.log(f"종료 시 미체결 주문 확인 실패: {e}")

        target_cano = config.session.auto_cano
        with utils.AccountContext(target_cano):
            from modules.telegram_bot import TelegramCommander
            reply_markup = TelegramCommander()._get_default_keyboard()
            api.send_telegram_message(msg, reply_markup=reply_markup)
        
        # [추가] 로거 연결 해제 (메시지 전송 후 해제)
        context.SYSTEM_LOGGER = None

    def halt_buys(self, reason, notify_msg=None, kind=None):
        """[안전장치] 방어 모드 진입 — 신규 매수·피라미딩만 중단하고 청산 감시는 유지한다.

        [추세추종 원칙] "어떤 전략을 쓰든 손절을 하지 않으면 언젠가는 계좌가 심각한 타격을 입는다."
        일일 손실 한도 초과 = 이미 여러 포지션이 손절선 근처라는 뜻이므로, 이때 시스템을 통째로
        정지(stop)하면 남은 포지션이 손절선을 뚫고 내려가도 아무도 팔지 않는 무방비 상태가 된다.
        따라서 '노출을 늘리는 행위'만 막고 매도/손절/트레일링 스탑 감시는 그대로 돌린다.

        날짜가 바뀌면(당일 시작 자산 재측정) 자동 해제된다. 즉시 해제는 resume_buys()를 쓴다.
        """
        today = datetime.now().date()
        if self.buy_halted and self.buy_halt_date == today:
            return False  # 이미 같은 날 발동 중 — 중복 알림 방지

        with self._lock:
            self.buy_halted = True
            self.buy_halt_reason = reason
            self.buy_halt_date = today
            #  발동 원인의 종류. 사유 문자열을 되읽지 않고 이 값으로 판단한다 —
            #  문구가 바뀌면 조용히 어긋나는 종류의 결합을 만들지 않는다.
            self.buy_halt_kind = kind

        self.log(f"[방어 모드] 신규 매수 중단: {reason} (매도·손절 감시는 계속됩니다)")
        if notify_msg:
            api.send_telegram_message(notify_msg)
        return True

    def resume_buys(self, reason="수동 해제"):
        """방어 모드 해제 — 신규 매수를 재개한다."""
        if not self.buy_halted:
            return False
        with self._lock:
            self.buy_halted = False
            self.buy_halt_reason = ""
            self.buy_halt_date = None
            self.buy_halt_kind = None
        self.log(f"[방어 모드 해제] 신규 매수를 재개합니다. ({reason})")
        return True

    def effective_baseline(self):
        """리스크 판정에 쓸 **오늘의 자본 기준선** = 시작 자산 + 오늘 누적 순입출금.

        [왜 파생값인가] 종전에는 입출금이 감지되면 initial_asset 자체를 옮겼다. 그러려면
        3주기 확인·자동 조정 상한·파일 저장이 필요했고, 상한을 넘으면 사람이 손대기 전까지
        **기준이 틀린 채로 하루가 갔다**(출금이면 일일 손실 한도가 종일 헛발동한다).

        순입출금은 원금 불변량(현금+매입원가-실현손익)의 변화라 매 주기 정확히 다시 잴 수
        있다. 저장하지 않고 판정할 때마다 재면 상한도 대기도 필요 없고, 스냅샷이 한 번
        튀어도 다음 주기에 저절로 낫는다 — 잘못된 값이 굳지 않는다.

        [옮기지 않는다] 입출금이 감지돼도 initial_asset·baseline_principal 은 그대로 둔다.
        옮기면 이 파생값이 0이 되어 같은 결과가 나오지만, 옮기는 쪽은 되돌릴 수 없고
        추정이 틀리면 잘못된 기준이 그대로 굳는다. 여러 날에 걸친 드로다운 기준도 이력을
        고치지 않고 daily_asset_history.net_transfer 로 환산한다(get_max_daily_asset).
        """
        # 산식은 RiskManager._equity_baseline 이 단독 보유한다 — 차단기·사이징과
        #  갈라지면 세 장치가 서로 다른 자본을 보게 된다.
        return self.risk_manager._equity_baseline()

    def daily_pnl_base(self):
        """표시용 일일 손익의 분모. **판정과 같은 기준선**을 쓴다(입출금 보정 포함).

        [왜] 출금은 손실이 아니다. 원본 시작 자산으로 나누면 1,000만 계좌에서 300만을 뺀
        순간 화면·텔레그램·종료 요약이 모두 -30%를 띄운다. 판정(차단기·사이징)은 이미
        보정된 기준선을 보는데 표시만 안 보면, 운용자는 시스템이 못 본 손실이 난 줄 알고
        개입하게 된다 — 자동으로 도는 시스템에서 가장 나쁜 종류의 오표시다.
        """
        base = self.effective_baseline()
        return float(base) if base > 0 else float(self.initial_asset or 0)

    def transfer_note(self):
        """오늘 순입출금이 있으면 손익 옆에 붙일 꼬리말. 없으면 빈 문자열."""
        net = getattr(self, 'net_transfer_today', 0) or 0
        return f" ※{'입금' if net > 0 else '출금'} {abs(int(net)):,}원 제외" if net else ""

    @staticmethod
    def _offline_transfer_threshold(baseline_principal):
        """오프라인 입출금으로 확정할 최소 금액. (상수 주석 참조)"""
        base = abs(float(baseline_principal or 0))
        return max(OFFLINE_TRANSFER_FLOOR,
                   min(OFFLINE_TRANSFER_ABS_MIN, base * OFFLINE_TRANSFER_RATIO))

    def _realized_matches_broker(self, start_date, end_date, realized_db, tolerance):
        """구간 실현손익을 **우리가 다 알고 있는가**를 증권사 장부와 대조한다.

        [왜 필요한가] 우리 DB는 우리가 아는 매매만 담는다. 기동 동기화(holdings_backfill)는
         **지금 보유 중인 종목**을 역산해 채우므로, 시스템이 꺼진 사이에 사서 그 사이에 판
         왕복매매는 기록이 아예 없다(그 모듈의 [한계] 주석). 그러면 그 실현손익이 통째로
         가짜 입출금이 된다 — 손실 쪽이면 자산 고점을 낮춰 **리스크 한도가 조용히 열린다**.
         행이 없으니 '외부 매도' 표식으로도 잡을 수 없다. 증권사 장부만이 답을 안다.

        [값으로 쓰지 않고 대조만 한다] rlzt_pfls 가 제비용을 포함하는지가 계좌·응답에 따라
         다르게 관측된다. 그 숫자를 그대로 식에 넣으면 규약이 어긋난 순간 없는 입출금을
         만들어낸다. 그래서 두 해석(제비용 포함/미포함) 중 **하나라도** 우리 합계와 맞으면
         '알고 있다'로 보고 통과시키고, 둘 다 어긋날 때만 보류한다.

        조회 자체가 불가능하면(모의·토스·관찰 모드, 통신 실패) 대조를 건너뛴다 — 이 대조는
        추가 방어막이지 전제 조건이 아니다. 여기서 막으면 정작 필요한 보정까지 사라진다.
        """
        try:
            cano, acnt = _get_trade_account()
            book = account.fetch_period_realized(cano, acnt, start_date, end_date)
            if not book:
                return True                    # 대조 불가 — 기존 판정을 유지한다
            gap = min(abs(realized_db - book['realized']),
                      abs(realized_db - (book['realized'] - book['cost'])))
            if gap <= max(tolerance, 1000.0):
                return True
            self.log(f"[오프라인 입출금] 증권사 실현손익({book['realized']:+,}원)과 기록"
                     f"({realized_db:+,}원)이 {gap:,.0f}원 어긋납니다 — 우리가 모르는 매매가 "
                     f"있어 보정을 보류합니다. (수동 매매 기록은 보유 종목만 복원됩니다)")
            return False
        except Exception as e:
            logger.debug(f"[오프라인 입출금] 증권사 실현손익 대조 실패(보정은 계속): {e}")
            return True

    def _reconcile_offline_transfer(self, account_key, current_principal, realized_ok):
        """프로그램이 꺼져 있던 사이의 입출금을 스스로 되찾아 자산 이력에 적는다.

        [왜 필요한가] 장중 감지(_monitor_account_status)는 **그날 기준 원금이 잡힌 뒤**의
         변화만 본다. 프로그램이 꺼진 사이의 입출금은 잴 주체가 없어 영영 기록되지 않고,
         출금이면 옛 자산이 그대로 고점으로 남아 룩백(DD_LOOKBACK_DAYS, 90일) 내내
         가짜 드로다운이 리스크 한도를 묶는다.
         [실사고 2026-08-31] 계좌 44048158-01에서 프로그램이 꺼져 있던 7/28~8/4 사이에
          약 1만원이 빠졌다. 07-27의 10,027원이 고점으로 남아 드로다운이 99.7%로
          계산됐고(현재 27원), 운용자가 DB를 직접 고치기 전에는 풀리지 않았다.

        [식] 원금(현금+매입원가-실현손익)은 입출금이 없으면 그 사이의 실현손익만큼만
         움직인다. 따라서
             순입출금 = (오늘 원금) - (마지막으로 남긴 원금) - (그 사이의 실현손익)
         오늘의 실현손익은 양변에서 상쇄되므로 합산 구간은 [그날, 어제]다.

        [어느 행에 적는가] 마지막 스냅샷을 남긴 그날의 행이다. 자산 행은 그날 '시작'
         스냅샷이고 환산식은 'd일 이후에 일어난 입출금'을 d의 자산에 더하므로
         (get_max_daily_asset), 그 이후에 일어난 이 입출금은 그 행에 실려야 한다.
         오늘 행에 적으면 이미 반영된 오늘 자산에 한 번 더 더해져 이중 계산이 된다.

        [모르면 안 건드린다] 실현손익을 못 쟀거나(realized_ok=False), 대조점이 거래
         보존 기간 밖이면(그 사이 매도 기록이 지워져 실현손익 합이 잘린다) 아무것도
         하지 않는다. 잘린 실현손익은 그대로 가짜 '출금'이 되고, 출금 방향의 오탐은
         고점을 낮춰 **한도를 여는** 위험한 방향이다.

        반환: 기록한 금액(입금 +, 출금 −). 아무것도 안 했으면 0.
        """
        if not realized_ok or not current_principal or current_principal <= 0:
            return 0
        try:
            today = datetime.now().strftime("%Y-%m-%d")
            # [하루 1회] 시세 결손 등으로 그날 기준 자산이 끝내 안 잡히면 이 블록이 매 주기
            #  다시 돌 수 있다. 그러면 같은 입출금을 주기마다 가산한다 — 오늘 대조점을 남기는
            #  것만으로는 못 막는다(대조점은 기준 자산이 잡혀야 저장되기 때문).
            if getattr(self, '_offline_reconcile_date', None) == today:
                return 0
            snap = db_manager.db.get_last_principal_snapshot(account_key, today)
            if not snap:
                return 0                       # 대조점 없음 = 이 계좌의 첫 운용
            last_date, last_principal = snap
            if last_principal <= 0:
                return 0
            if last_date >= today:
                return 0                       # 오늘 이미 대조를 끝냈다(같은 날 재기동).
                                               #  오늘 이후의 변화는 장중 감지가 맡는다 —
                                               #  여기서 또 재면 같은 입출금을 두 번 적는다.

            retention = int(getattr(config, 'DB_DATA_RETENTION_DAYS', 365) or 365)
            gap_days = (datetime.now() - datetime.strptime(last_date, "%Y-%m-%d")).days
            if retention > 0 and gap_days > retention - 7:
                self.log(f"[오프라인 입출금] 마지막 대조점({last_date})이 거래 보존 기간 밖이라 "
                         f"보정을 건너뜁니다 — 실현손익 합을 신뢰할 수 없습니다.")
                return 0

            yesterday = (datetime.now() - timedelta(days=1)).strftime("%Y-%m-%d")
            realized, ok = db_manager.db.get_realized_profit_between(
                last_date, yesterday, account_key)
            if not ok:
                self.log("[오프라인 입출금] 구간 실현손익을 신뢰할 수 없어 보정하지 않습니다.")
                return 0          # 못 쟀으면 표시하지 않는다 — 다음 주기에 다시 해 본다

            threshold = self._offline_transfer_threshold(last_principal)
            if not self._realized_matches_broker(last_date, yesterday, realized, threshold):
                return 0
            self._offline_reconcile_date = today      # 쟀다 = 오늘 몫은 끝났다

            residual = float(current_principal) - float(last_principal) - float(realized)
            if abs(residual) < threshold:
                return 0

            amount = int(round(residual))
            if not db_manager.db.add_net_transfer(last_date, account_key, amount):
                return 0                       # 그날 행이 없다 = 적을 자리가 없다

            self._hwm_cache_date = None        # 환산이 바뀌었으니 드로다운을 다시 잰다
            action_str = "입금" if amount > 0 else "출금"
            self.log(f"💰 정지 중 발생한 예수금 {action_str} 자동 반영: {amount:+,}원 "
                     f"(대조점 {last_date}, 구간 실현손익 {int(realized):+,}원)")
            try:
                api.send_telegram_message(
                    f"💰 [정지 중 {action_str} 자동 반영]\n"
                    f"프로그램이 꺼져 있던 사이({last_date} 이후)의 {action_str} "
                    f"약 {abs(amount):,}원을 확인해 자산 이력에 반영했습니다.\n\n"
                    f"✅ 드로다운 기준(자산 고점)이 이 금액을 빼고 계산되므로 "
                    f"조치할 것은 없습니다.\n"
                    f"(대조: {last_date} 원금 {int(last_principal):,}원 → 현재 "
                    f"{int(current_principal):,}원, 그 사이 실현손익 {int(realized):+,}원)")
            except Exception:
                pass
            return amount
        except Exception as e:
            logger.warning(f"[오프라인 입출금] 보정 실패 — 그대로 둡니다: {e}")
            return 0

    def _reevaluate_buy_halt_after_transfer(self, current_total, action_str="입출금"):
        """입출금으로 기준 자산을 옮긴 뒤, 방어 모드를 새 기준으로 다시 잰다.

        [왜] 출금은 기준 자산이 정정되기 **전** 3주기 동안 그대로 '손실'로 보인다. 그 사이
        일일 손실 한도가 걸리면 방어 모드가 켜지는데, 기준을 고쳐도 그것은 날짜가 바뀔
        때까지 풀리지 않았다(halt_buys는 당일 재발동만 막는다). 즉 **정상적인 출금 한 번이
        그날 신규 진입을 통째로 멈췄다.** 계좌 잔고가 수시로 변하는 실계좌에서는 흔한 일이다.

        일일 손실로 걸린 방어 모드만 다시 잰다 — 다른 사유(수동·장애)까지 풀면 안 된다.
        새 기준으로도 한도를 넘으면 그대로 둔다. 풀었는데 다음 주기에 진짜로 넘으면
        차단기가 다시 건다(같은 날 재발동은 halt_buys가 허용한다 — 해제로 날짜가 지워진다).
        """
        if getattr(self, 'buy_halt_kind', None) != 'daily_loss':
            return False
        try:
            limit_pct = getattr(config, 'SYSTEM_DAILY_LOSS_LIMIT', 10.0)
            # 차단기와 **같은 기준선**을 써야 한다. initial_asset을 직접 보면, 기준을 옮기지
            #  않고 순입출금으로만 보정되는 경로(대부분의 경우)에서 영영 풀리지 않는다.
            baseline = self.effective_baseline()
            if baseline <= 0 or current_total <= 0:
                return False
            new_rate = (current_total - baseline) / baseline * 100.0
            if new_rate <= -limit_pct:
                return False        # 새 기준으로도 한도 초과 — 그대로 둔다
            self.resume_buys(f"{action_str} 반영으로 기준 자산 정정 "
                             f"(손익률 {new_rate:+.2f}% / 한도 -{limit_pct}%)")
            api.send_telegram_message(
                f"✅ [방어 모드 해제] {action_str}을 기준 자산에 반영했습니다.\n"
                f"다시 잰 손익률 {new_rate:+.2f}% (한도 -{limit_pct}%) — 신규 매수를 재개합니다.")
            return True
        except Exception as e:
            logger.debug(f"[입출금] 방어 모드 재평가 실패: {e}")
            return False

    def log_current_holdings(self):
        """현재 보유 종목 현황을 조회하여 로그에 출력합니다 (체결 후 호출용)"""
        try:
            target_cano = config.session.auto_cano
            acnt = config.session.auto_acnt_prdt_cd
            
            with utils.AccountContext(target_cano):
                holdings, _ = api.get_domestic_balance(target_cano, acnt)
                valid_holdings = [h for h in holdings if int(h.get('hldg_qty', 0)) > 0]
                
                def get_display_width(s):
                    return len(s) + sum(1 for c in s if ord(c) > 127)

                def pad(s, width, align='>'):
                    real_len = get_display_width(s)
                    pad_len = width - real_len
                    if pad_len < 0: pad_len = 0
                    if align == '<': return s + ' ' * pad_len
                    else: return ' ' * pad_len + s

                max_name_width = 20
                if valid_holdings:
                    for item in valid_holdings:
                        name = f"{item['prdt_name']} ({item['pdno']})"
                        w = get_display_width(name)
                        if w > max_name_width:
                            max_name_width = w
                
                name_col_width = max(30, max_name_width + 2)
                line_length = name_col_width + 95

                header = (
                    f"{pad('종목명', name_col_width, '<')} "
                    f"{pad('보유수량', 10, '>')} "
                    f"{pad('매입단가', 12, '>')} "
                    f"{pad('현재가', 12, '>')} "
                    f"{pad('매입금액', 15, '>')} "
                    f"{pad('평가금액', 15, '>')} "
                    f"{pad('평가손익', 14, '>')} "
                    f"{pad('수익률', 10, '>')}"
                )
                
                self.log("")
                self.log("─" * line_length)
                self.log(header)
                self.log("─" * line_length)
                
                if not valid_holdings:
                    self.log(f"{pad('보유 종목 없음', name_col_width, '<')} ")
                else:
                    for item in valid_holdings:
                        name = f"{item['prdt_name']} ({item['pdno']})"
                        qty = int(item['hldg_qty'])
                        buy_price = float(item['pchs_avg_pric'])
                        cur_price = int(item['prpr'])
                        # 매입금액: 실전 잔고(INQR_DVSN=01)·토스 어댑터는 pchs_amt가 0/누락으로 오므로
                        # 합계 줄·잔고 화면과 동일하게 평단×수량으로 복원한다.
                        pchs_amt = api.safe_int(item.get('pchs_amt')) or int(qty * buy_price)
                        eval_amt = int(item.get('evlu_amt', 0))
                        profit = int(item['evlu_pfls_amt'])
                        rate = float(item['evlu_pfls_rt'])

                        row_str = f"{pad(name, name_col_width, '<')} {pad(f'{qty:,}주', 10, '>')} {pad(f'{buy_price:,.0f}원', 12, '>')} {pad(f'{cur_price:,.0f}원', 12, '>')} {pad(f'{pchs_amt:,}원', 15, '>')} {pad(f'{eval_amt:,}원', 15, '>')} {pad(f'{profit:+,}원', 14, '>')} {pad(f'{rate:.2f}%', 10, '>')}"
                        self.log(row_str)
                self.log("─" * line_length)
                self.log("")
        except Exception as e:
            self.log(f"보유 종목 로깅 실패: {e}")

    def get_status_message(self):
        """텔레그램 전송용 상태 요약 메시지 생성"""
        status_text = "STOPPED"
        status_icon = "🔴"
        if self.is_running:
            if self.is_market_open():
                status_text = "RUNNING"
                status_icon = "🟢"
            else:
                if is_single_price_break():
                    status_text = "WAITING (휴게 시간 대기)"
                elif api.is_holiday_today() or datetime.now().weekday() > 4:
                    status_text = "WAITING (공휴일/주말 휴장)"
                else:
                    status_text = "WAITING"
                status_icon = "🟡"
        
        msg = f"{status_icon} [시스템 상태: {status_text}]\n"

        # [안전장치] 방어 모드 표시 — 청산은 계속 돌고 신규 진입만 막혀 있음을 명확히 알린다.
        if self.is_running and getattr(self, 'buy_halted', False):
            msg += f"🛑 방어 모드: 신규 매수 중단 ({self.buy_halt_reason})\n   └ 매도·손절·트레일링 스탑 감시는 정상 동작 중\n"

        # 자산 정보 조회
        current_asset = None
        deposit = 0
        holdings = []

        target_cano = config.session.auto_cano
        with utils.AccountContext(target_cano):
            try:
                acnt = config.session.auto_acnt_prdt_cd
                
                # 1. 잔고 조회 (평가금 포함)
                holdings, summary = api.get_domestic_balance(target_cano, acnt)
                
                # 2. 예수금 및 총 자산 계산
                res = api.get_deposit_balance(target_cano, acnt)
                if res:
                    d2_val = res.get('d2_real', 0)
                    if d2_val == 0: d2_val = res.get('d2_deposit', 0)
                    deposit = d2_val
                
                # [수정] 보유 종목 개별 합산으로 평가금액 직접 계산 (데이터 정합성 보장)
                tot_evlu = 0
                if holdings:
                    valid_holdings = [h for h in holdings if int(h.get('hldg_qty', 0)) > 0]
                    if valid_holdings:
                        tot_evlu = sum(int(h['evlu_amt']) for h in valid_holdings)
                elif summary:
                    tot_evlu = api.safe_int(summary[0].get('scts_evlu_amt', 0))
                
                current_asset = deposit + tot_evlu
            except Exception: pass

        if current_asset is not None:
            tot_profit = 0
            tot_pchs = 0
            
            # [수정] API 요약 데이터 대신 보유 종목 합산 (데이터 불일치 방지)
            if holdings:
                valid_holdings = [h for h in holdings if int(h.get('hldg_qty', 0)) > 0]
                if valid_holdings:
                    tot_pchs = sum(int(int(h['hldg_qty']) * float(h['pchs_avg_pric'])) for h in valid_holdings)
                    tot_profit = sum(int(h['evlu_pfls_amt']) for h in valid_holdings)
            
            rate = (tot_profit / tot_pchs * 100) if tot_pchs > 0 else 0.0
            
            # [추가] 메모리에 초기 자산이 없으면 당일 백업 파일에서 복구 시도
            if self.initial_asset <= 0:
                account_key = f"{target_cano}-{acnt}"
                saved_initial = load_daily_initial_asset(account_key)
                if saved_initial > 0:
                    self.initial_asset = saved_initial
            
            if self.initial_asset > 0:
                msg += f"오늘 시작 자산: {self.initial_asset:,}원\n"
            else:
                msg += f"오늘 시작 자산: - (미설정)\n"
                
            msg += f"오늘 현재 자산: {current_asset:,}원\n"
            
            if self.initial_asset > 0:
                _base = self.daily_pnl_base()
                daily_profit = int(round(current_asset - _base))   # 기준선은 float 다 (원 단위로 표시)
                daily_profit_rate = (daily_profit / _base) * 100 if _base > 0 else 0.0
                msg += (f"오늘 현재 손익: {daily_profit:+,}원 "
                        f"({daily_profit_rate:+.2f}%){self.transfer_note()}\n")
                
            realized_profit = 0
            try:
                today_str = datetime.now().strftime("%Y-%m-%d")
                
                target_account = None
                if config.session.auto_cano:
                    target_account = f"{config.session.auto_cano}-{config.session.auto_acnt_prdt_cd}"
                    
                today_trades = db_manager.db.get_trades(
                    start_date=today_str, end_date=today_str, 
                    is_sim=False, account=target_account
                )
                
                today_trades_parsed = []
                for r in reversed(today_trades):
                    type_str = r['type']
                    simple_type = "buy" if "매수" in type_str or "buy" in type_str.lower() else "sell"
                    parsed_r = dict(r)
                    parsed_r['type'] = simple_type
                    today_trades_parsed.append(parsed_r)
                
                today_trades_refined = self._refine_trade_records(today_trades_parsed)
                # [추가] 체결된 내역만 당일 매매 요약에 포함
                today_trades_refined = [r for r in today_trades_refined if not r.get('order_status') or "체결" in r.get('order_status', '')]
                sell_trades = [x for x in today_trades_refined if x['type'] == 'sell']
                realized_profit = sum(int(t.get('profit_amt') or 0) for t in sell_trades)
            except Exception: pass
            
            _base = self.daily_pnl_base()
            realized_rate = (realized_profit / _base * 100) if _base > 0 else 0.0
            msg += f"오늘 실현 손익: {realized_profit:+,}원 ({realized_rate:+.2f}%)\n"
            msg += f"주문 가능 금액: {deposit:,}원\n"
            msg += f"증권 평가 자산: {tot_evlu:,}원\n"
            msg += f"증권 평가 손익: {tot_profit:+,}원 ({rate:+.2f}%)\n"
        else:
            msg += "자산 정보 조회 실패\n"
            
        # [시장 지수 및 필터링 데이터 준비]
        use_filter = getattr(config, 'USE_MARKET_FILTER', True)
        filter_str = "ON" if use_filter else "OFF"
        filter_ma = getattr(config, 'MARKET_FILTER_MA', 80)
        filter_band = getattr(config, 'MARKET_FILTER_BAND', 1.0)
        band_txt = f" ±{filter_band:g}%" if filter_band else ""

        is_healthy_k = True
        is_healthy_q = True
        
        market_idx_msgs = []
        market_flt_msgs = []

        try:
            for name, m_type in [("KOSPI", "KOSPI"), ("KOSDAQ", "KOSDAQ")]:
                df = analysis.get_domestic_index_data(m_type)
                if df is not None and not df.empty:
                    curr = df.iloc[-1]['close']
                    prev = df.iloc[-2]['close'] if len(df) > 1 else curr
                    rate = ((curr - prev) / prev) * 100
                    market_idx_msgs.append(f"• {name}: {curr:,.2f} ({rate:+.2f}%)")
                    
                    filter_status = "허용"
                    # 시스템 루프의 상태 캐시(market_index_status)를 우선 적용
                    cached_stat = self.market_index_status.get(m_type)
                    
                    if isinstance(cached_stat, dict) and cached_stat.get('unknown'):
                        is_healthy = False
                        filter_status = "보류 (판단불가)"
                    elif cached_stat and isinstance(cached_stat, dict) and cached_stat.get('current', 0) > 0:
                        is_healthy = cached_stat.get('is_healthy', True)
                        filter_status = "허용" if is_healthy else "보류"
                    else:
                        ma_period = getattr(config, 'MARKET_FILTER_MA', 80)
                        if len(df) >= ma_period:
                            is_healthy = not bool(indicators.get_market_filter_blocked(
                                df['close'], ma_period,
                                getattr(config, 'MARKET_FILTER_BAND', 1.0)).iloc[-1])
                            filter_status = "허용" if is_healthy else "보류"
                        else:
                            is_healthy = False
                            filter_status = "보류 (데이터부족)"
                            
                    if m_type == "KOSPI":
                        is_healthy_k = is_healthy
                    elif m_type == "KOSDAQ":
                        is_healthy_q = is_healthy
                        
                    market_flt_msgs.append(f"• {name}: {filter_status}")
                else:
                    # [fail-closed] 지수를 못 읽으면 표시도 '보류(판단불가)'다. 종전에는
                    #  화면만 '확인 불가'로 찍고 is_healthy_* 는 기본값 True 로 남아,
                    #  아래 보류 종목 수 집계와 이 줄이 서로 다른 말을 했다.
                    if m_type == "KOSPI":
                        is_healthy_k = False
                    elif m_type == "KOSDAQ":
                        is_healthy_q = False
                    market_idx_msgs.append(f"• {name}: 확인 불가")
                    market_flt_msgs.append(f"• {name}: 보류 (판단불가)")
        except Exception: pass
        
        # [시장 지수] 섹션 출력
        msg += "\n[시장 지수]\n"
        if market_idx_msgs:
            msg += "\n".join(market_idx_msgs) + "\n"
        else:
            msg += "• 지수 데이터 확인 불가\n"

        # [시장 상황] 섹션 출력
        msg += "\n[시장 상황]\n"
        rp = config.MARKET_REGIME_PARAMS
        ema_desc = f"EMA {rp.get('REGIME_EMA_FAST', 9)}/{rp.get('REGIME_EMA_SLOW', 41)}"

        for m_type, label in [("KOSPI", "KOSPI"), ("KOSDAQ", "KOSDAQ")]:
            try:
                info = analysis.get_market_regime_detail(m_type)
                regime = info['regime']
                # 이모지·라벨 모두 analysis 단일 소스에서 받는다(메뉴 헤더·텔레그램 버튼과 공유).
                regime_str = f"{analysis.regime_emoji(regime)} {analysis.format_regime(regime, markup=False)}"
                msg += f"• {label}: {regime_str} ({info['moved_pct']:+.1f}%, {ema_desc} 기준)\n"
            except Exception:
                msg += f"• {label}: 확인 불가\n"

        # [시장 필터링] 섹션 출력
        #  필터가 꺼져 있으면 판정 결과를 줄줄이 찍지 않는다 — 매수를 막지 않는데
        #  '보류'라고 적히면 오독한다(종전에는 OFF 여도 종목별 상태를 그대로 찍었다).
        msg += f"\n[시장 필터링] ({filter_str}, SMA {filter_ma}일{band_txt} 기준)\n"
        if not use_filter:
            msg += "• 필터 비활성 — 시장 상태와 무관하게 매수를 허용합니다\n"
        elif market_flt_msgs:
            msg += "\n".join(market_flt_msgs) + "\n"
        else:
            msg += "• 필터링 상태 확인 불가\n"

        if use_filter:
            skip_k = self.skipped_by_market_filter_count.get("KOSPI", 0)
            skip_q = self.skipped_by_market_filter_count.get("KOSDAQ", 0)
            
            if (not is_healthy_k and skip_k == 0) or (not is_healthy_q and skip_q == 0):
                calc_k, calc_q = self._get_skipped_stocks_count(holdings)
                if not is_healthy_k and skip_k == 0: skip_k = calc_k
                if not is_healthy_q and skip_q == 0: skip_q = calc_q
            
            skip_msg = []
            if not is_healthy_k or skip_k > 0:
                skip_msg.append(f"KOSPI {skip_k}종목")
            if not is_healthy_q or skip_q > 0:
                skip_msg.append(f"KOSDAQ {skip_q}종목")
                
            if skip_msg:
                msg += f"⚠️ 하락장 방어 중 (현재 {', '.join(skip_msg)} 신규 매수 보류)\n"

        # [수정] 보유수량 0 초과인 종목만 필터링
        valid_holdings = [h for h in holdings if int(h.get('hldg_qty', 0)) > 0] if holdings else []

        if valid_holdings:
            from modules import account
            analysis_results = account.run_holding_analysis(valid_holdings, [], _pkg().get_restricted_stocks(*_get_trade_account()),
                                                           account=trade_account_key())
            msg += "\n" + _pkg().format_holdings_block(valid_holdings, analysis_results=analysis_results)
        else:
            msg += "\n📋 [보유 종목] 없음"
            
        return msg

    @staticmethod
    def _health_time(value):
        """관제 메시지용 시각 포맷. 값이 없을 때도 상태를 명확히 표시한다."""
        if not value:
            return "기록 없음"
        if isinstance(value, datetime):
            return value.strftime("%H:%M:%S")
        return str(value)

    def _record_cycle_duration(self, secs, log=True):
        """[계측] 한 모니터링 주기의 소요 시간을 기록한다.

        SYSTEM_TRADING_INTERVAL은 주기가 끝난 뒤 쉬는 시간이므로, 실제 청산 감시 간격은
        (이 소요 시간 + interval)이다. 관심종목을 늘리면 후보 분석이 길어져 이 값만 커지고,
        그만큼 손절·트레일링 확인이 늦어진다. 유니버스를 어디까지 늘릴 수 있는지는
        수익률이 아니라 이 값이 정한다.

        최근 30회만 보관한다(라즈베리파이 1GB 환경에서 무한 증가 방지).
        """
        try:
            secs = float(secs)
        except (TypeError, ValueError):
            return
        if secs < 0:
            return
        self.last_cycle_secs = secs
        hist = getattr(self, 'cycle_secs_history', None)
        if hist is None:
            hist = self.cycle_secs_history = []
        hist.append(secs)
        if len(hist) > 30:
            del hist[:-30]
        if secs > getattr(self, 'cycle_secs_peak', 0.0):
            self.cycle_secs_peak = secs
        if log:
            interval = getattr(config, 'SYSTEM_TRADING_INTERVAL', 60)
            self.log(f"모니터링 완료 (소요 {secs:.1f}초 · 다음 주기까지 {interval}초 대기 "
                     f"→ 청산 감시 간격 {secs + interval:.0f}초). 대기 중...")

    def loop_stall_seconds(self):
        """마지막 '정상 루프 완료' 이후 흐른 초. 감시 대상이 아니면 None.

        [왜 필요한가 · 2026-09-04] 프로세스 죽음은 두 층이 본다 — 스케줄러가 스레드
         생존을, 밖의 감시자(hts_watchdog)가 하트비트 파일을 본다. 그런데 **루프가
         멈춘 채 스레드는 살아 있는 경우**는 둘 다 못 본다. 하트비트를 찍는 것은
         스케줄러 스레드지 매매 스레드가 아니고, 멈춘 스레드도 is_alive() 는 참이며,
         예외가 안 나므로 연속 에러도 0이다. 그동안 **손절·트레일링 감시가 통째로
         멈춰 있는데 아무 소리도 나지 않는다.**
         종전에도 이 지연은 계산됐지만 `get_status_message()` 안에만 있어, 운영자가
         상태 화면을 열어야 보였다.
        """
        if not self.is_running or getattr(self, 'waiting_for_server', False):
            return None            # 정지·의도된 대기는 정체가 아니다
        last = getattr(self, 'last_success_at', None)
        if not isinstance(last, datetime):
            return None            # 첫 주기 완료 전 — 여기서 판정하지 않는다
        return (datetime.now() - last).total_seconds()

    def loop_stall_threshold(self):
        """이 초를 넘기면 '루프가 멈췄다'로 본다.

        고정값을 쓰면 안 된다 — 관심종목이 늘면 한 주기가 그만큼 길어져(_record_cycle_duration)
        정상인데도 넘긴다. 실제 감시 간격(분석 평균 + 대기)의 5배를 쓰되 하한을 둔다.
        """
        _, gap = self._health_cycle_text()
        interval = getattr(config, 'SYSTEM_TRADING_INTERVAL', 60)
        base = gap if gap else (interval * 2)
        return max(300.0, float(base) * 5)

    def _health_cycle_text(self):
        """관제용 주기 소요 시간 문구와 '실제 감시 간격(초)'을 돌려준다.

        Returns: (표시 문자열, 감시 간격 초 또는 None)
        """
        last = getattr(self, 'last_cycle_secs', None)
        if last is None:
            return "미측정 (루프 1회 실행 후 표시)", None
        hist = getattr(self, 'cycle_secs_history', None) or [last]
        avg = sum(hist) / len(hist)
        interval = getattr(config, 'SYSTEM_TRADING_INTERVAL', 60)
        gap = avg + interval
        peak = getattr(self, 'cycle_secs_peak', 0.0) or last
        return (f"분석 {avg:.1f}초 (최근 {len(hist)}회 평균, 최대 {peak:.1f}초) + 대기 {interval}초 "
                f"= 청산 감시 간격 {gap:.0f}초", gap)

    @staticmethod
    def _peak_rss_mb():
        """프로세스 수명 동안의 최대 상주 메모리(MB). 실패하면 0.

        [왜 피크가 따로 필요한가] 현재 RSS는 '지금 이 순간'의 값이라 한가한 시각에 보면
        낮게 나온다. OOM은 종목 분석이 몰리는 순간에 나는데 그 순간을 사람이 보고 있을
        수는 없다. ru_maxrss는 커널이 대신 기억해 준 고점이라 사후에 확인할 수 있다.

        [주의] 프로세스 수명 기준이므로 재기동하면 0부터 다시 쌓인다 — 갓 띄운 직후의
        피크는 아직 아무것도 말해 주지 않는다. 하루 장을 다 돈 뒤의 값이라야 근거가 된다.
        """
        try:
            import resource
            usage = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
            # macOS는 바이트, 그 외 POSIX는 KB 단위로 반환한다.
            is_mac = getattr(os, "uname", None) and os.uname().sysname == "Darwin"
            return usage / (1024 ** 2) if is_mac else usage / 1024
        except Exception:
            return 0.0

    @staticmethod
    def _health_memory():
        """프로세스/시스템 메모리 사용량(MB)을 추가 의존성 없이 조회한다.

        운영 환경(라즈베리파이 1GB)에서는 OOM이 자동매매 중단의 주된 원인이라
        관제 화면에서 상주 메모리·피크·가용 메모리를 함께 확인할 수 있게 한다.
        조회에 실패하면 0을 돌려주고 해당 항목만 표시에서 빠진다.
        """
        rss_mb = 0.0
        avail_mb = 0.0
        try:
            # 리눅스는 statm의 상주 페이지 수가 가장 정확하고 비용도 낮다.
            with open("/proc/self/statm") as fp:
                pages = int(fp.read().split()[1])
            rss_mb = pages * os.sysconf("SC_PAGE_SIZE") / (1024 ** 2)
        except Exception:
            # statm을 못 읽는 환경(비리눅스)에서는 피크값으로 대신한다 — 현재값보다 크지만
            # 아예 표시하지 못하는 것보다 낫고, OOM 판단에서 과소평가는 위험한 방향이다.
            rss_mb = AutoTrader._peak_rss_mb()
        peak_mb = AutoTrader._peak_rss_mb()
        try:
            with open("/proc/meminfo") as fp:
                for line in fp:
                    if line.startswith("MemAvailable:"):
                        avail_mb = int(line.split()[1]) / 1024
                        break
        except Exception:
            avail_mb = 0.0
        return rss_mb, avail_mb, peak_mb

    def get_health_message(self):
        """외부 API 호출 없이 운영 상태를 요약한 관제 메시지를 만든다.

        /status는 잔고·지수 조회까지 수행하는 상세 화면이고, /health는 장애 상황에서도
        응답할 수 있도록 메모리·로컬 DB·실시간 피드 상태만 사용한다.
        """
        now = datetime.now()
        warnings = []
        risks = []

        if not self.is_running:
            state = "중지"
            icon = "🔴"
            warnings.append("자동매매가 실행 중이 아닙니다")
        elif getattr(self, "buy_halted", False):
            state = "방어 모드"
            icon = "🟠"
            warnings.append(f"신규 매수 중단: {self.buy_halt_reason or '사유 미기록'}")
        elif self.is_market_open():
            state = "운영 중"
            icon = "🟢"
        else:
            state = "대기"
            icon = "🟡"

        max_err = int(getattr(config, "SYSTEM_MAX_CONSECUTIVE_ERRORS", 5) or 5)
        errors = int(getattr(self, "consecutive_errors", 0) or 0)
        if errors:
            (risks if errors >= max_err else warnings).append(
                f"자동매매 루프 연속 오류 {errors}/{max_err}회"
            )

        # 체결 모니터 오류는 주문 안전성과 직결되므로 별도로 노출한다.
        monitor_errors = 0
        try:
            monitor_errors = int(getattr(_pkg().ConclusionMonitor(), "consecutive_errors", 0) or 0)
        except Exception:
            warnings.append("체결 감시 상태를 읽지 못했습니다")
        if monitor_errors:
            (risks if monitor_errors >= max_err else warnings).append(
                f"체결 감시 연속 오류 {monitor_errors}/{max_err}회"
            )

        # 로컬 주문 상태는 API 장애 중에도 확인 가능하다.
        with getattr(self.order_manager, "_lock", threading.RLock()):
            pending_orders = sum(len(v) for v in self.order_manager.pending_orders.values())
        try:
            reserved_orders = len(db_manager.db.get_pending_reserved_orders())
        except Exception:
            reserved_orders = 0
            warnings.append("예약 주문 DB를 읽지 못했습니다")

        # 당일 주문 이력도 로컬 DB에서만 집계한다. 주문번호 기준으로 중복된
        # 접수→체결 행은 한 건으로 정리해 운영자가 실제 주문 흐름을 오해하지 않게 한다.
        today_order_count = today_fill_count = today_cancel_count = 0
        try:
            target_account = f"{config.session.auto_cano}-{config.session.auto_acnt_prdt_cd}"
            today_trades = db_manager.db.get_trades(
                start_date=now.strftime("%Y-%m-%d"), end_date=now.strftime("%Y-%m-%d"),
                is_sim=False, account=target_account,
            )
            refined = self._refine_trade_records(today_trades)
            today_order_count = len(refined)
            today_fill_count = sum("체결" in str(t.get("order_status") or "") for t in refined)
            today_cancel_count = sum("취소" in str(t.get("order_status") or "") for t in refined)
        except Exception:
            warnings.append("당일 주문 이력을 읽지 못했습니다")

        feed_text = "REST 폴링"
        try:
            from brokers import realtime
            feed = realtime.get_feed()
            if getattr(config.session, "is_toss", False):
                feed_text = "토스: REST 폴링 (공식 WS 미지원)"
            elif not getattr(config, "USE_WEBSOCKET", True):
                feed_text = "REST 폴링 (WebSocket 비활성)"
            else:
                thread = getattr(feed, "_thread", None)
                alive = bool(thread and thread.is_alive())
                got_data = bool(getattr(feed, "_got_data", False))
                coverage = feed.coverage() if hasattr(feed, "coverage") else None
                feed_text = "WebSocket 연결·수신 중" if alive and got_data else "WebSocket 재연결/REST 폴백"
                if not (alive and got_data):
                    warnings.append("실시간 시세 WebSocket이 연결 또는 수신 대기 상태입니다")
                if coverage:
                    feed_text += f" (시세 {coverage.get('price_covered', 0)}/{coverage.get('priority', 0)}종목)"
        except Exception:
            warnings.append("실시간 피드 상태를 읽지 못했습니다")

        account = config.session.auto_cano
        #  [Fix 2026-09-05] 마지막 줄이 분기 **밖**에 있어 무엇으로 떴든 항상 "KIS 실전"으로
        #   덮였다. scheduler._heartbeat_context 에서 같은 형태를 2026-09-04 에 고쳤는데
        #   쌍둥이인 이 자리가 남아 있었다 — 관제 첫 화면이 가상투자·토스에서도 'KIS 실전'
        #   이라고 말한다. 두 인스턴스를 함께 돌리는 운용에서 화면이 계좌 성격을 거짓말하면
        #   장애 때 실계좌부터 뒤지게 된다.
        if getattr(config.session, "is_paper", False):
            mode = "가상투자"
        elif getattr(config.session, "is_toss", False):
            mode = "토스 실전"
        else:
            mode = "KIS 실전"

        if self.last_success_at and isinstance(self.last_success_at, datetime):
            age = (now - self.last_success_at).total_seconds()
            if self.is_running and age > max(120, getattr(config, "SYSTEM_TRADING_INTERVAL", 60) * 4):
                warnings.append(f"정상 루프 갱신 지연 {int(age)}초")
        elif self.is_running:
            warnings.append("아직 정상 루프 완료 기록이 없습니다")

        # 언제부터 돌고 있는지는 장애 판단의 기준 시각이라 관제 첫 화면에 함께 둔다.
        if self.is_running and self.start_time:
            elapsed = str(now - self.start_time).split(".")[0]
            run_text = f"{self.start_time.strftime('%Y-%m-%d %H:%M:%S')} (경과 {elapsed})"
        else:
            run_text = "미실행"

        rss_mb, avail_mb, peak_mb = self._health_memory()
        resource_parts = []
        if rss_mb:
            # 피크를 함께 보여야 '지금 여유롭다'가 안심의 근거가 되지 않는다 — OOM은
            #  분석이 몰리는 순간에 나고, 그 순간의 값은 여기 찍히지 않는다.
            mem_text = f"프로세스 메모리 {rss_mb:,.0f}MB"
            if peak_mb > rss_mb:
                mem_text += f" (피크 {peak_mb:,.0f}MB)"
            resource_parts.append(mem_text)
        if avail_mb:
            resource_parts.append(f"가용 메모리 {avail_mb:,.0f}MB")
            # 1GB 라즈베리파이 기준으로 가용 메모리가 이 아래로 떨어지면 OOM 종료 위험이 커진다.
            if avail_mb < 120:
                (risks if avail_mb < 60 else warnings).append(
                    f"가용 메모리 부족 {avail_mb:,.0f}MB (OOM 위험)"
                )
        resource_text = " · ".join(resource_parts) if resource_parts else "확인 불가"

        # [운영 관제] 주기 소요 시간 — 관심종목을 늘릴 때의 실질 상한 지표.
        #  SYSTEM_TRADING_INTERVAL은 '주기가 끝난 뒤 쉬는 시간'이므로 실제 감시 간격은
        #  (소요 시간 + interval)이다. 종목이 늘면 소요 시간만 길어져 손절·트레일링 확인이
        #  그만큼 늦어진다. 추세추종에서 청산은 생명줄이라 여기서 한계를 잡아야 한다.
        cycle_text, cycle_gap = self._health_cycle_text()
        if cycle_gap:
            if cycle_gap >= 300:
                risks.append(f"청산 감시 간격 {int(cycle_gap)}초 (관심종목 축소 또는 주기 간격 단축 필요)")
            elif cycle_gap >= 180:
                warnings.append(f"청산 감시 간격 {int(cycle_gap)}초 (종목 추가 시 주의)")

        # [운영 관제] 리스크 4종은 '지금 얼마나 위험한가'를 판단하는 핵심 수치다. 숫자만 나열하면
        #  운용자가 기준을 외우고 있어야 읽히므로, 각 값을 판단 기준(슬롯 수·히트 한도·기본 배수)
        #  대비로 함께 적는다. 값의 의미(무엇을 뜻하는 금액인지)도 짧게 덧붙인다.
        #  주의: 텔레그램과 공용 문자열이라 rich 마크업을 넣지 않는다(색은 _add_health_rows에서 부여).
        #        또한 CLI 표는 " · "로 줄을 나누므로 각 항목 내부에는 " · "를 쓰지 않는다.
        tracked_cnt = len(getattr(self, 'trailing_stop_cache', {}) or {})
        max_holdings = getattr(config, 'SYSTEM_MAX_HOLDINGS', 4) or 4
        equity = getattr(self, 'current_total_asset', 0) or 0
        heat_amt = getattr(self, 'portfolio_heat_amt', 0.0) or 0.0
        heat_cap_pct = getattr(config, 'SYSTEM_MAX_PORTFOLIO_RISK', 10.0) or 0.0
        risk_scale = getattr(self, 'risk_scale', 1.0) or 1.0
        risk_scale_reason = getattr(self, 'risk_scale_reason', "") or ""

        risk_parts = [f"추적 포지션 {tracked_cnt}/{max_holdings}종목 (손절·트레일링 감시 중)"]
        if equity > 0:
            risk_parts.append(f"현재 자산 {equity:,.0f}원 (예수금+주식평가)")
        else:
            risk_parts.append("현재 자산 미조회 (루프 1회 실행 후 표시)")

        # 히트는 절대 금액보다 '한도를 얼마나 썼는가 / 얼마나 남았는가'가 판단 기준이다.
        #  한도는 실제 매수 게이트가 쓰는 값과 같게 계산한다
        #  (자산 × 한도% × 리스크 배수 — engine.effective_portfolio_cap 참조).
        if heat_cap_pct <= 0:
            risk_parts.append(f"포트폴리오 히트 {heat_amt:,.0f}원 (한도 미사용)")
            risk_parts.append("동시 손절 시 최대손실")
        elif equity <= 0:
            risk_parts.append(f"포트폴리오 히트 {heat_amt:,.0f}원 (한도 {heat_cap_pct:.1f}%)")
            risk_parts.append("동시 손절 시 최대손실, 한도액은 자산 조회 후 산출")
        else:
            heat_limit = equity * heat_cap_pct * min(1.0, risk_scale) / 100.0
            used_pct = (heat_amt / heat_limit * 100.0) if heat_limit > 0 else 0.0
            room = heat_limit - heat_amt
            risk_parts.append(
                f"포트폴리오 히트 {heat_amt:,.0f}원 / 한도 {heat_limit:,.0f}원 ({used_pct:.0f}% 소진)"
            )
            if used_pct >= 100:
                risk_parts.append("동시 손절 시 최대손실, 한도 초과로 신규 매수·피라미딩 차단")
                risks.append(
                    f"포트폴리오 히트 한도 초과 {heat_amt:,.0f}/{heat_limit:,.0f}원 (신규 매수·피라미딩 차단 중)"
                )
            elif used_pct >= 80:
                risk_parts.append(f"동시 손절 시 최대손실, 한도까지 {room:,.0f}원 (임박)")
                warnings.append(f"포트폴리오 히트 한도 {used_pct:.0f}% 소진 (신규 매수 여력 축소)")
            else:
                risk_parts.append(f"동시 손절 시 최대손실, 한도까지 {room:,.0f}원 여유")

        if risk_scale >= 1.0:
            risk_parts.append("리스크 배수 x1.00 (기본 사이징 100%)")
        else:
            risk_parts.append(f"리스크 배수 x{risk_scale:.2f} (신규 매수 예산 {risk_scale * 100:.0f}%로 축소)")
            if risk_scale_reason:
                risk_parts.append(f"축소 사유: {risk_scale_reason[:60]}")

        # [표시 정직성] SYSTEM_RISK_PER_TRADE(명목 한도)는 변동성 타겟팅이 켜져 있는 한
        #  사이징 min 결합에서 한 번도 구속되지 않는다(config.py의 2026-07-27 실측 주석 참조).
        #  명목값만 보이면 "1회 4% 리스크로 돌고 있다"는 오해를 부르므로, 실제로 금액을 결정하는
        #  변동성층 기준의 실효 비중을 함께 적는다. 타겟팅을 끄면 명목 한도가 실효 한도가 된다.
        try:
            _nominal_risk = getattr(config, 'SYSTEM_RISK_PER_TRADE', 4.0) or 0.0
            if getattr(config, 'USE_VOLATILITY_TARGETING', True):
                _eff_ratio = (config.resolve_invest_ratio()
                              * getattr(config, 'VOLATILITY_SCALING_MIN', 0.4))
                _part = f"1회 사이징 기준 비중 약 {_eff_ratio * 100:.1f}% (변동성 타겟팅이 결정)"
                if _nominal_risk > 0:
                    _part += f", 명목 한도 {_nominal_risk:g}%는 미발동"
                risk_parts.append(_part)
            elif _nominal_risk > 0:
                risk_parts.append(f"변동성 타겟팅 OFF — 1회 리스크 한도 {_nominal_risk:g}%가 실효 제한")
        except Exception:
            pass

        lines = [
            f"{icon} [운영 관제 /health: {state}]",
            f"• 모드/계좌: {mode} / {account or '-'}",
            f"• 실행 시간: {run_text}",
            f"• 자동매매 루프: 최근 시작 {self._health_time(self.last_cycle_at)} · 정상 완료 {self._health_time(self.last_success_at)}",
            f"• 주기 소요: {cycle_text}",
            f"• 최근 오류: {self._health_time(self.last_error_at)}" + (f" — {self.last_error_message[:160]}" if self.last_error_message else ""),
            f"• Kill Switch: 자동매매 {errors}/{max_err} · 체결 감시 {monitor_errors}/{max_err}"
            + (f" · [bold red]서버 정상인데 루프 오류 {self.code_error_streaks}회 "
               f"— 원인 확인 필요[/bold red]" if getattr(self, 'code_error_streaks', 0) else ""),
            f"• 주문 감시: 미체결 {pending_orders}건 · 예약 대기 {reserved_orders}건 · 오늘 주문/체결/취소 {today_order_count}/{today_fill_count}/{today_cancel_count}건",
            f"• 시세 연결: {feed_text}",
            f"• 알림 발신: {self._health_telegram_text()}",
            f"• 저장 상태: {self._health_storage_text()}",
            f"• 계좌 차단기: {self._health_circuit_breaker_text()}",
            "• 리스크: " + " · ".join(risk_parts),
            f"• 시스템 자원: {resource_text}",
        ]
        if risks:
            lines.append("\n🚨 [위험]\n" + "\n".join(f"• {item}" for item in risks))
        if warnings:
            lines.append("\n⚠️ [주의]\n" + "\n".join(f"• {item}" for item in warnings))
        if not risks and not warnings:
            lines.append("\n✅ 관제상 즉시 조치가 필요한 신호가 없습니다.")
        return "\n".join(lines)

    def _health_telegram_text(self):
        """알림 발신 상태. 전송은 비동기라 호출부가 성공 여부를 모르므로 여기서 드러낸다.

        '알림이 조용하다'가 '이상 없음'인지 '경로가 죽었다'인지 구분되어야 한다.
        """
        try:
            h = telegram_notify.get_delivery_health()
        except Exception:
            return "상태 확인 불가"
        if not h['sent'] and not h['failed']:
            return "발신 이력 없음"
        text = f"성공 {h['sent']}건 · 실패 {h['failed']}건"
        if h['consecutive_failed'] > 0:
            text += (f" · [bold red]연속 실패 {h['consecutive_failed']}건 — 알림이 도착하지 "
                     f"않고 있습니다[/bold red]")
            if h['last_error']:
                text += f" ({h['last_error'][:60]})"
        if h['lost']:
            text += f" · 미전달 {len(h['lost'])}건(최근: {h['lost'][-1][0]} {h['lost'][-1][1][:40]})"
        return text

    def _health_circuit_breaker_text(self):
        """일일 손실 한도 감시가 살아 있는가. '조용함'이 '정상'과 구분되어야 한다."""
        limit = getattr(config, 'SYSTEM_DAILY_LOSS_LIMIT', 10.0)
        if limit <= 0:
            return "미사용 (SYSTEM_DAILY_LOSS_LIMIT=0)"
        fails = getattr(self, 'circuit_breaker_fails', 0)
        ran = getattr(self, 'circuit_breaker_ran_at', 0.0)
        text = f"한도 -{limit}% · 최근 점검 {self._health_time(ran) if ran else '없음'}"
        if getattr(self, 'buy_halted', False):
            text += f" · [bold yellow]방어 모드 작동 중({self.buy_halt_reason or ''})[/bold yellow]"
        if fails:
            text += f" · [bold red]연속 실패 {fails}회 — 한도가 감시되지 않는다[/bold red]"
        return text

    def _health_storage_text(self):
        """디스크 여유와 DB 쓰기 실패. 둘 다 조용히 진행되는 고장이라 눈에 띄어야 한다."""
        try:
            free_mb = db_manager.db.disk_free_mb()
            h = db_manager.db.get_write_failures()
        except Exception:
            return "상태 확인 불가"

        if free_mb < 0:
            text = "디스크 여유 확인 불가"
        elif free_mb < DISK_FREE_WARN_MB:
            text = f"[bold red]디스크 여유 {free_mb:,.0f}MB — 쓰기 실패 임박[/bold red]"
        else:
            text = f"디스크 여유 {free_mb:,.0f}MB"

        if h['count']:
            text += (f" · [bold red]DB 쓰기 실패 {h['count']}건[/bold red]"
                     f" (최근: {h['last_op']} — {h['last_error'][:60]})")
        return text

    def _add_health_rows(self, table, skip_labels=()):
        """기존 CLI 테이블 끝에 운영 관제 행을 추가한다.

        텔레그램은 한 화면에 읽기 쉬운 줄글을 쓰되, 터미널은 기존 ``print_status``와
        같은 표 형식을 사용해 운용 중 수치를 빠르게 비교할 수 있게 한다.
        ``skip_labels``에 넣은 항목은 상위 표에 이미 있는 값이라 중복 출력하지 않는다.
        """
        message_lines = self.get_health_message().splitlines()

        def _cli_text(text):
            """텔레그램용 상태 기호를 터미널 테이블에서는 제거한다."""
            for mark in ("🟢", "🟡", "🟠", "🔴", "🚨", "⚠️", "⚠", "✅"):
                text = text.replace(mark, "")
            # 오류 메시지의 대괄호가 rich 마크업으로 해석돼 글자가 사라지는 것을 막는다.
            return escape(text.strip())

        # '리스크' 셀에서 바로 앞 값을 부연하는 줄(무엇을 뜻하는 금액인지·축소 사유).
        #  텔레그램 한 줄 표기에서는 들여쓰기 기호가 어색하므로 CLI 표에서만 붙인다.
        risk_sub_prefixes = ("동시 손절 시 최대손실", "축소 사유:")

        def _compact_detail(label, detail):
            # 한 줄에 모든 지표를 나열하면 표가 화면 전체 폭으로 늘어난다. 관련 값은
            # 셀 안에서 줄바꿈해 기존 상태 표의 폭과 가독성을 맞춘다.
            if label in ("자동매매 루프", "Kill Switch", "주문 감시", "리스크"):
                rows = detail.split(" · ")
                if label == "리스크":
                    rows = [f"└ {r}" if r.startswith(risk_sub_prefixes) else r for r in rows]
                return "\n".join(rows)
            return detail

        def _styled(label, detail):
            """기존 상태 표와 같은 색 규칙(정상=dim green, 경고=yellow, 위험=red)을 적용한다."""
            if label == "Kill Switch":
                counts = re.findall(r"(\d+)/(\d+)", detail)
                if counts and any(int(cur) > 0 for cur, _ in counts):
                    color = "red" if any(int(cur) >= int(mx) for cur, mx in counts) else "yellow"
                    return f"[{color}]{detail}[/]"
                return f"[dim green]{detail}[/]"
            if label == "최근 오류":
                return f"[dim green]{detail}[/]" if detail.startswith("기록 없음") else f"[yellow]{detail}[/]"
            if label == "시세 연결":
                if "연결·수신" in detail:
                    return f"[green]{detail}[/]"
                if "재연결" in detail or "폴백" in detail:
                    return f"[yellow]{detail}[/]"
            if label == "리스크":
                # 히트 한도 소진과 사이징 축소는 '지금 매수가 막혔는지'를 결정하므로 색으로 먼저 보이게 한다.
                if "한도 초과" in detail:
                    return f"[red]{detail}[/]"
                if "한도 임박" in detail or "축소" in detail:
                    return f"[yellow]{detail}[/]"
            if label == "주기 소요":
                # 청산 감시가 늦어지면 손절이 늦게 걸린다 — 관심종목 확대의 실질 상한 지표.
                m = re.search(r"청산 감시 간격 (\d+)초", detail)
                if m:
                    gap = int(m.group(1))
                    if gap >= 300:
                        return f"[red]{detail}[/]"
                    if gap >= 180:
                        return f"[yellow]{detail}[/]"
                    return f"[dim green]{detail}[/]"
            return detail

        section = None
        section_messages = []

        def _flush_section_messages():
            """주의/위험 항목을 한 셀에 모아 라벨 반복을 피한다."""
            nonlocal section_messages
            if section and section_messages:
                color = "bold red" if section == "위험 신호" else "bold orange3"
                table.add_row(section, f"[{color}]" + "\n".join(section_messages) + "[/]")
                section_messages = []

        # 상태 제목은 기존 테이블 제목에 이미 있으므로 건너뛴다.
        for raw_line in message_lines[1:]:
            line = raw_line.strip()
            if not line:
                continue
            if line in ("🚨 [위험]", "⚠️ [주의]"):
                _flush_section_messages()
                section = "위험 신호" if line.startswith("🚨") else "주의 신호"
                table.add_section()
                continue
            if line.startswith("✅ "):
                _flush_section_messages()
                table.add_section()
                table.add_row("관제 결과", f"[dim green]{_cli_text(line)}[/]")
                continue

            # get_health_message의 표준 항목(• 구분: 내용)을 터미널 표의 두 열로 분리한다.
            if line.startswith("• "):
                body = line[2:]
                if section:
                    section_messages.append(_cli_text(body))
                elif ": " in body:
                    label, detail = body.split(": ", 1)
                    if label in skip_labels:
                        continue
                    table.add_row(label, _styled(label, _compact_detail(label, _cli_text(detail))))
                else:
                    table.add_row("상태", _cli_text(body))
            else:
                if section:
                    section_messages.append(_cli_text(line))
                else:
                    table.add_row("상태", _cli_text(line))

        _flush_section_messages()

    def print_health(self):
        """CLI용 운영 관제 단독 화면(하위 호환용)."""
        utils.clear_screen()
        utils.print_breadcrumb()

        table = Table(
            title="운영 관제",
            title_justify="center",
            title_style="",
            box=box.HORIZONTALS,
            show_header=True,
            header_style="dim",
            border_style="dim",
        )
        table.add_column("구분", justify="left", style="cyan", width=15, no_wrap=True)
        table.add_column("상세 내용", justify="left")
        self._add_health_rows(table)

        console.print()
        console.print(table)
        console.print()

    def _get_skipped_stocks_count(self, holdings):
        """현재 관심 종목 중 미보유 종목을 대상으로 시장별 대기 종목 수를 계산합니다."""
        targets = config.session.stock_data.get("stocks_kr", [])
        if getattr(config, 'SYSTEM_INCLUDE_ETF', False):
            targets += config.session.stock_data.get("etfs_kr", [])
        holding_codes = {h['pdno'] for h in holdings if int(h.get('hldg_qty', 0)) > 0} if holdings else set()
        
        count_k = 0
        count_q = 0
        for item in targets:
            code = item['code']
            if code in holding_codes:
                continue
            m_type = self._get_stock_market_type(code)
            if m_type == "KOSDAQ": count_q += 1
            else: count_k += 1
            
        return count_k, count_q

    def log(self, msg):
        now = datetime.now()
        timestamp = now.strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]
        log_msg = f"[{timestamp}] {msg}"
        self.logs.append(log_msg)
        if len(self.logs) > 300: self.logs.pop(0)
        
        # [추가] 로거가 없으면 재할당 시도 (안전장치)
        if not getattr(self, 'file_logger', None) or not self.file_logger.handlers:
            self.file_logger = config.get_autotrade_logger()

        # [수정] 로거를 통해 파일 기록 (자동 로테이션)
        if self.file_logger:
            try:
                self.file_logger.info(log_msg)
            except Exception as e:
                # 파일 쓰기 실패 시 콘솔에만 출력하고 중단하지 않음
                if threading.current_thread().name != "TelegramBot":
                    console.print(f"[dim red]로그 파일 기록 실패: {e}[/dim red]")

    def get_recent_logs(self):
        """최근 로그 반환 (텔레그램용)"""
        if not self.logs:
            return "📭 로그가 없습니다."
        
        final_logs = []
        current_len = 0
        max_len = 3800 # 텔레그램 제한(4096자) 고려하여 여유 있게 설정
        
        header = "📜 [최근 시스템 로그]\n"
        current_len += len(header)

        for log in reversed(self.logs):
            if current_len + len(log) + 1 > max_len:
                break
            final_logs.append(log)
            current_len += len(log) + 1
        
        final_logs.reverse()
        return header + "\n".join(final_logs)

    def print_status(self):
        utils.clear_screen()
        utils.print_breadcrumb()
        
        if not self.is_running:
            status_text = "STOPPED"
            status_color = "red"
        elif self.is_market_open():
            status_text = "RUNNING"
            status_color = "green"
        else:
            status_text = "WAITING"
            status_color = "yellow"
        
        kospi_regime, kospi_adj = "확인 불가", 0.0
        kosdaq_regime, kosdaq_adj = "확인 불가", 0.0

        # 3. 자산 및 손익 현황 (안전성 핵심)
        current_asset = None
        deposit = 0
        holdings = []
        
        # [추가] 상태 조회 시에도 시스템 트레이딩 컨텍스트 사용
        target_cano = config.session.auto_cano
        with utils.AccountContext(target_cano):
            with Progress(
                SpinnerColumn(),
                TextColumn("[progress.description]{task.description}"),
                BarColumn(),
                console=console,
                transient=True
            ) as progress:
                task = progress.add_task("[cyan]자산/시장 정보 병렬 조회 중...[/cyan]", total=None)
                acnt = config.session.auto_acnt_prdt_cd

                # [최적화] 잔고+예수금 / 시장 국면 / 지수 상태를 병렬 조회 (기존 순차 → 동시)
                def _fetch_asset():
                    _holdings, _summary, _deposit = [], [], 0
                    try:
                        _holdings, _summary = api.get_domestic_balance(target_cano, acnt)
                    except Exception:
                        _holdings, _summary = [], []
                    # 예수금 조회 (매수 여력 확인용) — 잔고 결과에 의존하므로 같은 태스크에서 수행
                    try:
                        if _summary and len(_summary) > 0:
                            _deposit = api.safe_int(_summary[0].get('dnca_tot_amt', 0))

                        # [수정] 예수금은 항상 상세 조회로 확인한다 (정확도 우선)
                        res = api.get_deposit_balance(target_cano, acnt)
                        if res:
                            _deposit = res.get('d2_real', 0)
                            if _deposit == 0: _deposit = res.get('d2_deposit', 0)
                    except Exception: pass
                    return _holdings, _summary, _deposit

                def _fetch_regimes():
                    try:
                        k = analysis.get_market_regime("KOSPI")
                        q = analysis.get_market_regime("KOSDAQ")
                        return k, q
                    except Exception:
                        return None, None

                def _update_indices():
                    # [추가] 지수 상태 정보가 없으면 업데이트 시도 (시장 필터링 사용 시)
                    # 시스템이 정지 상태이거나 장 시작 전이라도 상태 조회 시에는 최신 정보를 보여주기 위함
                    if not getattr(config, 'USE_MARKET_FILTER', True):
                        return
                    need_update = False
                    if "KOSPI" not in self.market_index_status or "KOSDAQ" not in self.market_index_status:
                        need_update = True
                    elif self.market_index_status.get("KOSPI", {}).get("current", 0) == 0 or \
                         self.market_index_status.get("KOSDAQ", {}).get("current", 0) == 0:
                        need_update = True
                    if need_update:
                        try:
                            self._update_market_indices_status(notify=False)
                        except Exception: pass

                summary = []
                with concurrent.futures.ThreadPoolExecutor(max_workers=3, thread_name_prefix="at_status") as executor:
                    fut_asset = executor.submit(_fetch_asset)
                    fut_regime = executor.submit(_fetch_regimes)
                    fut_indices = executor.submit(_update_indices)

                    holdings, summary, deposit = fut_asset.result()
                    _k, _q = fut_regime.result()
                    if _k: kospi_regime, kospi_adj = _k
                    if _q: kosdaq_regime, kosdaq_adj = _q
                    fut_indices.result()

                # [수정] 중복 API 호출 방지 및 동일 스냅샷 기반 현재 자산 일괄 계산
                tot_evlu = 0
                if holdings:
                    valid_holdings = [h for h in holdings if int(h.get('hldg_qty', 0)) > 0]
                    tot_evlu = sum(int(h['evlu_amt']) for h in valid_holdings)
                elif summary and len(summary) > 0:
                    tot_evlu = api.safe_int(summary[0].get('scts_evlu_amt', 0))

                current_asset = deposit + tot_evlu

        console.print()
        table = Table(title=f"시스템 트레이딩 상태 ({status_text})", title_justify="center", title_style="", box=box.HORIZONTALS, show_header=True, header_style="dim", border_style="dim")
        table.add_column("구분", justify="left", style="cyan", width=15)
        table.add_column("상세 내용", justify="left")

        # 1. 실행 정보
        if self.is_running and self.start_time:
            start_str = self.start_time.strftime("%Y-%m-%d %H:%M:%S")
            elapsed = datetime.now() - self.start_time
            elapsed_str = str(elapsed).split('.')[0]
            table.add_row("실행 시간", f"{start_str} (경과: {elapsed_str})")
        
        # 2. 마켓 상태
        if self.is_market_open():
            market_status = "장 운영 중 (거래 가능)"
        else:
            if is_single_price_break():
                market_status = "휴게 시간 (단일가 매매 동기화 대기 중)"
            elif api.is_holiday_today():
                market_status = "공휴일 휴장 (대기 중)"
            else:
                market_status = "장 마감 (대기 중)"
                
        if datetime.now().weekday() > 4: market_status = "주말 휴장 (대기 중)"
        table.add_row("마켓 상태", market_status)

        # [안전장치] 방어 모드 — 신규 매수만 차단, 청산 감시는 계속됨을 명시
        if self.is_running and getattr(self, 'buy_halted', False):
            table.add_row("방어 모드",
                          f"[bold red]🛑 신규 매수 중단[/] ({self.buy_halt_reason})\n"
                          f"[dim]매도·손절·트레일링 스탑 감시는 정상 동작 중 (날짜 변경 시 자동 해제)[/]")

        # [추가] 시장 국면 상태 표시
        k_regime_str = analysis.format_regime(kospi_regime)
        q_regime_str = analysis.format_regime(kosdaq_regime)
        rp = config.MARKET_REGIME_PARAMS
        regime_desc = f"EMA {rp.get('REGIME_EMA_FAST', 9)}/{rp.get('REGIME_EMA_SLOW', 41)} 교차 + {rp.get('REGIME_CONFIRM_PCT', 5.0):g}% 확인"
        table.add_row("시장 국면", f"KOSPI: {k_regime_str} (보정: {kospi_adj:+.1f}점) / KOSDAQ: {q_regime_str} (보정: {kosdaq_adj:+.1f}점) [dim]({regime_desc})[/]")

        # [추가] 지수 추세 상태 표시 (시장 필터링 사용 시)
        if getattr(config, 'USE_MARKET_FILTER', True):
            # ... existing code ...
            kospi_stat = self.market_index_status.get("KOSPI")
            kosdaq_stat = self.market_index_status.get("KOSDAQ")
            
            def get_stat_msg(stat):
                # [Fix] 판단 불가(조회 실패)는 '확인 중'이 아니라 '매수 보류' 상태임을 명시한다.
                if isinstance(stat, dict) and stat.get('unknown'):
                    return "[yellow]판단 불가 (신규 매수 보류)[/]"
                if not stat or not isinstance(stat, dict) or stat.get('current', 0) == 0:
                    return "[dim]확인 중[/]"

                is_healthy = stat.get('is_healthy', True)
                current = stat.get('current', 0)
                trend_icon = "(상승)" if is_healthy else "(하락)"
                color = "red" if is_healthy else "blue"
                return f"[{color}]{current:,.0f} {trend_icon}[/]{index_source_note(stat)}"
            
            table.add_row("지수 추세", f"KOSPI: {get_stat_msg(kospi_stat)} / KOSDAQ: {get_stat_msg(kosdaq_stat)}")
            
            # [추가] 필터링 보류 개수 표시
            skip_k = self.skipped_by_market_filter_count.get("KOSPI", 0)
            skip_q = self.skipped_by_market_filter_count.get("KOSDAQ", 0)
            
            is_healthy_k = kospi_stat.get('is_healthy', True) if isinstance(kospi_stat, dict) else True
            is_healthy_q = kosdaq_stat.get('is_healthy', True) if isinstance(kosdaq_stat, dict) else True
            
            # [추가] 분석 루프가 돌지 않았을 경우(0건) stock.json 기준으로 실제 보류 대상 개수 산출
            if (not is_healthy_k and skip_k == 0) or (not is_healthy_q and skip_q == 0):
                calc_k, calc_q = self._get_skipped_stocks_count(holdings)
                if not is_healthy_k and skip_k == 0: skip_k = calc_k
                if not is_healthy_q and skip_q == 0: skip_q = calc_q
            
            skip_msg = []
            if not is_healthy_k or skip_k > 0: skip_msg.append(f"KOSPI {skip_k}종목")
            if not is_healthy_q or skip_q > 0: skip_msg.append(f"KOSDAQ {skip_q}종목")
            
            if skip_msg:
                filter_ma = getattr(config, 'MARKET_FILTER_MA', 80)
                filter_band = getattr(config, 'MARKET_FILTER_BAND', 1.0)
                band_txt = f" -{filter_band:g}%" if filter_band else ""
                bear_txt = ", 확정 Bear 해제" if getattr(config, 'MARKET_FILTER_RELEASE_ON_BEAR', False) else ""
                table.add_row("시장 필터링", f"[bold blue]{', '.join(skip_msg)} 매수 보류[/] [dim](SMA {filter_ma}일{band_txt} 이탈{bear_txt})[/]")

        table.add_section()
        
        # [추가] 개별 종목 룰 설정 현황
        custom_rules = db_manager.db.get_all_stock_strategies()
        custom_rules = _enrich_rules_with_weights(custom_rules) # [Fix] 가중치 JSON 파싱
        rule_table = None
        rule_summary = None
        
        # 보유 종목 코드 집합 생성 (강조 표시용)
        held_codes = set()
        if holdings:
            for h in holdings:
                if int(h.get('hldg_qty', 0)) > 0:
                    held_codes.add(h.get('pdno'))

        if custom_rules:
            rule_summary = f"총 {len(custom_rules)}개 종목 개별 설정됨"
            
            # 별도 테이블로 상세 표시
            rule_table = Table(title="종목별 개별 트레이딩 룰 목록", title_justify="center", title_style="", box=box.HORIZONTALS, header_style="dim", border_style="dim")
            rule_table.add_column("종목명(코드)", justify="left")
            rule_table.add_column("매수(점수/RSI/체결/비대칭)", justify="center")
            rule_table.add_column("청산(익절/TS/RSI/기한)", justify="center")
            rule_table.add_column("리스크(비중/손절)", justify="center")
            rule_table.add_column("가중치", justify="center") # [추가]
            rule_table.add_column("수정일", justify="center", style="dim")
            
            for i, r in enumerate(custom_rules):
                # 보유 중인 종목이면 종목명 강조 (bold cyan)
                name_disp = f"{r['name']}({r['code']})"
                if r['code'] in held_codes:
                    name_disp = f"[bold cyan]{name_disp}[/]"
                
                w_str = "기본"
                if r.get('weights'):
                    w = r['weights']
                    w_str = f"{w.get('TREND',0):.1f}/{w.get('MOMENTUM',0):.1f}/{w.get('STRENGTH',0):.1f}/{w.get('SYNERGY',0):.1f}"

                sl_str = f"ATR(x{r.get('atr_stop_multiplier', 2.0)})" if r.get('use_atr_stop') else f"{r['stop_loss']}%"
                ratio_str = config.format_invest_ratio(r.get('invest_ratio'))

                rule_table.add_row(
                    name_disp,
                    f"{r['buy_score']}점 / {r.get('buy_rsi', 65.0)} / {r.get('buy_vol_strength', config.ANALYSIS_THRESHOLDS.get('BUY_VOL_STRENGTH', 100.0))}% / {r.get('buy_ask_bid_ratio', config.ANALYSIS_THRESHOLDS.get('BUY_ASK_BID_RATIO', 1.0))}배",
                    f"+{r['take_profit']}% / TS(+{r['ts_activation']}/-{r['ts_callback']}) / {r.get('take_profit_rsi', 75.0)} / {r.get('time_stop_days', 10)}일",
                    f"{ratio_str} / {sl_str}",
                    w_str,
                    r.get('updated_at', '-')
                )
                if (i + 1) % 5 == 0 and (i + 1) < len(custom_rules):
                    rule_table.add_section()

        # 금일 매매 & 실현 손익 계산 (상단 이동)
        today_profit = 0
        buy_cnt = 0
        sell_cnt = 0

        try:
            today_str = datetime.now().strftime("%Y-%m-%d")
            
            target_account = None
            if config.session.auto_cano:
                target_account = f"{config.session.auto_cano}-{config.session.auto_acnt_prdt_cd}"
            
            today_trades = db_manager.db.get_trades(
                start_date=today_str, end_date=today_str, 
                is_sim=False, account=target_account
            )
            
            today_trades_parsed = []
            for r in reversed(today_trades):
                type_str = r['type']
                simple_type = "buy" if "매수" in type_str or "buy" in type_str.lower() else "sell"
                parsed_r = dict(r)
                parsed_r['type'] = simple_type
                today_trades_parsed.append(parsed_r)
            
            # 중복 제거 및 정제
            today_trades_refined = self._refine_trade_records(today_trades_parsed)
            
            # [추가] 체결된 내역만 당일 매매 요약에 포함
            today_trades_refined = [r for r in today_trades_refined if "체결" in r.get('order_status', '')]
            
            buy_trades = [x for x in today_trades_refined if x['type'] == 'buy']
            sell_trades = [x for x in today_trades_refined if x['type'] == 'sell']
            
            buy_cnt = len(buy_trades)
            sell_cnt = len(sell_trades)
            
            # 금일 실현 손익 합산
            for t in sell_trades:
                today_profit += int(t.get('profit_amt') or 0)
                
        except Exception:
            # DB 조회 실패 시 메모리 값 사용 (Fallback)
            buy_cnt = len([x for x in self.trade_records if x['type'] == 'buy'])
            sell_cnt = len([x for x in self.trade_records if x['type'] == 'sell'])
            for r in self.trade_records:
                if r['type'] == 'sell':
                    today_profit += int(r.get('profit_amt') or 0)

        # 3. 자산 현황
        if current_asset is not None:
            # [추가] 메모리에 초기 자산이 없으면 당일 백업 파일에서 복구 시도
            if self.initial_asset <= 0:
                target_cano = config.session.auto_cano
                acnt = config.session.auto_acnt_prdt_cd
                account_key = f"{target_cano}-{acnt}"
                saved_initial = load_daily_initial_asset(account_key)
                if saved_initial > 0:
                    self.initial_asset = saved_initial
                    
            tot_profit = 0
            tot_pchs = 0
            tot_evlu = 0
            
            # [수정] API 요약 데이터 대신 보유 종목 합산 (데이터 불일치 방지)
            if holdings:
                valid_holdings = [h for h in holdings if int(h.get('hldg_qty', 0)) > 0]
                if valid_holdings:
                    tot_pchs = sum(int(int(h['hldg_qty']) * float(h['pchs_avg_pric'])) for h in valid_holdings)
                    tot_profit = sum(int(h['evlu_pfls_amt']) for h in valid_holdings)
                    tot_evlu = sum(int(h['evlu_amt']) for h in valid_holdings)
            
            rate = (tot_profit / tot_pchs * 100) if tot_pchs > 0 else 0.0
            color = "[red]" if tot_profit > 0 else ("[blue]" if tot_profit < 0 else "[white]")
            
            table.add_row("증권 매입 금액", f"{tot_pchs:,}원")
            table.add_row("증권 평가 금액", f"{tot_evlu:,}원")
            table.add_row("증권 평가 손익", f"{color}{tot_profit:+,}원 ({rate:+.2f}%)[/]")
            table.add_row("주문 가능 금액", f"{deposit:,}원")
            
            table.add_section()

            if self.initial_asset > 0:
                table.add_row("오늘 시작 자산", f"{self.initial_asset:,}원")
                table.add_row("오늘 현재 자산", f"{current_asset:,}원")
                
                _base = self.daily_pnl_base()
                daily_profit = int(round(current_asset - _base))   # 기준선은 float 다 (원 단위로 표시)
                daily_profit_rate = (daily_profit / _base) * 100 if _base > 0 else 0.0
                dp_color = "[red]" if daily_profit > 0 else ("[blue]" if daily_profit < 0 else "[white]")
                table.add_row("오늘 현재 손익",
                              f"{dp_color}{daily_profit:+,}원 ({daily_profit_rate:+.2f}%)[/]"
                              f"{self.transfer_note()}")
                
                realized_rate = (today_profit / _base) * 100 if _base > 0 else 0.0
                rp_color = "[red]" if today_profit > 0 else ("[blue]" if today_profit < 0 else "[white]")
                table.add_row("오늘 실현 손익", f"{rp_color}{today_profit:+,}원 ({realized_rate:+.2f}%)[/]")
            else:
                table.add_row("오늘 시작 자산", "- (미설정)")
                table.add_row("오늘 현재 자산", f"{current_asset:,}원")
                table.add_row("오늘 현재 손익", "-")
                table.add_row("오늘 실현 손익", "-")
        else:
            if self.initial_asset > 0:
                table.add_row("오늘 시작 자산", f"{self.initial_asset:,}원")

        table.add_section()

        # 4. 설정 및 상태 정보 (재구성)
        # 매수 조건
        buy_score = config.ANALYSIS_THRESHOLDS["BUY_SCORE"]
        buy_rsi = config.ANALYSIS_THRESHOLDS["BUY_RSI_MAX"]
        buy_vol = config.ANALYSIS_THRESHOLDS.get("BUY_VOL_STRENGTH", 100.0)
        buy_ask_ratio = config.ANALYSIS_THRESHOLDS.get("BUY_ASK_BID_RATIO", 1.0)
        auto_adj = "ON" if config.ANALYSIS_THRESHOLDS.get("AUTO_ADJUST_ASK_BID_RATIO", True) else "OFF"
        if config.session.is_toss:
            table.add_row("매수 조건", f"{buy_score}점↑ / RSI {buy_rsi}↓ / 매도잔량비 {buy_ask_ratio}배↑ (체결강도 미제공→매도잔량비 대체)")
        else:
            table.add_row("매수 조건", f"{buy_score}점↑ / RSI {buy_rsi}↓ / 체결강도 {buy_vol}%↑ / 비대칭 {buy_ask_ratio}배↑ (자동연동: {auto_adj})")

        # [추가] 역추세 매수 표시
        use_mr = config.ANALYSIS_THRESHOLDS.get("USE_MEAN_REVERSION", False)
        mr_status = "[green]ON[/]" if use_mr else "[red]OFF[/]"
        mr_rsi = config.ANALYSIS_THRESHOLDS.get("MR_RSI_MAX", 40.0)
        mr_disp = config.ANALYSIS_THRESHOLDS.get("MR_DISPARITY_MAX", 90.0)
        mr_vol = config.ANALYSIS_THRESHOLDS.get("MR_VOL_STRENGTH", 120.0)
        table.add_row("", f"역매수 (RSI {mr_rsi}↓ / 20일선 이격도 {mr_disp}%↓ / 체결 {mr_vol}%↑) {mr_status}")

        # 매도 조건
        sell_score = config.SELL_STRATEGY["SELL_SCORE"]
        tp_rsi = config.SELL_STRATEGY["TAKE_PROFIT_RSI"]
        tp = config.SELL_STRATEGY["TAKE_PROFIT_RATE"]
        sl = config.SELL_STRATEGY["STOP_LOSS_RATE"]
        ts_act = _pkg().ts_activation_label()
        ts_call = config.SELL_STRATEGY.get("TRAILING_STOP_CALLBACK_RATE", 5.0)
        
        use_half_tp = config.SELL_STRATEGY.get("HALF_TAKE_PROFIT_USE", False)
        use_atr = config.SELL_STRATEGY.get("USE_ATR_STOP", True)
        atr_mult = config.SELL_STRATEGY.get("ATR_STOP_MULTIPLIER", 2.0)
        use_time_stop = config.SELL_STRATEGY.get("TIME_STOP_USE", True)
        time_stop_days = config.SELL_STRATEGY["TIME_STOP_DAYS"]
        time_stop_min = config.SELL_STRATEGY.get("TIME_STOP_MIN_PROFIT_RATE", 0.0)

        # [수정] 0=미사용 규칙은 OFF로 명시 (활성 조건처럼 보이던 표시 모순 해소)
        overheat_str = f"과열 매도 (RSI {tp_rsi} 초과)" if tp_rsi > 0 else "과열 매도 [red]OFF[/]"
        table.add_row("매도 조건", f"추세이탈 ({sell_score}점 미만 + 60일선 이탈) / {overheat_str}")

        # 익절 / 반익절
        if tp > 0:
            half_tp_status = "[green]ON[/]" if use_half_tp else "[red]OFF[/]"
            tp_str = f"익절 (+{tp}%) / 반익절 (+{tp/2:.1f}%, 50%) {half_tp_status}"
        else:
            tp_str = "익절/반익절 [red]OFF[/] (추세추종: 트레일링 스탑 주청산)"
        table.add_row("", tp_str)
        
        # ATR손절 / 고정손절
        atr_status = "[green]ON[/]" if use_atr else "[red]OFF[/]"
        sl_str = f"ATR손절 (x{atr_mult}) {atr_status}"
        fixed_sl_status = "[red]OFF[/]" if use_atr else "[green]ON[/]"
        sl_str += f" / 고정손절 ({sl}%) {fixed_sl_status}"
        table.add_row("", sl_str)

        time_stop_status = "[green]ON[/]" if use_time_stop else "[red]OFF[/]"
        table.add_row("", f"시간청산 ({time_stop_days}일 경과 & 수익률 +{time_stop_min}% 미만) {time_stop_status}")
        
        ts_atr_mult = config.SELL_STRATEGY.get("TRAILING_ATR_MULTIPLIER", 3.5)
        act_mult = _pkg().ts_activation_atr_mult()
        if "손익분기" in ts_act:
            ts_act = f"동적 손익분기 (ATR x{act_mult})"
            
        table.add_row("", f"샹들리에 트레일링스탑 (발동: {ts_act} / 이탈: 고점대비 ATR x{ts_atr_mult}, 하한 -{ts_call}%)")

        # 투자 설정
        max_holdings = config.settings.SYSTEM_MAX_HOLDINGS
        include_etf = getattr(config, 'SYSTEM_INCLUDE_ETF', False)
        etf_str = "포함" if include_etf else "제외"
        table.add_row("투자 설정", f"비중 {config.format_invest_ratio()} (최대 {max_holdings}종목, ETF {etf_str})")

        # 손실 제한
        loss_limit = getattr(config, 'SYSTEM_DAILY_LOSS_LIMIT', 10.0)
        if loss_limit > 0:
            safety_msg = "[green]안전[/green]"
            if current_asset is not None and self.initial_asset > 0:
                profit = current_asset - self.initial_asset
                rate = (profit / self.initial_asset) * 100
                
                if rate <= -loss_limit: safety_msg = "[bold red]위험 (한도 초과)[/bold red]"
                elif rate <= -(loss_limit * 0.8): safety_msg = "[bold orange3]주의 (한도 임박)[/bold orange3]"
            table.add_row("손실 제한", f"-{loss_limit}% (상태: {safety_msg})")
        else:
            table.add_row("손실 제한", "미사용")

        # 연속 에러는 아래 운영 관제 섹션의 'Kill Switch' 행이 자동매매·체결 감시를
        # 함께 보여 주므로 여기서 중복 출력하지 않는다.
        table.add_row("오늘 매매", f"[red]매수 {buy_cnt}건[/] / [blue]매도 {sell_cnt}건[/]")
        
        if rule_summary:
            table.add_section()
            table.add_row("개별 룰 설정", rule_summary)

        # 운영 관제는 별도 표가 아닌 상태 표의 마지막 섹션으로 이어서 보여 준다.
        # 기존 상태/설정 정보와 관제 데이터를 수평 구분선으로 명확히 나눈다.
        table.add_section()
        # 표 상단에 이미 출력한 항목(실행 시간)은 관제 섹션에서 생략한다.
        self._add_health_rows(table, skip_labels={"실행 시간"} if (self.is_running and self.start_time) else ())

        console.print(table)
        
        if rule_table:
            console.print()
            console.print(rule_table)
            console.print()
        
        # [추가] 보유 종목 리스트 출력
        # [수정] 보유수량 0 초과인 종목만 필터링
        valid_holdings = [h for h in holdings if int(h.get('hldg_qty', 0)) > 0] if holdings else []

        if valid_holdings:
            holding_rows = []
            # [추가] 시장 구분 등 추가 정보를 가져오는 지연 시간에 대응하기 위한 프로그레스 바 적용
            with Progress(
                SpinnerColumn(),
                TextColumn("[progress.description]{task.description}"),
                BarColumn(),
                TextColumn("[progress.percentage]{task.percentage:>3.0f}%"),
                console=console,
                transient=True
            ) as progress:
                task = progress.add_task("[cyan]보유 종목 세부 정보 조회 중...[/cyan]", total=len(valid_holdings))
                for item in valid_holdings:
                    name = item['prdt_name']
                    code = item['pdno']
                    market_type = self._get_stock_market_type(code)
                    qty = int(item['hldg_qty'])
                    buy_price = float(item['pchs_avg_pric'])
                    cur_price = int(item['prpr'])
                    profit = int(item['evlu_pfls_amt'])
                    rate = float(item['evlu_pfls_rt'])
                    holding_rows.append((name, code, market_type, qty, buy_price, cur_price, profit, rate))
                    progress.advance(task)

            console.print()
            h_table = Table(title="보유 종목 리스트", title_justify="center", title_style="", box=box.HORIZONTALS, header_style="dim", border_style="dim")
            h_table.add_column("종목명(코드)", justify="left")
            h_table.add_column("시장", justify="center")
            h_table.add_column("수량", justify="right")
            h_table.add_column("매입가", justify="right")
            h_table.add_column("현재가", justify="right")
            h_table.add_column("평가손익", justify="right")
            h_table.add_column("수익률", justify="right")
            
            for name, code, market_type, qty, buy_price, cur_price, profit, rate in holding_rows:
                p_color = "[red]" if profit > 0 else ("[blue]" if profit < 0 else "[white]")
                h_table.add_row(
                    f"{name}({code})", 
                    market_type,
                    f"{qty:,}주", 
                    f"{buy_price:,.0f}원",
                    f"{cur_price:,}원", 
                    f"{p_color}{profit:+,}원[/]", 
                    f"{p_color}{rate:+.2f}%[/]"
                )
            console.print(h_table)
        else:
            console.print("\n[dim]현재 보유 중인 종목이 없습니다.[/dim]")

        console.print()

    def print_report(self, target_account=None):
        menu_items = [
            ("1", "일간 (오늘)", "Daily"),
            ("2", "주간 (최근 7일)", "Weekly"),
            ("3", "월간 (최근 30일)", "Monthly"),
            ("4", "기간 직접 입력", "Custom Days")
        ]
        choice = utils.show_menu("시스템 트레이딩 평가 리포트 (Trading Report)", menu_items, default_choice="4")
        if choice.lower() == 'q': return False
        
        menu_map = {"1": "일간", "2": "주간", "3": "월간", "4": "직접 입력"}
        if choice in menu_map:
            context.USER_ACTION_BREADCRUMB.append(f"[{choice}] {menu_map[choice]}")
            
        days = None
        if choice == "1": days = 0
        elif choice == "2": days = 7
        elif choice == "3": days = 30
        elif choice == "4":
            utils.print_breadcrumb()
            val = Prompt.ask("조회할 기간(일) 입력 [dim](Enter: 전체 내역, 이전: b, 메인: q)[/dim]", default="")
            console.print()
            if val.lower() in ['b', 'q']: return False
            
            if val.strip() and val.isdigit():
                days = int(val)
                context.USER_ACTION_BREADCRUMB.append(f"[{days}일]")
            else:
                days = None # 전체 내역
                context.USER_ACTION_BREADCRUMB.append("[전체]")

        # [추가] 토스 등 체결감시 미가동 상태에서도 평가 전에 당일 체결을 DB에 동기화한다.
        # (토스 CLOSED 주문 = 체결 데이터. 수동 주문 체결이 누락되어 리포트가 비던 문제 해결)
        if config.session.is_toss:
            try:
                _pkg().ConclusionMonitor()._check_conclusions()
            except Exception as e:
                logger.debug(f"[Report] 토스 체결 동기화 실패: {e}")

        self._load_trade_records(days=days, target_account=target_account)

        if not self.trade_records:
            console.print("\n[yellow]선택한 기간에 해당하는 매매 기록이 없습니다.[/yellow]")
            return
            
        with Progress(
            SpinnerColumn(),
            TextColumn("[progress.description]{task.description}"),
            BarColumn(),
            console=console,
            transient=True
        ) as progress:
            progress.add_task("[cyan]리포트 통계 분석 및 시장 데이터 수집 중...[/cyan]", total=None)
            
            stats = self._calculate_statistics()
            
            # [추가] 자산 증감 및 시장 성과 통계 추가
            now = datetime.now()
            end_dt = now.strftime("%Y-%m-%d")
            
            if days is not None:
                start_dt = (now - timedelta(days=days)).strftime("%Y-%m-%d")
            else:
                start_dt = self.trade_records[0]['time'][:10] if self.trade_records else end_dt
                
            if target_account:
                # 토스 계좌번호엔 '-'가 여러 개라(예: 189-01-501685-) 마지막 '-' 기준으로 분리
                target_cano, acnt = target_account.rsplit('-', 1)
            else:
                target_cano = config.session.auto_cano
                acnt = config.session.auto_acnt_prdt_cd
                if not target_cano:
                    target_cano = config.session.cano
                    acnt = config.session.acnt_prdt_cd
                    
                target_account = f"{target_cano}-{acnt}"
            
            current_asset = 0
            try:
                with utils.AccountContext(target_cano):
                    asset_data = account.get_asset_status_data(target_cano, acnt)
                    if asset_data:
                        current_asset = asset_data.get('tot_asset', 0)
            except Exception: pass
            
            initial_asset = db_manager.db.get_daily_asset(start_dt, target_account)
            stats['current_asset'] = current_asset
            stats['initial_asset'] = initial_asset
            
            kospi_rate = 0.0
            try:
                kospi_df = analysis.get_domestic_index_data("KOSPI")
                if kospi_df is not None and not kospi_df.empty:
                    s_dt = start_dt.replace('-', '')
                    e_dt = end_dt.replace('-', '')
                    
                    if 'date' in kospi_df.columns:
                        def to_yyyymmdd(x):
                            if hasattr(x, 'strftime'): return x.strftime('%Y%m%d')
                            return str(x).replace('-', '')[:8]
                        
                        dates = kospi_df['date'].apply(to_yyyymmdd)
                        mask = (dates >= s_dt) & (dates <= e_dt)
                        period_df = kospi_df[mask]
                        if not period_df.empty:
                            first_idx = kospi_df.index.get_loc(period_df.index[0])
                            last_idx = kospi_df.index.get_loc(period_df.index[-1])
                            
                            if first_idx > 0:
                                start_val = kospi_df.iloc[first_idx - 1]['close']
                            else:
                                start_val = kospi_df.iloc[first_idx]['close']
                                
                            end_val = kospi_df.iloc[last_idx]['close']
                            if start_val > 0:
                                kospi_rate = ((end_val - start_val) / start_val) * 100
            except Exception: pass
            stats['kospi_rate'] = kospi_rate

            # [추가] 현재 보유 종목에 대한 총 매입금액, 평가손익, 수익률 계산
            holdings_summary = None
            try:
                with utils.AccountContext(target_cano):
                    holdings, summary = api.get_domestic_balance(target_cano, acnt)
                    
                    tot_pchs = 0
                    tot_profit = 0
                    tot_evlu = 0
                    
                    if summary and len(summary) > 0:
                        tot_profit = api.safe_int(summary[0].get('evlu_pfls_smtl_amt'))
                        tot_pchs = api.safe_int(summary[0].get('pchs_amt_smtl'))
                        tot_evlu = api.safe_int(summary[0].get('scts_evlu_amt'))
                    
                    if tot_pchs == 0 and holdings:
                        tot_pchs = sum(int(int(h['hldg_qty']) * float(h['pchs_avg_pric'])) for h in holdings if int(h.get('hldg_qty', 0)) > 0)
                        tot_profit = sum(int(h['evlu_pfls_amt']) for h in holdings if int(h.get('hldg_qty', 0)) > 0)
                        tot_evlu = sum(int(h['evlu_amt']) for h in holdings if int(h.get('hldg_qty', 0)) > 0)
                    
                    if tot_pchs > 0 or tot_profit != 0:
                        rate = (tot_profit / tot_pchs * 100) if tot_pchs > 0 else 0.0
                        holdings_summary = {'tot_pchs': tot_pchs, 'tot_evlu': tot_evlu, 'tot_profit': tot_profit, 'rate': rate}
            except Exception: pass

        self._print_summary_table(stats, holdings_summary)
        self._print_current_holdings(target_cano, acnt)
        self._print_stock_details()

    def _load_trade_records(self, days=None, target_account=None):
        with Progress(
            SpinnerColumn(),
            TextColumn("[progress.description]{task.description}"),
                BarColumn(),
            console=console,
            transient=True
        ) as progress:
            progress.add_task("[cyan]DB에서 매매 내역 조회 및 분석 중...[/cyan]", total=None)
            
            # [수정] DB에서 매매 내역 조회 (수동 매매 포함을 위해 is_auto 필터 제거)
            # 시스템 매매와 수동 매매를 모두 포함하여 평가
            limit = 500
            start_date = None
            
            if days is not None:
                limit = None
                start_date = (datetime.now() - timedelta(days=days)).strftime("%Y-%m-%d")
            
            # [수정] 자동매매 계좌 번호로 필터링 (시스템 트레이딩 내역만 조회)
            if not target_account:
                if config.session.auto_cano:
                    target_account = f"{config.session.auto_cano}-{config.session.auto_acnt_prdt_cd}"
            
            # [Fix] DBManager.get_trades가 account 인자를 지원하지 않는 경우 대비 (메모리 필터링)
            try:
                db_records = db_manager.db.get_trades(is_sim=False, limit=limit, start_date=start_date, account=target_account)
            except TypeError:
                db_records = db_manager.db.get_trades(is_sim=False, limit=limit, start_date=start_date)
                if target_account:
                    db_records = [r for r in db_records if r.get('account') == target_account]
            
            # DB 레코드를 내부 포맷으로 변환
            self.trade_records = []
            for r in reversed(db_records): # DB는 최신순이므로 시간순(과거->최신)으로 뒤집음
                # type 파싱: "buy(AUTO)" -> "buy"
                type_str = r['type']
                simple_type = "buy" if "매수" in type_str or "buy" in type_str.lower() else "sell"
                
                parsed_r = dict(r)
                parsed_r['type'] = simple_type
                self.trade_records.append(parsed_r)
            
            # [추가] 중복 제거 및 정제 (시스템 주문과 체결 확인 병합)
            self.trade_records = self._refine_trade_records(self.trade_records)
            
            # [추가] 성과 평가(Report)에서는 체결된 내역만 포함하도록 필터링 (미체결/접수/취소 등 제외)
            self.trade_records = [r for r in self.trade_records if "체결" in r.get('order_status', '')]
            
            # [추가] 시간순 정렬 (통계 계산 및 기간 표시 정확성 확보)
            if self.trade_records:
                self.trade_records.sort(key=lambda x: x['time'])

    def get_performance_report(self, days=None):
        """텔레그램용 성과 리포트 생성"""
        # DB에서 조회 (로그 출력 없이)
        # [수정] 수동 매매 포함을 위해 is_auto 필터 제거
        limit = 500
        start_date = None
        period_msg = "전체 (최근 500건)"
        
        if days is not None:
            limit = None
            start_dt = datetime.now() - timedelta(days=days)
            start_date = start_dt.strftime("%Y-%m-%d")
            end_date = datetime.now().strftime("%Y-%m-%d")
            period_msg = f"{start_date} ~ {end_date}"
        
        # [수정] 자동매매 계좌 번호로 필터링
        target_account = None
        if config.session.auto_cano:
            target_account = f"{config.session.auto_cano}-{config.session.auto_acnt_prdt_cd}"
            
        # [Fix] DBManager.get_trades가 account 인자를 지원하지 않는 경우 대비
        try:
            db_records = db_manager.db.get_trades(is_sim=False, limit=limit, start_date=start_date, account=target_account)
        except TypeError:
            db_records = db_manager.db.get_trades(is_sim=False, limit=limit, start_date=start_date)
            if target_account:
                db_records = [r for r in db_records if r.get('account') == target_account]
        
        temp_records = []
        for r in reversed(db_records):
            type_str = r['type']
            simple_type = "buy" if "매수" in type_str or "buy" in type_str.lower() else "sell"
            
            parsed_r = dict(r)
            parsed_r['type'] = simple_type
            temp_records.append(parsed_r)
            
        # [추가] 중복 제거 및 정제
        refined_records = self._refine_trade_records(temp_records)
        
        # [추가] 성과 평가(Report)에서는 체결된 내역만 포함하도록 필터링
        refined_records = [r for r in refined_records if "체결" in r.get('order_status', '')]
        
        # [추가] 시간순 정렬 (통계 계산 및 기간 표시 정확성 확보)
        if refined_records:
            refined_records.sort(key=lambda x: x['time'])
            
        msg = "📊 [시스템 트레이딩 성과 리포트]\n"

        if not refined_records:
            msg += f"기간: {period_msg}\n\n"
            msg += "매매 기록이 없습니다."
            return msg
            
        stats = self._calculate_statistics(refined_records)
        
        # [추가] 기간 정보
        if refined_records:
            start_date = refined_records[0]['time'][:10]
            end_date = refined_records[-1]['time'][:10]
            msg += f"기간: {start_date} ~ {end_date}\n\n"
        
        # [추가] 현재 보유 종목에 대한 총 매입금액, 평가손익, 수익률 계산
        holdings_summary = None
        try:
            target_cano = config.session.auto_cano
            with utils.AccountContext(target_cano):
                acnt = config.session.auto_acnt_prdt_cd
                holdings, summary = api.get_domestic_balance(target_cano, acnt)
                
                tot_pchs = 0
                tot_profit = 0
                tot_evlu = 0
                
                # [수정] API 요약 데이터 대신 보유 종목 합산
                if holdings:
                    valid_holdings = [h for h in holdings if int(h.get('hldg_qty', 0)) > 0]
                    if valid_holdings:
                        tot_pchs = sum(int(int(h['hldg_qty']) * float(h['pchs_avg_pric'])) for h in valid_holdings)
                        tot_profit = sum(int(h['evlu_pfls_amt']) for h in valid_holdings)
                        tot_evlu = sum(int(h['evlu_amt']) for h in valid_holdings)
                
                if tot_pchs > 0 or tot_profit != 0:
                    rate = (tot_profit / tot_pchs * 100) if tot_pchs > 0 else 0.0
                    holdings_summary = {'tot_pchs': tot_pchs, 'tot_evlu': tot_evlu, 'tot_profit': tot_profit, 'rate': rate}
        except Exception: pass
        
        if stats['sell_trades_exist']:
            win_rate = stats['win_rate']
            total_profit = stats['total_profit']
            avg_profit_rate = stats['avg_profit_rate']
            
            msg += f"[매매 현황 요약]\n"
            msg += f"총 매매: {stats['total_trades']}건 (매수 {stats['buy_count']} / 매도 {stats['sell_count']})\n"
            msg += f"승률: {win_rate:.1f}% ({stats['win_trades']}승 {stats['loss_trades']}패)\n"
            msg += f"건당 평균 수익률: {avg_profit_rate:+.2f}%\n"
            msg += f"건당 평균 보유: {stats['avg_holding_str']}\n"
            total_realized_rate = stats.get('total_realized_rate', 0.0)
            msg += f"총 실현 손익: {total_profit:+,}원 (매매원금 대비 {total_realized_rate:+.2f}%)\n"
            
            msg += f"\n[최고 / 최다 손익]\n"
            if stats.get('best_trade'):
                b = stats['best_trade']
                msg += f"최고 수익: {b['name']} ({b['profit_amt']:+,}원 / {b['profit_rate']:+.2f}%)\n"
            if stats.get('worst_trade'):
                w = stats['worst_trade']
                msg += f"최다 손실: {w['name']} ({w['profit_amt']:+,}원 / {w['profit_rate']:+.2f}%)\n"
            
            msg += f"\n[매수 사유 분포]\n"
            buy_reasons = stats.get('buy_reasons', {})
            total_buys = stats['buy_count']
            if total_buys > 0:
                for r, count in buy_reasons.most_common():
                    msg += f"• {r}: {count}건 ({count/total_buys*100:.1f}%)\n"
            else:
                msg += "• 매수 내역 없음\n"

            msg += f"\n[매도 사유 분포]\n"
            reasons = stats.get('sell_reasons', {})
            total_sells = stats['sell_count']
            if total_sells > 0:
                for r, count in reasons.most_common():
                    msg += f"• {r}: {count}건 ({count/total_sells*100:.1f}%)\n"
            else:
                msg += "• 매도 내역 없음\n"
                    
            if holdings_summary:
                msg += f"\n[현재 보유 현황]\n"
                msg += f"총 매입금액: {holdings_summary['tot_pchs']:,}원\n"
                msg += f"총 평가금액: {holdings_summary['tot_evlu']:,}원\n"
                msg += f"총 평가손익: {holdings_summary['tot_profit']:+,}원 ({holdings_summary['rate']:+.2f}%)\n"
        else:
            msg += f"[매매 현황 요약]\n"
            msg += f"총 매매: {stats['total_trades']}건 (매수 {stats['buy_count']} / 매도 {stats['sell_count']})\n"
            msg += "(청산된 내역이 없어 수익률 산출 불가)\n"
            
            if holdings_summary:
                msg += f"\n[현재 보유 현황]\n"
                msg += f"총 매입금액: {holdings_summary['tot_pchs']:,}원\n"
                msg += f"총 평가금액: {holdings_summary['tot_evlu']:,}원\n"
                msg += f"총 평가손익: {holdings_summary['tot_profit']:+,}원 ({holdings_summary['rate']:+.2f}%)\n"
            
        return msg.strip()

    def _calculate_statistics(self, records=None):
        if records is None: records = self.trade_records
        
        # [수정] 이미 정제된 레코드를 사용하므로 필터링 제거
        # 수동 매매('체결 확인')도 통계에 포함
        
        total_trades = len(records)
        buy_trades = [r for r in records if r['type'] == 'buy']
        sell_trades = [r for r in records if r['type'] == 'sell']
        
        win_trades = 0
        loss_trades = 0
        total_profit = 0
        total_profit_rate = 0.0
        total_buy_amt_for_sell = 0
        
        # [추가] Best/Worst 및 사유 분석 변수
        best_trade = None
        worst_trade = None
        sell_reasons = Counter()
        buy_reasons = Counter()
        
        # [추가] 보유 기간 계산
        total_holding_seconds = 0
        holding_count = 0
        buy_times = {} # code -> list of datetime

        # 시간순 처리를 위해 전체 기록 순회
        for r in records:
            code = r['code']
            try:
                dt = datetime.strptime(r['time'], "%Y-%m-%d %H:%M:%S")
            except Exception: continue

            if r['type'] == 'buy':
                if code not in buy_times: buy_times[code] = []
                buy_times[code].append(dt)
                
                reason_raw = r.get('reason', '')
                reason_key = "기타"
                if "조건 만족" in reason_raw:
                    if "슈퍼모멘텀" in reason_raw:
                        reason_key = "추격 매수 (돌파)"
                    else:
                        reason_key = "스코어 진입 (조건 만족)"
                elif "역매수" in reason_raw or "역추세" in reason_raw:
                    reason_key = "역추세 매수 (낙폭과대)"
                elif "수동" in reason_raw or "사용자 수동 주문" in reason_raw:
                    reason_key = "수동 매수"
                elif "예약" in reason_raw:
                    reason_key = "예약 매수"
                buy_reasons[reason_key] += 1
            elif r['type'] == 'sell':
                # 매도 시 매수 기록과 매칭 (FIFO: 먼저 산 것을 먼저 판다고 가정)
                if code in buy_times and buy_times[code]:
                    buy_dt = buy_times[code].pop(0)
                    diff = (dt - buy_dt).total_seconds()
                    total_holding_seconds += diff
                    holding_count += 1
                else:
                    # [추가] 기간 검색으로 인해 주어진 레코드에 매수 기록이 없는 경우 DB에서 과거 내역 조회
                    try:
                        from modules import db_manager
                        past_trades = db_manager.db.get_trades(code=code, limit=100)
                        for pt in past_trades:
                            pt_type = pt.get('type', '').lower()
                            if "buy" in pt_type or "매수" in pt_type:
                                pt_dt = datetime.strptime(pt['time'], "%Y-%m-%d %H:%M:%S")
                                if pt_dt < dt:
                                    diff = (dt - pt_dt).total_seconds()
                                    total_holding_seconds += diff
                                    holding_count += 1
                                    break
                    except Exception:
                        pass
        
        for t in sell_trades:
            profit = t.get('profit_amt', 0)
            rate = t.get('profit_rate', 0.0)
            total_profit += profit
            total_profit_rate += rate
            
            qty = int(float(t.get('qty', 0)))
            price = float(t.get('price', 0))
            sell_amt = qty * price
            buy_amt = sell_amt - profit
            total_buy_amt_for_sell += buy_amt
            
            if profit > 0: win_trades += 1
            else: loss_trades += 1
            
            # [추가] Best/Worst 갱신
            if best_trade is None or profit > best_trade.get('profit_amt', 0):
                best_trade = t
            if worst_trade is None or profit < worst_trade.get('profit_amt', 0):
                worst_trade = t
            
            # [추가] 매도 사유 분석
            reason = t.get('reason', '기타')
            reason_key = "기타"
            if "반익절" in reason: reason_key = "반익절"
            elif "과열" in reason: reason_key = "과열매도"
            elif "익절" in reason: reason_key = "익절"
            elif "ATR손절" in reason: reason_key = "ATR손절"
            elif "손절" in reason: reason_key = "손절"
            elif "트레일링" in reason: reason_key = "트레일링스탑"
            elif "시간청산" in reason: reason_key = "시간청산"
            elif "추세" in reason or "점수하락" in reason or "매도진입" in reason: reason_key = "추세이탈"
            elif "수동" in reason: reason_key = "수동매도"
            sell_reasons[reason_key] += 1
            
        avg_profit_rate = (total_profit_rate / len(sell_trades)) if sell_trades else 0.0
        win_rate = (win_trades / len(sell_trades) * 100) if sell_trades else 0.0
        total_realized_rate = (total_profit / total_buy_amt_for_sell * 100) if total_buy_amt_for_sell > 0 else 0.0

        # [추가] 평균 보유 기간 포맷팅
        avg_holding_str = "-"
        if holding_count > 0:
            avg_sec = total_holding_seconds / holding_count
            if avg_sec < 60: avg_holding_str = f"{int(avg_sec)}초"
            elif avg_sec < 3600: avg_holding_str = f"{int(avg_sec//60)}분 {int(avg_sec%60)}초"
            else: avg_holding_str = f"{int(avg_sec//3600)}시간 {int((avg_sec%3600)//60)}분"

        return {
            "total_trades": total_trades,
            "buy_count": len(buy_trades),
            "sell_count": len(sell_trades),
            "win_trades": win_trades,
            "loss_trades": loss_trades,
            "total_profit": total_profit,
            "total_realized_rate": total_realized_rate,
            "total_buy_amt_for_sell": total_buy_amt_for_sell, # [추가] 투자 원금 기준 알파 계산용
            "avg_profit_rate": avg_profit_rate,
            "win_rate": win_rate,
            "avg_holding_str": avg_holding_str,
            "sell_trades_exist": len(sell_trades) > 0,
            "best_trade": best_trade,
            "worst_trade": worst_trade,
            "sell_reasons": sell_reasons,
            "buy_reasons": buy_reasons
        }

    def _print_summary_table(self, stats, holdings_summary=None):
        summary_table = Table(title="트레이딩 성과 요약", title_justify="center", title_style="", box=box.HORIZONTALS, show_header=False, border_style="dim")
        summary_table.add_column("항목", style="cyan", justify="left")
        summary_table.add_column("값", justify="left")
        
        # [추가] 조회 기간 표시
        period_str = "전체"
        if getattr(self, 'trade_records', None) and len(self.trade_records) > 0:
            start_date = self.trade_records[0]['time'][:10]
            end_date = self.trade_records[-1]['time'][:10]
            period_str = f"{start_date} ~ {end_date}"
            
        summary_table.add_row("조회 기간", period_str)
        summary_table.add_row("총 매매 실행", f"{stats['total_trades']}건 (매수 {stats['buy_count']} / 매도 {stats['sell_count']})")
        
        if stats['sell_trades_exist']:
            summary_table.add_row("승률 (Win Rate)", f"{stats['win_rate']:.1f}% ({stats['win_trades']}승 {stats['loss_trades']}패)")
            
            # [추가] 시작 자산 및 현재 자산 비교 표시
            initial_asset = stats.get('initial_asset', 0)
            current_asset = stats.get('current_asset', 0)
            if initial_asset and current_asset > 0:
                asset_profit = current_asset - initial_asset
                asset_roi = (asset_profit / initial_asset) * 100
                summary_table.add_row("총 계좌 시작 자산", f"{int(initial_asset):,}원")
                summary_table.add_row("총 계좌 현재 자산", f"{current_asset:,}원")
                ap_color = "[red]" if asset_profit > 0 else ("[blue]" if asset_profit < 0 else "[white]")
                summary_table.add_row("총 계좌 자산 증감", f"{ap_color}{int(asset_profit):+,}원 ({asset_roi:+.2f}%)[/]")
                
            tp = stats['total_profit']
            tr_rate = stats.get('total_realized_rate', 0.0)
            summary_table.add_row("총 실현 손익", f"[red]{tp:+,}원 (매매원금 대비 {tr_rate:+.2f}%)[/]" if tp > 0 else f"[blue]{tp:+,}원 (매매원금 대비 {tr_rate:+.2f}%)[/]")
            
            total_buy = stats.get('total_buy_amt_for_sell', 0)
            sec_pl = holdings_summary['tot_profit'] if holdings_summary else 0
            sec_buy = holdings_summary['tot_pchs'] if holdings_summary else 0
            total_invested = total_buy + sec_buy
            total_net_profit = tp + sec_pl
            
            strategy_roi = 0.0
            if total_invested > 0:
                strategy_roi = (total_net_profit / total_invested) * 100
            
            sp_color = "[red]" if total_net_profit > 0 else ("[blue]" if total_net_profit < 0 else "[white]")
            summary_table.add_row("현재 전략 손익", f"{sp_color}{total_net_profit:+,}원 (실현+평가 손익 {strategy_roi:+.2f}%)[/]")

            apr = stats['avg_profit_rate']
            summary_table.add_row("건당 평균 수익률", f"[red]{apr:+.2f}%[/]" if apr > 0 else f"[blue]{apr:+.2f}%[/]")
            summary_table.add_row("건당 평균 보유", stats['avg_holding_str'])
            
            # [추가] 시장 대비 성과 표시
            kospi_rate = stats.get('kospi_rate', 0.0)
            k_color = "[red]" if kospi_rate > 0 else ("[blue]" if kospi_rate < 0 else "[white]")
            market_perf_str = f"코스피 지수: {k_color}{kospi_rate:+.2f}%[/]"

            if total_invested > 0:
                alpha = strategy_roi - kospi_rate
                a_color = "[red]" if alpha > 0 else ("[blue]" if alpha < 0 else "[white]")
                # [수정] 가독성 향상을 위해 초과/부진 여부 명시
                if alpha > 0:
                    alpha_label = "시장 대비 초과 수익 (Outperform)"
                else:
                    alpha_label = "시장 대비 성과 (Underperform)"
                market_perf_str += f" / {alpha_label}: {a_color}{alpha:+.2f}%[/]"
            summary_table.add_row("시장 대비 성과", market_perf_str)
        
        if holdings_summary:
            summary_table.add_section()
            summary_table.add_row("총 매입금액", f"{holdings_summary['tot_pchs']:,}원")
            summary_table.add_row("총 평가금액", f"{holdings_summary['tot_evlu']:,}원")
            hp = holdings_summary['tot_profit']
            hr = holdings_summary['rate']
            hc = "[red]" if hp > 0 else ("[blue]" if hp < 0 else "[white]")
            summary_table.add_row("총 평가손익", f"{hc}{hp:+,}원 ({hr:+.2f}%)[/]")
            
        console.print(summary_table)

    def _print_current_holdings(self, target_cano=None, target_acnt=None):
        try:
            # 컨텍스트 설정 (시스템 트레이딩 계좌 조회)
            if not target_cano:
                target_cano = config.session.auto_cano
                target_acnt = config.session.auto_acnt_prdt_cd
            with utils.AccountContext(target_cano):
                holdings, _ = api.get_domestic_balance(target_cano, target_acnt)
                
                if holdings:
                    console.print()
                    h_table = Table(title="현재 보유 종목 현황", title_justify="center", title_style="", box=box.HORIZONTALS, header_style="dim", border_style="dim")
                    h_table.add_column("종목명(코드)", justify="left")
                    h_table.add_column("보유수량", justify="right")
                    h_table.add_column("매입단가", justify="right")
                    h_table.add_column("현재가", justify="right")
                    h_table.add_column("평가손익", justify="right")
                    h_table.add_column("수익률", justify="right")
                    
                    for item in holdings:
                        name = item['prdt_name']
                        code = item['pdno']
                        qty = int(item['hldg_qty'])
                        buy_price = float(item['pchs_avg_pric'])
                        cur_price = int(item['prpr'])
                        profit = int(item['evlu_pfls_amt'])
                        rate = float(item['evlu_pfls_rt'])
                        
                        p_color = "[red]" if profit > 0 else ("[blue]" if profit < 0 else "[white]")
                        
                        h_table.add_row(f"{name}({code})", f"{qty:,}주", f"{buy_price:,.0f}원", f"{cur_price:,}원", f"{p_color}{profit:+,}원[/]", f"{p_color}{rate:+.2f}%[/]")
                    console.print(h_table)
        except Exception: pass

    def _print_stock_details(self):
        stock_stats = {}
        buy_times_per_stock = {} # 종목별 매수 시간 추적 (FIFO)

        # [수정] 이미 정제된 레코드를 사용하므로 필터링 제거
        filtered_records = self.trade_records

        for r in filtered_records:
            code = r['code']
            if code not in stock_stats:
                stock_stats[code] = {
                    'name': r['name'], 
                    'buy': 0, 'sell': 0, 
                    'profit': 0, 'rates': [], 'wins': 0,
                    'reasons': [], # 매도 사유 리스트
                    'holding_secs': [], # 보유 기간 리스트
                    'max_rate': -999.0, 'min_rate': 999.0,
                    'total_buy_amt': 0 # [추가] 총 매수 금액
                }
            if code not in buy_times_per_stock:
                buy_times_per_stock[code] = []
            
            try:
                dt = datetime.strptime(r['time'], "%Y-%m-%d %H:%M:%S")
            except Exception: dt = datetime.now()
            
            if r['type'] == 'buy':
                stock_stats[code]['buy'] += 1
                stock_stats[code]['total_buy_amt'] += int(float(r.get('price', 0) or 0) * float(r.get('qty', 0) or 0)) # [추가] 매수 금액 누적
                buy_times_per_stock[code].append(dt)
            elif r['type'] == 'sell':
                stock_stats[code]['sell'] += 1
                p = r.get('profit_amt', 0)
                rate = r.get('profit_rate', 0.0)
                
                stock_stats[code]['profit'] += p
                stock_stats[code]['rates'].append(rate)
                if p > 0: stock_stats[code]['wins'] += 1
                
                # 사유 분석 (익절/손절/추세 등 키워드 추출)
                reason_raw = r.get('reason', '')
                reason_simple = "기타"
                if "익절" in reason_raw: reason_simple = "익절"
                elif "손절" in reason_raw: reason_simple = "손절"
                elif "추세" in reason_raw: reason_simple = "추세이탈"
                stock_stats[code]['reasons'].append(reason_simple)
                
                # 최대/최소 수익률 갱신
                if rate > stock_stats[code]['max_rate']: stock_stats[code]['max_rate'] = rate
                if rate < stock_stats[code]['min_rate']: stock_stats[code]['min_rate'] = rate
                
                # 보유 기간 계산 (FIFO)
                if buy_times_per_stock[code]:
                    buy_dt = buy_times_per_stock[code].pop(0)
                    hold_sec = (dt - buy_dt).total_seconds()
                    stock_stats[code]['holding_secs'].append(hold_sec)

        if stock_stats:
            console.print()
            s_table = Table(title="종목별 성과 분석", title_justify="center", title_style="", box=box.HORIZONTALS, header_style="dim", border_style="dim")
            s_table.add_column("종목명(코드)", justify="left")
            s_table.add_column("매매(매수/매도)", justify="center")
            s_table.add_column("승률", justify="right")
            s_table.add_column("총 손익", justify="right")
            s_table.add_column("평균 수익률", justify="right")
            # [추가] 상세 정보 컬럼
            s_table.add_column("최대/최소", justify="right")
            s_table.add_column("주요 사유", justify="center")
            s_table.add_column("평균 보유", justify="right")

            for i, (code, stat) in enumerate(stock_stats.items()):
                s_cnt = stat['sell']
                win_rate = (stat['wins'] / s_cnt * 100) if s_cnt > 0 else 0.0
                avg_rate = (sum(stat['rates']) / s_cnt) if s_cnt > 0 else 0.0
                
                p_color = "[red]" if stat['profit'] > 0 else ("[blue]" if stat['profit'] < 0 else "[white]")
                r_color = "[red]" if avg_rate > 0 else ("[blue]" if avg_rate < 0 else "[white]")
                
                # 최대/최소 수익률 포맷팅
                #  색상은 '최대/최소'라는 자리가 아니라 값의 부호를 따른다(+는 빨강, -는 파랑).
                #  자리 고정 색상은 전 구간 손실 종목의 최대 수익률(-4.9%)까지 빨강으로 보여
                #  같은 표의 총 손익·평균 수익률 색상과 어긋났다.
                max_r = stat['max_rate'] if stat['max_rate'] != -999.0 else 0.0
                min_r = stat['min_rate'] if stat['min_rate'] != 999.0 else 0.0
                max_c = "[red]" if max_r > 0 else ("[blue]" if max_r < 0 else "[white]")
                min_c = "[red]" if min_r > 0 else ("[blue]" if min_r < 0 else "[white]")
                range_str = f"{max_c}{max_r:+.1f}%[/] / {min_c}{min_r:+.1f}%[/]" if s_cnt > 0 else "-"
                
                # 주요 매도 사유 (최빈값)
                reason_str = "-"
                if stat['reasons']:
                    c = Counter(stat['reasons'])
                    most_common = c.most_common(1)[0] # (사유, 횟수)
                    reason_str = f"{most_common[0]}({most_common[1]}회)"
                
                # 평균 보유 시간 포맷팅
                hold_str = "-"
                if stat['holding_secs']:
                    avg_sec = sum(stat['holding_secs']) / len(stat['holding_secs'])
                    if avg_sec < 60: hold_str = f"{int(avg_sec)}초"
                    elif avg_sec < 3600: hold_str = f"{int(avg_sec//60)}분"
                    else: hold_str = f"{int(avg_sec//3600)}시간"
                
                s_table.add_row(
                    f"{stat['name']} ({code})",
                    f"{stat['buy']} / {stat['sell']}",
                    f"{win_rate:.1f}%",
                    f"{p_color}{stat['profit']:+,}원[/]",
                    f"{r_color}{avg_rate:+.2f}%[/]",
                    range_str,
                    reason_str,
                    hold_str
                )
                
                # [추가] 5개마다 실선 추가
                if (i + 1) % 5 == 0 and (i + 1) < len(stock_stats):
                    s_table.add_section()
            console.print(s_table)
        
        # 상세 내역 테이블
        console.print()
        detail_table = Table(title="상세 매매 내역 (최신순)", title_justify="center", title_style="", box=box.HORIZONTALS, header_style="dim", border_style="dim")
        detail_table.add_column("시간", justify="center")
        detail_table.add_column("구분", justify="center")
        detail_table.add_column("종목명", justify="left")
        detail_table.add_column("수량", justify="right")
        detail_table.add_column("단가", justify="right")
        detail_table.add_column("매매금액", justify="right") # [추가]
        detail_table.add_column("손익(수익률)", justify="right")
        detail_table.add_column("사유", justify="left")
        
        # [수정] 필터링된 레코드 사용 (최신순 정렬)
        records = list(reversed(filtered_records))
        
        for i, r in enumerate(records):
            type_str = "[red]매수[/]" if r['type'] == 'buy' else "[blue]매도[/]"
            
            # [수정] 단가 포맷팅 (시장가 0원 처리)
            price_val = float(r.get('price', 0) or 0)
            qty_val = float(r.get('qty', 0) or 0)
            if price_val <= 0:
                price_str = "시장가"
                amt_str = "-"
            else:
                if price_val.is_integer():
                    price_str = f"{int(price_val):,}"
                else:
                    price_str = f"{price_val:,.2f}"
                
                trade_amt = int(price_val * qty_val)
                amt_str = f"{trade_amt:,}"
            
            profit_display = "-"
            reason_display = r.get('reason', '-')

            if r['type'] == 'sell':
                p_amt = r.get('profit_amt', 0)
                p_rate = r.get('profit_rate', 0.0)
                color = "[red]" if p_amt > 0 else "[blue]"
                profit_display = f"{color}{p_amt:+,}원 ({p_rate:+.2f}%)[/]"
            
            detail_table.add_row(
                r['time'][5:], # MM-DD HH:MM:SS
                type_str,
                f"{r['name']}",
                f"{r['qty']}",
                price_str,
                amt_str, # [수정]
                profit_display,
                reason_display
            )
            
            # [추가] 5개마다 실선 추가
            if (i + 1) % 5 == 0 and (i + 1) < len(records):
                detail_table.add_section()
            
        console.print(detail_table)

    def view_log_file(self):
        """현재 날짜의 시스템 트레이딩 로그 파일을 실시간으로 출력합니다."""
        utils.clear_screen()
        utils.print_breadcrumb()
        
        log_dir = getattr(config, 'SYSTEM_TRADING_LOG_DIR', 'logs')
        filename = "autotrade.log" # [수정] 고정 파일명 사용
        filepath = os.path.join(log_dir, filename)

        # [추가] 파일이 생성될 때까지 잠시 대기 (최대 10초) - 부팅 직후 실행 시 필요
        for _ in range(10):
            if os.path.exists(filepath): break
            time.sleep(1)

        if not os.path.exists(filepath):
            console.print(f"\n[yellow]로그 파일({filename})이 없습니다.[/yellow]")
            return

        console.print(f"\n[bold cyan]━━━ 실시간 로그 모니터링 ({filename}) ━━━[/bold cyan]")
        console.print("[dim]종료하려면 Ctrl+C를 누르세요.[/dim]\n")

        with Progress(
            SpinnerColumn(),
            TextColumn("[progress.description]{task.description}"),
            BarColumn(),
            console=console,
            transient=True
        ) as progress:
            progress.add_task("[cyan]로그 파일 로딩 중...[/cyan]", total=None)

        f = None
        try:
            f = open(filepath, 'r', encoding='utf-8')
            # 초기 출력: 최근 50줄
            lines = f.readlines()
            for line in lines[-50:]:
                console.print(escape(line.strip()))
            
            # 현재 파일의 inode 저장 (파일 교체 감지용)
            current_inode = os.fstat(f.fileno()).st_ino
            
            # 실시간 모니터링
            while True:
                # [추가] 로그 뷰어 실행 중에도 토큰 만료 체크 및 갱신 수행
                api.check_and_refresh_token_if_expired()

                line = f.readline()
                if line:
                    console.print(escape(line.strip()))
                else:
                    time.sleep(0.1)
                    # 파일 교체(로테이션) 감지
                    try:
                        if os.path.exists(filepath):
                            new_inode = os.stat(filepath).st_ino
                            if new_inode != current_inode:
                                # 파일이 교체됨 (자정 로테이션 등)
                                f.close()
                                f = open(filepath, 'r', encoding='utf-8')
                                current_inode = new_inode
                                console.print("\n[dim yellow]>>> 로그 파일이 교체되었습니다 (Log Rotation) <<<[/dim yellow]\n")
                    except Exception:
                        pass
        except KeyboardInterrupt:
            console.print("\n[yellow]로그 모니터링을 종료합니다.[/yellow]")
        except Exception as e:
            console.print(f"\n[red]로그 파일 읽기 오류: {e}[/red]")
        finally:
            if f and not f.closed:
                f.close()

    def is_market_open(self):
        """국내 정규장 운영 시간 확인 (공용 판정 함수 위임)"""
        return is_system_market_open()

    def _sync_external_fills(self, cano, acnt, holdings):
        """기동 시 외부 체결을 DB에 채우고, 자동 계좌 외부 매수분은 제한 종목으로 올린다.

        [왜 기동 경로인가] 실시간 감지(ConclusionMonitor)는 get_today_history 라 '오늘'만
          본다. 시스템이 꺼져 있던 날의 외부 체결은 다음 날 켜도 들어오지 않는다.

        [왜 제한까지 거는가] 자동 계좌에서 운용자가 직접 산 종목을 시스템이 '자기
          포지션'으로 알고 관리하면, 제 손절 기준으로 운용자의 포지션을 청산한다.
          실시간 경로는 이미 같은 처리를 하는데 기동 경로에만 이 방어가 없었다.
          수동 계좌는 시스템이 보지도 팔지도 않으므로 제한이 필요 없다.

        동기화 실패가 자동매매 기동 자체를 막아선 안 된다 — 기록은 부가 정보다.
        """
        is_auto_account = bool(cano and cano == getattr(config.session, 'auto_cano', None))
        try:
            res = holdings_backfill.sync_account(
                cano, acnt, holdings=holdings, register_restrictions=is_auto_account)
        except Exception as e:
            self.log(f"[기동 동기화] 외부 체결 동기화 실패 — 매매는 계속합니다: {e}")
            return

        if res.get('error'):
            self.log(f"[기동 동기화] 실패 — 매매는 계속합니다: {res['error']}")
            return
        if not res['written'] and not res['restricted']:
            return

        self.log(f"[기동 동기화] 외부 체결 {res['written']}건을 기록했습니다."
                 + (f" 제한 등록: {', '.join(res['restricted'])}" if res['restricted'] else ""))

        msg = f"🔄 [기동 동기화] 정지 중 발생한 외부 체결 {res['written']}건을 기록했습니다."
        if res['restricted']:
            # [판정 근거를 밝힌다] '외부 매수' 판정은 '우리 DB에 주문 기록이 없다'가 근거다.
            #  시스템이 낸 주문은 접수 응답 즉시 기록되므로 보통 맞지만, 주문 응답이 유실되고
            #  대사까지 실패하면(그때 '주문 결과 불명' 알림이 나간다) 기록 없이 체결만 남는다.
            #  그 포지션이 여기서 제한으로 잡히면 시스템이 손절해 주지 않는다 — 운용자가
            #  뒤집을 수 있도록 근거와 해제 경로를 함께 적는다.
            msg += ("\n\n⛔ 아래 종목은 운용자가 직접 매수한 것으로 보고 "
                    "시스템 매매에서 제외했습니다.\n· " + "\n· ".join(res['restricted']))
            msg += ("\n\n판정 근거는 '이 계좌의 매수인데 시스템 주문 기록이 없다'입니다. "
                    "직전에 '주문 결과 불명(응답 유실)' 알림을 받으셨다면 시스템이 낸 주문일 수 "
                    "있습니다 — 그 경우 제한을 풀어야 손절·트레일링이 다시 돕니다 "
                    "(자동매매 메뉴의 제한 종목 관리).")
        if res['partial']:
            msg += ("\n\n⚠️ 조회 구간보다 과거에 진입해 일부만 복원된 종목:\n· "
                    + "\n· ".join(f"{n}({c}) {m}주" for c, n, m in res['partial']))
        try:
            api.send_telegram_message(msg)
        except Exception:
            pass

    def _get_holdings_message(self, target_cano):
        """보유 종목 현황 메시지 생성 (장 시작/마감 알림용)"""
        msg = ""
        try:
            acnt = config.session.auto_acnt_prdt_cd
            holdings, _ = api.get_domestic_balance(target_cano, acnt)
            
            # [수정] 보유수량 0 초과인 종목만 필터링
            valid_holdings = [h for h in holdings if int(h.get('hldg_qty', 0)) > 0] if holdings else []

            if valid_holdings:
                from modules import account
                analysis_results = account.run_holding_analysis(valid_holdings, [], _pkg().get_restricted_stocks(*_get_trade_account()),
                                                           account=trade_account_key())
                msg += "\n\n" + _pkg().format_holdings_block(valid_holdings, analysis_results=analysis_results)
            else:
                msg += "\n\n📋 [보유 종목] 없음"
        except Exception as e:
            logger.error(f"보유 종목 조회 실패: {e}")
            msg += "\n\n(보유 종목 조회 실패)"
            
        return msg

    def _run_loop(self):
        my_thread = threading.current_thread()
        while self.is_running and self.thread is my_thread:
            try:
                self.last_cycle_at = datetime.now()
                target_cano = config.session.auto_cano
                with utils.AccountContext(target_cano):
                    current_market_status = self.is_market_open()
                    is_log_needed = current_market_status or getattr(self, '_first_loop_flag', True) or (self.was_market_open != current_market_status)
                    self._first_loop_flag = False
                    
                    if is_log_needed:
                        self.log("모니터링 주기 시작...")
                    
                    # [추가] Kill Switch: 체결 감시 시스템 상태 점검
                    # 체결 확인이 불가능한 상태에서는 신규 주문도 위험하므로 중단
                    conclusion_monitor = _pkg().ConclusionMonitor()
                    if not conclusion_monitor.is_healthy():
                        # 모니터를 즉시 깨워 재점검 유도 — 서버가 정상이면 카운터가 0으로 리셋되어
                        # 스스로 회복된다 (모니터가 조회를 쉬는 동안 카운터가 얼어붙는 교착 방지)
                        conclusion_monitor.check_now()
                        raise Exception(f"체결 감시 시스템 불안정 (연속 에러 {conclusion_monitor.consecutive_errors}회)")
                    
                    # [수정] 매 사이클 시작 시점에 수행하던 일일 손실 한도 강제 체크 로직 제거
                    # API Rate Limit 발생 시 잔고가 누락되어 가짜 비상 정지를 유발할 수 있으므로,
                    # API 호출 성공이 보장된 루프 후반부(_monitor_account_status)에서만 안전하게 손실 한도를 체크함
                    
                    # [추가] 현재 운용 계좌 정보 로깅
                    if target_cano and is_log_needed:
                        # [수정] 토스/모의는 단일계좌라 시스템 트레이딩 계좌 = 기본 계좌.
                        #        모드 플래그를 보지 않으면 토스가 '한투증권(자동)'으로 오표시되므로 is_toss도 분기.
                        # [Fix] 가상투자(mode 1)도 자기 분기가 없어 '한투증권(자동)'으로 떨어졌다.
                        #  실전 시세를 쓸 뿐 계좌는 가상이므로 실전 자동매매 계좌로 읽히면 위험하다.
                        #  라벨은 trading.py의 표기와 같은 '가상투자'로 맞춘다.
                        display_cano = target_cano
                        if getattr(config.session, 'is_paper', False):
                            acc_type = "가상투자"
                            # CANO는 fail-safe 센티널('PAPER')이라 계좌 식별에 쓸 수 없다.
                            #  VIRT_ACC_NUM을 표시 전용으로 읽어 어느 계좌 앞으로 도는지 남긴다.
                            _vc = getattr(config.session, 'virt_cano', '') or ''
                            _va = getattr(config.session, 'virt_acnt_prdt_cd', '') or ''
                            if _vc:
                                display_cano = f"{_vc}-{_va}" if _va else _vc
                        elif config.session.is_toss:
                            acc_type = "토스증권"
                        acc_type = "한투증권(자동)"
                        self.log(f"운용 계좌: {display_cano} [{acc_type}]")
                    
                    
                    # [추가] 날짜 변경 감지 및 당일 기준 자산 재설정 (무중단 24시간 운용 지원)
                    current_date = datetime.now().date()
                    if current_date > self.last_log_date:
                        self.log("━" * 80)
                        self.log(f"📅 날짜 변경 감지: {self.last_log_date} -> {current_date}")
                        self.log("당일 기준 자산(initial_asset)을 새로 측정하여 갱신합니다.")
                        self.log("━" * 80)
                        self.last_log_date = current_date
                        self.initial_asset = 0
                        self.baseline_principal = 0  # [추가] 입금 감지 기준 원금도 당일 첫 측정 시 재산정되도록 리셋

                        # [안전장치] 일일 손실 한도는 '당일 시작 자산' 기준이므로, 기준이 재측정되는
                        #  날짜 변경 시점에 방어 모드(신규 매수 중단)도 함께 해제한다.
                        if self.buy_halted:
                            self.resume_buys(reason="날짜 변경 — 일일 손실 한도 기준 재설정")
                            api.send_telegram_message("🔄 [방어 모드 해제] 날짜가 변경되어 신규 매수를 재개합니다.")

                        try:
                            acnt = config.session.auto_acnt_prdt_cd
                            
                            # [수정] 해외 자산 누락 방지
                            asset_data = account.get_asset_status_data(target_cano, acnt)
                            if asset_data and asset_data.get('tot_asset', 0) > 0:
                                self.initial_asset = asset_data['tot_asset']
                                save_daily_initial_asset(f"{target_cano}-{acnt}", self.initial_asset)
                                today_str = datetime.now().strftime("%Y-%m-%d")
                                acc_str = f"{target_cano}-{acnt}"
                                db_manager.db.save_daily_asset(today_str, acc_str, self.initial_asset)
                                self.log(f"[초기화 완료] 새로운 당일 시작 자산 갱신: {self.initial_asset:,}원")
                        except Exception as e:
                            self.log(f"당일 시작 자산 갱신 실패: {e}")

                    # [추가] 장 시작/마감 상태 변경 감지 및 로그
                    # [추가] 국내장 세션 단계 전환 감지 및 텔레그램 알림
                    # [수정] 마감/휴장은 같은 '거래 없음'으로 접어 자정 날짜 변경만으로 알림이
                    #        나가지 않게 한다(session_phase_key 주석 참고).
                    current_phase = _pkg().session_phase_key(api.domestic_session_phase())
                    if self.last_session_phase is None:
                        self.last_session_phase = current_phase
                    elif self.last_session_phase != current_phase:
                        self.last_session_phase = current_phase
                        phase_label = api.market_session_label(False, False)
                        if phase_label:
                            phase_text = phase_label[0]
                            self.log(f"🔔 [시장 상태 변경] 세션 전환: {phase_text}")
                            msg = f"🔔 [시장 상태 변경]\n현재 시장 세션이 다음으로 전환되었습니다:\n👉 {phase_text}"
                            api.send_telegram_message(msg)

                    if self.was_market_open is not None:
                        if not self.was_market_open and current_market_status:
                            now_time_str = datetime.now().strftime("%H%M")
                            self.log("━" * 80)
                            if "0900" <= now_time_str < "0910":
                                self.log(f"📢 [정규장 시작] 정규 주식 시장 거래가 개시되었습니다. ({datetime.now().strftime('%H:%M')})")
                                msg = "🔔 [정규장 시작] 정규 주식 시장 거래가 개시되었습니다."
                            elif "1530" <= now_time_str < "1540":
                                self.log(f"📢 [거래 재개] 단일가 매매 동기화가 완료되어 거래를 재개합니다. ({datetime.now().strftime('%H:%M')})")
                                msg = "🔔 [거래 재개] 휴게 시간이 종료되어 매매를 재개합니다."
                            else:
                                self.log(f"📢 [거래 시작] 시스템 트레이딩 거래가 시작되었습니다. ({datetime.now().strftime('%H:%M')})")
                                msg = "🔔 [장 시작] 거래 가능 시간이 되었습니다."
                            self.log("━" * 80)
                            
                            msg += self._get_holdings_message(target_cano)
                            api.send_telegram_message(msg)
                        elif self.was_market_open and not current_market_status:
                            if is_single_price_break():
                                self.log("━" * 80)
                                self.log(f"⏸️ [휴게 시간] 거래소 단일가 매매 동기화를 위해 잠시 매매를 멈춥니다. ({datetime.now().strftime('%H:%M')})")
                                self.log("━" * 80)
                                
                                msg = f"⏸️ [휴게 시간] 거래소 단일가 매매 동기화를 위해 잠시 매매를 멈춥니다.\n(해당 시간: {datetime.now().strftime('%H:%M')} ~)"
                                api.send_telegram_message(msg)
                            else:
                                self.log("━" * 80)
                                self.log(f"💤 [거래 종료] 시스템 트레이딩 거래가 종료되었습니다. ({datetime.now().strftime('%H:%M')})")
                                self.log("━" * 80)
                                
                                msg = "🌙 [장 마감] 거래 시간이 종료되었습니다."
                                msg += self._get_holdings_message(target_cano)
                                api.send_telegram_message(msg)
                                
                                # [추가] 장 마감 후 AI 마감 브리핑 자동 실행 (기존 포트폴리오 진단 대체)
                                try:
                                    from modules.scheduler import SystemScheduler
                                    threading.Thread(target=SystemScheduler().execute_daily_closing_report, daemon=True, name="DailyClosingReport").start()
                                    self.log("장 마감 종합 브리핑(AI) 작성을 백그라운드에서 시작합니다.")
                                except Exception as e:
                                    self.log(f"장 마감 브리핑 스케줄러 호출 실패: {e}")
                    
                    # [변경] 장 마감 시 분석 중단 (트래픽 감소)
                    if not current_market_status:
                        if is_log_needed:
                            if is_single_price_break():
                                self.log("시스템 상태: WAITING (단일가 매매 동기화 대기)")
                            elif api.is_holiday_today():
                                self.log("시스템 상태: WAITING (휴장일 - 분석 중지)")
                            else:
                                self.log("시스템 상태: WAITING (장 마감 - 분석 중지)")
                        self.was_market_open = current_market_status
                        # [관찰 모드] 종가 확정 후 마감 스냅샷 1회. 주기 스냅샷은 RUNNING
                        #  분기에만 있어 15:20 단일가 휴게부터는 찍히지 않는다.
                        if getattr(config.session, 'is_paper', False):
                            self._snapshot_paper_closing_equity()
                        # [관측성] 마감 후 청산 신호 1회 스캔. 분석을 통째로 멈추면 트래픽은
                        #  아끼지만, 종가가 확정된 뒤 손절·트레일링선을 이탈한 사실을 아무도
                        #  모르는 채로 다음 개장까지 간다 — 갭이 그대로 손실이 되는 구간이다.
                        #  주문은 내지 않는다(낼 수 없다). 알림만 보낸다.
                        self._scan_after_hours_sell_signals(target_cano)
                    else:
                        status_msg = "RUNNING"
                        self.log(f"시스템 상태: {status_msg}")
                        
                        # [추가] 시장 지수 상태 업데이트 (KOSPI/KOSDAQ)
                        if getattr(config, 'USE_MARKET_FILTER', True):
                            self._update_market_indices_status()

                        # [리스크 스케일링] 약세 국면·계좌 드로다운 반영 리스크 한도 배수 갱신 (주기당 1회)
                        self._update_risk_scale()

                        # [관찰 모드] 가상 자산 일별 스냅샷 — 자산곡선·MDD 산출의 유일한 소스다.
                        #  같은 날 재호출은 덮어쓰므로 주기마다 불러도 하루 1행만 남는다.
                        if getattr(config.session, 'is_paper', False):
                            from modules import paper_broker
                            paper_broker.snapshot_equity()

                        # [최적화] 계좌 정보(잔고, 예수금)를 루프 시작 시 1회만 조회하여 공유
                        # 2 TPS 환경에서 중복 조회를 방지하여 성능 확보
                        acnt = config.session.auto_acnt_prdt_cd
                        
                        # 1. 잔고 조회
                        # [수정] 초기 구동 시 메인 스레드에서 조회한 데이터 재사용 (API 호출 절약)
                        if self.initial_holdings is not None:
                            holdings = self.initial_holdings
                            summary = self.initial_summary
                            self.initial_holdings = None
                            self.initial_summary = None
                        else:
                            holdings, summary = api.get_domestic_balance(target_cano, acnt)
                        
                        # [수정] 잔고 조회 실패 시 예외 발생 (Kill Switch 연동)
                        # 계좌 상태를 모르는 상태에서 매매를 진행하는 것은 위험함
                        if holdings is None:
                            raise Exception("잔고 조회 실패 (API 응답 없음)")

                        # [안전장치] '빈 잔고'는 조회 실패와 구분되지 않는다.
                        #  rt_cd='0' + output1=[] 는 정상적인 '보유 없음'이기도 하지만, 토스가
                        #  items를 비워 응답하거나 페이징이 어긋나면 같은 모양이 된다. 이때
                        #  len(holding_codes)==0 이 되어 매수 슬롯이 전부 열리므로, 실제로는
                        #  보유 중인데 SYSTEM_MAX_HOLDINGS 만큼 추가 매수가 나갈 수 있다.
                        #  → 직전 주기에 보유가 있었는데 0건이 되면 이번 주기 매수를 보류하고
                        #    다음 조회에서 다시 0건일 때만 진짜 청산으로 수용한다(1주기 지연).
                        #  매도는 막지 않는다. 보유가 없으면 어차피 할 일이 없고, 실재한다면
                        #  다음 주기에 다시 잡혀 손절 판정이 늦어질 이유가 없다.
                        #  재확인은 last_holdings_count 만으로 성립한다. 보류한 주기에 이 값이
                        #  0으로 내려가므로, 다음 주기가 또 0건이면 조건이 성립하지 않아 통과한다.
                        balance_unconfirmed = False
                        if not holdings and self.last_holdings_count > 0:
                            balance_unconfirmed = True
                            self.log(f"[잔고 확인] 보유 {self.last_holdings_count}건 → 0건. "
                                     f"조회 이상 가능성으로 이번 주기 매수를 보류하고 재확인합니다.")
                            logger.warning(f"[잔고 확인] 보유 종목이 {self.last_holdings_count}건에서 "
                                           f"0건으로 바뀌어 매수를 1주기 보류")
                        self.last_holdings_count = len(holdings)

                        # 2. 예수금 조회
                        # [최적화] 모의투자는 잔고 조회 결과(summary)에 예수금이 포함되어 있어 별도 호출 불필요
                        deposit_res = None
                        # [수정] 실전/모의 모두 summary 정보 우선 활용
                        if summary:
                            dnca = api.safe_int(summary[0].get('dnca_tot_amt', 0))
                            d2_dep = api.safe_int(summary[0].get('prvs_rcdl_excc_amt', 0))
                            deposit_res = {'deposit': dnca, 'foreign_deposit': 0, 'd2_deposit': d2_dep}
                        
                        # 예수금이 0이거나 실전투자에서 정밀 조회가 필요한 경우 Fallback
                        if (not deposit_res or deposit_res['deposit'] == 0):
                            deposit_res = api.get_deposit_balance(target_cano, acnt, skip_balance_check=True)
                        
                        # API 호출 간격 조절 (Rate Limit 방지)
                        time.sleep(0.2)

                        # [최적화] 이번 주기 주문 활동 감지용 스냅샷
                        #  (주문이 하나도 없었다면 루프 말미 잔고/예수금 재조회를 생략해 REST 콜 절감)
                        with self.order_manager._lock:
                            _pending_before = bool(self.order_manager.pending_orders)
                        _sent_before = self.order_manager.orders_sent_count

                        # [최적화] 개별 룰(DB)·트레이딩 제한 종목(파일)을 주기당 1회만 로드해
                        #  매도/매수 검사에 공유 (기존: 각 검사가 개별 로드 → DB 연결·파일 I/O 2회씩)
                        _cycle_rules = _enrich_rules_with_weights(db_manager.db.get_all_stock_strategies())
                        _cycle_rules_map = {r['code']: r for r in _cycle_rules}
                        _cycle_cano, _cycle_acnt = _get_trade_account()
                        _cycle_restricted = get_restricted_stocks(_cycle_cano, _cycle_acnt)

                        # [수정] 락 범위 축소: 전체 로직을 감싸던 락 제거 (api.call_api 내부 락 활용)
                        # 1. 매도 조건 점검 (리스크 관리)
                        self._check_sell_conditions(holdings, current_market_status, rules_map=_cycle_rules_map, restricted_stocks=_cycle_restricted)
                        # 2. 매수 조건 점검 ([안전장치] 잔고 0건 재확인 대기 중에는 건너뛴다)
                        if not balance_unconfirmed:
                            self._check_buy_conditions(holdings, deposit_res, current_market_status, rules_map=_cycle_rules_map, restricted_stocks=_cycle_restricted)
                        # 3. 미체결 주문 관리 (오래된 주문 취소) - 장 중에만 수행
                        self.order_manager.manage_unfilled_orders()
                        # 4. DB 쓰기 실패 확인 — 실패는 인메모리 캐시에 가려 세션 중엔
                        #    안 보이고, 재기동해야 소실이 드러난다. 그 전에 알린다.
                        self._check_db_write_failures()

                        # [수정] 루프 동안 매수/매도가 발생한 경우에만 최종 로깅 전 잔고와 예수금을 갱신.
                        #  주기 시작 시(직전) 조회한 스냅샷이 있고 주문 활동이 전혀 없었다면 계좌 상태가
                        #  변하지 않았으므로 재조회를 생략한다 (2 TPS 모의 환경에서 주기당 2~3콜+0.7초 절감).
                        with self.order_manager._lock:
                            _pending_after = bool(self.order_manager.pending_orders)
                        _had_order_activity = (
                            _pending_before or _pending_after
                            or self.order_manager.orders_sent_count > _sent_before
                        )
                        if _had_order_activity:
                            time.sleep(0.5)
                            try:
                                upd_holdings, upd_summary = api.get_domestic_balance(target_cano, acnt)
                                if upd_holdings is not None:
                                    holdings = upd_holdings
                                    summary = upd_summary

                                upd_dep = api.get_deposit_balance(target_cano, acnt, skip_balance_check=True)
                                if upd_dep: deposit_res = upd_dep
                            except Exception as e:
                                logger.debug(f"최종 상태 로깅을 위한 잔고 갱신 실패: {e}")

                        # [추가] 보유 종목 상태 로깅 및 자산 안전장치 체크
                        self._monitor_account_status(holdings, summary, deposit_res)
                        
                        # [추가] 관심 종목 변경 및 분석 제외 종목 메모리 캐시 정리
                        try:
                            valid_codes = {h['pdno'] for h in holdings}
                            for key in ["stocks_kr", "etfs_kr", "stocks_us", "etfs_us"]:
                                for item in config.session.stock_data.get(key, []):
                                    valid_codes.add(item['code'])
                                    
                            context.prune_stock_states(valid_codes)
                        except Exception as e:
                            logger.debug(f"상태 캐시 정리 중 오류: {e}")
                    
                    self.was_market_open = current_market_status
                    
                    # [계측] 주기 소요 시간 — SYSTEM_TRADING_INTERVAL은 '주기 후 쉬는 시간'이므로
                    #  실제 감시 간격은 (이 소요 시간 + interval)이다. 관심종목을 늘리면 이 값이
                    #  길어지고 그만큼 손절·트레일링 감시가 늦어지므로, 유니버스 상한의 실질 기준이 된다.
                    self._record_cycle_duration((datetime.now() - self.last_cycle_at).total_seconds(),
                                                log=is_log_needed)

                interval = getattr(config, 'SYSTEM_TRADING_INTERVAL', 60)
                
                # [수정] 미체결 주문 확인 시 발생하는 API 호출 지연(Delay)이 누적되어
                # 모니터링 주기가 설정값(180초)을 크게 초과하는 문제를 해결하기 위해 절대 시간 기반 대기 적용
                wait_start_time = time.time()
                last_unfilled_check = wait_start_time
                last_idle_unfilled_check = wait_start_time
                
                while self.is_running:
                    now = time.time()
                    if now - wait_start_time >= interval:
                        break
                        
                    # 5초 주기 도달 시
                    if now - last_unfilled_check >= 5:
                        last_unfilled_check = time.time()
                        
                        with self.order_manager._lock:
                            has_pending = bool(self.order_manager.pending_orders)
                            
                        # 시스템 내부에 대기 중인 미체결 주문이 있다면 5초 주기로 즉각 확인
                        # 없다면 외부 HTS 등 타 매체 주문 감지를 위해 60초 간격으로만 최소한의 API 확인 수행
                        if has_pending or (now - last_idle_unfilled_check >= 60):
                            self.order_manager.manage_unfilled_orders()
                            last_idle_unfilled_check = time.time()
                            
                    time.sleep(1)
                
                # 정상 루프 완료 시 에러 카운트 초기화
                self.consecutive_errors = 0
                self.last_success_at = datetime.now()
                    
            except Exception as e:
                self.last_error_at = datetime.now()
                self.last_error_message = str(e)
                self.consecutive_errors += 1
                max_err = getattr(config, 'SYSTEM_MAX_CONSECUTIVE_ERRORS', 5)
                self.log(f"에러 발생({self.consecutive_errors}/{max_err}): {str(e)}")
                logger.error(f"시스템 트레이딩 루프 예외 발생 ({self.consecutive_errors}/{max_err}): {str(e)}")
                console.print(f"[dim red]⚠️ 에러 발생: {str(e)}[/dim red]")
                if config.SCREEN_DEBUG_LEVEL in ["ERROR", "TRACE", "DEBUG"]:
                    console.print(f"[bold red][ERROR] 시스템 트레이딩 루프 예외 발생 ({self.consecutive_errors}/{max_err}): {str(e)}[/bold red]")
                
                if self.consecutive_errors >= max_err:
                    # [안전장치] 대기에 들어가기 전에 **정말 서버가 죽었는지** 먼저 확인한다.
                    #  consecutive_errors는 API 오류만이 아니라 루프의 모든 예외에 오른다.
                    #  코드 쪽 예외로 한도가 차면 서버가 멀쩡한데도 아래 대기 루프가 주기를
                    #  통째로 붙잡고, 그 동안 매도 검사가 돌지 않아 손절·트레일링이 무감시가
                    #  된다. 일일 손실 한도에서 이미 같은 결함을 고쳤다(engine.check_loss_limit
                    #  주석: "정지는 매도 감시까지 꺼서 무방비 상태를 만든다").
                    if self._errors_are_not_the_server(str(e)):
                        self.consecutive_errors = 0
                        time.sleep(10)
                        continue        # 대기하지 않는다 — 다음 주기에 매도 검사를 다시 돈다

                    # [수정] 중단 대신 대기 모드로 전환
                    self.log(f"[장애 감지] 연속 에러 {max_err}회 발생. 서버 장애로 판단하여 대기 모드로 전환합니다.")

                    # [개선] 상세 알림 메시지 구성
                    err_reason = str(e)
                    if config.SCREEN_DEBUG_LEVEL in ["ERROR", "TRACE", "DEBUG"]:
                        console.print(f"\n[bold red][ERROR] 연속 에러 {max_err}회 초과! 자동매매 시스템이 대기 모드(정지)로 전환되었습니다. (사유: {err_reason})[/bold red]\n")
                    msg = f"🚨 [시스템 긴급 대기] 연속 에러 {max_err}회 발생\n매매를 일시 중단하고 서버 복구를 대기합니다.\n\n원인: {err_reason}\n\n서버 복구 확인 시 자동으로 재개됩니다."

                    # [추가] 진입 알림 쿨타임(10분) — 대기/복구가 짧은 주기로 반복(진동)해도 스팸 방지
                    now = time.time()
                    if now - self.last_wait_alert_time > 600:
                        self.last_wait_alert_time = now
                        self._wait_alert_sent = True

                        # [추가] 에러 로그 꼬리 첨부 (1시간 쿨타임)
                        if now - self.last_emergency_alert_time > 3600:
                            log_tail = get_mystock_log_tail(20)
                            msg += f"\n\n📜 [최근 시스템 로그 (mystock.log)]\n```\n{log_tail}```"
                            self.last_emergency_alert_time = now

                        api.send_telegram_message(msg)
                    
                    # 대기 중임을 남긴다 — 정체 감시(scheduler._check_heartbeat)가 이
                    #  '의도된 멈춤'을 장애로 오탐하면 서버 장애 때마다 가짜 경보가 나간다.
                    self.waiting_for_server = True
                    try:
                        self._wait_for_server_recovery()
                    finally:
                        self.waiting_for_server = False

                    # 복구되어 리턴되면 에러 카운트 초기화 후 루프 재개
                    self.consecutive_errors = 0
                    continue
                
                time.sleep(10)

    def _errors_are_not_the_server(self, err_reason):
        """서버는 멀쩡한데 우리 쪽에서 터지고 있는가.

        [왜 구분하는가] 서버 장애라면 대기가 옳다 — 주문 자체가 나갈 수 없으니 붙잡고
        있어도 잃을 게 없다. 그러나 코드 쪽 예외라면 대기는 최악이다. 주문은 나갈 수
        있는데 매도 검사를 멈춰 손절·트레일링만 꺼진다.

        참이면 대기하지 않고 다음 주기로 넘어간다. 버그가 계속되면 에러가 다시 쌓여
        여기로 돌아오지만, 그 사이 **매 주기 매도 검사는 돈다**. 진동하더라도 무감시보다
        낫다(루프 말미 sleep이 하한을 잡아 준다).
        """
        try:
            healthy = api.check_server_health()
        except Exception:
            return False        # 확인조차 안 되면 서버 문제로 보고 대기한다(보수적)
        if not healthy:
            return False

        self.code_error_streaks = getattr(self, 'code_error_streaks', 0) + 1
        self.log(f"[장애 판정 정정] 연속 에러가 쌓였지만 서버는 정상입니다 — 코드 쪽 오류로 "
                 f"보고 대기하지 않습니다(매도 감시 유지). 원인: {err_reason}")
        logger.error(f"[킬스위치] 서버 정상 · 코드 오류 추정({self.code_error_streaks}회): {err_reason}")

        now = time.time()
        if now - getattr(self, '_code_error_alerted_at', 0.0) > CODE_ERROR_ALERT_COOLDOWN:
            self._code_error_alerted_at = now
            api.send_telegram_message(
                f"⚠️ [반복 오류] 연속 에러가 한도에 닿았으나 증권사 서버는 정상입니다.\n"
                f"코드 쪽 오류로 보고 **대기하지 않습니다**(매도·손절 감시 유지).\n"
                f"신규 매수는 계속되므로 원인 확인이 필요합니다.\n\n원인: {err_reason}")
        return True

    def _wait_for_server_recovery(self):
        """서버가 정상화될 때까지 대기"""
        check_interval = 60 # 1분마다 확인
        
        while self.is_running:
            time.sleep(check_interval)
            
            self.log("[장애 대기] 서버 상태 점검 중...")
            
            try:
                # 삼성전자 현재가 조회로 서버 상태 확인
                if api.check_server_health():
                    self.log("[서버 복구] 서버 정상화 확인. 매매를 재개합니다.")
                    # [추가] 서버 정상화가 확인되었으므로 체결 감시 에러 카운트도 리셋
                    # (장애 중 누적된 카운터가 Kill Switch를 계속 걸어 대기/복구가
                    #  무한 반복되는 교착 방지 — 이후 조회가 다시 실패하면 재누적됨)
                    _pkg().ConclusionMonitor().consecutive_errors = 0
                    # [수정] 진입 알림을 보냈을 때만 복구 알림 발송 (쿨타임으로 진입 알림이
                    # 생략된 반복 진동 구간에서는 복구 알림도 생략해 스팸 방지)
                    if self._wait_alert_sent:
                        self._wait_alert_sent = False
                        api.send_telegram_message("✅ [서버 복구] KIS 서버가 정상화되었습니다.\n자동매매를 재개합니다.")
                    return
                else:
                    self.log("[장애 대기] 서버 여전히 응답 없음.")
            except Exception as e:
                self.log(f"[장애 대기] 점검 중 오류: {e}")


    def _run_account_circuit_breaker(self, current_total):
        """계좌 차단기 — 일일 손실 한도 점검과 기준 평가자산 갱신.

        [왜 따로 떼는가] 이 호출은 표시·로깅과 같은 try 블록에 있으면 안 된다. 감싸는
        핸들러가 `except Exception: pass` 라서, 손익 표시나 입출금 감지가 던지는 순간
        차단기가 **조용히** 건너뛰어진다. 게다가 그런 예외는 손실이 큰 날에 더 잘 난다
        (다룰 값이 많아지므로) — 정확히 차단기가 필요한 날에 꺼지는 구조였다.

        여기서도 예외를 잡되 **삼키지 않는다**. 실패를 세어 상태창에 드러내고, 반복되면
        알린다. 차단기가 안 도는 것을 아무도 모르는 상태가 가장 나쁘다.
        """
        try:
            # 비정상 급감(API 누락 의심) 데이터는 기준자산에 반영하지 않는다.
            # [Fix 2026-09-01] 기준을 initial_asset(원본)이 아니라 **입출금 보정된 기준선**과
            #  대야 한다. 자산의 절반이 넘는 출금은 정상 거래인데, 원본과 대면 그날 내내
            #  '비정상 급감'으로 읽혀 current_total_asset 이 출금 전 값에 얼어붙는다. 그 값은
            #  히트 캡의 분모이자 드로다운의 현재 자산이므로, 없는 돈 기준으로 한도가
            #  계산되고 드로다운은 과소평가된다(= 한도가 조용히 열린다).
            #  check_loss_limit 은 이미 같은 판정을 보정된 기준선으로 한다 — 둘을 맞춘다.
            _floor_base = self.effective_baseline() or self.initial_asset
            if current_total > 0 and not (_floor_base > 0
                                          and current_total < _floor_base * 0.5):
                self.current_total_asset = current_total
            self.risk_manager.check_loss_limit(current_total)
            self.circuit_breaker_ran_at = time.time()
            self.circuit_breaker_fails = 0
        except Exception as e:
            self.circuit_breaker_fails = getattr(self, 'circuit_breaker_fails', 0) + 1
            logger.error(f"[계좌 차단기] 일일 손실 한도 점검 실패({self.circuit_breaker_fails}회): {e}")
            self.log(f"[계좌 차단기] 점검 실패 — 일일 손실 한도가 감시되지 않고 있습니다: {e}")
            if self.circuit_breaker_fails == CIRCUIT_BREAKER_ALERT_FAILS:
                api.send_telegram_message(
                    f"⚠️ [계좌 차단기 이상] 일일 손실 한도 점검이 {self.circuit_breaker_fails}회 "
                    f"연속 실패했습니다.\n손실이 한도를 넘어도 신규 매수가 차단되지 않습니다.\n"
                    f"오류: {e}")

    def _monitor_account_status(self, holdings, summary, deposit_res):
        """현재 보유 종목 상태 로깅 및 자산 손실 제한(Loss Cut) 체크"""
        try:
            # [수정] 보유수량 0 초과인 종목만 필터링
            valid_holdings = [h for h in holdings if int(h.get('hldg_qty', 0)) > 0]
            
            if not valid_holdings:
                self.log("보유 종목: 없음")
            else:
                # 한글 정렬 보정 헬퍼 함수
                def get_display_width(s):
                    return len(s) + sum(1 for c in s if ord(c) > 127)

                def pad(s, width, align='>'):
                    real_len = get_display_width(s)
                    pad_len = width - real_len
                    if pad_len < 0: pad_len = 0
                    if align == '<': return s + ' ' * pad_len
                    else: return ' ' * pad_len + s

                max_name_width = 20
                for item in valid_holdings:
                    name = f"{item['prdt_name']} ({item['pdno']})"
                    w = get_display_width(name)
                    if w > max_name_width:
                        max_name_width = w
                        
                name_col_width = max(30, max_name_width + 2)
                line_length = name_col_width + 95

                # 헤더 출력
                header = (
                    f"{pad('종목명', name_col_width, '<')} "
                    f"{pad('보유수량', 10, '>')} "
                    f"{pad('매입단가', 12, '>')} "
                    f"{pad('현재가', 12, '>')} "
                    f"{pad('매입금액', 15, '>')} "
                    f"{pad('평가금액', 15, '>')} "
                    f"{pad('평가손익', 14, '>')} "
                    f"{pad('수익률', 10, '>')}"
                )
                
                self.log("─" * line_length)
                self.log(header)
                self.log("─" * line_length)
                
                for item in valid_holdings:
                    name = f"{item['prdt_name']} ({item['pdno']})"
                    qty = int(item['hldg_qty'])
                    buy_price = float(item['pchs_avg_pric'])
                    cur_price = int(item['prpr'])
                    # 매입금액: 실전 잔고(INQR_DVSN=01)·토스 어댑터는 pchs_amt가 0/누락으로 오므로
                    # 아래 합계 줄과 동일하게 평단×수량으로 복원한다.
                    pchs_amt = api.safe_int(item.get('pchs_amt')) or int(qty * buy_price)
                    eval_amt = int(item.get('evlu_amt', 0))
                    profit = int(item['evlu_pfls_amt'])
                    rate = float(item['evlu_pfls_rt'])
                    
                    row_str = (
                        f"{pad(name, name_col_width, '<')} "
                        f"{pad(f'{qty:,}주', 10, '>')} "
                        f"{pad(f'{buy_price:,.0f}원', 12, '>')} "
                        f"{pad(f'{cur_price:,.0f}원', 12, '>')} "
                        f"{pad(f'{pchs_amt:,}원', 15, '>')} "
                        f"{pad(f'{eval_amt:,}원', 15, '>')} "
                        f"{pad(f'{profit:+,}원', 14, '>')} "
                        f"{pad(f'{rate:.2f}%', 10, '>')}"
                    )
                    self.log(row_str)
                
                self.log("─" * line_length)
                if summary and len(summary) > 0:
                    s_data = summary[0]
                    
                    # [수정] API 지연에 따른 왜곡 방지를 위해 보유 종목 개별 합산 값 사용
                    tot_pchs = 0
                    total_profit = 0
                    total_eval = 0
                    if valid_holdings:
                        tot_pchs = sum(int(int(h['hldg_qty']) * float(h['pchs_avg_pric'])) for h in valid_holdings)
                        total_profit = sum(int(h['evlu_pfls_amt']) for h in valid_holdings)
                        total_eval = sum(int(h['evlu_amt']) for h in valid_holdings)
                    
                    # 총 자산 계산 (예수금 + 평가금)
                    current_total = 0
                    deposit_d2 = 0
                    if deposit_res:
                        deposit_d2 = deposit_res.get('d2_real', 0)
                        if deposit_d2 == 0:
                            deposit_d2 = deposit_res.get('d2_deposit', 0)
                    
                    # [수정] account 모듈을 활용하여 해외 자산까지 완벽하게 포함된 총 자산 획득
                    target_cano = config.session.auto_cano
                    acnt_cd = config.session.auto_acnt_prdt_cd
                    
                    asset_data = account.get_asset_status_data(target_cano, acnt_cd)
                    
                    # [Fix] API 지연/오류로 인해 account 모듈 내부에서 주식 잔고가 누락(0)된 경우 감지
                    is_asset_broken = False
                    if asset_data and total_eval > 0 and asset_data.get('sec_eval', 0) == 0:
                        is_asset_broken = True

                    if asset_data and not is_asset_broken:
                        current_total = asset_data.get('tot_asset', 0)
                        order_possible = asset_data.get('order_possible', deposit_d2)
                    else:
                        # Fallback (API 실패 시 기존 로직으로 대안 계산)
                        cash = deposit_d2 + (deposit_res.get('foreign_deposit', 0) if deposit_res else 0)
                        current_total = cash + total_eval
                        order_possible = deposit_res.get('order_possible', deposit_d2) if deposit_res else deposit_d2
                        
                        if is_asset_broken:
                            self.log(f"⚠️ 통합 자산 조회 이상 감지 (API 지연 추정). 안전을 위해 이전 자산({current_total:,}원)으로 대체합니다.")

                    # [Fix] 토스: 미체결 매수 주문에 묶인 현금을 자산에 보정한다.
                    # (매수가능금액은 예약 현금을 제외하므로, 주문 접수/취소 시 자산이 출렁여
                    #  '가짜 입금' 자동 감지 및 손실률 왜곡을 유발한다.)
                    # 조회 실패 시 보정값을 신뢰할 수 없으므로, 이번 주기의 입금 자동 감지는 건너뛴다.
                    toss_cash_reliable = True
                    if config.session.is_toss and current_total > 0:
                        try:
                            reserved_buy_cash = self._get_toss_open_buy_reserved(target_cano, acnt_cd)
                            if reserved_buy_cash > 0:
                                current_total += reserved_buy_cash
                                self.log(f"[토스 자산 보정] 미체결 매수 예약 현금 {reserved_buy_cash:,}원을 자산에 합산했습니다. (보정 후 총자산: {current_total:,}원)")
                        except Exception as e:
                            toss_cash_reliable = False
                            logger.debug(f"[Toss] 미체결 매수 예약 현금 조회 실패(입금 감지 스킵): {e}")

                    # [추가] 일일 손실 제한 체크
                    if current_total > 0:
                        is_first_init = False
                        # [Fix] 초기 자산 로드 실패(0원) 시, 첫 유효 조회 값으로 보정
                        if self.initial_asset == 0:
                            is_first_init = True
                            target_cano = config.session.auto_cano
                            acnt = config.session.auto_acnt_prdt_cd
                            acc_str = f"{target_cano}-{acnt}"
                            saved_initial = load_daily_initial_asset(acc_str)
                            if saved_initial > 0:
                                self.initial_asset = saved_initial
                                self.log(f"[시스템 보정] 기존 초기 자산 기록 로드: {self.initial_asset:,}원")
                            elif is_plausible_baseline(acc_str, current_total):
                                self.initial_asset = current_total
                                save_daily_initial_asset(acc_str, self.initial_asset)
                                self.log(f"[시스템 보정] 초기 자산 정보 갱신 및 저장: {self.initial_asset:,}원")
                            else:
                                # [Fix 2026-09-01] 여기에 검사가 없어 initialize()의 안전장치가
                                #  무력했다. 기동 경로가 '직전 기록의 반토막 이하'라며 거부한
                                #  값을 이 루프가 한 주기 뒤 그대로 기준선으로 박았기 때문이다.
                                #  기준선이 작게 박히면 손실률이 늘 큰 양수라 **차단기가 종일
                                #  발동하지 않는다**. 0으로 두고(=차단기 스킵·사이징은 예수금
                                #  폴백) 다음 주기에 다시 잰다 — 시세가 돌아오면 스스로 낫는다.
                                today_str = datetime.now().strftime("%Y-%m-%d")
                                if getattr(self, '_baseline_reject_date', None) != today_str:
                                    self._baseline_reject_date = today_str
                                    warn = (f"⚠️ [시작 자산 이상] 조회값 {current_total:,}원이 직전 "
                                            f"기록 대비 지나치게 작습니다.\n시세 결손(주식 평가액 0 "
                                            f"수신)이 의심되어 오늘 기준 자산으로 삼지 않습니다.\n"
                                            f"계좌 차단기(일일 손실 한도)가 그때까지 동작하지 "
                                            f"않습니다 — 시세가 정상화되면 스스로 잡습니다.")
                                    self.log(warn)
                                    try:
                                        api.send_telegram_message(warn)
                                    except Exception:
                                        pass

                            # [추가] DB에 기록
                            if self.initial_asset > 0:
                                try:
                                    today_str = datetime.now().strftime("%Y-%m-%d")
                                    db_manager.db.save_daily_asset(today_str, acc_str, self.initial_asset)
                                except Exception: pass

                        # [안전장치] 계좌 차단기(일일 손실 한도)를 **표시 코드보다 먼저** 돌린다.
                        #  종전에는 이 함수 맨 끝(265줄 아래)에 있었고 함수 전체가
                        #  `except Exception: pass`로 묶여 있었다. 그래서 그 사이의 손익 표시·
                        #  입출금 감지·문자열 포맷 중 **어느 하나라도 던지면 차단기가 조용히
                        #  건너뛰어졌다** — 손실이 큰 날일수록 그 코드가 다룰 값이 많아진다.
                        #  기준자산이 정해진 직후, 표시와 무관한 이 자리가 옳다.
                        self._run_account_circuit_breaker(current_total)

                        profit_rate = (total_profit / tot_pchs * 100) if tot_pchs > 0 else 0.0
                        
                        realized_profit = 0
                        # [중요] 실현손익을 못 구했는데 0으로 두면 그만큼이 그대로 '입출금'으로
                        #  둔갑한다. 오늘 -50만 실현했는데 조회가 실패하면 원금이 50만 커 보이고,
                        #  같은 조회가 매 주기 똑같이 실패하므로 아래 '3회 연속' 규칙이 방어가
                        #  아니라 오탐 확정 장치가 된다(이 파일의 기존 주석이 우려한 그 경로다).
                        #  0인 것과 못 구한 것을 갈라, 못 구했으면 감지 자체를 하지 않는다.
                        realized_ok = True
                        try:
                            today_str = datetime.now().strftime("%Y-%m-%d")
                            
                            target_account = None
                            if config.session.auto_cano:
                                target_account = f"{config.session.auto_cano}-{config.session.auto_acnt_prdt_cd}"
                                
                            today_trades = db_manager.db.get_trades(
                                start_date=today_str, end_date=today_str, 
                                is_sim=False, account=target_account
                            )
                            
                            today_trades_parsed = []
                            for r in reversed(today_trades):
                                type_str = r['type']
                                simple_type = "buy" if "매수" in type_str or "buy" in type_str.lower() else "sell"
                                parsed_r = dict(r)
                                parsed_r['type'] = simple_type
                                today_trades_parsed.append(parsed_r)
                            
                            today_trades_refined = self._refine_trade_records(today_trades_parsed)
                            sell_trades = [x for x in today_trades_refined if x['type'] == 'sell']
                            realized_profit = sum(int(t.get('profit_amt') or 0) for t in sell_trades)

                            # [외부 매도] 운용자가 HTS/MTS로 자동매매 계좌에서 직접 팔면 우리
                            #  주문 기록이 없어 실현손익이 0으로 남는다(conclusion 은 origin_trade
                            #  가 있어야 손익을 채운다). 그 금액은 '0'이 아니라 '모른다'다 —
                            #  0으로 세면 100만원 이익 실현이 **100만원 입금**으로 둔갑해
                            #  기준 자산이 부풀고(차단기가 늦어진다) 자산 이력에도 남는다.
                            #  이 파일의 원칙 그대로, 모르면 감지하지 않는다.
                            if any("매도" in str(r['type']) and "(외부)" in str(r['type'])
                                   and not (r['profit_amt'] or 0) for r in today_trades):
                                realized_ok = False
                                if getattr(self, '_ext_sell_gate_date', None) != today_str:
                                    self._ext_sell_gate_date = today_str
                                    self.log("[입출금감지] 실현손익을 모르는 외부(HTS/MTS) 매도가 "
                                             "있어 오늘 입출금 자동 감지를 보류합니다.")
                        except Exception as _rp_e:
                            realized_ok = False
                            logger.warning(f"[입출금감지] 당일 실현손익 조회 실패 — 감지를 보류한다: {_rp_e}")
                            
                        # [수정] 오프라인(프로그램 종료) 상태에서의 입출금까지 완벽히 감지하도록 수학적 불변 원리 적용
                        # 매매 손익이 아닌 외부 현금 입출금을 스스로 포착하여 일일 손실 제한(Loss Cut) 오작동을 방지합니다.
                        #
                        # [Fix] 현금은 반드시 current_total과 '같은 스냅샷'의 평가금으로 빼야 한다.
                        #  total_eval은 보유 종목 리스트 합산이고 current_total은 get_asset_status_data의
                        #  별도 재조회라, 두 값의 시세 스냅샷이 어긋나면 현금이 틀어진다(음수까지 나온다).
                        #  실측 2026-07-27: 같은 로그 한 줄에서 평가금이 3,540,000(88,500 기준)과
                        #  3,536,000(88,400 기준)으로 갈려 현금이 1,371원 대신 -2,629원으로 계산됐다.
                        #  장 마감 후에도 어긋났다는 건 한쪽이 캐시라는 뜻이고, 캐시면 오차가 매 주기
                        #  '동일하게' 반복되어 아래 3회 연속 확인 규칙이 방어가 아니라 오탐 확정 장치가 된다.
                        #  (보유가 커져 오차가 5만원을 넘으면 가짜 입출금 → initial_asset 이동 →
                        #   일일 손실 제한 기준 왜곡. 과거 같은 계열 버그가 비상정지를 오작동시켰다.)
                        #  asset_data 경로에서는 tot_asset = real_cash + dep_ovs + sec_eval 이므로
                        #  sec_eval을 빼면 현금이 정확히 나온다. 폴백 경로의 current_total은
                        #  cash + total_eval로 만든 값이라 total_eval을 빼는 것이 맞다.
                        if asset_data and not is_asset_broken:
                            current_cash = current_total - api.safe_int(asset_data.get('sec_eval', total_eval))
                        else:
                            current_cash = current_total - total_eval
                        current_principal = current_cash + tot_pchs - realized_profit

                        # [Fix] 입금 감지 기준은 '원금(현금+매입원가-실현손익)'이어야 한다.
                        # 원금은 입출금이 없으면 가격 변동/매매와 무관하게 불변(=시작현금+시작매입원가)이다.
                        # 과거에는 initial_asset(=시작 총자산=현금+평가금)과 비교했는데, 보유 종목에 평가손익이
                        # 있으면 매입원가≠평가금이라 그 차이(=시작 시점 평가손익)가 가짜 입출금으로 오인되었다.
                        # → 보유 종목 하락만으로 '가짜 입금'이 잡혀 기준자산이 부풀고 비상정지가 오작동했다.
                        if is_first_init or self.baseline_principal <= 0:
                            # [오프라인 입출금] 기준을 새로 잡기 **전에** 꺼져 있던 사이의
                            #  입출금부터 되찾는다. 새로 잡고 나면 대조할 것이 없어진다.
                            self._reconcile_offline_transfer(
                                f"{target_cano}-{acnt_cd}", current_principal, realized_ok)
                            self.baseline_principal = current_principal
                            # 재기동이 같은 날 다시 일어나도 이 기준으로 대조할 수 있게 남긴다.
                            try:
                                save_daily_initial_asset(f"{target_cano}-{acnt_cd}",
                                                         self.initial_asset,
                                                         principal=int(current_principal))
                            except Exception:
                                pass
                            # [날짜를 넘는 대조점] 파일(daily_state)은 날짜가 바뀌면 비워지므로
                            #  오프라인 입출금을 되찾을 수 없다. 자산 이력 행에 함께 남긴다.
                            #  자산이 0(시세 결손 의심)인 날은 남기지 않는다 — 그런 행은
                            #  get_max_daily_asset의 환산에서 빠져 보정이 사라진다. 그날을
                            #  건너뛰어도 다음 대조는 그 이전 스냅샷과 이뤄져 식은 그대로 성립한다.
                            if realized_ok and self.initial_asset > 0:
                                try:
                                    db_manager.db.save_daily_asset(
                                        datetime.now().strftime("%Y-%m-%d"),
                                        f"{target_cano}-{acnt_cd}", self.initial_asset,
                                        principal=int(current_principal))
                                except Exception as _e:
                                    logger.debug(f"[입출금] 기준 원금 스냅샷 저장 실패: {_e}")

                        # [파생값] 오늘 누적 순입출금. 저장하지 않고 매 주기 다시 잰다.
                        #  기준 자산을 **옮기지 않아도** 차단기·사이징이 이 값으로 즉시 보정된다
                        #  (effective_baseline 참조). 옮기면 initial_asset과 baseline_principal이
                        #  같은 폭으로 움직여 이 값이 0이 되므로, 반영 전후의 유효 기준은 동일하다.
                        if toss_cash_reliable and realized_ok:
                            _net = int(current_principal - self.baseline_principal)
                            # [잡음 바닥] 원금 대조에는 잔돈이 남는다 — 매수 수수료는 현금만
                            #  깎고 매입원가에는 안 들어가서, 거래한 날마다 수십~수백원이
                            #  '입출금'으로 새어 나온다. 실측 2026-08-31: 가상계좌에 입출금이
                            #  없는데 net_transfer 77원이 기록됐다. 한 번은 무해하지만 매일
                            #  쌓이면 get_max_daily_asset 의 환산(고점)을 갉는다.
                            #  오프라인 경로는 같은 이유로 이미 이 바닥을 갖고 있었다 —
                            #  판정이 같으니 문턱도 같아야 한다([[seed-spend-guard-parity]]).
                            #  5만원(알림 문턱)이 아니라 100원인 이유: 소액 계좌에서는 1만원
                            #  출금도 전 재산에 가깝고, 이 값은 사이징·차단기의 보정에 쓰인다.
                            if abs(_net) < OFFLINE_TRANSFER_FLOOR:
                                _net = 0
                            if _net != getattr(self, 'net_transfer_today', 0):
                                self.net_transfer_today = _net
                                # [여러 날 보정] 오늘 행에 남겨야 내일부터의 드로다운 기준이 맞는다.
                                #  이력을 옮기지 않고 이 값으로 환산한다(get_max_daily_asset).
                                #  값이 바뀔 때만 쓴다 — 매 주기 쓰면 파이3에 부담이고 의미도 없다.
                                try:
                                    db_manager.db.save_daily_asset(
                                        datetime.now().strftime("%Y-%m-%d"),
                                        f"{target_cano}-{acnt_cd}", self.initial_asset,
                                        net_transfer=_net)
                                    self._hwm_cache_date = None   # 환산이 바뀌었으니 다시 잰다
                                except Exception as _e:
                                    logger.debug(f"[입출금] 일자별 순입출금 기록 실패: {_e}")
                        else:
                            self.net_transfer_today = 0   # 못 쟀으면 보정하지 않는다(옛 동작 유지)

                        if not is_first_init and toss_cash_reliable and realized_ok:
                            transfer_amt = current_principal - self.baseline_principal

                            # [Fix] 5만원 이상 원금 변동 발생 시 입출금으로 간주하되, 주문 체결 중 API 데이터 불일치(Lag)로 인한
                            # 오작동을 방지하기 위해 3회 연속(약 30초) 동일한 변동이 감지될 때만 실제 입출금으로 확정합니다.
                            if abs(transfer_amt) >= 50000 and self.baseline_principal > 0:
                                if not hasattr(self, '_pending_transfer_amt'):
                                    self._pending_transfer_amt = 0
                                    self._pending_transfer_count = 0
                                    
                                # 오차 범위 500원 이내면 동일한 변동으로 간주
                                if abs(self._pending_transfer_amt - transfer_amt) < 500:
                                    self._pending_transfer_count += 1
                                else:
                                    self._pending_transfer_amt = transfer_amt
                                    self._pending_transfer_count = 1
                                    
                                if self._pending_transfer_count >= 3:
                                    action_str = "입금" if transfer_amt > 0 else "출금"
                                    # [알림 전용] 감지된 입출금은 **기준선을 옮기지 않는다.**
                                    #  일일 손실 한도·사이징은 net_transfer_today 로, 드로다운은
                                    #  daily_asset_history.net_transfer 로 각각 파생 보정되므로
                                    #  옮길 상태가 없다. 여기서는 운용자에게 알리기만 한다.
                                    #  (종전에는 initial_asset 을 옮기고 자산 이력을 평행이동했다.
                                    #   되돌릴 수 없고, 추정이 틀리면 고점이 낮아져 드로다운을
                                    #   과소평가 = 리스크 한도가 조용히 열리는 방향이었다.
                                    #   그래서 30% 상한을 두고 초과분은 사람에게 넘겼는데,
                                    #   그 미반영분이 90일짜리 가짜 드로다운으로 남았다.)
                                    _sig = (datetime.now().strftime("%Y-%m-%d"),
                                            int(round(transfer_amt / 10000.0)))
                                    if getattr(self, '_transfer_alert_sig', None) != _sig:
                                        self._transfer_alert_sig = _sig
                                        self.log(f"💰 외부 예수금 {action_str} 자동 감지: {transfer_amt:+,}원 "
                                                 f"(리스크 기준은 자동 보정됩니다)")
                                        api.send_telegram_message(
                                            f"💰 [예수금 {action_str} 자동 감지]\n"
                                            f"약 {abs(int(transfer_amt)):,}원의 {action_str}을 확인했습니다.\n\n"
                                            f"✅ 일일 손실 한도·포지션 사이징·드로다운 기준은 이 금액을 "
                                            f"빼고 계산하므로 자동으로 맞춰집니다. 조치할 것은 없습니다.\n"
                                            f"(실제 {action_str}이 아니라면 잔고 조회 이상을 확인해 주세요)")
                            else:
                                if hasattr(self, '_pending_transfer_count'):
                                    self._pending_transfer_count = 0
                                    self._pending_transfer_amt = 0
                        realized_rate = (realized_profit / self.initial_asset * 100) if self.initial_asset > 0 else 0.0
                        daily_profit = current_total - self.initial_asset
                        daily_profit_rate = (daily_profit / self.initial_asset * 100) if self.initial_asset > 0 else 0.0
                        order_possible = deposit_res.get('order_possible', deposit_d2) if deposit_res else 0
                        
                        self.log(f"[증권 자산 현황] 증권 매입 금액: {tot_pchs:,}원 | 증권 평가 금액: {total_eval:,}원 | 증권 평가 손익: {total_profit:+,}원 ({profit_rate:+.2f}%) | 주문 가능 금액: {order_possible:,}원")
                        self.log(f"[오늘 자산 현황] 오늘 시작 자산: {self.initial_asset:,}원 | 오늘 현재 자산: {current_total:,}원 | 오늘 현재 손익: {daily_profit:+,}원 ({daily_profit_rate:+.2f}%) | 오늘 실현 손익: {realized_profit:+,}원 ({realized_rate:+.2f}%)")

                        # (계좌 차단기·평가자산 갱신은 위 _run_account_circuit_breaker에서
                        #  이미 수행했다 — 표시 코드가 던져도 건너뛰지 않도록 앞으로 옮겼다)
                    else:
                        self.log(f"   총 평가금액: {total_eval:,}원  |  총 평가손익: {total_profit:+,}원")
                    
        except Exception: pass

    def _get_toss_open_buy_reserved(self, cano=None, acnt=None):
        """[토스 전용] 미체결 매수 주문에 묶인 현금(KRW)을 합산한다.

        토스의 '매수가능금액(cashBuyingPower)'은 미체결 매수 주문에 예약된 현금을
        제외한 값이라, 주문 접수/취소에 따라 변동한다. 이를 보정하지 않으면 자산/원금
        계산이 흔들려 입금 자동 감지가 오작동(가짜 입금)하고 손실률이 왜곡된다.
        반환값을 current_total에 더하면 '매수가능금액 + 예약현금 = 실제 현금'이 되어
        주문 상태와 무관하게 안정적인 값이 된다.
        """
        reserved = 0
        open_orders = api.get_domestic_open_orders(cano, acnt) or []
        for o in open_orders:
            # KIS 형식: 02=매수, 01=매도
            if o.get('sll_buy_dvsn_cd') != '02':
                continue
            rmn = api.safe_int(o.get('rmn_qty')) or api.safe_int(o.get('ord_qty'))
            price = float(o.get('ord_unpr') or 0)
            if rmn > 0 and price > 0:
                reserved += int(rmn * price)
        return reserved

    def _get_total_estimated_asset(self):
        """현재 총 추정 자산(예수금 + 주식평가금) 계산"""
        try:
            cano = config.session.auto_cano
            acnt = config.session.auto_acnt_prdt_cd
            
            # [수정] 해외 자산 누락 방지를 위해 완벽하게 계산된 통합 자산 데이터 사용
            asset_data = account.get_asset_status_data(cano, acnt)
            if asset_data:
                return asset_data.get('tot_asset', 0)
        except Exception as e:
            logger.debug(f"자산 조회 중 예외 발생: {str(e)}")
        
        self.log(f"⚠️ 자산 조회 최종 실패. (KIS 서버 응답 지연)")
        return None


    def _get_prev_rsi(self, df):
        """전일 RSI 계산 (주의 조건 판단용)"""
        if df is not None and not df.empty and len(df) >= 16:
            delta = df['close'].diff()
            gain = delta.where(delta > 0, 0).ewm(com=13, adjust=False).mean()
            loss = -delta.where(delta < 0, 0).ewm(com=13, adjust=False).mean()
            try: return (100 - (100 / (1 + gain/loss))).iloc[-2]
            except Exception: pass
        return None

    def _live_risk_map(self, hold_codes):
        """오픈 리스크 산출에 넘길 '직전 주기 실측 손절선' 스냅샷 (보유분만).

        [왜 거르는가] 이 캐시는 매도 판정이 종목마다 채우고 스스로 지우지 않는다.
        판 종목의 값이 남아 있어도 히트는 잔고를 도니 곧바로 틀리진 않지만, 같은 종목을
        다시 담았을 때 **예전 포지션의 손절선**이 새 포지션의 리스크로 계상된다.
        보유분만 남기면 그 경로가 닫힌다(재진입 시엔 값이 없어 종전 근사로 돌아간다).
        """
        codes = set(hold_codes or ())
        with self._lock:
            self.holding_risk_cache = {k: v for k, v in self.holding_risk_cache.items()
                                       if k in codes}
            return dict(self.holding_risk_cache)

    def _effective_stop_loss_rate(self, buy_trades=None, rule=None, fallback_atr_rate=None):
        """포지션의 실효 손절률(%) — 매도 판정이 실제로 쓰는 그 값. 미사용(0)이면 None.

        [SSOT] 산식은 build_sell_thresholds가 단독 보유한다. 종전에는 이 자리와
         _alert_unmanaged_stop이 '수량가중평균'을 각자 복제해 **세 벌**이 돌았고, 세 벌이
         서로 달랐다 — 복제본은 ATR 손절 사용 여부도, 개별 룰의 손절 조이기도, 매수 기록이
         없는 포지션의 ATR 복원(fallback)도 보지 않는다. 그래서 '손절 보호'(미체결 매수
         취소)와 '미관리 포지션 이탈 경보'가 정작 매도 엔진과 다른 선을 보고 판단했다.
         경보·취소는 손절이 안 도는 포지션의 마지막 안전망이라, 그 선이 실제 판정과
         어긋나면 조용히 늦거나 조용히 빨라진다.

        fallback_atr_rate: 매수 기록이 없을 때 쓸 복원값(일봉이 있는 호출부만 넘긴다).
        """
        thresholds = _pkg().build_sell_thresholds(
            rule=rule, buy_trades=buy_trades, fallback_atr_rate=fallback_atr_rate)
        try:
            sl_rate = float(thresholds.get("STOP_LOSS_RATE",
                                           config.SELL_STRATEGY.get("STOP_LOSS_RATE", -7.0)))
        except (TypeError, ValueError):
            return None
        return sl_rate if sl_rate < 0 else None

    def _cancel_pending_buy_on_stop_loss(self, code, name, item, buy_trades=None, rule=None):
        """미체결 '매수'가 걸린 채 체결분이 손절선을 이탈하면 그 매수를 즉시 취소한다.

        [왜 취소가 먼저인가] 매수 주문을 열어 둔 채 같은 종목을 팔면 서로 싸운다 —
        손절로 턴 물량을 남은 매수가 다시 담을 수 있다. 그래서 이번 주기에는 매수만
        거둬들이고, 주문이 종결되어 pending이 풀린 다음 주기에 정상 청산 경로를 태운다.
        자동 취소 타임아웃(UNFILLED_ORDER_CANCEL_SECONDS)을 기다리지 않는 것이 요점이다.

        [매도 주문은 건드리지 않는다] 취소 대상은 '매수'뿐이다. 청산 주문을 취소하면
        포지션이 그대로 남아 손절이 되레 무산된다. 매수·매도가 함께 걸려 있으면 매수만
        거둔다(청산 중인 종목에 추가로 담는 것을 막는 쪽이 항상 옳다).
        """
        try:
            profit_rate = float(item.get('evlu_pfls_rt') or 0.0)
            sl_rate = self._effective_stop_loss_rate(buy_trades, rule=rule)
            if sl_rate is None or profit_rate > sl_rate:
                return

            with self.order_manager._lock:
                odnos = list((self.order_manager.pending_orders.get(code) or {}).keys())
            if not odnos:
                return

            buy_odnos = []
            for odno in odnos:
                t_type = ""
                try:
                    tr = db_manager.db.get_trade_by_odno(odno)
                    t_type = str((tr or {}).get('type', ''))
                except Exception:
                    pass
                is_sell = "매도" in t_type or "sell" in t_type.lower()
                if not is_sell and ("매수" in t_type or "buy" in t_type.lower()):
                    buy_odnos.append(odno)

            for odno in buy_odnos:
                # qty=0 은 '잔량 전부 취소'(QTY_ALL_ORD_YN=Y) — 잔량을 몰라도 안전하다.
                res = api.revise_cancel_order("domestic", "cancel", odno, code, 0, "0", "02", "00")
                if isinstance(res, dict) and res.get('rt_cd') == '0':
                    self.log(f"[손절 보호] {name}({code}) 수익률 {profit_rate:.2f}% ≤ 손절 {sl_rate:.2f}% "
                             f"→ 미체결 매수(No.{odno})를 즉시 취소했습니다. 다음 주기에 청산합니다.")
                    api.send_telegram_message(
                        f"🛑 [손절 보호] {name}({code})\n"
                        f"수익률 {profit_rate:.2f}% (손절 기준 {sl_rate:.2f}%)\n"
                        f"미체결 매수를 취소해 체결분의 청산 경로를 확보했습니다.")
                else:
                    msg = (res or {}).get('msg1') if isinstance(res, dict) else res
                    self.log(f"[손절 보호] {name}({code}) 미체결 매수 취소 실패: {msg}")
        except Exception as e:
            logger.debug(f"[손절 보호] 미체결 매수 취소 처리 실패({code}): {e}")

    def _clamp_order_price(self, code, order_price):
        """지정가를 가격제한폭(상·하한가) 안으로 되돌린다.

        판정은 utils.clamp_to_price_limit(순수 함수), 한도 조회는 api.get_price_limits가
        맡는다. 한도를 구하지 못하면 원래 값을 그대로 쓴다(fail-open) — 잘못된 한도로
        주문가를 흔드는 것이 제한폭 밖 주문보다 위험하기 때문이다.
        """
        try:
            upper, lower = api.get_price_limits(code)
            if not upper and not lower:
                return order_price
            clamped = utils.clamp_to_price_limit(order_price, upper, lower)
            if clamped != order_price:
                self.log(f"[제한폭 보정] {code} 주문가 {order_price:,}원 → {clamped:,}원 "
                         f"(상한 {upper:,} / 하한 {lower:,})")
            return clamped
        except Exception as e:
            logger.debug(f"[제한폭 보정] 실패({code}): {e}")
            return order_price

    def _apply_corporate_action(self, code, name, item, buy_price, highest_price):
        """[안전장치] 액면분할·무상증자를 감지해 트레일링 최고가를 같은 배율로 보정한다.

        판정은 engine.detect_corporate_action이 단독 보유한다(순수 함수). 여기서는 잔고
        한 줄에서 평단·매입금액을 꺼내 직전 주기 값과 비교하고, 결과를 DB에 반영할 뿐이다.

        보정하지 않으면 5:1 분할에서 고점만 분할 전 값으로 남아 트레일링 스탑이 즉시
        발동하고 시장가로 강제 청산된다. 백테스트 데이터는 수정주가라 이 경로를 재현하지
        못하므로, 실계좌 투입 전에 코드로 막아둔다.

        반환: 보정된 최고가(보정이 없으면 받은 값 그대로).
        """
        try:
            qty = api.safe_int(item.get('hldg_qty'))
            if qty <= 0 or buy_price <= 0:
                return highest_price
            # 매입금액은 실전 잔고(INQR_DVSN=01)·토스 어댑터에서 0/누락으로 오므로
            #  같은 정의(수량 × 평단)로 복원한다 — 잔고 표시부(_print_holdings)와 동일 규칙.
            pchs_amt = api.safe_int(item.get('pchs_amt')) or int(qty * buy_price)

            ref_avg, ref_amt = db_manager.db.get_position_ref(code)
            ratio, reason = _pkg().detect_corporate_action(ref_avg, ref_amt, buy_price, pchs_amt)

            if ratio != 1.0:
                lines = [f"🔀 [권리 조정] {name}({code})", reason]

                #  ① 트레일링 최고가는 같은 배율로 환산한다 — 시스템이 만든 값이고,
                #     환산하면 조정 전과 정확히 같은 판정이 나오기 때문이다.
                new_high = db_manager.db.rescale_highest_price(code, ratio) if highest_price > 0 else None
                if new_high:
                    with self._lock:
                        self.trailing_stop_cache[code] = new_high
                    highest_price = new_high
                    lines.append(f"· 트레일링 최고가 {ratio:.4f}배 보정 (→ {new_high:,.0f}원)")

                #  ② 예약 주문은 환산하지 않고 **취소**한다 — 목표가는 운영자가 조정 전
                #     가격을 보고 직접 정한 값이라, 기계적으로 환산해도 의도한 자리가
                #     아니다. 그대로 두면 목표가는 이미 도달한 것처럼, 추적 극값은 폭락한
                #     것처럼 보여 어느 쪽이든 즉시 오발동한다.
                canceled = db_manager.db.cancel_reserved_orders_on_corp_action(
                    code, f"권리 조정 감지({reason})로 자동 취소")
                for o in canceled:
                    lines.append(f"· 예약 {'매수' if o.get('order_type') == 'buy' else '매도'} 취소: "
                                 f"{o.get('condition_type')} "
                                 f"{float(o.get('target_price') or 0):,.0f} / {o.get('qty')}주")
                    db_manager.db.insert_trade(
                        f"{'매수' if o.get('order_type') == 'buy' else '매도'}취소(예약)",
                        code, name, o.get('qty'), o.get('order_price', 0),
                        f"RES_CORP_{o.get('id')}", order_status="취소",
                        reason=f"권리 조정({reason})으로 예약 자동 취소")
                if canceled:
                    lines.append("→ 조정 후 가격 기준으로 다시 설정해 주세요.")

                self.log(f"[권리 조정] {name}({code}) {reason} · 배율 {ratio:.4f} "
                         f"· 예약 취소 {len(canceled)}건")
                logger.warning(f"[권리 조정] {code} {reason} ratio={ratio:.4f} "
                               f"high={new_high} canceled={len(canceled)}")
                try:
                    api.send_telegram_message("\n".join(lines))
                except Exception:
                    pass

            # 배율이 1이어도 기준값은 항상 최신으로 옮겨야 다음 주기 비교가 성립한다.
            if (ref_avg, ref_amt) != (float(buy_price), float(pchs_amt)):
                db_manager.db.update_position_ref(code, buy_price, pchs_amt)
        except Exception as e:
            # 보정 실패가 매도 분석 자체를 막아서는 안 된다(원래 값으로 계속 진행).
            logger.debug(f"[권리 조정] 판정 실패({code}): {e}")
        return highest_price

    def _cached_anchor(self, code):
        """기록된 트레일링 앵커(최고가). 캐시에 없으면 DB에서 읽어 채운다. 없으면 0.0."""
        with self._lock:
            cached = self.trailing_stop_cache.get(code)
            if cached is None:
                val = db_manager.db.get_highest_price(code)
                cached = val if val is not None else 0.0
                self.trailing_stop_cache[code] = cached
        return cached

    def _restore_trailing_anchor(self, code, name, entry_date, highest_price=None,
                                 df=None, is_overseas=False):
        """진입일 이후 봉 고가가 기록된 앵커보다 높으면 그것으로 올린다. 확정된 앵커를 돌려준다.

        [왜 필요한가 · 2026-08-24] 주기마다의 갱신은 **봇이 보고 있는 동안의 현재가**만
        쌓으므로 다음 구간이 통째로 비어 있었다:
          · HTS·앱에서 직접 산 포지션 — 봇이 처음 본 날의 현재가부터 시작한다
          · 재기동·정지 구간의 고점 — 그 사이 고가가 앵커에 남지 않는다
          · 매수 주문이 심는 앵커 — 보유 중인 종목에 1주만 더 담아도 그 체결가가 앵커로
            기록되고(update_highest_price는 단조라 내리진 않지만, 기록이 없던 종목엔
            매수가가 그대로 앵커가 된다) 실제 고점이 사라진다
        백테스트는 진입 봉 고가에서 시작해 봉 고가의 러닝맥스를 쓴다
        (portfolio_backtest: pos["high"] = max(pos["high"], row["high"])). 즉 이 복원은
        실매매를 백테스트의 정의 쪽으로 되돌리는 것이다.

        [왜 매도 분석 밖에서도 부르나] 자동 매도에서 제외된 ETF는 판정 루프에 닿기 전에
        빠져나가, 저 세 구멍을 메울 기회가 영영 없었다. 앵커를 쓰는 쪽(메뉴 9-2 보유 분석)은
        매번 봉에서 되짚으므로 화면은 맞지만 DB에는 매수가가 그대로 남는다. **쓰는 주체는
        트레이더 하나로 묶는다** — 표시 경로에서 쓰면 메뉴 9-5의 가상 포지션(사용자가 입력한
        임의의 매수일)과 다른 계좌의 잔고가 같은 테이블(code가 PK)로 흘러들고, 단조 갱신이라
        한 번 높게 오염되면 되돌릴 수 없다(청산선이 올라가 오청산이 나간다).

        df 를 주지 않으면 여기서 조회한다(ETF 경로). 이때는 주기마다 차트를 새로 집는 셈이라
        종목당 1시간에 한 번으로 묶는다 — 앵커는 일봉 고가에서 나오므로 그보다 잦게 볼 이유가
        없고, 라즈베리파이에서 캐시 만료(6시간)와 겹치면 종목마다 KIS 호출이 붙는다.
        highest_price 를 주지 않으면 기록된 앵커를 직접 읽는다(스로틀에 걸리면 읽지도 않는다).
        실패하면 앵커를 건드리지 않는다.
        """
        if df is None:
            now = time.time()
            if now - self._anchor_restore_at.get(code, 0.0) < ANCHOR_RESTORE_INTERVAL_SEC:
                return highest_price
            self._anchor_restore_at[code] = now
        if highest_price is None:
            highest_price = self._cached_anchor(code)
        if not entry_date:
            return highest_price
        try:
            if df is None:
                df = api.get_chart_data(code, is_overseas=is_overseas)
            derived_high = _pkg().anchor_high_since(code, df, entry_date, is_overseas=is_overseas)
        except Exception as e:
            logger.debug(f"[TrailingStop] {code} 앵커 복원 실패: {e}")
            return highest_price

        if not derived_high or derived_high <= highest_price:
            return highest_price

        db_manager.db.update_highest_price(code, derived_high)
        with self._lock:
            self.trailing_stop_cache[code] = derived_high
        self.log(f"[TrailingStop] {name}({code}) 앵커 복원: "
                 f"{highest_price:,.0f} → {derived_high:,.0f} "
                 f"(진입일 {entry_date} 이후 봉 고가)")
        return derived_high

    def _alert_unmanaged_stop(self, code, name, item, kind, buy_trades=None, rule=None):
        """[안전장치] 자동 매도 대상에서 제외된 보유 포지션의 손절선 이탈 경보

        [추세추종 원칙] "탈출 전략이 없다면 포지션을 잡지 마라."
        트레이딩 제한 종목(수동 홀딩)과 ETF(SYSTEM_INCLUDE_ETF=False)는 의도적으로 매도 분석에서
        제외되므로 시스템이 손절하지 않는다. 자동 청산까지 하면 '수동 관리' 의도를 깨므로,
        대신 손절선 이탈 사실을 알려 사용자가 직접 판단할 수 있게 한다.

        손절선은 매수 기록에 저장된 실제 손절률(수량가중평균)을 쓰고, 없으면 전역 STOP_LOSS_RATE.
        같은 종목의 반복 알림은 24시간 스로틀하며, 손절선 위로 회복하면 스로틀을 풀어
        재이탈 시 다시 알린다.
        """
        try:
            # [중요] 평가손익률이 없을 때 0으로 두면 안 된다. 0은 손절선(음수) 위라서
            #  아래 분기가 '회복했다'로 읽고 경보를 건너뛰는 것은 물론 스로틀까지 푼다.
            #  이 경보는 시스템이 손절해 주지 않는 포지션의 **마지막 안전망**인데, 잔고
            #  데이터가 부실할수록 조용해지는 구조였다(토스 어댑터는 일부 필드가 0/누락으로
            #  온다 — 같은 이유로 pchs_amt는 이미 수량×평단으로 복원하고 있다).
            #  없으면 평단과 현재가로 직접 구하고, 그것도 안 되면 판단을 미룬다.
            profit_rate = _pkg().holding_profit_rate(item)
            if profit_rate is None:
                logger.warning(f"[미관리 경보] {name}({code}) 평가손익률을 구할 수 없어 판정을 보류한다 "
                               f"— 스로틀은 건드리지 않는다")
                return

            # [SSOT] 손절선은 매도 엔진이 쓰는 그 값이어야 한다(_effective_stop_loss_rate).
            #  종전에는 여기에 수량가중평균을 한 벌 더 복제해 두어, ATR 손절 OFF·개별 룰
            #  조이기 같은 조건에서 실제 판정과 다른 선을 보고 경보했다.
            sl_rate = self._effective_stop_loss_rate(buy_trades, rule=rule)
            if sl_rate is None:
                return  # 손절 기준 자체가 없으면(0=미사용) 경보할 기준도 없다

            if profit_rate > sl_rate:
                # 손절선 위로 회복 — 다음 이탈 때 다시 알리도록 스로틀 해제
                self.unmanaged_stop_notified.pop(code, None)
                return

            #  [전달 확인 뒤에 찍는다] 스로틀 값은 '보낸 시각'이 아니라 **다음 알림 가능
            #   시각**이다. 종전에는 보내기 전에 찍었는데, send_telegram_message 는 기본이
            #   비동기라 실패해도 예외가 없어 네트워크가 끊긴 채로 '보냈다'가 굳었다. 이
            #   경보는 위 독스트링대로 '시스템이 손절해 주지 않는 포지션의 마지막 안전망'
            #   이라, 한 번 놓치면 손절선 아래에서 24시간 침묵한다.
            now = time.time()
            if now < self.unmanaged_stop_notified.get(code, 0):
                return

            qty = api.safe_int(item.get('hldg_qty', 0))
            eval_amt = api.safe_int(item.get('evlu_amt', 0))
            loss_amt = api.safe_int(item.get('evlu_pfls_amt', 0))

            # 같은 '못 빠져나온다'라도 원인이 두 갈래다. 문구를 뭉뚱그리면 운영자가
            #  할 수 있는 조치를 오판한다 — 제외는 설정 문제고, 매도 불가는 시장 문제다.
            blocked = (kind == UNMANAGED_NO_SELLABLE)
            title = "매도 실패" if blocked else "자동매도 제외 종목"
            cause = (f"매도를 시도했으나 증권사 매도가능수량이 0입니다 ({kind}).\n"
                     f"시스템이 스스로 청산할 수 없는 상태입니다."
                     if blocked else
                     f"{kind}으로 자동 매도 대상에서 제외되어 있어 시스템이 손절하지 않습니다.")

            self.log(f"⚠️ [손절선 이탈 경보] {name}({code}): 수익률 {profit_rate:.2f}% ≤ 손절 기준 {sl_rate:.2f}% "
                     f"— {kind}이라 시스템이 청산하지 못합니다.")
            delivered = _pkg().alert_delivered(
                f"⚠️ [손절선 이탈 — {title}]\n\n"
                f"종목: {name}({code})\n"
                f"수익률: {profit_rate:.2f}% (손절 기준: {sl_rate:.2f}%)\n"
                f"보유: {qty:,}주 / 평가금 {eval_amt:,}원 / 평가손익 {loss_amt:,}원\n\n"
                f"사유: {cause}\n"
                f"직접 청산 여부를 판단해 주세요. (동일 종목 재알림은 24시간 후)")
            #  전달되면 24시간, 실패하면 짧게 — 침묵도 도배도 아닌 쪽으로 재시도한다.
            self.unmanaged_stop_notified[code] = now + (86400 if delivered else _pkg().ALERT_RETRY_SEC)
            if not delivered:
                self.log(f"[손절선 이탈 경보] {name}({code}) 전송 실패 — "
                         f"{int(_pkg().ALERT_RETRY_SEC)}초 뒤 다시 시도합니다.")
        except Exception as e:
            logger.debug(f"[손절선 이탈 경보] {code} 처리 실패: {e}")

    def _snapshot_paper_closing_equity(self):
        """[관찰 모드] 종가가 확정된 뒤 그날 자산 스냅샷을 한 번 더 찍는다(종가로 덮음).

        [왜] 주기 스냅샷은 RUNNING 분기에서만 돈다. 15:20 단일가 휴게부터 상태가
         WAITING이라 그날 마지막 스냅샷은 15:19 장중가로 굳고, 종가 단일가(15:20~15:30)
         에서 확정된 종가는 자산곡선에 영원히 들어가지 못한다. 곡선이 유일한 소스인
         MDD·누적수익률·고점대비가 전부 종가 기준이 아니게 된다.
         (2026-08-28 실측: 곡선 3,743,000 = 15:19:40 값 / 확정 종가 3,744,000)

        [시각] 시계가 아니라 확정 여부를 본다 — api.krx_last_settled_day()가 오늘을
         가리켜야(마감 + 확정 여유) 그날 봉을 종가로 인정한다. 그 전에 찍으면 직전
         거래일 종가를 오늘 값으로 굳힌다.
        [빈도] 거래일당 1회. 휴장일에는 찍지 않는다 — 새 봉이 없는데 행을 만들면
         주말·공휴일이 '변동 없는 거래일'로 곡선에 남아 일수·고점 아래 일수가 부푼다.
         일봉을 못 받아 실시간가로 폴백한 종목이 하나라도 있으면 '찍었다'로 세지 않고
         다음 주기에 다시 덮는다(같은 날 재호출은 덮어쓰기다).
        """
        try:
            if api.is_holiday_today():
                return
            today = datetime.now().strftime("%Y%m%d")
            if self.paper_closing_snapshot_date == today:
                return
            if api.krx_last_settled_day() != today:
                return      # 아직 종가가 확정되지 않았다 — 다음 주기에 다시 본다
            from modules import paper_broker
            if not paper_broker.snapshot_equity():
                return
            self.paper_closing_snapshot_date = today
            self.log("[관찰 모드] 마감 자산 스냅샷 기록 (KRX 확정 종가 기준)")
        except Exception as e:
            # 기록 전용 경로다. 실패해도 매매 루프를 흔들면 안 된다.
            logger.debug(f"[관찰 모드 마감 스냅샷] 실패: {e}")

    def _scan_after_hours_sell_signals(self, target_cano):
        """[관측성] 장 마감 후 청산 신호를 하루 한 번 스캔해 알린다. (주문 없음)

        메인 루프는 마감과 함께 분석을 통째로 멈춘다(트래픽 절감). 그래서 종가가
        확정된 뒤 손절선·트레일링선을 이탈해도 다음 개장까지 아무도 모른다 —
        추세추종에서 가장 비싼 공백이다("탈출 전략이 없다면 포지션을 잡지 마라").

        주문은 내지 않는다. 마감 뒤에는 낼 수 없고, 다음 개장 때 그 시점 가격으로
        정식 판정이 다시 돈다. 여기서 하는 일은 사실을 알리는 것뿐이다.

        [시각] 종가 단일가(15:20~15:30)가 끝나 일봉이 확정된 뒤에 돈다. 그 전에 돌면
        접속매매 마지막 가격으로 판정해 종가와 어긋난다.
        [빈도] 거래일당 1회. 휴장일에는 돌지 않는다(판정할 새 봉이 없다).
        """
        try:
            if not getattr(config, 'AFTER_HOURS_SELL_ALERT', True):
                return
            if api.is_holiday_today():
                return
            now = datetime.now()
            after = getattr(config, 'AFTER_HOURS_SELL_ALERT_TIME', "1535")
            if now.strftime("%H%M") < after:
                return
            today = now.strftime("%Y%m%d")
            # [기준] 일봉이 확정된 뒤에만 판정한다. 설정 시각(기본 15:35)이 확정 여유
            #  (15:40)보다 이르면 잔고의 현재가가 아직 NXT 체결가라 종가와 어긋난다 —
            #  '종가가 확정된 뒤에 돈다'는 이 함수의 전제를 시계가 아니라 확정 여부로 센다.
            if api.krx_last_settled_day() != today:
                return
            if self.after_hours_scan_date == today:
                return
            self.after_hours_scan_date = today

            acnt = config.session.auto_acnt_prdt_cd
            holdings, _summary = api.get_domestic_balance(target_cano, acnt)
            if not holdings:
                return

            self.log("[장마감] 청산 신호 점검 (주문 없음 · 알림 전용)")
            rules = _enrich_rules_with_weights(db_manager.db.get_all_stock_strategies())
            self._check_sell_conditions(
                holdings, is_market_open=False,
                rules_map={r['code']: r for r in rules},
                restricted_stocks=get_restricted_stocks(*_get_trade_account()))
        except Exception as e:
            # 알림 전용 경로다. 실패해도 매매 루프를 흔들면 안 된다.
            logger.debug(f"[장마감 청산 신호 스캔] 실패: {e}")

    def _alert_after_hours_sell(self, code, name, item, reason, current_price, order_price, qty):
        """[관측성] 장 마감 후 감지된 매도 신호를 알린다.

        마감 뒤에는 주문을 낼 수 없어 종전에는 로그 한 줄만 남고 끝났다. 청산이 다음
        개장까지 밀리는데 운영자가 그 사실을 알 방법이 없었다 — 손절·트레일링이면
        하룻밤의 갭이 그대로 손실이므로, 직접 판단할 기회를 준다.

        [주의] 이 신호는 확정이 아니다. 다음 개장 때 그 시점 가격으로 다시 판정하므로
        신호가 사라질 수도, 다른 사유로 바뀔 수도 있다. 문구에 그대로 적는다.

        같은 종목·같은 사유는 마감 세션당 한 번만 보낸다(주기마다 재감지되므로).
        사유가 바뀌면 다시 알린다 — 트레일링과 손절은 운영자가 할 판단이 다르다.
        """
        try:
            if self.after_hours_sell_notified.get(code) == reason:
                return

            profit_rate = float(item.get('evlu_pfls_rt') or 0.0)
            eval_amt = api.safe_int(item.get('evlu_amt', 0))
            pfls_amt = api.safe_int(item.get('evlu_pfls_amt', 0))

            #  전달을 확인한 뒤에 '보냈음'을 기록한다(위 손절선 경보와 같은 이유).
            #  실패하면 기록하지 않아 다음 주기에 다시 시도한다 — 마감 후 갭 전에
            #  운영자가 판단할 기회를 주는 것이 이 알림의 목적이다.
            delivered = _pkg().alert_delivered(
                f"🔔 [장마감 후 매도 신호]\n\n"
                f"종목: {name}({code})\n"
                f"사유: {reason}\n"
                f"수익률: {profit_rate:+.2f}% / 평가손익 {pfls_amt:,}원\n"
                f"보유: {qty:,}주 / 평가금 {eval_amt:,}원\n"
                f"기준가: {int(current_price):,}원 (예상 주문가 {order_price:,}원)\n\n"
                f"장이 마감되어 주문은 전송되지 않았습니다.\n"
                f"다음 개장 시 그 시점 가격으로 다시 판정합니다 — 신호가 유지되면 "
                f"자동 청산되고, 사라지면 보유를 유지합니다.")
            if delivered:
                self.after_hours_sell_notified[code] = reason
        except Exception as e:
            logger.debug(f"[장마감 매도 신호 알림] {code} 처리 실패: {e}")

    def _check_sell_conditions(self, holdings, is_market_open=True, rules_map=None, restricted_stocks=None):
        # [정리] 보유가 끝난 종목의 '매도 불가 연속 횟수'를 버린다. 남겨 두면 나중에 같은
        #  종목을 재매수했을 때 옛 횟수에 이어붙어 첫 일시적 0에서 곧바로 오경보가 난다.
        try:
            held = {h['pdno'] for h in (holdings or []) if api.safe_int(h.get('hldg_qty')) > 0}
            for gone in [c for c in self.no_sellable_streak if c not in held]:
                del self.no_sellable_streak[gone]
            for gone in [c for c in self.stuck_pending_streak if c not in held]:
                del self.stuck_pending_streak[gone]
            # 장이 열리면 마감 후 알림 스로틀을 푼다 — 신호가 개장 후에도 살아 있으면
            #  정상 청산되고, 그날 마감 뒤 다시 감지되면 그때 다시 알려야 한다.
            if is_market_open:
                self.after_hours_sell_notified.clear()
            else:
                for gone in [c for c in self.after_hours_sell_notified if c not in held]:
                    del self.after_hours_sell_notified[gone]
        except Exception:
            pass

        # [WS] 시스템 트레이딩 종목을 실시간 피드에 최우선으로 등록한다.
        #  보유종목(포지션, 최우선) → 매수후보 순서로 priority. 매수후보는 국내주식 + (ETF 포함 설정 시)국내 ETF.
        #  ETF 미포함 설정이면 ETF는 시스템 대상이 아니므로 '그 외(other) 로테이션'으로 둔다.
        try:
            from brokers import realtime
            hold_codes = [h['pdno'] for h in (holdings or [])]
            cand_codes = [s['code'] for s in config.session.stock_data.get('stocks_kr', [])]
            etf_codes = [s['code'] for s in config.session.stock_data.get('etfs_kr', [])]
            if getattr(config, 'SYSTEM_INCLUDE_ETF', False):
                realtime.update_symbols(hold_codes + cand_codes + etf_codes, [])
            else:
                realtime.update_symbols(hold_codes + cand_codes, etf_codes)

            # [WS] 커버리지 진단: 시스템 종목 수가 WS 동시 용량을 넘으면 초과분은 현재가/호가를
            #  REST로 폴백(로테이션)하므로 모의투자(2 TPS)에서 분석이 느려질 수 있다. 상태 변화 시에만 1회 로그.
            cov = realtime.coverage()
            if cov:
                sig = (cov.get('priority'), cov.get('capacity'), cov.get('rest_fallback'), cov.get('ob_covered'))
                if sig != getattr(self, '_ws_cov_sig', None):
                    self._ws_cov_sig = sig
                    if cov.get('rest_fallback', 0) > 0:
                        self.log(f"[WS] 시스템 종목 {cov['priority']}개 > 동시 용량 {cov['capacity']}개 "
                                 f"→ {cov['rest_fallback']}개는 현재가 REST 폴백(분석 지연 가능). "
                                 f"관심목록 축소를 권장합니다.")
                    else:
                        self.log(f"[WS] 커버리지 양호: 현재가 {cov['price_covered']}개 / 호가 {cov['ob_covered']}개 "
                                 f"실시간 구독(시스템 {cov['priority']}개 전부 커버).")
        except Exception: pass

        # [최적화] 인자로 전달받은 holdings 사용
        if not holdings:
            self.portfolio_heat_amt = 0.0  # 보유 없음 = 오픈 리스크 0 (매수 경로의 히트 캡 판정용)
            self.portfolio_heat_unknown = False
            with self._lock:
                self.holding_risk_cache.clear()
            return

        # [추가] 개별 룰 로드 ([최적화] 루프에서 주기당 1회 로드해 전달받으면 재조회 생략)
        if rules_map is None:
            custom_rules = db_manager.db.get_all_stock_strategies()
            custom_rules = _enrich_rules_with_weights(custom_rules) # [추가] 가중치 보강
            rules_map = {r['code']: r for r in custom_rules}

        # [추가] 트레이딩 제한 종목 로드 (현재 시스템 트레이딩 계좌 기준으로 필터링)
        if restricted_stocks is None:
            _trade_cano, _trade_acnt = _get_trade_account()
            restricted_stocks = get_restricted_stocks(_trade_cano, _trade_acnt)

        # [최적화] 종목별 개별 DB 조회(최근 매수/보유분 매수 내역)를 주기 시작 시 배치 쿼리로 일괄 로드
        #  (기존: 보유 종목 × 최대 5쿼리 → 배치 3쿼리, 저사양 SD카드 SQLite I/O 절감)
        _all_hold_codes = [h['pdno'] for h in holdings]
        # [계좌 귀속] trades 는 모든 모드·계좌가 한 파일을 공유한다(토스·한투가 같은
        #  테이블에 쌓인다). 계좌로 거르지 않으면 같은 종목을 두 계좌에서 들고 있을 때
        #  **남의 계좌 매수 기록**으로 손절선(수량가중평균)·오픈 리스크·진입일이 계산된다.
        _acct = self._trade_account_key()
        latest_buy_map = db_manager.db.get_latest_buy_trades(_all_hold_codes, account=_acct)
        buy_trades_map = db_manager.db.get_buy_trades_for_current_holdings(_all_hold_codes, account=_acct)
        # 진입일(보유수량이 0 → 1 이상이 된 시점) — 시간청산 기준
        entry_date_map = db_manager.db.get_position_entry_dates(_all_hold_codes, account=_acct)

        # [추가] 포트폴리오 히트(총 오픈 리스크) 스냅샷 갱신 — 같은 주기의 피라미딩/신규 매수 캡 판정에 사용
        # [알려진 한계] 기준은 **잔고**다. 직전 주기에 낸 매수·증액 주문이 아직 체결되지
        #  않았으면 그 오픈 리스크는 여기 안 잡혀, 체결될 때까지 히트가 과소평가된다
        #  (주기 안에서의 이중 사용은 매수/증액 경로의 '주문 전 선점'이 막지만, 그 선점분은
        #  다음 주기의 이 재계산으로 리셋된다). 미체결은 보통 한 주기 안에 체결·취소로
        #  정리되고, 과소평가 폭은 그 한 주문분이라 캡을 실질적으로 무너뜨리지 않는다.
        try:
            self.portfolio_heat_amt = self.risk_manager.compute_portfolio_heat(
                holdings, buy_trades_map, live_map=self._live_risk_map(_all_hold_codes))
            self.portfolio_heat_unknown = False
        except Exception as e:
            # [fail-closed] 0으로 두면 '오픈 리스크 없음'이 되어 히트 캡의 예산이 통째로 열린다.
            #  못 센 것과 없는 것은 다르다 — 못 셌다고 표시하고 신규 진입을 막는다.
            self.portfolio_heat_unknown = True
            logger.warning(f"[히트] 오픈 리스크 산출 실패 — 신규 진입을 보류한다: {e}")

        # [최적화] 보유 종목 실시간 데이터 일괄 수집 (Micro-Cache 사전 예열)
        codes_to_prefetch = []
        for item in holdings:
            code = item['pdno']
            qty = api.safe_int(item.get('ord_psbl_qty'))
            if not self.order_manager.is_pending(code) and qty > 0:
                codes_to_prefetch.append(code)
                
        if codes_to_prefetch:
            api.prefetch_multiple_current_prices(codes_to_prefetch, is_overseas=False, include_investor=False, prefer_ws=True)

        # [수정] 일괄 예열 캐시를 활용하므로 워커별 딜레이를 대폭 단축 (Rate Limit 안전장치 유지)
        tps = config.REAL_TX_PER_SECOND
        safe_delay = (1.0 / tps) * 0.1  
        
        # [추가] 시장 국면 판단 (적응형 임계값용) - 매도 분석 시에도 상태 분류를 위해 필요
        market_regime_adj = {}
        if config.MARKET_REGIME_PARAMS.get("USE_ADAPTIVE_THRESHOLD", True):
            for m_type in ["KOSPI", "KOSDAQ"]:
                regime, adj = analysis.get_market_regime(m_type)
                market_regime_adj[m_type] = adj

        # [Fix: Point 1] 매도 분석 루프 병렬화 (ThreadPoolExecutor 활용)
        def _sell_worker(item):
            if not self.is_running: return
            
            code = item['pdno']; name = item['prdt_name']
            
            # [추가] 트레이딩 제한 종목은 매도 분석에서 완전히 제외 (수동 매수/홀딩용)
            #  [Fix] 단, 시스템이 손절하지 않는 포지션이므로 손절선 이탈 시 경보는 발송한다.
            if code in restricted_stocks:
                self.set_stock_state(code, None)
                self.log(f"[분석스킵] {name}: {UNMANAGED_RESTRICTED}")
                self._alert_unmanaged_stop(code, name, item, UNMANAGED_RESTRICTED, rule=rules_map.get(code),
                                           buy_trades=buy_trades_map.get(code))
                return
            
            if self.order_manager.is_pending(code):
                self.set_stock_state(code, None)
                # [보호 공백 해소] 주문이 걸려 있는 동안 이 종목은 매도 판정에서 통째로 빠진다.
                #  부분체결로 이미 확보된 물량이 손절선을 이탈해도 주문이 종결될 때까지
                #  청산되지 않는다("탈출 전략이 없다면 포지션을 잡지 마라"와 충돌).
                #  손절 상황이면 미체결 '매수'를 즉시 취소해, 다음 주기에 정상 청산되게 한다.
                self._cancel_pending_buy_on_stop_loss(code, name, item, buy_trades_map.get(code),
                                                     rule=rules_map.get(code))
                # [관측성] 종전에는 DEBUG 로그라 화면·파일 어디에도 남지 않았다. 이 스킵은
                #  손절·트레일링을 통째로 끄는 경로이므로 **항상** 남긴다 — 매도가 안 나가는데
                #  이유를 알 수 없던 원인이 이것이었다(2026-08-05).
                odnos = self.order_manager.pending_odnos(code)
                streak = self.stuck_pending_streak.get(code, 0) + 1
                self.stuck_pending_streak[code] = streak
                self.log(f"[분석스킵] {name}({code}): 진행 중인 주문 존재 "
                         f"({', '.join(str(o) for o in odnos) or '?'} · {streak}회 연속) "
                         f"— 이 동안 손절·트레일링 판정이 건너뛰어집니다")
                if streak >= STUCK_PENDING_ALERT_CYCLES:
                    self._alert_unmanaged_stop(code, name, item, UNMANAGED_STUCK_PENDING, rule=rules_map.get(code),
                                               buy_trades=buy_trades_map.get(code))
                return
            self.stuck_pending_streak.pop(code, None)

            # [추가] 대체거래소(NXT) 운영 시간에는 ETF 및 NXT 비거래 종목 매도 스킵
            is_nxt_market = api.nxt_order_window()
            is_overseas_stock = not (len(code) == 6 and code[0].isdigit() and code.isalnum())
            
            # [수정] ETF 판정을 관심목록뿐 아니라 종목명 휴리스틱까지 포함하도록 일원화
            #  (보유만 하고 관심목록에 없는 ETF/ETN도 식별)
            is_domestic_etf = (not is_overseas_stock) and api.is_domestic_etf_etn(code, name)

            if is_nxt_market and not is_overseas_stock:
                if is_domestic_etf or (hasattr(api, 'is_nxt_tradeable') and not api.is_nxt_tradeable(code)):
                    self.set_stock_state(code, None)
                    # [앵커 복원] 이 분기는 아래 ETF 제외보다 **먼저** 걸린다. 여기서 그냥
                    #  돌아서면 NXT 시간대(15:30~20:00·08:00~08:50)에만 봇을 켜는 운용에서는
                    #  ETF 앵커가 영영 복원되지 않는다. 주문과 무관한 기록이므로 여기서도 남긴다.
                    self._restore_trailing_anchor(
                        code, name,
                        _pkg().resolve_entry_date(entry_date_map.get(code),
                                                  latest_buy_map.get(code)))
                    return

            # [추가] ETF 포함 여부가 False면 보유 ETF는 자동 매도 대상에서도 제외한다.
            #  (SYSTEM_INCLUDE_ETF는 매수 필터이지만, 사용자 요청에 따라 매도도 제외하여
            #   ETF는 전적으로 수동 관리하도록 한다. 단 시스템이 손절하지 않으므로 주의)
            if is_domestic_etf and not getattr(config, 'SYSTEM_INCLUDE_ETF', False):
                self.set_stock_state(code, None)
                self.log(f"[매도스킵] {name}({code}): {UNMANAGED_ETF}")
                # [Fix] 시스템이 손절하지 않는 포지션이므로 손절선 이탈 시 경보는 발송한다.
                self._alert_unmanaged_stop(code, name, item, UNMANAGED_ETF, rule=rules_map.get(code),
                                           buy_trades=buy_trades_map.get(code))
                # [앵커 복원] 매도 판정에서 빠져도 앵커는 남겨 둔다. 여기서 돌아서면 이 종목의
                #  trailing_stops 는 영영 갱신되지 않아, 마지막 매수의 체결가가 앵커로 굳는다
                #  (102780: 4월 진입·실제 고가 36,360인데 8월 1주 추가 매수로 25,500이 기록).
                #  ETF 포함 설정을 켜는 순간 그 값이 곧바로 청산선의 근거가 되고, DB만 보고
                #  판단하는 도구·감사도 같은 값을 읽는다. 주문은 내지 않으므로 부작용이 없다.
                self._restore_trailing_anchor(
                    code, name,
                    _pkg().resolve_entry_date(entry_date_map.get(code), latest_buy_map.get(code)))
                return

            qty = api.safe_int(item.get('ord_psbl_qty'))
            profit_rate = float(item['evlu_pfls_rt'])
            current_price = float(item['prpr'])
            buy_price = float(item['pchs_avg_pric'])
            
            time.sleep(safe_delay)
            
            if not self.is_running: return # 대기 후 재확인
            
            if qty <= 0:
                # [관측성] 위 대기 주문 스킵과 같은 이유로 항상 남긴다. 잔고에는 보이는데
                #  주문 가능 수량만 0이면 시스템은 그 포지션을 지켜주지 못한다.
                self.set_stock_state(code, None)
                self.log(f"[분석스킵] {name}({code}): 주문 가능 수량 0 "
                         f"— 손절·트레일링 판정이 건너뛰어집니다")
                return

            # [안전장치] 현재가가 0/음수면 판정 자체가 불가능하다. 잔고 응답의 prpr은 거래정지·
            #  장 시작 전·API 이상에서 0으로 올 수 있는데, 그대로 태우면 수익률이 -100%로 계산되어
            #  본전청산·트레일링이 동시에 오발동하고(실측: analyze_sell이 '본전청산(-100.0%)'로
            #  매도 판정), 주문가도 0원이 되어(order_price<=0 폴백이 current_price=0을 되돌림)
            #  지정가 0원 매도가 전송된다. 거부되더라도 그 종목은 is_pending으로 다음 주기의 매도
            #  분석에서 빠져, 정작 진짜 손절이 필요할 때 막힌다.
            #  판정 불가이므로 주문을 내지 않고 보류하되, 시스템이 지켜주지 못하는 포지션이므로
            #  손절선 이탈 경보는 보낸다(트레이딩 제한·ETF 제외와 같은 취급).
            if current_price <= 0:
                self.set_stock_state(code, None)
                self.log(f"[분석스킵] {name}({code}): 현재가 이상({current_price}) — {UNMANAGED_BAD_PRICE}")
                self._alert_unmanaged_stop(code, name, item, UNMANAGED_BAD_PRICE, rule=rules_map.get(code),
                                           buy_trades=buy_trades_map.get(code))
                return

            # [안전장치] 시세 조회에 실패해 폴백한 값(_price_stale)은 판정 근거가 될 수 없다.
            #  관찰 모드는 실패 시 직전 정상가로 폴백하는데, 그 값으로 손절을 판정하면
            #  실제로는 이미 손절선을 넘겼는데도 옛 가격으로 '아직 괜찮다'는 답이 나온다.
            #  실계좌(KIS)는 조회 실패 시 잔고 자체가 None이 되어 주기가 통째로 멈추므로,
            #  같은 취급(판정 보류 + 경보)이 실거래 동작과도 일치한다.
            if item.get('_price_stale'):
                self.set_stock_state(code, None)
                self.log(f"[분석스킵] {name}({code}): {UNMANAGED_STALE_PRICE}")
                self._alert_unmanaged_stop(code, name, item, UNMANAGED_STALE_PRICE, rule=rules_map.get(code),
                                           buy_trades=buy_trades_map.get(code))
                return

            rule = rules_map.get(code)
            market_type = self._get_stock_market_type(code)
            score_adj = market_regime_adj.get(market_type, 0.0)
            
            # [최적화] 주기 시작 시 배치 로드한 결과 사용 (종목별 개별 쿼리 제거)
            # [Fix] 보유일수는 '최근 매수'가 아니라 진입일(보유수량이 0 → 1 이상이 된 시점) 기준.
            #  분할 매수·피라미딩으로 1주만 더 담아도 시간청산 시계가 0으로 리셋되던 문제.
            last_buy = latest_buy_map.get(code)
            holding_days, is_mr_holding = _pkg().resolve_holding_context(
                last_buy, entry_date=entry_date_map.get(code))
            entry_date = _pkg().resolve_entry_date(entry_date_map.get(code), last_buy)

            highest_price = self._cached_anchor(code)

            # [안전장치] 액면분할·무상증자로 평단이 재조정되면 최고가만 조정 전 값으로 남아
            #  트레일링 스탑이 즉시 오발동한다(5:1 분할 → drop_rate 80% → 시장가 강제 매도).
            #  최고가 갱신은 단조 증가라 스스로 내려오지 못하므로 여기서 같은 배율로 보정한다.
            highest_price = self._apply_corporate_action(code, name, item, buy_price, highest_price)

            if current_price > buy_price:
                if highest_price == 0.0 or current_price > highest_price:
                    db_manager.db.update_highest_price(code, current_price)
                    with self._lock:
                        self.trailing_stop_cache[code] = current_price
                    highest_price = current_price

            df = api.get_chart_data(code, is_overseas=is_overseas_stock)
            
            # [추가] 차트 데이터 당일 종가/고가/저가 실시간 갱신 (지표 불일치 완벽 방지)
            #  모든 장 종료 후에는 반영하지 않는다(KRX 확정 종가 유지). 손절·트레일링 판정은
            #  아래 analyze_sell에 current_price를 그대로 넘기므로 실시간 대응에는 영향이 없다.
            indicators.apply_realtime_price(df, api.chart_overlay_price(current_price, is_overseas_stock))

            highest_price = self._restore_trailing_anchor(
                code, name, entry_date, highest_price, df=df, is_overseas=is_overseas_stock)

            # [SSOT] 임계값 조립은 build_sell_thresholds가 단독 보유한다.
            #  잔고 화면(메뉴 9-2)의 보유 분석도 같은 함수를 호출해 판정이 갈리지 않게 한다.
            # [최적화] 주기 시작 시 배치 로드한 buy_trades_map 사용 (종목별 개별 쿼리 제거)
            # [Fix] 매수 기록이 없는 포지션(HTS 직접 매수)은 진입 시점 봉의 ATR에서 손절률을
            #  복원한다. 차트가 필요하므로 df 확보 뒤로 옮겼다(그 전까지 thresholds는 쓰이지 않는다).
            thresholds = _pkg().build_sell_thresholds(
                rule=rule, score_adj=score_adj, buy_trades=buy_trades_map.get(code, []),
                fallback_atr_rate=_pkg().entry_atr_stop_rate(df, entry_date)
            )

            already_half_sold = code in self.half_tp_cache
            result = self.strategy.analyze_sell(code, name, df, current_price, buy_price, profit_rate, thresholds=thresholds, already_half_sold=already_half_sold, holding_days=holding_days, is_mr_holding=is_mr_holding, highest_price=highest_price)
            
            # [추가] 분석 성공 시 상태 업데이트
            self.set_stock_state(code, result['state'])
            
            ind = result['ind']

            # [히트] 이번 주기 판정이 실제로 쓴 손절선 재료를 남긴다. 오픈 리스크 산출은
            #  주기 앞머리(잔고 직후)에서 도는데 그 자리엔 차트가 없어, 종전에는 매수 시점
            #  손절률에서 ATR을 역산했다 — 추세가 길어질수록 실제 ATR이 커져 실제 콜백이
            #  넓어지는 만큼 리스크를 과소 계상한다(방어가 무뎌지는 한 방향).
            #  다음 주기가 이 값을 쓴다(60초 전의 일봉 ATR이라 실질 차이가 없다).
            try:
                with self._lock:
                    self.holding_risk_cache[code] = {
                        'sl_rate': float(thresholds.get(
                            "STOP_LOSS_RATE", config.SELL_STRATEGY.get("STOP_LOSS_RATE", -7.0)) or 0.0),
                        'atr': float(ind.get('atr') or 0.0),
                    }
            except (TypeError, ValueError):
                pass

            rsi_val = f"{ind.get('rsi'):.1f}" if ind.get('rsi') is not None else "-"
            adx_val = f"{ind.get('adx'):.1f}" if ind.get('adx') is not None else "-"
            cci_val = f"{ind.get('cci'):.1f}" if ind.get('cci') is not None else "-"
            action_str = "매도" if result['action'] == 'sell' else "보유"
            rule_msg = " [개별 룰 적용]" if rule else ""
            extra_info = ""
            if thresholds:
                bs = thresholds.get('BUY_SCORE')
                if bs is not None: extra_info += f", 기준={bs:.1f}"
                w = thresholds.get('WEIGHTS')
                if w: extra_info += f", 가중치={w.get('TREND',0):.1f}/{w.get('MOMENTUM',0):.1f}/{w.get('STRENGTH',0):.1f}/{w.get('SYNERGY',0):.1f}"

            self.log(f"[보유분석] {name}({code}): 수익률={profit_rate:.2f}%, 점수={result['score']}, 상태={result['state']}, 판단={action_str}, RSI={rsi_val}, ADX={adx_val}, CCI={cci_val}{extra_info}{rule_msg}")

            if result['action'] == 'sell':
                reason = result['reason']
                score = result['score']
                sell_ratio = result.get('sell_ratio', 1.0)
                
                if sell_ratio < 1.0:
                    target_sell_qty = int(qty * sell_ratio)
                    if target_sell_qty < 1:
                        self.log(f"매도 보류: {name} - 보유 수량({qty}주) 부족으로 분할 매도({reason}) 스킵 (최종 목표 대기)")
                        return
                else:
                    target_sell_qty = qty
                
                if rule: reason += " [개별 룰 적용]"
                if thresholds.get("ATR_APPLIED_SL_RATE") is not None and "손절" in reason: reason = reason.replace("손절", "ATR손절")
                
                raw_order_price = current_price * (1 - config.SLIPPAGE_RATE)
                #  ETF·ETN 은 호가 격자가 다르다(2,000원 이상 5원 단일). 주권 표로
                #  반올림하면 손절 지정가가 최대 tick/2 위로 밀려 체결이 늦어진다.
                order_price = int(utils.adjust_to_tick(raw_order_price, is_overseas=False,
                                                       is_etf=is_domestic_etf))
                if order_price <= 0: order_price = int(current_price)
                # [안전장치] 하한가에 락된 날 '현재가 - 슬리피지'는 제한폭 밖이라 주문이 거부된다.
                #  손절이 가장 필요한 날 접수조차 되지 않으므로 제한폭 안으로 되돌린다.
                order_price = int(self._clamp_order_price(code, order_price))

                if not is_market_open:
                    self.log(f"[장마감] 매도 신호 감지 (주문 미전송): {name} - {reason}")
                    self._alert_after_hours_sell(code, name, item, reason,
                                                 current_price, order_price, target_sell_qty)
                    return

                real_qty = api.fetch_sellable_quantity(code)

                # [안전장치] 조회 실패(None)는 '팔 수 없음'이 아니다. 종전에는 실패도 0으로
                #  와서 아래 분기가 매도를 중단시켰다 — 일시적 API 오류가 손절을 거르는
                #  결과로 이어진다. 매수 경로는 같은 상황에서 예수금 폴백으로 주문을 내고,
                #  수동 매매 화면도 잔고 수량으로 폴백한다. 자동 청산만 반대 방향이었다.
                #  추세추종에서 못 파는 비용 > 못 사는 비용이므로, 이미 확보한 잔고 수량으로
                #  낸다. 정말 팔 수 없는 상태라면 증권사가 거부하고, 그 비용이 훨씬 싸다.
                if real_qty is None:
                    held_qty = api.safe_int(item.get('hldg_qty'))
                    self.log(f"매도 수량 조회 실패: {name} — 보유 {held_qty}주 기준으로 진행합니다 "
                             f"(조회 실패를 '매도 불가'로 읽지 않는다)")
                    real_qty = held_qty

                if real_qty < target_sell_qty:
                    if real_qty > 0:
                        self.log(f"매도 수량 조정: {name} {target_sell_qty}주 -> {real_qty}주")
                        target_sell_qty = real_qty
                        self.no_sellable_streak.pop(code, None)
                    else:
                        # [안전장치] 매도를 결정했는데 팔 수 없는 상태다. 종전에는 로그 한 줄만
                        #  남기고 조용히 끝나, 거래정지·상장폐지처럼 시스템이 스스로 빠져나올 수
                        #  없는 포지션이 운영자 모르게 방치됐다. 다만 미체결 취소 직후 한 주기
                        #  정도는 정상적으로 0이 되므로, 연속 관측될 때만 경보한다.
                        streak = self.no_sellable_streak.get(code, 0) + 1
                        self.no_sellable_streak[code] = streak
                        self.log(f"매도 중단: {name} 주문 가능 수량 부족 "
                                 f"(미체결 존재 가능성 · {streak}회 연속)")
                        if streak >= NO_SELLABLE_ALERT_CYCLES:
                            self._alert_unmanaged_stop(code, name, item, UNMANAGED_NO_SELLABLE, rule=rules_map.get(code),
                                                       buy_trades=buy_trades_map.get(code))
                        return
                else:
                    self.no_sellable_streak.pop(code, None)

                self.log(f"매도 실행: {name} - {reason}")
                # [비용] 종전에는 KIS 잔고의 '평가손익'(evlu_pfls_amt)을 그대로 실현손익으로
                #  적었다. 그것은 매도 판단 시점의 미실현 손익이라 매도 수수료·거래세도,
                #  체결가와 판단가의 차이도 빠져 있다. 여기서는 왕복 비용을 뺀 값으로 적고,
                #  매입가를 함께 남겨 체결 확인 단계에서 실제 체결가로 다시 계산하게 한다.
                try:
                    sell_buy_price = float(item.get('pchs_avg_pric') or 0)
                except (TypeError, ValueError):
                    sell_buy_price = 0.0
                try:
                    ref_price = float(order_price) or float(item.get('prpr') or 0)
                except (TypeError, ValueError):
                    ref_price = 0.0
                est_profit, est_rate = trading_cost.net_realized_profit(
                    sell_buy_price, ref_price, target_sell_qty)
                if sell_buy_price <= 0:      # 매입가를 못 구하면 종전 값으로 폴백
                    est_profit, est_rate = int(item['evlu_pfls_amt']), profit_rate
                odno = self.order_manager.send_order(code, target_sell_qty, "sell", name=name, profit_amt=int(est_profit), profit_rate=est_rate, reason=reason, score=score, price=order_price, rule=rule, buy_price=sell_buy_price)
                if odno:
                    record = {
                        "type": "sell", "code": code, "name": name, "qty": target_sell_qty,
                        "price": float(order_price), "profit_rate": profit_rate,
                        "profit_amt": int(item['evlu_pfls_amt']), "reason": reason,
                        "time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"), "odno": odno
                    }
                    self.trade_records.append(record)
                    
                    # [Fix: Point 2] 반익절 캐시 DB 동기화
                    if sell_ratio < 1.0: 
                        self.half_tp_cache.add(code)
                        db_manager.db.insert_half_tp(code)
                    else:
                        target_cano = config.session.auto_cano
                        target_acnt = config.session.auto_acnt_prdt_cd
                        canceled_cnt = db_manager.db.cancel_reserved_sell_orders(target_cano, target_acnt, code)
                        if canceled_cnt > 0:
                            self.log(f"[예약취소] 전량 매도로 인해 대기 중이던 {name} 매도 예약 주문 {canceled_cnt}건 자동 취소")
                            api.send_telegram_message(f"🗑 [예약 취소] {name}({code}) 전량 매도로 인해 대기 중이던 매도 예약 주문 {canceled_cnt}건이 자동 취소되었습니다.")
                            
                        # [Fix] 앵커(트레일링 최고가·반익절 기록) 정리는 체결 확정(FILLED) 시점으로 유예.
                        #  접수 시점에 지우면 미체결 취소 시 포지션은 남는데 앵커만 현재가로 리셋되어
                        #  샹들리에 TS 감시가 느슨해지는 문제가 있었다 (정리는 OrderManager.update_order_status에서 수행)
                        with self.order_manager._lock:
                            self.order_manager.sell_cleanup_odnos[str(odno)] = code
                            
                    # [추가] 매수 로직(상관관계 분석 등)에서 이미 매도한 종목을 보유 중인 것으로 오인하지 않도록 메모리 잔고 즉시 차감
                    try:
                        item['hldg_qty'] = str(max(0, int(item.get('hldg_qty', 0)) - target_sell_qty))
                    except Exception: pass
            else:
                # [추세추종] 보유 판정 시 피라미딩(수익 포지션 증액) 평가
                self._try_pyramid_buy(code, name, qty, current_price, profit_rate, result, last_buy, is_market_open, rule=rule)

        # 병렬 처리 실행
        # [최적화] 모의투자도 워커 2개로 병렬화 (2 TPS 제한은 api 레이어의 스로틀이 보장하므로
        #  REST 대기 구간이 겹쳐져 주기당 소요 시간이 단축됨)
        max_workers = 5

        def _sell_worker_guarded(item):
            """[관측성] 매도 판정의 예외를 반드시 회수해 로그·경보로 남긴다.

            종전에는 executor.submit 결과를 wait만 하고 result()를 부르지 않아, 워커에서
            난 예외가 어디에도 남지 않았다. 그러면 그 종목은 [보유분석] 줄도 [분석스킵]
            줄도 없이 화면에서 사라지고, 손절·트레일링이 매 주기 조용히 건너뛰어진다
            (2026-08-05: 개별 룰의 NULL 컬럼 하나로 analyze_sell이 TypeError로 죽었는데
            로그에 아무 흔적이 없었다). 판정 못 한 포지션은 '보호되지 않는 포지션'이므로
            트레이딩 제한·ETF 제외와 같은 취급으로 손절선 이탈 경보까지 보낸다.
            """
            try:
                return _sell_worker(item)
            except Exception as e:
                code = item.get('pdno')
                name = item.get('prdt_name') or code
                try:
                    self.set_stock_state(code, None)
                    self.log(f"[분석실패] {name}({code}): 매도 판정 중 오류 — "
                             f"{type(e).__name__}: {e} — 이번 주기에 손절·트레일링 판정을 받지 못했습니다")
                    logger.exception(f"[매도분석] {code} 판정 실패")
                    self._alert_unmanaged_stop(code, name, item, UNMANAGED_ANALYSIS_ERROR, rule=rules_map.get(code),
                                               buy_trades=buy_trades_map.get(code))
                except Exception:
                    logger.exception(f"[매도분석] {code} 실패 처리 중 2차 오류")

        # [계좌 라우팅] 워커 스레드는 제출 스레드의 계좌 컨텍스트를 상속하지 않는다
        #  (threading.local). 감싸지 않으면 이 안에서 나가는 손절·트레일링 매도 주문과
        #  매도가능수량 조회가 자동매매 계좌가 아닌 수동 계좌로 향한다. 반드시 제출
        #  스레드에서 래핑한다 — utils.inherit_account_context 주석 참조.
        _sell_task = utils.inherit_account_context(_sell_worker_guarded)
        with concurrent.futures.ThreadPoolExecutor(max_workers=max_workers, thread_name_prefix="at_sell") as executor:
            futures = [executor.submit(_sell_task, item) for item in holdings]
            concurrent.futures.wait(futures)

    def _try_pyramid_buy(self, code, name, held_qty, current_price, profit_rate, result, last_buy, is_market_open, rule=None):
        """[추세추종] 수익 포지션 증액(피라미딩) 시도

        보유분석에서 '보유' 판정된 종목에 대해, 수익으로 추세가 검증되었고(수익률 트리거 이상)
        매수 신호가 유지 중이면 보유 수량의 일정 비율만큼 1회 한정(기본) 증액한다.
        물타기(손실 추가매수)와 정반대로, 손실 종목에는 절대 발동하지 않는다.
        """
        try:
            # 국내 종목만 지원 (시스템 트레이딩 매수 경로와 동일 범위)
            if not (len(code) == 6 and code[0].isdigit() and code.isalnum()):
                return

            # [안전장치] 방어 모드에서는 증액(노출 확대)도 신규 매수와 동일하게 보류한다.
            if getattr(self, 'buy_halted', False):
                return

            # [안전장치] 미체결 주문 현황을 모르면 증액도 보류한다 — 신규 매수와 같은 이유다
            #  (_check_buy_conditions의 pending_restore_ok 게이트). 재기동 직후 복구 조회가
            #  실패하면 is_pending 맵이 비어 있어 '미체결 없음'과 '모름'이 구분되지 않는다.
            #  그 상태로 증액하면 거래소에 이미 걸린 주문을 못 본 채 두 번째를 낸다
            #  (주문 유실은 재전송하지 않는다는 규약과 같은 자리). manage_unfilled_orders가
            #  성공하면 자동 해제된다.
            if not getattr(self, 'pending_restore_ok', True):
                self.log(f"피라미딩 보류: {name} - 미체결 주문 현황 미확인 (중복 주문 방지)")
                return

            # [증액 횟수] 권위 소스는 trailing_stops.pyramid_count 다(재시작에도 유지되고,
            #  신규 진입 시 delete_trailing_stop 으로 함께 지워져 자동으로 0이 된다).
            #
            #  종전에는 최근 매수 **사유 문자열**을 정규식으로 파싱했다. 그 기록이 유실되면
            #  (DB 쓰기 실패·수동 정리) 횟수가 0으로 읽혀 상한을 넘겨 계속 증액된다.
            #  증액은 보유수량의 50%씩이라 1 → 1.5 → 2.25 → 3.375 로 커지는데, 횟수가
            #  계속 0이면 여기서 멈추지 않고 한 종목이 계좌를 삼킨다.
            #
            #  [모르면 보류] 조회 실패(-1)는 '0회'가 아니다. 리스크를 키우는 동작이므로
            #  불확실하면 하지 않는다 — 증액을 거른 대가는 놓친 수익뿐이다.
            db_count = db_manager.db.get_pyramid_count(code)
            if db_count < 0:
                self.log(f"피라미딩 보류: {name} - 증액 횟수를 확인할 수 없습니다(DB 조회 실패)")
                return

            #  구 버전 포지션 호환: 사유 마커가 더 크면 그쪽을 믿는다(보수적).
            legacy_count = 0
            if last_buy:
                m = re.search(r'피라미딩\s*(\d+)차', str(last_buy.get('reason', '')))
                if m:
                    legacy_count = int(m.group(1))
            pyramid_count = max(db_count, legacy_count)

            ok, reason = self.strategy.analyze_pyramid(profit_rate, result['state'], result['score'], pyramid_count)
            if not ok:
                return

            # [리스크 관리] 시장 필터 게이트: 신규 매수가 차단되는 약세 시장(지수<SMA)에서는
            # 검증된 포지션이라도 증액(노출 확대)을 보류한다. (기존 보유·청산에는 영향 없음)
            # [Fix] 신규 매수 경로와 동일하게 fail-closed — 지수 판단 불가(데이터 장애·캐시 없음)도 보류.
            if (getattr(config, 'USE_MARKET_FILTER', True)
                    and config.ANALYSIS_THRESHOLDS.get("PYRAMIDING_REQUIRE_HEALTHY_MARKET", True)):
                m_type = self._get_stock_market_type(code)
                m_stat = self.market_index_status.get(m_type)
                if not isinstance(m_stat, dict) or not m_stat.get('is_healthy', False):
                    cause = "판단 불가(데이터 없음)" if not isinstance(m_stat, dict) or m_stat.get('unknown') else "약세"
                    self.log(f"피라미딩 보류: {name} - {m_type} 지수 {cause}(시장 필터)로 증액 보류")
                    return

            if not is_market_open:
                self.log(f"[장마감] 피라미딩 신호 감지 (주문 미전송): {name} - {reason}")
                return
            if self.order_manager.is_pending(code):
                return

            ratio = config.ANALYSIS_THRESHOLDS.get("PYRAMIDING_RATIO", 0.5)
            add_qty = int(held_qty * ratio)
            if add_qty < 1:
                return

            raw_order_price = current_price * (1 + config.SLIPPAGE_RATE)
            order_price = int(utils.adjust_to_tick(
                raw_order_price, is_overseas=False,
                is_etf=api.is_domestic_etf_etn(code, name)))
            if order_price <= 0:
                order_price = int(current_price)
            # [안전장치] 상한가에 락되면 '현재가 + 슬리피지'가 제한폭을 넘어 거부된다.
            order_price = int(self._clamp_order_price(code, order_price))

            max_qty = api.fetch_buyable_quantity(code, order_price)
            if max_qty < add_qty:
                if max_qty < 1:
                    self.log(f"피라미딩 보류: {name} - 예수금 부족 (필요:{add_qty}주)")
                    return
                add_qty = max_qty

            # 증액분 손절률: 신규 매수와 동일하게 현재 ATR 기준으로 계산 (가중평균 손절선에 자동 반영)
            # [Fix] 신규 매수 경로(_execute_buy_orders)와 동일하게 개별 룰의 손절 설정을 우선 적용
            sl_rate = config.SELL_STRATEGY["STOP_LOSS_RATE"]
            use_atr_stop = config.SELL_STRATEGY.get("USE_ATR_STOP", True)
            atr_mult = config.SELL_STRATEGY.get("ATR_STOP_MULTIPLIER", 2.0)
            if rule:
                sl_rate = rule.get('stop_loss', sl_rate)
                if rule.get('use_atr_stop') is not None:
                    use_atr_stop = bool(rule['use_atr_stop'])
                if rule.get('atr_stop_multiplier') is not None:
                    atr_mult = rule['atr_stop_multiplier']
            ind = result.get('ind') or {}
            atr_val = ind.get('atr', 0) or 0
            if use_atr_stop and atr_val > 0 and current_price > 0:
                # [SSOT] 신규 매수와 같은 함수를 쓴다 (engine.atr_stop_rate).
                sl_rate = _pkg().atr_stop_rate(atr_val, current_price, atr_mult=atr_mult) or sl_rate

            # [추세추종 안전장치] "탈출 전략이 없다면 포지션을 잡지 마라" — 신규 매수
            #  (_execute_buy_orders)와 같은 게이트다. 종전에는 증액 경로에만 이 검사가 없어,
            #  ATR 손절 OFF + 고정 손절 0(둘 다 사용자 설정으로 가능)이면 청산 기준 없는
            #  포지션을 더 키웠다. 게다가 아래 히트 캡은 sl_rate<0 일 때만 도는 구조라
            #  리스크 회계에서도 통째로 빠졌다(add_risk=0 → 예산 확인 자체를 건너뜀).
            #  개별 룰의 stop_loss가 NULL이면 값이 None으로 들어오는 것도 여기서 걸린다
            #  (종전에는 아래 비교에서 TypeError가 나 '피라미딩 오류'로만 남았다).
            try:
                sl_rate = float(sl_rate)
            except (TypeError, ValueError):
                sl_rate = 0.0
            if sl_rate >= 0:
                self.log(f"피라미딩 보류: {name} - 손절 기준 없음 "
                         f"(ATR 손절 {'ON(ATR 미확보)' if use_atr_stop else 'OFF'} + 고정 손절 0). "
                         f"청산 기준과 손실액 상한이 모두 없는 증액은 하지 않습니다.")
                return

            # [추가] 포트폴리오 히트 캡: 증액분 리스크가 남은 예산을 넘으면 피라미딩 보류.
            #  (_sell_worker 스레드 동시 실행 대비, 예산 확인과 선점을 락으로 원자화)
            #  (sl_rate < 0 은 위 게이트가 보장한다 — 조건부로 두면 '손절 없음'이 다시
            #   히트 캡 우회 경로가 된다)
            add_risk = (add_qty * order_price) * (abs(sl_rate) / 100.0)
            reserved_heat = False
            if add_risk > 0:
                with self._lock:
                    # 이 경로엔 예수금 변수가 없다. 기준자산이 없을 때의 폴백으로
                    #  '지금 살 수 있는 금액'(매수가능수량×주문가)을 넘긴다.
                    budget_left = self.risk_manager.portfolio_risk_budget_left(
                        avail_cash=max(max_qty, 0) * order_price)
                    if budget_left is not None:
                        if add_risk > budget_left:
                            cap = self.risk_manager.effective_portfolio_cap()
                            self.log(f"피라미딩 보류: {name} - 포트폴리오 총 리스크 한도({cap:.1f}%) 초과 "
                                     f"(증액 리스크 {add_risk:,.0f}원 > 남은 예산 {max(budget_left, 0):,.0f}원)")
                            return
                        self.portfolio_heat_amt += add_risk
                        reserved_heat = True

            # [순서] 횟수를 **먼저** 올리고 주문한다. 반대면 '주문은 나갔는데 횟수는 그대로'가
            #  되어 다음 주기에 같은 증액이 또 나간다. 기록만 되고 주문이 실패하면 증액
            #  기회를 하나 잃을 뿐이므로, 이쪽이 안전한 방향이다.
            if not db_manager.db.bump_pyramid_count(code, pyramid_count):
                if reserved_heat:
                    with self._lock:
                        self.portfolio_heat_amt -= add_risk
                self.log(f"피라미딩 보류: {name} - 증액 횟수를 기록하지 못했습니다(DB 쓰기 실패). "
                         f"기록 없이 증액하면 상한을 넘겨 반복됩니다.")
                return

            self.log(f"피라미딩 실행: {name} +{add_qty}주 - {reason}")
            odno = self.order_manager.send_order(code, add_qty, "buy", name=name, reason=reason, score=result['score'], price=order_price, stop_loss_rate=sl_rate)
            if not odno and reserved_heat:
                with self._lock:
                    self.portfolio_heat_amt -= add_risk  # 주문 실패 시 선점분 반납
            if odno:
                record = {
                    "type": "buy", "code": code, "name": name, "qty": add_qty,
                    "price": order_price, "reason": reason,
                    "time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"), "odno": odno,
                    "stop_loss_rate": sl_rate
                }
                self.trade_records.append(record)
        except Exception as e:
            self.log(f"[피라미딩 오류] {name}: {e}")

    def _check_buy_conditions(self, holdings, deposit_res, is_market_open=True, rules_map=None, restricted_stocks=None):
        # [안전장치] 방어 모드(일일 손실 한도 초과 등)에서는 신규 진입만 차단한다.
        #  매도 검사(_check_sell_conditions)는 이 게이트 앞에서 이미 수행되므로 손절 감시는 유지된다.
        if getattr(self, 'buy_halted', False):
            if self.consecutive_errors == 0:  # 로그 도배 방지
                self.log(f"매수 스킵: 방어 모드 — {self.buy_halt_reason or '신규 매수 중단'}")
            return

        # [진입 게이트] 개장 직후 신규 진입 보류. 방어 모드와 같은 자리에 두는 이유는
        #  '신규 매수만 막고 청산 감시는 그대로 둔다'는 성질이 같기 때문이다 —
        #  _check_sell_conditions는 이 함수 밖에서 이미 수행된다.
        #  [근거] 개장 첫 30분 진입은 스파이크를 고점에서 추격하고 되돌림을 맞는다.
        #   tools/audit_time_of_day.py(30m·씨드5) 전체 136.7% vs 현행 131.1%(47-0-28),
        #   같은 차단량 무작위 대조 5장 전승 — 상세는 config.SYSTEM_ENTRY_OPEN_DELAY_USE 주석.
        if is_market_open:
            delay_left = entry_open_delay_remaining()
            if delay_left > 0:
                if self.consecutive_errors == 0:  # 로그 도배 방지
                    self.log(f"매수 스킵: 개장 직후 진입 보류 — {delay_left // 60}분 "
                             f"{delay_left % 60}초 남음 (청산 감시는 계속됩니다)")
                return

        # [안전장치] 미체결 주문 현황을 모르면 신규 매수를 보류한다. 재기동 직후 복구
        #  조회가 실패한 상태로 매수하면, 이미 거래소에 걸린 주문을 못 보고 같은 종목에
        #  두 번째 주문을 낸다. manage_unfilled_orders가 성공하면 자동 해제된다.
        if not getattr(self, 'pending_restore_ok', True):
            if self.consecutive_errors == 0:
                self.log("매수 스킵: 미체결 주문 현황 미확인 — 중복 주문 방지를 위해 보류")
            return

        # [수정] 매수 대상 확장을 위해 국내 주식 및 국내 ETF 리스트 병합 (그룹 정보 추가)
        targets = []
        for item in config.session.stock_data.get("stocks_kr", []):
            item_copy = dict(item)
            item_copy['group'] = 'stocks_kr'
            targets.append(item_copy)
            
        # [수정] ETF 포함 여부 설정에 따라 대상에 추가
        if getattr(config, 'SYSTEM_INCLUDE_ETF', False):
            for item in config.session.stock_data.get("etfs_kr", []):
                item_copy = dict(item)
                item_copy['group'] = 'etfs_kr'
                targets.append(item_copy)
            
        if not targets: return
        
        # [추가] 필터링 카운트 초기화 (매 주기마다 갱신)
        self.skipped_by_market_filter_count = {"KOSPI": 0, "KOSDAQ": 0}
        skipped_stocks = [] # [추가] 시장 필터링으로 보류된 종목 리스트
        
        # [추가] 보유 종목 조회 (중복 매수 방지 및 그룹 정보 매핑)
        holding_codes = set()
        holding_names_map = {}
        holding_groups_map = {}
        
        code_to_group = {}
        for key in ["stocks_kr", "etfs_kr", "stocks_us", "etfs_us"]:
            for item in config.session.stock_data.get(key, []):
                code_to_group[item['code']] = key
                
        if holdings:
            for h in holdings:
                # [추가] 이번 루프의 매도 로직에서 수량이 0이 된 종목은 제외
                if int(h.get('hldg_qty', 0)) <= 0:
                    continue
                code = h['pdno']
                holding_codes.add(code)
                holding_names_map[code] = h['prdt_name']
                holding_groups_map[code] = code_to_group.get(code, 'stocks_kr')
        
        # [수정] 최대 보유 종목 수 체크 (투자 비중은 개별 룰이 없으면 전역/자동값을 따른다)
        invest_ratio = config.resolve_invest_ratio()
        max_holdings = config.settings.SYSTEM_MAX_HOLDINGS

        can_buy = True
        # [추가] 스킵 사유를 문자열로 들고 간다. 종전에는 여기서만 로그로 흘리고 버려서,
        #  뒤에서 후보를 못 산 이유가 '조건 미달'이라는 뭉뚱그린 문구로만 남았다.
        #  두 로그는 시각이 벌어져(분석 한 바퀴) 붙여 읽기도 어렵다.
        buy_skip_reason = ""
        # [신호 원장] 계좌 상태로 막힌 사실을 원장까지 들고 간다. 종전에는 로그로만 남아,
        #  원장을 읽는 감사가 '게이트를 통과했는데 왜 안 샀나'에 답할 수 없었다.
        buy_block = None

        if len(holding_codes) >= max_holdings:
            buy_skip_reason = (f"보유 슬롯 가득 참 — {len(holding_codes)}/{max_holdings}종목 "
                               f"(투자비중 {config.format_invest_ratio()})")
            if self.consecutive_errors == 0: # 로그 도배 방지
                self.log(f"매수 스킵: 최대 보유 종목 수({max_holdings}개) 도달 (투자비중 {config.format_invest_ratio()} 기준) - 종목분석은 계속 진행합니다.")
            can_buy = False
            buy_block = 'slot'

        # 예수금 확인 (API 직접 호출)
        avail_cash = 0
        if deposit_res:
            avail_cash = deposit_res['d2_deposit'] # 주문 가능 금액은 D+2 예수금 기준
        else:
            if not can_buy:
                avail_cash = 0
            else:
                return # 조회 실패 시 매수 중단

        # [수정] 최소 주문 가능 금액 하향 조정 (50,000 -> 1,000) 및 로그 추가
        min_cash = 1000
        if can_buy and avail_cash < min_cash:
            buy_skip_reason = f"예수금 부족 — {avail_cash:,}원 (최소 {min_cash:,}원)"
            if self.consecutive_errors == 0: # 로그 도배 방지
                 self.log(f"매수 스킵: 예수금 부족 ({avail_cash:,}원 < {min_cash:,}원) - 종목분석은 계속 진행합니다.")
            can_buy = False
            buy_block = 'cash'
            
        # [추가] 개별 룰 로드 ([최적화] 루프에서 주기당 1회 로드해 전달받으면 재조회 생략)
        if rules_map is None:
            custom_rules = db_manager.db.get_all_stock_strategies()
            custom_rules = _enrich_rules_with_weights(custom_rules) # [추가] 가중치 보강
            rules_map = {r['code']: r for r in custom_rules}

        # [추가] 당일 매도 이력 확인 및 재진입 허들(체결강도) 설정
        today_str = datetime.now().strftime("%Y-%m-%d")
        target_account = None
        if config.session.auto_cano:
            target_account = f"{config.session.auto_cano}-{config.session.auto_acnt_prdt_cd}"
            
        try:
            today_trades = db_manager.db.get_trades(start_date=today_str, end_date=today_str, is_sim=False, account=target_account)
        except TypeError:
            today_trades = db_manager.db.get_trades(start_date=today_str, end_date=today_str, is_sim=False)
            if target_account:
                today_trades = [t for t in today_trades if t.get('account') == target_account]
                
        sold_today = set(t['code'] for t in today_trades if "sell" in t.get('type', '').lower() or "매도" in t.get('type', ''))
        
        reentry_hurdles = {}
        # [최적화] 당일 매도 종목의 최근 매수 내역을 배치 쿼리로 일괄 조회
        _sold_latest_buys = db_manager.db.get_latest_buy_trades(sold_today, account=target_account)
        for scode in sold_today:
            last_buy = _sold_latest_buys.get(scode)
            if last_buy:
                reason = last_buy.get('reason', '')
                match = re.search(r'체결강도:\s*([0-9.]+)%', reason)
                if match:
                    reentry_hurdles[scode] = float(match.group(1))
                else:
                    reentry_hurdles[scode] = config.ANALYSIS_THRESHOLDS.get("BUY_VOL_STRENGTH", 100.0)

        # [추세추종] 손절로 잘린 종목을 그 손절가보다 **비싸게** 되사지 않는다.
        #  체결강도 허들만으로는 못 막는다 — 재진입할 때마다 그 값이 갱신되어 스스로 세운
        #  허들을 스스로 넘는다(2026-08-05 실측: 103.1% → 127.3% → 127.5%로 통과하며
        #  매 주기 손절·재매수를 반복, 왕복 스프레드만큼 실현 손실이 누적됐다).
        #  추세가 진짜로 돌아섰다면 눌림에서 다시 잡히므로, 이 게이트는 '더 비싸게 되사기'만
        #  정확히 막는다. 익절·트레일링 청산은 대상이 아니다(상승 추세의 정상 재진입까지
        #  막으면 추세추종에 역행한다).
        stop_exit_prices = self._collect_stop_exit_prices(today_trades)

        # 1. 후보 분석
        candidates = self._analyze_candidates(targets, holding_codes, rules_map, reentry_hurdles, holding_names_map, holding_groups_map, restricted_stocks=restricted_stocks, stop_exit_prices=stop_exit_prices, buy_block=buy_block)
        
        # 2. 매수 집행
        if candidates:
            if not is_market_open:
                self.log(f"[장마감] 매수 후보 감지 (주문 미전송): {len(candidates)}종목")
                for cand in candidates:
                     self.log(f"   - {cand['name']} ({cand['score']}점)")
                return

            if not can_buy:
                # [수정] '조건 미달'은 사실과 반대였다 — 이 종목들은 매수 조건을 통과한
                #  후보이고, 막은 것은 계좌 상태(슬롯·예수금)다. 무엇을 풀어야 살 수 있는지
                #  이 줄만 보고 알 수 있어야 한다.
                self.log(f"[매수스킵] 매수 조건 충족 {len(candidates)}종목 — 주문 미전송 "
                         f"({buy_skip_reason or '매수 불가 상태'})")
                for cand in candidates:
                     self.log(f"   - {cand['name']} ({cand['score']}점)")
                if buy_skip_reason.startswith("보유 슬롯"):
                    self.log("   └ 보유 종목이 청산되어 슬롯이 비면 다음 주기에 재평가됩니다.")
                return

            self._execute_buy_orders(candidates, avail_cash, invest_ratio, len(holding_codes), max_holdings)

    #  손실 청산 계열의 사유 접두어. 이 사유로 나간 종목은 같은 날 그 가격 위에서 되사지 않는다.
    #  (익절·반익절·트레일링·시간청산은 제외 — 추세가 살아 있는 상태의 청산이라 재진입이 정당하다)
    _STOP_EXIT_PREFIXES = ("손절", "본전청산")

    @staticmethod
    def _collect_stop_exit_prices(today_trades):
        """당일 손절 청산의 체결가를 종목별로 모은다. {code: 가장 최근 손절가}

        같은 날 여러 번 손절됐다면 가장 최근 값을 쓴다 — 직전 손절가가 지금 유효한 기준이다.
        """
        out = {}
        for t in sorted(today_trades or [], key=lambda x: str(x.get('time') or '')):
            type_str = str(t.get('type') or '')
            if "sell" not in type_str.lower() and "매도" not in type_str:
                continue
            reason = str(t.get('reason') or '')
            if not reason.startswith(AutoTrader._STOP_EXIT_PREFIXES):
                continue
            try:
                price = float(str(t.get('price') or '0').replace(',', ''))
            except (TypeError, ValueError):
                continue
            if price > 0:
                out[t['code']] = price   # 시간순이므로 마지막 것이 남는다
        return out

    def _analyze_candidate_worker(self, item, holding_codes, rules_map, restricted_stocks, market_regime_adj, safe_delay, reentry_hurdles, holdings_dfs, holding_groups_map, io_pool=None, stop_exit_prices=None, buy_block=None):
        """(내부함수) 매수 후보 분석용 단일 워커

        io_pool: 차트/체결강도/호가 동시 조회용 공유 스레드풀 (None이면 자체 생성 — 하위 호환)
        """
        if not self.is_running: return None # [추가] 중지 요청 시 즉시 종료
        
        try:
            # [추가] 시스템 트레이딩 스레드임을 마킹 (API 우선순위 획득용)
            context.trade_context.is_system_trading = True
            
            # [추가] API 호출 전 대기 (Rate Limit 방지 - 스레드별 분산 효과)
            time.sleep(safe_delay)
            
            if not self.is_running: return None # 대기 후 재확인
            
            code = item['code']; name = item['name']
            
            # 1. 트레이딩 제한 종목 체크
            if code in restricted_stocks:
                self.set_stock_state(code, None)
                return {'type': 'restricted_skip', 'name': name}

            # [추가] 대체거래소(NXT) 운영 시간에는 ETF 및 NXT 비거래 종목 스킵
            is_nxt_market = api.nxt_order_window()
            is_overseas_stock = not (len(code) == 6 and code[0].isdigit() and code.isalnum())
            
            if is_nxt_market and not is_overseas_stock:
                is_etf = item.get('group') == 'etfs_kr'
                if is_etf or (hasattr(api, 'is_nxt_tradeable') and not api.is_nxt_tradeable(code)):
                    self.set_stock_state(code, None)
                    return {'type': 'log_only', 'log': f"[NXT스킵] {name}({code}): 대체거래소(NXT) 거래 불가 종목(ETF 포함)으로 분석을 스킵합니다."}
            
            # 2. 진행 중인 주문 체크
            if self.order_manager.is_pending(code):
                # [관측성] 종전에는 조용히 빠졌다. 그러면 그 종목은 [분석] 줄도 [분석스킵] 줄도
                #  없이 화면에서 사라져, 왜 후보에서 빠졌는지 운영자가 알 수 없다
                #  (2026-08-05: 손절 직후 NAVER가 종목분석에서 통째로 사라졌다).
                self.set_stock_state(code, None)
                odnos = self.order_manager.pending_odnos(code)
                return {'type': 'log_only',
                        'log': f"[분석스킵] {name}({code}): 진행 중인 주문 존재 "
                               f"({', '.join(str(o) for o in odnos) or '?'}) — 매수 판정을 건너뜁니다"}

            # 3. 보유 종목 체크 (보유분석에서 다루므로 후보 분석에서는 조용히 제외한다)
            if code in holding_codes: return None
            
            # 4. 시장 지수 필터링 (종목별 적용)
            # [Fix] fail-closed: 상태 캐시가 아예 없는 경우(첫 주기 전·조회 실패)도 '판단 불가'로 보아
            #  신규 매수를 보류한다. 기존에는 캐시가 없으면 필터를 통과시켜, 시장 방향을 모르는
            #  상태에서 진입이 이뤄질 수 있었다. ('모르겠으면 아무것도 하지 마라')
            if getattr(config, 'USE_MARKET_FILTER', True):
                market_type = self._get_stock_market_type(code)
                market_stat = self.market_index_status.get(market_type)
                if not isinstance(market_stat, dict) or not market_stat.get('is_healthy', False):
                    self.set_stock_state(code, None)
                    return {'type': 'market_skip', 'name': name, 'market_type': market_type}
            
            if not self.is_running: return None # API 호출 전 최종 확인
            
            is_overseas_stock = not (len(code) == 6 and code[0].isdigit() and code.isalnum())
            
            # [최적화/#6] 호가창(order_book)은 ask_bid_ratio 수급 게이트에만 쓰인다. 이 종목의 유효
            # 임계값(BUY_ASK_BID_RATIO; 개별 룰 우선)이 0이면 게이트가 꺼져 있어 호가 조회가 무의미하므로
            # 생략한다. 토스는 체결강도 미제공으로 호가비가 유일한 수급지표이므로 항상 조회한다.
            _ab_rule = rules_map.get(code)
            _ab_thr = (_ab_rule.get('buy_ask_bid_ratio', config.ANALYSIS_THRESHOLDS.get("BUY_ASK_BID_RATIO", 1.0))
                       if _ab_rule else config.ANALYSIS_THRESHOLDS.get("BUY_ASK_BID_RATIO", 1.0))
            _need_ob = config.session.is_toss or (_ab_thr or 0) > 0

            # 5. [최적화] 차트, 체결강도, 호가(수급비율) 데이터 병렬(동시) 조회
            #  호가는 10호가 상세가 아니라 수급 게이트용 비율만 필요하므로 get_ask_bid_ratio를 쓴다.
            #  (WS 호가 총잔량이 신선하면 REST 없이 계산 → 종목당 호가 REST 1콜 절감)
            #  [최적화] 공유 io_pool 사용 시 후보마다 풀을 생성/파괴하지 않는다.
            _local_pool = None
            ex = io_pool
            if ex is None:
                _local_pool = concurrent.futures.ThreadPoolExecutor(max_workers=3, thread_name_prefix="at_cand_io")
                ex = _local_pool
            try:
                # [계좌 컨텍스트] 이 풀도 별도 스레드다 — 워커가 물려받은 컨텍스트를
                #  한 번 더 넘겨야 시세 조회가 같은 앱키로 나간다.
                _io = utils.inherit_account_context
                fut_chart = ex.submit(_io(api.get_chart_data), code, is_overseas=is_overseas_stock)
                fut_vol = ex.submit(_io(api.get_realtime_vol_strength), code) if not is_overseas_stock else None

                df = fut_chart.result()
                try: vol_strength = fut_vol.result() if fut_vol else None
                except Exception: vol_strength = None
                # [최적화] 호가는 여기서 당기지 않는다 — 점수·상태·체결강도 게이트를 모두
                #  통과한 종목에만 아래에서 조회한다(지연 조회). 판정 결과는 동일하다.
                ask_bid_ratio = None
            finally:
                if _local_pool is not None:
                    _local_pool.shutdown(wait=False)

            if df is None or df.empty:
                # [관측성] 차트가 없으면 판정 자체가 불가능하다. 조용히 빠지면 그 종목이
                #  왜 후보에서 사라졌는지 알 수 없다.
                self.set_stock_state(code, None)
                return {'type': 'log_only',
                        'log': f"[분석스킵] {name}({code}): 차트 데이터 없음 — 매수 판정 불가"}

            # [수정] 캐시된 차트 데이터의 당일 미확정 종가를 실시간 최신 현재가로 업데이트
            # (종목 분석 메뉴와 시스템 트레이딩 간의 지표 및 점수 불일치 원천 차단)
            realtime_price = 0.0
            try:
                realtime_price = api.get_current_price(code, is_overseas=is_overseas_stock)
                # 모든 장 종료 후에는 지표용 봉을 갱신하지 않는다(KRX 확정 종가 유지).
                indicators.apply_realtime_price(df, api.chart_overlay_price(realtime_price, is_overseas_stock))
            except Exception as e:      # noqa: BLE001 - 아래 price_is_realtime 이 결과를 들고 간다
                logger.debug(f"[분석] 실시간 현재가 조회 예외({code}): {e}")

            # [주문가 보호] 지표는 KRX 확정 종가로 계산하되, 매수 주문 단가는 항상 실시간가를 쓴다.
            #  이 값이 _execute_buy_orders의 cand['price'] → 주문 지정가가 되므로, NXT 시간대에
            #  KRX 종가로 굳으면 호가에서 벗어나 체결되지 않는다.
            #  [실패를 들고 간다 · 2026-09-04] get_current_price 는 실패해도 예외가 아니라 0을
            #   돌려준다. 종전에는 그 0이 조용히 '직전 확정 종가'로 폴백해 **주문 지정가·ATR
            #   손절폭·포지션 수량 세 가지가 한꺼번에** 어긋난 채 주문이 나갔다(셋 다 이 값을
            #   분모로 쓴다). 관심종목 44개 1년 실측으로 그 오차는 중앙값 2.0%·90분위 6.8%·
            #   최대 30.0%다. 분석·점수는 어차피 확정 종가 기준이므로 그대로 두고, **주문만**
            #   막는다(_execute_buy_orders). 판단 불가는 매수 허용이 아니다.
            price_is_realtime = bool(realtime_price and realtime_price > 0)
            current_price = float(realtime_price) if price_is_realtime else float(df.iloc[-1]['close'])

            # [추가] 상관계수 필터링
            correlation_skip_msg = None
            if getattr(config, 'USE_CORRELATION_FILTER', True) and holdings_dfs:
                corr_threshold = getattr(config, 'CORRELATION_THRESHOLD', 0.7)
                cand_ret = df.set_index('date')['close'].astype(float).pct_change().dropna()
                cand_group = item.get('group', 'stocks_kr') # 후보 종목의 그룹
                
                for hold_code, hold_info in holdings_dfs.items():
                    hold_group = holding_groups_map.get(hold_code, 'stocks_kr')
                    
                    # [추가] 같은 그룹(국내주식-국내주식, 국내ETF-국내ETF)끼리만 상관계수 비교
                    if cand_group != hold_group:
                        continue
                        
                    hold_name = hold_info['name']
                    # [최적화] _analyze_candidates에서 사전계산된 수익률 시리즈 재사용
                    #  (없으면 1회만 계산해 memoize — 후보×보유 조합마다 재계산 방지)
                    hold_ret = hold_info.get('ret')
                    if hold_ret is None:
                        hold_df = hold_info.get('df')
                        if hold_df is None or hold_df.empty: continue
                        hold_ret = hold_df.set_index('date')['close'].astype(float).pct_change().dropna()
                        hold_info['ret'] = hold_ret
                    if hold_ret.empty: continue

                    combined = pd.concat([cand_ret, hold_ret], axis=1, join='inner').dropna()
                    if len(combined) > 30:
                        corr = combined.iloc[:, 0].corr(combined.iloc[:, 1])
                        if corr >= corr_threshold:
                            correlation_skip_msg = f"[상관관계 보류] (보유종목 {hold_name} 상관계수: {corr:.2f} >= {corr_threshold})"
                            break

            # [추세추종] 상대강도(RS) 게이트: 소속 지수(KOSPI/KOSDAQ)보다 약한 종목의 신규 진입 차단.
            #   같은 +15%라도 지수가 +20%인 장에서는 열등주 — 지수 대비 초과수익이 없는 종목은
            #   '확실한 추세'가 아니라고 보고 게이트에서 제외한다 (약추세 진입 = 큰 손실의 원천).
            #   룩백은 RS_FILTER_LOOKBACK(>0) 우선, 0이면 스코어링 '가격 모멘텀'과 동일(MOMENTUM_LOOKBACK).
            #   종목 이력 부족·지수 조회 실패 시에는 통과(fail-open — 데이터 장애가 매수 전면 중단으로 번지지 않게).
            rs_skip_msg = None
            if getattr(config, 'USE_RS_FILTER', False) and not is_overseas_stock:
                mom_lb = getattr(config, 'RS_FILTER_LOOKBACK', 0) or config.INDICATOR_PARAMS.get('MOMENTUM_LOOKBACK', 126)
                if len(df) > mom_lb:
                    try:
                        past_close = float(df['close'].iloc[-(mom_lb + 1)])
                    except (TypeError, ValueError):
                        past_close = 0.0
                    if past_close > 0:
                        stock_mom = (current_price / past_close - 1) * 100
                        idx_mom = analysis.get_index_momentum(self._get_stock_market_type(code), lookback=mom_lb)
                        if idx_mom is not None and stock_mom <= idx_mom:
                            rs_skip_msg = f"[RS필터 보류] ({mom_lb}일 수익률 {stock_mom:+.1f}% ≤ 지수 {idx_mom:+.1f}%)"

            # 룰 및 임계값 설정
            rule = rules_map.get(code)
            market_type = self._get_stock_market_type(code)
            score_adj = market_regime_adj.get(market_type, 0.0)

            # [SSOT] 매도 경로(build_sell_thresholds)와 같은 규약으로 조립한다 —
            #  룰의 NULL 컬럼은 전역 기본값으로 되돌리고 가중치는 dict로 확정한다.
            #  개별 룰이 걸렸다는 이유로 종목이 분석 결과 없이 사라지면 안 된다.
            thresholds = _pkg().build_buy_thresholds(rule=rule, score_adj=score_adj)
            
            # 전략 실행
            result = self.strategy.analyze_buy(code, name, df, current_price, vol_strength=vol_strength, thresholds=thresholds, ask_bid_ratio=ask_bid_ratio)
            if not result:
                self.set_stock_state(code, None)
                return None

            # [추세추종] 추세품질 상한 게이트 — '너무 가파른' 추세는 사지 않는다.
            #  추세품질은 단조가 아니다. 300 위에서는 전방수익이 음수로 꺾이고 꼬리가
            #  잘린다(상위10% 56.6 → 14.2, ATR손절 20.5 → 45.5%). 종목 축의 모멘텀
            #  크래시라 '강할수록 좋다'는 직관과 반대다 — 근거는 config의
            #  ANALYSIS_THRESHOLDS['TREND_QUALITY_MAX'] 주석.
            #  이력 부족(None)은 통과시킨다(fail-open) — 데이터가 없다고 막으면 신규 상장·
            #  데이터 장애가 매수 전면 중단으로 번진다. 순위에서는 이미 최하위로 밀리므로
            #  이중으로 벌하지 않는다.
            #  [위치] 상관·RS 게이트와 같은 높이에 둔다. 매수 상태가 아닌 종목도 아래에서
            #  이 변수를 읽으므로(보류 집계 규약) 분기 안에서 만들면 NameError가 난다.
            #  전역 방어 게이트라 개별 룰로 덮지 않는다 — build_buy_thresholds를 거치지
            #  않고 config에서 직접 읽는다.
            tq_cap_skip_msg = None
            _tq_cap = float(config.ANALYSIS_THRESHOLDS.get('TREND_QUALITY_MAX', 0) or 0)
            _tq_now = result.get('trend_quality')
            if _tq_cap > 0 and _tq_now is not None and _tq_now >= _tq_cap:
                tq_cap_skip_msg = (f"[추세품질 상한] (추세품질 {_tq_now:,.0f} >= {_tq_cap:,.0f}"
                                   f" — 모멘텀 크래시 구간)")

            # [최적화] 호가(매도잔량비) 지연 조회.
            #  종전에는 **모든 후보**의 호가를 점수 계산 전에 미리 당겼다. 그런데 호가비는
            #  analyze_buy의 마지막 수급 게이트에만 쓰이고, 거기까지 도달하는 종목은 주기당
            #  0~3개다. WS 등록 한도(41건)가 현재가로 다 차서 호가 구독은 0개라, 이 조회는
            #  전부 REST로 나간다 — 관심종목 40개면 매 주기 40콜이 호가에만 소모됐다.
            #  (2026-08-05: 계정 실제 유량이 명목보다 낮아 EGW00201이 상시 발생했고, 한도를
            #   낮추는 것으로는 해결되지 않았다. 부하는 호출 수로 줄여야 한다.)
            #  판정 결과는 동일하다 — 여기서 쓰는 min_ask_bid_ratio는 analyze_buy가 이미
            #  자동보정까지 마쳐 돌려준 값이고, 원래 게이트와 같은 비교를 그대로 한다.
            #  호가 조회 실패(None)도 종전과 같이 '통과'로 다룬다(fail-open 동작 유지).
            if result['action'] == "buy" and _need_ob:
                min_abr = result.get('min_ask_bid_ratio') or 0
                if min_abr > 0:
                    try:
                        ask_bid_ratio = api.get_ask_bid_ratio(code, is_overseas_stock)
                    except Exception:
                        ask_bid_ratio = None
                    result['ask_bid_ratio'] = ask_bid_ratio
                    if ask_bid_ratio is not None and ask_bid_ratio < min_abr:
                        result['action'] = 'wait'
                        result['vol_reject_reason'] = f"매도비:{ask_bid_ratio:.2f}<{min_abr}"

            # [추가] 분석 성공 시 상태 업데이트
            self.set_stock_state(code, result['state'])
            
            # 로그 출력을 위한 문자열 구성
            rsi_val = f"{result['rsi']:.1f}" if result['rsi'] is not None else "-"
            adx_val = f"{result['adx']:.1f}" if result['adx'] is not None else "-"
            cci_val = f"{result['cci']:.1f}" if result['cci'] is not None else "-"
            sar_val = result.get('psar')
            if sar_val is not None:
                sar_str = "상승" if current_price > sar_val else "하락"
            else:
                sar_str = "-"
            macd_val = result.get('macd'); sig_val = result.get('macd_signal')
            macd_str = "골든" if macd_val is not None and sig_val is not None and macd_val > sig_val else "데드"
            obv_trend = result.get('obv_trend')
            obv_str = "상승" if obv_trend is True else ("하락" if obv_trend is False else "-")
            sm_str = "O" if result.get('smart_money') else "X"
            vol_val = f"{result['vol_strength']:.1f}%" if result.get('vol_strength') else "-"
            rule_msg = " [개별 룰 적용]" if rule else ""
            
            is_buy_state = result['state'] in ["매수", "강매수", "역매수"]

            # [신호 원장] 판정 결과를 로그 문자열이 아니라 **판정한 자리에서** 넘긴다.
            #  종전에는 감사가 [분석] 줄을 정규식으로 되읽었는데, `[매도비:3.92]`(정보 표기)와
            #  `매도비:3.92<1.0`(차단)이 한 글자 차이라 실제로 한 번 뒤집어 읽은 적이 있다
            #  (차단율 1.3% → 75%). 여기서 넘기면 그 위험이 원천적으로 없다.
            #  매수 상태였던 주기만 남긴다 — 신호가 아니었던 것까지 세면 차단율의 분모가
            #  부풀어 기회비용을 과대평가한다.
            def _ledger(outcome):
                if not is_buy_state:
                    return None
                row = {'code': code, 'name': name, 'outcome': outcome,
                       'score': result.get('score'), 'state': result['state'],
                       'vol': result.get('vol_strength'),
                       'abr': result.get('ask_bid_ratio')}
                # 계좌 상태(슬롯 만석·예수금 부족)는 게이트를 통과한 신호에만 의미가 있다.
                #  게이트가 이미 막은 종목까지 슬롯 탓으로 세면 기회비용이 부풀어,
                #  '슬롯을 늘리면 이만큼 더 샀을 것'이라는 틀린 결론을 부른다.
                if outcome == 'passed' and buy_block:
                    row['blocked_by'] = buy_block
                return row

            # [추가] 가짜 체결강도로 걸러진 경우 사유 표시 (매수 시그널일 때만)
            vol_reject_msg = ""
            if is_buy_state:
                if result.get('vol_reject_reason'):
                    vol_reject_msg = f" [{result['vol_reject_reason']}]"
                elif result.get('ask_bid_ratio') is not None:
                    vol_reject_msg = f" [매도비:{result['ask_bid_ratio']:.2f}]"
            
            log_msg = f"[분석] {name}({code}): 현재가={current_price:,.0f}, 점수={result['score']}, 상태={result['state']}, RSI={rsi_val}, ADX={adx_val}, CCI={cci_val}, OBV={obv_str}, SM={sm_str}, SAR={sar_str}, 체결={vol_val}{rule_msg}{vol_reject_msg}"
            
            if result['action'] == "buy":
                reentry_msg = ""

                # [추세추종] 당일 손절로 잘린 종목을 그 손절가 이상에서 되사지 않는다.
                #  체결강도 허들은 재진입마다 갱신되어 스스로 넘어가므로 이 경로를 못 막는다
                #  (2026-08-05 실측: 손절 → 10초 뒤 1,000원 비싸게 재매수를 매 주기 반복,
                #   왕복 스프레드만큼 실현 손실만 쌓였다). 추세가 진짜로 살아 있다면 눌림에서
                #  다시 잡히므로, '판 값보다 비싸게 되사기'만 정확히 막는다.
                stop_px = (stop_exit_prices or {}).get(code)
                if (stop_px and current_price >= stop_px
                        and getattr(config, 'REENTRY_BLOCK_ABOVE_STOP_PRICE', True)):
                    return {'type': 'log_only', 'ledger': _ledger('reentry'),
                            'log': f"[분석스킵] {name}({code}): 당일 손절가 재진입 불가 "
                                   f"(현재가 {current_price:,.0f} >= 손절가 {stop_px:,.0f}) "
                                   f"— 더 비싸게 되사지 않습니다"}

                if code in reentry_hurdles:
                    req_vol = reentry_hurdles[code]
                    vol_strength_val = result.get('vol_strength')
                    if config.session.is_toss:
                        # [추가] 토스: 체결강도 미제공 → 매도잔량비(ask_bid_ratio)로 당일 재진입 판단
                        # [알려진 비대칭 — 무동작에 가깝다] 여기 쓰는 min_abr은 analyze_buy가
                        #  일반 매수 게이트에서 이미 적용한 값과 같다. 즉 이 자리에 온 후보는
                        #  그 검사를 통과한 뒤라, 실제로 걸리는 경우는 '호가를 못 구했다(None)'
                        #  하나뿐이다. KIS의 '직전 진입 체결강도를 경신해야 한다'는 자기 갱신
                        #  허들에 해당하는 장치가 토스에는 없고, 당일 재진입 방어는 위의
                        #  손절가 게이트만 남는다.
                        #  고치려면 새 진입 필터를 넣는 일이 되므로 같은 차단율 무작위 대조가
                        #  먼저다(config ANALYSIS_THRESHOLDS 주석의 진입 필터 채택 규칙).
                        #  현재 동작은 tests/test_reentry_hurdle.py 가 못 박고 있다.
                        abr = result.get('ask_bid_ratio')
                        min_abr = result.get('min_ask_bid_ratio', 0) or 0
                        if abr is None or (min_abr > 0 and abr < min_abr):
                            log_msg = f"[분석스킵] {name}({code}): 당일 재진입 불가 (매도비 {abr if abr is not None else 0:.2f} < {min_abr:.2f})"
                            return {'type': 'log_only', 'log': log_msg, 'ledger': _ledger('reentry')}
                        else:
                            reentry_msg = f"당일 재진입(매도비 {abr:.2f})"
                    elif vol_strength_val is None or vol_strength_val <= req_vol:
                        log_msg = f"[분석스킵] {name}({code}): 당일 재진입 불가 (체결강도 {vol_strength_val if vol_strength_val else 0:.1f}% <= 기존매수 {req_vol:.1f}%)"
                        return {'type': 'log_only', 'log': log_msg, 'ledger': _ledger('reentry')}
                    else:
                        reentry_msg = f"당일 재진입(기존 {req_vol:.1f}% 경신)"

                candidate_data = {
                    'code': code, 'name': name, 'price': current_price,
                    'price_is_realtime': price_is_realtime,
                    'score': result['score'], 'rsi': result['rsi'], 'adx': result['adx'], 'cci': result['cci'], 'atr': result.get('atr', 0), 'vol_strength': result.get('vol_strength'),
                    'w52_pos': result.get('w52_pos', 0.0),  # [추세추종] 52주 위치 (우선순위 정렬용)
                    'trend_quality': result.get('trend_quality'),  # [추세추종] 추세 품질(회귀 모멘텀, 랭킹 1순위 키)
                    'ask_bid_ratio': result.get('ask_bid_ratio'),  # [추가] 토스 수급 지표(체결강도 대체)
                    'is_custom_rule': bool(rule), 'rule': rule, 'state': result['state'],
                    'state_reason': result.get('state_reason', ''),
                    'reentry_msg': reentry_msg
                }
                reentry_log = f" [{reentry_msg}]" if reentry_msg else ""
                stale_mark = "" if price_is_realtime else "(직전종가·실시간 조회 실패)"
                log_msg = f"[분석] {name}({code}): 현재가={current_price:,.0f}{stale_mark}, 점수={result['score']}, 상태={result['state']}, RSI={rsi_val}, ADX={adx_val}, CCI={cci_val}, OBV={obv_str}, SM={sm_str}, SAR={sar_str}, 체결={vol_val}{rule_msg}{vol_reject_msg}{reentry_log}"
                
                if correlation_skip_msg:
                    log_msg += f" {correlation_skip_msg}"
                    return {'type': 'correlation_skip', 'name': name, 'log': log_msg,
                            'ledger': _ledger('corr')}
                elif rs_skip_msg:
                    log_msg += f" {rs_skip_msg}"
                    return {'type': 'rs_skip', 'name': name, 'log': log_msg,
                            'ledger': _ledger('rs')}
                elif tq_cap_skip_msg:
                    log_msg += f" {tq_cap_skip_msg}"
                    return {'type': 'tq_cap_skip', 'name': name, 'log': log_msg,
                            'ledger': _ledger('tq')}

                return {'type': 'candidate', 'data': candidate_data, 'log': log_msg,
                        'ledger': _ledger('passed')}
            else:
                # [보류 집계의 의미] '보류'는 **살 수 있었는데 게이트가 막았다**는 뜻이어야 한다.
                #  애초에 매수 상태가 아니었던 종목까지 보류로 세면, 주기 말미 요약
                #  ("유사 테마로 매수 보류 N종목")이 실제 기회비용을 부풀린다. 운용자가
                #  그 숫자를 보고 상관 임계값이나 RS 설정을 조정하면 틀린 근거로 판단하게 된다.
                #  종전에는 사유 문구만 is_buy_state로 걸러내고 반환 타입은 그대로 두어,
                #  집계에는 들어가는데 로그엔 이유가 없는 상태였다.
                if correlation_skip_msg and is_buy_state:
                    log_msg += f" {correlation_skip_msg}"
                    return {'type': 'correlation_skip', 'name': name, 'log': log_msg,
                            'ledger': _ledger('corr')}
                elif rs_skip_msg and is_buy_state:
                    log_msg += f" {rs_skip_msg}"
                    return {'type': 'rs_skip', 'name': name, 'log': log_msg,
                            'ledger': _ledger('rs')}
                elif tq_cap_skip_msg and is_buy_state:
                    log_msg += f" {tq_cap_skip_msg}"
                    return {'type': 'tq_cap_skip', 'name': name, 'log': log_msg,
                            'ledger': _ledger('tq')}

                # [게이트 분류] action이 buy가 아닌데 상태는 매수인 경우 = 수급 게이트가 막았다.
                #  사유 문자열은 engine.analyze_buy가 만든 것을 그대로 분기 판단에만 쓴다
                #  (로그를 되읽는 것이 아니라 같은 프로세스의 구조화된 값이다).
                _reason = (result.get('vol_reject_reason') or "") if is_buy_state else ""
                if _reason.startswith("체결:"):
                    _out = 'gate_vol'
                elif _reason.startswith("매도비:"):
                    _out = 'gate_abr'
                elif _reason:
                    _out = 'gate_hold'          # "체결강도 미확인(보류)"
                else:
                    _out = 'other'
                return {'type': 'log_only', 'log': log_msg, 'ledger': _ledger(_out)}
        except Exception as e:
            # [관측성] 종전에는 `except Exception: return None` 이었다. 후보 하나가 던지면
            #  그 종목은 아무 흔적 없이 사라졌고 — 로그도, 신호 원장 행도 남지 않았다.
            #  원장이 비면 게이트 차단율의 분모가 조용히 줄어 tools/audit_gate_forward.py 의
            #  표본이 편향된다. 바깥 as_completed 쪽에 같은 목적의 가드가 있지만, 여기서
            #  먼저 삼켜 버리므로 그 가드는 닿지 않는다. 매도측(_sell_worker_guarded)과
            #  같은 형태로 남긴다.
            self.log(f"[분석실패] {item.get('name') or item.get('code')}"
                     f"({item.get('code')}): 매수 판정 중 오류 — {type(e).__name__}: {e}")
            logger.exception(f"[매수분석] {item.get('code')} 판정 실패")
            return None

    def _analyze_candidates(self, targets, holding_codes, rules_map, reentry_hurdles, holding_names_map, holding_groups_map, restricted_stocks=None, stop_exit_prices=None, buy_block=None):
        candidates = []
        skipped_stocks = []
        ledger_rows = []               # [신호 원장] 이 주기의 매수 신호 판정 (주기 끝에 1회 기록)
        restricted_skipped_stocks = [] # [추가] 트레이딩 제한 스킵 리스트
        correlation_skipped_stocks = [] # [추가] 상관관계 스킵 리스트
        rs_skipped_stocks = [] # [추세추종] 상대강도(RS) 필터 스킵 리스트
        tq_cap_skipped_stocks = [] # [추세추종] 추세품질 상한(모멘텀 크래시) 스킵 리스트

        # [추가] 트레이딩 제한 종목 로드 (현재 시스템 트레이딩 계좌 기준으로 필터링)
        #  ([최적화] 루프에서 주기당 1회 로드해 전달받으면 파일 재조회 생략)
        if restricted_stocks is None:
            _trade_cano, _trade_acnt = _get_trade_account()
            restricted_stocks = get_restricted_stocks(_trade_cano, _trade_acnt)
        
        # [추가] 시장 국면 판단 (적응형 임계값용)
        market_regime_adj = {} # Market Type -> Score Adj
        if config.MARKET_REGIME_PARAMS.get("USE_ADAPTIVE_THRESHOLD", True):
            for m_type in ["KOSPI", "KOSDAQ"]:
                regime, adj = analysis.get_market_regime(m_type)
                market_regime_adj[m_type] = adj
                if self.consecutive_errors == 0:
                    # [추가] 시장 필터링 상태 로그
                    filter_status_str = ""
                    if getattr(config, 'USE_MARKET_FILTER', True):
                        market_stat = self.market_index_status.get(m_type)
                        if market_stat and isinstance(market_stat, dict):
                            is_healthy = market_stat.get('is_healthy', True)
                            filter_status_str = "허용" if is_healthy else "보류"
                            filter_status_str = f" | 필터링: {filter_status_str}"
                    self.log(f"[{m_type}] 시장 국면: {regime} (매수기준 {adj:+.1f}점){filter_status_str}")

        # [추가] 보유 종목의 차트 데이터 수집 (상관계수 분석용)
        holdings_dfs = {}
        use_corr_filter = getattr(config, 'USE_CORRELATION_FILTER', True)
        if use_corr_filter and holding_codes:
            for code in holding_codes:
                is_overseas = not (len(code) == 6 and code[0].isdigit() and code.isalnum())
                df = api.get_chart_data(code, is_overseas)
                if df is not None and not df.empty:
                    name = holding_names_map.get(code, code)
                    # [최적화] 수익률 시리즈를 주기당 1회만 계산 (워커에서 후보×보유 조합마다 재계산 방지)
                    try:
                        ret = df.set_index('date')['close'].astype(float).pct_change().dropna()
                    except Exception:
                        ret = None
                    holdings_dfs[code] = {'name': name, 'df': df, 'ret': ret}

        # [최적화] 분석 대상 종목 실시간 데이터 일괄 수집 (Micro-Cache 사전 예열)
        codes_to_prefetch = []
        for item in targets:
            code = item['code']
            if code in restricted_stocks or code in holding_codes or self.order_manager.is_pending(code):
                continue
            
            codes_to_prefetch.append(code)
            
        if codes_to_prefetch:
            # [수정] 시장 구분(_get_stock_market_type)에 필요한 현재가 정보를 먼저 일괄 prefetch 합니다.
            # 이렇게 하면 _analyze_candidate_worker 내부에서 개별 API 호출을 방지할 수 있습니다.
            api.prefetch_multiple_current_prices(codes_to_prefetch, is_overseas=False, include_investor=False, prefer_ws=True)

        # [수정] 일괄 예열 캐시를 활용하므로 워커별 딜레이를 대폭 단축 (Rate Limit 안전장치 유지)
        tps = config.REAL_TX_PER_SECOND
        safe_delay = (1.0 / tps) * 0.1

        # [병렬 처리] 사용자 작업과의 충돌 및 모의투자 API 제한(2 TPS) 고려
        # (실전: 5개, 모의: 2개 - ThrottledSession이 병목 없이 안전하게 제어함)
        max_workers = 5

        # [최적화] 워커 내부의 차트/체결강도/호가 동시 조회용 I/O 풀을 공유
        #  (기존에는 후보 종목마다 ThreadPoolExecutor(3)를 생성/파괴 — 저사양 환경에서 오버헤드)
        io_pool = concurrent.futures.ThreadPoolExecutor(max_workers=max_workers * 3, thread_name_prefix="cand_io")
        # [계좌 컨텍스트] trade_context 는 threading.local 이라 워커로 상속되지 않는다.
        #  감싸지 않으면 워커의 시세 조회가 자동 계좌가 아니라 **수동 계좌 앱키**로 나간다
        #  (core.utils.get_common_headers). 매도 워커는 이미 이렇게 감싸고 있었는데
        #  매수측만 빠져 있었다 — 워커가 is_system_trading 만 손으로 세우고 있던 것이
        #  그 흔적이다. 반드시 제출 스레드에서 만든다.
        _cand_task = utils.inherit_account_context(self._analyze_candidate_worker)
        try:
            with concurrent.futures.ThreadPoolExecutor(max_workers=max_workers, thread_name_prefix="at_cand") as executor:
                futures = [executor.submit(_cand_task, item, holding_codes, rules_map, restricted_stocks, market_regime_adj, safe_delay, reentry_hurdles, holdings_dfs, holding_groups_map, io_pool=io_pool, stop_exit_prices=stop_exit_prices, buy_block=buy_block) for item in targets]

                for future in concurrent.futures.as_completed(futures):
                    if not self.is_running: break
                    # [관측성] 한 종목의 예외가 나머지 후보 분석을 통째로 중단시키면 안 된다.
                    #  종전에는 result()의 예외가 그대로 올라와 남은 종목이 조용히 분석되지
                    #  않았다. 실패한 종목만 로그로 남기고 나머지는 계속 본다.
                    try:
                        res = future.result()
                    except Exception as e:
                        self.log(f"[분석실패] 매수 후보 판정 중 오류 — {type(e).__name__}: {e}")
                        logger.exception("[매수분석] 후보 판정 실패")
                        continue
                    if res:
                        if res.get('ledger'):
                            ledger_rows.append(res['ledger'])
                        if res['type'] == 'candidate':
                            self.log(res['log'])
                            candidates.append(res['data'])
                        elif res['type'] == 'log_only':
                            self.log(res['log'])
                        elif res['type'] == 'restricted_skip':
                            restricted_skipped_stocks.append(res['name'])
                        elif res['type'] == 'market_skip':
                            m_type = res.get('market_type', 'KOSPI')
                            if m_type in self.skipped_by_market_filter_count:
                                self.skipped_by_market_filter_count[m_type] += 1
                            skipped_stocks.append(res['name'])
                        elif res['type'] == 'correlation_skip':
                            self.log(res['log'])
                            correlation_skipped_stocks.append(res['name'])
                        elif res['type'] == 'rs_skip':
                            self.log(res['log'])
                            rs_skipped_stocks.append(res['name'])
                        elif res['type'] == 'tq_cap_skip':
                            self.log(res['log'])
                            tq_cap_skipped_stocks.append(res['name'])
        finally:
            io_pool.shutdown(wait=False)

        # [신호 원장] 주기당 쓰기 1회. 실패해도 매매는 그대로 간다 — 계측이 매매를 막지 않는다.
        if ledger_rows:
            try:
                db_manager.db.record_signal_ledger(
                    datetime.now().strftime("%Y%m%d"), ledger_rows)
            except Exception as e:
                logger.warning(f"[Ledger] 신호 원장 기록 생략: {e}")

        # [추가] 트레이딩 제한 종목 스킵 로그 기록
        if restricted_skipped_stocks:
            self.log(f"[매수 스킵] 트레이딩 제한 종목 ({len(restricted_skipped_stocks)}개): {', '.join(restricted_skipped_stocks)}")

        # [추가] 시장 필터링 보류 종목 로그 기록
        if skipped_stocks:
            # [Fix] '약세라서 보류'와 '지수 판단 불가라서 보류'는 원인이 다르므로 로그에서 구분한다.
            _unknown_markets = [m for m, s in self.market_index_status.items()
                                if isinstance(s, dict) and s.get('unknown')]
            _cause = (f"지수 판단 불가({', '.join(_unknown_markets)}) 매수 보류"
                      if _unknown_markets else "하락장 매수 보류")
            self.log(f"[시장 필터링] {_cause} ({len(skipped_stocks)}종목): {', '.join(skipped_stocks)}")

        # [추가] 상관관계 보류 종목 로그 기록
        if correlation_skipped_stocks:
            self.log(f"[상관관계 보류] 보유 종목과 유사 테마로 매수 보류 ({len(correlation_skipped_stocks)}종목): {', '.join(correlation_skipped_stocks)}")

        # [추세추종] 상대강도(RS) 필터 보류 종목 로그 기록
        if rs_skipped_stocks:
            self.log(f"[RS필터 보류] 지수 대비 약세로 매수 제외 ({len(rs_skipped_stocks)}종목): {', '.join(rs_skipped_stocks)}")

        # [추세추종] 추세품질 상한 보류 종목 로그 기록 — 이 줄이 주기마다 여러 건 찍히면
        #  유니버스가 과열된 것이다(config TREND_QUALITY_MAX 주석의 '되돌릴 조건').
        if tq_cap_skipped_stocks:
            _cap = config.ANALYSIS_THRESHOLDS.get('TREND_QUALITY_MAX', 0)
            self.log(f"[추세품질 상한] 과열 추세(추세품질 {_cap:,.0f} 이상)로 매수 제외 "
                     f"({len(tq_cap_skipped_stocks)}종목): {', '.join(tq_cap_skipped_stocks)}")

        # [추세추종] 우선순위 정렬 — 추세 품질(회귀 모멘텀) 1순위 (근거는 candidate_priority_key docstring)
        candidates.sort(key=candidate_priority_key)

        # [추가] 선정된 후보군 우선순위 로그 출력
        if candidates:
            # [문구] 로그는 한 번 읽고 이해돼야 하고, 무엇보다 실제 정렬 순서와 같아야 한다.
            #  2026-08-12 실증으로 1순위가 추세품질에서 점수로 바뀌었다(candidate_priority_key
            #  docstring 참조) — 순서를 옛 문구로 남겨두면 로그를 믿고 원인을 엉뚱한 데서 찾게 된다.
            #  산식(연환산 기울기 × R²)과 밴드는 도움말 '색상 조건' 표 맨 아래로 옮겼다 —
            #  매 주기 찍히는 로그에 같은 설명을 되풀이할 이유가 없다.
            self.log(f"[매수 후보 선정] 총 {len(candidates)}종목 (우선순위순) "
                     f"— 점수가 1순위이고, 점수가 같으면 추세품질이 높을수록 검증된 추세며, "
                     f"매수 여부는 가르지 않고(게이트 아님) 순위만 정한다 "
                     f"— 그마저 같으면 52주위치 → 체결강도 순으로 가른다.")
            for i, c in enumerate(candidates):
                tq = c.get('trend_quality')
                tq_disp = f"{tq:.0f} ({indicators.describe_trend_quality(tq)})" if tq is not None else "- (이력부족)"
                w52_disp = f"{c['w52_pos']:.0f}%" if c.get('w52_pos') else "-"
                vol_disp = f"{c['vol_strength']:.1f}%" if c.get('vol_strength') else "-"
                # 표기 순서도 정렬 순서와 맞춘다 — 점수가 앞이고 추세품질이 그 동점을 가른다.
                self.log(f"   {i+1}순위: {c['name']} (점수:{c['score']}, 추세품질:{tq_disp}, 52주위치:{w52_disp}, 체결:{vol_disp})")
        
        return candidates


    def _execute_buy_orders(self, candidates, avail_cash, invest_ratio, current_holdings_count, max_holdings):
        for cand in candidates:
            if not self.is_running: break

            # [판단 불가는 매수 허용이 아니다] 실시간 현재가를 못 받은 후보는 주문하지 않는다.
            #  아래에서 이 값 하나로 주문 지정가·ATR 손절폭·포지션 수량이 전부 정해진다.
            #  직전 확정 종가로 대신하면 셋이 같은 방향으로 함께 틀어진다(1년 실측 중앙값 2.0%·
            #  90분위 6.8%·최대 30.0%). 못 사는 종목이 생기는 건 다음 주기에 회복된다.
            if not cand.get('price_is_realtime', True):
                self.log(f"매수 보류: {cand.get('name', '')}({cand.get('code', '')}) - "
                         f"실시간 현재가 조회 실패. 직전 확정 종가로는 주문가·손절폭·수량이 "
                         f"함께 어긋나므로 진입하지 않습니다(다음 주기 재평가).")
                continue

            # [수정] 최소 주문 가능 금액 하향 조정
            if avail_cash < 1000:
                self.log(f"매수 중단: 잔여 예수금 부족 ({avail_cash:,}원)")
                break
            
            # [추가] 최대 보유 종목 수 도달 시 추가 매수 중단
            if current_holdings_count >= max_holdings:
                self.log(f"매수 중단: 최대 보유 종목 수({max_holdings}개) 도달")
                break

            # [추가] 손절률, ATR 여부 및 투자 비중 확인 (개별 룰 or 전역 설정)
            sl_rate = config.SELL_STRATEGY["STOP_LOSS_RATE"]
            use_atr_stop = config.SELL_STRATEGY.get("USE_ATR_STOP", True)
            atr_mult = config.SELL_STRATEGY.get("ATR_STOP_MULTIPLIER", 2.0)
            cand_invest_ratio = invest_ratio

            if cand.get('rule'):
                rule = cand['rule']
                sl_rate = rule.get('stop_loss', sl_rate)
                if rule.get('use_atr_stop') is not None:
                    use_atr_stop = bool(rule['use_atr_stop'])
                if rule.get('atr_stop_multiplier') is not None:
                    atr_mult = rule['atr_stop_multiplier']
                # [수정] 개별 룰의 비중이 0/None이면 '자동' → 전역(또는 슬롯 균등 분할)을 따른다.
                #   종전에는 룰 저장 시점의 값이 박제돼 슬롯 수를 바꿔도 그 종목만 옛 비중으로
                #   남아 명목합이 조용히 100%를 넘었다.
                cand_invest_ratio = config.resolve_invest_ratio(rule.get('invest_ratio'))

            atr_val = cand.get('atr', 0)
            price_val = cand.get('price', 0)
            atr_sl_rate = None # DB 저장용
            
            if use_atr_stop and atr_val > 0 and price_val > 0:
                # [SSOT 2026-08-09] 산식·캡은 engine.atr_stop_rate 가 단독 보유한다.
                #  종전에는 이 자리와 피라미딩·백테스트가 각자 같은 식을 복제해 6벌이 있었다.
                #  캡(MAX_ATR_STOP_LOSS_RATE)을 조정하면 실매매와 백테스트가 갈라질 수 있는
                #  구조였고, 그 캡의 타당성을 백테스트로 검증하려던 참이라 먼저 통합한다.
                uncapped = -((atr_val * atr_mult / price_val) * 100)
                sl_rate = _pkg().atr_stop_rate(atr_val, price_val, atr_mult=atr_mult)
                if sl_rate is not None and sl_rate > uncapped:
                    self.log(f"[리스크 조정] ATR 손절률({uncapped:.1f}%)이 최대 한도({sl_rate:.1f}%)를 초과하여 조정됩니다.")

            # [추세추종 안전장치] "탈출 전략이 없다면 포지션을 잡지 마라"
            #  ATR 손절이 꺼져 있고(또는 ATR 미확보) 고정 손절도 0(미사용)이면 이 매수는
            #  청산 기준이 없는 포지션이 된다. 게다가 allocate_budget은 손절폭이 0이면
            #  리스크 캡을 건너뛰어 '손실액 상한'까지 함께 사라진다(집중 캡만 남음).
            #  기본 설정에서는 도달하지 않고(ATR 손절 ON·고정 -7%), 사용자가 전역이나
            #  개별 룰에서 둘 다 끈 경우에만 걸린다 → 매수를 진행하지 않고 건너뛴다.
            if not sl_rate or abs(sl_rate) <= 0:
                self.log(f"[매수 보류] {cand.get('name', '')}({cand.get('code', '')}) 손절 기준 없음 — "
                         f"ATR 손절 {'ON(ATR 미확보)' if use_atr_stop else 'OFF'} + 고정 손절 0(미사용). "
                         f"청산 기준과 손실액 상한이 모두 사라지므로 진입하지 않습니다. "
                         f"(설정 > 매도 전략에서 ATR 손절을 켜거나 고정 손절률을 지정하세요)")
                continue

            # [수정] 자산 배분 로직 개선: 마지막 슬롯인 경우 남은 예수금 전액 투자
            remaining_slots = max_holdings - current_holdings_count
            
            # 1. 예산 할당 계산 (변동성 타겟팅 및 리스크 관리 적용)
            calc_amt = self.risk_manager.allocate_budget(
                avail_cash, cand_invest_ratio, stop_loss_rate=sl_rate,
                atr=cand.get('atr'), current_price=cand.get('price'),
                market_type=self._get_stock_market_type(cand['code']))
            
            if remaining_slots == 1:
                # 마지막 종목일 때: 변동성 타겟팅/리스크 관리가 꺼져있다면 잔여 예수금 전액 사용, 켜져 있다면 계산된 금액 준수
                if not getattr(config, 'USE_VOLATILITY_TARGETING', True) and getattr(config, 'SYSTEM_RISK_PER_TRADE', 4.0) <= 0:
                    invest_amt = avail_cash
                else:
                    invest_amt = calc_amt
            else:
                invest_amt = calc_amt

            # [수정] 지정가 주문을 위해 현재가(정수) 확보
            current_price = int(cand['price'])

            # [수정] 슬리피지 비율 적용 및 호가 정렬 (체결 확률 증대)
            raw_order_price = current_price * (1 + config.SLIPPAGE_RATE)
            order_price = int(utils.adjust_to_tick(
                raw_order_price, is_overseas=False,
                is_etf=api.is_domestic_etf_etn(cand['code'], cand.get('name'))))
            # [안전장치] 상한가에 락되면 '현재가 + 슬리피지'가 제한폭을 넘어 거부된다.
            order_price = int(self._clamp_order_price(cand['code'], order_price))

            # [사이징 상한] 배분액이 1주 값에 못 미칠 때의 처리.
            #  종전에는 무조건 배분액을 1주 값까지 끌어올렸다(가용 예수금 전체를 쓰는 버그
            #  방지). 그러나 그러면 기초비중·리스크 한도·변동성 타겟팅이 min 결합으로 합의한
            #  상한이 1주 값 하나에 덮어써진다. 큰 계좌에서는 발동하지 않지만 시드 500만에서는
            #  관심목록의 17%(고가주)가 이 경로를 타 목표의 3배까지 집행됐다.
            #  → 초과 배수가 MAX_POSITION_OVERSHOOT 이내일 때만 1주를 허용하고, 넘으면
            #    진입하지 않는다. 못 사는 종목이 생기는 건 시드의 한계이지 고쳐야 할 버그가
            #    아니다 — 의도한 비중으로 담을 수 없는 종목을 억지로 담는 쪽이 위험하다.
            if invest_amt < order_price:
                overshoot_cap = float(getattr(config, 'MAX_POSITION_OVERSHOOT', 1.3) or 1.0)
                if invest_amt <= 0 or order_price > invest_amt * overshoot_cap:
                    ratio = f"{order_price / invest_amt:.1f}배" if invest_amt > 0 else "배분액 0원"
                    self.log(f"매수 보류: {cand.get('name', '')}({cand.get('code', '')}) - "
                             f"1주 {order_price:,}원이 배분액 {int(invest_amt):,}원의 {ratio} "
                             f"(상한 {overshoot_cap:.1f}배). 의도한 비중으로 담을 수 없는 종목입니다.")
                    continue
                invest_amt = order_price

            # [추가] 포트폴리오 히트 캡: 보유 전체 오픈 리스크 + 신규 리스크가 한도를 넘으면 축소/보류.
            #  종목당 한도(SYSTEM_RISK_PER_TRADE)와 별개로 '동시 다발 손절' 합산 손실을 통제한다.
            #  손절률이 없는(>=0) 경우 리스크 추정이 불가하므로 게이트를 건너뛴다(allocate_budget과 동일 기조).
            if sl_rate and sl_rate < 0:
                budget_left = self.risk_manager.portfolio_risk_budget_left(avail_cash=avail_cash)
                if budget_left is not None:
                    cap = self.risk_manager.effective_portfolio_cap()
                    new_risk = invest_amt * (abs(sl_rate) / 100.0)
                    if new_risk > budget_left:
                        allowed_amt = int(max(budget_left, 0) / (abs(sl_rate) / 100.0))
                        if allowed_amt < order_price:
                            # 한도에 '닿아서' 막는 것과 한도를 '계산 못 해서' 막는 것은 다르다.
                            #  같은 문구로 찍으면 데이터 결손을 정상 동작으로 읽게 된다.
                            if getattr(self, 'portfolio_heat_unknown', False):
                                why = "오픈 리스크 산출 실패 — 한도를 계산할 수 없어 보류한다"
                            elif not (getattr(self, 'current_total_asset', 0) or self.initial_asset):
                                why = "기준자산 미확보 — 한도를 계산할 수 없어 보류한다"
                            else:
                                why = (f"포트폴리오 총 리스크 한도({cap:.1f}%) 도달 "
                                       f"(현재 오픈 리스크 {self.portfolio_heat_amt:,.0f}원, "
                                       f"남은 예산 {max(budget_left, 0):,.0f}원)")
                            self.log(f"매수 보류: {cand['name']} - {why}")
                            continue
                        self.log(f"[히트 캡] {cand['name']} 투자금 축소: {invest_amt:,}원 → {allowed_amt:,}원 "
                                 f"(총 오픈 리스크 한도 {cap:.1f}% 준수)")
                        invest_amt = allowed_amt

            # [수정] 단순 계산 대신 API를 통해 정확한 매수 가능 수량 조회
            # 지정가 주문 시 해당 가격 기준으로 조회
            max_qty = api.fetch_buyable_quantity(cand['code'], order_price)
            
            # [추가] API가 0을 반환할 경우 로컬 예수금 기반 Fallback 계산
            if max_qty <= 0 and avail_cash > 0:
                max_qty = int((avail_cash * 0.998) / order_price)
            
            # 자산 배분 비중 적용 수량
            target_qty = int(invest_amt / order_price)
            
            # 실제 주문 수량은 (목표 수량)과 (API 조회 가능 수량) 중 작은 값
            qty = min(target_qty, max_qty)
            
            # [개선] 예수금(로컬) 부족 시 수량 자동 조정
            if avail_cash < (qty * order_price):
                qty = int(avail_cash / order_price)
            
            # [추가] 예수금 부족 로그
            if qty < 1:
                self.log(f"매수 실패: {cand['name']} - 매수 가능 수량 부족 (목표:{target_qty}, 가능:{max_qty}) | 예수금:{avail_cash:,}원, 필요:{order_price:,}원(1주)")
                continue
            
            rsi_val = f"{cand['rsi']:.1f}" if cand['rsi'] is not None else "-"
            adx_val = f"{cand['adx']:.1f}" if cand['adx'] is not None else "-"
            cci_val = f"{cand['cci']:.1f}" if cand.get('cci') is not None else "-"
            vol_val = f"{cand['vol_strength']:.1f}%" if cand.get('vol_strength') is not None else "-"
            
            # [수정] 매수 사유 포맷 분기 (일반/역매수)
            is_mr_buy = cand.get('state') == "역매수"
            state_reason = cand.get('state_reason', '')
            is_super = "슈퍼 모멘텀" in state_reason
            
            reason = "역매수 반등" if is_mr_buy else "조건 만족"
            if is_super:
                reason += "(슈퍼모멘텀)"
                
            if cand.get('reentry_msg'):
                reason += f" [{cand['reentry_msg']}]"
                
            if cand.get('is_custom_rule'):
                reason += " [개별 룰 적용]"
            
            # [수정] 토스는 체결강도 미제공 → 매도잔량비로 표기(재진입 허들 파싱과 무관)
            if config.session.is_toss:
                abr = cand.get('ask_bid_ratio')
                abr_val = f"{abr:.2f}" if abr is not None else "-"
                reason += f" [점수:{cand['score']}, RSI:{rsi_val}, 매도비:{abr_val}]"
            else:
                reason += f" [점수:{cand['score']}, RSI:{rsi_val}, 체결강도:{vol_val}]"
            
            atr_msg = ""
            if atr_val > 0 and price_val > 0:
                annual_vol = (atr_val / price_val) * math.sqrt(252) * 100
                atr_msg += f"[ATR:{int(atr_val):,}/변동성:{annual_vol:.1f}%]"
            
            # [ATR손절:-7%] 는 걷어냈다 — 바로 아래 청산선이 같은 값을 **가격까지 붙여**
            #  적는다. 두 번 적으면 한쪽만 고쳐질 때 기록끼리 어긋난다.
            if atr_msg:
                reason += f" {atr_msg}"

            # [기록] 진입 시점의 청산선을 함께 남긴다 — %만으로는 나중에 그 선이 어디였는지
            #  역산해야 하고, 청산이 끝난 종목은 화면 어디에도 그 값이 남지 않는다.
            #  TS 는 아직 무장 전이라 '언제 켜지고(발동가) 그때 어디서 잘리나'를 계산해 둔다.
            levels = _pkg().format_exit_levels(
                order_price, sl_rate=sl_rate,
                label=("ATR" if use_atr_stop else "고정"),
                atr=(atr_val if atr_val > 0 else None),
                is_usd=utils.is_usd_quoted(cand['code']))
            if levels:
                reason += f" {levels}"

            # [Fix] 신규 포지션 매수 전 이전 보유분의 잔존 상태 초기화.
            #  외부(MTS/HTS) 전량 매도는 엔진 매도 경로를 거치지 않아 트레일링 최고가·반익절
            #  DB 기록이 남는데(최고가 UPSERT는 단조 증가), 재시작 후 재매수 시 잔존 최고가로
            #  max_profit이 과대 계산되어 매수 직후 BEP/TS가 오발동(신규 포지션 즉시 청산)하는
            #  것을 방지한다. (후보군은 보유 종목을 제외하므로 이 시점은 항상 신규 포지션)
            # [히트 캡 선점] 주문을 내기 **전에** 예산을 잡는다. 종전에는 주문 성공 뒤에
            #  더했는데, 그 사이(네트워크 왕복)에 매도 워커의 피라미딩이 같은 예산을 보고
            #  자기 몫을 잡을 수 있어 합계가 캡을 넘었다. 피라미딩 경로는 이미 확인과
            #  선점을 락으로 원자화하고 실패 시 반납한다 — 같은 규약으로 맞춘다.
            #  [원자성] 위쪽 예산 확인은 배분액 기준이고 락 밖이다. 확인과 선점 사이에
            #   다른 경로(피라미딩)가 예산을 잡으면 합계가 캡을 넘으므로, **확정 수량으로
            #   다시 확인하고 같은 락 안에서 선점**한다. 경합이 없으면 이 재확인은 무동작이다
            #   (여기 도달한 시점의 qty×주문가는 위에서 통과한 배분액 이하다).
            new_risk_amt = (qty * order_price) * (abs(sl_rate) / 100.0) if (sl_rate and sl_rate < 0) else 0.0
            if new_risk_amt > 0:
                with self._lock:
                    budget_now = self.risk_manager.portfolio_risk_budget_left(avail_cash=avail_cash)
                    if budget_now is not None and new_risk_amt > budget_now:
                        self.log(f"매수 보류: {cand['name']} - 포트폴리오 총 리스크 예산이 "
                                 f"주문 직전에 소진됨 (필요 {new_risk_amt:,.0f}원 > 남은 "
                                 f"{max(budget_now, 0):,.0f}원)")
                        continue
                    self.portfolio_heat_amt += new_risk_amt

            db_manager.db.delete_trailing_stop(cand['code'])
            db_manager.db.delete_half_tp(cand['code'])
            with self._lock:
                self.trailing_stop_cache.pop(cand['code'], None)

            self.log(f"매수 실행: {cand['name']} - {reason}")

            # [수정] 매수 시 사유와 점수, 그리고 지정가 가격을 DB 저장을 위해 전달
            odno = self.order_manager.send_order(cand['code'], qty, "buy", name=cand['name'], reason=reason, score=cand['score'], price=order_price, rule=cand.get('rule'), stop_loss_rate=sl_rate)
            if not odno and new_risk_amt > 0:
                with self._lock:
                    self.portfolio_heat_amt -= new_risk_amt   # 주문 실패 시 선점분 반납
            if odno:
                # [추가] 매수 주문 성공 시 대기 중인 예약 매수 취소 방어 로직 (중복 진입 방지)
                target_cano = config.session.auto_cano
                target_acnt = config.session.auto_acnt_prdt_cd
                canceled_cnt = db_manager.db.cancel_reserved_buy_orders(target_cano, target_acnt, cand['code'])
                if canceled_cnt > 0:
                    self.log(f"[예약취소] 신규 매수로 인해 대기 중이던 {cand['name']} 매수 예약 주문 {canceled_cnt}건 자동 취소")
                    api.send_telegram_message(f"🗑 [예약 취소] {cand['name']}({cand['code']}) 신규 매수로 인해 대기 중이던 매수 예약 주문 {canceled_cnt}건이 자동 취소되었습니다.")
                
                self.half_tp_cache.discard(cand['code']) # 신규 매수 시 기존 반익절 캐시 방어적 초기화
                avail_cash -= (qty * order_price)
                current_holdings_count += 1 # [추가] 보유 종목 수 증가 반영
                # (히트 캡 선점은 주문 전에 끝냈다 — 위 new_risk_amt 참조)
                record = {
                    "type": "buy",
                    "code": cand['code'],
                    "name": cand['name'],
                    "qty": qty,
                    "price": order_price,
                    "reason": reason,
                    "time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                    "odno": odno,
                    "stop_loss_rate": sl_rate # [추가] 계산된 손절률 기록 (매도 시 참조 가능하도록)
                }
                self.trade_records.append(record)

    def _update_market_indices_status(self, notify=True):
        """KOSPI, KOSDAQ 지수 상태 업데이트 및 알림

        [Fix / 추세추종 원칙] "대체 무슨 일이 벌어지고 있는지 모르겠다면, 아무것도 하지 마라."
        기존에는 지수 데이터 조회 실패 시 is_healthy=True로 두어 '판단 불가'가 곧 '매수 허용'이
        되는 fail-open 구조였다. 시장 방향을 모르는 상태에서 신규 진입을 허용하는 것은
        추세추종의 전제(시장 방향을 파악해 포지션을 구축한다) 자체를 무너뜨리므로,
        매수 게이트는 fail-closed(보류)로 전환한다. 상태에 unknown=True 플래그를 실어
        '약세로 판정된 것'과 '판정 자체가 불가한 것'을 화면·로그에서 구분한다.
        (매도·손절 경로는 이 상태를 참조하지 않으므로 데이터 장애와 무관하게 계속 동작한다.)
        """
        # [수정] analysis 모듈의 공통 함수 사용을 위해 리스트로 변경
        target_indices = ["KOSPI", "KOSDAQ"]

        ma_period = getattr(config, 'MARKET_FILTER_MA', 80)
        band_pct = getattr(config, 'MARKET_FILTER_BAND', 1.0)

        for market_name in target_indices:
            try:
                # [수정] analysis 모듈의 공통 함수 사용 (Fallback 포함)
                df = analysis.get_domestic_index_data(market_name)

                if df is None or df.empty or len(df) < ma_period:
                    self.log(f"{market_name} 지수 데이터 부족/조회 실패 → 시장 방향 판단 불가로 신규 매수를 보류합니다. (매도·손절은 정상 동작)")
                    self.market_index_status[market_name] = {"is_healthy": False, "unknown": True, "current": 0,
                                                             "source": analysis.index_source(df)}
                    self._notify_market_unknown(market_name, notify)
                    continue

                # [이탈 확인 밴드] 상태 기계는 가격 이력만의 함수라 전 구간에서 재계산한다.
                #  재기동해도 상태가 유실되지 않고, 백테스트(prepare_market_filter)와 같은 값을 본다.
                current_idx = df['close'].iloc[-1]
                is_healthy = not bool(indicators.get_market_filter_blocked(
                    df['close'], ma_period, band_pct).iloc[-1])

                #  [Fix 2026-09-04] 판단에 쓴 지수가 **어디서 온 값인지** 함께 남긴다.
                #   지수는 KRX 확정 봉 위에 KIS/토스/tvDatafeed/yfinance 중 하나를 얹어
                #   만드는데(analysis._fetch_domestic_index_data), 종전에는 그 출처가
                #   attrs 에만 있고 아무도 읽지 않았다. 최후 폴백(yfinance)은 최신 종가를
                #   결측으로 주는 일이 잦아, 매수 중단·재개가 어긋났을 때 무엇으로 판단한
                #   것인지 되짚을 수 없었다.
                self.market_index_status[market_name] = {
                    "is_healthy": is_healthy,
                    "unknown": False,
                    "current": current_idx,
                    "source": analysis.index_source(df),
                }

                # [동적 손절 캡] KOSPI 실현변동성의 장기 대비 배율을 갱신한다. 이 값이
                #  engine.effective_atr_stop_cap을 통해 손절 캡을 국면에 맞춰 넓힌다.
                #  [기준 지수는 KOSPI 하나] 코스닥 종목도 KOSPI를 쓴다 — 캡은 '시장 전체가
                #  지금 얼마나 거친가'를 재는 장치이고, 무엇보다 이 설정을 정한 검증이
                #  KOSPI 단일 기준으로 수행됐다. 실매매가 다른 기준을 쓰면 그 검증이
                #  실거래에 옮겨가지 않는다(백테스트 backtest.prepare_vol_regime과 동일 규약).
                if market_name == "KOSPI":
                    try:
                        _pkg().set_vol_regime_ratio(
                            float(indicators.vol_regime_ratio(df['close']).iloc[-1]))
                    except Exception as e:
                        logger.debug(f"[동적 손절 캡] 변동성 배율 갱신 실패(고정 캡 유지): {e}")

                # 상태 변경 알림
                if not notify:
                    continue

                # 밴드가 켜져 있으면 '이평선 아래'가 아니라 '이평선 -밴드% 이탈'이 실제 트리거이므로
                #  문구에 밴드를 함께 실어 화면·알림과 판정식이 어긋나 보이지 않게 한다.
                band_txt = f" -{band_pct:g}%" if band_pct else ""
                notified = self.market_status_notified.get(market_name, False)
                if not is_healthy and not notified:
                    api.send_telegram_message(f"📉 [시장 감지] {market_name} 지수가 {ma_period}일 이평선{band_txt} 아래로 하락했습니다.\n해당 시장 종목의 신규 매수를 일시 중단합니다.")
                    self.market_status_notified[market_name] = True
                elif is_healthy and notified:
                    band_up = f" +{band_pct:g}%" if band_pct else ""
                    api.send_telegram_message(f"📈 [시장 회복] {market_name} 지수가 {ma_period}일 이평선{band_up}을 회복했습니다.\n매수를 재개합니다.")
                    self.market_status_notified[market_name] = False
            except Exception as e:
                self.log(f"{market_name} 지수 조회 실패: {e} → 시장 방향 판단 불가로 신규 매수를 보류합니다. (매도·손절은 정상 동작)")
                #  예외 경로라 df 를 신뢰할 수 없다 — 출처는 비워 둔다(모른다를 모른다로 남긴다).
                self.market_index_status[market_name] = {"is_healthy": False, "unknown": True, "current": 0,
                                                         "source": None}
                self._notify_market_unknown(market_name, notify)

    def _notify_market_unknown(self, market_name, notify=True):
        """지수 판단 불가(데이터 장애)로 매수를 보류할 때 1회만 알린다.

        '약세 판정'과 원인이 다르므로 문구를 분리하되, 스로틀 플래그는
        market_status_notified를 공유해 회복 시 '매수 재개' 알림이 정상적으로 나가게 한다.
        """
        if not notify:
            return
        if self.market_status_notified.get(market_name, False):
            return
        #  전달을 확인한 뒤에 래치를 건다. 종전에는 전송 여부와 무관하게 걸려, 실패하면
        #  '신규 매수 보류' 상태를 운영자가 끝까지 모른 채 지나갔다(래치는 회복 때만 풀린다).
        if _pkg().alert_delivered(
                f"⚠️ [판단 보류] {market_name} 지수 데이터를 확인할 수 없습니다.\n"
                f"시장 방향을 알 수 없으므로 해당 시장 종목의 신규 매수를 보류합니다.\n"
                f"(보유 종목의 손절·트레일링 스탑 감시는 계속됩니다)"):
            self.market_status_notified[market_name] = True

    @staticmethod
    def _whipsaw_risk_scale(whipsaw, params=None):
        """휩소율(0~1) → 리스크 한도 배수. LO 이하 1.0, HI 이상 MIN_SCALE, 사이는 선형 보간.

        휩소율이 높다 = 최근 추세 전환들이 확인 기준(5%)을 채우지 못하고 되돌려졌다
        = 톱니장이다. 추세추종 시스템이 가장 잃기 쉬운 구간이므로 진입 크기를 줄인다."""
        params = params if params is not None else (getattr(config, 'RISK_SCALING_PARAMS', {}) or {})
        try:
            lo = float(params.get("WHIPSAW_LO", 0.40))
            hi = float(params.get("WHIPSAW_HI", 0.75))
            min_scale = float(params.get("WHIPSAW_MIN_SCALE", 0.6))
        except (TypeError, ValueError):
            lo, hi, min_scale = 0.40, 0.75, 0.6
        if whipsaw is None or hi <= lo or not (0 < min_scale < 1.0):
            return 1.0
        if whipsaw <= lo:
            return 1.0
        if whipsaw >= hi:
            return min_scale
        return 1.0 - (whipsaw - lo) / (hi - lo) * (1.0 - min_scale)

    def _update_risk_scale(self):
        """[리스크 스케일링] 시장 국면·휩소율·계좌 드로다운에 따른 신규 진입 리스크 한도 배수 갱신

        [추세추종 2원칙] "자본대비 리스크에 한도를 둬야 한다" — 추세가 먹히지 않는 구간과
        손실 구간에서는 신규 진입 리스크 한도를 줄여 드로다운을 통제한다(터틀식).
        결과는 RiskManager가 종목당 리스크(SYSTEM_RISK_PER_TRADE)와
        히트 캡(SYSTEM_MAX_PORTFOLIO_RISK)에 곱해 사용한다. 청산 로직에는 관여하지 않는다.
        (국면 배수 × 휩소율 배수) × 드로다운 배수가 곱으로 결합된다.

        [시장별 분리 2026-07-27] 국면·휩소율은 KOSPI/KOSDAQ이 서로 다른 시장이므로 각각 산출해
        self.risk_scale_by_market에 담고, 종목 사이징에는 **그 종목이 속한 시장의 배수**를 쓴다.
        (종전에는 두 시장 중 나쁜 쪽 하나를 계좌 전체에 적용해, 코스닥이 톱니장이면 코스피
         종목까지 축소되고 로그에도 'KOSDAQ'만 찍혀 오인을 샀다.)
        계좌 드로다운은 시장과 무관한 계좌 상태이므로 두 시장 배수에 공통으로 곱한다.
        반면 self.risk_scale(단일 값)은 **두 시장 중 열위 쪽**을 유지한다 — 이 값이 쓰이는
        히트 캡은 계좌 전체의 총 오픈 리스크를 묶는 장치라 시장별로 나눌 수 없고,
        보수적인 쪽을 택하는 것이 맞기 때문이다.

        [적용 지점 2026-07-27 — 리스크층 → 기초 비중으로 이동]
        종전에는 이 배수를 allocate_budget의 2)리스크층에만 곱했는데, 그 층은 최종액을 결정하는
        일이 없어(3)변동성 타겟팅이 상시 구속) **배수가 약 0.45 미만으로 내려가기 전까지 배분액이
        1원도 변하지 않았다**. 단일 트리거(PendDown 0.6 / 휩소율 0.6 / DD-5% 0.75)로는 도달하지
        못해 사실상 무력했다(백테스트에서 '스케일링 OFF'와 현행이 거래 803건까지 동일).
        이를 1)기초 비중에 적용하도록 옮겨, 3)의 상한(기초×변동성배수)까지 함께 내려가게 했다.

        [실측 2026-07-27 — 시드 500만/1,000만 · 30종목 무작위 50회 짝비교]
          MDD 개선 46/50·45/50 (중앙 +2.8%p·+3.2%p), PF 개선 41/50·44/50 (중앙 +0.27·+0.34).
          대가는 3년 수익 중앙 -16.9%p·-24.1%p, 유휴현금 +13%p.
          타이밍 가치 검증: 같은 평균 배수(0.694)를 상수로 준 대조군은 수익이 절반(146.5→71.0%),
          PF도 낮았다(2.83→2.20). 셔플 대조군도 동일 → 국면·휩소율 판단이 실제로 기여한다.
          ※ 변동성 캡에 직접 곱하는 방식은 3년 수익 -26%로 열위여서 채택하지 않았다."""
        params = getattr(config, 'RISK_SCALING_PARAMS', {}) or {}
        scale = 1.0
        reasons = []
        # 시장별 맵은 항상 두 시장을 채운다 — 국면·휩소율을 모두 꺼도 드로다운 배수가 실릴 곳이 필요하다.
        market_scales = {"KOSPI": 1.0, "KOSDAQ": 1.0}
        market_reasons = {"KOSPI": "", "KOSDAQ": ""}
        try:
            # 1) 시장 국면 + 휩소율: 시장별로 각각 산출한다(코스피/코스닥은 별개 시장).
            #    종목 사이징엔 해당 종목의 시장 배수를, 계좌 단위 히트 캡엔 열위 시장 배수를 쓴다.
            use_regime = params.get("USE_REGIME_RISK_SCALING", True)
            use_whipsaw = params.get("USE_WHIPSAW_RISK_SCALING", True)
            if use_regime or use_whipsaw:
                best_scale, best_reason = 1.0, None
                for m_type in ("KOSPI", "KOSDAQ"):
                    info = analysis.get_market_regime_detail(m_type)
                    m_scale, m_parts = 1.0, []

                    # 1-a) 국면 배수 — 축소 대상은 '하락 미확정'(추세 붕괴 초기)이 핵심
                    if use_regime:
                        key = {"PendDown": ("PENDING_DOWN_RISK_SCALE", 0.6),
                               "Bear": ("BEAR_RISK_SCALE", 1.0)}.get(info['regime'])
                        if key:
                            try:
                                rs = float(params.get(key[0], key[1]))
                            except (TypeError, ValueError):
                                rs = key[1]
                            if 0 < rs < 1.0:
                                m_scale *= rs
                                m_parts.append(f"{analysis.format_regime(info['regime'], markup=False)} x{rs:g}")

                    # 1-b) 휩소율 배수 — 톱니장일수록 연속적으로 축소
                    if use_whipsaw and info.get('whipsaw_ratio') is not None:
                        ws_scale = self._whipsaw_risk_scale(info['whipsaw_ratio'], params)
                        if ws_scale < 1.0:
                            m_scale *= ws_scale
                            m_parts.append(f"휩소율 {info['whipsaw_ratio']*100:.0f}% x{ws_scale:.2f}")

                    market_scales[m_type] = m_scale
                    market_reasons[m_type] = " ".join(m_parts)
                    if m_scale < best_scale:
                        best_scale, best_reason = m_scale, f"{m_type} " + " ".join(m_parts)

                if best_reason:
                    scale *= best_scale
                    reasons.append(best_reason)

            # 2) 계좌 드로다운: 자산 고점(HWM) 대비 하락률에 따라 단계적 감속
            if params.get("USE_DRAWDOWN_RISK_SCALING", True):
                dd = self._get_account_drawdown_pct(params)
                try:
                    lv1, sc1 = float(params.get("DD_LEVEL_1", 5.0)), float(params.get("DD_SCALE_1", 0.75))
                    lv2, sc2 = float(params.get("DD_LEVEL_2", 10.0)), float(params.get("DD_SCALE_2", 0.5))
                except (TypeError, ValueError):
                    lv1, sc1, lv2, sc2 = 5.0, 0.75, 10.0, 0.5
                # 드로다운은 계좌 상태(시장 무관)이므로 두 시장 배수에 공통으로 곱한다.
                dd_scale, dd_reason = None, None
                if lv2 > 0 and dd >= lv2 and 0 < sc2 < 1.0:
                    dd_scale, dd_reason = sc2, f"드로다운 {dd:.1f}% x{sc2:g}"
                elif lv1 > 0 and dd >= lv1 and 0 < sc1 < 1.0:
                    dd_scale, dd_reason = sc1, f"드로다운 {dd:.1f}% x{sc1:g}"
                if dd_scale is not None:
                    scale *= dd_scale
                    reasons.append(dd_reason)
                    # [경보] 깊은 드로다운은 그 자체로 알려야 할 사건이다 — 진짜라면 재앙이고,
                    #  가짜라면(자산 스냅샷 오류) 룩백 내내 조용히 리스크 한도를 묶는다.
                    #  2026-08-23 가상투자에서 유령 고점 한 행이 드로다운을 50%로 만들어
                    #  히트 캡을 8.5%→6.8%로 조였고, 증액이 206주기 차단되는 동안 이 사실은
                    #  리스크 스케일링 로그 한 줄에만 묻혀 있었다. 하루 1회만 알린다.
                    if dd >= lv2:
                        today_key = datetime.now().strftime("%Y-%m-%d")
                        if getattr(self, '_dd_alert_date', None) != today_key:
                            self._dd_alert_date = today_key
                            equity = getattr(self, 'current_total_asset', 0) or self.initial_asset
                            msg = (f"⚠️ [계좌 드로다운 {dd:.1f}%]\n"
                                   f"자산 고점 대비 {dd:.1f}% 하락으로 판정해 신규 진입·증액 "
                                   f"리스크 한도를 x{dd_scale:g}로 줄였습니다.\n"
                                   f"(현재 평가자산 {equity:,.0f}원)\n\n"
                                   f"입출금은 정지 중에 있었던 것까지 자동 반영되므로 "
                                   f"조치할 것은 없습니다. 다만 손실이 실제와 다르다면 "
                                   f"자산 스냅샷(daily_asset_history)에 잘못된 고점이 남아 "
                                   f"있을 수 있습니다 — 그대로 두면 "
                                   f"{int(params.get('DD_LOOKBACK_DAYS', 90))}일간 한도가 묶입니다.")
                            self.log(f"[리스크 스케일링] 계좌 드로다운 {dd:.1f}% — 한도 x{dd_scale:g} 축소")
                            try:
                                api.send_telegram_message(msg)
                            except Exception:
                                pass
                    for m_type in market_scales:
                        market_scales[m_type] *= dd_scale
                        market_reasons[m_type] = " ".join(
                            p for p in (market_reasons.get(m_type, ""), dd_reason) if p)
        except Exception as e:
            logger.debug(f"[리스크 스케일링] 배수 계산 실패 (기존값 유지): {e}")
            return

        prev = getattr(self, 'risk_scale', 1.0)
        self.risk_scale = scale
        self.risk_scale_reason = ", ".join(reasons)
        self.risk_scale_by_market = market_scales
        self.risk_scale_reason_by_market = market_reasons
        if abs(scale - prev) > 1e-9:
            if scale < 1.0:
                rpt = getattr(config, 'SYSTEM_RISK_PER_TRADE', 4.0)
                cap = getattr(config, 'SYSTEM_MAX_PORTFOLIO_RISK', 10.0)
                per_market = ", ".join(
                    f"{m} x{market_scales[m]:.2f}(종목당 {rpt * market_scales[m]:.1f}%)"
                    for m in ("KOSPI", "KOSDAQ") if m in market_scales)
                self.log(f"[리스크 스케일링] 신규 진입 리스크 한도 축소 — {per_market or f'x{scale:.2f}'} "
                         f"| 히트 캡 {cap * scale:.1f}%(계좌 전체, 열위 시장 x{scale:.2f} 기준) "
                         f"({self.risk_scale_reason}) (청산 로직 영향 없음)")
            else:
                self.log("[리스크 스케일링] 리스크 한도 정상 복원 (x1.00)")

    def _get_account_drawdown_pct(self, params=None):
        """계좌 드로다운(%) — 최근 DD_LOOKBACK_DAYS 일간 자산 고점(HWM) 대비 현재 평가자산 하락률

        HWM은 daily_asset_history(일일 시작자산 스냅샷)의 룩백 구간 최대값과 당일 시작자산 중
        큰 값을 사용한다. 룩백 제한은 오래된 고점·입출금으로 인한 왜곡을 완화하기 위함이다.
        DB 조회는 하루 1회만 수행하고 캐싱한다."""
        params = params or getattr(config, 'RISK_SCALING_PARAMS', {}) or {}
        equity = getattr(self, 'current_total_asset', 0) or self.initial_asset
        if equity <= 0:
            return 0.0

        today = datetime.now().strftime("%Y-%m-%d")
        if getattr(self, '_hwm_cache_date', None) != today:
            hwm = 0.0
            try:
                lookback = int(params.get("DD_LOOKBACK_DAYS", 90))
                if lookback <= 0:
                    lookback = 90
                start_date = (datetime.now() - timedelta(days=lookback)).strftime("%Y-%m-%d")
                cano, acnt = _get_trade_account()
                account_key = f"{cano}-{acnt}"
                hwm = float(db_manager.db.get_max_daily_asset(start_date, account_key) or 0.0)
            except Exception:
                hwm = 0.0
            self._hwm_cache = hwm
            self._hwm_cache_date = today

        # [Fix 2026-09-01] 바닥값은 원본 시작 자산이 아니라 **오늘 입출금까지 반영한**
        #  기준선이다. 원본을 쓰면 오늘 나간 출금이 그대로 고점으로 남아, 정상적인 출금
        #  한 번이 그날 내내 가짜 드로다운(1,000만에서 300만 출금 = 30%)을 만들고 경보까지
        #  울린다. DB 이력 쪽은 net_transfer 로 이미 환산되는데 이 한 줄만 빠져 있었다.
        hwm = max(getattr(self, '_hwm_cache', 0.0),
                  float(self.effective_baseline() or self.initial_asset or 0))
        if hwm <= 0:
            return 0.0
        return max(0.0, (hwm - equity) / hwm * 100.0)

    def _get_stock_market_type(self, code):
        """종목 코드로 시장 구분(KOSPI/KOSDAQ) 확인 (인스턴스 캐시 사용)"""
        return _pkg().resolve_market_type(code, self.stock_market_map)
