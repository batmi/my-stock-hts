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
import context # [추가]
import api
import utils
from modules import manage
from modules import account
import indicators # [추가] ATR 계산을 위해 추가
from modules import db_manager
from modules import analysis
from modules import auto_trade # [추가] 체결 감시자 호출용

logger = logging.getLogger(__name__)

def select_account(title="주문을 수행할 계좌를 선택하세요"):
    """계좌를 선택합니다."""
    target_cano = config.session.cano
    target_acnt = config.session.acnt_prdt_cd
    if config.session.is_toss:
        acc_label = "토스증권"
    else:
        acc_label = "한투증권" if not config.session.is_simulation else "모의투자"

    # 실전 모드이고 자동매매 계좌가 별도로 설정된 경우 선택
    if not config.session.is_simulation and config.session.auto_cano and config.session.auto_cano != config.session.cano:
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
        with Progress(
            SpinnerColumn(),
            TextColumn("[progress.description]{task.description}"),
            BarColumn(),
            console=config.console,
            transient=True
        ) as progress:
            progress.add_task("[cyan]해외 잔고 조회 중...[/cyan]", total=None)
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
            exists_check = db_manager.db.check_trade_exists(odno, "체결") or db_manager.db.check_trade_exists(odno, "체결(추정)")
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
        label = "모의" if config.session.is_simulation else "실전"
        accounts.append({"cano": config.session.cano, "acnt": config.session.acnt_prdt_cd, "label": label})
    
    # 2. 자동매매 계좌 (실전 모드이고 별도 설정된 경우)
    if not config.session.is_simulation and config.session.auto_cano and config.session.auto_cano != config.session.cano:
        accounts.append({"cano": config.session.auto_cano, "acnt": config.session.auto_acnt_prdt_cd, "label": "자동"})

    selectable_orders = []

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
                
                # [추가] 모의투자 API 누락 대응: DB에서 '접수' 상태 주문 조회하여 병합
                if config.session.is_simulation:
                    try:
                        # [수정] 오늘 날짜 기준 접수 상태 주문만 조회 (과거 데이터 누적 방지 및 속도 개선)
                        today_str = datetime.now().strftime("%Y-%m-%d")
                        db_orders = db_manager.db.get_trades(limit=100, order_status="접수", start_date=today_str, is_sim=True)
                        
                        # [추가] 이미 체결 처리된 주문(체결 히스토리 존재) 필터링 (limit=None으로 전체 조회)
                        filled_trades = db_manager.db.get_trades(start_date=today_str, order_status="체결", is_sim=True, limit=None)
                        # [추가] 체결(추정) 상태도 포함하여 조회
                        filled_trades_est = db_manager.db.get_trades(start_date=today_str, order_status="체결(추정)", is_sim=True, limit=None)
                        filled_odnos = set(str(t['odno']) for t in filled_trades + filled_trades_est)
                        
                        # [DEBUG] DB 상태 로깅
                        if config.FILE_DEBUG_LEVEL == "DEBUG":
                            logger.debug(f"[ORDER_DEBUG] show_open_orders: DB접수({len(db_orders)}건), DB체결({len(filled_trades)}건), DB체결추정({len(filled_trades_est)}건)")
                            logger.debug(f"[ORDER_DEBUG] Filled ODNOs: {filled_odnos}")
                        
                        # [추가] 이미 취소/정정된 주문(후속 이력 존재) 필터링
                        # 취소/정정 주문은 org_odno(원주문번호)를 가지고 있음
                        # 오늘 날짜의 모든 거래 내역을 조회하여 org_odno가 있는 경우 원본 주문을 제외 대상에 추가
                        all_today_trades = db_manager.db.get_trades(start_date=today_str, is_sim=True)
                        canceled_org_odnos = set(str(t['org_odno']) for t in all_today_trades if t.get('org_odno'))
                        
                        # [추가] 잔고 정보 조회 (매도 주문 체결 여부 검증용)
                        holdings_map = {}
                        try:
                            h_list, _ = api.get_domestic_balance(cano, acnt)
                            if h_list:
                                for h in h_list:
                                    holdings_map[h['pdno']] = int(h['hldg_qty'])
                        except Exception: pass
                        
                        # [DEBUG] 잔고 상태 로깅
                        if config.FILE_DEBUG_LEVEL == "DEBUG":
                            logger.debug(f"[ORDER_DEBUG] Holdings Map: {holdings_map}")

                        # 이미 API로 조회된 주문번호 집합
                        api_odnos = set(str(o.get('odno')) for o in dom_orders if o.get('odno'))
                        
                        current_acc_str = f"{cano}-{acnt}"

                        # [추가] 모의투자 체결 알림 헬퍼 함수 (실전 포맷 적용)
                        def _send_sim_alert(type_name, db_order, reason_msg, fill_price=0.0):
                            try:
                                code = db_order.get('code')
                                name = db_order.get('name')
                                qty = int(float(db_order.get('qty', 0)))
                                price = fill_price if fill_price > 0 else float(db_order.get('price', 0))
                                
                                is_overseas = not (len(code) == 6 and code[0].isdigit() and code.isalnum()) if code else False
                                if price <= 0:
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
                                
                                custom_rules = db_manager.db.get_all_stock_strategies()
                                rules_map = {r['code']: r for r in custom_rules}
                                rule = rules_map.get(code)
                                
                                title_tag = f"[{type_name} 체결(추정)]" if type_name else "[체결 알림(추정)]"
                                rule_info = ""
                                if rule:
                                    title_tag += " [개별]"
                                    rule_info = f"\n🔧 [개별 룰] 익절 +{rule['take_profit']}% / 손절 {rule['stop_loss']}%"
                                    if rule.get('ts_activation'):
                                        rule_info += f" / TS +{rule['ts_activation']}%(-{rule['ts_callback']}%)"
                                
                                cur_info = ""
                                try:
                                    cp_data = api.get_current_price_data(code, is_overseas=is_overseas)
                                    if cp_data.get('rt_cd') == '0':
                                        if is_overseas:
                                            curr = float(cp_data['output'].get('last', 0))
                                            rate = float(cp_data['output'].get('rate', 0))
                                            icon = "🔺" if rate > 0 else ("" if rate < 0 else "➖")
                                            cur_info = f"\n현재가: ${curr:,.2f} ({icon} {rate:+.2f}%)"
                                        else:
                                            curr = float(cp_data['output']['stck_prpr'])
                                            rate = float(cp_data['output']['prdy_ctrt'])
                                            icon = "🔺" if rate > 0 else ("🔽" if rate < 0 else "➖")
                                            cur_info = f"\n현재가: {int(curr):,}원 ({icon} {rate:+.2f}%)"
                                except Exception: pass
                                
                                strategy_info = ""
                                if db_order.get('snapshot'):
                                    try:
                                        snap = json.loads(db_order['snapshot'])
                                        if 'indicators' in snap:
                                            ind = snap['indicators']
                                            score = db_order.get('strategy_score', 0)
                                            rsi_str = f"{ind.get('rsi', 0):.1f}"
                                            adx_str = f"{ind.get('adx', 0):.1f}"
                                            cci_str = f"{ind.get('cci', 0):.1f}"
                                            strategy_info = f"\n\n📊 [전략 지표(진입시점)]\n• 점수: {score}점\n• RSI: {rsi_str} / ADX: {adx_str} / CCI: {cci_str}"
                                    except Exception: pass
                                    
                                if strategy_info:
                                    strategy_info += cur_info
                                    cur_info = ""
                                elif cur_info:
                                    strategy_info = f"\n\n📊 [현재 시장 데이터]{cur_info}"
                                    cur_info = ""

                                exec_amt = price * qty
                                price_fmt = f"${price:,.2f}" if is_overseas and price > 0 else (f"{price:,.0f}원" if price > 0 else "시장가")
                                amt_fmt = f"${exec_amt:,.2f}" if is_overseas and exec_amt > 0 else (f"{int(exec_amt):,}원" if exec_amt > 0 else "-")
                                
                                original_reason = db_order.get('reason', reason_msg)
                                profit_msg = ""
                                if type_name == "매도":
                                    p_amt = db_order.get('profit_amt')
                                    p_rate = db_order.get('profit_rate')
                                    if p_amt is not None and p_rate is not None:
                                        profit_msg = f"\n손익: {int(p_amt):+,}원 ({float(p_rate):+.2f}%)"
                                        
                                db_odno = db_order.get('odno', '')
                                msg = f"✅ {title_tag} {name}({code})\n수량: {qty}주\n단가: {price_fmt}(추정체결가)\n금액: {amt_fmt}\n주문번호: {utils.format_order_no(db_odno)}{profit_msg}\n사유: {original_reason}{cur_info}{strategy_info}{rule_info}"
                                api.send_telegram_message(msg)

                                # [추가] 수동 매수 체결 시 트레이딩 제한 종목 자동 등록.
                                #  프로그램 수동 주문은 시스템 ODNO로 등록되어 auto_trade의
                                #  외부주문 감지 경로(앱/HTS)를 타지 않으므로 여기서 직접 등록한다.
                                #  (자동매매가 해당 종목을 건드리지 않도록 보호)
                                if type_name == "매수" and not auto_trade.is_system_odno(db_odno):
                                    try:
                                        auto_trade.add_restricted_stock(code, name, "수동매매", is_overseas=is_overseas, cano=cano, acnt=acnt, account_type=auto_trade._current_account_type())
                                    except Exception as e:
                                        logger.error(f"수동 매수 제한 종목 등록 오류: {e}")

                                # [수정] 중복 DB 저장 로직 제거 (_create_fill_history에서 이미 수행)

                            except Exception: pass

                        for db_o in db_orders:
                            # 계좌 일치 확인
                            if db_o.get('account') and db_o.get('account') != current_acc_str:
                                continue

                            db_odno = str(db_o.get('odno'))
                            
                            # [DEBUG] 주문별 판단 로직 로깅
                            if config.FILE_DEBUG_LEVEL == "DEBUG":
                                logger.debug(f"[ORDER_DEBUG] Checking DB Order: {db_odno} ({db_o.get('type')}, {db_o.get('name')})")

                            if not db_odno or db_odno in api_odnos:
                                if config.FILE_DEBUG_LEVEL == "DEBUG": logger.debug(f"[ORDER_DEBUG] -> Skip (In API or Invalid)")
                                continue
                            
                            # [추가] 이미 체결된 주문이면 스킵 (접수 상태라도 체결 내역이 있으면 제외)
                            if db_odno in filled_odnos:
                                if config.FILE_DEBUG_LEVEL == "DEBUG": logger.debug(f"[ORDER_DEBUG] -> Skip (Already Filled)")
                                continue
                            
                            # [추가] 이미 취소/정정된 주문이면 스킵 (원주문번호로 참조된 이력이 있으면 제외)
                            if db_odno in canceled_org_odnos:
                                if config.FILE_DEBUG_LEVEL == "DEBUG": logger.debug(f"[ORDER_DEBUG] -> Skip (Canceled)")
                                continue
                            
                            # DB 주문 정보를 API 포맷으로 변환
                            type_str = db_o.get('type', '')
                            sll_buy_name = "매수" if "buy" in type_str.lower() or "매수" in type_str else "매도"
                            
                            # [추가] 매도 주문인데 잔고가 0이면 '체결'로 간주하고 목록에서 제외 (DB 업데이트 포함)
                            # 모의투자 API가 체결 내역을 늦게 주거나 누락하는 경우 대응
                            if "매도" in sll_buy_name:
                                code = db_o.get('code')
                                cur_qty = holdings_map.get(code, 0)
                                # [수정] 발주 직전 보유수량 대비 감소분으로 체결 판정 (부분매도 대응)
                                #  pre_qty 미보유 시 기존 전량매도(잔고 0) 가정으로 폴백
                                order_qty = int(float(db_o.get('qty', 0)))
                                trader_om = auto_trade.AutoTrader().order_manager
                                pre_qty = trader_om.sell_pre_qty.get(str(db_odno))
                                is_delta_fill = pre_qty is not None and (pre_qty - cur_qty) >= order_qty
                                is_sell_filled = is_delta_fill or cur_qty == 0
                                if is_sell_filled:
                                    fill_reason = "잔고 감소 확인" if is_delta_fill and cur_qty != 0 else "잔고 0 확인"
                                    cm = auto_trade.ConclusionMonitor()
                                    with cm._lock:
                                        if db_odno in cm.processed_sim_fills: continue
                                        cm.processed_sim_fills.add(db_odno)

                                    if config.FILE_DEBUG_LEVEL == "DEBUG":
                                        logger.debug(f"[ORDER_DEBUG] 매도 주문({db_odno}) {fill_reason} -> 체결 처리 시작")
                                    # 원본 업데이트 제거 (접수 기록 보존)

                                    # [추가] 체결 히스토리 생성
                                    fill_price = _create_fill_history(db_o, fill_reason)

                                    if config.FILE_DEBUG_LEVEL == "DEBUG":
                                        logger.debug(f"[ORDER_DEBUG] 매도 주문({db_odno}) 체결 처리 완료 (알림 전송 예정)")

                                    # [수정] 텔레그램 알림 (헬퍼 함수 사용)
                                    _send_sim_alert("매도", db_o, fill_reason, fill_price)
                                    
                                    # [추가] AutoTrader 상태도 업데이트하여 중복 처리 방지
                                    trader = auto_trade.AutoTrader()
                                    if hasattr(trader, 'order_manager'):
                                        trader.update_order_status(code, db_odno, auto_trade.OrderStatus.FILLED)
                                        
                                    continue
                                else:
                                    if config.FILE_DEBUG_LEVEL == "DEBUG":
                                        logger.debug(f"[ORDER_DEBUG] 매도 주문({db_odno}) 잔고 보유중({cur_qty}) -> 미체결 유지")
                            
                            # [추가] 매수 주문인데 잔고가 주문 수량 이상이면 '체결'로 간주하고 목록에서 제외
                            # (모의투자 API 누락 대응: 잔고가 들어왔다면 체결된 것임)
                            if "매수" in sll_buy_name:
                                code = db_o.get('code')
                                order_qty = int(float(db_o.get('qty', 0)))
                                current_qty = holdings_map.get(code, 0)
                                
                                if current_qty >= order_qty:
                                    cm = auto_trade.ConclusionMonitor()
                                    with cm._lock:
                                        if db_odno in cm.processed_sim_fills: continue
                                        cm.processed_sim_fills.add(db_odno)
                                        
                                    if config.FILE_DEBUG_LEVEL == "DEBUG":
                                        logger.debug(f"[ORDER_DEBUG] 매수 주문({db_odno}) 잔고 입고 확인({current_qty}>={order_qty}) -> 체결 처리")
                                    # 원본 업데이트 제거 (접수 기록 보존)
                                    
                                    # [추가] 체결 히스토리 생성
                                    fill_price = _create_fill_history(db_o, "잔고 입고 확인")

                                    # [수정] 텔레그램 알림 (헬퍼 함수 사용)
                                    _send_sim_alert("매수", db_o, "잔고 입고 확인", fill_price)
                                    
                                    # [추가] AutoTrader 상태도 업데이트하여 중복 처리 방지
                                    trader = auto_trade.AutoTrader()
                                    if hasattr(trader, 'order_manager'):
                                        trader.update_order_status(code, db_odno, auto_trade.OrderStatus.FILLED)
                                        
                                    continue

                            # [DEBUG] 최종 추가
                            if config.FILE_DEBUG_LEVEL == "DEBUG":
                                logger.debug(f"[ORDER_DEBUG] -> Added to Open Orders List")
                            
                            # 시간 포맷 변환 (YYYY-MM-DD HH:MM:SS -> HHMMSS)
                            time_str = db_o.get('time', '')
                            ord_tmd = ""
                            if len(time_str) >= 19:
                                ord_tmd = time_str[11:19].replace(':', '')
                            
                            converted = {
                                'odno': db_odno,
                                'pdno': db_o.get('code'),
                                'prdt_name': db_o.get('name'),
                                'sll_buy_dvsn_cd_name': sll_buy_name,
                                'ord_qty': str(db_o.get('qty', 0)),
                                'ord_unpr': str(int(float(db_o.get('price', 0)))),
                                'rmn_qty': str(db_o.get('qty', 0)), # 잔량 정보가 없으므로 주문 수량으로 대체
                                'ord_tmd': ord_tmd,
                                '_is_db_fallback': True # DB 유래 플래그
                            }
                            dom_orders.append(converted)
                    except Exception as e:
                        logger.error(f"미체결 내역 DB 병합 중 오류: {e}")

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

    if not selectable_orders:
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
            if max_qty <= 0 and stock_info and stock_info.get('qty', 0) > 0:
                max_qty = int(stock_info['qty'])
            
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
        try:
            float(qty)
        except ValueError:
            config.console.print("[red]수량은 숫자만 입력 가능합니다.[/red]")
            return False

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
            # [추가] 미국 서머타임(DST) 및 현재 시장 시간 자동 판별
            now_utc = datetime.now(timezone.utc).replace(tzinfo=None)
            year = now_utc.year
            
            # 매년 3월 두 번째 일요일 07:00 UTC (02:00 EST) 서머타임 시작
            march_first = datetime(year, 3, 1)
            march_second_sunday = march_first + timedelta(days=(6 - march_first.weekday()) % 7 + 7)
            dst_start_utc = march_second_sunday.replace(hour=7, minute=0, second=0)
            
            # 매년 11월 첫 번째 일요일 06:00 UTC (02:00 EDT) 서머타임 종료
            nov_first = datetime(year, 11, 1)
            nov_first_sunday = nov_first + timedelta(days=(6 - nov_first.weekday()) % 7)
            dst_end_utc = nov_first_sunday.replace(hour=6, minute=0, second=0)
            
            is_dst = dst_start_utc <= now_utc < dst_end_utc
            now_et = now_utc - timedelta(hours=4 if is_dst else 5)
            time_et = now_et.time()
            
            time_0400 = datetime.strptime("04:00", "%H:%M").time()
            time_0930 = datetime.strptime("09:30", "%H:%M").time()
            time_1600 = datetime.strptime("16:00", "%H:%M").time()
            time_2000 = datetime.strptime("20:00", "%H:%M").time()
            
            if time_0400 <= time_et < time_0930:
                ord_dvsn = "32"
                ord_dvsn_name = "프리마켓"
            elif time_0930 <= time_et < time_1600:
                ord_dvsn = "00"
                ord_dvsn_name = "정규장"
            elif time_1600 <= time_et <= time_2000:
                ord_dvsn = "34"
                ord_dvsn_name = "애프터마켓"
            else:
                ord_dvsn = "31"
                ord_dvsn_name = "데이마켓(주간거래)"
                
            config.console.print(f"\n[cyan]◆ 미국 시장 시간 자동 판별: {ord_dvsn_name} [dim](현지시각: {now_et.strftime('%H:%M')}, 서머타임 {'적용' if is_dst else '미적용'})[/dim][/cyan]")

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
                now_time = datetime.now().strftime("%H%M")
                is_nxt_market = ("1530" <= now_time <= "2000") or ("0800" <= now_time <= "0850")
                
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
                    indicators.apply_realtime_price(df, calc_price)

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
            f"\n[bold white on {title_color}] [ {market_label} {title_text} 주문 최종 확인 ] [/]\n"
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
                odno = result.get('output', {}).get('ODNO') or result.get('output', {}).get('KRX_FWDG_ORD_ORGNO')
                
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
                    
                    if int(qty) >= int(max_qty):
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
                        auto_trade.schedule_buy_restriction_cleanup(stock_code, target_cano, target_acnt, is_overseas=is_overseas)
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
                if order_type == 'sell' and int(qty) >= int(max_qty):
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
        f"\n[bold white on magenta] [ {nation_str} 주문 {action_name} 최종 확인 ] [/]\n"
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
        from datetime import datetime
        now_time = datetime.now().strftime("%H%M")
        is_nxt_market = ("1530" <= now_time <= "2000") or ("0800" <= now_time <= "0850")
        
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
                odno = res_json.get('output', {}).get('ODNO') or res_json.get('output', {}).get('KRX_FWDG_ORD_ORGNO')
                if not odno and 'output' in res_json and 'ODNO' in res_json['output']:
                    odno = res_json['output']['ODNO']
                
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
                                    buy_price = float(h['pchs_avg_pric'])
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
                
                # [추가] 이미 체결/취소된 주문(40330000)인 경우 DB 상태 업데이트 (유령 주문 정리)
                if msg_cd == '40330000' and config.session.is_simulation:
                    config.console.print("[yellow]안내: 이미 체결되었거나 취소된 주문입니다. 상태를 업데이트합니다.[/yellow]")
                    # 원본 주문 보존 및 더미 이력(CLEAN) 생성으로 변경
                    db_manager.db.insert_trade(f"확인요망", pdno, prdt_name, final_qty, price, org_odno, order_status="체결/취소(추정)", reason="이미 체결/취소된 주문")
        except Exception as e:
            config.console.print(f"[red]에러: {e}[/]")

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
        return {"type": t, "value": v, "_label": f"점수{'≥' if ud=='1' else '≤'}{v}"}
    if choice == "2":  # RSI
        s = Prompt.ask("목표 RSI (예: 35 또는 70)")
        if not s or s.lower() in ['b', 'q']: return None
        try: v = float(s.strip())
        except ValueError:
            config.console.print("[red]RSI는 숫자만 입력 가능합니다.[/red]"); return None
        ud = Prompt.ask("1: RSI 이상, 2: RSI 이하", choices=["1", "2"], default="2")
        t = "RSI_UP" if ud == "1" else "RSI_DOWN"
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

def register_reserved_order():
    """예약 주문 등록 메뉴"""
    config.console.print("\n[bold yellow]예약 주문 등록 (Reserve Order)[/bold yellow]")
    
    target_cano, target_acnt, acc_label = select_account()
    if not target_cano:
        config.console.print("\n[yellow]예약 주문 등록이 취소되었습니다.[/yellow]")
        return None
    
    menu_items = [("1", "예약 매수", "Buy"), ("2", "예약 매도", "Sell")]
    choice = utils.show_menu("주문 방향", menu_items, default_choice="1")
    if choice.lower() in ['b', 'q']:
        config.console.print("\n[yellow]예약 주문 등록이 취소되었습니다.[/yellow]")
        return None
    
    menu_map_dict = dict((k, v) for k, v, _ in menu_items)
    context.USER_ACTION_BREADCRUMB.append(f"[{choice}] {menu_map_dict.get(choice, '')}")

    order_type = "buy" if choice == "1" else "sell"
    
    stock_info = {}
    if order_type == "sell":
        res = select_stock_from_balance(target_cano, target_acnt)
        if not res or res[0] in [None, False]:
            config.console.print("\n[yellow]예약 주문 등록이 취소되었습니다.[/yellow]")
            return None
        stock_code, stock_name, is_overseas, _, stock_info = res
    else:
        stock_code, stock_name, is_overseas = utils.select_target_stock()
        if not stock_code:
            config.console.print("\n[yellow]예약 주문 등록이 취소되었습니다.[/yellow]")
            return None

    market = "US" if is_overseas else "KR"
    
    # [추가] 현재가 및 매입단가(보유 시) 조회 (퍼센트 기준 계산용)
    current_price = api.get_current_price(stock_code, is_overseas)
    buy_price = 0.0
    if order_type == "sell":
        buy_price = float(stock_info.get('buy_price', 0.0))
                        
    price_info = f"현재가: ${current_price:,.2f}" if is_overseas else f"현재가: {int(current_price):,}원"
    if buy_price > 0:
        b_fmt = f"${buy_price:,.2f}" if is_overseas else f"{int(buy_price):,}원"
        price_info += f" / 매입단가: {b_fmt}"
        
    config.console.print(f"\n선택 종목: [bold cyan]{stock_name} ({stock_code})[/bold cyan] [dim]({price_info})[/dim]")
    
    base_price = buy_price if buy_price > 0 else current_price
    base_label = "매입단가" if buy_price > 0 else "현재가"
    
    analysis.print_table("", [(stock_name, stock_code)], is_overseas=is_overseas)
    config.console.print()
    
    # [추가] 예약 매매 일괄 취소 정책 안내
    config.console.print("[bold magenta]⚠️ 안내: 한 종목에 여러 예약 주문을 설정할 수 있으나, 어느 하나라도 체결되면 해당 종목에 설정된 나머지 모든 예약 주문(매수/매도)은 자동으로 일괄 취소됩니다.[/bold magenta]")
    config.console.print()
    
    # [정리] 단순 조건(지정가/특정시간/퀀트점수/RSI)은 단독 슬롯에서 제외하고 복합 조건(AND) 안으로 편입.
    #        단독 메뉴는 '가격 추세형'과 '시스템 고유/복합'만 노출하여 차별화 강화.
    cond_items = [
        ("1", "스탑로스/하향이탈 (STOP)", "현재가가 목표가 이하로 하락 시"),
        ("2", "돌파/상향이탈 (BREAKOUT)", "현재가가 목표가 이상으로 상승 시"),
        ("3", "트레일링 매수 (TRAILING_BUY)", "예약 후 최저점 바닥 다지고 N% 반등 시 (매수 전용)"),
        ("4", "트레일링 매도 (TRAILING_SELL)", "예약 후 최고점 달성 후 N% 하락 시 (매도 전용)"),
        ("5", "이평선 크로스 (EMA)", "주가가 특정 EMA를 상향돌파/하향이탈 시"),
        ("6", "수급 턴어라운드 (SMART_MONEY)", "외국인/기관 순매수 전환 시 (매수 전용)"),
        ("7", "상태 진입 (STATE)", "강매수/매수 상태 진입 시 (매수 전용)"),
        ("8", "신고가 돌파 (NEW_HIGH)", "52주/사상 신고가 경신 시 (추세추종 강세 진입)"),
        ("9", "복합 조건 (COMPOSITE)", "점수·RSI·지정가·시간 등 여러 조건 동시 충족(AND) 시 다중조건 결합")
    ]
    cond_choice = utils.show_menu("예약 발동 조건", cond_items)
    if cond_choice.lower() in ['b', 'q']:
        config.console.print("\n[yellow]예약 주문 등록이 취소되었습니다.[/yellow]")
        return None

    if order_type == 'sell' and cond_choice == '3':
        config.console.print("[red]트레일링 매수 조건은 '예약 매도'에서는 사용할 수 없습니다.[/red]")
        return None

    if order_type == 'buy' and cond_choice == '4':
        config.console.print("[red]트레일링 매도 조건은 '예약 매수'에서는 사용할 수 없습니다.[/red]")
        return None

    if order_type == 'sell' and cond_choice in ('6', '7'):
        config.console.print("[red]수급 턴어라운드/상태 진입 조건은 '예약 매수'에서만 사용할 수 있습니다.\n(매도 신호 결합은 9번 복합 조건을 활용하세요.)[/red]")
        return None

    condition_map = {"1": "STOP", "2": "BREAKOUT", "3": "TRAILING_BUY", "4": "TRAILING_SELL", "5": "EMA", "6": "SMART_MONEY", "7": "STATE", "8": "NEW_HIGH", "9": "COMPOSITE"}
    condition_type = condition_map[cond_choice]

    target_price = 0.0
    target_time = ""
    composite_json = None  # [추가] 복합 조건용
    
    # ※ 단순 조건(특정시간 TIME / 퀀트점수 SCORE / RSI / 지정가 LIMIT)은 단독 슬롯에서 제외되어
    #    복합 조건(COMPOSITE) 안의 서브 조건으로 편입되었습니다. (감시기는 레거시 단독 주문도 계속 지원)
    if condition_type == "EMA":
        config.console.print(f"\n[cyan]◆ 발동 목표 이동평균선(EMA) 선택[/cyan]")
        target_price_str = Prompt.ask("이동평균선 (5, 20, 60, 120 중 입력)", choices=["5", "20", "60", "120"], default="20")
        if not target_price_str or target_price_str.lower() in ['b', 'q']:
            config.console.print("\n[yellow]예약 주문 등록이 취소되었습니다.[/yellow]")
            return None
        target_price = float(target_price_str)
        updown = Prompt.ask("발동 방향 (1: 상향 돌파 시, 2: 하향 이탈 시) [dim](이전: b, 메인: q)[/dim]", choices=["1", "2", "b", "q"], default="1" if order_type == "buy" else "2")
        if updown.lower() in ['b', 'q']:
            config.console.print("\n[yellow]예약 주문 등록이 취소되었습니다.[/yellow]")
            return None
        condition_type = "EMA_UP" if updown == "1" else "EMA_DOWN"
        
    elif condition_type == "TRAILING_BUY":
        config.console.print(f"\n[cyan]◆ 반등 폭 설정 (트레일링 매수)[/cyan]")
        config.console.print("[dim]※ '최저점'은 본 예약 주문이 등록된 시점 이후부터 갱신된 가장 낮은 가격을 의미합니다.[/dim]")
        target_price_str = Prompt.ask("예약 후 최저점 대비 추격 매수할 반등 폭(%) 입력 (예: 3.0)")
        if not target_price_str or target_price_str.lower() in ['b', 'q']:
            config.console.print("\n[yellow]예약 주문 등록이 취소되었습니다.[/yellow]")
            return None
        try:
            target_price = float(target_price_str.replace('%', '').strip())
        except ValueError:
            config.console.print("\n[red]반등 폭은 숫자만 입력 가능합니다.[/red]")
            return None
    elif condition_type == "TRAILING_SELL":
        config.console.print(f"\n[cyan]◆ 하락 폭 설정 (트레일링 매도)[/cyan]")
        config.console.print("[dim]※ '최고점'은 본 예약 주문이 등록된 시점 이후부터 갱신된 가장 높은 가격을 의미합니다.[/dim]")
        target_price_str = Prompt.ask("예약 후 최고점 대비 하락 시 매도할 폭(%) 입력 (예: 3.0)")
        if not target_price_str or target_price_str.lower() in ['b', 'q']:
            config.console.print("\n[yellow]예약 주문 등록이 취소되었습니다.[/yellow]")
            return None
        try:
            target_price = float(target_price_str.replace('%', '').strip())
        except ValueError:
            config.console.print("\n[red]하락 폭은 숫자만 입력 가능합니다.[/red]")
            return None
    elif condition_type == "SMART_MONEY":
        config.console.print(f"\n[cyan]◆ 수급 턴어라운드 감지[/cyan]")
        config.console.print("[dim]※ 외국인/기관 수급이 순매수로 전환되는 신호를 포착하면 발동합니다. 별도 입력값이 없습니다.[/dim]")
        target_price = 0.0
    elif condition_type == "STATE":
        # [추세추종] 역매수(STATE_MR) 옵션 제거 — USE_MEAN_REVERSION OFF 고정으로 역매수 상태가
        #  분류되지 않아 영원히 발동하지 않는 죽은 조건이 됨 (기존 등록분 감시는 monitor가 계속 지원)
        config.console.print(f"\n[cyan]◆ 진입을 감지할 시스템 상태 선택[/cyan]")
        config.console.print("[dim]  - 강매수: 슈퍼모멘텀(신고가 주도주) / 매수: 일반 매수조건[/dim]")
        st = Prompt.ask("상태 선택 (1: 강매수, 2: 매수)", choices=["1", "2", "b", "q"], default="1")
        if st.lower() in ['b', 'q']:
            config.console.print("\n[yellow]예약 주문 등록이 취소되었습니다.[/yellow]")
            return None
        condition_type = {"1": "STATE_STRONGBUY", "2": "STATE_BUY"}[st]
        target_price = 0.0
    elif condition_type == "NEW_HIGH":
        config.console.print(f"\n[cyan]◆ 신고가 돌파 기준 선택[/cyan]")
        config.console.print("[dim]  - 직전 구간 최고가를 현재가가 경신하면 발동합니다. (목표가 입력 불필요, 자동 감지)[/dim]")
        nh = Prompt.ask("기준 (1: 52주 신고가, 2: 사상 최고가)", choices=["1", "2", "b", "q"], default="1")
        if nh.lower() in ['b', 'q']:
            config.console.print("\n[yellow]예약 주문 등록이 취소되었습니다.[/yellow]")
            return None
        target_price = 250.0 if nh == "1" else 0.0  # 0=전체기간(사상), 그 외=거래일 룩백
    elif condition_type == "COMPOSITE":
        composite_subs = _build_composite_conditions(base_price, is_overseas)
        if not composite_subs:
            config.console.print("\n[yellow]예약 주문 등록이 취소되었습니다.[/yellow]")
            return None
        composite_json = json.dumps([{"type": s["type"], "value": s.get("value")} for s in composite_subs], ensure_ascii=False)
        target_price = 0.0
    else:
        config.console.print(f"\n[cyan]◆ 발동 조건 가격(목표가) 입력[/cyan]")
        config.console.print(f"[dim]  - 절대 가격: 50000 (해당 금액 도달 시 발동)[/dim]")
        config.console.print(f"[dim]  - 퍼센트(%): +5%, -3% ({base_label} 대비 설정)[/dim]")
        target_price_str = Prompt.ask("목표가 입력")
        if not target_price_str or target_price_str.lower() in ['b', 'q']:
            config.console.print("\n[yellow]예약 주문 등록이 취소되었습니다.[/yellow]")
            return None
        if '%' in target_price_str:
            try:
                pct = float(target_price_str.replace('%', '').strip())
                target_price = base_price * (1 + (pct / 100.0))
                if not is_overseas: target_price = int(target_price)
                t_fmt = f"${target_price:,.2f}" if is_overseas else f"{int(target_price):,}원"
                config.console.print(f"[dim] -> {base_label} 기준 {pct:+.2f}% 계산된 목표가: {t_fmt}[/dim]")
            except ValueError:
                config.console.print("\n[red]퍼센트는 숫자만 입력 가능합니다.[/red]")
                return None
        else:
            try:
                target_price = float(target_price_str.replace(',', '').replace('$', '').replace('원', '').strip())
            except ValueError:
                config.console.print("\n[red]목표가는 숫자만 입력 가능합니다.[/red]")
                return None
        
    config.console.print(f"\n[cyan]◆ 주문 실행 단가 입력 (지정가 / 시장가)[/cyan]")
    is_price_target = condition_type in ["STOP", "BREAKOUT", "LIMIT"]
    
    config.console.print(f"[dim]  - 절대 가격: 49900 (해당 가격으로 주문)[/dim]")
    config.console.print(f"[dim]  - 퍼센트(%): -1% ({base_label} 대비 가격으로 주문)[/dim]")
    config.console.print(f"[dim]  - 0: 시장가 주문 (발동 시점의 시장가 또는 슬리피지가 반영된 현재가 지정가)[/dim]")
    if is_price_target:
        config.console.print(f"[dim]  - 엔터(빈 값): [bold yellow]발동 조건(목표가)과 완전히 동일한 가격[/bold yellow]으로 자동 설정[/dim]")
    else:
        config.console.print(f"[dim]  - 엔터(빈 값): [bold yellow]기준가격({base_label})과 완전히 동일한 가격[/bold yellow]으로 자동 설정[/dim]")
        
    order_price_str = Prompt.ask("주문 단가 입력", default="")
    if order_price_str.lower() in ['b', 'q']:
        config.console.print("\n[yellow]예약 주문 등록이 취소되었습니다.[/yellow]")
        return None
    
    if order_price_str.lower() == 'm':
        order_price_str = "0"
        
    if order_price_str == "0":
        order_price = 0.0
        config.console.print(f"[dim] -> 발동 시점의 시장가로 자동 설정[/dim]")
    elif not order_price_str:
        if is_price_target:
            order_price = target_price
            op_fmt = f"${order_price:,.2f}" if is_overseas else f"{int(order_price):,}원"
            config.console.print(f"[dim] -> 발동 조건(목표가)과 동일하게 자동 설정: {op_fmt}[/dim]")
        else:
            order_price = base_price
            op_fmt = f"${order_price:,.2f}" if is_overseas else f"{int(order_price):,}원"
            config.console.print(f"[dim] -> {base_label}와 동일하게 자동 설정: {op_fmt}[/dim]")
    elif '%' in order_price_str:
        try:
            pct = float(order_price_str.replace('%', '').strip())
            order_price = base_price * (1 + (pct / 100.0))
            if not is_overseas: order_price = int(order_price)
            op_fmt = f"${order_price:,.2f}" if is_overseas else f"{int(order_price):,}원"
            config.console.print(f"[dim] -> {base_label} 기준 {pct:+.2f}% 계산된 주문 단가: {op_fmt}[/dim]")
        except ValueError:
            config.console.print("\n[red]퍼센트는 숫자만 입력 가능합니다.[/red]")
            return None
    else:
        try:
            order_price = float(order_price_str.replace(',', '').replace('$', '').replace('원', '').strip())
        except ValueError:
            config.console.print("\n[red]주문 단가는 숫자만 입력 가능합니다.[/red]")
            return None
    
    if order_type == "sell":
        max_qty = int(stock_info.get('qty', 0))
        qty_str = Prompt.ask(f"주문 수량(주) [dim](보유 잔고: {max_qty:,}주)[/dim]", default=str(max_qty))
    else:
        qty_str = Prompt.ask("주문 수량(주)")
        
    if not qty_str or qty_str.lower() in ['b', 'q']:
        config.console.print("\n[yellow]예약 주문 등록이 취소되었습니다.[/yellow]")
        return None
    try:
        qty = int(qty_str.replace(',', '').replace('주', '').strip())
    except ValueError:
        config.console.print("\n[red]수량은 숫자만 입력 가능합니다.[/red]")
        return None
    
    if order_type == "sell" and qty > max_qty:
        config.console.print(f"[red]⚠️ 입력하신 수량({qty:,}주)이 보유 수량({max_qty:,}주)보다 많습니다. 잔고 전량으로 자동 조정합니다.[/red]")
        qty = max_qty
    
    config.console.print(f"\n[cyan]◆ 유효 기간 (만료일) 설정[/cyan]")
    config.console.print(f"[dim]  - 1: 당일 (오늘 장 마감 전까지)[/dim]")
    config.console.print(f"[dim]  - 2: 이번 주 (7일 후까지)[/dim]")
    config.console.print(f"[dim]  - 3: 이번 달 (30일 후까지)[/dim]")
    config.console.print(f"[dim]  - 4: 무기한 (취소할 때까지 유지)[/dim]")
    config.console.print(f"[dim]  - 직접 입력: YYYYMMDD (예: 20261231)[/dim]")
    expire_choice = Prompt.ask("유효 기간 선택 또는 입력", default="1")
    if expire_choice.lower() in ['b', 'q']:
        config.console.print("\n[yellow]예약 주문 등록이 취소되었습니다.[/yellow]")
        return None
    
    today_dt = datetime.now()
    if expire_choice == "1": expire_dt = today_dt.strftime("%Y%m%d")
    elif expire_choice == "2": expire_dt = (today_dt + timedelta(days=7)).strftime("%Y%m%d")
    elif expire_choice == "3": expire_dt = (today_dt + timedelta(days=30)).strftime("%Y%m%d")
    elif expire_choice == "4": expire_dt = "20991231"
    else: expire_dt = "".join(filter(str.isdigit, expire_choice))
    if not expire_dt or len(expire_dt) < 8: expire_dt = "20991231"
    
    t_type_str = "[red]매수[/red]" if order_type == "buy" else "[blue]매도[/blue]"
    
    if condition_type == "TIME":
        if len(target_time) >= 12: tt_disp = f"{target_time[:4]}-{target_time[4:6]}-{target_time[6:8]} {target_time[8:10]}:{target_time[10:12]}"
        elif len(target_time) == 4: tt_disp = f"{target_time[:2]}:{target_time[2:4]}"
        else: tt_disp = target_time
        cond_str = f"시간 {tt_disp}"
    elif 'SCORE' in condition_type:
        cond_str = f"점수 {target_price}점 {'이상' if 'UP' in condition_type else '이하'}"
    elif 'RSI' in condition_type:
        cond_str = f"RSI {target_price} {'이상' if 'UP' in condition_type else '이하'}"
    elif 'EMA' in condition_type:
        cond_str = f"EMA {int(target_price)}일선 {'상향돌파' if 'UP' in condition_type else '하향이탈'}"
    elif condition_type == 'TRAILING_BUY':
        cond_str = f"최저점 대비 {target_price}% 반등 시"
    elif condition_type == 'TRAILING_SELL':
        cond_str = f"최고점 대비 {target_price}% 하락 시"
    elif condition_type == 'SMART_MONEY':
        cond_str = "수급 턴어라운드(외국인/기관 순매수 전환)"
    elif condition_type == 'NEW_HIGH':
        cond_str = ("사상 최고가 경신 시" if target_price == 0 else f"{int(target_price)}거래일(52주) 신고가 경신 시")
    elif condition_type.startswith('STATE_'):
        sv = {'STATE_STRONGBUY': '강매수', 'STATE_BUY': '매수', 'STATE_MR': '역매수'}.get(condition_type, condition_type)
        cond_str = f"시스템 상태 '{sv}' 진입"
    elif condition_type == 'COMPOSITE':
        cond_str = "복합(AND): " + " AND ".join(s['_label'] for s in composite_subs)
    else:
        if is_overseas:
            cond_str = f"목표가 ${target_price:,.2f}"
        else:
            cond_str = f"목표가 {int(target_price):,}원"
    
    expire_disp = "무기한" if expire_dt == "20991231" else f"{expire_dt[:4]}-{expire_dt[4:6]}-{expire_dt[6:8]}까지"
    
    if order_price == 0:
        op_disp = "시장가"
    else:
        op_disp = f"${order_price:,.2f}" if is_overseas else f"{int(order_price):,}원"

    confirm_msg = (
        f"\n[bold white on yellow] [ 예약 {t_type_str.replace('[/red]', '').replace('[/blue]', '').replace('[red]', '').replace('[blue]', '')} 주문 최종 확인 ] [/]\n"
        f" 종목: {stock_name} ({stock_code})\n"
        f" 조건: [bold]{condition_type}[/bold] ({cond_str})\n"
        f" 주문: {t_type_str} {qty}주 @ {op_disp} (지정가)\n"
        f" 유효: {expire_disp}\n"
    )
    config.console.print(Panel(confirm_msg, expand=False))
    
    ans = Prompt.ask("위 내용으로 예약 주문을 시스템에 등록하시겠습니까? [dim](이전: b, 메인: q)[/dim]", choices=["y", "n", "b", "q"], default="n")
    if ans.lower() in ['b', 'q', 'n']:
        config.console.print("\n[yellow]예약 주문 등록이 취소되었습니다.[/yellow]")
        return None
        
    if ans == "y":
        db_manager.db.insert_reserved_order(
            cano=target_cano, acnt=target_acnt, market=market,
            order_type=order_type, code=stock_code, name=stock_name,
            qty=qty, order_price=order_price,
            condition_type=condition_type, target_price=target_price, target_time=target_time, expire_dt=expire_dt,
            composite_json=composite_json
        )
        config.console.print()
        config.console.print("[bold green]예약 주문이 성공적으로 등록되었습니다.[/bold green]")
        
        # [추가] 예약 주문 등록 시 텔레그램 알림 전송
        clean_type_str = t_type_str.replace('[/red]', '').replace('[/blue]', '').replace('[red]', '').replace('[blue]', '')
        api.send_telegram_message(f"📝 [예약 등록] {stock_name}({stock_code})\n구분: {clean_type_str}\n조건: {condition_type} ({cond_str})\n수량: {qty}주\n지정가: {op_disp}\n유효: {expire_disp}")
        
        config.console.print()
        _print_reserved_orders_table()

def _print_reserved_orders_table():
    """예약 주문 대기 목록을 테이블 형태로 출력하고 주문 목록을 반환합니다."""
    orders = db_manager.db.get_pending_reserved_orders()
    if not orders:
        config.console.print("[yellow]현재 대기 중인 예약 주문이 없습니다.[/yellow]")
        return orders
        
    table = Table(title="예약 주문 대기 목록", box=box.HORIZONTALS, header_style="dim", border_style="dim")
    table.add_column("ID", justify="center", style="cyan")
    table.add_column("계좌번호", justify="center", style="dim")
    table.add_column("계좌구분", justify="center")
    table.add_column("종목명", justify="left")
    table.add_column("코드", justify="center", style="dim")
    table.add_column("구분", justify="center")
    table.add_column("조건", justify="center")
    table.add_column("주문(수량@지정가)", justify="right")
    table.add_column("등록일시", justify="center", style="dim")
    table.add_column("유효기간", justify="center")
    
    for o in orders:
        is_ovs = (o.get('market') == 'US')
        
        cano = o.get('cano', '')
        acnt = o.get('acnt', '')
        
        acc_label = "기타"
        if config.session.is_simulation:
            if cano == config.session.cano and acnt == config.session.acnt_prdt_cd:
                acc_label = "[bold yellow]모의[/bold yellow]"
        else:
            if cano == config.session.cano and acnt == config.session.acnt_prdt_cd:
                acc_label = "[bold yellow]실전[/bold yellow]"
            elif config.session.auto_cano and cano == config.session.auto_cano and acnt == config.session.auto_acnt_prdt_cd:
                acc_label = "[bold orange3]자동[/bold orange3]"
                
        if o['condition_type'] == 'TIME':
            tt = o['target_time']
            if len(tt) >= 12: t_str = f"{tt[:4]}-{tt[4:6]}-{tt[6:8]} {tt[8:10]}:{tt[10:12]}"
            elif len(tt) == 4: t_str = f"{tt[:2]}:{tt[2:4]}"
            else: t_str = tt
        else:
            if 'SCORE' in o['condition_type']:
                t_str = f"{o['target_price']}점 {'이상' if 'UP' in o['condition_type'] else '이하'}"
            elif 'RSI' in o['condition_type']:
                t_str = f"RSI {o['target_price']} {'이상' if 'UP' in o['condition_type'] else '이하'}"
            elif 'EMA' in o['condition_type']:
                t_str = f"EMA {int(o['target_price'])} {'돌파' if 'UP' in o['condition_type'] else '이탈'}"
            elif o['condition_type'] == 'TRAILING_BUY':
                t_str = f"바닥 대비 {o['target_price']}% 반등"
            elif o['condition_type'] == 'TRAILING_SELL':
                t_str = f"고점 대비 {o['target_price']}% 하락"
            elif o['condition_type'] == 'SMART_MONEY':
                t_str = "외국인/기관 순매수 전환"
            elif o['condition_type'] == 'NEW_HIGH':
                t_str = "사상 최고가 경신" if o['target_price'] == 0 else "52주 신고가 경신"
            elif o['condition_type'].startswith('STATE_'):
                t_str = {'STATE_STRONGBUY': '강매수 진입', 'STATE_BUY': '매수 진입', 'STATE_MR': '역매수 진입'}.get(o['condition_type'], o['condition_type'])
            elif o['condition_type'] == 'COMPOSITE':
                t_str = _composite_summary(o.get('composite_json'))
            else:
                if is_ovs:
                    t_str = f"${o['target_price']:,.2f}"
                else:
                    t_str = f"{int(o['target_price']):,}원"
                
        cond_str = f"{o['condition_type'].replace('_UP','').replace('_DOWN','')} ({t_str})"
        
        if o['order_price'] == 0:
            op_disp = "시장가"
        else:
            op_disp = f"${o['order_price']:,.2f}" if is_ovs else f"{int(o['order_price']):,}원"
            
        ord_str = f"{o['qty']}주 @ {op_disp}"
        t_type = "[red]매수[/]" if o['order_type'] == 'buy' else "[blue]매도[/]"
        acc_str = f"{cano}-{acnt}"
        
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
        table.add_row(str(o['id']), acc_str, acc_label, o['name'], o['code'], t_type, cond_str, ord_str, created_str, exp_str)
        
    config.console.print(table)
    return orders
            
def manage_reserved_orders():
    """예약 주문 관리 메뉴"""
    while True:
        config.console.print("\n[bold green]예약 주문 관리 내역[/bold green]")
        config.console.print()
        orders = _print_reserved_orders_table()
        if not orders:
            return None
            
        config.console.print()
        cancel_input = Prompt.ask("취소할 예약 주문 ID 선택 [dim](다중: 1,3 / 전체: 0 / 이전: b, 메인: q, 취소: Enter)[/dim]", default="")
        if not cancel_input or cancel_input.lower() in ['b', 'q']:
            if cancel_input and cancel_input.lower() in ['b', 'q']:
                config.console.print("\n[yellow]입력이 취소되었습니다.[/yellow]")
                return None
            return False
        
        if cancel_input.strip() == "0":
            cancel_ids = [str(o['id']) for o in orders]
        else:
            cancel_ids = [cid.strip() for cid in cancel_input.split(',') if cid.strip().isdigit()]
        
        if cancel_ids:
            canceled_list = []
            for cid in cancel_ids:
                target_order = next((o for o in orders if str(o['id']) == cid), None)
                
                if target_order:
                    db_manager.db.update_reserved_order_status(int(cid), 'CANCELED')
                    canceled_list.append(cid)
            
                    t_type = "매수" if target_order['order_type'] == 'buy' else "매도"
                    cond_str = target_order['condition_type']
                    api.send_telegram_message(f"🗑️ [예약 수동 취소] {target_order['name']}({target_order['code']})\n사용자에 의해 대기 중이던 예약 {t_type} 주문(ID: {cid})이 취소되었습니다.\n조건: {cond_str}")
                    
                    # [추가] 수동 취소 내역 거래내역에 기록
                    db_manager.db.insert_trade(
                        f"{t_type}취소(예약)", target_order['code'], target_order['name'], target_order['qty'], 
                        target_order.get('order_price', 0), f"RES_CAN_{cid}", 
                        order_status="취소", reason=f"수동 취소 (조건: {cond_str})"
                    )
                    
            config.console.print()
            if canceled_list:
                config.console.print(f"[bold green]예약 주문(ID: {', '.join(canceled_list)})이 취소되었습니다.[/bold green]")
            else:
                config.console.print("[yellow]취소할 수 있는 유효한 예약 주문 ID가 없습니다.[/yellow]")

def get_reserved_orders_summary():
    """텔레그램 전송용 예약 주문 현황 요약 문자열 생성"""
    orders = db_manager.db.get_pending_reserved_orders()
    if not orders:
        return "📭 현재 대기 중인 예약 주문이 없습니다."
        
    msg = f"⏳ [대기 중인 예약 주문 현황] (총 {len(orders)}건)\n\n"
    for o in orders:
        is_ovs = (o.get('market') == 'US')
        
        cano = o.get('cano', '')
        acnt = o.get('acnt', '')
        
        acc_label = "기타"
        if config.session.is_simulation:
            if cano == config.session.cano and acnt == config.session.acnt_prdt_cd:
                acc_label = "모의"
        else:
            if cano == config.session.cano and acnt == config.session.acnt_prdt_cd:
                acc_label = "실전"
            elif config.session.auto_cano and cano == config.session.auto_cano and acnt == config.session.auto_acnt_prdt_cd:
                acc_label = "자동"
                
        if o['condition_type'] == 'TIME':
            tt = o['target_time']
            if len(tt) >= 12: t_str = f"{tt[:4]}-{tt[4:6]}-{tt[6:8]} {tt[8:10]}:{tt[10:12]}"
            elif len(tt) == 4: t_str = f"{tt[:2]}:{tt[2:4]}"
            else: t_str = tt
        else:
            if 'SCORE' in o['condition_type']:
                t_str = f"{o['target_price']}점 {'이상' if 'UP' in o['condition_type'] else '이하'}"
            elif 'RSI' in o['condition_type']:
                t_str = f"RSI {o['target_price']} {'이상' if 'UP' in o['condition_type'] else '이하'}"
            elif 'EMA' in o['condition_type']:
                t_str = f"EMA {int(o['target_price'])} {'돌파' if 'UP' in o['condition_type'] else '이탈'}"
            elif o['condition_type'] == 'TRAILING_BUY':
                t_str = f"바닥 대비 {o['target_price']}% 반등"
            elif o['condition_type'] == 'TRAILING_SELL':
                t_str = f"고점 대비 {o['target_price']}% 하락"
            elif o['condition_type'] == 'SMART_MONEY':
                t_str = "외국인/기관 순매수 전환"
            elif o['condition_type'] == 'NEW_HIGH':
                t_str = "사상 최고가 경신" if o['target_price'] == 0 else "52주 신고가 경신"
            elif o['condition_type'].startswith('STATE_'):
                t_str = {'STATE_STRONGBUY': '강매수 진입', 'STATE_BUY': '매수 진입', 'STATE_MR': '역매수 진입'}.get(o['condition_type'], o['condition_type'])
            elif o['condition_type'] == 'COMPOSITE':
                t_str = _composite_summary(o.get('composite_json'))
            else:
                if is_ovs:
                    t_str = f"${o['target_price']:,.2f}"
                else:
                    t_str = f"{int(o['target_price']):,}원"
            
        cond_str = f"{o['condition_type'].replace('_UP','').replace('_DOWN','')} ({t_str})"
        
        if o['order_price'] == 0:
            op_disp = "시장가"
        else:
            op_disp = f"${o['order_price']:,.2f}" if is_ovs else f"{int(o['order_price']):,}원"
            
        ord_str = f"{o['qty']}주 @ {op_disp}"
        t_type = "🔴 매수" if o['order_type'] == 'buy' else "🔵 매도"
        
        exp = o.get('expire_dt', '20991231')
        exp_str = "무기한" if not exp or exp == "20991231" else f"{exp[:4]}-{exp[4:6]}-{exp[6:8]}"
        
        created_at = o.get('created_at', '')
        created_str = "-"
        if created_at:
            try:
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
