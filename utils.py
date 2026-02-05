# utils.py
from rich.prompt import Prompt
import config
import api
import constants

def get_common_headers(tr_id):
    return {
        "Content-Type": "application/json",
        "authorization": f"Bearer {api.get_current_token()}",
        "appKey": config.APP_KEY,
        "appSecret": config.APP_SECRET,
        "tr_id": tr_id
    }

def get_tr_id(market, category, action):
    """constants.TR_ID_CONFIG에서 환경(실전/모의)에 맞는 TR_ID를 반환하는 헬퍼 함수"""
    env_key = "sim" if config.IS_SIMULATION else "real"
    try:
        return constants.TR_ID_CONFIG[market][category][action][env_key]
    except KeyError:
        return ""

def select_stock_for_chart():
    config.console.print("\n[bold]분석할 종목 그룹을 선택하세요:[/bold]")
    config.console.print("[1] 국내 주식")
    config.console.print("[2] 국내 ETF")
    config.console.print("[3] 미국 주식")
    config.console.print("[4] 미국 ETF")
    config.console.print("[5] 직접 입력 (코드 검색)")
    config.console.print("[6] 시장 지수")  # [복구] 시장 지수 옵션 추가
    
    config.console.print()
    # choices에 "6" 추가
    group_choice = Prompt.ask("선택 [dim](취소: q)[/dim]", choices=["1", "2", "3", "4", "5", "6", "q"], default="5")
    if group_choice.lower() == 'q': return None, None, None
    
    # [복구] 시장 지수 선택 로직 추가
    if group_choice == "6":
        indices_list = [
            ("코스피", "^KS11"), ("코스닥", "^KQ11"),
            ("나스닥", "^IXIC"), ("S&P500", "^GSPC"), ("다우존스", "^DJI"),
            ("금", "GC=F"), ("은", "SI=F"), ("구리", "HG=F"),
            ("WTI 원유", "CL=F"), ("천연가스", "NG=F"), ("밀", "ZW=F"),
            ("달러인덱스", "DX-Y.NYB"), ("달러환율", "KRW=X"),
            ("VIX (변동성)", "^VIX"), ("SOX (반도체)", "^SOX")
        ]

        config.console.print(f"\n[bold]시장 지수 목록:[/bold]")
        for i, (name, code) in enumerate(indices_list):
            config.console.print(f"[{i+1}] {name}")
        
        config.console.print()
        idx_choice = Prompt.ask("번호 선택 [dim](취소: q)[/dim]")
        if idx_choice.lower() == 'q': return None, None, None

        if idx_choice.isdigit() and 1 <= int(idx_choice) <= len(indices_list):
            name, code = indices_list[int(idx_choice)-1]
            return code, name, True # 지수는 해외/기타로 처리
        else:
            return None, None, None

    if group_choice == "5":
        config.console.print()
        raw_input = Prompt.ask("분석할 종목코드(6자리/티커) 또는 '종목명 코드' [dim](취소: q)[/dim]")
        if raw_input.lower() == 'q' or not raw_input.strip(): return None, None, None

        parts = raw_input.split()
        code = parts[-1].upper()
        guessed_name = " ".join(parts[:-1])
        is_overseas = not (code.isdigit() or (len(code) == 6 and code.startswith('0')))
        
        name = guessed_name if guessed_name else api.get_stock_name_by_code(code, is_overseas)
        
        if not name:
            config.console.print(f"\n[bold red]오류: '{code}'에 대한 정보를 찾을 수 없습니다.[/bold red]\n")
            return None, None, None

        with config.console.status("[bold green]종목 유효성 최종 확인 중 (API)...[/]"):
            res = api.get_current_price_data(code, is_overseas)
            
            if not res or res.get('rt_cd') != '0':
                msg = res.get('msg1') or "시세 데이터를 찾을 수 없는 종목입니다."
                config.console.print(f"\n[bold red]오류: 유효하지 않은 종목입니다. ({code})[/bold red]")
                config.console.print(f"[dim]사유: {msg}[/dim]\n")
                return None, None, None

        config.console.print(f"\n[bold green]검색 결과:[/bold green] [bold cyan]{name}[/bold cyan] ({code})")
        config.console.print()
        if Prompt.ask("이 종목으로 차트 분석을 진행하시겠습니까?", choices=["y", "n"], default="y").lower() == "n":
            return None, None, None

        return code, name, is_overseas

    target_info = {"1": ("stocks_kr", "국내 주식"), "2": ("etfs_kr", "국내 ETF"), "3": ("stocks_us", "미국 주식"), "4": ("etfs_us", "미국 ETF")}
    target_key, group_name = target_info[group_choice]
    target_list = config.STOCK_CONFIG_DATA[target_key]
    is_overseas = True if "us" in target_key else False

    config.console.print(f"\n[bold]{group_name} 목록:[/bold]")
    for i, item in enumerate(target_list):
        config.console.print(f"[{i+1}] {item['name']} ({item['code']})")
    
    config.console.print()
    choice_idx = Prompt.ask("번호 선택 [dim](취소: q)[/dim]")
    if choice_idx.lower() == 'q': return None, None, None
    
    if choice_idx.isdigit() and 1 <= int(choice_idx) <= len(target_list):
        item = target_list[int(choice_idx)-1]
        return item['code'], item['name'], is_overseas
    
    return None, None, None

# [수정] 매수/매도 주문 시 주식/ETF 구분 헤더 및 연속 번호 출력
def select_target_stock():
    config.console.print("\n[bold]거래 국가를 선택하세요:[/bold]")
    config.console.print("[1] 국내 (Domestic)")
    config.console.print("[2] 미국 (Overseas/US)")
    config.console.print()
    nation_choice = Prompt.ask("선택 [dim](취소: q)[/dim]", choices=["1", "2", "q"], default="1")
    if nation_choice.lower() == 'q': return None, None, None
    
    is_overseas = (nation_choice == "2")
    all_stocks = []
    idx = 1
    
    config.console.print()
    config.console.print("\n[bold]주문할 종목을 선택하세요:[/bold]")
    
    if is_overseas:
        config.console.print("[yellow]-- 미국 주식 --[/yellow]")
        for item in config.STOCK_CONFIG_DATA["stocks_us"]:
            config.console.print(f"[{idx}] {item['name']} ({item['code']})")
            all_stocks.append((item['name'], item['code']))
            idx += 1
        config.console.print("[yellow]-- 미국 ETF --[/yellow]")
        for item in config.STOCK_CONFIG_DATA["etfs_us"]:
            config.console.print(f"[{idx}] {item['name']} ({item['code']})")
            all_stocks.append((item['name'], item['code']))
            idx += 1
    else:
        config.console.print("[yellow]-- 국내 주식 --[/yellow]")
        for item in config.STOCK_CONFIG_DATA["stocks_kr"]:
            config.console.print(f"[{idx}] {item['name']} ({item['code']})")
            all_stocks.append((item['name'], item['code']))
            idx += 1
        config.console.print("[yellow]-- 국내 ETF --[/yellow]")
        for item in config.STOCK_CONFIG_DATA["etfs_kr"]:
            config.console.print(f"[{idx}] {item['name']} ({item['code']})")
            all_stocks.append((item['name'], item['code']))
            idx += 1

    config.console.print(f"[{idx}] 직접 입력")
    config.console.print()
    choice_idx = Prompt.ask("선택 [dim](취소: q)[/dim]", default=str(idx))
    
    if choice_idx.lower() == 'q': return None, None, None
    
    if choice_idx.isdigit():
        c_idx = int(choice_idx)
        if 1 <= c_idx <= len(all_stocks):
            return all_stocks[c_idx-1][1], all_stocks[c_idx-1][0], is_overseas
        elif c_idx == idx:
            code = Prompt.ask("종목코드(티커) 입력").upper()
            return code, code, is_overseas
    return None, None, None
