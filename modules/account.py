# modules/account.py
from rich.table import Table
from rich.panel import Panel
from rich import box
from datetime import datetime, timedelta
import os
import time
import config
import api
import utils
from modules import db_manager
import json
import pandas as pd

# -----------------------------------------------------------
# [내부 헬퍼] 계좌별 헤더 생성 (자동매매 계좌 키 적용)
# -----------------------------------------------------------
def _get_headers(tr_id, target_cano):
    # 컨텍스트 설정 (토큰 발급용)
    is_auto = (not config.IS_SIMULATION and target_cano == config.AUTO_CANO)
    
    # [수정] 메인 계좌와 자동매매 계좌가 동일한 경우, 메인(Manual) 컨텍스트를 우선 사용
    # (불필요한 자동매매 토큰 발급 로그 방지)
    if config.CANO and config.AUTO_CANO and config.CANO == config.AUTO_CANO:
        is_auto = False

    config.trade_context.use_auto_account = is_auto
    
    headers = utils.get_common_headers(tr_id)
    
    # AppKey/Secret 오버라이드 (utils가 메인 키를 사용할 수 있으므로)
    if is_auto and config.AUTO_APP_KEY:
        headers["appKey"] = config.AUTO_APP_KEY
        headers["appSecret"] = config.AUTO_APP_SECRET
        
    return headers

# -----------------------------------------------------------
# [보조 함수 1] 금일 투자 손익 요약 조회
# -----------------------------------------------------------
def fetch_today_profit_summary(cano=None, acnt_prdt_cd=None):
    if not cano: cano = config.CANO
    if not acnt_prdt_cd: acnt_prdt_cd = config.ACNT_PRDT_CD

    tr_id = utils.get_tr_id("domestic", "inquiry", "profit")
    url = f"{config.URL_BASE}/uapi/domestic-stock/v1/trading/inquire-period-profit"
    headers = _get_headers(tr_id, cano)
    today = datetime.now().strftime("%Y%m%d")
    
    params = {
        "CANO": cano, "ACNT_PRDT_CD": acnt_prdt_cd,
        "INQR_STRT_DT": today, "INQR_END_DT": today,
        "SLL_BUY_DVSN_CD": "00", "INQR_DVSN": "00", 
        "PDNO": "", "CTX_AREA_FK100": "", "CTX_AREA_NK100": "",
        "AFHR_FLPR_YN": "N", "OFL_YN": "N", "UNPR_DVSN": "01",          
        "FUND_STTL_ICLD_YN": "N", "FNCG_AMT_AUTO_RDPT_YN": "N", "PRCS_DVSN": "00",
        "COST_ICLD_YN": "Y" 
    }
    
    summary = {'buy_amt': 0, 'sell_amt': 0, 'total_cost': 0, 'realized_pl': 0}

    for retry in range(2):
        try:
            res = api.session.get(url, headers=headers, params=params, verify=False)
            data = res.json()
            if data['rt_cd'] == '0':
                out2 = data.get('output2')
                if isinstance(out2, list) and len(out2) > 0:
                    summary_data = out2[0]
                    summary['buy_amt'] = api.safe_int(summary_data.get('thdt_buy_amt'))
                    summary['sell_amt'] = api.safe_int(summary_data.get('thdt_sll_amt'))
                    summary['total_cost'] = api.safe_int(summary_data.get('thdt_tlex_amt'))
                    summary['realized_pl'] = api.safe_int(summary_data.get('rlzt_pfls'))
                return summary
            elif data.get('msg_cd') == 'EGW00201': 
                time.sleep(0.3); continue
            else: break 
        except: time.sleep(0.2); continue
    return summary

# -----------------------------------------------------------
# [보조 함수 2] 금일 체결(매매) 내역 조회 (백업용)
# -----------------------------------------------------------
def fetch_today_history(cano=None, acnt_prdt_cd=None):
    if not cano: cano = config.CANO
    if not acnt_prdt_cd: acnt_prdt_cd = config.ACNT_PRDT_CD

    tr_id = utils.get_tr_id("domestic", "inquiry", "history")
    url = f"{config.URL_BASE}/uapi/domestic-stock/v1/trading/inquire-daily-ccld"
    headers = _get_headers(tr_id, cano)
    today = datetime.now().strftime("%Y%m%d")
    params = {"CANO": cano, "ACNT_PRDT_CD": acnt_prdt_cd, "INQR_STRT_DT": today, "INQR_END_DT": today, "SLL_BUY_DVSN_CD": "00", "INQR_DVSN": "00", "PDNO": "", "CCLD_DVSN": "01", "ORD_GNO_BRNO": "", "ODNO": "", "INQR_DVSN_3": "00", "INQR_DVSN_1": "", "CTX_AREA_FK100": "", "CTX_AREA_NK100": ""}
    
    summary = {'buy_total': 0, 'sell_total': 0}
    try:
        res = api.session.get(url, headers=headers, params=params, verify=False)
        data = res.json()
        if data['rt_cd'] == '0':
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
    if not cano: cano = config.CANO
    if not acnt_prdt_cd: acnt_prdt_cd = config.ACNT_PRDT_CD

    tr_id = utils.get_tr_id("domestic", "inquiry", "balance")
    url = f"{config.URL_BASE}/uapi/domestic-stock/v1/trading/inquire-balance"
    headers = _get_headers(tr_id, cano)
    params = {"CANO": cano, "ACNT_PRDT_CD": acnt_prdt_cd, "AFHR_FLPR_YN": "N", "OFL_YN": "N", "INQR_DVSN": "02", "UNPR_DVSN": "01", "FUND_STTL_ICLD_YN": "N", "FNCG_AMT_AUTO_RDPT_YN": "N", "PRCS_DVSN": "00", "CTX_AREA_FK100": "", "CTX_AREA_NK100": ""}
    
    holdings = []
    summary = None
    
    try:
        res = api.session.get(url, headers=headers, params=params, verify=False, timeout=10)
        data = res.json()
        
        # [추가] OPSQ2001 에러 시 INQR_DVSN='01'로 재시도
        if data.get('msg_cd') == 'OPSQ2001':
            params['INQR_DVSN'] = '01'
            res = api.session.get(url, headers=headers, params=params, verify=False, timeout=10)
            data = res.json()
            
        if data['rt_cd'] == '0':
            for item in data.get('output1', []):
                qty = int(item['hldg_qty'])
                if qty > 0:
                    holdings.append(item)
            if data.get('output2'):
                summary = data['output2'][0]
        else:
            config.console.print(f"[red]국내 잔고 조회 실패: {data.get('msg1')}[/red]")
    except Exception as e:
        config.console.print(f"[red]국내 잔고 조회 실패: {e}[/red]")
        
    return holdings, summary

def fetch_overseas_balance(cano=None, acnt_prdt_cd=None):
    """해외 주식 잔고 데이터를 조회하여 반환"""
    if not cano: cano = config.CANO
    if not acnt_prdt_cd: acnt_prdt_cd = config.ACNT_PRDT_CD

    ovrs_tr_id = utils.get_tr_id("overseas", "inquiry", "balance")
    ovrs_url = f"{config.URL_BASE}/uapi/overseas-stock/v1/trading/inquire-balance"
    target_exchanges = ["NASD", "NYSE", "AMEX"]
    
    all_holdings = []
    
    for exc in target_exchanges:
        if config.IS_SIMULATION: time.sleep(0.3)
        headers = _get_headers(ovrs_tr_id, cano)
        params = {"CANO": cano, "ACNT_PRDT_CD": acnt_prdt_cd, "OVRS_EXCG_CD": exc, "TR_CRCY_CD": "USD", "CTX_AREA_FK100": "", "CTX_AREA_NK100": "", "CTX_AREA_FK200": "", "CTX_AREA_NK200": ""}
        
        if config.DEBUG_LEVEL == "TRACE":
            config.console.print(f"[dim cyan][TRACE] REQ (KIS:OvrsBal) | Exch: {exc}[/dim cyan]")
        elif config.DEBUG_LEVEL == "DEBUG":
            config.console.print(f"[dim cyan][DEBUG] REQ (KIS:OvrsBal) | URL: {ovrs_url} | Params: {params}[/dim cyan]")

        for retry in range(3):
            try:
                res = api.session.get(ovrs_url, headers=headers, params=params, verify=False, timeout=10)
                data = res.json()
                
                if config.DEBUG_LEVEL == "TRACE":
                    config.console.print(f"[dim magenta][TRACE] RES (KIS:OvrsBal) | RT_CD: {data.get('rt_cd')} | Msg: {data.get('msg1')}[/dim magenta]")
                elif config.DEBUG_LEVEL == "DEBUG":
                    config.console.print(f"[dim magenta][DEBUG] RES (KIS:OvrsBal) | Body: {data}[/dim magenta]")

                if data['rt_cd'] == '0':
                    output1 = data.get('output1', [])
                    if output1:
                        for item in output1:
                            if '_exchange' not in item: item['_exchange'] = exc
                            all_holdings.append(item)
                    break
                elif data.get('msg_cd') == 'EGW00201':
                    time.sleep(0.5); continue
                else: break
            except: time.sleep(0.3); continue
            
    return all_holdings

def sync_today_trades():
    """금일 체결 내역을 API로 조회하여 DB의 단가(시장가=0) 정보를 업데이트 (모든 계좌 대상)"""
    tr_id = utils.get_tr_id("domestic", "inquiry", "history")
    today = datetime.now().strftime("%Y%m%d")
    
    # 조회 대상 계좌 목록
    accounts = []
    if config.CANO and config.ACNT_PRDT_CD:
        accounts.append({"cano": config.CANO, "acnt": config.ACNT_PRDT_CD, "type": "MAIN"})
    
    if not config.IS_SIMULATION and config.AUTO_CANO and config.AUTO_ACNT_PRDT_CD:
        if config.AUTO_CANO != config.CANO or config.AUTO_ACNT_PRDT_CD != config.ACNT_PRDT_CD:
            accounts.append({"cano": config.AUTO_CANO, "acnt": config.AUTO_ACNT_PRDT_CD, "type": "AUTO"})
            
    total_count = 0
    original_context = getattr(config.trade_context, 'use_auto_account', False)
    
    try:
        for acc in accounts:
            cano = acc['cano']
            acnt = acc['acnt']
            
            # 헤더 생성 (자동매매 계좌인 경우 해당 키 사용)
            headers = _get_headers(tr_id, cano)
            
            params = {
                "CANO": cano, "ACNT_PRDT_CD": acnt, 
                "INQR_STRT_DT": today, "INQR_END_DT": today, 
                "SLL_BUY_DVSN_CD": "00", "INQR_DVSN": "00", 
                "PDNO": "", "CCLD_DVSN": "01", # 주문별 체결 합계
                "ORD_GNO_BRNO": "", "ODNO": "", "INQR_DVSN_3": "00", 
                "INQR_DVSN_1": "", "CTX_AREA_FK100": "", "CTX_AREA_NK100": ""
            }
            
            try:
                url = f"{config.URL_BASE}/uapi/domestic-stock/v1/trading/inquire-daily-ccld"
                res = api.session.get(url, headers=headers, params=params, verify=False, timeout=5)
                data = res.json()
                
                if data['rt_cd'] == '0':
                    trades = data.get('output1', [])
                    for item in trades:
                        odno = item.get('odno')
                        avg_price = float(item.get('avg_prvs', 0))
                        tot_qty = int(item.get('tot_ccld_qty', 0))
                        
                        if odno and avg_price > 0:
                            # [수정] 체결 내역 분리 저장 (기존 내역 업데이트 대신 신규 추가)
                            if not db_manager.db.check_trade_exists(odno, "체결"):
                                if config.DEBUG_LEVEL == "DEBUG":
                                    config.console.print(f"[dim cyan][Account] 신규 체결 DB 저장 시도: {odno}[/dim cyan]")
                                
                                # 체결 시간 포맷팅
                                ord_dt = item.get('ord_dt', '')
                                ord_tmd = item.get('ord_tmd', '')
                                trade_time = None
                                if len(ord_dt) == 8 and len(ord_tmd) == 6:
                                    trade_time = f"{ord_dt[:4]}-{ord_dt[4:6]}-{ord_dt[6:]} {ord_tmd[:2]}:{ord_tmd[2:4]}:{ord_tmd[4:]}"
                                
                                type_cd = item.get('sll_buy_dvsn_cd')
                                type_str = "매수" if type_cd == '02' else ("매도" if type_cd == '01' else "기타")
                                
                                # 원 주문 유형 조회 (수동/자동 태그 반영)
                                origin_type = db_manager.db.get_original_order_type(odno)
                                if origin_type:
                                    if "(수동)" in origin_type: type_str += "(수동)"
                                    elif "(AUTO)" in origin_type or "(자동)" in origin_type: type_str += "(자동)"
                                
                                db_manager.db.insert_trade(
                                    type_str, item.get('pdno'), item.get('prdt_name'), 
                                    tot_qty, avg_price, odno, 
                                    order_status="체결", custom_time=trade_time,
                                    reason="체결 확인"
                                )
                                # [추가] 시장가 주문 등의 경우를 위해 원 주문(접수)의 단가도 체결가로 업데이트
                                db_manager.db.update_trade(odno, price=avg_price)
                                
                                total_count += 1
                            else:
                                if config.DEBUG_LEVEL == "DEBUG":
                                    config.console.print(f"[dim yellow][Account] 이미 존재하는 체결 내역입니다. 저장 스킵 (ODNO: {odno})[/dim yellow]")
            except: pass
    finally:
        config.trade_context.use_auto_account = original_context
        
    return total_count

def _display_balance_details(cano, acnt_prdt_cd):
    """특정 계좌의 잔고 상세 출력"""
    # ---------------------------
    # [국내 주식 잔고]
    # ---------------------------
    with config.console.status("[bold green]국내 잔고 조회 중...[/]"):
        output1, summary = fetch_domestic_balance(cano, acnt_prdt_cd)
        
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
            
            calculated_total_pchs = 0
            for item in output1:
                name = f"{item['prdt_name']} ({item['pdno']})"
                qty = int(item['hldg_qty'])
                buy_price = float(item['pchs_avg_pric'])
                cur_price = int(item['prpr'])
                eval_amt = int(item['evlu_amt'])
                profit = int(item['evlu_pfls_amt'])
                rate = float(item['evlu_pfls_rt'])
                pchs_amt = int(qty * buy_price)
                calculated_total_pchs += pchs_amt
                
                p_color = "[red]" if rate > 0 else ("[blue]" if rate < 0 else "[white]")
                table.add_row(
                    name,
                    f"{qty:,}주",
                    f"{buy_price:,.0f}원",
                    f"{cur_price:,}원",
                    f"{pchs_amt:,}원",
                    f"{eval_amt:,}원",
                    f"{p_color}{profit:+,}원[/]",
                    f"{p_color}{rate:.2f}%[/]"
                )
            
            config.console.print(table)
            
            # 요약 정보 출력
            if summary:
                tot_evlu = api.safe_int(summary.get('scts_evlu_amt'))
                tot_profit = api.safe_int(summary.get('evlu_pfls_smtl_amt'))
                api_tot_pchs = api.safe_int(summary.get('pchs_amt_smtl'))
                
                # [보정] API 응답의 총 매입금액이 0인 경우, 개별 종목 합계로 대체
                if api_tot_pchs == 0 and calculated_total_pchs > 0:
                    api_tot_pchs = calculated_total_pchs

                total_rate = 0.0
                if api_tot_pchs > 0:
                    total_rate = (tot_profit / api_tot_pchs) * 100
                
                profit_color = "[red]" if tot_profit > 0 else ("[blue]" if tot_profit < 0 else "[white]")
                config.console.print(f"[bold]  국내 총 평가금액:[/bold] {tot_evlu:,}원  |  [bold]총 평가손익:[/bold] {profit_color}{tot_profit:+,}원 ({total_rate:+.2f}%)[/]")
        else:
            config.console.print("\n[yellow]국내 보유 종목이 없습니다.[/yellow]\n")

    config.console.print("\n")

    # ---------------------------
    # [해외 주식 잔고]
    # ---------------------------
    with config.console.status("[bold green]해외 잔고 조회 중...[/]"):
        all_overseas_holdings = fetch_overseas_balance(cano, acnt_prdt_cd)

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

        tot_ovrs_evlu = 0.0
        tot_ovrs_profit = 0.0
        tot_ovrs_pchs = 0.0
        has_ovrs_item = False

        for item in all_overseas_holdings:
            qty = float(item.get('ovrs_cblc_qty', 0) or item.get('ord_psbl_qty', 0))
            
            if qty > 0:
                has_ovrs_item = True
                code = item.get('ovrs_pdno', '-')
                name = item.get('ovrs_item_name', '-')
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
                    f"{color}{rate:+.2f}[/]"
                )

        if has_ovrs_item:
            config.console.print(table_ovrs)
            total_ovrs_rate = 0.0
            if tot_ovrs_pchs > 0:
                total_ovrs_rate = (tot_ovrs_profit / tot_ovrs_pchs) * 100
                
            profit_color = "[red]" if tot_ovrs_profit > 0 else ("[blue]" if tot_ovrs_profit < 0 else "[white]")
            config.console.print(f"[bold]  해외 총 평가금액:[/bold] ${tot_ovrs_evlu:,.2f}  |  [bold]총 평가손익:[/bold] {profit_color}${tot_ovrs_profit:+,.2f} ({total_ovrs_rate:+.2f}%)[/]")
        else:
            config.console.print("[yellow]해외 보유 종목이 없습니다 (수량 0).[/yellow]")

def get_account_balance():
    """보유 잔고 조회 (메인/자동 계좌 순차 조회)"""
    time.sleep(0.5)
    
    accounts = []
    if config.IS_SIMULATION:
        accounts.append((config.CANO, config.ACNT_PRDT_CD, "모의투자"))
    else:
        accounts.append((config.CANO, config.ACNT_PRDT_CD, "실전투자 (수동)"))
        if config.AUTO_CANO and config.AUTO_ACNT_PRDT_CD and \
           (config.AUTO_CANO != config.CANO or config.AUTO_ACNT_PRDT_CD != config.ACNT_PRDT_CD):
            accounts.append((config.AUTO_CANO, config.AUTO_ACNT_PRDT_CD, "실전투자 (자동)"))
    
    for cano, acnt, label in accounts:
        config.console.print(f"\n[bold cyan]➤ {label} 계좌 잔고 ({cano}-{acnt})[/]")
        _display_balance_details(cano, acnt)

def _display_asset_status(cano, acnt_prdt_cd):
    """특정 계좌의 자산 현황 출력"""
    time.sleep(0.5)
    
    if config.DEBUG_LEVEL in ["TRACE", "DEBUG"]:
        config.console.print("[dim cyan][TRACE] get_deposit_balance() 호출[/dim cyan]")
    
    summary_data = {
        "withdraw": 0,      "tot_asset": 0,
        "dep_dom": 0,       "dep_ovs": 0,
        "d1_dep": 0,        "d2_dep": 0,
        "sec_buy": 0,       "sec_eval": 0,      "sec_pl": 0,
        "realized_pl": 0,   "total_cost": 0,
        "buy_today": 0,     "sell_today": 0,
        "ovrs_eval_krw": 0,
        "ovrs_pl_krw": 0
    }
    
    # 1. 금일 데이터 조회
    with config.console.status("[bold green]금일 투자/매매 내역 조회 중...[/bold green]"):
        try:
            profit_data = fetch_today_profit_summary(cano, acnt_prdt_cd)
            summary_data['buy_today'] = profit_data['buy_amt']
            summary_data['sell_today'] = profit_data['sell_amt']
            summary_data['total_cost'] = profit_data['total_cost']
            summary_data['realized_pl'] = profit_data['realized_pl']
            
            if summary_data['buy_today'] == 0 and summary_data['sell_today'] == 0:
                 time.sleep(0.2)
                 backup_data = fetch_today_history(cano, acnt_prdt_cd)
                 if backup_data['buy_total'] > 0 or backup_data['sell_total'] > 0:
                     summary_data['buy_today'] = backup_data['buy_total']
                     summary_data['sell_today'] = backup_data['sell_total']
        except: pass

    # 2. 국내 주식 잔고 및 자산
    tr_id_balance = utils.get_tr_id("domestic", "inquiry", "balance")
    url_balance = f"{config.URL_BASE}/uapi/domestic-stock/v1/trading/inquire-balance"
    headers_balance = _get_headers(tr_id_balance, cano)
    params_balance = {"CANO": cano, "ACNT_PRDT_CD": acnt_prdt_cd, "AFHR_FLPR_YN": "N", "OFL_YN": "N", "INQR_DVSN": "02", "UNPR_DVSN": "01", "FUND_STTL_ICLD_YN": "N", "FNCG_AMT_AUTO_RDPT_YN": "N", "PRCS_DVSN": "00", "CTX_AREA_FK100": "", "CTX_AREA_NK100": ""}

    with config.console.status("[bold green]계좌 실시간 자산 평가 중...[/bold green]"):
        try:
            time.sleep(0.3)
            res = api.session.get(url_balance, headers=headers_balance, params=params_balance, verify=False, timeout=10)
            data = res.json()
            
            # [추가] OPSQ2001 에러 시 INQR_DVSN='01'로 재시도
            if data.get('msg_cd') == 'OPSQ2001':
                params_balance['INQR_DVSN'] = '01'
                res = api.session.get(url_balance, headers=headers_balance, params=params_balance, verify=False, timeout=10)
                data = res.json()
            
            if data['rt_cd'] == '0':
                holdings = data.get('output1', [])
                calc_buy = 0; calc_eval = 0; calc_pl = 0
                for item in holdings:
                    calc_buy += api.safe_int(item.get('pchs_amt'))
                    calc_eval += api.safe_int(item.get('evlu_amt'))
                    calc_pl += api.safe_int(item.get('evlu_pfls_amt'))
                summary_data['sec_buy'] = calc_buy
                summary_data['sec_eval'] = calc_eval
                summary_data['sec_pl'] = calc_pl

                if data.get('output2'):
                    summary = data['output2'][0]
                    if not config.IS_SIMULATION:
                        summary_data['d1_dep'] = api.safe_int(summary.get('nxdy_excc_amt'))
                        summary_data['d2_dep'] = api.safe_int(summary.get('prvs_rcdl_excc_amt'))
                        summary_data['dep_dom'] = api.safe_int(summary.get('dnca_tot_amt'))
                        summary_data['withdraw'] = summary_data['d2_dep'] 
            else:
                config.console.print(f"[bold red]자산 현황 조회 실패: {data.get('msg1')} (Code: {data.get('msg_cd')})[/bold red]")
                pass 

        except Exception as e:
            config.console.print(f"[bold red]자산 현황 조회 오류: {str(e)}[/bold red]")
            pass # [수정] 패스
            
        # [추가] 해외 주식 잔고 합산 (원화 환산)
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
            
            if config.DEBUG_LEVEL == "DEBUG":
                config.console.print(f"[dim magenta][DEBUG] CALC (Ovrs->KRW) | USD: Buy={ovrs_buy_usd:.2f}, Eval={ovrs_eval_usd:.2f}, PL={ovrs_pl_usd:.2f} | Rate: {exchange_rate} | KRW: Eval={ovrs_eval_krw}, PL={ovrs_pl_krw}[/dim magenta]")
            
            summary_data['sec_buy'] += int(ovrs_buy_usd * exchange_rate)
            summary_data['sec_eval'] += ovrs_eval_krw
            summary_data['sec_pl'] += ovrs_pl_krw
        except Exception as e:
            pass

    # 3. 예수금 조회
    tr_id = utils.get_tr_id("domestic", "inquiry", "deposit")
    if config.IS_SIMULATION:
        url = f"{config.URL_BASE}/uapi/domestic-stock/v1/trading/inquire-psbl-order"
        params = {"CANO": cano, "ACNT_PRDT_CD": acnt_prdt_cd, "PDNO": "005930", "ORD_UNPR": "0", "ORD_DVSN": "01", "CMA_EVLU_AMT_ICLD_YN": "Y", "OVRS_ICLD_YN": "Y", "CRDT_TYPE": "00"}
        headers = _get_headers(tr_id, cano)
        
        try:
            time.sleep(0.3)
            res = api.session.get(url, headers=headers, params=params, verify=False, timeout=10)
            data = res.json()
            if data['rt_cd'] == '0':
                output = data.get('output', {})
                cash_amt = api.safe_int(output.get('ord_psbl_cash'))
                summary_data['dep_dom'] = cash_amt
                summary_data['d2_dep'] = cash_amt
                summary_data['withdraw'] = cash_amt
            else:
                config.console.print(f"[red]예수금 조회 실패: {data.get('msg1')}[/red]")
                return
        except:
            config.console.print("[red]예수금 조회 중 통신 오류[/red]")
            return
    else:
        # 실전투자: 외화 예수금 조회 포함
        url = f"{config.URL_BASE}/uapi/domestic-stock/v1/trading/inquire-account-balance"
        params = {"CANO": cano, "ACNT_PRDT_CD": acnt_prdt_cd, "TR_CONT": "", "INQR_DVSN_1": "", "TR_CRCY_CD": "", "PDNO": "", "ORD_UNPR": "", "ORD_QTY": "", "ORD_DVSN": "00", "CMA_EVLU_AMT_ICLD_YN": "Y", "OVRS_ICLD_YN": "Y", "CTX_AREA_FK100": "", "CTX_AREA_NK100": "", "BSPR_BF_DT_APLY_YN": "N"}
        headers = _get_headers(tr_id, cano)
        
        try:
            time.sleep(0.3)
            res = api.session.get(url, headers=headers, params=params, verify=False, timeout=10)
            data = res.json()
            if data['rt_cd'] == '0' and data.get('output2'):
                out2 = data['output2'][0] if isinstance(data['output2'], list) else data['output2']
                summary_data['dep_ovs'] = int(float(out2.get('frcr_evlu_tota', 0)))
        except: pass

    # 4. 출력
    real_cash = summary_data['d2_dep']
    summary_data['tot_asset'] = real_cash + summary_data['dep_ovs'] + summary_data['sec_eval']
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
    if not config.IS_SIMULATION:
        summary_table.add_row("      └ D+1 (익일)", f"{summary_data['d1_dep']:,}원", style="dim")
        summary_table.add_row("      └ D+2 (가수도)", f"{summary_data['d2_dep']:,}원", style="dim")
    summary_table.add_row("    외화예수금", f"{summary_data['dep_ovs']:,}원", style="dim")
    summary_table.add_row("출금가능금액", f"{summary_data['withdraw']:,}원")
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
    
    summary_table.add_row("금일 매수 체결합계", f"{summary_data['buy_today']:,}원")
    summary_table.add_row("금일 매도 체결합계", f"{summary_data['sell_today']:,}원")
    summary_table.add_row("금일 제비용", f"{summary_data['total_cost']:,}원")
    summary_table.add_row("금일 실현손익 (확정)", f"[{get_color(summary_data['realized_pl'])}]{summary_data['realized_pl']:,}원[/]")

    panel = Panel(
        summary_table,
        title="계좌 자산 현황 요약",
        subtitle=f"[dim]업데이트: {datetime.now().strftime('%H:%M:%S')}[/]",
        subtitle_align="right",
        width=70,
        border_style="green"
    )

    config.console.print("\n")
    config.console.print(panel)
    config.console.print("\n")

def get_deposit_balance():
    """자산 현황 조회 (메인/자동 계좌 순차 조회)"""
    time.sleep(0.5)
    
    accounts = []
    if config.IS_SIMULATION:
        accounts.append((config.CANO, config.ACNT_PRDT_CD, "모의투자"))
    else:
        accounts.append((config.CANO, config.ACNT_PRDT_CD, "실전투자 (수동)"))
        if config.AUTO_CANO and config.AUTO_ACNT_PRDT_CD and \
           (config.AUTO_CANO != config.CANO or config.AUTO_ACNT_PRDT_CD != config.ACNT_PRDT_CD):
            accounts.append((config.AUTO_CANO, config.AUTO_ACNT_PRDT_CD, "실전투자 (자동)"))
            
    for cano, acnt, label in accounts:
        config.console.print(f"\n[bold cyan]➤ {label} 자산 현황 ({cano}-{acnt})[/]")
        _display_asset_status(cano, acnt)
        
def export_trade_history_to_excel():
    """전체 거래 내역을 엑셀 파일로 저장"""
    try:
        trades = db_manager.db.get_trades(limit=None)
        if not trades:
            config.console.print("\n[yellow]저장할 거래 내역이 없습니다.[/yellow]")
            return

        # DataFrame 생성
        df = pd.DataFrame(trades)
        
        # 컬럼 순서 및 이름 변경 (사용자 친화적)
        columns_map = {
            'time': '일시',
            'type': '유형',
            'name': '종목명',
            'code': '종목코드',
            'qty': '수량',
            'price': '단가',
            'profit_amt': '손익금',
            'profit_rate': '수익률(%)',
            'reason': '매매사유',
            'strategy_score': '점수',
            'order_status': '상태',
            'odno': '주문번호',
            'account': '계좌번호',
            'is_sim': '모의투자여부'
        }
        
        # 존재하는 컬럼만 선택하여 순서대로 정렬 (없는 컬럼은 제외)
        target_cols = [c for c in columns_map.keys() if c in df.columns]
        df = df[target_cols]
        df.rename(columns=columns_map, inplace=True)
        
        # 모의투자여부 가독성 좋게 변경
        if '모의투자여부' in df.columns:
            df['모의투자여부'] = df['모의투자여부'].apply(lambda x: '모의' if x == 1 else '실전')

        # 파일명 생성
        timestamp = datetime.now().strftime('%Y%m%d_%H%M%S')
        filename_xlsx = os.path.join(config.DATA_DIR, f"trade_history_{timestamp}.xlsx")
        
        # 엑셀 저장
        try:
            with config.console.status(f"[bold green]'{os.path.basename(filename_xlsx)}' 파일로 저장 중...[/]"):
                df.to_excel(filename_xlsx, index=False)
            config.console.print(f"\n[bold green]성공적으로 저장되었습니다: {os.path.basename(filename_xlsx)}[/bold green]")
        except ImportError:
            config.console.print("\n[yellow]openpyxl 라이브러리가 설치되지 않아 엑셀(.xlsx) 저장이 불가능합니다.[/yellow]")
            if config.Prompt.ask("대신 CSV 파일로 저장하시겠습니까?", choices=["y", "n"], default="y") == "y":
                filename_csv = os.path.join(config.DATA_DIR, f"trade_history_{timestamp}.csv")
                df.to_csv(filename_csv, index=False, encoding='utf-8-sig')
                config.console.print(f"\n[bold green]성공적으로 저장되었습니다: {os.path.basename(filename_csv)}[/bold green]")
            else:
                config.console.print("[dim]저장을 취소했습니다. (터미널에서 'pip install openpyxl'을 실행하세요)[/dim]")

    except Exception as e:
        config.console.print(f"\n[bold red]저장 실패: {e}[/bold red]")

def view_trade_history():
    """DB에 저장된 거래 내역 조회"""
    config.console.print("\n[bold]거래 내역 조회 옵션:[/bold]")
    config.console.print("[1] 전체 내역 (최신순 50건)")
    config.console.print("[2] 최근 30일 내역")
    config.console.print("[3] 종목코드(티커) 검색")
    config.console.print("[4] 전체 거래 내역 저장 (Excel)")
    config.console.print()
    
    choice = config.Prompt.ask("선택 [dim](취소: q)[/dim]", choices=["1", "2", "3", "4", "q"], default="1")
    if choice.lower() == 'q': return

    # [추가] 조회 전 금일 체결 내역 동기화 (시장가 주문 단가 업데이트)
    with config.console.status("[bold green]최신 체결 내역 동기화 중...[/]"):
        sync_today_trades()

    trades = []
    if choice == "1":
        trades = db_manager.db.get_trades(limit=50)
    elif choice == "2":
        start_dt = (datetime.now() - timedelta(days=30)).strftime("%Y-%m-%d")
        trades = db_manager.db.get_trades(start_date=start_dt)
    elif choice == "3":
        keyword = config.Prompt.ask("검색할 종목코드(티커) 입력")
        trades = db_manager.db.get_trades(code=keyword)
    elif choice == "4":
        export_trade_history_to_excel()
        return

    if not trades:
        config.console.print("\n[yellow]검색된 거래 내역이 없습니다.[/yellow]")
        return

    # [수정] 데이터 분류 및 그룹핑 (계좌 종류/번호별 분리)
    grouped_trades = {} # (category, account) -> list
    
    for t in trades:
        # 1. 모드 필터링 (모의투자 모드면 모의내역만, 실전이면 실전/자동 내역만)
        is_sim_data = bool(t['is_sim'])
        if config.IS_SIMULATION and not is_sim_data: continue
        if not config.IS_SIMULATION and is_sim_data: continue
        
        # 2. 카테고리 결정
        category = "모의"
        if not is_sim_data:
            if "AUTO" in t['type']: category = "자동"
            else: category = "실전"
            
        # 3. 그룹핑
        key = (category, t['account'])
        if key not in grouped_trades:
            grouped_trades[key] = []
        grouped_trades[key].append(t)
        
    if not grouped_trades:
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

        # 테이블 생성 (제목에 계좌번호 포함)
        table_title = f"\n[{cat}] 거래 히스토리 (계좌: {acc}) - {len(t_list)}건"
        table = Table(title=table_title, box=box.HORIZONTALS, header_style="dim", border_style="dim")
        table.add_column("시간", justify="center", style="dim", width=15)
        # 계좌 컬럼 제거됨
        table.add_column("유형", justify="center", no_wrap=True)
        table.add_column("상태", justify="center", width=6)
        table.add_column("종목명(코드)", justify="left", no_wrap=True)
        table.add_column("수량", justify="right")
        table.add_column("단가", justify="right", no_wrap=True)
        table.add_column("손익(수익률)", justify="right", no_wrap=True)
        table.add_column("점수", justify="center")
        table.add_column("사유", justify="left", no_wrap=True, overflow="ellipsis")
        table.add_column("주문번호", justify="center", style="dim", no_wrap=True)

        for i, t in enumerate(t_list):
            type_str = t['type']
            
            # [수정] 유형 표기 한글화 및 태그 변경
            if "buy" in type_str.lower(): type_str = type_str.replace("buy", "매수").replace("BUY", "매수")
            if "sell" in type_str.lower(): type_str = type_str.replace("sell", "매도").replace("SELL", "매도")
            type_str = type_str.replace("AUTO", "자동")
            
            # [수정] 색상 적용 (매수/매도, 자동/수동 분리)
            if "매수" in type_str: type_str = type_str.replace("매수", "[red]매수[/]")
            elif "매도" in type_str: type_str = type_str.replace("매도", "[blue]매도[/]")
            
            if "자동" in type_str: type_str = type_str.replace("자동", "[yellow]자동[/]")
            if "수동" in type_str: type_str = type_str.replace("수동", "[green]수동[/]")

            # 상태 표시
            status_str = t.get('order_status', '접수') # 기본값 접수
            if status_str == "체결": status_str = "[green]체결[/]"
            else: status_str = f"[dim]{status_str}[/]"

            # 가격 포맷팅
            price_display = t['price']
            try:
                p_val = float(t['price'])
                if p_val > 0:
                    price_display = f"{int(p_val):,}" if p_val >= 1000 else f"{p_val:,.2f}"
                elif p_val == 0:
                    price_display = "시장가"
            except: pass
            
            # 손익 정보
            profit_display = "-"
            if "매도" in type_str:
                amt = t.get('profit_amt', 0)
                rate = t.get('profit_rate', 0.0)
                if amt is not None and rate is not None:
                    color = "red" if amt > 0 else ("blue" if amt < 0 else "white")
                    profit_display = f"[{color}]{amt:+,}원 ({rate:+.2f}%)[/]"

            # 점수
            score = t.get('strategy_score', 0)
            score_str = str(score) if score else "-"

            table.add_row(
                t['time'][5:], # MM-DD HH:MM:SS
                type_str,
                status_str,
                f"{t['name']}({t['code']})",
                f"{t['qty']}",
                price_display,
                profit_display,
                score_str,
                t.get('reason') or "-",
                t['odno']
            )
            
            # [추가] 5개마다 실선 추가
            if (i + 1) % 5 == 0 and (i + 1) < len(t_list):
                table.add_section()

        config.console.print(table)

def asset_management_menu():
    """자산 관리 메인 메뉴"""
    config.console.print("\n[bold]자산 관리[/bold]")
    config.console.print("[1] 자산 조회 (예수금/총자산)")
    config.console.print("[2] 보유 잔고 (종목별 상세)")
    config.console.print("[3] 거래 내역 (히스토리)")
    config.console.print()
    
    choice = config.Prompt.ask("선택 [dim](취소: q)[/dim]", choices=["1", "2", "3", "q"], default="2")
    if choice.lower() == 'q': return

    if choice == "1":
        get_deposit_balance()
    elif choice == "2":
        get_account_balance()
    elif choice == "3":
        view_trade_history()
