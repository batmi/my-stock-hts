# main.py
#!/usr/bin/env python3
import sys
import time
import os
import re

# Mac/Linux 환경에서 한글 입력 시 백스페이스(Delete) 시각적 잔상 현상 방지
try:
    import readline
except ImportError:
    pass

# [추가] 프로그램 실행 직후 지연 체감을 줄이기 위한 초기 프로그래스 출력
print("  - 필수 데이터 분석 라이브러리(pandas, yfinance 등) 로딩 중...", flush=True)

from datetime import datetime
import threading
import signal
import logging
from rich.prompt import Prompt, InvalidResponse
from rich.table import Table
from rich import box
from rich.markup import escape
from rich.progress import Progress, SpinnerColumn, BarColumn, TextColumn, TimeRemainingColumn
import argparse
import config
from core import context # [추가]

# [추가] config(rich.console) 로드 후 추가 진행 상태 출력
config.console.print("  - 네트워크 및 코어 모듈(API, DB) 로딩 중...")

import api
from brokers import toss_api  # [추가] 토스증권 클라이언트 (mode 3)
from core import utils
from core import indicators
from modules import market, analysis, chart, account, manage, trading, backtest, settings, db_manager
from modules import auto_trade, telegram_bot, theme_analysis, db_queue # [추가]
from modules.reserved_order_monitor import ReservedOrderMonitor # [추가] 예약주문 모니터

config.console.print("  - 모듈 로딩 완료. 시스템 사전 점검을 준비합니다.\n")

# =========================================================================
# [추가] 글로벌 슬래시 명령어 (/052 등) 구현을 위한 Prompt.ask 몽키패칭
# =========================================================================
class GlobalCommandJump(BaseException):
    """글로벌 메뉴 점프를 위한 사용자 정의 예외"""
    def __init__(self, command_list):
        self.command_list = command_list

_global_command_queue = []
_original_ask = Prompt.ask
_original_check_choice = Prompt.check_choice
_original_process_response = Prompt.process_response

def _custom_check_choice(self, text: str) -> bool:
    # 1. 내부 유효성 검사 우회: 슬래시로 시작하면 조건 없이 통과
    if text.strip().startswith('/'):
        return True
        
    # [추가] 서브메뉴에서 q 입력 시 조건 없이 통과 (메인 메뉴 점프 예외 처리를 위함)
    if text.strip().lower() == 'q' and getattr(context, 'USER_ACTION_BREADCRUMB', []) and len(context.USER_ACTION_BREADCRUMB) > 0:
        return True
        
    return _original_check_choice(self, text)

def _custom_process_response(self, value: str):
    # 2. 내부 루프 강제 탈출: 슬래시 입력을 감지하면 즉시 예외를 던져 루프를 파괴함
    val = value.strip()
    if val.startswith('/'):
        cmd = val[1:].strip()
        # [수정] 숫자 외에도 '@' 기호나 영문 티커 등 다양한 입력을 지원하도록 제한 완화
        if cmd:
            command_list = [c for c in re.split(r'[, ]+', cmd) if c]
            if command_list:
                raise GlobalCommandJump(command_list)
                
        raise InvalidResponse("\n[yellow]유효하지 않은 글로벌 명령어입니다. (예: /5,1 또는 /2 12@)[/yellow]\n")
            
    # [추가] 서브메뉴에서 q 입력 시 빈 점프 예외를 던져 메인 메뉴로 즉시 탈출
    if val.lower() == 'q' and getattr(context, 'USER_ACTION_BREADCRUMB', []) and len(context.USER_ACTION_BREADCRUMB) > 0:
        raise GlobalCommandJump([])
        
    return _original_process_response(self, value)

@classmethod
def _custom_ask(cls, prompt="", *args, **kwargs):
    global _global_command_queue
    if _global_command_queue:
        val = _global_command_queue.pop(0)
        time.sleep(0.15) # 시각적인 딜레이 (타이핑 효과)
        config.console.print(f"{prompt} [cyan]{val}[/cyan]")
        
        # 내부 큐에서 꺼낸 명령어에 대해서도 기본 유효성 검사 수행
        init_kwargs = kwargs.copy()
        init_kwargs.pop('default', None)
        init_kwargs.pop('stream', None)
        
        prompt_obj = cls(prompt, *args, **init_kwargs)
        if prompt_obj.choices is not None:
            if not prompt_obj.check_choice(val):
                config.console.print(prompt_obj.illegal_choice_message)
                _global_command_queue.clear() # 잘못된 매크로는 중단
                return _original_ask(prompt, *args, **kwargs)
                
        try:
            return prompt_obj.process_response(val)
        except InvalidResponse as e:
            config.console.print(e.message if hasattr(e, 'message') else str(e))
            _global_command_queue.clear()
            return _original_ask(prompt, *args, **kwargs)
        except GlobalCommandJump as e:
            _global_command_queue = e.command_list
            return cls.ask(prompt, *args, **kwargs)
            
    return _original_ask(prompt, *args, **kwargs)

Prompt.check_choice = _custom_check_choice
Prompt.process_response = _custom_process_response
Prompt.ask = _custom_ask
Prompt.illegal_choice_message = "\n[yellow]유효하지 않은 선택입니다. 사용 가능한 옵션 중 하나를 선택해 주세요.[/yellow]\n"
# =========================================================================

# =========================================================================
# [추가] readline과 rich.Prompt 충돌로 인한 백스페이스 프롬프트 지워짐 현상 해결
# =========================================================================
import rich.console

_original_console_input = rich.console.Console.input

def _custom_console_input(self, prompt="", *args, **kwargs):
    password = kwargs.get("password", False)
    if not getattr(self, "is_terminal", True) or password:
        return _original_console_input(self, prompt, *args, **kwargs)
        
    try:
        # 1. rich 렌더링 엔진을 통해 마크업 태그가 파싱된 평문(Plain) 텍스트만 추출
        with self.capture() as cap:
            print_kwargs = {k: v for k, v in kwargs.items() if k not in ["stream", "password"]}
            self.print(prompt, *args, end="", **print_kwargs)
        plain_prompt = cap.get()
        
        # 2. 내장 input 함수에 평문 프롬프트를 직접 전달하여 readline 너비 계산 오류(지워짐 현상) 원천 차단
        return input(plain_prompt)
    except Exception:
        return _original_console_input(self, prompt, *args, **kwargs)

rich.console.Console.input = _custom_console_input
# =========================================================================

# =========================================================================
# [추가] 브레드크럼(경로) 출력 몽키패칭
# =========================================================================
_original_print_breadcrumb = utils.print_breadcrumb

def _get_preset_emoji():
    try:
        from modules import settings
        preset = settings.check_and_update_active_preset()
    except Exception:
        preset = getattr(config, 'ACTIVE_PRESET', 'default')
    
    if preset == 'bull': return "🔴"
    elif preset == 'bear': return "🔵"
    elif preset == 'sideways': return "🟡"
    elif preset == 'default': return "🟢"
    elif preset == 'custom': return "⚪"
    else: return "⚪"

def _custom_print_breadcrumb():
    """커스텀 브레드크럼 출력 함수"""
    now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    if getattr(config.session, 'is_paper', False):
        # 가상투자는 KIS 실전 시세를 쓰지만 계좌가 가상이므로 실전과 구분해 표시한다(오인 방지)
        env_str = "[가상투자]"; env_color = "bold cyan"
    elif config.session.is_toss:
        env_str = "[토스증권]"; env_color = "bold magenta"
    elif config.session.is_simulation:
        env_str = "[모의투자]"; env_color = "bold yellow"
    else:
        env_str = "[한투증권]"; env_color = "bold red"
    emoji = _get_preset_emoji()
    
    config.console.print("\n[dim]" + "─"*50 + "[/dim]")
    config.console.print(f" [cyan]시스템 시간: {now_str}[/cyan] | [{env_color}]{env_str}[/] {emoji}")
    config.console.print("[dim]" + "─"*50 + "[/dim]")
    
    if context.USER_ACTION_BREADCRUMB:
        if len(context.USER_ACTION_BREADCRUMB) == 1:
            path_str = context.USER_ACTION_BREADCRUMB[0]
            config.console.print(f"[dim] 메인 메뉴 > [/dim][green]{path_str}[/green]")
        else:
            prev_depths = " > ".join(context.USER_ACTION_BREADCRUMB[:-1])
            current_depth = context.USER_ACTION_BREADCRUMB[-1]
            config.console.print(f"[dim] 메인 메뉴 > {prev_depths} > [/dim][green]{current_depth}[/green]")
    else:
        config.console.print("[green] 메인 메뉴[/green]")
        
    config.console.print("[dim]" + "─"*50 + "[/dim]")

utils.print_breadcrumb = _custom_print_breadcrumb
# =========================================================================

def preflight_check():
    """프로그램 시작 전 필수 시스템 상태를 점검합니다."""
    config.console.print("\n[cyan]시스템 사전 점검 (Pre-flight Check) 시작...[/cyan]")
    checks_ok = True
    
    # 1. API 키 점검
    if config.session.is_toss:
        if not config.session.toss_app_key or not config.session.toss_app_secret:
            config.console.print("  - [bold red]실패[/]: 토스 API Key/Secret(TOSS_APP_KEY)이 설정되지 않았습니다.")
            checks_ok = False
        else:
            config.console.print("  - 성공: 토스 API Key/Secret 확인 완료.")
    elif config.session.is_simulation:
        if not config.session.app_key or not config.session.app_secret:
            config.console.print("  - [bold red]실패[/]: 모의투자 API Key/Secret이 설정되지 않았습니다.")
            checks_ok = False
        else:
            config.console.print("  - 성공: 모의투자 API Key/Secret 확인 완료.")
    else: # 실전
        if not config.session.real_app_key or not config.session.real_app_secret:
            config.console.print("  - [bold red]실패[/]: 한투증권 API Key/Secret이 설정되지 않았습니다.")
            checks_ok = False
        else:
            config.console.print("  - 성공: 한투증권 API Key/Secret 확인 완료.")
        
        if config.session.auto_app_key:
             config.console.print("  - 성공: 자동매매 전용 API Key 확인 완료.")

    if not checks_ok: return False

    # 2. API 토큰 발급 시도
    token_ok = False
    with Progress(
        SpinnerColumn(),
        TextColumn("[progress.description]{task.description}"),
        BarColumn(),
        console=config.console,
        transient=True
    ) as progress:
        progress.add_task("[cyan]  - API 토큰 발급 테스트 중...[/cyan]", total=None)
        if config.session.is_toss:
            token = toss_api.get_access_token(force_refresh=True)
        elif config.session.is_simulation:
            token = api.get_access_token(force_refresh=True)
        else:
            token = api.get_real_access_token(force_refresh=True)
        if token:
            token_ok = True
            # [추가] 토스: 토큰 발급 후 accountSeq 해석(계좌 매칭)
            if config.session.is_toss:
                seq = toss_api.resolve_account_seq(force=True)
                if seq is None:
                    token_ok = False
                    config.console.print("  - [bold red]실패[/]: 토스 계좌(accountSeq)를 확인하지 못했습니다.")

    if token_ok:
        config.console.print("  - 성공: API 토큰 발급 테스트 완료.")
        if config.session.is_toss and config.session.toss_account_seq is not None:
            config.console.print(f"  - 성공: 토스 계좌 확인 (seq={config.session.toss_account_seq}).")
    else:
        config.console.print("  - [bold red]실패[/]: API 토큰 발급에 실패했습니다. (서버 점검 또는 Key 오류)")
        # [추가] 허용 IP(화이트리스트) 미등록 등 원인별 안내 노출
        for line in config.build_token_failure_help(is_toss=config.session.is_toss):
            config.console.print(line)
        checks_ok = False

    if not checks_ok: return False

    # 3. 데이터 원천 점검 — KRX 공식 경로가 켜져 있는가.
    #  [순서 · 2026-08-25] 종목 데이터 로드보다 먼저 찍는다. 마스터 파일 적재는 자기
    #  메시지를 직접 출력해서, 뒤에 두면 '  - 성공:' 로 줄 맞춘 점검 항목들이 그 출력에
    #  끊긴 채 마지막 한 줄만 떨어져 보인다.
    #  자격증명이 없어도 프로그램은 종전 소스로 돌지만 **판단의 원천이 달라진다**.
    #  폴백이 조용하면 운영자는 켜져 있다고 믿는 동안 꺼진 채로 운용하게 된다.
    #  점검을 실패로 만들지는 않는다 — 폴백은 정상 동작이고, 알리는 것이 목적이다.
    try:
        from modules import krx_data
        krx_ok, krx_msg = krx_data.status_text()
        config.console.print(f"  - {'성공' if krx_ok else '[yellow]주의[/yellow]'}: {krx_msg}")
    except Exception as e:      # noqa: BLE001 - 점검 자체가 기동을 막으면 안 된다
        config.console.print(f"  - [dim]KRX 공식 데이터 상태 확인 실패: {e}[/dim]")

    # 4. 종목 데이터 로드 및 누락/오류 exchange 정보 보완
    config.session.load_stock_config()
    
    # [수정] API 현재가 조회 응답에 시장 구분이 없어 오분류(전부 KOSPI)되던 버그를 마스터 리스트를 통해 영구 교정
    from modules import analysis
    # [여백 · 2026-08-25] 마스터 파일 적재는 자기 메시지를 직접 출력한다. 위의 '  - 성공:'
    #  점검 항목들과 붙으면 한 덩어리로 읽히므로 한 줄 띄운다. 이미 적재돼 있으면 아무것도
    #  찍지 않으므로 그때는 빈 줄도 넣지 않는다(빈 줄만 덩그러니 남지 않게).
    if analysis._MASTER_KOSDAQ_CODES is None or analysis._MASTER_KOSPI_CODES is None:
        config.console.print("")
    needs_update = False
    
    for key in ["stocks_kr", "etfs_kr"]:
        for item in config.session.stock_data.get(key, []):
            code = item.get('code')
            if code:
                correct_exchange = analysis._get_market_type_by_master(code)
                if "exchange" not in item or item["exchange"] != correct_exchange:
                    item["exchange"] = correct_exchange
                    needs_update = True
                
    if needs_update:
        config.session.save_stock_config(config.session.stock_data)
        config.session.load_stock_config() # 갱신된 데이터를 메모리 캐시에 다시 로드
        config.console.print("  - 성공: 누락/오류 시장(exchange) 정보 교정 및 업데이트 완료.")

    return checks_ok

def show_help():
    config.console.print("\n[bold cyan]=== [Help] 색상 및 기능 설명 ===[/bold cyan]")
    config.console.print("[dim]※ '~지수명'·'종목명' 항목은 이름(첫 컬럼) 글자색 규칙이고, '지수값·현재가/등락률/52주 고점대비' 항목은 값 자체의 글자색 규칙입니다.[/dim]")
    config.console.print("[dim]※ 이름 색은 두 축입니다 — 방향성 자산은 '국면'(빨강/주황/하늘/파랑), 수준 자체가 매크로 의미인 자산(VIX·미국채·달러·유가/가스/밀)은 '위험도 밴드'(초록·보라 포함)입니다.[/dim]")
    config.console.print("[dim]※ 값 색은 자산 종류와 무관하게 '값 자체의 방향'만 나타냅니다(빨강=상승/강세). VIX·금리·달러처럼 오를수록 시장에 불리한 자산은 값이 빨개도 위험 신호이며, 그 해석은 지수명(위험도 밴드) 색으로 확인하세요.[/dim]")
    table = Table(title="지수 및 종목 상태별 색상 조건", box=box.HORIZONTALS, header_style="dim", border_style="dim")
    table.add_column("항목", style="bold"); table.add_column("조건", justify="left")
    table.add_column("색상", justify="center"); table.add_column("비고", justify="left")

    # [수정] 설정값 로드하여 동적 표시
    _rp = config.MARKET_REGIME_PARAMS
    ema_fast = _rp.get('REGIME_EMA_FAST', 9)
    ema_slow = _rp.get('REGIME_EMA_SLOW', 41)
    confirm_pct = _rp.get('REGIME_CONFIRM_PCT', 5.0)
    obv_period = config.INDICATOR_PARAMS.get("OBV_MA_PERIOD", 5)

    table.add_row("시장 지수명", f"EMA{ema_fast} > EMA{ema_slow} & 교차 후 {confirm_pct:+g}% 진행", "[red]빨간색[/]", "강세장 (Bull) - 확정 상승추세")
    table.add_row("(국내/미국/유럽/아시아 지수, KRX 금현물,", f"EMA{ema_fast} > EMA{ema_slow} & {confirm_pct:g}% 미달", "[orange3]주황색[/]", "상승 미확정 (PendUp)")
    table.add_row(" MSCI, 섹터지수 9종,", f"EMA{ema_fast} < EMA{ema_slow} & {confirm_pct:g}% 미달", "[sky_blue3]하늘색[/]", "하락 미확정 (PendDown) - 추세 붕괴 초기")
    table.add_row(" 금·은·구리, 암호화폐)", f"EMA{ema_fast} < EMA{ema_slow} & 교차 후 {-confirm_pct:+g}% 진행", "[blue]파란색[/]", "약세장 (Bear) - 확정 하락추세")
    table.add_row("", "데이터 부족으로 판정 불가", "[yellow]노란색[/]", "판정 보류 (Sideways)")
    table.add_section()

    # [수정] 미국채 금리 밴드는 config.US_TREASURY_YIELD_BANDS 단일 소스에서 생성한다
    #  (지수명 색상은 market, 상태 문구는 theme_analysis가 같은 소스를 공유 — 수정 누락 방지)
    color_kr = {"magenta": "보라색", "red": "빨간색", "orange3": "주황색",
                "green": "초록색", "yellow": "노란색", "blue": "파란색"}
    for t_name, t_meta in config.US_TREASURY_YIELD_BANDS.items():
        bands = t_meta["bands"]
        for i, (thr, color, _status, help_desc) in enumerate(bands):
            if i == 0:
                col1, cond = f'{t_meta["title"]} 지수명', f"금리 ≥ {thr:.2f}"
            elif thr is None:
                col1, cond = "", f"금리 < {bands[i-1][0]:.2f}"
            else:
                col1 = f"({t_meta['subtitle']})" if i == 1 else ""
                cond = f"{thr:.2f} ≤ 금리 < {bands[i-1][0]:.2f}"
            table.add_row(col1, cond, f"[{color}]{color_kr[color]}[/]", help_desc)
        table.add_section()

    table.add_row("브랜트유 지수명", "가격 ≥ 105", "[magenta]보라색[/]", "에너지 쇼크 (강제적 수요 파괴 및 스태그플레이션)")
    table.add_row("", "95 ≤ 가격 < 105", "[red]빨간색[/]", "인플레 재발 우려 (고금리 장기화 강요)")
    table.add_row("", "85 ≤ 가격 < 95", "[orange3]주황색[/]", "고유가 지속 (인플레 압력 상존)")
    table.add_row("", "70 ≤ 가격 < 85", "[green]초록색[/]", "골디락스 (산유국 수익과 물가 안정의 최적 균형점)")
    table.add_row("", "60 ≤ 가격 < 70", "[yellow]노란색[/]", "수요 둔화 (경기 하강 신호)")
    table.add_row("", "가격 < 60", "[blue]파란색[/]", "시스템 위기 (심각한 수요 파괴 및 침체)")
    table.add_section()

    table.add_row("WTI 원유 지수명", "가격 ≥ 100", "[magenta]보라색[/]", "에너지 쇼크 (강제적 수요 파괴 및 스태그플레이션)")
    table.add_row("", "90 ≤ 가격 < 100", "[red]빨간색[/]", "인플레 재발 우려 (고금리 장기화 강요)")
    table.add_row("", "80 ≤ 가격 < 90", "[orange3]주황색[/]", "고유가 지속 (인플레 압력 상존)")
    table.add_row("", "65 ≤ 가격 < 80", "[green]초록색[/]", "골디락스 (산유국 수익과 물가 안정의 최적 균형점)")
    table.add_row("", "55 ≤ 가격 < 65", "[yellow]노란색[/]", "수요 둔화 (경기 하강 신호)")
    table.add_row("", "가격 < 55", "[blue]파란색[/]", "시스템 위기 (심각한 수요 파괴 및 침체)")
    table.add_section()

    table.add_row("가솔린 RBOB 지수명", "가격 ≥ 4.00", "[magenta]보라색[/]", "에너지 쇼크: 강제적 수요 파괴 및 스태그플레이션 확정")
    table.add_row("", "3.20 ≤ 가격 < 4.00", "[red]빨간색[/]", "임계점: 고금리 긴축 강요, 기업 이익률 급격 둔화")
    table.add_row("", "2.60 ≤ 가격 < 3.20", "[orange3]주황색[/]", "고유가 지속: 인플레 압력 상존, 실물 경제 마지노선")
    table.add_row("", "2.10 ≤ 가격 < 2.60", "[green]초록색[/]", "골디락스: 산유국 수익성과 물가 안정의 최적 균형점")
    table.add_row("", "1.60 ≤ 가격 < 2.10", "[yellow]노란색[/]", "수요 둔화: 경기 하강 신호 및 에너지 투자 위축 시작")
    table.add_row("", "가격 < 1.60", "[blue]파란색[/]", "시스템 위기: 심각한 경기 침체 혹은 금융 위기 동반")
    table.add_section()

    # 밴드는 가솔린 RBOB 밴드의 백분위를 ULSD 분포(15년)에 옮긴 값이다 — modules/market.py 주석 참조.
    table.add_row("디젤 ULSD 지수명", "가격 ≥ 4.40", "[magenta]보라색[/]", "산업·물류 비용 쇼크: 화물 운임 전가로 전 산업 원가 상승")
    table.add_row("", "3.70 ≤ 가격 < 4.40", "[red]빨간색[/]", "임계점: 제조·물류 마진 압박, 원가 전가 본격화")
    table.add_row("", "2.85 ≤ 가격 < 3.70", "[orange3]주황색[/]", "물류비 인플레: 비용 압력 상존하나 산업 활동은 유지")
    table.add_row("", "2.25 ≤ 가격 < 2.85", "[green]초록색[/]", "골디락스: 산업 활동과 물가 안정의 균형점")
    table.add_row("", "1.70 ≤ 가격 < 2.25", "[yellow]노란색[/]", "산업 수요 둔화: 화물 물동량 감소 — 경기 하강 선행 신호")
    table.add_row("", "가격 < 1.70", "[blue]파란색[/]", "실물 경기 급랭: 산업 수요 파괴 수준")
    table.add_section()

    table.add_row("천연가스 지수명", "가격 ≥ 6.0", "[magenta]보라색[/]", "에너지 쇼크 (공급망 붕괴 또는 극단적 기후 위기)")
    table.add_row("", "4.0 ≤ 가격 < 6.0", "[red]빨간색[/]", "물가 비상 (에너지 인플레 유발)")
    table.add_row("", "3.0 ≤ 가격 < 4.0", "[orange3]주황색[/]", "수급 타이트 (겨울철 피크 또는 수출 수요 강세)")
    table.add_row("", "2.0 ≤ 가격 < 3.0", "[green]초록색[/]", "안정/중립 (현재 박스권 최적 균형점)")
    table.add_row("", "1.5 ≤ 가격 < 2.0", "[yellow]노란색[/]", "공급 과잉 (생산 업체 수익성 악화 우려)")
    table.add_row("", "가격 < 1.5", "[blue]파란색[/]", "시스템 하강 (심각한 수요 파괴 또는 디플레이션 신호)")
    table.add_section()

    table.add_row("밀 지수명", "가격 ≥ 800", "[magenta]보라색[/]", "식량 안보 위기 (전쟁/극단적 기후)")
    table.add_row("", "700 ≤ 가격 < 800", "[red]빨간색[/]", "식량 인플레 경계 (애그플레이션 우려)")
    table.add_row("", "600 ≤ 가격 < 700", "[orange3]주황색[/]", "수급 타이트 (기후 리스크 및 작황 부진)")
    table.add_row("", "500 ≤ 가격 < 600", "[green]초록색[/]", "안정/중립 (현재 박스권 최적 균형점)")
    table.add_row("", "400 ≤ 가격 < 500", "[yellow]노란색[/]", "공급 과잉 (풍작/재고 증가)")
    table.add_row("", "가격 < 400", "[blue]파란색[/]", "농가 수익성 악화 (디플레이션 신호)")
    table.add_section()

    table.add_row("달러 인덱스 지수명", "지수 ≥ 115", "[magenta]보라색[/]", "글로벌 달러 유동성 경색 (시스템 위기)")
    table.add_row("", "110 ≤ 지수 < 115", "[red]빨간색[/]", "초강달러 (신흥국 자본 유출 패닉)")
    table.add_row("", "105 ≤ 지수 < 110", "[orange3]주황색[/]", "강달러 경계 (미국 외 국가 인플레 자극)")
    table.add_row("", "95 ≤ 지수 < 105", "[green]초록색[/]", "안정/중립 (가장 이상적인 골디락스)")
    table.add_row("", "지수 < 95", "[blue]파란색[/]", "달러 약세 (신흥국/위험자산 랠리)")
    table.add_section()

    table.add_row("달러 환율 지수명", "환율 ≥ 1500원", "[magenta]보라색[/]", "시스템 위기 / 외환 패닉")
    table.add_row("", "1450 ≤ 환율 < 1500", "[red]빨간색[/]", "위험 구간 (당국 개입 및 자본 유출 우려)")
    table.add_row("", "1400 ≤ 환율 < 1450", "[orange3]주황색[/]", "구조적 고환율 (경제 부담 가중)")
    table.add_row("", "1300 ≤ 환율 < 1400", "[green]초록색[/]", "강달러 뉴노멀 (현재 시장 중립 구간)")
    table.add_row("", "1200 ≤ 환율 < 1300", "[sky_blue3]하늘색[/]", "안정화 (원화 강세 전환)")
    table.add_row("", "환율 < 1200", "[blue]파란색[/]", "초강세 원화 (수출 기업 실적 부담)")
    table.add_section()

    table.add_row("VIX 변동성 지수명", "지수 < 15", "[green]초록색[/]", "안정 (평균 수준/골디락스장)")
    table.add_row("(VIX·V코스피200 공통)", "15 ≤ 지수 < 20", "[yellow]노란색[/]", "경계 진입 (단기 변동성 확대/노이즈)")
    table.add_row("", "20 ≤ 지수 < 30", "[orange3]주황색[/]", "위험 구간 (추세 훼손/조정장 진입)")
    table.add_row("", "30 ≤ 지수 < 40", "[red]빨간색[/]", "공포/패닉 (급락장/베어마켓)")
    table.add_row("", "지수 ≥ 40", "[magenta]보라색[/]", "시스템 위기 (블랙스완/투매)")
    table.add_section()

    table.add_row("HY OAS 지수명", "지수 < 4.0", "[green]초록색[/]", "Risk-On: 안정 (평화로운 시기, 주식 비중 유지)")
    table.add_row("(신용위험/스프레드)", "4.0 ≤ 지수 < 5.0", "[orange3]주황색[/]", "스프레드 확대 조짐 (주의 구간)")
    table.add_row("", "5.0 ≤ 지수 < 8.0", "[red]빨간색[/]", "Warning: 경계 (부도위험 반영 시작, 레버리지 축소)")
    table.add_row("", "지수 ≥ 8.0", "[magenta]보라색[/]", "Risk-Off: 위기 (침체 본궤도 진입, 폭락장 우려)")
    table.add_section()

    table.add_row("등락폭/등락률 값", "상승 (> 0)", "[red]빨간색[/]", "전일 대비 상승")
    table.add_row("", "하락 (< 0)", "[blue]파란색[/]", "전일 대비 하락")
    table.add_row("", "보합 (== 0)", "[white]흰색[/]", "전일 대비 보합")
    table.add_section()

    table.add_row("52주 고점대비 값", "등락률 > -3.0%", "[red]빨간색[/]", "신고가 근접 (초강세)")
    table.add_row("", "등락률 < -20.0%", "[blue]파란색[/]", "침체/약세장 진입")
    table.add_row("", "-3.0% ~ -20.0%", "[white]흰색[/]", "일반 조정/중립")
    table.add_row("", "[dim]VIX·미국채·달러 등 역방향 자산[/dim]", "", "[dim]빨간색(고점 근접) = 초강세가 아니라 위험 고조로 해석[/dim]")
    table.add_section()

    # [통일] 종목명 색상은 시장 지수와 동일한 국면 룰(이중 EMA + 추종 확인)을 쓴다
    table.add_row("종목명 색상", f"EMA{ema_fast} > EMA{ema_slow} & 교차 후 {confirm_pct:+g}% 진행", "[red]빨간색[/]", "강세장 (Bull) - 확정 상승추세")
    table.add_row("", f"EMA{ema_fast} > EMA{ema_slow} & {confirm_pct:g}% 미달", "[orange3]주황색[/]", "상승 미확정 (PendUp)")
    table.add_row("", f"EMA{ema_fast} < EMA{ema_slow} & {confirm_pct:g}% 미달", "[sky_blue3]하늘색[/]", "하락 미확정 (PendDown) - 추세 붕괴 초기")
    table.add_row("", f"EMA{ema_fast} < EMA{ema_slow} & 교차 후 {-confirm_pct:+g}% 진행", "[blue]파란색[/]", "약세장 (Bear) - 확정 하락추세")
    table.add_row("", "데이터 부족으로 판정 불가", "[yellow]노란색[/]", "판정 보류 (Sideways)")
    table.add_section()

    buy_score = config.ANALYSIS_THRESHOLDS["BUY_SCORE"]
    rise_score = config.ANALYSIS_THRESHOLDS["RISE_SCORE"]
    buy_rsi = config.ANALYSIS_THRESHOLDS["BUY_RSI_MAX"]
    # [추가] 슈퍼 모멘텀(강매수)은 기본 ON이라 실제 화면에 보라색 종목이 나타난다 — 설정 OFF면 행을 감춘다
    _th = config.ANALYSIS_THRESHOLDS
    if _th.get("SUPER_MOMENTUM_USE", True):
        _sm_score = _th.get("SUPER_MOMENTUM_SCORE", 8.0)
        _sm_w52 = _th.get("SUPER_MOMENTUM_W52_POS", 90.0)
        _sm_rsi = _th.get("SUPER_BUY_RSI_MAX", 80.0)
        table.add_row("종목 분류", f"{_sm_score}점 이상 & 52주 위치 {_sm_w52:g}% 이상 & RSI<{_sm_rsi:g}",
                      "[magenta]강매수[/]", "슈퍼 모멘텀 (주도주 랠리 — RSI 상한 완화 적용)")
        table.add_row("", f"{buy_score}점 이상 & RSI<{buy_rsi}", "[red]매수[/]", "강력 매수 구간 (분할 진입)")
    else:
        table.add_row("종목 분류", f"{buy_score}점 이상 & RSI<{buy_rsi}", "[red]매수[/]", "강력 매수 구간 (분할 진입)")
    table.add_row("", f"{buy_score}점 이상 & RSI≥{buy_rsi} (과열)", "[orange3]대기[/]", "매수 직전 — 점수는 충족, RSI 식으면 매수 (눌림목 매수 대기)")
    table.add_row("", f"{rise_score} ~ {buy_score}점 미만 (상승 추세)", "[orange3]상승[/]", "상승 초입/지속 (점수 축적 대기/소량)")
    table.add_row("", "정렬 미완성 + 추세전환 초기신호 ≥3개 (위험신호 없음)", "[green]관심[/]", "태동 단계/수동 스윙 모니터링 (120일선 아래도 포착)")
    table.add_row("", "방향성 불명확 단계", "[white]관망[/]", "방향성 탐색 (거래 비권장)")
    table.add_row("", "추세 이탈 / 단기 하락", "[yellow]주의[/]", "신규매수 자제/비중축소 고려")
    table.add_row("", "장기추세 붕괴 및 과열", "[blue]매도[/]", "적극 매도/손절 고려 (위험)")
    table.add_section()

    # [추가] 추세품질 밴드 — 종목 분류 바로 아래에 둔다. 표의 '분류' 컬럼이 두 값을 함께
    #  보여주므로(예: 매수 (143)) 설명도 붙어 있어야 한다.
    #  값이 연환산 기울기 × R²의 곱이라 크기 감이 안 잡혀(p50 0.2 · p90 133) 밴드 없이는 못 읽는다.
    #  경계는 indicators.TREND_QUALITY_BANDS 단일 소스에서 읽는다 — 문구와 코드가 어긋나지 않도록.
    tq_lookback = config.INDICATOR_PARAMS.get("TREND_QUALITY_LOOKBACK", 90)
    tq_color = indicators.TREND_QUALITY_COLORS
    tq_note = {
        "강함": "검증된 주도주 — 동점 후보 중 최우선",
        "양호": "추세가 잡힌 일반적인 진입 후보",
        "약함": "기울기는 살아 있으나 매끄럽지 않음 (R² 낮음)",
        "미검증": "기울기가 미미하거나 R²가 낮음 (횡보 끝 급등 포함)",
        "하락": "회귀선이 우하향 — 추세추종 대상 아님",
    }
    _tq = list(indicators.TREND_QUALITY_BANDS)
    _tq_rows = [(f"{_tq[-1][0]:g} 이상", "강함")]
    for i in range(len(_tq) - 1, -1, -1):
        _upper, _label = _tq[i]
        _tq_rows.append((f"{_tq[i-1][0]:g} ~ {_upper:g} 미만" if i else f"{_upper:g} 미만", _label))
    _tq_rows.append((f"거래일 {tq_lookback}일 미만", "이력부족"))
    for i, (_cond, _label) in enumerate(_tq_rows):
        col1 = "추세품질 (TQ)" if i == 0 else (f"(Clenow 모멘텀 · 최근 {tq_lookback}일)" if i == 1 else "")
        table.add_row(col1, _cond, f"[{tq_color[_label]}]{_label}[/]",
                      tq_note.get(_label, "이력이 짧아 판정 불가 — 동점 시 최하순위"))
    table.add_row("", "[dim]연환산 기울기(%) × R²[/dim]", "",
                  "[dim]종목 표 '분류' 컬럼에 괄호로 함께 표기 — 예: 매수 (143)[/dim]")
    table.add_row("", "[dim]매수 여부는 가르지 않는다(게이트 아님)[/dim]", "",
                  "[dim]점수가 1순위, 점수 동점일 때만 이 값이 순위를 가른다[/dim]")
    table.add_section()


    # [추가] 보유 분석([9]-2 잔고 '상태' 컬럼) — 종목 분류와 달리 포지션 컨텍스트까지 반영한다
    table.add_row("보유 분석", "익절/손절/시간청산/트레일링스탑/추세이탈 중 하나 충족", "[blue]청산[/]",
                  "[9]-2 잔고 '상태' 컬럼 — 시스템 매도 신호 (사유는 표 아래 각주)")
    table.add_row("([9]-2 잔고,", "청산 조건 미충족", "위 종목 분류 색", "보유 유지 — 상태/점수를 그대로 표시")
    table.add_row(" [9]-5 수동 분석)", "잔고에 없는 포지션을 직접 입력", "", "[9]-5 — 매수단가·수량·매수일 입력, 판정 기준은 [9]-2와 동일")
    table.add_row("", "트레이딩 제한 / ETF 제외 설정 / 해외 종목", "[yellow]수동[/]",
                  "시스템 자동 매도 대상 아님 — 청산 신호가 떠도 직접 처리해야 함")
    table.add_row("", "매입단가·최고가·보유일수·개별 룰 반영", "", "메뉴 [2] 종목 분석과 결과가 다를 수 있음")
    table.add_section()

    # [통일] 지수 화면·종목 표·개별 분석이 analysis.price_trend_color 단일 소스를 공유한다
    table.add_row("지수값·종목 현재가", "현재가 > 20일선 & 20일선 > 60일선", "[red]빨간색[/]", "강세: 중장기 상승 추세 속 단기 강세")
    table.add_row("(추세)", "현재가 ≥ 20일선 & 20일선 < 60일선", "[orange3]주황색[/]", "반등 시도: 장기 약세 속 단기 추세 전환 시도")
    table.add_row("", "현재가 ≤ 20일선 & 20일선 > 60일선", "[yellow]노란색[/]", "눌림목 조정: 장기 강세 속 단기 조정")
    table.add_row("", "현재가 < 20일선 & 20일선 < 60일선", "[blue]파란색[/]", "약세: 중장기 하락 추세 속 단기 약세")
    table.add_row("", "20일선 == 60일선 (혼조)", "[white]흰색[/]", "방향 판단 보류")
    table.add_row("", "이평선 산출 불가 (데이터 부족)", "[dim]회색[/]", "판정 불가 (눌림목 노란색과 구분)")
    table.add_section()

    table.add_row("체결강도", "150% 이상", "[magenta]보라색[/]", "강력한 수급: 공격적인 매수세 유입, 주가 급등 가능성 높음")
    table.add_row("", "120% 이상 ~ 150% 미만", "[red]빨간색[/]", "매수 우위: 상승 추세 강화, 단기 모멘텀 발생")
    table.add_row("", "100% 초과 ~ 120% 미만", "[orange3]주황색[/]", "점진적 유입: 완만한 매수세, 주가 하방 지지력 강함")
    table.add_row("", "100% (동일)", "[white]흰색[/]", "균형: 매수와 매도의 힘이 팽팽하게 맞서는 상태")
    table.add_row("", "80% 이상 ~ 100% 미만", "[yellow]노란색[/]", "매도 우위: 주가 탄력 둔화, 관망세 확산")
    table.add_row("", "80% 미만", "[blue]파란색[/]", "하락 압력: 공격적인 매도세, 추가 하락 경계 필요")
    table.add_section()

    # [추가] 토스증권은 체결강도를 제공하지 않아 매도잔량비로 대체 표시된다 — 실제 화면 항목이므로 함께 안내
    #  값은 매도/매수 총잔량 비율 — 높을수록 매물을 받아내는 매수 압력으로 보아 체결강도와 같은 색 방향(빨강=양호)
    table.add_row("매도잔량비 [x.xx]", "2.00배 이상", "[magenta]보라색[/]", "매물 흡수 극대 (강한 수급 유입)")
    table.add_row("(토스 전용, 체결강도 대체)", "1.50배 이상 ~ 2.00배 미만", "[red]빨간색[/]", "수급 양호 (매도 물량 소화 중)")
    table.add_row("", "1.00배 초과 ~ 1.50배 미만", "[orange3]주황색[/]", "점진적 유입 (매수 기준선 충족)")
    table.add_row("", "1.00배 (동일)", "[white]흰색[/]", "잔량 균형")
    table.add_row("", "0.70배 이상 ~ 1.00배 미만", "[yellow]노란색[/]", "수급 둔화 (매수 기준선 미달)")
    table.add_row("", "0.70배 미만", "[blue]파란색[/]", "수급 약세")
    table.add_row("", "[dim]NXT 운영시간(08:00~20:00) 밖[/dim]", "", "[dim]호가 동결 구간 — 표기 생략[/dim]")
    table.add_section()

    table.add_row("52주 위치", "90% 이상", "[red]빨간색[/]", "신고가 근접/초강세")
    table.add_row("", "80% 이상 ~ 90% 미만", "[orange3]주황색[/]", "상승세 우위")
    table.add_row("", "50% 초과 ~ 80% 미만", "[white]흰색[/]", "중립")
    table.add_row("", "30% 초과 ~ 50% 이하", "[yellow]노란색[/]", "약세/바닥권 진입")
    table.add_row("", "30% 이하", "[blue]파란색[/]", "신저가 근접/침체")
    table.add_section()

    table.add_row("EMA 5일선 (초단기)", "5일선 > 20일선", "[red]빨간색[/]", "초단기 정배열 (단기 모멘텀)")
    table.add_row("", "5일선 < 20일선", "[blue]파란색[/]", "초단기 역배열")
    table.add_section()

    table.add_row("EMA 20일선 (단기)", "20일선 > 60일선", "[red]빨간색[/]", "단기 정배열 (추세선)")
    table.add_row("", "20일선 < 60일선", "[blue]파란색[/]", "단기 역배열")
    table.add_section()

    table.add_row("EMA 60일선 (중기)", "60일선 > 120일선", "[red]빨간색[/]", "중장기 정배열 (수급선)")
    table.add_row("", "60일선 < 120일선", "[blue]파란색[/]", "중장기 역배열")
    table.add_section()

    table.add_row("EMA 120일선 (장기)", "120일선 상승 추세", "[red]빨간색[/]", "장기 추세 상승 (경기선)")
    table.add_row("", "120일선 하락 추세", "[blue]파란색[/]", "장기 추세 하락")
    table.add_section()

    table.add_row("파라볼릭 SAR", "주가 > SAR (SAR이 주가 아래)", "[red]빨간색[/]", "상승 추세 (매수/보유)")
    table.add_row("", "주가 < SAR (SAR이 주가 위)", "[blue]파란색[/]", "하락 추세 (매도/청산)")
    table.add_section()

    table.add_row("추세SMO", "S (SAR)", "[red]▲[/] / [blue]▼[/]", "상승 / 하락")
    table.add_row("", "M (MACD)", "[red]+G[/] / [blue]-D[/]", "골든/데드 (0선 위/아래)")
    table.add_row("", "O (OBV)", "[red]▲[/] / [blue]▼[/]", f"수급 양호 (OBV > EMA {obv_period}일) / 약세")
    table.add_section()

    rsi_upper = config.INDICATOR_PARAMS["RSI_UPPER"]
    rsi_lower = config.INDICATOR_PARAMS["RSI_LOWER"]

    table.add_row("RSI", f"RSI ≥ {rsi_upper}", "[magenta]보라색[/]", "과열 (추격금지)")
    table.add_row("", f"55 ≤ RSI < {rsi_upper}", "[red]빨간색[/]", "강세 유지 구간")
    table.add_row("", "45 ≤ RSI < 55", "[orange3]주황색[/]", "강세 조정 구간 (진입후보)")
    table.add_row("", f"{rsi_lower} < RSI < 45", "[yellow]노란색[/]", "단기하락 전환가능")
    table.add_row("", f"RSI ≤ {rsi_lower}", "[blue]파란색[/]", "하락")
    table.add_section()

    table.add_row("ADX", "0 ~ 15 미만", "[white]흰색[/]", "추세 없음 (횡보/박스권)")
    table.add_row("", "15 ~ 20 미만", "[yellow]노란색[/]", "추세 형성 중 (CCI 방향 확인)")
    table.add_row("", "20 ~ 30 미만", "[orange3]주황색[/]", "안정적 추세 (매매 최적)")
    table.add_row("", "30 ~ 40 미만", "[red]빨간색[/]", "강한 추세 (과열 주의)")
    table.add_row("", "40 이상", "[magenta]보라색[/]", "과열 (조정 주의)")
    table.add_section()

    # ADX 셀 뒤에 붙는 DMI 방향 아이콘 (표 폭을 늘리지 않기 위해 ADX는 정수 표기)
    table.add_row("DMI (ADX 뒤 기호)", "+DI > -DI", "[red]▲ 빨간색[/]", "상승 방향 우위")
    table.add_row("", "-DI > +DI", "[blue]▼ 파란색[/]", "하락 방향 우위")
    table.add_row("", f"DX < {analysis.DMI_NEUTRAL_DX:.0f} (격차 미미)", "[dmi.neutral]● 회백색[/]", "중립 (방향성 없음)")
    table.add_section()

    cci_upper = config.INDICATOR_PARAMS["CCI_UPPER"]
    cci_lower = config.INDICATOR_PARAMS["CCI_LOWER"]

    table.add_row("CCI", f"CCI ≥ {cci_upper}", "[red]빨간색[/]", "과열 (추격 금물)")
    table.add_row("", f"0 < CCI < {cci_upper}", "[orange3]주황색[/]", "상승 방향시 (추세 매매)")
    table.add_row("", f"{cci_lower} < CCI < 0", "[yellow]노란색[/]", "상승 방향시 (반등 시도)")
    table.add_row("", f"CCI ≤ {cci_lower}", "[blue]파란색[/]", "과매도 (저점 탐색)")
    table.add_section()

    table.add_row("OBV (거래량)", f"OBV > EMA {obv_period}일선", "[red]빨간색[/]", "수급 양호 (매집)")
    table.add_row("", f"OBV < EMA {obv_period}일선", "[blue]파란색[/]", "수급 약세 (분산)")
    table.add_row("", "주가 하락 + OBV 상승", "", "강세 다이버전스 (반등 시그널)")
    table.add_row("", "주가 상승 + OBV 하락", "", "약세 다이버전스 (하락 시그널)")
    table.add_section()

    table.add_row("투자자 동향", "순매수 (> 0)", "[red]빨간색[/]", "매수 우위")
    table.add_row("(개인/외인/기관)", "순매도 (< 0)", "[blue]파란색[/]", "매도 우위")
    table.add_section()

    table.add_row("MDD (최대 낙폭)", "0% ~ -10%", "[green]초록색[/]", "매우 안정적 (방어력 우수)")
    table.add_row("", "-10% ~ -20%", "[yellow]노란색[/]", "일반적인 주식 투자 수준")
    table.add_row("", "-20% ~ -30%", "[orange3]주황색[/]", "주의 (높은 변동성)")
    table.add_row("", "-30% 이하", "[red]빨간색[/]", "고위험 (큰 하락 감내 필요)")
    table.add_section()

    table.add_row("손익비 (Profit Factor)", "2.0 이상", "[red]빨간색[/]", "매우 훌륭한 전략")
    table.add_row("", "1.5 ~ 2.0 미만", "[orange3]주황색[/]", "우수한 전략")
    table.add_row("", "1.0 ~ 1.5 미만", "[green]초록색[/]", "수익 전략 (평범)")
    table.add_row("", "1.0 미만", "[blue]파란색[/]", "손실 전략 (개선 필요)")
    table.add_section()

    table.add_row("샤프 지수 (Sharpe)", "1.0 이상", "[red]빨간색[/]", "매우 우수 (위험 대비 수익 높음)")
    table.add_row("", "0.5 ~ 1.0", "[green]초록색[/]", "양호")
    table.add_row("", "0.5 미만", "[blue]파란색[/]", "위험 대비 수익 낮음")
    table.add_section()

    config.console.print(table)
    
    # [추가] 스코어링 가중치 계산
    weights = config.SCORING_WEIGHTS
    r_trend = weights.get("TREND", 4.0) / 4.0
    r_mom = weights.get("MOMENTUM", 2.5) / 2.5
    r_str = weights.get("STRENGTH", 1.5) / 1.5
    r_syn = weights.get("SYNERGY", 2.0) / 2.0

    regime = config.MARKET_REGIME_PARAMS
    ema_f = regime.get('REGIME_EMA_FAST', 9)
    ema_s = regime.get('REGIME_EMA_SLOW', 41)
    confirm_p = regime.get('REGIME_CONFIRM_PCT', 5.0)

    market_status_info = None
    filter_info = None
    try:
        with Progress(
            SpinnerColumn(),
            TextColumn("[progress.description]{task.description}"),
            BarColumn(),
            console=config.console,
            transient=True
        ) as progress:
            progress.add_task("[cyan]현재 시장 상태 및 필터링 분석 중...[/cyan]", total=None)
            kospi_regime, kospi_adj = analysis.get_market_regime("KOSPI")
            kosdaq_regime, kosdaq_adj = analysis.get_market_regime("KOSDAQ")
        
            k_r_str = analysis.format_regime(kospi_regime) + "*"
            q_r_str = analysis.format_regime(kosdaq_regime) + "*"

            market_status_info = {
                "kospi_str": k_r_str, "kospi_adj": kospi_adj,
                "kosdaq_str": q_r_str, "kosdaq_adj": kosdaq_adj
            }
            
            # [추가] 실시간 필터링 상태 계산
            if getattr(config, 'USE_MARKET_FILTER', True):
                filter_info = {}
                ma_period_filter = getattr(config, 'MARKET_FILTER_MA', 80)
                band_filter = getattr(config, 'MARKET_FILTER_BAND', 1.0)
                for m_type in ["KOSPI", "KOSDAQ"]:
                    try:
                        df = analysis.get_domestic_index_data(m_type)
                        if df is not None and not df.empty and len(df) >= ma_period_filter:
                            # 실매매와 같은 판정 함수(밴드 히스테리시스 포함)
                            filter_info[m_type] = not bool(indicators.get_market_filter_blocked(
                                df['close'], ma_period_filter, band_filter).iloc[-1])
                    except Exception:
                        pass
    except Exception:
        pass

    # [추가] 매수 점수 산정 기준 테이블 (README 내용 반영)
    config.console.print()
    score_table = Table(title="스코어링 및 매매 전략 가이드", box=box.HORIZONTALS, header_style="dim", border_style="dim")
    score_table.add_column("구분", style="cyan", justify="left")
    score_table.add_column("조건", justify="left")
    score_table.add_column("점수/행동", justify="center", style="white")
    score_table.add_column("의미", justify="left")

    def _help_show(key, active):
        """도움말 행 노출 여부 — 시스템 설정 메뉴(메인메뉴 0)의 숨김 처리와 연동.

        추세추종 보호·백테스트 보호로 숨긴 설정(settings.ANTI_TREND_HIDDEN_KEYS /
        BACKTESTED_HIDDEN_KEYS)은 운영자가 조정할 수 없으므로, 그 기능이 꺼져 있으면
        도움말에서도 감춘다 — 끌 수도 켤 수도 없는 'OFF' 행이 남아 있으면 매매 판정
        기준을 오해하게 된다. 숨김 집합에서 키를 빼면 도움말에도 자동으로 되돌아온다.
        동작 중인 기능(예: 본전청산·시간청산)은 다이얼이 잠겨 있어도 그대로 노출한다 —
        실제 매매에 관여하는 규칙을 감추면 안 되기 때문이다.
        """
        if active:
            return True
        return (key not in settings.ANTI_TREND_HIDDEN_KEYS
                and key not in settings.BACKTESTED_HIDDEN_KEYS)

    # [수정] 표는 analysis.calculate_score 실구현과 1:1 대응 (구식 항목 제거·누락 항목 반영)
    _ip = config.INDICATOR_PARAMS
    rsi_mid = _ip.get('SCORE_RSI_MID', 50); rsi_strong = _ip.get('SCORE_RSI_STRONG', 60)
    rsi_overheat = _ip.get('SCORE_RSI_OVERHEAT', 80); rsi_rebound = _ip.get('SCORE_RSI_REBOUND', 40)
    score_adx = _ip.get('SCORE_ADX_MIN', 20)
    persist_lb = _ip.get('TREND_PERSIST_LOOKBACK', 120); persist_min = _ip.get('TREND_PERSIST_MIN', 70)
    w52_near = _ip.get('MOMENTUM_W52_NEAR', 80)

    # 1. Trend Factor — MA 군집(상한 2.0) + 추세 지속 0.5 + MACD 0선 0.5 + MACD GC/확산 0.5 + SAR 0.5
    score_table.add_row("Trend Factor", "현재가 > 20일선", f"+{0.5 * r_trend:.1f}", "단기 지지")
    score_table.add_row("(추세 4.0)", "EMA 5일선 > 20일선", f"+{0.5 * r_trend:.1f}", "5일선이 20일선 상회 (단기 추세 전환)")
    score_table.add_row("", "20/60/120선 정배열", f"+{1.0 * r_trend:.1f}", "중장기 이평선 정배열")
    score_table.add_row("", "주가 > 60선 돌파 or 단기급등", f"+{0.5 * r_trend:.1f}", "역배열 초기 돌파 또는 주가>5>20>60 정배열 급등")
    score_table.add_row("", "현재가 > 120일선", f"+{0.5 * r_trend:.1f}", "장기 지지")
    score_table.add_row("", "[dim]※ 위 이평선 신호 합산 상한[/dim]", f"[dim]최대 +{2.0 * r_trend:.1f}[/dim]", "[dim]상관 높은 MA 군집의 과대 가점 방지[/dim]")
    score_table.add_row("", f"최근 {persist_lb}일 중 {persist_min}%↑ 60일선 위", f"+{0.5 * r_trend:.1f}", "추세 지속 이력 (오래 유지된 추세 우대)")
    score_table.add_row("", "MACD > 0선", f"+{0.5 * r_trend:.1f}", "추세 확립 (MA와 독립적인 확인 신호)")
    score_table.add_row("", "MACD 신규 골든크로스 or 0선 위 확산", f"+{0.5 * r_trend:.1f}", "히스토그램 신규 양전환 또는 상승 확산")
    score_table.add_row("", "주가 > SAR", f"+{0.5 * r_trend:.1f}", "파라볼릭 매수")
    score_table.add_section()

    # 2. Momentum Factor — RSI 1.0 + CCI 0.5 + DMI 0.5 + 가격 모멘텀 0.5
    score_table.add_row("Momentum Factor", f"RSI ≥ {rsi_mid}", f"+{0.5 * r_mom:.1f}", "강세 구간 (과열이어도 기본 점수 유지)")
    score_table.add_row("(모멘텀 2.5)", f"{rsi_strong} ≤ RSI < {rsi_overheat}", f"+{0.5 * r_mom:.1f}", f"모멘텀 확장 ({rsi_overheat} 이상 과열은 동결)")
    score_table.add_row("", f"{rsi_rebound} ≤ RSI < {rsi_mid} & 주가>60일선", f"+{0.5 * r_mom:.1f}", "추세 내 눌림 회복 (강세 구간과 중복 불가)")
    score_table.add_row("", "CCI > 0 or 과매도(-100) 탈출(추세 내)", f"+{0.5 * r_mom:.1f}", "상승 추세 또는 추세 내 과매도 탈출 (택1)")
    score_table.add_row("", "+DI > -DI", f"+{0.5 * r_mom:.1f}", "매수세가 매도세 우위 (방향성)")
    score_table.add_row("", f"6개월 수익률 > 0 & 52주 위치 ≥ {w52_near}%", f"+{0.5 * r_mom:.1f}", "가격 모멘텀 — 신고가 근접 주도주 (1·3개월 음수면 보류)")
    score_table.add_section()

    # 3. Strength & Volume
    score_table.add_row("Strength/Volume", f"ADX ≥ {score_adx}", f"+{0.5 * r_str:.1f}", "추세 형성 확인")
    score_table.add_row("(강도/수급 1.5)", "거래량 폭증 or 5일>20일 추세상승", f"+{0.5 * r_str:.1f}", "단기 거래량 모멘텀 개선 (안정적 수급)")
    score_table.add_row("", "OBV 상승 or 스마트머니", f"+{0.5 * r_str:.1f}", "보조 지표 및 메이저 수급 턴어라운드")
    score_table.add_section()

    # 4. Synergy Bonus
    score_table.add_row("Synergy Bonus", f"주가>60선 + MACD확산 + ADX≥{score_adx}", f"+{1.0 * r_syn:.1f}", "추세 시작 시너지")
    score_table.add_row("(가산점 2.0)", f"MACD확산 + RSI≥{rsi_strong} + OBV", f"+{1.0 * r_syn:.1f}", "모멘텀 폭발 (Thrust)")
    score_table.add_row("", "[dim]※ 공통 게이트: MACD>0 or 주가>120일선[/dim]", "[dim]필수[/dim]", "[dim]역배열 데드캣 바운스의 시너지 독식 차단[/dim]")
    score_table.add_section()

    # 5. 추세 악화 감점 (Deterioration Penalty)
    score_table.add_row("악화 감점", "MACD 데드크로스", f"-{0.5 * r_trend:.1f}", "하락 반전 신호를 점수에 적시 반영")
    score_table.add_row("", "MACD 0선 이하 하락 가속", f"-{0.5 * r_mom:.1f}", "히스토그램 음수 확대")
    score_table.add_row("", "-DI 우위", f"-{0.5 * r_str:.1f}", "매도세 강화")

    # [병합] 점수대별 의미 — 실제 임계값(설정 연동)으로 표시
    _buy = config.ANALYSIS_THRESHOLDS.get("BUY_SCORE", 7.0)
    _rise = config.ANALYSIS_THRESHOLDS.get("RISE_SCORE", 6.0)
    _super = config.ANALYSIS_THRESHOLDS.get("SUPER_MOMENTUM_SCORE", 8.0)
    _sell = config.SELL_STRATEGY.get("SELL_SCORE", 4.0)
    score_table.add_section()
    score_table.add_row("점수대별 의미", f"{_super}점 이상 & 52주 고점 90%↑", "[magenta]강매수[/]", "슈퍼 모멘텀 — 주도주 랠리 추종 (매수 RSI 상향 허용)")
    score_table.add_row("", f"{_buy}점 이상", "[red]매수[/]", "강력 매수 구간 (분할 진입)")
    score_table.add_row("", f"{_buy}점 이상 & RSI 과열", "[orange3]대기[/]", "매수 직전 — 점수는 충족, RSI 식으면 매수 (눌림목 매수 대기)")
    score_table.add_row("", f"{_rise} ~ {_buy}점", "[orange3]상승[/]", "상승 초입/지속 (점수 축적 대기 또는 소량)")
    score_table.add_row("", f"{_sell}점 미만 & 60일선 이탈", "[blue]매도[/]", "추세이탈 청산 (두 조건 동시 충족 시)")
    score_table.add_row("", "그 외 구간", "[white]관망[/]", "방향성 탐색 (거래 비권장)")
    
    # [추가] 현재 설정 및 적응형 임계값, 시장 상태 정보
    score_table.add_section()
    score_table.add_row("스코어링 가중치", "현재 설정", f"{weights['TREND']} / {weights['MOMENTUM']} / {weights['STRENGTH']} / {weights['SYNERGY']}", "추세/모멘텀/강도/시너지")
    score_table.add_row("", "[dim](기본값)[/dim]", "[dim]4.0 / 2.5 / 1.5 / 2.0[/dim]", "[dim]초기 시스템 권장값[/dim]")
    score_table.add_row("", "[dim](추세 중시)[/dim]", "[dim]5.0 / 2.0 / 1.0 / 2.0[/dim]", "[dim]확실한 상승 추세를 타는 종목 집중[/dim]")
    score_table.add_row("", "[dim](모멘텀 중시)[/dim]", "[dim]3.0 / 3.5 / 1.5 / 2.0[/dim]", "[dim]빠른 단기 반등 및 시세 탄력 집중[/dim]")
    score_table.add_row("", "[dim](수급 중시)[/dim]", "[dim]3.0 / 2.0 / 3.0 / 2.0[/dim]", "[dim]거래량 및 외인/기관 매수세 포착[/dim]")
    score_table.add_row("", "[dim](균등 배분)[/dim]", "[dim]2.5 / 2.5 / 2.5 / 2.5[/dim]", "[dim]모든 팩터를 균형있게 고려[/dim]")
    
    score_table.add_section()
    adaptive_status = "[green]ON[/green]" if regime.get('USE_ADAPTIVE_THRESHOLD') else "[red]OFF[/red]"
    score_table.add_row(f"적응형 임계값 ({adaptive_status})",
                        f"[dim]EMA {ema_f}/{ema_s} 교차 후 {confirm_p:g}% 진행해야 '확정 추세'[/dim]", "", "")
    score_table.add_row("", f"강세장: EMA{ema_f} > EMA{ema_s} & 교차 후 {confirm_p:+g}% 달성", "[red]완화[/]", f"매수 기준 {regime['BULL_SCORE_ADJ']:+.1f}점 적용")
    score_table.add_row("", f"상승 미확정: EMA{ema_f} > EMA{ema_s} & {confirm_p:g}% 미달", "[orange3]유지[/]", f"매수 기준 {regime.get('PENDING_UP_SCORE_ADJ', 0.0):+.1f}점 적용")
    score_table.add_row("", f"하락 미확정: EMA{ema_f} < EMA{ema_s} & {confirm_p:g}% 미달", "[sky_blue3]강화[/]", f"매수 기준 {regime.get('PENDING_DOWN_SCORE_ADJ', 0.5):+.1f}점 적용 [dim](판별력상 최다 위험구간)[/dim]")
    score_table.add_row("", f"약세장: EMA{ema_f} < EMA{ema_s} & 교차 후 {-confirm_p:+g}% 달성", "[blue]강화[/]", f"매수 기준 {regime['BEAR_SCORE_ADJ']:+.1f}점 적용")
    
    if market_status_info:
        k_adj_str = f"보정: {market_status_info['kospi_adj']:+.1f}점"
        q_adj_str = f"보정: {market_status_info['kosdaq_adj']:+.1f}점"
        score_table.add_row("현재 시장 상태", f"KOSPI: {market_status_info['kospi_str']}", k_adj_str, "실시간 국면 분석")
        score_table.add_row("", f"KOSDAQ: {market_status_info['kosdaq_str']}", q_adj_str, "KIS API 데이터 기반 판단")
    else:
        score_table.add_row("현재 시장 상태", "분석 실패", "-", "-")
    
    # [추가] 시장 필터링 섹션
    score_table.add_section()
    filter_status = "[green]ON[/green]" if getattr(config, 'USE_MARKET_FILTER', True) else "[red]OFF[/red]"
    ma_period = getattr(config, 'MARKET_FILTER_MA', 80)
    _band = getattr(config, 'MARKET_FILTER_BAND', 1.0)
    _band_cond = f" -{_band:g}% 이탈 (회복은 +{_band:g}%)" if _band else ""
    score_table.add_row(f"시장 필터링 ({filter_status})", f"KOSPI/KOSDAQ 지수 < SMA {ma_period}일 이평선{_band_cond}", "[blue]보류[/]", "하락장 감지 시 신규 매수 중단")
    
    if filter_info is None and getattr(config, 'USE_MARKET_FILTER', True):
        score_table.add_row("현재 시장 필터링 상태", "확인 불가", "-", "-")
    elif filter_info:
        k_stat = "[green]허용[/]" if filter_info.get("KOSPI", True) else "[red]보류[/]"
        q_stat = "[green]허용[/]" if filter_info.get("KOSDAQ", True) else "[red]보류[/]"
        score_table.add_row("현재 시장 필터링 상태", f"KOSPI: {k_stat} / KOSDAQ: {q_stat}", "-", "실시간 필터링 적용 여부")

    # [추가] 상대강도(RS) 필터 섹션 (룩백: RS_FILTER_LOOKBACK>0 우선, 0이면 가격 모멘텀 룩백 연동 — trader RS 게이트와 동일 규칙)
    #  [추세추종 보호] 기본 OFF이므로 꺼져 있으면 섹션 자체를 그리지 않는다 — 동작하지 않는 필터를
    #   도움말에 남겨두면 매수 제외 사유를 오해하게 된다. dynamic_config.json에서 다시 켜면
    #   (설정 화면 숨김 해제와 무관하게) 이 섹션도 자동으로 다시 나타난다.
    rs_on = getattr(config, 'USE_RS_FILTER', False)
    if rs_on:
        score_table.add_section()
        rs_lb_cfg = getattr(config, 'RS_FILTER_LOOKBACK', 0)
        rs_lookback = rs_lb_cfg if rs_lb_cfg > 0 else config.INDICATOR_PARAMS.get('MOMENTUM_LOOKBACK', 126)
        rs_lb_src = "전용 설정" if rs_lb_cfg > 0 else "가격 모멘텀 룩백 연동"
        score_table.add_row("상대강도(RS) 필터 ([green]ON[/green])", f"종목 {rs_lookback}일 수익률 ≤ 소속 지수(KOSPI/KOSDAQ) 수익률", "[blue]제외[/]", f"지수 대비 약세 종목 신규 매수 제외 (국내 전용, 기간: {rs_lb_src})")

        # 현재 기준값 = 소속 지수의 룩백 기간 수익률 (이 값 이하의 종목이 신규 매수에서 제외됨)
        rs_k = analysis.get_index_momentum("KOSPI", lookback=rs_lookback)
        rs_q = analysis.get_index_momentum("KOSDAQ", lookback=rs_lookback)
        def _rs_colored(v):
            if v is None: return "확인 불가"
            color = "red" if v > 0 else ("blue" if v < 0 else "white")
            return f"[{color}]{v:+.1f}%[/]"
        rs_k_str = _rs_colored(rs_k)
        rs_q_str = _rs_colored(rs_q)
        score_table.add_row("현재 RS 필터 기준값", f"KOSPI: {rs_k_str} / KOSDAQ: {rs_q_str}", "-", f"소속 지수의 {rs_lookback}일 수익률 (기준값 이하 종목 제외)")

    # [추가] 매매 필터링 섹션
    score_table.add_section()
    score_table.add_row("매매 필터링 - 위험", "60일선 & 120일선 동시 이탈 or RSI ≤ 20", "[blue]매도[/]", "매수 금지 / 즉시 매도 (단, 매수 점수 획득 시 예외 통과)")
    score_table.add_row("매매 필터링 - 주의", "MACD 데드크로스, 60/120선 이탈, SAR 매도", "[yellow]주의[/]", "신규 진입 자제 (단, 매수 점수 획득 시 예외 통과)")

    # [추가] 매수 타이밍 섹션
    score_table.add_section()
    buy_score = config.ANALYSIS_THRESHOLDS["BUY_SCORE"]
    rise_score = config.ANALYSIS_THRESHOLDS["RISE_SCORE"]
    buy_rsi_max = config.ANALYSIS_THRESHOLDS["BUY_RSI_MAX"]
    buy_vol = config.ANALYSIS_THRESHOLDS["BUY_VOL_STRENGTH"]

    score_table.add_row("매수 - 진입", f"종합 점수 ≥ {buy_score}점 & RSI < {buy_rsi_max} & 체결강도 > {buy_vol}%", "[red]매수[/]", "강력 매수 구간 (분할 진입)")
    
    use_mr = config.ANALYSIS_THRESHOLDS.get("USE_MEAN_REVERSION", False)
    mr_status = "[green]ON[/green]" if use_mr else "[red]OFF[/red]"
    mr_disp = config.ANALYSIS_THRESHOLDS.get("MR_DISPARITY_MAX", 90.0)
    mr_rsi = config.ANALYSIS_THRESHOLDS.get("MR_RSI_MAX", 40.0)
    mr_vol = config.ANALYSIS_THRESHOLDS.get("MR_VOL_STRENGTH", 120.0)
    if _help_show("USE_MEAN_REVERSION", use_mr):
        score_table.add_row(f"매수 - 역추세 ({mr_status})", f"이격도 ≤ {mr_disp}% & RSI ≤ {mr_rsi} 반등 & 체결 > {mr_vol}%", "[magenta]역매수[/]", "낙폭과대 기술적 반등 노리기")
    
    use_super = config.ANALYSIS_THRESHOLDS.get("SUPER_MOMENTUM_USE", True)
    super_status = "[green]ON[/green]" if use_super else "[red]OFF[/red]"
    super_score = config.ANALYSIS_THRESHOLDS.get("SUPER_MOMENTUM_SCORE", 8.0)
    super_w52 = config.ANALYSIS_THRESHOLDS.get("SUPER_MOMENTUM_W52_POS", 90.0)
    super_buy_rsi = config.ANALYSIS_THRESHOLDS.get("SUPER_BUY_RSI_MAX", 80.0)
    super_sell_rsi = config.SELL_STRATEGY.get("SUPER_TAKE_PROFIT_RSI", 90.0)
    score_table.add_row(f"매수 - 슈퍼 모멘텀 ({super_status})", f"종합 점수 ≥ {super_score}점 & 52주 고점 {super_w52}% 이상 근접", "[magenta]강매수[/]", f"주도주 랠리 추종. 매수 RSI {super_buy_rsi}, 과열 매도 RSI {super_sell_rsi} 까지 허용")

    score_table.add_row("대기 - 눌림", f"종합 점수 ≥ {buy_score}점 & RSI ≥ {buy_rsi_max} (과열)", "[orange3]대기[/]", "매수 직전 — RSI 식으면 매수 (눌림목 매수 대기)")
    score_table.add_row("관망 - 상승", f"{rise_score}점 ≤ 종합 점수 < {buy_score}점", "[orange3]상승[/]", "상승 초입/지속 (점수 축적 대기/소량)")
    score_table.add_row("관망 - 중립", f"종합 점수 < {rise_score}점", "[white]관망[/]", "방향성 탐색 (거래 비권장)")
    
    # [추가] 매도 규칙 섹션
    score_table.add_section()
    sell_score = config.SELL_STRATEGY["SELL_SCORE"]
    stop_loss = config.SELL_STRATEGY["STOP_LOSS_RATE"]
    take_profit = config.SELL_STRATEGY["TAKE_PROFIT_RATE"]
    take_profit_rsi = config.SELL_STRATEGY["TAKE_PROFIT_RSI"]
    from modules.auto_trade.engine import ts_activation_label
    ts_activation = ts_activation_label()
    ts_callback = config.SELL_STRATEGY.get("TRAILING_STOP_CALLBACK_RATE", 5.0)
    use_atr = config.SELL_STRATEGY.get("USE_ATR_STOP", True)
    atr_mult = config.SELL_STRATEGY.get("ATR_STOP_MULTIPLIER", 2.0)
    half_tp_use = config.SELL_STRATEGY.get("HALF_TAKE_PROFIT_USE", False)
    time_stop_use = config.SELL_STRATEGY.get("TIME_STOP_USE", True)
    time_stop_days = config.SELL_STRATEGY["TIME_STOP_DAYS"]
    time_stop_min_profit = config.SELL_STRATEGY.get("TIME_STOP_MIN_PROFIT_RATE", 0.0)
    bep_activation = config.SELL_STRATEGY.get("BREAK_EVEN_PROFIT_RATE", 5.0)
    bep_stop = config.SELL_STRATEGY.get("BREAK_EVEN_STOP_RATE", 0.5)

    # [수정] 0=미사용 규칙은 (OFF)를 명시 — 값 0이 활성 조건("+0.0% 도달")처럼 보이던 표시 모순 해소
    #  (추세추종 기조: 고정 익절·RSI 과열 익절은 기본 OFF, 주청산은 트레일링 스탑)
    tp_status = "[green]ON[/green]" if take_profit > 0 else "[red]OFF[/red]"
    tp_cond = f"수익률 +{take_profit}% 도달" if take_profit > 0 else "미사용 (0 = OFF, 추세추종: TS 주청산)"
    if _help_show("TAKE_PROFIT_RATE", take_profit > 0):
        score_table.add_row(f"매도 - 익절 ({tp_status})", tp_cond, "[red]익절[/]", "목표 수익 달성 (최우선)")

    half_tp_status = "[green]ON[/green]" if (half_tp_use and take_profit > 0) else "[red]OFF[/red]"
    half_cond = f"수익률 +{take_profit/2:.1f}% 도달" if take_profit > 0 else "미사용 (익절 OFF 시 비활성)"
    if _help_show("HALF_TAKE_PROFIT_USE", half_tp_use and take_profit > 0):
        score_table.add_row(f"매도 - 반익절 ({half_tp_status})", half_cond, "[red]반익절[/]", "절반(50%) 선매도로 수익 확보")
    
    fixed_sl_status = "[red]OFF[/red]" if use_atr else "[green]ON[/green]"
    score_table.add_row(f"매도 - 고정손절 ({fixed_sl_status})", f"손실률 {stop_loss}% 도달", "[blue]손절[/]", "손실 제한 (고정 손절)")
    
    atr_status = "[green]ON[/green]" if use_atr else "[red]OFF[/red]"
    score_table.add_row(f"매도 - ATR손절 ({atr_status})", f"매수가 - (ATR x {atr_mult})", "[blue]손절[/]", "변동성 기반 동적 손절")
    
    # [수정] ATR 손절 사용 시 BEP 발동 기준은 BREAK_EVEN_PROFIT_RATE가 아니라 손절폭(1R)이다
    #  (trader/backtest가 그렇게 주입). 설정값을 그대로 쓰면 실제 동작과 달라 오해를 부른다.
    _bep_cond = ("손절폭(1R) 도달 후 하락 시" if use_atr else f"수익 +{bep_activation}% 달성 후 하락 시")
    use_bep = config.SELL_STRATEGY.get("USE_BREAK_EVEN_STOP", False)
    bep_status = "[green]ON[/green]" if use_bep else "[red]OFF[/red]"
    score_table.add_row(f"매도 - 본전청산 ({bep_status})", _bep_cond, "[blue]본전청산[/]", f"손실 방지 (손절선을 {bep_stop:+g}%로 끌어올림)")
    
    time_stop_status = "[green]ON[/green]" if time_stop_use else "[red]OFF[/red]"
    score_table.add_row(f"매도 - 시간청산 ({time_stop_status})", f"보유 {time_stop_days}일 경과 & 수익 < {time_stop_min_profit}% (최근 5일 고점 갱신 부재)", "[blue]시간청산[/]", "장기 횡보 종목 기회비용 보전")
    
    use_def_half = config.SELL_STRATEGY.get("DEFENSIVE_HALF_SELL_USE", False)
    def_half_status = "[green]ON[/green]" if use_def_half else "[red]OFF[/red]"
    if _help_show("DEFENSIVE_HALF_SELL_USE", use_def_half):
        score_table.add_row(f"매도 - 방어적 반매도 ({def_half_status})", f"주가 < SAR & 주가 < 5일선 동시 이탈 시", "[blue]반매도[/]", "하락 반전 신호 감지 시 50% 덜어내기 (리스크 방어)")

    score_table.add_row("매도 - 트레일링", f"수익 {ts_activation} 도달 후 고점 대비 하락 시", "[blue]매도[/]", "수익 보전 (ATR 사용 시 동적 변동폭 적용)")
    overheat_status = "[green]ON[/green]" if take_profit_rsi > 0 else "[red]OFF[/red]"
    overheat_cond = f"RSI > {take_profit_rsi}" if take_profit_rsi > 0 else "미사용 (0 = OFF, 강추세는 과매수 지속 허용)"
    if _help_show("TAKE_PROFIT_RSI", take_profit_rsi > 0):
        score_table.add_row(f"매도 - 과열 ({overheat_status})", overheat_cond, "[red]익절[/]", "RSI 과열 시 이익 실현")
    score_table.add_row("매도 - 추세이탈", f"종합 점수 < {sell_score}점 or 위험 상태", "[blue]매도[/]", "추세 붕괴 시 청산")

    # [추가] 주문 집행 상세 섹션
    score_table.add_section()
    slippage_rate = getattr(config, 'SLIPPAGE_RATE', 0.002)
    if slippage_rate > 0:
        slippage_val = slippage_rate * 100
        score_table.add_row("주문 집행", "매수 주문 시", f"[red]+{slippage_val:.2f}%[/]", "체결 확률 확보 (현재가 + 슬리피지)")
        score_table.add_row("", "매도 주문 시", f"[blue]-{slippage_val:.2f}%[/]", "즉시 체결 유도 (현재가 - 슬리피지)")
    else:
        score_table.add_row("주문 집행", "매수 주문 시", "[dim]미사용[/]", "현재가로 주문 (슬리피지 없음)")
        score_table.add_row("", "매도 주문 시", "[dim]미사용[/]", "현재가로 주문 (슬리피지 없음)")
    
    use_vol = getattr(config, 'USE_VOLATILITY_TARGETING', True)
    use_risk = getattr(config, 'SYSTEM_RISK_PER_TRADE', 4.0) > 0
    if use_vol or use_risk:
        score_table.add_row("", "자산 배분 (마지막)", "[yellow]비중 조절[/]", "리스크/변동성 한도 내에서 집행")
    else:
        score_table.add_row("", "자산 배분 (마지막)", "[green]전액[/]", "마지막 종목은 잔여 예수금 100% 사용")

    config.console.print(score_table)

def flush_input():
    """입력 버퍼를 비워 의도치 않은 입력을 방지합니다."""
    try:
        import msvcrt
        while msvcrt.kbhit():
            msvcrt.getch()
    except ImportError:
        import sys, termios
        try:
            termios.tcflush(sys.stdin, termios.TCIOFLUSH)
        except Exception:
            pass

def _install_journal_sigterm_handler():
    """[추가] kill / systemd stop 등 SIGTERM 종료에도 매매일지 표시등을 내린다.

    메뉴에서 종료하면 finally 블록이 journal_sync.stop() 을 부르지만, 외부에서
    프로세스를 내리면 그 경로를 타지 않아 웹 대시보드가 계속 '정상 가동중'으로
    남는다. 통지만 끼워 넣고 기존 종료 동작(기본 처리)은 그대로 이어가도록
    핸들러를 원복한 뒤 시그널을 다시 올린다.

    SIGINT 는 건드리지 않는다 — 메인 메뉴가 KeyboardInterrupt 로 종료 확인을
    받는 기존 흐름을 깨뜨리기 때문이다.
    """
    def _on_sigterm(signum, frame):
        try:
            from modules import journal_sync
            journal_sync.notify_shutdown('stopped', message=f'signal {signum}')
        except Exception:
            pass
        # [프로세스 감시] 외부에서 내린 종료는 사고사가 아니다 — 표식을 남겨
        #  감시자가 '죽었다' 알림을 보내지 않게 한다. (SIGKILL·OOM 은 여기 못 오므로
        #  그 경우에는 마지막 도장이 그대로 남아 정상적으로 사망 알림이 나간다.)
        try:
            from modules import heartbeat
            heartbeat.stopped(reason=f"signal {signum}")
        except Exception:
            pass
        signal.signal(signum, signal.SIG_DFL)
        os.kill(os.getpid(), signum)

    for _name in ('SIGTERM', 'SIGHUP'):
        sig = getattr(signal, _name, None)
        if sig is None:
            continue  # Windows 에는 SIGHUP 이 없다
        try:
            signal.signal(sig, _on_sigterm)
        except (ValueError, OSError):
            pass  # 메인 스레드가 아니거나 지원되지 않는 환경


_MODE_NAMES = {'1': "모의투자", '2': "한투증권", '3': "토스증권", '4': "가상투자"}


def _detect_appkey_duplicates():
    """같은 앱키를 쓰는 다른 프로세스가 있는가. (중복이 걸린 키 이름 목록)

    판정 결과는 instance_lock 전역에도 남아 api.py 의 TPS 경고가 매번 인용한다. 그래서
    차단 여부와 무관하게 **항상** 돌린다.
    """
    from modules import instance_lock

    if config.session.is_toss:
        return []       # 토스는 KIS 앱키를 쓰지 않는다 — 모드 잠금이 유일한 방어선이다
    # 수동 키와 자동매매 키를 모두 본다. 두 키가 다르면 유량 예산도 키마다 따로 잡히므로
    #  (api.ThrottledSession 의 앱키별 버킷), 자동매매 키의 중복은 수동 키를 아무리 확인해도
    #  드러나지 않는다 — 그런데 시스템 트레이딩 트래픽은 전부 그쪽 키로 나간다.
    keys = [(config.session.app_key, "수동")]
    auto = getattr(config.session, 'auto_app_key', '')
    if auto and auto != config.session.app_key:
        keys.append((auto, "자동매매"))
    return [lbl for k, lbl in keys if not instance_lock.guard_appkey(k, lbl)]


def _describe_process(pid):
    """선점 프로세스의 실체를 한 줄로 돌려준다(시작 시각 + 명령줄).

    잠금 파일이 알려 주는 것은 pid 뿐이다. '죽여도 되는가'를 판단하려면 그 pid 가 정말
    이 프로그램인지, 언제부터 떠 있는지를 봐야 한다 — 운영자가 명령을 한 번 더 치지
    않도록 여기서 대신 읽어 준다. 읽지 못하면 빈 문자열이다(안내는 그대로 나간다).
    """
    if not pid:
        return ""
    try:
        import subprocess
        out = subprocess.run(["ps", "-p", str(pid), "-o", "lstart=,command="],
                             capture_output=True, text=True, timeout=3)
        line = " ".join(out.stdout.split())
        return line[:200]
    except Exception:
        return ""


def _enforce_single_instance(mode, allow_duplicate=False):
    """[추가 2026-08-25] 두 번째 인스턴스면 안내하고 종료한다.

    종전에는 같은 모드를 두 번 띄워도 둘 다 정상 기동했다. 계좌 잠금(InstanceLock)은
    자동매매 엔진을 켤 때만 걸리고, 앱키 중복은 경고만 하고 지나갔다. 그 사이 두
    인스턴스가 텔레그램 폴링(409 Conflict)·KIS 유량·DB 파일을 서로 빼앗는다.

    판정은 두 갈래이고 **둘 중 하나만 걸려도 막는다.**
      · 모드 잠금 — 이 기능의 본줄기. 토스처럼 앱키가 없는 모드까지 덮는다.
      · 앱키 중복 — 모드 잠금이 못 보는 구멍을 메운다. 선점 프로세스가 모드 잠금이 없던
        버전으로 떠 있으면 자리가 비어 보이는데(무중단 교체 때마다 열리는 구멍이다),
        그 프로세스도 앱키 잠금만은 쥐고 있다. 앱키는 모드마다 갈라져 있으므로
        (SIM_/REAL_·AUTO_/VIRT_) 앱키가 겹친다는 것은 곧 같은 모드가 겹쳤다는 뜻이다.

    잠금은 세션 초기화 직후, 토큰 발급도 DB 오픈도 하기 전에 잡는다 — 실패해도 정리할
    것이 없는 유일한 지점이다.
    """
    from modules import instance_lock

    holder, reason = "", ""
    if not instance_lock.guard_mode(mode, allow_duplicate=allow_duplicate):
        holder = instance_lock.MODE_HOLDER
        reason = f"같은 모드({_MODE_NAMES.get(str(mode), f'mode {mode}')})"

    dup_labels = _detect_appkey_duplicates()
    if dup_labels and not reason:
        dup_holder = instance_lock.APPKEY_HOLDER
        other_mode = instance_lock.holder_mode(dup_holder)
        if other_mode and other_mode != str(mode):
            # 모드가 다르면 막지 않는다. 앱키만 겹친 것은 환경변수 설정 문제이고,
            #  '실전 운용 + 관찰 동시 기동'처럼 정상인 조합을 여기서 끊어서는 안 된다.
            config.console.print(
                f"\n[bold yellow]⚠️ 다른 모드({_MODE_NAMES.get(other_mode, other_mode)})가 "
                f"같은 앱키를 쓰고 있습니다 ({dup_holder}).[/bold yellow]")
            config.console.print(
                "[dim]  KIS 유량(20 TPS)·웹소켓(1개)·토큰 발급 제약은 앱키 단위입니다. "
                "모드별로 키를 나누세요(가상투자는 VIRT_APP_KEY).[/dim]")
        else:
            # 모드가 같거나(=차단 대상), 모드를 남기지 않던 버전이 쥔 잠금이다. 후자는
            #  앱키가 같은 이상 같은 모드일 가능성이 압도적이므로 막는 쪽을 고른다.
            holder = dup_holder
            reason = f"같은 {'·'.join(dup_labels)} 앱키"

    if not reason:
        return

    if allow_duplicate:
        config.console.print(
            f"\n[yellow]⚠️ {reason}로 다른 프로세스가 실행 중입니다 ({holder or 'unknown'}).[/yellow]")
        config.console.print(
            "[dim]  --allow-duplicate 로 중복 실행을 허용해 계속합니다. 조회 전용으로만 쓰세요 "
            "— 자동매매를 켜면 계좌 잠금에서 거부됩니다.[/dim]")
        return

    holder = holder or "unknown"
    pid = ""
    for token in holder.split():
        if token.startswith("pid="):
            pid = token[4:]

    config.console.print(f"\n[bold red]❌ 이미 {reason}로 실행 중인 프로세스가 있습니다.[/bold red]")
    config.console.print(f"[dim]   선점 프로세스: {holder}[/dim]")
    if pid:
        detail = _describe_process(pid)
        if detail:
            config.console.print(f"[dim]   실행 중인 명령: {detail}[/dim]")
        else:
            config.console.print(
                "[dim]   (해당 pid 의 프로세스 정보를 읽지 못했습니다 — 다른 사용자 소유일 수 있습니다)[/dim]")
        config.console.print("\n[bold]선점 프로세스를 종료하려면:[/bold]")
        config.console.print(f"[dim]  · 정상 종료(권장):  kill {pid}[/dim]")
        config.console.print(
            "[dim]    — 종료 신호를 받으면 매매일지·하트비트를 정리하고 내려갑니다.[/dim]")
        config.console.print(f"[dim]  · 응답이 없으면:    kill -9 {pid}[/dim]")
        config.console.print(f"[dim]  · 다시 확인:        ps -p {pid} -o pid,etime,command[/dim]")
    config.console.print(
        "\n[dim]조회 전용으로 하나 더 띄우려면:  ./run.sh --mode <모드> --allow-duplicate[/dim]")
    sys.exit(1)


def main():
    # [수정] 커맨드 라인 인자 파싱 설정 개선 (상세 도움말 추가)
    parser = argparse.ArgumentParser(
        prog='run.sh / run.bat',
        description='[MyStock HTS] 한국투자증권 API 기반 주식 자동매매 시스템',
        formatter_class=argparse.RawTextHelpFormatter,
        epilog="""
[사용 예시]
  1. 기본 실행 (대화형 메뉴 모드):
     ./run.sh        (macOS/Linux)
     run.bat         (Windows)

  2. 모의투자 모드로 자동매매 바로 시작:
     ./run.sh --mode 1 --auto

  3. 한투증권 모드로 바로 시작 (텔레그램 봇 수신 비활성화):
     ./run.sh --mode 2 --no-bot  (또는 run.bat ...)

  4. 토스증권 모드로 실행:
     ./run.sh --mode 3

  5. 가상투자(페이퍼 트레이딩) 모드로 자동매매 바로 시작:
     ./run.sh --mode 4 --auto
     (KIS 실전 시세 + 가상 계좌. 실주문이 나가지 않으므로 장기 관찰에 사용)

  6. 조회 전용 인스턴스를 하나 더 띄우기:
     ./run.sh --mode 2 --allow-duplicate --no-bot
     (같은 모드는 기본적으로 하나만 뜹니다. 텔레그램·KIS 유량·DB 를 서로 빼앗기 때문입니다)
"""
    )
    parser.add_argument('--mode', choices=['1', '2', '3', '4'], help='투자 모드 선택 (1: 모의투자, 2: 한투증권, 3: 토스증권, 4: 가상투자)\n지정하지 않으면 실행 시 모드 선택 화면이 출력됩니다.')
    parser.add_argument('--auto', action='store_true', help='프로그램 시작 시 시스템 트레이딩 자동 실행 및 로그 뷰어 활성화')
    parser.add_argument('--no-bot', action='store_true', help='텔레그램 봇 명령어 수신(폴링) 비활성화 (알림 전송 기능은 유지)')
    parser.add_argument('--allow-duplicate', action='store_true',
                        help='같은 모드 중복 실행 차단을 해제 (조회 전용 인스턴스를 하나 더 띄울 때만 사용)')
    args = parser.parse_args()

    # [추가] 로깅 설정 초기화
    config.setup_logging()

    # [추가] 프로그램 구동 시작 로그 기록 (mystock.log 생성 보장)
    # [수정] 루트 로거로 남기면 FILE_DEBUG_LEVEL 이 기본값(WARNING)일 때 이 한 줄이 통째로
    #  사라진다. 로그 파일은 하루 단위라 한 파일에 여러 실행이 섞이는데, 실행 구분선이
    #  없으면 사후 추적이 어렵다. 전용 로거로 레벨과 무관하게 항상 남긴다.
    config.log_system_start()

    # [추가] 메인 스레드 ID 등록 (토큰 발급 권한 제어용)
    context.MAIN_THREAD_ID = threading.get_ident()

    # [수정] 초기화 로직 통합 및 사전 점검 추가
    # 1. 환경 설정 로드 (모드 선택)
    config.session.initialize(mode=args.mode)

    # 1-0. [추가 2026-08-25] 중복 인스턴스 차단(모드 잠금 + 앱키 중복). 사전 점검보다
    #  먼저 본다 — 토큰도 DB도 아직 건드리지 않은 지점이라 종료해도 정리할 것이 없다.
    #  [pytest 제외] 테스트는 _enforce_single_instance 를 직접 부른다. main() 경로에서까지
    #  실제 잠금을 잡으면, 운영 인스턴스가 떠 있는 기기에서 테스트를 돌리는 순간 선점자에
    #  걸려 sys.exit(1) 로 죽는다(코드 결함이 아닌데 테스트가 빨개진다).
    if "pytest" not in sys.modules and "PYTEST_CURRENT_TEST" not in os.environ:
        _enforce_single_instance(getattr(config.session, 'mode', None) or args.mode,
                                 allow_duplicate=args.allow_duplicate)

    # 2. 사전 점검
    preflight_success = False
    for attempt in range(3):
        if preflight_check():
            preflight_success = True
            break
        
        if attempt < 2:
            config.console.print(f"\n[yellow]사전 점검에 실패했습니다. 5초 후 재시도합니다... ({attempt+1}/3)[/yellow]")
            time.sleep(5)
        else:
            config.console.print(f"\n[yellow]사전 점검에 실패했습니다. ({attempt+1}/3)[/yellow]")

    if not preflight_success:
        config.console.print("\n[bold red]시스템 사전 점검에 최종 실패하여 프로그램을 시작할 수 없습니다.[/bold red]")
        config.console.print("[dim]API Key 설정 및 네트워크 연결을 확인해주세요.[/dim]")
        sys.exit(1)
    config.console.print("\n[green]모든 점검 통과. 시스템을 시작합니다.[/green]\n")

    with Progress(
        SpinnerColumn(),
        TextColumn("[progress.description]{task.description}"),
        BarColumn(),
        console=config.console,
        transient=True
    ) as progress:
        progress.add_task("[cyan]시스템 리소스 로딩 및 백그라운드 서비스 시작 중...[/cyan]", total=None)
        # 3. DB 큐 프록시 설치
        if "pytest" not in sys.modules and "PYTEST_CURRENT_TEST" not in os.environ:
            db_queue.install_proxy(db_manager)

        # 4. 텔레그램 봇 비활성화 옵션 처리
        if args.no_bot:
            config.settings.ENABLE_TELEGRAM = False
            config.console.print("텔레그램 봇 명령어 수신 기능을 비활성화합니다.")

        # 5. 초기 프리셋 상태 동기화 (이모티콘 색상 결정)
        try:
            from modules import settings
            settings.check_and_update_active_preset()
        except Exception as e:
            logging.debug(f"초기 프리셋 업데이트 실패: {e}")

        # (종목 데이터 로드 로직은 사전 점검 단계로 이동됨)
        
        # 6. 백그라운드 서비스 시작
        # [수정] 관심종목 예열(prefetch)은 자동매매 초기화(잔고 조회)와 2-TPS를 경합하면
        # EGW00201 재시도를 유발해 메모리가 폭증한다. 예열도 '초기화 이후'로 미룬다
        # (아래 _start_deferred_services 에서 시작).

        # [추가] 시장 국면 캐시 워밍업 (순차 1회 선행)
        # 백그라운드 모니터들이 동시에 지수차트(inquire-daily-indexchartprice)를 조회하면
        # 모의투자(2 TPS) 서버에서 요청 폭주(EGW00201)가 발생한다. 모니터 기동 전에 TPS 게이트를
        # 통과하는 순차 호출로 공유 캐시를 미리 채워, 초기 동시 폭주를 원천 차단한다.
        if "pytest" not in sys.modules and "PYTEST_CURRENT_TEST" not in os.environ:
            for _m_type in ("KOSPI", "KOSDAQ"):
                try:
                    analysis.get_domestic_index_data(_m_type)
                except Exception as _e:
                    logging.debug(f"[Warmup] {_m_type} 국면 캐시 워밍업 실패: {_e}")

        # [수정] 텔레그램 봇은 KIS 2 TPS와 무관(텔레그램 서버 폴링)하므로 즉시 시작
        telegram_cmd = telegram_bot.TelegramCommander()
        telegram_cmd.start()

        # [프로세스 감시] 프로세스가 살아 있다는 도장(logs/heartbeat.json)은 스케줄러
        #  스레드가 1분마다 찍는다. 그런데 그 스레드는 텔레그램이 켜져 있을 때만 뜬다.
        #  꺼진 구성으로 기동하면 지난 기동의 도장이 그대로 남아, 밖에서 도는 감시자가
        #  '죽었다'고 오해한다. 그래서 감시가 성립하지 않는 구성임을 파일에 명시해 둔다
        #  (감시자는 이 표식을 보면 침묵한다). 어차피 알림 경로가 없으므로 손실은 없다.
        try:
            from modules import heartbeat as _heartbeat
            from modules.scheduler import SystemScheduler as _SystemScheduler
            if not _SystemScheduler().is_running:
                _heartbeat.stopped(reason="하트비트 미가동(텔레그램 알림 비활성)")
        except Exception as _hb_e:
            logging.debug(f"[Heartbeat] 초기 상태 표시 실패(무시): {_hb_e}")

    trader = auto_trade.AutoTrader()
    last_choice = "1"

    # [추가] 전역 도움말 함수 맵핑 (서브메뉴 호출용)
    utils.show_help = show_help

    # [수정] KIS API를 쓰는 백그라운드 작업(체결감시·예약감시 모니터 + 관심종목 예열)은
    # 자동매매 초기화(잔고 조회)와 모의투자 2 TPS를 두고 경합하면 EGW00201 재시도 폭주로
    # 초기화가 지연되고 메모리가 폭증한다. 초기화가 TPS를 선점하도록 '초기화 이후'에 시작한다.
    def _start_kis_monitors():
        auto_trade.ConclusionMonitor().start()
        ReservedOrderMonitor().start() # [추가] 예약 주문 모니터링 스레드 시작
        # [추가] 매매일지 웹서버 동기화 워커 (KIS API와 무관 — 미설정 시 즉시 반환)
        try:
            from modules import journal_sync
            journal_sync.start()
            _install_journal_sigterm_handler()
        except Exception as _e:
            logging.warning(f"[Journal] 매매일지 연동 시작 실패(무시): {_e}")
        api.prefetch_watchlists_async() # [수정] 관심종목 예열도 초기화 이후로 지연
        api.start_overview_warmer() # [추가] 개요 화면(시세/지수) 상시 백그라운드 예열 (실전 계좌)
        # [WS] KIS 실시간 시세 피드 시작 + 초기 구독 종목 설정.
        #  시스템 트레이딩 대상(국내주식)을 우선순위로, 국내 ETF를 그 외로 둔다.
        #  (보유종목은 자동매매 루프가 매 사이클 최우선으로 갱신한다.)
        try:
            from brokers import realtime
            sd = config.session.stock_data or {}
            pri = [s['code'] for s in sd.get('stocks_kr', [])]
            etf = [s['code'] for s in sd.get('etfs_kr', [])]
            # ETF 포함 설정 시 ETF도 시스템 트레이딩 대상(매수후보)이므로 우선순위에 포함한다.
            if getattr(config, 'SYSTEM_INCLUDE_ETF', False):
                pri += etf
                other = []
            else:
                other = etf
            realtime.update_symbols(pri, other)
            realtime.start_feed()
        except Exception as _ws_e:
            logging.getLogger("hts").debug(f"[WS] 실시간 피드 시작 실패(REST 폴백): {_ws_e}")

    # [안전장치] 실계좌만 — 안전장치가 꺼진 채로 시작하는 것을 알린다.
    #  종전에는 dynamic_config.json이 모드별로 나뉘지 않아 mode 4에서 끈 시장 필터가
    #  mode 2에도 그대로 적용됐고, 이 경고가 유일한 방어선이었다. 2026-08-23 설정 파일을
    #  모드별 프로필로 분리해 그 유입 경로 자체를 없앴다(config.set_config_profile).
    #  그래도 실전에서 직접 끄는 경로는 남아 있으므로 최종 확인으로 유지한다.
    #  시작을 막거나 값을 되돌리지는 않는다 — 의도적으로 끄는 경우도 있으므로
    #  판단은 사용자에게 두고, '모르는 채로 시작하는' 경우만 없앤다.
    if not (getattr(config.session, 'is_paper', False)
            or config.session.is_toss or config.session.is_simulation):
        try:
            from modules import settings as _settings
            _settings.warn_if_safety_switches_off()
        except Exception as _sw_e:
            logging.getLogger("hts").warning(f"[안전장치] 점검 실패(무시): {_sw_e}")

    # [추가] 자동 시작 모드 처리
    if args.auto:
        config.console.print("\n[bold magenta]━━━ 자동 시작 모드 (Auto Start) ━━━[/]")
        # 비대화형 모드로 트레이딩 시작 (잔고/예수금 초기화가 TPS를 선점하도록 모니터보다 먼저 수행)
        trader.start(interactive=False)

        # [수정] 초기화 완료 후 KIS 폴링 모니터 시작 (TPS 경합 해소)
        _start_kis_monitors()

        # 로그 뷰어 실행 (메인 스레드 블로킹 유지)
        time.sleep(1)
        trader.view_log_file()
    else:
        # [수정] 대화형 모드는 기동 시 자동매매 초기화가 없으므로 모니터를 바로 시작
        _start_kis_monitors()
    
    try:
        _eof_notified = False  # [추가] 비대화형(stdin EOF) 환경 안내를 1회만 남기기 위한 플래그
        _last_fatal_ts = 0.0   # [추가] 치명 오류 회로차단기: 마지막 발생 시각
        _fatal_burst = 0       # [추가] 치명 오류 회로차단기: 짧은 시간 내 연속 발생 횟수
        while True:
            # [추가] 루프 시작(메인 메뉴 복귀) 시 경로 초기화 (안전한 화면 클리어를 위함)
            context.USER_ACTION_BREADCRUMB = []
            
            # [추가] 메인 메뉴 진입 시 화면 정리
            utils.clear_screen()
            
            # [추가] 화면 출력 안정화를 위한 플러시 및 지연 (저사양 환경 대응)
            sys.stdout.flush()
            time.sleep(0.2)

            # [추가] 입력 버퍼 비우기 (이전 작업 중 눌린 키 무시)
            flush_input()

            # [추가] 루프 시작 시 토큰 만료 여부 확인 및 갱신
            api.check_and_refresh_token_if_expired()

            utils.print_breadcrumb()
            
            trader_status = ""
            if trader.is_running:
                if trader.is_market_open():
                    trader_status = " [bold green](RUNNING)[/]"
                else:
                    trader_status = " [bold yellow](WAITING)[/]"
                
            grid = Table.grid(padding=(0, 2))
            grid.add_column(justify="left", style="menu")
            grid.add_column(justify="left", style="dim")
            grid.add_row("[0] 시스템 설정", "(Settings)")
            grid.add_row("[1] 시장 지수 조회", "(Market Indices)")
            grid.add_row("[2] 종목 시세 분석", "(Stock Analysis)")
            grid.add_row("[3] 종목 차트 분석", "(Chart Analysis)")
            grid.add_row("[4] 전략 백테스팅", "(Backtesting)")
            grid.add_row("[5] 시스템 트레이딩", f"(System Trading){trader_status}")
            grid.add_row("[6] 종목발굴·재무분석", "(Discovery & Financials)")
            grid.add_row("[7] 관심 종목 관리", "(Watchlist Management)")
            grid.add_row("[8] 종목 주문 관리", "(Order Management)")
            grid.add_row("[9] 자산 관리", "(Asset Management)")
            config.console.print(grid)
            config.console.print("[dim][Q] 종료 (Quit)  |  [H] 도움말 (Help)[/dim]")
            config.console.print("[dim]" + "─"*50 + "[/dim]"); config.console.print()
            try:
                choice = Prompt.ask("선택 ", choices=["0", "1", "2", "3", "4", "5", "6", "7", "8", "9", "q", "Q", "h", "H"], default=last_choice)
                config.console.print() # 입력 후 공백 라인 추가
                
                # [추가] 운영자 메뉴 선택 로깅
                menu_map = {
                    "0": "시스템 설정", "1": "시장 지수 조회", "2": "종목 시세 분석", "3": "종목 차트 분석",
                    "4": "전략 백테스팅", "5": "시스템 트레이딩", "6": "종목발굴·재무분석",
                    "7": "관심 종목 관리", "8": "종목 주문 관리", "9": "자산 관리",
                    "q": "종료", "h": "도움말"
                }
                menu_name = menu_map.get(choice.lower(), '')
                
                if choice.lower() == "q": 
                    logging.info(f"운영자 실행: [{choice}] {menu_name}")
                    break
                
                if choice.lower() == "h": 
                    logging.info(f"운영자 실행: [{choice}] {menu_name}")
                    show_help()
                    utils.pause()
                    continue

                context.USER_ACTION_BREADCRUMB.append(f"[{choice}] {menu_name}")
                    
                action_taken = None

                if choice == "0": action_taken = settings.system_config_menu()
                elif choice == "1": action_taken = market.show_market_indices()
                elif choice == "2": action_taken = analysis.show_stock_analysis()
                elif choice == "3": 
                    base_breadcrumb_len = len(context.USER_ACTION_BREADCRUMB)
                    last_sub_choice = "6"
                    action_taken = False
                    while True:
                        utils.clear_screen()
                        context.USER_ACTION_BREADCRUMB = context.USER_ACTION_BREADCRUMB[:base_breadcrumb_len]
                        menu_items = [
                            ("1", "국내 주식", "Domestic Stock"), ("2", "국내 ETF", "Domestic ETF"),
                            ("3", "미국 주식", "US Stock"), ("4", "미국 ETF", "US ETF"),
                            ("5", "시장 지수", "Market Indices"), ("6", "직접 입력", "Direct Input")
                        ]
                        sub_choice = utils.show_menu("종목 차트 분석 (Chart Analysis)", menu_items, default_choice=last_sub_choice)
                        
                        if sub_choice.lower() in ['b', 'q']: 
                            break
                        if sub_choice.lower() == 'h': 
                            show_help()
                            utils.pause()
                            continue
                            
                        sub_map = dict((k, v) for k, v, _ in menu_items)
                        context.USER_ACTION_BREADCRUMB.append(f"[{sub_choice}] {sub_map.get(sub_choice, '')}")
                        
                        target_code, target_name, target_ovs = None, None, False
                        
                        if sub_choice == '6':
                            utils.print_breadcrumb()
                            raw_input = Prompt.ask("종목코드(6자리/티커) 입력 [dim](이전: b, 메인: q)[/dim]")
                            config.console.print()
                            if raw_input and raw_input.lower() not in ['b', 'q']:
                                context.USER_ACTION_BREADCRUMB.append(f"[직접입력] {raw_input}")
                                if raw_input.isdigit() and len(raw_input) == 6:
                                    target_code = raw_input
                                    target_name = api.get_stock_name_by_code(target_code, False) or target_code
                                    target_ovs = False
                                else:
                                    target_code = raw_input.upper()
                                    target_name = api.get_stock_name_by_code(target_code, True) or target_code
                                    target_ovs = True
                                    
                                if not utils.validate_and_confirm_stock(target_code, target_name, target_ovs, "이 종목으로 차트 분석을 진행하시겠습니까?"):
                                    target_code = None
                        elif sub_choice == '5':
                            # [수정] 지수 목록·소스 선정은 지수 화면(메인 1)과 동일한 규칙을 공유한다.
                            #  종전에는 목록의 yfinance 티커를 그대로 넘겨 코스피200·코스닥150이
                            #  모드별 소스(KIS/토스/tvDatafeed)를 타지 못했고, 자리표시자 티커
                            #  (^VKOSPI·^K200FUT·^US02Y)는 조회 자체가 실패했다.
                            indices_list = market.selectable_indices()
                            dict_list = [{'name': n, 'code': c} for n, c in indices_list]
                            idx, item = utils.search_stock_in_list(dict_list, title="시장 지수 목록", display_func=lambda i, s: f"[{i+1}] {s.get('name', 'Unknown')}")
                            if item:
                                target_name = item['name']
                                target_code, target_ovs = market.resolve_index_source(target_name, item['code'])
                                context.USER_ACTION_BREADCRUMB.append(f"[지수선택] {target_name}")
                        elif sub_choice in ["1", "2", "3", "4"]:
                            key_map = {"1": "stocks_kr", "2": "etfs_kr", "3": "stocks_us", "4": "etfs_us"}
                            s_list = config.session.stock_data.get(key_map[sub_choice], [])
                            if s_list:
                                idx, item = utils.search_stock_in_list(s_list, title=f"{sub_map[sub_choice]} 목록")
                                if item:
                                    target_code, target_name = item['code'], item['name']
                                    target_ovs = (sub_choice in ["3", "4"])
                                    context.USER_ACTION_BREADCRUMB.append(f"[종목선택] {target_name}")
                            else:
                                config.console.print("[yellow]목록이 비어있습니다.[/yellow]")
                                utils.pause()

                        if target_code: 
                            logging.info(f"운영자 실행: {' - '.join(context.USER_ACTION_BREADCRUMB)}")
                            
                            menu_items_type = [("1", "주봉", "Weekly"), ("2", "일봉", "Daily"), ("3", "시봉", "Hourly"), ("4", "분봉", "Intraday")]
                            c_type = utils.show_menu("차트 유형을 선택하세요", menu_items_type, default_choice="2")

                            if c_type.lower() not in ['b', 'q']:
                                type_map = dict((k, v) for k, v, _ in menu_items_type)
                                context.USER_ACTION_BREADCRUMB.append(f"[{c_type}] {type_map.get(c_type, '')}")

                                p_type = 'daily'
                                if c_type == '1': p_type = 'weekly'
                                elif c_type == '3': p_type = 'hourly'
                                elif c_type == '4': p_type = 'intraday'

                                # 일봉은 표시 기간 선택 (기본 6개월)
                                c_months = 6
                                if p_type == 'daily':
                                    menu_items_period = [("1", "6개월", "6 Months"), ("2", "1년", "1 Year")]
                                    c_period = utils.show_menu("표시 기간을 선택하세요", menu_items_period, default_choice="1")
                                    if c_period.lower() in ['b', 'q']:
                                        continue
                                    c_months = 12 if c_period == '2' else 6
                                    context.USER_ACTION_BREADCRUMB.append(f"[{c_period}] {'1년' if c_months == 12 else '6개월'}")

                                chart_path = chart.generate_visual_chart(target_code, target_name, target_ovs, period_type=p_type, months=c_months)

                                # [AI 분석] 생성된 차트 이미지를 Gemini 비전 모델로 전달해 전체 차트를 판독·분석
                                if chart_path and os.path.exists(chart_path):
                                    if not config.GEMINI_API_KEY:
                                        config.console.print("[dim]※ AI 차트 분석은 GEMINI_API_KEY 설정 시 이용할 수 있습니다.[/dim]")
                                    else:
                                        config.console.print()
                                        do_ai = Prompt.ask("🤖 이 차트를 AI로 분석하시겠습니까?", choices=["y", "n"], default="n")
                                        if do_ai.lower() == "y":
                                            period_str_map = {"weekly": "주봉", "daily": f"일봉({c_months}개월)", "hourly": "시봉", "intraday": "분봉"}
                                            period_str = period_str_map.get(p_type, "일봉")
                                            with Progress(SpinnerColumn(), TextColumn("[progress.description]{task.description}"), BarColumn(), console=config.console, transient=True) as _p:
                                                _p.add_task(f"[cyan]Gemini가 차트 이미지를 판독하여 분석 중...[/cyan]\n[dim]  (모델: {config.GEMINI_MODEL})[/dim]", total=None)
                                                ai_result = theme_analysis.analyze_chart_image_with_gemini(chart_path, target_name, target_code, period_str)
                                            if ai_result:
                                                if ai_result.startswith("⚠️"):
                                                    config.console.print(f"\n{ai_result}")
                                                else:
                                                    # [출력 통일] 기존 AI 심층 진단 리포트와 동일한 여백/패딩 사용
                                                    #   (theme_analysis.py: Panel padding=(1,2), width=120 + 좌우 마진 Padding(_,(0,4)))
                                                    from rich.markdown import Markdown
                                                    from rich.panel import Panel
                                                    from rich.padding import Padding
                                                    _md = Markdown(ai_result)
                                                    _panel = Panel(_md, title=f"🤖 AI 차트 분석: {target_name}({target_code}) - {period_str}", border_style="cyan", padding=(1, 2), width=120)
                                                    config.console.print()
                                                    config.console.print(Padding(_panel, (0, 4)))

                                last_sub_choice = sub_choice
                                action_taken = True
                                utils.pause()
                elif choice == "4": action_taken = backtest.run_backtest()
                elif choice == "5": action_taken = auto_trade.system_trading_menu() 
                elif choice == "6": action_taken = theme_analysis.run_theme_analysis()
                elif choice == "7": action_taken = manage.manage_stock_menu()
                elif choice == "8": action_taken = trading.stock_order_menu()
                elif choice == "9": 
                    try:
                        action_taken = account.asset_management_menu()
                    except Exception as e:
                        import traceback
                        config.console.print(f"[bold red]⚠️ 자산 관리 메뉴 실행 중 오류 발생: {e}[/bold red]")
                        logging.error(f"자산 관리 메뉴 오류: {e}\n{traceback.format_exc()}")
                        action_taken = None
                
                if action_taken is not False:
                    last_choice = choice
                    utils.pause()
            except GlobalCommandJump as e:
                global _global_command_queue
                _global_command_queue = e.command_list
                continue
            except KeyboardInterrupt:
                config.console.print()
                config.console.print()
                try:
                    config.console.print()
                    if Prompt.ask("프로그램을 종료하시겠습니까?", choices=["y", "n"], default="n") == "y":
                        break
                except (KeyboardInterrupt, EOFError):
                    break
            except EOFError:
                # [긴급] stdin이 없는 비대화형 환경(백그라운드/nohup 실행, SSH 세션 단절 등)에서는
                # Prompt.ask가 즉시 EOFError를 던진다. str(EOFError())는 빈 문자열이라 기존 Exception
                # 핸들러가 '원인 없음'으로 텔레그램 '치명적 시스템 오류'를 무한 도배했다.
                # 입력이 불가능한 환경이므로 알림 없이 조용히 대기하여 백그라운드(자동매매 등)는 유지한다.
                if not _eof_notified:
                    logging.warning("stdin EOF 감지(비대화형 환경). 메인 메뉴 입력을 중단하고 백그라운드 작업만 유지합니다.")
                    _eof_notified = True
                time.sleep(60)  # 폭주/도배 방지 (백그라운드 스레드는 계속 동작)
                continue
            except Exception as e:
                err_text = str(e).strip()

                # [방어] 원인 메시지가 비어있는 예외(EOFError 등 비대화형 stdin 문제로 인한 변종 포함)는
                # 무한 도배의 주범이므로 EOF와 동일하게 알림 없이 조용히 대기한다.
                if not err_text:
                    if not _eof_notified:
                        logging.warning(f"원인 불명(빈 메시지) 예외 감지({type(e).__name__}). 비대화형 환경으로 간주하여 메인 메뉴 입력을 중단합니다.")
                        _eof_notified = True
                    time.sleep(60)
                    continue

                config.console.print(f"\n[bold red]치명적인 오류 발생: {escape(err_text)}[/bold red]")

                # [방어] 회로차단기: 짧은 시간(10초) 내 반복되는 치명 오류는 텔레그램 알림을
                # 억제하고 대기하여 도배 및 CPU 폭주를 막는다.
                now = time.time()
                _fatal_burst = _fatal_burst + 1 if (now - _last_fatal_ts <= 10) else 0
                _last_fatal_ts = now

                if _fatal_burst < 3:
                    # [추가] 오류 발생 시 텔레그램 알림 및 로그 전송
                    # [Fix] 문구 정정 — 이 except 블록은 continue/break 없이 끝나 while 루프로 복귀한다.
                    #  즉 프로그램은 종료되지 않고 메인 메뉴가 다시 뜨며, 백그라운드 스레드(자동매매·
                    #  체결 감시)도 그대로 살아 있다. 그런데 기존 문구가 '강제 종료'라고 단정해,
                    #  운용자가 시스템이 죽은 줄 알고 불필요하게 재시작하게 만들었다.
                    #  (실제 사례: 백테스트 프롬프트에 '2,3,4'를 잘못 입력해 ValueError가 올라온 것)
                    try:
                        from modules.auto_trade import get_mystock_log_tail
                        log_tail = get_mystock_log_tail(20)
                        msg = (f"⚠️ [시스템 오류] 메뉴 처리 중 예외 발생\n"
                               f"메인 메뉴는 자동 복구되었고 자동매매·체결 감시는 계속 동작합니다.\n\n"
                               f"원인: {err_text}\n\n"
                               f"📜 [최근 시스템 로그 (mystock.log)]\n```\n{log_tail}```")
                        api.send_telegram_message(msg)
                    except Exception: pass
                else:
                    if _fatal_burst == 3:
                        logging.error("치명 오류 반복 폭주 감지: 텔레그램 알림을 억제하고 대기합니다.")
                    time.sleep(30)
    finally:
        config.console.print()
        with Progress(
            SpinnerColumn(),
            TextColumn("[progress.description]{task.description}"),
            BarColumn(),
            console=config.console,
            transient=True
        ) as progress:
            task = progress.add_task("[cyan]시스템 종료 프로세스 진행 중...[/cyan]", total=None)
            # 1. 자동매매 종료
            progress.update(task, description="[cyan][1/4] 자동매매 스레드 안전 종료 중...[/cyan]")
            try:
                if trader.is_running:
                    trader.stop(use_status=False)
            except Exception: pass
            time.sleep(0.5)
            config.console.print("[1/4] 자동매매 스레드 안전 종료 [bold green][완료][/]")
            
            # 2. 백그라운드 서비스 종료
            progress.update(task, description="[cyan][2/4] 백그라운드 서비스(텔레그램/감시) 종료 중...[/cyan]")
            auto_trade.ConclusionMonitor().stop()
            telegram_cmd.stop()
            ReservedOrderMonitor().stop() # [추가] 예약 주문 모니터링 중지
            # [추가] 매매일지 웹서버에 '정지됨'을 통지한 뒤 워커 종료.
            #  이 신호가 없으면 웹 대시보드 표시등이 Ping 3회 누락(약 35초) 전까지
            #  '정상 가동중'으로 남아, 종료한 뒤에도 가동 중인 것처럼 보인다.
            try:
                from modules import journal_sync
                journal_sync.stop()
            except Exception as _e:
                logging.warning(f"[Journal] 종료 통지 실패(무시): {_e}")
            time.sleep(0.5)
            config.console.print("[2/4] 백그라운드 서비스(텔레그램/감시) 종료 [bold green][완료][/]")
            
            # 3. DB 큐 종료
            progress.update(task, description="[cyan][3/4] DB 작업 큐 처리 및 종료 중...[/cyan]")
            db_queue.shutdown()
            time.sleep(0.5)
            config.console.print("[3/4] DB 작업 큐 처리 및 종료 [bold green][완료][/]")
            
            # 4. DB 최적화 (VACUUM)
            progress.update(task, description="[cyan][4/4] 데이터베이스 최적화(VACUUM) 수행 중...[/cyan]")
            try:
                # [수정] DB Proxy가 종료되었으므로 원본 DB 객체에 직접 접근하여 실행 (타임아웃 방지)
                real_db = db_manager.db
                if hasattr(real_db, '_real_db'):
                    real_db = real_db._real_db
                real_db.run_vacuum()
            except Exception as e:
                config.console.print(f"[red]VACUUM 실패: {e}[/red]")
            config.console.print("[4/4] 데이터베이스 최적화(VACUUM) 수행 [bold green][완료][/]")

        config.console.print("[yellow]프로그램을 종료합니다.[/yellow]")
        config.console.print()
        os._exit(0) # [추가] 스레드 대기 없이 즉시 종료 (KeyboardInterrupt Traceback 방지)
        
if __name__ == "__main__":
    main()
