# modules/trading.py
import logging
from rich.prompt import Prompt
from rich.panel import Panel
from rich.table import Table
from rich import box
from rich.progress import Progress, SpinnerColumn, BarColumn, TextColumn
import json
import time
from datetime import datetime
import config
import context # [추가]
import api
import utils
from modules import account
import indicators # [추가] ATR 계산을 위해 추가
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
        grid = Table.grid(padding=(0, 2))
        grid.add_column(justify="left")
        grid.add_column(justify="left", style="dim")
        grid.add_row(f"[1] {acc_label}", f"(Main): {config.session.cano}-{config.session.acnt_prdt_cd}")
        grid.add_row(f"[2] 자동투자", f"(Auto): {config.session.auto_cano}-{config.session.auto_acnt_prdt_cd}")
        config.console.print(grid)
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
    grid = Table.grid(padding=(0, 2))
    grid.add_column(justify="left")
    grid.add_column(justify="left", style="dim")
    grid.add_row("[1] 국내 주식", "(Domestic)")
    grid.add_row("[2] 해외 주식", "(Overseas)")
    config.console.print(grid)
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
        with Progress(
            SpinnerColumn(),
            TextColumn("[progress.description]{task.description}"),
            console=config.console,
            transient=True
        ) as progress:
            progress.add_task("[green]국내 잔고 조회 중...[/]", total=None)
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
            console=config.console,
            transient=True
        ) as progress:
            progress.add_task("[green]해외 잔고 조회 중...[/]", total=None)
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
            
            # [추가] None 값 안전 처리 (DB 저장 실패 방지)
            try: profit_amt = int(float(db_order.get('profit_amt') or 0))
            except: profit_amt = 0
            try: profit_rate = float(db_order.get('profit_rate') or 0.0)
            except: profit_rate = 0.0
            
            # [추가] snapshot 데이터 타입 안전 처리
            snapshot_data = db_order.get('snapshot')
            if isinstance(snapshot_data, dict):
                snapshot_data = json.dumps(snapshot_data, ensure_ascii=False)
            
            if config.FILE_DEBUG_LEVEL == "DEBUG":
                logger.debug(f"[ORDER_DEBUG] insert_trade 호출 시도: {odno}")

            # [수정] 큐 시스템 적용으로 단순 호출로 변경
            db_manager.db.insert_trade(
                type_str, 
                db_order.get('code'), 
                db_order.get('name'), 
                int(float(db_order.get('qty', 0))), 
                float(db_order.get('price', 0)), 
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
        else:
            if config.FILE_DEBUG_LEVEL == "DEBUG":
                logger.debug(f"[ORDER_DEBUG] 이미 체결 내역 존재하여 생성 스킵: {odno}")
    except Exception as e:
        logger.error(f"[ORDER_DEBUG] 체결 히스토리 생성 실패: {e}", exc_info=True)

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
                        except: pass
                        
                        # [DEBUG] 잔고 상태 로깅
                        if config.FILE_DEBUG_LEVEL == "DEBUG":
                            logger.debug(f"[ORDER_DEBUG] Holdings Map: {holdings_map}")

                        # 이미 API로 조회된 주문번호 집합
                        api_odnos = set(str(o.get('odno')) for o in dom_orders if o.get('odno'))
                        
                        current_acc_str = f"{cano}-{acnt}"

                        # [추가] 모의투자 체결 알림 헬퍼 함수 (실전 포맷 적용)
                        def _send_sim_alert(type_name, db_order, reason_msg):
                            try:
                                code = db_order.get('code')
                                name = db_order.get('name')
                                qty = db_order.get('qty')
                                price = float(db_order.get('price', 0))
                                
                                custom_rules = db_manager.db.get_all_stock_strategies()
                                rules_map = {r['code']: r for r in custom_rules}
                                rule = rules_map.get(code)
                                
                                title_tag = "[체결 알림(추정)]"
                                rule_info = ""
                                if rule:
                                    title_tag += " [개별]"
                                    rule_info = f"\n🔧 [개별 룰] 익절 +{rule['take_profit']}% / 손절 {rule['stop_loss']}%"
                                    if rule.get('ts_activation'):
                                        rule_info += f" / TS +{rule['ts_activation']}%(-{rule['ts_callback']}%)"
                                
                                cur_info = ""
                                try:
                                    cp_data = api.get_current_price_data(code, is_overseas=False)
                                    if cp_data.get('rt_cd') == '0':
                                        curr = float(cp_data['output']['stck_prpr'])
                                        rate = float(cp_data['output']['prdy_ctrt'])
                                        icon = "🔺" if rate > 0 else ("🔻" if rate < 0 else "➖")
                                        cur_info = f"\n현재가: {int(curr):,}원 ({icon} {rate:+.2f}%)"
                                except: pass
                                
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
                                    except: pass

                                exec_amt = int(price * qty)
                                msg = f"✅ {title_tag} {type_name} {name}({code})\n수량: {qty}주 / 단가: {price:,.0f}원(주문가) / 금액: {exec_amt:,}원\n사유: {reason_msg}{cur_info}{strategy_info}{rule_info}"
                                api.send_telegram_message(msg)
                                
                                # [수정] 중복 DB 저장 로직 제거 (_create_fill_history에서 이미 수행)
                                    
                            except: pass

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
                                if cur_qty == 0:
                                    if config.FILE_DEBUG_LEVEL == "DEBUG":
                                        logger.debug(f"[ORDER_DEBUG] 매도 주문({db_odno}) 잔고 0 확인 -> 체결 처리 시작")
                                    # [수정] 원본 업데이트 제거
                                    # db_manager.db.update_trade(db_odno, order_status="체결(추정)")
                                    
                                    # [추가] 체결 히스토리 생성
                                    _create_fill_history(db_o, "잔고 0 확인 (API 누락 보정)")

                                    if config.FILE_DEBUG_LEVEL == "DEBUG":
                                        logger.debug(f"[ORDER_DEBUG] 매도 주문({db_odno}) 체결 처리 완료 (알림 전송 예정)")

                                    # [수정] 텔레그램 알림 (헬퍼 함수 사용)
                                    _send_sim_alert("매도", db_o, "잔고 0 확인 (API 누락 보정)")
                                    
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
                                    if config.FILE_DEBUG_LEVEL == "DEBUG":
                                        logger.debug(f"[ORDER_DEBUG] 매수 주문({db_odno}) 잔고 입고 확인({current_qty}>={order_qty}) -> 체결 처리")
                                    # [수정] 원본 업데이트 제거
                                    # db_manager.db.update_trade(db_odno, order_status="체결(추정)")
                                    
                                    # [추가] 체결 히스토리 생성
                                    _create_fill_history(db_o, "잔고 입고 확인 (API 누락 보정)")

                                    # [수정] 텔레그램 알림 (헬퍼 함수 사용)
                                    _send_sim_alert("매수", db_o, "잔고 입고 확인 (API 누락 보정)")
                                    
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
                    odno_disp = str(order.get('odno'))
                    if order.get('_is_db_fallback'):
                        odno_disp += " [dim](DB)[/dim]"

                    table.add_row(str(idx), f"{cano}-{acnt}", acc_disp, "[bold]국내[/]", ord_time, odno_disp, display_name, sll_buy_colored, order.get('ord_qty'), f"{api.safe_int(order.get('ord_unpr')):,.0f}", cur_price_str, rmn_qty)
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
            grid = Table.grid(padding=(0, 2))
            grid.add_column(justify="left")
            grid.add_column(justify="left", style="dim")
            grid.add_row("[1] 국내 주식", "(Domestic Stock)")
            grid.add_row("[2] 국내 ETF", "(Domestic ETF)")
            grid.add_row("[3] 미국 주식", "(US Stock)")
            grid.add_row("[4] 미국 ETF", "(US ETF)")
            grid.add_row("[5] 직접 입력", "(Direct Input)")
            config.console.print(grid)
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
        context.USER_ACTION_BREADCRUMB.append(f"[수량] {qty}")
        qty = qty.replace(',', '')

        unit = "달러" if is_overseas else "원"
        price_prompt = f"[{title_color}]{title_text} 단가({unit})[/] [dim]0 입력 시 시장가(현재가), 취소: q[/dim]"
        price = Prompt.ask(price_prompt, default="0")
        if price.lower() == 'q': return
        context.USER_ACTION_BREADCRUMB.append(f"[단가] {price}")
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
                    ind = indicators.calculate_indicators(df)
                    indicator_info = ind
                    
                    # 점수 계산
                    current_price_val = float(df.iloc[-1]['close'])
                    score, _ = analysis.calculate_score(
                        current_price_val, ind['ema_20'], ind['ema_60'], ind['ema_120'], 
                        ind['psar'], ind['rsi'], ind['adx'], ind['cci'], ind.get('obv_trend'), ind.get('macd'), ind.get('macd_signal')
                    )
                    calculated_score = score

                    # [추가] ATR 손절 적용 (수동 매수 시에도 계산)
                    if config.SELL_STRATEGY.get("USE_ATR_STOP", False):
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
            except: pass

        confirm_msg = (
            f"\n[bold white on {title_color}] [ {market_label} {title_text} 주문 최종 확인 ] [/]\n"
            f" 종목: [bold]{stock_name} ({stock_code})[/bold]{excd_info}\n"
            f" 수량: [bold]{qty}주[/bold]\n"
            f" 단가: [bold]{display_price}[/bold]\n"
            f" 총액: [bold]{amt_str}[/bold]"
            f"{sl_msg}\n"
        )
        config.console.print(Panel(confirm_msg, expand=False, width=60))
        
        if Prompt.ask("\n위 내용으로 주문을 전송하시겠습니까?", choices=["y", "n"], default="n") != "y":
            config.console.print("[yellow]주문이 취소되었습니다.[/yellow]")
            return

        # 8. 주문 전송
        market_api_param = "overseas" if is_overseas else "domestic"
        
        if config.FILE_DEBUG_LEVEL == "DEBUG":
            logger.debug(f"REQ (Order-{market_label}) | {order_type} | {stock_code} {qty}ea")

        logger.info(f"운영자 실행: {' - '.join(context.USER_ACTION_BREADCRUMB)}")

        try:
            result = None
            # [수정] 단일 API 호출이므로 status 사용
            with config.console.status("[bold green]주문 전송 중...[/]"):
                result = api.place_order(market_api_param, order_type, stock_code, qty, price, ord_dvsn, exchange_code=excd)
            
            if result['rt_cd'] == '0':
                odno = result.get('output', {}).get('ODNO') or result.get('output', {}).get('KRX_FWDG_ORD_ORGNO')
                config.console.print(f"[bold green]주문 성공[/bold green] (주문번호: {odno})")
                
                # [추가] AutoTrader에 주문 상태 등록 (중복 매매 방지)
                trader = auto_trade.AutoTrader()
                # [수정] OrderManager를 통해 등록 (리팩토링 대응)
                if hasattr(trader, 'order_manager'):
                    trader.order_manager.register_manual_order(stock_code, odno)
                
                # 텔레그램 알림
                t_type = "매수" if order_type == 'buy' else "매도"
                msg = f"🚀 [수동 주문] {t_type} 접수\n종목: {stock_name} ({stock_code})\n수량: {qty}주\n단가: {display_price}\n주문번호: {odno}"
                
                if order_type == 'buy':
                    if calculated_score > 0:
                        rsi_str = f"{indicator_info.get('rsi', 0):.1f}" if indicator_info.get('rsi') is not None else "-"
                        msg += f"\n📊 [지표] 점수:{calculated_score}점 / RSI:{rsi_str}"

                    if stop_loss_rate_to_save != 0.0:
                        msg += f"\n📉 [ATR 손절] {stop_loss_rate_to_save:.2f}% 설정 (ATR손절 적용)"
                
                api.send_telegram_message(msg)
                
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

                db_manager.db.insert_trade(f"{t_type}(수동)", stock_code, stock_name, qty, price, odno, snapshot=snapshot, reason="사용자 수동 주문", profit_amt=profit_amt, profit_rate=profit_rate, stop_loss_rate=stop_loss_rate_to_save, score=calculated_score)
                
                # 매도 시 트레일링 스탑 초기화
                if order_type == 'sell':
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
    logger.info(f"운영자 실행: {' - '.join(context.USER_ACTION_BREADCRUMB)}")

    # [수정] 공통 함수 show_open_orders()를 사용하여 미체결 내역 조회 및 출력
    selectable_orders = show_open_orders()

    if not selectable_orders:
        return

    # =========================================================================
    # 4. 선택 및 분기 처리
    # =========================================================================
    choice = Prompt.ask("\n선택 번호 [dim](취소: q/Enter)[/dim]", default="q", show_default=False)
    if choice.lower() == 'q': return
    context.USER_ACTION_BREADCRUMB.append(f"[주문선택] {choice}")
    
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
    grid = Table.grid(padding=(0, 2))
    grid.add_column(justify="left")
    grid.add_column(justify="left", style="dim")
    grid.add_row("[1] 정정", "(Modify)")
    grid.add_row("[2] 취소", "(Cancel)")
    config.console.print(grid)
    config.console.print()
    action = Prompt.ask("작업 선택 [dim](취소: q)[/dim]", choices=["1", "2", "q"], default="1")
    if action.lower() == 'q': return
    
    action_map = {"1": "정정", "2": "취소"}
    if action in action_map: context.USER_ACTION_BREADCRUMB.append(f"[{action}] {action_map[action]}")

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
        context.USER_ACTION_BREADCRUMB.append(f"[수량] {qty}")
        
        price_prompt = "[magenta]정정 단가($)[/]" if is_overseas else "[magenta]정정 단가[/] (0: 시장가)"
        price = Prompt.ask(f"{price_prompt} [dim](취소: q)[/dim]", default="0")
        if price.lower() == 'q': return
        context.USER_ACTION_BREADCRUMB.append(f"[단가] {price}")
        if is_overseas and not price: 
            config.console.print("[red]가격 입력 필요[/]"); return
    else: # 취소
        rvse_cncl_dvsn_cd = "02"
        qty = Prompt.ask(f"\n[magenta]취소 수량[/] (최대 {target_rmn}주, 0: 전량) [dim](취소: q)[/dim]", default="0")
        if qty.lower() == 'q': return
        context.USER_ACTION_BREADCRUMB.append(f"[수량] {qty}")
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

    logger.info(f"운영자 실행: {' - '.join(context.USER_ACTION_BREADCRUMB)}")

    # 컨텍스트 적용 (선택된 주문의 계좌 사용)
    with utils.AccountContext(target_cano):
        try:
            res_json = None
            # [수정] 단일 API 호출이므로 status 사용
            with config.console.status(f"[bold green]주문 {action_name} 요청 전송 중...[/]"):
                res_json = api.revise_cancel_order(market, api_action, org_odno, pdno, req_qty, price, rvse_cncl_dvsn_cd, ord_dvsn, exchange_code=target_excd)
            
            if res_json['rt_cd'] == '0':
                odno = res_json.get('output', {}).get('ODNO') or res_json.get('output', {}).get('KRX_FWDG_ORD_ORGNO')
                if not odno and 'output' in res_json and 'ODNO' in res_json['output']:
                    odno = res_json['output']['ODNO']
                
                config.console.print(f"[bold green]접수 완료 (번호: {odno})[/]")
                
                api.send_telegram_message(f"🚀 [수동 주문] {full_action_name} 접수\n종목: {prdt_name} ({pdno})\n수량: {final_qty}주\n단가: {display_price}\n주문번호: {odno}")
                
                db_manager.db.insert_trade(f"{full_action_name}(수동)", pdno, prdt_name, final_qty, price, odno, org_odno=org_odno, reason=f"사용자 {action_name}")
                
                # [수정] 원본 주문 상태 업데이트 제거 (히스토리 보존)
                # show_open_orders에서 canceled_org_odnos 필터링으로 처리됨
                # if config.session.is_simulation:
                #     status_update = "정정" if action == "1" else "취소"
                #     db_manager.db.update_trade(org_odno, order_status=status_update)
                
                auto_trade.ConclusionMonitor().check_now()
                
                config.console.print("\n[dim]변경 사항 확인을 위해 미체결 내역을 조회합니다...[/dim]")
                show_open_orders()
            else:
                msg_cd = res_json.get('msg_cd')
                err_msg = res_json.get('msg1')
                config.console.print(f"[red]실패: {err_msg}[/]")
                
                # [추가] 이미 체결/취소된 주문(40330000)인 경우 DB 상태 업데이트 (유령 주문 정리)
                if msg_cd == '40330000' and config.session.is_simulation:
                    config.console.print("[yellow]안내: 이미 체결되었거나 취소된 주문입니다. 목록에서 제거합니다.[/yellow]")
                    # [수정] 상태 업데이트 대신 정리용 히스토리 추가 (선택 사항이나, 여기서는 상태 업데이트 유지 또는 별도 처리)
                    # 이미 체결/취소된 상태라면 원본을 건드리기보다, 사용자가 인지했으므로 
                    # 해당 주문번호에 대한 '정리' 이력을 남겨 필터링되게 함
                    db_manager.db.insert_trade("기타(정리)", pdno, prdt_name, 0, 0, f"CLEAN_{org_odno}", org_odno=org_odno, reason="이미 체결/취소됨(40330000)")
        except Exception as e:
            config.console.print(f"[red]에러: {e}[/]")

def order_menu():
    """매수/매도 주문 선택 메뉴"""
    config.console.print("\n[bold]주문 유형을 선택하세요:[/bold]")
    grid = Table.grid(padding=(0, 2))
    grid.add_column(justify="left")
    grid.add_column(justify="left", style="dim")
    grid.add_row("[1] 매수 주문", "(Buy Order)")
    grid.add_row("[2] 매도 주문", "(Sell Order)")
    config.console.print(grid)
    config.console.print()
    
    choice = Prompt.ask("선택 [dim](취소: q)[/dim]", choices=["1", "2", "q"], default="1")
    if choice.lower() == 'q': return

    if choice == "1":
        send_order("buy")
    elif choice == "2":
        send_order("sell")

def stock_order_menu():
    """종목 주문 관리 통합 메뉴"""
    config.console.print("\n[bold]종목 주문 관리 (Stock Order Management)[/bold]")
    grid = Table.grid(padding=(0, 2))
    grid.add_column(justify="left")
    grid.add_column(justify="left", style="dim")
    grid.add_row("[1] [red]매수[/red] 주문", "(Buy)")
    grid.add_row("[2] [blue]매도[/blue] 주문", "(Sell)")
    grid.add_row("[3] [magenta]정정/취소[/magenta] 주문", "(Modify/Cancel)")
    config.console.print(grid)
    config.console.print()
    
    choice = Prompt.ask("선택 [dim](취소: q)[/dim]", choices=["1", "2", "3", "q"], default="3")
    
    menu_map = {"1": "매수 주문", "2": "매도 주문", "3": "정정/취소 주문"}
    if choice in menu_map:
        context.USER_ACTION_BREADCRUMB.append(f"[{choice}] {menu_map[choice]}")

    if choice.lower() == 'q': return

    if choice == "1":
        send_order("buy")
    elif choice == "2":
        send_order("sell")
    elif choice == "3":
        modify_order()
