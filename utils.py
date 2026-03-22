# utils.py
from rich.prompt import Prompt
import yfinance as yf
import logging
import config
import context # [추가]
import api
import constants
from modules import market # [추가] 통합 지수 리스트 참조용
import math
from rich.table import Table
from rich import box
import os

logger = logging.getLogger(__name__)

def get_common_headers(tr_id):
    # [수정] 컨텍스트에 따라 적절한 앱 키/시크릿 선택
    use_auto = getattr(context.trade_context, 'use_auto_account', False)
    is_sim = config.session.is_simulation
    
    if is_sim:
        app_key = config.session.app_key
        app_secret = config.session.app_secret
    else:
        if use_auto and config.session.auto_app_key:
            app_key = config.session.auto_app_key
            app_secret = config.session.auto_app_secret
        else:
            app_key = config.session.real_app_key
            app_secret = config.session.real_app_secret

    return {
        "Content-Type": "application/json",
        "authorization": f"Bearer {api.get_current_token()}",
        "appKey": app_key,
        "appSecret": app_secret,
        "tr_id": tr_id
    }

def get_tr_id(market, category, action):
    """constants.TR_ID_CONFIG에서 환경(실전/모의)에 맞는 TR_ID를 반환하는 헬퍼 함수"""
    env_key = "sim" if config.session.is_simulation else "real"
    try:
        return constants.TR_ID_CONFIG[market][category][action][env_key]
    except KeyError:
        return ""

def get_exchange_rate():
    """
    실시간 원/달러 환율을 조회합니다. (yfinance: KRW=X)
    실패 시 config.DEFAULT_EXCHANGE_RATE를 반환합니다.
    """
    rate = config.DEFAULT_EXCHANGE_RATE
    try:
        if config.SCREEN_DEBUG_LEVEL == "TRACE":
            config.console.print(f"[dim cyan][TRACE] REQ (yfinance) | Ticker: KRW=X[/dim cyan]")
        elif config.SCREEN_DEBUG_LEVEL == "DEBUG":
            config.console.print(f"[dim cyan][DEBUG] REQ (yfinance) | Ticker: KRW=X | Method: fast_info.last_price[/dim cyan]")

        ticker = yf.Ticker("KRW=X")
        current_rate = ticker.fast_info.last_price
        
        if current_rate and current_rate > 0:
            rate = float(current_rate)
            if config.SCREEN_DEBUG_LEVEL == "TRACE":
                config.console.print(f"[dim magenta][TRACE] RES (yfinance) | Rate: {rate:.2f}[/dim magenta]")
            elif config.SCREEN_DEBUG_LEVEL == "DEBUG":
                config.console.print(f"[dim magenta][DEBUG] RES (yfinance) | Rate: {rate} | Raw: {current_rate}[/dim magenta]")
    except Exception as e:
        if config.SCREEN_DEBUG_LEVEL in ["TRACE", "DEBUG"]:
            config.console.print(f"[dim red][TRACE] RES (yfinance) | Error: {e}[/dim red]")
        pass
    
    return rate

def clear_screen():
    """config 설정에 따라 터미널 화면을 지웁니다."""
    if getattr(config, 'CLEAR_SCREEN_ON_MENU', False):
        os.system('cls' if os.name == 'nt' else 'clear')

def pause():
    """화면 자동 지우기 설정이 켜져 있을 때, 사용자가 결과를 확인할 수 있도록 대기합니다."""
    if getattr(config, 'CLEAR_SCREEN_ON_MENU', False):
        config.console.print()
        config.console.print("[dim]엔터를 누르면 메인 메뉴로 돌아갑니다...[/dim]")
        input()

def print_breadcrumb():
    """현재 메뉴 경로를 출력합니다."""
    if getattr(context, 'USER_ACTION_BREADCRUMB', None):
        config.console.print(f"[dim]경로: {' > '.join(context.USER_ACTION_BREADCRUMB)}[/dim]")
        config.console.print()

def show_menu(title, menu_items, default_choice="1", cancel_choice="q", text_before=None, custom_prompt=None):
    """
    통합 메뉴 출력 및 입력 헬퍼 함수
    menu_items: [("1", "이름", "설명"), ...] 또는 [("1", "이름"), ...]
    """
    clear_screen()
    config.console.print()
    print_breadcrumb()
    
    if text_before:
        config.console.print(text_before)
        config.console.print()
        
    config.console.print(f"[bold]{title}[/bold]")
    grid = Table.grid(padding=(0, 2))
    grid.add_column(justify="left")
    grid.add_column(justify="left", style="dim")
    
    valid_choices = []
    for item in menu_items:
        if len(item) == 3:
            key, name, desc = item
            grid.add_row(f"[{key}] {name}", f"({desc})" if desc else "")
        else:
            key, name = item
            grid.add_row(f"[{key}] {name}", "")
        valid_choices.append(str(key))
    
    if cancel_choice:
        valid_choices.append(str(cancel_choice))
        valid_choices.append(str(cancel_choice).upper())
        
    config.console.print(grid)
    config.console.print()
    
    if custom_prompt:
        choice = Prompt.ask(custom_prompt, default=str(default_choice))
    else:
        prompt_str = f"선택 [dim](이전: {cancel_choice})[/dim]" if cancel_choice else "선택"
        choice = Prompt.ask(prompt_str, choices=valid_choices, default=str(default_choice))
        
    config.console.print()
    return choice

def search_stock_in_list(stock_list, title="종목 선택", display_func=None):
    """리스트에서 종목을 번호, 이름, 코드로 검색하여 선택하는 통합 헬퍼 함수"""
    current_list = stock_list
    while True:
        config.console.print(f"[bold]{title}[/bold]")
        if len(current_list) > 15:
            config.console.print("[dim]목록이 깁니다. 종목명이나 코드로 검색해보세요.[/dim]")
        
        for i, s in enumerate(current_list):
            if display_func:
                config.console.print(display_func(i, s))
            else:
                name = s.get('name', 'Unknown')
                code = s.get('code', 'Unknown')
                config.console.print(f"[{i+1}] {name} ({code})")
            
        config.console.print()
        sel = Prompt.ask("번호, 종목명 또는 코드 검색 [dim](이전: q)[/dim]")
        config.console.print()
        
        if sel.lower() == 'q': return None, None
        
        if sel.isdigit():
            idx = int(sel) - 1
            if 0 <= idx < len(current_list):
                selected_item = current_list[idx]
                try: original_idx = stock_list.index(selected_item)
                except ValueError: original_idx = idx
                return original_idx, selected_item
            else:
                config.console.print("[red]잘못된 번호입니다.[/red]\n")
                continue
        
        # 검색 로직
        filtered = []
        for s in stock_list:
            name = s.get('name', '')
            code = s.get('code', '')
            if sel.lower() in name.lower() or sel.upper() in code.upper():
                filtered.append(s)
                
        if not filtered:
            config.console.print(f"[yellow]'{sel}' 검색 결과가 없습니다.[/yellow]\n")
            current_list = stock_list
            continue
            
        if len(filtered) == 1:
            selected_item = filtered[0]
            try: original_idx = stock_list.index(selected_item)
            except ValueError: original_idx = 0
            name = selected_item.get('name', '')
            code = selected_item.get('code', '')
            config.console.print(f"[green]검색됨: {name} ({code})[/green]\n")
            return original_idx, selected_item
            
        config.console.print(f"[yellow]{len(filtered)}개의 항목이 검색되었습니다. 번호를 선택해주세요.[/yellow]\n")
        current_list = filtered

def validate_and_confirm_stock(code, name, is_overseas, action_text="진행하시겠습니까?"):
    """API를 통해 종목 유효성을 검증하고 사용자에게 진행 여부를 확인합니다."""
    with config.console.status("[cyan]종목 유효성 확인 중 (API)...[/cyan]"):
        res = api.get_current_price_data(code, is_overseas)
        
        is_valid = False
        msg = "시세 데이터를 찾을 수 없는 종목입니다."
        if res and res.get('rt_cd') == '0':
            output = res.get('output', {})
            if is_overseas:
                if float(output.get('last') or 0) > 0: is_valid = True
            else:
                if int(output.get('stck_prpr') or 0) > 0: is_valid = True
        elif res:
            msg = res.get('msg1') or msg
            
        if not is_valid:
            config.console.print(f"[bold red]오류: 유효하지 않은 종목이거나 현재가가 존재하지 않습니다. ({code})[/bold red]")
            config.console.print(f"[dim]사유: {msg}[/dim]\n")
            return False

    config.console.print(f"[bold green]검색 결과:[/bold green] [bold cyan]{name}[/bold cyan] ({code})")
    config.console.print()
    ans = Prompt.ask(action_text, choices=["y", "n"], default="y")
    config.console.print()
    
    return ans.lower() == 'y'

def select_stock_for_chart():
    menu_items = [
        ("1", "국내 주식", "Domestic Stock"), ("2", "국내 ETF", "Domestic ETF"),
        ("3", "미국 주식", "US Stock"), ("4", "미국 ETF", "US ETF"),
        ("5", "직접 입력", "Direct Input"), ("6", "시장 지수", "Market Indices")
    ]
    group_choice = show_menu("분석할 종목 그룹을 선택하세요", menu_items, default_choice="5")
    if group_choice.lower() == 'q': return None, None, None
    
    group_map = {"1": "국내주식", "2": "국내ETF", "3": "미국주식", "4": "미국ETF", "5": "직접입력", "6": "시장지수"}
    context.USER_ACTION_BREADCRUMB.append(f"[{group_choice}] {group_map.get(group_choice, '')}")

    if group_choice == "6":
        indices_list = market.ALL_INDICES
        dict_list = [{'name': n, 'code': c} for n, c in indices_list]
        idx, item = search_stock_in_list(dict_list, title="시장 지수 목록")
        if item:
            context.USER_ACTION_BREADCRUMB.append(f"[지수선택] {item['name']}")
            return item['code'], item['name'], True
        return None, None, None

    if group_choice == "5":
        print_breadcrumb()
        raw_input = Prompt.ask("분석할 종목코드(6자리/티커) 또는 '종목명 코드' [dim](이전: q)[/dim]")
        config.console.print()
        if raw_input.lower() != 'q' and raw_input.strip():
            context.USER_ACTION_BREADCRUMB.append(f"[직접입력] {raw_input}")
        if raw_input.lower() == 'q' or not raw_input.strip(): return None, None, None

        parts = raw_input.split()
        code = parts[-1].upper()
        guessed_name = " ".join(parts[:-1])
        is_overseas = not (code.isdigit() or (len(code) == 6 and code.startswith('0')))
        
        name = guessed_name if guessed_name else api.get_stock_name_by_code(code, is_overseas)
        
        if not name:
            config.console.print(f"\n[bold red]오류: '{code}'에 대한 정보를 찾을 수 없습니다.[/bold red]\n")
            return None, None, None

        if not validate_and_confirm_stock(code, name, is_overseas, "이 종목으로 분석을 진행하시겠습니까?"):
            return None, None, None

        return code, name, is_overseas

    key_map = {"1": "stocks_kr", "2": "etfs_kr", "3": "stocks_us", "4": "etfs_us"}
    target_key = key_map[group_choice]
    target_list = config.session.stock_data.get(target_key, [])
    is_overseas = (group_choice in ["3", "4"])
    
    if not target_list:
        config.console.print(f"[yellow]{group_map[group_choice]} 목록이 비어있습니다.[/yellow]")
        return None, None, None
        
    idx, item = search_stock_in_list(target_list, title=f"{group_map[group_choice]} 목록")
    if item:
        context.USER_ACTION_BREADCRUMB.append(f"[종목선택] {item['name']}")
        return item['code'], item['name'], is_overseas
    
    return None, None, None

def select_target_stock():
    menu_items = [("1", "국내 (Domestic)", ""), ("2", "미국 (Overseas/US)", "")]
    nation_choice = show_menu("거래 국가를 선택하세요", menu_items, default_choice="1")
    if nation_choice.lower() == 'q': return None, None, None
    
    nation_map = {"1": "국내", "2": "미국"}
    context.USER_ACTION_BREADCRUMB.append(f"[{nation_choice}] {nation_map.get(nation_choice, '')}")
    
    is_overseas = (nation_choice == "2")
    all_stocks = []
    
    if is_overseas:
        all_stocks.extend(config.session.stock_data.get("stocks_us", []))
        all_stocks.extend(config.session.stock_data.get("etfs_us", []))
    else:
        all_stocks.extend(config.session.stock_data.get("stocks_kr", []))
        all_stocks.extend(config.session.stock_data.get("etfs_kr", []))

    all_stocks.append({'name': '직접 입력', 'code': 'DIRECT'})
    
    idx, item = search_stock_in_list(all_stocks, title="주문할 종목을 선택하세요")
    if item:
        if item['code'] == 'DIRECT':
            print_breadcrumb()
            code = Prompt.ask("종목코드(티커) 입력 [dim](이전: q)[/dim]").upper()
            config.console.print()
            if code.lower() == 'Q':
                return None, None, None
                
            context.USER_ACTION_BREADCRUMB.append(f"[직접입력] {code}")
            name = api.get_stock_name_by_code(code, is_overseas)
            if not name or name in ["Npay 증권", "네이버 페이 증권", "증권"]: name = code
            
            if not validate_and_confirm_stock(code, name, is_overseas, "이 종목으로 주문을 진행하시겠습니까?"):
                return None, None, None
                
            return code, name, is_overseas
        else:
            context.USER_ACTION_BREADCRUMB.append(f"[종목선택] {item['name']}")
            return item['code'], item['name'], is_overseas
    return None, None, None

class AccountContext:
    """
    특정 계좌(cano)에 맞춰 trade_context.use_auto_account를 설정하고,
    종료 시 원래 상태로 복구하는 Context Manager
    """
    def __init__(self, cano):
        self.cano = cano
        self.original_state = None

    def __enter__(self):
        self.original_state = getattr(context.trade_context, 'use_auto_account', False)
        if not config.session.is_simulation and self.cano:
            if self.cano == config.session.auto_cano:
                context.trade_context.use_auto_account = True
            elif self.cano == config.session.cano:
                context.trade_context.use_auto_account = False
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        context.trade_context.use_auto_account = self.original_state

def get_tick_size(price, is_overseas=False):
    """호가 단위(Tick Size) 반환"""
    if not isinstance(price, (int, float)):
        return 0  # 숫자가 아니면 0을 반환

    if is_overseas:
        return 0.01
    
    if price < 2000: return 1
    if price < 5000: return 5
    if price < 20000: return 10
    if price < 50000: return 50
    if price < 200000: return 100
    if price < 500000: return 500
    return 1000

def adjust_to_tick(price, is_overseas=False):
    """가격을 호가 단위에 맞춰 반올림 보정"""
    tick = get_tick_size(price, is_overseas)
    if not isinstance(price, (int, float)):
        return 0  # 숫자가 아니면 0을 반환

    return round(price / tick) * tick
