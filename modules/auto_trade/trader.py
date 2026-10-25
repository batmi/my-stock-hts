# modules/auto_trade/trader.py
"""AutoTrader: 시스템 트레이딩 본체 — 싱글톤·상태 초기값과 책임별 믹스인의 결합 (분석→매수/매도→리포트)

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
주기 제어는 둘로 나눴다(2026-10-25 C1 6단계 — 같은 규약). 기동·정지(초기화·중복 실행 잠금·DB 점검·시작/종료 알림)는
lifecycle.TraderLifecycleMixin, 주 루프(한 주기·루프 예외와 서버 장애 대기·주기 계측·메모리 경보·정체 감시·DB 쓰기
실패 알림·마감 스냅샷)는 loop.TraderLoopMixin 이 들고 있다. 세션 비교 키·알림 억제 간격도 loop 로 옮겼고 여기서 다시
import 한다.
여기 남은 것은 싱글톤 생성과 상태 초기값(__new__), 여러 믹스인이 함께 쓰는 것들이다 — 로그(log)·장 열림 판정
(is_market_open)·방어 모드 전환(halt_buys·resume_buys)·지정가 제한폭 보정·시장 구분·계좌 키·거래 기록 정리.
"""
import threading
import logging
import os
from datetime import datetime
import config
from core import context # [추가]
import api
from core import utils
from modules.auto_trade import performance # 성과 보고 계산(순수 함수)
from modules.auto_trade.reporting import TraderReportingMixin  # 화면·보고(상태·관제·보고서) — AutoTrader 가 상속한다
from modules.auto_trade.market_risk import TraderMarketRiskMixin  # 시장·리스크(지수 상태·리스크 배수) — AutoTrader 가 상속한다
from modules.auto_trade.account_sync import TraderAccountSyncMixin  # 계좌 동기화(기준선·입출금·차단기) — AutoTrader 가 상속한다
from modules.auto_trade.selling import TraderSellingMixin  # 매도(판정 루프·손절 안전망·앵커) — AutoTrader 가 상속한다
from modules.auto_trade.buying import TraderBuyingMixin  # 매수(게이트·후보 분석·집행·피라미딩) — AutoTrader 가 상속한다
#  후보 정렬 키·오염 일봉 판정은 매수와 함께 buying 으로 옮겼다. trader.candidate_priority_key 로 부르는 곳
#   (테스트·감사 도구 문서)과 패키지 네임스페이스(modules.auto_trade.X)가 그대로 동작하게 다시 import 한다.
from modules.auto_trade.buying import candidate_priority_key, contaminated_chart_reason  # noqa: F401
from modules.auto_trade.lifecycle import TraderLifecycleMixin  # 기동·정지(초기화·잠금·DB 점검·알림) — AutoTrader 가 상속한다
from modules.auto_trade.loop import TraderLoopMixin  # 주 루프(한 주기·장애 대기·계측·경보) — AutoTrader 가 상속한다
#  주 루프만 쓰던 알림 억제 간격·세션 비교 키는 loop 로 옮겼다. trader.session_phase_key · trader.CODE_ERROR_ALERT_COOLDOWN
#   으로 부르는 곳(테스트)과 패키지 네임스페이스(modules.auto_trade.X)가 그대로 동작하게 다시 import 한다.
from modules.auto_trade.loop import (CODE_ERROR_ALERT_COOLDOWN, DB_WRITE_FAIL_ALERT_COOLDOWN,  # noqa: F401
                                     _IDLE_SESSION_PHASES, session_phase_key)

from modules.auto_trade.engine import DefaultStrategy, OrderManager, RiskManager
from modules.auto_trade.common import trade_account_key, is_system_market_open

console = config.console

logger = logging.getLogger(__name__)


def _pkg():
    """패키지(modules.auto_trade) 네임스페이스 접근자.

    분해 전에는 모듈 전역 조회였던 상호 호출을 패키지 속성 조회로 유지해,
    테스트의 patch('modules.auto_trade.X') 가 분해 전과 동일하게 내부 호출에도
    적용되도록 한다. (지연 import라 순환 없음)
    """
    import modules.auto_trade as _at
    return _at


class AutoTrader(TraderReportingMixin, TraderMarketRiskMixin, TraderAccountSyncMixin, TraderSellingMixin,
                 TraderBuyingMixin, TraderLifecycleMixin, TraderLoopMixin):
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
                # [안전장치] 리스크 배수 계산 연속 실패 횟수 — 실패하면 직전 배수가 굳는다(market_risk._update_risk_scale).
                cls._instance.risk_scale_fails = 0
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

    def _trade_account_key(self):
        return trade_account_key()

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

    def _get_stock_market_type(self, code):
        """종목 코드로 시장 구분(KOSPI/KOSDAQ) 확인 (인스턴스 캐시 사용)"""
        return _pkg().resolve_market_type(code, self.stock_market_map)
