import threading
import time
import json
import os
from datetime import datetime
from rich.prompt import Prompt
from rich.markup import escape
from rich.table import Table
from rich import box
from rich.rule import Rule
import config
import api
import utils
import indicators
from modules import account, analysis

console = config.console

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
            cls._instance.order_status = {} # [추가] 주문별 체결 수량 추적 {odno: qty}
            
            # [추가] 로그 디렉토리 확인 및 생성
            log_dir = getattr(config, 'SYSTEM_TRADING_LOG_DIR', 'logs')
            if not os.path.exists(log_dir):
                try:
                    os.makedirs(log_dir)
                except Exception as e:
                    console.print(f"[red]로그 디렉토리 생성 실패: {e}[/red]")
        return cls._instance

    def start(self):
        if self.is_running:
            console.print("\n[yellow]이미 자동매매가 실행 중입니다.[/yellow]")
            return
        
        if not config.IS_SIMULATION:
            console.print("[bold red]실전 투자 모드에서는 자동매매를 실행할 수 없습니다.[/bold red]")
            return

        self.is_running = True
        self.start_time = datetime.now()
        self.consecutive_errors = 0
        self.was_market_open = self.is_market_open()
        
        # [추가] 시작 시점 총 자산 계산 (손실 제한 기준점)
        self.initial_asset = self._get_total_estimated_asset()
        if self.initial_asset > 0:
            self.log(f"시스템 시작 자산: {self.initial_asset:,}원")
        
        # [추가] API 모듈에서 로그를 남길 수 있도록 연결
        config.SYSTEM_LOGGER = self.log
        
        self.thread = threading.Thread(target=self._run_loop, daemon=True)
        self.thread.start()
        console.print("\n[green]자동매매 시스템이 시작되었습니다. (백그라운드)[/green]")
        self.log("시스템 시작")

    def stop(self):
        if not self.is_running:
            console.print("[yellow]실행 중인 자동매매가 없습니다.[/yellow]")
            return
            
        self.is_running = False
        
        # [추가] 로거 연결 해제
        config.SYSTEM_LOGGER = None
        
        if self.thread:
            self.thread.join(timeout=5)
        console.print("\n[red]자동매매 시스템이 중단되었습니다.[/red]")
        self.log("시스템 중단")

    def log(self, msg):
        now = datetime.now()
        timestamp = now.strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]
        log_msg = f"[{timestamp}] {msg}"
        self.logs.append(log_msg)
        if len(self.logs) > 300: self.logs.pop(0)
        
        # [추가] 파일에 로그 저장 (날짜별 분리)
        try:
            log_dir = getattr(config, 'SYSTEM_TRADING_LOG_DIR', 'logs')
            date_str = now.strftime("%Y-%m-%d")
            filename = f"system_trade_{date_str}.log"
            
            daily_log_path = os.path.join(log_dir, filename)
            
            with open(daily_log_path, 'a', encoding='utf-8') as f:
                f.write(log_msg + "\n")
        except Exception: pass

    def print_status(self):
        status_color = "green" if self.is_running else "red"
        status_text = "실행 중 (RUNNING)" if self.is_running else "중지됨 (STOPPED)"
        
        console.print(f"\n[bold]=== 시스템 트레이딩 상태 ({status_text}) ===[/bold]", style=status_color)
        
        # 1. 실행 시간 정보
        if self.is_running and self.start_time:
            start_str = self.start_time.strftime("%Y-%m-%d %H:%M:%S")
            elapsed = datetime.now() - self.start_time
            elapsed_str = str(elapsed).split('.')[0]
            console.print(f"• 실행 시간: {start_str} (경과: {elapsed_str})")
        
        # 2. 마켓 상태
        market_status = "장 운영 중 (거래 가능)" if self.is_market_open() else "장 마감/휴장 (대기 중)"
        if datetime.now().weekday() > 4: market_status = "주말 휴장 (대기 중)"
        console.print(f"• 마켓 상태: {market_status}")

        # 3. 자산 및 손익 현황 (안전성 핵심)
        current_asset = None
        deposit = 0
        
        with console.status("[bold green]트레이딩 상태 및 자산 정보 조회 중...[/]"):
            current_asset = self._get_total_estimated_asset()
            
            # 예수금 별도 조회 (매수 여력 확인용)
            try:
                tr_id = utils.get_tr_id("domestic", "inquiry", "deposit")
                url = f"{config.URL_BASE}/uapi/domestic-stock/v1/trading/inquire-psbl-order"
                headers = utils.get_common_headers(tr_id)
                params = {"CANO": config.CANO, "ACNT_PRDT_CD": config.ACNT_PRDT_CD, "PDNO": "005930", "ORD_UNPR": "0", "ORD_DVSN": "01", "CMA_EVLU_AMT_ICLD_YN": "Y", "OVRS_ICLD_YN": "Y", "CRDT_TYPE": "00"}
                res = api.session.get(url, headers=headers, params=params, verify=False)
                if res.json()['rt_cd'] == '0': deposit = int(res.json()['output']['ord_psbl_cash'])
            except: pass

        console.print("\n[bold cyan][자산 및 손익 현황][/bold cyan]")
        if self.initial_asset > 0:
            if current_asset is not None:
                profit = current_asset - self.initial_asset
                rate = (profit / self.initial_asset) * 100
                color = "[red]" if profit > 0 else ("[blue]" if profit < 0 else "[white]")
                
                console.print(f"  - 초기 자산: {self.initial_asset:,}원")
                console.print(f"  - 현재 자산: {current_asset:,}원")
                console.print(f"  - 누적 손익: {color}{profit:+,}원 ({rate:+.2f}%)[/]")
                console.print(f"  - 주문 가능: {deposit:,}원")
                
                # 일일 손실 제한 체크
                loss_limit = getattr(config, 'SYSTEM_DAILY_LOSS_LIMIT', 0.0)
                if loss_limit > 0:
                    safety_msg = "[green]안전[/green]"
                    if rate <= -loss_limit: safety_msg = "[bold red]위험 (한도 초과)[/bold red]"
                    elif rate <= -(loss_limit * 0.8): safety_msg = "[bold orange3]주의 (한도 임박)[/bold orange3]"
                    console.print(f"  - 손실 제한: -{loss_limit}% (상태: {safety_msg})")
            else:
                console.print("  - [bold red]자산 정보 조회 실패 (통신 오류)[/bold red]")
                console.print(f"  - 초기 자산: {self.initial_asset:,}원")
        else:
            console.print("  - 자산 정보 로딩 중...")

        # 4. 시스템 안정성 (에러 카운트)
        console.print("\n[bold cyan][시스템 안정성][/bold cyan]")
        err_cnt = self.consecutive_errors
        max_err = getattr(config, 'SYSTEM_MAX_CONSECUTIVE_ERRORS', 5)
        err_color = "[green]" if err_cnt == 0 else ("[red]" if err_cnt >= max_err else "[yellow]")
        console.print(f"  - 연속 에러: {err_color}{err_cnt} / {max_err}회[/]")
        
        # 5. 매매 요약
        buy_cnt = len([x for x in self.trade_records if x['type'] == 'buy'])
        sell_cnt = len([x for x in self.trade_records if x['type'] == 'sell'])
        console.print(f"  - 금일 매매: 매수 {buy_cnt}건 / 매도 {sell_cnt}건")

        console.print()

    def print_report(self):
        console.print("\n[bold yellow]=== 시스템 트레이딩 리포트 ===[/]")
        
        if not self.trade_records:
            console.print("\n[yellow]매매 기록이 없습니다.[/yellow]")
            return

        # 통계 계산
        total_trades = len(self.trade_records)
        buy_trades = [r for r in self.trade_records if r['type'] == 'buy']
        sell_trades = [r for r in self.trade_records if r['type'] == 'sell']
        
        win_trades = 0
        loss_trades = 0
        total_profit = 0
        total_profit_rate = 0.0
        
        # [추가] 보유 기간 계산
        total_holding_seconds = 0
        holding_count = 0
        buy_times = {} # code -> list of datetime

        # 시간순 처리를 위해 전체 기록 순회
        for r in self.trade_records:
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

        # 요약 테이블
        summary_table = Table(box=box.HORIZONTALS, show_header=False, border_style="dim")
        summary_table.add_column("항목", style="cyan", justify="left")
        summary_table.add_column("값", justify="left")
        
        summary_table.add_row("총 매매 실행", f"{total_trades}건 (매수 {len(buy_trades)} / 매도 {len(sell_trades)})")
        
        if sell_trades:
            summary_table.add_row("승률 (Win Rate)", f"{win_rate:.1f}% ({win_trades}승 {loss_trades}패)")
            summary_table.add_row("총 실현 손익", f"[red]{total_profit:+,}원[/]" if total_profit > 0 else f"[blue]{total_profit:+,}원[/]")
            summary_table.add_row("평균 수익률", f"[red]{avg_profit_rate:+.2f}%[/]" if avg_profit_rate > 0 else f"[blue]{avg_profit_rate:+.2f}%[/]")
            summary_table.add_row("평균 보유 기간", avg_holding_str)
        
        console.print(summary_table)

        # [추가] 종목별 성과 분석
        stock_stats = {}
        for r in self.trade_records:
            code = r['code']
            if code not in stock_stats:
                stock_stats[code] = {'name': r['name'], 'buy': 0, 'sell': 0, 'profit': 0, 'rates': [], 'wins': 0}
            
            if r['type'] == 'buy':
                stock_stats[code]['buy'] += 1
            elif r['type'] == 'sell':
                stock_stats[code]['sell'] += 1
                p = r.get('profit_amt', 0)
                stock_stats[code]['profit'] += p
                stock_stats[code]['rates'].append(r.get('profit_rate', 0.0))
                if p > 0: stock_stats[code]['wins'] += 1

        if stock_stats:
            console.print("\n[bold]종목별 성과 분석[/bold]")
            s_table = Table(box=box.HORIZONTALS, header_style="dim", border_style="dim")
            s_table.add_column("종목명(코드)", justify="left")
            s_table.add_column("매매(매수/매도)", justify="center")
            s_table.add_column("승률", justify="right")
            s_table.add_column("총 손익", justify="right")
            s_table.add_column("평균 수익률", justify="right")

            for code, stat in stock_stats.items():
                s_cnt = stat['sell']
                win_rate = (stat['wins'] / s_cnt * 100) if s_cnt > 0 else 0.0
                avg_rate = (sum(stat['rates']) / s_cnt) if s_cnt > 0 else 0.0
                
                p_color = "[red]" if stat['profit'] > 0 else ("[blue]" if stat['profit'] < 0 else "[white]")
                r_color = "[red]" if avg_rate > 0 else ("[blue]" if avg_rate < 0 else "[white]")
                
                s_table.add_row(
                    f"{stat['name']} ({code})",
                    f"{stat['buy']} / {stat['sell']}",
                    f"{win_rate:.1f}%",
                    f"{p_color}{stat['profit']:+,}원[/]",
                    f"{r_color}{avg_rate:+.2f}%[/]"
                )
            console.print(s_table)
        
        # 상세 내역 테이블
        console.print("\n[bold]상세 매매 내역 (최신순)[/bold]")
        detail_table = Table(box=box.HORIZONTALS, header_style="dim", border_style="dim")
        detail_table.add_column("시간", justify="center")
        detail_table.add_column("구분", justify="center")
        detail_table.add_column("종목명", justify="left")
        detail_table.add_column("수량", justify="right")
        detail_table.add_column("단가(현재가)", justify="right")
        detail_table.add_column("손익/비고", justify="right")
        
        for r in reversed(self.trade_records):
            type_str = "[red]매수[/]" if r['type'] == 'buy' else "[blue]매도[/]"
            price_str = f"{r['price']:,}"
            
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
        date_str = datetime.now().strftime("%Y-%m-%d")
        filename = f"system_trade_{date_str}.log"
        filepath = os.path.join(log_dir, filename)

        if not os.path.exists(filepath):
            console.print(f"\n[yellow]오늘 날짜({date_str})의 로그 파일이 없습니다.[/yellow]")
            return

        console.print(f"\n[bold cyan]=== 실시간 로그 모니터링 ({filename}) ===[/bold cyan]")
        console.print("[dim]종료하려면 Ctrl+C를 누르세요.[/dim]\n")

        try:
            with open(filepath, 'r', encoding='utf-8') as f:
                # 초기 출력: 최근 50줄
                lines = f.readlines()
                for line in lines[-50:]:
                    console.print(escape(line.strip()))
                
                # 실시간 모니터링
                while True:
                    line = f.readline()
                    if line:
                        console.print(escape(line.strip()))
                    else:
                        time.sleep(0.1)
        except KeyboardInterrupt:
            console.print("\n[yellow]로그 모니터링을 종료합니다.[/yellow]")
        except Exception as e:
            console.print(f"\n[red]로그 파일 읽기 오류: {e}[/red]")

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
                self.log("모니터링 주기 시작...")
                
                current_market_status = self.is_market_open()
                
                # [추가] 장 시작/마감 상태 변경 감지 및 로그
                if self.was_market_open is not None:
                    if not self.was_market_open and current_market_status:
                        self.log("=" * 80)
                        self.log(f"📢 [거래 시작] 시스템 트레이딩 거래가 시작되었습니다. ({datetime.now().strftime('%H:%M')})")
                        self.log("=" * 80)
                    elif self.was_market_open and not current_market_status:
                        self.log("=" * 80)
                        self.log(f"💤 [거래 종료] 시스템 트레이딩 거래가 종료되었습니다. ({datetime.now().strftime('%H:%M')})")
                        self.log("=" * 80)
                
                self.was_market_open = current_market_status
                
                if current_market_status:
                    self.log("시스템 상태: RUNNING")
                    # [수정] 시스템 트레이딩 작업 구간을 락으로 보호 (API 우선권 확보)
                    with config.SYSTEM_TRADING_LOCK:
                        # 1. 매도 조건 점검 (리스크 관리)
                        self._check_sell_conditions()
                        # 2. 매수 조건 점검
                        self._check_buy_conditions()
                        # [추가] 체결 내역 확인 (주문 체결 모니터링)
                        self._check_conclusions()
                        # [추가] 보유 종목 상태 로깅 및 자산 안전장치 체크
                        self._monitor_account_status()
                else:
                    self.log("시스템 상태: WAITING (거래 시간 외)")
                
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
                    self.log(f"[비상 정지] 연속 에러 {max_err}회 발생으로 시스템을 중단합니다.")
                    self.stop()
                    break
                time.sleep(10)

    def _check_conclusions(self):
        """금일 체결 내역을 확인하고 로그에 기록"""
        try:
            # API 호출 전 대기 (Rate Limit 방지)
            time.sleep(0.2)
            
            tr_id = utils.get_tr_id("domestic", "inquiry", "history")
            url = f"{config.URL_BASE}/uapi/domestic-stock/v1/trading/inquire-daily-ccld"
            headers = utils.get_common_headers(tr_id)
            today = datetime.now().strftime("%Y%m%d")
            
            params = {
                "CANO": config.CANO, 
                "ACNT_PRDT_CD": config.ACNT_PRDT_CD, 
                "INQR_STRT_DT": today, 
                "INQR_END_DT": today, 
                "SLL_BUY_DVSN_CD": "00", 
                "INQR_DVSN": "00", 
                "PDNO": "", 
                "CCLD_DVSN": "01", # 주문별 체결 합계
                "ORD_GNO_BRNO": "", 
                "ODNO": "", 
                "INQR_DVSN_3": "00", 
                "INQR_DVSN_1": "", 
                "CTX_AREA_FK100": "", 
                "CTX_AREA_NK100": ""
            }
            
            res = api.session.get(url, headers=headers, params=params, verify=False, timeout=config.DEFAULT_TIMEOUT)
            data = res.json()
            
            if data['rt_cd'] == '0':
                trades = data.get('output1', [])
                for item in trades:
                    odno = item.get('odno')
                    if not odno: continue
                    
                    tot_ccld_qty = int(item.get('tot_ccld_qty', 0))
                    if tot_ccld_qty <= 0: continue
                    
                    prev_qty = self.order_status.get(odno, 0)
                    
                    if tot_ccld_qty > prev_qty:
                        # 새로운 체결 발생
                        new_qty = tot_ccld_qty - prev_qty
                        avg_price = float(item.get('avg_prvs', 0))
                        name = item.get('prdt_name')
                        code = item.get('pdno')
                        type_name = item.get('sll_buy_dvsn_cd_name')
                        type_cd = item.get('sll_buy_dvsn_cd') # 01:매도, 02:매수
                        
                        trade_amt = int(new_qty * avg_price)
                        add_info = ""
                        
                        if type_cd == '02': # 매수
                            add_info = f" (매수금액: {trade_amt:,.0f}원)"
                        elif type_cd == '01': # 매도
                            # trade_records에서 해당 주문번호의 예상 수익률 조회
                            found_rate = None
                            for r in self.trade_records:
                                if r.get('odno') == odno:
                                    found_rate = r.get('profit_rate')
                                    break
                            if found_rate is not None:
                                r_color = "red" if found_rate > 0 else "blue"
                                add_info = f" (수익률: [{r_color}]{found_rate:+.2f}%[/], 매도금액: {trade_amt:,.0f}원)"
                            else:
                                add_info = f" (매도금액: {trade_amt:,.0f}원)"
                        
                        # 로그 기록
                        self.log(f"✅ [체결 확인] {type_name} {name}({code}) {new_qty}주 체결 (단가: {avg_price:,.0f}원){add_info}")
                        
                        # 상태 업데이트
                        self.order_status[odno] = tot_ccld_qty
                        
        except Exception as e:
            self.log(f"체결 내역 조회 중 오류: {str(e)}")

    def _monitor_account_status(self):
        """현재 보유 종목 상태 로깅 및 자산 손실 제한(Loss Cut) 체크"""
        try:
            # API 호출 전 대기 (Rate Limit 방지)
            time.sleep(0.2)
            holdings, summary = account.fetch_domestic_balance()
            
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
                if summary:
                    total_profit = api.safe_int(summary.get('evlu_pfls_smtl_amt'))
                    total_eval = api.safe_int(summary.get('scts_evlu_amt'))
                    self.log(f"   총 평가금액: {total_eval:,}원  |  총 평가손익: {total_profit:+,}원")
                    
                    # [추가] 일일 손실 제한 체크
                    self._check_loss_limit(total_eval)
                    
        except Exception: pass

    def _get_total_estimated_asset(self):
        """현재 총 추정 자산(예수금 + 주식평가금) 계산"""
        try:
            # 1. 예수금 조회
            tr_id = utils.get_tr_id("domestic", "inquiry", "deposit")
            url = f"{config.URL_BASE}/uapi/domestic-stock/v1/trading/inquire-psbl-order"
            headers = utils.get_common_headers(tr_id)
            params = {"CANO": config.CANO, "ACNT_PRDT_CD": config.ACNT_PRDT_CD, "PDNO": "005930", "ORD_UNPR": "0", "ORD_DVSN": "01", "CMA_EVLU_AMT_ICLD_YN": "Y", "OVRS_ICLD_YN": "Y", "CRDT_TYPE": "00"}
            res = api.session.get(url, headers=headers, params=params, verify=False)
            cash = 0
            if res.json()['rt_cd'] == '0': cash = int(res.json()['output']['ord_psbl_cash'])
            else: return None # [수정] 예수금 조회 실패 시 None 반환
            
            # 2. 주식 평가금 조회
            _, summary = account.fetch_domestic_balance()
            stock_eval = 0
            if summary: stock_eval = api.safe_int(summary.get('scts_evlu_amt'))
            else: return None # [수정] 잔고 조회 실패 시 None 반환
            
            return cash + stock_eval
        except: return None

    def _check_loss_limit(self, current_stock_eval):
        """자산 변동을 체크하여 손실 한도 초과 시 비상 정지"""
        loss_limit_pct = getattr(config, 'SYSTEM_DAILY_LOSS_LIMIT', 0.0)
        if loss_limit_pct <= 0 or self.initial_asset <= 0: return

        # 현재 총 자산 재계산 (예수금 + 현재 주식 평가금)
        current_total = self._get_total_estimated_asset()
        if current_total is None or current_total == 0: return # [수정] 조회 실패 시 체크 건너뜀

        loss_rate = (current_total - self.initial_asset) / self.initial_asset * 100
        
        if loss_rate <= -loss_limit_pct:
            self.log(f"[비상 정지] 일일 손실 한도 초과! (현재: {loss_rate:.2f}% / 제한: -{loss_limit_pct}%)")
            self.log(f"시작 자산: {self.initial_asset:,}원 -> 현재 자산: {current_total:,}원")
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

    def _check_sell_conditions(self):
        holdings, _ = account.fetch_domestic_balance()
        if not holdings: return

        stop_loss_rate = config.SELL_STRATEGY["STOP_LOSS_RATE"]
        take_profit_rate = config.SELL_STRATEGY["TAKE_PROFIT_RATE"]

        # [추가] Rate Limit 준수를 위한 딜레이 설정
        # 모의투자: 초당 2건 -> 0.5초 + 여유 / 실전투자: 초당 20건 -> 0.05초 + 여유
        tps = config.SIM_TX_PER_SECOND if config.IS_SIMULATION else config.REAL_TX_PER_SECOND
        safe_delay = (1.0 / tps) * 1.2  # 20% 여유 버퍼

        for item in holdings:
            if not self.is_running: break
            code = item['pdno']; name = item['prdt_name']
            
            # [수정] 보유수량(hldg_qty) 대신 주문가능수량(ord_psbl_qty) 사용
            # 미체결 매도 주문이 있을 경우 중복 매도를 방지하기 위함
            qty = int(item.get('ord_psbl_qty', 0))
            profit_rate = float(item['evlu_pfls_rt'])
            
            # [추가] API 호출 전 대기 (Rate Limit 방지)
            time.sleep(safe_delay)
            
            if qty <= 0: 
                continue # 주문 가능 수량이 없으면 스킵

            reason = ""
            if profit_rate <= stop_loss_rate: reason = f"손절({profit_rate}%)"
            elif profit_rate >= take_profit_rate: reason = f"익절({profit_rate}%)"
            
            # [수정] 손절/익절 여부와 관계없이 항상 기술적 분석 수행 및 로그 출력
            # [설명] 장 중에는 당일 실시간 시세가 반영된 일봉 데이터를 가져옵니다.
            df = api.get_chart_data(code, is_overseas=False)
            if df is not None and not df.empty:
                ind = indicators.calculate_indicators(df)
                prev_rsi = self._get_prev_rsi(df)
                current_price = float(item['prpr'])
                
                state, _ = analysis.classify_stock_state(
                    current_price, ind['ema_20'], ind['ema_60'], ind['ema_120'], 
                    ind['psar'], ind['rsi'], prev_rsi, ind['adx'], ind['cci'], ind.get('obv_trend')
                )
                
                # [추가] 분석 로그 기록
                score, _ = analysis.calculate_score(current_price, ind['ema_20'], ind['ema_60'], ind['ema_120'], ind['psar'], ind['rsi'], ind['adx'], ind['cci'], ind.get('obv_trend'))
                rsi_val = f"{ind['rsi']:.1f}" if ind['rsi'] is not None else "-"
                adx_val = f"{ind['adx']:.1f}" if ind['adx'] is not None else "-"
                self.log(f"[보유분석] {name}({code}): 점수={score}, 상태={state}, RSI={rsi_val}, ADX={adx_val}")

                # 이미 손절/익절 사유가 있다면 그것을 우선하고, 없다면 추세 이탈 여부를 체크
                if not reason:
                    if state in ["관망", "주의", "위험"]:
                        reason = f"추세이탈({state}) [점수:{score}, RSI:{rsi_val}, ADX:{adx_val}]"

            if reason:
                self.log(f"매도 실행: {name} - {reason}")
                odno = self._send_order(code, qty, "sell")
                if odno:
                    # 매도 성공 시 기록 (추정치)
                    record = {
                        "type": "sell",
                        "code": code,
                        "name": name,
                        "qty": qty,
                        "price": float(item['prpr']),
                        "profit_rate": profit_rate,
                        "profit_amt": int(item['evlu_pfls_amt']),
                        "reason": reason,
                        "time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                        "odno": odno
                    }
                    self.trade_records.append(record)

    def _check_buy_conditions(self):
        targets = config.STOCK_CONFIG_DATA.get("stocks_kr", [])
        if not targets: return
        
        # [추가] 보유 종목 조회 (중복 매수 방지)
        holdings, _ = account.fetch_domestic_balance()
        holding_codes = set()
        if holdings:
            for h in holdings:
                holding_codes.add(h['pdno'])
        
        # 예수금 확인 (API 직접 호출)
        tr_id = utils.get_tr_id("domestic", "inquiry", "deposit")
        url = f"{config.URL_BASE}/uapi/domestic-stock/v1/trading/inquire-psbl-order"
        headers = utils.get_common_headers(tr_id)
        params = {"CANO": config.CANO, "ACNT_PRDT_CD": config.ACNT_PRDT_CD, "PDNO": "005930", "ORD_UNPR": "0", "ORD_DVSN": "01", "CMA_EVLU_AMT_ICLD_YN": "Y", "OVRS_ICLD_YN": "Y", "CRDT_TYPE": "00"}
        
        avail_cash = 0
        try:
            res = api.session.get(url, headers=headers, params=params, verify=False)
            if res.json()['rt_cd'] == '0': avail_cash = int(res.json()['output']['ord_psbl_cash'])
        except: return

        if avail_cash < 50000: return # 최소 주문 가능 금액 설정

        # [추가] Rate Limit 준수를 위한 딜레이 설정
        tps = config.SIM_TX_PER_SECOND if config.IS_SIMULATION else config.REAL_TX_PER_SECOND
        safe_delay = (1.0 / tps) * 1.2  # 20% 여유 버퍼

        # [수정] 1단계: 전체 종목 분석 및 매수 후보 추출
        candidates = []
        for item in targets:
            if not self.is_running: break
            
            # [추가] API 호출 전 대기 (Rate Limit 방지)
            time.sleep(safe_delay)
            
            code = item['code']; name = item['name']
            
            # [추가] 보유 중이면 스킵
            if code in holding_codes: continue
            
            # [설명] 장 중에는 당일 실시간 시세가 반영된 일봉 데이터를 가져옵니다.
            df = api.get_chart_data(code, is_overseas=False)
            if df is None or df.empty: continue
            
            ind = indicators.calculate_indicators(df)
            prev_rsi = self._get_prev_rsi(df)
            current_price = float(df.iloc[-1]['close'])
            
            state, _ = analysis.classify_stock_state(
                current_price, ind['ema_20'], ind['ema_60'], ind['ema_120'], 
                ind['psar'], ind['rsi'], prev_rsi, ind['adx'], ind['cci'], ind.get('obv_trend')
            )
            
            # [추가] 분석 로그 기록
            score, _ = analysis.calculate_score(current_price, ind['ema_20'], ind['ema_60'], ind['ema_120'], ind['psar'], ind['rsi'], ind['adx'], ind['cci'], ind.get('obv_trend'))
            rsi_val = f"{ind['rsi']:.1f}" if ind['rsi'] is not None else "-"
            adx_val = f"{ind['adx']:.1f}" if ind['adx'] is not None else "-"
            self.log(f"[분석] {name}({code}): 점수={score}, 상태={state}, RSI={rsi_val}, ADX={adx_val}")
            
            if state == "매수":
                candidates.append({
                    'code': code, 'name': name, 'price': current_price,
                    'score': score, 'rsi': ind['rsi'], 'adx': ind['adx']
                })

        # [수정] 2단계: 우선순위 정렬 (점수 높은 순 -> RSI 낮은 순)
        # 점수가 같다면 RSI가 낮을수록(저평가) 우선순위
        candidates.sort(key=lambda x: (-x['score'], x['rsi']))

        # [수정] 3단계: 자산 배분 및 매수 집행
        invest_ratio = getattr(config, 'SYSTEM_INVEST_PER_STOCK', 0.1) # 기본 10%
        
        for cand in candidates:
            if not self.is_running: break
            if avail_cash < 50000: break

            # 투자 금액 계산 (예수금의 N%)
            invest_amt = int(avail_cash * invest_ratio)
            # 최소 주문 금액 보정 (너무 적으면 1주라도 살 수 있게)
            if invest_amt < cand['price']: invest_amt = avail_cash
            
            # [수정] 단순 계산 대신 API를 통해 정확한 매수 가능 수량 조회
            # 시장가 주문 시 증거금 부족 등을 방지하기 위해 price=0(시장가)으로 조회
            max_qty = api.fetch_buyable_quantity(cand['code'], 0)
            
            # 자산 배분 비중 적용 수량
            target_qty = int(invest_amt / cand['price'])
            
            # 실제 주문 수량은 (목표 수량)과 (API 조회 가능 수량) 중 작은 값
            qty = min(target_qty, max_qty)
            
            # [추가] 예수금 부족 로그
            if qty < 1:
                self.log(f"매수 실패: {cand['name']} - 매수 가능 수량 부족 (목표:{target_qty}, 가능:{max_qty})")
                continue

            if avail_cash >= (qty * cand['price']):
                rsi_val = f"{cand['rsi']:.1f}" if cand['rsi'] else "-"
                adx_val = f"{cand['adx']:.1f}" if cand['adx'] else "-"
                reason = f"조건 만족 [점수:{cand['score']}, RSI:{rsi_val}, ADX:{adx_val}]"
                
                self.log(f"매수 실행: {cand['name']} - {reason}")
                odno = self._send_order(cand['code'], qty, "buy")
                if odno: 
                    avail_cash -= (qty * cand['price'])
                    record = {
                        "type": "buy",
                        "code": cand['code'],
                        "name": cand['name'],
                        "qty": qty,
                        "price": cand['price'],
                        "reason": reason,
                        "time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                        "odno": odno
                    }
                    self.trade_records.append(record)

    def _send_order(self, code, qty, type_str):
        tr_id = utils.get_tr_id("domestic", "trade", type_str)
        url = f"{config.URL_BASE}/uapi/domestic-stock/v1/trading/order-cash"
        headers = utils.get_common_headers(tr_id)
        data = {"CANO": config.CANO, "ACNT_PRDT_CD": config.ACNT_PRDT_CD, "PDNO": code, "ORD_DVSN": "01", "ORD_QTY": str(qty), "ORD_UNPR": "0"}
        
        # [추가] 상세 로그: 요청 정보
        self.log(f"======== [주문 실행] {type_str.upper()} ========")
        self.log(f"대상: {code}, 수량: {qty}, 단가: 시장가(0)")
        self.log(f"API URL: {url}")
        if config.DEBUG_LEVEL == "DEBUG":
            self.log(f"Body: {json.dumps(data)}")

        try:
            res = api.session.post(url, headers=headers, data=json.dumps(data), verify=False, timeout=config.DEFAULT_TIMEOUT)
            res_json = res.json()
            
            if res_json['rt_cd'] == '0':
                odno = res_json['output']['ODNO']
                success_msg = f"[{datetime.now().strftime('%H:%M:%S')}] {type_str.upper()} 성공 | {code} | {qty}주 | No.{odno}"
                self.trade_history.append(success_msg)
                self.log(f"결과: 성공 (주문번호: {odno})")
                return odno
            else:
                err_msg = res_json.get('msg1', 'Unknown Error')
                self.log(f"결과: 실패 ({err_msg}) [Code: {res_json.get('msg_cd')}]")
        except Exception as e:
            self.log(f"결과: 에러 발생 ({str(e)})")
        finally:
            self.log("========================================")
        return None

def system_trading_menu():
    """시스템 트레이딩 메뉴"""
    # [추가] 실전 투자 모드 제한
    if not config.IS_SIMULATION:
        console.print("\n[bold red]경고: 시스템 트레이딩 기능은 현재 모의투자 환경에서만 지원합니다.[/bold red]")
        console.print("[dim]안전성을 위해 실전 투자 모드에서는 접근이 제한됩니다.[/dim]\n")
        return

    trader = AutoTrader()

    console.print("\n[bold yellow]=== 시스템 트레이딩 ===[/]")
    console.print("[dim]안내: 시스템 트레이딩은 현재 '국내주식(stocks_kr)' 리스트를 대상으로만 작동합니다.[/dim]")
    console.print(f"현재 상태: {'[green]실행 중[/green]' if trader.is_running else '[red]중지됨[/red]'}")
    console.print()
    console.print("[1] 트레이딩 실행 (Start)")
    console.print("[2] 트레이딩 중단 (Stop)")
    console.print("[3] 트레이딩 상태 (Status)")
    console.print("[4] 트레이딩 평가 (Report)")
    console.print("[5] 트레이딩 로그 (Log Viewer)")
    console.print()
    
    choice = Prompt.ask("선택 [dim](취소: q)[/dim]", choices=["1", "2", "3", "4", "5", "q"], default="3")
    if choice.lower() == 'q': return
    
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