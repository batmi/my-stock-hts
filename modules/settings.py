import json
import os
from rich.prompt import Prompt
from rich.console import Console
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
        "CONCLUSION_CHECK_ACTIVE_DURATION": getattr(config, 'CONCLUSION_CHECK_ACTIVE_DURATION', 100)
    }
    
    try:
        path = os.path.join(config.JSON_DIR, "dynamic_config.json")
        with open(path, 'w', encoding='utf-8') as f:
            json.dump(data, f, indent=4, ensure_ascii=False)
        console.print(f"\n[bold green]설정이 저장되었습니다. (재시작 시에도 유지됨)[/bold green]")
        console.print(f"[dim]저장 경로: {path}[/dim]")
    except Exception as e:
        console.print(f"\n[bold red]설정 저장 실패: {e}[/bold red]")

def modify_analysis_thresholds():
    console.print("\n[bold]1. 매수/분석 임계값 설정 (ANALYSIS_THRESHOLDS)[/bold]")
    
    curr = config.ANALYSIS_THRESHOLDS["BUY_SCORE"]
    val = Prompt.ask(f"매수 기준 점수 (현재: {curr})", default=str(curr))
    if val.isdigit(): config.ANALYSIS_THRESHOLDS["BUY_SCORE"] = int(val)

    curr = config.ANALYSIS_THRESHOLDS["RISE_SCORE"]
    val = Prompt.ask(f"상승 추세 점수 (현재: {curr})", default=str(curr))
    if val.isdigit(): config.ANALYSIS_THRESHOLDS["RISE_SCORE"] = int(val)

    curr = config.ANALYSIS_THRESHOLDS["BUY_RSI_MAX"]
    val = Prompt.ask(f"매수 허용 RSI 상한 (현재: {curr})", default=str(curr))
    try: config.ANALYSIS_THRESHOLDS["BUY_RSI_MAX"] = float(val)
    except: pass

    curr = config.ANALYSIS_THRESHOLDS.get("BUY_VOL_STRENGTH", 100.0)
    val = Prompt.ask(f"매수 체결강도 기준(%) (현재: {curr})", default=str(curr))
    try: config.ANALYSIS_THRESHOLDS["BUY_VOL_STRENGTH"] = float(val)
    except: pass
    
    _save_dynamic_config()

def modify_sell_strategy():
    console.print("\n[bold]2. 매도 전략 설정 (SELL_STRATEGY)[/bold]")
    
    curr = config.SELL_STRATEGY["TAKE_PROFIT_RATE"]
    val = Prompt.ask(f"익절 수익률(%) (현재: {curr})", default=str(curr))
    try: config.SELL_STRATEGY["TAKE_PROFIT_RATE"] = float(val)
    except: pass

    curr = config.SELL_STRATEGY["STOP_LOSS_RATE"]
    val = Prompt.ask(f"손절 수익률(%) (현재: {curr})", default=str(curr))
    try: config.SELL_STRATEGY["STOP_LOSS_RATE"] = float(val)
    except: pass

    curr = config.SELL_STRATEGY["SELL_SCORE"]
    val = Prompt.ask(f"매도(추세이탈) 기준 점수 (현재: {curr})", default=str(curr))
    if val.isdigit(): config.SELL_STRATEGY["SELL_SCORE"] = int(val)

    curr = config.SELL_STRATEGY["TAKE_PROFIT_RSI"]
    val = Prompt.ask(f"과열 매도 RSI 기준 (현재: {curr})", default=str(curr))
    try: config.SELL_STRATEGY["TAKE_PROFIT_RSI"] = float(val)
    except: pass

    curr_act = config.SELL_STRATEGY.get("TRAILING_STOP_ACTIVATION_RATE", 10.0)
    val = Prompt.ask(f"트레일링 스탑 발동 수익률(%) (현재: {curr_act})", default=str(curr_act))
    try: config.SELL_STRATEGY["TRAILING_STOP_ACTIVATION_RATE"] = float(val)
    except: pass

    curr_cb = config.SELL_STRATEGY.get("TRAILING_STOP_CALLBACK_RATE", 3.0)
    val = Prompt.ask(f"트레일링 스탑 하락 감지율(%) (현재: {curr_cb})", default=str(curr_cb))
    try: config.SELL_STRATEGY["TRAILING_STOP_CALLBACK_RATE"] = float(val)
    except: pass

    _save_dynamic_config()

def modify_indicator_params():
    console.print("\n[bold]3. 기술적 지표 설정 (INDICATOR_PARAMS)[/bold]")
    
    curr = config.INDICATOR_PARAMS["CHART_LOOKBACK_DAYS"]
    val = Prompt.ask(f"데이터 조회 기간(일) (현재: {curr})", default=str(curr))
    if val.isdigit(): config.INDICATOR_PARAMS["CHART_LOOKBACK_DAYS"] = int(val)

    curr = config.INDICATOR_PARAMS["RSI_PERIOD"]
    val = Prompt.ask(f"RSI 계산 기간 (현재: {curr})", default=str(curr))
    if val.isdigit(): config.INDICATOR_PARAMS["RSI_PERIOD"] = int(val)

    curr_up = config.INDICATOR_PARAMS["RSI_UPPER"]
    val = Prompt.ask(f"RSI 과매수 기준 (현재: {curr_up})", default=str(curr_up))
    if val.isdigit(): config.INDICATOR_PARAMS["RSI_UPPER"] = int(val)

    curr_low = config.INDICATOR_PARAMS["RSI_LOWER"]
    val = Prompt.ask(f"RSI 과매도 기준 (현재: {curr_low})", default=str(curr_low))
    if val.isdigit(): config.INDICATOR_PARAMS["RSI_LOWER"] = int(val)

    curr = config.INDICATOR_PARAMS["CCI_WINDOW"]
    val = Prompt.ask(f"CCI 계산 기간 (현재: {curr})", default=str(curr))
    if val.isdigit(): config.INDICATOR_PARAMS["CCI_WINDOW"] = int(val)

    _save_dynamic_config()

def modify_system_general():
    console.print("\n[bold]4. 시스템 일반 설정[/bold]")
    
    curr = getattr(config, 'SYSTEM_INVEST_PER_STOCK', 0.5)
    val = Prompt.ask(f"종목당 투자 비중 (0.1~1.0) (현재: {curr})", default=str(curr))
    try: 
        new_val = float(val)
        if 0 < new_val <= 1.0: config.SYSTEM_INVEST_PER_STOCK = new_val
    except: pass

    curr = getattr(config, 'SYSTEM_TRADING_INTERVAL', 180)
    val = Prompt.ask(f"자동매매 모니터링 주기(초) (현재: {curr})", default=str(curr))
    if val.isdigit(): config.SYSTEM_TRADING_INTERVAL = int(val)

    curr = getattr(config, 'SYSTEM_DAILY_LOSS_LIMIT', 10.0)
    val = Prompt.ask(f"일일 손실 제한율(%) (현재: {curr})", default=str(curr))
    try: config.SYSTEM_DAILY_LOSS_LIMIT = float(val)
    except: pass

    curr = getattr(config, 'USE_MARKET_FILTER', True)
    val = Prompt.ask(f"시장 지수 필터링 사용 (현재: {curr})", choices=["y", "n"], default="y" if curr else "n")
    config.USE_MARKET_FILTER = (val == "y")

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

    console.print("\n[bold green]모든 설정이 기본값으로 초기화되었습니다.[/bold green]")

def system_config_menu():
    while True:
        console.print("\n[bold cyan]=== 시스템 전체 설정 변경 ===[/]")
        console.print("[1] 매수/분석 임계값 (Analysis Thresholds)")
        console.print("[2] 매도 전략 (Sell Strategy)")
        console.print("[3] 기술적 지표 파라미터 (Indicators)")
        console.print("[4] 시스템 일반 설정 (General)")
        console.print("[5] 설정 초기화 (Reset to Default)")
        console.print("[Q] 뒤로 가기")
        console.print()
        
        choice = Prompt.ask("선택", choices=["1", "2", "3", "4", "5", "q", "Q"], default="q")
        if choice.lower() == 'q': return
        
        if choice == "1": modify_analysis_thresholds()
        elif choice == "2": modify_sell_strategy()
        elif choice == "3": modify_indicator_params()
        elif choice == "4": modify_system_general()
        elif choice == "5": reset_to_default()