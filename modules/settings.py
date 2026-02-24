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
        "SYSTEM_TRADING_INTERVAL": getattr(config, 'SYSTEM_TRADING_INTERVAL', 180),
        "SYSTEM_DAILY_LOSS_LIMIT": getattr(config, 'SYSTEM_DAILY_LOSS_LIMIT', 10.0),
        "USE_MARKET_FILTER": getattr(config, 'USE_MARKET_FILTER', True),
        "MARKET_FILTER_MA": getattr(config, 'MARKET_FILTER_MA', 20),
        "CONCLUSION_CHECK_INTERVAL": getattr(config, 'CONCLUSION_CHECK_INTERVAL', 5),
        "CONCLUSION_CHECK_IDLE_INTERVAL": getattr(config, 'CONCLUSION_CHECK_IDLE_INTERVAL', 300),
        "CONCLUSION_CHECK_ACTIVE_DURATION": getattr(config, 'CONCLUSION_CHECK_ACTIVE_DURATION', 100),
        "ENABLE_TELEGRAM": getattr(config, 'ENABLE_TELEGRAM', True),
        "TELEGRAM_INSTANCE_NAME": getattr(config, 'TELEGRAM_INSTANCE_NAME', "HTS"),
        "TELEGRAM_POLLING_TIMEOUT": getattr(config, 'TELEGRAM_POLLING_TIMEOUT', 10),
        "SCREEN_DEBUG_LEVEL": getattr(config, 'SCREEN_DEBUG_LEVEL', "OFF"),
        "FILE_DEBUG_LEVEL": getattr(config, 'FILE_DEBUG_LEVEL', "INFO"),
        "SYSTEM_MAX_CONSECUTIVE_ERRORS": getattr(config, 'SYSTEM_MAX_CONSECUTIVE_ERRORS', 5),
        "SYSTEM_TRADING_START_TIME": getattr(config, 'SYSTEM_TRADING_START_TIME', "0915"),
        "SYSTEM_TRADING_END_TIME": getattr(config, 'SYSTEM_TRADING_END_TIME', "1515")
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
    console.print()
    table = Table(title="현재 시스템 설정 (System Configuration)", box=box.HORIZONTALS, show_header=True, header_style="bold cyan", border_style="dim")
    table.add_column("그룹", style="bold yellow", justify="left")
    table.add_column("항목", style="white", justify="left")
    table.add_column("설정값", style="green", justify="right")

    # 1. 시스템 트레이딩 일반
    table.add_row("트레이딩 일반", "종목당 투자 비중", f"{getattr(config, 'SYSTEM_INVEST_PER_STOCK', 0.5)}")
    table.add_row("", "모니터링 주기 (초)", f"{getattr(config, 'SYSTEM_TRADING_INTERVAL', 180)}")
    table.add_row("", "일일 손실 제한 (%)", f"{getattr(config, 'SYSTEM_DAILY_LOSS_LIMIT', 10.0)}")
    table.add_row("", "시장 필터링 사용", f"{getattr(config, 'USE_MARKET_FILTER', True)}")
    table.add_row("", "시장 필터링 MA (일)", f"{getattr(config, 'MARKET_FILTER_MA', 20)}")
    table.add_row("", "연속 에러 허용", f"{getattr(config, 'SYSTEM_MAX_CONSECUTIVE_ERRORS', 5)}")
    table.add_row("", "거래 시작 시간", f"{getattr(config, 'SYSTEM_TRADING_START_TIME', '0915')}")
    table.add_row("", "거래 종료 시간", f"{getattr(config, 'SYSTEM_TRADING_END_TIME', '1515')}")
    table.add_section()

    # 2. 매수/분석 임계값
    thresholds = config.ANALYSIS_THRESHOLDS
    table.add_row("매수/분석", "매수 기준 점수", f"{thresholds.get('BUY_SCORE')}")
    table.add_row("", "상승 추세 점수", f"{thresholds.get('RISE_SCORE')}")
    table.add_row("", "매수 허용 RSI 상한", f"{thresholds.get('BUY_RSI_MAX')}")
    table.add_row("", "매수 체결강도 기준", f"{thresholds.get('BUY_VOL_STRENGTH')}%")
    table.add_section()

    # 3. 매도 전략
    sell = config.SELL_STRATEGY
    table.add_row("매도 전략", "익절 수익률", f"{sell.get('TAKE_PROFIT_RATE')}%")
    table.add_row("", "손절 수익률", f"{sell.get('STOP_LOSS_RATE')}%")
    table.add_row("", "매도(추세이탈) 점수", f"{sell.get('SELL_SCORE')}")
    table.add_row("", "과열 매도 RSI", f"{sell.get('TAKE_PROFIT_RSI')}")
    table.add_row("", "TS 발동 수익률", f"{sell.get('TRAILING_STOP_ACTIVATION_RATE')}%")
    table.add_row("", "TS 하락 감지율", f"{sell.get('TRAILING_STOP_CALLBACK_RATE')}%")
    table.add_section()

    # 4. 기술적 지표
    ind = config.INDICATOR_PARAMS
    table.add_row("기술적 지표", "데이터 조회 기간", f"{ind.get('CHART_LOOKBACK_DAYS')}일")
    table.add_row("", "SAR (Start/Step/Max)", f"{ind.get('SAR_AF_START')}/{ind.get('SAR_AF_STEP')}/{ind.get('SAR_AF_MAX')}")
    table.add_row("", "RSI (Period/Signal)", f"{ind.get('RSI_PERIOD')}/{ind.get('RSI_SIGNAL')}")
    table.add_row("", "RSI (Up/Mid/Low)", f"{ind.get('RSI_UPPER')}/{ind.get('RSI_MID')}/{ind.get('RSI_LOWER')}")
    table.add_row("", "ADX 기간", f"{ind.get('ADX_PERIOD')}")
    table.add_row("", "CCI (Window/Up/Low)", f"{ind.get('CCI_WINDOW')}/{ind.get('CCI_UPPER')}/{ind.get('CCI_LOWER')}")
    table.add_row("", "MACD (Fast/Slow/Sig)", f"{ind.get('MACD_FAST')}/{ind.get('MACD_SLOW')}/{ind.get('MACD_SIGNAL')}")
    table.add_row("", "OBV MA 기간", f"{ind.get('OBV_MA_PERIOD')}")
    table.add_section()

    # 5. 텔레그램
    table.add_row("텔레그램", "사용 여부", f"{getattr(config, 'ENABLE_TELEGRAM', True)}")
    table.add_row("", "인스턴스 이름", f"{getattr(config, 'TELEGRAM_INSTANCE_NAME', 'HTS')}")
    table.add_row("", "폴링 타임아웃", f"{getattr(config, 'TELEGRAM_POLLING_TIMEOUT', 10)}")
    table.add_section()

    # 6. 로그 레벨
    table.add_row("로그 레벨", "화면 (Screen)", f"{getattr(config, 'SCREEN_DEBUG_LEVEL', 'OFF')}")
    table.add_row("", "파일 (File)", f"{getattr(config, 'FILE_DEBUG_LEVEL', 'INFO')}")

    console.print(table)
    console.print()

def modify_analysis_thresholds():
    console.print("\n[bold]3. 매수/분석 임계값 설정 (ANALYSIS_THRESHOLDS)[/bold]")
    
    curr = config.ANALYSIS_THRESHOLDS["BUY_SCORE"]
    val = Prompt.ask(f"매수 기준 점수 (현재: {curr})", default=str(curr))
    if val.lower() == 'q': return
    if val.isdigit(): config.ANALYSIS_THRESHOLDS["BUY_SCORE"] = int(val)

    curr = config.ANALYSIS_THRESHOLDS["RISE_SCORE"]
    val = Prompt.ask(f"상승 추세 점수 (현재: {curr})", default=str(curr))
    if val.lower() == 'q': return
    if val.isdigit(): config.ANALYSIS_THRESHOLDS["RISE_SCORE"] = int(val)

    curr = config.ANALYSIS_THRESHOLDS["BUY_RSI_MAX"]
    val = Prompt.ask(f"매수 허용 RSI 상한 (현재: {curr})", default=str(curr))
    if val.lower() == 'q': return
    try: config.ANALYSIS_THRESHOLDS["BUY_RSI_MAX"] = float(val)
    except: pass

    curr = config.ANALYSIS_THRESHOLDS.get("BUY_VOL_STRENGTH", 100.0)
    val = Prompt.ask(f"매수 체결강도 기준(%) (현재: {curr})", default=str(curr))
    if val.lower() == 'q': return
    try: config.ANALYSIS_THRESHOLDS["BUY_VOL_STRENGTH"] = float(val)
    except: pass
    
    _save_dynamic_config()

def modify_sell_strategy():
    console.print("\n[bold]4. 매도 전략 설정 (SELL_STRATEGY)[/bold]")
    
    curr = config.SELL_STRATEGY["TAKE_PROFIT_RATE"]
    val = Prompt.ask(f"익절 수익률(%) (현재: {curr})", default=str(curr))
    if val.lower() == 'q': return
    try: config.SELL_STRATEGY["TAKE_PROFIT_RATE"] = float(val)
    except: pass

    curr = config.SELL_STRATEGY["STOP_LOSS_RATE"]
    val = Prompt.ask(f"손절 수익률(%) (현재: {curr})", default=str(curr))
    if val.lower() == 'q': return
    try: config.SELL_STRATEGY["STOP_LOSS_RATE"] = float(val)
    except: pass

    curr = config.SELL_STRATEGY["SELL_SCORE"]
    val = Prompt.ask(f"매도(추세이탈) 기준 점수 (현재: {curr})", default=str(curr))
    if val.lower() == 'q': return
    if val.isdigit(): config.SELL_STRATEGY["SELL_SCORE"] = int(val)

    curr = config.SELL_STRATEGY["TAKE_PROFIT_RSI"]
    val = Prompt.ask(f"과열 매도 RSI 기준 (현재: {curr})", default=str(curr))
    if val.lower() == 'q': return
    try: config.SELL_STRATEGY["TAKE_PROFIT_RSI"] = float(val)
    except: pass

    curr_act = config.SELL_STRATEGY.get("TRAILING_STOP_ACTIVATION_RATE", 10.0)
    val = Prompt.ask(f"트레일링 스탑 발동 수익률(%) (현재: {curr_act})", default=str(curr_act))
    if val.lower() == 'q': return
    try: config.SELL_STRATEGY["TRAILING_STOP_ACTIVATION_RATE"] = float(val)
    except: pass

    curr_cb = config.SELL_STRATEGY.get("TRAILING_STOP_CALLBACK_RATE", 3.0)
    val = Prompt.ask(f"트레일링 스탑 하락 감지율(%) (현재: {curr_cb})", default=str(curr_cb))
    if val.lower() == 'q': return
    try: config.SELL_STRATEGY["TRAILING_STOP_CALLBACK_RATE"] = float(val)
    except: pass

    _save_dynamic_config()

def modify_indicator_params():
    console.print("\n[bold]5. 기술적 지표 파라미터 (Indicators)[/bold]")
    
    curr = config.INDICATOR_PARAMS["CHART_LOOKBACK_DAYS"]
    val = Prompt.ask(f"데이터 조회 기간(일) (현재: {curr})", default=str(curr))
    if val.lower() == 'q': return
    if val.isdigit(): config.INDICATOR_PARAMS["CHART_LOOKBACK_DAYS"] = int(val)

    # 1. SAR
    console.print("\n[bold cyan][1] Parabolic SAR (추세 반전)[/]")
    curr = config.INDICATOR_PARAMS["SAR_AF_START"]
    val = Prompt.ask(f"가속변수 시작값(AF Start) (현재: {curr})", default=str(curr))
    if val.lower() == 'q': return
    try: config.INDICATOR_PARAMS["SAR_AF_START"] = float(val)
    except: pass
    
    curr = config.INDICATOR_PARAMS["SAR_AF_STEP"]
    val = Prompt.ask(f"가속변수 증가값(AF Step) (현재: {curr})", default=str(curr))
    if val.lower() == 'q': return
    try: config.INDICATOR_PARAMS["SAR_AF_STEP"] = float(val)
    except: pass

    curr = config.INDICATOR_PARAMS["SAR_AF_MAX"]
    val = Prompt.ask(f"가속변수 최대값(AF Max) (현재: {curr})", default=str(curr))
    if val.lower() == 'q': return
    try: config.INDICATOR_PARAMS["SAR_AF_MAX"] = float(val)
    except: pass

    # 2. RSI
    console.print("\n[bold cyan][2] RSI (상대강도지수)[/]")
    curr = config.INDICATOR_PARAMS["RSI_PERIOD"]
    val = Prompt.ask(f"RSI 계산 기간 (현재: {curr})", default=str(curr))
    if val.lower() == 'q': return
    if val.isdigit(): config.INDICATOR_PARAMS["RSI_PERIOD"] = int(val)

    curr = config.INDICATOR_PARAMS["RSI_SIGNAL"]
    val = Prompt.ask(f"RSI 시그널 기간 (현재: {curr})", default=str(curr))
    if val.lower() == 'q': return
    if val.isdigit(): config.INDICATOR_PARAMS["RSI_SIGNAL"] = int(val)

    curr_up = config.INDICATOR_PARAMS["RSI_UPPER"]
    val = Prompt.ask(f"RSI 과매수 기준 (현재: {curr_up})", default=str(curr_up))
    if val.lower() == 'q': return
    if val.isdigit(): config.INDICATOR_PARAMS["RSI_UPPER"] = int(val)

    curr = config.INDICATOR_PARAMS["RSI_MID"]
    val = Prompt.ask(f"RSI 중심선 (현재: {curr})", default=str(curr))
    if val.lower() == 'q': return
    if val.isdigit(): config.INDICATOR_PARAMS["RSI_MID"] = int(val)

    curr_low = config.INDICATOR_PARAMS["RSI_LOWER"]
    val = Prompt.ask(f"RSI 과매도 기준 (현재: {curr_low})", default=str(curr_low))
    if val.lower() == 'q': return
    if val.isdigit(): config.INDICATOR_PARAMS["RSI_LOWER"] = int(val)

    # 3. ADX
    console.print("\n[bold cyan][3] ADX (추세 강도)[/]")
    curr = config.INDICATOR_PARAMS["ADX_PERIOD"]
    val = Prompt.ask(f"ADX 계산 기간 (현재: {curr})", default=str(curr))
    if val.lower() == 'q': return
    if val.isdigit(): config.INDICATOR_PARAMS["ADX_PERIOD"] = int(val)

    # 4. CCI
    console.print("\n[bold cyan][4] CCI (상품채널지수)[/]")
    curr = config.INDICATOR_PARAMS["CCI_WINDOW"]
    val = Prompt.ask(f"CCI 계산 기간 (현재: {curr})", default=str(curr))
    if val.lower() == 'q': return
    if val.isdigit(): config.INDICATOR_PARAMS["CCI_WINDOW"] = int(val)
    
    curr = config.INDICATOR_PARAMS["CCI_UPPER"]
    val = Prompt.ask(f"CCI 과매수 기준 (현재: {curr})", default=str(curr))
    if val.lower() == 'q': return
    if val.isdigit(): config.INDICATOR_PARAMS["CCI_UPPER"] = int(val)

    curr = config.INDICATOR_PARAMS["CCI_LOWER"]
    val = Prompt.ask(f"CCI 과매도 기준 (현재: {curr})", default=str(curr))
    if val.lower() == 'q': return
    if val.isdigit(): config.INDICATOR_PARAMS["CCI_LOWER"] = int(val)

    # 5. MACD
    console.print("\n[bold cyan][5] MACD (이동평균수렴확산)[/]")
    curr = config.INDICATOR_PARAMS["MACD_FAST"]
    val = Prompt.ask(f"Fast EMA 기간 (현재: {curr})", default=str(curr))
    if val.lower() == 'q': return
    if val.isdigit(): config.INDICATOR_PARAMS["MACD_FAST"] = int(val)

    curr = config.INDICATOR_PARAMS["MACD_SLOW"]
    val = Prompt.ask(f"Slow EMA 기간 (현재: {curr})", default=str(curr))
    if val.lower() == 'q': return
    if val.isdigit(): config.INDICATOR_PARAMS["MACD_SLOW"] = int(val)

    curr = config.INDICATOR_PARAMS["MACD_SIGNAL"]
    val = Prompt.ask(f"Signal 기간 (현재: {curr})", default=str(curr))
    if val.lower() == 'q': return
    if val.isdigit(): config.INDICATOR_PARAMS["MACD_SIGNAL"] = int(val)

    # 6. OBV
    console.print("\n[bold cyan][6] OBV (거래량 추세)[/]")
    curr = config.INDICATOR_PARAMS["OBV_MA_PERIOD"]
    val = Prompt.ask(f"OBV 이동평균 기간 (현재: {curr})", default=str(curr))
    if val.lower() == 'q': return
    if val.isdigit(): config.INDICATOR_PARAMS["OBV_MA_PERIOD"] = int(val)

    _save_dynamic_config()

def modify_telegram_settings():
    console.print("\n[bold]6. 텔레그램 설정 (Telegram)[/bold]")
    
    curr = getattr(config, 'ENABLE_TELEGRAM', True)
    val = Prompt.ask(f"텔레그램 알림 사용 (현재: {curr})", choices=["y", "n"], default="y" if curr else "n")
    if val.lower() == 'q': return
    config.ENABLE_TELEGRAM = (val == "y")
    
    curr_name = getattr(config, 'TELEGRAM_INSTANCE_NAME', "HTS")
    val = Prompt.ask(f"텔레그램 인스턴스 이름 (현재: {curr_name})", default=curr_name)
    if val.lower() == 'q': return
    config.TELEGRAM_INSTANCE_NAME = val

    curr_timeout = getattr(config, 'TELEGRAM_POLLING_TIMEOUT', 10)
    val = Prompt.ask(f"폴링 타임아웃(초) (현재: {curr_timeout})", default=str(curr_timeout))
    if val.lower() == 'q': return
    if val.isdigit(): config.TELEGRAM_POLLING_TIMEOUT = int(val)
    
    console.print("[dim]※ 봇 토큰 및 Chat ID는 보안상 config.py 또는 환경변수에서 설정해주세요.[/dim]")
    
    _save_dynamic_config()

def modify_log_settings():
    console.print("\n[bold]7. 로그 레벨 설정 (Log Level)[/bold]")
    
    curr_screen = getattr(config, 'SCREEN_DEBUG_LEVEL', "OFF")
    val = Prompt.ask(f"화면 로그 레벨 (OFF/TRACE/DEBUG) (현재: {curr_screen})", choices=["OFF", "TRACE", "DEBUG"], default=curr_screen)
    if val.lower() == 'q': return
    config.SCREEN_DEBUG_LEVEL = val
    
    curr_file = getattr(config, 'FILE_DEBUG_LEVEL', "INFO")
    val = Prompt.ask(f"파일 로그 레벨 (DEBUG/INFO/WARNING/ERROR) (현재: {curr_file})", choices=["DEBUG", "INFO", "WARNING", "ERROR"], default=curr_file)
    if val.lower() == 'q': return
    config.FILE_DEBUG_LEVEL = val
    
    # 로깅 설정 즉시 적용
    config.setup_logging()
    
    _save_dynamic_config()

def _validate_time_format(val):
    if len(val) == 4 and val.isdigit():
        hh = int(val[:2])
        mm = int(val[2:])
        if 0 <= hh <= 23 and 0 <= mm <= 59:
            return True
    return False

def modify_system_trading_general():
    console.print("\n[bold]2. 시스템 트레이딩 일반설정 (Trading General)[/bold]")
    
    # SYSTEM_TRADING_INTERVAL
    curr = getattr(config, 'SYSTEM_INVEST_PER_STOCK', 0.5)
    val = Prompt.ask(f"종목당 투자 비중 (0.1~1.0) (현재: {curr})", default=str(curr))
    if val.lower() == 'q': return
    try: 
        new_val = float(val)
        if 0 < new_val <= 1.0: config.SYSTEM_INVEST_PER_STOCK = new_val
    except: pass

    # SYSTEM_TRADING_INTERVAL
    curr = getattr(config, 'SYSTEM_TRADING_INTERVAL', 180)
    val = Prompt.ask(f"자동매매 모니터링 주기(초) (현재: {curr})", default=str(curr))
    if val.lower() == 'q': return
    if val.isdigit(): config.SYSTEM_TRADING_INTERVAL = int(val)

    # SYSTEM_DAILY_LOSS_LIMIT
    curr = getattr(config, 'SYSTEM_DAILY_LOSS_LIMIT', 10.0)
    val = Prompt.ask(f"일일 손실 제한율(%) (현재: {curr})", default=str(curr))
    if val.lower() == 'q': return
    try: config.SYSTEM_DAILY_LOSS_LIMIT = float(val)
    except: pass

    # USE_MARKET_FILTER
    curr = getattr(config, 'USE_MARKET_FILTER', True)
    val = Prompt.ask(f"장세 판단 필터 사용 (현재: {curr})", choices=["y", "n"], default="y" if curr else "n")
    if val.lower() == 'q': return
    config.USE_MARKET_FILTER = (val == "y")

    # MARKET_FILTER_MA
    curr = getattr(config, 'MARKET_FILTER_MA', 20)
    val = Prompt.ask(f"시장 필터링 기준 이동평균선(일) (현재: {curr})", default=str(curr))
    if val.lower() == 'q': return
    if val.isdigit(): config.MARKET_FILTER_MA = int(val)

    # SYSTEM_MAX_CONSECUTIVE_ERRORS
    curr = getattr(config, 'SYSTEM_MAX_CONSECUTIVE_ERRORS', 5)
    val = Prompt.ask(f"연속 에러 허용 횟수 (현재: {curr})", default=str(curr))
    if val.lower() == 'q': return
    if val.isdigit(): config.SYSTEM_MAX_CONSECUTIVE_ERRORS = int(val)

    # SYSTEM_TRADING_START_TIME
    curr = getattr(config, 'SYSTEM_TRADING_START_TIME', "0915")
    val = Prompt.ask(f"거래 시작 시간(HHMM) (현재: {curr})", default=curr)
    if val.lower() == 'q': return
    if _validate_time_format(val):
        config.SYSTEM_TRADING_START_TIME = val
    else:
        console.print("[red]잘못된 시간 형식입니다. (0000~2359)[/red]")

    # SYSTEM_TRADING_END_TIME
    curr = getattr(config, 'SYSTEM_TRADING_END_TIME', "1515")
    val = Prompt.ask(f"거래 종료 시간(HHMM) (현재: {curr})", default=curr)
    if val.lower() == 'q': return
    if _validate_time_format(val):
        config.SYSTEM_TRADING_END_TIME = val
    else:
        console.print("[red]잘못된 시간 형식입니다. (0000~2359)[/red]")

    _save_dynamic_config()

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
        "BUY_SCORE": 8, "RISE_SCORE": 6, "BUY_RSI_MAX": 60, "BUY_VOL_STRENGTH": 100.0,
        "DISPARITY_UPPER": 110, "DISPARITY_LOWER": 90
    })
    config.SELL_STRATEGY.update({
        "STOP_LOSS_RATE": -7.0, "TAKE_PROFIT_RATE": 30.0, "TAKE_PROFIT_RSI": 75, "SELL_SCORE": 5,
        "TRAILING_STOP_ACTIVATION_RATE": 10.0, "TRAILING_STOP_CALLBACK_RATE": 3.0
    })
    config.INDICATOR_PARAMS.update({
        "CHART_LOOKBACK_DAYS": 730, "SAR_AF_START": 0.02, "SAR_AF_STEP": 0.02, "SAR_AF_MAX": 0.2,
        "ADX_PERIOD": 14, "CCI_WINDOW": 20, "CCI_UPPER": 100, "CCI_LOWER": -100,
        "MACD_FAST": 12, "MACD_SLOW": 26, "MACD_SIGNAL": 9, "OBV_MA_PERIOD": 5,
        "RSI_PERIOD": 14, "RSI_SIGNAL": 14, "RSI_UPPER": 70, "RSI_MID": 50, "RSI_LOWER": 30
    })
    
    config.SYSTEM_INVEST_PER_STOCK = 0.5
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
    config.FILE_DEBUG_LEVEL = "INFO"
    config.SYSTEM_MAX_CONSECUTIVE_ERRORS = 5
    config.SYSTEM_TRADING_START_TIME = "0915"
    config.SYSTEM_TRADING_END_TIME = "1515"

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