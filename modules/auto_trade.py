import threading
import logging
import time
import requests
import json
import os
from datetime import datetime, timedelta
from collections import Counter
from rich.prompt import Prompt
from rich.markup import escape
from rich.table import Table
from rich import box
import config
import api
import utils
import indicators
from modules import analysis, account # [수정] account 모듈 재사용
from modules import db_manager # [추가] DB 매니저
from modules import chart # [추가] 차트 모듈
import re # [추가] 정규식 모듈

console = config.console

logger = logging.getLogger(__name__)

class ConclusionMonitor:
    _instance = None
    
    def __new__(cls):
        if cls._instance is None:
            cls._instance = super(ConclusionMonitor, cls).__new__(cls)
            cls._instance.is_running = False
            cls._instance.thread = None
            cls._instance.order_status = {} # 주문별 체결 수량 추적 {계좌-주문번호: qty}
            
            # [수정] 적응형 폴링 설정 로드
            cls._instance.active_interval = getattr(config, 'CONCLUSION_CHECK_INTERVAL', 2)
            cls._instance.idle_interval = getattr(config, 'CONCLUSION_CHECK_IDLE_INTERVAL', 180)
            cls._instance.active_duration = getattr(config, 'CONCLUSION_CHECK_ACTIVE_DURATION', 60)
            cls._instance.active_until = 0 # 집중 감시 유지 만료 시간
            
            cls._instance.event = threading.Event() # 즉시 실행 트리거용
            cls._instance.initialized = False # [추가] 초기화 여부
        return cls._instance

    def start(self):
        if self.is_running: return
        
        self.is_running = True
        self.thread = threading.Thread(target=self._run_loop, daemon=True, name="ConclusionMonitor")
        self.thread.start()

    def stop(self):
        self.is_running = False
        self.event.set() # 대기 해제
        if self.thread:
            self.thread.join(timeout=2)

    def check_now(self):
        """즉시 체결 확인 요청"""
        # [추가] 주문 발생 시 집중 감시 모드 시간 연장
        self.active_until = time.time() + self.active_duration
        self.event.set()

    def _is_market_open(self):
        """국내 정규장 운영 시간 확인"""
        now = datetime.now()
        if now.weekday() > 4: return False # 주말
        current_time = now.strftime("%H%M")
        start_time = getattr(config, 'SYSTEM_TRADING_START_TIME', "0915")
        end_time = getattr(config, 'SYSTEM_TRADING_END_TIME', "1515")
        return start_time <= current_time <= end_time

    def _run_loop(self):
        self.was_active_mode = False
        
        # [수정] 프로그램 시작 직후 API 요청 집중 방지를 위한 초기 지연 (설정값의 3배)
        initial_delay = getattr(config, 'CONCLUSION_CHECK_INTERVAL', 5) * 3
        time.sleep(initial_delay)
        
        # [이동] 초기화 로직을 스레드 내부에서 수행 (메인 스레드와 부하 분산)
        if not self.initialized:
            try:
                self._check_conclusions(initial=True)
                self.initialized = True
            except Exception as e:
                logger.error(f"체결 감시 초기화 중 오류: {e}")

        while self.is_running:
            # [추가] 장 운영 시간 외에는 모니터링 중단 (트래픽 감소)
            if not self._is_market_open():
                self.event.wait(60)
                if not self.event.is_set():
                    continue

            # [추가] 현재 모드(Active/Idle)에 따른 주기 결정
            is_active_mode = time.time() < self.active_until
            
            if is_active_mode != self.was_active_mode:
                if is_active_mode:
                    logger.debug(f"[Monitor] 집중 감시 모드 진입 (주기: {self.active_interval}초)")
                else:
                    logger.debug(f"[Monitor] 대기 모드 복귀 (주기: {self.idle_interval}초)")
                self.was_active_mode = is_active_mode
            
            if is_active_mode:
                wait_time = self.active_interval
            else:
                wait_time = self.idle_interval
            
            # Idle 주기가 0이면(비활성), 이벤트가 올 때까지 무한 대기 (트래픽 0)
            if not is_active_mode and wait_time <= 0:
                self.event.wait() 
                self.event.clear()
                continue

            is_rate_limited = False
            has_error = False # [추가] 에러 발생 여부 플래그
            try:
                is_rate_limited, has_error = self._check_conclusions() # [수정] 반환값 변경
            except Exception as e:
                logger.error(f"체결 감시 중 오류: {e}")
                has_error = True
            
            # [추가] Rate Limit 감지 시 호출 간격 자동 조절
            if is_rate_limited:
                wait_time = min(wait_time * 2.0, 60.0) # 최대 60초까지 증가
                logger.warning(f"[Monitor] API 호출 제한(Rate Limit) 감지. 대기 시간을 {wait_time:.1f}초로 조정합니다.")
            elif has_error:
                # [추가] 서버 에러(OPSQ2000 등) 발생 시 대기 시간을 늘려 로그 도배 방지
                wait_time = max(wait_time, 20.0)
                logger.debug(f"[Monitor] 체결 확인 중 에러 발생. 대기 시간을 {wait_time:.1f}초로 조정합니다.")
            
            # interval 만큼 대기하되, event가 설정되면 즉시 깨어남
            self.event.wait(wait_time)
            self.event.clear()

    def _check_conclusions(self, initial=False):
        """금일 체결 내역을 확인하고 로그에 기록 (모든 활성 계좌 대상)"""
        rate_limit_hit = False
        has_error = False # [추가]
        try:
            # 모니터링 대상 계좌 목록 구성
            accounts_to_check = []
            
            # 1. 메인 계좌 (수동 매매용)
            if config.session.cano and config.session.acnt_prdt_cd:
                accounts_to_check.append({
                    "cano": config.session.cano,
                    "acnt": config.session.acnt_prdt_cd,
                    "type": "MAIN"
                })
            
            # 2. 자동매매 계좌 (실전 모드이고 별도 설정된 경우)
            if not config.session.is_simulation and config.session.auto_cano and config.session.auto_acnt_prdt_cd:
                if config.session.auto_cano != config.session.cano or config.session.auto_acnt_prdt_cd != config.session.acnt_prdt_cd:
                    accounts_to_check.append({
                        "cano": config.session.auto_cano,
                        "acnt": config.session.auto_acnt_prdt_cd,
                        "type": "AUTO"
                    })
            
            # 중복 제거
            unique_accounts = []
            seen = set()
            for acc in accounts_to_check:
                key = (acc['cano'], acc['acnt'])
                if key not in seen:
                    seen.add(key)
                    unique_accounts.append(acc)
            
            for acc in unique_accounts:
                cano = acc['cano']
                acnt = acc['acnt']
                
                try:
                    # [최적화] retries=0 설정: 모니터링 루프가 주기적으로 돌기 때문에
                    # API 내부에서 blocking 재시도를 하지 않고 즉시 실패 처리(Fail Fast)하여 다음 주기로 넘김
                    data = api.get_today_history(cano, acnt, retries=0)
                    
                    if data.get('msg_cd') == 'EGW00201':
                        rate_limit_hit = True
                    
                    # [추가] API 호출 실패(RT_CD != 0) 감지
                    if data.get('rt_cd') != '0':
                        has_error = True
                    
                    if data.get('rt_cd') == '0':
                        trades = data.get('output1', [])
                        for item in trades:
                            odno = item.get('odno')
                            if not odno: continue
                            
                            tot_ccld_qty = api.safe_int(item.get('tot_ccld_qty'))
                            if tot_ccld_qty <= 0: continue
                            
                            order_key = f"{cano}-{odno}"
                            prev_qty = self.order_status.get(order_key, 0)
                            
                            if tot_ccld_qty > prev_qty:
                                new_qty = tot_ccld_qty - prev_qty
                                avg_price = float(item.get('avg_prvs', 0))
                                name = item.get('prdt_name')
                                code = item.get('pdno')
                                type_name = item.get('sll_buy_dvsn_cd_name')
                                
                                # [추가] 매매일시 정보 추출 (DB 저장용)
                                ord_dt = item.get('ord_dt', '')
                                ord_tmd = item.get('ord_tmd', '')
                                trade_time_str = None
                                if len(ord_dt) == 8 and len(ord_tmd) == 6:
                                    trade_time_str = f"{ord_dt[:4]}-{ord_dt[4:6]}-{ord_dt[6:]} {ord_tmd[:2]}:{ord_tmd[2:4]}:{ord_tmd[4:]}"
                                
                                # 원 주문 유형 조회 (수동/자동 태그 반영)
                                try:
                                    origin_trade = db_manager.db.get_trade_by_odno(odno)
                                    db_type_name = type_name
                                    profit_amt = 0
                                    profit_rate = 0.0
                                    score = 0
                                    if origin_trade:
                                        db_type_name = origin_trade['type']
                                        profit_amt = origin_trade.get('profit_amt', 0)
                                        profit_rate = origin_trade.get('profit_rate', 0.0)
                                        score = origin_trade.get('strategy_score', 0)
                                except Exception:
                                    db_type_name = type_name
                                
                                # [추가] 매도 체결 시 실현 손익 및 사유 조회
                                profit_msg = ""
                                reason_msg = ""
                                if "매도" in type_name:
                                    try:
                                        found_record = None
                                        # 1. AutoTrader 메모리 검색 (클래스가 정의된 경우)
                                        trader_cls = globals().get('AutoTrader')
                                        if trader_cls:
                                            trader = trader_cls()
                                            for record in reversed(trader.trade_records):
                                                if str(record.get('odno')) == str(odno):
                                                    found_record = record
                                                    break
                                        # 2. DB 검색 (메모리에 없을 경우)
                                        if not found_record:
                                            trades = db_manager.db.get_trades(limit=30)
                                            for t in trades:
                                                if str(t.get('odno')) == str(odno):
                                                    found_record = t
                                                    break
                                        if found_record:
                                            if found_record.get('profit_amt') is not None:
                                                profit_msg = f"\n손익: {int(found_record['profit_amt']):+,}원 ({float(found_record.get('profit_rate', 0)):+.2f}%)"
                                            if found_record.get('reason'):
                                                reason_msg = f"\n사유: {found_record['reason']}"
                                    except: pass

                                # [수정] 초기화 단계가 아닐 때만 알림 및 로그 수행
                                if not initial:
                                    # [추가] 현재가 및 등락률 조회 (알림용)
                                    cur_info = ""
                                    try:
                                        # 체결 확인은 주로 국내 주식 대상
                                        cp_data = api.get_current_price_data(code, is_overseas=False)
                                        if cp_data.get('rt_cd') == '0':
                                            curr = float(cp_data['output']['stck_prpr'])
                                            rate = float(cp_data['output']['prdy_ctrt'])
                                            icon = "🔺" if rate > 0 else ("🔻" if rate < 0 else "➖")
                                            cur_info = f"\n현재가: {int(curr):,}원 ({icon} {rate:+.2f}%)"
                                    except: pass
                                    
                                    # [추가] 매수 체결 시 전략 점수 및 지표 추가
                                    strategy_info = ""
                                    if type_name and "매수" in type_name:
                                        try:
                                            is_overseas_stock = not (code.isdigit() and len(code) == 6)
                                            df = api.get_chart_data(code, is_overseas=is_overseas_stock)
                                            if df is not None and not df.empty:
                                                ind = indicators.calculate_indicators(df)
                                                current_price = float(df.iloc[-1]['close'])
                                                
                                                # [추가] prev_rsi 계산 (상태 분류용)
                                                delta = df['close'].diff()
                                                gain = delta.where(delta > 0, 0).ewm(com=13, adjust=False).mean()
                                                loss = -delta.where(delta < 0, 0).ewm(com=13, adjust=False).mean()
                                                prev_rsi = (100 - (100 / (1 + gain/loss))).iloc[-2] if len(df) >= 16 else None

                                                # [수정] 상태 및 사유 조회
                                                state, _, state_reason = analysis.classify_stock_state(
                                                    current_price, ind['ema_20'], ind['ema_60'], ind['ema_120'], 
                                                    ind['psar'], ind['rsi'], prev_rsi, ind['adx'], ind['cci'], ind.get('obv_trend')
                                                )

                                                score, _ = analysis.calculate_score(
                                                    current_price, ind['ema_20'], ind['ema_60'], ind['ema_120'], 
                                                    ind['psar'], ind['rsi'], ind['adx'], ind['cci'], ind.get('obv_trend')
                                                )
                                                
                                                rsi_str = f"{ind['rsi']:.1f}" if ind['rsi'] is not None else "-"
                                                adx_str = f"{ind['adx']:.1f}" if ind['adx'] is not None else "-"
                                                cci_str = f"{ind['cci']:.1f}" if ind['cci'] is not None else "-"
                                                
                                                strategy_info = f"\n\n📊 [전략 지표]\n• 점수: {score}점 ({state})\n• 상태: {state_reason}\n• RSI: {rsi_str} / ADX: {adx_str} / CCI: {cci_str}"
                                        except Exception as e:
                                            logger.error(f"체결 지표 계산 중 오류: {e}")

                                    # 알림 발송
                                    msg = f"✅ [체결 알림] {type_name} {name}({code})\n수량: {new_qty}주 / 단가: {avg_price:,.0f}원{profit_msg}{reason_msg}{cur_info}{strategy_info}"
                                    with utils.AccountContext(cano):
                                        api.send_telegram_message(msg)
                                    
                                    # 로그 기록 (시스템 로거 활용)
                                    if config.SYSTEM_LOGGER:
                                        config.SYSTEM_LOGGER(f"[체결 확인] {type_name} {name}({code}) {new_qty}주 (단가: {avg_price:,.0f}원)")
                                    
                                else:
                                    logger.debug(f"[Init] 체결 내역 동기화: {name} {tot_ccld_qty}주 (ODNO: {odno})")
                                
                                # 상태 업데이트
                                self.order_status[order_key] = tot_ccld_qty
                                
                                # DB 저장
                                if not db_manager.db.check_trade_exists(odno, "체결"):
                                    if config.FILE_DEBUG_LEVEL == "DEBUG":
                                        logger.debug(f"[AutoTrade] 신규 체결 DB 저장 시도: {odno} ({name})")
                                    
                                    db_manager.db.insert_trade(db_type_name, code, name, tot_ccld_qty, avg_price, odno, order_status="체결", reason="체결 확인", custom_time=trade_time_str, profit_amt=profit_amt, profit_rate=profit_rate, score=score)
                                    
                                    # [추가] 시장가 주문 등의 경우를 위해 원 주문(접수)의 단가도 체결가로 업데이트
                                    db_manager.db.update_trade(odno, price=avg_price)
                                else:
                                    logger.debug(f"[AutoTrade] 이미 존재하는 체결 내역(체결)입니다. 저장 스킵 (ODNO: {odno})")
                except Exception as e:
                    logger.error(f"계좌({cano}) 체결 확인 중 오류: {e}")
                    has_error = True
        except Exception as e:
            logger.error(f"체결 확인 중 오류 발생: {e}")
            has_error = True
        return rate_limit_hit, has_error

class DefaultStrategy:
    """기본 매매 전략 클래스 (매수/매도 판단 로직 분리)"""
    def __init__(self):
        self.trailing_stop_cache = {}

    def analyze_buy(self, code, name, df, current_price):
        """매수 진입 여부 판단"""
        if df is None or df.empty:
            return None

        ind = indicators.calculate_indicators(df)
        # 전일 RSI 계산 (상태 분류용)
        delta = df['close'].diff()
        gain = delta.where(delta > 0, 0).ewm(com=13, adjust=False).mean()
        loss = -delta.where(delta < 0, 0).ewm(com=13, adjust=False).mean()
        prev_rsi = (100 - (100 / (1 + gain/loss))).iloc[-2] if len(df) >= 16 else None

        state, _, state_reason = analysis.classify_stock_state(
            current_price, ind['ema_20'], ind['ema_60'], ind['ema_120'], 
            ind['psar'], ind['rsi'], prev_rsi, ind['adx'], ind['cci'], ind.get('obv_trend')
        )
        
        score, _ = analysis.calculate_score(current_price, ind['ema_20'], ind['ema_60'], ind['ema_120'], ind['psar'], ind['rsi'], ind['adx'], ind['cci'], ind.get('obv_trend'))
        
        return {
            'action': 'buy' if state == "매수" else 'wait',
            'state': state,
            'score': score,
            'rsi': ind['rsi'],
            'adx': ind['adx'],
            'cci': ind['cci']
        }

    def analyze_sell(self, code, name, df, current_price, buy_price, profit_rate, ts_msg=""):
        """매도 청산 여부 판단"""
        reason = ""
        ind = {}
        score = 0
        state = ""
        
        # 1. 고정 익절/손절
        if profit_rate >= config.SELL_STRATEGY["TAKE_PROFIT_RATE"]:
            reason = f"익절({profit_rate}%)"
        elif profit_rate <= config.SELL_STRATEGY["STOP_LOSS_RATE"]:
            reason = f"손절({profit_rate}%)"
        # 2. 트레일링 스탑 (외부에서 계산된 메시지 반영)
        elif ts_msg:
            reason = ts_msg
        
        # 3. 기술적 지표 분석
        if df is not None and not df.empty:
            ind = indicators.calculate_indicators(df)
            # 전일 RSI 계산 (상태 분류용)
            delta = df['close'].diff()
            gain = delta.where(delta > 0, 0).ewm(com=13, adjust=False).mean()
            loss = -delta.where(delta < 0, 0).ewm(com=13, adjust=False).mean()
            prev_rsi = (100 - (100 / (1 + gain/loss))).iloc[-2] if len(df) >= 16 else None

            state, _, state_reason = analysis.classify_stock_state(
                current_price, ind['ema_20'], ind['ema_60'], ind['ema_120'], 
                ind['psar'], ind['rsi'], prev_rsi, ind['adx'], ind['cci'], ind.get('obv_trend')
            )
            score, _ = analysis.calculate_score(current_price, ind['ema_20'], ind['ema_60'], ind['ema_120'], ind['psar'], ind['rsi'], ind['adx'], ind['cci'], ind.get('obv_trend'))

            # 4. RSI 과열 익절
            if not reason and ind.get('rsi') is not None and ind['rsi'] > config.SELL_STRATEGY["TAKE_PROFIT_RSI"]:
                reason = f"RSI 과열 익절 (RSI: {ind['rsi']:.1f})"
            
            # 5. 추세 이탈
            if not reason and (state == "위험" or score < config.SELL_STRATEGY["SELL_SCORE"]):
                rsi_val = f"{ind.get('rsi'):.1f}" if ind.get('rsi') is not None else "-"
                adx_val = f"{ind.get('adx'):.1f}" if ind.get('adx') is not None else "-"
                cci_val = f"{ind.get('cci'):.1f}" if ind.get('cci') is not None else "-"
                if state == "위험":
                    reason = f"위험진입({state_reason}) [점수:{score}, RSI:{rsi_val}]"
                else:
                    reason = f"추세이탈({state}/점수하락) [점수:{score}, RSI:{rsi_val}, ADX:{adx_val}, CCI:{cci_val}]"
            
        return {
            'action': 'sell' if reason else 'hold',
            'reason': reason,
            'ind': ind,
            'score': score,
            'state': state
        }


class AutoTrader:
    _instance = None
    
    def __new__(cls):
        if cls._instance is None:
            cls._instance = super(AutoTrader, cls).__new__(cls)
            cls._instance.is_running = False
            cls._instance.thread = None
            cls._instance.logs = []
            cls._instance.trade_history = []
            cls._instance.trade_records = []
            cls._instance.start_time = None
            cls._instance.consecutive_errors = 0
            cls._instance.initial_asset = 0
            cls._instance.was_market_open = None
            cls._instance.trailing_stop_cache = {} # [추가] 트레일링 스탑 메모리 캐시 (DB 부하 감소용)
            cls._instance.market_status_notified = {} # [수정] 시장 상태 알림 플래그 (시장별 관리)
            cls._instance.market_index_status = {}    # [추가] 지수 상태 캐시
            cls._instance.stock_market_map = {}       # [추가] 종목별 시장 구분 캐시
            cls._instance.skipped_by_market_filter_count = 0 # [추가] 시장 필터링 보류 종목 수
            cls._instance.strategy = DefaultStrategy() # [추가] 전략 인스턴스
            cls._instance.last_log_date = datetime.now().date() # [추가] 로그 파일 날짜 추적용
            cls._instance.initial_holdings = None # [추가] 초기 조회 잔고 캐시
            cls._instance.initial_summary = None  # [추가] 초기 조회 요약 캐시
            cls._instance.file_logger = config.get_autotrade_logger() # [추가] 파일 로거 초기화
            
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

    def start(self, interactive=True):
        if self.is_running:
            console.print("\n[yellow]이미 자동매매가 실행 중입니다.[/yellow]")
            return
        
        # [추가] 로그 파일 생성을 보장하기 위해 시작 즉시 로그 기록
        self.log("=== 자동매매 시스템 시작 프로세스 진입 ===")
        
        # [수정] 실전 모드일 경우 자동매매 전용 계좌 설정 확인
        if not config.session.is_simulation:
            if not config.session.auto_app_key or not config.session.auto_cano:
                console.print("[bold red]오류: 실전 투자 모드에서 시스템 트레이딩을 실행하려면 별도의 자동매매 계좌 설정이 필요합니다.[/bold red]")
                console.print("[dim]환경 변수 AUTO_APP_KEY, AUTO_APP_SECRET, AUTO_ACC_NUM을 설정해주세요.[/dim]")
                return
            
            console.print("\n[bold red]!!! 경고: 실전 투자 모드에서 시스템 트레이딩을 시작합니다 !!![/bold red]")
            console.print(f"운용 계좌: [bold yellow]{config.session.auto_cano}-{config.session.auto_acnt_prdt_cd}[/bold yellow] (시스템 트레이딩 전용)")
            
            if interactive:
                if Prompt.ask("위 계좌로 실제 매매가 수행됩니다. 진행하시겠습니까?", choices=["y", "n"], default="n") != "y":
                    console.print("[yellow]시작을 취소했습니다.[/yellow]")
                    return
            else:
                console.print("[bold cyan][텔레그램 명령] 실전 투자 자동매매를 시작합니다.[/bold cyan]")

        # [추가] 텔레그램 메시지 구성을 위한 변수 미리 선언
        holdings = []
        summary = []
        deposit = 0
        asset_check_failed = False # [추가] 자산 조회 실패 여부 플래그

        with console.status("[bold green]시스템 시작 준비 중 (자산 조회 및 스레드 시작)...[/]"):
            self.is_running = True
            self.start_time = datetime.now()
            self.consecutive_errors = 0
            self.was_market_open = self.is_market_open()
            self.market_status_notified = {} # 시작 시 알림 상태 초기화
            
            # [추가] 시작 시점 총 자산 계산 (손실 제한 기준점)
            # [수정] AccountContext 사용
            target_cano = config.session.auto_cano if not config.session.is_simulation else config.session.cano
            with utils.AccountContext(target_cano):
                # [최적화] 자산 조회 로직 통합 (중복 API 호출 제거)
                acnt = config.session.auto_acnt_prdt_cd if not config.session.is_simulation else config.session.acnt_prdt_cd
                
                try:
                    # [수정] 상위 레벨 재시도 루프 제거 -> API 레벨 재시도(MAX_RETRIES) 활용
                    # 1. 잔고 및 평가금 조회
                    holdings, summary = api.get_domestic_balance(target_cano, acnt)
                    
                    # [추가] 스레드 첫 실행 시 재사용을 위해 저장
                    self.initial_holdings = holdings
                    self.initial_summary = summary
                    
                    # 2. 예수금 조회
                    if config.session.is_simulation and summary:
                        deposit = api.safe_int(summary[0].get('dnca_tot_amt', 0))
                    else:
                        # 실전투자거나 데이터가 없으면 정석대로 조회
                        res = api.get_deposit_balance(target_cano, acnt, skip_balance_check=True)
                        if res:
                            deposit = res['deposit'] + res['foreign_deposit']
                        else:
                            deposit = 0
                    
                    # 3. 총 자산 계산
                    stock_eval = 0
                    if summary:
                        stock_eval = api.safe_int(summary[0].get('scts_evlu_amt'))
                    
                    self.initial_asset = deposit + stock_eval
                    asset_check_failed = False

                except Exception as e:
                    logger.error(f"시작 자산 조회 실패: {e}")
                    asset_check_failed = True
                
                # [수정] 시작 시 불필요한 집중 감시 모드 진입 방지 (IDLE_INTERVAL=0 설정 존중)
                # ConclusionMonitor().check_now()
            
            if asset_check_failed:
                self.log("초기 자산 조회 실패 (API 응답 없음 또는 오류)")

            if self.initial_asset > 0:
                self.log(f"시스템 시작 자산: {self.initial_asset:,}원")
            
            # [추가] API 모듈에서 로그를 남길 수 있도록 연결
            config.SYSTEM_LOGGER = self.log
            
            self.thread = threading.Thread(target=self._run_loop, daemon=True, name="AutoTrader")
            self.thread.start()

        console.print("\n[green]자동매매 시스템이 시작되었습니다. (백그라운드)[/green]")
        self.log("시스템 시작")
        
        # [수정] 텔레그램 전송 시 AUTO 계좌 정보가 포함되도록 컨텍스트 설정
        # AccountContext는 블록 내에서만 유효하므로, 메시지 생성 시점에만 적용하거나
        # send_telegram_message 내부에서 처리하도록 두는 것이 좋으나, 여기서는 명시적으로 설정
        
        # [수정] 시작 메시지에 보유 종목 및 자산 현황 추가
        msg = f"🟢 [시스템 시작] 자동매매가 시작되었습니다.\n"
        
        if asset_check_failed:
            msg += "⚠️ [경고] 자산 정보를 불러오지 못했습니다. (API 오류)\n"
            
        msg += f"초기 자산: {self.initial_asset:,}원"
        msg += f"\n현재 예수금: {deposit:,}원"
        
        # [추가] 전략 설정 요약 정보 추가
        buy_score = config.ANALYSIS_THRESHOLDS["BUY_SCORE"]
        buy_rsi = config.ANALYSIS_THRESHOLDS["BUY_RSI_MAX"]
        sl = config.SELL_STRATEGY["STOP_LOSS_RATE"]
        tp = config.SELL_STRATEGY["TAKE_PROFIT_RATE"]
        ts_act = config.SELL_STRATEGY.get("TRAILING_STOP_ACTIVATION_RATE", 10.0)
        ts_call = config.SELL_STRATEGY.get("TRAILING_STOP_CALLBACK_RATE", 3.0)
        invest_ratio = getattr(config, 'SYSTEM_INVEST_PER_STOCK', 0.5)
        
        msg += "\n\n⚙️ [적용 전략]"
        msg += f"\n• 매수: {buy_score}점↑ & RSI<{buy_rsi}"
        msg += f"\n• 익절: +{tp}% / 손절: {sl}%"
        msg += f"\n• 트레일링: +{ts_act}% 도달 후 -{ts_call}%"
        msg += f"\n• 비중: 종목당 {invest_ratio*100:.0f}%"

        total_eval = 0 # [추가] 초기화
        if summary and len(summary) > 0:
            s_data = summary[0]
            total_eval = api.safe_int(s_data.get('scts_evlu_amt')) # 값 저장
            total_profit = api.safe_int(s_data.get('evlu_pfls_smtl_amt'))
            msg += f"\n현재 평가: {total_eval:,}원 (손익: {total_profit:+,}원)"
            
        if holdings:
            msg += "\n\n📋 [보유 종목 현황]"
            for item in holdings:
                name = item['prdt_name']
                qty = int(item['hldg_qty'])
                cur_price = int(item['prpr'])
                eval_amt = int(item['evlu_amt'])
                rate = float(item['evlu_pfls_rt'])
                profit = int(item['evlu_pfls_amt'])
                msg += f"\n• {name} ({qty}주)\n  현재가: {cur_price:,}원 | 평가: {eval_amt:,}원\n  손익: {profit:+,}원 ({rate:+.2f}%)"
        else:
            msg += "\n\n📋 [보유 종목] 없음"
            if total_eval > 0:
                msg += " (⚠️ 평가금액 존재 - API 데이터 불일치)"

        target_cano = config.session.auto_cano if not config.session.is_simulation else config.session.cano
        with utils.AccountContext(target_cano):
            api.send_telegram_message(msg)

    def stop(self, use_status=True):
        if not self.is_running:
            console.print("\n[yellow]실행 중인 자동매매가 없습니다.[/yellow]")
            return
            
        def _stop_logic():
            self.is_running = False
            if self.thread:
                self.thread.join(timeout=10) # [수정] 타임아웃 연장 (DB 락 대기 고려)

        if use_status:
            with console.status("[bold red]시스템 중단 요청 처리 중...[/]"):
                _stop_logic()
        else:
            _stop_logic()

        if self.thread and self.thread.is_alive():
            console.print("\n[bold red]경고: 시스템 트레이딩 스레드가 응답하지 않습니다. (DB/API 작업 지연)[/bold red]")
            console.print("[dim]강제로 중단 절차를 진행합니다. 일부 데이터가 누락될 수 있습니다.[/dim]")

        console.print("\n[red]자동매매 시스템이 중단되었습니다.[/red]")
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
                        deposit = res['deposit'] + res['foreign_deposit']
                        is_data_valid = True
                    else:
                        deposit = 0

                    # 2. 잔고 및 평가금 조회
                    holdings, summary = api.get_domestic_balance(target_cano, acnt)
                    if summary and len(summary) > 0:
                        stock_eval = api.safe_int(summary[0].get('scts_evlu_amt'))

                    if is_data_valid:
                        final_asset = deposit + stock_eval
                        profit = final_asset - self.initial_asset
                        profit_rate = 0.0 if self.initial_asset <= 0 else (profit / self.initial_asset) * 100

                        msg += f"\n종료 자산: {final_asset:,}원\n최종 예수금: {deposit:,}원\n금일 손익: {profit:+,}원 ({profit_rate:+.2f}%)"
                    else:
                        msg += "\n(⚠️ 종료 시 자산 정보 조회 실패 - 서버 응답 없음)"

                    if holdings:
                        msg += "\n\n📋 [최종 보유 종목 현황]"
                        for item in holdings:
                            name = item['prdt_name']
                            qty = int(item['hldg_qty'])
                            cur_price = int(item['prpr'])
                            eval_amt = int(item['evlu_amt'])
                            rate = float(item['evlu_pfls_rt'])
                            profit_amt = int(item['evlu_pfls_amt'])
                            msg += f"\n• {name} ({qty}주)\n  현재가: {cur_price:,}원 | 평가: {eval_amt:,}원\n  손익: {profit_amt:+,}원 ({rate:+.2f}%)"
                    else:
                        msg += "\n\n📋 [최종 보유 종목] 없음"
                        if not is_data_valid:
                            msg += " (조회 실패 가능성 있음)"
                except Exception as e:
                    self.log(f"종료 시 자산/잔고 조회 실패: {e}")
                    msg += "\n(자산 조회 실패)"
            
                api.send_telegram_message(msg)
        else:
            msg += "\n(시스템 응답 지연으로 최종 자산 정보 생략)"
            self.log("스레드 종료 지연으로 최종 자산/잔고 조회 생략")
        
        # [추가] 로거 연결 해제 (메시지 전송 후 해제)
        config.SYSTEM_LOGGER = None

    def get_status_message(self):
        """텔레그램 전송용 상태 요약 메시지 생성"""
        status_text = "STOPPED"
        if self.is_running:
            status_text = "RUNNING" if self.is_market_open() else "WAITING"
        
        msg = f"📊 [시스템 상태: {status_text}]\n"
        
        # 자산 정보 조회
        current_asset = None
        deposit = 0
        holdings = []
        
        target_cano = config.session.auto_cano if not config.session.is_simulation else config.session.cano
        with utils.AccountContext(target_cano):
            try:
                # [최적화] API 호출 통합 (중복 조회 제거)
                acnt = config.session.auto_acnt_prdt_cd if not config.session.is_simulation else config.session.acnt_prdt_cd
                
                # 1. 잔고 조회 (평가금 포함)
                holdings, summary = api.get_domestic_balance(target_cano, acnt)
                
                # 2. 예수금 및 총 자산 계산
                if config.session.is_simulation:
                    if summary:
                        deposit = api.safe_int(summary[0].get('dnca_tot_amt', 0))
                        stock_eval = api.safe_int(summary[0].get('scts_evlu_amt', 0))
                        current_asset = deposit + stock_eval
                else:
                    res = api.get_deposit_balance(target_cano, acnt)
                    if res:
                        deposit = res['d2_deposit']
                        stock_eval = 0
                        if summary: stock_eval = api.safe_int(summary[0].get('scts_evlu_amt', 0))
                        total_cash = res['deposit'] + res['foreign_deposit']
                        current_asset = total_cash + stock_eval
            except: pass

        if current_asset is not None:
            profit = current_asset - self.initial_asset
            rate = (profit / self.initial_asset * 100) if self.initial_asset > 0 else 0.0
            msg += f"현재 자산: {current_asset:,}원\n"
            msg += f"누적 손익: {profit:+,}원 ({rate:+.2f}%)\n"
            msg += f"주문 가능: {deposit:,}원\n"
        else:
            msg += "자산 정보 조회 실패\n"
            
        if holdings:
            msg += "\n📋 [보유 종목 현황]"
            for item in holdings:
                name = item['prdt_name']
                qty = int(item['hldg_qty'])
                cur_price = int(item['prpr'])
                eval_amt = int(item['evlu_amt'])
                rate = float(item['evlu_pfls_rt'])
                profit = int(item['evlu_pfls_amt'])
                msg += f"\n• {name} ({qty}주)\n  현재가: {cur_price:,}원 | 평가: {eval_amt:,}원\n  손익: {profit:+,}원 ({rate:+.2f}%)"
        else:
            msg += "\n📋 [보유 종목] 없음"
            
        return msg

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
                console.print(f"[dim red]로그 파일 기록 실패: {e}[/dim red]")

    def get_recent_logs(self, count=10):
        """최근 로그 반환 (텔레그램용)"""
        if not self.logs:
            return "📭 로그가 없습니다."
        
        candidates = self.logs[-count:]
        final_logs = []
        current_len = 0
        max_len = 3800 # 텔레그램 제한(4096자) 고려하여 여유 있게 설정

        for log in reversed(candidates):
            if current_len + len(log) + 1 > max_len:
                break
            final_logs.append(log)
            current_len += len(log) + 1
        
        final_logs.reverse()
        msg = "📜 [최근 시스템 로그]\n"
        if len(final_logs) < len(candidates):
            msg += f"(길이 제한으로 최근 {len(final_logs)}줄만 표시)\n"
        return msg + "\n".join(final_logs)

    def print_status(self):
        if not self.is_running:
            status_text = "STOPPED"
            status_color = "red"
        elif self.is_market_open():
            status_text = "RUNNING"
            status_color = "green"
        else:
            status_text = "WAITING"
            status_color = "yellow"
        
        # 3. 자산 및 손익 현황 (안전성 핵심)
        current_asset = None
        deposit = 0
        holdings = []
        
        # [추가] 상태 조회 시에도 시스템 트레이딩 컨텍스트 사용
        target_cano = config.session.auto_cano if not config.session.is_simulation else config.session.cano
        with utils.AccountContext(target_cano):
            with console.status("[bold green]트레이딩 상태 및 자산 정보 조회 중...[/]"):
                current_asset = self._get_total_estimated_asset()
                
                # [추가] 보유 종목 확인
                acnt = config.session.auto_acnt_prdt_cd if not config.session.is_simulation else config.session.acnt_prdt_cd
                try:
                    holdings, _ = api.get_domestic_balance(target_cano, acnt)
                except: pass
                
                # 예수금 별도 조회 (매수 여력 확인용)
                try:
                    res = api.get_deposit_balance(target_cano, acnt)
                    if res:
                        deposit = res['d2_deposit']
                    else:
                        deposit = 0
                except: pass
                
                # [추가] 지수 상태 정보가 없으면 업데이트 시도 (시장 필터링 사용 시)
                # 시스템이 정지 상태이거나 장 시작 전이라도 상태 조회 시에는 최신 정보를 보여주기 위함
                if getattr(config, 'USE_MARKET_FILTER', True):
                    need_update = False
                    if "KOSPI" not in self.market_index_status or "KOSDAQ" not in self.market_index_status:
                        need_update = True
                    elif self.market_index_status.get("KOSPI", {}).get("current", 0) == 0 or \
                         self.market_index_status.get("KOSDAQ", {}).get("current", 0) == 0:
                        need_update = True
                    
                    if need_update:
                        self._update_market_indices_status()

        console.print()
        table = Table(title=f"시스템 트레이딩 상태 ({status_text})", title_style=status_color, box=box.HORIZONTALS, show_header=True, header_style="dim", border_style="dim")
        table.add_column("구분", justify="left", style="cyan", width=15)
        table.add_column("상세 내용", justify="left")

        # 1. 실행 정보
        if self.is_running and self.start_time:
            start_str = self.start_time.strftime("%Y-%m-%d %H:%M:%S")
            elapsed = datetime.now() - self.start_time
            elapsed_str = str(elapsed).split('.')[0]
            table.add_row("실행 시간", f"{start_str} (경과: {elapsed_str})")
        
        # 2. 마켓 상태
        market_status = "장 운영 중 (거래 가능)" if self.is_market_open() else "장 마감/휴장 (대기 중)"
        if datetime.now().weekday() > 4: market_status = "주말 휴장 (대기 중)"
        table.add_row("마켓 상태", market_status)
        
        # [추가] 지수 추세 상태 표시 (시장 필터링 사용 시)
        if getattr(config, 'USE_MARKET_FILTER', True):
            kospi_stat = self.market_index_status.get("KOSPI")
            kosdaq_stat = self.market_index_status.get("KOSDAQ")
            
            def get_stat_msg(stat):
                if not stat or not isinstance(stat, dict) or stat.get('current', 0) == 0:
                    return "[dim]확인 중[/]"
                
                is_healthy = stat.get('is_healthy', True)
                current = stat.get('current', 0)
                trend_icon = "(상승)" if is_healthy else "(하락)"
                color = "red" if is_healthy else "blue"
                return f"[{color}]{current:,.0f} {trend_icon}[/]"
            
            table.add_row("지수 추세", f"KOSPI: {get_stat_msg(kospi_stat)} / KOSDAQ: {get_stat_msg(kosdaq_stat)}")
            
            # [추가] 필터링 보류 개수 표시
            if self.skipped_by_market_filter_count > 0:
                table.add_row("시장 필터링", f"[bold blue]{self.skipped_by_market_filter_count}종목 매수 보류[/] (하락장)")

        table.add_section()

        # 3. 자산 현황
        if current_asset is not None:
            if self.initial_asset > 0:
                profit = current_asset - self.initial_asset
                rate = (profit / self.initial_asset) * 100
                color = "[red]" if profit > 0 else ("[blue]" if profit < 0 else "[white]")
                
                table.add_row("초기 자산", f"{self.initial_asset:,}원")
                table.add_row("현재 자산", f"{current_asset:,}원")
                table.add_row("누적 손익", f"{color}{profit:+,}원 ({rate:+.2f}%)[/]")
            else:
                table.add_row("초기 자산", "- (미설정)")
                table.add_row("현재 자산", f"{current_asset:,}원")
                table.add_row("누적 손익", "-")
            
            table.add_row("현재 예수금", f"{deposit:,}원")
            
            # 일일 손실 제한 체크 (초기 자산이 있을 때만)
            if self.initial_asset > 0:
                loss_limit = getattr(config, 'SYSTEM_DAILY_LOSS_LIMIT', 0.0)
                if loss_limit > 0:
                    safety_msg = "[green]안전[/green]"
                    if rate <= -loss_limit: safety_msg = "[bold red]위험 (한도 초과)[/bold red]"
                    elif rate <= -(loss_limit * 0.8): safety_msg = "[bold orange3]주의 (한도 임박)[/bold orange3]"
                    table.add_row("손실 제한", f"-{loss_limit}% (상태: {safety_msg})")
        else:
            table.add_row("자산 정보", "[bold red]조회 실패 (KIS 서버 응답 없음/장애 가능성)[/bold red]")
            if self.initial_asset > 0:
                table.add_row("초기 자산", f"{self.initial_asset:,}원")

        # [추가] 투자 설정 정보 표시
        invest_ratio = getattr(config, 'SYSTEM_INVEST_PER_STOCK', 0.5)
        if invest_ratio <= 0: invest_ratio = 0.1
        max_holdings = int(1 / invest_ratio)
        table.add_row("투자 설정", f"비중 {invest_ratio*100:.0f}% (최대 {max_holdings}종목)")

        table.add_section()

        # 4. 시스템 안정성
        err_cnt = self.consecutive_errors
        max_err = getattr(config, 'SYSTEM_MAX_CONSECUTIVE_ERRORS', 5)
        if err_cnt == 0:
            err_display = f"[dim green]{err_cnt} / {max_err}회[/]"
        else:
            err_color = "[red]" if err_cnt >= max_err else "[yellow]"
            err_display = f"{err_color}{err_cnt} / {max_err}회[/]"
        table.add_row("연속 에러", err_display)
        
        # 5. 매매 요약
        buy_cnt = len([x for x in self.trade_records if x['type'] == 'buy'])
        sell_cnt = len([x for x in self.trade_records if x['type'] == 'sell'])
        table.add_row("금일 매매", f"[red]매수 {buy_cnt}건[/] / [blue]매도 {sell_cnt}건[/]")

        console.print(table)
        
        # [추가] 보유 종목 리스트 출력
        if holdings:
            console.print("\n[bold]보유 종목 리스트[/bold]")
            h_table = Table(box=box.HORIZONTALS, header_style="dim", border_style="dim")
            h_table.add_column("종목명(코드)", justify="left")
            h_table.add_column("시장", justify="center")
            h_table.add_column("수량", justify="right")
            h_table.add_column("매입가", justify="right")
            h_table.add_column("현재가", justify="right")
            h_table.add_column("평가손익", justify="right")
            h_table.add_column("수익률", justify="right")
            
            for item in holdings:
                name = item['prdt_name']
                code = item['pdno']
                market_type = self._get_stock_market_type(code)
                qty = int(item['hldg_qty'])
                buy_price = float(item['pchs_avg_pric'])
                cur_price = int(item['prpr'])
                profit = int(item['evlu_pfls_amt'])
                rate = float(item['evlu_pfls_rt'])
                
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

    def print_report(self):
        console.print("\n[bold yellow]=== 시스템 트레이딩 리포트 ===[/]")
        self._load_trade_records()
        if not self.trade_records:
            console.print("\n[yellow]저장된 시스템 트레이딩 기록이 없습니다.[/yellow]")
            return
        stats = self._calculate_statistics()
        self._print_summary_table(stats)
        self._print_current_holdings()
        self._print_stock_details()

    def _load_trade_records(self):
        with console.status("[bold green]DB에서 매매 내역 조회 및 분석 중...[/]"):
            time.sleep(0.5)
            
            # [수정] DB에서 시스템 트레이딩 내역 조회 (현재 모드에 맞는 내역만)
            db_records = db_manager.db.get_trades(is_auto=True, is_sim=config.session.is_simulation, limit=500)
            
            # DB 레코드를 내부 포맷으로 변환
            self.trade_records = []
            for r in reversed(db_records): # DB는 최신순이므로 시간순(과거->최신)으로 뒤집음
                # type 파싱: "buy(AUTO)" -> "buy"
                type_str = r['type']
                simple_type = "buy" if "매수" in type_str or "buy" in type_str.lower() else "sell"
                
                self.trade_records.append({
                    "type": simple_type,
                    "code": r['code'],
                    "name": r['name'],
                    "qty": int(r['qty']),
                    "price": float(r['price']),
                    "profit_rate": float(r['profit_rate'] or 0),
                    "profit_amt": int(r['profit_amt'] or 0),
                    "reason": r['reason'],
                    "time": r['time'],
                    "odno": r['odno']
                })

    def get_performance_report(self):
        """텔레그램용 성과 리포트 생성"""
        # DB에서 조회 (로그 출력 없이)
        db_records = db_manager.db.get_trades(is_auto=True, is_sim=config.session.is_simulation, limit=500)
        
        temp_records = []
        for r in reversed(db_records):
            type_str = r['type']
            simple_type = "buy" if "매수" in type_str or "buy" in type_str.lower() else "sell"
            
            temp_records.append({
                "type": simple_type,
                "code": r['code'],
                "name": r['name'],
                "qty": int(r['qty']),
                "price": float(r['price']),
                "profit_rate": float(r['profit_rate'] or 0),
                "profit_amt": int(r['profit_amt'] or 0),
                "reason": r['reason'],
                "time": r['time'],
                "odno": r['odno']
            })
            
        if not temp_records:
            return "📭 매매 기록이 없습니다."
            
        stats = self._calculate_statistics(temp_records)
        
        msg = "📊 [시스템 트레이딩 성과 리포트]\n"
        msg += f"총 매매: {stats['total_trades']}건 (매수 {stats['buy_count']} / 매도 {stats['sell_count']})\n"
        
        if stats['sell_trades_exist']:
            win_rate = stats['win_rate']
            total_profit = stats['total_profit']
            avg_profit_rate = stats['avg_profit_rate']
            
            icon = "🔴" if total_profit > 0 else ("🔵" if total_profit < 0 else "⚪️")
            msg += f"승률: {win_rate:.1f}% ({stats['win_trades']}승 {stats['loss_trades']}패)\n"
            msg += f"총 손익: {icon} {total_profit:+,}원\n"
            msg += f"평균 수익률: {avg_profit_rate:+.2f}%\n"
            msg += f"평균 보유: {stats['avg_holding_str']}"
        else:
            msg += "\n(청산된 내역이 없어 수익률 산출 불가)"
            
        return msg

    def _calculate_statistics(self, records=None):
        if records is None: records = self.trade_records
        
        total_trades = len(records)
        buy_trades = [r for r in records if r['type'] == 'buy']
        sell_trades = [r for r in records if r['type'] == 'sell']
        
        win_trades = 0
        loss_trades = 0
        total_profit = 0
        total_profit_rate = 0.0
        
        # [추가] 보유 기간 계산
        total_holding_seconds = 0
        holding_count = 0
        buy_times = {} # code -> list of datetime

        # 시간순 처리를 위해 전체 기록 순회
        for r in records:
            code = r['code']
            try:
                dt = datetime.strptime(r['time'], "%Y-%m-%d %H:%M:%S")
            except: continue

            if r['type'] == 'buy':
                if code not in buy_times: buy_times[code] = []
                buy_times[code].append(dt)
            elif r['type'] == 'sell':
                # 매도 시 매수 기록과 매칭 (FIFO: 먼저 산 것을 먼저 판다고 가정)
                if code in buy_times and buy_times[code]:
                    buy_dt = buy_times[code].pop(0)
                    diff = (dt - buy_dt).total_seconds()
                    total_holding_seconds += diff
                    holding_count += 1
        
        for t in sell_trades:
            profit = t.get('profit_amt', 0)
            rate = t.get('profit_rate', 0.0)
            total_profit += profit
            total_profit_rate += rate
            if profit > 0: win_trades += 1
            else: loss_trades += 1
            
        avg_profit_rate = (total_profit_rate / len(sell_trades)) if sell_trades else 0.0
        win_rate = (win_trades / len(sell_trades) * 100) if sell_trades else 0.0

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
            "avg_profit_rate": avg_profit_rate,
            "win_rate": win_rate,
            "avg_holding_str": avg_holding_str,
            "sell_trades_exist": len(sell_trades) > 0
        }

    def _print_summary_table(self, stats):
        summary_table = Table(box=box.HORIZONTALS, show_header=False, border_style="dim")
        summary_table.add_column("항목", style="cyan", justify="left")
        summary_table.add_column("값", justify="left")
        
        summary_table.add_row("총 매매 실행", f"{stats['total_trades']}건 (매수 {stats['buy_count']} / 매도 {stats['sell_count']})")
        
        if stats['sell_trades_exist']:
            summary_table.add_row("승률 (Win Rate)", f"{stats['win_rate']:.1f}% ({stats['win_trades']}승 {stats['loss_trades']}패)")
            tp = stats['total_profit']
            summary_table.add_row("총 실현 손익", f"[red]{tp:+,}원[/]" if tp > 0 else f"[blue]{tp:+,}원[/]")
            apr = stats['avg_profit_rate']
            summary_table.add_row("평균 수익률", f"[red]{apr:+.2f}%[/]" if apr > 0 else f"[blue]{apr:+.2f}%[/]")
            summary_table.add_row("평균 보유 기간", stats['avg_holding_str'])
        
        console.print(summary_table)

    def _print_current_holdings(self):
        try:
            # 컨텍스트 설정 (시스템 트레이딩 계좌 조회)
            target_cano = config.session.auto_cano if not config.session.is_simulation else config.session.cano
            with utils.AccountContext(target_cano):
                acnt = config.session.auto_acnt_prdt_cd if not config.session.is_simulation else config.session.acnt_prdt_cd
                holdings, _ = api.get_domestic_balance(target_cano, acnt)
                
                if holdings:
                    console.print("\n[bold]현재 보유 종목 현황[/bold]")
                    h_table = Table(box=box.HORIZONTALS, header_style="dim", border_style="dim")
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

        for r in self.trade_records:
            code = r['code']
            if code not in stock_stats:
                stock_stats[code] = {
                    'name': r['name'], 
                    'buy': 0, 'sell': 0, 
                    'profit': 0, 'rates': [], 'wins': 0,
                    'reasons': [], # 매도 사유 리스트
                    'holding_secs': [], # 보유 기간 리스트
                    'max_rate': -999.0, 'min_rate': 999.0
                }
            if code not in buy_times_per_stock:
                buy_times_per_stock[code] = []
            
            try:
                dt = datetime.strptime(r['time'], "%Y-%m-%d %H:%M:%S")
            except: dt = datetime.now()
            
            if r['type'] == 'buy':
                stock_stats[code]['buy'] += 1
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
            console.print("\n[bold]종목별 성과 분석[/bold]")
            s_table = Table(box=box.HORIZONTALS, header_style="dim", border_style="dim")
            s_table.add_column("종목명(코드)", justify="left")
            s_table.add_column("매매(매수/매도)", justify="center")
            s_table.add_column("승률", justify="right")
            s_table.add_column("총 손익", justify="right")
            s_table.add_column("평균 수익률", justify="right")
            # [추가] 상세 정보 컬럼
            s_table.add_column("최대/최소", justify="right")
            s_table.add_column("주요 사유", justify="center")
            s_table.add_column("평균 보유", justify="right")

            for code, stat in stock_stats.items():
                s_cnt = stat['sell']
                win_rate = (stat['wins'] / s_cnt * 100) if s_cnt > 0 else 0.0
                avg_rate = (sum(stat['rates']) / s_cnt) if s_cnt > 0 else 0.0
                
                p_color = "[red]" if stat['profit'] > 0 else ("[blue]" if stat['profit'] < 0 else "[white]")
                r_color = "[red]" if avg_rate > 0 else ("[blue]" if avg_rate < 0 else "[white]")
                
                # 최대/최소 수익률 포맷팅
                max_r = stat['max_rate'] if stat['max_rate'] != -999.0 else 0.0
                min_r = stat['min_rate'] if stat['min_rate'] != 999.0 else 0.0
                range_str = f"[red]{max_r:+.1f}%[/] / [blue]{min_r:+.1f}%[/]" if s_cnt > 0 else "-"
                
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
            console.print(s_table)
        
        # 상세 내역 테이블
        console.print("\n[bold]상세 매매 내역 (최신순)[/bold]")
        detail_table = Table(box=box.HORIZONTALS, header_style="dim", border_style="dim")
        detail_table.add_column("시간", justify="center")
        detail_table.add_column("구분", justify="center")
        detail_table.add_column("종목명", justify="left")
        detail_table.add_column("수량", justify="right")
        detail_table.add_column("단가", justify="right")
        detail_table.add_column("손익/비고", justify="right")
        
        for r in reversed(self.trade_records):
            type_str = "[red]매수[/]" if r['type'] == 'buy' else "[blue]매도[/]"
            
            # 단가 포맷팅 (정수/실수 구분)
            price_val = r['price']
            if price_val.is_integer():
                price_str = f"{int(price_val):,}"
            else:
                price_str = f"{price_val:,.2f}"
            
            note = "-"
            if r['type'] == 'sell':
                p_amt = r.get('profit_amt', 0)
                p_rate = r.get('profit_rate', 0.0)
                color = "[red]" if p_amt > 0 else "[blue]"
                note = f"{color}{p_amt:+,}원 ({p_rate:+.2f}%)[/]"
            else:
                note = r.get('reason', '-')
            
            detail_table.add_row(
                r['time'][5:], # MM-DD HH:MM:SS
                type_str,
                f"{r['name']}",
                f"{r['qty']}",
                price_str,
                note
            )
            
        console.print(detail_table)

    def view_log_file(self):
        """현재 날짜의 시스템 트레이딩 로그 파일을 실시간으로 출력합니다."""
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

        console.print(f"\n[bold cyan]=== 실시간 로그 모니터링 ({filename}) ===[/bold cyan]")
        console.print("[dim]종료하려면 Ctrl+C를 누르세요.[/dim]\n")

        with console.status("[bold green]로그 파일 로딩 중...[/]"):
            time.sleep(0.5)

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
        """국내 정규장 운영 시간 확인 (config 설정 시간 따름)"""
        now = datetime.now()
        if now.weekday() > 4: return False # 주말
        current_time = now.strftime("%H%M")
        
        start_time = getattr(config, 'SYSTEM_TRADING_START_TIME', "0915")
        end_time = getattr(config, 'SYSTEM_TRADING_END_TIME', "1515")
        return start_time <= current_time <= end_time

    def _run_loop(self):
        while self.is_running:
            try:
                target_cano = config.session.auto_cano if not config.session.is_simulation else config.session.cano
                with utils.AccountContext(target_cano):
                    self.log("모니터링 주기 시작...")
                    
                    # [추가] 현재 운용 계좌 정보 로깅
                    if target_cano:
                        acc_type = "모의투자" if config.session.is_simulation else "실전투자(자동)"
                        self.log(f"운용 계좌: {target_cano} [{acc_type}]")
                    
                    current_market_status = self.is_market_open()
                    
                    # [추가] 장 시작/마감 상태 변경 감지 및 로그
                    if self.was_market_open is not None:
                        if not self.was_market_open and current_market_status:
                            self.log("=" * 80)
                            self.log(f"📢 [거래 시작] 시스템 트레이딩 거래가 시작되었습니다. ({datetime.now().strftime('%H:%M')})")
                            self.log("=" * 80)
                            api.send_telegram_message("🔔 [장 시작] 거래 가능 시간이 되었습니다.")
                        elif self.was_market_open and not current_market_status:
                            self.log("=" * 80)
                            self.log(f"💤 [거래 종료] 시스템 트레이딩 거래가 종료되었습니다. ({datetime.now().strftime('%H:%M')})")
                            self.log("=" * 80)
                            api.send_telegram_message("🌙 [장 마감] 거래 시간이 종료되었습니다.")
                    
                    # [변경] 장 마감 시 분석 중단 (트래픽 감소)
                    if not current_market_status:
                        self.log("시스템 상태: WAITING (장 마감 - 분석 중지)")
                        self.was_market_open = current_market_status
                    else:
                        status_msg = "RUNNING"
                        self.log(f"시스템 상태: {status_msg}")
                        
                        # [추가] 시장 지수 상태 업데이트 (KOSPI/KOSDAQ)
                        if getattr(config, 'USE_MARKET_FILTER', True):
                            self._update_market_indices_status()
                            
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
                        
                        # [추가] 잔고 조회 실패(API 오류) 시 이번 주기 스킵 (연쇄 오류 방지)
                        if holdings is None:
                            self.log("잔고 조회 실패(API 오류). 잠시 대기 후 재시도합니다.")
                            time.sleep(5.0) # [수정] 실패 시 즉시 재시도 방지를 위한 대기
                            continue
                        
                        # 2. 예수금 조회
                        # [최적화] 모의투자는 잔고 조회 결과(summary)에 예수금이 포함되어 있어 별도 호출 불필요
                        deposit_res = None
                        if config.session.is_simulation and summary:
                            dnca = api.safe_int(summary[0].get('dnca_tot_amt', 0))
                            # [수정] 주문가능금액은 가수도금(prvs_rcdl_excc_amt) 사용 (매도 대금 포함)
                            d2_dep = api.safe_int(summary[0].get('prvs_rcdl_excc_amt', 0))
                            deposit_res = {'deposit': dnca, 'foreign_deposit': 0, 'd2_deposit': d2_dep}
                        else:
                            # [최적화] 이미 get_domestic_balance를 시도했으므로 내부 재호출 방지
                            deposit_res = api.get_deposit_balance(target_cano, acnt, skip_balance_check=True)
                        
                        # API 호출 간격 조절 (Rate Limit 방지)
                        time.sleep(0.2)

                        # [수정] 락 범위 축소: 전체 로직을 감싸던 락 제거 (api.call_api 내부 락 활용)
                        # 1. 매도 조건 점검 (리스크 관리)
                        self._check_sell_conditions(holdings, current_market_status)
                        # 2. 매수 조건 점검
                        self._check_buy_conditions(holdings, deposit_res, current_market_status)
                        # 3. 미체결 주문 관리 (오래된 주문 취소) - 장 중에만 수행
                        self._manage_unfilled_orders()
                        # [추가] 보유 종목 상태 로깅 및 자산 안전장치 체크
                        self._monitor_account_status(holdings, summary, deposit_res)
                    
                    self.was_market_open = current_market_status
                    
                    self.log("모니터링 완료. 대기 중...")
                
                # 설정된 주기만큼 대기 (중단 요청 시 즉시 반응)
                # [확인] 설정된 간격(현재 180초)마다 위 로직을 반복합니다.
                interval = getattr(config, 'SYSTEM_TRADING_INTERVAL', 60)
                for _ in range(interval): 
                    if not self.is_running: break
                    time.sleep(1)
                
                # 정상 루프 완료 시 에러 카운트 초기화
                self.consecutive_errors = 0
                    
            except Exception as e:
                self.consecutive_errors += 1
                max_err = getattr(config, 'SYSTEM_MAX_CONSECUTIVE_ERRORS', 5)
                self.log(f"에러 발생({self.consecutive_errors}/{max_err}): {str(e)}")
                
                if self.consecutive_errors >= max_err:
                    # [수정] 중단 대신 대기 모드로 전환
                    self.log(f"[장애 감지] 연속 에러 {max_err}회 발생. 서버 장애로 판단하여 대기 모드로 전환합니다.")
                    api.send_telegram_message(f"🚨 [서버 장애] 연속 에러 {max_err}회 발생.\n매매를 일시 중지하고 서버 복구를 대기합니다.")
                    
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
                    api.send_telegram_message("✅ [서버 복구] KIS 서버가 정상화되었습니다.\n자동매매를 재개합니다.")
                    return
                else:
                    self.log("[장애 대기] 서버 여전히 응답 없음.")
            except Exception as e:
                self.log(f"[장애 대기] 점검 중 오류: {e}")

    def _manage_unfilled_orders(self):
        """오래된 미체결 주문 확인 및 취소"""
        try:
            unfilled_list = api.get_unfilled_orders()
            if not unfilled_list: return

            cancel_seconds = getattr(config, 'UNFILLED_ORDER_CANCEL_SECONDS', 600)
            now = datetime.now()
            
            for item in unfilled_list:
                odno = item.get('odno')
                code = item.get('pdno')
                name = item.get('prdt_name')
                qty = int(item.get('rmn_qty', 0)) # 잔여 수량
                ord_time_str = item.get('ord_tmd') # 주문시간 (HHMMSS)
                
                if not odno or qty <= 0 or not ord_time_str: continue
                
                # 주문 시간 파싱
                try:
                    ord_dt = datetime.strptime(f"{now.strftime('%Y%m%d')}{ord_time_str}", "%Y%m%d%H%M%S")
                    elapsed = (now - ord_dt).total_seconds()
                    
                    if elapsed >= cancel_seconds:
                        self.log(f"[미체결 관리] {name}({code}) 주문({odno})이 {int(elapsed)}초 동안 체결되지 않아 취소합니다.")
                        
                        # 취소 주문 전송 (api.revise_cancel_order 사용)
                        # 국내 주식 기준, 취소 코드는 "02", 단가는 "0"
                        res = api.revise_cancel_order("domestic", "cancel", odno, code, qty, "0", "02", "00")
                        
                        if res.get('rt_cd') == '0':
                            api.send_telegram_message(f"🗑 [주문 취소] {name} {qty}주\n사유: 미체결 시간 초과 ({int(elapsed)}초)")
                        else:
                            self.log(f"취소 실패: {res.get('msg1')}")
                            
                except Exception: pass
        except Exception as e:
            self.log(f"미체결 관리 중 오류: {e}")

    def _monitor_account_status(self, holdings, summary, deposit_res):
        """현재 보유 종목 상태 로깅 및 자산 손실 제한(Loss Cut) 체크"""
        try:
            if not holdings:
                self.log("보유 종목: 없음")
            else:
                # 한글 정렬 보정 헬퍼 함수
                def pad(s, width, align='>'):
                    k = sum(1 for c in s if ord(c) > 127)
                    real_len = len(s) + k
                    pad_len = width - real_len
                    if pad_len < 0: pad_len = 0
                    if align == '<': return s + ' ' * pad_len
                    else: return ' ' * pad_len + s

                # 헤더 출력
                header = (
                    f"{pad('종목명', 30, '<')} "
                    f"{pad('보유수량', 10, '>')} "
                    f"{pad('매입단가', 12, '>')} "
                    f"{pad('현재가', 12, '>')} "
                    f"{pad('매입금액', 15, '>')} "
                    f"{pad('평가금액', 15, '>')} "
                    f"{pad('평가손익', 14, '>')} "
                    f"{pad('수익률', 10, '>')}"
                )
                
                self.log("-" * 125)
                self.log(header)
                self.log("-" * 125)
                
                for item in holdings:
                    name = f"{item['prdt_name']} ({item['pdno']})"
                    qty = int(item['hldg_qty'])
                    buy_price = float(item['pchs_avg_pric'])
                    cur_price = int(item['prpr'])
                    pchs_amt = int(item.get('pchs_amt', 0))
                    eval_amt = int(item.get('evlu_amt', 0))
                    profit = int(item['evlu_pfls_amt'])
                    rate = float(item['evlu_pfls_rt'])
                    
                    row_str = (
                        f"{pad(name, 30, '<')} "
                        f"{pad(f'{qty:,}주', 10, '>')} "
                        f"{pad(f'{buy_price:,.0f}원', 12, '>')} "
                        f"{pad(f'{cur_price:,.0f}원', 12, '>')} "
                        f"{pad(f'{pchs_amt:,}원', 15, '>')} "
                        f"{pad(f'{eval_amt:,}원', 15, '>')} "
                        f"{pad(f'{profit:+,}원', 14, '>')} "
                        f"{pad(f'{rate:.2f}%', 10, '>')}"
                    )
                    self.log(row_str)
                
                self.log("-" * 125)
                if summary and len(summary) > 0:
                    s_data = summary[0]
                    total_profit = api.safe_int(s_data.get('evlu_pfls_smtl_amt'))
                    total_eval = api.safe_int(s_data.get('scts_evlu_amt'))
                    self.log(f"   총 평가금액: {total_eval:,}원  |  총 평가손익: {total_profit:+,}원")
                    
                    # 총 자산 계산 (예수금 + 평가금)
                    current_total = 0
                    if deposit_res:
                        # 총 자산 계산 시에는 원화+외화 예수금 합산
                        cash = deposit_res['deposit'] + deposit_res['foreign_deposit']
                        current_total = cash + total_eval
                    
                    # [추가] 일일 손실 제한 체크
                    if current_total > 0:
                        # [Fix] 초기 자산 로드 실패(0원) 시, 첫 유효 조회 값으로 보정
                        if self.initial_asset == 0:
                            self.initial_asset = current_total
                            self.log(f"[시스템 보정] 초기 자산 정보 갱신: {self.initial_asset:,}원")

                        self._check_loss_limit(current_total)
                    
        except Exception: pass

    def _get_total_estimated_asset(self):
        """현재 총 추정 자산(예수금 + 주식평가금) 계산"""
        try:
            # [수정] 상위 레벨 재시도 루프 제거 -> API 레벨 재시도 활용
            cano = config.session.auto_cano if not config.session.is_simulation else config.session.cano
            acnt = config.session.auto_acnt_prdt_cd if not config.session.is_simulation else config.session.acnt_prdt_cd
            
            # [최적화] 모의투자일 경우 잔고 조회만으로 해결 (예수금 포함됨)
            if config.session.is_simulation:
                _, summary = api.get_domestic_balance(cano, acnt)
                if summary and len(summary) > 0:
                    stock_eval = api.safe_int(summary[0].get('scts_evlu_amt', 0))
                    # [수정] 총 자산 계산 시 D+2 예수금(가수도금) 사용 (매도 대금 반영)
                    cash = api.safe_int(summary[0].get('prvs_rcdl_excc_amt', 0))
                    return cash + stock_eval
                return 0

            # 실전투자: 예수금 별도 조회 필요
            res = api.get_deposit_balance(cano, acnt, skip_balance_check=True)
            if res is None: raise Exception("예수금 조회 실패 (API 응답 없음)")
            
            cash = res['deposit'] + res['foreign_deposit']
            
            _, summary = api.get_domestic_balance(cano, acnt)
            stock_eval = 0
            if summary and len(summary) > 0: stock_eval = api.safe_int(summary[0].get('scts_evlu_amt'))
            
            return cash + stock_eval
        except Exception as e:
            logger.debug(f"자산 조회 중 예외 발생: {str(e)}")
        
        self.log(f"⚠️ 자산 조회 최종 실패. (KIS 서버 응답 지연)")
        return None

    def _check_loss_limit(self, current_total):
        """자산 변동을 체크하여 손실 한도 초과 시 비상 정지"""
        loss_limit_pct = getattr(config, 'SYSTEM_DAILY_LOSS_LIMIT', 0.0)
        if loss_limit_pct <= 0 or self.initial_asset <= 0: return

        if current_total <= 0: return

        loss_rate = (current_total - self.initial_asset) / self.initial_asset * 100
        
        if loss_rate <= -loss_limit_pct:
            self.log(f"[비상 정지] 일일 손실 한도 초과! (현재: {loss_rate:.2f}% / 제한: -{loss_limit_pct}%)")
            self.log(f"시작 자산: {self.initial_asset:,}원 -> 현재 자산: {current_total:,}원")
            api.send_telegram_message(f"🚨 [손실 제한] 일일 손실 한도 초과!\n수익률: {loss_rate:.2f}%\n현재 자산: {current_total:,}원")
            self.stop()

    def _get_prev_rsi(self, df):
        """전일 RSI 계산 (주의 조건 판단용)"""
        if df is not None and not df.empty and len(df) >= 16:
            delta = df['close'].diff()
            gain = delta.where(delta > 0, 0).ewm(com=13, adjust=False).mean()
            loss = -delta.where(delta < 0, 0).ewm(com=13, adjust=False).mean()
            try: return (100 - (100 / (1 + gain/loss))).iloc[-2]
            except: pass
        return None

    def _check_sell_conditions(self, holdings, is_market_open=True):
        # [최적화] 인자로 전달받은 holdings 사용
        if not holdings: return

        stop_loss_rate = config.SELL_STRATEGY["STOP_LOSS_RATE"]
        take_profit_rate = config.SELL_STRATEGY["TAKE_PROFIT_RATE"]
        take_profit_rsi = config.SELL_STRATEGY["TAKE_PROFIT_RSI"]
        sell_score_limit = config.SELL_STRATEGY["SELL_SCORE"]
        
        # [추가] 트레일링 스탑 설정 로드
        ts_activation = config.SELL_STRATEGY.get("TRAILING_STOP_ACTIVATION_RATE", 5.0)
        ts_callback = config.SELL_STRATEGY.get("TRAILING_STOP_CALLBACK_RATE", 3.0)

        # [추가] Rate Limit 준수를 위한 딜레이 설정
        # 모의투자: 초당 2건 -> 0.5초 + 여유 / 실전투자: 초당 20건 -> 0.05초 + 여유
        tps = config.SIM_TX_PER_SECOND if config.session.is_simulation else config.REAL_TX_PER_SECOND
        safe_delay = (1.0 / tps) * 1.2  # 20% 여유 버퍼

        for item in holdings:
            if not self.is_running: break
            code = item['pdno']; name = item['prdt_name']
            
            # [수정] 보유수량(hldg_qty) 대신 주문가능수량(ord_psbl_qty) 사용
            # 미체결 매도 주문이 있을 경우 중복 매도를 방지하기 위함
            qty = api.safe_int(item.get('ord_psbl_qty'))
            profit_rate = float(item['evlu_pfls_rt'])
            current_price = float(item['prpr'])
            buy_price = float(item['pchs_avg_pric'])
            
            # [추가] API 호출 전 대기 (Rate Limit 방지)
            time.sleep(safe_delay)
            
            if qty <= 0: 
                continue # 주문 가능 수량이 없으면 스킵
            
            # [트레일링 스탑 로직] - 상태 관리가 필요하므로 AutoTrader에서 계산 후 Strategy에 전달
            ts_msg = ""
            # [최적화] 메모리 캐시 활용하여 DB 조회/쓰기 최소화
            cached_highest = self.trailing_stop_cache.get(code)
            if cached_highest is None:
                # 캐시에 없으면 DB 조회 (최초 1회)
                val = db_manager.db.get_highest_price(code)
                cached_highest = val if val is not None else 0.0
                self.trailing_stop_cache[code] = cached_highest
            
            highest_price = cached_highest
            
            # 현재가가 매수가보다 높고, 기록된 최고가보다 높을 때만 DB 업데이트
            if current_price > buy_price:
                if highest_price == 0.0 or current_price > highest_price:
                    db_manager.db.update_highest_price(code, current_price)
                    self.trailing_stop_cache[code] = current_price # 캐시 갱신
                    highest_price = current_price
            
            if highest_price and highest_price > 0:
                max_profit_rate = ((highest_price - buy_price) / buy_price) * 100
                if max_profit_rate >= ts_activation:
                    drop_rate = ((highest_price - current_price) / highest_price) * 100
                    if drop_rate >= ts_callback:
                        ts_msg = f"트레일링스탑 (최고가:{int(highest_price):,}원, 하락률:-{drop_rate:.1f}%)"

            # [전략 실행] 매도 분석 위임
            df = api.get_chart_data(code, is_overseas=False)
            result = self.strategy.analyze_sell(code, name, df, current_price, buy_price, profit_rate, ts_msg)
            
            # [로그] 분석 결과 기록
            ind = result['ind']
            rsi_val = f"{ind.get('rsi'):.1f}" if ind.get('rsi') is not None else "-"
            adx_val = f"{ind.get('adx'):.1f}" if ind.get('adx') is not None else "-"
            cci_val = f"{ind.get('cci'):.1f}" if ind.get('cci') is not None else "-"
            action_str = "매도" if result['action'] == 'sell' else "보유"
            self.log(f"[보유분석] {name}({code}): 수익률={profit_rate:.2f}%, 점수={result['score']}, 상태={result['state']}, 판단={action_str}, RSI={rsi_val}, ADX={adx_val}, CCI={cci_val}")

            if result['action'] == 'sell':
                reason = result['reason']
                score = result['score']
                
                # [추가] 매도 체결 확률을 높이기 위해 -1호가 적용
                tick_size = self._get_tick_size(current_price)
                order_price = int(current_price - tick_size)
                if order_price <= 0: order_price = int(current_price)

                if not is_market_open:
                    self.log(f"[장마감] 매도 신호 감지 (주문 미전송): {name} - {reason}")
                    continue

                # [추가] 매도 주문 전 실제 매도 가능 수량 재조회 (미체결 등 변동 고려)
                real_qty = api.fetch_sellable_quantity(code)
                if real_qty < qty:
                    if real_qty > 0:
                        self.log(f"매도 수량 조정: {name} {qty}주 -> {real_qty}주 (주문 가능 수량 변동)")
                        qty = real_qty
                    else:
                        self.log(f"매도 중단: {name} 주문 가능 수량 부족 (미체결 존재 가능성)")
                        continue

                self.log(f"매도 실행: {name} - {reason}")
                # [수정] 매도 시 수익 정보와 사유, 점수 등을 DB 저장을 위해 전달
                odno = self._send_order(code, qty, "sell", name=name, profit_amt=int(item['evlu_pfls_amt']), profit_rate=profit_rate, reason=reason, score=score, price=order_price)
                if odno:
                    # 매도 성공 시 기록 (추정치)
                    record = {
                        "type": "sell",
                        "code": code,
                        "name": name,
                        "qty": qty,
                        "price": float(order_price),
                        "profit_rate": profit_rate,
                        "profit_amt": int(item['evlu_pfls_amt']),
                        "reason": reason,
                        "time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                        "odno": odno
                    }
                    self.trade_records.append(record)
                    # [추가] 매도 성공 시 트레일링 스탑 정보 삭제
                    db_manager.db.delete_trailing_stop(code)
                    if code in self.trailing_stop_cache: # 캐시 삭제
                        del self.trailing_stop_cache[code]

    def _get_tick_size(self, price):
        """국내 주식 호가 단위 계산"""
        if price < 2000: return 1
        if price < 5000: return 5
        if price < 20000: return 10
        if price < 50000: return 50
        if price < 200000: return 100
        if price < 500000: return 500
        return 1000

    def _check_buy_conditions(self, holdings, deposit_res, is_market_open=True):
        targets = config.session.stock_data.get("stocks_kr", [])
        if not targets: return
        
        # [추가] 필터링 카운트 초기화 (매 주기마다 갱신)
        self.skipped_by_market_filter_count = 0
        skipped_stocks = [] # [추가] 시장 필터링으로 보류된 종목 리스트
        
        # [추가] 보유 종목 조회 (중복 매수 방지)
        holding_codes = set()
        if holdings:
            for h in holdings:
                holding_codes.add(h['pdno'])
        
        # [수정] 최대 보유 종목 수 체크 (투자 비중에 따라 자동 계산)
        invest_ratio = getattr(config, 'SYSTEM_INVEST_PER_STOCK', 0.5)
        if invest_ratio <= 0: invest_ratio = 0.1 # 0 이하일 경우 기본값 10%
        max_holdings = int(1 / invest_ratio)
        
        if len(holding_codes) >= max_holdings:
            if self.consecutive_errors == 0: # 로그 도배 방지
                self.log(f"매수 스킵: 최대 보유 종목 수({max_holdings}개) 도달 (투자비중 {invest_ratio*100:.0f}% 기준)")
            return

        # 예수금 확인 (API 직접 호출)
        avail_cash = 0
        if deposit_res:
            avail_cash = deposit_res['d2_deposit'] # 주문 가능 금액은 D+2 예수금 기준
        else:
            return # 조회 실패 시 매수 중단

        if avail_cash < 50000: return # 최소 주문 가능 금액 설정

        # 1. 후보 분석
        candidates = self._analyze_candidates(targets, holding_codes)
        
        # 2. 매수 집행
        if candidates:
            if not is_market_open:
                self.log(f"[장마감] 매수 후보 감지 (주문 미전송): {len(candidates)}종목")
                for cand in candidates:
                     self.log(f"   - {cand['name']} ({cand['score']}점)")
                return

            self._execute_buy_orders(candidates, avail_cash, invest_ratio, len(holding_codes), max_holdings)

    def _analyze_candidates(self, targets, holding_codes):
        candidates = []
        skipped_stocks = []
        
        # [추가] Rate Limit 준수를 위한 딜레이 설정
        tps = config.SIM_TX_PER_SECOND if config.session.is_simulation else config.REAL_TX_PER_SECOND
        safe_delay = (1.0 / tps) * 1.2  # 20% 여유 버퍼

        for item in targets:
            if not self.is_running: break
            
            # [추가] API 호출 전 대기 (Rate Limit 방지)
            time.sleep(safe_delay)
            
            code = item['code']; name = item['name']
            
            # [추가] 보유 중이면 스킵
            if code in holding_codes: continue
            
            # [추가] 시장 지수 필터링 (종목별 적용)
            if getattr(config, 'USE_MARKET_FILTER', True):
                market_type = self._get_stock_market_type(code)
                # 해당 시장이 하락장이면 스킵 (기본값 True로 설정하여 데이터 없을 시 매수 허용)
                market_stat = self.market_index_status.get(market_type)
                if market_stat and isinstance(market_stat, dict):
                    if not market_stat.get('is_healthy', True):
                        self.skipped_by_market_filter_count += 1
                        skipped_stocks.append(f"{name}")
                        continue
            
            # [설명] 장 중에는 당일 실시간 시세가 반영된 일봉 데이터를 가져옵니다.
            df = api.get_chart_data(code, is_overseas=False)
            if df is None or df.empty: continue
            
            current_price = float(df.iloc[-1]['close'])
            
            # [Refactoring] Use Strategy for analysis
            result = self.strategy.analyze_buy(code, name, df, current_price)
            if not result: continue
            
            rsi_val = f"{result['rsi']:.1f}" if result['rsi'] is not None else "-"
            adx_val = f"{result['adx']:.1f}" if result['adx'] is not None else "-"
            cci_val = f"{result['cci']:.1f}" if result['cci'] is not None else "-"
            self.log(f"[분석] {name}({code}): 현재가={current_price:,.0f}, 점수={result['score']}, 상태={result['state']}, RSI={rsi_val}, ADX={adx_val}, CCI={cci_val}")
            
            if result['action'] == "buy":
                candidates.append({
                    'code': code, 'name': name, 'price': current_price,
                    'score': result['score'], 'rsi': result['rsi'], 'adx': result['adx'], 'cci': result['cci']
                })

        # [추가] 시장 필터링 보류 종목 로그 기록
        if skipped_stocks:
            self.log(f"[시장 필터링] 하락장 매수 보류 ({len(skipped_stocks)}종목): {', '.join(skipped_stocks)}")

        # [수정] 우선순위 정렬 (1. 점수 높은 순, 2. RSI 낮은 순)
        # 점수가 같다면 RSI가 낮을수록 상승 여력이 있다고 판단하여 우선순위를 둡니다.
        candidates.sort(key=lambda x: (-x['score'], x['rsi']))
        
        # [추가] 선정된 후보군 우선순위 로그 출력
        if candidates:
            log_cnt = min(len(candidates), 3)
            self.log(f"[매수 후보 선정] 총 {len(candidates)}종목 중 우선순위 상위 {log_cnt}개:")
            for i in range(log_cnt):
                c = candidates[i]
                rsi_disp = f"{c['rsi']:.1f}" if c['rsi'] is not None else "-"
                self.log(f"   {i+1}순위: {c['name']} (점수:{c['score']}, RSI:{rsi_disp})")
        
        return candidates

    def _allocate_budget(self, avail_cash, invest_ratio):
        # 투자 금액 계산 (초기 자산의 N%)
        if self.initial_asset > 0:
            target_invest_amt = int(self.initial_asset * invest_ratio)
        else:
            target_invest_amt = int(avail_cash * invest_ratio)
        
        # 실제 집행 금액은 목표 금액과 현재 예수금 중 작은 값 (예수금 초과 불가)
        invest_amt = min(target_invest_amt, avail_cash)
        
        if config.FILE_DEBUG_LEVEL == "DEBUG":
            logger.debug(f"[자산배분] 목표금액: {target_invest_amt:,}원 (초기자산의 {invest_ratio*100:.0f}%) / 현재예수금: {avail_cash:,}원 -> 투자금액: {invest_amt:,}원")
            
        return invest_amt

    def _execute_buy_orders(self, candidates, avail_cash, invest_ratio, current_holdings_count, max_holdings):
        for cand in candidates:
            if not self.is_running: break
            if avail_cash < 50000: break
            
            # [추가] 최대 보유 종목 수 도달 시 추가 매수 중단
            if current_holdings_count >= max_holdings:
                self.log(f"매수 중단: 최대 보유 종목 수({max_holdings}개) 도달")
                break

            # [수정] 자산 배분 로직 개선: 마지막 슬롯인 경우 남은 예수금 전액 투자
            remaining_slots = max_holdings - current_holdings_count
            if remaining_slots == 1:
                invest_amt = avail_cash
            else:
                invest_amt = self._allocate_budget(avail_cash, invest_ratio)

            # 최소 주문 금액 보정 (너무 적으면 1주라도 살 수 있게)
            if invest_amt < cand['price']: invest_amt = avail_cash
            
            # [수정] 지정가 주문을 위해 현재가(정수) 확보
            current_price = int(cand['price'])

            # [추가] 매수 체결 확률을 높이기 위해 +1호가 적용
            tick_size = self._get_tick_size(current_price)
            order_price = current_price + tick_size
            
            # [수정] 단순 계산 대신 API를 통해 정확한 매수 가능 수량 조회
            # 지정가 주문 시 해당 가격 기준으로 조회
            max_qty = api.fetch_buyable_quantity(cand['code'], order_price)
            
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

            rsi_val = f"{cand['rsi']:.1f}" if cand['rsi'] else "-"
            adx_val = f"{cand['adx']:.1f}" if cand['adx'] else "-"
            cci_val = f"{cand['cci']:.1f}" if cand.get('cci') else "-"
            reason = f"조건 만족 [점수:{cand['score']}, RSI:{rsi_val}, ADX:{adx_val}, CCI:{cci_val}]"
            
            self.log(f"매수 실행: {cand['name']} - {reason}")
            # [수정] 매수 시 사유와 점수, 그리고 지정가 가격을 DB 저장을 위해 전달
            odno = self._send_order(cand['code'], qty, "buy", name=cand['name'], reason=reason, score=cand['score'], price=order_price)
            if odno: 
                avail_cash -= (qty * order_price)
                current_holdings_count += 1 # [추가] 보유 종목 수 증가 반영
                record = {
                    "type": "buy",
                    "code": cand['code'],
                    "name": cand['name'],
                    "qty": qty,
                    "price": order_price,
                    "reason": reason,
                    "time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                    "odno": odno
                }
                self.trade_records.append(record)

    def _update_market_indices_status(self):
        """KOSPI, KOSDAQ 지수 상태 업데이트 및 알림"""
        # [수정] KIS API 사용을 위한 종목 코드 변경 (KOSPI: 0001, KOSDAQ: 1001)
        target_indices = {"KOSPI": "0001", "KOSDAQ": "1001"}
        # [추가] Fallback용 yfinance 티커
        yf_tickers = {"KOSPI": "^KS11", "KOSDAQ": "^KQ11"}
        
        ma_period = getattr(config, 'MARKET_FILTER_MA', 20)
        
        for market_name, ticker in target_indices.items():
            try:
                # [수정] KIS API를 통한 지수 차트 조회
                df = api.get_domestic_index_chart(ticker)
                
                # [추가] KIS API 실패 시 yfinance로 재시도 (Fallback)
                if df is None or df.empty or len(df) < ma_period:
                    yf_ticker = yf_tickers.get(market_name)
                    if yf_ticker:
                        logger.debug(f"[지수] KIS API 조회 실패. yfinance로 재시도합니다: {yf_ticker}")
                        df = api.get_chart_data(yf_ticker, is_overseas=True)

                if df is None or df.empty or len(df) < ma_period:
                    self.market_index_status[market_name] = {"is_healthy": True, "current": 0}
                    continue
                
                ma_val = df['close'].rolling(window=ma_period).mean().iloc[-1]
                current_idx = df['close'].iloc[-1]
                is_healthy = current_idx >= ma_val
                
                self.market_index_status[market_name] = {
                    "is_healthy": is_healthy,
                    "current": current_idx
                }
                
                # 상태 변경 알림
                notified = self.market_status_notified.get(market_name, False)
                if not is_healthy and not notified:
                    api.send_telegram_message(f"📉 [시장 감지] {market_name} 지수가 {ma_period}일 이평선 아래로 하락했습니다.\n해당 시장 종목의 신규 매수를 일시 중단합니다.")
                    self.market_status_notified[market_name] = True
                elif is_healthy and notified:
                    api.send_telegram_message(f"📈 [시장 회복] {market_name} 지수가 {ma_period}일 이평선을 회복했습니다.\n매수를 재개합니다.")
                    self.market_status_notified[market_name] = False
            except Exception as e:
                self.log(f"{market_name} 지수 조회 실패: {e}")
                self.market_index_status[market_name] = True

    def _get_stock_market_type(self, code):
        """종목 코드로 시장 구분(KOSPI/KOSDAQ) 확인 (캐싱 적용)"""
        if code in self.stock_market_map: return self.stock_market_map[code]
        
        try:
            res = api.get_current_price_data(code, is_overseas=False)
            if res and res.get('rt_cd') == '0':
                market_name = res['output'].get('rprs_mrkt_kor_name', '')
                if "코스닥" in market_name:
                    self.stock_market_map[code] = "KOSDAQ"
                    return "KOSDAQ"
                elif "유가증권" in market_name or "KOSPI" in market_name:
                    self.stock_market_map[code] = "KOSPI"
                    return "KOSPI"
        except: pass
        
        return "KOSPI" # 기본값

    def _send_order(self, code, qty, type_str, name=None, profit_amt=0, profit_rate=0.0, reason=None, score=0, price=0):
        # [수정] 지정가/시장가 구분 (price > 0 이면 지정가)
        ord_dvsn = "00" if price > 0 else "01"
        
        # [추가] 상세 로그: 요청 정보
        self.log(f"======== [주문 실행] {type_str.upper()} ========")
        price_log = f"{price:,}원(지정가)" if price > 0 else "시장가(0)"
        self.log(f"대상: {code}, 수량: {qty}, 단가: {price_log}")

        try:
            # api.place_order 사용 (국내 전용)
            res_json = api.place_order("domestic", type_str, code, qty, price, ord_dvsn)
            
            if res_json['rt_cd'] == '0':
                odno = res_json['output']['ODNO']
                success_msg = f"[{datetime.now().strftime('%H:%M:%S')}] {type_str.upper()} 성공 | {code} | {qty}주 | No.{odno}"
                self.trade_history.append(success_msg)
                self.log(f"결과: 성공 (주문번호: {odno})")
                stock_display = f"{name}({code})" if name else code
                msg = f"🚀 [주문 접수] {type_str.upper()} {stock_display} {qty}주 ({price_log})\n주문번호: {odno}"
                if reason:
                    msg += f"\n사유: {reason}"
                api.send_telegram_message(msg)
                
                # [DB] 시스템 트레이딩 주문 기록 (스냅샷 및 상세 정보 포함)
                snapshot = analysis.get_snapshot(code, is_overseas=False)
                
                if config.FILE_DEBUG_LEVEL == "DEBUG":
                    logger.debug(f"[AutoTrade] 주문 접수 DB 저장 시도: {odno}")
                db_manager.db.insert_trade(f"{type_str}(AUTO)", code, name, qty, str(price), odno, snapshot=snapshot, profit_amt=profit_amt, profit_rate=profit_rate, reason=reason, score=score)
                
                # [추가] 체결 감시자에게 즉시 확인 요청
                ConclusionMonitor().check_now()
                
                return odno
            else:
                err_msg = res_json.get('msg1', 'Unknown Error')
                msg_cd = res_json.get('msg_cd')
                self.log(f"결과: 실패 ({err_msg}) [Code: {msg_cd}]")
                
                # [추가] 주문 실패 알림
                stock_display = f"{name}({code})" if name else code
                fail_msg = f"🚫 [주문 실패] {type_str.upper()} {stock_display}\n수량: {qty}주 / 단가: {price_log}\n원인: {err_msg} (Code: {msg_cd})"
                api.send_telegram_message(fail_msg)
        except Exception as e:
            self.log(f"결과: 에러 발생 ({str(e)})")
            
            # [추가] 주문 에러 알림
            stock_display = f"{name}({code})" if name else code
            fail_msg = f"🚫 [주문 에러] {type_str.upper()} {stock_display}\n수량: {qty}주 / 단가: {price_log}\n에러: {str(e)}"
            api.send_telegram_message(fail_msg)
        finally:
            self.log("========================================")
        return None

def system_trading_menu():
    """시스템 트레이딩 메뉴"""

    trader = AutoTrader()

    console.print("\n[bold yellow]=== 시스템 트레이딩 ===[/]")
    console.print("[dim]안내: 시스템 트레이딩은 현재 '국내주식' 리스트를 대상으로만 작동합니다.[/dim]")
    console.print(f"현재 상태: {'[green]실행 중[/green]' if trader.is_running else '[red]중지됨[/red]'}")
    console.print()
    console.print("[1] 트레이딩 실행 (Start)")
    console.print("[2] 트레이딩 중단 (Stop)")
    console.print("[3] 트레이딩 상태 (Status)")
    console.print("[4] 트레이딩 평가 (Report)")
    console.print("[5] 트레이딩 로그 (Log Viewer)")
    console.print()
    
    try:
        choice = Prompt.ask("선택 [dim](취소: q)[/dim]", choices=["1", "2", "3", "4", "5", "q"], default="3")
        
        menu_map = {"1": "실행", "2": "중단", "3": "상태", "4": "평가", "5": "로그"}
        if choice in menu_map:
            config.USER_ACTION_BREADCRUMB.append(f"[{choice}] {menu_map[choice]}")
            
    except KeyboardInterrupt:
        console.print()
        return

    if choice.lower() == 'q': return
    
    logger.info(f"운영자 실행: {' - '.join(config.USER_ACTION_BREADCRUMB)}")
    
    if choice == "1":
        trader.start()
    elif choice == "2":
        trader.stop()
    elif choice == "3":
        trader.print_status()
    elif choice == "4":
        trader.print_report()
    elif choice == "5":
        trader.view_log_file()