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
        "RISK_SCALING_PARAMS": config.RISK_SCALING_PARAMS,
        "SYSTEM_INVEST_PER_STOCK": config.settings.SYSTEM_INVEST_PER_STOCK,
        "SYSTEM_MAX_HOLDINGS": config.settings.SYSTEM_MAX_HOLDINGS,
        "SYSTEM_TRADING_INTERVAL": getattr(config.settings, 'SYSTEM_TRADING_INTERVAL', 180),
        "SYSTEM_DAILY_LOSS_LIMIT": getattr(config.settings, 'SYSTEM_DAILY_LOSS_LIMIT', 10.0),
        "USE_MARKET_FILTER": getattr(config.settings, 'USE_MARKET_FILTER', True),
        "USE_RS_FILTER": getattr(config.settings, 'USE_RS_FILTER', False),
        "RS_FILTER_LOOKBACK": getattr(config.settings, 'RS_FILTER_LOOKBACK', 0),
        "MARKET_FILTER_MA": getattr(config.settings, 'MARKET_FILTER_MA', 60),
        "CONCLUSION_CHECK_INTERVAL": getattr(config.settings, 'CONCLUSION_CHECK_INTERVAL', 5),
        "CONCLUSION_CHECK_IDLE_INTERVAL": getattr(config.settings, 'CONCLUSION_CHECK_IDLE_INTERVAL', 300),
        "CONCLUSION_CHECK_ACTIVE_DURATION": getattr(config.settings, 'CONCLUSION_CHECK_ACTIVE_DURATION', 60),
        "UNFILLED_ORDER_CANCEL_SECONDS": getattr(config.settings, 'UNFILLED_ORDER_CANCEL_SECONDS', 120),
        "CHART_CACHE_TTL_MINUTES": getattr(config.settings, 'CHART_CACHE_TTL_MINUTES', 360),
        "USE_KRX_CLOSE_AFTER_HOURS": getattr(config.settings, 'USE_KRX_CLOSE_AFTER_HOURS', True),
        "JOURNAL_SYNC_USE": getattr(config.settings, 'JOURNAL_SYNC_USE', False),
        "ENABLE_TELEGRAM": getattr(config.settings, 'ENABLE_TELEGRAM', True),
        "TELEGRAM_INSTANCE_NAME": getattr(config.settings, 'TELEGRAM_INSTANCE_NAME', "HTS"),
        "TELEGRAM_POLLING_TIMEOUT": getattr(config.settings, 'TELEGRAM_POLLING_TIMEOUT', 10),
        "AUTO_MORNING_BRIEFING_USE": getattr(config.settings, 'AUTO_MORNING_BRIEFING_USE', False),
        "AUTO_MORNING_BRIEFING_TIME": getattr(config.settings, 'AUTO_MORNING_BRIEFING_TIME', "0830"),
        "AUTO_DISCLOSURE_ALERT_USE": getattr(config.settings, 'AUTO_DISCLOSURE_ALERT_USE', True),
        "AUTO_CALENDAR_ALERT_USE": getattr(config.settings, 'AUTO_CALENDAR_ALERT_USE', True),
        "AUTO_CALENDAR_ALERT_TIME": getattr(config.settings, 'AUTO_CALENDAR_ALERT_TIME', "0820"),
        "MARKET_HALT_ALERT_USE": getattr(config.settings, 'MARKET_HALT_ALERT_USE', True),
        "MARKET_HALT_VI_USE": getattr(config.settings, 'MARKET_HALT_VI_USE', False),
        "SCREEN_DEBUG_LEVEL": getattr(config.settings, 'SCREEN_DEBUG_LEVEL', "ERROR"),
        "CLEAR_SCREEN_ON_MENU": getattr(config.settings, 'CLEAR_SCREEN_ON_MENU', False),
        "FILE_DEBUG_LEVEL": getattr(config.settings, 'FILE_DEBUG_LEVEL', "WARNING"),
        "SYSTEM_MAX_CONSECUTIVE_ERRORS": getattr(config.settings, 'SYSTEM_MAX_CONSECUTIVE_ERRORS', 5),
        "SYSTEM_TRADING_START_TIME": getattr(config.settings, 'SYSTEM_TRADING_START_TIME', "0900"),
        "SYSTEM_TRADING_END_TIME": getattr(config.settings, 'SYSTEM_TRADING_END_TIME', "1530"),
        "SYSTEM_RISK_PER_TRADE": getattr(config.settings, 'SYSTEM_RISK_PER_TRADE', 4.0),
        "SYSTEM_MAX_PORTFOLIO_RISK": getattr(config.settings, 'SYSTEM_MAX_PORTFOLIO_RISK', 10.0),
        "USE_VOLATILITY_TARGETING": getattr(config.settings, 'USE_VOLATILITY_TARGETING', True),
        "TARGET_VOLATILITY": getattr(config.settings, 'TARGET_VOLATILITY', 0.20),
        "VOLATILITY_SCALING_MAX": getattr(config.settings, 'VOLATILITY_SCALING_MAX', 2.0),
        "VOLATILITY_SCALING_MIN": getattr(config.settings, 'VOLATILITY_SCALING_MIN', 0.4),
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

# [UX] 설정 하위 메뉴(1~5) 공통 '전체보기' 항목.
#  편집 항목을 고르기 전에 현재 값을 먼저 보는 것이 일반적인 흐름이므로 각 하위 메뉴의
#  기본 선택지로 둔다. 숨김 처리된 키(ANTI_TREND/BACKTESTED_HIDDEN_KEYS)도 조회 화면에는
#  읽기 전용으로 나오므로, 이 항목이 '무엇으로 도는지'를 확인하는 정식 경로가 된다.
_VIEW_ALL_KEY = "9"
_VIEW_ALL_ITEM = (_VIEW_ALL_KEY, "전체보기", "View All")


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

    # [PRESET_RETIRED] 전략 프리셋 폐지로 '*'(프리셋 연동) 범례도 제거한다.
    caption = "그룹 번호(n-m)는 시스템 설정 메뉴의 편집 경로와 동일 (예: 5-5 → 메뉴 5 → 5)"

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

    # [PRESET_RETIRED] 프리셋 폐지로 '*'(프리셋 연동) 표시는 더 이상 붙이지 않는다.
    preset_keys = set()

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
        subheader("1-1. 진입 조건", first=True)
        row("매수 기준 점수", "진입 임계값 (종합 점수)", "ANALYSIS_THRESHOLDS['BUY_SCORE']", f"{thresholds.get('BUY_SCORE')}", key="BUY_SCORE")
        row("상승 추세 점수", "'상승' 상태 분류 기준", "ANALYSIS_THRESHOLDS['RISE_SCORE']", f"{thresholds.get('RISE_SCORE')}", key="RISE_SCORE")
        row("매수 허용 RSI 상한", "과열 방지 (이 값보다 낮아야 매수)", "ANALYSIS_THRESHOLDS['BUY_RSI_MAX']", f"{thresholds.get('BUY_RSI_MAX')}", key="BUY_RSI_MAX")
        row("매수 체결강도 기준", "수급 확인 (이 값 이상이어야 매수)", "ANALYSIS_THRESHOLDS['BUY_VOL_STRENGTH']", f"{thresholds.get('BUY_VOL_STRENGTH')}%", key="BUY_VOL_STRENGTH")
        row("비대칭성 자동 계산", "체결강도 100% 기준으로 비례하여 자동 조정", "ANALYSIS_THRESHOLDS['AUTO_ADJUST_ASK_BID_RATIO']", f"{thresholds.get('AUTO_ADJUST_ASK_BID_RATIO', True)}", key="AUTO_ADJUST_ASK_BID_RATIO")
        row("매도잔량 비율 기준", "가짜 체결강도 방어 (체결강도 100% 기준 비율)", "ANALYSIS_THRESHOLDS['BUY_ASK_BID_RATIO']", f"{thresholds.get('BUY_ASK_BID_RATIO', 1.0)}배", key="BUY_ASK_BID_RATIO")

        subheader("1-2. 서브전략 (슈퍼 모멘텀/피라미딩)")
        # [추세추종 보호] 역매수(역추세) 관련 설정은 조회·편집 화면에서 숨김 (ANTI_TREND_HIDDEN_KEYS 주석 참조)
        # [추세추종 보호] 슈퍼 모멘텀 스위치는 잠금 — BUY_RSI_MAX와 조합되면 매매 0건이 된다
        #  (ANTI_TREND_HIDDEN_KEYS 주석 참조). 값 자체는 읽기 전용으로 계속 보여준다.
        table.add_row(
            "슈퍼 모멘텀 (RSI 유연화)\n[dim]주도주 랠리 시 RSI 허용치 완화[/dim]",
            "[dim](추세추종 검증값 — 조정 잠금)[/dim]",
            f"{thresholds.get('SUPER_MOMENTUM_USE', True)}")
        if thresholds.get('SUPER_MOMENTUM_USE', True):
            row("슈퍼 매수 발동 점수", "기준 점수 이상 & 신고가 90% 이상 시 발동", "ANALYSIS_THRESHOLDS['SUPER_MOMENTUM_SCORE']", f"{thresholds.get('SUPER_MOMENTUM_SCORE', 8.0)}", key="SUPER_MOMENTUM_SCORE", indent=True)
            row("슈퍼 52주 위치 기준", "신고가 근접 여부 (예: 90.0% 이상)", "ANALYSIS_THRESHOLDS['SUPER_MOMENTUM_W52_POS']", f"{thresholds.get('SUPER_MOMENTUM_W52_POS', 90.0)}%", key="SUPER_MOMENTUM_W52_POS", indent=True)
            row("완화된 매수 RSI 상한", "발동 시 적용되는 진입 최대 RSI", "ANALYSIS_THRESHOLDS['SUPER_BUY_RSI_MAX']", f"{thresholds.get('SUPER_BUY_RSI_MAX', 80.0)}", key="SUPER_BUY_RSI_MAX", indent=True)
        row("피라미딩 (수익 증액)", "수익으로 추세 검증된 포지션만 증액", "ANALYSIS_THRESHOLDS['PYRAMIDING_USE']", f"{thresholds.get('PYRAMIDING_USE', True)}", key="PYRAMIDING_USE")
        if thresholds.get('PYRAMIDING_USE', True):
            row("증액 발동 수익률", "이 수익률 이상 & 매수신호 유지 시 증액", "ANALYSIS_THRESHOLDS['PYRAMIDING_PROFIT_TRIGGER']", f"{thresholds.get('PYRAMIDING_PROFIT_TRIGGER', 10.0)}%", key="PYRAMIDING_PROFIT_TRIGGER", indent=True)
            row("증액 비율", "보유 수량 대비 증액 수량 비율", "ANALYSIS_THRESHOLDS['PYRAMIDING_RATIO']", f"{thresholds.get('PYRAMIDING_RATIO', 0.5)}", key="PYRAMIDING_RATIO", indent=True)
            row("최대 증액 횟수", "포지션당 피라미딩 허용 횟수", "ANALYSIS_THRESHOLDS['PYRAMIDING_MAX_COUNT']", f"{thresholds.get('PYRAMIDING_MAX_COUNT', 1)}회", key="PYRAMIDING_MAX_COUNT", indent=True)

        subheader("1-3. 청산 — 손절·트레일링·시간")
        # [추세추종 보호] 고정 익절/반익절/RSI 과열 매도는 조회·편집 화면에서 숨김 (ANTI_TREND_HIDDEN_KEYS 주석 참조)
        row("TS 발동 수익률", "트레일링 스탑 감시 시작점", "SELL_STRATEGY['TRAILING_STOP_ACTIVATION_RATE']", f"{sell.get('TRAILING_STOP_ACTIVATION_RATE')}%", key="TRAILING_STOP_ACTIVATION_RATE")
        # [추세추종 보호] TS 하락 감지율은 실효 콜백의 '하한'일 뿐이고 동적 콜백(ATR×배수)이
        #  사실상 항상 이를 넘어서 조정해도 결과가 바뀌지 않는다(실측: 5→2%에서 거래 486건 불변).
        #  편집 가능한 것처럼 보이면 오해를 부르므로 실제 규칙을 그대로 적어 읽기 전용으로 둔다.
        table.add_row(
            f"TS 하락 감지율\n[dim]실효 콜백 = max({sell.get('TRAILING_STOP_CALLBACK_RATE')}%, "
            f"ATR×{sell.get('TRAILING_ATR_MULTIPLIER', 3.5)}/고점) — 통상 동적값이 지배[/dim]",
            "[dim](추세추종 검증값 — 조정 잠금)[/dim]", f"{sell.get('TRAILING_STOP_CALLBACK_RATE')}%")
        # TRAILING_ATR_MULTIPLIER는 애초에 편집 목록에 없다. 변수명을 그대로 띄우면
        # 편집 가능한 것처럼 보이므로 다른 잠금 항목과 표기를 맞춘다.
        table.add_row(
            "TS ATR 배수\n[dim]샹들리에 엑시트: TS 콜백을 ATR×배수로 동적 확대[/dim]",
            "[dim](추세추종 검증값 — 조정 잠금)[/dim]", f"{sell.get('TRAILING_ATR_MULTIPLIER', 3.5)}")

        # [추세추종 보호] 손절 체계 일체 잠금 (ANTI_TREND_HIDDEN_KEYS 주석 참조).
        #  ATR 손절을 끄면 >50% 대박이 12→4건으로 잘리므로 스위치부터 잠그고,
        #  잠근 뒤 실질 다이얼이 되는 배수·상한도 함께 잠근다. 무엇으로 도는지는 계속 보여준다.
        table.add_row(
            f"ATR 손절 (변동성 기반)\n[dim]손절폭 = ATR×{sell.get('ATR_STOP_MULTIPLIER', 2.0)}, "
            f"최대 {sell.get('MAX_ATR_STOP_LOSS_RATE', -15.0):g}% 한도\n"
            f"    (산출 실패 시 폴백 {sell.get('STOP_LOSS_RATE')}%)[/dim]",
            "[dim](추세추종 검증값 — 조정 잠금)[/dim]",
            "사용" if sell.get('USE_ATR_STOP', True) else "미사용")
        # [추세추종 보호] 본전 청산 다이얼은 숨기고 실제 동작만 읽기 전용으로 안내한다
        #  (ANTI_TREND_HIDDEN_KEYS 주석 참조). ATR 손절 사용 시 발동 기준은 설정된
        #  BREAK_EVEN_PROFIT_RATE가 아니라 '손절폭(1R)'이므로 그대로 표시하면 오해를 부른다.
        _bep_trigger = ("손절폭(1R) 도달 시" if sell.get('USE_ATR_STOP', True)
                        else f"+{sell.get('BREAK_EVEN_PROFIT_RATE', 5.0)}% 도달 시")
        table.add_row(
            f"본전 청산\n[dim]{_bep_trigger} 손절선을 {sell.get('BREAK_EVEN_STOP_RATE', 0.5):+g}%로 상향[/dim]",
            "[dim](추세추종 검증값 — 조정 잠금)[/dim]", "사용")

        row("시간 청산 사용", "장기 횡보 종목 강제 매도", "SELL_STRATEGY['TIME_STOP_USE']", f"{sell.get('TIME_STOP_USE', True)}", key="TIME_STOP_USE")
        if sell.get('TIME_STOP_USE', True):
            table.add_row(
                f"  [dim]└ {sell.get('TIME_STOP_DAYS', 20)}일 경과 & 수익 "
                f"{sell.get('TIME_STOP_MIN_PROFIT_RATE', 0.0)}% 미만일 때만 청산\n"
                f"    (상방 모멘텀 살아있으면 유예)[/dim]",
                "[dim](추세추종 검증값 — 조정 잠금)[/dim]", "")
        table.add_row(
            f"매도(추세이탈) 점수\n[dim]점수가 이 값 미만 [bold]이고[/bold] 주가가 60일선 이탈 시 매도 (동시 충족)[/dim]",
            "[dim](추세추종 검증값 — 조정 잠금)[/dim]", f"{sell.get('SELL_SCORE')}")

        # [분리] 아래 항목들은 화면 분류·스크리닝 표시에만 쓰이고 매수/매도 판정에는 관여하지 않는다.
        #  매매 설정과 섞여 있어 오해를 부르던 것을 별도 섹션으로 뺐다.
        #  ('관심'(태동) 상태는 수동 모니터링용 표시이고, 이격도는 개별 종목 분석 화면의 평가 문구 전용)
        subheader("1-4. 화면 표시 전용 (매매 판정 무관)")
        row("관심 신호 최소 개수", "추세전환 초기신호 N개 이상 시 '관심'(태동) 표시 (0=미사용)", "ANALYSIS_THRESHOLDS['INTEREST_SIGNAL_MIN']", f"{thresholds.get('INTEREST_SIGNAL_MIN', 3)}", key="INTEREST_SIGNAL_MIN")
        row("관심 60일선 근접 비율", "60일선의 이 비율 이상이면 '돌파 시도' 신호로 인정", "ANALYSIS_THRESHOLDS['INTEREST_MA60_NEAR']", f"{thresholds.get('INTEREST_MA60_NEAR', 0.97)}", key="INTEREST_MA60_NEAR")
        row("과열 이격도 상한", "20일선 기준 이 비율 이상 시 '단기과열' 표시", "ANALYSIS_THRESHOLDS['DISPARITY_UPPER']", f"{thresholds.get('DISPARITY_UPPER', 110.0)}%", key="DISPARITY_UPPER")
        row("침체 이격도 하한", "20일선 기준 이 비율 이하 시 '과매도' 표시", "ANALYSIS_THRESHOLDS['DISPARITY_LOWER']", f"{thresholds.get('DISPARITY_LOWER', 90.0)}%", key="DISPARITY_LOWER")

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

        # [백테스트 보호] 국면 판정 파라미터·점수 보정은 숨김 (BACKTESTED_HIDDEN_KEYS 주석 참조).
        #  ON/OFF 킬 스위치만 노출하고, 실제 판정 기준은 아래 요약으로 읽기 전용 안내한다.
        subheader("2-2. 적응형 임계값 (시장국면)")
        regime = config.MARKET_REGIME_PARAMS
        row("사용 여부", "시장 국면 반영", "MARKET_REGIME_PARAMS['USE_ADAPTIVE_THRESHOLD']", f"{regime.get('USE_ADAPTIVE_THRESHOLD')}", key="USE_ADAPTIVE_THRESHOLD")
        if regime.get('USE_ADAPTIVE_THRESHOLD'):
            _ef = regime.get('REGIME_EMA_FAST', 9)
            _es = regime.get('REGIME_EMA_SLOW', 41)
            _cp = regime.get('REGIME_CONFIRM_PCT', 5.0)
            table.add_row(
                f"  [dim]└ 판정: EMA {_ef}/{_es} 교차 후 {_cp:g}% 진행 시 추세 확정\n"
                f"    보정: 강세 {regime.get('BULL_SCORE_ADJ', -0.5):+.1f} / 하락 미확정 "
                f"{regime.get('PENDING_DOWN_SCORE_ADJ', 0.5):+.1f} / 약세 {regime.get('BEAR_SCORE_ADJ', 0.5):+.1f}[/dim]",
                "[dim](백테스트 검증값 — 조정 잠금)[/dim]", "")

    # =========================================================
    # 3. 리스크 및 자산 배분 설정
    # =========================================================
    if group in (None, 3):
        header(3)
        subheader("3-1. 자산 배분/포지션", first=True)
        _ipr = config.settings.SYSTEM_INVEST_PER_STOCK
        _ipr_str = f"0 (자동 = {config.resolve_invest_ratio() * 100:.0f}%)" if not _ipr or _ipr <= 0 else f"{_ipr}"
        row("종목당 투자 비중", "0이면 1/최대보유종목수로 자동 계산", "SYSTEM_INVEST_PER_STOCK", _ipr_str, key="SYSTEM_INVEST_PER_STOCK")
        row("최대 보유 종목 수", "포트폴리오 최대 종목 개수", "SYSTEM_MAX_HOLDINGS", f"{config.settings.SYSTEM_MAX_HOLDINGS}", key="SYSTEM_MAX_HOLDINGS")
        row("자동매매 대상에 ETF 포함", "관심종목 내 ETF도 자동매매 대상으로 감시/매수", "SYSTEM_INCLUDE_ETF", f"{getattr(config.settings, 'SYSTEM_INCLUDE_ETF', False)}", key="SYSTEM_INCLUDE_ETF")
        slippage = getattr(config.settings, 'SLIPPAGE_RATE', 0.002)
        slippage_str = f"{slippage} (미사용)" if slippage == 0 else f"{slippage}"
        row("슬리피지 비율", "주문가 보정 및 백테스트 비용", "SLIPPAGE_RATE", slippage_str, key="SLIPPAGE_RATE")
        # [추세추종 보호] 변동성 타겟팅은 "자본대비 변동성에 한도를 둔다"는 추세추종 2원칙의
        #  구현부다. 끄면 전형 수익은 오르지만 MDD가 크게 악화되므로 조회 전용으로만 노출한다.
        _vt_on = getattr(config.settings, 'USE_VOLATILITY_TARGETING', True)
        table.add_row(
            "변동성 타겟팅\n[dim]ATR 기반 비중 조절 — 변동성 큰 종목의 비중을 자동 축소[/dim]",
            "[dim](추세추종 검증값 — 조정 잠금)[/dim]", f"{_vt_on}")
        if _vt_on:
            table.add_row(
                f"  [dim]└ 목표 연간 변동성 {getattr(config.settings, 'TARGET_VOLATILITY', 0.20)} / "
                f"비중 배수 {getattr(config.settings, 'VOLATILITY_SCALING_MIN', 0.4)} ~ "
                f"{getattr(config.settings, 'VOLATILITY_SCALING_MAX', 2.0)}[/dim]", "", "")

        subheader("3-2. 매수 필터")
        row("시장 필터링 사용", "지수 하락 시 신규 매수 보류", "USE_MARKET_FILTER", f"{getattr(config.settings, 'USE_MARKET_FILTER', True)}", key="USE_MARKET_FILTER")
        if getattr(config.settings, 'USE_MARKET_FILTER', True):
            row("시장 필터링 SMA (일)", "지수 추세 판단용 단순이동평균선", "MARKET_FILTER_MA", f"{getattr(config.settings, 'MARKET_FILTER_MA', 60)}", key="MARKET_FILTER_MA", indent=True)
        # [추세추종 보호] 상대강도(RS) 필터는 기본 OFF + 숨김 (ANTI_TREND_HIDDEN_KEYS 주석 참조).
        #  숨김 집합에서 키를 빼면 조회·편집·도움말에 자동으로 다시 나타난다.
        if "USE_RS_FILTER" not in ANTI_TREND_HIDDEN_KEYS:
            row("상대강도(RS) 필터 사용", "지수 대비 약세 종목 신규 매수 제외", "USE_RS_FILTER", f"{getattr(config.settings, 'USE_RS_FILTER', False)}", key="USE_RS_FILTER")
            if getattr(config.settings, 'USE_RS_FILTER', False):
                _rs_lb = getattr(config.settings, 'RS_FILTER_LOOKBACK', 0)
                _rs_lb_str = f"{_rs_lb}일" if _rs_lb > 0 else f"연동 ({config.INDICATOR_PARAMS.get('MOMENTUM_LOOKBACK', 126)}일)"
                row("상대강도(RS) 필터 기간", "지수 대비 수익률 비교 룩백 (0=가격 모멘텀 룩백 연동)", "RS_FILTER_LOOKBACK", _rs_lb_str, key="RS_FILTER_LOOKBACK", indent=True)
        row("상관계수 필터링 사용", "유사 테마 종목 중복 매수 방지", "USE_CORRELATION_FILTER", f"{getattr(config.settings, 'USE_CORRELATION_FILTER', True)}", key="USE_CORRELATION_FILTER")
        if getattr(config.settings, 'USE_CORRELATION_FILTER', True):
            row("상관계수 임계값", "동조화 판단 기준치 (0.0~1.0)", "CORRELATION_THRESHOLD", f"{getattr(config.settings, 'CORRELATION_THRESHOLD', 0.7)}", key="CORRELATION_THRESHOLD", indent=True)

        subheader("3-3. 비상 안전장치")
        row("연속 에러 허용", "시스템 중단 임계값", "SYSTEM_MAX_CONSECUTIVE_ERRORS", f"{getattr(config.settings, 'SYSTEM_MAX_CONSECUTIVE_ERRORS', 5)}", key="SYSTEM_MAX_CONSECUTIVE_ERRORS")
        row("일일 손실 제한 (%)", "비상 정지 기준 손실률 (0%면 비상 정지 OFF)", "SYSTEM_DAILY_LOSS_LIMIT", f"{getattr(config.settings, 'SYSTEM_DAILY_LOSS_LIMIT', 10.0)}", key="SYSTEM_DAILY_LOSS_LIMIT")
        # [표시 정직성] 아래 두 한도는 '상한선'이지 실효 다이얼이 아니다. 변동성 타겟팅이 켜져 있으면
        #  사이징 min 결합에서 항상 변동성층이 더 낮은 금액을 내므로 구속되지 않는다(config.py 실측 주석 참조).
        #  값만 보고 "1회 4% 리스크로 돌고 있다"고 오해하지 않도록 실효값을 함께 적는다.
        _vt_governs = getattr(config.settings, 'USE_VOLATILITY_TARGETING', True)
        _risk_pt = getattr(config.settings, 'SYSTEM_RISK_PER_TRADE', 4.0)
        row("1회 최대 리스크 (%)", "계좌 대비 1회 매매 손실폭 상한", "SYSTEM_RISK_PER_TRADE", f"{_risk_pt}", key="SYSTEM_RISK_PER_TRADE")
        row("총 오픈 리스크 한도 (%)", "보유 전체 동시 손절 잠재손실 상한 (0%면 미사용)", "SYSTEM_MAX_PORTFOLIO_RISK", f"{getattr(config.settings, 'SYSTEM_MAX_PORTFOLIO_RISK', 10.0)}", key="SYSTEM_MAX_PORTFOLIO_RISK")

        # [백테스트 보호] 3-4 리스크 한도 동적 스케일링(국면·휩소율·드로다운 배수)은
        #  조회·편집 화면 모두에서 숨긴다 (BACKTESTED_HIDDEN_KEYS 주석 참조).
        #  현재 적용 중인 배수와 그 사유는 자동매매 상태표·로그에서 실시간 확인할 수 있다.
        #   예: [리스크 스케일링] 신규 진입 리스크 한도 축소 x0.60 (KOSDAQ 하락 미확정 x0.6)

    # =========================================================
    # 4. 기술적 지표 파라미터
    # =========================================================
    if group in (None, 4):
        ind = config.INDICATOR_PARAMS
        header(4)
        subheader("4-1. 데이터·추세", first=True)
        row("데이터 조회 기간", "일봉 데이터 조회 범위", "INDICATOR_PARAMS['CHART_LOOKBACK_DAYS']", f"{ind.get('CHART_LOOKBACK_DAYS')}일")

        row("SAR (Start/Step/Max)", "파라볼릭 SAR 가속변수", "INDICATOR_PARAMS['SAR_AF_START', 'SAR_AF_STEP', 'SAR_AF_MAX']", f"{ind.get('SAR_AF_START')}/{ind.get('SAR_AF_STEP')}/{ind.get('SAR_AF_MAX')}")
        row("MACD (Fast/Slow/Sig)", "이동평균수렴확산", "INDICATOR_PARAMS['MACD_FAST', 'MACD_SLOW', 'MACD_SIGNAL']", f"{ind.get('MACD_FAST')}/{ind.get('MACD_SLOW')}/{ind.get('MACD_SIGNAL')}")
        row("단기 이평선(EMA) 기간", "단기 급등 추세 판단용", "INDICATOR_PARAMS['EMA_SHORT']", f"{ind.get('EMA_SHORT', 5)}")
        row("상승/하락 추세선 기간", "스윙 피봇 연결을 위한 룩백 기간", "INDICATOR_PARAMS['TREND_PERIOD']", f"{ind.get('TREND_PERIOD', 60)}일")

        subheader("4-2. 모멘텀")
        row("RSI (Period/Signal)", "상대강도지수 기간", "INDICATOR_PARAMS['RSI_PERIOD', 'RSI_SIGNAL']", f"{ind.get('RSI_PERIOD')}/{ind.get('RSI_SIGNAL')}")
        row("RSI (Up/Mid/Low)", "과매수/중심/과매도 기준", "INDICATOR_PARAMS['RSI_UPPER', 'RSI_MID', 'RSI_LOWER']", f"{ind.get('RSI_UPPER')}/{ind.get('RSI_MID')}/{ind.get('RSI_LOWER')}")
        row("CCI (Window/Up/Low)", "상품채널지수", "INDICATOR_PARAMS['CCI_WINDOW', 'CCI_UPPER', 'CCI_LOWER']", f"{ind.get('CCI_WINDOW')}/{ind.get('CCI_UPPER')}/{ind.get('CCI_LOWER')}")

        subheader("4-3. 강도·수급·가격구조")
        row("ADX 기간", "추세 강도 지표", "INDICATOR_PARAMS['ADX_PERIOD']", f"{ind.get('ADX_PERIOD')}")
        row("OBV EMA 기간", "거래량 추세 지수이동평균", "INDICATOR_PARAMS['OBV_MA_PERIOD']", f"{ind.get('OBV_MA_PERIOD')}")
        row("거래량 이동평균 기간", "수급 추세 및 폭발 판단용", "INDICATOR_PARAMS['VOLUME_MA_PERIOD']", f"{ind.get('VOLUME_MA_PERIOD', 20)}")
        row("거래량 폭발 배수", "이동평균 대비 폭발 기준", "INDICATOR_PARAMS['VOLUME_SPIKE_RATIO']", f"{ind.get('VOLUME_SPIKE_RATIO', 2.0)}")
        row("ATR 기간", "평균 진폭 (변동성)", "INDICATOR_PARAMS['ATR_PERIOD']", f"{ind.get('ATR_PERIOD')}")

        row("박스권 탐지 기간", "매물대 기반 박스권 탐지 룩백 봉 수 (일봉=일, 분봉=분)", "INDICATOR_PARAMS['BOX_PERIOD']", f"{ind.get('BOX_PERIOD', 30)}봉")
        row("박스권 매물대 %", "핵심 매물대 집중도", "INDICATOR_PARAMS['BOX_VALUE_AREA_PCT']", f"{ind.get('BOX_VALUE_AREA_PCT', 50.0)}%")

    # =========================================================
    # 5. 환경 및 시스템 설정
    # =========================================================
    if group in (None, 5):
        header(5)
        subheader("5-1. 거래 시간·주기", first=True)
        row("거래 시작 시간", "매매 허용 시작 시각 (HHMM, 기본 KRX 개장 0900)", "SYSTEM_TRADING_START_TIME", f"{getattr(config.settings, 'SYSTEM_TRADING_START_TIME', '0900')}")
        row("거래 종료 시간", "매매 허용 종료 시각 (HHMM, 기본 KRX 마감 1530)", "SYSTEM_TRADING_END_TIME", f"{getattr(config.settings, 'SYSTEM_TRADING_END_TIME', '1530')}")
        row("모니터링 주기 (초)", "자동매매 루프 실행 간격", "SYSTEM_TRADING_INTERVAL", f"{getattr(config.settings, 'SYSTEM_TRADING_INTERVAL', 180)}")

        subheader("5-2. 주문·체결 감시")
        row("체결 감시 주기", "주문 직후 체결 확인 간격", "CONCLUSION_CHECK_INTERVAL", f"{getattr(config.settings, 'CONCLUSION_CHECK_INTERVAL', 5)}")
        row("대기 모드 주기", "주문이 없는 평상시 체결 확인 간격", "CONCLUSION_CHECK_IDLE_INTERVAL", f"{getattr(config.settings, 'CONCLUSION_CHECK_IDLE_INTERVAL', 300)}")
        row("집중 감시 시간", "주문 후 집중 감시 유지 시간", "CONCLUSION_CHECK_ACTIVE_DURATION", f"{getattr(config.settings, 'CONCLUSION_CHECK_ACTIVE_DURATION', 60)}")
        row("미체결 취소 대기", "지정가 주문 유지 시간", "UNFILLED_ORDER_CANCEL_SECONDS", f"{getattr(config.settings, 'UNFILLED_ORDER_CANCEL_SECONDS', 120)}")

        subheader("5-3. 데이터·통신")
        row("차트 캐시 시간(분)", "일봉 데이터 메모리 캐시 유지", "CHART_CACHE_TTL_MINUTES", f"{getattr(config.settings, 'CHART_CACHE_TTL_MINUTES', 360)}")
        row("실시간 WebSocket 사용", "KIS 실시간 시세 push(끄면 REST 폴링). 토스 미지원", "USE_WEBSOCKET", f"{getattr(config.settings, 'USE_WEBSOCKET', True)}")
        row("장 종료 후 KRX 종가 기준", "모든 장 마감 후 현재가를 KRX 정규장 종가로 고정", "USE_KRX_CLOSE_AFTER_HOURS", f"{getattr(config.settings, 'USE_KRX_CLOSE_AFTER_HOURS', True)}")
        row("매매일지 웹서버 연동", "체결 내역을 원격 매매일지 서버로 전송", "JOURNAL_SYNC_USE", f"{getattr(config.settings, 'JOURNAL_SYNC_USE', False)}")

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
                        # [교차 검증] bool 스위치도 조합 모순의 절반을 차지한다 (슈퍼모멘텀 OFF 등)
                        _print_config_conflicts()
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

                # [입력 검증] 항목별 validator가 없어도 중앙 규칙표로 한 번 더 거른다
                _err = _range_error(item.get('name'), converted_val)
                if _err:
                    console.print(f"[red]{_err}[/red]")
                    continue

                item['set'](converted_val)

                if 'callback' in item:
                    item['callback']()

                # [교차 검증] 저장 후 조합 모순 확인 (개별 값은 정상인데 조합이 깨지는 경우)
                _print_config_conflicts()

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

# [백테스트 보호] 시장 국면 판정과 리스크 한도 스케일링의 파라미터는 KOSPI·KOSDAQ 15년
#  백테스트로 도출된 값이다. 임의로 바꾸면 검증 근거가 통째로 무효가 되고, 그 사실이
#  화면상 드러나지 않은 채 매매가 계속되므로 시스템 설정 메뉴(메인메뉴 0)에서 숨긴다.
#  설정 키와 로직은 내부적으로 그대로 유지되며, 조정이 필요하면 dynamic_config.json을
#  직접 편집한다(그 경우 '커스텀 설정 조회'(메뉴 0-6)에 기본값과 다른 항목으로 드러난다).
#
#  단, 기능 자체를 끄는 킬 스위치 MARKET_REGIME_PARAMS['USE_ADAPTIVE_THRESHOLD']는 노출한다 —
#  운영 중 국면 판정이 이상하게 동작할 때 코드 수정 없이 중단할 수단은 남겨야 한다.
BACKTESTED_HIDDEN_KEYS = {
    # 2-2. 적응형 임계값 — 국면별 점수 보정 및 판정 파라미터
    "BULL_SCORE_ADJ", "PENDING_UP_SCORE_ADJ", "PENDING_DOWN_SCORE_ADJ",
    "BEAR_SCORE_ADJ", "SIDEWAYS_SCORE_ADJ",
    "REGIME_EMA_FAST", "REGIME_EMA_SLOW", "REGIME_CONFIRM_PCT", "REGIME_WHIPSAW_LOOKBACK",
    "REGIME_MA_PERIOD", "REGIME_ADX_THRESHOLD",   # 데이터 부족 시 폴백용 (구 방식 잔여)
    # 3-4. 리스크 한도 동적 스케일링 — 국면·휩소율·드로다운 배수 일체
    "USE_REGIME_RISK_SCALING", "PENDING_DOWN_RISK_SCALE", "BEAR_RISK_SCALE",
    "USE_WHIPSAW_RISK_SCALING", "WHIPSAW_LO", "WHIPSAW_HI", "WHIPSAW_MIN_SCALE",
    "USE_DRAWDOWN_RISK_SCALING", "DD_LEVEL_1", "DD_SCALE_1", "DD_LEVEL_2", "DD_SCALE_2",
    "DD_LOOKBACK_DAYS", "GAP_RISK_BUFFER",
}

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
    # --- 아래는 '수익 종목을 일찍 잘라내는' 방향으로만 위험한 다이얼 (기능이 아니라 값이 문제) ---
    #  TIME_STOP_MIN_PROFIT_RATE: 0을 넘기면 +0.1~+2.9% 수익 중인 포지션도 기간 경과 시 강제 청산된다
    #   (engine.analyze_sell: profit_rate < time_stop_min_profit). '수익 종목은 보유' 원칙으로 3.0→0.0 확정.
    #  TIME_STOP_DAYS: 추세 전개 시간 확보를 위해 10→20으로 완화한 값. 줄이면 추세 초입을 잘라낸다.
    #  SELL_SCORE: 60일선 이탈 동시 조건이 코드에 고정돼 있으나, 값을 올리면 정배열 유지 중의
    #   통상적 눌림목에서도 청산이 잦아져 샹들리에 TS의 fat-tail 추종을 무력화한다. 5.0→4.0 완화 확정.
    #  변동성 타겟팅: 추세추종 2원칙 "자본대비 변동성에 한도를 둔다"의 구현부. 끄면 전형 수익은
    #   오르지만(median 8.6→14.9%) MDD가 -20→-30%로 악화된다.
    #  BREAK_EVEN_STOP_RATE: 30종목×3.8년 백테스트 스윕 결과, 현행 +0.5%는 fat-tail을 전혀
    #   해치지 않는다(>30% 거래 29건·최고 220.6%가 BEP ON/OFF 동일, 종목별 승패도 14:16 무차이).
    #   반면 +2.0%로 올리면 평균이익이 17.8%→9.9%로 반토막 나고 PF도 1.71→1.56으로 무너진다.
    #   '수익을 일찍 확정하는' 방향으로만 위험한 다이얼이므로 잠근다.
    #  BREAK_EVEN_PROFIT_RATE: ATR 손절 사용 시(기본값) 발동 기준이 손절폭(1R)으로 덮어써져
    #   이 값 자체는 쓰이지 않는다(backtest.py bep_activation / trader.py thresholds 주입).
    #   화면에 5.0%가 보이면 실제 동작(1R)과 달라 오해를 부르므로 숨기고 읽기 전용으로 안내한다.
    "TIME_STOP_MIN_PROFIT_RATE",
    "TIME_STOP_DAYS",
    "SELL_SCORE",
    "BREAK_EVEN_PROFIT_RATE",
    "BREAK_EVEN_STOP_RATE",
    "USE_VOLATILITY_TARGETING",
    "TARGET_VOLATILITY",
    "VOLATILITY_SCALING_MIN",
    "VOLATILITY_SCALING_MAX",
    "MR_VOL_STRENGTH",         # 역매수 체결강도 기준 (부속 설정)
    "MR_GRACE_LOSS_RATE",      # 역매수 보유분 점수매도 유예 손실폭 (부속 설정)
    #  USE_RS_FILTER / RS_FILTER_LOOKBACK: 상대강도 필터는 '지수 대비 열위 = 신규 매수 금지'라는
    #   이진 차단이라 추세 초입 진입과 정면충돌한다. 실증(37종목×9년, 신호 10,307건)에서 신호의
    #   28.6%를 잘라내고도 대박률↓·손실률↑·MDD↑로 순손실이었다(config.py USE_RS_FILTER 주석 참조).
    #   기본 OFF로 전환하고, 다시 켜는 다이얼 자체를 숨긴다.
    "USE_RS_FILTER",
    "RS_FILTER_LOOKBACK",
    # --- 2026-07-26 설정 감사: 편집 가능 71개를 30종목×3년으로 다이얼별 실측한 결과 ---
    #  기준값: 평균수익 +21.02% / MDD -22.51% / PF 1.74 / 거래 486건 / >50% 대박 12건 / 평균이익 +19.85%
    #
    #  USE_ATR_STOP: 끄면(고정 -7% 손절) >50% 대박이 12→4건으로 3분의 1이 잘리고
    #   평균이익 +19.85→+10.87%, PF 1.74→1.36, 거래는 486→711건으로 churn이 급증한다.
    #   한국 변동성에 고정 손절은 너무 타이트해 승자에서 먼저 털린다(drawdown-lever 실측의
    #   '한국은 ATR 유지'와 일치). 단일 토글 중 fat-tail 파괴력이 가장 크다.
    #  ATR_STOP_MULTIPLIER: USE_ATR_STOP을 잠그면 이것이 실질 손절폭 다이얼이 된다.
    #   2.0→1.0에서 거래 486→624건, MDD -22.51→-23.95%, PF 1.74→1.60, 중앙값 +3.45→-0.39%.
    #   BEP 다이얼과 같은 '좁히는 방향으로만 위험한' 부류라 함께 잠근다.
    #  MAX_ATR_STOP_LOSS_RATE: -15→-1%에서 거래가 2배(164→327)로 튀고 PF 1.91→1.56.
    #   데이터 오류 방어용 상한이지 튜닝 대상이 아니다.
    #  STOP_LOSS_RATE: USE_ATR_STOP이 잠긴 뒤에는 ATR 산출 실패 시의 폴백으로만 쓰인다.
    #   실제로 지배하지 않는 값을 '손절 수익률 -7%'로 띄우면 BREAK_EVEN_PROFIT_RATE와 같은
    #   오해를 부르고, 부호를 +로 잘못 넣어도 통과하던 자리다(실측: +5.0 입력 수용).
    #  TRAILING_STOP_CALLBACK_RATE: **죽은 다이얼**. 실효 콜백은 max(설정값, 3×ATR/고점)인데
    #   동적 콜백이 사실상 항상 5%를 넘어, 5→3%·5→2%로 낮춰도 거래 486건이 1건도 안 변한다.
    #   화면에만 보이고 동작하지 않으므로 읽기 전용 안내로 내린다.
    #  SUPER_MOMENTUM_USE: 끄면 +21.02→+18.17%인데, 진짜 문제는 BUY_RSI_MAX와의 조합이다.
    #   RSI 상한 50 + 슈퍼모멘텀 OFF = 30종목 3년간 매매 0건(폐지한 횡보 프리셋의 자기모순을
    #   CLI에서 손으로 재현할 수 있다). 이 스위치를 잠그면 조합 자체가 도달 불가능해진다.
    "USE_ATR_STOP",
    "ATR_STOP_MULTIPLIER",
    "MAX_ATR_STOP_LOSS_RATE",
    "STOP_LOSS_RATE",
    "TRAILING_STOP_CALLBACK_RATE",
    "SUPER_MOMENTUM_USE",
}


# [입력 검증] 설정 항목별 허용 범위 (min, max, 안내문). 둘 중 하나가 None이면 그쪽은 무제한.
#
#  왜 필요한가: 2026-07-26 감사 시점에 편집 가능 71개 중 범위 검증이 걸린 항목은
#  SYSTEM_INVEST_PER_STOCK 하나뿐이었다. `validator` 훅은 있었지만 나머지 12개는 전부
#  편집 불가능한 BACKTESTED_HIDDEN_KEYS에 붙어 있어 실효가 없었다. 실측 결과
#  SYSTEM_RISK_PER_TRADE=50, BUY_SCORE=0, BUY_RSI_MAX=0, STOP_LOSS_RATE=+5(부호 오입력),
#  RSI_PERIOD=1이 전부 그대로 수용됐다.
#
#  범위는 '추세추종을 깨지 않는 폭'으로 잡되, 정상적인 튜닝은 막지 않는다.
#  숨김(HIDDEN_KEYS)이 '이 항목은 건드리지 말 것'이라면, 이쪽은 '이 항목은 이 폭 안에서'다.
_RANGE_RULES = {
    # --- 진입 ---
    #  BUY_RSI_MAX 하한 55: 50으로 낮추면 30종목 3년 실측에서 수익이 반토막(+21.02→+11.73%)
    #   나고 거래가 486→213건으로 줄었다. 이 스코어러는 점수 7.0을 넘으려면 RSI가 50 위여야 해서
    #   상한을 50에 붙이면 진입 자체가 고갈된다.
    "BUY_SCORE":                  (3.0, 10.0, "매수 게이트가 사라지거나 도달 불가한 값"),
    "RISE_SCORE":                 (1.0, 10.0, None),
    "BUY_RSI_MAX":                (55.0, 95.0, "55 미만은 점수 게이트와 충돌해 진입이 고갈됩니다"),
    "BUY_VOL_STRENGTH":           (0.0, 300.0, None),
    "BUY_ASK_BID_RATIO":          (0.0, 10.0, None),
    # --- 슈퍼 모멘텀 (스위치는 잠금, 다이얼만 조정 가능) ---
    "SUPER_MOMENTUM_SCORE":       (5.0, 10.0, None),
    "SUPER_BUY_RSI_MAX":          (60.0, 100.0, None),
    "SUPER_MOMENTUM_W52_POS":     (50.0, 100.0, None),
    # --- 피라미딩 ---
    "PYRAMIDING_MAX_COUNT":       (0, 5, None),
    "PYRAMIDING_PROFIT_TRIGGER":  (1.0, 100.0, None),
    "PYRAMIDING_RATIO":           (0.1, 1.0, None),
    # --- 청산 ---
    #  TS 발동률 상한 30: 실측상 999로 올리면 트레일링 스탑이 사실상 꺼져 주청산 수단이 사라진다
    #   (수익은 표본에 따라 올라 보이지만 MDD가 -22.51→-26.29%로 악화).
    "TRAILING_STOP_ACTIVATION_RATE": (1.0, 30.0, "30% 초과는 트레일링 스탑을 사실상 비활성화합니다"),
    # --- 리스크 ---
    #  SYSTEM_RISK_PER_TRADE 상한 10: drawdown-lever 실측에서 MDD 통제의 핵심 레버였다
    #   (5→3%가 MDD를 좌우). 4.0이 50.0으로 바뀌는 것을 아무것도 막지 않던 자리다.
    "SYSTEM_RISK_PER_TRADE":      (0.0, 10.0, "1회 리스크 10% 초과는 계좌를 몇 번의 손절로 소진시킵니다"),
    "SYSTEM_MAX_PORTFOLIO_RISK":  (0.0, 30.0, None),
    "SYSTEM_DAILY_LOSS_LIMIT":    (0.0, 30.0, None),
    "SYSTEM_MAX_HOLDINGS":        (1, 20, None),
    "SYSTEM_MAX_CONSECUTIVE_ERRORS": (1, 100, None),
    "CORRELATION_THRESHOLD":      (0.1, 1.0, None),
    "MARKET_FILTER_MA":           (5, 250, None),
    "SLIPPAGE_RATE":              (0.0, 0.05, None),
    # --- 지표 기간 (0·1봉은 계산 자체가 불가) ---
    "EMA_SHORT":                  (2, 250, None),
    "TREND_PERIOD":               (5, 250, None),
    "RSI_PERIOD":                 (2, 250, None),
    "RSI_SIGNAL":                 (2, 250, None),
    "CCI_WINDOW":                 (2, 250, None),
    "ADX_PERIOD":                 (2, 250, None),
    "ATR_PERIOD":                 (2, 250, None),
    "BOX_PERIOD":                 (5, 250, None),
    "OBV_MA_PERIOD":              (2, 250, None),
    "VOLUME_MA_PERIOD":           (2, 250, None),
    "MACD_FAST":                  (2, 250, None),
    "MACD_SLOW":                  (2, 250, None),
    "MACD_SIGNAL":                (2, 250, None),
    "VOLUME_SPIKE_RATIO":         (1.0, 20.0, None),
    "BOX_VALUE_AREA_PCT":         (10.0, 100.0, None),
    "SAR_AF_START":               (0.001, 1.0, None),
    "SAR_AF_STEP":                (0.001, 1.0, None),
    "SAR_AF_MAX":                 (0.01, 1.0, None),
    "RSI_UPPER":                  (50, 100, None),
    "RSI_MID":                    (1, 99, None),
    "RSI_LOWER":                  (0, 50, None),
    "CCI_UPPER":                  (0, 500, None),
    "CCI_LOWER":                  (-500, 0, None),
    "DISPARITY_UPPER":            (100.0, 200.0, None),
    "DISPARITY_LOWER":            (1.0, 100.0, None),
    "INTEREST_SIGNAL_MIN":        (0, 10, None),
    "INTEREST_MA60_NEAR":         (0.5, 1.5, None),
    # --- 시간·주기 ---
    #  CHART_LOOKBACK_DAYS 하한 373: EMA120·52주 밴드에 250봉이 필요하고 250거래일 ≈ 373달력일.
    "CHART_LOOKBACK_DAYS":        (373, 3650, "250봉(EMA120·52주 밴드)을 채우려면 373일 이상이 필요합니다"),
    "CHART_CACHE_TTL_MINUTES":    (0, 10080, None),
    "SYSTEM_TRADING_INTERVAL":    (5, 3600, None),
    "CONCLUSION_CHECK_INTERVAL":  (1, 600, None),
    "CONCLUSION_CHECK_ACTIVE_DURATION": (1, 3600, None),
    "CONCLUSION_CHECK_IDLE_INTERVAL":   (1, 3600, None),
    "UNFILLED_ORDER_CANCEL_SECONDS":    (1, 3600, None),
}


def _range_error(name, value):
    """_RANGE_RULES 위반 시 사용자에게 보여줄 문구. 통과하면 None."""
    rule = _RANGE_RULES.get(name)
    if not rule:
        return None
    lo, hi, why = rule
    try:
        v = float(value)
    except (TypeError, ValueError):
        return None
    if lo is not None and v < lo:
        pass
    elif hi is not None and v > hi:
        pass
    else:
        return None
    bound = (f"{lo:g} ~ {hi:g}" if lo is not None and hi is not None
             else (f"{lo:g} 이상" if lo is not None else f"{hi:g} 이하"))
    msg = f"허용 범위를 벗어났습니다: {name} 는 {bound} 이어야 합니다."
    return f"{msg}\n{why}" if why else msg


# [교차 검증] 개별 값은 정상 범위인데 '조합'이 시스템을 멈추거나 자기모순이 되는 경우를 잡는다.
#  범위 검증으로도 숨김으로도 막을 수 없는 부류라 별도로 확인한다.
#  대표 사례가 폐지된 횡보 프리셋의 자기모순(매수 RSI 상한 50 + 슈퍼모멘텀 OFF = 3.8년 매매 0건)이다.
def check_config_conflicts():
    """현재 설정의 조합 모순을 점검해 경고 문구 리스트를 돌려준다(빈 리스트면 정상)."""
    t = config.ANALYSIS_THRESHOLDS
    s = config.SELL_STRATEGY
    i = config.INDICATOR_PARAMS
    warns = []

    def _g(d, k, default=None):
        """설정값 조회. 키가 없을 때뿐 아니라 값이 None/빈값일 때도 기본값으로 떨어진다.

        dict.get의 기본값은 '키 부재'에만 적용되는데, dynamic_config.json에 키가 null로
        남아 있는 경우가 있어 그대로 두면 아래 비교 연산이 TypeError로 죽는다.
        조합 점검은 설정 저장 직후에 도는 보조 기능이므로 절대 예외를 던져선 안 된다.
        """
        try:
            v = d.get(k, default)
        except Exception:
            return default
        return default if v is None else v

    # 1) 진입 자체가 불가능해지는 조합 (가장 위험 — 증상이 '매매가 안 됨'으로만 나타난다)
    buy_rsi = _g(t, "BUY_RSI_MAX", 70.0)
    if not _g(t, "SUPER_MOMENTUM_USE", True) and buy_rsi is not None and buy_rsi <= 55:
        warns.append(
            f"매수 RSI 상한({buy_rsi:g})이 낮은데 슈퍼 모멘텀이 꺼져 있습니다. "
            "이 조합은 매수 신호가 0건이 됩니다 (폐지된 횡보 프리셋과 동일한 자기모순).")
    if _g(t, "BUY_SCORE", 7.0) > 10.0:
        warns.append("매수 기준 점수가 만점(10.0)을 넘어 어떤 종목도 진입할 수 없습니다.")
    if _g(t, "SUPER_MOMENTUM_USE", True) and _g(t, "SUPER_MOMENTUM_SCORE", 8.0) < _g(t, "BUY_SCORE", 7.0):
        warns.append("슈퍼 모멘텀 발동 점수가 매수 기준 점수보다 낮아 완화 조건이 상시 적용됩니다.")

    # 2) 지표 파라미터의 대소 관계
    if _g(i, "MACD_FAST", 12) >= _g(i, "MACD_SLOW", 26):
        warns.append("MACD Fast가 Slow 이상입니다. MACD 부호가 뒤집혀 추세 판정이 반대로 갑니다.")
    if not (_g(i, "RSI_LOWER", 30) < _g(i, "RSI_MID", 50) < _g(i, "RSI_UPPER", 70)):
        warns.append("RSI 과매도 < 중심선 < 과매수 순서가 어긋났습니다.")
    if _g(i, "CCI_LOWER", -100) >= _g(i, "CCI_UPPER", 100):
        warns.append("CCI 과매도 기준이 과매수 기준 이상입니다.")
    if _g(t, "DISPARITY_LOWER", 90.0) >= _g(t, "DISPARITY_UPPER", 110.0):
        warns.append("침체 이격도 하한이 과열 상한 이상입니다. (표시 전용)")

    # 3) 리스크 한도 정합
    risk = getattr(config.settings, "SYSTEM_RISK_PER_TRADE", 4.0)
    port = getattr(config.settings, "SYSTEM_MAX_PORTFOLIO_RISK", 10.0)
    holds = getattr(config.settings, "SYSTEM_MAX_HOLDINGS", 4)
    if risk > 0 and port > 0 and risk > port:
        warns.append(f"1회 리스크({risk:g}%)가 총 오픈 리스크 한도({port:g}%)보다 큽니다. 첫 진입부터 한도를 넘습니다.")
    if risk > 0 and port > 0 and holds and risk * holds < port:
        warns.append(
            f"1회 리스크 {risk:g}% × 최대 보유 {holds}종목 = {risk*holds:g}% 로 "
            f"총 한도({port:g}%)에 못 미쳐, 포트폴리오 한도가 실질적으로 작동하지 않습니다.")
    # [사이징 주도권] 변동성 타겟팅은 메뉴에서 숨겨져 있지만 dynamic_config.json 직접 편집으로는
    #  꺼질 수 있다. 꺼지는 순간 사이징 주도권이 변동성층에서 리스크 한도로 넘어가 포지션이
    #  일제히 커진다(실측: 리스크층 구속 0.0% → 5.6%, 전형 수익 median 8.6→14.9% / MDD -20→-30%).
    #  '값 하나가 조용히 바뀌었는데 노출만 커진' 상태를 운용자가 모르고 지나치지 않도록 경고한다.
    if not getattr(config.settings, "USE_VOLATILITY_TARGETING", True):
        warns.append(
            "변동성 타겟팅이 꺼져 있습니다. 사이징 주도권이 1회 리스크 한도"
            f"({risk:g}%)로 넘어가 종목당 노출이 크게 늘고 MDD가 악화됩니다(실측 -20%→-30%).")

    # 4) 청산 수단이 모두 사라지는 경우 (익절 계열은 기본 OFF라 TS·손절이 유일한 출구다)
    if _g(s, "TRAILING_STOP_ACTIVATION_RATE", 10.0) > 30 and not _g(s, "USE_ATR_STOP", True):
        warns.append("트레일링 스탑이 사실상 비활성인데 ATR 손절도 꺼져 있어 청산 수단이 남지 않습니다.")

    # 5) 조회 기간이 지표 요구를 못 채우는 경우
    look = _g(i, "CHART_LOOKBACK_DAYS", 730)
    if look and look < 373:
        warns.append(f"차트 조회 기간({look}일)이 250봉에 못 미쳐 EMA120·52주 밴드가 계산되지 않습니다.")

    return warns


def _print_config_conflicts():
    """조합 경고가 있으면 화면에 띄운다. 값은 이미 저장된 뒤이므로 '차단'이 아니라 '경고'다."""
    try:
        warns = check_config_conflicts()
    except Exception:
        return
    if not warns:
        return
    console.print()
    for w in warns:
        console.print(f"[bold yellow]⚠️  설정 조합 경고:[/bold yellow] [yellow]{w}[/yellow]")


def _entry_strategy_items():
    """매수/진입 조건 + 서브전략 항목 (섹션 1-1, 1-2)"""
    items = [
        {"desc": "매수 기준 점수", "help": "진입 임계값 (종합 점수)", "name": "BUY_SCORE", "type": "float", "section": "1-1. 진입 조건",
         "get": lambda: config.ANALYSIS_THRESHOLDS["BUY_SCORE"], "set": lambda v: config.ANALYSIS_THRESHOLDS.update({"BUY_SCORE": v})},
        {"desc": "상승 추세 점수", "help": "'상승' 상태 분류 기준", "name": "RISE_SCORE", "type": "float", "section": "1-1. 진입 조건",
         "get": lambda: config.ANALYSIS_THRESHOLDS["RISE_SCORE"], "set": lambda v: config.ANALYSIS_THRESHOLDS.update({"RISE_SCORE": v})},
        {"desc": "관심 신호 최소 개수", "help": "추세전환 초기신호 N개 이상 시 '관심'(태동) 표시 (0=미사용)", "name": "INTEREST_SIGNAL_MIN", "type": "int", "section": "1-4. 화면 표시 전용",
         "get": lambda: config.ANALYSIS_THRESHOLDS.get("INTEREST_SIGNAL_MIN", 3), "set": lambda v: config.ANALYSIS_THRESHOLDS.update({"INTEREST_SIGNAL_MIN": v})},
        {"desc": "관심 60일선 근접 비율", "help": "60일선의 이 비율 이상이면 '돌파 시도' 신호로 인정 (예: 0.97)", "name": "INTEREST_MA60_NEAR", "type": "float", "section": "1-4. 화면 표시 전용",
         "get": lambda: config.ANALYSIS_THRESHOLDS.get("INTEREST_MA60_NEAR", 0.97), "set": lambda v: config.ANALYSIS_THRESHOLDS.update({"INTEREST_MA60_NEAR": v})},
        {"desc": "매수 허용 RSI 상한", "help": "과열 방지 (이 값보다 낮아야 매수)", "name": "BUY_RSI_MAX", "type": "float", "section": "1-1. 진입 조건",
         "get": lambda: config.ANALYSIS_THRESHOLDS["BUY_RSI_MAX"], "set": lambda v: config.ANALYSIS_THRESHOLDS.update({"BUY_RSI_MAX": v})},
        {"desc": "매수 체결강도 기준", "help": "수급 확인 (이 값 이상이어야 매수)", "name": "BUY_VOL_STRENGTH", "type": "float", "section": "1-1. 진입 조건",
         "get": lambda: config.ANALYSIS_THRESHOLDS.get("BUY_VOL_STRENGTH", 100.0), "set": lambda v: config.ANALYSIS_THRESHOLDS.update({"BUY_VOL_STRENGTH": v})},
        {"desc": "매도잔량비 자동 연동", "help": "체결강도 100% 기준으로 비례하여 매도잔량비를 자동 조정", "name": "AUTO_ADJUST_ASK_BID_RATIO", "type": "bool", "choices": ["y", "n"], "section": "1-1. 진입 조건",
         "get": lambda: config.ANALYSIS_THRESHOLDS.get("AUTO_ADJUST_ASK_BID_RATIO", config.ANALYSIS_THRESHOLDS.get('AUTO_ADJUST_ASK_BID_RATIO', True)), "set": lambda v: config.ANALYSIS_THRESHOLDS.update({"AUTO_ADJUST_ASK_BID_RATIO": v})},
        {"desc": "매도잔량 비율 기준", "help": "가짜 체결강도 방어 (체결강도 100% 기준 비율, 0: 미사용)", "name": "BUY_ASK_BID_RATIO", "type": "float", "section": "1-1. 진입 조건",
         "get": lambda: config.ANALYSIS_THRESHOLDS.get("BUY_ASK_BID_RATIO", config.ANALYSIS_THRESHOLDS.get('BUY_ASK_BID_RATIO', 1.0)), "set": lambda v: config.ANALYSIS_THRESHOLDS.update({"BUY_ASK_BID_RATIO": v})},
        {"desc": "과열 이격도 상한", "help": "20일선 대비 '단기 과열' 표시 기준 (예: 110.0)", "name": "DISPARITY_UPPER", "type": "float", "section": "1-4. 화면 표시 전용",
         "get": lambda: config.ANALYSIS_THRESHOLDS.get("DISPARITY_UPPER", 110.0), "set": lambda v: config.ANALYSIS_THRESHOLDS.update({"DISPARITY_UPPER": v})},
        {"desc": "침체 이격도 하한", "help": "20일선 대비 '과매도' 표시 기준 (예: 90.0)", "name": "DISPARITY_LOWER", "type": "float", "section": "1-4. 화면 표시 전용",
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
         "get": lambda: config.SELL_STRATEGY.get("MR_GRACE_LOSS_RATE", -7.0), "set": lambda v: config.SELL_STRATEGY.update({"MR_GRACE_LOSS_RATE": v})},
        {"desc": "슈퍼 모멘텀(RSI 유연화) 사용", "help": "주도주 랠리 시 RSI 허용치 상향", "name": "SUPER_MOMENTUM_USE", "type": "bool", "choices": ["y", "n"], "section": "1-2. 서브전략 — 슈퍼 모멘텀",
         "get": lambda: config.ANALYSIS_THRESHOLDS.get("SUPER_MOMENTUM_USE", True), "set": lambda v: config.ANALYSIS_THRESHOLDS.update({"SUPER_MOMENTUM_USE": v})},
        {"desc": "슈퍼 모멘텀 발동 점수", "help": "기준 점수 이상 & 신고가 90% 근접 시 발동", "name": "SUPER_MOMENTUM_SCORE", "type": "float", "section": "1-2. 서브전략 — 슈퍼 모멘텀",
         "get": lambda: config.ANALYSIS_THRESHOLDS.get("SUPER_MOMENTUM_SCORE", 8.0), "set": lambda v: config.ANALYSIS_THRESHOLDS.update({"SUPER_MOMENTUM_SCORE": v})},
        {"desc": "슈퍼 52주 위치 기준", "help": "신고가 근접 여부 (예: 90.0% 이상)", "name": "SUPER_MOMENTUM_W52_POS", "type": "float", "section": "1-2. 서브전략 — 슈퍼 모멘텀",
         "get": lambda: config.ANALYSIS_THRESHOLDS.get("SUPER_MOMENTUM_W52_POS", 90.0), "set": lambda v: config.ANALYSIS_THRESHOLDS.update({"SUPER_MOMENTUM_W52_POS": v})},
        {"desc": "슈퍼 모멘텀 매수 RSI", "help": "발동 시 완화되는 진입 허용 RSI (예: 75.0)", "name": "SUPER_BUY_RSI_MAX", "type": "float", "section": "1-2. 서브전략 — 슈퍼 모멘텀",
         "get": lambda: config.ANALYSIS_THRESHOLDS.get("SUPER_BUY_RSI_MAX", 80.0), "set": lambda v: config.ANALYSIS_THRESHOLDS.update({"SUPER_BUY_RSI_MAX": v})},
        {"desc": "슈퍼 모멘텀 과열 매도 RSI", "help": "추세 유지 시 매도 지연 RSI (예: 85.0)", "name": "SUPER_TAKE_PROFIT_RSI", "type": "float", "section": "1-2. 서브전략 — 슈퍼 모멘텀",
         "get": lambda: config.SELL_STRATEGY.get("SUPER_TAKE_PROFIT_RSI", 90.0), "set": lambda v: config.SELL_STRATEGY.update({"SUPER_TAKE_PROFIT_RSI": v})},
        {"desc": "피라미딩(수익 증액) 사용", "help": "수익으로 추세 검증된 포지션만 증액", "name": "PYRAMIDING_USE", "type": "bool", "choices": ["y", "n"], "section": "1-2. 서브전략 — 피라미딩",
         "get": lambda: config.ANALYSIS_THRESHOLDS.get("PYRAMIDING_USE", True), "set": lambda v: config.ANALYSIS_THRESHOLDS.update({"PYRAMIDING_USE": v})},
        {"desc": "증액 발동 수익률(%)", "help": "이 수익률 이상 & 매수신호 유지 시 증액 (예: 10.0)", "name": "PYRAMIDING_PROFIT_TRIGGER", "type": "float", "section": "1-2. 서브전략 — 피라미딩",
         "get": lambda: config.ANALYSIS_THRESHOLDS.get("PYRAMIDING_PROFIT_TRIGGER", 10.0), "set": lambda v: config.ANALYSIS_THRESHOLDS.update({"PYRAMIDING_PROFIT_TRIGGER": v})},
        {"desc": "증액 비율", "help": "보유 수량 대비 증액 수량 비율 (예: 0.5 = 50%)", "name": "PYRAMIDING_RATIO", "type": "float", "section": "1-2. 서브전략 — 피라미딩",
         "get": lambda: config.ANALYSIS_THRESHOLDS.get("PYRAMIDING_RATIO", 0.5), "set": lambda v: config.ANALYSIS_THRESHOLDS.update({"PYRAMIDING_RATIO": v})},
        {"desc": "최대 증액 횟수", "help": "포지션당 피라미딩 허용 횟수 (기본 3회)", "name": "PYRAMIDING_MAX_COUNT", "type": "int", "section": "1-2. 서브전략 — 피라미딩",
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
        {"desc": "익절 수익률(%)", "help": "목표 수익 달성 시 매도 (0: 미사용)", "name": "TAKE_PROFIT_RATE", "type": "float", "section": "1-3. 청산 — 손절·트레일링·시간",
         "get": lambda: config.SELL_STRATEGY["TAKE_PROFIT_RATE"], "set": lambda v: config.SELL_STRATEGY.update({"TAKE_PROFIT_RATE": v})},
        {"desc": "반익절 사용", "help": "익절 수익률의 절반 도달 시 50% 선매도", "name": "HALF_TAKE_PROFIT_USE", "type": "bool", "choices": ["y", "n"], "section": "1-3. 청산 — 손절·트레일링·시간",
         "get": lambda: config.SELL_STRATEGY.get("HALF_TAKE_PROFIT_USE", False), "set": lambda v: config.SELL_STRATEGY.update({"HALF_TAKE_PROFIT_USE": v})},
        {"desc": "과열 매도 RSI", "help": "RSI 과열 시 선제 매도", "name": "TAKE_PROFIT_RSI", "type": "float", "section": "1-3. 청산 — 손절·트레일링·시간",
         "get": lambda: config.SELL_STRATEGY["TAKE_PROFIT_RSI"], "set": lambda v: config.SELL_STRATEGY.update({"TAKE_PROFIT_RSI": v})},
        {"desc": "TS 발동 수익률(%)", "help": "트레일링 스탑 감시 시작점", "name": "TRAILING_STOP_ACTIVATION_RATE", "type": "float", "section": "1-3. 청산 — 손절·트레일링·시간",
         "get": lambda: config.SELL_STRATEGY.get("TRAILING_STOP_ACTIVATION_RATE", 10.0), "set": lambda v: config.SELL_STRATEGY.update({"TRAILING_STOP_ACTIVATION_RATE": v})},
        {"desc": "TS 하락 감지율(%)", "help": "최고가 대비 하락 시 매도", "name": "TRAILING_STOP_CALLBACK_RATE", "type": "float", "section": "1-3. 청산 — 손절·트레일링·시간",
         "get": lambda: config.SELL_STRATEGY.get("TRAILING_STOP_CALLBACK_RATE", 5.0), "set": lambda v: config.SELL_STRATEGY.update({"TRAILING_STOP_CALLBACK_RATE": v})},
        {"desc": "손절 수익률(%)", "help": "손실 제한 (Stop Loss) (0: 미사용)", "name": "STOP_LOSS_RATE", "type": "float", "section": "1-3. 청산 — 손절·트레일링·시간",
         "get": lambda: config.SELL_STRATEGY["STOP_LOSS_RATE"], "set": lambda v: config.SELL_STRATEGY.update({"STOP_LOSS_RATE": v})},
        {"desc": "ATR 손절 사용", "help": "변동성 기반 동적 손절", "name": "USE_ATR_STOP", "type": "bool", "choices": ["y", "n"], "section": "1-3. 청산 — 손절·트레일링·시간",
         "get": lambda: config.SELL_STRATEGY.get("USE_ATR_STOP", True), "set": lambda v: config.SELL_STRATEGY.update({"USE_ATR_STOP": v})},
        {"desc": "ATR 손절 배수", "help": "ATR * 배수 만큼 손절폭 설정 (0: 미사용)", "name": "ATR_STOP_MULTIPLIER", "type": "float", "section": "1-3. 청산 — 손절·트레일링·시간",
         "get": lambda: config.SELL_STRATEGY.get("ATR_STOP_MULTIPLIER", 2.0), "set": lambda v: config.SELL_STRATEGY.update({"ATR_STOP_MULTIPLIER": v})},
        {"desc": "ATR 최대 손절률(%)", "help": "데이터 오류 및 과열 변동성으로 인한 과도한 리스크 제한 (0: 미사용)", "name": "MAX_ATR_STOP_LOSS_RATE", "type": "float", "section": "1-3. 청산 — 손절·트레일링·시간",
         "get": lambda: config.SELL_STRATEGY.get("MAX_ATR_STOP_LOSS_RATE", -15.0), "set": lambda v: config.SELL_STRATEGY.update({"MAX_ATR_STOP_LOSS_RATE": v})},
        {"desc": "본전 청산 발동 수익률(%)", "help": "최고 수익률이 이 값에 도달하면 손절선 상향 (0: 미사용, ATR 사용 시 동적 연동)", "name": "BREAK_EVEN_PROFIT_RATE", "type": "float", "section": "1-3. 청산 — 손절·트레일링·시간",
         "get": lambda: config.SELL_STRATEGY.get("BREAK_EVEN_PROFIT_RATE", 5.0), "set": lambda v: config.SELL_STRATEGY.update({"BREAK_EVEN_PROFIT_RATE": v})},
        {"desc": "본전 청산 손절선(%)", "help": "본전 청산 발동 시 변경될 손절률 (예: 0.5)", "name": "BREAK_EVEN_STOP_RATE", "type": "float", "section": "1-3. 청산 — 손절·트레일링·시간",
         "get": lambda: config.SELL_STRATEGY.get("BREAK_EVEN_STOP_RATE", 0.5), "set": lambda v: config.SELL_STRATEGY.update({"BREAK_EVEN_STOP_RATE": v})},
        {"desc": "시간 청산 사용", "help": "장기 횡보 시 강제 매도", "name": "TIME_STOP_USE", "type": "bool", "choices": ["y", "n"], "section": "1-3. 청산 — 손절·트레일링·시간",
         "get": lambda: config.SELL_STRATEGY.get("TIME_STOP_USE", True), "set": lambda v: config.SELL_STRATEGY.update({"TIME_STOP_USE": v})},
        {"desc": "시간 청산 기준일", "help": "매수 후 제한 일수 (예: 10)", "name": "TIME_STOP_DAYS", "type": "int", "section": "1-3. 청산 — 손절·트레일링·시간",
         "get": lambda: config.SELL_STRATEGY.get("TIME_STOP_DAYS", 20), "set": lambda v: config.SELL_STRATEGY.update({"TIME_STOP_DAYS": v})},
        {"desc": "시간청산 최소수익(%)", "help": "기간 내 달성해야 할 목표치", "name": "TIME_STOP_MIN_PROFIT_RATE", "type": "float", "section": "1-3. 청산 — 손절·트레일링·시간",
         "get": lambda: config.SELL_STRATEGY.get("TIME_STOP_MIN_PROFIT_RATE", 0.0), "set": lambda v: config.SELL_STRATEGY.update({"TIME_STOP_MIN_PROFIT_RATE": v})},
        {"desc": "매도(추세이탈) 점수", "help": "점수 하락 시 매도", "name": "SELL_SCORE", "type": "float", "section": "1-3. 청산 — 손절·트레일링·시간",
         "get": lambda: config.SELL_STRATEGY["SELL_SCORE"], "set": lambda v: config.SELL_STRATEGY.update({"SELL_SCORE": v})},
        {"desc": "방어적 반매도 사용", "help": "SAR 매도 + 5일선 이탈 시 50% 수익실현 및 리스크 회피", "name": "DEFENSIVE_HALF_SELL_USE", "type": "bool", "choices": ["y", "n"], "section": "1-3. 청산 — 손절·트레일링·시간",
         "get": lambda: config.SELL_STRATEGY.get("DEFENSIVE_HALF_SELL_USE", False), "set": lambda v: config.SELL_STRATEGY.update({"DEFENSIVE_HALF_SELL_USE": v})},
    ]
    # [추세추종 보호] 반추세성 청산 설정은 편집 목록에서 숨김 (ANTI_TREND_HIDDEN_KEYS 주석 참조)
    return [it for it in items if it["name"] not in ANTI_TREND_HIDDEN_KEYS]

def modify_sell_strategy():
    return _edit_config_table("매도/청산 전략 설정 (SELL_STRATEGY)", _sell_strategy_items)

def _indicator_items():
    """기술적 지표 항목 (섹션 4-1 ~ 4-5)"""
    items = [
        {"desc": "데이터 조회 기간(일)", "help": "일봉 데이터 조회 범위", "name": "CHART_LOOKBACK_DAYS", "type": "int", "section": "4-1. 데이터·추세",
         "get": lambda: config.INDICATOR_PARAMS["CHART_LOOKBACK_DAYS"], "set": lambda v: config.INDICATOR_PARAMS.update({"CHART_LOOKBACK_DAYS": v})},

        {"desc": "SAR 가속변수 시작(Start)", "help": "파라볼릭 SAR 초기값", "name": "SAR_AF_START", "type": "float", "section": "4-1. 데이터·추세",
         "get": lambda: config.INDICATOR_PARAMS["SAR_AF_START"], "set": lambda v: config.INDICATOR_PARAMS.update({"SAR_AF_START": v})},
        {"desc": "SAR 가속변수 증가(Step)", "help": "파라볼릭 SAR 증가값", "name": "SAR_AF_STEP", "type": "float", "section": "4-1. 데이터·추세",
         "get": lambda: config.INDICATOR_PARAMS["SAR_AF_STEP"], "set": lambda v: config.INDICATOR_PARAMS.update({"SAR_AF_STEP": v})},
        {"desc": "SAR 가속변수 최대(Max)", "help": "파라볼릭 SAR 최대값", "name": "SAR_AF_MAX", "type": "float", "section": "4-1. 데이터·추세",
         "get": lambda: config.INDICATOR_PARAMS["SAR_AF_MAX"], "set": lambda v: config.INDICATOR_PARAMS.update({"SAR_AF_MAX": v})},
        {"desc": "MACD Fast EMA", "help": "단기 지수이동평균", "name": "MACD_FAST", "type": "int", "section": "4-1. 데이터·추세",
         "get": lambda: config.INDICATOR_PARAMS["MACD_FAST"], "set": lambda v: config.INDICATOR_PARAMS.update({"MACD_FAST": v})},
        {"desc": "MACD Slow EMA", "help": "장기 지수이동평균", "name": "MACD_SLOW", "type": "int", "section": "4-1. 데이터·추세",
         "get": lambda: config.INDICATOR_PARAMS["MACD_SLOW"], "set": lambda v: config.INDICATOR_PARAMS.update({"MACD_SLOW": v})},
        {"desc": "MACD Signal", "help": "시그널 기간", "name": "MACD_SIGNAL", "type": "int", "section": "4-1. 데이터·추세",
         "get": lambda: config.INDICATOR_PARAMS["MACD_SIGNAL"], "set": lambda v: config.INDICATOR_PARAMS.update({"MACD_SIGNAL": v})},
        {"desc": "단기 EMA 기간", "help": "단기 급등 추세 판단 (기본 5)", "name": "EMA_SHORT", "type": "int", "section": "4-1. 데이터·추세",
         "get": lambda: config.INDICATOR_PARAMS.get("EMA_SHORT", 5), "set": lambda v: config.INDICATOR_PARAMS.update({"EMA_SHORT": v})},
        {"desc": "상승/하락 추세선 기간", "help": "추세선 룩백 기간 (기본 60일)", "name": "TREND_PERIOD", "type": "int", "section": "4-1. 데이터·추세",
         "get": lambda: config.INDICATOR_PARAMS.get("TREND_PERIOD", 60), "set": lambda v: config.INDICATOR_PARAMS.update({"TREND_PERIOD": v})},

        {"desc": "RSI 계산 기간", "help": "상대강도지수 기간", "name": "RSI_PERIOD", "type": "int", "section": "4-2. 모멘텀",
         "get": lambda: config.INDICATOR_PARAMS["RSI_PERIOD"], "set": lambda v: config.INDICATOR_PARAMS.update({"RSI_PERIOD": v})},
        {"desc": "RSI 시그널 기간", "help": "RSI 이동평균 기간", "name": "RSI_SIGNAL", "type": "int", "section": "4-2. 모멘텀",
         "get": lambda: config.INDICATOR_PARAMS["RSI_SIGNAL"], "set": lambda v: config.INDICATOR_PARAMS.update({"RSI_SIGNAL": v})},
        {"desc": "RSI 과매수 기준", "help": "이 값 이상이면 과열", "name": "RSI_UPPER", "type": "int", "section": "4-2. 모멘텀",
         "get": lambda: config.INDICATOR_PARAMS["RSI_UPPER"], "set": lambda v: config.INDICATOR_PARAMS.update({"RSI_UPPER": v})},
        {"desc": "RSI 중심선", "help": "강세/약세 기준선", "name": "RSI_MID", "type": "int", "section": "4-2. 모멘텀",
         "get": lambda: config.INDICATOR_PARAMS["RSI_MID"], "set": lambda v: config.INDICATOR_PARAMS.update({"RSI_MID": v})},
        {"desc": "RSI 과매도 기준", "help": "이 값 이하면 침체", "name": "RSI_LOWER", "type": "int", "section": "4-2. 모멘텀",
         "get": lambda: config.INDICATOR_PARAMS["RSI_LOWER"], "set": lambda v: config.INDICATOR_PARAMS.update({"RSI_LOWER": v})},
        {"desc": "CCI 계산 기간", "help": "상품채널지수 기간", "name": "CCI_WINDOW", "type": "int", "section": "4-2. 모멘텀",
         "get": lambda: config.INDICATOR_PARAMS["CCI_WINDOW"], "set": lambda v: config.INDICATOR_PARAMS.update({"CCI_WINDOW": v})},
        {"desc": "CCI 과매수 기준", "help": "이 값 이상이면 과열", "name": "CCI_UPPER", "type": "int", "section": "4-2. 모멘텀",
         "get": lambda: config.INDICATOR_PARAMS["CCI_UPPER"], "set": lambda v: config.INDICATOR_PARAMS.update({"CCI_UPPER": v})},
        {"desc": "CCI 과매도 기준", "help": "이 값 이하면 침체", "name": "CCI_LOWER", "type": "int", "section": "4-2. 모멘텀",
         "get": lambda: config.INDICATOR_PARAMS["CCI_LOWER"], "set": lambda v: config.INDICATOR_PARAMS.update({"CCI_LOWER": v})},

        {"desc": "ADX 계산 기간", "help": "추세 강도 지표", "name": "ADX_PERIOD", "type": "int", "section": "4-3. 강도·수급·가격구조",
         "get": lambda: config.INDICATOR_PARAMS["ADX_PERIOD"], "set": lambda v: config.INDICATOR_PARAMS.update({"ADX_PERIOD": v})},
        {"desc": "OBV EMA 기간", "help": "거래량 추세 판단 (지수이동평균)", "name": "OBV_MA_PERIOD", "type": "int", "section": "4-3. 강도·수급·가격구조",
         "get": lambda: config.INDICATOR_PARAMS["OBV_MA_PERIOD"], "set": lambda v: config.INDICATOR_PARAMS.update({"OBV_MA_PERIOD": v})},
        {"desc": "거래량 이동평균 기간", "help": "단기 수급 추세 판단 (기본 20)", "name": "VOLUME_MA_PERIOD", "type": "int", "section": "4-3. 강도·수급·가격구조",
         "get": lambda: config.INDICATOR_PARAMS.get("VOLUME_MA_PERIOD", 20), "set": lambda v: config.INDICATOR_PARAMS.update({"VOLUME_MA_PERIOD": v})},
        {"desc": "거래량 폭발 배수", "help": "이평선 대비 폭증 기준 (기본 2.0)", "name": "VOLUME_SPIKE_RATIO", "type": "float", "section": "4-3. 강도·수급·가격구조",
         "get": lambda: config.INDICATOR_PARAMS.get("VOLUME_SPIKE_RATIO", 2.0), "set": lambda v: config.INDICATOR_PARAMS.update({"VOLUME_SPIKE_RATIO": v})},
        {"desc": "ATR 계산 기간", "help": "평균 진폭 (변동성)", "name": "ATR_PERIOD", "type": "int", "section": "4-3. 강도·수급·가격구조",
         "get": lambda: config.INDICATOR_PARAMS.get("ATR_PERIOD", 14), "set": lambda v: config.INDICATOR_PARAMS.update({"ATR_PERIOD": v})},

        {"desc": "박스권 탐지 기간", "help": "매물대 기반 박스권 룩백 봉 수 (기본 20봉, 일봉=일/분봉=분)", "name": "BOX_PERIOD", "type": "int", "section": "4-3. 강도·수급·가격구조",
         "get": lambda: config.INDICATOR_PARAMS.get("BOX_PERIOD", 30), "set": lambda v: config.INDICATOR_PARAMS.update({"BOX_PERIOD": v})},
        {"desc": "박스권 매물대 %", "help": "핵심 매물대 집중도 (기본 50.0)", "name": "BOX_VALUE_AREA_PCT", "type": "float", "section": "4-3. 강도·수급·가격구조",
         "get": lambda: config.INDICATOR_PARAMS.get("BOX_VALUE_AREA_PCT", 50.0), "set": lambda v: config.INDICATOR_PARAMS.update({"BOX_VALUE_AREA_PCT": v})}
    ]
    return items

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
        {"desc": "상승 미확정 점수 보정", "help": "빠른EMA>느린EMA이나 확인 기준 미달 구간 (예: 0.0)", "name": "PENDING_UP_SCORE_ADJ", "type": "float",
         "get": lambda: config.MARKET_REGIME_PARAMS.get("PENDING_UP_SCORE_ADJ", 0.0), "set": lambda v: config.MARKET_REGIME_PARAMS.update({"PENDING_UP_SCORE_ADJ": v})},
        {"desc": "하락 미확정 점수 보정", "help": "추세 붕괴 초기 구간 — 판별력상 가장 위험 (예: +0.5)", "name": "PENDING_DOWN_SCORE_ADJ", "type": "float",
         "get": lambda: config.MARKET_REGIME_PARAMS.get("PENDING_DOWN_SCORE_ADJ", 0.5), "set": lambda v: config.MARKET_REGIME_PARAMS.update({"PENDING_DOWN_SCORE_ADJ": v})},
        {"desc": "약세장 점수 보정", "help": "약세장일 때 기준 점수 조정값 (예: +0.5)", "name": "BEAR_SCORE_ADJ", "type": "float",
         "get": lambda: config.MARKET_REGIME_PARAMS["BEAR_SCORE_ADJ"], "set": lambda v: config.MARKET_REGIME_PARAMS.update({"BEAR_SCORE_ADJ": v})},
        {"desc": "빠른 EMA (일)", "help": "국면 판단용 단기 지수이동평균 (기본 9일, β=0.25)", "name": "REGIME_EMA_FAST", "type": "int",
         "get": lambda: config.MARKET_REGIME_PARAMS.get("REGIME_EMA_FAST", 9), "set": lambda v: config.MARKET_REGIME_PARAMS.update({"REGIME_EMA_FAST": v})},
        {"desc": "느린 EMA (일)", "help": "국면 판단용 장기 지수이동평균 (기본 41일, β≈0.05)", "name": "REGIME_EMA_SLOW", "type": "int",
         "get": lambda: config.MARKET_REGIME_PARAMS.get("REGIME_EMA_SLOW", 41), "set": lambda v: config.MARKET_REGIME_PARAMS.update({"REGIME_EMA_SLOW": v})},
        {"desc": "추세 확인 기준 (%)", "help": "교차 후 이만큼 진행해야 확정 추세로 인정 (기본 5%)", "name": "REGIME_CONFIRM_PCT", "type": "float",
         "get": lambda: config.MARKET_REGIME_PARAMS.get("REGIME_CONFIRM_PCT", 5.0), "set": lambda v: config.MARKET_REGIME_PARAMS.update({"REGIME_CONFIRM_PCT": v})},
        {"desc": "휩소율 룩백 (회)", "help": "휩소율 산출에 쓸 직전 교차 구간 수 (기본 8회)", "name": "REGIME_WHIPSAW_LOOKBACK", "type": "int",
         "get": lambda: config.MARKET_REGIME_PARAMS.get("REGIME_WHIPSAW_LOOKBACK", 8), "set": lambda v: config.MARKET_REGIME_PARAMS.update({"REGIME_WHIPSAW_LOOKBACK": v})}
    ]
    # [백테스트 보호] 판정 파라미터·점수 보정은 편집 목록에서 제외 (BACKTESTED_HIDDEN_KEYS 주석 참조).
    #  남는 항목은 기능 ON/OFF 킬 스위치뿐이다.
    items = [it for it in items if it["name"] not in BACKTESTED_HIDDEN_KEYS]
    return _edit_config_table("시장 국면 및 적응형 임계값 (Adaptive Thresholds)", items)

def _validate_time_format(val):
    if len(val) == 4 and val.isdigit():
        hh = int(val[:2])
        mm = int(val[2:])
        if 0 <= hh <= 23 and 0 <= mm <= 59:
            return True
    return False

def _warn_nominal_exposure():
    """명목합(종목당 비중 × 최대 보유 종목 수)이 100%를 넘으면 경고만 띄운다(차단 아님).

    개별 종목 룰에 지정된 비중까지 합산해 판정한다. 오버커밋은 의도적 선택일 수 있어
    허용하되, 슬롯 수만 바꿨다가 조용히 초과되는 상황만 알린다.
    """
    try:
        from modules import db_manager  # 지연 임포트(순환 참조 방지)
        overrides = [r.get('invest_ratio') for r in (db_manager.db.get_all_stock_strategies() or [])]
    except Exception:
        overrides = []

    warn = config.nominal_exposure_warning(override_ratios=overrides)
    if warn:
        console.print(f"\n[yellow]⚠️ {warn}[/yellow]\n")
    elif config.is_invest_ratio_auto():
        console.print(f"[dim]종목당 비중 자동: {config.resolve_invest_ratio() * 100:.0f}% "
                      f"(= 1 / {config.settings.SYSTEM_MAX_HOLDINGS}종목, 명목합 100%)[/dim]")


def _risk_portfolio_items():
    """리스크/자산 배분 항목 (섹션 3-1 ~ 3-3)"""
    items = [
        {"desc": "종목당 투자 비중", "help": "0 = 자동(1/최대보유종목수). 0보다 크면 그 값을 그대로 사용 (0~1.0)", "name": "SYSTEM_INVEST_PER_STOCK", "type": "float", "section": "3-1. 자산 배분/포지션",
             "get": lambda: config.settings.SYSTEM_INVEST_PER_STOCK, "set": lambda v: setattr(config.settings, 'SYSTEM_INVEST_PER_STOCK', v),
         "validator": lambda v: 0 <= v <= 1.0, "callback": _warn_nominal_exposure},
        {"desc": "최대 보유 종목 수", "help": "포트폴리오 최대 종목 개수 (종목당 비중이 0이면 이 값으로 비중까지 자동 결정)", "name": "SYSTEM_MAX_HOLDINGS", "type": "int", "section": "3-1. 자산 배분/포지션",
             "get": lambda: config.settings.SYSTEM_MAX_HOLDINGS, "set": lambda v: setattr(config.settings, 'SYSTEM_MAX_HOLDINGS', v),
         "callback": _warn_nominal_exposure},
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
             "get": lambda: getattr(config.settings, 'TARGET_VOLATILITY', 0.20), "set": lambda v: setattr(config.settings, 'TARGET_VOLATILITY', v)},
            {"desc": "스케일링 최대 배수", "help": "비중 확대 제한", "name": "VOLATILITY_SCALING_MAX", "type": "float", "section": "3-1. 자산 배분/포지션",
             "get": lambda: getattr(config.settings, 'VOLATILITY_SCALING_MAX', 2.0), "set": lambda v: setattr(config.settings, 'VOLATILITY_SCALING_MAX', v)},
            {"desc": "스케일링 최소 배수", "help": "비중 축소 제한", "name": "VOLATILITY_SCALING_MIN", "type": "float", "section": "3-1. 자산 배분/포지션",
             "get": lambda: getattr(config.settings, 'VOLATILITY_SCALING_MIN', 0.4), "set": lambda v: setattr(config.settings, 'VOLATILITY_SCALING_MIN', v)}
        ])

    items.extend([
        {"desc": "시장 필터링 사용", "help": "지수 하락 시 신규 매수 보류", "name": "USE_MARKET_FILTER", "type": "bool", "choices": ["y", "n"], "section": "3-2. 매수 필터",
         "get": lambda: getattr(config.settings, 'USE_MARKET_FILTER', True), "set": lambda v: setattr(config.settings, 'USE_MARKET_FILTER', v)},
        {"desc": "시장 필터링 SMA (일)", "help": "지수 추세 판단용 단순이동평균선", "name": "MARKET_FILTER_MA", "type": "int", "section": "3-2. 매수 필터",
         "get": lambda: getattr(config.settings, 'MARKET_FILTER_MA', 60), "set": lambda v: setattr(config.settings, 'MARKET_FILTER_MA', v)},
        {"desc": "상대강도(RS) 필터 사용", "help": "수익률이 소속 지수를 밑도는 종목 신규 매수 제외", "name": "USE_RS_FILTER", "type": "bool", "choices": ["y", "n"], "section": "3-2. 매수 필터",
         "get": lambda: getattr(config.settings, 'USE_RS_FILTER', False), "set": lambda v: setattr(config.settings, 'USE_RS_FILTER', v)},
        {"desc": "상대강도(RS) 필터 기간 (일)", "help": "지수 대비 수익률 비교 룩백 거래일 (0 = 가격 모멘텀 룩백 연동)", "name": "RS_FILTER_LOOKBACK", "type": "int", "section": "3-2. 매수 필터",
         "get": lambda: getattr(config.settings, 'RS_FILTER_LOOKBACK', 0), "set": lambda v: setattr(config.settings, 'RS_FILTER_LOOKBACK', v), "validator": lambda v: v >= 0},
        {"desc": "상관계수 필터링 사용", "help": "유사 테마 종목 중복 매수 방지", "name": "USE_CORRELATION_FILTER", "type": "bool", "choices": ["y", "n"], "section": "3-2. 매수 필터",
         "get": lambda: getattr(config.settings, 'USE_CORRELATION_FILTER', True), "set": lambda v: setattr(config.settings, 'USE_CORRELATION_FILTER', v)},
        {"desc": "상관계수 임계값", "help": "이 값 이상일 때 동조화로 판단 (0.0~1.0)", "name": "CORRELATION_THRESHOLD", "type": "float", "section": "3-2. 매수 필터",
         "get": lambda: getattr(config.settings, 'CORRELATION_THRESHOLD', 0.7), "set": lambda v: setattr(config.settings, 'CORRELATION_THRESHOLD', v)},

        {"desc": "연속 에러 허용", "help": "시스템 중단 임계값", "name": "SYSTEM_MAX_CONSECUTIVE_ERRORS", "type": "int", "section": "3-3. 비상 안전장치",
         "get": lambda: getattr(config.settings, 'SYSTEM_MAX_CONSECUTIVE_ERRORS', 5), "set": lambda v: setattr(config.settings, 'SYSTEM_MAX_CONSECUTIVE_ERRORS', v)},
        {"desc": "일일 손실 제한 (%)", "help": "비상 정지 기준 손실률 (0%면 비상 정지 OFF)", "name": "SYSTEM_DAILY_LOSS_LIMIT", "type": "float", "section": "3-3. 비상 안전장치",
         "get": lambda: getattr(config.settings, 'SYSTEM_DAILY_LOSS_LIMIT', 10.0), "set": lambda v: setattr(config.settings, 'SYSTEM_DAILY_LOSS_LIMIT', v)},
        {"desc": "1회 최대 리스크 (%)", "help": "계좌 대비 1회 매매 최대 손실폭", "name": "SYSTEM_RISK_PER_TRADE", "type": "float", "section": "3-3. 비상 안전장치",
         "get": lambda: getattr(config.settings, 'SYSTEM_RISK_PER_TRADE', 4.0), "set": lambda v: setattr(config.settings, 'SYSTEM_RISK_PER_TRADE', v)},
        {"desc": "총 오픈 리스크 한도 (%)", "help": "보유 전체 '현재가→손절선' 잠재손실 합의 상한 (0%면 미사용)", "name": "SYSTEM_MAX_PORTFOLIO_RISK", "type": "float", "section": "3-3. 비상 안전장치",
         "get": lambda: getattr(config.settings, 'SYSTEM_MAX_PORTFOLIO_RISK', 10.0), "set": lambda v: setattr(config.settings, 'SYSTEM_MAX_PORTFOLIO_RISK', v)},

        {"desc": "국면 연동 리스크 축소 사용", "help": "추세 붕괴 초기(하락 미확정) 구간에서 신규 진입 리스크 한도를 배수만큼 축소", "name": "USE_REGIME_RISK_SCALING", "type": "bool", "choices": ["y", "n"], "section": "3-4. 리스크 한도 동적 스케일링",
         "get": lambda: config.RISK_SCALING_PARAMS.get("USE_REGIME_RISK_SCALING", True), "set": lambda v: config.RISK_SCALING_PARAMS.update({"USE_REGIME_RISK_SCALING": v})},
        {"desc": "하락 미확정 배수", "help": "추세 붕괴 초기 리스크 한도 곱 배수 (0<v<=1, 예: 0.6)", "name": "PENDING_DOWN_RISK_SCALE", "type": "float", "section": "3-4. 리스크 한도 동적 스케일링",
         "get": lambda: config.RISK_SCALING_PARAMS.get("PENDING_DOWN_RISK_SCALE", 0.6), "set": lambda v: config.RISK_SCALING_PARAMS.update({"PENDING_DOWN_RISK_SCALE": v}), "validator": lambda v: 0 < v <= 1.0},
        {"desc": "확정 약세 배수", "help": "확정 하락추세 배수 (1.0=미적용 권장 — 이미 하락한 뒤라 축소가 역효과)", "name": "BEAR_RISK_SCALE", "type": "float", "section": "3-4. 리스크 한도 동적 스케일링",
         "get": lambda: config.RISK_SCALING_PARAMS.get("BEAR_RISK_SCALE", 1.0), "set": lambda v: config.RISK_SCALING_PARAMS.update({"BEAR_RISK_SCALE": v}), "validator": lambda v: 0 < v <= 1.0},
        {"desc": "휩소율 연동 리스크 축소 사용", "help": "교차가 확인 기준을 못 채우고 되돌려지는 톱니장일수록 진입 크기 축소", "name": "USE_WHIPSAW_RISK_SCALING", "type": "bool", "choices": ["y", "n"], "section": "3-4. 리스크 한도 동적 스케일링",
         "get": lambda: config.RISK_SCALING_PARAMS.get("USE_WHIPSAW_RISK_SCALING", True), "set": lambda v: config.RISK_SCALING_PARAMS.update({"USE_WHIPSAW_RISK_SCALING": v})},
        {"desc": "휩소율 하한", "help": "이 값 이하 휩소율이면 축소 없음 (0~1, 예: 0.40)", "name": "WHIPSAW_LO", "type": "float", "section": "3-4. 리스크 한도 동적 스케일링",
         "get": lambda: config.RISK_SCALING_PARAMS.get("WHIPSAW_LO", 0.40), "set": lambda v: config.RISK_SCALING_PARAMS.update({"WHIPSAW_LO": v}), "validator": lambda v: 0 <= v < 1.0},
        {"desc": "휩소율 상한", "help": "이 값 이상이면 최대 축소 (0~1, 하한보다 커야 함, 예: 0.75)", "name": "WHIPSAW_HI", "type": "float", "section": "3-4. 리스크 한도 동적 스케일링",
         "get": lambda: config.RISK_SCALING_PARAMS.get("WHIPSAW_HI", 0.75), "set": lambda v: config.RISK_SCALING_PARAMS.update({"WHIPSAW_HI": v}), "validator": lambda v: 0 < v <= 1.0},
        {"desc": "휩소율 최소 배수", "help": "휩소율 연동 최소 리스크 배수 (0<v<1, 기본 0.85 — 낮출수록 톱니장에서 크게 줄이지만 수익 비용이 급증)", "name": "WHIPSAW_MIN_SCALE", "type": "float", "section": "3-4. 리스크 한도 동적 스케일링",
         "get": lambda: config.RISK_SCALING_PARAMS.get("WHIPSAW_MIN_SCALE", 0.6), "set": lambda v: config.RISK_SCALING_PARAMS.update({"WHIPSAW_MIN_SCALE": v}), "validator": lambda v: 0 < v < 1.0},
        {"desc": "드로다운 리스크 감속 사용", "help": "계좌 고점(HWM) 대비 하락 시 단계적으로 리스크 한도 축소", "name": "USE_DRAWDOWN_RISK_SCALING", "type": "bool", "choices": ["y", "n"], "section": "3-4. 리스크 한도 동적 스케일링",
         "get": lambda: config.RISK_SCALING_PARAMS.get("USE_DRAWDOWN_RISK_SCALING", True), "set": lambda v: config.RISK_SCALING_PARAMS.update({"USE_DRAWDOWN_RISK_SCALING": v})},
        {"desc": "드로다운 1단계 기준 (%)", "help": "고점 대비 이 % 이상 하락 시 1단계 배수 적용", "name": "DD_LEVEL_1", "type": "float", "section": "3-4. 리스크 한도 동적 스케일링",
         "get": lambda: config.RISK_SCALING_PARAMS.get("DD_LEVEL_1", 5.0), "set": lambda v: config.RISK_SCALING_PARAMS.update({"DD_LEVEL_1": v}), "validator": lambda v: v >= 0},
        {"desc": "드로다운 1단계 배수", "help": "1단계 리스크 곱 배수 (0<v<1, 예: 0.75)", "name": "DD_SCALE_1", "type": "float", "section": "3-4. 리스크 한도 동적 스케일링",
         "get": lambda: config.RISK_SCALING_PARAMS.get("DD_SCALE_1", 0.75), "set": lambda v: config.RISK_SCALING_PARAMS.update({"DD_SCALE_1": v}), "validator": lambda v: 0 < v <= 1.0},
        {"desc": "드로다운 2단계 기준 (%)", "help": "고점 대비 이 % 이상 하락 시 2단계 배수 적용", "name": "DD_LEVEL_2", "type": "float", "section": "3-4. 리스크 한도 동적 스케일링",
         "get": lambda: config.RISK_SCALING_PARAMS.get("DD_LEVEL_2", 10.0), "set": lambda v: config.RISK_SCALING_PARAMS.update({"DD_LEVEL_2": v}), "validator": lambda v: v >= 0},
        {"desc": "드로다운 2단계 배수", "help": "2단계 리스크 곱 배수 (0<v<1, 예: 0.5)", "name": "DD_SCALE_2", "type": "float", "section": "3-4. 리스크 한도 동적 스케일링",
         "get": lambda: config.RISK_SCALING_PARAMS.get("DD_SCALE_2", 0.5), "set": lambda v: config.RISK_SCALING_PARAMS.update({"DD_SCALE_2": v}), "validator": lambda v: 0 < v <= 1.0},
        {"desc": "자산 고점 룩백 (일)", "help": "HWM(자산 고점) 산출 기간", "name": "DD_LOOKBACK_DAYS", "type": "int", "section": "3-4. 리스크 한도 동적 스케일링",
         "get": lambda: config.RISK_SCALING_PARAMS.get("DD_LOOKBACK_DAYS", 90), "set": lambda v: config.RISK_SCALING_PARAMS.update({"DD_LOOKBACK_DAYS": v}), "validator": lambda v: v > 0},
        {"desc": "갭 리스크 버퍼", "help": "사이징 시 손절폭에 곱하는 배수 (갭하락 대비, 1.0=미사용)", "name": "GAP_RISK_BUFFER", "type": "float", "section": "3-4. 리스크 한도 동적 스케일링",
         "get": lambda: config.RISK_SCALING_PARAMS.get("GAP_RISK_BUFFER", 1.2), "set": lambda v: config.RISK_SCALING_PARAMS.update({"GAP_RISK_BUFFER": v}), "validator": lambda v: v >= 1.0},
    ])
    # [백테스트 보호] 3-4 리스크 스케일링 항목은 편집 목록에서 제외 (BACKTESTED_HIDDEN_KEYS 주석 참조).
    #  정의는 남겨 두어 dynamic_config.json 직접 편집 시의 타입·검증 규칙 근거로 삼는다.
    # [추세추종 보호] 변동성 타겟팅(자본대비 변동성 한도)도 동일하게 제외.
    return [it for it in items
            if it["name"] not in BACKTESTED_HIDDEN_KEYS
            and it["name"] not in ANTI_TREND_HIDDEN_KEYS]

def modify_risk_portfolio_settings():
    return _edit_config_table("리스크 및 자산 배분 설정 (Risk & Portfolio)", _risk_portfolio_items)

def _set_journal_sync_use(value):
    """매매일지 연동 토글 — 값 반영 + 워커 기동/중지 + 미설정 환경변수 안내.

    환경변수(JOURNAL_API_URL/KEY)가 없으면 켜도 동작하지 않으므로, 조용히 넘어가지 않고
    그 자리에서 무엇이 빠졌는지 알려준다. 재시작을 기다리지 않도록 워커도 즉시 제어한다.
    """
    setattr(config.settings, 'JOURNAL_SYNC_USE', value)

    try:
        from modules import journal_sync
    except Exception as e:
        config.console.print(f"\n[red]매매일지 연동 모듈 로드 실패: {e}[/red]")
        return

    if not value:
        journal_sync.stop()
        config.console.print("\n[yellow]매매일지 웹서버 연동을 껐습니다. (전송 대기열 적재도 중단)[/yellow]")
        return

    missing = [name for name in ('JOURNAL_API_URL', 'JOURNAL_API_KEY')
               if not getattr(config, name, '')]
    if missing:
        config.console.print(
            f"\n[yellow]※ 환경변수 {', '.join(missing)} 가 설정되지 않아 연동이 동작하지 않습니다.[/yellow]")
        config.console.print(
            "[dim]  ~/.htsrc 에 export 로 추가한 뒤 `source ~/.htsrc` 하고 프로그램을 재시작하세요.[/dim]")
        return

    journal_sync.start()
    pending = journal_sync.pending_count()
    config.console.print(f"\n[green]매매일지 웹서버 연동을 켰습니다. ({config.JOURNAL_API_URL})[/green]")
    if pending:
        config.console.print(f"[dim]  미전송 대기 {pending}건은 곧 자동으로 전송됩니다.[/dim]")


def _trading_cycle_items():
    """트레이딩 시간/주기/통신 항목 (섹션 5-1 ~ 5-3)"""
    return [
        {"desc": "거래 시작 시간", "help": "매매 허용 시작 시각 (HHMM). 기본값은 KRX 정규장 개장(0900). NXT 프리마켓까지 운용하려면 0800", "name": "SYSTEM_TRADING_START_TIME", "type": "time", "section": "5-1. 거래 시간·주기",
         "get": lambda: getattr(config.settings, 'SYSTEM_TRADING_START_TIME', "0900"), "set": lambda v: setattr(config.settings, 'SYSTEM_TRADING_START_TIME', v)},
        {"desc": "거래 종료 시간", "help": "매매 허용 종료 시각 (HHMM). 기본값은 KRX 정규장 마감(1530)이며, 종가 단일가(동시호가) 15:20~15:30은 자동 회피하므로 실효 매매는 15:20까지다. NXT 애프터마켓까지 운용하려면 2000", "name": "SYSTEM_TRADING_END_TIME", "type": "time", "section": "5-1. 거래 시간·주기",
         "get": lambda: getattr(config.settings, 'SYSTEM_TRADING_END_TIME', "1530"), "set": lambda v: setattr(config.settings, 'SYSTEM_TRADING_END_TIME', v)},
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
         "get": lambda: getattr(config.settings, 'CHART_CACHE_TTL_MINUTES', 360), "set": lambda v: setattr(config.settings, 'CHART_CACHE_TTL_MINUTES', v)},
        {"desc": "실시간 WebSocket 사용", "help": "KIS 실시간 시세 push 사용(끄면 REST 폴링). 미구독/끊김 시 자동 REST 폴백. 토스는 미지원", "name": "USE_WEBSOCKET", "type": "bool", "choices": ["y", "n"], "section": "5-3. 데이터·통신",
         "get": lambda: getattr(config.settings, 'USE_WEBSOCKET', True), "set": lambda v: setattr(config.settings, 'USE_WEBSOCKET', v)},
        {"desc": "장 종료 후 KRX 종가 기준", "help": "모든 장(NXT 애프터마켓 20:00)이 끝난 뒤 화면 '현재가'를 KRX 정규장 확정 종가로 고정합니다. 끄면 마지막 실거래가(전날 NXT 종가)가 다음 개장까지 그대로 보입니다. NXT 거래시간(08:00~09:00, 15:30~20:00)에는 설정과 무관하게 NXT 현재가를 표시합니다. ※ 지표는 이 설정과 무관하게 항상 KRX 정규장 확정 봉으로 계산하며, 주문 가격과 손절·트레일링 트리거도 항상 실시간가를 씁니다.", "name": "USE_KRX_CLOSE_AFTER_HOURS", "type": "bool", "choices": ["y", "n"], "section": "5-3. 데이터·통신",
         "get": lambda: getattr(config.settings, 'USE_KRX_CLOSE_AFTER_HOURS', True), "set": lambda v: setattr(config.settings, 'USE_KRX_CLOSE_AFTER_HOURS', v)},
        {"desc": "매매일지 웹서버 연동", "help": "체결 내역을 원격 매매일지 웹서버(stock-memo)로 자동 전송합니다. 켜려면 환경변수 JOURNAL_API_URL·JOURNAL_API_KEY 가 모두 필요하며(~/.htsrc 에 export 후 재시작), 둘 중 하나라도 없으면 켜도 동작하지 않습니다. 전송은 체결 기록과 같은 트랜잭션으로 대기열에 쌓고 백그라운드 워커가 배치로 보내므로 매매 루프가 네트워크에 지연되지 않습니다. 끄면 대기열 적재도 워커 기동도 하지 않습니다. (기본 OFF)", "name": "JOURNAL_SYNC_USE", "type": "bool", "choices": ["y", "n"], "section": "5-3. 데이터·통신",
         "get": lambda: getattr(config.settings, 'JOURNAL_SYNC_USE', False), "set": _set_journal_sync_use},
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
        "SYSTEM_INVEST_PER_STOCK": 0.0, "SYSTEM_DAILY_LOSS_LIMIT": 10.0, "USE_MARKET_FILTER": True, "MARKET_FILTER_MA": 50
    },
    "bear": {
        "BUY_SCORE": 8.0, "BUY_RSI_MAX": 65.0, "BUY_VOL_STRENGTH": 105.0, "BUY_ASK_BID_RATIO": 1.0, "AUTO_ADJUST_ASK_BID_RATIO": True, "USE_MEAN_REVERSION": False, "MR_RSI_MAX": 30.0, "MR_VOL_STRENGTH": 110.0, "SUPER_MOMENTUM_USE": False,
        "TAKE_PROFIT_RATE": 0.0, "HALF_TAKE_PROFIT_USE": False, "DEFENSIVE_HALF_SELL_USE": False, "STOP_LOSS_RATE": -3.0, "USE_ATR_STOP": True, "ATR_STOP_MULTIPLIER": 1.5, "MAX_ATR_STOP_LOSS_RATE": -15.0, "BREAK_EVEN_PROFIT_RATE": 5.0, "BREAK_EVEN_STOP_RATE": 0.5, "TIME_STOP_DAYS": 3, "SELL_SCORE": 6.0, "TAKE_PROFIT_RSI": 0.0, "TRAILING_STOP_ACTIVATION_RATE": 4.0, "TRAILING_STOP_CALLBACK_RATE": 2.0, "TRAILING_ATR_MULTIPLIER": 2.0,
        "TREND": 3.0, "MOMENTUM": 3.0, "STRENGTH": 2.0, "SYNERGY": 2.0,
        "SYSTEM_INVEST_PER_STOCK": 0.0, "SYSTEM_DAILY_LOSS_LIMIT": 5.0, "USE_MARKET_FILTER": True, "MARKET_FILTER_MA": 20
    },
    "sideways": {
        "BUY_SCORE": 7.0, "BUY_RSI_MAX": 50.0, "BUY_VOL_STRENGTH": 100.0, "BUY_ASK_BID_RATIO": 1.0, "AUTO_ADJUST_ASK_BID_RATIO": True, "USE_MEAN_REVERSION": False, "MR_RSI_MAX": 40.0, "MR_VOL_STRENGTH": 105.0, "SUPER_MOMENTUM_USE": False,
        "TAKE_PROFIT_RATE": 0.0, "HALF_TAKE_PROFIT_USE": False, "DEFENSIVE_HALF_SELL_USE": False, "STOP_LOSS_RATE": -5.0, "USE_ATR_STOP": True, "ATR_STOP_MULTIPLIER": 1.8, "MAX_ATR_STOP_LOSS_RATE": -15.0, "BREAK_EVEN_PROFIT_RATE": 5.0, "BREAK_EVEN_STOP_RATE": 0.5, "TIME_STOP_DAYS": 5, "SELL_SCORE": 5.0, "TAKE_PROFIT_RSI": 0.0, "TRAILING_STOP_ACTIVATION_RATE": 7.0, "TRAILING_STOP_CALLBACK_RATE": 3.0, "TRAILING_ATR_MULTIPLIER": 2.5,
        "TREND": 3.5, "MOMENTUM": 3.0, "STRENGTH": 1.5, "SYNERGY": 2.0,
        "SYSTEM_INVEST_PER_STOCK": 0.0, "SYSTEM_DAILY_LOSS_LIMIT": 7.0, "USE_MARKET_FILTER": True, "MARKET_FILTER_MA": 20
    },
    "default": {
        "BUY_SCORE": 7.0, "BUY_RSI_MAX": 70.0, "BUY_VOL_STRENGTH": 100.0, "BUY_ASK_BID_RATIO": 1.0, "AUTO_ADJUST_ASK_BID_RATIO": True, "USE_MEAN_REVERSION": False, "MR_RSI_MAX": 40.0, "MR_VOL_STRENGTH": 120.0, "SUPER_MOMENTUM_USE": True,
        "TAKE_PROFIT_RATE": 0.0, "HALF_TAKE_PROFIT_USE": False, "DEFENSIVE_HALF_SELL_USE": False, "STOP_LOSS_RATE": -7.0, "USE_ATR_STOP": True, "ATR_STOP_MULTIPLIER": 2.0, "MAX_ATR_STOP_LOSS_RATE": -15.0, "BREAK_EVEN_PROFIT_RATE": 5.0, "BREAK_EVEN_STOP_RATE": 0.5, "TIME_STOP_DAYS": 20, "SELL_SCORE": 4.0, "TAKE_PROFIT_RSI": 0.0, "TRAILING_STOP_ACTIVATION_RATE": 10.0, "TRAILING_STOP_CALLBACK_RATE": 5.0, "TRAILING_ATR_MULTIPLIER": 3.5,
        "TREND": 4.0, "MOMENTUM": 2.5, "STRENGTH": 1.5, "SYNERGY": 2.0,
        "SYSTEM_INVEST_PER_STOCK": 0.0, "SYSTEM_DAILY_LOSS_LIMIT": 10.0, "USE_MARKET_FILTER": True, "MARKET_FILTER_MA": 60
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

# [PRESET_RETIRED] 시장 국면별 전략 프리셋(강세/약세/횡보)은 2026-07-20 백테스트 검증 결과
#  폐지했다. 시스템 설정 메뉴(7번)와 텔레그램 /preset 진입 경로를 제거했으며, 아래 함수와
#  DEFAULT_PRESETS 정의는 이력 보존·기존 저장값 해석(check_and_update_active_preset)을 위해
#  남겨 두되 UI에서 호출되지 않는다.
#
#  [폐지 근거] 한국 대형/중형 30종목 × 3.8년:
#   - default 평균수익 17.48% / MDD -19.22% / PF 1.70 / >30% 대박 29건
#   - bull    17.15% / -19.81% / 1.68 / 29건  → default와 사실상 동일(차등 의미 없음)
#   - bear     5.00% / -14.05% / 1.29 / 15건  → 수익 1/3 토막, 대박 절반 절단, PF 붕괴
#   - sideways 3.8년간 매매 0건 → 논리적 자기모순으로 아무것도 사지 못함
#     (BUY_RSI_MAX=50 + SUPER_MOMENTUM_USE=False. 이 스코어러에서 점수 7.0을 넘으려면
#      RSI가 50 위여야 하는데 RSI 50 미만을 요구했고, 유일한 예외였던 슈퍼모멘텀마저 꺼버림)
#   - KOSPI 국면 구간별로 쪼개 봐도 '맞는 프리셋'이 이기지 못했다. 상승 구간에서 bull은
#     default 대비 +1.1%p 수준, 미확정 구간에서는 모든 프리셋이 손실이었다.
#
#  [구조적 문제] 프리셋의 국면 차등이 대부분 추세추종 핵심을 훼손하는 방식이었다 —
#   주청산(샹들리에 TS) 폭 축소(TRAILING_* 3종), 추세 팩터 가중치 삭감(TREND 4.5→3.0),
#   주도주 랠리 차단(SUPER_MOMENTUM_USE=False), 진입 RSI 상한 과도 축소.
#  [대체 수단] 국면 대응은 이미 자동화되어 있다 — 시장 필터(SMA60), 국면 연동 리스크
#   스케일링(PendDown ×0.6), 휩소율 스케일링, 드로다운 단계 감속, 적응형 매수 임계값.
#   사람이 국면을 수동 판정해 전략을 통째로 갈아끼우면 이 자동 판정과 이중으로 충돌한다.
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
        "BREAK_EVEN_PROFIT_RATE": vals.get("BREAK_EVEN_PROFIT_RATE", 5.0),
        "BREAK_EVEN_STOP_RATE": vals.get("BREAK_EVEN_STOP_RATE", 0.5),
        # [추세추종 보호] TIME_STOP_DAYS·SELL_SCORE는 프리셋으로 덮어쓰지 않는다.
        #  약세 프리셋의 과거 값(3일 / 6.0)은 보유 3일 만에 강제 청산하고 정배열 유지 중의
        #  눌림목에서도 점수 매도를 유발해, 설정 메뉴에서 이 두 키를 잠근 취지를 무력화했다.
        #  (DEFAULT_PRESETS의 값은 이력 보존을 위해 남겨 두되 적용하지 않는다.)
        #  프리셋 차등은 진입 조건(BUY_SCORE·BUY_RSI_MAX)과 손절/TS 폭으로만 준다.
        "TAKE_PROFIT_RSI": vals["TAKE_PROFIT_RSI"],
        "TRAILING_STOP_ACTIVATION_RATE": vals["TRAILING_STOP_ACTIVATION_RATE"],
        "TRAILING_STOP_CALLBACK_RATE": vals["TRAILING_STOP_CALLBACK_RATE"],
        "TRAILING_ATR_MULTIPLIER": vals.get("TRAILING_ATR_MULTIPLIER", 3.5)
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
        ("트레일링 스탑", f"+{config.SELL_STRATEGY.get('TRAILING_STOP_ACTIVATION_RATE', 10.0)}% 발동 후 -{config.SELL_STRATEGY.get('TRAILING_STOP_CALLBACK_RATE', 5.0)}%"),
        ("본전 청산 (방어)", f"수익 +{config.SELL_STRATEGY.get('BREAK_EVEN_PROFIT_RATE', 5.0)}% 도달 시 손절선 +{config.SELL_STRATEGY.get('BREAK_EVEN_STOP_RATE', 0.5)}%로 상향"),
        ("시간 청산", f"{config.SELL_STRATEGY['TIME_STOP_DAYS']}일 경과 시 강제 매도"),
        ("안전 장치 (비상정지/필터)", f"일일손실 -{getattr(config, 'SYSTEM_DAILY_LOSS_LIMIT', 10.0)}% 제한 / 시장필터 {'ON ('+str(getattr(config, 'MARKET_FILTER_MA', 60))+'일선)' if getattr(config, 'USE_MARKET_FILTER', True) else 'OFF (무조건 진입)'}"),
        ("스코어링 가중치", f"추세 {config.SCORING_WEIGHTS['TREND']} / 모멘텀 {config.SCORING_WEIGHTS['MOMENTUM']} / 강도 {config.SCORING_WEIGHTS['STRENGTH']} / 시너지 {config.SCORING_WEIGHTS['SYNERGY']}"),
        ("종목당 투자 비중", config.format_invest_ratio())
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
            {"desc": "종목당 투자 비중", "help": "0 = 자동(1/최대보유종목수), 0~1.0", "name": "SYSTEM_INVEST_PER_STOCK", "type": "float", "section": "Risk", "get": make_getter("SYSTEM_INVEST_PER_STOCK"), "set": make_setter("SYSTEM_INVEST_PER_STOCK", 'float'), "callback": _warn_nominal_exposure}
        ]

        # [추가] 토스: 체결강도 미제공 → 체결강도 관련 항목 편집 숨김(매도잔량비는 유지)
        if config.session.is_toss:
            _toss_hidden = {"BUY_VOL_STRENGTH", "AUTO_ADJUST_ASK_BID_RATIO", "MR_VOL_STRENGTH"}
            items = [it for it in items if it["name"] not in _toss_hidden]

        # [추세추종 보호] 커스텀 프리셋 경로로도 반추세성 청산 설정이 켜지지 않도록 동일하게 숨김
        # [백테스트 보호] 국면 판정·리스크 스케일링 파라미터도 프리셋 경로로 우회 변경되지 않도록 제외
        items = [it for it in items
                 if it["name"] not in ANTI_TREND_HIDDEN_KEYS
                 and it["name"] not in BACKTESTED_HIDDEN_KEYS]

        acted = _edit_config_table(title, items, check_preset=False)
        if not acted: break

def edit_strategy_preset_menu():
    """전략 프리셋 개별 조정 메뉴"""
    while True:
        utils.clear_screen()
        menu_items = [
            ("0", "커스텀 프리셋 전체 초기화", "Reset All Custom Presets"),
            ("1", "강세장 (Bull) 프리셋 수정", "Edit Bull"),
            ("2", "약세장 (Bear) 프리셋 수정", "Edit Bear"),
            ("3", "횡보장 (Sideways) 프리셋 수정", "Edit Sideways")
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
        ("0", "전략 프리셋 조정", "Edit Presets"),
        ("1", "강세장  (Bull) - 수익 극대화 & 추세 추종", "Bull"),
        ("2", "약세장  (Bear) - 생존 우선 & 낙폭과대 스윙", "Bear"),
        ("3", "횡보장  (Sideways) - 박스권 단기 스윙", "Sideways"),
        ("9", "기본설정 (Default) - 시스템 권장 설정", "Default")
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
            "PENDING_UP_SCORE_ADJ": "상승 미확정 점수 보정",
            "PENDING_DOWN_SCORE_ADJ": "하락 미확정 점수 보정",
            "BEAR_SCORE_ADJ": "약세장 점수 보정",
            "SIDEWAYS_SCORE_ADJ": "판정 보류 점수 보정",
            "REGIME_EMA_FAST": "국면 판단 빠른 EMA (일)",
            "REGIME_EMA_SLOW": "국면 판단 느린 EMA (일)",
            "REGIME_CONFIRM_PCT": "추세 확인 기준 (%)",
            "REGIME_WHIPSAW_LOOKBACK": "휩소율 룩백 (회)",
            "REGIME_MA_PERIOD": "(폴백) 추세 판단 EMA (일)",
            "REGIME_ADX_THRESHOLD": "(폴백) 추세 판단 ADX",
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
            "USE_RS_FILTER": "상대강도(RS) 필터 사용",
            "RS_FILTER_LOOKBACK": "상대강도(RS) 필터 기간",
            "SYSTEM_MAX_CONSECUTIVE_ERRORS": "연속 에러 허용",
            "SYSTEM_DAILY_LOSS_LIMIT": "일일 손실 제한 (%)",
            "SYSTEM_RISK_PER_TRADE": "1회 최대 리스크 (%)",
            "SYSTEM_MAX_PORTFOLIO_RISK": "총 오픈 리스크 한도 (%)",
            "USE_REGIME_RISK_SCALING": "국면 연동 리스크 축소 사용",
            "PENDING_DOWN_RISK_SCALE": "하락 미확정 배수",
            "BEAR_RISK_SCALE": "확정 약세 배수",
            "USE_WHIPSAW_RISK_SCALING": "휩소율 연동 리스크 축소 사용",
            "WHIPSAW_LO": "휩소율 하한",
            "WHIPSAW_HI": "휩소율 상한",
            "WHIPSAW_MIN_SCALE": "휩소율 최소 배수",
            "USE_DRAWDOWN_RISK_SCALING": "드로다운 리스크 감속 사용",
            "DD_LEVEL_1": "드로다운 1단계 기준 (%)",
            "DD_SCALE_1": "드로다운 1단계 배수",
            "DD_LEVEL_2": "드로다운 2단계 기준 (%)",
            "DD_SCALE_2": "드로다운 2단계 배수",
            "DD_LOOKBACK_DAYS": "자산 고점 룩백 (일)",
            "GAP_RISK_BUFFER": "갭 리스크 버퍼",
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
            "USE_KRX_CLOSE_AFTER_HOURS": "장 종료 후 KRX 종가 기준",
            "JOURNAL_SYNC_USE": "매매일지 웹서버 연동",
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
            "BUY_SCORE": (_CAT1, "1-1. 진입 조건"),
            "RISE_SCORE": (_CAT1, "1-1. 진입 조건"),
            "INTEREST_SIGNAL_MIN": (_CAT1, "1-4. 화면 표시 전용"),
            "INTEREST_MA60_NEAR": (_CAT1, "1-4. 화면 표시 전용"),
            "BUY_RSI_MAX": (_CAT1, "1-1. 진입 조건"),
            "BUY_VOL_STRENGTH": (_CAT1, "1-1. 진입 조건"),
            "AUTO_ADJUST_ASK_BID_RATIO": (_CAT1, "1-1. 진입 조건"),
            "BUY_ASK_BID_RATIO": (_CAT1, "1-1. 진입 조건"),
            "DISPARITY_UPPER": (_CAT1, "1-4. 화면 표시 전용"),
            "DISPARITY_LOWER": (_CAT1, "1-4. 화면 표시 전용"),
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
            "TAKE_PROFIT_RATE": (_CAT1, "1-3. 청산 — 손절·트레일링·시간"),
            "HALF_TAKE_PROFIT_USE": (_CAT1, "1-3. 청산 — 손절·트레일링·시간"),
            "TAKE_PROFIT_RSI": (_CAT1, "1-3. 청산 — 손절·트레일링·시간"),
            "TRAILING_STOP_ACTIVATION_RATE": (_CAT1, "1-3. 청산 — 손절·트레일링·시간"),
            "TRAILING_STOP_CALLBACK_RATE": (_CAT1, "1-3. 청산 — 손절·트레일링·시간"),
            "STOP_LOSS_RATE": (_CAT1, "1-3. 청산 — 손절·트레일링·시간"),
            "USE_ATR_STOP": (_CAT1, "1-3. 청산 — 손절·트레일링·시간"),
            "ATR_STOP_MULTIPLIER": (_CAT1, "1-3. 청산 — 손절·트레일링·시간"),
            "MAX_ATR_STOP_LOSS_RATE": (_CAT1, "1-3. 청산 — 손절·트레일링·시간"),
            "BREAK_EVEN_PROFIT_RATE": (_CAT1, "1-3. 청산 — 손절·트레일링·시간"),
            "BREAK_EVEN_STOP_RATE": (_CAT1, "1-3. 청산 — 손절·트레일링·시간"),
            "TIME_STOP_USE": (_CAT1, "1-3. 청산 — 손절·트레일링·시간"),
            "TIME_STOP_DAYS": (_CAT1, "1-3. 청산 — 손절·트레일링·시간"),
            "TIME_STOP_MIN_PROFIT_RATE": (_CAT1, "1-3. 청산 — 손절·트레일링·시간"),
            "SELL_SCORE": (_CAT1, "1-3. 청산 — 손절·트레일링·시간"),
            "DEFENSIVE_HALF_SELL_USE": (_CAT1, "1-3. 청산 — 손절·트레일링·시간"),
            "TREND": (_CAT2, "2-1. 스코어링 가중치"),
            "MOMENTUM": (_CAT2, "2-1. 스코어링 가중치"),
            "STRENGTH": (_CAT2, "2-1. 스코어링 가중치"),
            "SYNERGY": (_CAT2, "2-1. 스코어링 가중치"),
            "USE_ADAPTIVE_THRESHOLD": (_CAT2, "2-2. 적응형 임계값 (시장국면)"),
            "BULL_SCORE_ADJ": (_CAT2, "2-2. 적응형 임계값 (시장국면)"),
            "PENDING_UP_SCORE_ADJ": (_CAT2, "2-2. 적응형 임계값 (시장국면)"),
            "PENDING_DOWN_SCORE_ADJ": (_CAT2, "2-2. 적응형 임계값 (시장국면)"),
            "BEAR_SCORE_ADJ": (_CAT2, "2-2. 적응형 임계값 (시장국면)"),
            "SIDEWAYS_SCORE_ADJ": (_CAT2, "2-2. 적응형 임계값 (시장국면)"),
            "REGIME_EMA_FAST": (_CAT2, "2-2. 적응형 임계값 (시장국면)"),
            "REGIME_EMA_SLOW": (_CAT2, "2-2. 적응형 임계값 (시장국면)"),
            "REGIME_CONFIRM_PCT": (_CAT2, "2-2. 적응형 임계값 (시장국면)"),
            "REGIME_WHIPSAW_LOOKBACK": (_CAT2, "2-2. 적응형 임계값 (시장국면)"),
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
            "USE_RS_FILTER": (_CAT3, "3-2. 매수 필터"),
            "RS_FILTER_LOOKBACK": (_CAT3, "3-2. 매수 필터"),
            "USE_CORRELATION_FILTER": (_CAT3, "3-2. 매수 필터"),
            "CORRELATION_THRESHOLD": (_CAT3, "3-2. 매수 필터"),
            "SYSTEM_MAX_CONSECUTIVE_ERRORS": (_CAT3, "3-3. 비상 안전장치"),
            "SYSTEM_DAILY_LOSS_LIMIT": (_CAT3, "3-3. 비상 안전장치"),
            "SYSTEM_RISK_PER_TRADE": (_CAT3, "3-3. 비상 안전장치"),
            "SYSTEM_MAX_PORTFOLIO_RISK": (_CAT3, "3-3. 비상 안전장치"),
            "USE_REGIME_RISK_SCALING": (_CAT3, "3-4. 리스크 한도 동적 스케일링"),
            "PENDING_DOWN_RISK_SCALE": (_CAT3, "3-4. 리스크 한도 동적 스케일링"),
            "BEAR_RISK_SCALE": (_CAT3, "3-4. 리스크 한도 동적 스케일링"),
            "USE_WHIPSAW_RISK_SCALING": (_CAT3, "3-4. 리스크 한도 동적 스케일링"),
            "WHIPSAW_LO": (_CAT3, "3-4. 리스크 한도 동적 스케일링"),
            "WHIPSAW_HI": (_CAT3, "3-4. 리스크 한도 동적 스케일링"),
            "WHIPSAW_MIN_SCALE": (_CAT3, "3-4. 리스크 한도 동적 스케일링"),
            "USE_DRAWDOWN_RISK_SCALING": (_CAT3, "3-4. 리스크 한도 동적 스케일링"),
            "DD_LEVEL_1": (_CAT3, "3-4. 리스크 한도 동적 스케일링"),
            "DD_SCALE_1": (_CAT3, "3-4. 리스크 한도 동적 스케일링"),
            "DD_LEVEL_2": (_CAT3, "3-4. 리스크 한도 동적 스케일링"),
            "DD_SCALE_2": (_CAT3, "3-4. 리스크 한도 동적 스케일링"),
            "DD_LOOKBACK_DAYS": (_CAT3, "3-4. 리스크 한도 동적 스케일링"),
            "GAP_RISK_BUFFER": (_CAT3, "3-4. 리스크 한도 동적 스케일링"),
            "CHART_LOOKBACK_DAYS": (_CAT4, "4-1. 데이터·추세"),
            "SAR_AF_START": (_CAT4, "4-1. 데이터·추세"),
            "SAR_AF_STEP": (_CAT4, "4-1. 데이터·추세"),
            "SAR_AF_MAX": (_CAT4, "4-1. 데이터·추세"),
            "MACD_FAST": (_CAT4, "4-1. 데이터·추세"),
            "MACD_SLOW": (_CAT4, "4-1. 데이터·추세"),
            "MACD_SIGNAL": (_CAT4, "4-1. 데이터·추세"),
            "EMA_SHORT": (_CAT4, "4-1. 데이터·추세"),
            "TREND_PERIOD": (_CAT4, "4-1. 데이터·추세"),
            "RSI_PERIOD": (_CAT4, "4-2. 모멘텀"),
            "RSI_SIGNAL": (_CAT4, "4-2. 모멘텀"),
            "RSI_UPPER": (_CAT4, "4-2. 모멘텀"),
            "RSI_MID": (_CAT4, "4-2. 모멘텀"),
            "RSI_LOWER": (_CAT4, "4-2. 모멘텀"),
            "CCI_WINDOW": (_CAT4, "4-2. 모멘텀"),
            "CCI_UPPER": (_CAT4, "4-2. 모멘텀"),
            "CCI_LOWER": (_CAT4, "4-2. 모멘텀"),
            "ADX_PERIOD": (_CAT4, "4-3. 강도·수급·가격구조"),
            "OBV_MA_PERIOD": (_CAT4, "4-3. 강도·수급·가격구조"),
            "VOLUME_MA_PERIOD": (_CAT4, "4-3. 강도·수급·가격구조"),
            "VOLUME_SPIKE_RATIO": (_CAT4, "4-3. 강도·수급·가격구조"),
            "ATR_PERIOD": (_CAT4, "4-3. 강도·수급·가격구조"),
            "BOX_PERIOD": (_CAT4, "4-3. 강도·수급·가격구조"),
            "BOX_VALUE_AREA_PCT": (_CAT4, "4-3. 강도·수급·가격구조"),
            "SYSTEM_TRADING_START_TIME": (_CAT5, "5-1. 거래 시간·주기"),
            "SYSTEM_TRADING_END_TIME": (_CAT5, "5-1. 거래 시간·주기"),
            "SYSTEM_TRADING_INTERVAL": (_CAT5, "5-1. 거래 시간·주기"),
            "CONCLUSION_CHECK_INTERVAL": (_CAT5, "5-2. 주문·체결 감시"),
            "CONCLUSION_CHECK_IDLE_INTERVAL": (_CAT5, "5-2. 주문·체결 감시"),
            "CONCLUSION_CHECK_ACTIVE_DURATION": (_CAT5, "5-2. 주문·체결 감시"),
            "UNFILLED_ORDER_CANCEL_SECONDS": (_CAT5, "5-2. 주문·체결 감시"),
            "CHART_CACHE_TTL_MINUTES": (_CAT5, "5-3. 데이터·통신"),
            "USE_WEBSOCKET": (_CAT5, "5-3. 데이터·통신"),
            "USE_KRX_CLOSE_AFTER_HOURS": (_CAT5, "5-3. 데이터·통신"),
            "JOURNAL_SYNC_USE": (_CAT5, "5-3. 데이터·통신"),
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
            ("0", "설정 초기화", "Reset to Default"),
            ("1", "매수 및 매도 전략 설정", "Buy & Sell Strategy"),
            ("2", "스코어링 및 시장 국면 설정", "Scoring & Regime"),
            ("3", "리스크 및 자산 배분 설정", "Risk & Portfolio"),
            ("4", "기술적 지표 파라미터", "Indicators"),
            ("5", "환경 및 시스템 설정", "Environment & System"),
            ("6", "커스텀 설정 조회 및 초기화", "Manage Custom Settings"),
            # [추세추종 보호] 7번 '시장 국면별 전략 프리셋' 제거 (PRESET_RETIRED 주석 참조)
            ("8", "데이터 캐시 초기화", "Clear Cache"),
            ("9", "시스템 설정 전체 조회", "View Config")
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
                ("1", "진입 조건", "Entry"),
                ("2", "서브전략 (슈퍼 모멘텀/피라미딩)", "Sub-Strategy"),
                ("3", "청산 (손절·트레일링·시간)", "Exit"),
                ("4", "화면 표시 전용 (매매 무관)", "Display Only"),
                _VIEW_ALL_ITEM,
            ]
            sub_choice = utils.show_menu("매수 및 매도 전략 설정", sub_items, default_choice=_VIEW_ALL_KEY)
            if sub_choice.lower() in ['b', 'q']: continue

            sub_map = dict((k, v) for k, v, _ in sub_items)
            context.USER_ACTION_BREADCRUMB.append(f"[{sub_choice}] {sub_map.get(sub_choice, '')}")

            if sub_choice == _VIEW_ALL_KEY:
                view_system_config(1); utils.pause(); continue

            if sub_choice in sub_map:
                items_src = _entry_strategy_items if sub_choice in ("1", "2") else _sell_strategy_items
                _edit_section(f"{sub_map[sub_choice]} (1-{sub_choice})", items_src, f"1-{sub_choice}")

        elif choice == "2":
            sub_items = [("1", "스코어링 가중치 설정", "Weights"), ("2", "적응형 임계값 (시장국면) 설정", "Regime"),
                         _VIEW_ALL_ITEM]
            sub_choice = utils.show_menu("스코어링 및 시장 국면 설정", sub_items, default_choice=_VIEW_ALL_KEY)
            if sub_choice.lower() in ['b', 'q']: continue

            sub_map = dict((k, v) for k, v, _ in sub_items)
            context.USER_ACTION_BREADCRUMB.append(f"[{sub_choice}] {sub_map.get(sub_choice, '')}")

            if sub_choice == _VIEW_ALL_KEY:
                view_system_config(2); utils.pause(); continue
            if sub_choice == "1": modify_scoring_weights()
            elif sub_choice == "2": modify_market_regime_params()
            
        elif choice == "3":
            sub_items = [
                ("1", "자산 배분/포지션", "Portfolio"),
                ("2", "매수 필터", "Filters"),
                ("3", "비상 안전장치", "Safety"),
                _VIEW_ALL_ITEM,
            ]
            sub_choice = utils.show_menu("리스크 및 자산 배분 설정", sub_items, default_choice=_VIEW_ALL_KEY)
            if sub_choice.lower() in ['b', 'q']: continue

            sub_map = dict((k, v) for k, v, _ in sub_items)
            context.USER_ACTION_BREADCRUMB.append(f"[{sub_choice}] {sub_map.get(sub_choice, '')}")

            if sub_choice == _VIEW_ALL_KEY:
                view_system_config(3); utils.pause(); continue

            if sub_choice in sub_map:
                _edit_section(f"{sub_map[sub_choice]} (3-{sub_choice})", _risk_portfolio_items, f"3-{sub_choice}")

        elif choice == "4":
            sub_items = [
                ("1", "데이터·추세", "Data & Trend"),
                ("2", "모멘텀", "Momentum"),
                ("3", "강도·수급·가격구조", "Strength & Structure"),
                _VIEW_ALL_ITEM,
            ]
            sub_choice = utils.show_menu("기술적 지표 파라미터", sub_items, default_choice=_VIEW_ALL_KEY)
            if sub_choice.lower() in ['b', 'q']: continue

            sub_map = dict((k, v) for k, v, _ in sub_items)
            context.USER_ACTION_BREADCRUMB.append(f"[{sub_choice}] {sub_map.get(sub_choice, '')}")

            if sub_choice == _VIEW_ALL_KEY:
                view_system_config(4); utils.pause(); continue

            if sub_choice in sub_map:
                _edit_section(f"{sub_map[sub_choice]} (4-{sub_choice})", _indicator_items, f"4-{sub_choice}")

        elif choice == "5":
            sub_items = [
                ("1", "거래 시간·주기", "Time & Cycle"),
                ("2", "주문·체결 감시", "Order & Conclusion"),
                ("3", "데이터·통신", "Data & Comm"),
                ("4", "텔레그램 및 AI 브리핑", "Telegram"),
                ("5", "화면 및 로그", "Log"),
                _VIEW_ALL_ITEM,
            ]
            sub_choice = utils.show_menu("환경 및 시스템 설정", sub_items, default_choice=_VIEW_ALL_KEY)
            if sub_choice.lower() in ['b', 'q']: continue

            sub_map = dict((k, v) for k, v, _ in sub_items)
            context.USER_ACTION_BREADCRUMB.append(f"[{sub_choice}] {sub_map.get(sub_choice, '')}")

            if sub_choice == _VIEW_ALL_KEY:
                view_system_config(5); utils.pause(); continue

            if sub_choice in ("1", "2", "3"):
                _edit_section(f"{sub_map[sub_choice]} (5-{sub_choice})", _trading_cycle_items, f"5-{sub_choice}")
            elif sub_choice == "4": modify_telegram_settings()
            elif sub_choice == "5": modify_log_settings()

        elif choice == "6":
            manage_custom_settings()
        
        # [추세추종 보호] choice == "7" (전략 프리셋) 제거 — PRESET_RETIRED 주석 참조
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
            # [UX] '전체 조회' 메뉴이므로 그룹 선택을 되묻지 않고 바로 전체를 출력한다.
            #  기존엔 기본값 a(전체)를 물어보기만 하고 대부분 그대로 Enter를 치는 단계였다.
            #  그룹별 조회가 필요하면 해당 그룹 메뉴(1~5)에서 볼 수 있다.
            console.print()
            view_system_config(None)
            utils.pause()
        elif choice == "0": 
            if reset_to_default() is not False: utils.pause()