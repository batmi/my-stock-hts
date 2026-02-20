# modules/trading.py
import logging
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
from modules import auto_trade # [추가] 체결 감시자 호출용

logger = logging.getLogger(__name__)

def select_account():
    """주문 수행할 계좌를 선택합니다."""
    target_cano = config.session.cano
    target_acnt = config.session.acnt_prdt_cd
    acc_label = "실전투자" if not config.session.is_simulation else "모의투자"

    # 실전 모드이고 자동매매 계좌가 별도로 설정된 경우 선택
    if not config.session.is_simulation and config.session.auto_cano and config.session.auto_cano != config.session.cano:
        config.console.print("\n[bold]주문을 수행할 계좌를 선택하세요:[/bold]")
        config.console.print(f"[1] {acc_label}: {config.session.cano}-{config.session.acnt_prdt_cd}")
        config.console.print(f"[2] 자동투자: {config.session.auto_cano}-{config.session.auto_acnt_prdt_cd}")
        config.console.print()
        
        choice = Prompt.ask("선택 [dim](기본값: 1, 취소: q)[/dim]", choices=["1", "2", "q"], default="1")
        if choice.lower() == 'q':
            return None, None, None
            
        if choice == "2":
            target_cano = config.session.auto_cano
            target_acnt = config.session.auto_acnt_prdt_cd
            acc_label = "자동투자"
            
    return target_cano, target_acnt, acc_label

def select_stock_from_balance(cano=None, acnt_prdt_cd=None):
    """
    매도 시 보유 잔고에서 종목을 선택하는 함수
    (메뉴 4번 잔고 조회와 동일한 상세 정보를 출력)
    """
    config.console.print("\n[bold]어떤 시장의 보유 주식을 매도하시겠습니까?[/bold]")
    config.console.print("[1] 국내 주식")
    config.console.print("[2] 해외 주식")
    config.console.print()
    market_choice = Prompt.ask("선택 [dim](취소: q)[/dim]", choices=["1", "2", "q"], default="1", show_choices=False, show_default=False)
    
    if market_choice == 'q':
        return None, None, False, None, None

    is_overseas = (market_choice == "2")
    candidates = []

    # ---------------------------
    # 1. 국내 잔고 조회
    # ---------------------------
    if not is_overseas:
        with config.console.status("[bold green]국내 잔고 조회 중...[/]"):
            holdings, _ = account.fetch_domestic_balance(cano, acnt_prdt_cd)
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
            holdings = account.fetch_overseas_balance(cano, acnt_prdt_cd)
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
        return None, None, False, None, None

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
    if sel_idx.lower() == 'q': return None, None, False, None, None

    try:
        idx = int(sel_idx) - 1
        if 0 <= idx < len(candidates):
            selected = candidates[idx]
            
            mapped_excd = None
            if is_overseas:
                ex_map = {"NASD": "NAS", "NYSE": "NYS", "AMEX": "AMS"}
                mapped_excd = ex_map.get(selected['excd'], "NAS")
                
            return selected['code'], selected['name'], is_overseas, mapped_excd, selected
        else:
            config.console.print("[red]잘못된 번호입니다.[/red]")
            return None, None, False, None, None
    except ValueError:
        config.console.print("[red]숫자를 입력해주세요.[/red]")
        return None, None, False, None, None


def show_open_orders():
    """모든 계좌의 미체결 내역을 조회하고 테이블로 출력하며, 선택 가능한 주문 리스트를 반환합니다."""
    
    # 계좌 목록 구성
    accounts = []
    # 1. 메인 계좌
    if config.session.cano:
        label = "모의" if config.session.is_simulation else "실전"
        accounts.append({"cano": config.session.cano, "acnt": config.session.acnt_prdt_cd, "label": label})
    
    # 2. 자동매매 계좌 (실전 모드이고 별도 설정된 경우)
    if not config.session.is_simulation and config.session.auto_cano and config.session.auto_cano != config.session.cano:
        accounts.append({"cano": config.session.auto_cano, "acnt": config.session.auto_acnt_prdt_cd, "label": "자동"})

    selectable_orders = []

    with config.console.status("[bold green]전체 계좌 미체결 내역 조회 중...[/]"):
        table = Table(title="\n미체결 내역 (전체 계좌)", box=box.HORIZONTALS, header_style="dim", border_style="dim")
        table.add_column("No", justify="right")
        table.add_column("계좌번호", justify="center")
        table.add_column("종류", justify="center")
        table.add_column("국가", justify="center")
        table.add_column("주문시간", justify="center")
        table.add_column("주문번호")
        table.add_column("종목명(코드)")
        table.add_column("구분", justify="center")
        table.add_column("주문수량", justify="right")
        table.add_column("주문단가", justify="right")
        table.add_column("현재가", justify="right", style="bold")
        table.add_column("잔량", justify="right")

        idx = 1
        
        for acc in accounts:
            cano = acc['cano']
            acnt = acc['acnt']
            acc_label = acc['label']
            
            # [수정] 계좌 라벨 스타일링 (실전: 노랑, 자동: 주황)
            acc_disp = acc_label
            if "실전" in acc_label:
                acc_disp = f"[bold yellow]{acc_label}[/]"
            elif "자동" in acc_label:
                acc_disp = f"[bold orange3]{acc_label}[/]"
            
            # 해당 계좌 컨텍스트로 API 호출
            with utils.AccountContext(cano):
                # [A] 국내 주문
                dom_orders = api.get_domestic_open_orders(cano, acnt)
                for order in dom_orders:
                    rmn_qty = order.get('rmn_qty') or order.get('psbl_qty', '0')
                    if api.safe_int(rmn_qty) <= 0: continue
                    
                    order['_origin'] = 'KR'
                    order['_account'] = acc # 계좌 정보 저장
                    selectable_orders.append(order)
                    
                    sll_buy = order.get('sll_buy_dvsn_cd_name', '').strip()
                    if not sll_buy:
                        cd = order.get('sll_buy_dvsn_cd', '')
                        sll_buy = "매도" if cd == '01' else ("매수" if cd == '02' else cd)
                    sll_buy_colored = f"[red]{sll_buy}[/]" if "매수" in sll_buy else f"[blue]{sll_buy}[/]"
                    
                    cur_price_str = "-"
                    if order.get('pdno'):
                        price = api.get_current_price(order.get('pdno'), False)
                        if price > 0: cur_price_str = f"{price:,}원"
                    
                    display_name = f"{order.get('prdt_name')} ({order.get('pdno')})"
                    ord_tmd = order.get('ord_tmd', '')
                    ord_time = f"{ord_tmd[:2]}:{ord_tmd[2:4]}:{ord_tmd[4:]}" if len(ord_tmd) == 6 else "-"

                    table.add_row(str(idx), f"{cano}-{acnt}", acc_disp, "[bold]국내[/]", ord_time, order.get('odno'), display_name, sll_buy_colored, order.get('ord_qty'), f"{api.safe_int(order.get('ord_unpr')):,.0f}", cur_price_str, rmn_qty)
                    idx += 1

                # [B] 해외 주문
                us_orders = api.get_overseas_open_orders(cano, acnt)
                for order in us_orders:
                    rmn_qty = order.get('nccs_qty', '0')
                    if float(rmn_qty) <= 0: continue

                    order['_origin'] = 'US'
                    order['_account'] = acc
                    selectable_orders.append(order)

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
                        price = api.get_current_price(order.get('pdno'), True)
                        if price > 0: cur_price_str = f"${price:,.2f}"

                    display_name = f"{order.get('prdt_name')} ({order.get('pdno')})"
                    ord_dt = order.get('ord_dt', '')
                    ord_tmd = order.get('ord_tmd', '')
                    ord_time = "-"
                    if len(ord_tmd) == 6:
                        t_str = f"{ord_tmd[:2]}:{ord_tmd[2:4]}:{ord_tmd[4:]}"
                        ord_time = f"{ord_dt[4:6]}/{ord_dt[6:]} {t_str}" if len(ord_dt) == 8 else t_str

                    table.add_row(str(idx), f"{cano}-{acnt}", acc_disp, "[bold magenta]해외[/]", ord_time, order.get('odno'), display_name, sll_buy_colored, order.get('ft_ord_qty', '0'), f"${ord_unpr:,.2f}", cur_price_str, rmn_qty)
                    idx += 1

    if not selectable_orders:
        config.console.print("\n[yellow]미체결 주문 내역이 없습니다.[/yellow]")
        return []

    config.console.print(table)
    return selectable_orders


def send_order(order_type):
    # 0. 계좌 선택
    target_cano, target_acnt, acc_label = select_account()
    if not target_cano: return # 취소 시 복귀

    # 1. 타이틀 출력
    title_color = 'red' if order_type == 'buy' else 'blue'
    title_text = "매수" if order_type == 'buy' else "매도"
    config.console.print(f"\n[bold {title_color}]=== 주식 {title_text.upper()} 주문 (현금 전용) ===[/]")
    config.console.print(f"주문 계좌: [bold]{target_cano}-{target_acnt}[/bold] ({acc_label})")
    
    # 컨텍스트 적용 (이후 모든 API 호출은 이 계좌 기준)
    with utils.AccountContext(target_cano):
        # 2. 종목 선택 (매수: 검색 / 매도: 잔고에서 선택)
        pre_selected_excd = None
        stock_info = {}
        
        if order_type == 'sell':
            # 매도 시 상세 잔고 리스트에서 선택
            res = select_stock_from_balance(target_cano, target_acnt)
            if not res or res[0] is None: return
            stock_code, stock_name, is_overseas, pre_selected_excd, stock_info = res
        else:
            # [수정] 매수 시 종목 선택 메뉴 확장 ([5] 직접 입력 추가)
            config.console.print("\n[bold]매수할 종목을 선택하세요:[/bold]")
            config.console.print("[1] 국내 주식")
            config.console.print("[2] 국내 ETF")
            config.console.print("[3] 미국 주식")
            config.console.print("[4] 미국 ETF")
            config.console.print("[5] 직접 입력 (코드 검색)")
            config.console.print()
            
            choice = Prompt.ask("선택 [dim](취소: q)[/dim]", choices=["1", "2", "3", "4", "5", "q"], default="5")
            if choice.lower() == 'q': return

            stock_code, stock_name, is_overseas = None, None, False

            if choice == '5':
                raw_input = Prompt.ask("종목코드(6자리/티커) 또는 종목명 [dim](취소: q)[/dim]")
                if not raw_input or raw_input.lower() == 'q': return
                
                # 간단 검색 로직
                if raw_input.isdigit() and len(raw_input) == 6:
                    stock_code = raw_input
                    stock_name = api.get_stock_name_by_code(stock_code, False) or stock_code
                    is_overseas = False
                elif all(ord(c) < 128 for c in raw_input) and not raw_input.isdigit():
                    stock_code = raw_input.upper()
                    stock_name = api.get_stock_name_by_code(stock_code, True) or stock_code
                    is_overseas = True
                else:
                    # 한글명 검색 등은 생략하거나 utils 활용 필요하나 여기선 코드로 유도
                    config.console.print("[yellow]정확한 종목 코드를 입력해주세요.[/yellow]")
                    return
            else:
                # 리스트 선택
                key_map = {"1": "stocks_kr", "2": "etfs_kr", "3": "stocks_us", "4": "etfs_us"}
                target_key = key_map.get(choice)
                stock_list = config.session.stock_data.get(target_key, [])
                
                if not stock_list:
                    config.console.print("[yellow]등록된 종목이 없습니다.[/yellow]")
                    return
                    
                for i, s in enumerate(stock_list):
                    config.console.print(f"[{i+1}] {s['name']} ({s['code']})")
                
                config.console.print()
                sel = Prompt.ask("번호 선택 [dim](취소: q)[/dim]")
                if sel.lower() == 'q': return
                if sel.isdigit() and 1 <= int(sel) <= len(stock_list):
                    item = stock_list[int(sel)-1]
                    stock_code, stock_name = item['code'], item['name']
                    is_overseas = (choice in ["3", "4"])
                else: return
        
        if not stock_code: 
            config.console.print("[yellow]주문이 취소되었습니다.[/yellow]")
            return

        # 3. 거래소 확인 (해외)
        excd = ""
        if is_overseas:
            if pre_selected_excd:
                excd = pre_selected_excd
            else:
                found_excd = api.find_best_exchange_code(stock_code)
                excd = found_excd if found_excd else Prompt.ask("거래소 코드 수동 입력 (NAS/NYS/AMS)", default="NAS").upper()
            config.console.print(f"선택 종목: [bold cyan]{stock_name} ({stock_code})[/bold cyan] (거래소: {excd})")
        else:
            config.console.print(f"선택 종목: [bold cyan]{stock_name} ({stock_code})[/bold cyan]")

        # 4. 현재가 및 최대 수량 조회
        curr_price = api.get_current_price(stock_code, is_overseas)
        
        if curr_price > 0:
            price_fmt = f"${curr_price:,.2f}" if is_overseas else f"{curr_price:,}원"
            config.console.print(f"\n[bold green]현재가: {price_fmt}[/bold green]")

        default_qty = "1"
        if order_type == 'buy':
            if curr_price > 0:
                if is_overseas:
                    max_qty = api.fetch_overseas_buyable_quantity(stock_code, curr_price, excd)
                else:
                    max_qty = api.fetch_buyable_quantity(stock_code, curr_price)
                
                if max_qty > 0:
                    default_qty = str(max_qty)
                    config.console.print(f"[green]최대 {max_qty}주 매수 가능[/green]")
                else:
                    config.console.print(f"[yellow]매수 가능 수량이 0입니다.[/yellow]")
        elif order_type == 'sell':
            if is_overseas:
                max_qty = api.fetch_overseas_sellable_quantity(stock_code, excd)
            else:
                max_qty = api.fetch_sellable_quantity(stock_code)
            
            if max_qty > 0:
                default_qty = str(max_qty)
                config.console.print(f"[blue]보유 잔고: {max_qty}주 매도 가능[/blue]")
            else:
                config.console.print("[yellow]매도 가능 수량이 0입니다.[/yellow]")

        # 5. 수량 및 단가 입력
        qty = Prompt.ask(f"\n[{title_color}]{title_text} 수량(주)[/] [dim](취소: q)[/dim]", default=default_qty)
        if qty.lower() == 'q': return
        config.USER_ACTION_BREADCRUMB.append(f"[수량] {qty}")
        qty = qty.replace(',', '')

        unit = "달러" if is_overseas else "원"
        price_prompt = f"[{title_color}]{title_text} 단가({unit})[/] [dim]0 입력 시 시장가(현재가), 취소: q[/dim]"
        price = Prompt.ask(price_prompt, default="0")
        if price.lower() == 'q': return
        config.USER_ACTION_BREADCRUMB.append(f"[단가] {price}")
        if is_overseas and not price: config.console.print("[red]가격을 입력해야 합니다.[/red]"); return
        price = price.replace(',', '')

        # 6. 가격 처리 및 주문 구분 설정
        ord_dvsn = "00"
        calc_price = 0
        display_price = ""
        is_market_order = (price == "0")

        if is_overseas:
            # [해외] 시장가(0) 입력 시 현재가 기준 지정가로 변환
            if is_market_order:
                if curr_price > 0:
                    if curr_price >= 1.0:
                        price = f"{curr_price:.2f}"
                    else:
                        price = f"{curr_price:.4f}"
                    config.console.print(f"[yellow]안내: 0(시장가)을 입력하여 현재가(${price}) 기준 지정가로 주문을 접수합니다.[/yellow]")
                else:
                    config.console.print("[red]오류: 현재가 정보를 가져오지 못해 0(시장가) 주문을 수행할 수 없습니다.[/red]")
                    return
            
            calc_price = float(price)
            display_price = f"${price}" + (" (현재가)" if is_market_order else " (지정가)")
        else:
            # [국내] 시장가 주문 처리
            if is_market_order:
                ord_dvsn = "01"
                display_price = "시장가(0)"
                if curr_price == 0:
                     p = api.get_current_price(stock_code, False)
                     if p > 0: curr_price = int(p)
                calc_price = curr_price
            else:
                ord_dvsn = "00"
                calc_price = int(price)
                display_price = f"{int(price):,}원"

        # 7. 예상 금액 계산 및 확인 메시지
        total_amt = float(qty) * calc_price
        est_tag = " (예상)" if is_market_order else ""
        
        if is_overseas:
            amt_str = f"${total_amt:,.2f}{est_tag}"
        else:
            amt_str = f"{int(total_amt):,}원{est_tag}"

        market_label = "해외" if is_overseas else "국내"
        excd_info = f" (거래소: {excd})" if is_overseas else ""
        
        confirm_msg = (
            f"\n[bold white on {title_color}] [ {market_label} {title_text} 주문 최종 확인 ] [/]\n"
            f" 종목: [bold]{stock_name} ({stock_code})[/bold]{excd_info}\n"
            f" 수량: [bold]{qty}주[/bold]\n"
            f" 단가: [bold]{display_price}[/bold]\n"
            f" 총액: [bold]{amt_str}[/bold]\n"
        )
        config.console.print(Panel(confirm_msg, expand=False, width=60))
        
        if Prompt.ask("\n위 내용으로 주문을 전송하시겠습니까?", choices=["y", "n"], default="n") != "y":
            config.console.print("[yellow]주문이 취소되었습니다.[/yellow]")
            return

        # 8. 주문 전송
        market_api_param = "overseas" if is_overseas else "domestic"
        
        if config.FILE_DEBUG_LEVEL == "DEBUG":
            logger.debug(f"REQ (Order-{market_label}) | {order_type} | {stock_code} {qty}ea")

        logger.info(f"운영자 실행: {' - '.join(config.USER_ACTION_BREADCRUMB)}")

        try:
            result = api.place_order(market_api_param, order_type, stock_code, qty, price, ord_dvsn, exchange_code=excd)
            
            if result['rt_cd'] == '0':
                odno = result.get('output', {}).get('ODNO') or result.get('output', {}).get('KRX_FWDG_ORD_ORGNO')
                config.console.print(f"[bold green]주문 성공[/bold green] (주문번호: {odno})")
                
                # 텔레그램 알림
                t_type = "매수" if order_type == 'buy' else "매도"
                api.send_telegram_message(f"🚀 [수동 주문] {t_type} 접수\n종목: {stock_name} ({stock_code})\n수량: {qty}주\n단가: {display_price}\n주문번호: {odno}")
                
                # DB 저장
                snapshot = analysis.get_snapshot(stock_code, is_overseas=is_overseas)
                
                # [추가] 매도 시 예상 손익 계산 및 저장
                profit_amt = 0
                profit_rate = 0.0
                if order_type == 'sell' and stock_info:
                    try:
                        buy_price = float(stock_info.get('buy_price', 0))
                        if buy_price > 0:
                            # calc_price: 주문 단가 (시장가인 경우 현재가)
                            est_sell_amt = float(qty) * calc_price
                            est_buy_amt = float(qty) * buy_price
                            # 단순 차익 계산 (수수료/세금 제외)
                            profit_amt = int(est_sell_amt - est_buy_amt)
                            profit_rate = ((calc_price - buy_price) / buy_price) * 100
                    except: pass

                db_manager.db.insert_trade(f"{t_type}(수동)", stock_code, stock_name, qty, price, odno, snapshot=snapshot, reason="사용자 수동 주문", profit_amt=profit_amt, profit_rate=profit_rate)
                
                # 매도 시 트레일링 스탑 초기화
                if order_type == 'sell':
                    db_manager.db.delete_trailing_stop(stock_code)
                    auto_trade.AutoTrader().trailing_stop_cache.pop(stock_code, None)
                
                # 체결 감시 및 미체결 조회
                auto_trade.ConclusionMonitor().check_now()
                
                config.console.print("\n[dim]체결 확인을 위해 미체결 내역을 조회합니다...[/dim]")
                show_open_orders()
            else:
                msg1 = result.get('msg1', '알 수 없는 오류')
                config.console.print(f"[bold red]주문 실패: {msg1} (Code: {result.get('msg_cd')})[/bold red]")
                if "장운영일자가" in msg1: config.console.print("[yellow]오늘은 휴장일이거나 주문 가능한 시간이 아닐 수 있습니다.[/yellow]")
                
        except Exception as e:
            config.console.print(f"[bold red]통신/시스템 에러: {str(e)}[/bold red]")

def modify_order():
    config.console.print("\n[bold magenta]=== 통합 정정/취소 주문 ===[/]")
    # config.console.print(f"주문 계좌: [bold]{target_cano}-{target_acnt}[/bold] ({acc_label})") # 계좌 선택 제거로 주석 처리

    # [추가] 메뉴 진입 시점 로깅 (미체결 내역이 없어도 기록 남기기 위함)
    logger.info(f"운영자 실행: {' - '.join(config.USER_ACTION_BREADCRUMB)}")

    # [수정] 공통 함수 show_open_orders()를 사용하여 미체결 내역 조회 및 출력
    selectable_orders = show_open_orders()

    if not selectable_orders:
        return

    # =========================================================================
    # 4. 선택 및 분기 처리
    # =========================================================================
    choice = Prompt.ask("\n선택 번호 [dim](취소: q)[/dim]")
    if choice.lower() == 'q': return
    config.USER_ACTION_BREADCRUMB.append(f"[주문선택] {choice}")
    
    if not choice.isdigit() or int(choice) < 1 or int(choice) > len(selectable_orders):
        config.console.print("[red]잘못된 번호입니다.[/red]")
        return
        
    target_order = selectable_orders[int(choice)-1]
    origin = target_order['_origin']
    
    # [추가] 선택된 주문의 계좌 정보 추출
    acc_info = target_order.get('_account', {})
    target_cano = acc_info.get('cano') or config.session.cano
    target_acnt = acc_info.get('acnt') or config.session.acnt_prdt_cd
    acc_label = acc_info.get('label', '메인')
    
    config.console.print(f"주문 계좌: [bold]{target_cano}-{target_acnt}[/bold] ({acc_label})")
    
    config.console.print(f"\n[bold cyan]선택된 주문: {target_order.get('prdt_name')} ({origin})[/bold cyan]")
    config.console.print("[1] 정정 (Modify)")
    config.console.print("[2] 취소 (Cancel)")
    config.console.print()
    action = Prompt.ask("작업 선택 [dim](취소: q)[/dim]", choices=["1", "2", "q"], default="1")
    if action.lower() == 'q': return
    
    action_map = {"1": "정정", "2": "취소"}
    if action in action_map: config.USER_ACTION_BREADCRUMB.append(f"[{action}] {action_map[action]}")

    # 공통 변수 추출
    org_odno = target_order.get('odno')
    pdno = target_order.get('pdno')
    prdt_name = target_order.get('prdt_name')
    is_overseas = (origin == 'US')
    market = "overseas" if is_overseas else "domestic"
    action_name = "정정" if action == "1" else "취소"
    
    # [추가] 매수/매도 구분 식별 (정정/취소 시 명확한 표기를 위함)
    sb_cd = target_order.get('sll_buy_dvsn_cd')
    sb_label = "매수" if sb_cd == '02' else ("매도" if sb_cd == '01' else "")
    full_action_name = f"{sb_label}{action_name}"

    # 시장별 변수 설정
    if is_overseas:
        target_rmn = target_order.get('nccs_qty')
        target_excd = target_order.get('ovrs_excg_cd')
    else:
        target_rmn = target_order.get('rmn_qty') or target_order.get('psbl_qty', '0')
        target_excd = None

    # 입력 로직 (정정/취소 공통)
    if action == "1": # 정정
        rvse_cncl_dvsn_cd = "01"
        qty = Prompt.ask(f"\n[magenta]정정 수량[/] (최대 {target_rmn}주, 0: 전량) [dim](취소: q)[/dim]", default="0")
        if qty.lower() == 'q': return
        config.USER_ACTION_BREADCRUMB.append(f"[수량] {qty}")
        
        price_prompt = "[magenta]정정 단가($)[/]" if is_overseas else "[magenta]정정 단가[/] (0: 시장가)"
        price = Prompt.ask(f"{price_prompt} [dim](취소: q)[/dim]", default="0")
        if price.lower() == 'q': return
        config.USER_ACTION_BREADCRUMB.append(f"[단가] {price}")
        if is_overseas and not price: 
            config.console.print("[red]가격 입력 필요[/]"); return
    else: # 취소
        rvse_cncl_dvsn_cd = "02"
        qty = Prompt.ask(f"\n[magenta]취소 수량[/] (최대 {target_rmn}주, 0: 전량) [dim](취소: q)[/dim]", default="0")
        if qty.lower() == 'q': return
        config.USER_ACTION_BREADCRUMB.append(f"[수량] {qty}")
        price = "0"

    qty = qty.replace(',', ''); price = price.replace(',', '')
    final_qty = target_rmn if qty == "0" else qty
    
    # 표시용 가격 문자열
    display_price = "(취소 시 미적용)"
    if action == "1":
        if is_overseas:
            display_price = f"${price}"
        else:
            display_price = f"{int(price):,}원" if price != "0" else "시장가(0)"

    # 예상 금액 계산
    amt_msg = ""
    if action == "1":
        try:
            calc_price = float(price)
            if price == "0":
                try:
                    p = api.get_current_price(pdno, is_overseas)
                    if p > 0: calc_price = float(p)
                except: pass
            
            if calc_price > 0:
                total_amt = float(final_qty) * calc_price
                est_tag = " (예상)" if price == "0" else ""
                
                if is_overseas:
                    amt_str = f"${total_amt:,.2f}{est_tag}"
                else:
                    amt_str = f"{int(total_amt):,}원{est_tag}"
                    
                amt_msg = f" 총액: [bold]{amt_str}[/bold]\n"
        except: pass

    nation_str = "해외" if is_overseas else "국내"
    excd_info = f" (거래소: {target_excd})" if is_overseas and target_excd else ""
    
    confirm_msg = (
        f"\n[bold white on magenta] [ {nation_str} 주문 {action_name} 최종 확인 ] [/]\n"
        f" 원주문번호: [bold]{org_odno}[/bold]\n"
        f" 종목: [bold]{prdt_name} ({pdno})[/bold]{excd_info}\n"
        f" {action_name} 수량: [bold]{final_qty}주[/bold]\n"
        f" {action_name} 단가: [bold]{display_price}[/bold]\n"
        f"{amt_msg}"
    )
    config.console.print(Panel(confirm_msg, expand=False, width=60))
    if Prompt.ask("\n진행하시겠습니까?", choices=["y", "n"], default="n") != "y": return

    api_action = "revise" if action == "1" else "cancel"
    ord_dvsn = "00"
    req_qty = final_qty

    if not is_overseas:
        ord_dvsn = "01" if price == "0" else "00"
        req_qty = 0 if qty == "0" or qty == target_rmn else int(final_qty)

    if config.FILE_DEBUG_LEVEL == "DEBUG":
        logger.debug(f"REQ (Modify-{origin}) | {action_name}")

    logger.info(f"운영자 실행: {' - '.join(config.USER_ACTION_BREADCRUMB)}")

    # 컨텍스트 적용 (선택된 주문의 계좌 사용)
    with utils.AccountContext(target_cano):
        try:
            res_json = api.revise_cancel_order(market, api_action, org_odno, pdno, req_qty, price, rvse_cncl_dvsn_cd, ord_dvsn, exchange_code=target_excd)
            
            if res_json['rt_cd'] == '0':
                odno = res_json.get('output', {}).get('ODNO') or res_json.get('output', {}).get('KRX_FWDG_ORD_ORGNO')
                if not odno and 'output' in res_json and 'ODNO' in res_json['output']:
                    odno = res_json['output']['ODNO']
                
                config.console.print(f"[bold green]접수 완료 (번호: {odno})[/]")
                
                api.send_telegram_message(f"🚀 [수동 주문] {full_action_name} 접수\n종목: {prdt_name} ({pdno})\n수량: {final_qty}주\n단가: {display_price}\n주문번호: {odno}")
                
                db_manager.db.insert_trade(f"{full_action_name}(수동)", pdno, prdt_name, final_qty, price, odno, org_odno=org_odno, reason=f"사용자 {action_name}")
                
                auto_trade.ConclusionMonitor().check_now()
                
                config.console.print("\n[dim]변경 사항 확인을 위해 미체결 내역을 조회합니다...[/dim]")
                show_open_orders()
            else:
                config.console.print(f"[red]실패: {res_json.get('msg1')}[/]")
        except Exception as e:
            config.console.print(f"[red]에러: {e}[/]")
