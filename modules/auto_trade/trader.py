# modules/auto_trade/trader.py
"""AutoTrader: 시스템 트레이딩 메인 루프 (분석→매수/매도→리포트)

기존 modules/auto_trade.py 에서 분해. 외부 인터페이스는 패키지(__init__)가 재수출한다.
화면·보고(상태 화면·운영 관제·성과 보고서·로그 보기)는 reporting.TraderReportingMixin 이 들고 있고
AutoTrader 가 상속한다(2026-10-24 C1 1단계 — 이름·호출 방법은 그대로).
시장·리스크(지수 상태·판단 보류 알림·리스크 배수·동적 손절 캡 배율)는 market_risk.TraderMarketRiskMixin 이
들고 있다(2026-10-24 C1 2단계 — 같은 규약).
계좌 동기화(오늘의 자본 기준선·입출금 감지와 오프라인 대조·계좌 차단기·기동 외부 체결 동기화)는
account_sync.TraderAccountSyncMixin 이 들고 있다(2026-10-25 C1 3단계 — 같은 규약).
매도(판정 루프·미관리 포지션 경보·손절 보호·트레일링 앵커·반익절 이력·권리 조정·장 마감 뒤 청산 신호)는
selling.TraderSellingMixin 이 들고 있다(2026-10-25 C1 4단계 — 같은 규약).
매수(계좌 상태 게이트·당일 재진입 방어·후보 분석과 게이트·주문 집행·피라미딩)는 buying.TraderBuyingMixin 이
들고 있다(2026-10-25 C1 5단계 — 같은 규약). 후보 정렬 키·오염 일봉 판정도 그쪽으로 옮겼고 여기서 다시 import 한다.
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
from datetime import datetime
from rich.prompt import Prompt
from rich.progress import Progress, SpinnerColumn, BarColumn, TextColumn, TimeRemainingColumn
import config
from core import context # [추가]
import api
from core import utils
from modules import account # [수정] account 모듈 재사용
from modules import db_manager # [추가] DB 매니저
from modules import chart # [추가] 차트 모듈
from modules import instance_lock # [추가] 자동매매 단일 실행 보장
from modules.auto_trade import performance # 성과 보고 계산(순수 함수)
from modules.auto_trade.reporting import TraderReportingMixin  # 화면·보고(상태·관제·보고서) — AutoTrader 가 상속한다
from modules.auto_trade.market_risk import TraderMarketRiskMixin  # 시장·리스크(지수 상태·리스크 배수) — AutoTrader 가 상속한다
from modules.auto_trade.account_sync import TraderAccountSyncMixin  # 계좌 동기화(기준선·입출금·차단기) — AutoTrader 가 상속한다
from modules.auto_trade.selling import TraderSellingMixin  # 매도(판정 루프·손절 안전망·앵커) — AutoTrader 가 상속한다
from modules.auto_trade.buying import TraderBuyingMixin  # 매수(게이트·후보 분석·집행·피라미딩) — AutoTrader 가 상속한다
#  후보 정렬 키·오염 일봉 판정은 매수와 함께 buying 으로 옮겼다. trader.candidate_priority_key 로 부르는 곳
#   (테스트·감사 도구 문서)과 패키지 네임스페이스(modules.auto_trade.X)가 그대로 동작하게 다시 import 한다.
from modules.auto_trade.buying import candidate_priority_key, contaminated_chart_reason  # noqa: F401

from modules.auto_trade.engine import DefaultStrategy, OrderManager, RiskManager
from modules.auto_trade.common import (DISK_FREE_WARN_MB, _enrich_rules_with_weights, _get_trade_account,
    trade_account_key, get_mystock_log_tail, get_restricted_stocks, is_plausible_baseline, is_single_price_break, is_system_market_open, load_daily_initial_asset, load_daily_principal, save_daily_initial_asset)

console = config.console

logger = logging.getLogger(__name__)

#  같은 DB 쓰기 실패를 다시 알리기까지의 간격(초). 디스크가 차면 매 주기 실패하므로
#  억제하지 않으면 알림이 도배되어 정작 중요한 경보가 묻힌다.
DB_WRITE_FAIL_ALERT_COOLDOWN = 1800.0
# [입출금 자동 조정 상한] 기준 자산은 계좌 차단기의 분모이자 사이징의 기준이라, 오탐 한 번이
#  두 장치를 동시에 틀어 놓는다. 추정 금액이 기준 자산의 이 비율(또는 아래 절대 하한)을 넘으면
#  자동 반영하지 않고 사람에게 넘긴다 — 반영하지 않으면 기준이 옛 값(더 작은 쪽)으로 남아
#  차단기가 더 일찍 걸리므로, '안 고치는 쪽'이 안전한 방향이다.

#  '서버는 정상인데 코드가 터진다'를 다시 알리기까지의 간격(초). 이 상태는 대기로
#  숨겨지지 않고 매 주기 반복되므로, 억제하지 않으면 알림이 도배된다.
CODE_ERROR_ALERT_COOLDOWN = 1800.0


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


class AutoTrader(TraderReportingMixin, TraderMarketRiskMixin, TraderAccountSyncMixin, TraderSellingMixin,
                 TraderBuyingMixin):
    _instance = None
    _instance_lock = threading.RLock()
    
    #  [동시성 2026-09-05] 싱글톤 생성을 락으로 감싼다. 종전 `if cls._instance is None:` 는
    #   검사와 대입 사이가 열려 있었고, 더 나쁜 것은 **인스턴스를 먼저 대입하고 속성을 그 뒤에
    #   채운다**는 점이었다 — 두 번째 스레드는 그 사이에 들어와 '있다'고 보고 반쯤 만들어진
    #   객체를 그대로 가져간다. 초기화 도중에 파일 I/O(로거 생성)·DB 접근이 있어 GIL 이 실제로
    #   놓이므로 이론상의 경합이 아니다(실측: 8스레드 중 7개가 미완성 객체를 받는다).
    #   기동 순서상 열려 있다 — main 이 텔레그램 봇 스레드를 먼저 띄우고(telegram_cmd.start())
    #   스케줄러·트레이더는 그 뒤에 처음 만든다. 봇 스레드의 명령 처리는 이 생성자를 부른다.
    def __new__(cls):
        with cls._instance_lock:
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
                #  드로다운을 곱하기 전의 국면 배수. 국면을 못 읽은 주기가 이 값을 재사용한다
                #  (저장본에는 드로다운이 이미 곱해져 있어 그대로 쓰면 두 번 곱해진다).
                cls._instance.risk_scale_regime_by_market = {}
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
                #  캐시가 **실제로 적재됐는가**. 빈 집합은 '아무도 반익절 안 했다'로도
                #  '못 읽었다'로도 보인다 — 바로 아래 portfolio_heat_unknown 과 같은 규약이다.
                cls._instance.half_tp_cache_loaded = False
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
        """거래 내역 중복 제거 및 우선순위 적용 (전략 사유 > 체결 확인) — performance 에 위임."""
        return performance.refine_trade_records(records)

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
                    try:
                        ts_cache = db_manager.db.get_all_trailing_stops()
                    except Exception as _te:
                        #  앵커는 종목별로 다시 읽는 길이 있다(_cached_anchor) — 비워 둬도
                        #  스스로 낫는다. 흔적만 남긴다.
                        logger.warning(f"[기동] 트레일링 앵커 캐시 적재 실패 — 종목별로 "
                                       f"다시 읽습니다: {_te}")
                        ts_cache = {}
                    try:
                        half_cache = db_manager.db.get_all_half_tp()
                    except Exception as _he:
                        #  반익절은 다르다. 빈 집합은 '아무도 반익절 안 했다'로 읽혀
                        #  이미 반쪽 판 종목을 **또 판다**. 못 읽었다는 사실을 들고 간다.
                        logger.error(f"[기동] 반익절 이력 적재 실패: {_he}")
                        half_cache = None
                    # [재기동 복구] 거래소에 살아 있는 미체결 주문을 메모리 추적에 되살린다.
                    #  이걸 안 하면 첫 주기에 같은 종목으로 두 번째 주문이 나간다.
                    #  DB 캐시 작업에 얹어 시작 시 API 호출이 몰리지 않게 한다(라즈베리파이 OOM).
                    ok = self.order_manager.restore_pending_orders(target_cano, acnt)
                    progress.advance(task)
                    return "caches", (ts_cache, half_cache, ok)


                #  [계좌 컨텍스트 · 2026-09-05] use_auto_account 는 threading.local 이라
                #   AccountContext 안에서 제출해도 워커 스레드에는 상속되지 않는다. 풀리면
                #   그 요청은 **수동 앱키·수동 토큰**으로 나가고(core.utils.get_common_headers,
                #   api.auth.get_current_token) TPS 도 수동 버킷에서 깎인다
                #   (api.http._real_bucket_key). 자동 계좌 잔고를 수동 키로 묻는 꼴이다.
                #   매도·후보 풀은 이미 감싸져 있었는데 기동 초기화 풀만 빠져 있었다.
                _init_task = utils.inherit_account_context
                with concurrent.futures.ThreadPoolExecutor(max_workers=3, thread_name_prefix="at_init") as executor:
                    # [수정] 모의투자는 잔고 summary에 예수금이 포함되어 있어 별도 예수금 API 호출이 불필요.
                    # 초기화 시 중복 잔고조회(get_domestic_balance)+예수금조회가 2-TPS 경합으로 재시도
                    # 폭주를 일으켜 메모리가 폭증하던 문제를 제거한다. (실전만 별도 예수금 조회 수행)
                    futures = [executor.submit(_init_task(_fetch_balance)),
                               executor.submit(_init_task(_load_db_caches))]
                    futures.append(executor.submit(_init_task(_fetch_deposit)))
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
                self.half_tp_cache = half_cache if half_cache is not None else set()
                self.half_tp_cache_loaded = half_cache is not None
                if not self.half_tp_cache_loaded:
                    self.log("[기동] 반익절 이력을 읽지 못했습니다 — 판정 때 다시 읽고, "
                             "그래도 못 읽으면 추가 반익절을 내지 않습니다(중복 매도 방지).")
                self.initial_holdings = holdings
                self.initial_summary = summary
                #  하트비트(scheduler)가 사망 알림에 싣는 '보유 종목 수'가 이 값이다. 종전에는
                #  첫 매매 주기의 잔고 조회에서만 갱신돼, 휴장일에 재기동하면 다음 거래일
                #  09:00까지 0으로 남았다(실측 2026-09-13 파이: 보유 4종목인데 holdings=0).
                #  그 사이 프로세스가 죽으면 "포지션이 무방비" 줄이 빠진 채 알림이 나간다.
                #  기동 시 잔고를 이미 손에 들고 있으니 여기서 바로 씨딩한다.
                self.last_holdings_count = len(holdings)
                
                # [수정] 초기 총자산 산출.
                # 기존에는 get_asset_status_data를 재호출했으나, 이는 startup 직후 잔고조회를
                # 또 수행(+해외잔고/체결내역)하여 짧은 시간에 KIS 호출이 몰리고, 그 중 일부가
                # 재시도(타임아웃/EGW00201) 경로로 빠지며 네이티브 메모리가 폭증(OOM)하는 원인이었다.
                # 모의투자는 이미 조회한 잔고 summary의 총평가금(tot_evlu_amt)으로 유도하여
                # 추가 KIS 호출을 제거한다. (실전만 해외자산 포함 통합 조회 유지)
                asset_data = account.get_asset_status_data(target_cano, acnt)
                tot_asset = asset_data.get('tot_asset', 0) if asset_data else 0

                #  [Fix 2026-09-08] 장중 경로(_degraded 검사 2곳)는 결손을 보고 대안 계산으로
                #   비키는데, **하루의 기준선을 박는 이 자리만** 그 신호를 안 봤다. 결손이 있는
                #   총자산은 '그만큼 작은 정상 숫자'라 아래 is_plausible_baseline 의 반토막
                #   문턱에 닿지 않는다(노출 상한이 40%라 주식이 통째로 빠져도 못 닿는다).
                #   기준선은 하루를 지배하고 되돌릴 수 없으므로, 모르면 박지 않는다.
                _degraded = list((asset_data or {}).get('degraded') or [])
                if tot_asset > 0 and _degraded:
                    tot_asset = 0
                    warn = (f"⚠️ [시작 자산 보류] 자산 집계에서 결손이 있습니다"
                            f"({', '.join(_degraded)}).\n"
                            f"이 값을 오늘 기준 자산으로 삼지 않습니다 — 계좌 차단기"
                            f"(일일 손실 한도)가 동작하지 않으니 확인해 주세요.")
                    self.log(warn)
                    api.send_telegram_message(warn)
                    #  첫 주기의 계좌 감시도 결손이면 같은 보류를 한다(account_sync) — 같은 날
                    #  같은 사실을 두 번 알리지 않도록 그쪽 스로틀을 찍어 둔다.
                    self._baseline_defer_date = datetime.now().strftime("%Y-%m-%d")

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
                        if not save_daily_initial_asset(account_key, self.initial_asset):
                            self.log("[기준선] 당일 시작 자산을 파일에 저장하지 못했습니다 — "
                                     "재기동하면 오늘의 손실 한도·드로다운 기준이 리셋됩니다")
                        #  파일 저장의 성공 여부는 바로 위에서 검사한다 — 같은 기준선을 담는
                        #  DB 쪽도 같게 다룬다(둘 중 하나만 검사하면 절반은 조용히 사라진다).
                        if not db_manager.db.save_daily_asset(
                                datetime.now().strftime("%Y-%m-%d"), account_key, self.initial_asset):
                            self.log("[기준선] 당일 시작 자산을 자산 이력에 남기지 못했습니다 — "
                                     "내일의 드로다운 기준에서 오늘이 빠집니다")
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
        self.start_block_reason = ""
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
        #  [2026-09-07] 사유가 로그에만 남으면 원격(텔레그램)에서 /start 를 친 운용자는
        #   왜 안 떴는지 알 수 없다. 그가 화면 앞에 없다는 것이 이 경로의 전제다.
        self.start_block_reason = f"같은 계좌로 다른 프로세스가 이미 매매 중입니다{holder}"
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

        free_mb = db_manager.db.disk_free_mb()
        msg = (f"DB 쓰기 실패 누적 {h['count']}건 (최근: {h['last_op']} — {h['last_error']})\n"
               f"디스크 여유 {free_mb:,.0f}MB\n"
               f"트레일링 최고가가 저장되지 않으면 재기동 후 청산선이 어긋납니다.")
        self.log(f"[DB 쓰기 실패] {msg}")
        #  [전달 확인 뒤 쿨다운 · 2026-09-07] 종전에는 보내기 전에 쿨다운을 찍었다.
        #   비동기 전송이라 실패가 예외로 오지 않으므로, 못 닿아도 그 쿨다운 동안 침묵한다.
        #   내용이 '디스크가 차서 트레일링 최고가가 저장되지 않는다' 라 놓치면 비싸다.
        if _pkg().alert_delivered(f"⚠️ [DB 쓰기 실패] {msg}", urgent=True):
            self._db_write_fail_alerted_at = now

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
            #  [Fix 2026-10-25] 시작 자산을 보류한 날(집계 결손·직전 대비 급감·조회 실패)에는
            #   '초기 자산 0원'이 찍혔고, 예수금도 그 조건에 묶여 함께 0원이었다 — 잔고가 빈
            #   계좌와 글자가 같다. 보류 사유는 위의 경고가 따로 알린다. 예수금은 기동 잔고
            #   조회(summary)에서 읽으므로 시작 자산과 무관하게 보이고, 그 조회가 없을 때만
            #   '확인 불가'로 적는다(못 읽은 것은 0원이 아니다).
            deposit = None
            if summary:
                deposit = api.safe_int(summary[0].get('prvs_rcdl_excc_amt', 0))
                if deposit == 0:
                    deposit = api.safe_int(summary[0].get('dnca_tot_amt', 0))

            msg = f"🟢 [시스템 시작] 자동매매가 시작되었습니다.\n"
            if self.initial_asset > 0:
                msg += f"초기 자산: {self.initial_asset:,}원"
            else:
                msg += "초기 자산: 보류 — 결손 없는 자산 조회에서 잡습니다(그때까지 계좌 차단기 미동작)"
            msg += f"\n현재 예수금: {deposit:,}원" if deposit is not None else "\n현재 예수금: 확인 불가"
            
            # [복원] 상세 자산 현황 추가
            stock_eval_amt = 0
            if summary and len(summary) > 0:
                s_data = summary[0]
                stock_eval_amt = api.safe_int(s_data.get('scts_evlu_amt'))
                total_profit = api.safe_int(s_data.get('evlu_pfls_smtl_amt'))
                
                tot_pchs = api.safe_int(s_data.get('pchs_amt_smtl'))
                if tot_pchs == 0 and holdings:
                    tot_pchs = sum(int(api.safe_int(h.get('hldg_qty')) * api.safe_float(h.get('pchs_avg_pric'), default=0.0)) for h in holdings if api.safe_int(h.get('hldg_qty')) > 0)
                
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
            valid_holdings = [h for h in holdings if api.safe_int(h.get('hldg_qty')) > 0] if holdings else []

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
                        valid_holdings = [h for h in holdings if api.safe_int(h.get('hldg_qty')) > 0]
                        unmanaged_count = len(valid_holdings)
                        stock_eval = sum(api.safe_int(h.get('evlu_amt')) for h in valid_holdings)
                        tot_profit = sum(api.safe_int(h.get('evlu_pfls_amt')) for h in valid_holdings)
                        tot_pchs = sum(int(api.safe_int(h.get('hldg_qty')) * api.safe_float(h.get('pchs_avg_pric'), default=0.0)) for h in valid_holdings)
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
                    valid_holdings = [h for h in holdings if api.safe_int(h.get('hldg_qty')) > 0] if holdings else []

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
            #  [전달 확인 · 2026-09-07] 이 알림은 **하루에 한 번**만 나간다(위의
            #   `buy_halted and buy_halt_date == today` 가 재발동을 막는다). 그래서 한 번
            #   못 닿으면 그날은 끝이다 — 운영자는 신규 매수가 멈춘 것을 모른 채 하루를
            #   보낸다. 상태는 이미 뒤집혔으므로 되돌리지 않고, 못 닿은 사실만 남긴다.
            if not _pkg().alert_delivered(notify_msg, urgent=True):
                logger.warning(f"[방어 모드] 발동 알림을 전달하지 못했습니다 — 신규 매수가 "
                               f"멈춘 사실이 운영자에게 닿지 않았습니다(사유: {reason}). "
                               f"상태 화면에는 표시됩니다.")
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
        self._warn_if_memory_low()

    def _warn_if_memory_low(self):
        """가용 메모리가 OOM 문턱에 다가서면 **먼저 알린다.**

        [왜 · 2026-09-08 감사] 문턱(경고 120MB · 위험 60MB)은 이미 있었는데
         `get_health_message()` 안에만 있었다 — 사람이 `/health` 를 **쳐야** 보이는
         값이다. 그런데 OOM 은 종목 분석이 몰리는 순간에 나고, 그 순간 화면을 보고 있는
         사람은 없다(헤드리스 운영). 결국 메모리는 늘 사후에만 알려졌다: 프로세스가
         죽고, 밖의 cron 감시자가 사망을 통보하는 순서다([[deployment-raspberry-pi]]).
         알림 인프라는 이미 다 있으므로 여기서 한 번 밀어 준다.

        [왜 주기 끝인가] 이 자리가 한 주기의 최대 사용 직후다 — 후보 분석·차트가 방금
         돌았으므로 여유 메모리가 가장 적게 나온다. 한가한 시각의 값으로 안심하지 않는다.

        같은 등급으로는 하루 한 번만 보낸다. 매 주기(기본 60초) 보내면 경보가 소음이 되고,
        정작 다음 사고를 놓친다. 전달을 **확인한 뒤에** 표식을 남긴다([[unknown-vs-empty]]
        와 같은 이유 — 못 닿은 알림을 '보냈다'로 굳히면 그날은 영영 조용하다).
        """
        try:
            _rss, avail_mb, peak_mb = self._health_memory()
            if not avail_mb:
                return          # 비리눅스 등 — 못 재는 것은 경보하지 않는다
            warn_at = float(getattr(config, 'SYSTEM_MEMORY_WARN_MB', 120))
            risk_at = float(getattr(config, 'SYSTEM_MEMORY_RISK_MB', 60))
            level = "위험" if avail_mb < risk_at else ("경고" if avail_mb < warn_at else None)
            if level is None:
                self._mem_alert_key = None      # 회복 — 다음 악화는 다시 알린다
                return

            key = (level, datetime.now().strftime("%Y-%m-%d"))
            if getattr(self, '_mem_alert_key', None) == key:
                return
            msg = (f"⚠️ [메모리 {level}] 가용 메모리 {avail_mb:,.0f}MB "
                   f"(문턱 {risk_at if level == '위험' else warn_at:,.0f}MB)\n"
                   f"프로세스 피크 {peak_mb:,.0f}MB · 방금 주기 직후 측정값입니다.\n"
                   f"이대로면 OOM 으로 프로세스가 종료될 수 있습니다 — 종료되면 손절·"
                   f"트레일링 감시가 함께 멈춥니다. 관심종목 수를 줄이거나 재기동을 검토하세요.")
            if _pkg().alert_delivered(msg, urgent=(level == "위험")):
                self._mem_alert_key = key
            self.log(msg.splitlines()[0])
        except Exception as e:      # noqa: BLE001 - 경보 실패가 매매 주기를 멈추지 않는다
            logger.debug(f"[Health] 메모리 경보 확인 실패: {e}")

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

    def is_market_open(self):
        """국내 정규장 운영 시간 확인 (공용 판정 함수 위임)"""
        return is_system_market_open()

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
                        else:
                            #  [Fix 2026-09-06] 이 줄이 분기 **밖**에 있어 위에서 무엇을
                            #   골랐든 항상 "한투증권(자동)"으로 덮였다. 위 두 분기의 주석이
                            #   막으려던 것이 정확히 이 오표시인데, 마지막 한 줄이 그것을
                            #   무력화했다. 관제 화면(1874줄)과 하트비트(scheduler)에서
                            #   같은 모양을 각각 고쳤는데 주기 로그인 여기가 남아 있었다 —
                            #   가상투자·토스로 띄워도 매 주기 로그가 '한투증권(자동)'이라
                            #   말한다. 파이(가상)와 맥북(실전)을 함께 돌리는 운용에서
                            #   로그가 계좌 성격을 거짓말하면 장애 때 실계좌부터 뒤지게 된다.
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

                        # [Fix 2026-09-20 · 백업 복원 리허설] 백업은 기동 시 한 번(위 start 경로)뿐이었다.
                        #  무중단 운용에서는 재기동이 없으니 마지막 백업이 기동일에 멈춘다 — 파이가 한 달
                        #  돌면 한 달 묵은 백업만 남는다. 날짜가 바뀔 때 하루치를 뜬다(같은 날은 건너뛴다).
                        _bk = db_manager.db.backup()
                        self.log(f"[DB 백업] {os.path.basename(_bk)}" if _bk
                                 else "[DB 백업] 실패 — 백업 없이 진행합니다(운영자 확인 필요)")

                        try:
                            acnt = config.session.auto_acnt_prdt_cd
                            
                            # [수정] 해외 자산 누락 방지
                            asset_data = account.get_asset_status_data(target_cano, acnt)
                            #  [Fix 2026-09-06] 이 자리만 검사가 없었다. 같은 값을 쓰는
                            #   기동 경로(488줄)와 주기 갱신 경로(4353줄)는 둘 다
                            #   is_plausible_baseline 을 지나는데, **날짜 변경 갱신**만
                            #   `> 0` 이었다. 이 값은 그날의 손실 한도 분모이자 사이징
                            #   기준이고, 한 번 저장되면 하루 종일 고정된다.
                            #   게다가 집계가 일부 실패해도 tot_asset 은 숫자로 나온다 —
                            #   실측: 국내 잔고 조회만 실패하면 -36%(주식비중만큼). 이
                            #   시스템의 노출 상한이 40%라 0.5배 문턱에는 영영 닿지
                            #   않는다. 그래서 비율이 아니라 **결손 표식**을 본다.
                            _degraded = list((asset_data or {}).get('degraded') or [])
                            _acc_key = f"{target_cano}-{acnt}"
                            _tot = (asset_data or {}).get('tot_asset', 0)
                            if _degraded:
                                self.log(f"[기준선] 자산 집계가 온전하지 않아 갱신을 "
                                         f"미룹니다(결손: {', '.join(_degraded)}). "
                                         f"다음 주기에 다시 측정합니다.")
                            elif _tot > 0 and not is_plausible_baseline(_acc_key, _tot):
                                self.log(f"[기준선] 새 시작 자산 {_tot:,}원이 직전 이력과 "
                                         f"너무 어긋나 채택하지 않습니다. 다음 주기에 "
                                         f"다시 측정합니다.")
                            elif asset_data and _tot > 0:
                                self.initial_asset = asset_data['tot_asset']
                                saved_ok = save_daily_initial_asset(f"{target_cano}-{acnt}", self.initial_asset)
                                today_str = datetime.now().strftime("%Y-%m-%d")
                                acc_str = f"{target_cano}-{acnt}"
                                db_ok = db_manager.db.save_daily_asset(today_str, acc_str, self.initial_asset)
                                self.log(f"[초기화 완료] 새로운 당일 시작 자산 갱신: {self.initial_asset:,}원"
                                         + ("" if saved_ok else " ([bold red]파일 저장 실패[/] — 재기동 시 기준선 소실)")
                                         + ("" if db_ok else " ([bold red]이력 저장 실패[/] — 내일 드로다운 기준에서 오늘이 빠짐)"))
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
                            elif "1600" <= now_time_str < "1610":
                                # [2026-09-14] 15:30~16:00 은 시장 없는 휴게라 종전 '15:30 재개' 분기는 도달 불가.
                                #  거래 종료 시간을 2000 으로 넓힌 경우 16:00 에 KRX 애프터마켓으로 재개한다.
                                self.log(f"📢 [애프터마켓 개시] KRX 애프터마켓(16:00~20:00) 거래를 재개합니다. ({datetime.now().strftime('%H:%M')})")
                                msg = "🔔 [애프터마켓 개시] KRX 애프터마켓(16:00~20:00) 거래를 재개합니다. (ETF/ETN 제외)"
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
                                    threading.Thread(target=utils.inherit_account_context(SystemScheduler().execute_daily_closing_report), daemon=True, name="DailyClosingReport").start()
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

                        # [동적 손절 캡] 지수 변동성 배율 — 시장 필터 설정과 무관하게 갱신한다(값은 하루 한 번
                        #  바뀐다). 매도·매수 검사보다 먼저 — 신규 진입·증액의 손절률이 이 캡을 본다.
                        self._update_vol_regime()

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
                        # [주기 설정 스냅샷 · 2026-10-16] 아래 세 단계는 시작 시점의 설정 한 벌로
                        #  판단한다 — 도중에 메뉴·텔레그램이 바꾼 값은 다음 주기에 반영된다
                        #  (config.cycle_snapshot 주석). 워커 풀은 inherit_account_context 로 물려받는다.
                        with config.cycle_snapshot():
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
                        # [추가] 에러 로그 꼬리 첨부 (1시간 쿨타임)
                        _attach_tail = now - self.last_emergency_alert_time > 3600
                        if _attach_tail:
                            log_tail = get_mystock_log_tail(20)
                            msg += f"\n\n📜 [최근 시스템 로그 (mystock.log)]\n```\n{log_tail}```"

                        #  [전달 확인 뒤 표식 · 2026-09-07] 종전에는 쿨타임(10분)과
                        #   _wait_alert_sent 를 **보내기 전에** 찍었다. 비동기 전송이라
                        #   실패가 예외로 오지 않으므로 못 닿아도 10분간 침묵하고,
                        #   더 나쁜 것은 _wait_alert_sent 가 True 로 남는다는 점이다 —
                        #   진입 알림은 못 갔는데 **복구 알림만** 나가서, 운영자는 있지도
                        #   않았던 장애의 복구를 통보받는다(짝이 어긋난다).
                        if _pkg().alert_delivered(msg, urgent=True):
                            self.last_wait_alert_time = now
                            self._wait_alert_sent = True
                            if _attach_tail:
                                self.last_emergency_alert_time = now

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
            #  [전달 확인 뒤 쿨다운 · 2026-09-07] 보내기 전에 찍으면, 비동기 전송이
            #   못 닿아도 그 쿨다운 동안 침묵한다. 본문이 "신규 매수는 계속되므로 원인
            #   확인이 필요합니다" 라 — 그 사이 매수는 계속 나간다.
            if _pkg().alert_delivered(
                    f"⚠️ [반복 오류] 연속 에러가 한도에 닿았으나 증권사 서버는 정상입니다.\n"
                    f"코드 쪽 오류로 보고 **대기하지 않습니다**(매도·손절 감시 유지).\n"
                    f"신규 매수는 계속되므로 원인 확인이 필요합니다.\n\n원인: {err_reason}",
                    urgent=True):
                self._code_error_alerted_at = now
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
                        #  복구 알림도 전달을 확인한 뒤에 짝을 푼다 — 못 닿았는데 풀어
                        #  버리면 다음 장애의 진입 알림만 남고 복구 통보는 영영 없다.
                        if _pkg().alert_delivered("✅ [서버 복구] KIS 서버가 정상화되었습니다.\n"
                                                  "자동매매를 재개합니다."):
                            self._wait_alert_sent = False
                    return
                else:
                    self.log("[장애 대기] 서버 여전히 응답 없음.")
            except Exception as e:
                self.log(f"[장애 대기] 점검 중 오류: {e}")

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

    def _get_stock_market_type(self, code):
        """종목 코드로 시장 구분(KOSPI/KOSDAQ) 확인 (인스턴스 캐시 사용)"""
        return _pkg().resolve_market_type(code, self.stock_market_map)
