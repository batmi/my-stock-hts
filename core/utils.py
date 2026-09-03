# utils.py
from rich.prompt import Prompt
import yfinance as yf
import logging
import config
config.silence_yfinance_numpy_warning()  # yfinance import 뒤에 걸어야 억제 유효(아래 함수 주석 참조)
from core import context # [추가]
from core import constants
import math
import functools
from contextlib import closing
from rich.table import Table
from rich import box
from rich.progress import Progress, SpinnerColumn, BarColumn, TextColumn
import os
import sqlite3
from datetime import datetime, timedelta, timezone

logger = logging.getLogger(__name__)


def is_overseas_code(code):
    """종목코드 형태로 해외 여부를 판단한다 (국내는 6자리 숫자).

    [SSOT] 같은 한 줄이 여러 모듈에 흩어져 있었다. 판정이 갈리면 해외 종목의 가격 표기
    (달러/원)와 거래일 귀속이 모듈마다 달라진다 — 값이 그럴듯해서 눈에 잘 안 띈다.
    """
    code = (code or '').strip()
    return not (len(code) == 6 and code.isdigit())


# 6자리 숫자가 아니면서도 **원화로 값이 매겨지는** 코드들. is_overseas_code 는 '시세 소스와
#  거래일' 축이라 이들을 해외로 본다(맞다 — KIS 국내 주식 API 로는 뽑을 수 없다). 하지만
#  표기 통화는 다른 축이다: KRX 금현물은 원/g, 국내 지수는 포인트다. 두 축을 한 함수로
#  답하게 두면 원화 값에 $ 가 붙는다(2026-09-03 텔레그램 청산선에서 실제로 그랬다).
KRW_QUOTED_CODES = {
    "^KRXGOLD",                                  # KRX 금현물(원/g)
    "^KS11", "^KS200", "^KQ11", "^KQ150",        # 국내 지수
    "^K200FUT", "^VKOSPI",                       # 코스피200 선물·변동성
}


def is_usd_quoted(code):
    """가격을 달러로 적어야 하는 코드인가. (통화 축 — 거래일 축인 is_overseas_code 와 다르다)"""
    return is_overseas_code(code) and (code or '').strip().upper() not in KRW_QUOTED_CODES


def market_today(is_overseas=False):
    """실시간 현재가 반영 시 '당일' 판정에 쓰는 시장 기준일(YYYYMMDD 문자열).

    api.market_today로 위임한다: 국내는 KST, 해외(미국)는 ET 기준이며 주말·공휴일(휴장일)이면
    직전 거래일을 반환한다. indicators.apply_realtime_price()에 넘겨, 당일 일봉이 아직 없을 때만
    당일 봉을 새로 추가하고 비거래일엔 마지막 거래일 봉을 덮어쓰게 한다(가짜 봉 방지).
    """
    import api      # 지연 임포트 — core 는 상위 계층을 import 시점에 끌어오지 않는다
    return api.market_today(is_overseas)

def get_common_headers(tr_id):
    # [수정] 컨텍스트에 따라 적절한 앱 키/시크릿 선택
    use_auto = getattr(context.trade_context, 'use_auto_account', False)
    is_sim = False
    
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

    import api      # 지연 임포트 — core 는 상위 계층을 import 시점에 끌어오지 않는다
    return {
        "Content-Type": "application/json",
        "authorization": f"Bearer {api.get_current_token()}",
        "appKey": app_key,
        "appSecret": app_secret,
        "tr_id": tr_id
    }

def get_tr_id(market, category, action):
    """constants.TR_ID_CONFIG에서 TR_ID를 반환하는 헬퍼 함수"""
    try:
        return constants.TR_ID_CONFIG[market][category][action]
    except KeyError:
        return ""

def get_exchange_rate():
    """
    실시간 원/달러 환율을 조회합니다. (yfinance: KRW=X)
    실패 시 config.DEFAULT_EXCHANGE_RATE를 반환합니다.
    """
    rate = config.DEFAULT_EXCHANGE_RATE
    try:
        # 1. TradingView 기반 환율 조회 (가장 빠르고 Lock 없음)
        from tradingview_screener import get_all_indicators
        tv_data = get_all_indicators("FX_IDC:USDKRW")
        if tv_data and 'close' in tv_data:
            rate = float(tv_data['close'])
            if config.SCREEN_DEBUG_LEVEL in ["TRACE", "DEBUG"]:
                config.console.print(f"[dim magenta][TRACE] RES (TradingView) | Rate: {rate:.2f}[/dim magenta]")
            return rate
    except Exception as e:
        pass
        
    try:
        # 2. yfinance Fallback
        ticker = yf.Ticker("KRW=X")
        if getattr(ticker.fast_info, 'last_price', None):
            rate = float(ticker.fast_info.last_price)
            if config.SCREEN_DEBUG_LEVEL == "TRACE":
                config.console.print(f"[dim magenta][TRACE] RES (yfinance) | Rate: {rate:.2f}[/dim magenta]")
            elif config.SCREEN_DEBUG_LEVEL == "DEBUG":
                config.console.print(f"[dim magenta][DEBUG] RES (yfinance) | Rate: {rate} | Raw: {ticker.fast_info.last_price}[/dim magenta]")
    except Exception as e:
        if config.SCREEN_DEBUG_LEVEL in ["TRACE", "DEBUG"]:
            config.console.print(f"[dim red][TRACE] RES (yfinance) | Error: {e}[/dim red]")
        pass
    
    return rate

# ==========================================================
# [추가] 종목 메모 DB 관리 기능
# ==========================================================
def init_memo_db():
    try:
        with closing(sqlite3.connect(config.DB_FILE_PATH)) as conn, conn:
            cursor = conn.cursor()
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS stock_memo_entries (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    code TEXT,
                    name TEXT,
                    memo TEXT,
                    updated_at TEXT
                )
            """)
            
            # 기존 stock_memos 테이블이 있다면 데이터 마이그레이션 후 삭제
            cursor.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='stock_memos'")
            if cursor.fetchone():
                cursor.execute("SELECT code, name, memo, updated_at FROM stock_memos")
                old_memos = cursor.fetchall()
                if old_memos:
                    for old_m in old_memos:
                        cursor.execute("INSERT INTO stock_memo_entries (code, name, memo, updated_at) VALUES (?, ?, ?, ?)", old_m)
                cursor.execute("DROP TABLE stock_memos")
                
            conn.commit()
    except Exception as e:
        logger.error(f"메모 DB 초기화 오류: {e}")

def get_stock_memos(code):
    try:
        init_memo_db()
        with closing(sqlite3.connect(config.DB_FILE_PATH)) as conn, conn:
            conn.row_factory = sqlite3.Row
            cursor = conn.cursor()
            cursor.execute("SELECT id, name, memo, updated_at FROM stock_memo_entries WHERE code = ? ORDER BY updated_at DESC", (code,))
            rows = cursor.fetchall()
            return [dict(row) for row in rows]
    except Exception as e:
        logger.error(f"메모 DB 로드 오류: {e}")
    return []

def get_all_stock_memos():
    try:
        init_memo_db()
        with closing(sqlite3.connect(config.DB_FILE_PATH)) as conn, conn:
            conn.row_factory = sqlite3.Row
            cursor = conn.cursor()
            cursor.execute("SELECT id, code, name, memo, updated_at FROM stock_memo_entries ORDER BY updated_at DESC")
            rows = cursor.fetchall()
            return [dict(row) for row in rows]
    except Exception as e:
        logger.error(f"메모 DB 전체 로드 오류: {e}")
    return []

def add_stock_memo(code, name, memo_text):
    try:
        init_memo_db()
        with closing(sqlite3.connect(config.DB_FILE_PATH)) as conn, conn:
            cursor = conn.cursor()
            now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            cursor.execute("INSERT INTO stock_memo_entries (code, name, memo, updated_at) VALUES (?, ?, ?, ?)", (code, name, memo_text, now_str))
            conn.commit()
            return True
    except Exception as e:
        logger.error(f"메모 DB 추가 오류: {e}")
        return False

def update_stock_memo(memo_id, memo_text):
    try:
        init_memo_db()
        with closing(sqlite3.connect(config.DB_FILE_PATH)) as conn, conn:
            cursor = conn.cursor()
            now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            cursor.execute("UPDATE stock_memo_entries SET memo = ?, updated_at = ? WHERE id = ?", (memo_text, now_str, memo_id))
            conn.commit()
            return True
    except Exception as e:
        logger.error(f"메모 DB 수정 오류: {e}")
        return False

def delete_stock_memo_by_id(memo_id):
    try:
        init_memo_db()
        with closing(sqlite3.connect(config.DB_FILE_PATH)) as conn, conn:
            cursor = conn.cursor()
            cursor.execute("DELETE FROM stock_memo_entries WHERE id = ?", (memo_id,))
            conn.commit()
            return True
    except Exception as e:
        logger.error(f"메모 DB 삭제 오류: {e}")
    return False

def delete_all_stock_memos(code):
    try:
        init_memo_db()
        with closing(sqlite3.connect(config.DB_FILE_PATH)) as conn, conn:
            cursor = conn.cursor()
            cursor.execute("DELETE FROM stock_memo_entries WHERE code = ?", (code,))
            conn.commit()
            return True
    except Exception as e:
        logger.error(f"메모 DB 전체 삭제 오류: {e}")
    return False

def get_memo_codes():
    """메모가 존재하는 종목 코드 목록 반환 (마킹용)"""
    try:
        init_memo_db()
        with closing(sqlite3.connect(config.DB_FILE_PATH)) as conn, conn:
            cursor = conn.cursor()
            cursor.execute("SELECT DISTINCT code FROM stock_memo_entries")
            return set(row[0] for row in cursor.fetchall())
    except Exception as e:
        logger.error(f"메모 DB 목록 조회 오류: {e}")
    return set()

def clear_screen():
    """config 설정에 따라 터미널 화면을 지웁니다."""
    if getattr(config, 'CLEAR_SCREEN_ON_MENU', False):
        # 1-depth 메뉴까지만 화면을 지우고, 그 이하 단계(세부 작업)에서는 이전 출력 내용을 유지합니다.
        breadcrumb = getattr(context, 'USER_ACTION_BREADCRUMB', [])
        if len(breadcrumb) <= 1:
            os.system('cls' if os.name == 'nt' else 'clear')

def pause():
    """화면 자동 지우기 설정이 켜져 있을 때, 사용자가 결과를 확인할 수 있도록 대기합니다."""
    if getattr(config, 'CLEAR_SCREEN_ON_MENU', False):
        config.console.print()
        config.console.print("[dim]엔터를 누르면 메뉴로 돌아갑니다...[/dim]")
        input()

def print_breadcrumb():
    """현재 메뉴 경로를 출력합니다."""
    if getattr(context, 'USER_ACTION_BREADCRUMB', None) and context.USER_ACTION_BREADCRUMB:
        path_str = " > ".join(context.USER_ACTION_BREADCRUMB)
        
        # 1-Depth 메뉴일 때만 박스형 헤더 적용, 그 이상은 심플하게 표시
        if len(context.USER_ACTION_BREADCRUMB) == 1:
            now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            if getattr(config.session, 'is_paper', False):
                env_str = "[가상투자]"; env_color = "cyan"
            elif config.session.is_toss:
                env_str = "[토스증권]"; env_color = "magenta"
            env_str = "[한투증권]"; env_color = "bold red"
            
            print("\n" + "─"*50)
            config.console.print(f" [cyan]시스템 시간: {now_str}[/cyan] | [{env_color}]{env_str}[/]")
            print("─"*50)
            config.console.print(f"경로: {path_str}")
            print("─"*50)
        else:
            config.console.print(f"\n[dim]경로: {path_str}[/dim]")
            config.console.print()

def show_menu(title, menu_items, default_choice="1", cancel_choice="b", text_before=None,
              custom_prompt=None, disabled=None):
    """
    통합 메뉴 출력 및 입력 헬퍼 함수
    menu_items: [("1", "이름", "설명"), ...] 또는 [("1", "이름"), ...]
    disabled: 회색으로만 보여주고 선택은 막을 키의 집합 {"4", ...}

    [왜 감추지 않고 회색인가] 상황에 따라 못 쓰는 항목을 목록에서 빼면 번호가 비거나
    화면마다 같은 번호가 다른 뜻이 된다. 자리는 그대로 두고 고를 수만 없게 하면
    번호는 어디서나 같은 조건을 가리키면서 막다른 길도 사라진다.
    """
    disabled = {str(k) for k in (disabled or ())}
    clear_screen()
    print_breadcrumb()
    
    if text_before:
        config.console.print(text_before)
        
    # [수정] 메인/서브메뉴 타이틀은 경로(Breadcrumb)와 의미가 중복되므로 생략하여 깔끔하게 구성
    # 단, "(English)" 형태가 없는 "주문 유형을 선택하세요" 등 질문형 타이틀은 출력 유지
    is_depth_1 = getattr(context, 'USER_ACTION_BREADCRUMB', None) and len(context.USER_ACTION_BREADCRUMB) == 1
    
    if title and (not is_depth_1 or not ("(" in title and ")" in title)):
        config.console.print(f"[bold]{title}[/bold]")

    grid = Table.grid(padding=(0, 2))
    grid.add_column(justify="left", style="menu")
    grid.add_column(justify="left", style="dim")
    
    valid_choices = []
    for item in menu_items:
        if len(item) == 3:
            key, name, desc = item
        else:
            key, name = item
            desc = ""
        key = str(key)
        if key in disabled:
            grid.add_row(f"[dim][{key}] {name}[/dim]", f"[dim]({desc})[/dim]" if desc else "")
            continue
        grid.add_row(f"[{key}] {name}", f"({desc})" if desc else "")
        valid_choices.append(key)
    
    if cancel_choice:
        valid_choices.append(str(cancel_choice))
        valid_choices.append(str(cancel_choice).upper())
        
    config.console.print(grid)
    
    # [추가] 1-Depth 메뉴일 경우 하단에 Q/H 안내 및 선택지 추가
    if is_depth_1:
        config.console.print(f"[dim][{cancel_choice.upper()}] 이전 (Back)  |  [H] 도움말 (Help)[/dim]")
        valid_choices.extend(['h', 'H'])
        
    # [추가] 메인 메뉴 점프(q) 처리를 위해 모든 show_menu의 유효 선택지에 q 포함
    if 'q' not in valid_choices:
        valid_choices.extend(['q', 'Q'])

    # [추가] 1-Depth 메뉴 항목 출력 후에만 하단 실선 배치
    if is_depth_1:
        config.console.print("[dim]" + "─"*50 + "[/dim]")
    config.console.print()
    
    if custom_prompt:
        choice = Prompt.ask(custom_prompt, default=str(default_choice))
    else:
        prompt_str = f"선택 [dim](이전: {cancel_choice}, 메인: q)[/dim]" if cancel_choice and not is_depth_1 else "선택"
        choice = Prompt.ask(prompt_str, choices=valid_choices, default=str(default_choice))
        
    config.console.print()
    return choice

def format_order_no(odno):
    """주문번호 표시 정규화. 토스 주문번호는 매우 길어 뒤 10자리만, KIS는 그대로 표시한다."""
    s = str(odno or "")
    if config.session.is_toss:
        return s[-10:]
    return s

def search_stock_in_list(stock_list, title="종목 선택", display_func=None, hide_list=False, number_only=False):
    """리스트에서 종목을 번호, 이름, 코드로 검색하여 선택하는 통합 헬퍼 함수.
    number_only=True 이면 검색 없이 번호 선택만 허용한다(정정/취소 주문 선택 등)."""
    current_list = stock_list
    while True:
        if title and not hide_list:
            config.console.print(f"[bold]{title}[/bold]")

        if not hide_list:
            for i, s in enumerate(current_list):
                if display_func:
                    config.console.print(display_func(i, s))
                else:
                    name = s.get('name', 'Unknown')
                    code = s.get('code', 'Unknown')
                    config.console.print(f"[{i+1}] {name} ({code})")
            config.console.print()

        prompt_msg = ("번호 선택 [dim](이전: b, 메인: q)[/dim]" if number_only
                      else "번호 선택 (또는 종목명·코드 검색) [dim](이전: b, 메인: q)[/dim]")
        sel = Prompt.ask(prompt_msg)
        config.console.print()

        if sel.lower() in ['b', 'q']: return None, None

        # 목록 범위 안의 숫자만 '번호 선택'으로 처리한다.
        # (042660 등 6자리 종목코드가 행 번호로 오인되지 않도록 → 범위 밖이면 검색으로 넘어감)
        if sel.isdigit() and 1 <= int(sel) <= len(current_list):
            idx = int(sel) - 1
            selected_item = current_list[idx]
            try: original_idx = stock_list.index(selected_item)
            except ValueError: original_idx = idx
            return original_idx, selected_item

        # 번호 전용 모드: 검색하지 않고 재입력 요구
        if number_only:
            config.console.print("[red]올바른 번호를 입력해주세요.[/red]\n")
            continue

        # 검색 로직
        filtered = []
        for s in stock_list:
            name = s.get('name', '')
            code = s.get('code', '')
            alt_name = s.get('prdt_name', '')
            alt_code = s.get('pdno', '')

            if (sel.lower() in name.lower() or sel.upper() in code.upper() or
                sel.lower() in alt_name.lower() or sel.upper() in alt_code.upper()):
                filtered.append(s)
                
        if not filtered:
            config.console.print(f"[yellow]'{sel}' 검색 결과가 없습니다.[/yellow]\n")
            current_list = stock_list
            hide_list = False # 검색결과가 없으면 전체리스트를 다시 보여줌
            continue
            
        if len(filtered) == 1:
            selected_item = filtered[0]
            try: original_idx = stock_list.index(selected_item)
            except ValueError: original_idx = 0
            name = selected_item.get('name', selected_item.get('prdt_name', ''))
            code = selected_item.get('code', selected_item.get('pdno', ''))
            config.console.print(f"[green]검색됨: {name} ({code})[/green]\n")
            return original_idx, selected_item
            
        config.console.print(f"[yellow]{len(filtered)}개의 항목이 검색되었습니다. 번호를 선택해주세요.[/yellow]\n")
        current_list = filtered
        hide_list = False # 여러개가 검색되면 목록을 보여줌

def validate_and_confirm_stock(code, name, is_overseas, action_text="진행하시겠습니까?"):
    """API를 통해 종목 유효성을 검증하고 사용자에게 진행 여부를 확인합니다."""
    with Progress(
        SpinnerColumn(),
        TextColumn("[progress.description]{task.description}"),
        BarColumn(),
        console=config.console,
        transient=True
    ) as progress:
        progress.add_task("[cyan]종목 유효성 확인 중 (API)...[/cyan]", total=None)
        import api      # 지연 임포트 — core 는 상위 계층을 import 시점에 끌어오지 않는다
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
            pause()
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
    if group_choice.lower() in ['b', 'q']: return None, None, None
    
    group_map = {"1": "국내주식", "2": "국내ETF", "3": "미국주식", "4": "미국ETF", "5": "직접입력", "6": "시장지수"}
    context.USER_ACTION_BREADCRUMB.append(f"[{group_choice}] {group_map.get(group_choice, '')}")

    if group_choice == "6":
        from modules import market   # 지연 임포트 — 통합 지수 목록은 도메인 계층 소유다
        indices_list = market.ALL_INDICES
        dict_list = [{'name': n, 'code': c} for n, c in indices_list]
        idx, item = search_stock_in_list(dict_list, title="시장 지수 목록", display_func=lambda i, s: f"[{i+1}] {s.get('name', 'Unknown')}")
        if item:
            context.USER_ACTION_BREADCRUMB.append(f"[지수선택] {item['name']}")
            return item['code'], item['name'], True
        return None, None, None

    if group_choice == "5":
        print_breadcrumb()
        raw_input = Prompt.ask("분석할 종목코드(6자리/티커) 또는 '종목명 코드' [dim](이전: b, 메인: q)[/dim]")
        config.console.print()
        if raw_input.lower() not in ['b', 'q'] and raw_input.strip():
            context.USER_ACTION_BREADCRUMB.append(f"[직접입력] {raw_input}")
        if raw_input.lower() in ['b', 'q'] or not raw_input.strip(): return None, None, None

        parts = raw_input.split()
        code = parts[-1].upper()
        guessed_name = " ".join(parts[:-1])
        is_overseas = not (code.isdigit() or (len(code) == 6 and code.startswith('0')))
        
        import api      # 지연 임포트 — core 는 상위 계층을 import 시점에 끌어오지 않는다
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
        pause()
        return None, None, None
        
    idx, item = search_stock_in_list(target_list, title=f"{group_map[group_choice]} 목록")
    if item:
        context.USER_ACTION_BREADCRUMB.append(f"[종목선택] {item['name']}")
        return item['code'], item['name'], is_overseas
    
    return None, None, None

def select_target_stock():
    menu_items = [("1", "국내 (Domestic)", ""), ("2", "미국 (Overseas/US)", "")]
    nation_choice = show_menu("거래 국가를 선택하세요", menu_items, default_choice="1")
    if nation_choice.lower() in ['b', 'q']: return None, None, None
    
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
            code = Prompt.ask("종목코드(티커) 입력 [dim](이전: b, 메인: q)[/dim]").upper()
            config.console.print()
            if code.lower() in ['b', 'q']:
                return None, None, None
                
            context.USER_ACTION_BREADCRUMB.append(f"[직접입력] {code}")
            import api      # 지연 임포트 — core 는 상위 계층을 import 시점에 끌어오지 않는다
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
        if self.cano:
            if self.cano == config.session.auto_cano:
                context.trade_context.use_auto_account = True
            elif self.cano == config.session.cano:
                context.trade_context.use_auto_account = False
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        context.trade_context.use_auto_account = self.original_state


def system_trading_account():
    """시스템 트레이딩이 쓰는 계좌 (cano, acnt_prdt_cd).

    실전(mode 2)에서만 수동 계좌와 갈라지고, 모의·토스·가상투자는 세션 로드 시
    auto_cano = cano로 동기화되므로 어느 모드에서든 이 함수 하나로 답이 나온다.
    종전에는 이 삼항식이 trader.py에만 19곳 흩어져 있어, 한 곳이라도 빠뜨리면
    주문이 조용히 수동 계좌로 새는 구조였다.
    """
    s = config.session
    return (s.auto_cano or s.cano), (s.auto_acnt_prdt_cd if s.auto_cano else s.acnt_prdt_cd)


def inherit_account_context(fn):
    """제출 스레드의 계좌 컨텍스트를 워커 스레드로 전파하는 래퍼.

    [왜 필요한가] context.trade_context는 threading.local()이라 **스레드 간 상속되지
    않는다**. 부모가 AccountContext(자동계좌) 안에서 ThreadPoolExecutor에 작업을
    제출해도, 워커 스레드에서는 use_auto_account가 아예 미설정 상태이고 읽는 쪽이
    모두 getattr(..., False) 폴백이라 **수동 계좌로 판정된다**.

    그 결과 매도 워커(at_sell)에서 나가는 손절·트레일링 매도 주문과 매도가능수량
    조회가 자동 계좌가 아니라 수동 계좌를 향했다. 자동 계좌로 사고 수동 계좌에서
    파는 꼴이라, 손절이 '수량 0'으로 조용히 취소되거나 같은 종목의 수동 보유분이
    대신 팔린다.

    반드시 **제출 스레드에서** 호출해야 한다(그 시점 값을 캡처한다).
        executor.submit(utils.inherit_account_context(worker), item)
    """
    captured = getattr(context.trade_context, 'use_auto_account', False)

    @functools.wraps(fn)
    def _wrapped(*args, **kwargs):
        prev = getattr(context.trade_context, 'use_auto_account', False)
        context.trade_context.use_auto_account = captured
        try:
            return fn(*args, **kwargs)
        finally:
            context.trade_context.use_auto_account = prev

    return _wrapped


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


def clamp_to_price_limit(price, upper=0, lower=0):
    """지정가를 가격제한폭(상한가~하한가) 안으로 되돌린다.

    [왜 필요한가] 주문가는 현재가 ± 슬리피지로 만든다. 평소엔 문제없지만 종목이
    상·하한가에 락되면 그 값이 제한폭 **밖**으로 나가 주문이 통째로 거부된다.
      · 하한가 70,000원에 락 → 매도 지정가 69,900원 → 거부
      · 상한가 130,000원에 락 → 매수 지정가 130,300원 → 거부
    하필 손절이 가장 필요한 -30% 폭락일에 매도 주문이 접수조차 되지 않는다. 게다가
    실패해도 상태가 정리되어 다음 주기에 같은 가격으로 재시도하므로, 하루 종일
    거부만 반복하며 포지션이 방치된다.

    체결을 보장하지는 않는다(하한가엔 매도 잔량이 쌓여 있다). 다만 **대기열에 들어가는
    것과 접수조차 안 되는 것은 다르다** — 락이 풀리는 순간을 잡으려면 주문이 걸려 있어야 한다.

    상·하한가를 못 구했으면(0) 클램프하지 않는다 — 잘못된 한도로 주문가를 흔드는 것이
    제한폭 밖 주문보다 위험하다(fail-open).
    """
    if not isinstance(price, (int, float)) or price <= 0:
        return price
    if upper and upper > 0:
        price = min(price, upper)
    if lower and lower > 0:
        price = max(price, lower)
    return price


def print_krx_fallback_warning(name_map=None):
    """KRX 공식 일봉 확보에 실패해 토스 캔들(NXT 포함)로 계산된 종목이 있으면 노란 경고를 띄운다.

    폴백하면 일봉 OHLC에 NXT 장전(08:00~09:00)·장후(15:30~20:00) 체결이 섞인다. 실측상
    ATR이 6~15% 부풀고 ADX가 최대 9.45 어긋나 손절폭·포지션 크기까지 영향을 받으므로,
    결과를 그대로 신뢰하지 않도록 출력 직전에 알린다. 폴백이 없으면 아무것도 출력하지 않는다.

    name_map: {종목코드: 종목명} — 있으면 이름을 함께 보여준다(없으면 코드만).
    """
    try:
        import api  # 지연 임포트 — core 는 상위 계층을 import 시점에 끌어오지 않는다
        fallback = api.get_krx_fallback()
    except Exception:
        return
    if not fallback:
        return

    name_map = name_map or {}
    items = []
    for code in sorted(fallback):
        nm = name_map.get(code)
        items.append(f"{nm}({code})" if nm else str(code))

    MAX_SHOW = 8
    shown = ", ".join(items[:MAX_SHOW])
    if len(items) > MAX_SHOW:
        shown += f" 외 {len(items) - MAX_SHOW}종목"

    reasons = sorted(set(fallback.values()))
    config.console.print(
        f"[bold yellow]⚠️  KRX 공식 일봉(pykrx/FDR) 조회 실패 — 아래 {len(items)}종목은 "
        f"토스 캔들(NXT 장전·장후 포함)로 계산했습니다.[/bold yellow]")
    config.console.print(f"[yellow]   대상: {shown}[/yellow]")
    config.console.print(f"[yellow]   사유: {', '.join(reasons)}[/yellow]")
    config.console.print(
        "[yellow]   → ATR이 6~15% 부풀고 ADX가 최대 9.45 어긋날 수 있습니다"
        " (손절폭·포지션 크기에 영향). 지표를 그대로 신뢰하지 마세요.[/yellow]")
    config.console.print()
