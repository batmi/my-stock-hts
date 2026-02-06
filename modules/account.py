# modules/account.py
from rich.table import Table
from rich.panel import Panel
from rich import box
from datetime import datetime
import time
import config
import api
import utils

# -----------------------------------------------------------
# [보조 함수 1] 금일 투자 손익 요약 조회
# -----------------------------------------------------------
def fetch_today_profit_summary():
    tr_id = utils.get_tr_id("domestic", "inquiry", "profit")
    url = f"{config.URL_BASE}/uapi/domestic-stock/v1/trading/inquire-period-profit"
    headers = utils.get_common_headers(tr_id)
    today = datetime.now().strftime("%Y%m%d")
    
    params = {
        "CANO": config.CANO, "ACNT_PRDT_CD": config.ACNT_PRDT_CD,
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
def fetch_today_history():
    tr_id = utils.get_tr_id("domestic", "inquiry", "history")
    url = f"{config.URL_BASE}/uapi/domestic-stock/v1/trading/inquire-daily-ccld"
    headers = utils.get_common_headers(tr_id)
    today = datetime.now().strftime("%Y%m%d")
    params = {"CANO": config.CANO, "ACNT_PRDT_CD": config.ACNT_PRDT_CD, "INQR_STRT_DT": today, "INQR_END_DT": today, "SLL_BUY_DVSN_CD": "00", "INQR_DVSN": "00", "PDNO": "", "CCLD_DVSN": "01", "ORD_GNO_BRNO": "", "ODNO": "", "INQR_DVSN_3": "00", "INQR_DVSN_1": "", "CTX_AREA_FK100": "", "CTX_AREA_NK100": ""}
    
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

def fetch_domestic_balance():
    """국내 주식 잔고 데이터를 조회하여 반환"""
    tr_id = utils.get_tr_id("domestic", "inquiry", "balance")
    url = f"{config.URL_BASE}/uapi/domestic-stock/v1/trading/inquire-balance"
    headers = utils.get_common_headers(tr_id)
    params = {"CANO": config.CANO, "ACNT_PRDT_CD": config.ACNT_PRDT_CD, "AFHR_FLPR_YN": "N", "OFL_YN": "N", "INQR_DVSN": "02", "UNPR_DVSN": "01", "FUND_STTL_ICLD_YN": "N", "FNCG_AMT_AUTO_RDPT_YN": "N", "PRCS_DVSN": "00", "CTX_AREA_FK100": "", "CTX_AREA_NK100": ""}
    
    holdings = []
    summary = None
    
    try:
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

def fetch_overseas_balance():
    """해외 주식 잔고 데이터를 조회하여 반환"""
    ovrs_tr_id = utils.get_tr_id("overseas", "inquiry", "balance")
    ovrs_url = f"{config.URL_BASE}/uapi/overseas-stock/v1/trading/inquire-balance"
    target_exchanges = ["NASD", "NYSE", "AMEX"]
    
    all_holdings = []
    
    for exc in target_exchanges:
        if config.IS_SIMULATION: time.sleep(0.3)
        headers = utils.get_common_headers(ovrs_tr_id)
        params = {"CANO": config.CANO, "ACNT_PRDT_CD": config.ACNT_PRDT_CD, "OVRS_EXCG_CD": exc, "TR_CRCY_CD": "USD", "CTX_AREA_FK100": "", "CTX_AREA_NK100": "", "CTX_AREA_FK200": "", "CTX_AREA_NK200": ""}
        
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

def get_account_balance():
    # [수정] ★★★ 핵심: 진입 시 강제 대기 (서버 세션 안정화) ★★★
    time.sleep(0.5)

    # ---------------------------
    # [국내 주식 잔고]
    # ---------------------------
    with config.console.status("[bold green]국내 잔고 조회 중...[/]"):
        output1, summary = fetch_domestic_balance()
        
        if output1:
            table = Table(title=f"\n[cyan][국내] 계좌 잔고 현황[/]", box=box.HORIZONTALS, show_header=True, header_style="dim", border_style="dim")
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
        all_overseas_holdings = fetch_overseas_balance()

    if not all_overseas_holdings:
        config.console.print("\n[yellow]해외 보유 종목이 없습니다.[/yellow]\n")
    else:
        table_ovrs = Table(title=f"\n[magenta][해외] 계좌 잔고 현황[/] ", box=box.HORIZONTALS, show_header=True, header_style="dim", border_style="dim")
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

def get_deposit_balance():
    # [수정] 여기도 안정성을 위해 진입 대기
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
            profit_data = fetch_today_profit_summary()
            summary_data['buy_today'] = profit_data['buy_amt']
            summary_data['sell_today'] = profit_data['sell_amt']
            summary_data['total_cost'] = profit_data['total_cost']
            summary_data['realized_pl'] = profit_data['realized_pl']
            
            if summary_data['buy_today'] == 0 and summary_data['sell_today'] == 0:
                 time.sleep(0.2)
                 backup_data = fetch_today_history()
                 if backup_data['buy_total'] > 0 or backup_data['sell_total'] > 0:
                     summary_data['buy_today'] = backup_data['buy_total']
                     summary_data['sell_today'] = backup_data['sell_total']
        except: pass

    # 2. 국내 주식 잔고 및 자산
    tr_id_balance = utils.get_tr_id("domestic", "inquiry", "balance")
    url_balance = f"{config.URL_BASE}/uapi/domestic-stock/v1/trading/inquire-balance"
    headers_balance = utils.get_common_headers(tr_id_balance)
    params_balance = {"CANO": config.CANO, "ACNT_PRDT_CD": config.ACNT_PRDT_CD, "AFHR_FLPR_YN": "N", "OFL_YN": "N", "INQR_DVSN": "02", "UNPR_DVSN": "01", "FUND_STTL_ICLD_YN": "N", "FNCG_AMT_AUTO_RDPT_YN": "N", "PRCS_DVSN": "00", "CTX_AREA_FK100": "", "CTX_AREA_NK100": ""}

    with config.console.status("[bold green]계좌 실시간 자산 평가 중...[/bold green]"):
        try:
            time.sleep(0.3)
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
            ovrs_holdings = fetch_overseas_balance()
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
        params = {"CANO": config.CANO, "ACNT_PRDT_CD": config.ACNT_PRDT_CD, "PDNO": "005930", "ORD_UNPR": "0", "ORD_DVSN": "01", "CMA_EVLU_AMT_ICLD_YN": "Y", "OVRS_ICLD_YN": "Y", "CRDT_TYPE": "00"}
        headers = utils.get_common_headers(tr_id)
        
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
        params = {"CANO": config.CANO, "ACNT_PRDT_CD": config.ACNT_PRDT_CD, "TR_CONT": "", "INQR_DVSN_1": "", "TR_CRCY_CD": "", "PDNO": "", "ORD_UNPR": "", "ORD_QTY": "", "ORD_DVSN": "00", "CMA_EVLU_AMT_ICLD_YN": "Y", "OVRS_ICLD_YN": "Y", "CTX_AREA_FK100": "", "CTX_AREA_NK100": "", "BSPR_BF_DT_APLY_YN": "N"}
        headers = utils.get_common_headers(tr_id)
        
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
        width=75,
        border_style="green"
    )

    config.console.print("\n")
    config.console.print(panel)
    config.console.print("\n")
