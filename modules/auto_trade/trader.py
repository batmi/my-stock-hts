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
import jsonio
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
import context # [추가]
import api
import utils
import indicators
from modules import analysis, account # [수정] account 모듈 재사용
import math # [추가] math 모듈
from modules import db_manager # [추가] DB 매니저
from modules import chart # [추가] 차트 모듈
import re # [추가] 정규식 모듈
import pandas as pd

from modules.auto_trade.engine import (DefaultStrategy, OrderManager, RiskManager,
                                       UNMANAGED_ETF, UNMANAGED_RESTRICTED)
from modules.auto_trade.common import (_enrich_rules_with_weights, _get_trade_account, get_mystock_log_tail, get_restricted_stocks, is_single_price_break, is_system_market_open, load_daily_initial_asset, save_daily_initial_asset)

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


#  휴장 판정은 달력일 기준이라 자정에 뒤집힌다(일 23:59 'holiday' → 월 00:00 'closed').
#  둘 다 거래가 없는 같은 상태인데 문자열만 달라 자정마다 '시장 상태 변경' 알림이 나갔다
#  (실측 2026-08-03 00:00 "장 마감 · KRX 종가"). 알림 비교에서는 한 상태로 묶는다.
_IDLE_SESSION_PHASES = frozenset({'closed', 'holiday'})


def session_phase_key(phase):
    """세션 전환 알림용 비교 키. 거래 없는 단계(마감·휴장)는 하나로 접는다."""
    return 'idle' if phase in _IDLE_SESSION_PHASES else phase


def candidate_priority_key(c):
    """[추세추종] 매수 후보 우선순위 정렬 키 — 게이트(매수 점수 통과)와 랭킹을 분리한다.

    점수는 이진 신호 합산이라 동점이 흔하고 '추세의 강도·지속성'을 구분하지 못하므로,
    게이트를 통과한 후보끼리는 연속값인 추세 품질(회귀 모멘텀 = 연환산 기울기 × R²)로
    1차 정렬한다. R²가 '매끄러운 추세'를 우대해 급등락 끝에 우연히 정배열에 걸린
    약추세 종목을 뒤로 보낸다. 이력 부족(None)은 검증 불가로 보아 최하순위.
    (2순위 이하: 점수 → 52주 위치 → 체결강도)
    """
    tq = c.get('trend_quality')
    return (-(tq if tq is not None else float('-inf')),
            -c['score'], -(c.get('w52_pos') or 0.0), -(c.get('vol_strength') or 0.0))


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
            # 운영 관제용 수명주기 정보. /health는 외부 API를 추가 호출하지 않고
            # 이 값을 읽어 현재 루프의 생존성·최근 장애를 보여준다.
            cls._instance.last_cycle_at = None
            # [계측] 주기 소요 시간(초). 실제 청산 감시 간격 = 이 값 + SYSTEM_TRADING_INTERVAL
            cls._instance.last_cycle_secs = None
            cls._instance.cycle_secs_history = []   # 최근 값만 유지(라즈베리파이 메모리 고려)
            cls._instance.cycle_secs_peak = 0.0
            cls._instance.last_success_at = None
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
            cls._instance.last_emergency_alert_time = 0 # [추가] 긴급 알림 쿨타임용 타임스탬프
            cls._instance.last_wait_alert_time = 0    # [추가] 대기 모드 진입 알림 쿨타임 (진입/복구 반복 시 스팸 방지)
            cls._instance._wait_alert_sent = False    # [추가] 진입 알림 발송 여부 (복구 알림과 짝 맞춤)
            # [안전장치] 방어 모드 — 신규 매수(피라미딩 포함)만 중단하고 매도·손절 감시는 계속 돌린다.
            #  일일 손실 한도 초과 시 시스템 전체를 정지하던 기존 동작은, 정작 손절이 가장 필요한
            #  순간에 청산 엔진을 꺼버려 보유 포지션이 손절선 아래로 방치되는 문제가 있었다.
            cls._instance.buy_halted = False          # 방어 모드 활성 여부
            cls._instance.buy_halt_reason = ""        # 방어 모드 사유 (상태 표시용)
            cls._instance.buy_halt_date = None        # 방어 모드 발동 일자 (날짜 변경 시 자동 해제)
            cls._instance.unmanaged_stop_notified = {} # [안전장치] 자동매도 제외 포지션의 손절선 이탈 경보 스로틀 {code: ts}
            
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
            _init_total = 2 if config.session.is_simulation else 3
            task = progress.add_task("[cyan]자동매매 세션 초기화 중...[/cyan]", total=_init_total)
            
            target_cano = config.session.auto_cano if not config.session.is_simulation else config.session.cano
            acnt = config.session.auto_acnt_prdt_cd if not config.session.is_simulation else config.session.acnt_prdt_cd
            
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
                    progress.advance(task)
                    return "caches", (ts_cache, half_cache)

                with concurrent.futures.ThreadPoolExecutor(max_workers=3) as executor:
                    # [수정] 모의투자는 잔고 summary에 예수금이 포함되어 있어 별도 예수금 API 호출이 불필요.
                    # 초기화 시 중복 잔고조회(get_domestic_balance)+예수금조회가 2-TPS 경합으로 재시도
                    # 폭주를 일으켜 메모리가 폭증하던 문제를 제거한다. (실전만 별도 예수금 조회 수행)
                    futures = [executor.submit(_fetch_balance), executor.submit(_load_db_caches)]
                    if not config.session.is_simulation:
                        futures.append(executor.submit(_fetch_deposit))
                    for future in concurrent.futures.as_completed(futures):
                        key, value = future.result()
                        results[key] = value

                # 결과 처리
                holdings, summary = results.get("balance", (None, None))
                ts_cache, half_cache = results.get("caches", ({}, set()))

                if config.session.is_simulation:
                    # [수정] 모의투자: 잔고 summary에서 예수금 유도 (_run_loop와 동일 방식)
                    deposit_res = None
                    if summary:
                        dnca = api.safe_int(summary[0].get('dnca_tot_amt', 0))
                        d2_dep = api.safe_int(summary[0].get('prvs_rcdl_excc_amt', 0))
                        deposit_res = {'deposit': dnca, 'foreign_deposit': 0, 'd2_deposit': d2_dep}
                else:
                    deposit_res = results.get("deposit")

                if holdings is None or deposit_res is None:
                    raise Exception("자산/예수금 조회 실패 (API 응답 없음)")

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
                if config.session.is_simulation:
                    tot_asset = api.safe_int(summary[0].get('tot_evlu_amt', 0)) if summary else 0
                else:
                    asset_data = account.get_asset_status_data(target_cano, acnt)
                    tot_asset = asset_data.get('tot_asset', 0) if asset_data else 0

                if tot_asset > 0:
                    account_key = f"{target_cano}-{acnt}"
                    saved_initial = load_daily_initial_asset(account_key)
                    self.initial_asset = saved_initial if saved_initial > 0 else tot_asset
                    if saved_initial <= 0:
                        save_daily_initial_asset(account_key, self.initial_asset)
                        db_manager.db.save_daily_asset(datetime.now().strftime("%Y-%m-%d"), account_key, self.initial_asset)
                else:
                    self.initial_asset = 0

                self.initialized = True
                return True
        return False

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

            if interactive:
                console.print("\n[bold magenta]!!! 경고: 토스증권 실계좌에서 시스템 트레이딩을 시작합니다 !!![/bold magenta]")
                console.print(f"운용 계좌: [bold yellow]{config.session.cano}[/bold yellow] (토스증권, 실제 자산 거래)")
                utils.print_breadcrumb()
                if Prompt.ask("위 계좌로 실제 매매가 수행됩니다. 진행하시겠습니까?", choices=["y", "n"], default="n") != "y":
                    console.print("[yellow]시작을 취소했습니다.[/yellow]")
                    return
            else:
                if api._is_screen_output_allowed():
                    console.print("[bold cyan][시스템 명령] 토스증권 자동매매를 시작합니다.[/bold cyan]")
        elif not config.session.is_simulation:
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

        try:
            # [수정] 초기화 로직 분리
            if not self.initialized:
                if not self.initialize():
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
            ts_act = config.SELL_STRATEGY.get("TRAILING_STOP_ACTIVATION_RATE", 10.0)
            ts_call = config.SELL_STRATEGY.get("TRAILING_STOP_CALLBACK_RATE", 5.0)
            sell_score = config.SELL_STRATEGY["SELL_SCORE"]
            tp_rsi = config.SELL_STRATEGY["TAKE_PROFIT_RSI"]
            invest_ratio_str = config.format_invest_ratio()

            use_half_tp = config.SELL_STRATEGY.get("HALF_TAKE_PROFIT_USE", False)
            use_atr_stop = config.SELL_STRATEGY.get("USE_ATR_STOP", True)
            atr_mult = config.SELL_STRATEGY.get("ATR_STOP_MULTIPLIER", 2.0)
            use_time_stop = config.SELL_STRATEGY.get("TIME_STOP_USE", True)
            time_stop_days = config.SELL_STRATEGY.get("TIME_STOP_DAYS", 20)

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
            msg += f"\n• 트레일링: +{ts_act}% 도달 후 -{ts_call}%"
            if use_time_stop:
                msg += f"\n• 시간청산: {time_stop_days}일 경과"
            msg += f"\n• 비중: 종목당 {invest_ratio_str}"
                
            # [복원] 보유 종목 현황 추가
            valid_holdings = [h for h in holdings if int(h.get('hldg_qty', 0)) > 0] if holdings else []

            if valid_holdings:
                msg += "\n\n" + _pkg().format_holdings_block(valid_holdings)
            else:
                msg += "\n\n📋 [보유 종목] 없음"
                if stock_eval_amt > 0:
                    msg += " (⚠️ 평가금액 존재 - API 데이터 불일치)"

            target_cano = config.session.auto_cano if not config.session.is_simulation else config.session.cano
            with utils.AccountContext(target_cano):
                from modules.telegram_bot import TelegramCommander
                reply_markup = TelegramCommander()._get_default_keyboard()
                api.send_telegram_message(msg, reply_markup=reply_markup)

            # 초기화에 사용된 데이터는 비워줌
            self.initial_holdings = None
            self.initial_summary = None
            self.initialized = False

        except Exception as e:
            logger.error(f"자동매매 시작 실패: {e}")
            if api._is_screen_output_allowed():
                console.print(f"[bold red]자동매매 시작 실패: {e}[/bold red]")
                if config.SCREEN_DEBUG_LEVEL in ["ERROR", "TRACE", "DEBUG"]:
                    console.print(f"[bold red][ERROR] 자동매매 시작 실패: {e}[/bold red]")

            if self.initial_asset > 0:
                target_cano = config.session.auto_cano if not config.session.is_simulation else config.session.cano
                acnt = config.session.auto_acnt_prdt_cd if not config.session.is_simulation else config.session.acnt_prdt_cd
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

            target_cano = config.session.auto_cano if not config.session.is_simulation else config.session.cano
            with utils.AccountContext(target_cano):
                try:
                    # 1. 예수금 조회
                    acnt = config.session.auto_acnt_prdt_cd if not config.session.is_simulation else config.session.acnt_prdt_cd
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

                    if holdings:
                        valid_holdings = [h for h in holdings if int(h.get('hldg_qty', 0)) > 0]
                        stock_eval = sum(int(h['evlu_amt']) for h in valid_holdings)
                        tot_profit = sum(int(h['evlu_pfls_amt']) for h in valid_holdings)
                        tot_pchs = sum(int(int(h['hldg_qty']) * float(h['pchs_avg_pric'])) for h in valid_holdings)
                    elif summary and len(summary) > 0:
                        stock_eval = api.safe_int(summary[0].get('scts_evlu_amt', 0))
                        tot_profit = api.safe_int(summary[0].get('evlu_pfls_smtl_amt', 0))
                        tot_pchs = api.safe_int(summary[0].get('pchs_amt_smtl', 0))

                    if is_data_valid:
                        final_asset = deposit + stock_eval
                        profit = final_asset - self.initial_asset
                        profit_rate = 0.0 if self.initial_asset <= 0 else (profit / self.initial_asset) * 100
                        stock_rate = (tot_profit / tot_pchs * 100) if tot_pchs > 0 else 0.0

                        msg += f"\n최종 예수금: {deposit:,}원"
                        msg += f"\n증권 평가 자산: {stock_eval:,}원"
                        msg += f"\n증권 평가 손익: {tot_profit:+,}원 ({stock_rate:+.2f}%)"
                        msg += f"\n금일 최종 손익: {profit:+,}원 ({profit_rate:+.2f}%)"
                    else:
                        msg += "\n(⚠️ 종료 시 자산 정보 조회 실패 - 서버 응답 없음)"

                    # [추가] 금일 매매 요약 집계
                    buy_cnt = 0
                    sell_cnt = 0
                    best_stock = None
                    worst_stock = None
                    max_p = 0
                    min_p = 0
                    try:
                        today_str = datetime.now().strftime("%Y-%m-%d")
                        target_account = f"{config.session.cano}-{config.session.acnt_prdt_cd}" if config.session.is_simulation else (f"{config.session.auto_cano}-{config.session.auto_acnt_prdt_cd}" if config.session.auto_cano else None)
                        today_trades = db_manager.db.get_trades(start_date=today_str, end_date=today_str, is_sim=config.session.is_simulation, account=target_account)
                        
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
                        msg += "\n\n" + _pkg().format_holdings_block(valid_holdings, title="최종 보유 종목 현황")
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
            
        target_cano = config.session.auto_cano if not config.session.is_simulation else config.session.cano
        with utils.AccountContext(target_cano):
            from modules.telegram_bot import TelegramCommander
            reply_markup = TelegramCommander()._get_default_keyboard()
            api.send_telegram_message(msg, reply_markup=reply_markup)
        
        # [추가] 로거 연결 해제 (메시지 전송 후 해제)
        context.SYSTEM_LOGGER = None

    def halt_buys(self, reason, notify_msg=None):
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
        self.log(f"[방어 모드 해제] 신규 매수를 재개합니다. ({reason})")
        return True

    def log_current_holdings(self):
        """현재 보유 종목 현황을 조회하여 로그에 출력합니다 (체결 후 호출용)"""
        try:
            target_cano = config.session.auto_cano if not config.session.is_simulation else config.session.cano
            acnt = config.session.auto_acnt_prdt_cd if not config.session.is_simulation else config.session.acnt_prdt_cd
            
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

        target_cano = config.session.auto_cano if not config.session.is_simulation else config.session.cano
        with utils.AccountContext(target_cano):
            try:
                acnt = config.session.auto_acnt_prdt_cd if not config.session.is_simulation else config.session.acnt_prdt_cd
                
                # 1. 잔고 조회 (평가금 포함)
                holdings, summary = api.get_domestic_balance(target_cano, acnt)
                
                # 2. 예수금 및 총 자산 계산
                if config.session.is_simulation:
                    if summary:
                        # [수정] D+2 예수금(가수도금) 사용 (매도 대금 포함, /balance와 통일)
                        deposit = api.safe_int(summary[0].get('prvs_rcdl_excc_amt', 0))
                else:
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
                daily_profit = current_asset - self.initial_asset
                daily_profit_rate = (daily_profit / self.initial_asset) * 100
                msg += f"오늘 현재 손익: {daily_profit:+,}원 ({daily_profit_rate:+.2f}%)\n"
                
            realized_profit = 0
            try:
                today_str = datetime.now().strftime("%Y-%m-%d")
                
                target_account = None
                if config.session.is_simulation:
                    target_account = f"{config.session.cano}-{config.session.acnt_prdt_cd}"
                elif config.session.auto_cano:
                    target_account = f"{config.session.auto_cano}-{config.session.auto_acnt_prdt_cd}"
                    
                today_trades = db_manager.db.get_trades(
                    start_date=today_str, end_date=today_str, 
                    is_sim=config.session.is_simulation, account=target_account
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
            
            realized_rate = (realized_profit / self.initial_asset * 100) if self.initial_asset > 0 else 0.0
            msg += f"오늘 실현 손익: {realized_profit:+,}원 ({realized_rate:+.2f}%)\n"
            msg += f"증권 평가 자산: {tot_evlu:,}원\n"
            msg += f"증권 평가 손익: {tot_profit:+,}원 ({rate:+.2f}%)\n"
            msg += f"주문 가능 금액: {deposit:,}원\n"
        else:
            msg += "자산 정보 조회 실패\n"
            
        # [수정] 현재 시장 상황 정보
        msg += "\n[시장 상황]\n"
        # 하락축은 🔷(미확정) → 🔵(확정)로 단계를 표현 — 화면 색상(sky_blue3 → blue)과 동일 계열
        #  (하늘색 원형 이모지는 유니코드에 없어 밝은 파랑 마름모로 대체)
        emoji_map = {"Bull": "🔴", "PendUp": "🟠", "PendDown": "🔷", "Bear": "🔵", "Sideways": "🟡"}
        rp = config.MARKET_REGIME_PARAMS
        ema_desc = f"EMA {rp.get('REGIME_EMA_FAST', 9)}/{rp.get('REGIME_EMA_SLOW', 41)}"

        for m_type, label in [("KOSPI", "KOSPI"), ("KOSDAQ", "KOSDAQ")]:
            try:
                info = analysis.get_market_regime_detail(m_type)
                regime = info['regime']
                regime_str = f"{emoji_map.get(regime, '🟡')} {analysis.format_regime(regime, markup=False)}"
                msg += f"• {label}: {regime_str} (교차 후 {info['moved_pct']:+.1f}%, {ema_desc} 기준)\n"
            except Exception:
                msg += f"• {label}: 확인 불가\n"

        # [추가] 시장 지수 요약 정보 및 필터링 상태 (시장 상황 아래 배치)
        use_filter = getattr(config, 'USE_MARKET_FILTER', True)
        filter_str = "ON" if use_filter else "OFF"
        filter_ma = getattr(config, 'MARKET_FILTER_MA', 80)
        filter_band = getattr(config, 'MARKET_FILTER_BAND', 1.0)
        band_txt = f" ±{filter_band:g}%" if filter_band else ""
        msg += f"\n[시장 지수 및 필터링 (필터: {filter_str}, SMA {filter_ma}일{band_txt} 기준)]\n"
        
        is_healthy_k = True
        is_healthy_q = True

        try:
            for name, m_type in [("KOSPI", "KOSPI"), ("KOSDAQ", "KOSDAQ")]:
                df = analysis.get_domestic_index_data(m_type)
                if df is not None and not df.empty:
                    curr = df.iloc[-1]['close']
                    prev = df.iloc[-2]['close'] if len(df) > 1 else curr
                    rate = ((curr - prev) / prev) * 100
                    
                    filter_msg = ""
                    if use_filter:
                        # 시스템 루프의 상태 캐시(market_index_status)를 우선 적용하여 보류 카운트와 상태 불일치 방지
                        cached_stat = self.market_index_status.get(m_type)
                        
                        if isinstance(cached_stat, dict) and cached_stat.get('unknown'):
                            # [Fix] 판단 불가 = 매수 보류(fail-closed). '약세 보류'와 원인을 구분해 표시한다.
                            is_healthy = False
                            filter_msg = " [🚫보류(판단불가)]"
                        elif cached_stat and isinstance(cached_stat, dict) and cached_stat.get('current', 0) > 0:
                            is_healthy = cached_stat.get('is_healthy', True)
                            filter_msg = " [🟢허용]" if is_healthy else " [🚫보류]"
                        else:
                            # 대기 상태(WAITING) 등 캐시가 없을 때만 실시간 계산
                            #  (밴드 히스테리시스 포함 — 시스템 루프와 같은 판정식을 써야 표시가 어긋나지 않는다)
                            ma_period = getattr(config, 'MARKET_FILTER_MA', 80)
                            if len(df) >= ma_period:
                                is_healthy = not bool(indicators.get_market_filter_blocked(
                                    df['close'], ma_period,
                                    getattr(config, 'MARKET_FILTER_BAND', 1.0)).iloc[-1])
                                filter_msg = " [🟢허용]" if is_healthy else " [🚫보류]"
                            else:
                                is_healthy = False
                                filter_msg = " [🚫보류(데이터부족)]"
                                
                        if m_type == "KOSPI":
                            is_healthy_k = is_healthy
                        elif m_type == "KOSDAQ":
                            is_healthy_q = is_healthy
                            
                    msg += f"• {name}: {curr:,.2f} ({rate:+.2f}%){filter_msg}\n"
        except Exception: pass
        
        if use_filter:
            skip_k = self.skipped_by_market_filter_count.get("KOSPI", 0)
            skip_q = self.skipped_by_market_filter_count.get("KOSDAQ", 0)
            
            # [추가] 분석 루프가 돌지 않았을 경우(0건) stock.json 기준으로 실제 보류 대상 개수 산출
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
            msg += "\n" + _pkg().format_holdings_block(valid_holdings)
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
            interval = getattr(config, 'SYSTEM_TRADING_INTERVAL', 180)
            self.log(f"모니터링 완료 (소요 {secs:.1f}초 · 다음 주기까지 {interval}초 대기 "
                     f"→ 청산 감시 간격 {secs + interval:.0f}초). 대기 중...")

    def _health_cycle_text(self):
        """관제용 주기 소요 시간 문구와 '실제 감시 간격(초)'을 돌려준다.

        Returns: (표시 문자열, 감시 간격 초 또는 None)
        """
        last = getattr(self, 'last_cycle_secs', None)
        if last is None:
            return "미측정 (루프 1회 실행 후 표시)", None
        hist = getattr(self, 'cycle_secs_history', None) or [last]
        avg = sum(hist) / len(hist)
        interval = getattr(config, 'SYSTEM_TRADING_INTERVAL', 180)
        gap = avg + interval
        peak = getattr(self, 'cycle_secs_peak', 0.0) or last
        return (f"분석 {avg:.1f}초 (최근 {len(hist)}회 평균, 최대 {peak:.1f}초) + 대기 {interval}초 "
                f"= 청산 감시 간격 {gap:.0f}초", gap)

    @staticmethod
    def _health_memory():
        """프로세스/시스템 메모리 사용량(MB)을 추가 의존성 없이 조회한다.

        운영 환경(라즈베리파이 1GB)에서는 OOM이 자동매매 중단의 주된 원인이라
        관제 화면에서 상주 메모리와 가용 메모리를 함께 확인할 수 있게 한다.
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
            try:
                import resource
                usage = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
                # macOS는 바이트, 그 외 POSIX는 KB 단위로 반환한다.
                is_mac = getattr(os, "uname", None) and os.uname().sysname == "Darwin"
                rss_mb = usage / (1024 ** 2) if is_mac else usage / 1024
            except Exception:
                rss_mb = 0.0
        try:
            with open("/proc/meminfo") as fp:
                for line in fp:
                    if line.startswith("MemAvailable:"):
                        avail_mb = int(line.split()[1]) / 1024
                        break
        except Exception:
            avail_mb = 0.0
        return rss_mb, avail_mb

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
            target_account = (
                f"{config.session.cano}-{config.session.acnt_prdt_cd}"
                if config.session.is_simulation
                else f"{config.session.auto_cano}-{config.session.auto_acnt_prdt_cd}"
            )
            today_trades = db_manager.db.get_trades(
                start_date=now.strftime("%Y-%m-%d"), end_date=now.strftime("%Y-%m-%d"),
                is_sim=config.session.is_simulation, account=target_account,
            )
            refined = self._refine_trade_records(today_trades)
            today_order_count = len(refined)
            today_fill_count = sum("체결" in str(t.get("order_status") or "") for t in refined)
            today_cancel_count = sum("취소" in str(t.get("order_status") or "") for t in refined)
        except Exception:
            warnings.append("당일 주문 이력을 읽지 못했습니다")

        feed_text = "REST 폴링"
        try:
            import realtime
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

        account = config.session.cano if config.session.is_simulation else config.session.auto_cano
        if getattr(config.session, "is_paper", False):
            mode = "가상투자"
        elif getattr(config.session, "is_toss", False):
            mode = "토스 실전"
        elif config.session.is_simulation:
            mode = "KIS 모의"
        else:
            mode = "KIS 실전"

        if self.last_success_at and isinstance(self.last_success_at, datetime):
            age = (now - self.last_success_at).total_seconds()
            if self.is_running and age > max(120, getattr(config, "SYSTEM_TRADING_INTERVAL", 10) * 4):
                warnings.append(f"정상 루프 갱신 지연 {int(age)}초")
        elif self.is_running:
            warnings.append("아직 정상 루프 완료 기록이 없습니다")

        # 언제부터 돌고 있는지는 장애 판단의 기준 시각이라 관제 첫 화면에 함께 둔다.
        if self.is_running and self.start_time:
            elapsed = str(now - self.start_time).split(".")[0]
            run_text = f"{self.start_time.strftime('%Y-%m-%d %H:%M:%S')} (경과 {elapsed})"
        else:
            run_text = "미실행"

        rss_mb, avail_mb = self._health_memory()
        resource_parts = []
        if rss_mb:
            resource_parts.append(f"프로세스 메모리 {rss_mb:,.0f}MB")
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
            f"• Kill Switch: 자동매매 {errors}/{max_err} · 체결 감시 {monitor_errors}/{max_err}",
            f"• 주문 감시: 미체결 {pending_orders}건 · 예약 대기 {reserved_orders}건 · 오늘 주문/체결/취소 {today_order_count}/{today_fill_count}/{today_cancel_count}건",
            f"• 시세 연결: {feed_text}",
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
        target_cano = config.session.auto_cano if not config.session.is_simulation else config.session.cano
        with utils.AccountContext(target_cano):
            with Progress(
                SpinnerColumn(),
                TextColumn("[progress.description]{task.description}"),
                BarColumn(),
                console=console,
                transient=True
            ) as progress:
                task = progress.add_task("[cyan]자산/시장 정보 병렬 조회 중...[/cyan]", total=None)
                acnt = config.session.auto_acnt_prdt_cd if not config.session.is_simulation else config.session.acnt_prdt_cd

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
                            if config.session.is_simulation:
                                _deposit = api.safe_int(_summary[0].get('prvs_rcdl_excc_amt', 0))

                        # [수정] 실전 투자는 항상 상세 조회 시도 (정확도 우선)
                        if _deposit == 0 or not config.session.is_simulation:
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
                with concurrent.futures.ThreadPoolExecutor(max_workers=3) as executor:
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
                return f"[{color}]{current:,.0f} {trend_icon}[/]"
            
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
            if config.session.is_simulation:
                target_account = f"{config.session.cano}-{config.session.acnt_prdt_cd}"
            elif config.session.auto_cano:
                target_account = f"{config.session.auto_cano}-{config.session.auto_acnt_prdt_cd}"
            
            today_trades = db_manager.db.get_trades(
                start_date=today_str, end_date=today_str, 
                is_sim=config.session.is_simulation, account=target_account
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
                target_cano = config.session.auto_cano if not config.session.is_simulation else config.session.cano
                acnt = config.session.auto_acnt_prdt_cd if not config.session.is_simulation else config.session.acnt_prdt_cd
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
                
                daily_profit = current_asset - self.initial_asset
                daily_profit_rate = (daily_profit / self.initial_asset) * 100
                dp_color = "[red]" if daily_profit > 0 else ("[blue]" if daily_profit < 0 else "[white]")
                table.add_row("오늘 현재 손익", f"{dp_color}{daily_profit:+,}원 ({daily_profit_rate:+.2f}%)[/]")
                
                realized_rate = (today_profit / self.initial_asset) * 100
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
        ts_act = config.SELL_STRATEGY.get("TRAILING_STOP_ACTIVATION_RATE", 10.0)
        ts_call = config.SELL_STRATEGY.get("TRAILING_STOP_CALLBACK_RATE", 5.0)
        
        use_half_tp = config.SELL_STRATEGY.get("HALF_TAKE_PROFIT_USE", False)
        use_atr = config.SELL_STRATEGY.get("USE_ATR_STOP", True)
        atr_mult = config.SELL_STRATEGY.get("ATR_STOP_MULTIPLIER", 2.0)
        use_time_stop = config.SELL_STRATEGY.get("TIME_STOP_USE", True)
        time_stop_days = config.SELL_STRATEGY.get("TIME_STOP_DAYS", 20)
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
        
        table.add_row("", f"트레일링스탑 (+{ts_act}%/-{ts_call}%)")

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
                target_cano = config.session.auto_cano if not config.session.is_simulation else config.session.cano
                acnt = config.session.auto_acnt_prdt_cd if not config.session.is_simulation else config.session.acnt_prdt_cd
                if not config.session.is_simulation and not target_cano:
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
                if config.session.is_simulation:
                    target_account = f"{config.session.cano}-{config.session.acnt_prdt_cd}"
                elif config.session.auto_cano:
                    target_account = f"{config.session.auto_cano}-{config.session.auto_acnt_prdt_cd}"
            
            # [Fix] DBManager.get_trades가 account 인자를 지원하지 않는 경우 대비 (메모리 필터링)
            try:
                db_records = db_manager.db.get_trades(is_sim=config.session.is_simulation, limit=limit, start_date=start_date, account=target_account)
            except TypeError:
                db_records = db_manager.db.get_trades(is_sim=config.session.is_simulation, limit=limit, start_date=start_date)
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
        if config.session.is_simulation:
            target_account = f"{config.session.cano}-{config.session.acnt_prdt_cd}"
        elif config.session.auto_cano:
            target_account = f"{config.session.auto_cano}-{config.session.auto_acnt_prdt_cd}"
            
        # [Fix] DBManager.get_trades가 account 인자를 지원하지 않는 경우 대비
        try:
            db_records = db_manager.db.get_trades(is_sim=config.session.is_simulation, limit=limit, start_date=start_date, account=target_account)
        except TypeError:
            db_records = db_manager.db.get_trades(is_sim=config.session.is_simulation, limit=limit, start_date=start_date)
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
            target_cano = config.session.auto_cano if not config.session.is_simulation else config.session.cano
            with utils.AccountContext(target_cano):
                acnt = config.session.auto_acnt_prdt_cd if not config.session.is_simulation else config.session.acnt_prdt_cd
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
                target_cano = config.session.auto_cano if not config.session.is_simulation else config.session.cano
                target_acnt = config.session.auto_acnt_prdt_cd if not config.session.is_simulation else config.session.acnt_prdt_cd
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

    def _get_holdings_message(self, target_cano):
        """보유 종목 현황 메시지 생성 (장 시작/마감 알림용)"""
        msg = ""
        try:
            acnt = config.session.auto_acnt_prdt_cd if not config.session.is_simulation else config.session.acnt_prdt_cd
            holdings, _ = api.get_domestic_balance(target_cano, acnt)
            
            # [수정] 보유수량 0 초과인 종목만 필터링
            valid_holdings = [h for h in holdings if int(h.get('hldg_qty', 0)) > 0] if holdings else []

            if valid_holdings:
                msg += "\n\n" + _pkg().format_holdings_block(valid_holdings)
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
                target_cano = config.session.auto_cano if not config.session.is_simulation else config.session.cano
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
                        #        is_simulation만 보면 토스가 '한투증권(자동)'으로 오표시되므로 is_toss도 분기.
                        if config.session.is_toss:
                            acc_type = "토스증권"
                        elif config.session.is_simulation:
                            acc_type = "모의투자"
                        else:
                            acc_type = "한투증권(자동)"
                        self.log(f"운용 계좌: {target_cano} [{acc_type}]")
                    
                    
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
                            acnt = config.session.auto_acnt_prdt_cd if not config.session.is_simulation else config.session.acnt_prdt_cd
                            
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
                        acnt = config.session.auto_acnt_prdt_cd if not config.session.is_simulation else config.session.acnt_prdt_cd
                        
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
                        
                        # 2. 예수금 조회
                        # [최적화] 모의투자는 잔고 조회 결과(summary)에 예수금이 포함되어 있어 별도 호출 불필요
                        deposit_res = None
                        # [수정] 실전/모의 모두 summary 정보 우선 활용
                        if summary:
                            dnca = api.safe_int(summary[0].get('dnca_tot_amt', 0))
                            d2_dep = api.safe_int(summary[0].get('prvs_rcdl_excc_amt', 0))
                            deposit_res = {'deposit': dnca, 'foreign_deposit': 0, 'd2_deposit': d2_dep}
                        
                        # 예수금이 0이거나 실전투자에서 정밀 조회가 필요한 경우 Fallback
                        if (not deposit_res or deposit_res['deposit'] == 0) and not config.session.is_simulation:
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
                        # 2. 매수 조건 점검
                        self._check_buy_conditions(holdings, deposit_res, current_market_status, rules_map=_cycle_rules_map, restricted_stocks=_cycle_restricted)
                        # 3. 미체결 주문 관리 (오래된 주문 취소) - 장 중에만 수행
                        self.order_manager.manage_unfilled_orders()

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

                                if not config.session.is_simulation:
                                    upd_dep = api.get_deposit_balance(target_cano, acnt, skip_balance_check=True)
                                    if upd_dep: deposit_res = upd_dep
                                else:
                                    if summary:
                                        dnca = api.safe_int(summary[0].get('dnca_tot_amt', 0))
                                        d2_dep = api.safe_int(summary[0].get('prvs_rcdl_excc_amt', 0))
                                        deposit_res = {'deposit': dnca, 'foreign_deposit': 0, 'd2_deposit': d2_dep}
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

                interval = getattr(config, 'SYSTEM_TRADING_INTERVAL', 180)
                
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
                    
                    self._wait_for_server_recovery()
                    
                    # 복구되어 리턴되면 에러 카운트 초기화 후 루프 재개
                    self.consecutive_errors = 0
                    continue
                
                time.sleep(10)

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
                    target_cano = config.session.auto_cano if not config.session.is_simulation else config.session.cano
                    acnt_cd = config.session.auto_acnt_prdt_cd if not config.session.is_simulation else config.session.acnt_prdt_cd
                    
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
                            target_cano = config.session.auto_cano if not config.session.is_simulation else config.session.cano
                            acnt = config.session.auto_acnt_prdt_cd if not config.session.is_simulation else config.session.acnt_prdt_cd
                            acc_str = f"{target_cano}-{acnt}"
                            saved_initial = load_daily_initial_asset(acc_str)
                            if saved_initial > 0:
                                self.initial_asset = saved_initial
                                self.log(f"[시스템 보정] 기존 초기 자산 기록 로드: {self.initial_asset:,}원")
                            else:
                                self.initial_asset = current_total
                                save_daily_initial_asset(acc_str, self.initial_asset)
                                self.log(f"[시스템 보정] 초기 자산 정보 갱신 및 저장: {self.initial_asset:,}원")

                            # [추가] DB에 기록
                            try:
                                today_str = datetime.now().strftime("%Y-%m-%d")
                                db_manager.db.save_daily_asset(today_str, acc_str, self.initial_asset)
                            except Exception: pass

                        profit_rate = (total_profit / tot_pchs * 100) if tot_pchs > 0 else 0.0
                        
                        realized_profit = 0
                        try:
                            today_str = datetime.now().strftime("%Y-%m-%d")
                            
                            target_account = None
                            if config.session.is_simulation:
                                target_account = f"{config.session.cano}-{config.session.acnt_prdt_cd}"
                            elif config.session.auto_cano:
                                target_account = f"{config.session.auto_cano}-{config.session.auto_acnt_prdt_cd}"
                                
                            today_trades = db_manager.db.get_trades(
                                start_date=today_str, end_date=today_str, 
                                is_sim=config.session.is_simulation, account=target_account
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
                        except Exception:
                            pass
                            
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
                            self.baseline_principal = current_principal

                        if not is_first_init and toss_cash_reliable:
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
                                    self.log(f"💰 외부 예수금 {action_str} 자동 감지: {transfer_amt:+,}원")
                                    self.log(f"-> 시스템 오작동 방지를 위해 기준 자산을 동기화합니다. ({self.initial_asset:,} -> {self.initial_asset + int(transfer_amt):,})")

                                    self.initial_asset += int(transfer_amt)
                                    self.baseline_principal += int(transfer_amt)  # [추가] 입금 감지 기준 원금도 함께 이동

                                    account_key = f"{target_cano}-{acnt_cd}"
                                    save_daily_initial_asset(account_key, self.initial_asset)
                                    try:
                                        today_str = datetime.now().strftime("%Y-%m-%d")
                                        db_manager.db.save_daily_asset(today_str, account_key, self.initial_asset)
                                    except Exception: pass
                                    
                                    api.send_telegram_message(f"💰 [예수금 {action_str} 자동 감지]\n백그라운드 감시 결과, 계좌에 약 {abs(int(transfer_amt)):,}원의 {action_str}이 발생한 것을 확인했습니다.\n\n안전한 수익률 계산을 위해 시스템 기준 자산을 {self.initial_asset:,}원으로 스스로 자동 동기화했습니다.")
                                    
                                    # 처리 후 초기화
                                    self._pending_transfer_count = 0
                                    self._pending_transfer_amt = 0
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

                        # [리스크 스케일링] 최근 평가자산 갱신 (히트 캡 기준자산·드로다운 산출용)
                        # check_loss_limit과 동일하게 비정상 급감(API 누락 의심) 데이터는 반영하지 않는다.
                        if current_total > 0 and not (self.initial_asset > 0 and current_total < self.initial_asset * 0.5):
                            self.current_total_asset = current_total

                        self.risk_manager.check_loss_limit(current_total)
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
            cano = config.session.auto_cano if not config.session.is_simulation else config.session.cano
            acnt = config.session.auto_acnt_prdt_cd if not config.session.is_simulation else config.session.acnt_prdt_cd
            
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

    def _alert_unmanaged_stop(self, code, name, item, kind, buy_trades=None):
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
            profit_rate = float(item.get('evlu_pfls_rt') or 0.0)

            sl_rate = None
            tq, ws = 0, 0.0
            for t in (buy_trades or []):
                q = api.safe_int(t.get('qty', 0))
                try:
                    s = float(t.get('stop_loss_rate') or 0.0)
                except (TypeError, ValueError):
                    s = 0.0
                if q > 0 and s != 0.0:
                    tq += q
                    ws += q * s
            if tq > 0:
                sl_rate = ws / tq
            if sl_rate is None or sl_rate >= 0:
                sl_rate = config.SELL_STRATEGY.get("STOP_LOSS_RATE", -7.0)
            if sl_rate >= 0:
                return  # 손절 기준 자체가 없으면(0=미사용) 경보할 기준도 없다

            if profit_rate > sl_rate:
                # 손절선 위로 회복 — 다음 이탈 때 다시 알리도록 스로틀 해제
                self.unmanaged_stop_notified.pop(code, None)
                return

            now = time.time()
            last = self.unmanaged_stop_notified.get(code, 0)
            if now - last < 86400:
                return
            self.unmanaged_stop_notified[code] = now

            qty = api.safe_int(item.get('hldg_qty', 0))
            eval_amt = api.safe_int(item.get('evlu_amt', 0))
            loss_amt = api.safe_int(item.get('evlu_pfls_amt', 0))

            self.log(f"⚠️ [손절선 이탈 경보] {name}({code}): 수익률 {profit_rate:.2f}% ≤ 손절 기준 {sl_rate:.2f}% "
                     f"— {kind}이라 시스템이 자동 매도하지 않습니다.")
            api.send_telegram_message(
                f"⚠️ [손절선 이탈 — 자동매도 제외 종목]\n\n"
                f"종목: {name}({code})\n"
                f"수익률: {profit_rate:.2f}% (손절 기준: {sl_rate:.2f}%)\n"
                f"보유: {qty:,}주 / 평가금 {eval_amt:,}원 / 평가손익 {loss_amt:,}원\n\n"
                f"사유: {kind}으로 자동 매도 대상에서 제외되어 있어 시스템이 손절하지 않습니다.\n"
                f"직접 청산 여부를 판단해 주세요. (동일 종목 재알림은 24시간 후)")
        except Exception as e:
            logger.debug(f"[손절선 이탈 경보] {code} 처리 실패: {e}")

    def _check_sell_conditions(self, holdings, is_market_open=True, rules_map=None, restricted_stocks=None):
        # [WS] 시스템 트레이딩 종목을 실시간 피드에 최우선으로 등록한다.
        #  보유종목(포지션, 최우선) → 매수후보 순서로 priority. 매수후보는 국내주식 + (ETF 포함 설정 시)국내 ETF.
        #  ETF 미포함 설정이면 ETF는 시스템 대상이 아니므로 '그 외(other) 로테이션'으로 둔다.
        try:
            import realtime
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
        latest_buy_map = db_manager.db.get_latest_buy_trades(_all_hold_codes)
        buy_trades_map = db_manager.db.get_buy_trades_for_current_holdings(_all_hold_codes)
        # 진입일(보유수량이 0 → 1 이상이 된 시점) — 시간청산 기준
        entry_date_map = db_manager.db.get_position_entry_dates(_all_hold_codes)

        # [추가] 포트폴리오 히트(총 오픈 리스크) 스냅샷 갱신 — 같은 주기의 피라미딩/신규 매수 캡 판정에 사용
        try:
            self.portfolio_heat_amt = self.risk_manager.compute_portfolio_heat(holdings, buy_trades_map)
        except Exception:
            self.portfolio_heat_amt = 0.0

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
        tps = config.SIM_TX_PER_SECOND if config.session.is_simulation else config.REAL_TX_PER_SECOND
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
                self._alert_unmanaged_stop(code, name, item, UNMANAGED_RESTRICTED,
                                           buy_trades_map.get(code))
                return
            
            if self.order_manager.is_pending(code):
                self.set_stock_state(code, None)
                if config.FILE_DEBUG_LEVEL == "DEBUG": self.log(f"[분석스킵] {name}: 진행 중인 주문 존재")
                return

            # [추가] 대체거래소(NXT) 운영 시간에는 ETF 및 NXT 비거래 종목 매도 스킵
            now_time = datetime.now().strftime("%H%M")
            is_nxt_market = ("1530" <= now_time <= "2000") or ("0800" <= now_time <= "0850")
            is_overseas_stock = not (len(code) == 6 and code[0].isdigit() and code.isalnum())
            
            # [수정] ETF 판정을 관심목록뿐 아니라 종목명 휴리스틱까지 포함하도록 일원화
            #  (보유만 하고 관심목록에 없는 ETF/ETN도 식별)
            is_domestic_etf = (not is_overseas_stock) and api.is_domestic_etf_etn(code, name)

            if is_nxt_market and not is_overseas_stock:
                if is_domestic_etf or (hasattr(api, 'is_nxt_tradeable') and not api.is_nxt_tradeable(code)):
                    self.set_stock_state(code, None)
                    return

            # [추가] ETF 포함 여부가 False면 보유 ETF는 자동 매도 대상에서도 제외한다.
            #  (SYSTEM_INCLUDE_ETF는 매수 필터이지만, 사용자 요청에 따라 매도도 제외하여
            #   ETF는 전적으로 수동 관리하도록 한다. 단 시스템이 손절하지 않으므로 주의)
            if is_domestic_etf and not getattr(config, 'SYSTEM_INCLUDE_ETF', False):
                self.set_stock_state(code, None)
                self.log(f"[매도스킵] {name}({code}): {UNMANAGED_ETF}")
                # [Fix] 시스템이 손절하지 않는 포지션이므로 손절선 이탈 시 경보는 발송한다.
                self._alert_unmanaged_stop(code, name, item, UNMANAGED_ETF,
                                           buy_trades_map.get(code))
                return

            qty = api.safe_int(item.get('ord_psbl_qty'))
            profit_rate = float(item['evlu_pfls_rt'])
            current_price = float(item['prpr'])
            buy_price = float(item['pchs_avg_pric'])
            
            time.sleep(safe_delay)
            
            if not self.is_running: return # 대기 후 재확인
            
            if qty <= 0: 
                self.set_stock_state(code, None)
                if config.FILE_DEBUG_LEVEL == "DEBUG": self.log(f"[분석스킵] {name}: 주문 가능 수량 0")
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

            with self._lock:
                cached_highest = self.trailing_stop_cache.get(code)
                if cached_highest is None:
                    val = db_manager.db.get_highest_price(code)
                    cached_highest = val if val is not None else 0.0
                    self.trailing_stop_cache[code] = cached_highest
            
            highest_price = cached_highest
            
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
                order_price = int(utils.adjust_to_tick(raw_order_price, is_overseas=False))
                if order_price <= 0: order_price = int(current_price)

                if not is_market_open:
                    self.log(f"[장마감] 매도 신호 감지 (주문 미전송): {name} - {reason}")
                    return

                real_qty = api.fetch_sellable_quantity(code)
                if real_qty < target_sell_qty:
                    if real_qty > 0:
                        self.log(f"매도 수량 조정: {name} {target_sell_qty}주 -> {real_qty}주")
                        target_sell_qty = real_qty
                    else:
                        self.log(f"매도 중단: {name} 주문 가능 수량 부족 (미체결 존재 가능성)")
                        return

                self.log(f"매도 실행: {name} - {reason}")
                odno = self.order_manager.send_order(code, target_sell_qty, "sell", name=name, profit_amt=int(item['evlu_pfls_amt']), profit_rate=profit_rate, reason=reason, score=score, price=order_price, rule=rule)
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
                        target_cano = config.session.auto_cano if not config.session.is_simulation else config.session.cano
                        target_acnt = config.session.auto_acnt_prdt_cd if not config.session.is_simulation else config.session.acnt_prdt_cd
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
        max_workers = 5 if not config.session.is_simulation else 2
        with concurrent.futures.ThreadPoolExecutor(max_workers=max_workers) as executor:
            futures = [executor.submit(_sell_worker, item) for item in holdings]
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

            # 증액 횟수 판별: 최근 매수 사유의 '피라미딩 N차' 마커 (DB 기록이라 재시작에도 유지)
            pyramid_count = 0
            if last_buy:
                m = re.search(r'피라미딩\s*(\d+)차', str(last_buy.get('reason', '')))
                if m:
                    pyramid_count = int(m.group(1))

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
            order_price = int(utils.adjust_to_tick(raw_order_price, is_overseas=False))
            if order_price <= 0:
                order_price = int(current_price)

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
                sl_rate = -((atr_val * atr_mult / current_price) * 100)
                max_atr_sl = config.SELL_STRATEGY.get("MAX_ATR_STOP_LOSS_RATE", -15.0)
                if max_atr_sl != 0 and sl_rate < max_atr_sl:
                    sl_rate = max_atr_sl

            # [추가] 포트폴리오 히트 캡: 증액분 리스크가 남은 예산을 넘으면 피라미딩 보류.
            #  (_sell_worker 스레드 동시 실행 대비, 예산 확인과 선점을 락으로 원자화)
            add_risk = (add_qty * order_price) * (abs(sl_rate) / 100.0) if sl_rate < 0 else 0.0
            reserved_heat = False
            if add_risk > 0:
                with self._lock:
                    budget_left = self.risk_manager.portfolio_risk_budget_left()
                    if budget_left is not None:
                        if add_risk > budget_left:
                            cap = self.risk_manager.effective_portfolio_cap()
                            self.log(f"피라미딩 보류: {name} - 포트폴리오 총 리스크 한도({cap:.1f}%) 초과 "
                                     f"(증액 리스크 {add_risk:,.0f}원 > 남은 예산 {max(budget_left, 0):,.0f}원)")
                            return
                        self.portfolio_heat_amt += add_risk
                        reserved_heat = True

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

        if len(holding_codes) >= max_holdings:
            if self.consecutive_errors == 0: # 로그 도배 방지
                self.log(f"매수 스킵: 최대 보유 종목 수({max_holdings}개) 도달 (투자비중 {config.format_invest_ratio()} 기준)")
            return

        # 예수금 확인 (API 직접 호출)
        avail_cash = 0
        if deposit_res:
            avail_cash = deposit_res['d2_deposit'] # 주문 가능 금액은 D+2 예수금 기준
        else:
            return # 조회 실패 시 매수 중단

        # [수정] 최소 주문 가능 금액 하향 조정 (50,000 -> 1,000) 및 로그 추가
        min_cash = 1000
        if avail_cash < min_cash:
            if self.consecutive_errors == 0: # 로그 도배 방지
                 self.log(f"매수 스킵: 예수금 부족 ({avail_cash:,}원 < {min_cash:,}원)")
            return 
            
        # [추가] 개별 룰 로드 ([최적화] 루프에서 주기당 1회 로드해 전달받으면 재조회 생략)
        if rules_map is None:
            custom_rules = db_manager.db.get_all_stock_strategies()
            custom_rules = _enrich_rules_with_weights(custom_rules) # [추가] 가중치 보강
            rules_map = {r['code']: r for r in custom_rules}

        # [추가] 당일 매도 이력 확인 및 재진입 허들(체결강도) 설정
        today_str = datetime.now().strftime("%Y-%m-%d")
        target_account = None
        if config.session.is_simulation:
            target_account = f"{config.session.cano}-{config.session.acnt_prdt_cd}"
        elif config.session.auto_cano:
            target_account = f"{config.session.auto_cano}-{config.session.auto_acnt_prdt_cd}"
            
        try:
            today_trades = db_manager.db.get_trades(start_date=today_str, end_date=today_str, is_sim=config.session.is_simulation, account=target_account)
        except TypeError:
            today_trades = db_manager.db.get_trades(start_date=today_str, end_date=today_str, is_sim=config.session.is_simulation)
            if target_account:
                today_trades = [t for t in today_trades if t.get('account') == target_account]
                
        sold_today = set(t['code'] for t in today_trades if "sell" in t.get('type', '').lower() or "매도" in t.get('type', ''))
        
        reentry_hurdles = {}
        # [최적화] 당일 매도 종목의 최근 매수 내역을 배치 쿼리로 일괄 조회
        _sold_latest_buys = db_manager.db.get_latest_buy_trades(sold_today)
        for scode in sold_today:
            last_buy = _sold_latest_buys.get(scode)
            if last_buy:
                reason = last_buy.get('reason', '')
                match = re.search(r'체결강도:\s*([0-9.]+)%', reason)
                if match:
                    reentry_hurdles[scode] = float(match.group(1))
                else:
                    reentry_hurdles[scode] = config.ANALYSIS_THRESHOLDS.get("BUY_VOL_STRENGTH", 100.0)

        # 1. 후보 분석
        candidates = self._analyze_candidates(targets, holding_codes, rules_map, reentry_hurdles, holding_names_map, holding_groups_map, restricted_stocks=restricted_stocks)
        
        # 2. 매수 집행
        if candidates:
            if not is_market_open:
                self.log(f"[장마감] 매수 후보 감지 (주문 미전송): {len(candidates)}종목")
                for cand in candidates:
                     self.log(f"   - {cand['name']} ({cand['score']}점)")
                return

            self._execute_buy_orders(candidates, avail_cash, invest_ratio, len(holding_codes), max_holdings)

    def _analyze_candidate_worker(self, item, holding_codes, rules_map, restricted_stocks, market_regime_adj, safe_delay, reentry_hurdles, holdings_dfs, holding_groups_map, io_pool=None):
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
            now_time = datetime.now().strftime("%H%M")
            is_nxt_market = ("1530" <= now_time <= "2000") or ("0800" <= now_time <= "0850")
            is_overseas_stock = not (len(code) == 6 and code[0].isdigit() and code.isalnum())
            
            if is_nxt_market and not is_overseas_stock:
                is_etf = item.get('group') == 'etfs_kr'
                if is_etf or (hasattr(api, 'is_nxt_tradeable') and not api.is_nxt_tradeable(code)):
                    self.set_stock_state(code, None)
                    return {'type': 'log_only', 'log': f"[NXT스킵] {name}({code}): 대체거래소(NXT) 거래 불가 종목(ETF 포함)으로 분석을 스킵합니다."}
            
            # 2. 진행 중인 주문 체크
            if self.order_manager.is_pending(code):
                self.set_stock_state(code, None)
                return None

            # 3. 보유 종목 체크
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
                _local_pool = concurrent.futures.ThreadPoolExecutor(max_workers=3)
                ex = _local_pool
            try:
                fut_chart = ex.submit(api.get_chart_data, code, is_overseas=is_overseas_stock)
                fut_vol = ex.submit(api.get_realtime_vol_strength, code) if not is_overseas_stock else None
                fut_ab = ex.submit(api.get_ask_bid_ratio, code, is_overseas_stock) if _need_ob else None

                df = fut_chart.result()
                try: vol_strength = fut_vol.result() if fut_vol else None
                except Exception: vol_strength = None
                try: ask_bid_ratio = fut_ab.result() if fut_ab else None
                except Exception: ask_bid_ratio = None
            finally:
                if _local_pool is not None:
                    _local_pool.shutdown(wait=False)

            if df is None or df.empty:
                self.set_stock_state(code, None)
                return None
            
            # [수정] 캐시된 차트 데이터의 당일 미확정 종가를 실시간 최신 현재가로 업데이트
            # (종목 분석 메뉴와 시스템 트레이딩 간의 지표 및 점수 불일치 원천 차단)
            realtime_price = 0.0
            try:
                realtime_price = api.get_current_price(code, is_overseas=is_overseas_stock)
                # 모든 장 종료 후에는 지표용 봉을 갱신하지 않는다(KRX 확정 종가 유지).
                indicators.apply_realtime_price(df, api.chart_overlay_price(realtime_price, is_overseas_stock))
            except Exception: pass

            # [주문가 보호] 지표는 KRX 확정 종가로 계산하되, 매수 주문 단가는 항상 실시간가를 쓴다.
            #  이 값이 _execute_buy_orders의 cand['price'] → 주문 지정가가 되므로, NXT 시간대에
            #  KRX 종가로 굳으면 호가에서 벗어나 체결되지 않는다.
            current_price = float(realtime_price) if realtime_price and realtime_price > 0 else float(df.iloc[-1]['close'])

            # [추가] 상관계수 필터링
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
                            log_msg = f"[상관관계 보류] {name}({code}): 보유 종목 '{hold_name}'과 높은 상관관계 (상관계수: {corr:.2f} >= {corr_threshold})"
                            self.set_stock_state(code, None)
                            return {'type': 'correlation_skip', 'name': name, 'log': log_msg}

            # [추세추종] 상대강도(RS) 게이트: 소속 지수(KOSPI/KOSDAQ)보다 약한 종목의 신규 진입 차단.
            #   같은 +15%라도 지수가 +20%인 장에서는 열등주 — 지수 대비 초과수익이 없는 종목은
            #   '확실한 추세'가 아니라고 보고 게이트에서 제외한다 (약추세 진입 = 큰 손실의 원천).
            #   룩백은 RS_FILTER_LOOKBACK(>0) 우선, 0이면 스코어링 '가격 모멘텀'과 동일(MOMENTUM_LOOKBACK).
            #   종목 이력 부족·지수 조회 실패 시에는 통과(fail-open — 데이터 장애가 매수 전면 중단으로 번지지 않게).
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
                            self.set_stock_state(code, None)
                            return {'type': 'rs_skip', 'name': name,
                                    'log': f"[RS필터] {name}({code}): {mom_lb}일 수익률 {stock_mom:+.1f}% ≤ 지수 {idx_mom:+.1f}% (지수 대비 약세)"}

            # 룰 및 임계값 설정
            rule = rules_map.get(code)
            market_type = self._get_stock_market_type(code)
            score_adj = market_regime_adj.get(market_type, 0.0)
            base_buy_score = config.ANALYSIS_THRESHOLDS["BUY_SCORE"]
            
            thresholds = None
            if rule:
                thresholds = {
                    "BUY_SCORE": rule['buy_score'], # [수정] 개별 룰은 시장 보정 무시 (절대값)
                    "BUY_RSI_MAX": rule['buy_rsi'],
                    "BUY_VOL_STRENGTH": rule.get('buy_vol_strength', config.ANALYSIS_THRESHOLDS.get("BUY_VOL_STRENGTH", 100.0)),
                    "BUY_ASK_BID_RATIO": rule.get('buy_ask_bid_ratio', config.ANALYSIS_THRESHOLDS.get("BUY_ASK_BID_RATIO", 1.0)),
                    "AUTO_ADJUST_ASK_BID_RATIO": bool(rule.get('auto_adjust_ask_bid_ratio', config.ANALYSIS_THRESHOLDS.get("AUTO_ADJUST_ASK_BID_RATIO", True))),
                    "WEIGHTS": rule.get('weights')
                }
            else:
                thresholds = {
                    "BUY_SCORE": base_buy_score + score_adj,
                    "WEIGHTS": config.SCORING_WEIGHTS
                }
            
            # 전략 실행
            result = self.strategy.analyze_buy(code, name, df, current_price, vol_strength=vol_strength, thresholds=thresholds, ask_bid_ratio=ask_bid_ratio)
            if not result:
                self.set_stock_state(code, None)
                return None
                
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
            
            # [추가] 가짜 체결강도로 걸러진 경우 사유 표시
            vol_reject_msg = ""
            if result.get('vol_reject_reason'):
                vol_reject_msg = f" [{result['vol_reject_reason']}]"
            elif result.get('ask_bid_ratio') is not None:
                vol_reject_msg = f" [매도비:{result['ask_bid_ratio']:.2f}]"
            
            log_msg = f"[분석] {name}({code}): 현재가={current_price:,.0f}, 점수={result['score']}, 상태={result['state']}, RSI={rsi_val}, ADX={adx_val}, CCI={cci_val}, OBV={obv_str}, SM={sm_str}, SAR={sar_str}, 체결={vol_val}{rule_msg}{vol_reject_msg}"
            
            if result['action'] == "buy":
                reentry_msg = ""
                if code in reentry_hurdles:
                    req_vol = reentry_hurdles[code]
                    vol_strength_val = result.get('vol_strength')
                    if config.session.is_toss:
                        # [추가] 토스: 체결강도 미제공 → 매도잔량비(ask_bid_ratio)로 당일 재진입 판단
                        abr = result.get('ask_bid_ratio')
                        min_abr = result.get('min_ask_bid_ratio', 0) or 0
                        if abr is None or (min_abr > 0 and abr < min_abr):
                            log_msg = f"[분석스킵] {name}({code}): 당일 재진입 불가 (매도비 {abr if abr is not None else 0:.2f} < {min_abr:.2f})"
                            return {'type': 'log_only', 'log': log_msg}
                        else:
                            reentry_msg = f"당일 재진입(매도비 {abr:.2f})"
                    elif vol_strength_val is None or vol_strength_val <= req_vol:
                        log_msg = f"[분석스킵] {name}({code}): 당일 재진입 불가 (체결강도 {vol_strength_val if vol_strength_val else 0:.1f}% <= 기존매수 {req_vol:.1f}%)"
                        return {'type': 'log_only', 'log': log_msg}
                    else:
                        reentry_msg = f"당일 재진입(기존 {req_vol:.1f}% 경신)"

                candidate_data = {
                    'code': code, 'name': name, 'price': current_price,
                    'score': result['score'], 'rsi': result['rsi'], 'adx': result['adx'], 'cci': result['cci'], 'atr': result.get('atr', 0), 'vol_strength': result.get('vol_strength'),
                    'w52_pos': result.get('w52_pos', 0.0),  # [추세추종] 52주 위치 (우선순위 정렬용)
                    'trend_quality': result.get('trend_quality'),  # [추세추종] 추세 품질(회귀 모멘텀, 랭킹 1순위 키)
                    'ask_bid_ratio': result.get('ask_bid_ratio'),  # [추가] 토스 수급 지표(체결강도 대체)
                    'is_custom_rule': bool(rule), 'rule': rule, 'state': result['state'],
                    'state_reason': result.get('state_reason', ''),
                    'reentry_msg': reentry_msg
                }
                reentry_log = f" [{reentry_msg}]" if reentry_msg else ""
                log_msg = f"[분석] {name}({code}): 현재가={current_price:,.0f}, 점수={result['score']}, 상태={result['state']}, RSI={rsi_val}, ADX={adx_val}, CCI={cci_val}, OBV={obv_str}, SM={sm_str}, SAR={sar_str}, 체결={vol_val}{rule_msg}{vol_reject_msg}{reentry_log}"
                return {'type': 'candidate', 'data': candidate_data, 'log': log_msg}
            else:
                return {'type': 'log_only', 'log': log_msg}
        except Exception: return None

    def _analyze_candidates(self, targets, holding_codes, rules_map, reentry_hurdles, holding_names_map, holding_groups_map, restricted_stocks=None):
        candidates = []
        skipped_stocks = []
        restricted_skipped_stocks = [] # [추가] 트레이딩 제한 스킵 리스트
        correlation_skipped_stocks = [] # [추가] 상관관계 스킵 리스트
        rs_skipped_stocks = [] # [추세추종] 상대강도(RS) 필터 스킵 리스트

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
        tps = config.SIM_TX_PER_SECOND if config.session.is_simulation else config.REAL_TX_PER_SECOND
        safe_delay = (1.0 / tps) * 0.1

        # [병렬 처리] 사용자 작업과의 충돌 및 모의투자 API 제한(2 TPS) 고려
        # (실전: 5개, 모의: 2개 - ThrottledSession이 병목 없이 안전하게 제어함)
        max_workers = 5 if not config.session.is_simulation else 2

        # [최적화] 워커 내부의 차트/체결강도/호가 동시 조회용 I/O 풀을 공유
        #  (기존에는 후보 종목마다 ThreadPoolExecutor(3)를 생성/파괴 — 저사양 환경에서 오버헤드)
        io_pool = concurrent.futures.ThreadPoolExecutor(max_workers=max_workers * 3, thread_name_prefix="cand_io")
        try:
            with concurrent.futures.ThreadPoolExecutor(max_workers=max_workers) as executor:
                futures = [executor.submit(self._analyze_candidate_worker, item, holding_codes, rules_map, restricted_stocks, market_regime_adj, safe_delay, reentry_hurdles, holdings_dfs, holding_groups_map, io_pool=io_pool) for item in targets]

                for future in concurrent.futures.as_completed(futures):
                    if not self.is_running: break
                    res = future.result()
                    if res:
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
        finally:
            io_pool.shutdown(wait=False)

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

        # [추세추종] 우선순위 정렬 — 추세 품질(회귀 모멘텀) 1순위 (근거는 candidate_priority_key docstring)
        candidates.sort(key=candidate_priority_key)

        # [추가] 선정된 후보군 우선순위 로그 출력
        if candidates:
            lookback = config.INDICATOR_PARAMS.get("TREND_QUALITY_LOOKBACK", 90)
            self.log(f"[매수 후보 선정] 총 {len(candidates)}종목 (우선순위순) "
                     f"— 추세품질 = 최근 {lookback}일 회귀 '연환산 기울기(%) × R²(추세 매끄러움)'. "
                     f"높을수록 검증된 추세이며 동점 후보 간 1순위 정렬 키다(매수 게이트 아님)")
            for i, c in enumerate(candidates):
                tq = c.get('trend_quality')
                tq_disp = f"{tq:.0f} ({indicators.describe_trend_quality(tq)})" if tq is not None else "- (이력부족)"
                w52_disp = f"{c['w52_pos']:.0f}%" if c.get('w52_pos') else "-"
                vol_disp = f"{c['vol_strength']:.1f}%" if c.get('vol_strength') else "-"
                self.log(f"   {i+1}순위: {c['name']} (추세품질:{tq_disp}, 점수:{c['score']}, 52주위치:{w52_disp}, 체결:{vol_disp})")
        
        return candidates


    def _execute_buy_orders(self, candidates, avail_cash, invest_ratio, current_holdings_count, max_holdings):
        for cand in candidates:
            if not self.is_running: break
            
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
                # [수정] ATR 기반 동적 손절률 계산 (음수 값)
                stop_distance = atr_val * atr_mult
                sl_rate = -((stop_distance / price_val) * 100)

                # [추가] ATR 손절률 최대 한도 설정 (데이터 오류 등으로 인한 과도한 리스크 방지)
                max_atr_sl = config.SELL_STRATEGY.get("MAX_ATR_STOP_LOSS_RATE", -15.0)
                if max_atr_sl != 0 and sl_rate < max_atr_sl:
                    self.log(f"[리스크 조정] ATR 손절률({sl_rate:.1f}%)이 최대 한도({max_atr_sl}%)를 초과하여 조정됩니다.")
                    sl_rate = max_atr_sl

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
            order_price = int(utils.adjust_to_tick(raw_order_price, is_overseas=False))

            # 최소 주문 금액 보정 (할당된 예산이 1주 가격보다 적을 때 가용 예수금 전체를 쓰는 버그 방지)
            if invest_amt < order_price: invest_amt = order_price

            # [추가] 포트폴리오 히트 캡: 보유 전체 오픈 리스크 + 신규 리스크가 한도를 넘으면 축소/보류.
            #  종목당 한도(SYSTEM_RISK_PER_TRADE)와 별개로 '동시 다발 손절' 합산 손실을 통제한다.
            #  손절률이 없는(>=0) 경우 리스크 추정이 불가하므로 게이트를 건너뛴다(allocate_budget과 동일 기조).
            if sl_rate and sl_rate < 0:
                budget_left = self.risk_manager.portfolio_risk_budget_left()
                if budget_left is not None:
                    cap = self.risk_manager.effective_portfolio_cap()
                    new_risk = invest_amt * (abs(sl_rate) / 100.0)
                    if new_risk > budget_left:
                        allowed_amt = int(max(budget_left, 0) / (abs(sl_rate) / 100.0))
                        if allowed_amt < order_price:
                            self.log(f"매수 보류: {cand['name']} - 포트폴리오 총 리스크 한도({cap:.1f}%) 도달 "
                                     f"(현재 오픈 리스크 {self.portfolio_heat_amt:,.0f}원, 남은 예산 {max(budget_left, 0):,.0f}원)")
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
            
            if use_atr_stop:
                if atr_msg: atr_msg += " "
                atr_msg += f"[ATR손절:{sl_rate:.0f}%]"
            
            if atr_msg:
                reason += f" {atr_msg}"

            # [Fix] 신규 포지션 매수 전 이전 보유분의 잔존 상태 초기화.
            #  외부(MTS/HTS) 전량 매도는 엔진 매도 경로를 거치지 않아 트레일링 최고가·반익절
            #  DB 기록이 남는데(최고가 UPSERT는 단조 증가), 재시작 후 재매수 시 잔존 최고가로
            #  max_profit이 과대 계산되어 매수 직후 BEP/TS가 오발동(신규 포지션 즉시 청산)하는
            #  것을 방지한다. (후보군은 보유 종목을 제외하므로 이 시점은 항상 신규 포지션)
            db_manager.db.delete_trailing_stop(cand['code'])
            db_manager.db.delete_half_tp(cand['code'])
            with self._lock:
                self.trailing_stop_cache.pop(cand['code'], None)

            self.log(f"매수 실행: {cand['name']} - {reason}")
            # [수정] 매수 시 사유와 점수, 그리고 지정가 가격을 DB 저장을 위해 전달
            odno = self.order_manager.send_order(cand['code'], qty, "buy", name=cand['name'], reason=reason, score=cand['score'], price=order_price, rule=cand.get('rule'), stop_loss_rate=sl_rate)
            if odno:
                # [추가] 매수 주문 성공 시 대기 중인 예약 매수 취소 방어 로직 (중복 진입 방지)
                target_cano = config.session.auto_cano if not config.session.is_simulation else config.session.cano
                target_acnt = config.session.auto_acnt_prdt_cd if not config.session.is_simulation else config.session.acnt_prdt_cd
                canceled_cnt = db_manager.db.cancel_reserved_buy_orders(target_cano, target_acnt, cand['code'])
                if canceled_cnt > 0:
                    self.log(f"[예약취소] 신규 매수로 인해 대기 중이던 {cand['name']} 매수 예약 주문 {canceled_cnt}건 자동 취소")
                    api.send_telegram_message(f"🗑 [예약 취소] {cand['name']}({cand['code']}) 신규 매수로 인해 대기 중이던 매수 예약 주문 {canceled_cnt}건이 자동 취소되었습니다.")
                
                self.half_tp_cache.discard(cand['code']) # 신규 매수 시 기존 반익절 캐시 방어적 초기화
                avail_cash -= (qty * order_price)
                current_holdings_count += 1 # [추가] 보유 종목 수 증가 반영
                # [추가] 히트 캡 스냅샷에 신규 포지션 리스크 반영 (같은 주기의 후순위 후보 판정용)
                if sl_rate and sl_rate < 0:
                    with self._lock:
                        self.portfolio_heat_amt += (qty * order_price) * (abs(sl_rate) / 100.0)
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
                    self.market_index_status[market_name] = {"is_healthy": False, "unknown": True, "current": 0}
                    self._notify_market_unknown(market_name, notify)
                    continue

                # [이탈 확인 밴드] 상태 기계는 가격 이력만의 함수라 전 구간에서 재계산한다.
                #  재기동해도 상태가 유실되지 않고, 백테스트(prepare_market_filter)와 같은 값을 본다.
                current_idx = df['close'].iloc[-1]
                is_healthy = not bool(indicators.get_market_filter_blocked(
                    df['close'], ma_period, band_pct).iloc[-1])

                self.market_index_status[market_name] = {
                    "is_healthy": is_healthy,
                    "unknown": False,
                    "current": current_idx
                }

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
                self.market_index_status[market_name] = {"is_healthy": False, "unknown": True, "current": 0}
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
        api.send_telegram_message(
            f"⚠️ [판단 보류] {market_name} 지수 데이터를 확인할 수 없습니다.\n"
            f"시장 방향을 알 수 없으므로 해당 시장 종목의 신규 매수를 보류합니다.\n"
            f"(보유 종목의 손절·트레일링 스탑 감시는 계속됩니다)")
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

        hwm = max(getattr(self, '_hwm_cache', 0.0), float(self.initial_asset or 0))
        if hwm <= 0:
            return 0.0
        return max(0.0, (hwm - equity) / hwm * 100.0)

    def _get_stock_market_type(self, code):
        """종목 코드로 시장 구분(KOSPI/KOSDAQ) 확인 (인스턴스 캐시 사용)"""
        return _pkg().resolve_market_type(code, self.stock_market_map)
