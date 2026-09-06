# modules/trading.py
import logging
from rich.prompt import Prompt
from rich.panel import Panel
from rich.table import Table
from rich import box
from rich.progress import Progress, SpinnerColumn, BarColumn, TextColumn
import json
import time
import concurrent.futures
import os
from datetime import datetime, timedelta, timezone
import config
from core import context # [추가]
import api
from core import utils
from modules import manage
from modules import account
from core import indicators # [추가] ATR 계산을 위해 추가
from modules import db_manager
from modules import analysis
from modules import auto_trade # [추가] 체결 감시자 호출용

logger = logging.getLogger(__name__)

def select_account(title="주문을 수행할 계좌를 선택하세요"):
    """계좌를 선택합니다."""
    target_cano = config.session.cano
    target_acnt = config.session.acnt_prdt_cd
    if getattr(config.session, 'is_paper', False):
        acc_label = "가상투자"
    elif config.session.is_toss:
        acc_label = "토스증권"
    else:
        acc_label = "한투증권"

    # 실전 모드이고 자동매매 계좌가 별도로 설정된 경우 선택
    if config.session.auto_cano and config.session.auto_cano != config.session.cano:
        menu_items = [
            ("1", f"{acc_label}", f"(Main): {config.session.cano}-{config.session.acnt_prdt_cd}"),
            ("2", "자동투자", f"(Auto): {config.session.auto_cano}-{config.session.auto_acnt_prdt_cd}")
        ]
        choice = utils.show_menu(title, menu_items, default_choice="1")
        if choice.lower() in ['b', 'q']:
            return False, False, False
            
        menu_map_dict = dict((k, v) for k, v, _ in menu_items)
        context.USER_ACTION_BREADCRUMB.append(f"[{choice}] 계좌: {menu_map_dict.get(choice, '')}")
            
        if choice == "2":
            target_cano = config.session.auto_cano
            target_acnt = config.session.auto_acnt_prdt_cd
            acc_label = "자동투자"

    return target_cano, target_acnt, acc_label

def _fmt_account(cano, acnt):
    """계좌 표시 문자열. 토스 등 상품코드(acnt)가 없으면 끝의 '-'를 제거한다."""
    return f"{cano}-{acnt or ''}".rstrip('-')

def _fmt_odno(odno):
    """주문번호 표시(토스는 뒤 10자리, KIS는 그대로) - utils.format_order_no 위임."""
    return utils.format_order_no(odno)

def select_stock_from_balance(cano=None, acnt_prdt_cd=None):
    """
    매도 시 보유 잔고에서 종목을 선택하는 함수
    (메뉴 4번 잔고 조회와 동일한 상세 정보를 출력)
    """
    menu_items = [("1", "국내 주식", "Domestic"), ("2", "해외 주식", "Overseas")]
    market_choice = utils.show_menu("어떤 시장의 보유 주식을 매도하시겠습니까?", menu_items, default_choice="1")
    
    if market_choice.lower() in ['b', 'q']:
        return False, False, False, False, False

    menu_map_dict = dict((k, v) for k, v, _ in menu_items)
    context.USER_ACTION_BREADCRUMB.append(f"[{market_choice}] 잔고: {menu_map_dict.get(market_choice, '')}")

    is_overseas = (market_choice == "2")
    candidates = []
    overseas_query_failed = False
    domestic_query_failed = False

    # ---------------------------
    # 1. 국내 잔고 조회
    # ---------------------------
    if not is_overseas:
        with Progress(
            SpinnerColumn(),
            TextColumn("[progress.description]{task.description}"),
            BarColumn(),
            console=config.console,
            transient=True
        ) as progress:
            progress.add_task("[cyan]국내 잔고 조회 중...[/cyan]", total=None)
            holdings, _ = account.fetch_domestic_balance(cano, acnt_prdt_cd)
            #  None = 조회 실패다(빈 목록 = 진짜 보유 없음). 아래에서 함께 밝힌다.
            if holdings is None:
                domestic_query_failed = True
                holdings = []
            for item in holdings:
                qty = api.safe_int(item.get('hldg_qty'))
                buy_price = api.safe_float(item.get('pchs_avg_pric'), default=0.0)
                cur_price = api.safe_int(item.get('prpr'))
                eval_amt = api.safe_int(item.get('evlu_amt'))
                profit = api.safe_int(item.get('evlu_pfls_amt'))
                rate = api.safe_float(item.get('evlu_pfls_rt'), default=0.0)
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
        with Progress(
            SpinnerColumn(),
            TextColumn("[progress.description]{task.description}"),
            BarColumn(),
            console=config.console,
            transient=True
        ) as progress:
            progress.add_task("[cyan]해외 잔고 조회 중...[/cyan]", total=None)
            holdings = account.fetch_overseas_balance(cano, acnt_prdt_cd)
            #  None = 조회 실패다. 아래의 '매도 가능한 잔고가 없습니다'로 흘려보내면, 손으로
            #  손절하러 들어온 운영자가 **팔 것이 없다고 믿고 나간다**. 실패는 실패로 밝힌다.
            if holdings is None:
                overseas_query_failed = True
                holdings = []
            for item in holdings:
                qty = float(item.get('ovrs_cblc_qty', 0) or item.get('ord_psbl_qty', 0))
                if qty > 0:
                    pchs_avg = api.safe_float(item.get('pchs_avg_pric'), default=0.0)
                    profit = api.safe_float(item.get('frcr_evlu_pfls_amt'), default=0.0)
                    rate = api.safe_float(item.get('evlu_pfls_rt'), default=0.0)
                    cur_price = api.safe_float(item.get('ovrs_now_pric'), default=0.0)
                    
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
    if overseas_query_failed or domestic_query_failed:
        #  조회가 실패했는데 '잔고 없음'으로 안내하면, 손으로 손절하러 들어온 운영자가
        #  **팔 것이 없다고 믿고 나간다**. 목록이 반쪽인지 통째로 빈 것인지 밝힌다.
        market = "해외" if overseas_query_failed else "국내"
        config.console.print(f"\n[bold red]{market} 잔고를 조회하지 못했습니다 — '보유 없음'이 아닙니다.[/bold red]")
        config.console.print("[dim]잠시 후 다시 시도하거나 증권사 HTS에서 직접 확인하세요.[/dim]")
        utils.pause()
        return None, None, False, None, None

    if not candidates:
        config.console.print("\n[yellow]매도 가능한 잔고가 없습니다.[/yellow]")
        utils.pause()
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
    
    config.console.print()
    sel_idx = Prompt.ask("매도할 종목 번호를 입력하세요 [dim](이전: b, 메인: q)[/dim]")
    config.console.print()
    if sel_idx.lower() in ['b', 'q']: return None, None, False, None, None

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
            utils.pause()
            return None, None, False, None, None
    except ValueError:
        config.console.print("[red]숫자를 입력해주세요.[/red]")
        utils.pause()
        return None, None, False, None, None

def _create_fill_history(db_order, reason_msg):
    """체결 히스토리 생성 (DB Insert) - 모의투자 API 누락 대응용"""
    try:
        odno = str(db_order.get('odno'))
        if config.FILE_DEBUG_LEVEL == "DEBUG":
            logger.debug(f"[ORDER_DEBUG] _create_fill_history 진입: {odno}")

        # 이미 체결 내역이 있는지 확인 (중복 방지)
        # [수정] '체결' 또는 '체결(추정)' 상태가 이미 존재하는지 확인
        exists_check = False
        try:
            #  odno 는 당일 채번이라 날짜와 짝지어야 유일하다. 방금 낸 주문이므로 오늘로 좁힌다.
            _today = datetime.now().strftime('%Y-%m-%d')
            exists_check = (db_manager.db.check_trade_exists(odno, "체결", on_date=_today)
                            or db_manager.db.check_trade_exists(odno, "체결(추정)", on_date=_today))
            if config.FILE_DEBUG_LEVEL == "DEBUG":
                logger.debug(f"[ORDER_DEBUG] exists_check 결과: {exists_check}")
        except Exception as e:
            logger.error(f"[ORDER_DEBUG] check_trade_exists 오류: {e}", exc_info=True)

        if not exists_check:
            type_str = db_order.get('type', '')
            code = db_order.get('code')
            name = db_order.get('name')
            qty = int(float(db_order.get('qty', 0)))
            price = float(db_order.get('price', 0))
            
            # [추가] 시장가(0)인 경우 현재가 조회하여 대체
            if price <= 0:
                is_overseas = not (len(code) == 6 and code[0].isdigit() and code.isalnum()) if code else False
                try:
                    cp = api.get_current_price(code, is_overseas=is_overseas)
                    if cp > 0: price = float(cp)
                    else:
                        # [추가] get_current_price 실패 시 상세 데이터에서 추출
                        cp_data = api.get_current_price_data(code, is_overseas=is_overseas)
                        if cp_data and cp_data.get('rt_cd') == '0':
                            if is_overseas:
                                price = float(cp_data['output'].get('last', 0))
                            else:
                                price = float(cp_data['output'].get('stck_prpr', 0))
                except Exception: pass
            
            # [추가] None 값 안전 처리 (DB 저장 실패 방지)
            try: profit_amt = int(float(db_order.get('profit_amt') or 0))
            except Exception: profit_amt = 0
            try: profit_rate = float(db_order.get('profit_rate') or 0.0)
            except Exception: profit_rate = 0.0
            
            # [추가] snapshot 데이터 타입 안전 처리
            snapshot_data = db_order.get('snapshot')
            if isinstance(snapshot_data, dict):
                snapshot_data = json.dumps(snapshot_data, ensure_ascii=False)
            
            if config.FILE_DEBUG_LEVEL == "DEBUG":
                logger.debug(f"[ORDER_DEBUG] insert_trade 호출 시도: {odno}")

            # [수정] 큐 시스템 적용으로 단순 호출로 변경
            db_manager.db.insert_trade(
                type_str, 
                code, 
                name, 
                qty, 
                price, 
                odno, 
                order_status="체결(추정)", 
                reason=f"체결 확인 ({reason_msg})",
                custom_time=datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                snapshot=snapshot_data,
                score=db_order.get('strategy_score', 0),
                profit_amt=profit_amt,
                profit_rate=profit_rate,
                stop_loss_rate=float(db_order.get('stop_loss_rate', 0.0))
            )
            if config.FILE_DEBUG_LEVEL == "DEBUG":
                logger.debug(f"[ORDER_DEBUG] 체결 히스토리 생성 완료: {odno} (체결(추정))")
            return price
        else:
            if config.FILE_DEBUG_LEVEL == "DEBUG":
                logger.debug(f"[ORDER_DEBUG] 이미 체결 내역 존재하여 생성 스킵: {odno}")
            return float(db_order.get('price', 0))
    except Exception as e:
        logger.error(f"[ORDER_DEBUG] 체결 히스토리 생성 실패: {e}", exc_info=True)
        return float(db_order.get('price', 0)) if db_order else 0.0
            
def _show_order_book(code, name, is_overseas, levels=5):
    """간단한 호가창 출력 (5호가)"""
    with Progress(
        SpinnerColumn(),
        TextColumn("[cyan]호가창 데이터 조회 중...[/cyan]"),
        console=config.console,
        transient=True
    ) as progress:
        progress.add_task("order_book")
        res = api.get_order_book(code, is_overseas)

    if not res or res.get('rt_cd') != '0':
        config.console.print("[yellow]호가창 데이터를 불러오지 못했습니다.[/yellow]")
        return

    out1 = res.get('output1', {})
    
    ask_prices, ask_vols = [], []
    bid_prices, bid_vols = [], []
    
    if is_overseas:
        for i in range(levels, 0, -1):
            ask_prices.append(float(out1.get(f'pask{i}', 0)))
            ask_vols.append(int(float(out1.get(f'vask{i}', 0))))
        for i in range(1, levels + 1):
            bid_prices.append(float(out1.get(f'pbid{i}', 0)))
            bid_vols.append(int(float(out1.get(f'vbid{i}', 0))))
    else:
        for i in range(levels, 0, -1):
            ask_prices.append(float(out1.get(f'askp{i}', 0)))
            ask_vols.append(int(float(out1.get(f'askp_rsqn{i}', 0))))
        for i in range(1, levels + 1):
            bid_prices.append(float(out1.get(f'bidp{i}', 0)))
            bid_vols.append(int(float(out1.get(f'bidp_rsqn{i}', 0))))

    table = Table(title=f"📊 {name} 호가창 (상하 {levels}호가)", box=box.SIMPLE_HEAD, header_style="dim", border_style="dim")
    table.add_column("매도잔량", justify="right", style="blue", width=15)
    table.add_column("호가", justify="center", style="bold", width=15)
    table.add_column("매수잔량", justify="right", style="red", width=15)

    has_data = False
    for p, v in zip(ask_prices, ask_vols):
        if p > 0:
            has_data = True
            p_str = f"${p:,.2f}" if is_overseas else f"{int(p):,}"
            table.add_row(f"{v:,}", f"[blue]{p_str}[/blue]", "")
            
    for p, v in zip(bid_prices, bid_vols):
        if p > 0:
            has_data = True
            p_str = f"${p:,.2f}" if is_overseas else f"{int(p):,}"
            table.add_row("", f"[red]{p_str}[/red]", f"{v:,}")

    if has_data:
        config.console.print()
        config.console.print(table)
    else:
        config.console.print("[dim]현재가 호가 데이터가 없습니다.[/dim]")

def show_open_orders():
    """모든 계좌의 미체결 내역을 조회하고 테이블로 출력하며, 선택 가능한 주문 리스트를 반환합니다."""
    
    # 계좌 목록 구성
    accounts = []
    # 1. 메인 계좌
    if config.session.cano:
        label = "실전"
        accounts.append({"cano": config.session.cano, "acnt": config.session.acnt_prdt_cd, "label": label})
    
    # 2. 자동매매 계좌 (실전 모드이고 별도 설정된 경우)
    if config.session.auto_cano and config.session.auto_cano != config.session.cano:
        accounts.append({"cano": config.session.auto_cano, "acnt": config.session.auto_acnt_prdt_cd, "label": "자동"})

    selectable_orders = []
    query_failed = []      # 조회 자체가 실패한 계좌·시장 ('없음'과 구분해 밝힌다)

    # [수정] Progress -> status 변경
    with Progress(
        SpinnerColumn(),
        TextColumn("[progress.description]{task.description}"),
        BarColumn(),
        console=config.console,
        transient=True
    ) as progress:
        progress.add_task("[cyan]전체 계좌 미체결 내역 조회 중...[/cyan]", total=None)
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
                #  None = 조회 실패다. '미체결 없음'으로 보여 주면 운영자가 주문이 정리된
                #  줄 알고 다시 낸다 — 화면에 실패를 밝히고 목록은 비워 둔다.
                if dom_orders is None:
                    query_failed.append(f"{_fmt_account(cano, acnt)} 국내")
                    dom_orders = []

                # [추가] 모의투자 API 누락 대응: DB에서 '접수' 상태 주문 조회하여 병합

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
                    
                    # [추가] DB 데이터 표시
                    odno_disp = _fmt_odno(order.get('odno'))
                    if order.get('_is_db_fallback'):
                        odno_disp += " [dim](DB)[/dim]"

                    table.add_row(str(idx), _fmt_account(cano, acnt), acc_disp, "[bold]국내[/]", ord_time, odno_disp, display_name, sll_buy_colored, order.get('ord_qty'), f"{api.safe_int(order.get('ord_unpr')):,.0f}", cur_price_str, rmn_qty)
                    idx += 1

                # [B] 해외 주문
                us_orders = api.get_overseas_open_orders(cano, acnt)
                if us_orders is None:
                    query_failed.append(f"{_fmt_account(cano, acnt)} 해외")
                    us_orders = []
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

                    table.add_row(str(idx), _fmt_account(cano, acnt), acc_disp, "[bold magenta]해외[/]", ord_time, _fmt_odno(order.get('odno')), display_name, sll_buy_colored, order.get('ft_ord_qty', '0'), f"${ord_unpr:,.2f}", cur_price_str, rmn_qty)
                    idx += 1

    if query_failed:
        #  실패를 먼저 밝힌다 — '미체결 없음'을 읽고 주문을 다시 내면 중복 주문이 된다.
        config.console.print(
            f"\n[yellow]⚠️ 미체결 조회에 실패한 곳이 있습니다: {', '.join(query_failed)}. "
            f"아래 목록에는 그 계좌·시장이 빠져 있습니다 — '주문 없음'이 아닙니다.[/yellow]")

    if not selectable_orders:
        if not query_failed:
            config.console.print("\n[yellow]미체결 주문 내역이 없습니다.[/yellow]")
        return []

    config.console.print(table)
    config.console.print()
    return selectable_orders


def send_order(order_type):
    # 0. 계좌 선택
    target_cano, target_acnt, acc_label = select_account()
    if not target_cano: return # 취소 시 복귀

    # 1. 타이틀 출력
    title_color = 'red' if order_type == 'buy' else 'blue'
    title_text = "매수" if order_type == 'buy' else "매도"
    config.console.print()
    config.console.print(f"[bold]주식 {title_text} 주문 (Order)[/bold]")
    config.console.print(f"주문 계좌: [bold]{_fmt_account(target_cano, target_acnt)}[/bold] ({acc_label})")
    
    # 컨텍스트 적용 (이후 모든 API 호출은 이 계좌 기준)
    with utils.AccountContext(target_cano):
        # 2. 종목 선택 (매수: 검색 / 매도: 잔고에서 선택)
        pre_selected_excd = None
        stock_info = {}
        
        if order_type == 'sell':
            # 매도 시 상세 잔고 리스트에서 선택
            res = select_stock_from_balance(target_cano, target_acnt)
            if not res or res[0] in [None, False]: return False
            stock_code, stock_name, is_overseas, pre_selected_excd, stock_info = res
        else:
            menu_items = [
                ("1", "국내 주식", "Domestic Stock"), ("2", "국내 ETF", "Domestic ETF"), 
                ("3", "미국 주식", "US Stock"), ("4", "미국 ETF", "US ETF"), ("5", "직접 입력", "Direct Input")
            ]
            choice = utils.show_menu("매수할 종목 분류", menu_items, default_choice="5")
            if choice.lower() in ['b', 'q']: return

            menu_map_dict = dict((k, v) for k, v, _ in menu_items)
            context.USER_ACTION_BREADCRUMB.append(f"[{choice}] {menu_map_dict.get(choice, '')}")

            stock_code, stock_name, is_overseas = None, None, False

            if choice == '5':
                utils.print_breadcrumb()
                raw_input = Prompt.ask("종목코드(6자리/티커) 또는 종목명 [dim](이전: b, 메인: q)[/dim]")
                config.console.print()
                if not raw_input or raw_input.lower() in ['b', 'q']: return
                
                # 간단 검색 로직
                if len(raw_input) == 6 and raw_input[0].isdigit() and raw_input.isalnum():
                    stock_code = raw_input
                    stock_name = api.get_stock_name_by_code(stock_code, False) or stock_code
                    is_overseas = False
                elif all(ord(c) < 128 for c in raw_input):
                    stock_code = raw_input.upper()
                    stock_name = api.get_stock_name_by_code(stock_code, True) or stock_code
                    is_overseas = True
                else:
                    # 한글명 검색 등은 생략하거나 utils 활용 필요하나 여기선 코드로 유도
                    config.console.print("[yellow]정확한 종목 코드를 입력해주세요.[/yellow]")
                    return
                    
                if not utils.validate_and_confirm_stock(stock_code, stock_name, is_overseas, "이 종목으로 매수를 진행하시겠습니까?"):
                    return False
            else:
                # 리스트 선택
                key_map = {"1": "stocks_kr", "2": "etfs_kr", "3": "stocks_us", "4": "etfs_us"}
                target_key = key_map.get(choice)
                stock_list = config.session.stock_data.get(target_key, [])
                
                if not stock_list:
                    config.console.print("[yellow]등록된 종목이 없습니다.[/yellow]")
                    return
                    
                idx, item = utils.search_stock_in_list(stock_list, title=f"{menu_map_dict[choice]} 목록")
                if not item: return False
                
                stock_code, stock_name = item['code'], item['name']
                is_overseas = (choice in ["3", "4"])
                context.USER_ACTION_BREADCRUMB.append(f"[종목선택] {stock_name}")
        
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
                config.console.print()
                excd = found_excd if found_excd else Prompt.ask("거래소 코드 수동 입력 (NAS/NYS/AMS)", default="NAS").upper()
                config.console.print()
            config.console.print(f"선택 종목: [bold cyan]{stock_name} ({stock_code})[/bold cyan] (거래소: {excd})")
        else:
            config.console.print(f"선택 종목: [bold cyan]{stock_name} ({stock_code})[/bold cyan]")

        # 4. 현재가 및 최대 수량 조회
        curr_price = api.get_current_price(stock_code, is_overseas)
        
        if curr_price > 0:
            price_fmt = f"${curr_price:,.2f}" if is_overseas else f"{int(curr_price):,}원"
            config.console.print(f"\n[bold green]현재가: {price_fmt}[/bold green]")
            
            if "PYTEST_CURRENT_TEST" not in os.environ:
                analysis.print_table("", [(stock_name, stock_code)], is_overseas=is_overseas)
                config.console.print()

        default_qty = "1"
        max_qty = 0
        if order_type == 'buy':
            if curr_price > 0:
                if is_overseas:
                    max_qty = api.fetch_overseas_buyable_quantity(stock_code, curr_price, excd)
                else:
                    max_qty = api.fetch_buyable_quantity(stock_code, int(curr_price))
                    
                # [추가] API가 매수 가능 수량을 0으로 반환하거나 실패한 경우, 로컬 예수금 데이터로 Fallback 계산
                if max_qty <= 0:
                    dep_res = api.get_deposit_balance(target_cano, target_acnt, skip_balance_check=True)
                    if dep_res:
                        ord_psbl_amt = dep_res.get('order_possible', 0)
                        if ord_psbl_amt == 0:
                            ord_psbl_amt = dep_res.get('d2_deposit', 0)
                            
                        if ord_psbl_amt > 0 and curr_price > 0:
                            # 보수적으로 수수료(약 0.015%)를 고려하여 99.8%만 매수 가능 금액으로 산정
                            max_qty = int((ord_psbl_amt * 0.998) / curr_price)
                
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
                
            # [추가] 잔고 API 실패 시 기존 선택된 잔고(stock_info) 수량으로 Fallback
            #  (fetch_sellable_quantity는 조회 실패를 None으로 돌려준다 — 0과 구분된다)
            if (max_qty is None or max_qty <= 0) and stock_info and stock_info.get('qty', 0) > 0:
                max_qty = int(stock_info['qty'])
            if max_qty is None:
                max_qty = 0
            
            if max_qty > 0:
                default_qty = str(max_qty)
                config.console.print(f"[blue]보유 잔고: {max_qty}주 매도 가능[/blue]")
            else:
                config.console.print("[yellow]매도 가능 수량이 0입니다.[/yellow]")

        # 5. 수량 및 단가 입력
        config.console.print()
        qty = Prompt.ask(f"[{title_color}]{title_text} 수량(주)[/] [dim](이전: b, 메인: q)[/dim]", default=default_qty)
        if qty.lower() in ['b', 'q']: return False
        context.USER_ACTION_BREADCRUMB.append(f"[수량] {qty}")
        
        qty = qty.replace(',', '').replace('주', '').strip()
        # [수정] 종전 검증은 float()이었는데 이후 코드는 int(qty)를 쓴다. '1.5'를 넣으면
        #  검증을 통과해 주문이 나간 뒤 int()에서 터져, 텔레그램 알림·트레일링 정리·
        #  예약 일괄취소가 통째로 건너뛰고 "통신/시스템 에러"만 뜬다. 여기서 정수로 확정한다.
        try:
            qty_int = int(float(qty))
        except ValueError:
            config.console.print("[red]수량은 숫자만 입력 가능합니다.[/red]")
            return False
        if qty_int <= 0:
            config.console.print("[red]수량은 1주 이상이어야 합니다.[/red]")
            return False
        if float(qty) != qty_int:
            config.console.print(f"[yellow]소수점 수량은 지원하지 않아 {qty_int}주로 처리합니다.[/yellow]")
        qty = str(qty_int)

        unit = "달러" if is_overseas else "원"
        price_prompt = f"[{title_color}]{title_text} 단가({unit})[/] [dim]0 입력 시 시장가(현재가), 이전: b, 메인: q[/dim]"
        price = Prompt.ask(price_prompt, default="0")
        config.console.print()
        if price.lower() in ['b', 'q']: return False
        context.USER_ACTION_BREADCRUMB.append(f"[단가] {price}")
        if is_overseas and not price: config.console.print("[red]가격을 입력해야 합니다.[/red]"); return
        
        price = price.replace(',', '').replace('$', '').replace('원', '').strip()
        try:
            float(price)
        except ValueError:
            config.console.print("[red]단가는 숫자만 입력 가능합니다.[/red]")
            return False

        # 6. 가격 처리 및 주문 구분 설정
        ord_dvsn = "00"
        calc_price = 0
        display_price = ""
        is_market_order = (price == "0")
        ord_dvsn_name = "정규장"

        if is_overseas:
            # [Fix 2026-09-04] 세션 판별을 api.us_session_phase() 하나로 모은다.
            #  종전에는 여기서 UTC→ET 서머타임 계산과 구간 판정을 직접 했다. api 쪽
            #  us_session_phase / now_us_eastern 의 주석이 "trading.py 의 주문 세션
            #  판별과 동일 규칙"이라고 적고 있었지만 실제로는 별개 구현이었고,
            #  결정적으로 **휴장일을 보지 않았다**(api 는 XNYS 달력을 본다).
            #  실측: 2026-11-26 추수감사절 ET 10:00 → 여기서는 '정규장'(ord_dvsn=00),
            #        2026-09-05 토요일 KST 낮 → '데이마켓'. 둘 다 api 는 '휴장'이다.
            #  잘못된 세션 코드로 나간 주문은 거부되는데, 화면은 '정규장 접수'라고
            #  말하므로 왜 안 됐는지를 사람이 알 수 없다.
            phase = api.us_session_phase()
            _PHASE_ORD = {
                'pre':     ("32", "프리마켓"),
                'regular': ("00", "정규장"),
                'after':   ("34", "애프터마켓"),
                'day':     ("31", "데이마켓(주간거래)"),
            }
            now_et = api.now_us_eastern()
            if phase == 'closed':
                config.console.print(
                    f"\n[bold yellow]⚠️ 미국 시장 휴장/폐장 구간입니다 "
                    f"[dim](현지시각 {now_et.strftime('%m-%d %H:%M')} ET)[/dim][/bold yellow]")
                config.console.print(
                    "[dim]주문을 보내면 증권사가 거부할 가능성이 높습니다.[/dim]")
                utils.print_breadcrumb()
                if Prompt.ask("그래도 주문을 진행하시겠습니까?",
                              choices=["y", "n"], default="n") != "y":
                    config.console.print("[yellow]주문이 취소되었습니다.[/yellow]")
                    return False
                # 사용자가 진행을 택했다 — 시간대만으로 가장 가까운 세션을 고른다.
                hm = now_et.strftime('%H%M')
                phase = ('pre' if "0400" <= hm < "0930" else
                         'regular' if "0930" <= hm < "1600" else
                         'after' if "1600" <= hm <= "2000" else 'day')
            ord_dvsn, ord_dvsn_name = _PHASE_ORD[phase]

            config.console.print(f"\n[cyan]◆ 미국 시장 시간 자동 판별: {ord_dvsn_name} [dim](현지시각: {now_et.strftime('%H:%M')} ET)[/dim][/cyan]")

            # [해외] 시장가(0) 입력 시 현재가 기준 지정가로 변환
            if is_market_order:
                if curr_price > 0:
                    if curr_price >= 1.0:
                        price = f"{curr_price:.2f}"
                    else:
                        price = f"{curr_price:.4f}"
                    
                    if ord_dvsn == "31":
                        config.console.print(f"[yellow]안내: 주간거래(데이마켓)는 시장가 주문이 불가하므로 현재가(${price}) 지정가로 자동 변환됩니다.[/yellow]")
                    else:
                        config.console.print(f"[yellow]안내: 0(시장가)을 입력하여 현재가(${price}) 기준 지정가({ord_dvsn_name})로 주문을 접수합니다.[/yellow]")
                else:
                    config.console.print("[red]오류: 현재가 정보를 가져오지 못해 0(시장가) 주문을 수행할 수 없습니다.[/red]")
                    return
            
            calc_price = float(price)
            display_price = f"${price}" + (" (현재가 자동변환)" if is_market_order else " (지정가)")
            display_price += f" [{ord_dvsn_name}]"
        else:
            # [국내] 시장가 주문 처리
            if is_market_order:
                # [Fix 2026-09-04] NXT 주문 구간 판정을 api.nxt_order_window() 하나로 모은다.
                #  같은 구간이 이 파일·예약 감시기·자동매매에 네 벌 복사돼 있었다.
                #  시세 쪽 경계(domestic_session_phase 의 nxt_pre = 08:00~09:00)를 쓰면 안 된다 —
                #  08:50~09:00 은 NXT 가 KRX 시가 단일가에 맞춰 쉬는 시간이라, 그때 주문은
                #  KRX 동시호가로 들어가고 시장가가 정상 접수된다.
                is_nxt_market = api.nxt_order_window()
                
                if curr_price == 0:
                     p = api.get_current_price(stock_code, False)
                     if p > 0: curr_price = int(p)
                     
                if is_nxt_market:
                    # 대체거래소(NXT)는 시장가 주문 미지원하므로 현재가 기준 지정가로 변경
                    ord_dvsn = "00"
                    display_price = f"{curr_price:,}원 (NXT현재가 자동변환)"
                    price = str(int(curr_price))
                    config.console.print(f"[yellow]안내: NXT장(08:00~08:50, 15:30~20:00)은 시장가 주문이 불가능하여 현재가({curr_price:,}원) 지정가로 자동 변환됩니다.[/yellow]")
                else:
                    ord_dvsn = "01"
                    display_price = "시장가(0)"
                    
                calc_price = curr_price
            else:
                ord_dvsn = "00"
                # [수정] 종전에는 int(price)가 그대로 예외를 던져(예: '50000.5') 메뉴 밖으로 튕겼다.
                #  또한 호가단위 보정이 없어 50,001원 같은 값은 거래소가 거부한다.
                #  utils.adjust_to_tick은 예약 발동 경로만 쓰고 수동 주문 경로는 안 쓰고 있었다.
                raw_price = float(price)
                #  ETF·ETN 은 호가 격자가 다르다(2,000원 이상 5원 단일) — 주권 표로
                #  반올림하면 사용자가 입력한 유효한 호가를 굳이 다른 값으로 바꾼다.
                calc_price = int(utils.adjust_to_tick(
                    raw_price, is_overseas=False,
                    is_etf=api.is_domestic_etf_etn(stock_code, stock_name)))
                if calc_price != raw_price:
                    config.console.print(f"[yellow]호가단위에 맞춰 {int(raw_price):,}원 → {calc_price:,}원으로 보정했습니다.[/yellow]")
                price = str(calc_price)
                display_price = f"{calc_price:,}원"

        # 7. 예상 금액 계산 및 확인 메시지
        total_amt = float(qty) * calc_price
        est_tag = " (예상)" if is_market_order else ""
        
        if is_overseas:
            amt_str = f"${total_amt:,.2f}{est_tag}"
        else:
            amt_str = f"{int(total_amt):,}원{est_tag}"

        market_label = "해외" if is_overseas else "국내"
        excd_info = f" (거래소: {excd})" if is_overseas else ""
        
        # [추가] 예상 손절가 계산 (매수 시)
        sl_msg = ""
        stop_loss_rate_to_save = 0.0 # [추가] DB 저장용 변수
        
        # [추가] 지표 정보 저장을 위한 변수
        calculated_score = 0
        indicator_info = {}

        if order_type == 'buy' and calc_price > 0:
            try:
                # 기본 손절률
                sl_rate = config.SELL_STRATEGY.get("STOP_LOSS_RATE", -7.0)
                
                # 개별 룰 확인
                custom_rule = db_manager.db.get_stock_strategy(stock_code)
                if custom_rule:
                    sl_rate = custom_rule.get('stop_loss', sl_rate)
                
                final_sl_rate = sl_rate
                label = "고정"

                # [수정] 차트 데이터 조회 및 지표 계산 (ATR 사용 여부와 무관하게 수행)
                df = api.get_chart_data(stock_code, is_overseas)
                if df is not None and not df.empty:
                    # [추가] 주문 단가(시장가인 경우 현재가)를 바탕으로 차트 갱신
                    indicators.apply_realtime_price(df, api.chart_overlay_price(calc_price, is_overseas))

                    ind = indicators.calculate_indicators(df)
                    indicator_info = ind
                    
                    # 점수 계산
                    custom_rule = db_manager.db.get_stock_strategy(stock_code)
                    weights = config.SCORING_WEIGHTS
                    if custom_rule and custom_rule.get('weights'):
                        try:
                            w_data = custom_rule['weights']
                            if isinstance(w_data, str): weights = json.loads(w_data)
                            elif isinstance(w_data, dict): weights = w_data
                        except Exception: pass
                    sm_flag, _ = analysis.check_smart_money_turnaround(stock_code, is_overseas)
                    score, _ = analysis.calculate_score(
                        df=df, ind=ind, weights=weights, smart_money=sm_flag
                    )
                    calculated_score = score

                    # [추가] ATR 손절 적용 (수동 매수 시에도 계산)
                    if config.SELL_STRATEGY.get("USE_ATR_STOP", True):
                        atr = ind.get('atr')
                        if atr and atr > 0:
                            atr_mult = config.SELL_STRATEGY.get("ATR_STOP_MULTIPLIER", 2.0)
                            # ATR 손절률 = -(ATR * Multiplier / Price) * 100
                            atr_sl_rate = -((atr * atr_mult) / calc_price) * 100
                            
                            final_sl_rate = atr_sl_rate
                            label = "ATR"
                            stop_loss_rate_to_save = final_sl_rate

                # 예상 손절가 (주문 단가 기준)
                sl_price = calc_price * (1 + final_sl_rate / 100)
                
                if is_overseas:
                    sl_p_str = f"${sl_price:,.2f}"
                else:
                    sl_p_str = f"{int(sl_price):,}원"
                    
                sl_msg = f"\n 예상 손절: [bold blue]{sl_p_str}[/bold blue] ({final_sl_rate:.2f}%, {label})"
            except Exception: pass

        confirm_msg = (
            f"\n[bold bright_white on {title_color}] [ {market_label} {title_text} 주문 최종 확인 ] [/]\n"
            f" 종목: [bold]{stock_name} ({stock_code})[/bold]{excd_info}\n"
            f" 수량: [bold]{qty}주[/bold]\n"
            f" 단가: [bold]{display_price}[/bold]\n"
            f" 총액: [bold]{amt_str}[/bold]"
            f"{sl_msg}\n"
        )
        config.console.print(Panel(confirm_msg, expand=False, width=60))
        
        config.console.print()
        ans = Prompt.ask("위 내용으로 주문을 전송하시겠습니까?", choices=["y", "n"], default="n")
        config.console.print()
        if ans != "y":
            config.console.print("[yellow]주문이 취소되었습니다.[/yellow]")
            return None

        # 8. 주문 전송
        market_api_param = "overseas" if is_overseas else "domestic"
        
        if config.FILE_DEBUG_LEVEL == "DEBUG":
            logger.debug(f"REQ (Order-{market_label}) | {order_type} | {stock_code} {qty}ea")

        logger.info(f"운영자 실행: {' - '.join(context.USER_ACTION_BREADCRUMB)}")

        try:
            # [제한 정리 기준] 매수는 **발주 전 보유수량**을 먼저 잡아 둔다. 수동 매수를
            #  발주 즉시 제한 종목에 넣고 미체결이면 자동 해제하는데, 그 판정이 '잔고 > 0'
            #  이면 **이미 들고 있던 종목**을 추가 매수했다가 취소한 경우 기존 보유분 때문에
            #  제한이 영원히 남는다 = 시스템이 자기 포지션의 손절을 멈춘다.
            pre_hold_qty = None
            if order_type == 'buy':
                try:
                    pre_hold_qty = auto_trade.current_holding_qty(
                        stock_code, target_cano, target_acnt, is_overseas)
                except Exception as e:
                    logger.debug(f"[수동주문] 발주 전 보유수량 조회 실패({stock_code}): {e}")

            result = None
            # [수정] 단일 API 호출이므로 status 사용
            with Progress(
                SpinnerColumn(),
                TextColumn("[progress.description]{task.description}"),
                BarColumn(),
                console=config.console,
                transient=True
            ) as progress:
                progress.add_task("[cyan]주문 전송 중...[/cyan]", total=None)
                result = api.place_order(market_api_param, order_type, stock_code, qty, price, ord_dvsn, exchange_code=excd)
            
            if result['rt_cd'] == '0':
                # [Fix 2026-09-05] 종전엔 ODNO 가 비면 KRX_FWDG_ORD_ORGNO 로 대신했다.
                #  그 값은 **한국거래소 전송 주문 조직번호(지점 코드)** 라 주문번호가 아니고,
                #  같은 지점의 모든 주문이 같은 값을 갖는다. trades.odno 는 체결 대사의
                #  유일 키라(get_trade_by_odno) 그 값이 들어가면 다른 주문의 접수 행을
                #  물어 온다 — 손절률·점수·사유가 남의 것으로 상속된다.
                #  api.place_order 가 rt_cd='0' 이면 ODNO 를 보장한다(주문번호 불변식).
                odno = str((result.get('output') or {}).get('ODNO') or '').strip()
                
                # [수정] Race Condition 방지를 위해 DB 저장부터 최우선으로 실행
                profit_amt = 0
                profit_rate = 0.0
                if order_type == 'sell' and stock_info:
                    try:
                        buy_price = float(stock_info.get('buy_price', 0))
                        if buy_price > 0:
                            est_sell_amt = float(qty) * calc_price
                            est_buy_amt = float(qty) * buy_price
                            profit_amt = int(est_sell_amt - est_buy_amt)
                            profit_rate = ((calc_price - buy_price) / buy_price) * 100
                    except Exception: pass
                
                # [수정] 종전엔 int(qty) >= int(max_qty)를 두 곳에서 그때그때 계산했다.
                #  매도가능수량 조회가 실패해 max_qty가 0이면 이 비교는 **항상 참**이라,
                #  부분 매도인데도 전량으로 보고 트레일링 앵커를 지우고 예약 매도를 일괄 취소했다.
                #  '수량을 모른다'와 '전량이다'는 다르다 — 모르면 전량으로 단정하지 않는다.
                is_full_sell = (order_type == 'sell' and max_qty > 0 and int(qty) >= int(max_qty))

                t_type = "매수" if order_type == 'buy' else "매도"
                snapshot = analysis.get_snapshot(stock_code, is_overseas=is_overseas)
                
                db_manager.db.insert_trade(f"{t_type}(수동)", stock_code, stock_name, qty, price, odno, snapshot=snapshot, reason="사용자 수동 주문", profit_amt=profit_amt, profit_rate=profit_rate, stop_loss_rate=stop_loss_rate_to_save, score=calculated_score)
                
                config.console.print(f"[bold green]주문 성공[/bold green] (주문번호: {odno})")
                # [추가] AutoTrader에 주문 상태 등록 (중복 매매 방지)
                trader = auto_trade.AutoTrader()
                # [수정] OrderManager를 통해 등록 (리팩토링 대응)
                if hasattr(trader, 'order_manager'):
                    # [추가] 매도는 발주 직전 보유수량(max_qty=매도가능수량)을 함께 등록하여
                    #  모의투자에서 부분매도 체결을 잔고 감소분으로 즉시 감지하게 한다.
                    sell_pre_qty = int(max_qty) if order_type == 'sell' and max_qty > 0 else None
                    trader.order_manager.register_manual_order(stock_code, odno, pre_qty=sell_pre_qty)
                
                # 텔레그램 알림
                msg = f"🚀 [수동 주문] {t_type} {stock_name} ({stock_code})\n수량: {qty}주\n단가: {display_price}"
                if total_amt > 0:
                    if is_overseas:
                        msg += f"\n금액: ${total_amt:,.2f}"
                    else:
                        msg += f"\n금액: {int(total_amt):,}원"
                        
                if order_type == 'sell':
                    msg += f"\n손익: {int(profit_amt):+,}원 ({float(profit_rate):+.2f}%)"
                    
                    if is_full_sell:
                        canceled_cnt = db_manager.db.cancel_reserved_sell_orders(target_cano, target_acnt, stock_code)
                        if canceled_cnt > 0:
                            config.console.print(f"\n[bold magenta]💡 안내: 잔고 전량 매도로 인해 대기 중이던 매도 예약 주문 {canceled_cnt}건이 자동 취소되었습니다.[/bold magenta]")
                            msg += f"\n🗑 [예약취소] 잔고 매도로 매도 예약 {canceled_cnt}건 자동 취소"
                    
                msg += f"\n주문번호: {utils.format_order_no(odno)}"
                
                if order_type == 'buy':
                    # [추가] 수동 매수는 '발주 즉시' 제한 종목으로 등록한다.
                    #  체결 감지는 모니터 주기(잔고 확인)에 의존해 지연이 있어,
                    #  그 사이 시스템 트레이딩이 해당 종목을 매도하는 타이밍 윈도우가
                    #  생긴다. 발주 시점에 등록하면 이 윈도우를 제거할 수 있다.
                    #  (미체결/취소 시에는 전량 매도 확인 로직이 제한을 자동 해제)
                    try:
                        auto_trade.add_restricted_stock(stock_code, stock_name, "수동매매", is_overseas=is_overseas, cano=target_cano, acnt=target_acnt, account_type=auto_trade._current_account_type())
                        # [추가] 미체결(취소/거부) 시 제한을 자동 정리하는 사후 추적 시작
                        auto_trade.schedule_buy_restriction_cleanup(
                            stock_code, target_cano, target_acnt, is_overseas=is_overseas,
                            pre_qty=pre_hold_qty, odno=odno)
                    except Exception as e:
                        logger.error(f"수동 매수 제한 종목 등록 오류(발주): {e}")

                    # [추가] 매수 시 기존 예약 매수 주문 자동 취소 (비중 중복 방어)
                    canceled_cnt = db_manager.db.cancel_reserved_buy_orders(target_cano, target_acnt, stock_code)
                    if canceled_cnt > 0:
                        config.console.print(f"\n[bold magenta]💡 안내: 신규 매수 진행으로 인해 대기 중이던 매수 예약 주문 {canceled_cnt}건이 자동 취소되었습니다.[/bold magenta]")
                        msg += f"\n🗑 [예약취소] 신규 매수로 매수 예약 {canceled_cnt}건 자동 취소"

                    if calculated_score > 0:
                        rsi_str = f"{indicator_info.get('rsi', 0):.1f}" if indicator_info.get('rsi') is not None else "-"
                        msg += f"\n[지표] 점수:{calculated_score}점 / RSI:{rsi_str}"

                    if stop_loss_rate_to_save != 0.0:
                        msg += f"\n[ATR 손절] {stop_loss_rate_to_save:.2f}% 설정 (ATR손절 적용)"
                
                api.send_telegram_message(msg)
                
                # 매도 시 트레일링 스탑 초기화
                # [Fix] '전량' 매도일 때만 초기화 — 부분 매도 시 앵커를 지우면 잔여 물량의
                #  샹들리에 TS가 현재가부터 다시 시작해 감시가 느슨해지므로 앵커를 보존한다
                if is_full_sell:
                    db_manager.db.delete_trailing_stop(stock_code)
                    with trader._lock:
                        trader.trailing_stop_cache.pop(stock_code, None)
                
                # [추가] 매수 시 트레일링 스탑 감시 시작가 설정
                elif order_type == 'buy':
                    init_price = float(calc_price) # calc_price는 시장가일 경우 현재가로 이미 계산됨
                    if init_price > 0:
                        db_manager.db.update_highest_price(stock_code, init_price)
                        # AutoTrader 캐시도 갱신 (실행 중일 경우)
                        with trader._lock:
                            trader.trailing_stop_cache[stock_code] = init_price
                        config.console.print(f"[dim green]트레일링 스탑 감시 시작가 설정: {init_price:,.0f}원[/dim green]")
                
                # 체결 감시 및 미체결 조회
                auto_trade.ConclusionMonitor().check_now()
            else:
                msg1 = result.get('msg1', '알 수 없는 오류')
                config.console.print(f"[bold red]주문 실패: {msg1} (Code: {result.get('msg_cd')})[/bold red]")
                if "장운영일자가" in msg1: config.console.print("[yellow]오늘은 휴장일이거나 주문 가능한 시간이 아닐 수 있습니다.[/yellow]")
                
        except Exception as e:
            config.console.print(f"[bold red]통신/시스템 에러: {str(e)}[/bold red]")

def modify_order():
    config.console.print()
    config.console.print("[bold]통합 정정/취소 주문 (Modify/Cancel Order)[/bold]")
    # config.console.print(f"주문 계좌: [bold]{target_cano}-{target_acnt}[/bold] ({acc_label})") # 계좌 선택 제거로 주석 처리

    # [추가] 메뉴 진입 시점 로깅 (미체결 내역이 없어도 기록 남기기 위함)
    logger.info(f"운영자 실행: {' - '.join(context.USER_ACTION_BREADCRUMB)}")

    # [수정] 공통 함수 show_open_orders()를 사용하여 미체결 내역 조회 및 출력
    selectable_orders = show_open_orders()

    if not selectable_orders:
        return

    # =========================================================================
    # 4. 선택 및 분기 처리
    # =========================================================================
    def disp_func(i, o):
        name = o.get('prdt_name')
        code = o.get('pdno')
        origin = o.get('_origin')
        qty = o.get('ord_qty') if origin == 'KR' else o.get('ft_ord_qty')
        return f"[{i+1}] {name}({code}) | 수량: {qty}"

    idx, target_order = utils.search_stock_in_list(selectable_orders, title="정정/취소할 주문 선택", display_func=disp_func, hide_list=True, number_only=True)
    if not target_order: return False

    # [수정] 주문선택 표시를 긴 주문번호 대신 종목명(코드)로
    context.USER_ACTION_BREADCRUMB.append(f"[주문선택] {target_order.get('prdt_name')} ({target_order.get('pdno')})")
    origin = target_order['_origin']
    
    # [추가] 선택된 주문의 계좌 정보 추출
    acc_info = target_order.get('_account', {})
    target_cano = acc_info.get('cano') or config.session.cano
    target_acnt = acc_info.get('acnt') or config.session.acnt_prdt_cd
    acc_label = acc_info.get('label', '메인')
    
    config.console.print(f"주문 계좌: [bold]{_fmt_account(target_cano, target_acnt)}[/bold] ({acc_label})")
    
    config.console.print(f"\n[bold cyan]선택된 주문: {target_order.get('prdt_name')} ({origin})[/bold cyan]")
    menu_items = [("1", "정정", "Modify"), ("2", "취소", "Cancel")]
    action = utils.show_menu(f"작업 선택", menu_items, default_choice="1")
    if action.lower() in ['b', 'q']: return False
    
    action_map = {"1": "정정", "2": "취소"}
    if action in action_map: context.USER_ACTION_BREADCRUMB.append(f"[{action}] {action_map[action]}")

    # 공통 변수 추출
    org_odno = target_order.get('odno')
    pdno = target_order.get('pdno')
    prdt_name = target_order.get('prdt_name')
    is_overseas = (origin == 'US')
    market = "overseas" if is_overseas else "domestic"
    action_name = "정정" if action == "1" else "취소"
    
    # [수정] 매수/매도 구분 식별: API에 의존하지 않고 DB의 원본 주문에서 우선 추출
    org_trade_info = db_manager.db.get_trade_by_odno(org_odno)
    sb_label = ""
    if org_trade_info:
        t_type = org_trade_info.get('type', '')
        if "매도" in t_type or "sell" in t_type.lower(): sb_label = "매도"
        elif "매수" in t_type or "buy" in t_type.lower(): sb_label = "매수"
        
    if not sb_label:
        sb_cd = target_order.get('sll_buy_dvsn_cd')
        sb_name = target_order.get('sll_buy_dvsn_cd_name', '')
        sb_label = "매수" if sb_cd == '02' or "매수" in sb_name else ("매도" if sb_cd == '01' or "매도" in sb_name else "")
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
        config.console.print()
        qty = Prompt.ask(f"[magenta]정정 수량[/] (최대 {target_rmn}주, 0: 전량) [dim](이전: b, 메인: q)[/dim]", default="0")
        if qty.lower() in ['b', 'q']: return False
        context.USER_ACTION_BREADCRUMB.append(f"[수량] {qty}")
        
        price_prompt = "[magenta]정정 단가($)[/]" if is_overseas else "[magenta]정정 단가[/] (0: 시장가)"
        price = Prompt.ask(f"{price_prompt} [dim](이전: b, 메인: q)[/dim]", default="0")
        config.console.print()
        if price.lower() in ['b', 'q']: return False
        context.USER_ACTION_BREADCRUMB.append(f"[단가] {price}")
        if is_overseas and not price: 
            config.console.print("[red]가격 입력 필요[/]"); return
    else: # 취소
        rvse_cncl_dvsn_cd = "02"
        config.console.print()
        qty = Prompt.ask(f"[magenta]취소 수량[/] (최대 {target_rmn}주, 0: 전량) [dim](이전: b, 메인: q)[/dim]", default="0")
        config.console.print()
        if qty.lower() in ['b', 'q']: return False
        context.USER_ACTION_BREADCRUMB.append(f"[수량] {qty}")
        price = "0"

    qty = qty.replace(',', '').replace('주', '').strip()
    price = price.replace(',', '').replace('$', '').replace('원', '').strip()
    
    try:
        float(qty)
    except ValueError:
        config.console.print("[red]수량은 숫자만 입력 가능합니다.[/red]")
        return False
    try:
        float(price)
    except ValueError:
        config.console.print("[red]단가는 숫자만 입력 가능합니다.[/red]")
        return False
        
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
                except Exception: pass
            
            if calc_price > 0:
                total_amt = float(final_qty) * calc_price
                est_tag = " (예상)" if price == "0" else ""
                
                if is_overseas:
                    amt_str = f"${total_amt:,.2f}{est_tag}"
                else:
                    amt_str = f"{int(total_amt):,}원{est_tag}"
                    
                amt_msg = f" 총액: [bold]{amt_str}[/bold]\n"
        except Exception: pass

    nation_str = "해외" if is_overseas else "국내"
    excd_info = f" (거래소: {target_excd})" if is_overseas and target_excd else ""
    
    confirm_msg = (
        f"\n[bold bright_white on magenta] [ {nation_str} 주문 {action_name} 최종 확인 ] [/]\n"
        f" 원주문번호: [bold]{org_odno}[/bold]\n"
        f" 종목: [bold]{prdt_name} ({pdno})[/bold]{excd_info}\n"
        f" {action_name} 수량: [bold]{final_qty}주[/bold]\n"
        f" {action_name} 단가: [bold]{display_price}[/bold]\n"
        f"{amt_msg}"
    )
    config.console.print(Panel(confirm_msg, expand=False, width=60))
    config.console.print()
    ans = Prompt.ask("진행하시겠습니까?", choices=["y", "n"], default="n")
    config.console.print()
    if ans != "y": return False

    api_action = "revise" if action == "1" else "cancel"
    ord_dvsn = "00"
    req_qty = final_qty

    if not is_overseas:
        # 발주 화면과 같은 판정을 쓴다(api.nxt_order_window). 두 화면이 서로 다른
        #  NXT 구간을 쓰면, 낼 때는 지정가로 변환됐던 주문이 정정할 때 시장가로 나간다.
        is_nxt_market = api.nxt_order_window()

        if price == "0":
            if is_nxt_market:
                ord_dvsn = "00"
                try:
                    p = api.get_current_price(pdno, is_overseas=False)
                    if p > 0: price = str(int(p))
                except Exception: pass
                config.console.print(f"[yellow]안내: NXT장(08:00~08:50, 15:30~20:00)은 시장가 정정이 불가능하여 현재가({price}원) 지정가로 자동 변환됩니다.[/yellow]")
            else:
                ord_dvsn = "01"
        else:
            ord_dvsn = "00"
            
        # KIS는 0=전량정정 sentinel을 쓰지만, 토스 정정은 수량 필수(0/None이면 '수량 유효하지 않음' 오류)
        # → 토스는 항상 실제 수량(final_qty)을 명시한다.
        if config.session.is_toss:
            req_qty = int(final_qty)
        else:
            req_qty = 0 if qty == "0" or qty == target_rmn else int(final_qty)

    if config.FILE_DEBUG_LEVEL == "DEBUG":
        logger.debug(f"REQ (Modify-{origin}) | {action_name}")

    logger.info(f"운영자 실행: {' - '.join(context.USER_ACTION_BREADCRUMB)}")

    # 컨텍스트 적용 (선택된 주문의 계좌 사용)
    with utils.AccountContext(target_cano):
        try:
            res_json = None
            # [수정] 단일 API 호출이므로 status 사용
            with Progress(
                SpinnerColumn(),
                TextColumn("[progress.description]{task.description}"),
                BarColumn(),
                console=config.console,
                transient=True
            ) as progress:
                progress.add_task(f"[cyan]주문 {action_name} 요청 전송 중...[/cyan]", total=None)
                res_json = api.revise_cancel_order(market, api_action, org_odno, pdno, req_qty, price, rvse_cncl_dvsn_cd, ord_dvsn, exchange_code=target_excd)
            
            if res_json['rt_cd'] == '0':
                # [Fix 2026-09-05] 지점 코드(KRX_FWDG_ORD_ORGNO) 폴백 제거 — 위 신규
                #  주문과 같은 이유다. 아래 두 줄짜리 재확인도 같은 값을 다시 읽는
                #  무동작이었다. api.revise_cancel_order 가 주문번호를 보장한다.
                odno = str((res_json.get('output') or {}).get('ODNO') or '').strip()
                
                # [수정] DB 저장을 가장 최우선으로 실행하여 Race Condition 원천 차단
                org_trade = db_manager.db.get_trade_by_odno(org_odno)
                profit_amt = 0
                profit_rate = 0.0
                inherited_score = 0
                inherited_sl_rate = 0.0
                inherited_snapshot = None
                
                if org_trade:
                    inherited_score = org_trade.get('strategy_score', 0)
                    inherited_sl_rate = org_trade.get('stop_loss_rate', 0.0)
                    inherited_snapshot = org_trade.get('snapshot')
                    # 기본적으로 기존 수익 정보 상속
                    profit_amt = org_trade.get('profit_amt') or 0
                    profit_rate = org_trade.get('profit_rate') or 0.0

                db_manager.db.insert_trade(
                    f"{full_action_name}(수동)", pdno, prdt_name, final_qty, price, odno, 
                    org_odno=org_odno, reason=f"사용자 {action_name}", order_status=action_name,
                    profit_amt=profit_amt, profit_rate=profit_rate, score=inherited_score, 
                    stop_loss_rate=inherited_sl_rate, snapshot=inherited_snapshot
                )

                config.console.print(f"[bold green]접수 완료 (번호: {odno})[/]")
                
                msg = f"🚀 [수동 {full_action_name}] {prdt_name} ({pdno})\n수량: {final_qty}주\n단가: {display_price}"
                if action == "1":
                    try:
                        c_price = float(price) if price != "0" else float(api.get_current_price(pdno, is_overseas) or 0)
                        if c_price > 0:
                            t_amt = float(final_qty) * c_price
                            if is_overseas: msg += f"\n금액: ${t_amt:,.2f}"
                            else: msg += f"\n금액: {int(t_amt):,}원"
                    except Exception: pass
                msg += f"\n주문번호: {utils.format_order_no(odno)}"

                # 매도 정정일 경우 새로운 가격으로 예상 손익 재계산 시도
                if "매도" in full_action_name and action == "1":
                    try:
                        buy_price = 0
                        h_list, _ = api.get_domestic_balance(target_cano, target_acnt)
                        if h_list:
                            for h in h_list:
                                if h['pdno'] == pdno:
                                    buy_price = api.safe_float(h.get('pchs_avg_pric'), default=0.0)
                                    break
                        if buy_price > 0:
                            c_price = float(price) if price != "0" else float(api.get_current_price(pdno, is_overseas) or 0)
                            if c_price > 0:
                                est_sell_amt = float(final_qty) * c_price
                                est_buy_amt = float(final_qty) * buy_price
                                profit_amt = int(est_sell_amt - est_buy_amt)
                                profit_rate = ((c_price - buy_price) / buy_price) * 100
                                # [추가] 재계산된 손익을 DB에 업데이트
                                db_manager.db.update_trade(odno, profit_amt=profit_amt, profit_rate=profit_rate)
                                msg += f"\n예상손익: {int(profit_amt):+,}원 ({profit_rate:+.2f}%)"
                    except Exception: pass

                api.send_telegram_message(msg)
                
                # [추가] DB 비동기 저장 시간 확보를 위해 딜레이 추가 (Race Condition 원천 방지)
                time.sleep(0.5)
                auto_trade.ConclusionMonitor().check_now()
            else:
                msg_cd = res_json.get('msg_cd')
                err_msg = res_json.get('msg1')
                config.console.print(f"[red]실패: {err_msg}[/]")
        except Exception as e:
            config.console.print(f"[red]에러: {e}[/]")

def _rsv_trivial_sub(cond_type, value):
    """이 서브조건이 **언제나 참**이면 사유를 돌려준다 (아니면 None).

    [왜 필요한가 · 2026-09-06] 단일 조건 경로에는 '등록 즉시 발동'을 되묻는 가드가 있다
     (_rsv_immediate_trigger). 그런데 복합(AND) 서브조건 경로에는 그것이 통째로 없었고,
     범위 검사도 없었다. 그래서 이런 입력이 그대로 등록된다:
       · 목표 퀀트 점수 0 + '점수 이상'  → `score >= 0` — 언제나 참
       · 목표 RSI 0 + 'RSI 이상'        → `rsi >= 0`   — 언제나 참
       · 목표가 0 + '가격 이상'          → `price >= 0` — 언제나 참
     복합은 AND 라 이 하나로 곧장 발주되지는 않지만, **사용자가 걸었다고 믿는 조건 하나가
     조용히 사라진다.** 두 조건짜리 복합이라면 사실상 단일 조건이 되어, 의도한 것보다
     훨씬 이른 시점에 실주문이 나간다. 조건을 지우는 실수는 화면에 나타나지 않으므로
     여기서 막는다(트레일링 폭·ATR 배수 프롬프트는 이미 같은 범위 검사를 갖고 있다).
    """
    try:
        v = float(value)
    except (TypeError, ValueError):
        return None
    if cond_type == 'SCORE_UP' and v <= 0:
        return "점수는 0 이상이므로 '점수 ≥ 0'은 언제나 참입니다"
    if cond_type == 'RSI_UP' and v <= 0:
        return "RSI는 0 이상이므로 'RSI ≥ 0'은 언제나 참입니다"
    if cond_type == 'RSI_DOWN' and v >= 100:
        return "RSI는 100 이하이므로 'RSI ≤ 100'은 언제나 참입니다"
    if cond_type in ('PRICE_UP',) and v <= 0:
        return "가격은 0보다 크므로 '가격 ≥ 0'은 언제나 참입니다"
    if cond_type == 'PRICE_DOWN' and v <= 0:
        return "'가격 ≤ 0'은 영원히 발동하지 않습니다"
    return None


def _prompt_sub_condition(choice, base_price, is_overseas):
    """복합 조건의 서브 조건 1개를 대화형으로 입력받아 {type, value, _label} 반환 (취소 시 None)."""
    if choice == "1":  # SCORE
        s = Prompt.ask("목표 퀀트 점수 (예: 8.0)")
        if not s or s.lower() in ['b', 'q']: return None
        try: v = float(s.replace('점', '').strip())
        except ValueError:
            config.console.print("[red]점수는 숫자만 입력 가능합니다.[/red]"); return None
        ud = Prompt.ask("1: 점수 이상, 2: 점수 이하", choices=["1", "2"], default="1")
        t = "SCORE_UP" if ud == "1" else "SCORE_DOWN"
        trivial = _rsv_trivial_sub(t, v)
        if trivial:
            config.console.print(f"[red]{trivial} — 조건 하나가 사라집니다. 다시 입력하세요.[/red]")
            return None
        return {"type": t, "value": v, "_label": f"점수{'≥' if ud=='1' else '≤'}{v}"}
    if choice == "2":  # RSI
        s = Prompt.ask("목표 RSI (예: 35 또는 70)")
        if not s or s.lower() in ['b', 'q']: return None
        try: v = float(s.strip())
        except ValueError:
            config.console.print("[red]RSI는 숫자만 입력 가능합니다.[/red]"); return None
        ud = Prompt.ask("1: RSI 이상, 2: RSI 이하", choices=["1", "2"], default="2")
        t = "RSI_UP" if ud == "1" else "RSI_DOWN"
        trivial = _rsv_trivial_sub(t, v)
        if trivial:
            config.console.print(f"[red]{trivial} — 조건 하나가 사라집니다. 다시 입력하세요.[/red]")
            return None
        return {"type": t, "value": v, "_label": f"RSI{'≥' if ud=='1' else '≤'}{v}"}
    if choice == "3":  # EMA 위치
        p = Prompt.ask("이동평균선 (5, 20, 60, 120)", choices=["5", "20", "60", "120"], default="20")
        ud = Prompt.ask("1: 현재가가 이평선 위(상회), 2: 아래(하회)", choices=["1", "2"], default="1")
        t = "EMA_UP" if ud == "1" else "EMA_DOWN"
        return {"type": t, "value": float(p), "_label": f"{int(p)}일선{'상회' if ud=='1' else '하회'}"}
    if choice == "4":  # SMART_MONEY
        return {"type": "SMART_MONEY", "value": None, "_label": "수급전환"}
    if choice == "5":  # STATE
        # [추세추종] 역매수 상태는 USE_MEAN_REVERSION OFF 고정으로 분류 자체가 발생하지 않아
        #  예약 조건으로 걸어도 영원히 발동하지 않으므로 신규 등록 옵션에서 제외 (기존 주문 감시는 유지)
        st = Prompt.ask("1: 강매수, 2: 매수", choices=["1", "2"], default="1")
        sv = {"1": "강매수", "2": "매수"}[st]
        return {"type": "STATE", "value": sv, "_label": f"상태={sv}"}
    if choice == "6":  # PRICE
        config.console.print("[dim]  - 절대가: 50000 / 기준가 대비 %: +5%, -3%[/dim]")
        s = Prompt.ask("목표가 입력")
        if not s or s.lower() in ['b', 'q']: return None
        try:
            if '%' in s:
                pct = float(s.replace('%', '').strip())
                v = base_price * (1 + pct / 100.0)
                if not is_overseas: v = int(v)
            else:
                v = float(s.replace(',', '').replace('$', '').replace('원', '').strip())
        except ValueError:
            config.console.print("[red]목표가는 숫자만 입력 가능합니다.[/red]"); return None
        ud = Prompt.ask("1: 현재가가 목표가 이상, 2: 이하", choices=["1", "2"], default="1")
        t = "PRICE_UP" if ud == "1" else "PRICE_DOWN"
        trivial = _rsv_trivial_sub(t, v)
        if trivial:
            config.console.print(f"[red]{trivial} — 조건 하나가 사라집니다. 다시 입력하세요.[/red]")
            return None
        #  방향을 반대로 고른 실수는 '등록 즉시 참'으로 나타난다 — 단일 조건 경로와 같은
        #  가드를 여기에도 건다(그쪽은 _rsv_immediate_trigger 가 되묻는다).
        warn = _rsv_immediate_trigger("BREAKOUT" if t == "PRICE_UP" else "STOP", v, base_price)
        if warn:
            config.console.print(f"[yellow]⚠️ {warn} — 이 서브조건은 등록 시점부터 이미 참입니다."
                                 f" (기준가 {_fmt_price(base_price, is_overseas)})[/yellow]")
            if Prompt.ask("그래도 이 값으로 추가하시겠습니까?", choices=["y", "n"], default="n") != "y":
                return None
        return {"type": t, "value": v, "_label": f"가격{'≥' if ud=='1' else '≤'}{int(v) if not is_overseas else v}"}
    if choice == "7":  # TIME (지정 시각 이후)
        config.console.print("[dim]  - HHMM 형식 (예: 1500 → 15:00 이후)[/dim]")
        s = Prompt.ask("발동 기준 시각 입력 (HHMM)")
        if not s or s.lower() in ['b', 'q']: return None
        digits = "".join(filter(str.isdigit, s))[:4]
        if len(digits) != 4:
            config.console.print("[red]시각은 HHMM 4자리로 입력하세요.[/red]"); return None
        return {"type": "TIME_AFTER", "value": digits, "_label": f"시각≥{digits[:2]}:{digits[2:]}"}
    if choice == "8":  # NEW_HIGH
        nh = Prompt.ask("기준 (1: 52주 신고가, 2: 사상 최고가)", choices=["1", "2"], default="1")
        v = 250 if nh == "1" else 0
        return {"type": "NEW_HIGH", "value": v, "_label": "52주신고가" if v else "사상최고가"}
    return None

def _render_composite_box(subs):
    """복합 조건 구성 현황을 박스(Panel+Table) 형태로 출력하여 시인성을 높인다."""
    if not subs:
        config.console.print(Panel(
            "[dim]아직 추가된 조건이 없습니다. 최소 2개를 추가하세요.[/dim]",
            title="🧩 현재 복합 조건 구성", border_style="cyan", expand=False
        ))
        return
    t = Table(box=box.SIMPLE_HEAD, show_header=True, header_style="dim", border_style="dim", expand=False)
    t.add_column("#", justify="center", style="cyan", width=3)
    t.add_column("연결", justify="center", style="bold yellow", width=5)
    t.add_column("조건", justify="left")
    for i, s in enumerate(subs):
        t.add_row(str(i + 1), "" if i == 0 else "AND", s['_label'])
    config.console.print(Panel(
        t,
        title=f"🧩 현재 복합 조건 구성 ({len(subs)}개)",
        border_style="cyan", expand=False
    ))
    config.console.print("[dim]   └ 위 조건을 모두 충족(AND)하면 발동합니다.[/dim]")

def _build_composite_conditions(base_price, is_overseas):
    """복합(AND) 조건을 대화형으로 구성하여 서브조건 리스트 반환 (취소 시 None).

    일반 HTS에 없는 다중조건 결합 기능. 최소 2개 ~ 최대 5개.
    """
    subs = []
    type_menu = [
        ("0", "조건 구성 완료 (등록 진행)", "최소 2개 이상 선택 시 가능"),
        ("1", "퀀트 점수 (SCORE)", "시스템 종합 점수"),
        ("2", "RSI 지표", "RSI 수치"),
        ("3", "이평선 위치 (EMA)", "현재가의 N일선 상회/하회"),
        ("4", "수급 턴어라운드 (SMART_MONEY)", "외국인/기관 순매수 전환"),
        ("5", "시스템 상태 (STATE)", "강매수/매수 진입"),
        ("6", "지정가/가격 도달 (LIMIT)", "절대가 또는 기준가 대비 % (이상/이하)"),
        ("7", "특정 시간 도달 (TIME)", "지정 시각(HHMM) 이후"),
        ("8", "신고가 돌파 (NEW_HIGH)", "52주/사상 신고가 경신"),
    ]
    config.console.print("\n[bold cyan]◆ 복합 조건 구성 (모든 조건 동시 충족 시 발동 — AND)[/bold cyan]")
    config.console.print("[dim]※ 일반 HTS에 없는 다중조건 결합 기능입니다. 최소 2개 ~ 최대 5개.[/dim]")
    while len(subs) < 5:
        config.console.print()
        _render_composite_box(subs)
        c = utils.show_menu(f"추가할 조건 ({len(subs)}개 선택됨)", type_menu, default_choice="0" if len(subs) >= 2 else "1")
        if c.lower() == 'q':
            return None
        if c.lower() == 'b':
            if subs:
                removed = subs.pop()
                config.console.print(f"[yellow]마지막 조건({removed['_label']}) 제거됨[/yellow]")
                continue
            return None
        if c == "0":
            if len(subs) >= 2:
                break
            config.console.print("[yellow]복합 조건은 최소 2개가 필요합니다.[/yellow]")
            continue
        sub = _prompt_sub_condition(c, base_price, is_overseas)
        if sub:
            subs.append(sub)
    return subs

def _composite_summary(raw):
    """복합 조건 JSON을 사람이 읽는 'A AND B AND C' 요약 문자열로 변환 (목록 표시용)."""
    if not raw:
        return "복합조건"
    try:
        subs = json.loads(raw)
    except Exception:
        return "복합조건"
    labels = []
    for s in subs:
        t, v = s.get('type'), s.get('value')
        labels.append({
            'SMART_MONEY': '수급전환', 'STATE': f"상태={v}",
            'SCORE_UP': f"점수≥{v}", 'SCORE_DOWN': f"점수≤{v}",
            'RSI_UP': f"RSI≥{v}", 'RSI_DOWN': f"RSI≤{v}",
            'EMA_UP': f"{int(v) if v is not None else ''}일선상회", 'EMA_DOWN': f"{int(v) if v is not None else ''}일선하회",
            'PRICE_UP': f"가격≥{v}", 'PRICE_DOWN': f"가격≤{v}",
            'TIME_AFTER': f"시각≥{str(v)[:2]}:{str(v)[2:]}" if v else "시각",
            'NEW_HIGH': "52주신고가" if v else "사상최고가",
        }.get(t, str(t)))
    return " AND ".join(labels)

# ==========================================================
# 예약 주문 공용 헬퍼
#  조건 표기는 등록 확인·대기 목록·텔레그램 요약 세 곳에서 쓰인다. 종전엔 같은
#  분기가 세 번 복사돼 있어 조건을 하나 추가할 때마다 한 곳이 빠졌다.
# ==========================================================
_RSV_BACK = "__RSV_BACK__"
_RSV_QUIT = "__RSV_QUIT__"


def _condition_text(condition_type, target_price=0.0, target_time="",
                    composite_json=None, composite_labels=None, is_overseas=False):
    """예약 발동 조건을 사람이 읽는 한 줄로 변환한다 (등록·목록·텔레그램 공용)."""
    ct = condition_type or ""
    tp = target_price or 0.0

    if ct == 'TIME':
        tt = target_time or ""
        if len(tt) >= 12: return f"{tt[:4]}-{tt[4:6]}-{tt[6:8]} {tt[8:10]}:{tt[10:12]} 도달"
        if len(tt) == 4: return f"{tt[:2]}:{tt[2:4]} 도달"
        return tt or "시각"
    if 'SCORE' in ct:
        return f"점수 {tp}점 {'이상' if 'UP' in ct else '이하'}"
    if 'RSI' in ct:
        return f"RSI {tp} {'이상' if 'UP' in ct else '이하'}"
    if 'EMA' in ct:
        return f"EMA {int(tp)}일선 {'상향돌파' if 'UP' in ct else '하향이탈'}"
    if ct == 'TRAILING_BUY':
        return f"바닥 대비 {tp}% 반등"
    if ct == 'TRAILING_SELL':
        return f"고점 대비 {tp}% 하락"
    if ct == 'SMART_MONEY':
        return "외국인/기관 순매수 전환"
    if ct == 'NEW_HIGH':
        return "사상 최고가 경신" if tp == 0 else "52주 신고가 경신"
    if ct == 'ATR_BREAKOUT':
        return f"전일 종가 ± (ATR × {tp}) 돌파"
    if ct == 'HOLDING_EXIT':
        return "보유분석 청산 신호"
    if ct.startswith('STATE_'):
        return {'STATE_STRONGBUY': '강매수 진입', 'STATE_BUY': '매수 진입',
                'STATE_MR': '역매수 진입'}.get(ct, ct)
    if ct == 'COMPOSITE':
        if composite_labels:
            return " AND ".join(composite_labels)
        return _composite_summary(composite_json)
    return f"${tp:,.2f}" if is_overseas else f"{int(tp):,}원"


def _fmt_price(price, is_overseas):
    """가격 표기 통일 (해외 $, 국내 원)."""
    if price is None:
        return "-"
    return f"${price:,.2f}" if is_overseas else f"{int(price):,}원"


def _rsv_ask(prompt, **kwargs):
    """예약 등록 공용 입력. b는 '직전 단계', q는 '메인'으로 정규화한다.

    [왜] 종전에는 b도 q도 전부 '등록 취소'였다. 9단계짜리 흐름에서 오타 하나에
    계좌 선택부터 다시 시작해야 했던 것이 이 화면의 가장 큰 불편이었다.
    """
    choices = kwargs.get('choices')
    if choices:
        kwargs['choices'] = list(choices) + [c for c in ('b', 'q') if c not in choices]
    v = Prompt.ask(prompt, **kwargs)
    if v is None:
        return _RSV_BACK
    sv = str(v).strip()
    if sv.lower() == 'b': return _RSV_BACK
    if sv.lower() == 'q': return _RSV_QUIT
    return sv


def _rsv_header(state):
    """단계마다 상단에 고정으로 뿌리는 진행 요약 (지금 무엇을 예약 중인지)."""
    parts = []
    if state.get('acc_label'):
        parts.append(f"{_fmt_account(state['cano'], state['acnt'])} ({state['acc_label']})")
    if state.get('order_type'):
        parts.append("[red]매수[/red]" if state['order_type'] == 'buy' else "[blue]매도[/blue]")
    if state.get('name'):
        cur = state.get('current_price') or 0
        px = f" 현재가 {_fmt_price(cur, state.get('is_overseas'))}" if cur > 0 else ""
        parts.append(f"[bold cyan]{state['name']}({state['code']})[/bold cyan]{px}")
    if parts:
        config.console.print(f"\n[dim]예약 등록 ▸[/dim] " + " [dim]·[/dim] ".join(parts))


def _rsv_parse_price(raw, base_price, is_overseas, label="기준가", code=None, name=None):
    """목표가/주문단가 입력 문자열을 숫자로 변환한다. 실패 시 None.

    절대가와 '기준가 대비 %' 두 형식을 함께 받는다. 국내는 호가단위로 보정한다 —
    보정하지 않으면 50,001원 같은 값이 발동 시점에 거래소에서 거부된다.

    code/name 을 주면 ETF·ETN 격자(2,000원 이상 5원 단일)로 보정한다. 안 주면 주권
    표를 쓴다 — 그러면 사용자가 입력한 **유효한 ETF 호가를 다른 값으로 옮긴다**
    (23,070 → 23,050). 호출부가 종목을 알고 있으면 반드시 넘겨라.
    """
    txt = str(raw).strip()
    try:
        if '%' in txt:
            pct = float(txt.replace('%', '').strip())
            val = base_price * (1 + pct / 100.0)
        else:
            val = float(txt.replace(',', '').replace('$', '').replace('원', '').strip())
    except ValueError:
        return None
    if val <= 0:
        return None
    if not is_overseas:
        is_etf = False
        if code:
            try:
                is_etf = bool(api.is_domestic_etf_etn(code, name))
            except Exception as e:      # noqa: BLE001 - 판정 실패는 주권 표로(종전 동작)
                logger.debug(f"[예약] ETF 판정 실패({code}): {e}")
        val = float(utils.adjust_to_tick(val, is_overseas=False, is_etf=is_etf))
    return val


def _rsv_immediate_trigger(condition_type, target_price, current_price):
    """등록하자마자 발동할 조건이면 사유를 돌려준다 (아니면 None).

    [왜] BREAKOUT(상향 돌파)에 현재가보다 낮은 목표가를 넣으면 감시 첫 주기에
    바로 시장가 주문이 나간다. 방향을 반대로 적은 실수가 즉시 체결로 이어지는
    유일한 경로라, 확인 화면 앞에서 한 번 되묻는다.
    """
    if current_price <= 0 or target_price <= 0:
        return None
    if condition_type == 'BREAKOUT' and current_price >= target_price:
        return "현재가가 이미 목표가 이상입니다"
    if condition_type == 'STOP' and current_price <= target_price:
        return "현재가가 이미 목표가 이하입니다"
    return None


def _rsv_resolve_expire(choice):
    """유효기간 선택/직접입력을 YYYYMMDD로 해석한다. 잘못된 입력은 None.

    [왜 None인가] 종전에는 8자리가 아니면 무기한("20991231")으로 떨어뜨렸다.
    '1231' 같은 오타가 가장 오래 사는 주문이 되는 방향이라, 실패는 재입력으로 돌린다.
    """
    today_dt = datetime.now()
    if choice == "1": return today_dt.strftime("%Y%m%d")
    if choice == "2": return (today_dt + timedelta(days=7)).strftime("%Y%m%d")
    if choice == "3": return (today_dt + timedelta(days=30)).strftime("%Y%m%d")
    if choice == "4": return "20991231"

    digits = "".join(filter(str.isdigit, choice))
    if len(digits) != 8:
        return None
    try:
        dt = datetime.strptime(digits, "%Y%m%d")
    except ValueError:
        return None
    if dt.date() < today_dt.date():
        return None
    return digits


# 예약 발동 조건 메뉴 — 방향(매수/매도)은 앞 단계에서 이미 정해지므로 조건에서 다시 나누지 않는다.
#  종전에는 STOP/BREAKOUT, TRAILING_BUY/TRAILING_SELL처럼 같은 조건이 방향별로 쪼개져
#  아홉 개가 되었고, 그 중 절반이 현재 방향에서 못 쓰는 회색 항목이었다.
RSV_COND_ITEMS = [
    ("1", "지정가 도달 (PRICE)", "현재가가 목표가를 상향 돌파 / 하향 이탈 시"),
    ("2", "트레일링 (TRAILING)", "매수=최저점 대비 N% 반등 / 매도=최고점 대비 N% 하락"),
    ("3", "이평선 크로스 (EMA)", "주가가 특정 EMA를 상향돌파 / 하향이탈 시"),
    ("4", "신고가 돌파 (NEW_HIGH)", "52주 / 사상 신고가 경신 시 (추세추종 강세 진입)"),
    ("5", "변동성 돌파 (ATR)", "전일 종가 ± (ATR × 배수) 돌파 시"),
    ("6", "시스템 신호 (SIGNAL)", "매수=종목분석 진입 신호(수급·강매수·매수) / 매도=보유분석 청산 신호"),
    ("7", "시간 도달 (TIME)", "지정 시각(HHMM) 이후"),
    ("8", "복합 조건 (COMPOSITE)", "점수·RSI·지정가·시간 등 여러 조건 동시 충족(AND)"),
]


def _warn_if_auto_managed(state):
    """자동매매가 이미 같은 판정으로 청산하는 종목이면 중복 발주 위험을 알린다.

    [왜 막지 않고 알리기만 하나] 자동매매를 껐다 켜는 운용에서는 이 예약이 유일한
    보호막이 된다. 등록을 거부하면 그 운용이 통째로 막히므로, 판단에 필요한 사실만
    보여 주고 선택은 운용자에게 남긴다.
    """
    try:
        from modules import auto_trade
        restricted = set(auto_trade.get_restricted_stocks(state.get('cano'), state.get('acnt')) or {})
        unmanaged = auto_trade.get_unmanaged_reason(
            state['code'], state.get('name', ''), state.get('is_overseas', False),
            restricted_codes=restricted)
    except Exception as e:
        logger.debug(f"자동매매 관리 여부 확인 실패: {e}")
        return

    if unmanaged:
        config.console.print(f"[dim]  · 이 종목은 자동 매도 대상이 아닙니다 ({unmanaged}) — "
                             f"예약이 유일한 청산 경로가 됩니다.[/dim]")
        return

    config.console.print("\n[bold yellow]⚠️ 이 종목은 자동매매의 청산 대상입니다.[/bold yellow]")
    config.console.print("[yellow]  자동매매가 켜져 있으면 같은 신호에 시스템도 매도를 냅니다 — "
                         "같은 주기에 겹치면 주문이 두 번 나갈 수 있습니다.[/yellow]")
    config.console.print("[dim]  (자동매매를 끄고 운용하거나, 이 종목을 제한종목으로 두는 경우에만 권합니다.)[/dim]")


def _rsv_step_condition_value(cond_choice, order_type, state):
    """조건 종류별 세부 값을 입력받아 dict로 돌려준다. 취소 시 _RSV_BACK/_RSV_QUIT."""
    is_overseas = state['is_overseas']
    base_price = state['base_price']
    base_label = state['base_label']
    is_buy = (order_type == 'buy')

    # ---------- 1. 지정가 도달 ----------
    if cond_choice == "1":
        config.console.print("\n[cyan]◆ 발동 방향 선택[/cyan]")
        updown = _rsv_ask("1: 목표가 이상으로 상승(돌파), 2: 목표가 이하로 하락(이탈)",
                          choices=["1", "2"], default="2" if is_buy is False else "1")
        if updown in (_RSV_BACK, _RSV_QUIT): return updown
        condition_type = "BREAKOUT" if updown == "1" else "STOP"

        arrow = "이상으로 상승" if condition_type == "BREAKOUT" else "이하로 하락"
        config.console.print(f"\n[cyan]◆ 목표가 입력 — 현재가가 목표가 {arrow} 시 발동[/cyan]")
        config.console.print("[dim]  - 절대 가격: 50000 (해당 금액 도달 시 발동)[/dim]")
        config.console.print(f"[dim]  - 퍼센트(%): +5%, -3% ({base_label} 대비 설정)[/dim]")
        v = _rsv_ask("목표가 입력")
        if v in (_RSV_BACK, _RSV_QUIT): return v

        target_price = _rsv_parse_price(v, base_price, is_overseas, base_label,
                                       code=state.get('code'), name=state.get('name'))
        if target_price is None:
            config.console.print("[red]목표가는 0보다 큰 숫자 또는 '+5%' 형식으로 입력하세요.[/red]")
            return None
        if '%' in v:
            config.console.print(f"[dim] -> {base_label} 기준 계산된 목표가: {_fmt_price(target_price, is_overseas)}[/dim]")

        # [추가] 방향을 반대로 고른 실수는 '등록 즉시 발동'으로 나타난다 — 여기서 한 번 막는다.
        warn = _rsv_immediate_trigger(condition_type, target_price, state.get('current_price') or 0)
        if warn:
            config.console.print(
                f"\n[bold red]⚠️ {warn}[/bold red] "
                f"(현재가 {_fmt_price(state.get('current_price'), is_overseas)} / "
                f"목표가 {_fmt_price(target_price, is_overseas)})")
            config.console.print("[yellow]이대로 등록하면 다음 감시 주기에 곧바로 주문이 나갑니다. "
                                 "발동 방향을 반대로 고르지 않았는지 확인하세요.[/yellow]")
            if Prompt.ask("그래도 이 목표가로 등록하시겠습니까?", choices=["y", "n"], default="n") != "y":
                return None
        return {'condition_type': condition_type, 'target_price': target_price}

    # ---------- 2. 트레일링 ----------
    if cond_choice == "2":
        condition_type = "TRAILING_BUY" if is_buy else "TRAILING_SELL"
        config.console.print(f"\n[cyan]◆ {'반등' if is_buy else '하락'} 폭 설정[/cyan]")
        config.console.print(f"[dim]※ '{'최저점' if is_buy else '최고점'}'은 본 예약 주문이 등록된 시점 이후부터 "
                             f"갱신된 가장 {'낮은' if is_buy else '높은'} 가격을 의미합니다.[/dim]")
        v = _rsv_ask(f"예약 후 {'최저점 대비 추격 매수할 반등' if is_buy else '최고점 대비 매도할 하락'} 폭(%) 입력 (예: 3.0)")
        if v in (_RSV_BACK, _RSV_QUIT): return v
        try:
            pct = float(v.replace('%', '').strip())
        except ValueError:
            config.console.print("[red]폭은 숫자만 입력 가능합니다.[/red]")
            return None
        # [추가] 0 이하는 등록 즉시 발동하고, 지나치게 큰 값은 영원히 발동하지 않는다.
        if not (0 < pct <= 50):
            config.console.print("[red]폭은 0 초과 50 이하로 입력하세요. "
                                 "(0 이하는 등록 즉시 발동, 과도한 값은 사실상 미발동)[/red]")
            return None
        return {'condition_type': condition_type, 'target_price': pct}

    # ---------- 3. 이평선 크로스 ----------
    if cond_choice == "3":
        config.console.print("\n[cyan]◆ 발동 목표 이동평균선(EMA) 선택[/cyan]")
        v = _rsv_ask("이동평균선 (5, 20, 60, 120 중 입력)", choices=["5", "20", "60", "120"], default="20")
        if v in (_RSV_BACK, _RSV_QUIT): return v
        updown = _rsv_ask("발동 방향 (1: 상향 돌파 시, 2: 하향 이탈 시)",
                          choices=["1", "2"], default="1" if is_buy else "2")
        if updown in (_RSV_BACK, _RSV_QUIT): return updown
        return {'condition_type': "EMA_UP" if updown == "1" else "EMA_DOWN", 'target_price': float(v)}

    # ---------- 4. 신고가 돌파 ----------
    if cond_choice == "4":
        config.console.print("\n[cyan]◆ 신고가 돌파 기준 선택[/cyan]")
        config.console.print("[dim]  - 직전 구간 최고가를 현재가가 경신하면 발동합니다. (목표가 입력 불필요, 자동 감지)[/dim]")
        nh = _rsv_ask("기준 (1: 52주 신고가, 2: 사상 최고가)", choices=["1", "2"], default="1")
        if nh in (_RSV_BACK, _RSV_QUIT): return nh
        return {'condition_type': "NEW_HIGH", 'target_price': 250.0 if nh == "1" else 0.0}

    # ---------- 5. 변동성 돌파 (ATR) ----------
    if cond_choice == "5":
        config.console.print("\n[cyan]◆ 변동성 돌파 배수 설정[/cyan]")
        config.console.print(f"[dim]  - 전일 종가 {'+' if is_buy else '-'} (ATR × 배수)를 "
                             f"현재가가 {'상향 돌파' if is_buy else '하향 이탈'}하면 발동합니다.[/dim]")
        config.console.print("[dim]  - 배수가 작을수록 자주, 클수록 드물게 발동합니다 (통상 0.3 ~ 1.0).[/dim]")
        v = _rsv_ask("ATR 배수 입력 (예: 0.5)", default="0.5")
        if v in (_RSV_BACK, _RSV_QUIT): return v
        try:
            k = float(v.replace('배', '').strip())
        except ValueError:
            config.console.print("[red]배수는 숫자만 입력 가능합니다.[/red]")
            return None
        if not (0 < k <= 5):
            config.console.print("[red]배수는 0 초과 5 이하로 입력하세요.[/red]")
            return None
        return {'condition_type': "ATR_BREAKOUT", 'target_price': k}

    # ---------- 6. 시스템 신호 ----------
    if cond_choice == "6":
        # [추세추종] 역매수(STATE_MR)는 USE_MEAN_REVERSION OFF 고정으로 분류 자체가 발생하지 않아
        #  등록 옵션에서 제외한다 (기존 등록분 감시는 monitor가 계속 지원).
        if not is_buy:
            # [보유분석 청산] 파는 대상은 차트가 아니라 '내 포지션'이다. 매수는 포지션이 없어
            #  종목분석(classify_stock_state)밖에 볼 것이 없지만, 매도는 수익률·보유일수·최고가·
            #  반익절 이력·진입 시 ATR 손절률이 전부 있다. 자동매매가 실제 청산에 쓰는
            #  analyze_sell을 그대로 트리거로 삼는다 — 종목분석의 '매도' 상태는 이 판정이
            #  이미 품고 있는 부분집합이라 따로 두지 않는다.
            config.console.print("\n[cyan]◆ 보유분석 청산 신호 발생 시 발동[/cyan]")
            config.console.print("[dim]  - 익절 / 손절 / 본전청산(BEP) / 시간청산 / 샹들리에 트레일링 스톱 /[/dim]")
            config.console.print("[dim]    RSI 과열 / 추세이탈(점수 하락+60일선 이탈) / 매도 상태 진입[/dim]")
            config.console.print("[dim]  - 잔고 화면 [9]-2의 '상태' 컬럼에 뜨는 청산 신호와 같은 판정입니다.[/dim]")
            config.console.print("[yellow]  ※ 반익절 신호에는 발동하지 않습니다 — 예약 수량은 등록 시점에 고정이라 "
                                 "'절반만 판다'를 표현할 수 없습니다.[/yellow]")
            _warn_if_auto_managed(state)
            st = _rsv_ask("1: 이 신호로 등록", choices=["1"], default="1")
            if st in (_RSV_BACK, _RSV_QUIT): return st
            return {'condition_type': "HOLDING_EXIT", 'target_price': 0.0}

        config.console.print("\n[cyan]◆ 진입을 감지할 시스템 신호 선택[/cyan]")
        config.console.print("[dim]  - 수급 전환: 외국인/기관이 순매수로 돌아서는 신호[/dim]")
        config.console.print("[dim]  - 강매수: 슈퍼모멘텀(신고가 주도주) / 매수: 일반 매수조건[/dim]")
        st = _rsv_ask("신호 선택 (1: 수급 전환, 2: 강매수 진입, 3: 매수 진입)",
                      choices=["1", "2", "3"], default="1")
        if st in (_RSV_BACK, _RSV_QUIT): return st
        return {'condition_type': {"1": "SMART_MONEY", "2": "STATE_STRONGBUY", "3": "STATE_BUY"}[st],
                'target_price': 0.0}

    # ---------- 7. 시간 도달 ----------
    if cond_choice == "7":
        config.console.print("\n[cyan]◆ 발동 기준 시각 입력[/cyan]")
        config.console.print("[dim]  - HHMM 4자리 (예: 1500 → 15:00 이후 최초 감시 주기에 발동)[/dim]")
        config.console.print("[dim]  - 시각 조건은 가격과 무관하게 발동합니다. 주문 단가를 시장가로 두는 편이 안전합니다.[/dim]")
        v = _rsv_ask("발동 시각 (HHMM)")
        if v in (_RSV_BACK, _RSV_QUIT): return v
        digits = "".join(filter(str.isdigit, v))
        if len(digits) != 4 or not ("0000" <= digits <= "2359") or digits[2:] > "59":
            config.console.print("[red]시각은 HHMM 4자리(0000~2359)로 입력하세요.[/red]")
            return None
        return {'condition_type': "TIME", 'target_price': 0.0, 'target_time': digits}

    # ---------- 8. 복합 조건 ----------
    if cond_choice == "8":
        subs = _build_composite_conditions(base_price, is_overseas)
        if not subs:
            return _RSV_BACK
        return {
            'condition_type': "COMPOSITE",
            'target_price': 0.0,
            'composite_json': json.dumps([{"type": s["type"], "value": s.get("value")} for s in subs],
                                         ensure_ascii=False),
            'composite_labels': [s['_label'] for s in subs],
        }

    return None


def _warn_existing_reserved(code, cano, acnt, is_overseas):
    """같은 계좌·종목에 이미 걸린 예약이 있으면 먼저 보여준다.

    하나가 발동하면 나머지가 일괄 취소되므로(cancel_other_reserved_orders), 모르고 겹쳐
    걸면 의도치 않게 기존 예약 — 대개 손절 — 이 조용히 사라진다.

    [Fix 2026-09-04] 등록 마법사에만 있고 OCO 단축 경로에는 없었다. OCO 는 한 번에 두 건을
     넣으므로 겹쳐 걸 여지가 더 크다. 조회 범위도 계좌번호(cano)만 보고 있었는데, 실제
     일괄 취소는 (cano, acnt, code) 로 좁혀 지운다 — 안내와 실제 대상이 달랐다.
    """
    try:
        existing = [o for o in db_manager.db.get_pending_reserved_orders()
                    if o['code'] == code and o.get('cano') == cano and o.get('acnt') == acnt]
    except Exception as e:      # noqa: BLE001 - 안내 실패로 등록 자체를 막지는 않는다
        config.console.print(f"[dim yellow]※ 기존 예약 조회 실패({type(e).__name__}) — "
                             f"겹친 예약이 있는지 확인하지 못했습니다.[/dim yellow]")
        return
    if not existing:
        return
    config.console.print(f"\n[bold magenta]※ 이 종목에 이미 대기 중인 예약이 {len(existing)}건 있습니다. "
                         f"새 예약이 발동하면 아래 예약은 자동 취소됩니다.[/bold magenta]")
    for o in existing[:5]:
        ot = "매수" if o['order_type'] == 'buy' else "매도"
        config.console.print(
            f"[dim]   · ID {o['id']} {ot} {o['qty']}주 — "
            f"{_condition_text(o['condition_type'], o.get('target_price', 0), o.get('target_time', ''), o.get('composite_json'), is_overseas=is_overseas)}[/dim]")
    if len(existing) > 5:
        config.console.print(f"[dim]   · … 외 {len(existing) - 5}건[/dim]")


def _register_oco_orders(cano, acnt, acc_label):
    """보유 종목에 손절가·익절가를 한 번에 예약한다 (OCO).

    [왜 이게 OCO인가] 같은 종목의 예약 하나가 발동하면 나머지가 자동 일괄 취소된다
    (cancel_other_reserved_orders). 즉 손절 예약과 익절 예약을 함께 걸어 두면
    한쪽이 나가는 순간 다른 쪽이 사라지는 One-Cancels-Other가 이미 성립한다.
    이 함수는 그 두 건을 따로 아홉 단계씩 두 번 입력하지 않게 해 주는 단축 경로다.

    반환: True(등록됨) / False(취소, 방향 선택으로 복귀) / 'quit'(메인으로)
    """
    res = select_stock_from_balance(cano, acnt)
    if not res or res[0] in [None, False]:
        return False
    code, name, is_overseas, _, stock_info = res

    current_price = api.get_current_price(code, is_overseas)
    buy_price = float(stock_info.get('buy_price', 0.0) or 0.0)
    held_qty = int(stock_info.get('qty', 0) or 0)
    base_price = buy_price if buy_price > 0 else current_price
    base_label = "매입단가" if buy_price > 0 else "현재가"

    config.console.print(f"\n[bold cyan]{name} ({code})[/bold cyan] "
                         f"[dim](현재가 {_fmt_price(current_price, is_overseas)}"
                         + (f" / 매입단가 {_fmt_price(buy_price, is_overseas)}" if buy_price > 0 else "")
                         + f" / 보유 {held_qty:,}주)[/dim]")
    config.console.print("[dim]※ 손절과 익절을 각각 한 건씩 등록합니다. 한쪽이 발동하면 다른 쪽은 자동 취소됩니다.[/dim]")
    _warn_existing_reserved(code, cano, acnt, is_overseas)

    if current_price <= 0:
        config.console.print("[red]현재가를 조회하지 못해 OCO를 등록할 수 없습니다. "
                             "손절가·익절가가 이미 도달했는지 판단할 수 없습니다.[/red]")
        utils.pause()
        return False

    # ---------- 손절가 ----------
    config.console.print(f"\n[cyan]◆ 손절가 (이 가격 이하로 내려가면 매도)[/cyan]")
    config.console.print(f"[dim]  - 절대 가격 또는 {base_label} 대비 %(예: -7%)[/dim]")
    v = _rsv_ask("손절가 입력")
    if v == _RSV_QUIT: return 'quit'
    if v == _RSV_BACK: return False
    stop_price = _rsv_parse_price(v, base_price, is_overseas, base_label,
                                  code=code, name=name)
    if stop_price is None:
        config.console.print("[red]손절가는 0보다 큰 숫자 또는 '-7%' 형식으로 입력하세요.[/red]")
        utils.pause()
        return False

    # ---------- 익절가 ----------
    config.console.print(f"\n[cyan]◆ 익절가 (이 가격 이상으로 올라가면 매도)[/cyan]")
    config.console.print(f"[dim]  - 절대 가격 또는 {base_label} 대비 %(예: +15%)[/dim]")
    v = _rsv_ask("익절가 입력")
    if v == _RSV_QUIT: return 'quit'
    if v == _RSV_BACK: return False
    take_price = _rsv_parse_price(v, base_price, is_overseas, base_label,
                                  code=code, name=name)
    if take_price is None:
        config.console.print("[red]익절가는 0보다 큰 숫자 또는 '+15%' 형식으로 입력하세요.[/red]")
        utils.pause()
        return False

    # [검증] 손절가 < 현재가 < 익절가가 성립하지 않으면 등록하자마자 한쪽이 발동한다.
    problems = []
    if stop_price >= take_price:
        problems.append("손절가가 익절가보다 높거나 같습니다")
    if current_price <= stop_price:
        problems.append("현재가가 이미 손절가 이하입니다 — 등록 즉시 매도됩니다")
    if current_price >= take_price:
        problems.append("현재가가 이미 익절가 이상입니다 — 등록 즉시 매도됩니다")
    if problems:
        config.console.print()
        for msg in problems:
            config.console.print(f"[bold red]⚠️ {msg}[/bold red]")
        if Prompt.ask("그래도 등록하시겠습니까?", choices=["y", "n"], default="n") != "y":
            return False

    # ---------- 수량 ----------
    v = _rsv_ask(f"매도 수량(주) [dim](보유 {held_qty:,}주)[/dim]", default=str(held_qty))
    if v == _RSV_QUIT: return 'quit'
    if v == _RSV_BACK: return False
    try:
        qty = int(float(v.replace(',', '').replace('주', '').strip()))
    except ValueError:
        config.console.print("[red]수량은 숫자만 입력 가능합니다.[/red]")
        utils.pause()
        return False
    if qty <= 0:
        config.console.print("[red]수량은 1주 이상이어야 합니다.[/red]")
        utils.pause()
        return False
    if held_qty > 0 and qty > held_qty:
        config.console.print(f"[red]⚠️ 보유 수량({held_qty:,}주)보다 많아 전량으로 조정합니다.[/red]")
        qty = held_qty

    # ---------- 유효기간 ----------
    config.console.print(f"\n[cyan]◆ 유효 기간 (만료일) 설정[/cyan]")
    config.console.print("[dim]  - 1: 당일 / 2: 이번 주 / 3: 이번 달 / 4: 무기한 / YYYYMMDD 직접 입력[/dim]")
    v = _rsv_ask("유효 기간 선택 또는 입력", default="4")
    if v == _RSV_QUIT: return 'quit'
    if v == _RSV_BACK: return False
    expire_dt = _rsv_resolve_expire(v)
    if expire_dt is None:
        config.console.print("[red]유효 기간은 1~4 또는 오늘 이후의 YYYYMMDD 8자리로 입력하세요.[/red]")
        utils.pause()
        return False
    expire_disp = "무기한" if expire_dt == "20991231" else f"{expire_dt[:4]}-{expire_dt[4:6]}-{expire_dt[6:8]}까지"

    # ---------- 확인 ----------
    stop_gap = (stop_price - current_price) / current_price * 100
    take_gap = (take_price - current_price) / current_price * 100
    config.console.print(Panel(
        f"\n[bold bright_white on yellow] [ 손절 + 익절 동시 예약 (OCO) 최종 확인 ] [/]\n"
        f" 계좌: {_fmt_account(cano, acnt)} ({acc_label})\n"
        f" 종목: {name} ({code})\n"
        f" 현재가: {_fmt_price(current_price, is_overseas)}\n"
        f" 손절: [blue]{_fmt_price(stop_price, is_overseas)}[/blue] [dim]({stop_gap:+.2f}%)[/dim]\n"
        f" 익절: [red]{_fmt_price(take_price, is_overseas)}[/red] [dim]({take_gap:+.2f}%)[/dim]\n"
        f" 수량: {qty:,}주 (각 예약에 동일 적용, 주문은 시장가)\n"
        f" 유효: {expire_disp}\n"
        f"\n[dim]한쪽이 발동하면 나머지 한 건은 자동으로 취소됩니다.[/dim]\n",
        expand=False))

    ans = _rsv_ask("위 내용으로 두 건을 등록하시겠습니까?", choices=["y", "n"], default="n")
    if ans == _RSV_QUIT: return 'quit'
    if ans != "y":
        config.console.print("\n[yellow]예약 주문 등록이 취소되었습니다.[/yellow]")
        return False

    market = "US" if is_overseas else "KR"
    for cond_type, target in (("STOP", stop_price), ("BREAKOUT", take_price)):
        db_manager.db.insert_reserved_order(
            cano=cano, acnt=acnt, market=market, order_type="sell",
            code=code, name=name, qty=qty, order_price=0.0,
            condition_type=cond_type, target_price=target, target_time="",
            expire_dt=expire_dt, composite_json=None)

    config.console.print()
    config.console.print("[bold green]손절·익절 예약 2건이 등록되었습니다.[/bold green]")
    api.send_telegram_message(
        f"📝 [예약 등록 · OCO] {name}({code})\n"
        f"손절: {_fmt_price(stop_price, is_overseas)} ({stop_gap:+.2f}%)\n"
        f"익절: {_fmt_price(take_price, is_overseas)} ({take_gap:+.2f}%)\n"
        f"수량: {qty}주 · 시장가\n"
        f"유효: {expire_disp}\n"
        f"한쪽 발동 시 나머지는 자동 취소됩니다.")

    config.console.print()
    _print_reserved_orders_table()
    return True


def register_reserved_order():
    """예약 주문 등록 메뉴 (단계별 뒤로가기 지원).

    [설계] 계좌→방향→종목→조건→조건값→주문단가→수량→유효기간→확인의 9단계를
    인덱스로 돌린다. 각 단계에서 b는 직전 단계, q는 메인이다. 종전에는 어느 단계든
    b/q가 모두 '등록 취소'라, 마지막 유효기간에서 오타를 내면 계좌 선택부터
    아홉 단계를 다시 입력해야 했다.
    """
    state = {}
    step = 0

    # [추가] 단계마다 진입 시점의 경로(Breadcrumb) 길이를 기억해 두고, 그 단계로 되돌아오면
    #  그 지점까지 잘라낸다. 이렇게 하지 않으면 b로 계좌 선택에 돌아갔을 때 이전 선택이
    #  경로에 그대로 남아 "[2] 계좌: 자동투자 > [1] 계좌: 한투증권"처럼 두 번 찍힌다.
    base_len = len(context.USER_ACTION_BREADCRUMB)
    marks = {0: base_len}

    while True:
        if step < 0:
            context.USER_ACTION_BREADCRUMB = context.USER_ACTION_BREADCRUMB[:base_len]
            config.console.print("\n[yellow]예약 주문 등록이 취소되었습니다.[/yellow]")
            return None

        context.USER_ACTION_BREADCRUMB = context.USER_ACTION_BREADCRUMB[:marks.get(step, base_len)]

        # ---------- 0. 계좌 ----------
        if step == 0:
            config.console.print("\n[bold yellow]예약 주문 등록 (Reserve Order)[/bold yellow] [dim](1/9 계좌)[/dim]")
            cano, acnt, acc_label = select_account()
            if not cano:
                step = -1
                continue
            state.update(cano=cano, acnt=acnt, acc_label=acc_label)
            step += 1
            marks[step] = len(context.USER_ACTION_BREADCRUMB)
            continue

        # ---------- 1. 방향 ----------
        if step == 1:
            _rsv_header(state)
            config.console.print("[dim](2/9 주문 방향)[/dim]")
            menu_items = [
                ("1", "예약 매수", "Buy"),
                ("2", "예약 매도", "Sell"),
                ("3", "손절 + 익절 동시 (OCO)", "보유 종목에 손절가·익절가를 한 번에 예약"),
            ]
            choice = utils.show_menu("주문 방향", menu_items, default_choice="1")
            if choice.lower() == 'q':
                step = -1
                continue
            if choice.lower() == 'b':
                step -= 1
                continue
            if choice == "3":
                # 한쪽이 발동하면 같은 종목의 나머지 예약이 일괄 취소되는 기존 정책이
                # 그대로 OCO가 된다 — 두 건을 한 번에 거는 단축 경로다.
                context.USER_ACTION_BREADCRUMB.append("[3] 손절+익절 동시(OCO)")
                res = _register_oco_orders(state['cano'], state['acnt'], state['acc_label'])
                context.USER_ACTION_BREADCRUMB = context.USER_ACTION_BREADCRUMB[:marks[1]]
                if res == 'quit':
                    step = -1
                    continue
                if res is True:
                    return True
                continue
            new_type = "buy" if choice == "1" else "sell"
            if state.get('order_type') != new_type:
                # 방향이 바뀌면 종목 선택 근거(잔고/검색)가 달라진다 — 이후 상태를 비운다.
                for k in ('code', 'name', 'is_overseas', 'stock_info', 'current_price',
                          'buy_price', 'condition_type', 'target_price'):
                    state.pop(k, None)
            state['order_type'] = new_type
            # [복원] 재작성 과정에서 빠졌던 항목. 경로에 매수/매도가 없으면 계좌 다음이
            #  바로 종목이라, 지금 무엇을 예약 중인지 상단만 보고는 알 수 없다.
            context.USER_ACTION_BREADCRUMB.append(f"[{choice}] {'예약 매수' if new_type == 'buy' else '예약 매도'}")
            step += 1
            marks[step] = len(context.USER_ACTION_BREADCRUMB)
            continue

        # ---------- 2. 종목 ----------
        if step == 2:
            _rsv_header(state)
            config.console.print("[dim](3/9 종목 선택)[/dim]")
            if state['order_type'] == "sell":
                res = select_stock_from_balance(state['cano'], state['acnt'])
                if not res or res[0] in [None, False]:
                    step -= 1
                    continue
                code, name, is_overseas, _, stock_info = res
            else:
                code, name, is_overseas = utils.select_target_stock()
                if not code:
                    step -= 1
                    continue
                stock_info = {}

            state.update(code=code, name=name, is_overseas=is_overseas, stock_info=stock_info)

            current_price = api.get_current_price(code, is_overseas)
            buy_price = float(stock_info.get('buy_price', 0.0)) if state['order_type'] == "sell" else 0.0
            state['current_price'] = current_price
            state['buy_price'] = buy_price
            state['base_price'] = buy_price if buy_price > 0 else current_price
            state['base_label'] = "매입단가" if buy_price > 0 else "현재가"

            price_info = f"현재가: {_fmt_price(current_price, is_overseas)}"
            if buy_price > 0:
                price_info += f" / 매입단가: {_fmt_price(buy_price, is_overseas)}"
            config.console.print(f"\n선택 종목: [bold cyan]{name} ({code})[/bold cyan] [dim]({price_info})[/dim]")

            _warn_existing_reserved(code, state['cano'], state['acnt'], is_overseas)

            analysis.print_table("", [(name, code)], is_overseas=is_overseas)
            config.console.print()
            config.console.print("[bold magenta]⚠️ 안내: 한 종목에 여러 예약 주문을 설정할 수 있으나, 어느 하나라도 체결되면 "
                                 "해당 종목에 설정된 나머지 모든 예약 주문(매수/매도)은 자동으로 일괄 취소됩니다.[/bold magenta]")
            step += 1
            marks[step] = len(context.USER_ACTION_BREADCRUMB)
            continue

        # ---------- 3. 조건 종류 ----------
        if step == 3:
            _rsv_header(state)
            config.console.print("[dim](4/9 발동 조건)[/dim]")
            cond_items = RSV_COND_ITEMS
            # [수정] 6번은 방향에 따라 보는 판정기가 다르다 — 매수는 포지션이 없으므로 종목분석,
            #  매도는 포지션이 있으므로 보유분석(자동매매가 실제 청산에 쓰는 analyze_sell).
            #  종전에는 진입 신호만 있어 매도에서 통째로 잠겼다.
            side_note = None if state['order_type'] == 'buy' else (
                "[dim]※ 6번 매도 신호는 잔고 화면의 '상태'와 같은 보유분석 청산 판정입니다 "
                "(익절·손절·시간청산·트레일링스탑·추세이탈).[/dim]")
            cond_choice = utils.show_menu("예약 발동 조건", cond_items,
                                          text_before=side_note)
            if cond_choice.lower() == 'q':
                step = -1
                continue
            if cond_choice.lower() == 'b':
                step -= 1
                continue

            state['cond_choice'] = cond_choice
            cond_name = dict((k, v) for k, v, _ in cond_items).get(cond_choice, '')
            context.USER_ACTION_BREADCRUMB.append(f"[{cond_choice}] {cond_name}")
            step += 1
            marks[step] = len(context.USER_ACTION_BREADCRUMB)
            continue

        # ---------- 4. 조건 값 ----------
        if step == 4:
            _rsv_header(state)
            res = _rsv_step_condition_value(state['cond_choice'], state['order_type'], state)
            if res == _RSV_QUIT:
                step = -1
                continue
            if res == _RSV_BACK:
                step -= 1
                continue
            if res is None:
                utils.pause()
                continue
            state.pop('composite_json', None)
            state.pop('composite_labels', None)
            state.pop('target_time', None)
            state.update(res)
            step += 1
            marks[step] = len(context.USER_ACTION_BREADCRUMB)
            continue

        # ---------- 5. 주문 단가 ----------
        if step == 5:
            _rsv_header(state)
            config.console.print("[dim](6/9 주문 실행 단가)[/dim]")
            is_overseas = state['is_overseas']
            condition_type = state['condition_type']
            base_label = state['base_label']
            base_price = state['base_price']
            # 목표가 자체가 주문가로 의미 있는 조건과, 그렇지 않은 조건을 나눈다.
            is_price_target = condition_type in ("STOP", "BREAKOUT", "LIMIT")

            config.console.print("\n[cyan]◆ 주문 실행 단가 입력 (지정가 / 시장가)[/cyan]")
            config.console.print("[dim]  - 절대 가격: 49900 (해당 가격으로 주문)[/dim]")
            config.console.print(f"[dim]  - 퍼센트(%): -1% ({base_label} 대비 가격으로 주문)[/dim]")
            config.console.print("[dim]  - 0 (또는 m): 시장가 — 발동 시점의 현재가에 슬리피지를 반영해 주문[/dim]")
            if is_price_target:
                config.console.print("[dim]  - 엔터(빈 값): [bold yellow]발동 조건(목표가)과 동일한 가격[/bold yellow]으로 자동 설정[/dim]")
                default_hint = ""
            else:
                # [수정] 종전 기본값은 '등록 시점 현재가' 고정이었다. 신고가 돌파·트레일링 매수처럼
                #  발동 시점에 가격이 이미 올라 있는 조건에서는 그 지정가에 영원히 안 붙는다.
                #  조건은 맞았는데 체결이 안 되는 가장 흔한 경로라 기본을 시장가로 바꾼다.
                config.console.print("[dim]  - 엔터(빈 값): [bold yellow]시장가[/bold yellow] "
                                     "— 이 조건은 발동 시점 가격을 미리 알 수 없어 지정가를 고정하면 미체결로 남기 쉽습니다[/dim]")
                default_hint = "0"

            v = _rsv_ask("주문 단가 입력", default=default_hint)
            if v == _RSV_QUIT:
                step = -1
                continue
            if v == _RSV_BACK:
                step -= 1
                continue

            if v.lower() == 'm':
                v = "0"

            if v == "0":
                state['order_price'] = 0.0
                config.console.print("[dim] -> 발동 시점의 시장가로 자동 설정[/dim]")
            elif not v:
                # is_price_target 인 경우에만 빈 값이 올 수 있다 (그 외는 기본값 "0")
                state['order_price'] = state['target_price']
                config.console.print(f"[dim] -> 발동 조건(목표가)과 동일하게 자동 설정: "
                                     f"{_fmt_price(state['order_price'], is_overseas)}[/dim]")
            else:
                op = _rsv_parse_price(v, base_price, is_overseas, base_label,
                                      code=state.get('code'), name=state.get('name'))
                if op is None:
                    config.console.print("[red]주문 단가는 0보다 큰 숫자 또는 '-1%' 형식으로 입력하세요.[/red]")
                    utils.pause()
                    continue
                state['order_price'] = op
                config.console.print(f"[dim] -> 주문 단가: {_fmt_price(op, is_overseas)}"
                                     f"{' (호가단위 보정됨)' if not is_overseas else ''}[/dim]")
            step += 1
            marks[step] = len(context.USER_ACTION_BREADCRUMB)
            continue

        # ---------- 6. 수량 ----------
        if step == 6:
            _rsv_header(state)
            config.console.print("[dim](7/9 수량)[/dim]")
            is_overseas = state['is_overseas']
            max_qty = 0
            # [보유분석 청산] '전량 청산' 신호이므로 수량을 묻지 않는다. 부분 수량을 받으면
            #  신호의 뜻과 어긋나고, 등록 후 추가 매수한 몫이 무방비로 남는다. 발주 직전에
            #  실제 매도가능수량으로 다시 맞추므로 여기 값은 표시용 기준선이다.
            if state['condition_type'] == 'HOLDING_EXIT':
                state['qty'] = int(state['stock_info'].get('qty', 0))
                config.console.print(f"\n[cyan]◆ 수량: 전량 청산[/cyan] "
                                     f"[dim](현재 보유 {state['qty']:,}주 — 발동 시점의 보유 전량을 매도합니다)[/dim]")
                utils.pause()
                step += 1
                marks[step] = len(context.USER_ACTION_BREADCRUMB)
                continue

            if state['order_type'] == "sell":
                max_qty = int(state['stock_info'].get('qty', 0))
                v = _rsv_ask(f"주문 수량(주) [dim](보유 잔고: {max_qty:,}주)[/dim]", default=str(max_qty))
            else:
                # [추가] 매수는 상한 안내가 아예 없었다. 발동 시점 예수금이 모자라면
                #  주문이 FAILED로 끝나므로, 등록 단계에서 현재 기준 최대 수량을 알려준다.
                est_px = state['order_price'] or state.get('current_price') or 0
                hint = ""
                if est_px > 0:
                    try:
                        with utils.AccountContext(state['cano']):
                            dep = api.get_deposit_balance(state['cano'], state['acnt'], skip_balance_check=True)
                        cash = 0
                        if dep:
                            cash = dep.get('order_possible', 0) or dep.get('d2_deposit', 0)
                        if cash > 0 and not is_overseas:
                            max_qty = int((cash * 0.998) / est_px)
                            hint = f" [dim](현 예수금 기준 최대 {max_qty:,}주)[/dim]"
                    except Exception:
                        hint = ""
                v = _rsv_ask(f"주문 수량(주){hint}")

            if v == _RSV_QUIT:
                step = -1
                continue
            if v == _RSV_BACK:
                step -= 1
                continue

            try:
                qty = int(float(v.replace(',', '').replace('주', '').strip()))
            except ValueError:
                config.console.print("[red]수량은 숫자만 입력 가능합니다.[/red]")
                utils.pause()
                continue
            if qty <= 0:
                config.console.print("[red]수량은 1주 이상이어야 합니다.[/red]")
                utils.pause()
                continue

            if state['order_type'] == "sell" and qty > max_qty:
                config.console.print(f"[red]⚠️ 입력하신 수량({qty:,}주)이 보유 수량({max_qty:,}주)보다 많습니다. "
                                     f"잔고 전량으로 자동 조정합니다.[/red]")
                qty = max_qty
            elif state['order_type'] == "buy" and max_qty > 0 and qty > max_qty:
                config.console.print(f"[yellow]⚠️ 현재 예수금 기준 최대 {max_qty:,}주입니다. "
                                     f"발동 시점에 자금이 부족하면 주문이 실패로 기록됩니다.[/yellow]")

            state['qty'] = qty
            step += 1
            marks[step] = len(context.USER_ACTION_BREADCRUMB)
            continue

        # ---------- 7. 유효기간 ----------
        if step == 7:
            _rsv_header(state)
            config.console.print("[dim](8/9 유효 기간)[/dim]")
            config.console.print("\n[cyan]◆ 유효 기간 (만료일) 설정[/cyan]")
            config.console.print("[dim]  - 1: 당일 (오늘 장 마감 전까지)[/dim]")
            config.console.print("[dim]  - 2: 이번 주 (7일 후까지)[/dim]")
            config.console.print("[dim]  - 3: 이번 달 (30일 후까지)[/dim]")
            config.console.print("[dim]  - 4: 무기한 (취소할 때까지 유지)[/dim]")
            config.console.print("[dim]  - 직접 입력: YYYYMMDD (예: 20261231)[/dim]")
            v = _rsv_ask("유효 기간 선택 또는 입력", default="1")
            if v == _RSV_QUIT:
                step = -1
                continue
            if v == _RSV_BACK:
                step -= 1
                continue

            expire_dt = _rsv_resolve_expire(v)
            if expire_dt is None:
                config.console.print("[red]유효 기간은 1~4 또는 오늘 이후의 YYYYMMDD 8자리로 입력하세요.[/red]")
                utils.pause()
                continue

            # [추가] 장 마감 후에 '당일'을 고르면 사실상 감시 시간이 남지 않는다.
            if v == "1" and datetime.now().strftime("%H%M") >= "1530" and not state['is_overseas']:
                config.console.print("[yellow]⚠️ 이미 정규장이 끝난 시각입니다. '당일'은 오늘 안에 발동하지 않으면 "
                                     "내일 만료 처리됩니다.[/yellow]")
            state['expire_dt'] = expire_dt
            step += 1
            marks[step] = len(context.USER_ACTION_BREADCRUMB)
            continue

        # ---------- 8. 확인 ----------
        if step == 8:
            is_overseas = state['is_overseas']
            cond_str = _condition_text(state['condition_type'], state.get('target_price', 0.0),
                                       state.get('target_time', ''), state.get('composite_json'),
                                       state.get('composite_labels'), is_overseas)
            expire_dt = state['expire_dt']
            expire_disp = "무기한" if expire_dt == "20991231" else f"{expire_dt[:4]}-{expire_dt[4:6]}-{expire_dt[6:8]}까지"
            op_disp = "시장가 (발동 시점 현재가 기준)" if state['order_price'] == 0 else \
                f"{_fmt_price(state['order_price'], is_overseas)} (지정가)"
            t_type_str = "[red]매수[/red]" if state['order_type'] == "buy" else "[blue]매도[/blue]"
            t_type_plain = "매수" if state['order_type'] == "buy" else "매도"

            cur = state.get('current_price') or 0
            cur_line = f" 현재가: {_fmt_price(cur, is_overseas)}"
            # [추가] 목표가까지의 거리 — 확인 화면에 현재가조차 없어 방향 실수를 눈으로도 못 잡았다.
            tp = state.get('target_price') or 0
            if state['condition_type'] in ("STOP", "BREAKOUT") and cur > 0 and tp > 0:
                gap = (tp - cur) / cur * 100
                cur_line += f"  [dim]→ 목표가까지 {gap:+.2f}%[/dim]"

            est_amt = state['qty'] * (state['order_price'] or cur)
            amt_line = f" 예상 금액: {_fmt_price(est_amt, is_overseas)}" + (" (시장가 추정)" if state['order_price'] == 0 else "")

            confirm_msg = (
                f"\n[bold bright_white on yellow] [ 예약 {t_type_plain} 주문 최종 확인 ] [/]\n"
                f" 계좌: {_fmt_account(state['cano'], state['acnt'])} ({state['acc_label']})\n"
                f" 종목: {state['name']} ({state['code']})\n"
                f"{cur_line}\n"
                f" 조건: [bold]{state['condition_type']}[/bold] — {cond_str}\n"
                f" 주문: {t_type_str} {state['qty']}주 @ {op_disp}\n"
                f"{amt_line}\n"
                f" 유효: {expire_disp}\n"
            )
            config.console.print(Panel(confirm_msg, expand=False))

            ans = _rsv_ask("위 내용으로 예약 주문을 시스템에 등록하시겠습니까?", choices=["y", "n"], default="n")
            if ans == _RSV_QUIT:
                step = -1
                continue
            if ans == _RSV_BACK or ans == "n":
                if ans == "n":
                    config.console.print("\n[yellow]예약 주문 등록이 취소되었습니다.[/yellow]")
                    return None
                step -= 1
                continue

            db_manager.db.insert_reserved_order(
                cano=state['cano'], acnt=state['acnt'],
                market=("US" if is_overseas else "KR"),
                order_type=state['order_type'], code=state['code'], name=state['name'],
                qty=state['qty'], order_price=state['order_price'],
                condition_type=state['condition_type'], target_price=state.get('target_price', 0.0),
                target_time=state.get('target_time', ''), expire_dt=expire_dt,
                composite_json=state.get('composite_json')
            )
            config.console.print()
            config.console.print("[bold green]예약 주문이 성공적으로 등록되었습니다.[/bold green]")

            api.send_telegram_message(
                f"📝 [예약 등록] {state['name']}({state['code']})\n"
                f"구분: {t_type_plain}\n"
                f"조건: {state['condition_type']} ({cond_str})\n"
                f"수량: {state['qty']}주\n"
                f"단가: {'시장가' if state['order_price'] == 0 else _fmt_price(state['order_price'], is_overseas)}\n"
                f"유효: {expire_disp}")

            config.console.print()
            _print_reserved_orders_table()
            return True


def _reserved_account_label(order):
    """예약 주문이 걸린 계좌를 '실전/모의/자동/기타'로 표기한다."""
    cano, acnt = order.get('cano', ''), order.get('acnt', '')
    if cano == config.session.cano and acnt == config.session.acnt_prdt_cd:
        return "실전"
    if (config.session.auto_cano
            and cano == config.session.auto_cano and acnt == config.session.auto_acnt_prdt_cd):
        return "자동"
    return "기타"


def _reserved_distance(order, curr_price):
    """발동까지 남은 거리를 (정렬키, 표시문자열)로 돌려준다.

    [왜 필요한가] 예약 목록을 보는 이유는 '언제 발동하나, 지금 얼마나 가까운가'인데
    종전 표에는 현재가도 거리도 없었다. 계산할 수 없는 조건(수급·상태·복합)은
    정렬에서 뒤로 보낸다 — 없는 정보를 0으로 두면 가장 임박한 것처럼 위에 뜬다.
    """
    ct = order['condition_type']
    tp = float(order.get('target_price') or 0)
    if curr_price is None or curr_price <= 0:
        return (10 ** 9, "-")

    if ct in ('STOP', 'BREAKOUT', 'LIMIT') and tp > 0:
        gap = (tp - curr_price) / curr_price * 100
        return (abs(gap), f"{gap:+.2f}%")

    if ct == 'TRAILING_BUY':
        low = float(order.get('lowest_price') or 0)
        if low > 0:
            trigger = low * (1 + tp / 100.0)
            gap = (trigger - curr_price) / curr_price * 100
            return (abs(gap), f"{gap:+.2f}%")
        return (10 ** 9, "바닥 대기")

    if ct == 'TRAILING_SELL':
        high = float(order.get('highest_price') or 0)
        if high > 0:
            trigger = high * (1 - tp / 100.0)
            gap = (trigger - curr_price) / curr_price * 100
            return (abs(gap), f"{gap:+.2f}%")
        return (10 ** 9, "고점 대기")

    return (10 ** 9, "-")


def _load_reserved_orders_with_context(fetch_price=True):
    """대기 예약 주문에 현재가·거리·만료 정보를 붙이고 임박한 순으로 정렬한다."""
    orders = db_manager.db.get_pending_reserved_orders()
    if not orders:
        return orders

    prices = {}
    if fetch_price:
        for o in orders:
            key = (o['code'], o.get('market') == 'US')
            if key not in prices:
                try:
                    prices[key] = api.get_current_price(o['code'], key[1])
                except Exception:
                    prices[key] = 0

    today_str = datetime.now().strftime("%Y%m%d")
    for o in orders:
        curr = prices.get((o['code'], o.get('market') == 'US'), 0)
        o['_curr_price'] = curr
        o['_sort_key'], o['_gap_str'] = _reserved_distance(o, curr)
        exp = o.get('expire_dt') or "20991231"
        o['_expire_soon'] = (exp != "20991231" and exp <= today_str)

    orders.sort(key=lambda x: (not x['_expire_soon'], x['_sort_key'], x['id']))
    return orders


def _print_reserved_orders_table(orders=None, fetch_price=True):
    """예약 주문 대기 목록을 출력하고 주문 목록을 반환합니다.

    발동까지의 거리를 함께 보여주고 임박한 순으로 정렬한다. 계좌 컬럼은 여러
    계좌에 예약이 걸려 있을 때만 띄운다 — 한 계좌만 쓰는 화면에서 계좌번호와
    계좌구분이 늘 두 칸을 먹으면 좁은 터미널에서 줄이 접힌다.
    """
    if orders is None:
        orders = _load_reserved_orders_with_context(fetch_price=fetch_price)
    if not orders:
        config.console.print("[yellow]현재 대기 중인 예약 주문이 없습니다.[/yellow]")
        return orders

    multi_account = len({(o.get('cano'), o.get('acnt')) for o in orders}) > 1

    #  [2026-09-06] 한 종목이 세 칸에서 줄바꿈(\n)을 써 두 줄을 차지했다 — 예약이 몇 건만
    #  쌓여도 화면이 두 배로 길어지고 눈으로 훑기 어렵다. 정보는 그대로 두고 가로로 편다.
    #  폭이 늘어나므로 안쪽 여백을 접는다(표 폭 상한 135열).
    table = Table(title=f"예약 주문 대기 목록 ({len(orders)}건)", box=box.HORIZONTALS,
                  header_style="dim", border_style="dim",
                  collapse_padding=True, pad_edge=False)
    table.add_column("ID", justify="center", style="cyan")
    table.add_column("종목", justify="left")
    table.add_column("구분", justify="center")
    table.add_column("발동 조건", justify="left")
    table.add_column("현재가", justify="right")
    table.add_column("거리", justify="right")
    table.add_column("주문", justify="right")
    table.add_column("유효", justify="center")
    if multi_account:
        table.add_column("계좌", justify="center", style="dim")

    for o in orders:
        is_ovs = (o.get('market') == 'US')
        cond_str = _condition_text(o['condition_type'], o.get('target_price', 0),
                                   o.get('target_time', ''), o.get('composite_json'),
                                   is_overseas=is_ovs)
        op_disp = "시장가" if o['order_price'] == 0 else _fmt_price(o['order_price'], is_ovs)
        t_type = "[red]매수[/]" if o['order_type'] == 'buy' else "[blue]매도[/]"

        curr = o.get('_curr_price') or 0
        curr_str = _fmt_price(curr, is_ovs) if curr > 0 else "[dim]-[/dim]"

        gap = o.get('_gap_str', '-')
        if gap not in ('-',) and gap.endswith('%'):
            try:
                gap_col = "bold yellow" if abs(float(gap.rstrip('%'))) <= 2.0 else "dim"
            except ValueError:
                gap_col = "dim"
            gap_str = f"[{gap_col}]{gap}[/{gap_col}]"
        else:
            gap_str = f"[dim]{gap}[/dim]"

        exp = o.get('expire_dt') or "20991231"
        if exp == "20991231":
            exp_str = "[dim]무기한[/dim]"
        elif o.get('_expire_soon'):
            exp_str = f"[bold red]{exp[4:6]}-{exp[6:8]} ⚠[/bold red]"
        else:
            exp_str = f"{exp[4:6]}-{exp[6:8]}"

        #  조건 종류를 앞에 붙이는 것은 _condition_text 가 **값만** 돌려줄 때뿐이다.
        #  STOP 은 "10,000원"이라 방향을 알 수 없어 종류가 필요하지만, 나머지는 이미
        #  문장이라("고점 대비 3.5% 하락") 앞에 붙이면 "EMA EMA 60일선…"처럼 겹친다.
        ct_short = o['condition_type'].replace('_UP', '').replace('_DOWN', '')
        cond_cell = (f"{ct_short} [dim]{cond_str}[/dim]"
                     if cond_str == _fmt_price(o.get('target_price', 0), is_ovs)
                     else cond_str)
        row = [str(o['id']), f"{o['name']} [dim]{o['code']}[/dim]", t_type,
               cond_cell,
               curr_str, gap_str, f"{o['qty']}주 [dim]@{op_disp}[/dim]", exp_str]
        if multi_account:
            row.append(f"{_reserved_account_label(o)} [dim]{o.get('cano', '')}[/dim]")
        table.add_row(*row)

    config.console.print(table)
    config.console.print("[dim]  ※ 거리 = 현재가에서 발동까지 남은 폭 · ⚠ = 오늘 만료 · "
                         "계산 불가 조건(수급·상태·복합)은 '-'[/dim]")
    return orders


def _cancel_reserved_orders(orders, cancel_ids):
    """지정한 ID들의 예약 주문을 취소하고 취소된 ID 목록을 반환한다."""
    canceled = []
    for cid in cancel_ids:
        target = next((o for o in orders if str(o['id']) == str(cid)), None)
        if not target:
            continue
        # 조건부 취소 — 그 사이 감시 스레드가 발동시켰으면 손대지 않는다(cancel_reserved_order 주석).
        if not db_manager.db.cancel_reserved_order(int(cid), reason="사용자 수동 취소"):
            config.console.print(
                f"[yellow]⚠️ 예약 주문(ID: {cid})은 이미 발동/처리 중이라 취소하지 못했습니다. "
                f"미체결 조회(9-3)에서 확인하세요.[/yellow]")
            continue
        canceled.append(str(cid))

        t_type = "매수" if target['order_type'] == 'buy' else "매도"
        cond_str = target['condition_type']
        api.send_telegram_message(
            f"🗑️ [예약 수동 취소] {target['name']}({target['code']})\n"
            f"사용자에 의해 대기 중이던 예약 {t_type} 주문(ID: {cid})이 취소되었습니다.\n조건: {cond_str}")
        db_manager.db.insert_trade(
            f"{t_type}취소(예약)", target['code'], target['name'], target['qty'],
            target.get('order_price', 0), f"RES_CAN_{cid}",
            order_status="취소", reason=f"수동 취소 (조건: {cond_str})")
    return canceled


def _prompt_reserved_ids(orders, action_label):
    """작업 대상 예약 주문 ID를 입력받아 리스트로 돌려준다 (취소 시 None)."""
    valid = {str(o['id']) for o in orders}
    raw = Prompt.ask(f"{action_label}할 예약 주문 ID [dim](다중: 1,3 / 전체: 0 / 취소: Enter)[/dim]",
                     default="")
    if not raw or raw.lower() in ('b', 'q'):
        return None
    if raw.strip() == "0":
        return [str(o['id']) for o in orders]

    picked, unknown = [], []
    for token in raw.split(','):
        t = token.strip()
        if not t:
            continue
        if t in valid:
            if t not in picked:
                picked.append(t)
        else:
            unknown.append(t)
    # [추가] 종전에는 없는 ID를 조용히 버렸다. 3건을 지우려다 1건만 지워진 것을 모른 채 나갔다.
    if unknown:
        config.console.print(f"[yellow]목록에 없는 ID는 무시했습니다: {', '.join(unknown)}[/yellow]")
    return picked or None


def _edit_reserved_order(order):
    """대기 중인 예약 주문의 수량·주문단가·목표가·유효기간을 수정한다.

    조건 종류와 종목은 바꾸지 않는다 — 트레일링 최저/최고가 같은 누적 상태가
    그대로 남아 등록한 적 없는 기준으로 발동하기 때문. 그 경우는 취소 후 재등록이 맞다.
    """
    is_ovs = (order.get('market') == 'US')
    curr = order.get('_curr_price') or api.get_current_price(order['code'], is_ovs)
    base = curr if curr > 0 else float(order.get('order_price') or 0)

    ct = order['condition_type']
    editable_target = ct in ('STOP', 'BREAKOUT', 'LIMIT')

    while True:
        config.console.print()
        config.console.print(Panel(
            f"[bold]ID {order['id']} · {order['name']}({order['code']})[/bold]\n"
            f"조건: {ct} — {_condition_text(ct, order.get('target_price', 0), order.get('target_time', ''), order.get('composite_json'), is_overseas=is_ovs)}\n"
            f"주문: {'매수' if order['order_type'] == 'buy' else '매도'} {order['qty']}주 @ "
            f"{'시장가' if order['order_price'] == 0 else _fmt_price(order['order_price'], is_ovs)}\n"
            f"현재가: {_fmt_price(curr, is_ovs) if curr > 0 else '-'}\n"
            f"유효: {'무기한' if (order.get('expire_dt') or '20991231') == '20991231' else order['expire_dt']}",
            title="예약 주문 수정", border_style="cyan", expand=False))

        items = [("1", "수량 변경", "Quantity")]
        if editable_target:
            items.append(("2", "발동 목표가 변경", "Target Price"))
        items.append(("3", "주문 단가 변경", "Order Price"))
        items.append(("4", "유효 기간 변경", "Expiry"))
        if not editable_target:
            config.console.print("[dim]※ 이 조건은 목표가가 없어 수량·단가·유효기간만 바꿀 수 있습니다. "
                                 "조건 자체를 바꾸려면 취소 후 다시 등록하세요.[/dim]")

        choice = utils.show_menu(f"수정할 항목 (ID {order['id']})", items, default_choice="1")
        if choice.lower() in ('b', 'q'):
            return False

        if choice == "1":
            v = Prompt.ask("새 수량(주)", default=str(order['qty']))
            try:
                qty = int(float(v.replace(',', '').replace('주', '').strip()))
            except ValueError:
                config.console.print("[red]수량은 숫자만 입력 가능합니다.[/red]"); continue
            if qty <= 0:
                config.console.print("[red]수량은 1주 이상이어야 합니다.[/red]"); continue
            if db_manager.db.update_reserved_order_fields(order['id'], qty=qty):
                order['qty'] = qty
                config.console.print(f"[green]✓ 수량을 {qty:,}주로 변경했습니다.[/green]")
            else:
                config.console.print("[red]변경에 실패했습니다 (이미 발동·취소되었을 수 있습니다).[/red]")
                return True

        elif choice == "2" and editable_target:
            v = Prompt.ask(f"새 목표가 [dim](절대가 또는 현재가 대비 %)[/dim]",
                           default=str(int(order['target_price']) if not is_ovs else order['target_price']))
            #  [Fix 2026-09-06] 종전 키는 'stock_name' 이었는데 reserved_orders 의 컬럼은
            #   **name** 이다 — 항상 None 이라 ETF 격자 보정이 꺼진 채였다.
            #   _rsv_parse_price 독스트링이 그 결과를 적어 뒀다: "사용자가 입력한 유효한
            #   ETF 호가를 다른 값으로 옮긴다(23,070 → 23,050)". 등록 경로 네 곳은 올바른
            #   키를 쓰는데 **수정 화면 두 곳만** 어긋나 있었다.
            tp = _rsv_parse_price(v, base, is_ovs,
                                  code=order.get('code'), name=order.get('name'))
            if tp is None:
                config.console.print("[red]목표가는 0보다 큰 숫자 또는 '+5%' 형식으로 입력하세요.[/red]"); continue
            warn = _rsv_immediate_trigger(ct, tp, curr)
            if warn:
                config.console.print(f"[bold red]⚠️ {warn} — 저장 즉시 발동합니다.[/bold red]")
                if Prompt.ask("그래도 저장하시겠습니까?", choices=["y", "n"], default="n") != "y":
                    continue
            if db_manager.db.update_reserved_order_fields(order['id'], target_price=tp):
                order['target_price'] = tp
                config.console.print(f"[green]✓ 목표가를 {_fmt_price(tp, is_ovs)}로 변경했습니다.[/green]")
            else:
                config.console.print("[red]변경에 실패했습니다.[/red]")
                return True

        elif choice == "3":
            v = Prompt.ask("새 주문 단가 [dim](0: 시장가 / 절대가 / 현재가 대비 %)[/dim]",
                           default="0" if order['order_price'] == 0 else str(order['order_price']))
            if v.strip() in ("0", "m", "M"):
                op = 0.0
            else:
                op = _rsv_parse_price(v, base, is_ovs,
                                      code=order.get('code'), name=order.get('name'))
                if op is None:
                    config.console.print("[red]단가는 0(시장가) 또는 0보다 큰 숫자로 입력하세요.[/red]"); continue
            if db_manager.db.update_reserved_order_fields(order['id'], order_price=op):
                order['order_price'] = op
                config.console.print(f"[green]✓ 주문 단가를 "
                                     f"{'시장가' if op == 0 else _fmt_price(op, is_ovs)}로 변경했습니다.[/green]")
            else:
                config.console.print("[red]변경에 실패했습니다.[/red]")
                return True

        elif choice == "4":
            config.console.print("[dim]  1: 당일 / 2: 이번 주 / 3: 이번 달 / 4: 무기한 / YYYYMMDD 직접 입력[/dim]")
            v = Prompt.ask("새 유효 기간", default="4")
            exp = _rsv_resolve_expire(v.strip())
            if exp is None:
                config.console.print("[red]1~4 또는 오늘 이후의 YYYYMMDD 8자리로 입력하세요.[/red]"); continue
            if db_manager.db.update_reserved_order_fields(order['id'], expire_dt=exp):
                order['expire_dt'] = exp
                config.console.print(f"[green]✓ 유효 기간을 "
                                     f"{'무기한' if exp == '20991231' else exp}으로 변경했습니다.[/green]")
            else:
                config.console.print("[red]변경에 실패했습니다.[/red]")
                return True


def manage_reserved_orders():
    """예약 주문 관리 메뉴 (상세·수정·취소).

    [변경] 종전에는 ID를 입력하면 확인 없이 곧바로 취소됐고, '0'(전체 취소)마저
    되묻지 않았다. 이 시스템의 다른 파괴적 동작(주문 전송·메모 삭제·9-5 삭제)에는
    모두 확인이 있다. 또한 '관리'라는 이름과 달리 수정 수단이 없어 값 하나를
    바꾸려면 아홉 단계를 다시 입력해야 했다.
    """
    while True:
        utils.clear_screen()
        config.console.print()
        utils.print_breadcrumb()
        config.console.print("[bold green]예약 주문 관리[/bold green]")
        config.console.print()

        orders = _load_reserved_orders_with_context()
        if not orders:
            config.console.print("[yellow]현재 대기 중인 예약 주문이 없습니다.[/yellow]")
            config.console.print("[dim]새 예약은 [8]-4 예약 주문 등록에서 만들 수 있습니다.[/dim]")
            time.sleep(1.2)
            return None
        _print_reserved_orders_table(orders)

        config.console.print()
        act = Prompt.ask("작업 선택 (0: 상세, 1: 수정, 2: 취소) [dim](이전: b, 메인: q)[/dim]",
                         choices=["0", "1", "2", "b", "q"], default="0")
        if act.lower() in ('b', 'q'):
            return False

        if act == "0":
            ids = _prompt_reserved_ids(orders, "상세를 볼")
            if not ids:
                continue
            for cid in ids:
                o = next((x for x in orders if str(x['id']) == cid), None)
                if not o:
                    continue
                is_ovs = (o.get('market') == 'US')
                config.console.print(Panel(
                    f"[bold]{o['name']} ({o['code']})[/bold]  "
                    f"{'[red]매수[/red]' if o['order_type'] == 'buy' else '[blue]매도[/blue]'}\n"
                    f"계좌: {o.get('cano', '')}-{o.get('acnt', '')} ({_reserved_account_label(o)})\n"
                    f"조건: {o['condition_type']} — "
                    f"{_condition_text(o['condition_type'], o.get('target_price', 0), o.get('target_time', ''), o.get('composite_json'), is_overseas=is_ovs)}\n"
                    f"현재가: {_fmt_price(o.get('_curr_price'), is_ovs) if (o.get('_curr_price') or 0) > 0 else '-'}"
                    f"  (거리 {o.get('_gap_str', '-')})\n"
                    f"주문: {o['qty']}주 @ "
                    f"{'시장가' if o['order_price'] == 0 else _fmt_price(o['order_price'], is_ovs)}\n"
                    f"추적: 최저 {_fmt_price(o.get('lowest_price') or 0, is_ovs)} / "
                    f"최고 {_fmt_price(o.get('highest_price') or 0, is_ovs)}\n"
                    f"등록: {o.get('created_at', '-')} (UTC)\n"
                    f"유효: {'무기한' if (o.get('expire_dt') or '20991231') == '20991231' else o['expire_dt']}",
                    title=f"예약 주문 ID {o['id']}", border_style="green", expand=False))
            utils.pause()
            continue

        if act == "1":
            ids = _prompt_reserved_ids(orders, "수정할")
            if not ids:
                continue
            if len(ids) > 1:
                config.console.print("[yellow]수정은 한 번에 한 건만 가능합니다. 첫 번째 ID로 진행합니다.[/yellow]")
            target = next((x for x in orders if str(x['id']) == ids[0]), None)
            if target:
                _edit_reserved_order(target)
                utils.pause()
            continue

        # act == "2" — 취소
        ids = _prompt_reserved_ids(orders, "취소할")
        if not ids:
            continue

        targets = [o for o in orders if str(o['id']) in ids]
        config.console.print()
        config.console.print(f"[bold]다음 {len(targets)}건을 취소합니다.[/bold]")
        for o in targets:
            is_ovs = (o.get('market') == 'US')
            config.console.print(
                f"  · ID {o['id']} {o['name']}({o['code']}) "
                f"{'매수' if o['order_type'] == 'buy' else '매도'} {o['qty']}주 — "
                f"{_condition_text(o['condition_type'], o.get('target_price', 0), o.get('target_time', ''), o.get('composite_json'), is_overseas=is_ovs)}")
        config.console.print()
        if Prompt.ask("정말 취소하시겠습니까?", choices=["y", "n"], default="n") != "y":
            config.console.print("[yellow]취소하지 않았습니다.[/yellow]")
            time.sleep(1)
            continue

        canceled = _cancel_reserved_orders(orders, ids)
        config.console.print()
        if canceled:
            config.console.print(f"[bold green]예약 주문(ID: {', '.join(canceled)})이 취소되었습니다.[/bold green]")
        else:
            config.console.print("[yellow]취소할 수 있는 유효한 예약 주문 ID가 없습니다.[/yellow]")
        time.sleep(1.2)


def get_reserved_orders_summary():
    """텔레그램 전송용 예약 주문 현황 요약 문자열 생성"""
    orders = db_manager.db.get_pending_reserved_orders()
    if not orders:
        return "📭 현재 대기 중인 예약 주문이 없습니다."

    msg = f"⏳ [대기 중인 예약 주문 현황] (총 {len(orders)}건)\n\n"
    for o in orders:
        is_ovs = (o.get('market') == 'US')
        acc_label = _reserved_account_label(o)
        cond_str = (f"{o['condition_type'].replace('_UP', '').replace('_DOWN', '')} "
                    f"({_condition_text(o['condition_type'], o.get('target_price', 0), o.get('target_time', ''), o.get('composite_json'), is_overseas=is_ovs)})")
        op_disp = "시장가" if o['order_price'] == 0 else _fmt_price(o['order_price'], is_ovs)
        ord_str = f"{o['qty']}주 @ {op_disp}"
        t_type = "🔴 매수" if o['order_type'] == 'buy' else "🔵 매도"

        exp = o.get('expire_dt', '20991231')
        exp_str = "무기한" if not exp or exp == "20991231" else f"{exp[:4]}-{exp[4:6]}-{exp[6:8]}"

        created_at = o.get('created_at', '')
        created_str = "-"
        if created_at:
            try:
                # SQLite DEFAULT CURRENT_TIMESTAMP는 UTC 기준이므로 KST(+9시간)로 변환
                dt = datetime.strptime(created_at, "%Y-%m-%d %H:%M:%S") + timedelta(hours=9)
                created_str = dt.strftime("%m-%d %H:%M")
            except Exception:
                created_str = created_at[5:16] if len(created_at) >= 16 else "-"

        msg += f"{o['name']}({o['code']}) [{acc_label}]\n"
        msg += f"  {t_type} | {ord_str}\n"
        msg += f"  조건: {cond_str}\n"
        msg += f"  등록: {created_str} | 유효: {exp_str} (ID: {o['id']})\n\n"

    return msg.strip()


def order_menu():
    """매수/매도 주문 선택 메뉴"""
    menu_items = [("1", "매수 주문", "Buy Order"), ("2", "매도 주문", "Sell Order")]
    choice = utils.show_menu("주문 유형을 선택하세요", menu_items, default_choice="1")
    if choice.lower() in ['b', 'q']: return False

    menu_map_dict = dict((k, v) for k, v, _ in menu_items)
    context.USER_ACTION_BREADCRUMB.append(f"[{choice}] {menu_map_dict.get(choice, '')}")

    if choice == "1":
        send_order("buy")
    elif choice == "2":
        send_order("sell")

def stock_order_menu():
    """종목 주문 관리 통합 메뉴"""
    base_breadcrumb_len = len(context.USER_ACTION_BREADCRUMB)
    last_choice = "3"
    while True:
        context.USER_ACTION_BREADCRUMB = context.USER_ACTION_BREADCRUMB[:base_breadcrumb_len]
        
        menu_items = [
            ("1", "[red]매수[/red] 주문", "Buy"), 
            ("2", "[blue]매도[/blue] 주문", "Sell"), 
            ("3", "[magenta]정정/취소[/magenta] 주문", "Modify/Cancel"), 
            ("4", "[yellow]예약 주문 등록[/yellow]", "Reserve Order"), 
            ("5", "[green]예약 주문 관리[/green]", "Manage Reserves")
        ]
        choice = utils.show_menu("종목 주문 관리 (Stock Order Management)", menu_items, default_choice=last_choice)
        
        if choice.lower() in ['b', 'q']: return False
        if choice.lower() == 'h':
            if getattr(utils, 'show_help', None):
                utils.show_help()
                utils.pause()
            continue
        
        last_choice = choice

        menu_map = {"1": "매수 주문", "2": "매도 주문", "3": "정정/취소 주문", "4": "예약 주문 등록", "5": "예약 주문 관리"}
        if choice in menu_map:
            context.USER_ACTION_BREADCRUMB.append(f"[{choice}] {menu_map[choice]}")

        if choice == "1":
            if send_order("buy") is not False: utils.pause()
        elif choice == "2":
            if send_order("sell") is not False: utils.pause()
        elif choice == "3":
            if modify_order() is not False: utils.pause()
        elif choice == "4":
            if register_reserved_order() is not False: utils.pause()
        elif choice == "5":
            if manage_reserved_orders() is not False: utils.pause()
