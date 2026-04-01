import json
import os
from rich.prompt import Prompt
from rich.console import Console
from rich.table import Table
from rich import box
import config
import utils # [추가]
import context # [추가]

console = config.console

def _save_dynamic_config():
    """현재 메모리 상의 설정을 파일로 저장 (영구 반영)"""
    data = {
        "ANALYSIS_THRESHOLDS": config.ANALYSIS_THRESHOLDS,
        "SELL_STRATEGY": config.SELL_STRATEGY,
        "INDICATOR_PARAMS": config.INDICATOR_PARAMS,
        "SCORING_WEIGHTS": config.SCORING_WEIGHTS,
        "MARKET_REGIME_PARAMS": config.MARKET_REGIME_PARAMS,
        "SYSTEM_INVEST_PER_STOCK": getattr(config, 'SYSTEM_INVEST_PER_STOCK', 0.2),
        "SYSTEM_MAX_HOLDINGS": getattr(config, 'SYSTEM_MAX_HOLDINGS', 10),
        "SYSTEM_TRADING_INTERVAL": getattr(config, 'SYSTEM_TRADING_INTERVAL', 180),
        "SYSTEM_DAILY_LOSS_LIMIT": getattr(config, 'SYSTEM_DAILY_LOSS_LIMIT', 10.0),
        "USE_MARKET_FILTER": getattr(config, 'USE_MARKET_FILTER', True),
        "MARKET_FILTER_MA": getattr(config, 'MARKET_FILTER_MA', 20),
        "CONCLUSION_CHECK_INTERVAL": getattr(config, 'CONCLUSION_CHECK_INTERVAL', 5),
        "CONCLUSION_CHECK_IDLE_INTERVAL": getattr(config, 'CONCLUSION_CHECK_IDLE_INTERVAL', 300),
        "CONCLUSION_CHECK_ACTIVE_DURATION": getattr(config, 'CONCLUSION_CHECK_ACTIVE_DURATION', 60),
        "UNFILLED_ORDER_CANCEL_SECONDS": getattr(config, 'UNFILLED_ORDER_CANCEL_SECONDS', 120),
        "CHART_CACHE_TTL_MINUTES": getattr(config, 'CHART_CACHE_TTL_MINUTES', 180),
        "ENABLE_TELEGRAM": getattr(config, 'ENABLE_TELEGRAM', True),
        "TELEGRAM_INSTANCE_NAME": getattr(config, 'TELEGRAM_INSTANCE_NAME', "HTS"),
        "TELEGRAM_POLLING_TIMEOUT": getattr(config, 'TELEGRAM_POLLING_TIMEOUT', 10),
        "AUTO_MORNING_BRIEFING_USE": getattr(config, 'AUTO_MORNING_BRIEFING_USE', False),
        "AUTO_MORNING_BRIEFING_TIME": getattr(config, 'AUTO_MORNING_BRIEFING_TIME', "0830"),
        "SCREEN_DEBUG_LEVEL": getattr(config, 'SCREEN_DEBUG_LEVEL', "OFF"),
        "CLEAR_SCREEN_ON_MENU": getattr(config, 'CLEAR_SCREEN_ON_MENU', False),
        "FILE_DEBUG_LEVEL": getattr(config, 'FILE_DEBUG_LEVEL', "WARNING"),
        "SYSTEM_MAX_CONSECUTIVE_ERRORS": getattr(config, 'SYSTEM_MAX_CONSECUTIVE_ERRORS', 5),
        "SYSTEM_TRADING_START_TIME": getattr(config, 'SYSTEM_TRADING_START_TIME', "0915"),
        "SYSTEM_TRADING_END_TIME": getattr(config, 'SYSTEM_TRADING_END_TIME', "1515"),
        "SYSTEM_RISK_PER_TRADE": getattr(config, 'SYSTEM_RISK_PER_TRADE', 5.0),
        "USE_VOLATILITY_TARGETING": getattr(config, 'USE_VOLATILITY_TARGETING', True),
        "TARGET_VOLATILITY": getattr(config, 'TARGET_VOLATILITY', 0.20),
        "VOLATILITY_SCALING_MAX": getattr(config, 'VOLATILITY_SCALING_MAX', 2.0),
        "VOLATILITY_SCALING_MIN": getattr(config, 'VOLATILITY_SCALING_MIN', 0.5),
        "SLIPPAGE_RATE": getattr(config, 'SLIPPAGE_RATE', 0.002)
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

    # =========================================================
    # 1. 매수 및 매도 전략 설정
    # =========================================================
    table.add_row("[bold]1. 매수 및 매도 전략 설정[/]", "", "")
    table.add_row("[bold dim]  1-1. 매수/분석 임계값[/]", "", "")
    thresholds = config.ANALYSIS_THRESHOLDS
    table.add_row("매수 기준 점수\n[dim]진입 임계값 (종합 점수)[/dim]", "ANALYSIS_THRESHOLDS['BUY_SCORE']", f"{thresholds.get('BUY_SCORE')}")
    table.add_row("상승 추세 점수\n[dim]관망/상승 판단 기준[/dim]", "ANALYSIS_THRESHOLDS['RISE_SCORE']", f"{thresholds.get('RISE_SCORE')}")
    table.add_row("매수 허용 RSI 상한\n[dim]과열 방지 (이 값보다 낮아야 매수)[/dim]", "ANALYSIS_THRESHOLDS['BUY_RSI_MAX']", f"{thresholds.get('BUY_RSI_MAX')}")
    table.add_row("매수 체결강도 기준\n[dim]수급 확인 (이 값 이상이어야 매수)[/dim]", "ANALYSIS_THRESHOLDS['BUY_VOL_STRENGTH']", f"{thresholds.get('BUY_VOL_STRENGTH')}%")
    table.add_row("역추세 매수 사용\n[dim]낙폭과대 반등 노리기[/dim]", "ANALYSIS_THRESHOLDS['USE_MEAN_REVERSION']", f"{thresholds.get('USE_MEAN_REVERSION', True)}")
    if thresholds.get('USE_MEAN_REVERSION', True):
        table.add_row("  └ 역추세 RSI\n    [dim]과매도/침체 기준[/dim]", "MR_RSI_MAX", f"{thresholds.get('MR_RSI_MAX', 40.0)}")
        table.add_row("  └ 역추세 이격도\n    [dim]20일선 기준 하락폭 한계[/dim]", "MR_DISPARITY_MAX", f"{thresholds.get('MR_DISPARITY_MAX', 90.0)}%")
        table.add_row("  └ 역추세 체결강도\n    [dim]바닥 매수세 확증 기준[/dim]", "MR_VOL_STRENGTH", f"{thresholds.get('MR_VOL_STRENGTH', 120.0)}%")
        sell = config.SELL_STRATEGY
        table.add_row("  └ 역매수 유예 손실\n    [dim]역매수 종목 유예 기간 내 허용 하락폭[/dim]", "MR_GRACE_LOSS_RATE", f"{sell.get('MR_GRACE_LOSS_RATE', -5.0)}%")
        
    table.add_row("슈퍼 모멘텀 (RSI 유연화)\n[dim]주도주 랠리 시 RSI 허용치 완화[/dim]", "SUPER_MOMENTUM_USE", f"{thresholds.get('SUPER_MOMENTUM_USE', True)}")
    if thresholds.get('SUPER_MOMENTUM_USE', True):
        table.add_row("  └ 슈퍼 매수 발동 점수\n    [dim]기준 점수 이상 & 신고가 90% 이상 시 발동[/dim]", "SUPER_MOMENTUM_SCORE", f"{thresholds.get('SUPER_MOMENTUM_SCORE', 8.5)}")
        table.add_row("  └ 슈퍼 52주 위치 기준\n    [dim]신고가 근접 여부 (예: 90.0% 이상)[/dim]", "SUPER_MOMENTUM_W52_POS", f"{thresholds.get('SUPER_MOMENTUM_W52_POS', 90.0)}%")
        table.add_row("  └ 완화된 매수 RSI 상한\n    [dim]발동 시 적용되는 진입 최대 RSI[/dim]", "SUPER_BUY_RSI_MAX", f"{thresholds.get('SUPER_BUY_RSI_MAX', 75.0)}")
        sell = config.SELL_STRATEGY
        table.add_row("  └ 슈퍼 매도 과열 RSI\n    [dim]추세 유지 시 매도 지연 RSI 기준[/dim]", "SUPER_TAKE_PROFIT_RSI", f"{sell.get('SUPER_TAKE_PROFIT_RSI', 85.0)}")
    
    table.add_section()

    table.add_row("[bold dim]  1-2. 매도/청산 전략[/]", "", "")
    sell = config.SELL_STRATEGY
    table.add_row("익절 수익률\n[dim]목표 수익 달성 시 매도[/dim]", "SELL_STRATEGY['TAKE_PROFIT_RATE']", f"{sell.get('TAKE_PROFIT_RATE')}%")
    table.add_row("반익절 사용\n[dim]익절 수익률의 절반 도달 시 50% 선매도[/dim]", "SELL_STRATEGY['HALF_TAKE_PROFIT_USE']", f"{sell.get('HALF_TAKE_PROFIT_USE', True)}")
    table.add_row("시간 청산 사용\n[dim]장기 횡보 종목 강제 매도[/dim]", "SELL_STRATEGY['TIME_STOP_USE']", f"{sell.get('TIME_STOP_USE', True)}")
    table.add_row("  └ 청산 기준일\n    [dim]매수 후 경과 일수 (달력 기준)[/dim]", "TIME_STOP_DAYS", f"{sell.get('TIME_STOP_DAYS', 5)}일")
    table.add_row("  └ 최소 기대 수익\n    [dim]해당 기간 내 도달해야 할 수익률[/dim]", "TIME_STOP_MIN_PROFIT_RATE", f"{sell.get('TIME_STOP_MIN_PROFIT_RATE', 3.0)}%")
    table.add_row("손절 수익률\n[dim]손실 제한 (Stop Loss)[/dim]", "SELL_STRATEGY['STOP_LOSS_RATE']", f"{sell.get('STOP_LOSS_RATE')}%")
    table.add_row("매도(추세이탈) 점수\n[dim]점수 하락 시 매도[/dim]", "SELL_STRATEGY['SELL_SCORE']", f"{sell.get('SELL_SCORE')}")
    table.add_row("과열 매도 RSI\n[dim]RSI 과열 시 선제 매도[/dim]", "SELL_STRATEGY['TAKE_PROFIT_RSI']", f"{sell.get('TAKE_PROFIT_RSI')}")
    table.add_row("TS 발동 수익률\n[dim]트레일링 스탑 감시 시작점[/dim]", "SELL_STRATEGY['TRAILING_STOP_ACTIVATION_RATE']", f"{sell.get('TRAILING_STOP_ACTIVATION_RATE')}%")
    table.add_row("TS 하락 감지율\n[dim]최고가 대비 하락 시 매도[/dim]", "SELL_STRATEGY['TRAILING_STOP_CALLBACK_RATE']", f"{sell.get('TRAILING_STOP_CALLBACK_RATE')}%")
    table.add_row("ATR 손절 사용\n[dim]변동성 기반 동적 손절[/dim]", "SELL_STRATEGY['USE_ATR_STOP']", f"{sell.get('USE_ATR_STOP', False)}")
    table.add_row("  └ ATR 손절 배수\n    [dim]ATR * 배수 만큼 손절폭 설정[/dim]", "SELL_STRATEGY['ATR_STOP_MULTIPLIER']", f"{sell.get('ATR_STOP_MULTIPLIER', 2.0)}")
    table.add_row("  └ ATR 손절 최대 한도\n    [dim]비정상적인 과도한 손절폭 제한[/dim]", "SELL_STRATEGY['MAX_ATR_STOP_LOSS_RATE']", f"{sell.get('MAX_ATR_STOP_LOSS_RATE', -15.0)}%")

    table.add_section()

    # =========================================================
    # 2. 스코어링 및 시장 국면 설정
    # =========================================================
    table.add_row("[bold]2. 스코어링 및 시장 국면 설정[/]", "", "")
    weights = config.SCORING_WEIGHTS
    total_score = sum(weights.values())
    table.add_row(f"[bold dim]  2-1. 스코어링 가중치 (총점: {total_score:.1f})[/]", "", "")
    table.add_row("추세 팩터\n[dim]이평선, MACD, SAR[/dim]", "SCORING_WEIGHTS['TREND']", f"{weights.get('TREND')}")
    table.add_row("모멘텀 팩터\n[dim]RSI, CCI[/dim]", "SCORING_WEIGHTS['MOMENTUM']", f"{weights.get('MOMENTUM')}")
    table.add_row("강도/수급 팩터\n[dim]ADX, OBV[/dim]", "SCORING_WEIGHTS['STRENGTH']", f"{weights.get('STRENGTH')}")
    table.add_row("시너지 가산점\n[dim]지표 동조화 보너스[/dim]", "SCORING_WEIGHTS['SYNERGY']", f"{weights.get('SYNERGY')}")

    table.add_section()
    table.add_row("[bold dim]  2-2. 적응형 임계값 (시장국면)[/]", "", "")
    regime = config.MARKET_REGIME_PARAMS
    table.add_row("사용 여부\n[dim]시장 국면 반영[/dim]", "MARKET_REGIME_PARAMS['USE_ADAPTIVE_THRESHOLD']", f"{regime.get('USE_ADAPTIVE_THRESHOLD')}")
    table.add_row("강세장 보정\n[dim]기준 점수 완화값[/dim]", "MARKET_REGIME_PARAMS['BULL_SCORE_ADJ']", f"{regime.get('BULL_SCORE_ADJ')}")
    table.add_row("약세장 보정\n[dim]기준 점수 강화값[/dim]", "MARKET_REGIME_PARAMS['BEAR_SCORE_ADJ']", f"{regime.get('BEAR_SCORE_ADJ')}")
    table.add_row("횡보장 보정\n[dim]기준 점수 유지값[/dim]", "MARKET_REGIME_PARAMS['SIDEWAYS_SCORE_ADJ']", f"{regime.get('SIDEWAYS_SCORE_ADJ')}")
    table.add_row("추세 판단 EMA (일)\n[dim]시장 국면 판단용 지수이동평균선[/dim]", "MARKET_REGIME_PARAMS['REGIME_MA_PERIOD']", f"{regime.get('REGIME_MA_PERIOD', 20)}")
    table.add_row("추세 판단 ADX\n[dim]강세장 판단용 ADX 기준[/dim]", "MARKET_REGIME_PARAMS['REGIME_ADX_THRESHOLD']", f"{regime.get('REGIME_ADX_THRESHOLD', 20)}")

    table.add_section()

    # =========================================================
    # 3. 리스크 및 자산 배분 설정
    # =========================================================
    table.add_row("[bold]3. 리스크 및 자산 배분 설정[/]", "", "")
    table.add_row("종목당 투자 비중\n[dim]전체 자산 대비 한 종목 투자 비율[/dim]", "SYSTEM_INVEST_PER_STOCK", f"{getattr(config, 'SYSTEM_INVEST_PER_STOCK', 0.2)}")
    table.add_row("최대 보유 종목 수\n[dim]포트폴리오 최대 종목 개수[/dim]", "SYSTEM_MAX_HOLDINGS", f"{getattr(config, 'SYSTEM_MAX_HOLDINGS', 10)}")
    
    slippage = getattr(config, 'SLIPPAGE_RATE', 0.002)
    slippage_str = f"{slippage} (미사용)" if slippage == 0 else f"{slippage}"
    table.add_row("슬리피지 비율\n[dim]주문가 보정 및 백테스트 비용[/dim]", "SLIPPAGE_RATE", slippage_str)
    
    table.add_row("변동성 타겟팅\n[dim]ATR 기반 비중 조절 사용 여부[/dim]", "USE_VOLATILITY_TARGETING", f"{getattr(config, 'USE_VOLATILITY_TARGETING', True)}")
    if getattr(config, 'USE_VOLATILITY_TARGETING', True):
        table.add_row("  └ 목표 변동성\n    [dim]연간 변동성 목표치[/dim]", "TARGET_VOLATILITY", f"{getattr(config, 'TARGET_VOLATILITY', 0.20)}")
        table.add_row("  └ 스케일링 범위\n    [dim]비중 조절 최소~최대 배수[/dim]", "VOLATILITY_SCALING_MIN/MAX", f"{getattr(config, 'VOLATILITY_SCALING_MIN', 0.5)} ~ {getattr(config, 'VOLATILITY_SCALING_MAX', 2.0)}")
        
    table.add_row("시장 필터링 사용\n[dim]지수 하락 시 신규 매수 보류[/dim]", "USE_MARKET_FILTER", f"{getattr(config, 'USE_MARKET_FILTER', True)}")
    table.add_row("  └ 시장 필터링 SMA (일)\n[dim]지수 추세 판단용 단순이동평균선[/dim]", "MARKET_FILTER_MA", f"{getattr(config, 'MARKET_FILTER_MA', 50)}")
    table.add_row("연속 에러 허용\n[dim]시스템 중단 임계값[/dim]", "SYSTEM_MAX_CONSECUTIVE_ERRORS", f"{getattr(config, 'SYSTEM_MAX_CONSECUTIVE_ERRORS', 5)}")
    table.add_row("일일 손실 제한 (%)\n[dim]자산 보호를 위한 비상 정지 기준[/dim]", "SYSTEM_DAILY_LOSS_LIMIT", f"{getattr(config, 'SYSTEM_DAILY_LOSS_LIMIT', 10.0)}")
    table.add_row("1회 최대 리스크 (%)\n[dim]계좌 대비 1회 매매 최대 손실폭[/dim]", "SYSTEM_RISK_PER_TRADE", f"{getattr(config, 'SYSTEM_RISK_PER_TRADE', 5.0)}")

    table.add_section()

    # =========================================================
    # 4. 기술적 지표 파라미터
    # =========================================================
    ind = config.INDICATOR_PARAMS
    table.add_row("[bold]4. 기술적 지표 파라미터[/]", "", "")
    table.add_row("데이터 조회 기간\n[dim]일봉 데이터 조회 범위[/dim]", "INDICATOR_PARAMS['CHART_LOOKBACK_DAYS']", f"{ind.get('CHART_LOOKBACK_DAYS')}일")
    table.add_row("SAR (Start/Step/Max)\n[dim]파라볼릭 SAR 가속변수[/dim]", "INDICATOR_PARAMS['SAR_AF_START', 'SAR_AF_STEP', 'SAR_AF_MAX']", f"{ind.get('SAR_AF_START')}/{ind.get('SAR_AF_STEP')}/{ind.get('SAR_AF_MAX')}")
    table.add_row("RSI (Period/Signal)\n[dim]상대강도지수 기간[/dim]", "INDICATOR_PARAMS['RSI_PERIOD', 'RSI_SIGNAL']", f"{ind.get('RSI_PERIOD')}/{ind.get('RSI_SIGNAL')}")
    table.add_row("RSI (Up/Mid/Low)\n[dim]과매수/중심/과매도 기준[/dim]", "INDICATOR_PARAMS['RSI_UPPER', 'RSI_MID', 'RSI_LOWER']", f"{ind.get('RSI_UPPER')}/{ind.get('RSI_MID')}/{ind.get('RSI_LOWER')}")
    table.add_row("ADX 기간\n[dim]추세 강도 지표[/dim]", "INDICATOR_PARAMS['ADX_PERIOD']", f"{ind.get('ADX_PERIOD')}")
    table.add_row("CCI (Window/Up/Low)\n[dim]상품채널지수[/dim]", "INDICATOR_PARAMS['CCI_WINDOW', 'CCI_UPPER', 'CCI_LOWER']", f"{ind.get('CCI_WINDOW')}/{ind.get('CCI_UPPER')}/{ind.get('CCI_LOWER')}")
    table.add_row("MACD (Fast/Slow/Sig)\n[dim]이동평균수렴확산[/dim]", "INDICATOR_PARAMS['MACD_FAST', 'MACD_SLOW', 'MACD_SIGNAL']", f"{ind.get('MACD_FAST')}/{ind.get('MACD_SLOW')}/{ind.get('MACD_SIGNAL')}")
    table.add_row("OBV EMA 기간\n[dim]거래량 추세 지수이동평균[/dim]", "INDICATOR_PARAMS['OBV_MA_PERIOD']", f"{ind.get('OBV_MA_PERIOD')}")
    table.add_row("ATR 기간\n[dim]평균 진폭 (변동성)[/dim]", "INDICATOR_PARAMS['ATR_PERIOD']", f"{ind.get('ATR_PERIOD')}")

    table.add_section()

    # =========================================================
    # 5. 환경 및 시스템 설정
    # =========================================================
    table.add_row("[bold]5. 환경 및 시스템 설정[/]", "", "")
    table.add_row("[bold dim]  5-1. 트레이딩 시간 및 주기[/]", "", "")
    table.add_row("거래 시작 시간\n[dim]매매 허용 시작 시각 (HHMM)[/dim]", "SYSTEM_TRADING_START_TIME", f"{getattr(config, 'SYSTEM_TRADING_START_TIME', '0920')}")
    table.add_row("거래 종료 시간\n[dim]매매 허용 종료 시각 (HHMM)[/dim]", "SYSTEM_TRADING_END_TIME", f"{getattr(config, 'SYSTEM_TRADING_END_TIME', '1510')}")
    table.add_row("모니터링 주기 (초)\n[dim]자동매매 루프 실행 간격[/dim]", "SYSTEM_TRADING_INTERVAL", f"{getattr(config, 'SYSTEM_TRADING_INTERVAL', 180)}")
    table.add_row("체결 감시 주기\n[dim]주문 직후 체결 확인 간격[/dim]", "CONCLUSION_CHECK_INTERVAL", f"{getattr(config, 'CONCLUSION_CHECK_INTERVAL', 5)}")
    table.add_row("대기 모드 주기\n[dim]주문이 없는 평상시 체결 확인 간격[/dim]", "CONCLUSION_CHECK_IDLE_INTERVAL", f"{getattr(config, 'CONCLUSION_CHECK_IDLE_INTERVAL', 300)}")
    table.add_row("집중 감시 시간\n[dim]주문 후 집중 감시 유지 시간[/dim]", "CONCLUSION_CHECK_ACTIVE_DURATION", f"{getattr(config, 'CONCLUSION_CHECK_ACTIVE_DURATION', 60)}")
    table.add_row("미체결 취소 대기\n[dim]지정가 주문 유지 시간[/dim]", "UNFILLED_ORDER_CANCEL_SECONDS", f"{getattr(config, 'UNFILLED_ORDER_CANCEL_SECONDS', 120)}")
    table.add_row("차트 캐시 시간(분)\n[dim]일봉 데이터 메모리 캐시 유지[/dim]", "CHART_CACHE_TTL_MINUTES", f"{getattr(config, 'CHART_CACHE_TTL_MINUTES', 180)}")
    
    table.add_section()
    table.add_row("[bold dim]  5-2. 텔레그램 및 AI 브리핑[/]", "", "")
    table.add_row("사용 여부\n[dim]알림 기능 활성화 여부[/dim]", "ENABLE_TELEGRAM", f"{getattr(config, 'ENABLE_TELEGRAM', True)}")
    table.add_row("인스턴스 이름\n[dim]알림 메시지 머리말[/dim]", "TELEGRAM_INSTANCE_NAME", f"{getattr(config, 'TELEGRAM_INSTANCE_NAME', 'HTS')}")
    table.add_row("폴링 타임아웃\n[dim]봇 명령어 수신 대기 시간[/dim]", "TELEGRAM_POLLING_TIMEOUT", f"{getattr(config, 'TELEGRAM_POLLING_TIMEOUT', 10)}")
    table.add_row("장전 AI 브리핑\n[dim]매일 글로벌 매크로 시황 전송[/dim]", "AUTO_MORNING_BRIEFING_USE", f"{getattr(config, 'AUTO_MORNING_BRIEFING_USE', False)}")
    table.add_row("장전 AI 브리핑 시간\n[dim]발송 시각 (HHMM)[/dim]", "AUTO_MORNING_BRIEFING_TIME", f"{getattr(config, 'AUTO_MORNING_BRIEFING_TIME', '0830')}")

    table.add_section()
    table.add_row("[bold dim]  5-3. 화면 및 로그 설정[/]", "", "")
    table.add_row("화면 자동 지우기\n[dim]메뉴 이동 시 터미널 클리어[/dim]", "CLEAR_SCREEN_ON_MENU", f"{getattr(config, 'CLEAR_SCREEN_ON_MENU', False)}")
    table.add_row("화면 로그 레벨\n[dim]터미널 디버그 출력 레벨[/dim]", "SCREEN_DEBUG_LEVEL", f"{getattr(config, 'SCREEN_DEBUG_LEVEL', 'OFF')}")
    table.add_row("파일 로그 레벨\n[dim]로그 파일 저장 레벨[/dim]", "FILE_DEBUG_LEVEL", f"{getattr(config, 'FILE_DEBUG_LEVEL', 'WARNING')}")

    console.print(table)
    console.print()
    return True

def _edit_config_table(title_source, items_source):
    """설정 변경을 위한 공통 테이블 UI 함수"""
    action_taken = False
    while True:
        utils.print_breadcrumb()
        # items_source가 함수면 호출하여 최신 리스트 가져오기 (동적 메뉴 지원)
        items = items_source() if callable(items_source) else items_source
        
        # [수정] title_source가 함수면 호출하여 동적 타이틀 지원 (총점 갱신용)
        title = title_source() if callable(title_source) else title_source
        
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
        
        console.print()
        choice = Prompt.ask("\n수정할 항목 번호 선택 [dim](전체: a, 이전: b, 메인: q)[/dim]", choices=[str(i+1) for i in range(len(items))] + ['a', 'b', 'q', 'A', 'B', 'Q'], default='b')
        console.print()
        
        if choice.lower() in ['b', 'q']:
            break
            
        targets = []
        if choice.lower() == 'a':
            targets = items
        else:
            targets = [items[int(choice)-1]]
            
        changed_in_this_loop = False
        for item in targets:
            curr_val = item['get']()
            console.print(f"\n[bold cyan]>> {item['desc']} 수정[/bold cyan]")
            console.print(f"[dim]설명: {item['help']}[/dim]")
            
            if item['type'] == 'bool':
                # [수정] True/False 입력 지원을 위해 Confirm 대신 Prompt 사용
                prompt_msg = f"현재 설정을 변경하시겠습니까? \\[y/N] [dim](현재: {curr_val})[/dim]"
                
                canceled = False
                while True:
                    console.print()
                    val = Prompt.ask(prompt_msg, default="n")
                    console.print()
                    val_lower = val.lower()
                    
                    if val_lower in ['b', 'q']:
                        canceled = True
                        break
                    
                    target_val = curr_val
                    
                    if val_lower in ['y', 'yes', '1']:
                        target_val = not curr_val
                    elif val_lower in ['n', 'no', '0']:
                        target_val = curr_val
                    elif val_lower == 'true':
                        target_val = True
                    elif val_lower == 'false':
                        target_val = False
                    else:
                        console.print("[red]잘못된 입력입니다. Y/N 또는 True/False를 입력해주세요. (이전: b, 메인: q)[/red]")
                        continue

                    if target_val != curr_val:
                        item['set'](target_val)
                        if 'callback' in item:
                            item['callback']()
                        console.print(f"[cyan]>> 설정이 변경되었습니다: {target_val}[/cyan]")
                        changed_in_this_loop = True
                    break
                
                if canceled:
                    console.print("[yellow]입력이 취소되었습니다.[/yellow]")
                    break
                continue

            prompt_kwargs = {"default": str(curr_val)}
            if 'choices' in item:
                choices = [c for c in list(item['choices']) if c.lower() != 'q']
                for c in ['b', 'B', 'q', 'Q']:
                    if c not in choices: choices.append(c)
                prompt_kwargs["choices"] = choices
                
            console.print()
            val = Prompt.ask(f"새로운 값 입력 [dim](이전: b, 메인: q)[/dim]", **prompt_kwargs)
            console.print()
            
            if val.lower() in ['b', 'q']:
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
                    
                changed_in_this_loop = True
                    
            except Exception as e:
                console.print(f"[red]잘못된 입력입니다: {e}[/red]")
        
        if changed_in_this_loop:
            _save_dynamic_config()
            action_taken = True
            
    return action_taken

def modify_analysis_thresholds():
    items = [
        {"desc": "매수 기준 점수", "help": "진입 임계값 (종합 점수)", "name": "BUY_SCORE", "type": "float",
         "get": lambda: config.ANALYSIS_THRESHOLDS["BUY_SCORE"], "set": lambda v: config.ANALYSIS_THRESHOLDS.update({"BUY_SCORE": v})},
        {"desc": "상승 추세 점수", "help": "관망/상승 판단 기준", "name": "RISE_SCORE", "type": "float",
         "get": lambda: config.ANALYSIS_THRESHOLDS["RISE_SCORE"], "set": lambda v: config.ANALYSIS_THRESHOLDS.update({"RISE_SCORE": v})},
        {"desc": "매수 허용 RSI 상한", "help": "과열 방지 (이 값보다 낮아야 매수)", "name": "BUY_RSI_MAX", "type": "float",
         "get": lambda: config.ANALYSIS_THRESHOLDS["BUY_RSI_MAX"], "set": lambda v: config.ANALYSIS_THRESHOLDS.update({"BUY_RSI_MAX": v})},
        {"desc": "매수 체결강도 기준", "help": "수급 확인 (이 값 이상이어야 매수)", "name": "BUY_VOL_STRENGTH", "type": "float",
         "get": lambda: config.ANALYSIS_THRESHOLDS.get("BUY_VOL_STRENGTH", 100.0), "set": lambda v: config.ANALYSIS_THRESHOLDS.update({"BUY_VOL_STRENGTH": v})},
        {"desc": "역추세(낙폭과대) 사용", "help": "하락장/급락 시 반등 매수", "name": "USE_MEAN_REVERSION", "type": "bool", "choices": ["y", "n"],
         "get": lambda: config.ANALYSIS_THRESHOLDS.get("USE_MEAN_REVERSION", True), "set": lambda v: config.ANALYSIS_THRESHOLDS.update({"USE_MEAN_REVERSION": v})},
        {"desc": "역추세 RSI 상한", "help": "과매도 진입 기준 (예: 40)", "name": "MR_RSI_MAX", "type": "float",
         "get": lambda: config.ANALYSIS_THRESHOLDS.get("MR_RSI_MAX", 40.0), "set": lambda v: config.ANALYSIS_THRESHOLDS.update({"MR_RSI_MAX": v})},
        {"desc": "역추세 이격도 상한", "help": "20일선 대비 이격도 (예: 90%)", "name": "MR_DISPARITY_MAX", "type": "float",
         "get": lambda: config.ANALYSIS_THRESHOLDS.get("MR_DISPARITY_MAX", 90.0), "set": lambda v: config.ANALYSIS_THRESHOLDS.update({"MR_DISPARITY_MAX": v})},
        {"desc": "역추세 최소 체결강도", "help": "바닥 매수세 확인 (예: 120%)", "name": "MR_VOL_STRENGTH", "type": "float",
         "get": lambda: config.ANALYSIS_THRESHOLDS.get("MR_VOL_STRENGTH", 120.0), "set": lambda v: config.ANALYSIS_THRESHOLDS.update({"MR_VOL_STRENGTH": v})},
        {"desc": "역매수 유예 손실(%)", "help": "역매수 진입 시 시간청산 기간 내 허용 하락폭", "name": "MR_GRACE_LOSS_RATE", "type": "float",
         "get": lambda: config.SELL_STRATEGY.get("MR_GRACE_LOSS_RATE", -5.0), "set": lambda v: config.SELL_STRATEGY.update({"MR_GRACE_LOSS_RATE": v})},
        {"desc": "슈퍼 모멘텀(RSI 유연화) 사용", "help": "주도주 랠리 시 RSI 허용치 상향", "name": "SUPER_MOMENTUM_USE", "type": "bool", "choices": ["y", "n"],
         "get": lambda: config.ANALYSIS_THRESHOLDS.get("SUPER_MOMENTUM_USE", True), "set": lambda v: config.ANALYSIS_THRESHOLDS.update({"SUPER_MOMENTUM_USE": v})},
        {"desc": "슈퍼 모멘텀 발동 점수", "help": "기준 점수 이상 & 신고가 90% 근접 시 발동", "name": "SUPER_MOMENTUM_SCORE", "type": "float",
         "get": lambda: config.ANALYSIS_THRESHOLDS.get("SUPER_MOMENTUM_SCORE", 8.5), "set": lambda v: config.ANALYSIS_THRESHOLDS.update({"SUPER_MOMENTUM_SCORE": v})},
        {"desc": "슈퍼 52주 위치 기준", "help": "신고가 근접 여부 (예: 90.0% 이상)", "name": "SUPER_MOMENTUM_W52_POS", "type": "float",
         "get": lambda: config.ANALYSIS_THRESHOLDS.get("SUPER_MOMENTUM_W52_POS", 90.0), "set": lambda v: config.ANALYSIS_THRESHOLDS.update({"SUPER_MOMENTUM_W52_POS": v})},
        {"desc": "슈퍼 모멘텀 매수 RSI", "help": "발동 시 완화되는 진입 허용 RSI (예: 75.0)", "name": "SUPER_BUY_RSI_MAX", "type": "float",
         "get": lambda: config.ANALYSIS_THRESHOLDS.get("SUPER_BUY_RSI_MAX", 75.0), "set": lambda v: config.ANALYSIS_THRESHOLDS.update({"SUPER_BUY_RSI_MAX": v})}
    ]
    return _edit_config_table("매수/분석 임계값 설정 (ANALYSIS_THRESHOLDS)", items)

def modify_sell_strategy():
    items = [
        {"desc": "익절 수익률(%)", "help": "목표 수익 달성 시 매도 (0: 미사용)", "name": "TAKE_PROFIT_RATE", "type": "float",
         "get": lambda: config.SELL_STRATEGY["TAKE_PROFIT_RATE"], "set": lambda v: config.SELL_STRATEGY.update({"TAKE_PROFIT_RATE": v})},
        {"desc": "반익절 사용", "help": "익절 수익률의 절반 도달 시 50% 선매도", "name": "HALF_TAKE_PROFIT_USE", "type": "bool", "choices": ["y", "n"],
         "get": lambda: config.SELL_STRATEGY.get("HALF_TAKE_PROFIT_USE", True), "set": lambda v: config.SELL_STRATEGY.update({"HALF_TAKE_PROFIT_USE": v})},
        {"desc": "시간 청산 사용", "help": "장기 횡보 시 강제 매도", "name": "TIME_STOP_USE", "type": "bool", "choices": ["y", "n"],
         "get": lambda: config.SELL_STRATEGY.get("TIME_STOP_USE", True), "set": lambda v: config.SELL_STRATEGY.update({"TIME_STOP_USE": v})},
        {"desc": "시간 청산 기준일", "help": "매수 후 제한 일수 (예: 10)", "name": "TIME_STOP_DAYS", "type": "int",
         "get": lambda: config.SELL_STRATEGY.get("TIME_STOP_DAYS", 10), "set": lambda v: config.SELL_STRATEGY.update({"TIME_STOP_DAYS": v})},
        {"desc": "시간청산 최소수익(%)", "help": "기간 내 달성해야 할 목표치", "name": "TIME_STOP_MIN_PROFIT_RATE", "type": "float",
         "get": lambda: config.SELL_STRATEGY.get("TIME_STOP_MIN_PROFIT_RATE", 3.0), "set": lambda v: config.SELL_STRATEGY.update({"TIME_STOP_MIN_PROFIT_RATE": v})},
        {"desc": "손절 수익률(%)", "help": "손실 제한 (Stop Loss) (0: 미사용)", "name": "STOP_LOSS_RATE", "type": "float",
         "get": lambda: config.SELL_STRATEGY["STOP_LOSS_RATE"], "set": lambda v: config.SELL_STRATEGY.update({"STOP_LOSS_RATE": v})},
        {"desc": "매도(추세이탈) 점수", "help": "점수 하락 시 매도", "name": "SELL_SCORE", "type": "float",
         "get": lambda: config.SELL_STRATEGY["SELL_SCORE"], "set": lambda v: config.SELL_STRATEGY.update({"SELL_SCORE": v})},
        {"desc": "과열 매도 RSI", "help": "RSI 과열 시 선제 매도", "name": "TAKE_PROFIT_RSI", "type": "float",
         "get": lambda: config.SELL_STRATEGY["TAKE_PROFIT_RSI"], "set": lambda v: config.SELL_STRATEGY.update({"TAKE_PROFIT_RSI": v})},
        {"desc": "슈퍼 모멘텀 과열 매도 RSI", "help": "추세 유지 시 매도 지연 RSI (예: 85.0)", "name": "SUPER_TAKE_PROFIT_RSI", "type": "float",
         "get": lambda: config.SELL_STRATEGY.get("SUPER_TAKE_PROFIT_RSI", 85.0), "set": lambda v: config.SELL_STRATEGY.update({"SUPER_TAKE_PROFIT_RSI": v})},
        {"desc": "TS 발동 수익률(%)", "help": "트레일링 스탑 감시 시작점", "name": "TS_ACTIVATION", "type": "float",
         "get": lambda: config.SELL_STRATEGY.get("TRAILING_STOP_ACTIVATION_RATE", 10.0), "set": lambda v: config.SELL_STRATEGY.update({"TRAILING_STOP_ACTIVATION_RATE": v})},
        {"desc": "TS 하락 감지율(%)", "help": "최고가 대비 하락 시 매도", "name": "TS_CALLBACK", "type": "float",
         "get": lambda: config.SELL_STRATEGY.get("TRAILING_STOP_CALLBACK_RATE", 3.0), "set": lambda v: config.SELL_STRATEGY.update({"TRAILING_STOP_CALLBACK_RATE": v})},
        {"desc": "ATR 손절 사용", "help": "변동성 기반 동적 손절", "name": "USE_ATR_STOP", "type": "bool", "choices": ["y", "n"],
         "get": lambda: config.SELL_STRATEGY.get("USE_ATR_STOP", False), "set": lambda v: config.SELL_STRATEGY.update({"USE_ATR_STOP": v})},
        {"desc": "ATR 손절 배수", "help": "ATR * 배수 만큼 손절폭 설정 (0: 미사용)", "name": "ATR_STOP_MULTIPLIER", "type": "float",
         "get": lambda: config.SELL_STRATEGY.get("ATR_STOP_MULTIPLIER", 2.0), "set": lambda v: config.SELL_STRATEGY.update({"ATR_STOP_MULTIPLIER": v})},
        {"desc": "ATR 최대 손절률(%)", "help": "데이터 오류 및 과열 변동성으로 인한 과도한 리스크 제한 (0: 미사용)", "name": "MAX_ATR_STOP_LOSS_RATE", "type": "float",
         "get": lambda: config.SELL_STRATEGY.get("MAX_ATR_STOP_LOSS_RATE", -15.0), "set": lambda v: config.SELL_STRATEGY.update({"MAX_ATR_STOP_LOSS_RATE": v})}
    ]
    return _edit_config_table("매도 전략 설정 (SELL_STRATEGY)", items)

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
        
        {"desc": "OBV EMA 기간", "help": "거래량 추세 판단 (지수이동평균)", "name": "OBV_MA_PERIOD", "type": "int", "section": "OBV",
         "get": lambda: config.INDICATOR_PARAMS["OBV_MA_PERIOD"], "set": lambda v: config.INDICATOR_PARAMS.update({"OBV_MA_PERIOD": v})},
        
        {"desc": "ATR 계산 기간", "help": "평균 진폭 (변동성)", "name": "ATR_PERIOD", "type": "int", "section": "ATR",
         "get": lambda: config.INDICATOR_PARAMS.get("ATR_PERIOD", 14), "set": lambda v: config.INDICATOR_PARAMS.update({"ATR_PERIOD": v})}
    ]
    return _edit_config_table("기술적 지표 파라미터 (Indicators)", items)

def _on_telegram_enable_changed():
    """텔레그램 알림 사용 여부 변경 시 봇 스레드 제어"""
    from modules.telegram_bot import TelegramCommander
    bot = TelegramCommander()
    if getattr(config, 'ENABLE_TELEGRAM', True):
        if not bot.is_running:
            bot.start()
            config.console.print("\n[green]텔레그램 봇 수신 스레드가 시작되었습니다.[/green]")
    else:
        if bot.is_running:
            bot.stop()
            config.console.print("\n[yellow]텔레그램 봇 수신 스레드가 중지되었습니다.[/yellow]")

def modify_telegram_settings():
    items = [
        {"desc": "텔레그램 알림 사용", "help": "알림 기능 활성화 여부", "name": "ENABLE_TELEGRAM", "type": "bool", "choices": ["y", "n"],
         "get": lambda: getattr(config, 'ENABLE_TELEGRAM', True), "set": lambda v: setattr(config, 'ENABLE_TELEGRAM', v),
         "callback": _on_telegram_enable_changed},
        {"desc": "인스턴스 이름", "help": "알림 메시지 머리말", "name": "TELEGRAM_INSTANCE_NAME", "type": "str",
         "get": lambda: getattr(config, 'TELEGRAM_INSTANCE_NAME', "HTS"), "set": lambda v: setattr(config, 'TELEGRAM_INSTANCE_NAME', v)},
        {"desc": "폴링 타임아웃(초)", "help": "봇 명령어 수신 대기 시간", "name": "TELEGRAM_POLLING_TIMEOUT", "type": "int",
         "get": lambda: getattr(config, 'TELEGRAM_POLLING_TIMEOUT', 10), "set": lambda v: setattr(config, 'TELEGRAM_POLLING_TIMEOUT', v)},
        {"desc": "장전 AI 브리핑 사용", "help": "매일 지정된 시간에 글로벌 매크로 시황 알림", "name": "AUTO_MORNING_BRIEFING_USE", "type": "bool", "choices": ["y", "n"],
         "get": lambda: getattr(config, 'AUTO_MORNING_BRIEFING_USE', False), "set": lambda v: setattr(config, 'AUTO_MORNING_BRIEFING_USE', v)},
        {"desc": "장전 AI 브리핑 시간", "help": "발송 시각 (예: 0830)", "name": "AUTO_MORNING_BRIEFING_TIME", "type": "time",
         "get": lambda: getattr(config, 'AUTO_MORNING_BRIEFING_TIME', "0830"), "set": lambda v: setattr(config, 'AUTO_MORNING_BRIEFING_TIME', v)}
    ]
    return _edit_config_table("텔레그램 설정 (Telegram)", items)

def modify_log_settings():
    items = [
        {"desc": "화면 자동 지우기", "help": "메뉴 이동 시 터미널 화면 클리어 여부", "name": "CLEAR_SCREEN_ON_MENU", "type": "bool", "choices": ["y", "n"],
         "get": lambda: getattr(config, 'CLEAR_SCREEN_ON_MENU', False), "set": lambda v: setattr(config, 'CLEAR_SCREEN_ON_MENU', v)},
        {"desc": "화면 로그 레벨", "help": "터미널 출력 레벨", "name": "SCREEN_DEBUG_LEVEL", "type": "str", "choices": ["OFF", "TRACE", "DEBUG"],
         "get": lambda: getattr(config, 'SCREEN_DEBUG_LEVEL', "OFF"), "set": lambda v: setattr(config, 'SCREEN_DEBUG_LEVEL', v)},
        {"desc": "파일 로그 레벨", "help": "로그 파일 저장 레벨", "name": "FILE_DEBUG_LEVEL", "type": "str", "choices": ["DEBUG", "INFO", "WARNING", "ERROR"],
         "get": lambda: getattr(config, 'FILE_DEBUG_LEVEL', "WARNING"), "set": lambda v: setattr(config, 'FILE_DEBUG_LEVEL', v),
         "callback": config.setup_logging}
    ]
    return _edit_config_table("화면 및 로그 설정 (Screen & Log)", items)

def modify_scoring_weights():
    # 기본값 정의
    defaults = {"TREND": 4.0, "MOMENTUM": 2.5, "STRENGTH": 1.5, "SYNERGY": 2.0}
    action_taken = False

    while True:
        weights = config.SCORING_WEIGHTS
        total_score = sum(weights.values())
        
        console.print("\n[bold]스코어링 모델 가중치 설정 (Scoring Weights)[/bold]")
        console.print(f"[dim]현재 총점: {total_score:.1f}점 (목표: 10.0점)[/dim]")
        
        table = Table(box=box.HORIZONTALS, show_header=True, header_style="dim", border_style="dim")
        table.add_column("항목", justify="left")
        table.add_column("현재값", justify="right")
        table.add_column("설명", justify="left", style="dim")
        
        # [수정] 항목 정의 (키, 라벨, 설명)
        items_info = [
            ("TREND", "추세 (TREND)", "이평선, MACD, SAR"),
            ("MOMENTUM", "모멘텀 (MOMENTUM)", "RSI, CCI"),
            ("STRENGTH", "강도 (STRENGTH)", "ADX, OBV"),
            ("SYNERGY", "시너지 (SYNERGY)", "지표 동조화 보너스")
        ]

        for key, label, detail in items_info:
            def_val = defaults.get(key, 0.0)
            desc = f"{detail} (기본: {def_val})"
            table.add_row(label, f"{weights[key]}", desc)
        
        console.print(table)
        
        console.print()
        choice = Prompt.ask("\n수정할 여부 선택 [dim](전체: a, 이전: b, 메인: q)[/dim]", choices=['a', 'b', 'q', 'A', 'B', 'Q'], default='b')
        console.print()
        
        if choice.lower() in ['b', 'q']:
            break
            
        if choice.lower() == 'a':
            console.print("\n[bold]각 항목의 가중치를 순서대로 입력하세요.[/bold]")
            console.print("[dim]입력하지 않고 Enter를 누르면 현재값을 유지합니다. (이전: b, 메인: q)[/dim]")
            console.print()
            
            new_weights = {}
            
            try:
                for key, label, detail in items_info:
                    current_val = weights[key]
                    prompt_msg = f"{label} [dim][{detail}][/dim] [dim](현재: {current_val})[/dim]"
                    val = Prompt.ask(prompt_msg, default=str(current_val))
                    if val.lower() in ['b', 'q']: 
                        raise ValueError("canceled")
                    new_weights[key] = float(val)
                
                new_total = sum(new_weights.values())
                
                if abs(new_total - 10.0) > 0.01:
                    console.print(f"\n[bold red]경고: 입력한 값의 합계가 {new_total:.1f}점입니다.[/bold red]")
                    console.print("[yellow]가중치의 합은 10.0점이 되어야 합니다. 다시 입력해주세요.[/yellow]")
                    continue
                
                config.SCORING_WEIGHTS.update(new_weights)
                
                _save_dynamic_config()
                console.print("\n[bold green]가중치 설정이 저장되었습니다.[/bold green]")
                action_taken = True
                
            except ValueError as e:
                if str(e) == "canceled":
                    console.print("\n[yellow]입력이 취소되었습니다.[/yellow]")
                else:
                    console.print("[red]잘못된 입력입니다. 숫자를 입력해주세요.[/red]")
    return action_taken

def modify_market_regime_params():
    items = [
        {"desc": "적응형 임계값 사용", "help": "시장 국면에 따른 점수 조절", "name": "USE_ADAPTIVE_THRESHOLD", "type": "bool", "choices": ["y", "n"],
         "get": lambda: config.MARKET_REGIME_PARAMS["USE_ADAPTIVE_THRESHOLD"], "set": lambda v: config.MARKET_REGIME_PARAMS.update({"USE_ADAPTIVE_THRESHOLD": v})},
        {"desc": "강세장 점수 보정", "help": "강세장일 때 기준 점수 조정값 (예: -1.0)", "name": "BULL_SCORE_ADJ", "type": "float",
         "get": lambda: config.MARKET_REGIME_PARAMS["BULL_SCORE_ADJ"], "set": lambda v: config.MARKET_REGIME_PARAMS.update({"BULL_SCORE_ADJ": v})},
        {"desc": "약세장 점수 보정", "help": "약세장일 때 기준 점수 조정값 (예: +1.0)", "name": "BEAR_SCORE_ADJ", "type": "float",
         "get": lambda: config.MARKET_REGIME_PARAMS["BEAR_SCORE_ADJ"], "set": lambda v: config.MARKET_REGIME_PARAMS.update({"BEAR_SCORE_ADJ": v})},
        {"desc": "횡보장 점수 보정", "help": "횡보장일 때 기준 점수 조정값 (예: 0.0)", "name": "SIDEWAYS_SCORE_ADJ", "type": "float",
         "get": lambda: config.MARKET_REGIME_PARAMS["SIDEWAYS_SCORE_ADJ"], "set": lambda v: config.MARKET_REGIME_PARAMS.update({"SIDEWAYS_SCORE_ADJ": v})},
        {"desc": "추세 판단 EMA (일)", "help": "시장 국면 판단용 지수이동평균선 (기본 20일)", "name": "REGIME_MA_PERIOD", "type": "int",
         "get": lambda: config.MARKET_REGIME_PARAMS.get("REGIME_MA_PERIOD", 20), "set": lambda v: config.MARKET_REGIME_PARAMS.update({"REGIME_MA_PERIOD": v})},
        {"desc": "추세 판단 ADX", "help": "강세장 판단용 ADX 기준 (기본 20)", "name": "REGIME_ADX_THRESHOLD", "type": "int",
         "get": lambda: config.MARKET_REGIME_PARAMS.get("REGIME_ADX_THRESHOLD", 20), "set": lambda v: config.MARKET_REGIME_PARAMS.update({"REGIME_ADX_THRESHOLD": v})}
    ]
    return _edit_config_table("시장 국면 및 적응형 임계값 (Adaptive Thresholds)", items)

def _validate_time_format(val):
    if len(val) == 4 and val.isdigit():
        hh = int(val[:2])
        mm = int(val[2:])
        if 0 <= hh <= 23 and 0 <= mm <= 59:
            return True
    return False

def modify_risk_portfolio_settings():
    """리스크 관리 및 자산 배분 설정 분리"""
    def get_items():
        items = [
            {"desc": "종목당 투자 비중", "help": "전체 자산 대비 한 종목 투자 비율 (0.1~1.0)", "name": "SYSTEM_INVEST_PER_STOCK", "type": "float", "section": "Portfolio",
             "get": lambda: getattr(config, 'SYSTEM_INVEST_PER_STOCK', 0.2), "set": lambda v: setattr(config, 'SYSTEM_INVEST_PER_STOCK', v),
             "validator": lambda v: 0 < v <= 1.0},
            {"desc": "최대 보유 종목 수", "help": "포트폴리오 최대 종목 개수", "name": "SYSTEM_MAX_HOLDINGS", "type": "int", "section": "Portfolio",
             "get": lambda: getattr(config, 'SYSTEM_MAX_HOLDINGS', 10), "set": lambda v: setattr(config, 'SYSTEM_MAX_HOLDINGS', v)},
            {"desc": "슬리피지 비율", "help": "주문가 보정 및 백테스트 비용", "name": "SLIPPAGE_RATE", "type": "float", "section": "Portfolio",
             "get": lambda: getattr(config, 'SLIPPAGE_RATE', 0.002), "set": lambda v: setattr(config, 'SLIPPAGE_RATE', v)},
             
            {"desc": "변동성 타겟팅 사용", "help": "ATR 기반 비중 조절 사용 여부", "name": "USE_VOLATILITY_TARGETING", "type": "bool", "choices": ["y", "n"], "section": "Volatility",
             "get": lambda: getattr(config, 'USE_VOLATILITY_TARGETING', True), "set": lambda v: setattr(config, 'USE_VOLATILITY_TARGETING', v)}
        ]
        
        if getattr(config, 'USE_VOLATILITY_TARGETING', True):
            items.extend([
                {"desc": "목표 연간 변동성", "help": "0.1=10%, 0.2=20%, 0.3=30%", "name": "TARGET_VOLATILITY", "type": "float", "section": "Volatility",
                 "get": lambda: getattr(config, 'TARGET_VOLATILITY', 0.30), "set": lambda v: setattr(config, 'TARGET_VOLATILITY', v)},
                {"desc": "스케일링 최대 배수", "help": "비중 확대 제한", "name": "VOLATILITY_SCALING_MAX", "type": "float", "section": "Volatility",
                 "get": lambda: getattr(config, 'VOLATILITY_SCALING_MAX', 2.0), "set": lambda v: setattr(config, 'VOLATILITY_SCALING_MAX', v)},
                {"desc": "스케일링 최소 배수", "help": "비중 축소 제한", "name": "VOLATILITY_SCALING_MIN", "type": "float", "section": "Volatility",
                 "get": lambda: getattr(config, 'VOLATILITY_SCALING_MIN', 0.5), "set": lambda v: setattr(config, 'VOLATILITY_SCALING_MIN', v)}
            ])
            
        items.extend([
            {"desc": "시장 필터링 사용", "help": "지수 하락 시 신규 매수 보류", "name": "USE_MARKET_FILTER", "type": "bool", "choices": ["y", "n"], "section": "Risk",
             "get": lambda: getattr(config, 'USE_MARKET_FILTER', True), "set": lambda v: setattr(config, 'USE_MARKET_FILTER', v)},
            {"desc": "시장 필터링 SMA (일)", "help": "지수 추세 판단용 단순이동평균선", "name": "MARKET_FILTER_MA", "type": "int", "section": "Risk",
             "get": lambda: getattr(config, 'MARKET_FILTER_MA', 50), "set": lambda v: setattr(config, 'MARKET_FILTER_MA', v)},
            {"desc": "연속 에러 허용", "help": "시스템 중단 임계값", "name": "SYSTEM_MAX_CONSECUTIVE_ERRORS", "type": "int", "section": "Risk",
             "get": lambda: getattr(config, 'SYSTEM_MAX_CONSECUTIVE_ERRORS', 5), "set": lambda v: setattr(config, 'SYSTEM_MAX_CONSECUTIVE_ERRORS', v)},
            {"desc": "일일 손실 제한 (%)", "help": "자산 보호를 위한 비상 정지 기준", "name": "SYSTEM_DAILY_LOSS_LIMIT", "type": "float", "section": "Risk",
             "get": lambda: getattr(config, 'SYSTEM_DAILY_LOSS_LIMIT', 10.0), "set": lambda v: setattr(config, 'SYSTEM_DAILY_LOSS_LIMIT', v)},
            {"desc": "1회 최대 리스크 (%)", "help": "계좌 대비 1회 매매 최대 손실폭", "name": "SYSTEM_RISK_PER_TRADE", "type": "float", "section": "Risk",
             "get": lambda: getattr(config, 'SYSTEM_RISK_PER_TRADE', 5.0), "set": lambda v: setattr(config, 'SYSTEM_RISK_PER_TRADE', v)},
        ])
        return items

    return _edit_config_table("리스크 및 자산 배분 설정 (Risk & Portfolio)", get_items)

def modify_trading_cycle_settings():
    """트레이딩 시간 및 주기 설정 분리"""
    def get_items():
        items = [
            {"desc": "거래 시작 시간", "help": "매매 허용 시작 시각 (HHMM)", "name": "SYSTEM_TRADING_START_TIME", "type": "time", "section": "Time",
             "get": lambda: getattr(config, 'SYSTEM_TRADING_START_TIME', "0920"), "set": lambda v: setattr(config, 'SYSTEM_TRADING_START_TIME', v)},
            {"desc": "거래 종료 시간", "help": "매매 허용 종료 시각 (HHMM)", "name": "SYSTEM_TRADING_END_TIME", "type": "time", "section": "Time",
             "get": lambda: getattr(config, 'SYSTEM_TRADING_END_TIME', "1510"), "set": lambda v: setattr(config, 'SYSTEM_TRADING_END_TIME', v)},
            {"desc": "모니터링 주기 (초)", "help": "자동매매 루프 실행 간격", "name": "SYSTEM_TRADING_INTERVAL", "type": "int", "section": "Time",
             "get": lambda: getattr(config, 'SYSTEM_TRADING_INTERVAL', 180), "set": lambda v: setattr(config, 'SYSTEM_TRADING_INTERVAL', v)},

            {"desc": "체결 감시 주기(초)", "help": "주문 직후 체결 확인 간격", "name": "CONCLUSION_CHECK_INTERVAL", "type": "int", "section": "Execution",
             "get": lambda: getattr(config, 'CONCLUSION_CHECK_INTERVAL', 5), "set": lambda v: setattr(config, 'CONCLUSION_CHECK_INTERVAL', v)},
            {"desc": "대기 모드 주기(초)", "help": "주문이 없는 평상시 체결 확인 간격", "name": "CONCLUSION_CHECK_IDLE_INTERVAL", "type": "int", "section": "Execution",
             "get": lambda: getattr(config, 'CONCLUSION_CHECK_IDLE_INTERVAL', 300), "set": lambda v: setattr(config, 'CONCLUSION_CHECK_IDLE_INTERVAL', v)},
            {"desc": "집중 감시 시간(초)", "help": "주문 후 집중 감시 유지 시간", "name": "CONCLUSION_CHECK_ACTIVE_DURATION", "type": "int", "section": "Execution",
             "get": lambda: getattr(config, 'CONCLUSION_CHECK_ACTIVE_DURATION', 60), "set": lambda v: setattr(config, 'CONCLUSION_CHECK_ACTIVE_DURATION', v)},
            {"desc": "미체결 취소 대기(초)", "help": "지정가 주문 유지 시간", "name": "UNFILLED_ORDER_CANCEL_SECONDS", "type": "int", "section": "Execution",
             "get": lambda: getattr(config, 'UNFILLED_ORDER_CANCEL_SECONDS', 120), "set": lambda v: setattr(config, 'UNFILLED_ORDER_CANCEL_SECONDS', v)},
            {"desc": "차트 데이터 캐시(분)", "help": "일봉 메모리 캐시 유지 시간", "name": "CHART_CACHE_TTL_MINUTES", "type": "int", "section": "Execution",
             "get": lambda: getattr(config, 'CHART_CACHE_TTL_MINUTES', 180), "set": lambda v: setattr(config, 'CHART_CACHE_TTL_MINUTES', v)},
        ]
        return items

    return _edit_config_table("트레이딩 시간 및 주기 (Time & Cycle)", get_items)

def reset_to_default():
    console.print()
    if Prompt.ask("모든 설정을 시스템 기본값으로 초기화하시겠습니까?", choices=["y", "n"], default="n") != "y":
        return False
    console.print()

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
        "BUY_SCORE": 7.5, "RISE_SCORE": 6.0, "BUY_RSI_MAX": 65, "BUY_VOL_STRENGTH": 100.0,
        "DISPARITY_UPPER": 110, "DISPARITY_LOWER": 90,
        "USE_MEAN_REVERSION": True, "MR_RSI_MAX": 40.0, 
        "MR_DISPARITY_MAX": 90.0, "MR_VOL_STRENGTH": 120.0,
        "SUPER_MOMENTUM_USE": True, "SUPER_MOMENTUM_SCORE": 8.5,
        "SUPER_MOMENTUM_W52_POS": 90.0, "SUPER_BUY_RSI_MAX": 75.0
    })
    config.SELL_STRATEGY.update({
        "STOP_LOSS_RATE": -7.0, "TAKE_PROFIT_RATE": 20.0, "TAKE_PROFIT_RSI": 75, "SELL_SCORE": 5.0,
        "TRAILING_STOP_ACTIVATION_RATE": 10.0, "TRAILING_STOP_CALLBACK_RATE": 3.0
        ,"USE_ATR_STOP": True, "ATR_STOP_MULTIPLIER": 2.0,
        "HALF_TAKE_PROFIT_USE": True,
        "TIME_STOP_USE": True, "TIME_STOP_DAYS": 10, "TIME_STOP_MIN_PROFIT_RATE": 3.0,
        "MR_GRACE_LOSS_RATE": -5.0, "SUPER_TAKE_PROFIT_RSI": 85.0,
        "MAX_ATR_STOP_LOSS_RATE": -15.0
    })
    config.INDICATOR_PARAMS.update({
        "CHART_LOOKBACK_DAYS": 730, "SAR_AF_START": 0.02, "SAR_AF_STEP": 0.02, "SAR_AF_MAX": 0.2,
        "ADX_PERIOD": 14, "CCI_WINDOW": 20, "CCI_UPPER": 100, "CCI_LOWER": -100,
        "MACD_FAST": 12, "MACD_SLOW": 26, "MACD_SIGNAL": 9, "OBV_MA_PERIOD": 5,
        "RSI_PERIOD": 14, "RSI_SIGNAL": 14, "RSI_UPPER": 70, "RSI_MID": 50, "RSI_LOWER": 30,
        "ATR_PERIOD": 14
    })
    config.SCORING_WEIGHTS.update({
        "TREND": 4.0, "MOMENTUM": 2.5, "STRENGTH": 1.5, "SYNERGY": 2.0
    })
    config.MARKET_REGIME_PARAMS.update({
        "USE_ADAPTIVE_THRESHOLD": True, "BULL_SCORE_ADJ": -1.0, "BEAR_SCORE_ADJ": 1.0,
        "SIDEWAYS_SCORE_ADJ": 0.0, "REGIME_MA_PERIOD": 20, "REGIME_ADX_THRESHOLD": 20
    })
    
    config.SYSTEM_INVEST_PER_STOCK = 0.2
    config.SYSTEM_MAX_HOLDINGS = 10
    config.SYSTEM_TRADING_INTERVAL = 180
    config.SYSTEM_DAILY_LOSS_LIMIT = 10.0
    config.USE_MARKET_FILTER = True
    config.MARKET_FILTER_MA = 50
    config.CONCLUSION_CHECK_INTERVAL = 5
    config.CONCLUSION_CHECK_IDLE_INTERVAL = 300
    config.CONCLUSION_CHECK_ACTIVE_DURATION = 60
    config.ENABLE_TELEGRAM = True
    config.TELEGRAM_INSTANCE_NAME = "HTS"
    config.TELEGRAM_POLLING_TIMEOUT = 10
    config.AUTO_MORNING_BRIEFING_USE = False
    config.AUTO_MORNING_BRIEFING_TIME = "0830"
    config.SCREEN_DEBUG_LEVEL = "OFF"
    config.CLEAR_SCREEN_ON_MENU = False
    config.FILE_DEBUG_LEVEL = "WARNING"
    config.SYSTEM_MAX_CONSECUTIVE_ERRORS = 5
    config.SYSTEM_TRADING_START_TIME = "0920"
    config.SYSTEM_TRADING_END_TIME = "1510"
    config.SYSTEM_RISK_PER_TRADE = 5.0
    config.USE_VOLATILITY_TARGETING = True
    config.TARGET_VOLATILITY = 0.20
    config.VOLATILITY_SCALING_MAX = 2.0
    config.VOLATILITY_SCALING_MIN = 0.5
    config.UNFILLED_ORDER_CANCEL_SECONDS = 120
    config.CHART_CACHE_TTL_MINUTES = 180
    config.SLIPPAGE_RATE = 0.002

    console.print("\n[bold green]모든 설정이 기본값으로 초기화되었습니다.[/bold green]")
    return True

def system_config_menu():
    base_breadcrumb_len = len(context.USER_ACTION_BREADCRUMB)
    last_choice = "9"
    while True:
        # 경로 누적 방지를 위해 루프 시작 시 진입 시점의 길이로 복원
        context.USER_ACTION_BREADCRUMB = context.USER_ACTION_BREADCRUMB[:base_breadcrumb_len]
        
        menu_items = [
            ("1", "매수 및 매도 전략 설정", "Buy & Sell Strategy"),
            ("2", "스코어링 및 시장 국면 설정", "Scoring & Regime"),
            ("3", "리스크 및 자산 배분 설정", "Risk & Portfolio"),
            ("4", "기술적 지표 파라미터", "Indicators"),
            ("5", "환경 및 시스템 설정", "Environment & System"),
            ("9", "시스템 설정 전체 조회", "View Config"),
            ("0", "설정 초기화", "Reset to Default")
        ]
        choice = utils.show_menu("시스템 설정 (System Settings)", menu_items, default_choice=last_choice)
        if choice.lower() in ['b', 'q']: return False
        if choice.lower() == 'h':
            if getattr(utils, 'show_help', None):
                utils.show_help()
                utils.pause()
            continue
        
        last_choice = choice
        menu_map = dict((k, v) for k, v, _ in menu_items)
        context.USER_ACTION_BREADCRUMB.append(f"[{choice}] {menu_map.get(choice, '')}")
        
        if choice == "1":
            sub_items = [("1", "매수/분석 임계값", "Buy"), ("2", "매도/청산 전략", "Sell")]
            sub_choice = utils.show_menu("매수 및 매도 전략 설정", sub_items, default_choice="b")
            if sub_choice.lower() in ['b', 'q']: continue
            
            sub_map = dict((k, v) for k, v, _ in sub_items)
            context.USER_ACTION_BREADCRUMB.append(f"[{sub_choice}] {sub_map.get(sub_choice, '')}")
            
            if sub_choice == "1": modify_analysis_thresholds()
            elif sub_choice == "2": modify_sell_strategy()
            
        elif choice == "2":
            sub_items = [("1", "스코어링 가중치 설정", "Weights"), ("2", "적응형 임계값 (시장국면) 설정", "Regime")]
            sub_choice = utils.show_menu("스코어링 및 시장 국면 설정", sub_items, default_choice="b")
            if sub_choice.lower() in ['b', 'q']: continue
            
            sub_map = dict((k, v) for k, v, _ in sub_items)
            context.USER_ACTION_BREADCRUMB.append(f"[{sub_choice}] {sub_map.get(sub_choice, '')}")
            
            if sub_choice == "1": modify_scoring_weights()
            elif sub_choice == "2": modify_market_regime_params()
            
        elif choice == "3": modify_risk_portfolio_settings()
        elif choice == "4": modify_indicator_params()
        elif choice == "5":
            sub_items = [("1", "트레이딩 시간 및 주기", "Time & Cycle"), ("2", "텔레그램 및 AI 브리핑", "Telegram"), ("3", "화면 및 로그", "Log")]
            sub_choice = utils.show_menu("환경 및 시스템 설정", sub_items, default_choice="b")
            if sub_choice.lower() in ['b', 'q']: continue
            
            sub_map = dict((k, v) for k, v, _ in sub_items)
            context.USER_ACTION_BREADCRUMB.append(f"[{sub_choice}] {sub_map.get(sub_choice, '')}")
            
            if sub_choice == "1": modify_trading_cycle_settings()
            elif sub_choice == "2": modify_telegram_settings()
            elif sub_choice == "3": modify_log_settings()
            
        elif choice == "9": 
            view_system_config()
            utils.pause()
        elif choice == "0": 
            if reset_to_default() is not False: utils.pause()