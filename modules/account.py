# modules/account.py
import logging
from rich.table import Table
from rich.panel import Panel
from rich import box
from rich.prompt import Prompt
from rich.progress import Progress, SpinnerColumn, BarColumn, TextColumn
from datetime import datetime, timedelta
import os
import time
import config
import context # [추가]
import utils
import api
from modules import db_manager
import json
import pandas as pd
from openpyxl.utils import get_column_letter

logger = logging.getLogger(__name__)

def fetch_today_profit_summary(cano=None, acnt_prdt_cd=None, target_date=None):
    summary = {'buy_amt': 0, 'sell_amt': 0, 'total_cost': 0, 'realized_pl': 0}
    try:
        data = api.get_today_profit_summary(cano, acnt_prdt_cd, target_date=target_date)
        if data.get('rt_cd') == '0':
            out2 = data.get('output2')
            if isinstance(out2, list) and len(out2) > 0:
                summary_data = out2[0]
                summary['buy_amt'] = api.safe_int(summary_data.get('thdt_buy_amt'))
                summary['sell_amt'] = api.safe_int(summary_data.get('thdt_sll_amt'))
                summary['total_cost'] = api.safe_int(summary_data.get('thdt_tlex_amt'))
                summary['realized_pl'] = api.safe_int(summary_data.get('rlzt_pfls'))
    except: pass
    return summary

def fetch_today_history(cano=None, acnt_prdt_cd=None, target_date=None):
    summary = {'buy_total': 0, 'sell_total': 0}
    try:
        data = api.get_today_history(cano, acnt_prdt_cd, target_date=target_date)
        if data.get('rt_cd') == '0':
            trades = data.get('output1', [])
            if trades:
                for item in trades:
                    amt = api.safe_int(item.get('tot_ccld_amt'))
                    type_cd = item.get('sll_buy_dvsn_cd')
                    if type_cd == '01': summary['sell_total'] += amt
                    elif type_cd == '02': summary['buy_total'] += amt
    except: pass
    return summary

def fetch_domestic_balance(cano=None, acnt_prdt_cd=None):
    """국내 주식 잔고 데이터를 조회하여 반환"""
    holdings = []
    summary = None
    
    try:
        # api.py의 함수 사용 (내부에서 OPSQ2001 재시도 및 토큰 처리)
        output1, output2 = api.get_domestic_balance(cano, acnt_prdt_cd)
        
        if output1:
            for item in output1:
                qty = int(item['hldg_qty'])
                if qty > 0:
                    holdings.append(item)
        if output2:
            summary = output2[0]
            
    except Exception as e:
        logger.error(f"국내 잔고 조회 실패: {e}")
        
    return holdings, summary

def fetch_overseas_balance(cano=None, acnt_prdt_cd=None):
    """해외 주식 잔고 데이터를 조회하여 반환"""

    return api.get_overseas_balance(cano, acnt_prdt_cd)

def sync_today_trades():
    """금일 체결 내역을 API로 조회하여 DB의 단가(시장가=0) 정보를 업데이트 (모든 계좌 대상)"""
    logger.debug("[HISTORY_DEBUG] sync_today_trades() 시작")
    
    # 조회 대상 계좌 목록
    accounts = []
    if config.session.cano and config.session.acnt_prdt_cd:
        accounts.append({"cano": config.session.cano, "acnt": config.session.acnt_prdt_cd, "type": "MAIN"})
    
    if not config.session.is_simulation and config.session.auto_cano and config.session.auto_acnt_prdt_cd:
        if config.session.auto_cano != config.session.cano or config.session.auto_acnt_prdt_cd != config.session.acnt_prdt_cd:
            accounts.append({"cano": config.session.auto_cano, "acnt": config.session.auto_acnt_prdt_cd, "type": "AUTO"})
            
    total_count = 0
    original_context = getattr(context.trade_context, 'use_auto_account', False)
    
    with Progress(
        SpinnerColumn(),
        TextColumn("[progress.description]{task.description}"),
        BarColumn(),
        TextColumn("[progress.percentage]{task.percentage:>3.0f}%"),
        console=config.console,
        transient=True
    ) as progress:
        task = progress.add_task("[cyan]최신 체결 내역 동기화 중...[/cyan]", total=len(accounts))
        
        try:
            for acc in accounts:
                cano = acc['cano']
                acnt = acc['acnt']
                
                try:
                    data = api.get_today_history(cano, acnt)
                    ovrs_data = api.get_overseas_today_history(cano, acnt)
                    
                    all_trades = []
                    if data.get('rt_cd') == '0':
                        all_trades.extend(data.get('output1', []))
                    if ovrs_data.get('rt_cd') == '0':
                        all_trades.extend(ovrs_data.get('output', []))

                    if all_trades:
                        for item in all_trades:
                            odno = item.get('odno')
                            is_overseas_trade = 'ft_ccld_qty' in item
                            
                            if is_overseas_trade:
                                avg_price = float(item.get('ft_ccld_unpr3', 0))
                                tot_qty = int(item.get('ft_ccld_qty', 0))
                            else:
                                avg_price = float(item.get('avg_prvs', 0))
                                tot_qty = int(item.get('tot_ccld_qty', 0))
                            
                            if odno and avg_price > 0:
                                # [수정] 체결 내역 분리 저장 (기존 내역 업데이트 대신 신규 추가)
                                if not db_manager.db.check_trade_exists(odno, "체결"):
                                    if config.FILE_DEBUG_LEVEL == "DEBUG":
                                        logger.debug(f"[Account] 신규 체결 DB 저장 시도: {odno}")
                                    
                                    # 체결 시간 포맷팅
                                    ord_dt = item.get('ord_dt', '')
                                    ord_tmd = item.get('ord_tmd', '')
                                    trade_time = None
                                    if len(ord_dt) == 8 and len(ord_tmd) == 6:
                                        trade_time = f"{ord_dt[:4]}-{ord_dt[4:6]}-{ord_dt[6:]} {ord_tmd[:2]}:{ord_tmd[2:4]}:{ord_tmd[4:]}"
                                    
                                    type_cd = item.get('sll_buy_dvsn_cd')
                                    type_str = "매수" if type_cd == '02' else ("매도" if type_cd == '01' else "기타")
                                    
                                    # 원 주문 유형 조회 (수동/자동 태그 반영)
                                    origin_trade = db_manager.db.get_trade_by_odno(odno)
                                    profit_amt = 0
                                    profit_rate = 0.0
                                    score = 0
                                    stop_loss_rate = 0.0
                                    
                                    if origin_trade:
                                        type_str = origin_trade['type'] # 기존 타입 유지
                                        profit_amt = origin_trade.get('profit_amt', 0)
                                        profit_rate = origin_trade.get('profit_rate', 0.0)
                                        score = origin_trade.get('strategy_score', 0)
                                        stop_loss_rate = float(origin_trade.get('stop_loss_rate', 0.0))
                                    
                                    db_manager.db.insert_trade(
                                        type_str, item.get('pdno'), item.get('prdt_name') or item.get('ovrs_item_name') or item.get('item_nm'), 
                                        tot_qty, avg_price, odno, 
                                        order_status="체결", custom_time=trade_time,
                                        reason="체결 확인",
                                        profit_amt=profit_amt, profit_rate=profit_rate, strategy_score=score,
                                        stop_loss_rate=stop_loss_rate
                                    )
                                    # [추가] 시장가 주문 등의 경우를 위해 원 주문(접수)의 단가도 체결가로 업데이트
                                    # [수정] 원본 주문 보존을 위해 업데이트 제거
                                    # db_manager.db.update_trade(odno, price=avg_price)
                                    
                                    total_count += 1
                                else:
                                    if config.FILE_DEBUG_LEVEL == "DEBUG":
                                        logger.debug(f"[Account] 이미 존재하는 체결 내역입니다. 저장 스킵 (ODNO: {odno})")
                except: pass
                progress.advance(task)
        finally:
            context.trade_context.use_auto_account = original_context
        
    logger.debug(f"[HISTORY_DEBUG] sync_today_trades() 종료. 처리 건수: {total_count}")
    return total_count

def _display_balance_details(cano, acnt_prdt_cd):
    """특정 계좌의 잔고 상세 출력"""
    # ---------------------------
    # [국내 주식 잔고]
    # ---------------------------
    
    with Progress(
        SpinnerColumn(),
        TextColumn("[progress.description]{task.description}"),
        console=config.console,
        transient=True
    ) as progress:
        progress.add_task("[cyan]국내 잔고 조회 중...[/cyan]", total=None)
        # [수정] api.get_domestic_balance 직접 호출
        raw_holdings, raw_summary = api.get_domestic_balance(cano, acnt_prdt_cd)
        
        if raw_holdings is None:
            config.console.print("[red]잔고 조회 실패 (API 오류)[/red]")
            return

        # 보유수량 0 이상인 종목만 필터링
        output1 = [item for item in raw_holdings if int(item.get('hldg_qty', 0)) > 0]
        summary = raw_summary[0] if raw_summary else None
        
        if output1:
            table = Table(title="\n[국내] 계좌 잔고 현황", box=box.HORIZONTALS, show_header=True, header_style="dim", border_style="dim")
            table.add_column("종목명", justify="left")
            table.add_column("보유수량", justify="right")
            table.add_column("매입단가", justify="right")
            table.add_column("현재가", justify="right")
            table.add_column("매입금액", justify="right")
            table.add_column("평가금액", justify="right")
            table.add_column("평가손익", justify="right")
            table.add_column("수익률", justify="right")
            table.add_column("목표가", justify="right", style="dim")
            table.add_column("손절가", justify="right", style="dim")
            
            calculated_total_pchs = 0
            calculated_total_eval = 0
            calculated_total_profit = 0
            
            m_codes = utils.get_memo_codes() # [추가]
            for item in output1:
                code = item['pdno']
                m_mark = "[M]" if code in m_codes else ""
                name = f"{item['prdt_name']} ({code}) {m_mark}".strip()
                qty = int(item['hldg_qty'])
                buy_price = float(item['pchs_avg_pric'])
                cur_price = int(item['prpr'])
                eval_amt = int(item['evlu_amt'])
                profit = int(item['evlu_pfls_amt'])
                rate = float(item['evlu_pfls_rt'])
                pchs_amt = int(qty * buy_price)
                calculated_total_pchs += pchs_amt
                calculated_total_eval += eval_amt
                calculated_total_profit += profit
                
                # [추가] 손절가 및 기준 계산
                sl_rate = config.SELL_STRATEGY["STOP_LOSS_RATE"]
                tp_rate = config.SELL_STRATEGY["TAKE_PROFIT_RATE"]
                
                # 개별 룰 확인
                rule = db_manager.db.get_stock_strategy(code)
                if rule:
                    sl_rate = rule['stop_loss']
                    tp_rate = rule['take_profit']
                
                target_price = buy_price * (1 + tp_rate / 100)
                target_str = f"[red]{int(target_price):,}[/][dim](+{tp_rate:g}%)[/dim]"
                
                fixed_stop_price = buy_price * (1 + sl_rate / 100)
                stop_str_list = [f"[dim]고정:[/dim][blue]{int(fixed_stop_price):,}[/][dim]({sl_rate:g}%)[/dim]"]
                
                atr_sl_rate = None
                last_buy = db_manager.db.get_latest_buy_trade(code)
                if last_buy and last_buy.get('stop_loss_rate'):
                    val = float(last_buy['stop_loss_rate'])
                    if val != 0.0:
                        atr_sl_rate = val
                
                if atr_sl_rate is not None:
                    atr_stop_price = buy_price * (1 + atr_sl_rate / 100)
                    stop_str_list.append(f"[dim]ATR:[/dim][blue]{int(atr_stop_price):,}[/][dim]({atr_sl_rate:g}%)[/dim]")
                
                stop_str = " ".join(stop_str_list)
                
                p_color = "[red]" if rate > 0 else ("[blue]" if rate < 0 else "[white]")
                table.add_row(
                    name,
                    f"{qty:,}주",
                    f"{buy_price:,.0f}원",
                    f"{cur_price:,}원",
                    f"{pchs_amt:,}원",
                    f"{eval_amt:,}원",
                    f"{p_color}{profit:+,}원[/]",
                    f"{p_color}{rate:.2f}%[/]",
                    target_str,
                    stop_str
                )
            
            config.console.print(table)
            
            # 요약 정보 출력
            if summary:
                total_rate = 0.0
                if calculated_total_pchs > 0:
                    total_rate = (calculated_total_profit / calculated_total_pchs) * 100
                
                profit_color = "[red]" if calculated_total_profit > 0 else ("[blue]" if calculated_total_profit < 0 else "[white]")
                config.console.print(f"[bold]  국내 총 평가금액:[/bold] {calculated_total_eval:,}원  |  [bold]총 평가손익:[/bold] {profit_color}{calculated_total_profit:+,}원 ({total_rate:+.2f}%)[/]")
        else:
            config.console.print("\n[yellow]국내 보유 종목이 없습니다.[/yellow]")

    config.console.print()

    # ---------------------------
    # [해외 주식 잔고]
    # ---------------------------
    with Progress(
        SpinnerColumn(),
        TextColumn("[progress.description]{task.description}"),
        console=config.console,
        transient=True
    ) as progress:
        progress.add_task("[cyan]해외 잔고 조회 중...[/cyan]", total=None)
        # [수정] api.get_overseas_balance 직접 호출
        all_overseas_holdings = api.get_overseas_balance(cano, acnt_prdt_cd)

    if not all_overseas_holdings:
        config.console.print("\n[yellow]해외 보유 종목이 없습니다.[/yellow]\n")
    else:
        table_ovrs = Table(title="\n[해외] 계좌 잔고 현황", box=box.HORIZONTALS, show_header=True, header_style="dim", border_style="dim")
        table_ovrs.add_column("종목명(코드)", justify="left")
        table_ovrs.add_column("거래소", justify="center")
        table_ovrs.add_column("보유수량", justify="right")
        table_ovrs.add_column("매입단가($)", justify="right")
        table_ovrs.add_column("현재가($)", justify="right") 
        table_ovrs.add_column("매입금액($)", justify="right") 
        table_ovrs.add_column("평가금액($)", justify="right")
        table_ovrs.add_column("평가손익($)", justify="right")
        table_ovrs.add_column("수익률(%)", justify="right")
        table_ovrs.add_column("목표가", justify="right", style="dim")
        table_ovrs.add_column("손절가", justify="right", style="dim")

        tot_ovrs_evlu = 0.0
        tot_ovrs_profit = 0.0
        tot_ovrs_pchs = 0.0
        has_ovrs_item = False
        
        m_codes = utils.get_memo_codes() # [추가]

        for item in all_overseas_holdings:
            qty = float(item.get('ovrs_cblc_qty', 0) or item.get('ord_psbl_qty', 0))
            
            if qty > 0:
                has_ovrs_item = True
                code = item.get('ovrs_pdno', '-')
                m_mark = "[M]" if code in m_codes else ""
                name = f"{item.get('ovrs_item_name', '-')} {m_mark}".strip()
                pchs_avg = float(item.get('pchs_avg_pric', 0))
                profit = float(item.get('frcr_evlu_pfls_amt', 0))
                rate = float(item.get('evlu_pfls_rt', 0))
                exc_name = item.get('_exchange', '')
                cur_price = float(item.get('ovrs_now_pric', 0))
                item_pchs = qty * pchs_avg
                item_eval = item_pchs + profit
                if cur_price == 0 and qty > 0: cur_price = item_eval / qty

                tot_ovrs_evlu += item_eval
                tot_ovrs_pchs += item_pchs
                tot_ovrs_profit += profit

                # [추가] 손절가 및 기준 계산
                sl_rate = config.SELL_STRATEGY["STOP_LOSS_RATE"]
                tp_rate = config.SELL_STRATEGY["TAKE_PROFIT_RATE"]
                
                # 개별 룰 확인
                rule = db_manager.db.get_stock_strategy(code)
                if rule:
                    sl_rate = rule['stop_loss']
                    tp_rate = rule['take_profit']
                
                target_price = pchs_avg * (1 + tp_rate / 100)
                target_str = f"[red]${target_price:,.2f}[/][dim](+{tp_rate:g}%)[/dim]"
                
                fixed_stop_price = pchs_avg * (1 + sl_rate / 100)
                stop_str_list = [f"[dim]고정:[/dim][blue]${fixed_stop_price:,.2f}[/][dim]({sl_rate:g}%)[/dim]"]
                
                atr_sl_rate = None
                last_buy = db_manager.db.get_latest_buy_trade(code)
                if last_buy and last_buy.get('stop_loss_rate'):
                    val = float(last_buy['stop_loss_rate'])
                    if val != 0.0:
                        atr_sl_rate = val
                
                if atr_sl_rate is not None:
                    atr_stop_price = pchs_avg * (1 + atr_sl_rate / 100)
                    stop_str_list.append(f"[dim]ATR:[/dim][blue]${atr_stop_price:,.2f}[/][dim]({atr_sl_rate:g}%)[/dim]")
                    
                stop_str = " ".join(stop_str_list)

                color = "[red]" if profit > 0 else ("[blue]" if profit < 0 else "[white]")
                
                table_ovrs.add_row(
                    f"{name} ({code})", 
                    exc_name,
                    f"{qty:,.0f}", 
                    f"{pchs_avg:,.2f}",
                    f"{cur_price:,.2f}", 
                    f"{item_pchs:,.2f}", 
                    f"{item_eval:,.2f}", 
                    f"{color}{profit:+,.2f}[/]", 
                    f"{color}{rate:+.2f}[/]",
                    target_str,
                    stop_str
                )

        if has_ovrs_item:
            config.console.print(table_ovrs)
            total_ovrs_rate = 0.0
            if tot_ovrs_pchs > 0:
                total_ovrs_rate = (tot_ovrs_profit / tot_ovrs_pchs) * 100
                
            profit_color = "[red]" if tot_ovrs_profit > 0 else ("[blue]" if tot_ovrs_profit < 0 else "[white]")
            config.console.print(f"[bold]  해외 총 평가금액:[/bold] ${tot_ovrs_evlu:,.2f}  |  [bold]총 평가손익:[/bold] {profit_color}${tot_ovrs_profit:+,.2f} ({total_ovrs_rate:+.2f}%)[/]")
        else:
            config.console.print("\n[yellow]해외 보유 종목이 없습니다 (수량 0).[/yellow]")

def get_account_balance():
    """보유 잔고 조회 (메인/자동 계좌 순차 조회)"""
    time.sleep(0.5)
    
    accounts = []
    if config.session.is_simulation:
        accounts.append((config.session.cano, config.session.acnt_prdt_cd, "모의투자"))
    else:
        accounts.append((config.session.cano, config.session.acnt_prdt_cd, "실전투자 (수동)"))
        if config.session.auto_cano and config.session.auto_acnt_prdt_cd and \
           (config.session.auto_cano != config.session.cano or config.session.auto_acnt_prdt_cd != config.session.acnt_prdt_cd):
            accounts.append((config.session.auto_cano, config.session.auto_acnt_prdt_cd, "실전투자 (자동)"))
    
    for i, (cano, acnt, label) in enumerate(accounts):
        if i > 0: config.console.print("\n")
        config.console.print(f"\n[bold cyan]{label} 계좌 잔고 ({cano}-{acnt})[/]")
        _display_balance_details(cano, acnt)

def get_asset_status_data(cano, acnt_prdt_cd, progress=None, task=None):
    """자산 현황 데이터 조회 및 계산 (UI 로직 없음)"""
    summary_data = {
        "withdraw": 0,      "tot_asset": 0,
        "dep_dom": 0,       "dep_ovs": 0,
        "d1_dep": 0,        "d2_dep": 0,
        "sec_buy": 0,       "sec_eval": 0,      "sec_pl": 0,
        "realized_pl": 0,   "total_cost": 0,
        "buy_today": 0,     "sell_today": 0,
        "ovrs_eval_krw": 0, "ovrs_pl_krw": 0,
        "order_possible": 0, # [추가] 주문가능금액
        "d2_real": 0,        # [추가] 실제 D+2 예수금
        "next_day_plus": 0,  # [추가] 익일결재(+)
        "next_day_minus": 0, # [추가] 익일결재(-)
        "api_tot_asset": 0   # [추가] API 제공 총 평가금액 (검증용)
    }
    
    # 1. 금일 데이터 조회
    if progress: progress.update(task, description="[cyan]금일 매매 손익 조회 중...[/cyan]")
    try:
        # [원복] 항상 현재 날짜 기준 조회 (새벽 로직 제거)
        profit_data = fetch_today_profit_summary(cano, acnt_prdt_cd)
        summary_data['buy_today'] = profit_data['buy_amt']
        summary_data['sell_today'] = profit_data['sell_amt']
        summary_data['total_cost'] = profit_data['total_cost']
        summary_data['realized_pl'] = profit_data['realized_pl']
        
        # [수정] 기간별 손익 API가 매매금액을 0으로 반환하는 경우가 많으므로
        # 체결 내역(fetch_today_history)을 조회하여 값이 더 크다면(누락된 경우) 덮어쓰기 수행
        backup_data = fetch_today_history(cano, acnt_prdt_cd)
        if backup_data['buy_total'] > summary_data['buy_today']:
            summary_data['buy_today'] = backup_data['buy_total']
        if backup_data['sell_total'] > summary_data['sell_today']:
            summary_data['sell_today'] = backup_data['sell_total']
            
        # [추가] 모의투자이거나 실현손익이 0인 경우 DB에서 금일 손익 및 매매금액 합산 (Fallback)
        # 모의투자는 기간별 손익 API를 지원하지 않으므로 DB 활용 필수
        if config.session.is_simulation or summary_data['realized_pl'] == 0:
            try:
                today_str = datetime.now().strftime("%Y-%m-%d")
                target_acc = f"{cano}-{acnt_prdt_cd}"
                
                # DB 조회
                db_trades = db_manager.db.get_trades(
                    start_date=today_str, end_date=today_str,
                    is_sim=config.session.is_simulation, account=target_acc
                )
                
                db_pl = 0
                db_buy = 0
                db_sell = 0
                
                for t in db_trades:
                    # [수정] 중복 합산 방지: '체결 확인' 사유가 있는 확정 내역만 집계
                    # (DB에는 원주문(접수)과 체결 내역이 모두 존재할 수 있어 단순 합산 시 이중 계산됨)
                    if "체결 확인" not in t.get('reason', ''):
                        continue

                    type_str = t.get('type', '').lower()
                    price = float(t.get('price', 0))
                    qty = int(t.get('qty', 0))
                    amt = int(price * qty)
                    
                    if "sell" in type_str or "매도" in type_str:
                        db_pl += int(t.get('profit_amt') or 0)
                        db_sell += amt
                    elif "buy" in type_str or "매수" in type_str:
                        db_buy += amt
                
                if summary_data['realized_pl'] == 0: summary_data['realized_pl'] = db_pl
                if db_buy > summary_data['buy_today']: summary_data['buy_today'] = db_buy
                if db_sell > summary_data['sell_today']: summary_data['sell_today'] = db_sell
                    
            except Exception as e:
                logger.debug(f"DB 금일 데이터 조회 실패: {e}")

    except: pass

    # 2. 국내 주식 잔고 및 자산
    if progress: progress.update(task, description="[cyan]국내 주식 잔고 및 평가금 조회 중...[/cyan]")
    try:
        # api.get_domestic_balance 사용 (내부에서 OPSQ2001 처리)
        output1, output2 = api.get_domestic_balance(cano, acnt_prdt_cd)
        
        if output1 is not None:
            # [수정] 보유 중인 종목만 필터링
            holdings = [h for h in output1 if int(h.get('hldg_qty', 0)) > 0]
            calc_buy = 0; calc_eval = 0; calc_pl = 0
            for item in holdings:
                qty = int(item['hldg_qty'])
                avg_pric = float(item['pchs_avg_pric'])
                calc_buy += int(qty * avg_pric) # 매입금액 직접 계산 (API 누락 방지)
                calc_eval += api.safe_int(item.get('evlu_amt'))
                calc_pl += api.safe_int(item.get('evlu_pfls_amt'))
            summary_data['sec_buy'] = calc_buy
            summary_data['sec_eval'] = calc_eval
            summary_data['sec_pl'] = calc_pl

            if output2:
                summary = output2[0]
                # [수정] 실전/모의 공통으로 D+1, D+2, 예수금 데이터 파싱
                summary_data['api_tot_asset'] = api.safe_int(summary.get('tot_evlu_amt')) # API 제공 총평가금
                summary_data['d1_dep'] = api.safe_int(summary.get('nxdy_excc_amt'))
                summary_data['d2_dep'] = api.safe_int(summary.get('prvs_rcdl_excc_amt'))
                summary_data['dep_dom'] = api.safe_int(summary.get('dnca_tot_amt'))

                if config.FILE_DEBUG_LEVEL == "DEBUG":
                    logger.debug(f"[ACCOUNT_DEBUG] Balance Summary (Output2): {summary}")
                
                # [추가] 익일결재 금액 계산 (전일 매도/매수 기준)
                bfdy_sll = api.safe_int(summary.get('bfdy_sll_amt'))
                bfdy_buy = api.safe_int(summary.get('bfdy_buy_amt'))
                bfdy_tlex = api.safe_int(summary.get('bfdy_tlex_amt'))
                summary_data['next_day_plus'] = bfdy_sll - bfdy_tlex
                summary_data['next_day_minus'] = bfdy_buy
                
                if not config.session.is_simulation:
                    # [추가] 금일 제비용 보정 (기간별 손익 API 누락 시 잔고 요약 데이터 활용)
                    tlex_amt = api.safe_int(summary.get('thdt_tlex_amt'))
                    if tlex_amt > summary_data['total_cost']:
                        summary_data['total_cost'] = tlex_amt
                    
                    summary_data['withdraw'] = summary_data['d2_dep'] 

    except Exception as e:
        logger.error(f"자산 현황 조회 오류: {str(e)}")
        pass
        
    # [추가] 해외 주식 잔고 합산 (원화 환산)
    if progress: progress.update(task, description="[cyan]해외 주식 잔고 및 환산액 계산 중...[/cyan]")
    try:
        ovrs_holdings = fetch_overseas_balance(cano, acnt_prdt_cd)
        ovrs_buy_usd = 0.0
        ovrs_eval_usd = 0.0
        ovrs_pl_usd = 0.0
        
        for item in ovrs_holdings:
            qty = float(item.get('ovrs_cblc_qty', 0) or item.get('ord_psbl_qty', 0))
            if qty > 0:
                pchs = float(item.get('pchs_avg_pric', 0))
                profit = float(item.get('frcr_evlu_pfls_amt', 0))
                buy_amt = qty * pchs
                eval_amt = buy_amt + profit
                
                ovrs_buy_usd += buy_amt
                ovrs_eval_usd += eval_amt
                ovrs_pl_usd += profit
        
        exchange_rate = utils.get_exchange_rate()
        
        ovrs_eval_krw = int(ovrs_eval_usd * exchange_rate)
        summary_data['ovrs_eval_krw'] = ovrs_eval_krw
        ovrs_pl_krw = int(ovrs_pl_usd * exchange_rate)
        summary_data['ovrs_pl_krw'] = ovrs_pl_krw
        
        if config.FILE_DEBUG_LEVEL == "DEBUG":
            logger.debug(f"CALC (Ovrs->KRW) | USD: Buy={ovrs_buy_usd:.2f}, Eval={ovrs_eval_usd:.2f}, PL={ovrs_pl_usd:.2f} | Rate: {exchange_rate} | KRW: Eval={ovrs_eval_krw}, PL={ovrs_pl_krw}")
        
        summary_data['sec_buy'] += int(ovrs_buy_usd * exchange_rate)
        summary_data['sec_eval'] += ovrs_eval_krw
        summary_data['sec_pl'] += ovrs_pl_krw
    except Exception as e:
        pass

    # 3. 예수금 조회
    if progress: progress.update(task, description="[cyan]예수금 조회 및 최종 집계 중...[/cyan]")
    try:
        with utils.AccountContext(cano):
            dep_data = api.get_deposit_balance(cano, acnt_prdt_cd)
            
            if dep_data:
                # [수정] 실전/모의 모두 상세 예수금 정보로 업데이트 (잔고 조회 API보다 정확함)
                summary_data['dep_dom'] = dep_data['deposit']
                summary_data['d2_dep'] = dep_data['d2_deposit']
                summary_data['withdraw'] = dep_data['withdraw']
                summary_data['order_possible'] = dep_data.get('order_possible', 0)
                summary_data['d2_real'] = dep_data.get('d2_real', 0)
                
                # [수정] 실전투자일 경우 UI 표시용 D+2 값을 실제 D+2(가수도) 값으로 덮어쓰기
                if not config.session.is_simulation and summary_data['d2_real'] > 0:
                    summary_data['d2_dep'] = summary_data['d2_real']
                
                if not config.session.is_simulation:
                    summary_data['dep_ovs'] = dep_data['foreign_deposit']
            
            if config.FILE_DEBUG_LEVEL == "DEBUG":
                logger.debug(f"[ACCOUNT_DEBUG] Deposit Detail: {dep_data}")
    except Exception: pass
    
    # 4. 최종 계산
    # API 지연(Lag)에 의한 총 자산 금액의 왜곡을 방지하기 위해 
    # API가 제공하는 tot_evlu_amt 대신 개별 종목 합산 기반으로 직접 계산하여 일관성 유지
    real_cash = summary_data['d2_dep']
    summary_data['tot_asset'] = real_cash + summary_data['dep_ovs'] + summary_data['sec_eval'] + summary_data['ovrs_eval_krw']
    
    if config.FILE_DEBUG_LEVEL == "DEBUG":
        logger.debug(f"[ACCOUNT_DEBUG] Calculated Total Asset: {summary_data['tot_asset']:,} (D2 + Ovs + Sec)")
    
    return summary_data

def _display_asset_status(cano, acnt_prdt_cd):
    """특정 계좌의 자산 현황 출력 (UI)"""
    
    summary_data = None
    with Progress(
        SpinnerColumn(),
        TextColumn("[progress.description]{task.description}"),
        console=config.console,
        transient=True
    ) as progress:
        task = progress.add_task("[cyan]자산 현황 조회 시작...[/cyan]", total=None)
        summary_data = get_asset_status_data(cano, acnt_prdt_cd, progress, task)

    display_tot_deposit = summary_data['dep_dom'] + summary_data['dep_ovs']
    roi = 0.0
    if summary_data['sec_buy'] > 0: roi = (summary_data['sec_pl'] / summary_data['sec_buy']) * 100
    
    def get_color(val): return "red" if val > 0 else ("blue" if val < 0 else "white")

    summary_table = Table(box=box.HORIZONTALS, show_header=False, padding=(0, 2), expand=True, border_style="dim", show_edge=False)
    summary_table.add_column("Item", justify="left", style="white", ratio=6) 
    summary_table.add_column("Value", justify="right", style="white", ratio=4)

    summary_table.add_row("총 평가금액", f"{summary_data['tot_asset']:,}원")
    summary_table.add_row("총 예수금(D+0)", f"{display_tot_deposit:,}원")
    summary_table.add_row("    원화 예수금", f"{summary_data['dep_dom']:,}원", style="dim")
    
    # [수정] 모의투자도 D+1, D+2 정보 표시 (사용자 요청)
    d2_val = summary_data.get('d2_real', 0)
    if d2_val == 0: d2_val = summary_data['d2_dep']
    
    summary_table.add_row("      └ D+1 (익일)", f"{summary_data['d1_dep']:,}원", style="dim")
    summary_table.add_row("      └ D+2 (가수도)", f"{d2_val:,}원", style="dim")
    
    # [추가] 익일결재 정보 표시 (값이 있을 때만)
    if summary_data['next_day_plus'] > 0:
        summary_table.add_row("      └ 익일결재(+)", f"{summary_data['next_day_plus']:,}원", style="dim")
    if summary_data['next_day_minus'] > 0:
        summary_table.add_row("      └ 익일결재(-)", f"{summary_data['next_day_minus']:,}원", style="dim")
    
    summary_table.add_row("    외화예수금", f"{summary_data['dep_ovs']:,}원", style="dim")
    summary_table.add_row("주문가능금액", f"[bold green]{summary_data['order_possible']:,}원[/]")
    summary_table.add_row("출금가능금액", f"{summary_data['withdraw']:,}원")
    summary_table.add_section()
    summary_table.add_row("유가증권매입금액", f"{summary_data['sec_buy']:,}원")
    summary_table.add_row("유가증권평가금액", f"{summary_data['sec_eval']:,}원")
    if summary_data['ovrs_eval_krw'] > 0:
        summary_table.add_row("  └ 해외주식(원화)", f"{summary_data['ovrs_eval_krw']:,}원", style="dim")
    
    pl_str = f"{summary_data['sec_pl']:,}원  ({roi:.2f}%)"
    summary_table.add_row("평가손익금액(보유)", f"[{get_color(summary_data['sec_pl'])}]{pl_str}[/]")
    if summary_data['ovrs_eval_krw'] > 0:
        ovrs_pl_val = summary_data['ovrs_pl_krw']
        summary_table.add_row("  └ 해외손익(원화)", f"[{get_color(ovrs_pl_val)}]{ovrs_pl_val:+,}원[/]", style="dim")

    summary_table.add_section()
    
    # [원복] 라벨 고정
    summary_table.add_row("금일 매수 체결합계", f"{summary_data['buy_today']:,}원")
    summary_table.add_row("금일 매도 체결합계", f"{summary_data['sell_today']:,}원")
    summary_table.add_row("금일 제비용", f"{summary_data['total_cost']:,}원")
    summary_table.add_row("금일 실현 손익 (확정)", f"[{get_color(summary_data['realized_pl'])}]{summary_data['realized_pl']:,}원[/]")

    panel = Panel(
        summary_table,
        title="계좌 자산 현황 요약",
        subtitle=f"[dim]업데이트: {datetime.now().strftime('%H:%M:%S')}[/]",
        subtitle_align="right",
        width=70,
        border_style="green"
    )

    config.console.print()
    config.console.print(panel)
    config.console.print("\n")

def get_deposit_balance():
    """자산 현황 조회 (메인/자동 계좌 순차 조회)"""
    time.sleep(0.5)
    
    accounts = []
    if config.session.is_simulation:
        accounts.append((config.session.cano, config.session.acnt_prdt_cd, "모의투자"))
    else:
        accounts.append((config.session.cano, config.session.acnt_prdt_cd, "실전투자 (수동)"))
        if config.session.auto_cano and config.session.auto_acnt_prdt_cd and \
           (config.session.auto_cano != config.session.cano or config.session.auto_acnt_prdt_cd != config.session.acnt_prdt_cd):
            accounts.append((config.session.auto_cano, config.session.auto_acnt_prdt_cd, "실전투자 (자동)"))
            
    for cano, acnt, label in accounts:
        config.console.print(f"\n[bold cyan]{label} 자산 현황 ({cano}-{acnt})[/]")
        _display_asset_status(cano, acnt)
        
def export_trade_history_to_excel():
    """전체 거래 내역을 엑셀 파일로 저장"""
    try:
        trades = db_manager.db.get_trades(is_sim=config.session.is_simulation, limit=None)
        if not trades:
            config.console.print("\n[yellow]저장할 거래 내역이 없습니다.[/yellow]")
            return

        # DataFrame 생성
        df = pd.DataFrame(trades)
        
        # [추가] 단가 포맷팅 (국내 주식 소수점 제거)
        if 'price' in df.columns and 'code' in df.columns:
            def _format_price(row):
                try:
                    val = float(row['price'])
                    code = str(row['code'])
                    # 국내 주식 (6자리, 숫자로 시작)
                    if len(code) == 6 and code[0].isdigit() and code.isalnum():
                        return int(val)
                    return val
                except: return row['price']
            df['price'] = df.apply(_format_price, axis=1)

        # [추가] 수익률 및 손익금 포맷팅 (+/- 기호 추가)
        if 'profit_rate' in df.columns:
            def _format_rate(val):
                try:
                    if val is None or val == '': return "0.00"
                    f = float(val)
                    return f"{f:+.2f}"
                except: return val
            df['profit_rate'] = df['profit_rate'].apply(_format_rate)

        if 'profit_amt' in df.columns and 'code' in df.columns:
            def _format_amt(row):
                val = row.get('profit_amt')
                code = str(row.get('code', ''))
                try:
                    if val is None or val == '': return "0"
                    f = float(val)
                    # 국내 주식 (6자리, 숫자로 시작)
                    if len(code) == 6 and code[0].isdigit() and code.isalnum():
                        return f"{int(f):+,}"
                    # 해외 주식
                    return f"{f:+,.2f}"
                except: return val
            df['profit_amt'] = df.apply(_format_amt, axis=1)

        if 'snapshot' in df.columns:
            def _process_snapshot(row):
                val = row.get('snapshot')
                score = row.get('strategy_score')
                
                data = {}
                # 점수 정보 병합 (가장 앞에 추가)
                if score is not None and score != '':
                    try:
                        data['score'] = float(score)
                    except: pass

                try:
                    if val:
                        loaded = json.loads(val)
                        if isinstance(loaded, dict):
                            data.update(loaded)
                except: pass

                if not data: return val
            
                def _recursive_round(obj):
                    if isinstance(obj, float): return round(obj, 2)
                    if isinstance(obj, dict): return {k: _recursive_round(v) for k, v in obj.items()}
                    if isinstance(obj, list): return [_recursive_round(v) for v in obj]
                    return obj
                return json.dumps(_recursive_round(data), ensure_ascii=False)
            
            # 행 단위(axis=1)로 처리하여 점수 컬럼 접근
            df['snapshot'] = df.apply(_process_snapshot, axis=1)

        # 컬럼 순서 및 이름 변경 (사용자 친화적)
        columns_map = {
            'time': '일시',
            'account': '계좌번호',
            'is_sim': '종류',
            'odno': '주문번호',
            'org_odno': '원주문',
            'type': '유형',
            'order_status': '상태',
            'name': '종목명',
            'code': '종목코드',
            'qty': '수량',
            'price': '단가',
            'profit_amt': '손익금',
            'profit_rate': '수익률',
            'reason': '매매사유',
            'snapshot': '스냅샷'
        }
        
        # 존재하는 컬럼만 선택하여 순서대로 정렬 (없는 컬럼은 제외)
        target_cols = [c for c in columns_map.keys() if c in df.columns]
        df = df[target_cols]
        df.rename(columns=columns_map, inplace=True)
        
        # 모의투자여부 가독성 좋게 변경
        if '종류' in df.columns:
            if '유형' in df.columns:
                # 실전(0)이면서 유형에 'AUTO'나 '자동'이 포함되면 '자동'으로 표시
                df['종류'] = df.apply(lambda row: '모의' if row['종류'] == 1 else ('자동' if 'AUTO' in str(row['유형']) or '자동' in str(row['유형']) else '실전'), axis=1)
            else:
                df['종류'] = df['종류'].apply(lambda x: '모의' if x == 1 else '실전')

        # 파일명 생성
        timestamp = datetime.now().strftime('%Y%m%d_%H%M%S')
        filename_xlsx = os.path.join(config.DATA_DIR, f"trade_history_{timestamp}.xlsx")
        
        # 엑셀 저장
        try:
            with Progress(
                SpinnerColumn(),
                TextColumn("[progress.description]{task.description}"),
                BarColumn(),
                TextColumn("[progress.percentage]{task.percentage:>3.0f}%"),
                console=config.console,
                transient=True
            ) as progress:
                task = progress.add_task(f"[cyan]'{os.path.basename(filename_xlsx)}' 파일로 저장 중...[/cyan]", total=None)
                
                with pd.ExcelWriter(filename_xlsx, engine='openpyxl') as writer:
                    # [추가] 컬럼 너비 자동 조절 헬퍼 함수
                    def _auto_adjust_width(worksheet):
                        for i, col in enumerate(worksheet.columns):
                            col_idx = i + 1
                            col_letter = get_column_letter(col_idx)
                            
                            # 헤더 길이 계산
                            header_val = worksheet.cell(row=1, column=col_idx).value
                            s_header = str(header_val) if header_val else ""
                            max_width = len(s_header) + sum(0.7 for c in s_header if ord(c) > 127)
                            
                            # 데이터 길이 계산
                            for cell in col[1:]:
                                val = cell.value
                                if val:
                                    s_val = str(val)
                                    length = len(s_val) + sum(0.7 for c in s_val if ord(c) > 127)
                                    if length > max_width: max_width = length
                            
                            # 최대 너비 제한 (스냅샷 등 긴 컬럼 고려)
                            limit = 100 if s_header in ["매매사유", "스냅샷", "비고"] else 60
                            worksheet.column_dimensions[col_letter].width = min(max_width * 1.2, limit)

                    if '계좌번호' in df.columns:
                        # 계좌번호가 없는 데이터 처리
                        df['계좌번호'] = df['계좌번호'].fillna('기타')
                        
                        # 계좌번호별로 시트 분리 저장
                        accounts = df['계좌번호'].unique()
                        progress.update(task, total=len(accounts))
                        
                        for acc in accounts:
                            # 시트 이름 정제 (특수문자 제거 및 길이 제한 31자)
                            sheet_name = str(acc).replace(':', '').replace('\\', '').replace('/', '').replace('?', '').replace('*', '').replace('[', '').replace(']', '')[:31]
                            if not sheet_name: sheet_name = "Unknown"
                            df[df['계좌번호'] == acc].to_excel(writer, sheet_name=sheet_name, index=False)
                            
                            # [추가] 너비 조절 적용
                            _auto_adjust_width(writer.sheets[sheet_name])
                            
                            progress.advance(task)
                    else:
                        progress.update(task, total=1)
                        sheet_name = '전체내역'
                        df.to_excel(writer, sheet_name=sheet_name, index=False)
                        
                        # [추가] 너비 조절 적용
                        _auto_adjust_width(writer.sheets[sheet_name])
                        
                        progress.advance(task)

            config.console.print(f"\n[bold green]성공적으로 저장되었습니다: {os.path.basename(filename_xlsx)}[/bold green]")
            config.console.print("[dim]  - 탭 구분: 계좌번호[/dim]")
        except ImportError:
            config.console.print("\n[yellow]openpyxl 라이브러리가 설치되지 않아 엑셀(.xlsx) 저장이 불가능합니다.[/yellow]")
            config.console.print()
            if Prompt.ask("대신 CSV 파일로 저장하시겠습니까?", choices=["y", "n"], default="y") == "y":
                config.console.print()
                filename_csv = os.path.join(config.DATA_DIR, f"trade_history_{timestamp}.csv")
                df.to_csv(filename_csv, index=False, encoding='utf-8-sig')
                config.console.print(f"\n[bold green]성공적으로 저장되었습니다: {os.path.basename(filename_csv)}[/bold green]")
            else:
                config.console.print("[dim]저장을 취소했습니다. (터미널에서 'pip install openpyxl'을 실행하세요)[/dim]")

    except Exception as e:
        config.console.print(f"\n[bold red]저장 실패: {e}[/bold red]")

def view_trade_history():
    """DB에 저장된 거래 내역 조회"""
    logger.debug("[HISTORY_DEBUG] view_trade_history() 진입")
    
    menu_items = [
        ("1", "전체 내역 (최신순 50건)", "All - Latest 50"), ("2", "최근 30일 내역", "Last 30 Days"),
        ("3", "종목코드(티커) 검색", "Search by Ticker"), ("4", "전체 거래 내역 저장", "Save to Excel")
    ]
    choice = utils.show_menu("거래 내역 조회 옵션 (Trade History Options)", menu_items, default_choice="1")
    logger.debug(f"[HISTORY_DEBUG] 사용자 선택: {choice}")
    
    menu_map = {"1": "전체 내역", "2": "최근 30일", "3": "종목 검색", "4": "엑셀 저장"}
    if choice in menu_map:
        context.USER_ACTION_BREADCRUMB.append(f"[{choice}] {menu_map[choice]}")

    if choice.lower() == 'q':
        logger.debug("[HISTORY_DEBUG] 사용자 취소(q)로 종료")
        return False

    # [추가] 조회 전 금일 체결 내역 동기화 (시장가 주문 단가 업데이트)
    try:
        logger.debug("[HISTORY_DEBUG] 체결 내역 동기화 시도")
        sync_today_trades()
    except Exception as e:
        config.console.print(f"[dim red]⚠️ 체결 내역 동기화 중 오류 발생: {e}[/dim red]")
        logger.error(f"[HISTORY_DEBUG] sync_today_trades error: {e}")

    trades = []
    if choice == "1":
        logger.info("운영자 실행: " + " - ".join(context.USER_ACTION_BREADCRUMB))
        try:
            logger.debug("[HISTORY_DEBUG] DB 조회 요청 (limit=50)")
            trades = db_manager.db.get_trades(is_sim=config.session.is_simulation, limit=50)
            logger.debug(f"[HISTORY_DEBUG] DB 조회 완료. 건수: {len(trades)}")
        except Exception as e:
            logger.error(f"[HISTORY_DEBUG] DB 조회 실패: {e}")
            config.console.print(f"[bold red]❌ 거래 내역 조회 실패: {e}[/bold red]")
            return
    elif choice == "2":
        logger.info("운영자 실행: " + " - ".join(context.USER_ACTION_BREADCRUMB))
        start_dt = (datetime.now() - timedelta(days=30)).strftime("%Y-%m-%d")
        try:
            logger.debug(f"[HISTORY_DEBUG] DB 조회 요청 (start_date={start_dt})")
            trades = db_manager.db.get_trades(is_sim=config.session.is_simulation, start_date=start_dt)
            logger.debug(f"[HISTORY_DEBUG] DB 조회 완료. 건수: {len(trades)}")
        except Exception as e:
            logger.error(f"[HISTORY_DEBUG] DB 조회 실패: {e}")
            config.console.print(f"[bold red]❌ 거래 내역 조회 실패: {e}[/bold red]")
            return
    elif choice == "3":
        config.console.print()
        keyword = Prompt.ask("검색할 종목코드(티커) 입력 [dim](이전: q)[/dim]")
        config.console.print()
        if keyword.lower() == 'q': return False
        context.USER_ACTION_BREADCRUMB.append(f"[검색] {keyword}")
        logger.info("운영자 실행: " + " - ".join(context.USER_ACTION_BREADCRUMB))
        try:
            logger.debug(f"[HISTORY_DEBUG] DB 조회 요청 (code={keyword})")
            trades = db_manager.db.get_trades(is_sim=config.session.is_simulation, code=keyword)
            logger.debug(f"[HISTORY_DEBUG] DB 조회 완료. 건수: {len(trades)}")
        except Exception as e:
            logger.error(f"[HISTORY_DEBUG] DB 조회 실패: {e}")
            config.console.print(f"[bold red]❌ 거래 내역 조회 실패: {e}[/bold red]")
            return
    elif choice == "4":
        logger.info("운영자 실행: " + " - ".join(context.USER_ACTION_BREADCRUMB))
        export_trade_history_to_excel()
        return

    if not trades:
        logger.debug("[HISTORY_DEBUG] 조회된 내역 없음. 리턴.")
        config.console.print("\n[yellow]검색된 거래 내역이 없습니다.[/yellow]")
        return

    # [추가] 현재 설정된 계좌 정보 확인 (그룹핑용)
    current_main_acc = f"{config.session.cano}-{config.session.acnt_prdt_cd}"
    current_auto_acc = ""
    if config.session.auto_cano:
        current_auto_acc = f"{config.session.auto_cano}-{config.session.auto_acnt_prdt_cd}"

    # [수정] 데이터 분류 및 그룹핑 (계좌 종류/번호별 분리)
    grouped_trades = {} # (category, account) -> list
    
    for t in trades:
        # 1. 모드 필터링 (모의투자 모드면 모의내역만, 실전이면 실전/자동 내역만)
        is_sim_data = bool(t['is_sim'])
        if config.session.is_simulation and not is_sim_data: continue
        if not config.session.is_simulation and is_sim_data: continue
        
        acc_no = t.get('account', '')

        # 2. 카테고리 결정
        category = "모의"
        if not is_sim_data:
            # [수정] 자동매매 계좌가 별도로 설정되어 있고, 해당 계좌의 내역인 경우 '자동'으로 통합
            if current_auto_acc and current_auto_acc != current_main_acc and acc_no == current_auto_acc:
                category = "자동"
            else:
                if "AUTO" in t['type']: category = "자동"
                else: category = "실전"
            
        # 3. 그룹핑
        key = (category, acc_no)
        if key not in grouped_trades:
            grouped_trades[key] = []
        grouped_trades[key].append(t)
        
    if not grouped_trades:
        logger.debug("[HISTORY_DEBUG] 그룹핑 결과 없음 (필터링됨). 리턴.")
        config.console.print("\n[yellow]현재 모드에 해당하는 거래 내역이 없습니다.[/yellow]")
        return

    # 출력 순서 정의 (실전 -> 자동 -> 모의)
    def sort_key(k):
        cat, acc = k
        order = {"실전": 1, "자동": 2, "모의": 3}
        return (order.get(cat, 99), acc)

    sorted_keys = sorted(grouped_trades.keys(), key=sort_key)

    for cat, acc in sorted_keys:
        t_list = grouped_trades[(cat, acc)]
        if not t_list: continue

        logger.debug(f"[HISTORY_DEBUG] 테이블 생성 및 출력: {cat} {acc} ({len(t_list)}건)")

        # 테이블 생성 (제목에 계좌번호 포함)
        table_title = f"\n[{cat}] 거래 히스토리 (계좌: {acc}) - {len(t_list)}건"
        table = Table(title=table_title, box=box.HORIZONTALS, header_style="dim", border_style="dim", show_lines=True)
        table.add_column("시간", justify="center", style="dim", width=15, overflow="fold")
        table.add_column("주문번호", justify="center", style="dim", width=10, overflow="fold")
        # 계좌 컬럼 제거됨
        table.add_column("유형", justify="center", width=10, no_wrap=True)
        table.add_column("상태", justify="center", width=6, overflow="fold")
        table.add_column("종목명(코드)", justify="left", overflow="fold")
        table.add_column("수량", justify="right", width=6, overflow="fold")
        table.add_column("단가", justify="right", width=9, overflow="fold")
        table.add_column("금액", justify="right", width=10, overflow="fold")
        table.add_column("손익(수익률)", justify="right", overflow="fold")
        table.add_column("사유", justify="left", overflow="fold")

        for i, t in enumerate(t_list):
            # [수정] 유형 표기 개선 (줄바꿈 및 색상 적용)
            raw_type = t['type']
            clean_type = raw_type.replace("buy", "매수").replace("BUY", "매수").replace("sell", "매도").replace("SELL", "매도").replace("AUTO", "자동")
            
            base_type = "기타"
            # [수정] 정정/취소 우선 확인
            if "정정" in clean_type: base_type = "정정"
            elif "취소" in clean_type: base_type = "취소"
            elif "매수" in clean_type: base_type = "매수"
            elif "매도" in clean_type: base_type = "매도"
            
            type_disp = base_type
            if base_type == "매수": type_disp = "[red]매수[/]"
            elif base_type == "매도": type_disp = "[blue]매도[/]"
            elif base_type == "정정": type_disp = "[magenta]정정[/]"
            elif base_type == "취소": type_disp = "[yellow]취소[/]"
            
            tag_disp = ""
            if "자동" in clean_type: tag_disp = "([yellow]자동[/])"
            elif "수동" in clean_type: tag_disp = "([green]수동[/])"
            else: tag_disp = "([dim]외부[/])"
            
            type_str = f"{type_disp}{tag_disp}"

            # 상태 표시
            status_str = t.get('order_status', '접수') # 기본값 접수
            if status_str == "체결": status_str = "[green]체결[/]"
            elif "체결(추정)" in status_str: status_str = "[green]체결 추정[/]" # [수정] 괄호 제거 및 색상 적용
            elif "취소" in status_str: status_str = f"[yellow]{status_str}[/]"
            elif "정정" in status_str: status_str = f"[magenta]{status_str}[/]"
            else: status_str = f"[dim]{status_str}[/]"

            # 가격 포맷팅
            price_display = t['price']
            try:
                p_val = float(t['price'])
                code = str(t.get('code', ''))
                is_domestic = code.isdigit() and len(code) == 6

                if p_val > 0:
                    if is_domestic:
                        price_display = f"{int(p_val):,}"
                    else:
                        price_display = f"{p_val:,.2f}"
                elif p_val == 0:
                    if "취소" in t['type'] or "cancel" in t['type'].lower():
                        price_display = "-"
                    else:
                        price_display = "시장가"
            except: pass
            
            # [추가] 체결금액 계산 (단가 * 수량)
            total_amt_display = "-"
            try:
                p_val = float(t['price'])
                q_val = float(t['qty'])
                if p_val > 0 and q_val > 0:
                    tot = p_val * q_val
                    code = str(t.get('code', ''))
                    is_domestic = code.isdigit() and len(code) == 6
                    if is_domestic:
                        total_amt_display = f"{int(tot):,}"
                    else:
                        total_amt_display = f"{tot:,.2f}"
            except: pass
            
            # 손익 정보
            profit_display = "-"
            if base_type == "매도":
                amt = t.get('profit_amt', 0)
                rate = t.get('profit_rate', 0.0)
                if amt is not None and rate is not None:
                    color = "red" if amt > 0 else ("blue" if amt < 0 else "white")
                    profit_display = f"[{color}]{amt:+,}원 ({rate:+.2f}%)[/]"

            # [추가] 사유 상세화: 스냅샷 정보를 활용하여 지표 정보 보강
            reason_display = t.get('reason') or "-"
            
            # 사용자 수동 주문인 경우 스냅샷에서 지표 정보 추출하여 표시
            if "수동" in reason_display and t.get('snapshot'):
                try:
                    snap_data = json.loads(t['snapshot'])
                    if 'indicators' in snap_data:
                        ind = snap_data['indicators']
                        add_info = []
                        if ind.get('rsi') is not None: add_info.append(f"RSI:{ind['rsi']:.1f}")
                        if ind.get('adx') is not None: add_info.append(f"ADX:{ind['adx']:.1f}")
                        if ind.get('cci') is not None: add_info.append(f"CCI:{ind['cci']:.1f}")
                        if add_info:
                            reason_display += f" [{', '.join(add_info)}]"
                except: pass

            # [수정] 사유 내 강제 줄바꿈 제거 (2줄 내 유동적 출력 지원)
            reason_display = reason_display.replace('\n', ' ')

            table.add_row(
                t['time'][5:19], # MM-DD HH:MM:SS
                t['odno'],
                type_str,
                status_str,
                f"{t['name']}\n({t['code']})",
                f"{int(float(t['qty'])):,}",
                price_display,
                total_amt_display,
                profit_display,
                reason_display
            )
            
            # [추가] 5개마다 실선 추가
            if (i + 1) % 5 == 0 and (i + 1) < len(t_list):
                table.add_section()

        config.console.print(table)
        logger.debug("[HISTORY_DEBUG] 테이블 출력 완료")

def asset_management_menu():
    """자산 관리 메인 메뉴"""
    menu_items = [("1", "자산 조회", "Asset Inquiry"), ("2", "보유 잔고", "Holdings"), ("3", "거래 내역", "Trade History")]
    choice = utils.show_menu("자산 관리 (Asset Management)", menu_items, default_choice="2")
    
    menu_map = {"1": "자산 조회", "2": "보유 잔고", "3": "거래 내역"}
    if choice in menu_map:
        context.USER_ACTION_BREADCRUMB.append(f"[{choice}] {menu_map[choice]}")

    if choice.lower() == 'q': return False

    if choice == "1":
        logger.info("운영자 실행: " + " - ".join(context.USER_ACTION_BREADCRUMB))
        get_deposit_balance()
    elif choice == "2":
        logger.info("운영자 실행: " + " - ".join(context.USER_ACTION_BREADCRUMB))
        get_account_balance()
    elif choice == "3":
        if view_trade_history() is False: return False
