# modules/trading.py
from rich.prompt import Prompt
from rich.panel import Panel
from rich.table import Table
from rich import box
import json
import time
from datetime import datetime
import config
import api
import utils
from modules import account
from modules import db_manager
from modules import analysis

def select_stock_from_balance():
    """
    매도 시 보유 잔고에서 종목을 선택하는 함수
    (메뉴 4번 잔고 조회와 동일한 상세 정보를 출력)
    """
    config.console.print("\n[bold]어떤 시장의 보유 주식을 매도하시겠습니까?[/bold]")
    config.console.print("[1] 국내 주식 잔고")
    config.console.print("[2] 해외 주식 잔고")
    config.console.print()
    market_choice = Prompt.ask("선택 [dim](취소: q)[/dim]", choices=["1", "2", "q"], default="1")
    
    if market_choice == 'q':
        return None, None, False, None

    is_overseas = (market_choice == "2")
    candidates = []

    # ---------------------------
    # 1. 국내 잔고 조회
    # ---------------------------
    if not is_overseas:
        with config.console.status("[bold green]국내 잔고 조회 중...[/]"):
            holdings, _ = account.fetch_domestic_balance()
            for item in holdings:
                qty = int(item.get('hldg_qty', 0))
                buy_price = float(item.get('pchs_avg_pric', 0))
                cur_price = int(item.get('prpr', 0))
                eval_amt = int(item.get('evlu_amt', 0))
                profit = int(item.get('evlu_pfls_amt', 0))
                rate = float(item.get('evlu_pfls_rt', 0))
                pchs_amt = int(qty * buy_price)

                candidates.append({
                    'code': item['pdno'],
                    'name': item['prdt_name'],
                    'qty': qty,
                    'buy_price': buy_price,
                    'cur_price': cur_price,
                    'pchs_amt': pchs_amt,
                    'eval_amt': eval_amt,
                    'profit': profit,
                    'rate': rate
                })

    # ---------------------------
    # 2. 해외 잔고 조회
    # ---------------------------
    else:
        with config.console.status("[bold green]해외 잔고 조회 중...[/]"):
            holdings = account.fetch_overseas_balance()
            for item in holdings:
                qty = float(item.get('ovrs_cblc_qty', 0) or item.get('ord_psbl_qty', 0))
                if qty > 0:
                    pchs_avg = float(item.get('pchs_avg_pric', 0))
                    profit = float(item.get('frcr_evlu_pfls_amt', 0))
                    rate = float(item.get('evlu_pfls_rt', 0))
                    cur_price = float(item.get('ovrs_now_pric', 0))
                    
                    item_pchs = qty * pchs_avg
                    item_eval = item_pchs + profit
                    
                    if cur_price == 0 and qty > 0: cur_price = item_eval / qty

                    candidates.append({
                        'code': item['ovrs_pdno'],
                        'name': item['ovrs_item_name'],
                        'qty': qty,
                        'buy_price': pchs_avg,
                        'cur_price': cur_price,
                        'pchs_amt': item_pchs,
                        'eval_amt': item_eval,
                        'profit': profit,
                        'rate': rate,
                        'excd': item.get('_exchange', '')
                    })

    # ---------------------------
    # 3. 상세 리스트 출력 및 선택
    # ---------------------------
    if not candidates:
        config.console.print("\n[yellow]매도 가능한 잔고가 없습니다.[/yellow]")
        return None, None, False, None

    title = f"\n[{'해외' if is_overseas else '국내'}] 매도 가능 종목 리스트"
    table = Table(title=title, box=box.HORIZONTALS, show_header=True, header_style="dim", border_style="dim")
    
    table.add_column("No.", justify="center", style="cyan")
    table.add_column("종목명 (코드)", justify="left")
    if is_overseas: table.add_column("거래소", justify="center")
    table.add_column("보유수량", justify="right")
    table.add_column("매입단가", justify="right")
    table.add_column("현재가", justify="right")
    table.add_column("매입금액", justify="right")
    table.add_column("평가금액", justify="right")
    table.add_column("평가손익", justify="right")
    table.add_column("수익률", justify="right")

    for idx, item in enumerate(candidates):
        profit_color = "[red]" if item['profit'] > 0 else ("[blue]" if item['profit'] < 0 else "[white]")
        
        # 종목명과 코드를 한 줄에 표시
        name_display = f"{item['name']} ({item['code']})"
        
        if not is_overseas:
            qty_s = f"{item['qty']:,}주"
            buy_p_s = f"{item['buy_price']:,.0f}원"
            cur_p_s = f"{item['cur_price']:,}원"
            pchs_a_s = f"{item['pchs_amt']:,}원"
            eval_a_s = f"{item['eval_amt']:,}원"
            profit_s = f"{profit_color}{item['profit']:+,}원[/]"
            rate_s = f"{profit_color}{item['rate']:+.2f}%[/]"
        else:
            qty_s = f"{item['qty']:,.0f}"
            buy_p_s = f"{item['buy_price']:,.2f}"
            cur_p_s = f"{item['cur_price']:,.2f}"
            pchs_a_s = f"{item['pchs_amt']:,.2f}"
            eval_a_s = f"{item['eval_amt']:,.2f}"
            profit_s = f"{profit_color}{item['profit']:+,.2f}[/]"
            rate_s = f"{profit_color}{item['rate']:+.2f}%[/]"

        row_data = [str(idx + 1), name_display]
        if is_overseas: row_data.append(item['excd'])
        
        row_data.extend([qty_s, buy_p_s, cur_p_s, pchs_a_s, eval_a_s, profit_s, rate_s])
        table.add_row(*row_data)

    config.console.print(table)
    
    sel_idx = Prompt.ask("\n매도할 종목 번호를 입력하세요 [dim](취소: q)[/dim]")
    if sel_idx.lower() == 'q': return None, None, False, None

    try:
        idx = int(sel_idx) - 1
        if 0 <= idx < len(candidates):
            selected = candidates[idx]
            
            mapped_excd = None
            if is_overseas:
                ex_map = {"NASD": "NAS", "NYSE": "NYS", "AMEX": "AMS"}
                mapped_excd = ex_map.get(selected['excd'], "NAS")
                
            return selected['code'], selected['name'], is_overseas, mapped_excd
        else:
            config.console.print("[red]잘못된 번호입니다.[/red]")
            return None, None, False, None
    except ValueError:
        config.console.print("[red]숫자를 입력해주세요.[/red]")
        return None, None, False, None


# =========================================================================
# [공통] 미체결 내역 조회 및 출력 함수 (재사용)
# =========================================================================
def _get_domestic_open_orders():
    tr_id = utils.get_tr_id("domestic", "inquiry", "open_orders")
    if config.IS_SIMULATION:
        url = f"{config.URL_BASE}/uapi/domestic-stock/v1/trading/inquire-daily-ccld"
        dt_str = datetime.now().strftime("%Y%m%d")
        params = {
            "CANO": config.CANO, "ACNT_PRDT_CD": config.ACNT_PRDT_CD, 
            "INQR_STRT_DT": dt_str, "INQR_END_DT": dt_str,
            "SLL_BUY_DVSN_CD": "00", "INQR_DVSN": "00",
            "PDNO": "", "CCLD_DVSN": "02",
            "ORD_GNO_BRNO": "", "ODNO": "", "INQR_DVSN_3": "00", 
            "INQR_DVSN_1": "", "CTX_AREA_FK100": "", "CTX_AREA_NK100": ""
        }
    else:
        url = f"{config.URL_BASE}/uapi/domestic-stock/v1/trading/inquire-psbl-rvsecncl"
        params = {
            "CANO": config.CANO, "ACNT_PRDT_CD": config.ACNT_PRDT_CD, 
            "CTX_AREA_FK100": "", "CTX_AREA_NK100": "", 
            "INQR_DVSN_1": "0", "INQR_DVSN_2": "0"
        }

    headers = utils.get_common_headers(tr_id)
    try:
        res = api.session.get(url, headers=headers, params=params, verify=False)
        data = res.json()
        if data.get('rt_cd') == '0':
            return data.get('output1', []) if config.IS_SIMULATION else data.get('output', [])
    except: pass
    return []

def _get_us_open_orders():
    tr_id = utils.get_tr_id("overseas", "inquiry", "open_orders")
    url = f"{config.URL_BASE}/uapi/overseas-stock/v1/trading/inquire-nccs"
    headers = utils.get_common_headers(tr_id)
    
    all_orders = []
    target_exchanges = ["NASD", "NYSE", "AMEX"] if config.IS_SIMULATION else ["NASD"]
    
    for exc in target_exchanges:
        params = {
            "CANO": config.CANO, "ACNT_PRDT_CD": config.ACNT_PRDT_CD, 
            "OVRS_EXCG_CD": exc, "SORT_SQN": "DS", 
            "CTX_AREA_FK100": "", "CTX_AREA_NK100": "", "CTX_AREA_FK200": "", "CTX_AREA_NK200": ""
        }
        try:
            res = api.session.get(url, headers=headers, params=params, verify=False)
            data = res.json()
            if data.get('rt_cd') == '0':
                orders = data.get('output', [])
                if orders:
                    for o in orders:
                        if not o.get('ovrs_excg_cd'): o['ovrs_excg_cd'] = exc
                    all_orders.extend(orders)
        except: pass
    return all_orders

def show_open_orders():
    """미체결 내역을 조회하고 테이블로 출력하며, 선택 가능한 주문 리스트를 반환합니다."""
    with config.console.status("[bold green]미체결 내역 조회 중...[/]"):
        dom_orders = _get_domestic_open_orders()
        us_orders = _get_us_open_orders()
    
    if not dom_orders and not us_orders:
        config.console.print("\n[yellow]미체결 주문 내역이 없습니다.[/yellow]")
        return []

    table = Table(title="\n미체결 내역", box=box.HORIZONTALS, header_style="dim", border_style="dim")
    table.add_column("No", justify="right")
    table.add_column("국가", justify="center")
    table.add_column("주문시간", justify="center")
    table.add_column("주문번호")
    table.add_column("종목명(코드)")
    table.add_column("구분", justify="center")
    table.add_column("주문수량", justify="right")
    table.add_column("주문단가", justify="right")
    table.add_column("현재가", justify="right", style="bold")
    table.add_column("잔량", justify="right")

    selectable_orders = []
    
    # --- [A] 국내 주문 처리 ---
    for order in dom_orders:
        rmn_qty = order.get('rmn_qty') or order.get('psbl_qty', '0')
        if api.safe_int(rmn_qty) <= 0: continue
        
        order['_origin'] = 'KR'
        selectable_orders.append(order)
        idx = len(selectable_orders)
        
        sll_buy = order.get('sll_buy_dvsn_cd_name', '').strip()
        if not sll_buy:
            cd = order.get('sll_buy_dvsn_cd', '')
            sll_buy = "매도" if cd == '01' else ("매수" if cd == '02' else cd)
        
        sll_buy_colored = f"[red]{sll_buy}[/]" if "매수" in sll_buy else f"[blue]{sll_buy}[/]"
        
        cur_price_str = "-"
        if order.get('pdno'):
            cp_res = api.get_current_price_data(order.get('pdno'), False)
            if cp_res.get('rt_cd') == '0':
                cur_price_str = f"{api.safe_int(cp_res['output']['stck_prpr']):,}원"
        
        display_name = f"{order.get('prdt_name')} ({order.get('pdno')})"
        ord_tmd = order.get('ord_tmd', '')
        ord_time = f"{ord_tmd[:2]}:{ord_tmd[2:4]}:{ord_tmd[4:]}" if len(ord_tmd) == 6 else "-"

        table.add_row(str(idx), "[bold]국내[/]", ord_time, order.get('odno'), display_name, sll_buy_colored, order.get('ord_qty'), f"{api.safe_int(order.get('ord_unpr')):,.0f}", cur_price_str, rmn_qty)

    # --- [B] 해외 주문 처리 ---
    for order in us_orders:
        rmn_qty = order.get('nccs_qty', '0')
        if float(rmn_qty) <= 0: continue

        order['_origin'] = 'US'
        selectable_orders.append(order)
        idx = len(selectable_orders)

        sll_buy_code = order.get('sll_buy_dvsn_cd')
        sll_buy = "매수" if sll_buy_code == "02" else ("매도" if sll_buy_code == "01" else sll_buy_code)
        sll_buy_colored = f"[red]{sll_buy}[/]" if "매수" in sll_buy else f"[blue]{sll_buy}[/]"

        ord_unpr = 0.0
        for key in ['ft_ord_unpr3', 'ft_ord_unpr', 'ord_unpr', 'ord_init_unpr', 'ovrs_ord_unpr']:
            if order.get(key) and float(order.get(key)) > 0:
                ord_unpr = float(order.get(key))
                break
        
        cur_price_str = "-"
        if order.get('pdno'):
            cp_res = api.get_current_price_data(order.get('pdno'), True)
            if cp_res.get('rt_cd') == '0':
                cur_price_str = f"${float(cp_res['output'].get('last',0)):,.2f}"

        display_name = f"{order.get('prdt_name')} ({order.get('pdno')})"
        ord_dt = order.get('ord_dt', '')
        ord_tmd = order.get('ord_tmd', '')
        ord_time = "-"
        if len(ord_tmd) == 6:
            t_str = f"{ord_tmd[:2]}:{ord_tmd[2:4]}:{ord_tmd[4:]}"
            ord_time = f"{ord_dt[4:6]}/{ord_dt[6:]} {t_str}" if len(ord_dt) == 8 else t_str

        table.add_row(str(idx), "[bold magenta]해외[/]", ord_time, order.get('odno'), display_name, sll_buy_colored, order.get('ft_ord_qty', '0'), f"${ord_unpr:,.2f}", cur_price_str, rmn_qty)

    config.console.print(table)
    return selectable_orders


def send_order(order_type):
    # 1. 타이틀 출력
    title_color = 'red' if order_type == 'buy' else 'blue'
    title_text = "매수" if order_type == 'buy' else "매도"
    config.console.print(f"\n[bold {title_color}]=== 주식 {title_text.upper()} 주문 (현금 전용) ===[/]")
    config.console.print(f"주문 계좌: [bold]{config.CANO}-{config.ACNT_PRDT_CD}[/bold]")
    
    # 2. 종목 선택 (매수: 검색 / 매도: 잔고에서 선택)
    pre_selected_excd = None
    
    if order_type == 'sell':
        # 매도 시 상세 잔고 리스트에서 선택
        stock_code, stock_name, is_overseas, pre_selected_excd = select_stock_from_balance()
    else:
        # 매수 시 기존 검색 기능 사용
        stock_code, stock_name, is_overseas = utils.select_target_stock()
    
    if not stock_code: 
        config.console.print("[yellow]주문이 취소되었습니다.[/yellow]")
        return

    config.console.print(f"선택 종목: [bold cyan]{stock_name} ({stock_code})[/bold cyan]")

    # 4. 거래소 확인 (해외)
    excd = ""
    if is_overseas:
        if pre_selected_excd:
            excd = pre_selected_excd
        else:
            found_excd = api.find_best_exchange_code(stock_code)
            if found_excd:
                excd = found_excd
            else:
                config.console.print(f"[yellow]'{stock_code}'의 거래소 정보를 찾을 수 없습니다. 기본값(NAS)을 사용합니다.[/yellow]")
                excd = Prompt.ask("거래소 코드 수동 입력 (NAS/NYS/AMS)", default="NAS").upper()

    default_qty = "1"
    
    # [국내 주식]
    if not is_overseas:
        curr_price = 0
        cur_res = api.get_current_price_data(stock_code, is_overseas=False)
        if cur_res.get('rt_cd') == '0':
            curr_price = int(cur_res['output']['stck_prpr'])
            config.console.print(f"\n[bold green]현재가: {curr_price:,}원[/bold green]")

        if order_type == 'buy':
            if curr_price > 0:
                max_qty = api.fetch_buyable_quantity(stock_code, curr_price)
                if max_qty > 0:
                    default_qty = str(max_qty)
                    config.console.print(f"[green]최대 {max_qty}주 매수 가능 (예수금 기준)[/green]")
                else: config.console.print(f"[yellow]매수 가능 수량이 0입니다.[/yellow]")
        elif order_type == 'sell':
            max_qty = api.fetch_sellable_quantity(stock_code)
            if max_qty > 0:
                default_qty = str(max_qty)
                config.console.print(f"[blue]보유 잔고: {max_qty}주 매도 가능[/blue]")
            else: config.console.print("[yellow]매도 가능 수량이 0입니다.[/yellow]")

        qty = Prompt.ask(f"\n[{title_color}]{title_text} 수량(주)[/] [dim](취소: q)[/dim]", default=default_qty)
        if qty.lower() == 'q': return
        qty = qty.replace(',', '')

        price = Prompt.ask(f"[{title_color}]{title_text} 단가(원)[/] [dim]0 입력 시 시장가, 취소: q[/dim]", default="0")
        if price.lower() == 'q': return
        price = price.replace(',', '')

        display_price = "시장가(0)" if price == "0" else f"{int(price):,}원"
        
        # 주문 총액 계산 (시장가는 현재가 기준 예상)
        calc_price = int(price) if price != "0" else curr_price
        
        # [보정] 시장가 주문인데 현재가가 0인 경우(초기 조회 실패 등), 재조회 시도
        if price == "0" and calc_price == 0:
            try:
                retry_res = api.get_current_price_data(stock_code, is_overseas=False)
                if retry_res.get('rt_cd') == '0':
                    calc_price = int(retry_res['output']['stck_prpr'])
            except: pass
            
        total_amt = int(qty) * calc_price
        amt_str = f"{total_amt:,}원" + (" (예상)" if price == "0" else "")

        confirm_msg = (
            f"\n[bold white on {title_color}] [ 국내 {title_text} 주문 최종 확인 ] [/]\n"
            f" 종목: [bold]{stock_name} ({stock_code})[/bold]\n"
            f" 수량: [bold]{qty}주[/bold]\n"
            f" 단가: [bold]{display_price}[/bold]\n"
            f" 총액: [bold]{amt_str}[/bold]\n"
        )
        config.console.print(Panel(confirm_msg, expand=False, width=60))
        
        if Prompt.ask("\n위 내용으로 주문을 전송하시겠습니까?", choices=["y", "n"], default="n") != "y":
            config.console.print("[yellow]주문이 취소되었습니다.[/yellow]")
            return

        tr_id = utils.get_tr_id("domestic", "trade", order_type)
        url = f"{config.URL_BASE}/uapi/domestic-stock/v1/trading/order-cash"
        headers = utils.get_common_headers(tr_id)
        ord_dvsn = "01" if price == "0" else "00"
        data = {"CANO": config.CANO, "ACNT_PRDT_CD": config.ACNT_PRDT_CD, "PDNO": stock_code, "ORD_DVSN": ord_dvsn, "ORD_QTY": str(qty), "ORD_UNPR": str(price)}
        
        if config.DEBUG_LEVEL == "DEBUG":
            config.console.print(f"[dim cyan][DEBUG] REQ (Order-KR) | URL: {url} | Body: {data}[/dim cyan]")

        try:
            res = api.session.post(url, headers=headers, data=json.dumps(data), verify=False)
            result = res.json()
            if result['rt_cd'] == '0': 
                config.console.print(f"[bold green]주문 성공[/bold green] (번호: {result['output']['ODNO']})")
                # [추가] 주문 후 미체결 내역 자동 조회
                
                # [DB] 거래 내역 저장
                snapshot = analysis.get_snapshot(stock_code, is_overseas=False)
                db_manager.db.insert_trade(f"매수(수동)" if order_type=='buy' else f"매도(수동)", stock_code, stock_name, qty, price, result['output']['ODNO'], snapshot=snapshot, reason="사용자 수동 주문")

                config.console.print("\n[dim]체결 확인을 위해 미체결 내역을 조회합니다...[/dim]")
                show_open_orders()
            else: 
                msg1 = result.get('msg1', '알 수 없는 오류')
                config.console.print(f"[bold red]주문 실패: {msg1} (Code: {result.get('msg_cd')})[/bold red]")
                if "장운영일자가" in msg1: config.console.print("[yellow]오늘은 휴장일이거나 주문 가능한 시간이 아닐 수 있습니다.[/yellow]")
        except Exception as e: config.console.print(f"[bold red]통신/시스템 에러: {str(e)}[/bold red]")

    # [해외 주식]
    else:
        curr_price = 0.0
        cur_res = api.get_current_price_data(stock_code, is_overseas=True)
        if cur_res.get('rt_cd') == '0':
            _last = cur_res['output'].get('last', '0')
            curr_price = float(_last)
            config.console.print(f"\n[bold green]현재가: ${curr_price:,.2f}[/bold green]")

        if order_type == 'buy':
            if curr_price > 0:
                max_qty = api.fetch_overseas_buyable_quantity(stock_code, curr_price, excd)
                if max_qty > 0:
                    default_qty = str(max_qty)
                    config.console.print(f"[green]최대 {max_qty}주 매수 가능[/green]")
                else: config.console.print(f"[yellow]매수 가능 수량이 0입니다.[/yellow]")
        elif order_type == 'sell':
            max_qty = api.fetch_overseas_sellable_quantity(stock_code, excd)
            if max_qty > 0:
                default_qty = str(max_qty)
                config.console.print(f"[blue]보유 잔고: {max_qty}주 매도 가능[/blue]")
            else: config.console.print("[yellow]매도 가능 수량이 0입니다.[/yellow]")

        qty = Prompt.ask(f"\n[{title_color}]{title_text} 수량(주)[/] [dim](취소: q)[/dim]", default=default_qty)
        if qty.lower() == 'q': return
        qty = qty.replace(',', '')

        price = Prompt.ask(f"[{title_color}]{title_text} 단가(달러)[/] [dim]0 입력 시 현재가(시장가), 취소: q[/dim]", default="0")
        if price.lower() == 'q': return
        if not price: config.console.print("[red]가격을 입력해야 합니다.[/red]"); return
        price = price.replace(',', '')

        tr_id = utils.get_tr_id("overseas", "trade", order_type)

        url = f"{config.URL_BASE}/uapi/overseas-stock/v1/trading/order"
        headers = utils.get_common_headers(tr_id)
        
        trade_excd = excd
        if excd == "NAS": trade_excd = "NASD"
        elif excd == "NYS": trade_excd = "NYSE"
        elif excd == "AMS": trade_excd = "AMEX"

        is_market_order = False
        if price == "0":
            is_market_order = True
            if curr_price > 0:
                if curr_price >= 1.0:
                    price = f"{curr_price:.2f}"
                else:
                    price = f"{curr_price:.4f}"
                ord_dvsn = "00"
                config.console.print(f"[yellow]안내: 0(시장가)을 입력하여 현재가(${price}) 기준 지정가로 주문을 접수합니다.[/yellow]")
            else:
                config.console.print("[red]오류: 현재가 정보를 가져오지 못해 0(시장가) 주문을 수행할 수 없습니다.[/red]")
                return
        else: ord_dvsn = "00"

        display_price_type = "(현재가)" if is_market_order else "(지정가)"
        
        # 주문 총액 계산 (시장가는 현재가 기준 예상)
        calc_price = float(price) if price != "0" else curr_price
        total_amt = int(qty) * calc_price
        amt_str = f"${total_amt:,.2f}" + (" (예상)" if is_market_order else "")

        confirm_msg = (
            f"\n[bold white on {title_color}] [ 해외 {title_text} 주문 최종 확인 ] [/]\n"
            f" 종목: [bold]{stock_name} ({stock_code})[/bold] (거래소: {trade_excd})\n"
            f" 수량: [bold]{qty}주[/bold]\n"
            f" 단가: [bold]${price} {display_price_type}[/bold]\n"
            f" 총액: [bold]{amt_str}[/bold]\n"
        )
        config.console.print(Panel(confirm_msg, expand=False, width=60))
        
        if Prompt.ask("\n위 내용으로 주문을 전송하시겠습니까?", choices=["y", "n"], default="n") != "y":
            config.console.print("[yellow]주문이 취소되었습니다.[/yellow]")
            return

        data = {"CANO": config.CANO, "ACNT_PRDT_CD": config.ACNT_PRDT_CD, "OVRS_EXCG_CD": trade_excd, "PDNO": stock_code, "ORD_QTY": str(qty), "OVRS_ORD_UNPR": str(price), "ORD_SVR_DVSN_CD": "0", "ORD_DVSN": ord_dvsn}
        
        if config.DEBUG_LEVEL == "DEBUG":
            config.console.print(f"[dim cyan][DEBUG] REQ (Order-US) | URL: {url} | Body: {data}[/dim cyan]")

        try:
            res = api.session.post(url, headers=headers, data=json.dumps(data), verify=False)
            result = res.json()
            if result['rt_cd'] == '0': 
                odno = result.get('output', {}).get('ODNO') or result.get('output', {}).get('KRX_FWDG_ORD_ORGNO')
                config.console.print(f"[bold green]주문 성공[/bold green] (주문번호: {odno})")
                
                # [DB] 거래 내역 저장
                snapshot = analysis.get_snapshot(stock_code, is_overseas=True)
                db_manager.db.insert_trade(f"매수(수동)" if order_type=='buy' else f"매도(수동)", stock_code, stock_name, qty, price, odno, snapshot=snapshot, reason="사용자 수동 주문")

                # [추가] 주문 후 미체결 내역 자동 조회
                config.console.print("\n[dim]체결 확인을 위해 미체결 내역을 조회합니다...[/dim]")
                show_open_orders()
            else: 
                msg1 = result.get('msg1', '알 수 없는 오류')
                config.console.print(f"[bold red]주문 실패: {msg1} (Code: {result.get('msg_cd')})[/bold red]")
        except Exception as e: config.console.print(f"[bold red]통신/시스템 에러: {str(e)}[/bold red]")

def modify_order():
    config.console.print("\n[bold magenta]=== 통합 정정/취소 주문 ===[/]")
    config.console.print(f"주문 계좌: [bold]{config.CANO}-{config.ACNT_PRDT_CD}[/bold]")

    # [수정] 공통 함수 show_open_orders()를 사용하여 미체결 내역 조회 및 출력
    selectable_orders = show_open_orders()

    if not selectable_orders:
        return

    # =========================================================================
    # 4. 선택 및 분기 처리
    # =========================================================================
    choice = Prompt.ask("\n선택 번호 [dim](취소: q)[/dim]")
    if choice.lower() == 'q': return
    
    if not choice.isdigit() or int(choice) < 1 or int(choice) > len(selectable_orders):
        config.console.print("[red]잘못된 번호입니다.[/red]")
        return
        
    target_order = selectable_orders[int(choice)-1]
    origin = target_order['_origin']
    
    config.console.print(f"\n[bold cyan]선택된 주문: {target_order.get('prdt_name')} ({origin})[/bold cyan]")
    config.console.print("[1] 정정 (Modify)")
    config.console.print("[2] 취소 (Cancel)")
    config.console.print()
    action = Prompt.ask("작업 선택 [dim](취소: q)[/dim]", choices=["1", "2", "q"], default="1")
    if action.lower() == 'q': return

    # -------------------------------------------------------------------------
    # [Case 1] 국내 주식 정정/취소
    # -------------------------------------------------------------------------
    if origin == 'KR':
        org_odno = target_order.get('odno')
        target_rmn = target_order.get('rmn_qty') or target_order.get('psbl_qty', '0')
        action_name = "정정" if action == "1" else "취소"

        if action == "1": 
            rvse_cncl_dvsn_cd = "01"
            qty = Prompt.ask(f"\n[magenta]정정 수량[/] (최대 {target_rmn}주, 0: 전량) [dim](취소: q)[/dim]", default="0")
            if qty.lower() == 'q': return
            price = Prompt.ask("[magenta]정정 단가[/] (0: 시장가) [dim](취소: q)[/dim]", default="0")
            if price.lower() == 'q': return
        else: 
            rvse_cncl_dvsn_cd = "02"
            qty = Prompt.ask(f"\n[magenta]취소 수량[/] (최대 {target_rmn}주, 0: 전량) [dim](취소: q)[/dim]", default="0")
            if qty.lower() == 'q': return
            price = "0"

        qty = qty.replace(',', ''); price = price.replace(',', '')
        final_qty = target_rmn if qty == "0" else qty
        display_price = "시장가(0)" if price == "0" else f"{int(price):,}원"
        if action == "2": display_price = "(취소 시 미적용)"

        amt_msg = ""
        if action == "1":
            calc_price = int(price)
            if price == "0":
                try:
                    cp_res = api.get_current_price_data(target_order.get('pdno'), False)
                    if cp_res.get('rt_cd') == '0':
                        calc_price = int(cp_res['output']['stck_prpr'])
                except: pass
            if calc_price > 0:
                total_amt = int(final_qty) * calc_price
                amt_str = f"{total_amt:,}원" + (" (예상)" if price == "0" else "")
                amt_msg = f" 총액: [bold]{amt_str}[/bold]\n"

        confirm_msg = (
            f"\n[bold white on magenta] [ 국내 주문 {action_name} 최종 확인 ] [/]\n"
            f" 원주문번호: [bold]{org_odno}[/bold]\n"
            f" 종목: [bold]{target_order.get('prdt_name')} ({target_order.get('pdno')})[/bold]\n"
            f" {action_name} 수량: [bold]{final_qty}주[/bold]\n"
            f" {action_name} 단가: [bold]{display_price}[/bold]\n"
            f"{amt_msg}"
        )
        config.console.print(Panel(confirm_msg, expand=False, width=60))
        if Prompt.ask("\n진행하시겠습니까?", choices=["y", "n"], default="n") != "y": return

        tr_id = utils.get_tr_id("domestic", "modify", "revise") # 정정/취소 동일 TR
        url = f"{config.URL_BASE}/uapi/domestic-stock/v1/trading/order-rvsecncl"
        headers = utils.get_common_headers(tr_id)
        ord_dvsn = "01" if price == "0" else "00"
        qty_all_yn = "Y" if qty == "0" or qty == target_rmn else "N"
        
        data = {"CANO": config.CANO, "ACNT_PRDT_CD": config.ACNT_PRDT_CD, "KRX_FWDG_ORD_ORGNO": "", "ORGN_ODNO": org_odno, "ORD_DVSN": ord_dvsn, "RVSE_CNCL_DVSN_CD": rvse_cncl_dvsn_cd, "ORD_QTY": final_qty, "ORD_UNPR": price, "QTY_ALL_ORD_YN": qty_all_yn}
        
        if config.DEBUG_LEVEL == "DEBUG":
            config.console.print(f"[dim cyan][DEBUG] REQ (Modify-KR) | URL: {url} | Body: {data}[/dim cyan]")

        try:
            res = api.session.post(url, headers=headers, data=json.dumps(data), verify=False)
            res_json = res.json()
            if res_json['rt_cd'] == '0': 
                config.console.print(f"[bold green]접수 완료 (번호: {res_json['output']['ODNO']})[/]")
                
                # [DB] 정정/취소 내역 저장
                db_manager.db.insert_trade(f"{action_name}(수동)", target_order.get('pdno'), target_order.get('prdt_name'), final_qty, price, res_json['output']['ODNO'], org_odno=org_odno, reason=f"사용자 {action_name}")

                # [추가] 정정/취소 후 미체결 내역 자동 조회
                config.console.print("\n[dim]변경 사항 확인을 위해 미체결 내역을 조회합니다...[/dim]")
                show_open_orders()
            else: config.console.print(f"[red]실패: {res_json.get('msg1')}[/]")
        except Exception as e: config.console.print(f"[red]에러: {e}[/]")

    # -------------------------------------------------------------------------
    # [Case 2] 해외 주식 정정/취소
    # -------------------------------------------------------------------------
    elif origin == 'US':
        org_odno = target_order.get('odno')
        target_rmn = target_order.get('nccs_qty')
        target_excd = target_order.get('ovrs_excg_cd')
        action_name = "정정" if action == "1" else "취소"

        if action == "1":
            rvse_cncl_dvsn_cd = "01"
            qty = Prompt.ask(f"[magenta]정정 수량[/] (최대 {target_rmn}주, 0: 전량) [dim](취소: q)[/dim]", default="0")
            if qty.lower() == 'q': return
            price = Prompt.ask("[magenta]정정 단가($)[/] [dim](취소: q)[/dim]")
            if price.lower() == 'q': return
            if not price: config.console.print("[red]가격 입력 필요[/]"); return
        else:
            rvse_cncl_dvsn_cd = "02"
            qty = Prompt.ask(f"\n[magenta]취소 수량[/] (최대 {target_rmn}주, 0: 전량) [dim](취소: q)[/dim]", default="0")
            if qty.lower() == 'q': return
            price = "0"

        qty = qty.replace(',', ''); price = price.replace(',', '')
        final_qty = target_rmn if qty == "0" else qty
        display_price = f"${price}" if action == "1" else "(취소 시 미적용)"

        amt_msg = ""
        if action == "1":
            try:
                calc_price = float(price)
                total_amt = float(final_qty) * calc_price
                amt_str = f"${total_amt:,.2f}"
                amt_msg = f" 총액: [bold]{amt_str}[/bold]\n"
            except: pass

        confirm_msg = (
            f"\n[bold white on magenta] [ 해외 주문 {action_name} 최종 확인 ] [/]\n"
            f" 원주문번호: [bold]{org_odno}[/bold]\n"
            f" 종목: [bold]{target_order.get('prdt_name')} ({target_order.get('pdno')})[/bold]\n"
            f" {action_name} 수량: [bold]{final_qty}주[/bold]\n"
            f" {action_name} 단가: [bold]{display_price}[/bold]\n"
            f"{amt_msg}"
        )
        config.console.print(Panel(confirm_msg, expand=False, width=60))
        if Prompt.ask("\n진행하시겠습니까?", choices=["y", "n"], default="n") != "y": return

        tr_id = utils.get_tr_id("overseas", "modify", "revise")
        url = f"{config.URL_BASE}/uapi/overseas-stock/v1/trading/order-rvsecncl"
        headers = utils.get_common_headers(tr_id)
        
        data = {
            "CANO": config.CANO, "ACNT_PRDT_CD": config.ACNT_PRDT_CD, 
            "OVRS_EXCG_CD": target_excd, "PDNO": target_order.get('pdno'), 
            "ORGN_ODNO": org_odno, "RVSE_CNCL_DVSN_CD": rvse_cncl_dvsn_cd, 
            "ORD_QTY": final_qty, "OVRS_ORD_UNPR": price
        }
        
        if config.DEBUG_LEVEL == "DEBUG":
            config.console.print(f"[dim cyan][DEBUG] REQ (Modify-US) | URL: {url} | Body: {data}[/dim cyan]")

        try:
            res = api.session.post(url, headers=headers, data=json.dumps(data), verify=False)
            res_json = res.json()
            if res_json['rt_cd'] == '0': 
                config.console.print(f"[bold green]접수 완료 (번호: {res_json.get('output', {}).get('ODNO')})[/]")
                
                # [DB] 정정/취소 내역 저장
                db_manager.db.insert_trade(f"{action_name}(수동)", target_order.get('pdno'), target_order.get('prdt_name'), final_qty, price, res_json.get('output', {}).get('ODNO'), org_odno=org_odno, reason=f"사용자 {action_name}")

                # [추가] 정정/취소 후 미체결 내역 자동 조회
                config.console.print("\n[dim]변경 사항 확인을 위해 미체결 내역을 조회합니다...[/dim]")
                show_open_orders()
            else: config.console.print(f"[red]실패: {res_json.get('msg1')}[/]")
        except Exception as e: config.console.print(f"[red]에러: {e}[/]")
