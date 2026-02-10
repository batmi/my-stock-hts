import threading
import time
import json
import os
from datetime import datetime
from collections import Counter
from rich.prompt import Prompt
from rich.markup import escape
from rich.table import Table
from rich import box
from rich.rule import Rule
import config
import api
import utils
import indicators
from modules import analysis # account 모듈 의존성 제거
from modules import db_manager # [추가] DB 매니저

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
        
        # [수정] 실전 모드일 경우 자동매매 전용 계좌 설정 확인
        if not config.IS_SIMULATION:
            if not config.AUTO_APP_KEY or not config.AUTO_CANO:
                console.print("[bold red]오류: 실전 투자 모드에서 시스템 트레이딩을 실행하려면 별도의 자동매매 계좌 설정이 필요합니다.[/bold red]")
                console.print("[dim]환경 변수 AUTO_APP_KEY, AUTO_APP_SECRET, AUTO_ACC_NUM을 설정해주세요.[/dim]")
                return
            
            console.print("\n[bold red]!!! 경고: 실전 투자 모드에서 시스템 트레이딩을 시작합니다 !!![/bold red]")
            console.print(f"운용 계좌: [bold yellow]{config.AUTO_CANO}-{config.AUTO_ACNT_PRDT_CD}[/bold yellow] (시스템 트레이딩 전용)")
            if Prompt.ask("위 계좌로 실제 매매가 수행됩니다. 진행하시겠습니까?", choices=["y", "n"], default="n") != "y":
                console.print("[yellow]시작을 취소했습니다.[/yellow]")
                return

        # [추가] 텔레그램 메시지 구성을 위한 변수 미리 선언
        holdings = []
        summary = []

        with console.status("[bold green]시스템 시작 준비 중 (자산 조회 및 스레드 시작)...[/]"):
            self.is_running = True
            self.start_time = datetime.now()
            self.consecutive_errors = 0
            self.was_market_open = self.is_market_open()
            
            # [추가] 시작 시 컨텍스트 임시 설정하여 초기 자산 조회
            config.trade_context.use_auto_account = True
            # [추가] 시작 시점 총 자산 계산 (손실 제한 기준점)
            self.initial_asset = self._get_total_estimated_asset()
            
            # [추가] 체결 내역 상태 동기화 (재시작 시 과거 내역 알림 방지)
            self._check_conclusions(initial=True)
            
            # [추가] 텔레그램 알림용 잔고 조회 (예외 처리 추가)
            try:
                holdings, summary = api.get_domestic_balance()
            except Exception as e:
                self.log(f"시작 시 잔고 조회 실패: {e}")
                holdings, summary = [], []
            
            config.trade_context.use_auto_account = False # 복구
            
            if self.initial_asset is None:
                self.initial_asset = 0
                self.log("초기 자산 조회 실패 또는 자산 없음 (0원으로 설정)")

            if self.initial_asset > 0:
                self.log(f"시스템 시작 자산: {self.initial_asset:,}원")
            
            # [추가] API 모듈에서 로그를 남길 수 있도록 연결
            config.SYSTEM_LOGGER = self.log
            
            self.thread = threading.Thread(target=self._run_loop, daemon=True)
            self.thread.start()

        console.print("\n[green]자동매매 시스템이 시작되었습니다. (백그라운드)[/green]")
        self.log("시스템 시작")
        
        # [수정] 텔레그램 전송 시 AUTO 계좌 정보가 포함되도록 컨텍스트 설정
        config.trade_context.use_auto_account = True
        
        # [수정] 시작 메시지에 보유 종목 및 자산 현황 추가
        msg = f"▶️ [시스템 시작] 자동매매가 시작되었습니다.\n초기 자산: {self.initial_asset:,}원"
        
        if summary and len(summary) > 0:
            s_data = summary[0]
            total_eval = api.safe_int(s_data.get('scts_evlu_amt'))
            total_profit = api.safe_int(s_data.get('evlu_pfls_smtl_amt'))
            msg += f"\n현재 평가: {total_eval:,}원 (손익: {total_profit:+,}원)"
            
        if holdings:
            msg += "\n\n📋 [보유 종목 현황]"
            for item in holdings:
                name = item['prdt_name']
                qty = int(item['hldg_qty'])
                rate = float(item['evlu_pfls_rt'])
                profit = int(item['evlu_pfls_amt'])
                msg += f"\n• {name}: {qty}주 | {rate:+.2f}% ({profit:+,}원)"
        else:
            msg += "\n\n📋 [보유 종목] 없음"

        api.send_telegram_message(msg)
        config.trade_context.use_auto_account = False

    def stop(self):
        if not self.is_running:
            console.print("\n[yellow]실행 중인 자동매매가 없습니다.[/yellow]")
            return
            
        with console.status("[bold red]시스템 중단 요청 처리 중...[/]"):
            self.is_running = False
            
            if self.thread:
                self.thread.join(timeout=5)

        console.print("\n[red]자동매매 시스템이 중단되었습니다.[/red]")
        self.log("시스템 중단")
        
        # [수정] 텔레그램 전송 시 AUTO 계좌 정보가 포함되도록 컨텍스트 설정
        config.trade_context.use_auto_account = True
        
        # [추가] 종료 시 최종 자산 현황 요약 전송
        final_asset = self._get_total_estimated_asset()
        if final_asset is None: final_asset = 0
        
        profit = final_asset - self.initial_asset
        profit_rate = 0.0
        if self.initial_asset > 0:
            profit_rate = (profit / self.initial_asset) * 100
            
        msg = f"⏹ [시스템 종료] 자동매매가 종료되었습니다.\n시작 자산: {self.initial_asset:,}원\n종료 자산: {final_asset:,}원\n금일 손익: {profit:+,}원 ({profit_rate:+.2f}%)"
        
        # [추가] 종료 시 보유 종목 현황 추가
        try:
            holdings, _ = api.get_domestic_balance()
            if holdings:
                msg += "\n\n📋 [최종 보유 종목]"
                for item in holdings:
                    name = item['prdt_name']
                    qty = int(item['hldg_qty'])
                    rate = float(item['evlu_pfls_rt'])
                    profit_amt = int(item['evlu_pfls_amt'])
                    msg += f"\n• {name}: {qty}주 | {rate:+.2f}% ({profit_amt:+,}원)"
            else:
                msg += "\n\n📋 [최종 보유 종목] 없음"
        except Exception as e:
            self.log(f"종료 시 잔고 조회 실패: {e}")

        api.send_telegram_message(msg)
        config.trade_context.use_auto_account = False
        
        # [추가] 로거 연결 해제 (메시지 전송 후 해제)
        config.SYSTEM_LOGGER = None

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
        
        # [추가] 상태 조회 시에도 시스템 트레이딩 컨텍스트 사용
        original_context = getattr(config.trade_context, 'use_auto_account', False)
        config.trade_context.use_auto_account = True
        
        with console.status("[bold green]트레이딩 상태 및 자산 정보 조회 중...[/]"):
            current_asset = self._get_total_estimated_asset()
            
            # 예수금 별도 조회 (매수 여력 확인용)
            try:
                cano = config.AUTO_CANO if not config.IS_SIMULATION else config.CANO
                acnt = config.AUTO_ACNT_PRDT_CD if not config.IS_SIMULATION else config.ACNT_PRDT_CD
                params = {"CANO": cano, "ACNT_PRDT_CD": acnt, "PDNO": "005930", "ORD_UNPR": "0", "ORD_DVSN": "01", "CMA_EVLU_AMT_ICLD_YN": "Y", "OVRS_ICLD_YN": "Y", "CRDT_TYPE": "00"}
                
                # [수정] api.call_api 사용하여 안정성 확보
                res = api.call_api("uapi/domestic-stock/v1/trading/inquire-psbl-order", "domestic", "inquiry", "buyable", params=params)
                if res.get('rt_cd') == '0': deposit = int(res['output']['ord_psbl_cash'])
            except: pass
            
        config.trade_context.use_auto_account = original_context # 복구

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
            
            table.add_row("주문 가능", f"{deposit:,}원")
            
            # 일일 손실 제한 체크 (초기 자산이 있을 때만)
            if self.initial_asset > 0:
                loss_limit = getattr(config, 'SYSTEM_DAILY_LOSS_LIMIT', 0.0)
                if loss_limit > 0:
                    safety_msg = "[green]안전[/green]"
                    if rate <= -loss_limit: safety_msg = "[bold red]위험 (한도 초과)[/bold red]"
                    elif rate <= -(loss_limit * 0.8): safety_msg = "[bold orange3]주의 (한도 임박)[/bold orange3]"
                    table.add_row("손실 제한", f"-{loss_limit}% (상태: {safety_msg})")
        else:
            table.add_row("자산 정보", "[bold red]조회 실패 (통신 오류)[/bold red]")
            if self.initial_asset > 0:
                table.add_row("초기 자산", f"{self.initial_asset:,}원")

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
        console.print()

    def print_report(self):
        console.print("\n[bold yellow]=== 시스템 트레이딩 리포트 ===[/]")
        
        # [추가] 리포트 생성 상태 표시
        with console.status("[bold green]DB에서 매매 내역 조회 및 분석 중...[/]"):
            time.sleep(0.5) # UX를 위한 짧은 대기
            
            # [수정] DB에서 시스템 트레이딩 내역 조회 (현재 모드에 맞는 내역만)
            db_records = db_manager.db.get_trades(is_auto=True, is_sim=config.IS_SIMULATION, limit=500)
            
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

        if not self.trade_records:
            console.print("\n[yellow]저장된 시스템 트레이딩 기록이 없습니다.[/yellow]")
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
        date_str = datetime.now().strftime("%Y-%m-%d")
        filename = f"system_trade_{date_str}.log"
        filepath = os.path.join(log_dir, filename)

        if not os.path.exists(filepath):
            console.print(f"\n[yellow]오늘 날짜({date_str})의 로그 파일이 없습니다.[/yellow]")
            return

        console.print(f"\n[bold cyan]=== 실시간 로그 모니터링 ({filename}) ===[/bold cyan]")
        console.print("[dim]종료하려면 Ctrl+C를 누르세요.[/dim]\n")

        with console.status("[bold green]로그 파일 로딩 중...[/]"):
            time.sleep(0.5)

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
                # [추가] 스레드 내에서 시스템 트레이딩 컨텍스트 활성화
                config.trade_context.use_auto_account = True
                
                self.log("모니터링 주기 시작...")
                
                # [추가] 현재 운용 계좌 정보 로깅
                current_cano = config.AUTO_CANO if not config.IS_SIMULATION else config.CANO
                if current_cano:
                    acc_type = "모의투자" if config.IS_SIMULATION else "실전투자(자동)"
                    self.log(f"운용 계좌: {current_cano} [{acc_type}]")
                
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
                    api.send_telegram_message(f"🚨 [비상 정지] 연속 에러 {max_err}회 발생으로 시스템을 중단합니다.")
                    self.stop()
                    break
                time.sleep(10)

    def _check_conclusions(self, initial=False):
        """금일 체결 내역을 확인하고 로그에 기록 (모든 활성 계좌 대상)"""
        try:
            # API 호출 전 대기 (Rate Limit 방지)
            time.sleep(0.2)
            if not initial:
                time.sleep(0.2)
            
            # 모니터링 대상 계좌 목록 구성
            accounts_to_check = []
            
            # 1. 메인 계좌 (수동 매매용)
            if config.CANO and config.ACNT_PRDT_CD:
                accounts_to_check.append({
                    "cano": config.CANO,
                    "acnt": config.ACNT_PRDT_CD,
                    "type": "MAIN"
                })
            
            # 2. 자동매매 계좌 (실전 모드이고 별도 설정된 경우)
            if not config.IS_SIMULATION and config.AUTO_CANO and config.AUTO_ACNT_PRDT_CD:
                # 메인 계좌와 다를 경우에만 추가
                if config.AUTO_CANO != config.CANO or config.AUTO_ACNT_PRDT_CD != config.ACNT_PRDT_CD:
                    accounts_to_check.append({
                        "cano": config.AUTO_CANO,
                        "acnt": config.AUTO_ACNT_PRDT_CD,
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
            
            tr_id = utils.get_tr_id("domestic", "inquiry", "history")
            url = f"{config.URL_BASE}/uapi/domestic-stock/v1/trading/inquire-daily-ccld"
            headers = utils.get_common_headers(tr_id)
            today = datetime.now().strftime("%Y%m%d")
            
            for acc in unique_accounts:
                cano = acc['cano']
                acnt = acc['acnt']
                
                # 컨텍스트 스위칭 (API 호출 및 텔레그램 전송 시 올바른 계좌 태그 사용)
                original_context = getattr(config.trade_context, 'use_auto_account', False)
                config.trade_context.use_auto_account = (acc['type'] == 'AUTO')
                
                try:
                    params = {
                        "CANO": cano, 
                        "ACNT_PRDT_CD": acnt, 
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
                            
                            # 계좌별 고유 키 생성 (계좌번호-주문번호)
                            order_key = f"{cano}-{odno}"
                            prev_qty = self.order_status.get(order_key, 0)
                            
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
                                
                                # 매매일시 정보 추출
                                ord_dt = item.get('ord_dt', '')
                                ord_tmd = item.get('ord_tmd', '')
                                trade_time_str = ""
                                if len(ord_dt) == 8 and len(ord_tmd) == 6:
                                    trade_time_str = f"{ord_dt[:4]}-{ord_dt[4:6]}-{ord_dt[6:]} {ord_tmd[:2]}:{ord_tmd[2:4]}:{ord_tmd[4:]}"

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
                                
                                # 초기화 단계가 아닐 때만 알림 및 로그 수행
                                if not initial:
                                    # 로그 기록
                                    self.log(f"✅ [체결 확인] {type_name} {name}({code}) {new_qty}주 체결 (단가: {avg_price:,.0f}원){add_info}")
                                    
                                    # 알림 발송 (텔레그램 + 비프음)
                                    print('\a') # 소리 알림
                                    
                                    msg = f"✅ [체결 알림] {type_name} {name}({code})\n"
                                    if trade_time_str:
                                        msg += f"일시: {trade_time_str}\n"
                                    msg += f"수량: {new_qty}주 / 단가: {avg_price:,.0f}원\n{add_info.strip()}"
                                    
                                    # [추가] 현재 총 자산 및 수익률 정보 추가
                                    # 자산 조회 시 올바른 토큰 사용을 위해 컨텍스트 임시 변경
                                    temp_ctx = getattr(config.trade_context, 'use_auto_account', False)
                                    config.trade_context.use_auto_account = True # 시스템 계좌 기준 조회
                                    try:
                                        curr_asset = self._get_total_estimated_asset()
                                        if curr_asset and self.initial_asset > 0:
                                            profit = curr_asset - self.initial_asset
                                            profit_rate = (profit / self.initial_asset) * 100
                                            msg += f"\n💰 자산: {curr_asset:,}원 ({profit:+,}원 / {profit_rate:+.2f}%)"
                                    except: pass
                                    finally:
                                        config.trade_context.use_auto_account = temp_ctx # 복구
                                    
                                    api.send_telegram_message(msg)
                                
                                # 상태 업데이트
                                self.order_status[order_key] = tot_ccld_qty
                                
                                # [수정] DB에 체결 내역 별도 저장 (중복 방지)
                                if not db_manager.db.check_trade_exists(odno, "체결"):
                                    db_manager.db.insert_trade(
                                        type_name, code, name, tot_ccld_qty, avg_price, odno,
                                        order_status="체결", custom_time=trade_time_str,
                                        reason="시스템 감지 체결"
                                    )
                finally:
                    # 컨텍스트 복구
                    config.trade_context.use_auto_account = original_context
                    time.sleep(0.1) # 계좌 간 호출 간격
                        
        except Exception as e:
            self.log(f"체결 내역 조회 중 오류: {str(e)}")

    def _monitor_account_status(self):
        """현재 보유 종목 상태 로깅 및 자산 손실 제한(Loss Cut) 체크"""
        try:
            # API 호출 전 대기 (Rate Limit 방지)
            time.sleep(0.2)
            holdings, summary = api.get_domestic_balance() # [수정] api 모듈 함수 사용
            
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
                    
                    # [추가] 일일 손실 제한 체크
                    self._check_loss_limit(total_eval)
                    
        except Exception: pass

    def _get_total_estimated_asset(self):
        """현재 총 추정 자산(예수금 + 주식평가금) 계산"""
        # [추가] 일시적 오류 대비 재시도 로직 (최대 3회)
        for attempt in range(3):
            try:
                cano = config.AUTO_CANO if not config.IS_SIMULATION else config.CANO
                acnt = config.AUTO_ACNT_PRDT_CD if not config.IS_SIMULATION else config.ACNT_PRDT_CD
                
                # 1. 예수금 조회
                # [수정] api.call_api 사용하여 토큰 갱신 및 재시도 로직 활용
                params = {"CANO": cano, "ACNT_PRDT_CD": acnt, "PDNO": "005930", "ORD_UNPR": "0", "ORD_DVSN": "01", "CMA_EVLU_AMT_ICLD_YN": "Y", "OVRS_ICLD_YN": "Y", "CRDT_TYPE": "00"}
                res = api.call_api("uapi/domestic-stock/v1/trading/inquire-psbl-order", "domestic", "inquiry", "buyable", params=params)
                
                cash = 0
                if res.get('rt_cd') == '0': 
                    cash = int(res['output']['ord_psbl_cash'])
                else: 
                    # [추가] 실패 원인 로그 기록
                    if config.DEBUG_LEVEL != "OFF":
                        self.log(f"[DEBUG] 예수금 조회 실패({attempt+1}/3): {res.get('msg1')} [{res.get('msg_cd')}]")
                    time.sleep(0.5)
                    continue # 실패 시 재시도
                
                # 2. 주식 평가금 조회
                _, summary = api.get_domestic_balance() # [수정] api 모듈 함수 사용
                stock_eval = 0
                if summary and len(summary) > 0: stock_eval = api.safe_int(summary[0].get('scts_evlu_amt'))
                # 잔고 조회 실패(빈 리스트)일 수도 있으나, 실제 잔고가 없는 경우와 구분 어려우므로 진행
                
                return cash + stock_eval
            except Exception as e:
                # [추가] 예외 로그 기록
                if config.DEBUG_LEVEL != "OFF":
                    self.log(f"[DEBUG] 자산 조회 중 예외 발생({attempt+1}/3): {str(e)}")
                time.sleep(1)
        
        return None

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

    def _check_sell_conditions(self):
        holdings, _ = api.get_domestic_balance() # [수정] api 모듈 함수 사용
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
        tps = config.SIM_TX_PER_SECOND if config.IS_SIMULATION else config.REAL_TX_PER_SECOND
        safe_delay = (1.0 / tps) * 1.2  # 20% 여유 버퍼

        for item in holdings:
            if not self.is_running: break
            code = item['pdno']; name = item['prdt_name']
            
            # [수정] 보유수량(hldg_qty) 대신 주문가능수량(ord_psbl_qty) 사용
            # 미체결 매도 주문이 있을 경우 중복 매도를 방지하기 위함
            qty = int(item.get('ord_psbl_qty', 0))
            profit_rate = float(item['evlu_pfls_rt'])
            current_price = float(item['prpr'])
            buy_price = float(item['pchs_avg_pric'])
            
            # [추가] API 호출 전 대기 (Rate Limit 방지)
            time.sleep(safe_delay)
            
            if qty <= 0: 
                continue # 주문 가능 수량이 없으면 스킵

            # [리팩토링] 기술적 분석을 먼저 수행하여 모든 지표를 확보
            # [설명] 장 중에는 당일 실시간 시세가 반영된 일봉 데이터를 가져옵니다.
            df = api.get_chart_data(code, is_overseas=False)
            reason = ""
            
            if df is not None and not df.empty:
                ind = indicators.calculate_indicators(df)
                prev_rsi = self._get_prev_rsi(df)
                
                state, _ = analysis.classify_stock_state(
                    current_price, ind['ema_20'], ind['ema_60'], ind['ema_120'], 
                    ind['psar'], ind['rsi'], prev_rsi, ind['adx'], ind['cci'], ind.get('obv_trend')
                )
                score, _ = analysis.calculate_score(current_price, ind['ema_20'], ind['ema_60'], ind['ema_120'], ind['psar'], ind['rsi'], ind['adx'], ind['cci'], ind.get('obv_trend'))
                rsi_val = f"{ind['rsi']:.1f}" if ind['rsi'] is not None else "-"
                adx_val = f"{ind['adx']:.1f}" if ind['adx'] is not None else "-"
                cci_val = f"{ind['cci']:.1f}" if ind['cci'] is not None else "-"
                # [수정] 수익률 정보를 로그에 포함하여 매도 판단 근거(익절/손절 여부)를 명확히 함
                self.log(f"[보유분석] {name}({code}): 수익률={profit_rate:.2f}%, 점수={score}, 상태={state}, RSI={rsi_val}, ADX={adx_val}, CCI={cci_val}")

                # [리팩토링] 매도 조건 우선순위 적용
                # 1. 고정 익절 (최우선)
                if profit_rate >= take_profit_rate:
                    reason = f"익절({profit_rate}%)"
                # 2. 고정 손절
                elif profit_rate <= stop_loss_rate:
                    reason = f"손절({profit_rate}%)"
                # 3. 트레일링 스탑
                else:
                    if current_price > buy_price:
                        db_manager.db.update_highest_price(code, current_price)
                    
                    highest_price = db_manager.db.get_highest_price(code)
                    if highest_price and highest_price > 0:
                        max_profit_rate = ((highest_price - buy_price) / buy_price) * 100
                        if max_profit_rate >= ts_activation:
                            drop_rate = ((highest_price - current_price) / highest_price) * 100
                            if drop_rate >= ts_callback:
                                reason = f"트레일링스탑 (최고가:{int(highest_price):,}원, 하락률:-{drop_rate:.1f}%)"
                
                # 4. RSI 과열 익절
                if not reason and ind['rsi'] is not None and ind['rsi'] > take_profit_rsi:
                    reason = f"RSI 과열 익절 (RSI: {ind['rsi']:.1f})"
                
                # 5. 추세 이탈
                if not reason and (state == "위험" or score < sell_score_limit):
                    reason = f"추세이탈({state}/점수하락) [점수:{score}, RSI:{rsi_val}, ADX:{adx_val}, CCI:{cci_val}]"

            if reason:
                self.log(f"매도 실행: {name} - {reason}")
                # [수정] 매도 시 수익 정보와 사유, 점수 등을 DB 저장을 위해 전달
                odno = self._send_order(code, qty, "sell", name=name, profit_amt=int(item['evlu_pfls_amt']), profit_rate=profit_rate, reason=reason, score=score)
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
                    # [추가] 매도 성공 시 트레일링 스탑 정보 삭제
                    db_manager.db.delete_trailing_stop(code)

    def _check_buy_conditions(self):
        targets = config.STOCK_CONFIG_DATA.get("stocks_kr", [])
        if not targets: return
        
        # [추가] 보유 종목 조회 (중복 매수 방지)
        holdings, _ = api.get_domestic_balance() # [수정] api 모듈 함수 사용
        holding_codes = set()
        if holdings:
            for h in holdings:
                holding_codes.add(h['pdno'])
        
        # 예수금 확인 (API 직접 호출)
        tr_id = utils.get_tr_id("domestic", "inquiry", "deposit")
        cano = config.AUTO_CANO if not config.IS_SIMULATION else config.CANO
        acnt = config.AUTO_ACNT_PRDT_CD if not config.IS_SIMULATION else config.ACNT_PRDT_CD
        url = f"{config.URL_BASE}/uapi/domestic-stock/v1/trading/inquire-psbl-order"
        headers = utils.get_common_headers(tr_id)
        params = {"CANO": cano, "ACNT_PRDT_CD": acnt, "PDNO": "005930", "ORD_UNPR": "0", "ORD_DVSN": "01", "CMA_EVLU_AMT_ICLD_YN": "Y", "OVRS_ICLD_YN": "Y", "CRDT_TYPE": "00"}
        
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
            cci_val = f"{ind['cci']:.1f}" if ind['cci'] is not None else "-"
            # [수정] 현재가 정보를 로그에 포함
            self.log(f"[분석] {name}({code}): 현재가={current_price:,.0f}, 점수={score}, 상태={state}, RSI={rsi_val}, ADX={adx_val}, CCI={cci_val}")
            
            if state == "매수":
                candidates.append({
                    'code': code, 'name': name, 'price': current_price,
                    'score': score, 'rsi': ind['rsi'], 'adx': ind['adx'], 'cci': ind['cci']
                })

        # [수정] 2단계: 우선순위 정렬 (점수 높은 순 -> RSI 낮은 순)
        # 점수가 같다면 RSI가 낮을수록(저평가) 우선순위
        candidates.sort(key=lambda x: (-x['score'], x['rsi']))

        # [수정] 3단계: 자산 배분 및 매수 집행
        invest_ratio = getattr(config, 'SYSTEM_INVEST_PER_STOCK', 0.1) # 기본 10%
        
        for cand in candidates:
            if not self.is_running: break
            if avail_cash < 50000: break

            # 투자 금액 계산 (초기 자산의 N%)
            # [수정] 현재 예수금이 아닌 시스템 시작 시점의 총 자산(initial_asset) 기준으로 비중 산정
            if self.initial_asset > 0:
                target_invest_amt = int(self.initial_asset * invest_ratio)
            else:
                target_invest_amt = int(avail_cash * invest_ratio)
            
            # 실제 집행 금액은 목표 금액과 현재 예수금 중 작은 값 (예수금 초과 불가)
            # (예: 50% 설정 시, 첫 번째 매수 후 남은 예수금이 목표 금액 이하가 되므로 두 번째 매수 시 전액 투자됨)
            invest_amt = min(target_invest_amt, avail_cash)
            
            # [추가] 자산 배분 계산 로그 기록 (동작 확인용)
            if config.DEBUG_LEVEL == "DEBUG":
                self.log(f"[자산배분] 목표금액: {target_invest_amt:,}원 (초기자산의 {invest_ratio*100:.0f}%) / 현재예수금: {avail_cash:,}원 -> 투자금액: {invest_amt:,}원")

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
                cci_val = f"{cand['cci']:.1f}" if cand.get('cci') else "-"
                reason = f"조건 만족 [점수:{cand['score']}, RSI:{rsi_val}, ADX:{adx_val}, CCI:{cci_val}]"
                
                self.log(f"매수 실행: {cand['name']} - {reason}")
                # [수정] 매수 시 사유와 점수를 DB 저장을 위해 전달
                odno = self._send_order(cand['code'], qty, "buy", name=cand['name'], reason=reason, score=cand['score'])
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

    def _send_order(self, code, qty, type_str, name=None, profit_amt=0, profit_rate=0.0, reason=None, score=0):
        tr_id = utils.get_tr_id("domestic", "trade", type_str)
        url = f"{config.URL_BASE}/uapi/domestic-stock/v1/trading/order-cash"
        headers = utils.get_common_headers(tr_id)
        cano = config.AUTO_CANO if not config.IS_SIMULATION else config.CANO
        acnt = config.AUTO_ACNT_PRDT_CD if not config.IS_SIMULATION else config.ACNT_PRDT_CD
        data = {"CANO": cano, "ACNT_PRDT_CD": acnt, "PDNO": code, "ORD_DVSN": "01", "ORD_QTY": str(qty), "ORD_UNPR": "0"}
        
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
                stock_display = f"{name}({code})" if name else code
                msg = f"🚀 [주문 접수] {type_str.upper()} {stock_display} {qty}주 (시장가)\n주문번호: {odno}"
                if reason:
                    msg += f"\n사유: {reason}"
                api.send_telegram_message(msg)
                
                # [DB] 시스템 트레이딩 주문 기록 (스냅샷 및 상세 정보 포함)
                snapshot = analysis.get_snapshot(code, is_overseas=False)
                db_manager.db.insert_trade(f"{type_str}(AUTO)", code, name, qty, "0", odno, snapshot=snapshot, profit_amt=profit_amt, profit_rate=profit_rate, reason=reason, score=score)
                
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