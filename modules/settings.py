import json
import jsonio
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
    try:
        check_and_update_active_preset()
    except Exception:
        pass

    data = {
        "ACTIVE_PRESET": getattr(config.settings, 'ACTIVE_PRESET', 'default'),
        "ANALYSIS_THRESHOLDS": config.ANALYSIS_THRESHOLDS,
        "SELL_STRATEGY": config.SELL_STRATEGY,
        "INDICATOR_PARAMS": config.INDICATOR_PARAMS,
        "SCORING_WEIGHTS": config.SCORING_WEIGHTS,
        "MARKET_REGIME_PARAMS": config.MARKET_REGIME_PARAMS,
        "SYSTEM_INVEST_PER_STOCK": config.settings.SYSTEM_INVEST_PER_STOCK,
        "SYSTEM_MAX_HOLDINGS": config.settings.SYSTEM_MAX_HOLDINGS,
        "SYSTEM_TRADING_INTERVAL": getattr(config.settings, 'SYSTEM_TRADING_INTERVAL', 180),
        "SYSTEM_DAILY_LOSS_LIMIT": getattr(config.settings, 'SYSTEM_DAILY_LOSS_LIMIT', 10.0),
        "USE_MARKET_FILTER": getattr(config.settings, 'USE_MARKET_FILTER', True),
        "USE_RS_FILTER": getattr(config.settings, 'USE_RS_FILTER', True),
        "MARKET_FILTER_MA": getattr(config.settings, 'MARKET_FILTER_MA', 20),
        "CONCLUSION_CHECK_INTERVAL": getattr(config.settings, 'CONCLUSION_CHECK_INTERVAL', 5),
        "CONCLUSION_CHECK_IDLE_INTERVAL": getattr(config.settings, 'CONCLUSION_CHECK_IDLE_INTERVAL', 300),
        "CONCLUSION_CHECK_ACTIVE_DURATION": getattr(config.settings, 'CONCLUSION_CHECK_ACTIVE_DURATION', 60),
        "UNFILLED_ORDER_CANCEL_SECONDS": getattr(config.settings, 'UNFILLED_ORDER_CANCEL_SECONDS', 120),
        "CHART_CACHE_TTL_MINUTES": getattr(config.settings, 'CHART_CACHE_TTL_MINUTES', 180),
        "ENABLE_TELEGRAM": getattr(config.settings, 'ENABLE_TELEGRAM', True),
        "TELEGRAM_INSTANCE_NAME": getattr(config.settings, 'TELEGRAM_INSTANCE_NAME', "HTS"),
        "TELEGRAM_POLLING_TIMEOUT": getattr(config.settings, 'TELEGRAM_POLLING_TIMEOUT', 10),
        "AUTO_MORNING_BRIEFING_USE": getattr(config.settings, 'AUTO_MORNING_BRIEFING_USE', False),
        "AUTO_MORNING_BRIEFING_TIME": getattr(config.settings, 'AUTO_MORNING_BRIEFING_TIME', "0830"),
        "AUTO_DISCLOSURE_ALERT_USE": getattr(config.settings, 'AUTO_DISCLOSURE_ALERT_USE', True),
        "MARKET_HALT_ALERT_USE": getattr(config.settings, 'MARKET_HALT_ALERT_USE', True),
        "MARKET_HALT_VI_USE": getattr(config.settings, 'MARKET_HALT_VI_USE', False),
        "SCREEN_DEBUG_LEVEL": getattr(config.settings, 'SCREEN_DEBUG_LEVEL', "ERROR"),
        "CLEAR_SCREEN_ON_MENU": getattr(config.settings, 'CLEAR_SCREEN_ON_MENU', False),
        "FILE_DEBUG_LEVEL": getattr(config.settings, 'FILE_DEBUG_LEVEL', "WARNING"),
        "SYSTEM_MAX_CONSECUTIVE_ERRORS": getattr(config.settings, 'SYSTEM_MAX_CONSECUTIVE_ERRORS', 5),
        "SYSTEM_TRADING_START_TIME": getattr(config.settings, 'SYSTEM_TRADING_START_TIME', "0800"),
        "SYSTEM_TRADING_END_TIME": getattr(config.settings, 'SYSTEM_TRADING_END_TIME', "2000"),
        "SYSTEM_RISK_PER_TRADE": getattr(config.settings, 'SYSTEM_RISK_PER_TRADE', 5.0),
        "SYSTEM_MAX_PORTFOLIO_RISK": getattr(config.settings, 'SYSTEM_MAX_PORTFOLIO_RISK', 10.0),
        "USE_VOLATILITY_TARGETING": getattr(config.settings, 'USE_VOLATILITY_TARGETING', True),
        "TARGET_VOLATILITY": getattr(config.settings, 'TARGET_VOLATILITY', 0.30),
        "VOLATILITY_SCALING_MAX": getattr(config.settings, 'VOLATILITY_SCALING_MAX', 2.0),
        "VOLATILITY_SCALING_MIN": getattr(config.settings, 'VOLATILITY_SCALING_MIN', 0.5),
        "SLIPPAGE_RATE": getattr(config.settings, 'SLIPPAGE_RATE', 0.002),
        "USE_CORRELATION_FILTER": getattr(config.settings, 'USE_CORRELATION_FILTER', True),
        "CORRELATION_THRESHOLD": getattr(config.settings, 'CORRELATION_THRESHOLD', 0.7)
    }
    
    path = os.path.join(config.JSON_DIR, "dynamic_config.json")
    if jsonio.save_json(path, data):
        console.print(f"\n[green]설정이 저장되었습니다. (재시작 시에도 유지됨)[/green]")
        console.print(f"[dim]저장 경로: {path}[/dim]")
    else:
        console.print("\n[bold red]설정 저장 실패 (상세는 로그 참조)[/bold red]")

def _get_custom_settings_summary():
    """커스텀 프리셋 전환 시 텔레그램 알림에 포함할 변경 내역 요약 문자열 생성"""
    changed_items = config.get_custom_settings()
    if not changed_items:
        return ""
        
    lines = []
    for key, info in changed_items.items():
        dict_key = info.get("key", key)
        desc = getattr(config, 'CONFIG_DESCRIPTIONS', {}).get(dict_key, dict_key)
        lines.append(f"• {desc}: {info['default']} ➔ {info['current']}")
        
    if lines:
        return "\n\n[현재 적용된 커스텀 설정 내역]\n" + "\n".join(lines)
    return ""

_PRESET_DISPLAY = {
    "default": ("🟢", "기본 (Default)"),
    "bull": ("🔴", "강세장 (Bull)"),
    "bear": ("🔵", "약세장 (Bear)"),
    "sideways": ("🟡", "횡보장 (Sideways)"),
    "custom": ("⚪", "커스텀 (Custom)"),
}

def _notify_preset_switch(old_preset, new_preset):
    """설정 변경으로 활성 프리셋이 바뀌면 텔레그램 알림을 보내고, 하단 고정 키보드
    (상태 요약 버튼의 프리셋 색상 이모지)를 최신 프리셋으로 재전송하여 갱신한다.

    핵심: 텔레그램 Reply Keyboard는 새 메시지를 보낼 때만 갱신되므로, 프리셋이
    '커스텀으로 진입'하는 경우뿐 아니라 '커스텀→기본' 등 어떤 전환이든 메시지를
    보내야 버튼 색상이 CLI와 동기화된다.
    """
    if new_preset == old_preset:
        return
    try:
        if not getattr(config, 'ENABLE_TELEGRAM', True):
            return
        from modules.telegram_bot import TelegramCommander
        emoji, name = _PRESET_DISPLAY.get(new_preset, ("⚪", new_preset))
        if new_preset == "custom":
            custom_summary = _get_custom_settings_summary()
            msg = f"{emoji} [설정 변경] 세부 설정이 변경되어 '{name}' 프리셋 모드로 전환되었습니다.{custom_summary}"
        else:
            msg = f"{emoji} [설정 변경] 세부 설정이 '{name}' 프리셋과 일치하여 해당 모드로 전환되었습니다."
        TelegramCommander()._send_reply(msg)
    except Exception:
        pass

def view_system_config(group=None):
    """현재 시스템 설정 조회 (group: 1~5 해당 그룹만 조회, None이면 전체)"""
    group_names = {
        1: "매수 및 매도 전략 설정",
        2: "스코어링 및 시장 국면 설정",
        3: "리스크 및 자산 배분 설정",
        4: "기술적 지표 파라미터",
        5: "환경 및 시스템 설정",
    }
    if group not in group_names:
        group = None

    title = "현재 시스템 설정 (System Configuration)"
    if group:
        title += f" — {group}. {group_names[group]}"

    caption = "그룹 번호(n-m)는 시스템 설정 메뉴의 편집 경로와 동일 (예: 5-5 → 메뉴 5 → 5)"
    if group in (None, 1, 2, 3):
        caption = "[cyan]*[/cyan] : 전략 프리셋(메뉴 7) 적용 시 함께 변경되는 항목\n" + caption

    console.print()
    table = Table(
        title=title,
        caption=caption,
        caption_justify="left",
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

    # 프리셋(메뉴 7) 적용 시 함께 변경되는 항목 표시용
    preset_keys = set(DEFAULT_PRESETS.get("default", {}))

    def row(desc, help_text, var_name, value, key=None, indent=False):
        mark = " [cyan]*[/cyan]" if key in preset_keys else ""
        if indent:
            table.add_row(f"  └ {desc}{mark}\n    [dim]{help_text}[/dim]", var_name, value)
        else:
            table.add_row(f"{desc}{mark}\n[dim]{help_text}[/dim]", var_name, value)

    def header(no):
        if table.row_count:
            table.add_section()
        table.add_row(f"[bold]{no}. {group_names[no]}[/]", "", "")

    def subheader(text, first=False):
        if not first:
            table.add_section()
        table.add_row(f"[bold dim]  {text}[/]", "", "")

    thresholds = config.ANALYSIS_THRESHOLDS
    sell = config.SELL_STRATEGY

    # =========================================================
    # 1. 매수 및 매도 전략 설정
    # =========================================================
    if group in (None, 1):
        header(1)
        subheader("1-1. 기본 진입 조건", first=True)
        row("매수 기준 점수", "진입 임계값 (종합 점수)", "ANALYSIS_THRESHOLDS['BUY_SCORE']", f"{thresholds.get('BUY_SCORE')}", key="BUY_SCORE")
        row("상승 추세 점수", "관망/상승 판단 기준", "ANALYSIS_THRESHOLDS['RISE_SCORE']", f"{thresholds.get('RISE_SCORE')}", key="RISE_SCORE")
        row("관심 신호 최소 개수", "추세전환 초기신호 N개 이상 시 '관심'(태동) 분류 (0=미사용)", "ANALYSIS_THRESHOLDS['INTEREST_SIGNAL_MIN']", f"{thresholds.get('INTEREST_SIGNAL_MIN', 3)}", key="INTEREST_SIGNAL_MIN")
        row("관심 60일선 근접 비율", "60일선의 이 비율 이상이면 '돌파 시도' 신호로 인정", "ANALYSIS_THRESHOLDS['INTEREST_MA60_NEAR']", f"{thresholds.get('INTEREST_MA60_NEAR', 0.97)}", key="INTEREST_MA60_NEAR")
        row("매수 허용 RSI 상한", "과열 방지 (이 값보다 낮아야 매수)", "ANALYSIS_THRESHOLDS['BUY_RSI_MAX']", f"{thresholds.get('BUY_RSI_MAX')}", key="BUY_RSI_MAX")
        row("매수 체결강도 기준", "수급 확인 (이 값 이상이어야 매수)", "ANALYSIS_THRESHOLDS['BUY_VOL_STRENGTH']", f"{thresholds.get('BUY_VOL_STRENGTH')}%", key="BUY_VOL_STRENGTH")
        row("비대칭성 자동 계산", "체결강도 100% 기준으로 비례하여 자동 조정", "ANALYSIS_THRESHOLDS['AUTO_ADJUST_ASK_BID_RATIO']", f"{thresholds.get('AUTO_ADJUST_ASK_BID_RATIO', True)}", key="AUTO_ADJUST_ASK_BID_RATIO")
        row("매도잔량 비율 기준", "가짜 체결강도 방어 (체결강도 100% 기준 비율)", "ANALYSIS_THRESHOLDS['BUY_ASK_BID_RATIO']", f"{thresholds.get('BUY_ASK_BID_RATIO', 1.0)}배", key="BUY_ASK_BID_RATIO")
        row("과열 이격도 상한", "20일선 기준 이 비율 이상 시 단기과열", "ANALYSIS_THRESHOLDS['DISPARITY_UPPER']", f"{thresholds.get('DISPARITY_UPPER', 110.0)}%", key="DISPARITY_UPPER")
        row("침체 이격도 하한", "20일선 기준 이 비율 이하 시 과매도", "ANALYSIS_THRESHOLDS['DISPARITY_LOWER']", f"{thresholds.get('DISPARITY_LOWER', 90.0)}%", key="DISPARITY_LOWER")

        subheader("1-2. 서브전략 (슈퍼 모멘텀/피라미딩)")
        # [추세추종 보호] 역매수(역추세) 관련 설정은 조회·편집 화면에서 숨김 (ANTI_TREND_HIDDEN_KEYS 주석 참조)
        row("슈퍼 모멘텀 (RSI 유연화)", "주도주 랠리 시 RSI 허용치 완화", "ANALYSIS_THRESHOLDS['SUPER_MOMENTUM_USE']", f"{thresholds.get('SUPER_MOMENTUM_USE', True)}", key="SUPER_MOMENTUM_USE")
        if thresholds.get('SUPER_MOMENTUM_USE', True):
            row("슈퍼 매수 발동 점수", "기준 점수 이상 & 신고가 90% 이상 시 발동", "ANALYSIS_THRESHOLDS['SUPER_MOMENTUM_SCORE']", f"{thresholds.get('SUPER_MOMENTUM_SCORE', 8.5)}", key="SUPER_MOMENTUM_SCORE", indent=True)
            row("슈퍼 52주 위치 기준", "신고가 근접 여부 (예: 90.0% 이상)", "ANALYSIS_THRESHOLDS['SUPER_MOMENTUM_W52_POS']", f"{thresholds.get('SUPER_MOMENTUM_W52_POS', 90.0)}%", key="SUPER_MOMENTUM_W52_POS", indent=True)
            row("완화된 매수 RSI 상한", "발동 시 적용되는 진입 최대 RSI", "ANALYSIS_THRESHOLDS['SUPER_BUY_RSI_MAX']", f"{thresholds.get('SUPER_BUY_RSI_MAX', 75.0)}", key="SUPER_BUY_RSI_MAX", indent=True)
        row("피라미딩 (수익 증액)", "수익으로 추세 검증된 포지션만 증액 (물타기 반대)", "ANALYSIS_THRESHOLDS['PYRAMIDING_USE']", f"{thresholds.get('PYRAMIDING_USE', True)}", key="PYRAMIDING_USE")
        if thresholds.get('PYRAMIDING_USE', True):
            row("증액 발동 수익률", "이 수익률 이상 & 매수신호 유지 시 증액", "ANALYSIS_THRESHOLDS['PYRAMIDING_PROFIT_TRIGGER']", f"{thresholds.get('PYRAMIDING_PROFIT_TRIGGER', 10.0)}%", key="PYRAMIDING_PROFIT_TRIGGER", indent=True)
            row("증액 비율", "보유 수량 대비 증액 수량 비율", "ANALYSIS_THRESHOLDS['PYRAMIDING_RATIO']", f"{thresholds.get('PYRAMIDING_RATIO', 0.5)}", key="PYRAMIDING_RATIO", indent=True)
            row("최대 증액 횟수", "포지션당 피라미딩 허용 횟수", "ANALYSIS_THRESHOLDS['PYRAMIDING_MAX_COUNT']", f"{thresholds.get('PYRAMIDING_MAX_COUNT', 1)}회", key="PYRAMIDING_MAX_COUNT", indent=True)

        subheader("1-3. 매도/청산 — 트레일링 스탑")
        # [추세추종 보호] 고정 익절/반익절/RSI 과열 매도는 조회·편집 화면에서 숨김 (ANTI_TREND_HIDDEN_KEYS 주석 참조)
        row("TS 발동 수익률", "트레일링 스탑 감시 시작점", "SELL_STRATEGY['TRAILING_STOP_ACTIVATION_RATE']", f"{sell.get('TRAILING_STOP_ACTIVATION_RATE')}%", key="TRAILING_STOP_ACTIVATION_RATE")
        row("TS 하락 감지율", "최고가 대비 최소 하락률 (ATR 동적 콜백의 하한)", "SELL_STRATEGY['TRAILING_STOP_CALLBACK_RATE']", f"{sell.get('TRAILING_STOP_CALLBACK_RATE')}%", key="TRAILING_STOP_CALLBACK_RATE")
        row("TS ATR 배수", "샹들리에 엑시트: TS 콜백을 ATR×배수로 동적 확대", "SELL_STRATEGY['TRAILING_ATR_MULTIPLIER']", f"{sell.get('TRAILING_ATR_MULTIPLIER', 3.0)}", key="TRAILING_ATR_MULTIPLIER")

        subheader("1-4. 매도/청산 — 손절")
        row("손절 수익률", "손실 제한 (Stop Loss)", "SELL_STRATEGY['STOP_LOSS_RATE']", f"{sell.get('STOP_LOSS_RATE')}%", key="STOP_LOSS_RATE")
        row("ATR 손절 사용", "변동성 기반 동적 손절", "SELL_STRATEGY['USE_ATR_STOP']", f"{sell.get('USE_ATR_STOP', False)}", key="USE_ATR_STOP")
        if sell.get('USE_ATR_STOP', False):
            row("ATR 손절 배수", "ATR * 배수 만큼 손절폭 설정", "SELL_STRATEGY['ATR_STOP_MULTIPLIER']", f"{sell.get('ATR_STOP_MULTIPLIER', 2.0)}", key="ATR_STOP_MULTIPLIER", indent=True)
            row("ATR 손절 최대 한도", "비정상적인 과도한 손절폭 제한", "SELL_STRATEGY['MAX_ATR_STOP_LOSS_RATE']", f"{sell.get('MAX_ATR_STOP_LOSS_RATE', -15.0)}%", key="MAX_ATR_STOP_LOSS_RATE", indent=True)
        row("본전 청산 수익률", "손절선 상향 발동 기준 수익률", "SELL_STRATEGY['BREAK_EVEN_PROFIT_RATE']", f"{sell.get('BREAK_EVEN_PROFIT_RATE', 7.0)}%", key="BREAK_EVEN_PROFIT_RATE")
        row("본전 청산 손절선", "발동 시 상향될 새로운 손절률", "SELL_STRATEGY['BREAK_EVEN_STOP_RATE']", f"{sell.get('BREAK_EVEN_STOP_RATE', 0.5)}%", key="BREAK_EVEN_STOP_RATE")

        subheader("1-5. 매도/청산 — 기타")
        row("시간 청산 사용", "장기 횡보 종목 강제 매도", "SELL_STRATEGY['TIME_STOP_USE']", f"{sell.get('TIME_STOP_USE', True)}", key="TIME_STOP_USE")
        if sell.get('TIME_STOP_USE', True):
            row("청산 기준일", "매수 후 경과 일수 (달력 기준)", "SELL_STRATEGY['TIME_STOP_DAYS']", f"{sell.get('TIME_STOP_DAYS', 5)}일", key="TIME_STOP_DAYS", indent=True)
            row("최소 기대 수익", "해당 기간 내 도달해야 할 수익률", "SELL_STRATEGY['TIME_STOP_MIN_PROFIT_RATE']", f"{sell.get('TIME_STOP_MIN_PROFIT_RATE', 3.0)}%", key="TIME_STOP_MIN_PROFIT_RATE", indent=True)
        row("매도(추세이탈) 점수", "점수 하락 시 매도", "SELL_STRATEGY['SELL_SCORE']", f"{sell.get('SELL_SCORE')}", key="SELL_SCORE")

    # =========================================================
    # 2. 스코어링 및 시장 국면 설정
    # =========================================================
    if group in (None, 2):
        header(2)
        weights = config.SCORING_WEIGHTS
        total_score = sum(weights.values())
        subheader(f"2-1. 스코어링 가중치 (총점: {total_score:.1f})", first=True)
        row("추세 팩터", "이평선, MACD, SAR", "SCORING_WEIGHTS['TREND']", f"{weights.get('TREND')}", key="TREND")
        row("모멘텀 팩터", "RSI, CCI", "SCORING_WEIGHTS['MOMENTUM']", f"{weights.get('MOMENTUM')}", key="MOMENTUM")
        row("강도/수급 팩터", "ADX, OBV", "SCORING_WEIGHTS['STRENGTH']", f"{weights.get('STRENGTH')}", key="STRENGTH")
        row("시너지 가산점", "지표 동조화 보너스", "SCORING_WEIGHTS['SYNERGY']", f"{weights.get('SYNERGY')}", key="SYNERGY")

        subheader("2-2. 적응형 임계값 (시장국면)")
        regime = config.MARKET_REGIME_PARAMS
        row("사용 여부", "시장 국면 반영", "MARKET_REGIME_PARAMS['USE_ADAPTIVE_THRESHOLD']", f"{regime.get('USE_ADAPTIVE_THRESHOLD')}", key="USE_ADAPTIVE_THRESHOLD")
        if regime.get('USE_ADAPTIVE_THRESHOLD'):
            row("강세장 보정", "기준 점수 완화값", "MARKET_REGIME_PARAMS['BULL_SCORE_ADJ']", f"{regime.get('BULL_SCORE_ADJ')}", key="BULL_SCORE_ADJ", indent=True)
            row("약세장 보정", "기준 점수 강화값", "MARKET_REGIME_PARAMS['BEAR_SCORE_ADJ']", f"{regime.get('BEAR_SCORE_ADJ')}", key="BEAR_SCORE_ADJ", indent=True)
            row("횡보장 보정", "기준 점수 유지값", "MARKET_REGIME_PARAMS['SIDEWAYS_SCORE_ADJ']", f"{regime.get('SIDEWAYS_SCORE_ADJ')}", key="SIDEWAYS_SCORE_ADJ", indent=True)
            row("추세 판단 EMA (일)", "시장 국면 판단용 지수이동평균선", "MARKET_REGIME_PARAMS['REGIME_MA_PERIOD']", f"{regime.get('REGIME_MA_PERIOD', 20)}", key="REGIME_MA_PERIOD", indent=True)
            row("추세 판단 ADX", "강세장 판단용 ADX 기준", "MARKET_REGIME_PARAMS['REGIME_ADX_THRESHOLD']", f"{regime.get('REGIME_ADX_THRESHOLD', 20)}", key="REGIME_ADX_THRESHOLD", indent=True)

    # =========================================================
    # 3. 리스크 및 자산 배분 설정
    # =========================================================
    if group in (None, 3):
        header(3)
        subheader("3-1. 자산 배분/포지션", first=True)
        row("종목당 투자 비중", "전체 자산 대비 한 종목 투자 비율", "SYSTEM_INVEST_PER_STOCK", f"{config.settings.SYSTEM_INVEST_PER_STOCK}", key="SYSTEM_INVEST_PER_STOCK")
        row("최대 보유 종목 수", "포트폴리오 최대 종목 개수", "SYSTEM_MAX_HOLDINGS", f"{config.settings.SYSTEM_MAX_HOLDINGS}", key="SYSTEM_MAX_HOLDINGS")
        row("자동매매 대상에 ETF 포함", "관심종목 내 ETF도 자동매매 대상으로 감시/매수", "SYSTEM_INCLUDE_ETF", f"{getattr(config.settings, 'SYSTEM_INCLUDE_ETF', False)}", key="SYSTEM_INCLUDE_ETF")
        slippage = getattr(config.settings, 'SLIPPAGE_RATE', 0.002)
        slippage_str = f"{slippage} (미사용)" if slippage == 0 else f"{slippage}"
        row("슬리피지 비율", "주문가 보정 및 백테스트 비용", "SLIPPAGE_RATE", slippage_str, key="SLIPPAGE_RATE")
        row("변동성 타겟팅", "ATR 기반 비중 조절 사용 여부", "USE_VOLATILITY_TARGETING", f"{getattr(config.settings, 'USE_VOLATILITY_TARGETING', True)}", key="USE_VOLATILITY_TARGETING")
        if getattr(config.settings, 'USE_VOLATILITY_TARGETING', True):
            row("목표 변동성", "연간 변동성 목표치", "TARGET_VOLATILITY", f"{getattr(config.settings, 'TARGET_VOLATILITY', 0.30)}", key="TARGET_VOLATILITY", indent=True)
            row("스케일링 범위", "비중 조절 최소~최대 배수", "VOLATILITY_SCALING_MIN/MAX", f"{getattr(config.settings, 'VOLATILITY_SCALING_MIN', 0.5)} ~ {getattr(config.settings, 'VOLATILITY_SCALING_MAX', 2.0)}", indent=True)

        subheader("3-2. 매수 필터")
        row("시장 필터링 사용", "지수 하락 시 신규 매수 보류", "USE_MARKET_FILTER", f"{getattr(config.settings, 'USE_MARKET_FILTER', True)}", key="USE_MARKET_FILTER")
        if getattr(config.settings, 'USE_MARKET_FILTER', True):
            row("시장 필터링 SMA (일)", "지수 추세 판단용 단순이동평균선", "MARKET_FILTER_MA", f"{getattr(config.settings, 'MARKET_FILTER_MA', 50)}", key="MARKET_FILTER_MA", indent=True)
        row("상대강도(RS) 필터 사용", "지수 대비 약세 종목 신규 매수 제외", "USE_RS_FILTER", f"{getattr(config.settings, 'USE_RS_FILTER', True)}", key="USE_RS_FILTER")
        row("상관계수 필터링 사용", "유사 테마 종목 중복 매수 방지", "USE_CORRELATION_FILTER", f"{getattr(config.settings, 'USE_CORRELATION_FILTER', True)}", key="USE_CORRELATION_FILTER")
        if getattr(config.settings, 'USE_CORRELATION_FILTER', True):
            row("상관계수 임계값", "동조화 판단 기준치 (0.0~1.0)", "CORRELATION_THRESHOLD", f"{getattr(config.settings, 'CORRELATION_THRESHOLD', 0.7)}", key="CORRELATION_THRESHOLD", indent=True)

        subheader("3-3. 비상 안전장치")
        row("연속 에러 허용", "시스템 중단 임계값", "SYSTEM_MAX_CONSECUTIVE_ERRORS", f"{getattr(config.settings, 'SYSTEM_MAX_CONSECUTIVE_ERRORS', 5)}", key="SYSTEM_MAX_CONSECUTIVE_ERRORS")
        row("일일 손실 제한 (%)", "비상 정지 기준 손실률 (0%면 비상 정지 OFF)", "SYSTEM_DAILY_LOSS_LIMIT", f"{getattr(config.settings, 'SYSTEM_DAILY_LOSS_LIMIT', 10.0)}", key="SYSTEM_DAILY_LOSS_LIMIT")
        row("1회 최대 리스크 (%)", "계좌 대비 1회 매매 최대 손실폭", "SYSTEM_RISK_PER_TRADE", f"{getattr(config.settings, 'SYSTEM_RISK_PER_TRADE', 5.0)}", key="SYSTEM_RISK_PER_TRADE")
        row("총 오픈 리스크 한도 (%)", "보유 전체 동시 손절 잠재손실 상한 (0%면 미사용)", "SYSTEM_MAX_PORTFOLIO_RISK", f"{getattr(config.settings, 'SYSTEM_MAX_PORTFOLIO_RISK', 10.0)}", key="SYSTEM_MAX_PORTFOLIO_RISK")

    # =========================================================
    # 4. 기술적 지표 파라미터
    # =========================================================
    if group in (None, 4):
        ind = config.INDICATOR_PARAMS
        header(4)
        subheader("4-1. 데이터 조회", first=True)
        row("데이터 조회 기간", "일봉 데이터 조회 범위", "INDICATOR_PARAMS['CHART_LOOKBACK_DAYS']", f"{ind.get('CHART_LOOKBACK_DAYS')}일")

        subheader("4-2. 추세")
        row("SAR (Start/Step/Max)", "파라볼릭 SAR 가속변수", "INDICATOR_PARAMS['SAR_AF_START', 'SAR_AF_STEP', 'SAR_AF_MAX']", f"{ind.get('SAR_AF_START')}/{ind.get('SAR_AF_STEP')}/{ind.get('SAR_AF_MAX')}")
        row("MACD (Fast/Slow/Sig)", "이동평균수렴확산", "INDICATOR_PARAMS['MACD_FAST', 'MACD_SLOW', 'MACD_SIGNAL']", f"{ind.get('MACD_FAST')}/{ind.get('MACD_SLOW')}/{ind.get('MACD_SIGNAL')}")
        row("단기 이평선(EMA) 기간", "단기 급등 추세 판단용", "INDICATOR_PARAMS['EMA_SHORT']", f"{ind.get('EMA_SHORT', 5)}")
        row("상승/하락 추세선 기간", "스윙 피봇 연결을 위한 룩백 기간", "INDICATOR_PARAMS['TREND_PERIOD']", f"{ind.get('TREND_PERIOD', 60)}일")

        subheader("4-3. 모멘텀")
        row("RSI (Period/Signal)", "상대강도지수 기간", "INDICATOR_PARAMS['RSI_PERIOD', 'RSI_SIGNAL']", f"{ind.get('RSI_PERIOD')}/{ind.get('RSI_SIGNAL')}")
        row("RSI (Up/Mid/Low)", "과매수/중심/과매도 기준", "INDICATOR_PARAMS['RSI_UPPER', 'RSI_MID', 'RSI_LOWER']", f"{ind.get('RSI_UPPER')}/{ind.get('RSI_MID')}/{ind.get('RSI_LOWER')}")
        row("CCI (Window/Up/Low)", "상품채널지수", "INDICATOR_PARAMS['CCI_WINDOW', 'CCI_UPPER', 'CCI_LOWER']", f"{ind.get('CCI_WINDOW')}/{ind.get('CCI_UPPER')}/{ind.get('CCI_LOWER')}")

        subheader("4-4. 강도/수급/변동성")
        row("ADX 기간", "추세 강도 지표", "INDICATOR_PARAMS['ADX_PERIOD']", f"{ind.get('ADX_PERIOD')}")
        row("OBV EMA 기간", "거래량 추세 지수이동평균", "INDICATOR_PARAMS['OBV_MA_PERIOD']", f"{ind.get('OBV_MA_PERIOD')}")
        row("거래량 이동평균 기간", "수급 추세 및 폭발 판단용", "INDICATOR_PARAMS['VOLUME_MA_PERIOD']", f"{ind.get('VOLUME_MA_PERIOD', 20)}")
        row("거래량 폭발 배수", "이동평균 대비 폭발 기준", "INDICATOR_PARAMS['VOLUME_SPIKE_RATIO']", f"{ind.get('VOLUME_SPIKE_RATIO', 2.0)}")
        row("ATR 기간", "평균 진폭 (변동성)", "INDICATOR_PARAMS['ATR_PERIOD']", f"{ind.get('ATR_PERIOD')}")

        subheader("4-5. 가격 구조")
        row("박스권 탐지 기간", "매물대 기반 박스권 탐지 룩백 봉 수 (일봉=일, 분봉=분)", "INDICATOR_PARAMS['BOX_PERIOD']", f"{ind.get('BOX_PERIOD', 20)}봉")
        row("박스권 매물대 %", "핵심 매물대 집중도", "INDICATOR_PARAMS['BOX_VALUE_AREA_PCT']", f"{ind.get('BOX_VALUE_AREA_PCT', 50.0)}%")

    # =========================================================
    # 5. 환경 및 시스템 설정
    # =========================================================
    if group in (None, 5):
        header(5)
        subheader("5-1. 거래 시간·주기", first=True)
        row("거래 시작 시간", "매매 허용 시작 시각 (HHMM)", "SYSTEM_TRADING_START_TIME", f"{getattr(config.settings, 'SYSTEM_TRADING_START_TIME', '0920')}")
        row("거래 종료 시간", "매매 허용 종료 시각 (HHMM)", "SYSTEM_TRADING_END_TIME", f"{getattr(config.settings, 'SYSTEM_TRADING_END_TIME', '1510')}")
        row("모니터링 주기 (초)", "자동매매 루프 실행 간격", "SYSTEM_TRADING_INTERVAL", f"{getattr(config.settings, 'SYSTEM_TRADING_INTERVAL', 180)}")

        subheader("5-2. 주문·체결 감시")
        row("체결 감시 주기", "주문 직후 체결 확인 간격", "CONCLUSION_CHECK_INTERVAL", f"{getattr(config.settings, 'CONCLUSION_CHECK_INTERVAL', 5)}")
        row("대기 모드 주기", "주문이 없는 평상시 체결 확인 간격", "CONCLUSION_CHECK_IDLE_INTERVAL", f"{getattr(config.settings, 'CONCLUSION_CHECK_IDLE_INTERVAL', 300)}")
        row("집중 감시 시간", "주문 후 집중 감시 유지 시간", "CONCLUSION_CHECK_ACTIVE_DURATION", f"{getattr(config.settings, 'CONCLUSION_CHECK_ACTIVE_DURATION', 60)}")
        row("미체결 취소 대기", "지정가 주문 유지 시간", "UNFILLED_ORDER_CANCEL_SECONDS", f"{getattr(config.settings, 'UNFILLED_ORDER_CANCEL_SECONDS', 120)}")

        subheader("5-3. 데이터·통신")
        row("차트 캐시 시간(분)", "일봉 데이터 메모리 캐시 유지", "CHART_CACHE_TTL_MINUTES", f"{getattr(config.settings, 'CHART_CACHE_TTL_MINUTES', 180)}")
        row("실시간 WebSocket 사용", "KIS 실시간 시세 push(끄면 REST 폴링). 토스 미지원", "USE_WEBSOCKET", f"{getattr(config.settings, 'USE_WEBSOCKET', True)}")

        subheader("5-4. 텔레그램 및 AI 브리핑")
        row("사용 여부", "알림 기능 활성화 여부", "ENABLE_TELEGRAM", f"{getattr(config.settings, 'ENABLE_TELEGRAM', True)}")
        row("인스턴스 이름", "알림 메시지 머리말", "TELEGRAM_INSTANCE_NAME", f"{getattr(config.settings, 'TELEGRAM_INSTANCE_NAME', 'HTS')}")
        row("폴링 타임아웃", "봇 명령어 수신 대기 시간", "TELEGRAM_POLLING_TIMEOUT", f"{getattr(config.settings, 'TELEGRAM_POLLING_TIMEOUT', 10)}")
        row("장전 AI 브리핑", "매일 글로벌 매크로 시황 전송", "AUTO_MORNING_BRIEFING_USE", f"{getattr(config.settings, 'AUTO_MORNING_BRIEFING_USE', False)}")
        if getattr(config.settings, 'AUTO_MORNING_BRIEFING_USE', False):
            row("장전 AI 브리핑 시간", "발송 시각 (HHMM)", "AUTO_MORNING_BRIEFING_TIME", f"{getattr(config.settings, 'AUTO_MORNING_BRIEFING_TIME', '0830')}", indent=True)
        row("서킷브레이커(CB) 알림", "시장 전체 정지 (KIS 대표종목 폴링)", "MARKET_HALT_ALERT_USE", f"{getattr(config.settings, 'MARKET_HALT_ALERT_USE', True)}")
        row("VI 발동 알림", "보유+관심 종목별 발동/해제 (REST 폴링, 기본 OFF)", "MARKET_HALT_VI_USE", f"{getattr(config.settings, 'MARKET_HALT_VI_USE', False)}")

        subheader("5-5. 화면 및 로그")
        row("화면 자동 지우기", "메뉴 이동 시 터미널 클리어", "CLEAR_SCREEN_ON_MENU", f"{getattr(config.settings, 'CLEAR_SCREEN_ON_MENU', False)}")
        row("화면 로그 레벨", "터미널 디버그 출력 레벨", "SCREEN_DEBUG_LEVEL", f"{getattr(config.settings, 'SCREEN_DEBUG_LEVEL', 'OFF')}")
        row("파일 로그 레벨", "로그 파일 저장 레벨", "FILE_DEBUG_LEVEL", f"{getattr(config.settings, 'FILE_DEBUG_LEVEL', 'WARNING')}")

    console.print(table)
    console.print()
    return True

def _edit_config_table(title_source, items_source, check_preset=True):
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

        # 섹션이 2개 이상일 때만 섹션 제목 표시 (단일 섹션은 테이블 제목과 중복)
        show_sections = len({it.get('section') for it in items}) > 1
        for i, item in enumerate(items):
            # Section 구분 (조회 화면과 동일한 이름의 섹션 제목 표시)
            sec = item.get('section')
            if i > 0 and sec != items[i-1].get('section'):
                table.add_section()
            if show_sections and sec and (i == 0 or sec != items[i-1].get('section')):
                table.add_row("", f"[bold dim]{sec}[/]", "", "")

            val = item['get']()
            table.add_row(
                str(i + 1),
                f"{item['desc']}\n[dim]{item['help']}[/dim]",
                item['name'],
                str(val)
            )
        
        console.print(table)
        
        console.print()
        choice = Prompt.ask("\n수정할 항목 번호 선택 [dim](전체: a, 이전: b, 메인: q)[/dim]", choices=[str(i+1) for i in range(len(items))] + ['a', 'b', 'q', 'A', 'B', 'Q'], default='b', show_choices=False)
        console.print()
        
        if choice.lower() in ['b', 'q']:
            break
            
        targets = []
        if choice.lower() == 'a':
            targets = items
        else:
            targets = [items[int(choice)-1]]
            
        changed_in_this_loop = False
        changed_preset_keys = False
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
                        if item.get('name') in DEFAULT_PRESETS.get('default', {}):
                            changed_preset_keys = True
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
                if item.get('name') in DEFAULT_PRESETS.get('default', {}):
                    changed_preset_keys = True
                    
            except Exception as e:
                console.print(f"[red]잘못된 입력입니다: {e}[/red]")
        
        if changed_in_this_loop:
            old_preset = getattr(config.settings, 'ACTIVE_PRESET', 'default')
            if check_preset and changed_preset_keys:
                config.settings.ACTIVE_PRESET = "custom"
            # _save_dynamic_config() 내부의 check_and_update_active_preset()가
            # 변경된 값이 실제로 어떤 프리셋과 일치하는지 재판별하여 ACTIVE_PRESET을 보정한다.
            _save_dynamic_config()
            action_taken = True

            if check_preset and changed_preset_keys:
                # 보정 후의 실제 프리셋을 기준으로 어떤 전환이든(예: 커스텀→기본) 알림/키보드 갱신
                new_preset = getattr(config.settings, 'ACTIVE_PRESET', 'default')
                _notify_preset_switch(old_preset, new_preset)
            
    return action_taken

def _edit_section(title, items_source, prefix):
    """항목 리스트에서 특정 서브그룹(section 번호 접두사)만 골라 편집"""
    def get_items():
        items = items_source() if callable(items_source) else items_source
        return [it for it in items if it.get('section', '').startswith(prefix)]
    return _edit_config_table(title, get_items)

# [추세추종 보호] 추세추종 원칙(이익은 달리게, 청산은 손절/샹들리에 TS/추세이탈로)에 위배될 수 있는
#  청산 설정(고정 익절·반익절·RSI 과열 매도·방어적 반매도)은 시스템 설정 메뉴(메인메뉴 0)에서 숨겨
#  임의 변경을 차단한다. 설정 키와 매도 로직 자체는 내부적으로 그대로 유지되며(기본 OFF),
#  dynamic_config.json 직접 편집 또는 자동매매 메뉴의 '종목별 개별 룰'로만 활성화할 수 있다.
ANTI_TREND_HIDDEN_KEYS = {
    "TAKE_PROFIT_RATE",        # 고정 익절
    "HALF_TAKE_PROFIT_USE",    # 반익절
    "TAKE_PROFIT_RSI",         # RSI 과열 매도
    "SUPER_TAKE_PROFIT_RSI",   # RSI 과열 매도의 슈퍼 모멘텀 완화 기준 (부속 설정)
    "DEFENSIVE_HALF_SELL_USE", # 방어적 반매도
    "USE_MEAN_REVERSION",      # 역매수(낙폭과대 역추세 진입) — 추세추종 청산 체계와 부정합
    "MR_RSI_MAX",              # 역매수 RSI 상한 (부속 설정)
    "MR_DISPARITY_MAX",        # 역매수 이격도 상한 (부속 설정)
    "MR_VOL_STRENGTH",         # 역매수 체결강도 기준 (부속 설정)
    "MR_GRACE_LOSS_RATE",      # 역매수 보유분 점수매도 유예 손실폭 (부속 설정)
}


def _entry_strategy_items():
    """매수/진입 조건 + 서브전략 항목 (섹션 1-1, 1-2)"""
    items = [
        {"desc": "매수 기준 점수", "help": "진입 임계값 (종합 점수)", "name": "BUY_SCORE", "type": "float", "section": "1-1. 기본 진입 조건",
         "get": lambda: config.ANALYSIS_THRESHOLDS["BUY_SCORE"], "set": lambda v: config.ANALYSIS_THRESHOLDS.update({"BUY_SCORE": v})},
        {"desc": "상승 추세 점수", "help": "관망/상승 판단 기준", "name": "RISE_SCORE", "type": "float", "section": "1-1. 기본 진입 조건",
         "get": lambda: config.ANALYSIS_THRESHOLDS["RISE_SCORE"], "set": lambda v: config.ANALYSIS_THRESHOLDS.update({"RISE_SCORE": v})},
        {"desc": "관심 신호 최소 개수", "help": "추세전환 초기신호 N개 이상 시 '관심'(태동) 분류 (0=미사용)", "name": "INTEREST_SIGNAL_MIN", "type": "int", "section": "1-1. 기본 진입 조건",
         "get": lambda: config.ANALYSIS_THRESHOLDS.get("INTEREST_SIGNAL_MIN", 3), "set": lambda v: config.ANALYSIS_THRESHOLDS.update({"INTEREST_SIGNAL_MIN": v})},
        {"desc": "관심 60일선 근접 비율", "help": "60일선의 이 비율 이상이면 '돌파 시도' 신호로 인정 (예: 0.97)", "name": "INTEREST_MA60_NEAR", "type": "float", "section": "1-1. 기본 진입 조건",
         "get": lambda: config.ANALYSIS_THRESHOLDS.get("INTEREST_MA60_NEAR", 0.97), "set": lambda v: config.ANALYSIS_THRESHOLDS.update({"INTEREST_MA60_NEAR": v})},
        {"desc": "매수 허용 RSI 상한", "help": "과열 방지 (이 값보다 낮아야 매수)", "name": "BUY_RSI_MAX", "type": "float", "section": "1-1. 기본 진입 조건",
         "get": lambda: config.ANALYSIS_THRESHOLDS["BUY_RSI_MAX"], "set": lambda v: config.ANALYSIS_THRESHOLDS.update({"BUY_RSI_MAX": v})},
        {"desc": "매수 체결강도 기준", "help": "수급 확인 (이 값 이상이어야 매수)", "name": "BUY_VOL_STRENGTH", "type": "float", "section": "1-1. 기본 진입 조건",
         "get": lambda: config.ANALYSIS_THRESHOLDS.get("BUY_VOL_STRENGTH", 100.0), "set": lambda v: config.ANALYSIS_THRESHOLDS.update({"BUY_VOL_STRENGTH": v})},
        {"desc": "매도잔량비 자동 연동", "help": "체결강도 100% 기준으로 비례하여 매도잔량비를 자동 조정", "name": "AUTO_ADJUST_ASK_BID_RATIO", "type": "bool", "choices": ["y", "n"], "section": "1-1. 기본 진입 조건",
         "get": lambda: config.ANALYSIS_THRESHOLDS.get("AUTO_ADJUST_ASK_BID_RATIO", config.ANALYSIS_THRESHOLDS.get('AUTO_ADJUST_ASK_BID_RATIO', True)), "set": lambda v: config.ANALYSIS_THRESHOLDS.update({"AUTO_ADJUST_ASK_BID_RATIO": v})},
        {"desc": "매도잔량 비율 기준", "help": "가짜 체결강도 방어 (체결강도 100% 기준 비율, 0: 미사용)", "name": "BUY_ASK_BID_RATIO", "type": "float", "section": "1-1. 기본 진입 조건",
         "get": lambda: config.ANALYSIS_THRESHOLDS.get("BUY_ASK_BID_RATIO", config.ANALYSIS_THRESHOLDS.get('BUY_ASK_BID_RATIO', 1.0)), "set": lambda v: config.ANALYSIS_THRESHOLDS.update({"BUY_ASK_BID_RATIO": v})},
        {"desc": "과열 이격도 상한", "help": "20일선 대비 단기 과열 기준 (예: 110.0)", "name": "DISPARITY_UPPER", "type": "float", "section": "1-1. 기본 진입 조건",
         "get": lambda: config.ANALYSIS_THRESHOLDS.get("DISPARITY_UPPER", 110.0), "set": lambda v: config.ANALYSIS_THRESHOLDS.update({"DISPARITY_UPPER": v})},
        {"desc": "침체 이격도 하한", "help": "20일선 대비 과매도 기준 (예: 90.0)", "name": "DISPARITY_LOWER", "type": "float", "section": "1-1. 기본 진입 조건",
         "get": lambda: config.ANALYSIS_THRESHOLDS.get("DISPARITY_LOWER", 90.0), "set": lambda v: config.ANALYSIS_THRESHOLDS.update({"DISPARITY_LOWER": v})},
        {"desc": "역추세(낙폭과대) 사용", "help": "하락장/급락 시 반등 매수", "name": "USE_MEAN_REVERSION", "type": "bool", "choices": ["y", "n"], "section": "1-2. 서브전략 — 역추세",
         "get": lambda: config.ANALYSIS_THRESHOLDS.get("USE_MEAN_REVERSION", False), "set": lambda v: config.ANALYSIS_THRESHOLDS.update({"USE_MEAN_REVERSION": v})},
        {"desc": "역추세 RSI 상한", "help": "과매도 진입 기준 (예: 40)", "name": "MR_RSI_MAX", "type": "float", "section": "1-2. 서브전략 — 역추세",
         "get": lambda: config.ANALYSIS_THRESHOLDS.get("MR_RSI_MAX", 40.0), "set": lambda v: config.ANALYSIS_THRESHOLDS.update({"MR_RSI_MAX": v})},
        {"desc": "역추세 이격도 상한", "help": "20일선 대비 이격도 (예: 90%)", "name": "MR_DISPARITY_MAX", "type": "float", "section": "1-2. 서브전략 — 역추세",
         "get": lambda: config.ANALYSIS_THRESHOLDS.get("MR_DISPARITY_MAX", 90.0), "set": lambda v: config.ANALYSIS_THRESHOLDS.update({"MR_DISPARITY_MAX": v})},
        {"desc": "역추세 최소 체결강도", "help": "바닥 매수세 확인 (예: 120%)", "name": "MR_VOL_STRENGTH", "type": "float", "section": "1-2. 서브전략 — 역추세",
         "get": lambda: config.ANALYSIS_THRESHOLDS.get("MR_VOL_STRENGTH", 120.0), "set": lambda v: config.ANALYSIS_THRESHOLDS.update({"MR_VOL_STRENGTH": v})},
        {"desc": "역매수 유예 손실(%)", "help": "역매수 진입 시 시간청산 기간 내 허용 하락폭", "name": "MR_GRACE_LOSS_RATE", "type": "float", "section": "1-2. 서브전략 — 역추세",
         "get": lambda: config.SELL_STRATEGY.get("MR_GRACE_LOSS_RATE", -5.0), "set": lambda v: config.SELL_STRATEGY.update({"MR_GRACE_LOSS_RATE": v})},
        {"desc": "슈퍼 모멘텀(RSI 유연화) 사용", "help": "주도주 랠리 시 RSI 허용치 상향", "name": "SUPER_MOMENTUM_USE", "type": "bool", "choices": ["y", "n"], "section": "1-2. 서브전략 — 슈퍼 모멘텀",
         "get": lambda: config.ANALYSIS_THRESHOLDS.get("SUPER_MOMENTUM_USE", True), "set": lambda v: config.ANALYSIS_THRESHOLDS.update({"SUPER_MOMENTUM_USE": v})},
        {"desc": "슈퍼 모멘텀 발동 점수", "help": "기준 점수 이상 & 신고가 90% 근접 시 발동", "name": "SUPER_MOMENTUM_SCORE", "type": "float", "section": "1-2. 서브전략 — 슈퍼 모멘텀",
         "get": lambda: config.ANALYSIS_THRESHOLDS.get("SUPER_MOMENTUM_SCORE", 8.5), "set": lambda v: config.ANALYSIS_THRESHOLDS.update({"SUPER_MOMENTUM_SCORE": v})},
        {"desc": "슈퍼 52주 위치 기준", "help": "신고가 근접 여부 (예: 90.0% 이상)", "name": "SUPER_MOMENTUM_W52_POS", "type": "float", "section": "1-2. 서브전략 — 슈퍼 모멘텀",
         "get": lambda: config.ANALYSIS_THRESHOLDS.get("SUPER_MOMENTUM_W52_POS", 90.0), "set": lambda v: config.ANALYSIS_THRESHOLDS.update({"SUPER_MOMENTUM_W52_POS": v})},
        {"desc": "슈퍼 모멘텀 매수 RSI", "help": "발동 시 완화되는 진입 허용 RSI (예: 75.0)", "name": "SUPER_BUY_RSI_MAX", "type": "float", "section": "1-2. 서브전략 — 슈퍼 모멘텀",
         "get": lambda: config.ANALYSIS_THRESHOLDS.get("SUPER_BUY_RSI_MAX", 75.0), "set": lambda v: config.ANALYSIS_THRESHOLDS.update({"SUPER_BUY_RSI_MAX": v})},
        {"desc": "슈퍼 모멘텀 과열 매도 RSI", "help": "추세 유지 시 매도 지연 RSI (예: 85.0)", "name": "SUPER_TAKE_PROFIT_RSI", "type": "float", "section": "1-2. 서브전략 — 슈퍼 모멘텀",
         "get": lambda: config.SELL_STRATEGY.get("SUPER_TAKE_PROFIT_RSI", 85.0), "set": lambda v: config.SELL_STRATEGY.update({"SUPER_TAKE_PROFIT_RSI": v})},
        {"desc": "피라미딩(수익 증액) 사용", "help": "수익으로 추세 검증된 포지션만 증액 (물타기 반대)", "name": "PYRAMIDING_USE", "type": "bool", "choices": ["y", "n"], "section": "1-2. 서브전략 — 피라미딩",
         "get": lambda: config.ANALYSIS_THRESHOLDS.get("PYRAMIDING_USE", True), "set": lambda v: config.ANALYSIS_THRESHOLDS.update({"PYRAMIDING_USE": v})},
        {"desc": "증액 발동 수익률(%)", "help": "이 수익률 이상 & 매수신호 유지 시 증액 (예: 10.0)", "name": "PYRAMIDING_PROFIT_TRIGGER", "type": "float", "section": "1-2. 서브전략 — 피라미딩",
         "get": lambda: config.ANALYSIS_THRESHOLDS.get("PYRAMIDING_PROFIT_TRIGGER", 10.0), "set": lambda v: config.ANALYSIS_THRESHOLDS.update({"PYRAMIDING_PROFIT_TRIGGER": v})},
        {"desc": "증액 비율", "help": "보유 수량 대비 증액 수량 비율 (예: 0.5 = 50%)", "name": "PYRAMIDING_RATIO", "type": "float", "section": "1-2. 서브전략 — 피라미딩",
         "get": lambda: config.ANALYSIS_THRESHOLDS.get("PYRAMIDING_RATIO", 0.5), "set": lambda v: config.ANALYSIS_THRESHOLDS.update({"PYRAMIDING_RATIO": v})},
        {"desc": "최대 증액 횟수", "help": "포지션당 피라미딩 허용 횟수 (기본 1회)", "name": "PYRAMIDING_MAX_COUNT", "type": "int", "section": "1-2. 서브전략 — 피라미딩",
         "get": lambda: config.ANALYSIS_THRESHOLDS.get("PYRAMIDING_MAX_COUNT", 1), "set": lambda v: config.ANALYSIS_THRESHOLDS.update({"PYRAMIDING_MAX_COUNT": v})}
    ]
    # [추가] 토스: 체결강도 미제공 → 체결강도 관련 항목은 편집 목록에서 숨김(미사용 유지)
    #   수급 확인은 매도잔량비(BUY_ASK_BID_RATIO)로 수행하므로 해당 항목은 유지한다.
    if config.session.is_toss:
        _toss_hidden = {"BUY_VOL_STRENGTH", "AUTO_ADJUST_ASK_BID_RATIO", "MR_VOL_STRENGTH"}
        items = [it for it in items if it["name"] not in _toss_hidden]
    # [추세추종 보호] 반추세성 청산 설정은 편집 목록에서 숨김 (ANTI_TREND_HIDDEN_KEYS 주석 참조)
    items = [it for it in items if it["name"] not in ANTI_TREND_HIDDEN_KEYS]
    return items

def modify_analysis_thresholds():
    return _edit_config_table("매수/진입 조건 설정 (ANALYSIS_THRESHOLDS)", _entry_strategy_items)

def _sell_strategy_items():
    """매도/청산 전략 항목 (섹션 1-3 ~ 1-5)

    반추세성 청산 설정(고정 익절/반익절/RSI 과열 매도/방어적 반매도)은
    ANTI_TREND_HIDDEN_KEYS 필터로 목록에서 제외된다 (내부 로직·설정 키는 유지).
    """
    items = [
        {"desc": "익절 수익률(%)", "help": "목표 수익 달성 시 매도 (0: 미사용)", "name": "TAKE_PROFIT_RATE", "type": "float", "section": "1-3. 매도/청산 — 트레일링 스탑",
         "get": lambda: config.SELL_STRATEGY["TAKE_PROFIT_RATE"], "set": lambda v: config.SELL_STRATEGY.update({"TAKE_PROFIT_RATE": v})},
        {"desc": "반익절 사용", "help": "익절 수익률의 절반 도달 시 50% 선매도", "name": "HALF_TAKE_PROFIT_USE", "type": "bool", "choices": ["y", "n"], "section": "1-3. 매도/청산 — 트레일링 스탑",
         "get": lambda: config.SELL_STRATEGY.get("HALF_TAKE_PROFIT_USE", False), "set": lambda v: config.SELL_STRATEGY.update({"HALF_TAKE_PROFIT_USE": v})},
        {"desc": "과열 매도 RSI", "help": "RSI 과열 시 선제 매도", "name": "TAKE_PROFIT_RSI", "type": "float", "section": "1-3. 매도/청산 — 트레일링 스탑",
         "get": lambda: config.SELL_STRATEGY["TAKE_PROFIT_RSI"], "set": lambda v: config.SELL_STRATEGY.update({"TAKE_PROFIT_RSI": v})},
        {"desc": "TS 발동 수익률(%)", "help": "트레일링 스탑 감시 시작점", "name": "TRAILING_STOP_ACTIVATION_RATE", "type": "float", "section": "1-3. 매도/청산 — 트레일링 스탑",
         "get": lambda: config.SELL_STRATEGY.get("TRAILING_STOP_ACTIVATION_RATE", 15.0), "set": lambda v: config.SELL_STRATEGY.update({"TRAILING_STOP_ACTIVATION_RATE": v})},
        {"desc": "TS 하락 감지율(%)", "help": "최고가 대비 하락 시 매도", "name": "TRAILING_STOP_CALLBACK_RATE", "type": "float", "section": "1-3. 매도/청산 — 트레일링 스탑",
         "get": lambda: config.SELL_STRATEGY.get("TRAILING_STOP_CALLBACK_RATE", 4.0), "set": lambda v: config.SELL_STRATEGY.update({"TRAILING_STOP_CALLBACK_RATE": v})},
        {"desc": "손절 수익률(%)", "help": "손실 제한 (Stop Loss) (0: 미사용)", "name": "STOP_LOSS_RATE", "type": "float", "section": "1-4. 매도/청산 — 손절",
         "get": lambda: config.SELL_STRATEGY["STOP_LOSS_RATE"], "set": lambda v: config.SELL_STRATEGY.update({"STOP_LOSS_RATE": v})},
        {"desc": "ATR 손절 사용", "help": "변동성 기반 동적 손절", "name": "USE_ATR_STOP", "type": "bool", "choices": ["y", "n"], "section": "1-4. 매도/청산 — 손절",
         "get": lambda: config.SELL_STRATEGY.get("USE_ATR_STOP", True), "set": lambda v: config.SELL_STRATEGY.update({"USE_ATR_STOP": v})},
        {"desc": "ATR 손절 배수", "help": "ATR * 배수 만큼 손절폭 설정 (0: 미사용)", "name": "ATR_STOP_MULTIPLIER", "type": "float", "section": "1-4. 매도/청산 — 손절",
         "get": lambda: config.SELL_STRATEGY.get("ATR_STOP_MULTIPLIER", 2.0), "set": lambda v: config.SELL_STRATEGY.update({"ATR_STOP_MULTIPLIER": v})},
        {"desc": "ATR 최대 손절률(%)", "help": "데이터 오류 및 과열 변동성으로 인한 과도한 리스크 제한 (0: 미사용)", "name": "MAX_ATR_STOP_LOSS_RATE", "type": "float", "section": "1-4. 매도/청산 — 손절",
         "get": lambda: config.SELL_STRATEGY.get("MAX_ATR_STOP_LOSS_RATE", -15.0), "set": lambda v: config.SELL_STRATEGY.update({"MAX_ATR_STOP_LOSS_RATE": v})},
        {"desc": "본전 청산 발동 수익률(%)", "help": "최고 수익률이 이 값에 도달하면 손절선 상향 (0: 미사용, ATR 사용 시 동적 연동)", "name": "BREAK_EVEN_PROFIT_RATE", "type": "float", "section": "1-4. 매도/청산 — 손절",
         "get": lambda: config.SELL_STRATEGY.get("BREAK_EVEN_PROFIT_RATE", 7.0), "set": lambda v: config.SELL_STRATEGY.update({"BREAK_EVEN_PROFIT_RATE": v})},
        {"desc": "본전 청산 손절선(%)", "help": "본전 청산 발동 시 변경될 손절률 (예: 0.5)", "name": "BREAK_EVEN_STOP_RATE", "type": "float", "section": "1-4. 매도/청산 — 손절",
         "get": lambda: config.SELL_STRATEGY.get("BREAK_EVEN_STOP_RATE", 0.5), "set": lambda v: config.SELL_STRATEGY.update({"BREAK_EVEN_STOP_RATE": v})},
        {"desc": "시간 청산 사용", "help": "장기 횡보 시 강제 매도", "name": "TIME_STOP_USE", "type": "bool", "choices": ["y", "n"], "section": "1-5. 매도/청산 — 기타",
         "get": lambda: config.SELL_STRATEGY.get("TIME_STOP_USE", True), "set": lambda v: config.SELL_STRATEGY.update({"TIME_STOP_USE": v})},
        {"desc": "시간 청산 기준일", "help": "매수 후 제한 일수 (예: 10)", "name": "TIME_STOP_DAYS", "type": "int", "section": "1-5. 매도/청산 — 기타",
         "get": lambda: config.SELL_STRATEGY.get("TIME_STOP_DAYS", 10), "set": lambda v: config.SELL_STRATEGY.update({"TIME_STOP_DAYS": v})},
        {"desc": "시간청산 최소수익(%)", "help": "기간 내 달성해야 할 목표치", "name": "TIME_STOP_MIN_PROFIT_RATE", "type": "float", "section": "1-5. 매도/청산 — 기타",
         "get": lambda: config.SELL_STRATEGY.get("TIME_STOP_MIN_PROFIT_RATE", 3.0), "set": lambda v: config.SELL_STRATEGY.update({"TIME_STOP_MIN_PROFIT_RATE": v})},
        {"desc": "매도(추세이탈) 점수", "help": "점수 하락 시 매도", "name": "SELL_SCORE", "type": "float", "section": "1-5. 매도/청산 — 기타",
         "get": lambda: config.SELL_STRATEGY["SELL_SCORE"], "set": lambda v: config.SELL_STRATEGY.update({"SELL_SCORE": v})},
        {"desc": "방어적 반매도 사용", "help": "SAR 매도 + 5일선 이탈 시 50% 수익실현 및 리스크 회피", "name": "DEFENSIVE_HALF_SELL_USE", "type": "bool", "choices": ["y", "n"], "section": "1-5. 매도/청산 — 기타",
         "get": lambda: config.SELL_STRATEGY.get("DEFENSIVE_HALF_SELL_USE", False), "set": lambda v: config.SELL_STRATEGY.update({"DEFENSIVE_HALF_SELL_USE": v})},
    ]
    # [추세추종 보호] 반추세성 청산 설정은 편집 목록에서 숨김 (ANTI_TREND_HIDDEN_KEYS 주석 참조)
    return [it for it in items if it["name"] not in ANTI_TREND_HIDDEN_KEYS]

def modify_sell_strategy():
    return _edit_config_table("매도/청산 전략 설정 (SELL_STRATEGY)", _sell_strategy_items)

def _indicator_items():
    """기술적 지표 항목 (섹션 4-1 ~ 4-5)"""
    return [
        {"desc": "데이터 조회 기간(일)", "help": "일봉 데이터 조회 범위", "name": "CHART_LOOKBACK_DAYS", "type": "int", "section": "4-1. 데이터 조회",
         "get": lambda: config.INDICATOR_PARAMS["CHART_LOOKBACK_DAYS"], "set": lambda v: config.INDICATOR_PARAMS.update({"CHART_LOOKBACK_DAYS": v})},

        {"desc": "SAR 가속변수 시작(Start)", "help": "파라볼릭 SAR 초기값", "name": "SAR_AF_START", "type": "float", "section": "4-2. 추세",
         "get": lambda: config.INDICATOR_PARAMS["SAR_AF_START"], "set": lambda v: config.INDICATOR_PARAMS.update({"SAR_AF_START": v})},
        {"desc": "SAR 가속변수 증가(Step)", "help": "파라볼릭 SAR 증가값", "name": "SAR_AF_STEP", "type": "float", "section": "4-2. 추세",
         "get": lambda: config.INDICATOR_PARAMS["SAR_AF_STEP"], "set": lambda v: config.INDICATOR_PARAMS.update({"SAR_AF_STEP": v})},
        {"desc": "SAR 가속변수 최대(Max)", "help": "파라볼릭 SAR 최대값", "name": "SAR_AF_MAX", "type": "float", "section": "4-2. 추세",
         "get": lambda: config.INDICATOR_PARAMS["SAR_AF_MAX"], "set": lambda v: config.INDICATOR_PARAMS.update({"SAR_AF_MAX": v})},
        {"desc": "MACD Fast EMA", "help": "단기 지수이동평균", "name": "MACD_FAST", "type": "int", "section": "4-2. 추세",
         "get": lambda: config.INDICATOR_PARAMS["MACD_FAST"], "set": lambda v: config.INDICATOR_PARAMS.update({"MACD_FAST": v})},
        {"desc": "MACD Slow EMA", "help": "장기 지수이동평균", "name": "MACD_SLOW", "type": "int", "section": "4-2. 추세",
         "get": lambda: config.INDICATOR_PARAMS["MACD_SLOW"], "set": lambda v: config.INDICATOR_PARAMS.update({"MACD_SLOW": v})},
        {"desc": "MACD Signal", "help": "시그널 기간", "name": "MACD_SIGNAL", "type": "int", "section": "4-2. 추세",
         "get": lambda: config.INDICATOR_PARAMS["MACD_SIGNAL"], "set": lambda v: config.INDICATOR_PARAMS.update({"MACD_SIGNAL": v})},
        {"desc": "단기 EMA 기간", "help": "단기 급등 추세 판단 (기본 5)", "name": "EMA_SHORT", "type": "int", "section": "4-2. 추세",
         "get": lambda: config.INDICATOR_PARAMS.get("EMA_SHORT", 5), "set": lambda v: config.INDICATOR_PARAMS.update({"EMA_SHORT": v})},
        {"desc": "상승/하락 추세선 기간", "help": "추세선 룩백 기간 (기본 60일)", "name": "TREND_PERIOD", "type": "int", "section": "4-2. 추세",
         "get": lambda: config.INDICATOR_PARAMS.get("TREND_PERIOD", 60), "set": lambda v: config.INDICATOR_PARAMS.update({"TREND_PERIOD": v})},

        {"desc": "RSI 계산 기간", "help": "상대강도지수 기간", "name": "RSI_PERIOD", "type": "int", "section": "4-3. 모멘텀",
         "get": lambda: config.INDICATOR_PARAMS["RSI_PERIOD"], "set": lambda v: config.INDICATOR_PARAMS.update({"RSI_PERIOD": v})},
        {"desc": "RSI 시그널 기간", "help": "RSI 이동평균 기간", "name": "RSI_SIGNAL", "type": "int", "section": "4-3. 모멘텀",
         "get": lambda: config.INDICATOR_PARAMS["RSI_SIGNAL"], "set": lambda v: config.INDICATOR_PARAMS.update({"RSI_SIGNAL": v})},
        {"desc": "RSI 과매수 기준", "help": "이 값 이상이면 과열", "name": "RSI_UPPER", "type": "int", "section": "4-3. 모멘텀",
         "get": lambda: config.INDICATOR_PARAMS["RSI_UPPER"], "set": lambda v: config.INDICATOR_PARAMS.update({"RSI_UPPER": v})},
        {"desc": "RSI 중심선", "help": "강세/약세 기준선", "name": "RSI_MID", "type": "int", "section": "4-3. 모멘텀",
         "get": lambda: config.INDICATOR_PARAMS["RSI_MID"], "set": lambda v: config.INDICATOR_PARAMS.update({"RSI_MID": v})},
        {"desc": "RSI 과매도 기준", "help": "이 값 이하면 침체", "name": "RSI_LOWER", "type": "int", "section": "4-3. 모멘텀",
         "get": lambda: config.INDICATOR_PARAMS["RSI_LOWER"], "set": lambda v: config.INDICATOR_PARAMS.update({"RSI_LOWER": v})},
        {"desc": "CCI 계산 기간", "help": "상품채널지수 기간", "name": "CCI_WINDOW", "type": "int", "section": "4-3. 모멘텀",
         "get": lambda: config.INDICATOR_PARAMS["CCI_WINDOW"], "set": lambda v: config.INDICATOR_PARAMS.update({"CCI_WINDOW": v})},
        {"desc": "CCI 과매수 기준", "help": "이 값 이상이면 과열", "name": "CCI_UPPER", "type": "int", "section": "4-3. 모멘텀",
         "get": lambda: config.INDICATOR_PARAMS["CCI_UPPER"], "set": lambda v: config.INDICATOR_PARAMS.update({"CCI_UPPER": v})},
        {"desc": "CCI 과매도 기준", "help": "이 값 이하면 침체", "name": "CCI_LOWER", "type": "int", "section": "4-3. 모멘텀",
         "get": lambda: config.INDICATOR_PARAMS["CCI_LOWER"], "set": lambda v: config.INDICATOR_PARAMS.update({"CCI_LOWER": v})},

        {"desc": "ADX 계산 기간", "help": "추세 강도 지표", "name": "ADX_PERIOD", "type": "int", "section": "4-4. 강도/수급/변동성",
         "get": lambda: config.INDICATOR_PARAMS["ADX_PERIOD"], "set": lambda v: config.INDICATOR_PARAMS.update({"ADX_PERIOD": v})},
        {"desc": "OBV EMA 기간", "help": "거래량 추세 판단 (지수이동평균)", "name": "OBV_MA_PERIOD", "type": "int", "section": "4-4. 강도/수급/변동성",
         "get": lambda: config.INDICATOR_PARAMS["OBV_MA_PERIOD"], "set": lambda v: config.INDICATOR_PARAMS.update({"OBV_MA_PERIOD": v})},
        {"desc": "거래량 이동평균 기간", "help": "단기 수급 추세 판단 (기본 20)", "name": "VOLUME_MA_PERIOD", "type": "int", "section": "4-4. 강도/수급/변동성",
         "get": lambda: config.INDICATOR_PARAMS.get("VOLUME_MA_PERIOD", 20), "set": lambda v: config.INDICATOR_PARAMS.update({"VOLUME_MA_PERIOD": v})},
        {"desc": "거래량 폭발 배수", "help": "이평선 대비 폭증 기준 (기본 2.0)", "name": "VOLUME_SPIKE_RATIO", "type": "float", "section": "4-4. 강도/수급/변동성",
         "get": lambda: config.INDICATOR_PARAMS.get("VOLUME_SPIKE_RATIO", 2.0), "set": lambda v: config.INDICATOR_PARAMS.update({"VOLUME_SPIKE_RATIO": v})},
        {"desc": "ATR 계산 기간", "help": "평균 진폭 (변동성)", "name": "ATR_PERIOD", "type": "int", "section": "4-4. 강도/수급/변동성",
         "get": lambda: config.INDICATOR_PARAMS.get("ATR_PERIOD", 14), "set": lambda v: config.INDICATOR_PARAMS.update({"ATR_PERIOD": v})},

        {"desc": "박스권 탐지 기간", "help": "매물대 기반 박스권 룩백 봉 수 (기본 20봉, 일봉=일/분봉=분)", "name": "BOX_PERIOD", "type": "int", "section": "4-5. 가격 구조",
         "get": lambda: config.INDICATOR_PARAMS.get("BOX_PERIOD", 20), "set": lambda v: config.INDICATOR_PARAMS.update({"BOX_PERIOD": v})},
        {"desc": "박스권 매물대 %", "help": "핵심 매물대 집중도 (기본 50.0)", "name": "BOX_VALUE_AREA_PCT", "type": "float", "section": "4-5. 가격 구조",
         "get": lambda: config.INDICATOR_PARAMS.get("BOX_VALUE_AREA_PCT", 50.0), "set": lambda v: config.INDICATOR_PARAMS.update({"BOX_VALUE_AREA_PCT": v})}
    ]

def modify_indicator_params():
    return _edit_config_table("기술적 지표 파라미터 (Indicators)", _indicator_items)

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
         "get": lambda: getattr(config.settings, 'ENABLE_TELEGRAM', True), "set": lambda v: setattr(config.settings, 'ENABLE_TELEGRAM', v),
         "callback": _on_telegram_enable_changed},
        {"desc": "인스턴스 이름", "help": "알림 메시지 머리말", "name": "TELEGRAM_INSTANCE_NAME", "type": "str",
         "get": lambda: getattr(config.settings, 'TELEGRAM_INSTANCE_NAME', "HTS"), "set": lambda v: setattr(config.settings, 'TELEGRAM_INSTANCE_NAME', v)},
        {"desc": "폴링 타임아웃(초)", "help": "봇 명령어 수신 대기 시간", "name": "TELEGRAM_POLLING_TIMEOUT", "type": "int",
         "get": lambda: getattr(config.settings, 'TELEGRAM_POLLING_TIMEOUT', 10), "set": lambda v: setattr(config.settings, 'TELEGRAM_POLLING_TIMEOUT', v)},
        {"desc": "장전 AI 브리핑 사용", "help": "매일 지정된 시간에 글로벌 매크로 시황 알림", "name": "AUTO_MORNING_BRIEFING_USE", "type": "bool", "choices": ["y", "n"],
         "get": lambda: getattr(config.settings, 'AUTO_MORNING_BRIEFING_USE', False), "set": lambda v: setattr(config.settings, 'AUTO_MORNING_BRIEFING_USE', v)},
        {"desc": "장전 AI 브리핑 시간", "help": "발송 시각 (예: 0830)", "name": "AUTO_MORNING_BRIEFING_TIME", "type": "time",
         "get": lambda: getattr(config.settings, 'AUTO_MORNING_BRIEFING_TIME', "0830"), "set": lambda v: setattr(config.settings, 'AUTO_MORNING_BRIEFING_TIME', v)},
        {"desc": "서킷브레이커(CB) 알림 사용", "help": "시장 전체 거래정지 감지 (KIS 대표종목 바스켓 REST 폴링)", "name": "MARKET_HALT_ALERT_USE", "type": "bool", "choices": ["y", "n"],
         "get": lambda: getattr(config.settings, 'MARKET_HALT_ALERT_USE', True), "set": lambda v: setattr(config.settings, 'MARKET_HALT_ALERT_USE', v)},
        {"desc": "VI 발동 알림 사용", "help": "보유+관심 종목별 VI 발동/해제 감지 (REST 폴링, 기본 OFF)", "name": "MARKET_HALT_VI_USE", "type": "bool", "choices": ["y", "n"],
         "get": lambda: getattr(config.settings, 'MARKET_HALT_VI_USE', False), "set": lambda v: setattr(config.settings, 'MARKET_HALT_VI_USE', v)}
    ]
    return _edit_config_table("텔레그램 설정 (Telegram)", items)

def modify_log_settings():
    items = [
        {"desc": "화면 자동 지우기", "help": "메뉴 이동 시 터미널 화면 클리어 여부", "name": "CLEAR_SCREEN_ON_MENU", "type": "bool", "choices": ["y", "n"],
         "get": lambda: getattr(config.settings, 'CLEAR_SCREEN_ON_MENU', False), "set": lambda v: setattr(config.settings, 'CLEAR_SCREEN_ON_MENU', v)},
        {"desc": "화면 로그 레벨", "help": "터미널 출력 레벨", "name": "SCREEN_DEBUG_LEVEL", "type": "str", "choices": ["OFF", "ERROR", "TRACE", "DEBUG"],
         "get": lambda: getattr(config.settings, 'SCREEN_DEBUG_LEVEL', "ERROR"), "set": lambda v: setattr(config.settings, 'SCREEN_DEBUG_LEVEL', v)},
        {"desc": "파일 로그 레벨", "help": "로그 파일 저장 레벨", "name": "FILE_DEBUG_LEVEL", "type": "str", "choices": ["DEBUG", "INFO", "WARNING", "ERROR"],
         "get": lambda: getattr(config.settings, 'FILE_DEBUG_LEVEL', "WARNING"), "set": lambda v: setattr(config.settings, 'FILE_DEBUG_LEVEL', v),
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
            table.add_row(label, f"{weights.get(key, def_val)}", desc)
        
        console.print(table)
        
        console.print()
        choice = Prompt.ask("\n수정할 여부 선택 [dim](전체: a, 이전: b, 메인: q)[/dim]", choices=['a', 'b', 'q', 'A', 'B', 'Q'], default='b', show_choices=False)
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
                    current_val = weights.get(key, defaults.get(key, 0.0))
                    prompt_msg = f"{label} [dim][{detail}][/dim] [dim](현재: {current_val})[/dim]"
                    val = Prompt.ask(prompt_msg, default=str(current_val))
                    if val.lower() in ['b', 'q']: 
                        raise ValueError("canceled")
                    new_weights[key] = float(val)
                
                new_total = round(sum(new_weights.values()), 2)

                # 합계는 정확히 10.0점이어야 한다(자동 재계산하지 않음). 미달/초과 시 재입력 안내.
                if abs(new_total - 10.0) > 0.01:
                    console.print(f"\n[bold red]경고: 입력한 값의 합계가 {new_total:.1f}점입니다.[/bold red]")
                    console.print("[yellow]가중치의 합은 정확히 10.0점이 되어야 합니다. 다시 입력해주세요.[/yellow]")
                    continue

                old_preset = getattr(config.settings, 'ACTIVE_PRESET', 'default')
                config.SCORING_WEIGHTS.update(new_weights)

                config.settings.ACTIVE_PRESET = "custom"
                # _save_dynamic_config() 내부에서 실제 매칭 프리셋으로 보정됨
                _save_dynamic_config()
                new_preset = getattr(config.settings, 'ACTIVE_PRESET', 'default')
                _notify_preset_switch(old_preset, new_preset)
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
        {"desc": "강세장 점수 보정", "help": "강세장일 때 기준 점수 조정값 (예: -0.5)", "name": "BULL_SCORE_ADJ", "type": "float",
         "get": lambda: config.MARKET_REGIME_PARAMS["BULL_SCORE_ADJ"], "set": lambda v: config.MARKET_REGIME_PARAMS.update({"BULL_SCORE_ADJ": v})},
        {"desc": "약세장 점수 보정", "help": "약세장일 때 기준 점수 조정값 (예: +0.5)", "name": "BEAR_SCORE_ADJ", "type": "float",
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

def _risk_portfolio_items():
    """리스크/자산 배분 항목 (섹션 3-1 ~ 3-3)"""
    items = [
        {"desc": "종목당 투자 비중", "help": "전체 자산 대비 한 종목 투자 비율 (0.1~1.0)", "name": "SYSTEM_INVEST_PER_STOCK", "type": "float", "section": "3-1. 자산 배분/포지션",
             "get": lambda: config.settings.SYSTEM_INVEST_PER_STOCK, "set": lambda v: setattr(config.settings, 'SYSTEM_INVEST_PER_STOCK', v),
         "validator": lambda v: 0 < v <= 1.0},
        {"desc": "최대 보유 종목 수", "help": "포트폴리오 최대 종목 개수", "name": "SYSTEM_MAX_HOLDINGS", "type": "int", "section": "3-1. 자산 배분/포지션",
             "get": lambda: config.settings.SYSTEM_MAX_HOLDINGS, "set": lambda v: setattr(config.settings, 'SYSTEM_MAX_HOLDINGS', v)},
        {"desc": "자동매매 대상에 ETF 포함", "help": "관심종목 내 ETF도 자동매매 대상으로 감시/매수", "name": "SYSTEM_INCLUDE_ETF", "type": "bool", "choices": ["y", "n"], "section": "3-1. 자산 배분/포지션",
         "get": lambda: getattr(config.settings, 'SYSTEM_INCLUDE_ETF', False), "set": lambda v: setattr(config.settings, 'SYSTEM_INCLUDE_ETF', v)},
        {"desc": "슬리피지 비율", "help": "주문가 보정 및 백테스트 비용", "name": "SLIPPAGE_RATE", "type": "float", "section": "3-1. 자산 배분/포지션",
         "get": lambda: getattr(config.settings, 'SLIPPAGE_RATE', 0.002), "set": lambda v: setattr(config.settings, 'SLIPPAGE_RATE', v)},

        {"desc": "변동성 타겟팅 사용", "help": "ATR 기반 비중 조절 사용 여부", "name": "USE_VOLATILITY_TARGETING", "type": "bool", "choices": ["y", "n"], "section": "3-1. 자산 배분/포지션",
         "get": lambda: getattr(config.settings, 'USE_VOLATILITY_TARGETING', True), "set": lambda v: setattr(config.settings, 'USE_VOLATILITY_TARGETING', v)}
    ]

    if getattr(config.settings, 'USE_VOLATILITY_TARGETING', True):
        items.extend([
            {"desc": "목표 연간 변동성", "help": "0.1=10%, 0.2=20%, 0.3=30%", "name": "TARGET_VOLATILITY", "type": "float", "section": "3-1. 자산 배분/포지션",
             "get": lambda: getattr(config.settings, 'TARGET_VOLATILITY', 0.30), "set": lambda v: setattr(config.settings, 'TARGET_VOLATILITY', v)},
            {"desc": "스케일링 최대 배수", "help": "비중 확대 제한", "name": "VOLATILITY_SCALING_MAX", "type": "float", "section": "3-1. 자산 배분/포지션",
             "get": lambda: getattr(config.settings, 'VOLATILITY_SCALING_MAX', 2.0), "set": lambda v: setattr(config.settings, 'VOLATILITY_SCALING_MAX', v)},
            {"desc": "스케일링 최소 배수", "help": "비중 축소 제한", "name": "VOLATILITY_SCALING_MIN", "type": "float", "section": "3-1. 자산 배분/포지션",
             "get": lambda: getattr(config.settings, 'VOLATILITY_SCALING_MIN', 0.5), "set": lambda v: setattr(config.settings, 'VOLATILITY_SCALING_MIN', v)}
        ])

    items.extend([
        {"desc": "시장 필터링 사용", "help": "지수 하락 시 신규 매수 보류", "name": "USE_MARKET_FILTER", "type": "bool", "choices": ["y", "n"], "section": "3-2. 매수 필터",
         "get": lambda: getattr(config.settings, 'USE_MARKET_FILTER', True), "set": lambda v: setattr(config.settings, 'USE_MARKET_FILTER', v)},
        {"desc": "시장 필터링 SMA (일)", "help": "지수 추세 판단용 단순이동평균선", "name": "MARKET_FILTER_MA", "type": "int", "section": "3-2. 매수 필터",
         "get": lambda: getattr(config.settings, 'MARKET_FILTER_MA', 50), "set": lambda v: setattr(config.settings, 'MARKET_FILTER_MA', v)},
        {"desc": "상대강도(RS) 필터 사용", "help": "6개월 수익률이 소속 지수를 밑도는 종목 신규 매수 제외", "name": "USE_RS_FILTER", "type": "bool", "choices": ["y", "n"], "section": "3-2. 매수 필터",
         "get": lambda: getattr(config.settings, 'USE_RS_FILTER', True), "set": lambda v: setattr(config.settings, 'USE_RS_FILTER', v)},
        {"desc": "상관계수 필터링 사용", "help": "유사 테마 종목 중복 매수 방지", "name": "USE_CORRELATION_FILTER", "type": "bool", "choices": ["y", "n"], "section": "3-2. 매수 필터",
         "get": lambda: getattr(config.settings, 'USE_CORRELATION_FILTER', True), "set": lambda v: setattr(config.settings, 'USE_CORRELATION_FILTER', v)},
        {"desc": "상관계수 임계값", "help": "이 값 이상일 때 동조화로 판단 (0.0~1.0)", "name": "CORRELATION_THRESHOLD", "type": "float", "section": "3-2. 매수 필터",
         "get": lambda: getattr(config.settings, 'CORRELATION_THRESHOLD', 0.7), "set": lambda v: setattr(config.settings, 'CORRELATION_THRESHOLD', v)},

        {"desc": "연속 에러 허용", "help": "시스템 중단 임계값", "name": "SYSTEM_MAX_CONSECUTIVE_ERRORS", "type": "int", "section": "3-3. 비상 안전장치",
         "get": lambda: getattr(config.settings, 'SYSTEM_MAX_CONSECUTIVE_ERRORS', 5), "set": lambda v: setattr(config.settings, 'SYSTEM_MAX_CONSECUTIVE_ERRORS', v)},
        {"desc": "일일 손실 제한 (%)", "help": "비상 정지 기준 손실률 (0%면 비상 정지 OFF)", "name": "SYSTEM_DAILY_LOSS_LIMIT", "type": "float", "section": "3-3. 비상 안전장치",
         "get": lambda: getattr(config.settings, 'SYSTEM_DAILY_LOSS_LIMIT', 10.0), "set": lambda v: setattr(config.settings, 'SYSTEM_DAILY_LOSS_LIMIT', v)},
        {"desc": "1회 최대 리스크 (%)", "help": "계좌 대비 1회 매매 최대 손실폭", "name": "SYSTEM_RISK_PER_TRADE", "type": "float", "section": "3-3. 비상 안전장치",
         "get": lambda: getattr(config.settings, 'SYSTEM_RISK_PER_TRADE', 5.0), "set": lambda v: setattr(config.settings, 'SYSTEM_RISK_PER_TRADE', v)},
        {"desc": "총 오픈 리스크 한도 (%)", "help": "보유 전체 '현재가→손절선' 잠재손실 합의 상한 (0%면 미사용)", "name": "SYSTEM_MAX_PORTFOLIO_RISK", "type": "float", "section": "3-3. 비상 안전장치",
         "get": lambda: getattr(config.settings, 'SYSTEM_MAX_PORTFOLIO_RISK', 10.0), "set": lambda v: setattr(config.settings, 'SYSTEM_MAX_PORTFOLIO_RISK', v)},
    ])
    return items

def modify_risk_portfolio_settings():
    return _edit_config_table("리스크 및 자산 배분 설정 (Risk & Portfolio)", _risk_portfolio_items)

def _trading_cycle_items():
    """트레이딩 시간/주기/통신 항목 (섹션 5-1 ~ 5-3)"""
    return [
        {"desc": "거래 시작 시간", "help": "매매 허용 시작 시각 (HHMM)", "name": "SYSTEM_TRADING_START_TIME", "type": "time", "section": "5-1. 거래 시간·주기",
         "get": lambda: getattr(config.settings, 'SYSTEM_TRADING_START_TIME', "0800"), "set": lambda v: setattr(config.settings, 'SYSTEM_TRADING_START_TIME', v)},
        {"desc": "거래 종료 시간", "help": "매매 허용 종료 시각 (HHMM)", "name": "SYSTEM_TRADING_END_TIME", "type": "time", "section": "5-1. 거래 시간·주기",
         "get": lambda: getattr(config.settings, 'SYSTEM_TRADING_END_TIME', "2000"), "set": lambda v: setattr(config.settings, 'SYSTEM_TRADING_END_TIME', v)},
        {"desc": "모니터링 주기 (초)", "help": "자동매매 루프 실행 간격", "name": "SYSTEM_TRADING_INTERVAL", "type": "int", "section": "5-1. 거래 시간·주기",
         "get": lambda: getattr(config.settings, 'SYSTEM_TRADING_INTERVAL', 180), "set": lambda v: setattr(config.settings, 'SYSTEM_TRADING_INTERVAL', v)},

        {"desc": "체결 감시 주기(초)", "help": "주문 직후 체결 확인 간격", "name": "CONCLUSION_CHECK_INTERVAL", "type": "int", "section": "5-2. 주문·체결 감시",
         "get": lambda: getattr(config.settings, 'CONCLUSION_CHECK_INTERVAL', 5), "set": lambda v: setattr(config.settings, 'CONCLUSION_CHECK_INTERVAL', v)},
        {"desc": "대기 모드 주기(초)", "help": "주문이 없는 평상시 체결 확인 간격", "name": "CONCLUSION_CHECK_IDLE_INTERVAL", "type": "int", "section": "5-2. 주문·체결 감시",
         "get": lambda: getattr(config.settings, 'CONCLUSION_CHECK_IDLE_INTERVAL', 300), "set": lambda v: setattr(config.settings, 'CONCLUSION_CHECK_IDLE_INTERVAL', v)},
        {"desc": "집중 감시 시간(초)", "help": "주문 후 집중 감시 유지 시간", "name": "CONCLUSION_CHECK_ACTIVE_DURATION", "type": "int", "section": "5-2. 주문·체결 감시",
         "get": lambda: getattr(config.settings, 'CONCLUSION_CHECK_ACTIVE_DURATION', 60), "set": lambda v: setattr(config.settings, 'CONCLUSION_CHECK_ACTIVE_DURATION', v)},
        {"desc": "미체결 취소 대기(초)", "help": "지정가 주문 유지 시간", "name": "UNFILLED_ORDER_CANCEL_SECONDS", "type": "int", "section": "5-2. 주문·체결 감시",
         "get": lambda: getattr(config.settings, 'UNFILLED_ORDER_CANCEL_SECONDS', 120), "set": lambda v: setattr(config.settings, 'UNFILLED_ORDER_CANCEL_SECONDS', v)},

        {"desc": "차트 데이터 캐시(분)", "help": "일봉 메모리 캐시 유지 시간", "name": "CHART_CACHE_TTL_MINUTES", "type": "int", "section": "5-3. 데이터·통신",
         "get": lambda: getattr(config.settings, 'CHART_CACHE_TTL_MINUTES', 180), "set": lambda v: setattr(config.settings, 'CHART_CACHE_TTL_MINUTES', v)},
        {"desc": "실시간 WebSocket 사용", "help": "KIS 실시간 시세 push 사용(끄면 REST 폴링). 미구독/끊김 시 자동 REST 폴백. 토스는 미지원", "name": "USE_WEBSOCKET", "type": "bool", "choices": ["y", "n"], "section": "5-3. 데이터·통신",
         "get": lambda: getattr(config.settings, 'USE_WEBSOCKET', True), "set": lambda v: setattr(config.settings, 'USE_WEBSOCKET', v)},
    ]

def modify_trading_cycle_settings():
    return _edit_config_table("트레이딩 시간 및 주기 (Time & Cycle)", _trading_cycle_items)

# =========================================================
# [추가] 전략 프리셋 커스텀 (JSON 저장) 기능
# =========================================================
DEFAULT_PRESETS = {
    "bull": {
        "BUY_SCORE": 7.0, "BUY_RSI_MAX": 75.0, "BUY_VOL_STRENGTH": 95.0, "BUY_ASK_BID_RATIO": 1.0, "AUTO_ADJUST_ASK_BID_RATIO": True, "USE_MEAN_REVERSION": False, "MR_RSI_MAX": 40.0, "MR_VOL_STRENGTH": 100.0, "SUPER_MOMENTUM_USE": True,
        "TAKE_PROFIT_RATE": 0.0, "HALF_TAKE_PROFIT_USE": False, "DEFENSIVE_HALF_SELL_USE": False, "STOP_LOSS_RATE": -7.0, "USE_ATR_STOP": True, "ATR_STOP_MULTIPLIER": 2.0, "MAX_ATR_STOP_LOSS_RATE": -15.0, "BREAK_EVEN_PROFIT_RATE": 5.0, "BREAK_EVEN_STOP_RATE": 0.5, "TIME_STOP_DAYS": 25, "SELL_SCORE": 4.0, "TAKE_PROFIT_RSI": 0.0, "TRAILING_STOP_ACTIVATION_RATE": 10.0, "TRAILING_STOP_CALLBACK_RATE": 5.0, "TRAILING_ATR_MULTIPLIER": 3.0,
        "TREND": 4.5, "MOMENTUM": 2.5, "STRENGTH": 1.0, "SYNERGY": 2.0,
        "SYSTEM_INVEST_PER_STOCK": 0.25, "SYSTEM_DAILY_LOSS_LIMIT": 10.0, "USE_MARKET_FILTER": True, "MARKET_FILTER_MA": 50
    },
    "bear": {
        "BUY_SCORE": 8.0, "BUY_RSI_MAX": 65.0, "BUY_VOL_STRENGTH": 105.0, "BUY_ASK_BID_RATIO": 1.0, "AUTO_ADJUST_ASK_BID_RATIO": True, "USE_MEAN_REVERSION": False, "MR_RSI_MAX": 30.0, "MR_VOL_STRENGTH": 110.0, "SUPER_MOMENTUM_USE": False,
        "TAKE_PROFIT_RATE": 0.0, "HALF_TAKE_PROFIT_USE": False, "DEFENSIVE_HALF_SELL_USE": False, "STOP_LOSS_RATE": -3.0, "USE_ATR_STOP": True, "ATR_STOP_MULTIPLIER": 1.5, "MAX_ATR_STOP_LOSS_RATE": -15.0, "BREAK_EVEN_PROFIT_RATE": 5.0, "BREAK_EVEN_STOP_RATE": 0.5, "TIME_STOP_DAYS": 3, "SELL_SCORE": 6.0, "TAKE_PROFIT_RSI": 0.0, "TRAILING_STOP_ACTIVATION_RATE": 4.0, "TRAILING_STOP_CALLBACK_RATE": 2.0, "TRAILING_ATR_MULTIPLIER": 2.0,
        "TREND": 3.0, "MOMENTUM": 3.0, "STRENGTH": 2.0, "SYNERGY": 2.0,
        "SYSTEM_INVEST_PER_STOCK": 0.25, "SYSTEM_DAILY_LOSS_LIMIT": 5.0, "USE_MARKET_FILTER": True, "MARKET_FILTER_MA": 20
    },
    "sideways": {
        "BUY_SCORE": 7.0, "BUY_RSI_MAX": 50.0, "BUY_VOL_STRENGTH": 100.0, "BUY_ASK_BID_RATIO": 1.0, "AUTO_ADJUST_ASK_BID_RATIO": True, "USE_MEAN_REVERSION": False, "MR_RSI_MAX": 40.0, "MR_VOL_STRENGTH": 105.0, "SUPER_MOMENTUM_USE": False,
        "TAKE_PROFIT_RATE": 0.0, "HALF_TAKE_PROFIT_USE": False, "DEFENSIVE_HALF_SELL_USE": False, "STOP_LOSS_RATE": -5.0, "USE_ATR_STOP": True, "ATR_STOP_MULTIPLIER": 1.8, "MAX_ATR_STOP_LOSS_RATE": -15.0, "BREAK_EVEN_PROFIT_RATE": 5.0, "BREAK_EVEN_STOP_RATE": 0.5, "TIME_STOP_DAYS": 5, "SELL_SCORE": 5.0, "TAKE_PROFIT_RSI": 0.0, "TRAILING_STOP_ACTIVATION_RATE": 7.0, "TRAILING_STOP_CALLBACK_RATE": 3.0, "TRAILING_ATR_MULTIPLIER": 2.5,
        "TREND": 3.5, "MOMENTUM": 3.0, "STRENGTH": 1.5, "SYNERGY": 2.0,
        "SYSTEM_INVEST_PER_STOCK": 0.25, "SYSTEM_DAILY_LOSS_LIMIT": 7.0, "USE_MARKET_FILTER": True, "MARKET_FILTER_MA": 20
    },
    "default": {
        "BUY_SCORE": 7.0, "BUY_RSI_MAX": 70.0, "BUY_VOL_STRENGTH": 100.0, "BUY_ASK_BID_RATIO": 1.0, "AUTO_ADJUST_ASK_BID_RATIO": True, "USE_MEAN_REVERSION": False, "MR_RSI_MAX": 40.0, "MR_VOL_STRENGTH": 120.0, "SUPER_MOMENTUM_USE": True,
        "TAKE_PROFIT_RATE": 0.0, "HALF_TAKE_PROFIT_USE": False, "DEFENSIVE_HALF_SELL_USE": False, "STOP_LOSS_RATE": -7.0, "USE_ATR_STOP": True, "ATR_STOP_MULTIPLIER": 2.0, "MAX_ATR_STOP_LOSS_RATE": -15.0, "BREAK_EVEN_PROFIT_RATE": 5.0, "BREAK_EVEN_STOP_RATE": 0.5, "TIME_STOP_DAYS": 20, "SELL_SCORE": 4.0, "TAKE_PROFIT_RSI": 0.0, "TRAILING_STOP_ACTIVATION_RATE": 10.0, "TRAILING_STOP_CALLBACK_RATE": 5.0, "TRAILING_ATR_MULTIPLIER": 3.0,
        "TREND": 4.0, "MOMENTUM": 2.5, "STRENGTH": 1.5, "SYNERGY": 2.0,
        "SYSTEM_INVEST_PER_STOCK": 0.25, "SYSTEM_DAILY_LOSS_LIMIT": 10.0, "USE_MARKET_FILTER": True, "MARKET_FILTER_MA": 60
    }
}

def load_custom_presets():
    path = getattr(config, 'PRESETS_FILE', os.path.join(config.JSON_DIR, "presets.json"))
    return jsonio.load_json(path, default={}) or {}

def save_custom_presets(presets):
    path = getattr(config, 'PRESETS_FILE', os.path.join(config.JSON_DIR, "presets.json"))
    if not jsonio.save_json(path, presets):
        console.print("[red]프리셋 저장 실패 (상세는 로그 참조)[/red]")

def get_preset_values(preset_type):
    base = DEFAULT_PRESETS.get(preset_type, {}).copy()
    customs = load_custom_presets().get(preset_type, {})
    base.update(customs)
    return base

def check_and_update_active_preset():
    """현재 전략 설정값들이 특정 프리셋과 일치하는지 확인하고 ACTIVE_PRESET을 동적으로 갱신합니다."""
    current_vals = {}
    for key in DEFAULT_PRESETS['default'].keys():
        if key in config.ANALYSIS_THRESHOLDS:
            current_vals[key] = config.ANALYSIS_THRESHOLDS[key]
        elif key in config.SELL_STRATEGY:
            current_vals[key] = config.SELL_STRATEGY[key]
        elif key in config.SCORING_WEIGHTS:
            current_vals[key] = config.SCORING_WEIGHTS[key]
        elif key in config.MARKET_REGIME_PARAMS:
            current_vals[key] = config.MARKET_REGIME_PARAMS[key]
        else:
            current_vals[key] = getattr(config.settings, key, None)
            
    matched_preset = "custom"
    
    for p_name in ['bull', 'bear', 'sideways', 'default']:
        p_vals = get_preset_values(p_name)
        is_match = True
        for k, v in p_vals.items():
            curr_v = current_vals.get(k)
            if isinstance(v, (int, float)) and isinstance(curr_v, (int, float)):
                if abs(float(v) - float(curr_v)) > 1e-5:
                    is_match = False; break
            elif v != curr_v:
                is_match = False; break
                
        if is_match:
            matched_preset = p_name
            break
            
    if getattr(config.settings, 'ACTIVE_PRESET', 'default') != matched_preset:
        config.settings.ACTIVE_PRESET = matched_preset
        path = os.path.join(config.JSON_DIR, "dynamic_config.json")
        data = jsonio.load_json(path)
        if data is not None:
            data['ACTIVE_PRESET'] = matched_preset
            jsonio.save_json(path, data)
            
    return matched_preset

def apply_strategy_preset(preset_type="bull", interactive=True):
    if preset_type not in DEFAULT_PRESETS:
        return "⚠️ 알 수 없는 프리셋입니다. (bull/bear/sideways/default 중 선택)"
        
    vals = get_preset_values(preset_type)
    
    config.ANALYSIS_THRESHOLDS.update({
        "BUY_SCORE": vals["BUY_SCORE"],
        "BUY_RSI_MAX": vals["BUY_RSI_MAX"],
        "BUY_VOL_STRENGTH": vals.get("BUY_VOL_STRENGTH", 100.0),
        "BUY_ASK_BID_RATIO": vals.get("BUY_ASK_BID_RATIO", 1.0),
        "AUTO_ADJUST_ASK_BID_RATIO": vals.get("AUTO_ADJUST_ASK_BID_RATIO", True),
        "USE_MEAN_REVERSION": vals["USE_MEAN_REVERSION"],
        "MR_RSI_MAX": vals["MR_RSI_MAX"],
        "MR_VOL_STRENGTH": vals.get("MR_VOL_STRENGTH", 100.0),
        "SUPER_MOMENTUM_USE": vals["SUPER_MOMENTUM_USE"]
    })
    config.SELL_STRATEGY.update({
        "TAKE_PROFIT_RATE": vals["TAKE_PROFIT_RATE"],
        "HALF_TAKE_PROFIT_USE": vals["HALF_TAKE_PROFIT_USE"],
        "DEFENSIVE_HALF_SELL_USE": vals.get("DEFENSIVE_HALF_SELL_USE", False),
        "STOP_LOSS_RATE": vals["STOP_LOSS_RATE"],
        "USE_ATR_STOP": vals.get("USE_ATR_STOP", True),
        "ATR_STOP_MULTIPLIER": vals.get("ATR_STOP_MULTIPLIER", 2.0),
        "MAX_ATR_STOP_LOSS_RATE": vals.get("MAX_ATR_STOP_LOSS_RATE", -15.0),
        "BREAK_EVEN_PROFIT_RATE": vals.get("BREAK_EVEN_PROFIT_RATE", 7.0),
        "BREAK_EVEN_STOP_RATE": vals.get("BREAK_EVEN_STOP_RATE", 0.5),
        "TIME_STOP_DAYS": vals["TIME_STOP_DAYS"],
        "SELL_SCORE": vals.get("SELL_SCORE", 4.0),
        "TAKE_PROFIT_RSI": vals["TAKE_PROFIT_RSI"],
        "TRAILING_STOP_ACTIVATION_RATE": vals["TRAILING_STOP_ACTIVATION_RATE"],
        "TRAILING_STOP_CALLBACK_RATE": vals["TRAILING_STOP_CALLBACK_RATE"],
        "TRAILING_ATR_MULTIPLIER": vals.get("TRAILING_ATR_MULTIPLIER", 3.0)
    })
    config.SCORING_WEIGHTS.pop("MOMENTUM_PRICE", None)  # 구버전 잔여 키 제거
    config.SCORING_WEIGHTS.update({
        "TREND": vals["TREND"],
        "MOMENTUM": vals["MOMENTUM"],
        "STRENGTH": vals["STRENGTH"],
        "SYNERGY": vals["SYNERGY"],
    })
    config.settings.SYSTEM_INVEST_PER_STOCK = vals["SYSTEM_INVEST_PER_STOCK"]
    config.settings.SYSTEM_DAILY_LOSS_LIMIT = vals["SYSTEM_DAILY_LOSS_LIMIT"]
    config.settings.USE_MARKET_FILTER = vals["USE_MARKET_FILTER"]
    config.settings.MARKET_FILTER_MA = vals["MARKET_FILTER_MA"]
    
    config.settings.ACTIVE_PRESET = preset_type
    
    msg = ""
    if preset_type == "bull":
        msg = "🔴 [강세장(Bull)] 전략 프리셋이 적용되었습니다.\n(주도주 돌파매매 및 추세 추종에 최적화)"
    elif preset_type == "bear":
        msg = "🔵 [약세장(Bear)] 전략 프리셋이 적용되었습니다.\n(보수적 퀀트 기준 적용 및 하락장 낙폭과대 수급(Volume) 유입 포착에 집중)"
    elif preset_type == "sideways":
        msg = "🟡 [횡보장(Sideways)] 전략 프리셋이 적용되었습니다.\n(과매도권(CCI/RSI) 선행 탈출 및 거래량 스파이크 포착에 최적화)"
    elif preset_type == "default":
        msg = "🟢 [기본설정(Default)] 전략 프리셋이 적용되었습니다.\n(시스템 권장 설정으로 복귀)"
        
    _save_dynamic_config()
    
    # 변경된 주요 설정값 요약 데이터 구성
    summary_data = [
        ("매수 허들 (점수/RSI/체결/잔량비)", f"{config.ANALYSIS_THRESHOLDS['BUY_SCORE']}점 이상 / RSI {config.ANALYSIS_THRESHOLDS['BUY_RSI_MAX']} 미만 / 체결 {config.ANALYSIS_THRESHOLDS.get('BUY_VOL_STRENGTH', 100.0)}%↑ / 잔량비 {config.ANALYSIS_THRESHOLDS.get('BUY_ASK_BID_RATIO', 1.0)}배↑ (자동연동: {'ON' if config.ANALYSIS_THRESHOLDS.get('AUTO_ADJUST_ASK_BID_RATIO', True) else 'OFF'})"),
        ("슈퍼 모멘텀 (돌파매수)", f"{'ON' if config.ANALYSIS_THRESHOLDS['SUPER_MOMENTUM_USE'] else 'OFF'}"),
        ("매도 허들 (추세이탈)", f"점수 {config.SELL_STRATEGY.get('SELL_SCORE', 4.0)} 미만+60일선 이탈"),
        ("손절", f"{config.SELL_STRATEGY['STOP_LOSS_RATE']}% (ATR x{config.SELL_STRATEGY.get('ATR_STOP_MULTIPLIER', 2.0)})"),
        ("트레일링 스탑", f"+{config.SELL_STRATEGY.get('TRAILING_STOP_ACTIVATION_RATE', 10.0)}% 발동 후 -{config.SELL_STRATEGY.get('TRAILING_STOP_CALLBACK_RATE', 3.0)}%"),
        ("본전 청산 (방어)", f"수익 +{config.SELL_STRATEGY.get('BREAK_EVEN_PROFIT_RATE', 7.0)}% 도달 시 손절선 +{config.SELL_STRATEGY.get('BREAK_EVEN_STOP_RATE', 0.5)}%로 상향"),
        ("시간 청산", f"{config.SELL_STRATEGY['TIME_STOP_DAYS']}일 경과 시 강제 매도"),
        ("안전 장치 (비상정지/필터)", f"일일손실 -{getattr(config, 'SYSTEM_DAILY_LOSS_LIMIT', 10.0)}% 제한 / 시장필터 {'ON ('+str(getattr(config, 'MARKET_FILTER_MA', 20))+'일선)' if getattr(config, 'USE_MARKET_FILTER', True) else 'OFF (무조건 진입)'}"),
        ("스코어링 가중치", f"추세 {config.SCORING_WEIGHTS['TREND']} / 모멘텀 {config.SCORING_WEIGHTS['MOMENTUM']} / 강도 {config.SCORING_WEIGHTS['STRENGTH']} / 시너지 {config.SCORING_WEIGHTS['SYNERGY']}"),
        ("종목당 투자 비중", f"{config.settings.SYSTEM_INVEST_PER_STOCK * 100:.0f}%")
    ]
    
    if interactive:
        console.print(f"\n{msg}")
        
        console.print("[dim]" + "─" * 75 + "[/dim]")
        table = Table(box=box.SIMPLE, show_header=False, border_style="dim", padding=(0, 2))
        table.add_column("항목", style="cyan", justify="left")
        table.add_column("설정값", justify="left")
        for k, v in summary_data:
            table.add_row(k, v)
        console.print(table)
        console.print("[dim]" + "─" * 75 + "[/dim]")
        
        console.print("[dim]설정 메뉴에서 각 세부 항목을 다시 조정할 수 있습니다.[/dim]")
        
    detail_msg = "\n\n[적용된 주요 설정]\n" + "\n".join([f"• {k}: {v}" for k, v in summary_data])
    
    if interactive:
        try:
            if getattr(config, 'ENABLE_TELEGRAM', True):
                from modules.telegram_bot import TelegramCommander
                TelegramCommander()._send_reply(msg + detail_msg)
        except Exception:
            pass
            
    return msg + (detail_msg if not interactive else "")

def _edit_single_preset(preset_type):
    name_map = {"bull": "강세장", "bear": "약세장", "sideways": "횡보장", "default": "기본설정"}
    title = f"{name_map[preset_type]} 프리셋 세부 설정 조정"
    
    while True:
        vals = get_preset_values(preset_type)
        
        def make_setter(key, typ):
            def setter(v):
                all_presets = load_custom_presets()
                if preset_type not in all_presets:
                    all_presets[preset_type] = {}
                if typ == 'float': v = float(v)
                elif typ == 'int': v = int(v)
                elif typ == 'bool': v = bool(v)
                all_presets[preset_type][key] = v
                save_custom_presets(all_presets)
            return setter
            
        def make_getter(key):
            return lambda: get_preset_values(preset_type)[key]

        items = [
            {"desc": "매수 기준 점수", "help": "진입 임계값", "name": "BUY_SCORE", "type": "float", "section": "Buy", "get": make_getter("BUY_SCORE"), "set": make_setter("BUY_SCORE", 'float')},
            {"desc": "매수 허용 RSI 상한", "help": "과열 방지", "name": "BUY_RSI_MAX", "type": "float", "section": "Buy", "get": make_getter("BUY_RSI_MAX"), "set": make_setter("BUY_RSI_MAX", 'float')},
            {"desc": "매수 체결강도 기준", "help": "매수 수급 확인", "name": "BUY_VOL_STRENGTH", "type": "float", "section": "Buy", "get": make_getter("BUY_VOL_STRENGTH"), "set": make_setter("BUY_VOL_STRENGTH", 'float')},
            {"desc": "매도잔량비 자동 연동", "help": "체결강도 100% 기준 비례 자동조정", "name": "AUTO_ADJUST_ASK_BID_RATIO", "type": "bool", "choices": ["y", "n"], "section": "Buy", "get": make_getter("AUTO_ADJUST_ASK_BID_RATIO"), "set": make_setter("AUTO_ADJUST_ASK_BID_RATIO", 'bool')},
            {"desc": "매도잔량 비율 기준", "help": "가짜 체결강도 방어 (체결강도 100% 기준 배수)", "name": "BUY_ASK_BID_RATIO", "type": "float", "section": "Buy", "get": make_getter("BUY_ASK_BID_RATIO"), "set": make_setter("BUY_ASK_BID_RATIO", 'float')},
            {"desc": "역추세 매수 사용", "help": "낙폭과대 반등", "name": "USE_MEAN_REVERSION", "type": "bool", "choices": ["y", "n"], "section": "Buy", "get": make_getter("USE_MEAN_REVERSION"), "set": make_setter("USE_MEAN_REVERSION", 'bool')},
            {"desc": "역추세 RSI 상한", "help": "과매도 진입 기준", "name": "MR_RSI_MAX", "type": "float", "section": "Buy", "get": make_getter("MR_RSI_MAX"), "set": make_setter("MR_RSI_MAX", 'float')},
            {"desc": "역추세 체결강도 기준", "help": "바닥권 매수세 확인", "name": "MR_VOL_STRENGTH", "type": "float", "section": "Buy", "get": make_getter("MR_VOL_STRENGTH"), "set": make_setter("MR_VOL_STRENGTH", 'float')},
            {"desc": "슈퍼 모멘텀 사용", "help": "주도주 랠리 시 RSI 완화", "name": "SUPER_MOMENTUM_USE", "type": "bool", "choices": ["y", "n"], "section": "Buy", "get": make_getter("SUPER_MOMENTUM_USE"), "set": make_setter("SUPER_MOMENTUM_USE", 'bool')},
            
            {"desc": "익절 수익률(%)", "help": "목표 수익", "name": "TAKE_PROFIT_RATE", "type": "float", "section": "Sell", "get": make_getter("TAKE_PROFIT_RATE"), "set": make_setter("TAKE_PROFIT_RATE", 'float')},
            {"desc": "반익절 사용 여부", "help": "절반 수익 도달 시 절반 매도", "name": "HALF_TAKE_PROFIT_USE", "type": "bool", "choices": ["y", "n"], "section": "Sell", "get": make_getter("HALF_TAKE_PROFIT_USE"), "set": make_setter("HALF_TAKE_PROFIT_USE", 'bool')},
            {"desc": "방어적 반매도 사용", "help": "SAR 매도 + 5일선 이탈 시 50% 수익실현 및 리스크 회피", "name": "DEFENSIVE_HALF_SELL_USE", "type": "bool", "choices": ["y", "n"], "section": "Sell", "get": make_getter("DEFENSIVE_HALF_SELL_USE"), "set": make_setter("DEFENSIVE_HALF_SELL_USE", 'bool')},
            {"desc": "손절 수익률(%)", "help": "기본 손절 라인", "name": "STOP_LOSS_RATE", "type": "float", "section": "Sell", "get": make_getter("STOP_LOSS_RATE"), "set": make_setter("STOP_LOSS_RATE", 'float')},
            {"desc": "ATR 손절 배수", "help": "동적 손절 배수 설정", "name": "ATR_STOP_MULTIPLIER", "type": "float", "section": "Sell", "get": make_getter("ATR_STOP_MULTIPLIER"), "set": make_setter("ATR_STOP_MULTIPLIER", 'float')},
        {"desc": "ATR 최대 손절률(%)", "help": "과열 변동성으로 인한 과도한 리스크 제한", "name": "MAX_ATR_STOP_LOSS_RATE", "type": "float", "section": "Sell", "get": make_getter("MAX_ATR_STOP_LOSS_RATE"), "set": make_setter("MAX_ATR_STOP_LOSS_RATE", 'float')},
            {"desc": "본전 청산 발동(%)", "help": "손절선 상향 기준 수익률", "name": "BREAK_EVEN_PROFIT_RATE", "type": "float", "section": "Sell", "get": make_getter("BREAK_EVEN_PROFIT_RATE"), "set": make_setter("BREAK_EVEN_PROFIT_RATE", 'float')},
            {"desc": "본전 청산 손절선(%)", "help": "상향될 새 손절선", "name": "BREAK_EVEN_STOP_RATE", "type": "float", "section": "Sell", "get": make_getter("BREAK_EVEN_STOP_RATE"), "set": make_setter("BREAK_EVEN_STOP_RATE", 'float')},
            {"desc": "시간청산 보유기한(일)", "help": "제한일 초과 시 강제 매도", "name": "TIME_STOP_DAYS", "type": "int", "section": "Sell", "get": make_getter("TIME_STOP_DAYS"), "set": make_setter("TIME_STOP_DAYS", 'int')},
            {"desc": "매도(추세이탈) 점수", "help": "점수 하락 시 청산", "name": "SELL_SCORE", "type": "float", "section": "Sell", "get": make_getter("SELL_SCORE"), "set": make_setter("SELL_SCORE", 'float')},
            {"desc": "과열 매도 RSI", "help": "RSI 과열 시 매도", "name": "TAKE_PROFIT_RSI", "type": "float", "section": "Sell", "get": make_getter("TAKE_PROFIT_RSI"), "set": make_setter("TAKE_PROFIT_RSI", 'float')},
            {"desc": "TS 발동 수익률(%)", "help": "트레일링 감시 시작", "name": "TRAILING_STOP_ACTIVATION_RATE", "type": "float", "section": "Sell", "get": make_getter("TRAILING_STOP_ACTIVATION_RATE"), "set": make_setter("TRAILING_STOP_ACTIVATION_RATE", 'float')},
            {"desc": "TS 하락 감지율(%)", "help": "트레일링 하락 시 매도", "name": "TRAILING_STOP_CALLBACK_RATE", "type": "float", "section": "Sell", "get": make_getter("TRAILING_STOP_CALLBACK_RATE"), "set": make_setter("TRAILING_STOP_CALLBACK_RATE", 'float')},
            
            {"desc": "추세 가중치", "help": "Trend", "name": "TREND", "type": "float", "section": "Weights", "get": make_getter("TREND"), "set": make_setter("TREND", 'float')},
            {"desc": "모멘텀 가중치", "help": "Momentum", "name": "MOMENTUM", "type": "float", "section": "Weights", "get": make_getter("MOMENTUM"), "set": make_setter("MOMENTUM", 'float')},
            {"desc": "강도 가중치", "help": "Strength", "name": "STRENGTH", "type": "float", "section": "Weights", "get": make_getter("STRENGTH"), "set": make_setter("STRENGTH", 'float')},
            {"desc": "시너지 가중치", "help": "Synergy", "name": "SYNERGY", "type": "float", "section": "Weights", "get": make_getter("SYNERGY"), "set": make_setter("SYNERGY", 'float')},

            {"desc": "시장 필터링 사용", "help": "지수 하락 시 매수 보류", "name": "USE_MARKET_FILTER", "type": "bool", "choices": ["y", "n"], "section": "Risk", "get": make_getter("USE_MARKET_FILTER"), "set": make_setter("USE_MARKET_FILTER", 'bool')},
            {"desc": "시장 필터 SMA(일)", "help": "필터 감지 이평선 주기", "name": "MARKET_FILTER_MA", "type": "int", "section": "Risk", "get": make_getter("MARKET_FILTER_MA"), "set": make_setter("MARKET_FILTER_MA", 'int')},
            {"desc": "일일 손실 제한(%)", "help": "비상 정지 기준 (0%면 비상 정지 OFF)", "name": "SYSTEM_DAILY_LOSS_LIMIT", "type": "float", "section": "Risk", "get": make_getter("SYSTEM_DAILY_LOSS_LIMIT"), "set": make_setter("SYSTEM_DAILY_LOSS_LIMIT", 'float')},
            {"desc": "종목당 투자 비중", "help": "0.1 ~ 1.0", "name": "SYSTEM_INVEST_PER_STOCK", "type": "float", "section": "Risk", "get": make_getter("SYSTEM_INVEST_PER_STOCK"), "set": make_setter("SYSTEM_INVEST_PER_STOCK", 'float')}
        ]

        # [추가] 토스: 체결강도 미제공 → 체결강도 관련 항목 편집 숨김(매도잔량비는 유지)
        if config.session.is_toss:
            _toss_hidden = {"BUY_VOL_STRENGTH", "AUTO_ADJUST_ASK_BID_RATIO", "MR_VOL_STRENGTH"}
            items = [it for it in items if it["name"] not in _toss_hidden]

        # [추세추종 보호] 커스텀 프리셋 경로로도 반추세성 청산 설정이 켜지지 않도록 동일하게 숨김
        items = [it for it in items if it["name"] not in ANTI_TREND_HIDDEN_KEYS]

        acted = _edit_config_table(title, items, check_preset=False)
        if not acted: break

def edit_strategy_preset_menu():
    """전략 프리셋 개별 조정 메뉴"""
    while True:
        utils.clear_screen()
        menu_items = [
            ("1", "강세장 (Bull) 프리셋 수정", "Edit Bull"),
            ("2", "약세장 (Bear) 프리셋 수정", "Edit Bear"),
            ("3", "횡보장 (Sideways) 프리셋 수정", "Edit Sideways"),
            ("0", "커스텀 프리셋 전체 초기화", "Reset All Custom Presets")
        ]
        choice = utils.show_menu("전략 프리셋 세부 조정", menu_items, default_choice="b")
        if choice.lower() in ['b', 'q']: return False
        
        if choice == "0":
            if Prompt.ask("\n모든 커스텀 프리셋 설정을 초기화하고 각 프리셋의 고유 기본값으로 되돌리시겠습니까?", choices=["y", "n"], default="n") == 'y':
                save_custom_presets({})
                console.print("[green]초기화가 완료되었습니다.[/green]")
                utils.pause()
            continue
            
        ptype = ""
        if choice == "1": ptype = "bull"
        elif choice == "2": ptype = "bear"
        elif choice == "3": ptype = "sideways"
        
        _edit_single_preset(ptype)

def select_strategy_preset():
    """시장 국면별 전략 프리셋 선택 메뉴"""
    menu_items = [
        ("1", "강세장  (Bull) - 수익 극대화 & 추세 추종", "Bull"),
        ("2", "약세장  (Bear) - 생존 우선 & 낙폭과대 스윙", "Bear"),
        ("3", "횡보장  (Sideways) - 박스권 단기 스윙", "Sideways"),
        ("9", "기본설정 (Default) - 시스템 권장 설정", "Default"),
        ("0", "전략 프리셋 조정", "Edit Presets")
    ]
    choice = utils.show_menu("시장 국면별 전략 프리셋 (Strategy Presets)", menu_items, default_choice="b")
    if choice.lower() in ['b', 'q']: return False
    
    if choice == "1": apply_strategy_preset("bull")
    elif choice == "2": apply_strategy_preset("bear")
    elif choice == "3": apply_strategy_preset("sideways")
    elif choice == "9": apply_strategy_preset("default")
    elif choice == "0":
        edit_strategy_preset_menu()
    return True

def reset_to_default(interactive=True):
    if interactive:
        console.print()
        if Prompt.ask("모든 설정을 시스템 기본값으로 초기화하시겠습니까?", choices=["y", "n"], default="n") != "y":
            return False
        console.print()

    # 1. 설정 파일 삭제 및 모든 메모리 설정값 갱신
    config.reset_all_settings()
    
    try:
        if getattr(config, 'ENABLE_TELEGRAM', True):
            from modules.telegram_bot import TelegramCommander
            TelegramCommander()._send_reply("🟢 [초기화(Default)] 시스템의 모든 설정이 최초 기본값으로 초기화되었습니다.")
    except Exception:
        pass
        
    if interactive:
        config_path = os.path.join(config.JSON_DIR, "dynamic_config.json")
        console.print(f"[dim]설정 파일 삭제 및 기본값 복원 완료: {config_path}[/dim]")
        console.print("\n[bold green]모든 설정이 기본값으로 초기화되었습니다.[/bold green]")
        return True
    else:
        return "🟢 [초기화(Default)] 시스템의 모든 설정이 최초 기본값으로 초기화되었습니다."

def manage_custom_settings():
    """[6] 커스텀 변경된 설정 내역 조회 및 기본값 초기화"""
    import time
    import re
    
    context.USER_ACTION_BREADCRUMB.append("[6] 커스텀 설정 조회/초기화")

    while True:
        utils.clear_screen()
        utils.print_breadcrumb()

        changed_items = config.get_custom_settings()

        if not changed_items:
            console.print("\n[dim]현재 기본값에서 변경된 커스텀 설정이 없습니다.[/dim]\n")
            utils.pause()
            context.USER_ACTION_BREADCRUMB.pop()
            return

        console.print()
        table = Table(
            title="커스텀 변경된 설정 내역 (Custom Configuration)",
            box=box.HORIZONTALS,
            show_header=True,
            header_style="dim",
            border_style="dim",
            expand=False,
            padding=(0, 1)
        )
        table.add_column("No.", justify="right", style="dim")
        table.add_column("설정 항목 (Description)", justify="left", style="white")
        table.add_column("변수명 (Config Name)", justify="left", style="dim")
        table.add_column("기본값 (Default)", justify="right", style="dim")
        table.add_column("현재값 (Custom)", justify="right", style="cyan")

        short_names = {
            "BUY_SCORE": "매수 기준 점수",
            "RISE_SCORE": "상승 추세 점수",
            "BUY_RSI_MAX": "매수 허용 RSI 상한",
            "BUY_VOL_STRENGTH": "매수 체결강도 기준",
            "AUTO_ADJUST_ASK_BID_RATIO": "비대칭성 자동 계산",
            "BUY_ASK_BID_RATIO": "매도잔량 비율 기준",
            "USE_MEAN_REVERSION": "역추세 매수 사용",
            "MR_RSI_MAX": "역추세 RSI",
            "MR_DISPARITY_MAX": "역추세 이격도",
            "MR_VOL_STRENGTH": "역추세 체결강도",
            "DISPARITY_UPPER": "과열 이격도 상한",
            "DISPARITY_LOWER": "침체 이격도 하한",
            "SUPER_MOMENTUM_USE": "슈퍼 모멘텀 (RSI 유연화)",
            "SUPER_MOMENTUM_SCORE": "슈퍼 매수 발동 점수",
            "SUPER_MOMENTUM_W52_POS": "슈퍼 52주 위치 기준",
            "SUPER_BUY_RSI_MAX": "완화된 매수 RSI 상한",
            "TAKE_PROFIT_RATE": "익절 수익률",
            "HALF_TAKE_PROFIT_USE": "반익절 사용",
            "DEFENSIVE_HALF_SELL_USE": "방어적 반매도 사용",
            "STOP_LOSS_RATE": "손절 수익률",
            "USE_ATR_STOP": "ATR 손절 사용",
            "ATR_STOP_MULTIPLIER": "ATR 손절 배수",
            "MAX_ATR_STOP_LOSS_RATE": "ATR 최대 손절률",
            "BREAK_EVEN_PROFIT_RATE": "본전 청산 발동 수익률",
            "BREAK_EVEN_STOP_RATE": "본전 청산 손절선",
            "TIME_STOP_USE": "시간 청산 사용",
            "TIME_STOP_DAYS": "시간 청산 기준일",
            "TIME_STOP_MIN_PROFIT_RATE": "시간청산 최소수익",
            "MR_GRACE_LOSS_RATE": "역매수 유예 손실",
            "SELL_SCORE": "매도(추세이탈) 점수",
            "TAKE_PROFIT_RSI": "과열 매도 RSI",
            "SUPER_TAKE_PROFIT_RSI": "슈퍼 모멘텀 과열 매도 RSI",
            "TRAILING_STOP_ACTIVATION_RATE": "TS 발동 수익률",
            "TRAILING_STOP_CALLBACK_RATE": "TS 하락 감지율",
            "TREND": "추세 팩터",
            "MOMENTUM": "모멘텀 팩터",
            "STRENGTH": "강도/수급 팩터",
            "SYNERGY": "시너지 가산점",
            "USE_ADAPTIVE_THRESHOLD": "적응형 임계값 사용",
            "BULL_SCORE_ADJ": "강세장 점수 보정",
            "BEAR_SCORE_ADJ": "약세장 점수 보정",
            "SIDEWAYS_SCORE_ADJ": "횡보장 점수 보정",
            "REGIME_MA_PERIOD": "추세 판단 EMA (일)",
            "REGIME_ADX_THRESHOLD": "추세 판단 ADX",
            "SYSTEM_INVEST_PER_STOCK": "종목당 투자 비중",
            "SYSTEM_MAX_HOLDINGS": "최대 보유 종목 수",
            "SYSTEM_INCLUDE_ETF": "자동매매 대상에 ETF 포함",
            "SLIPPAGE_RATE": "슬리피지 비율",
            "USE_VOLATILITY_TARGETING": "변동성 타겟팅 사용",
            "TARGET_VOLATILITY": "목표 연간 변동성",
            "VOLATILITY_SCALING_MAX": "스케일링 최대 배수",
            "VOLATILITY_SCALING_MIN": "스케일링 최소 배수",
            "USE_MARKET_FILTER": "시장 필터링 사용",
            "MARKET_FILTER_MA": "시장 필터링 SMA (일)",
            "SYSTEM_MAX_CONSECUTIVE_ERRORS": "연속 에러 허용",
            "SYSTEM_DAILY_LOSS_LIMIT": "일일 손실 제한 (%)",
            "SYSTEM_RISK_PER_TRADE": "1회 최대 리스크 (%)",
            "SYSTEM_MAX_PORTFOLIO_RISK": "총 오픈 리스크 한도 (%)",
            "USE_CORRELATION_FILTER": "상관계수 필터링 사용",
            "CORRELATION_THRESHOLD": "상관계수 임계값",
            "CHART_LOOKBACK_DAYS": "데이터 조회 기간",
            "SAR_AF_START": "SAR 가속 시작",
            "SAR_AF_STEP": "SAR 가속 증가",
            "SAR_AF_MAX": "SAR 가속 최대",
            "RSI_PERIOD": "RSI 계산 기간",
            "RSI_SIGNAL": "RSI 시그널 기간",
            "RSI_UPPER": "RSI 과매수 기준",
            "RSI_MID": "RSI 중심선",
            "RSI_LOWER": "RSI 과매도 기준",
            "ADX_PERIOD": "ADX 계산 기간",
            "CCI_WINDOW": "CCI 계산 기간",
            "CCI_UPPER": "CCI 과매수 기준",
            "CCI_LOWER": "CCI 과매도 기준",
            "MACD_FAST": "MACD Fast EMA",
            "MACD_SLOW": "MACD Slow EMA",
            "MACD_SIGNAL": "MACD Signal",
            "OBV_MA_PERIOD": "OBV EMA 기간",
            "ATR_PERIOD": "ATR 계산 기간",
            "EMA_SHORT": "단기 이평선(EMA) 기간",
            "VOLUME_MA_PERIOD": "거래량 이동평균 기간",
            "VOLUME_SPIKE_RATIO": "거래량 폭발 배수",
            "SYSTEM_TRADING_START_TIME": "거래 시작 시간",
            "SYSTEM_TRADING_END_TIME": "거래 종료 시간",
            "SYSTEM_TRADING_INTERVAL": "모니터링 주기 (초)",
            "CONCLUSION_CHECK_INTERVAL": "체결 감시 주기(초)",
            "CONCLUSION_CHECK_IDLE_INTERVAL": "대기 모드 주기(초)",
            "CONCLUSION_CHECK_ACTIVE_DURATION": "집중 감시 시간(초)",
            "UNFILLED_ORDER_CANCEL_SECONDS": "미체결 취소 대기(초)",
            "CHART_CACHE_TTL_MINUTES": "차트 캐시 시간(분)",
            "USE_WEBSOCKET": "실시간 WebSocket 사용",
            "ENABLE_TELEGRAM": "사용 여부",
            "TELEGRAM_INSTANCE_NAME": "인스턴스 이름",
            "TELEGRAM_POLLING_TIMEOUT": "폴링 타임아웃",
            "AUTO_MORNING_BRIEFING_USE": "장전 AI 브리핑 사용",
            "AUTO_MORNING_BRIEFING_TIME": "장전 AI 브리핑 시간",
            "CLEAR_SCREEN_ON_MENU": "화면 자동 지우기",
            "SCREEN_DEBUG_LEVEL": "화면 로그 레벨",
            "FILE_DEBUG_LEVEL": "파일 로그 레벨",
            "BOX_PERIOD": "박스권 기준 기간",
            "BOX_VALUE_AREA_PCT": "박스권 밀집 비율",
            "TREND_PERIOD": "추세선 룩백 기간",
        }

        _CAT1 = "1. 매수 및 매도 전략 설정"
        _CAT2 = "2. 스코어링 및 시장 국면 설정"
        _CAT3 = "3. 리스크 및 자산 배분 설정"
        _CAT4 = "4. 기술적 지표 파라미터"
        _CAT5 = "5. 환경 및 시스템 설정"

        category_map = {
            "BUY_SCORE": (_CAT1, "1-1. 기본 진입 조건"),
            "RISE_SCORE": (_CAT1, "1-1. 기본 진입 조건"),
            "INTEREST_SIGNAL_MIN": (_CAT1, "1-1. 기본 진입 조건"),
            "INTEREST_MA60_NEAR": (_CAT1, "1-1. 기본 진입 조건"),
            "BUY_RSI_MAX": (_CAT1, "1-1. 기본 진입 조건"),
            "BUY_VOL_STRENGTH": (_CAT1, "1-1. 기본 진입 조건"),
            "AUTO_ADJUST_ASK_BID_RATIO": (_CAT1, "1-1. 기본 진입 조건"),
            "BUY_ASK_BID_RATIO": (_CAT1, "1-1. 기본 진입 조건"),
            "DISPARITY_UPPER": (_CAT1, "1-1. 기본 진입 조건"),
            "DISPARITY_LOWER": (_CAT1, "1-1. 기본 진입 조건"),
            "USE_MEAN_REVERSION": (_CAT1, "1-2. 서브전략 (슈퍼 모멘텀/피라미딩)"),
            "MR_RSI_MAX": (_CAT1, "1-2. 서브전략 (슈퍼 모멘텀/피라미딩)"),
            "MR_DISPARITY_MAX": (_CAT1, "1-2. 서브전략 (슈퍼 모멘텀/피라미딩)"),
            "MR_VOL_STRENGTH": (_CAT1, "1-2. 서브전략 (슈퍼 모멘텀/피라미딩)"),
            "MR_GRACE_LOSS_RATE": (_CAT1, "1-2. 서브전략 (슈퍼 모멘텀/피라미딩)"),
            "SUPER_MOMENTUM_USE": (_CAT1, "1-2. 서브전략 (슈퍼 모멘텀/피라미딩)"),
            "SUPER_MOMENTUM_SCORE": (_CAT1, "1-2. 서브전략 (슈퍼 모멘텀/피라미딩)"),
            "SUPER_MOMENTUM_W52_POS": (_CAT1, "1-2. 서브전략 (슈퍼 모멘텀/피라미딩)"),
            "SUPER_BUY_RSI_MAX": (_CAT1, "1-2. 서브전략 (슈퍼 모멘텀/피라미딩)"),
            "SUPER_TAKE_PROFIT_RSI": (_CAT1, "1-2. 서브전략 (슈퍼 모멘텀/피라미딩)"),
            "TAKE_PROFIT_RATE": (_CAT1, "1-3. 매도/청산 — 트레일링 스탑"),
            "HALF_TAKE_PROFIT_USE": (_CAT1, "1-3. 매도/청산 — 트레일링 스탑"),
            "TAKE_PROFIT_RSI": (_CAT1, "1-3. 매도/청산 — 트레일링 스탑"),
            "TRAILING_STOP_ACTIVATION_RATE": (_CAT1, "1-3. 매도/청산 — 트레일링 스탑"),
            "TRAILING_STOP_CALLBACK_RATE": (_CAT1, "1-3. 매도/청산 — 트레일링 스탑"),
            "STOP_LOSS_RATE": (_CAT1, "1-4. 매도/청산 — 손절"),
            "USE_ATR_STOP": (_CAT1, "1-4. 매도/청산 — 손절"),
            "ATR_STOP_MULTIPLIER": (_CAT1, "1-4. 매도/청산 — 손절"),
            "MAX_ATR_STOP_LOSS_RATE": (_CAT1, "1-4. 매도/청산 — 손절"),
            "BREAK_EVEN_PROFIT_RATE": (_CAT1, "1-4. 매도/청산 — 손절"),
            "BREAK_EVEN_STOP_RATE": (_CAT1, "1-4. 매도/청산 — 손절"),
            "TIME_STOP_USE": (_CAT1, "1-5. 매도/청산 — 기타"),
            "TIME_STOP_DAYS": (_CAT1, "1-5. 매도/청산 — 기타"),
            "TIME_STOP_MIN_PROFIT_RATE": (_CAT1, "1-5. 매도/청산 — 기타"),
            "SELL_SCORE": (_CAT1, "1-5. 매도/청산 — 기타"),
            "DEFENSIVE_HALF_SELL_USE": (_CAT1, "1-5. 매도/청산 — 기타"),
            "TREND": (_CAT2, "2-1. 스코어링 가중치"),
            "MOMENTUM": (_CAT2, "2-1. 스코어링 가중치"),
            "STRENGTH": (_CAT2, "2-1. 스코어링 가중치"),
            "SYNERGY": (_CAT2, "2-1. 스코어링 가중치"),
            "USE_ADAPTIVE_THRESHOLD": (_CAT2, "2-2. 적응형 임계값 (시장국면)"),
            "BULL_SCORE_ADJ": (_CAT2, "2-2. 적응형 임계값 (시장국면)"),
            "BEAR_SCORE_ADJ": (_CAT2, "2-2. 적응형 임계값 (시장국면)"),
            "SIDEWAYS_SCORE_ADJ": (_CAT2, "2-2. 적응형 임계값 (시장국면)"),
            "REGIME_MA_PERIOD": (_CAT2, "2-2. 적응형 임계값 (시장국면)"),
            "REGIME_ADX_THRESHOLD": (_CAT2, "2-2. 적응형 임계값 (시장국면)"),
            "SYSTEM_INVEST_PER_STOCK": (_CAT3, "3-1. 자산 배분/포지션"),
            "SYSTEM_MAX_HOLDINGS": (_CAT3, "3-1. 자산 배분/포지션"),
            "SYSTEM_INCLUDE_ETF": (_CAT3, "3-1. 자산 배분/포지션"),
            "SLIPPAGE_RATE": (_CAT3, "3-1. 자산 배분/포지션"),
            "USE_VOLATILITY_TARGETING": (_CAT3, "3-1. 자산 배분/포지션"),
            "TARGET_VOLATILITY": (_CAT3, "3-1. 자산 배분/포지션"),
            "VOLATILITY_SCALING_MAX": (_CAT3, "3-1. 자산 배분/포지션"),
            "VOLATILITY_SCALING_MIN": (_CAT3, "3-1. 자산 배분/포지션"),
            "USE_MARKET_FILTER": (_CAT3, "3-2. 매수 필터"),
            "MARKET_FILTER_MA": (_CAT3, "3-2. 매수 필터"),
            "USE_CORRELATION_FILTER": (_CAT3, "3-2. 매수 필터"),
            "CORRELATION_THRESHOLD": (_CAT3, "3-2. 매수 필터"),
            "SYSTEM_MAX_CONSECUTIVE_ERRORS": (_CAT3, "3-3. 비상 안전장치"),
            "SYSTEM_DAILY_LOSS_LIMIT": (_CAT3, "3-3. 비상 안전장치"),
            "SYSTEM_RISK_PER_TRADE": (_CAT3, "3-3. 비상 안전장치"),
            "SYSTEM_MAX_PORTFOLIO_RISK": (_CAT3, "3-3. 비상 안전장치"),
            "CHART_LOOKBACK_DAYS": (_CAT4, "4-1. 데이터 조회"),
            "SAR_AF_START": (_CAT4, "4-2. 추세"),
            "SAR_AF_STEP": (_CAT4, "4-2. 추세"),
            "SAR_AF_MAX": (_CAT4, "4-2. 추세"),
            "MACD_FAST": (_CAT4, "4-2. 추세"),
            "MACD_SLOW": (_CAT4, "4-2. 추세"),
            "MACD_SIGNAL": (_CAT4, "4-2. 추세"),
            "EMA_SHORT": (_CAT4, "4-2. 추세"),
            "TREND_PERIOD": (_CAT4, "4-2. 추세"),
            "RSI_PERIOD": (_CAT4, "4-3. 모멘텀"),
            "RSI_SIGNAL": (_CAT4, "4-3. 모멘텀"),
            "RSI_UPPER": (_CAT4, "4-3. 모멘텀"),
            "RSI_MID": (_CAT4, "4-3. 모멘텀"),
            "RSI_LOWER": (_CAT4, "4-3. 모멘텀"),
            "CCI_WINDOW": (_CAT4, "4-3. 모멘텀"),
            "CCI_UPPER": (_CAT4, "4-3. 모멘텀"),
            "CCI_LOWER": (_CAT4, "4-3. 모멘텀"),
            "ADX_PERIOD": (_CAT4, "4-4. 강도/수급/변동성"),
            "OBV_MA_PERIOD": (_CAT4, "4-4. 강도/수급/변동성"),
            "VOLUME_MA_PERIOD": (_CAT4, "4-4. 강도/수급/변동성"),
            "VOLUME_SPIKE_RATIO": (_CAT4, "4-4. 강도/수급/변동성"),
            "ATR_PERIOD": (_CAT4, "4-4. 강도/수급/변동성"),
            "BOX_PERIOD": (_CAT4, "4-5. 가격 구조"),
            "BOX_VALUE_AREA_PCT": (_CAT4, "4-5. 가격 구조"),
            "SYSTEM_TRADING_START_TIME": (_CAT5, "5-1. 거래 시간·주기"),
            "SYSTEM_TRADING_END_TIME": (_CAT5, "5-1. 거래 시간·주기"),
            "SYSTEM_TRADING_INTERVAL": (_CAT5, "5-1. 거래 시간·주기"),
            "CONCLUSION_CHECK_INTERVAL": (_CAT5, "5-2. 주문·체결 감시"),
            "CONCLUSION_CHECK_IDLE_INTERVAL": (_CAT5, "5-2. 주문·체결 감시"),
            "CONCLUSION_CHECK_ACTIVE_DURATION": (_CAT5, "5-2. 주문·체결 감시"),
            "UNFILLED_ORDER_CANCEL_SECONDS": (_CAT5, "5-2. 주문·체결 감시"),
            "CHART_CACHE_TTL_MINUTES": (_CAT5, "5-3. 데이터·통신"),
            "USE_WEBSOCKET": (_CAT5, "5-3. 데이터·통신"),
            "ENABLE_TELEGRAM": (_CAT5, "5-4. 텔레그램 및 AI 브리핑"),
            "TELEGRAM_INSTANCE_NAME": (_CAT5, "5-4. 텔레그램 및 AI 브리핑"),
            "TELEGRAM_POLLING_TIMEOUT": (_CAT5, "5-4. 텔레그램 및 AI 브리핑"),
            "AUTO_MORNING_BRIEFING_USE": (_CAT5, "5-4. 텔레그램 및 AI 브리핑"),
            "AUTO_MORNING_BRIEFING_TIME": (_CAT5, "5-4. 텔레그램 및 AI 브리핑"),
            "MARKET_HALT_ALERT_USE": (_CAT5, "5-4. 텔레그램 및 AI 브리핑"),
            "MARKET_HALT_VI_USE": (_CAT5, "5-4. 텔레그램 및 AI 브리핑"),
            "CLEAR_SCREEN_ON_MENU": (_CAT5, "5-5. 화면 및 로그"),
            "SCREEN_DEBUG_LEVEL": (_CAT5, "5-5. 화면 및 로그"),
            "FILE_DEBUG_LEVEL": (_CAT5, "5-5. 화면 및 로그"),
        }

        category_order = {
            _CAT1: 1, _CAT2: 2, _CAT3: 3, _CAT4: 4, _CAT5: 5, "기타 설정": 99
        }

        sub_category_order = {
            "1-1. 기본 진입 조건": 1, "1-2. 서브전략 (슈퍼 모멘텀/피라미딩)": 2,
            "1-3. 매도/청산 — 트레일링 스탑": 3, "1-4. 매도/청산 — 손절": 4, "1-5. 매도/청산 — 기타": 5,
            "2-1. 스코어링 가중치": 1, "2-2. 적응형 임계값 (시장국면)": 2,
            "3-1. 자산 배분/포지션": 1, "3-2. 매수 필터": 2, "3-3. 비상 안전장치": 3,
            "4-1. 데이터 조회": 1, "4-2. 추세": 2, "4-3. 모멘텀": 3,
            "4-4. 강도/수급/변동성": 4, "4-5. 가격 구조": 5,
            "5-1. 거래 시간·주기": 1, "5-2. 주문·체결 감시": 2, "5-3. 데이터·통신": 3,
            "5-4. 텔레그램 및 AI 브리핑": 4, "5-5. 화면 및 로그": 5,
            "": 0, "기타": 99
        }

        short_names_keys = list(short_names.keys())

        keys_list = list(changed_items.keys())
        keys_list.sort(key=lambda k: (
            category_order.get(category_map.get(changed_items[k].get("key", k), ("기타 설정", ""))[0], 99),
            sub_category_order.get(category_map.get(changed_items[k].get("key", k), ("기타 설정", ""))[1], 99),
            short_names_keys.index(changed_items[k].get("key", k)) if changed_items[k].get("key", k) in short_names_keys else 999,
            k
        ))
        
        current_main = None
        current_sub = None
        
        for i, key in enumerate(keys_list):
            info = changed_items[key]
            dict_key = info.get("key", key)
            desc = getattr(config, 'CONFIG_DESCRIPTIONS', {}).get(dict_key, "사용자 설정 항목")
            short_name = short_names.get(dict_key, dict_key)
            
            key_display = f"{short_name}\n[white dim]{desc}[/white dim]"
            
            main_cat, sub_cat = category_map.get(dict_key, ("기타 설정", ""))
            if main_cat != current_main or sub_cat != current_sub:
                table.add_section()
                if main_cat != current_main:
                    table.add_row("", f"[bold]{main_cat}[/bold]", "", "", "")
                    current_main = main_cat
                if sub_cat:
                    table.add_row("", f"[bold dim]  {sub_cat}[/bold dim]", "", "", "")
                current_sub = sub_cat
                
            table.add_row(
                str(i + 1),
                key_display,
                key,
                str(info["default"]),
                str(info["current"])
            )

        console.print(table)
        console.print()

        ans = Prompt.ask(
            "초기화할 설정 번호를 입력하세요 [dim](다중: 1,3 / 전체: 0 / 취소: b 또는 Enter)[/dim]"
        )

        if not ans or ans.lower() in ['b', 'q']:
            context.USER_ACTION_BREADCRUMB.pop()
            return

        target_indices = []
        if ans == '0':
            target_indices = list(range(1, len(keys_list) + 1))
        elif ',' in ans or ' ' in ans:
            parts = re.split(r'[, ]+', ans)
            target_indices = [int(p) for p in parts if p.isdigit()]
        elif ans.isdigit():
            target_indices = [int(ans)]

        valid_keys_to_reset = []
        for i in target_indices:
            if 1 <= i <= len(keys_list):
                valid_keys_to_reset.append(keys_list[i - 1])

        if not valid_keys_to_reset:
            console.print("[red]잘못된 번호입니다.[/red]")
            utils.pause()
            continue

        confirm = Prompt.ask(f"선택한 {len(valid_keys_to_reset)}개의 설정을 기본값으로 초기화하시겠습니까?", choices=["y", "n"], default="n")
        if confirm.lower() == 'y':
            # 텔레그램 요약 메시지 생성을 위해 초기화 전 정보 미리 추출
            reset_details = []
            for k in valid_keys_to_reset:
                info = changed_items.get(k, {})
                dict_key = info.get("key", k)
                short_name = short_names.get(dict_key, dict_key)
                prev_val = info.get("current", "-")
                def_val = info.get("default", "-")
                reset_details.append(f"• {short_name}: {prev_val} ➔ {def_val}")
                
            config.reset_custom_settings(valid_keys_to_reset)
            
            try:
                if getattr(config, 'ENABLE_TELEGRAM', True):
                    from modules.telegram_bot import TelegramCommander
                    msg = "🔄 [설정 변경] 선택한 커스텀 설정이 기본값으로 초기화되었습니다."
                    if reset_details:
                        msg += "\n\n[초기화 내역]\n" + "\n".join(reset_details)
                    TelegramCommander()._send_reply(msg)
            except Exception:
                pass
            console.print(f"\n[green]성공적으로 초기화되었습니다.[/green]")
            utils.pause()

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
            ("6", "커스텀 설정 조회 및 초기화", "Manage Custom Settings"),
            ("7", "시장 국면별 전략 프리셋", "Strategy Presets"),
            ("8", "데이터 캐시 초기화", "Clear Cache"),
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
            sub_items = [
                ("1", "기본 진입 조건", "Entry"),
                ("2", "서브전략 (슈퍼 모멘텀/피라미딩)", "Sub-Strategy"),
                ("3", "매도/청산 — 트레일링 스탑", "Trailing Stop"),
                ("4", "매도/청산 — 손절", "Stop Loss"),
                ("5", "매도/청산 — 기타", "Exit Etc")
            ]
            sub_choice = utils.show_menu("매수 및 매도 전략 설정", sub_items, default_choice="b")
            if sub_choice.lower() in ['b', 'q']: continue

            sub_map = dict((k, v) for k, v, _ in sub_items)
            context.USER_ACTION_BREADCRUMB.append(f"[{sub_choice}] {sub_map.get(sub_choice, '')}")

            if sub_choice in sub_map:
                items_src = _entry_strategy_items if sub_choice in ("1", "2") else _sell_strategy_items
                _edit_section(f"{sub_map[sub_choice]} (1-{sub_choice})", items_src, f"1-{sub_choice}")

        elif choice == "2":
            sub_items = [("1", "스코어링 가중치 설정", "Weights"), ("2", "적응형 임계값 (시장국면) 설정", "Regime")]
            sub_choice = utils.show_menu("스코어링 및 시장 국면 설정", sub_items, default_choice="b")
            if sub_choice.lower() in ['b', 'q']: continue
            
            sub_map = dict((k, v) for k, v, _ in sub_items)
            context.USER_ACTION_BREADCRUMB.append(f"[{sub_choice}] {sub_map.get(sub_choice, '')}")
            
            if sub_choice == "1": modify_scoring_weights()
            elif sub_choice == "2": modify_market_regime_params()
            
        elif choice == "3":
            sub_items = [
                ("1", "자산 배분/포지션", "Portfolio"),
                ("2", "매수 필터", "Filters"),
                ("3", "비상 안전장치", "Safety")
            ]
            sub_choice = utils.show_menu("리스크 및 자산 배분 설정", sub_items, default_choice="b")
            if sub_choice.lower() in ['b', 'q']: continue

            sub_map = dict((k, v) for k, v, _ in sub_items)
            context.USER_ACTION_BREADCRUMB.append(f"[{sub_choice}] {sub_map.get(sub_choice, '')}")

            if sub_choice in sub_map:
                _edit_section(f"{sub_map[sub_choice]} (3-{sub_choice})", _risk_portfolio_items, f"3-{sub_choice}")

        elif choice == "4":
            sub_items = [
                ("1", "데이터 조회", "Data"),
                ("2", "추세", "Trend"),
                ("3", "모멘텀", "Momentum"),
                ("4", "강도/수급/변동성", "Strength & Volume"),
                ("5", "가격 구조", "Price Structure")
            ]
            sub_choice = utils.show_menu("기술적 지표 파라미터", sub_items, default_choice="b")
            if sub_choice.lower() in ['b', 'q']: continue

            sub_map = dict((k, v) for k, v, _ in sub_items)
            context.USER_ACTION_BREADCRUMB.append(f"[{sub_choice}] {sub_map.get(sub_choice, '')}")

            if sub_choice in sub_map:
                _edit_section(f"{sub_map[sub_choice]} (4-{sub_choice})", _indicator_items, f"4-{sub_choice}")

        elif choice == "5":
            sub_items = [
                ("1", "거래 시간·주기", "Time & Cycle"),
                ("2", "주문·체결 감시", "Order & Conclusion"),
                ("3", "데이터·통신", "Data & Comm"),
                ("4", "텔레그램 및 AI 브리핑", "Telegram"),
                ("5", "화면 및 로그", "Log")
            ]
            sub_choice = utils.show_menu("환경 및 시스템 설정", sub_items, default_choice="b")
            if sub_choice.lower() in ['b', 'q']: continue

            sub_map = dict((k, v) for k, v, _ in sub_items)
            context.USER_ACTION_BREADCRUMB.append(f"[{sub_choice}] {sub_map.get(sub_choice, '')}")

            if sub_choice in ("1", "2", "3"):
                _edit_section(f"{sub_map[sub_choice]} (5-{sub_choice})", _trading_cycle_items, f"5-{sub_choice}")
            elif sub_choice == "4": modify_telegram_settings()
            elif sub_choice == "5": modify_log_settings()

        elif choice == "6":
            manage_custom_settings()
        
        elif choice == "7":
            if select_strategy_preset() is not False: utils.pause()
        elif choice == "8":
            import api
            from modules import market
            from modules import analysis 
            api.clear_chart_cache()
            market.clear_market_yf_cache()
            analysis.clear_smart_money_cache() 
            config.console.print("\n[bold green]데이터 캐시가 초기화되었습니다.[/bold green]")
            utils.pause()
        elif choice == "9":
            console.print()
            grp = Prompt.ask(
                "조회할 설정 그룹 선택 [dim](1:전략 2:스코어링 3:리스크 4:지표 5:환경, 전체: Enter, 이전: b)[/dim]",
                choices=["1", "2", "3", "4", "5", "a", "b", "q", "A", "B", "Q"],
                default="a", show_choices=False
            )
            if grp.lower() in ["b", "q"]:
                continue
            view_system_config(int(grp) if grp.isdigit() else None)
            utils.pause()
        elif choice == "0": 
            if reset_to_default() is not False: utils.pause()