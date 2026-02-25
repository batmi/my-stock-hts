import json
import os
from rich.prompt import Prompt
from rich.console import Console
from rich.table import Table
from rich import box
import config

console = config.console

def _save_dynamic_config():
    """현재 메모리 상의 설정을 파일로 저장 (영구 반영)"""
    data = {
        "ANALYSIS_THRESHOLDS": config.ANALYSIS_THRESHOLDS,
        "SELL_STRATEGY": config.SELL_STRATEGY,
        "INDICATOR_PARAMS": config.INDICATOR_PARAMS,
        "SYSTEM_INVEST_PER_STOCK": getattr(config, 'SYSTEM_INVEST_PER_STOCK', 0.5),
        "SYSTEM_MAX_HOLDINGS": getattr(config, 'SYSTEM_MAX_HOLDINGS', 5),
        "SYSTEM_TRADING_INTERVAL": getattr(config, 'SYSTEM_TRADING_INTERVAL', 180),
        "SYSTEM_DAILY_LOSS_LIMIT": getattr(config, 'SYSTEM_DAILY_LOSS_LIMIT', 10.0),
        "USE_MARKET_FILTER": getattr(config, 'USE_MARKET_FILTER', True),
        "MARKET_FILTER_MA": getattr(config, 'MARKET_FILTER_MA', 20),
        "CONCLUSION_CHECK_INTERVAL": getattr(config, 'CONCLUSION_CHECK_INTERVAL', 5),
        "CONCLUSION_CHECK_IDLE_INTERVAL": getattr(config, 'CONCLUSION_CHECK_IDLE_INTERVAL', 300),
        "CONCLUSION_CHECK_ACTIVE_DURATION": getattr(config, 'CONCLUSION_CHECK_ACTIVE_DURATION', 100),
        "UNFILLED_ORDER_CANCEL_SECONDS": getattr(config, 'UNFILLED_ORDER_CANCEL_SECONDS', 600),
        "ENABLE_TELEGRAM": getattr(config, 'ENABLE_TELEGRAM', True),
        "TELEGRAM_INSTANCE_NAME": getattr(config, 'TELEGRAM_INSTANCE_NAME', "HTS"),
        "TELEGRAM_POLLING_TIMEOUT": getattr(config, 'TELEGRAM_POLLING_TIMEOUT', 10),
        "SCREEN_DEBUG_LEVEL": getattr(config, 'SCREEN_DEBUG_LEVEL', "OFF"),
        "FILE_DEBUG_LEVEL": getattr(config, 'FILE_DEBUG_LEVEL', "WARNING"),
        "SYSTEM_MAX_CONSECUTIVE_ERRORS": getattr(config, 'SYSTEM_MAX_CONSECUTIVE_ERRORS', 5),
        "SYSTEM_TRADING_START_TIME": getattr(config, 'SYSTEM_TRADING_START_TIME', "0915"),
        "SYSTEM_TRADING_END_TIME": getattr(config, 'SYSTEM_TRADING_END_TIME', "1515"),
        "SYSTEM_RISK_PER_TRADE": getattr(config, 'SYSTEM_RISK_PER_TRADE', 5.0),
        "USE_VOLATILITY_TARGETING": getattr(config, 'USE_VOLATILITY_TARGETING', True),
        "TARGET_VOLATILITY": getattr(config, 'TARGET_VOLATILITY', 0.20),
        "VOLATILITY_SCALING_MAX": getattr(config, 'VOLATILITY_SCALING_MAX', 2.0),
        "VOLATILITY_SCALING_MIN": getattr(config, 'VOLATILITY_SCALING_MIN', 0.3)
    }
    
    try:
        path = os.path.join(config.JSON_DIR, "dynamic_config.json")
        with open(path, 'w', encoding='utf-8') as f:
            json.dump(data, f, indent=4, ensure_ascii=False)
        console.print(f"\n[bold green]설정이 저장되었습니다. (재시작 시에도 유지됨)[/bold green]")
        console.print(f"[dim]저장 경로: {path}[/dim]")
    except Exception as e:
        console.print(f"\n[bold red]설정 저장 실패: {e}[/bold red]")

def view_system_config():
    """현재 시스템 설정 조회"""
    from rich.table import Table
    from rich import box

    console.print()
    # 테이블 생성 (일반적인 테이블 스타일)
    table = Table(
        title="현재 시스템 설정 (System Configuration)",
        box=box.HORIZONTALS,
        show_header=True,
        header_style="dim",
        border_style="dim",
        expand=False,
        padding=(0, 1)
    )
    
    table.add_column("설정 항목 (Description)", justify="left", style="white")
    table.add_column("변수명 (Config Name)", justify="left", style="dim")
    table.add_column("설정값 (Value)", justify="right", style="cyan")

    # 1. 시스템 트레이딩 일반
    table.add_row("[bold]1. 트레이딩 일반[/]", "", "")
    table.add_row("종목당 투자 비중\n[dim]전체 자산 대비 한 종목 투자 비율[/dim]", "SYSTEM_INVEST_PER_STOCK", f"{getattr(config, 'SYSTEM_INVEST_PER_STOCK', 0.5)}")
    table.add_row("최대 보유 종목 수\n[dim]포트폴리오 최대 종목 개수[/dim]", "SYSTEM_MAX_HOLDINGS", f"{getattr(config, 'SYSTEM_MAX_HOLDINGS', 5)}")
    table.add_row("모니터링 주기 (초)\n[dim]자동매매 루프 실행 간격[/dim]", "SYSTEM_TRADING_INTERVAL", f"{getattr(config, 'SYSTEM_TRADING_INTERVAL', 180)}")
    table.add_row("일일 손실 제한 (%)\n[dim]자산 보호를 위한 비상 정지 기준[/dim]", "SYSTEM_DAILY_LOSS_LIMIT", f"{getattr(config, 'SYSTEM_DAILY_LOSS_LIMIT', 10.0)}")
    table.add_row("시장 필터링 사용\n[dim]지수 하락 시 신규 매수 보류[/dim]", "USE_MARKET_FILTER", f"{getattr(config, 'USE_MARKET_FILTER', True)}")
    table.add_row("시장 필터링 MA (일)\n[dim]지수 추세 판단용 이동평균선[/dim]", "MARKET_FILTER_MA", f"{getattr(config, 'MARKET_FILTER_MA', 20)}")
    table.add_row("연속 에러 허용\n[dim]시스템 중단 임계값[/dim]", "SYSTEM_MAX_CONSECUTIVE_ERRORS", f"{getattr(config, 'SYSTEM_MAX_CONSECUTIVE_ERRORS', 5)}")
    table.add_row("거래 시작 시간\n[dim]매매 허용 시작 시각 (HHMM)[/dim]", "SYSTEM_TRADING_START_TIME", f"{getattr(config, 'SYSTEM_TRADING_START_TIME', '0915')}")
    table.add_row("거래 종료 시간\n[dim]매매 허용 종료 시각 (HHMM)[/dim]", "SYSTEM_TRADING_END_TIME", f"{getattr(config, 'SYSTEM_TRADING_END_TIME', '1515')}")
    table.add_row("1회 최대 리스크 (%)\n[dim]계좌 대비 1회 매매 최대 손실폭[/dim]", "SYSTEM_RISK_PER_TRADE", f"{getattr(config, 'SYSTEM_RISK_PER_TRADE', 5.0)}")
    
    table.add_row("변동성 타겟팅\n[dim]ATR 기반 비중 조절 사용 여부[/dim]", "USE_VOLATILITY_TARGETING", f"{getattr(config, 'USE_VOLATILITY_TARGETING', True)}")
    if getattr(config, 'USE_VOLATILITY_TARGETING', True):
        table.add_row("  └ 목표 변동성\n    [dim]연간 변동성 목표치[/dim]", "TARGET_VOLATILITY", f"{getattr(config, 'TARGET_VOLATILITY', 0.20)}")
        table.add_row("  └ 스케일링 범위\n    [dim]비중 조절 최소~최대 배수[/dim]", "VOLATILITY_SCALING_MIN/MAX", f"{getattr(config, 'VOLATILITY_SCALING_MIN', 0.3)} ~ {getattr(config, 'VOLATILITY_SCALING_MAX', 2.0)}")
    
    table.add_row("체결 감시 주기\n[dim]주문 직후 체결 확인 간격[/dim]", "CONCLUSION_CHECK_INTERVAL", f"{getattr(config, 'CONCLUSION_CHECK_INTERVAL', 5)}")
    table.add_row("집중 감시 시간\n[dim]주문 후 집중 감시 유지 시간[/dim]", "CONCLUSION_CHECK_ACTIVE_DURATION", f"{getattr(config, 'CONCLUSION_CHECK_ACTIVE_DURATION', 100)}")
    table.add_row("미체결 취소 대기\n[dim]지정가 주문 유지 시간[/dim]", "UNFILLED_ORDER_CANCEL_SECONDS", f"{getattr(config, 'UNFILLED_ORDER_CANCEL_SECONDS', 600)}")
    
    table.add_section()

    # 2. 매수/분석 임계값
    thresholds = config.ANALYSIS_THRESHOLDS
    table.add_row("[bold]2. 매수/분석[/]", "", "")
    table.add_row("매수 기준 점수\n[dim]진입 임계값 (종합 점수)[/dim]", "ANALYSIS_THRESHOLDS['BUY_SCORE']", f"{thresholds.get('BUY_SCORE')}")
    table.add_row("상승 추세 점수\n[dim]관망/상승 판단 기준[/dim]", "ANALYSIS_THRESHOLDS['RISE_SCORE']", f"{thresholds.get('RISE_SCORE')}")
    table.add_row("매수 허용 RSI 상한\n[dim]과열 방지 (이 값보다 낮아야 매수)[/dim]", "ANALYSIS_THRESHOLDS['BUY_RSI_MAX']", f"{thresholds.get('BUY_RSI_MAX')}")
    table.add_row("매수 체결강도 기준\n[dim]수급 확인 (이 값 이상이어야 매수)[/dim]", "ANALYSIS_THRESHOLDS['BUY_VOL_STRENGTH']", f"{thresholds.get('BUY_VOL_STRENGTH')}%")
    
    table.add_section()

    # 3. 매도 전략
    sell = config.SELL_STRATEGY
    table.add_row("[bold]3. 매도 전략[/]", "", "")
    table.add_row("익절 수익률\n[dim]목표 수익 달성 시 매도[/dim]", "SELL_STRATEGY['TAKE_PROFIT_RATE']", f"{sell.get('TAKE_PROFIT_RATE')}%")
    table.add_row("손절 수익률\n[dim]손실 제한 (Stop Loss)[/dim]", "SELL_STRATEGY['STOP_LOSS_RATE']", f"{sell.get('STOP_LOSS_RATE')}%")
    table.add_row("매도(추세이탈) 점수\n[dim]점수 하락 시 매도[/dim]", "SELL_STRATEGY['SELL_SCORE']", f"{sell.get('SELL_SCORE')}")
    table.add_row("과열 매도 RSI\n[dim]RSI 과열 시 선제 매도[/dim]", "SELL_STRATEGY['TAKE_PROFIT_RSI']", f"{sell.get('TAKE_PROFIT_RSI')}")
    table.add_row("TS 발동 수익률\n[dim]트레일링 스탑 감시 시작점[/dim]", "SELL_STRATEGY['TRAILING_STOP_ACTIVATION_RATE']", f"{sell.get('TRAILING_STOP_ACTIVATION_RATE')}%")
    table.add_row("TS 하락 감지율\n[dim]최고가 대비 하락 시 매도[/dim]", "SELL_STRATEGY['TRAILING_STOP_CALLBACK_RATE']", f"{sell.get('TRAILING_STOP_CALLBACK_RATE')}%")
    table.add_row("ATR 손절 사용\n[dim]변동성 기반 동적 손절[/dim]", "SELL_STRATEGY['USE_ATR_STOP']", f"{sell.get('USE_ATR_STOP', False)}")
    table.add_row("ATR 손절 배수\n[dim]ATR * 배수 만큼 손절폭 설정[/dim]", "SELL_STRATEGY['ATR_STOP_MULTIPLIER']", f"{sell.get('ATR_STOP_MULTIPLIER', 2.0)}")

    table.add_section()

    # 4. 기술적 지표
    ind = config.INDICATOR_PARAMS
    table.add_row("[bold]4. 기술적 지표[/]", "", "")
    table.add_row("데이터 조회 기간\n[dim]일봉 데이터 조회 범위[/dim]", "INDICATOR_PARAMS['CHART_LOOKBACK_DAYS']", f"{ind.get('CHART_LOOKBACK_DAYS')}일")
    table.add_row("SAR (Start/Step/Max)\n[dim]파라볼릭 SAR 가속변수[/dim]", "INDICATOR_PARAMS['SAR_AF_...']", f"{ind.get('SAR_AF_START')}/{ind.get('SAR_AF_STEP')}/{ind.get('SAR_AF_MAX')}")
    table.add_row("RSI (Period/Signal)\n[dim]상대강도지수 기간[/dim]", "INDICATOR_PARAMS['RSI_...']", f"{ind.get('RSI_PERIOD')}/{ind.get('RSI_SIGNAL')}")
    table.add_row("RSI (Up/Mid/Low)\n[dim]과매수/중심/과매도 기준[/dim]", "INDICATOR_PARAMS['RSI_...']", f"{ind.get('RSI_UPPER')}/{ind.get('RSI_MID')}/{ind.get('RSI_LOWER')}")
    table.add_row("ADX 기간\n[dim]추세 강도 지표[/dim]", "INDICATOR_PARAMS['ADX_PERIOD']", f"{ind.get('ADX_PERIOD')}")
    table.add_row("CCI (Window/Up/Low)\n[dim]상품채널지수[/dim]", "INDICATOR_PARAMS['CCI_...']", f"{ind.get('CCI_WINDOW')}/{ind.get('CCI_UPPER')}/{ind.get('CCI_LOWER')}")
    table.add_row("MACD (Fast/Slow/Sig)\n[dim]이동평균수렴확산[/dim]", "INDICATOR_PARAMS['MACD_...']", f"{ind.get('MACD_FAST')}/{ind.get('MACD_SLOW')}/{ind.get('MACD_SIGNAL')}")
    table.add_row("OBV MA 기간\n[dim]거래량 추세 이동평균[/dim]", "INDICATOR_PARAMS['OBV_MA_PERIOD']", f"{ind.get('OBV_MA_PERIOD')}")
    table.add_row("ATR 기간\n[dim]평균 진폭 (변동성)[/dim]", "INDICATOR_PARAMS['ATR_PERIOD']", f"{ind.get('ATR_PERIOD')}")

    table.add_section()

    # 5. 텔레그램
    table.add_row("[bold]5. 텔레그램[/]", "", "")
    table.add_row("사용 여부", "ENABLE_TELEGRAM", f"{getattr(config, 'ENABLE_TELEGRAM', True)}")
    table.add_row("인스턴스 이름\n[dim]알림 메시지 머리말[/dim]", "TELEGRAM_INSTANCE_NAME", f"{getattr(config, 'TELEGRAM_INSTANCE_NAME', 'HTS')}")
    table.add_row("폴링 타임아웃\n[dim]봇 명령어 수신 대기 시간[/dim]", "TELEGRAM_POLLING_TIMEOUT", f"{getattr(config, 'TELEGRAM_POLLING_TIMEOUT', 10)}")

    table.add_section()

    # 6. 로그 레벨
    table.add_row("[bold]6. 로그 레벨[/]", "", "")
    table.add_row("화면 (Screen)\n[dim]터미널 출력 레벨[/dim]", "SCREEN_DEBUG_LEVEL", f"{getattr(config, 'SCREEN_DEBUG_LEVEL', 'OFF')}")
    table.add_row("파일 (File)\n[dim]로그 파일 저장 레벨[/dim]", "FILE_DEBUG_LEVEL", f"{getattr(config, 'FILE_DEBUG_LEVEL', 'WARNING')}")

    console.print(table)
    console.print()

def _edit_config_table(title, items_source):
    """설정 변경을 위한 공통 테이블 UI 함수"""
    while True:
        # items_source가 함수면 호출하여 최신 리스트 가져오기 (동적 메뉴 지원)
        items = items_source() if callable(items_source) else items_source
        
        console.print()
        table = Table(title=title, box=box.HORIZONTALS, show_header=True, header_style="dim", border_style="dim", expand=False)
        table.add_column("No.", justify="right", style="dim", width=4)
        table.add_column("설정 항목 (Description)", justify="left", style="white")
        table.add_column("변수명 (Config Name)", justify="left", style="dim")
        table.add_column("설정값 (Value)", justify="right", style="cyan")

        for i, item in enumerate(items):
            # Section 구분
            if i > 0 and item.get('section') != items[i-1].get('section'):
                table.add_section()
                
            val = item['get']()
            table.add_row(
                str(i + 1),
                f"{item['desc']}\n[dim]{item['help']}[/dim]",
                item['name'],
                str(val)
            )
        
        console.print(table)
        
        choice = Prompt.ask("\n수정할 항목 번호 선택 [dim](a: 전체, q: 종료)[/dim]", choices=[str(i+1) for i in range(len(items))] + ['a', 'q', 'A', 'Q'], default='q')
        
        if choice.lower() == 'q':
            break
            
        targets = []
        if choice.lower() == 'a':
            targets = items
        else:
            targets = [items[int(choice)-1]]
            
        for item in targets:
            curr_val = item['get']()
            console.print(f"\n[bold cyan]>> {item['desc']} 수정[/bold cyan]")
            console.print(f"[dim]설명: {item['help']}[/dim]")
            
            if item['type'] == 'bool':
                # [수정] True/False 입력 지원을 위해 Confirm 대신 Prompt 사용
                prompt_msg = f"현재 설정을 유지하시겠습니까? [Y/n] ([bold cyan]{curr_val}[/bold cyan])"
                
                canceled = False
                while True:
                    val = Prompt.ask(prompt_msg, default="y", show_default=False)
                    val_lower = val.lower()
                    
                    if val_lower == 'q':
                        canceled = True
                        break
                    
                    target_val = curr_val
                    
                    if val_lower in ['y', 'yes', '1']:
                        target_val = curr_val
                    elif val_lower in ['n', 'no', '0']:
                        target_val = not curr_val
                    elif val_lower == 'true':
                        target_val = True
                    elif val_lower == 'false':
                        target_val = False
                    else:
                        console.print("[red]잘못된 입력입니다. Y/N 또는 True/False를 입력해주세요. (취소: q)[/red]")
                        continue

                    if target_val != curr_val:
                        item['set'](target_val)
                        if 'callback' in item:
                            item['callback']()
                        console.print(f"[cyan]>> 설정이 변경되었습니다: {target_val}[/cyan]")
                    break
                
                if canceled:
                    console.print("[yellow]입력이 취소되었습니다.[/yellow]")
                    break
                continue

            prompt_kwargs = {"default": str(curr_val)}
            if 'choices' in item:
                choices = list(item['choices'])
                if 'q' not in choices: choices.append('q')
                if 'Q' not in choices: choices.append('Q')
                prompt_kwargs["choices"] = choices
                
            val = Prompt.ask(f"새로운 값 입력 [dim](취소: q)[/dim]", **prompt_kwargs)
            
            if val.lower() == 'q':
                console.print("[yellow]입력이 취소되었습니다.[/yellow]")
                break
            
            if val == str(curr_val): continue
            
            try:
                converted_val = val
                if item['type'] == 'float':
                    converted_val = float(val)
                elif item['type'] == 'int':
                    converted_val = int(val)
                elif item['type'] == 'bool':
                    converted_val = val.lower() in ['y', 'yes', 'true', '1']
                elif item['type'] == 'time':
                    if not _validate_time_format(val):
                        console.print("[red]잘못된 시간 형식입니다. (HHMM)[/red]")
                        continue
                    converted_val = val
                
                if 'validator' in item and not item['validator'](converted_val):
                    console.print("[red]입력값이 유효 범위를 벗어났습니다.[/red]")
                    continue

                item['set'](converted_val)
                
                if 'callback' in item:
                    item['callback']()
                    
            except Exception as e:
                console.print(f"[red]잘못된 입력입니다: {e}[/red]")
        
        _save_dynamic_config()

def modify_analysis_thresholds():
    items = [
        {"desc": "매수 기준 점수", "help": "진입 임계값 (종합 점수)", "name": "BUY_SCORE", "type": "float",
         "get": lambda: config.ANALYSIS_THRESHOLDS["BUY_SCORE"], "set": lambda v: config.ANALYSIS_THRESHOLDS.update({"BUY_SCORE": v})},
        {"desc": "상승 추세 점수", "help": "관망/상승 판단 기준", "name": "RISE_SCORE", "type": "float",
         "get": lambda: config.ANALYSIS_THRESHOLDS["RISE_SCORE"], "set": lambda v: config.ANALYSIS_THRESHOLDS.update({"RISE_SCORE": v})},
        {"desc": "매수 허용 RSI 상한", "help": "과열 방지 (이 값보다 낮아야 매수)", "name": "BUY_RSI_MAX", "type": "float",
         "get": lambda: config.ANALYSIS_THRESHOLDS["BUY_RSI_MAX"], "set": lambda v: config.ANALYSIS_THRESHOLDS.update({"BUY_RSI_MAX": v})},
        {"desc": "매수 체결강도 기준", "help": "수급 확인 (이 값 이상이어야 매수)", "name": "BUY_VOL_STRENGTH", "type": "float",
         "get": lambda: config.ANALYSIS_THRESHOLDS.get("BUY_VOL_STRENGTH", 100.0), "set": lambda v: config.ANALYSIS_THRESHOLDS.update({"BUY_VOL_STRENGTH": v})}
    ]
    _edit_config_table("매수/분석 임계값 설정 (ANALYSIS_THRESHOLDS)", items)

def modify_sell_strategy():
    items = [
        {"desc": "익절 수익률(%)", "help": "목표 수익 달성 시 매도", "name": "TAKE_PROFIT_RATE", "type": "float",
         "get": lambda: config.SELL_STRATEGY["TAKE_PROFIT_RATE"], "set": lambda v: config.SELL_STRATEGY.update({"TAKE_PROFIT_RATE": v})},
        {"desc": "손절 수익률(%)", "help": "손실 제한 (Stop Loss)", "name": "STOP_LOSS_RATE", "type": "float",
         "get": lambda: config.SELL_STRATEGY["STOP_LOSS_RATE"], "set": lambda v: config.SELL_STRATEGY.update({"STOP_LOSS_RATE": v})},
        {"desc": "매도(추세이탈) 점수", "help": "점수 하락 시 매도", "name": "SELL_SCORE", "type": "float",
         "get": lambda: config.SELL_STRATEGY["SELL_SCORE"], "set": lambda v: config.SELL_STRATEGY.update({"SELL_SCORE": v})},
        {"desc": "과열 매도 RSI", "help": "RSI 과열 시 선제 매도", "name": "TAKE_PROFIT_RSI", "type": "float",
         "get": lambda: config.SELL_STRATEGY["TAKE_PROFIT_RSI"], "set": lambda v: config.SELL_STRATEGY.update({"TAKE_PROFIT_RSI": v})},
        {"desc": "TS 발동 수익률(%)", "help": "트레일링 스탑 감시 시작점", "name": "TS_ACTIVATION", "type": "float",
         "get": lambda: config.SELL_STRATEGY.get("TRAILING_STOP_ACTIVATION_RATE", 10.0), "set": lambda v: config.SELL_STRATEGY.update({"TRAILING_STOP_ACTIVATION_RATE": v})},
        {"desc": "TS 하락 감지율(%)", "help": "최고가 대비 하락 시 매도", "name": "TS_CALLBACK", "type": "float",
         "get": lambda: config.SELL_STRATEGY.get("TRAILING_STOP_CALLBACK_RATE", 3.0), "set": lambda v: config.SELL_STRATEGY.update({"TRAILING_STOP_CALLBACK_RATE": v})},
        {"desc": "ATR 손절 사용", "help": "변동성 기반 동적 손절", "name": "USE_ATR_STOP", "type": "bool", "choices": ["y", "n"],
         "get": lambda: config.SELL_STRATEGY.get("USE_ATR_STOP", False), "set": lambda v: config.SELL_STRATEGY.update({"USE_ATR_STOP": v})},
        {"desc": "ATR 손절 배수", "help": "ATR * 배수 만큼 손절폭 설정", "name": "ATR_STOP_MULTIPLIER", "type": "float",
         "get": lambda: config.SELL_STRATEGY.get("ATR_STOP_MULTIPLIER", 2.0), "set": lambda v: config.SELL_STRATEGY.update({"ATR_STOP_MULTIPLIER": v})}
    ]
    _edit_config_table("매도 전략 설정 (SELL_STRATEGY)", items)

def modify_indicator_params():
    items = [
        {"desc": "데이터 조회 기간(일)", "help": "일봉 데이터 조회 범위", "name": "CHART_LOOKBACK_DAYS", "type": "int", "section": "General",
         "get": lambda: config.INDICATOR_PARAMS["CHART_LOOKBACK_DAYS"], "set": lambda v: config.INDICATOR_PARAMS.update({"CHART_LOOKBACK_DAYS": v})},
        
        {"desc": "SAR 가속변수 시작(Start)", "help": "파라볼릭 SAR 초기값", "name": "SAR_AF_START", "type": "float", "section": "SAR",
         "get": lambda: config.INDICATOR_PARAMS["SAR_AF_START"], "set": lambda v: config.INDICATOR_PARAMS.update({"SAR_AF_START": v})},
        {"desc": "SAR 가속변수 증가(Step)", "help": "파라볼릭 SAR 증가값", "name": "SAR_AF_STEP", "type": "float", "section": "SAR",
         "get": lambda: config.INDICATOR_PARAMS["SAR_AF_STEP"], "set": lambda v: config.INDICATOR_PARAMS.update({"SAR_AF_STEP": v})},
        {"desc": "SAR 가속변수 최대(Max)", "help": "파라볼릭 SAR 최대값", "name": "SAR_AF_MAX", "type": "float", "section": "SAR",
         "get": lambda: config.INDICATOR_PARAMS["SAR_AF_MAX"], "set": lambda v: config.INDICATOR_PARAMS.update({"SAR_AF_MAX": v})},
        
        {"desc": "RSI 계산 기간", "help": "상대강도지수 기간", "name": "RSI_PERIOD", "type": "int", "section": "RSI",
         "get": lambda: config.INDICATOR_PARAMS["RSI_PERIOD"], "set": lambda v: config.INDICATOR_PARAMS.update({"RSI_PERIOD": v})},
        {"desc": "RSI 시그널 기간", "help": "RSI 이동평균 기간", "name": "RSI_SIGNAL", "type": "int", "section": "RSI",
         "get": lambda: config.INDICATOR_PARAMS["RSI_SIGNAL"], "set": lambda v: config.INDICATOR_PARAMS.update({"RSI_SIGNAL": v})},
        {"desc": "RSI 과매수 기준", "help": "이 값 이상이면 과열", "name": "RSI_UPPER", "type": "int", "section": "RSI",
         "get": lambda: config.INDICATOR_PARAMS["RSI_UPPER"], "set": lambda v: config.INDICATOR_PARAMS.update({"RSI_UPPER": v})},
        {"desc": "RSI 중심선", "help": "강세/약세 기준선", "name": "RSI_MID", "type": "int", "section": "RSI",
         "get": lambda: config.INDICATOR_PARAMS["RSI_MID"], "set": lambda v: config.INDICATOR_PARAMS.update({"RSI_MID": v})},
        {"desc": "RSI 과매도 기준", "help": "이 값 이하면 침체", "name": "RSI_LOWER", "type": "int", "section": "RSI",
         "get": lambda: config.INDICATOR_PARAMS["RSI_LOWER"], "set": lambda v: config.INDICATOR_PARAMS.update({"RSI_LOWER": v})},
        
        {"desc": "ADX 계산 기간", "help": "추세 강도 지표", "name": "ADX_PERIOD", "type": "int", "section": "ADX",
         "get": lambda: config.INDICATOR_PARAMS["ADX_PERIOD"], "set": lambda v: config.INDICATOR_PARAMS.update({"ADX_PERIOD": v})},
        
        {"desc": "CCI 계산 기간", "help": "상품채널지수 기간", "name": "CCI_WINDOW", "type": "int", "section": "CCI",
         "get": lambda: config.INDICATOR_PARAMS["CCI_WINDOW"], "set": lambda v: config.INDICATOR_PARAMS.update({"CCI_WINDOW": v})},
        {"desc": "CCI 과매수 기준", "help": "이 값 이상이면 과열", "name": "CCI_UPPER", "type": "int", "section": "CCI",
         "get": lambda: config.INDICATOR_PARAMS["CCI_UPPER"], "set": lambda v: config.INDICATOR_PARAMS.update({"CCI_UPPER": v})},
        {"desc": "CCI 과매도 기준", "help": "이 값 이하면 침체", "name": "CCI_LOWER", "type": "int", "section": "CCI",
         "get": lambda: config.INDICATOR_PARAMS["CCI_LOWER"], "set": lambda v: config.INDICATOR_PARAMS.update({"CCI_LOWER": v})},
        
        {"desc": "MACD Fast EMA", "help": "단기 지수이동평균", "name": "MACD_FAST", "type": "int", "section": "MACD",
         "get": lambda: config.INDICATOR_PARAMS["MACD_FAST"], "set": lambda v: config.INDICATOR_PARAMS.update({"MACD_FAST": v})},
        {"desc": "MACD Slow EMA", "help": "장기 지수이동평균", "name": "MACD_SLOW", "type": "int", "section": "MACD",
         "get": lambda: config.INDICATOR_PARAMS["MACD_SLOW"], "set": lambda v: config.INDICATOR_PARAMS.update({"MACD_SLOW": v})},
        {"desc": "MACD Signal", "help": "시그널 기간", "name": "MACD_SIGNAL", "type": "int", "section": "MACD",
         "get": lambda: config.INDICATOR_PARAMS["MACD_SIGNAL"], "set": lambda v: config.INDICATOR_PARAMS.update({"MACD_SIGNAL": v})},
        
        {"desc": "OBV 이동평균 기간", "help": "거래량 추세 판단", "name": "OBV_MA_PERIOD", "type": "int", "section": "OBV",
         "get": lambda: config.INDICATOR_PARAMS["OBV_MA_PERIOD"], "set": lambda v: config.INDICATOR_PARAMS.update({"OBV_MA_PERIOD": v})},
        
        {"desc": "ATR 계산 기간", "help": "평균 진폭 (변동성)", "name": "ATR_PERIOD", "type": "int", "section": "ATR",
         "get": lambda: config.INDICATOR_PARAMS.get("ATR_PERIOD", 14), "set": lambda v: config.INDICATOR_PARAMS.update({"ATR_PERIOD": v})}
    ]
    _edit_config_table("기술적 지표 파라미터 (Indicators)", items)

def modify_telegram_settings():
    items = [
        {"desc": "텔레그램 알림 사용", "help": "알림 기능 활성화 여부", "name": "ENABLE_TELEGRAM", "type": "bool", "choices": ["y", "n"],
         "get": lambda: getattr(config, 'ENABLE_TELEGRAM', True), "set": lambda v: setattr(config, 'ENABLE_TELEGRAM', v)},
        {"desc": "인스턴스 이름", "help": "알림 메시지 머리말", "name": "TELEGRAM_INSTANCE_NAME", "type": "str",
         "get": lambda: getattr(config, 'TELEGRAM_INSTANCE_NAME', "HTS"), "set": lambda v: setattr(config, 'TELEGRAM_INSTANCE_NAME', v)},
        {"desc": "폴링 타임아웃(초)", "help": "봇 명령어 수신 대기 시간", "name": "TELEGRAM_POLLING_TIMEOUT", "type": "int",
         "get": lambda: getattr(config, 'TELEGRAM_POLLING_TIMEOUT', 10), "set": lambda v: setattr(config, 'TELEGRAM_POLLING_TIMEOUT', v)}
    ]
    _edit_config_table("텔레그램 설정 (Telegram)", items)

def modify_log_settings():
    items = [
        {"desc": "화면 로그 레벨", "help": "터미널 출력 레벨", "name": "SCREEN_DEBUG_LEVEL", "type": "str", "choices": ["OFF", "TRACE", "DEBUG"],
         "get": lambda: getattr(config, 'SCREEN_DEBUG_LEVEL', "OFF"), "set": lambda v: setattr(config, 'SCREEN_DEBUG_LEVEL', v)},
        {"desc": "파일 로그 레벨", "help": "로그 파일 저장 레벨", "name": "FILE_DEBUG_LEVEL", "type": "str", "choices": ["DEBUG", "INFO", "WARNING", "ERROR"],
         "get": lambda: getattr(config, 'FILE_DEBUG_LEVEL', "WARNING"), "set": lambda v: setattr(config, 'FILE_DEBUG_LEVEL', v),
         "callback": config.setup_logging}
    ]
    _edit_config_table("로그 레벨 설정 (Log Level)", items)

def _validate_time_format(val):
    if len(val) == 4 and val.isdigit():
        hh = int(val[:2])
        mm = int(val[2:])
        if 0 <= hh <= 23 and 0 <= mm <= 59:
            return True
    return False

def modify_system_trading_general():
    def get_items():
        items = [
            {"desc": "종목당 투자 비중", "help": "전체 자산 대비 한 종목 투자 비율 (0.1~1.0)", "name": "SYSTEM_INVEST_PER_STOCK", "type": "float", "section": "General",
             "get": lambda: getattr(config, 'SYSTEM_INVEST_PER_STOCK', 0.5), "set": lambda v: setattr(config, 'SYSTEM_INVEST_PER_STOCK', v),
             "validator": lambda v: 0 < v <= 1.0},
            {"desc": "최대 보유 종목 수", "help": "포트폴리오 최대 종목 개수", "name": "SYSTEM_MAX_HOLDINGS", "type": "int", "section": "General",
             "get": lambda: getattr(config, 'SYSTEM_MAX_HOLDINGS', 5), "set": lambda v: setattr(config, 'SYSTEM_MAX_HOLDINGS', v)},
            {"desc": "모니터링 주기 (초)", "help": "자동매매 루프 실행 간격", "name": "SYSTEM_TRADING_INTERVAL", "type": "int", "section": "General",
             "get": lambda: getattr(config, 'SYSTEM_TRADING_INTERVAL', 180), "set": lambda v: setattr(config, 'SYSTEM_TRADING_INTERVAL', v)},
            {"desc": "일일 손실 제한 (%)", "help": "자산 보호를 위한 비상 정지 기준", "name": "SYSTEM_DAILY_LOSS_LIMIT", "type": "float", "section": "General",
             "get": lambda: getattr(config, 'SYSTEM_DAILY_LOSS_LIMIT', 10.0), "set": lambda v: setattr(config, 'SYSTEM_DAILY_LOSS_LIMIT', v)},
            {"desc": "1회 최대 리스크 (%)", "help": "계좌 대비 1회 매매 최대 손실폭", "name": "SYSTEM_RISK_PER_TRADE", "type": "float", "section": "General",
             "get": lambda: getattr(config, 'SYSTEM_RISK_PER_TRADE', 5.0), "set": lambda v: setattr(config, 'SYSTEM_RISK_PER_TRADE', v)},
            
            {"desc": "변동성 타겟팅 사용", "help": "ATR 기반 비중 조절 사용 여부", "name": "USE_VOLATILITY_TARGETING", "type": "bool", "choices": ["y", "n"], "section": "Volatility",
             "get": lambda: getattr(config, 'USE_VOLATILITY_TARGETING', True), "set": lambda v: setattr(config, 'USE_VOLATILITY_TARGETING', v)}
        ]
        
        if getattr(config, 'USE_VOLATILITY_TARGETING', True):
            items.extend([
                {"desc": "목표 연간 변동성", "help": "0.1=10%, 0.2=20%", "name": "TARGET_VOLATILITY", "type": "float", "section": "Volatility",
                 "get": lambda: getattr(config, 'TARGET_VOLATILITY', 0.20), "set": lambda v: setattr(config, 'TARGET_VOLATILITY', v)},
                {"desc": "스케일링 최대 배수", "help": "비중 확대 제한", "name": "VOLATILITY_SCALING_MAX", "type": "float", "section": "Volatility",
                 "get": lambda: getattr(config, 'VOLATILITY_SCALING_MAX', 2.0), "set": lambda v: setattr(config, 'VOLATILITY_SCALING_MAX', v)},
                {"desc": "스케일링 최소 배수", "help": "비중 축소 제한", "name": "VOLATILITY_SCALING_MIN", "type": "float", "section": "Volatility",
                 "get": lambda: getattr(config, 'VOLATILITY_SCALING_MIN', 0.3), "set": lambda v: setattr(config, 'VOLATILITY_SCALING_MIN', v)}
            ])
            
        items.extend([
            {"desc": "체결 집중 감시 주기(초)", "help": "주문 직후 체결 확인 간격", "name": "CONCLUSION_CHECK_INTERVAL", "type": "int", "section": "Monitoring",
             "get": lambda: getattr(config, 'CONCLUSION_CHECK_INTERVAL', 5), "set": lambda v: setattr(config, 'CONCLUSION_CHECK_INTERVAL', v)},
            {"desc": "체결 대기 감시 주기(초)", "help": "평상시 체결 확인 간격 (0:미사용)", "name": "CONCLUSION_CHECK_IDLE_INTERVAL", "type": "int", "section": "Monitoring",
             "get": lambda: getattr(config, 'CONCLUSION_CHECK_IDLE_INTERVAL', 300), "set": lambda v: setattr(config, 'CONCLUSION_CHECK_IDLE_INTERVAL', v)},
            {"desc": "집중 감시 유지 시간(초)", "help": "주문 후 집중 감시 유지 시간", "name": "CONCLUSION_CHECK_ACTIVE_DURATION", "type": "int", "section": "Monitoring",
             "get": lambda: getattr(config, 'CONCLUSION_CHECK_ACTIVE_DURATION', 100), "set": lambda v: setattr(config, 'CONCLUSION_CHECK_ACTIVE_DURATION', v)},
            {"desc": "미체결 취소 대기(초)", "help": "지정가 주문 유지 시간", "name": "UNFILLED_ORDER_CANCEL_SECONDS", "type": "int", "section": "Monitoring",
             "get": lambda: getattr(config, 'UNFILLED_ORDER_CANCEL_SECONDS', 600), "set": lambda v: setattr(config, 'UNFILLED_ORDER_CANCEL_SECONDS', v)},
             
            {"desc": "시장 필터링 사용", "help": "지수 하락 시 신규 매수 보류", "name": "USE_MARKET_FILTER", "type": "bool", "choices": ["y", "n"], "section": "Filter",
             "get": lambda: getattr(config, 'USE_MARKET_FILTER', True), "set": lambda v: setattr(config, 'USE_MARKET_FILTER', v)},
            {"desc": "시장 필터링 MA (일)", "help": "지수 추세 판단용 이동평균선", "name": "MARKET_FILTER_MA", "type": "int", "section": "Filter",
             "get": lambda: getattr(config, 'MARKET_FILTER_MA', 20), "set": lambda v: setattr(config, 'MARKET_FILTER_MA', v)},
             
            {"desc": "연속 에러 허용", "help": "시스템 중단 임계값", "name": "SYSTEM_MAX_CONSECUTIVE_ERRORS", "type": "int", "section": "System",
             "get": lambda: getattr(config, 'SYSTEM_MAX_CONSECUTIVE_ERRORS', 5), "set": lambda v: setattr(config, 'SYSTEM_MAX_CONSECUTIVE_ERRORS', v)},
            {"desc": "거래 시작 시간", "help": "매매 허용 시작 시각 (HHMM)", "name": "SYSTEM_TRADING_START_TIME", "type": "time", "section": "System",
             "get": lambda: getattr(config, 'SYSTEM_TRADING_START_TIME', "0915"), "set": lambda v: setattr(config, 'SYSTEM_TRADING_START_TIME', v)},
            {"desc": "거래 종료 시간", "help": "매매 허용 종료 시각 (HHMM)", "name": "SYSTEM_TRADING_END_TIME", "type": "time", "section": "System",
             "get": lambda: getattr(config, 'SYSTEM_TRADING_END_TIME', "1515"), "set": lambda v: setattr(config, 'SYSTEM_TRADING_END_TIME', v)}
        ])
        return items

    _edit_config_table("시스템 트레이딩 일반설정 (Trading General)", get_items)

def reset_to_default():
    if Prompt.ask("모든 설정을 시스템 기본값으로 초기화하시겠습니까?", choices=["y", "n"], default="n") != "y":
        return

    # 1. 파일 삭제
    config_path = os.path.join(config.JSON_DIR, "dynamic_config.json")
    if os.path.exists(config_path):
        try:
            os.remove(config_path)
            console.print(f"[dim]설정 파일 삭제 완료: {config_path}[/dim]")
        except Exception as e:
            console.print(f"[red]설정 파일 삭제 실패: {e}[/red]")

    # 2. 메모리 변수 초기화 (기본값 복원)
    config.ANALYSIS_THRESHOLDS.update({
        "BUY_SCORE": 8.0, "RISE_SCORE": 6.0, "BUY_RSI_MAX": 65, "BUY_VOL_STRENGTH": 100.0,
        "DISPARITY_UPPER": 110, "DISPARITY_LOWER": 90
    })
    config.SELL_STRATEGY.update({
        "STOP_LOSS_RATE": -7.0, "TAKE_PROFIT_RATE": 30.0, "TAKE_PROFIT_RSI": 75, "SELL_SCORE": 5.0,
        "TRAILING_STOP_ACTIVATION_RATE": 10.0, "TRAILING_STOP_CALLBACK_RATE": 3.0
        ,"USE_ATR_STOP": True, "ATR_STOP_MULTIPLIER": 2.0
    })
    config.INDICATOR_PARAMS.update({
        "CHART_LOOKBACK_DAYS": 730, "SAR_AF_START": 0.02, "SAR_AF_STEP": 0.02, "SAR_AF_MAX": 0.2,
        "ADX_PERIOD": 14, "CCI_WINDOW": 20, "CCI_UPPER": 100, "CCI_LOWER": -100,
        "MACD_FAST": 12, "MACD_SLOW": 26, "MACD_SIGNAL": 9, "OBV_MA_PERIOD": 5,
        "RSI_PERIOD": 14, "RSI_SIGNAL": 14, "RSI_UPPER": 70, "RSI_MID": 50, "RSI_LOWER": 30,
        "ATR_PERIOD": 14
    })
    
    config.SYSTEM_INVEST_PER_STOCK = 0.5
    config.SYSTEM_MAX_HOLDINGS = 5
    config.SYSTEM_TRADING_INTERVAL = 180
    config.SYSTEM_DAILY_LOSS_LIMIT = 10.0
    config.USE_MARKET_FILTER = True
    config.MARKET_FILTER_MA = 20
    config.CONCLUSION_CHECK_INTERVAL = 5
    config.CONCLUSION_CHECK_IDLE_INTERVAL = 300
    config.CONCLUSION_CHECK_ACTIVE_DURATION = 100
    config.ENABLE_TELEGRAM = True
    config.TELEGRAM_INSTANCE_NAME = "HTS"
    config.TELEGRAM_POLLING_TIMEOUT = 10
    config.SCREEN_DEBUG_LEVEL = "OFF"
    config.FILE_DEBUG_LEVEL = "WARNING"
    config.SYSTEM_MAX_CONSECUTIVE_ERRORS = 5
    config.SYSTEM_TRADING_START_TIME = "0915"
    config.SYSTEM_TRADING_END_TIME = "1515"
    config.SYSTEM_RISK_PER_TRADE = 5.0
    config.USE_VOLATILITY_TARGETING = True
    config.TARGET_VOLATILITY = 0.20
    config.VOLATILITY_SCALING_MAX = 2.0
    config.VOLATILITY_SCALING_MIN = 0.3
    config.UNFILLED_ORDER_CANCEL_SECONDS = 600

    console.print("\n[bold green]모든 설정이 기본값으로 초기화되었습니다.[/bold green]")

def system_config_menu():
    console.print("\n[bold cyan]=== 시스템 전체 설정 변경 ===[/]")
    console.print("[1] 시스템 설정 조회 (View Config)")
    console.print("[2] 시스템 트레이딩 일반설정 (Trading General)")
    console.print("[3] 매수/분석 임계값 (Analysis Thresholds)")
    console.print("[4] 매도 전략 (Sell Strategy)")
    console.print("[5] 기술적 지표 파라미터 (Indicators)")
    console.print("[6] 텔레그램 설정 (Telegram)")
    console.print("[7] 로그 레벨 설정 (Log Level)")
    console.print("[8] 설정 초기화 (Reset to Default)")
    console.print()
    
    choice = Prompt.ask("선택 [dim](취소: q)[/dim]", choices=["1", "2", "3", "4", "5", "6", "7", "8", "q", "Q"], default="1")
    if choice.lower() == 'q': return
    
    if choice == "1": view_system_config()
    elif choice == "2": modify_system_trading_general()
    elif choice == "3": modify_analysis_thresholds()
    elif choice == "4": modify_sell_strategy()
    elif choice == "5": modify_indicator_params()
    elif choice == "6": modify_telegram_settings()
    elif choice == "7": modify_log_settings()
    elif choice == "8": reset_to_default()