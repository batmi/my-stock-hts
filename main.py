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
import context # [추가]

# [추가] config(rich.console) 로드 후 추가 진행 상태 출력
config.console.print("  - 네트워크 및 코어 모듈(API, DB) 로딩 중...")

import api
import toss_api  # [추가] 토스증권 클라이언트 (mode 3)
import utils
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
    if config.session.is_toss:
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
        checks_ok = False

    if not checks_ok: return False

    # 3. 종목 데이터 로드 및 누락/오류 exchange 정보 보완
    config.session.load_stock_config()
    
    # [수정] API 현재가 조회 응답에 시장 구분이 없어 오분류(전부 KOSPI)되던 버그를 마스터 리스트를 통해 영구 교정
    from modules import analysis
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
    table = Table(title="지수 및 종목 상태별 색상 조건", box=box.HORIZONTALS, header_style="dim", border_style="dim")
    table.add_column("항목", style="bold"); table.add_column("조건", justify="left")
    table.add_column("색상", justify="center"); table.add_column("비고", justify="left")

    # [수정] 설정값 로드하여 동적 표시
    ma_period = config.MARKET_REGIME_PARAMS.get('REGIME_MA_PERIOD', 20)
    obv_period = config.INDICATOR_PARAMS.get("OBV_MA_PERIOD", 5)

    table.add_row("시장 지수", f"지수 > EMA {ma_period}일선 & 이평선우상향 & ADX조건", "[red]빨간색[/]", "강세장 (Bull)")
    table.add_row("(글로벌/원자재/코인)", f"지수 < EMA {ma_period}일선", "[blue]파란색[/]", "약세장 (Bear)")
    table.add_row("", "그 외 구간", "[yellow]노란색[/]", "횡보장 (Sideways)")
    table.add_section()

    table.add_row("미 국채 5년", "금리 ≥ 5.00", "[magenta]보라색[/]", "단기 유동성 위기/초긴축 발작")
    table.add_row("(중기 통화정책 반영)", "4.70 ≤ 금리 < 5.00", "[red]빨간색[/]", "긴축 강화/금리 재인상 공포")
    table.add_row("", "4.20 ≤ 금리 < 4.70", "[orange3]주황색[/]", "중립 상단/불확실성 상존")
    table.add_row("", "3.70 ≤ 금리 < 4.20", "[green]초록색[/]", "안정/적정 수준 유동성")
    table.add_row("", "3.20 ≤ 금리 < 3.70", "[yellow]노란색[/]", "금리 인하 기대감 선반영")
    table.add_row("", "금리 < 3.20", "[blue]파란색[/]", "금리 급락/단기 유동성 경색 또는 침체 우려")
    table.add_section()

    table.add_row("미 국채 10년", "금리 ≥ 5.10", "[magenta]보라색[/]", "시스템 위기/주식 Valuation 붕괴")
    table.add_row("(글로벌 벤치마크)", "4.80 ≤ 금리 < 5.10", "[red]빨간색[/]", "임계점/고금리 쇼크 (기술주 부담 가중)")
    table.add_row("", "4.40 ≤ 금리 < 4.80", "[orange3]주황색[/]", "고금리 지속/인플레이션 끈적임 경계")
    table.add_row("", "4.00 ≤ 금리 < 4.40", "[green]초록색[/]", "골디락스/적정 성장과 물가의 균형점 (뉴노멀)")
    table.add_row("", "3.50 ≤ 금리 < 4.00", "[yellow]노란색[/]", "수요 둔화/금리 인하 선반영")
    table.add_row("", "금리 < 3.50", "[blue]파란색[/]", "침체 확정/안전 자산 선호 (Flight to Quality)")
    table.add_section()

    table.add_row("미 국채 30년", "금리 ≥ 5.50", "[magenta]보라색[/]", "재정 적자 심화/기간 프리미엄 극대화")
    table.add_row("(장기 기대 인플레)", "5.10 ≤ 금리 < 5.50", "[red]빨간색[/]", "장기 인플레 우려/국채 발행 부담")
    table.add_row("", "4.60 ≤ 금리 < 5.10", "[orange3]주황색[/]", "구조적 고금리 안착 경계")
    table.add_row("", "4.10 ≤ 금리 < 4.60", "[green]초록색[/]", "장기 안정/수급 균형")
    table.add_row("", "3.70 ≤ 금리 < 4.10", "[yellow]노란색[/]", "장기 성장률 둔화 우려")
    table.add_row("", "금리 < 3.70", "[blue]파란색[/]", "장기 저성장/디플레이션 우려")
    table.add_section()

    table.add_row("브랜트유", "가격 ≥ 105", "[magenta]보라색[/]", "에너지 쇼크 (강제적 수요 파괴 및 스태그플레이션)")
    table.add_row("", "95 ≤ 가격 < 105", "[red]빨간색[/]", "인플레 재발 우려 (고금리 장기화 강요)")
    table.add_row("", "85 ≤ 가격 < 95", "[orange3]주황색[/]", "고유가 지속 (인플레 압력 상존)")
    table.add_row("", "70 ≤ 가격 < 85", "[green]초록색[/]", "골디락스 (산유국 수익과 물가 안정의 최적 균형점)")
    table.add_row("", "60 ≤ 가격 < 70", "[yellow]노란색[/]", "수요 둔화 (경기 하강 신호)")
    table.add_row("", "가격 < 60", "[blue]파란색[/]", "시스템 위기 (심각한 수요 파괴 및 침체)")
    table.add_section()

    table.add_row("WTI 원유", "가격 ≥ 100", "[magenta]보라색[/]", "에너지 쇼크 (강제적 수요 파괴 및 스태그플레이션)")
    table.add_row("", "90 ≤ 가격 < 100", "[red]빨간색[/]", "인플레 재발 우려 (고금리 장기화 강요)")
    table.add_row("", "80 ≤ 가격 < 90", "[orange3]주황색[/]", "고유가 지속 (인플레 압력 상존)")
    table.add_row("", "65 ≤ 가격 < 80", "[green]초록색[/]", "골디락스 (산유국 수익과 물가 안정의 최적 균형점)")
    table.add_row("", "55 ≤ 가격 < 65", "[yellow]노란색[/]", "수요 둔화 (경기 하강 신호)")
    table.add_row("", "가격 < 55", "[blue]파란색[/]", "시스템 위기 (심각한 수요 파괴 및 침체)")
    table.add_section()

    table.add_row("가솔린 RBOB", "가격 ≥ 4.00", "[magenta]보라색[/]", "에너지 쇼크: 강제적 수요 파괴 및 스태그플레이션 확정")
    table.add_row("", "3.20 ≤ 가격 < 4.00", "[red]빨간색[/]", "임계점: 고금리 긴축 강요, 기업 이익률 급격 둔화")
    table.add_row("", "2.60 ≤ 가격 < 3.20", "[orange3]주황색[/]", "고유가 지속: 인플레 압력 상존, 실물 경제 마지노선")
    table.add_row("", "2.10 ≤ 가격 < 2.60", "[green]초록색[/]", "골디락스: 산유국 수익성과 물가 안정의 최적 균형점")
    table.add_row("", "1.60 ≤ 가격 < 2.10", "[yellow]노란색[/]", "수요 둔화: 경기 하강 신호 및 에너지 투자 위축 시작")
    table.add_row("", "가격 < 1.60", "[blue]파란색[/]", "시스템 위기: 심각한 경기 침체 혹은 금융 위기 동반")
    table.add_section()

    table.add_row("천연가스", "가격 ≥ 6.0", "[magenta]보라색[/]", "에너지 쇼크 (공급망 붕괴 또는 극단적 기후 위기)")
    table.add_row("", "4.0 ≤ 가격 < 6.0", "[red]빨간색[/]", "물가 비상 (에너지 인플레 유발)")
    table.add_row("", "3.0 ≤ 가격 < 4.0", "[orange3]주황색[/]", "수급 타이트 (겨울철 피크 또는 수출 수요 강세)")
    table.add_row("", "2.0 ≤ 가격 < 3.0", "[green]초록색[/]", "안정/중립 (현재 박스권 최적 균형점)")
    table.add_row("", "1.5 ≤ 가격 < 2.0", "[yellow]노란색[/]", "공급 과잉 (생산 업체 수익성 악화 우려)")
    table.add_row("", "가격 < 1.5", "[blue]파란색[/]", "시스템 하강 (심각한 수요 파괴 또는 디플레이션 신호)")
    table.add_section()

    table.add_row("밀", "가격 ≥ 800", "[magenta]보라색[/]", "식량 안보 위기 (전쟁/극단적 기후)")
    table.add_row("", "700 ≤ 가격 < 800", "[red]빨간색[/]", "식량 인플레 경계 (애그플레이션 우려)")
    table.add_row("", "600 ≤ 가격 < 700", "[orange3]주황색[/]", "수급 타이트 (기후 리스크 및 작황 부진)")
    table.add_row("", "500 ≤ 가격 < 600", "[green]초록색[/]", "안정/중립 (현재 박스권 최적 균형점)")
    table.add_row("", "400 ≤ 가격 < 500", "[yellow]노란색[/]", "공급 과잉 (풍작/재고 증가)")
    table.add_row("", "가격 < 400", "[blue]파란색[/]", "농가 수익성 악화 (디플레이션 신호)")
    table.add_section()

    table.add_row("달러 인덱스", "지수 ≥ 115", "[magenta]보라색[/]", "글로벌 달러 유동성 경색 (시스템 위기)")
    table.add_row("", "110 ≤ 지수 < 115", "[red]빨간색[/]", "초강달러 (신흥국 자본 유출 패닉)")
    table.add_row("", "105 ≤ 지수 < 110", "[orange3]주황색[/]", "강달러 경계 (미국 외 국가 인플레 자극)")
    table.add_row("", "95 ≤ 지수 < 105", "[green]초록색[/]", "안정/중립 (가장 이상적인 골디락스)")
    table.add_row("", "지수 < 95", "[blue]파란색[/]", "달러 약세 (신흥국/위험자산 랠리)")
    table.add_section()

    table.add_row("달러 환율", "환율 ≥ 1500원", "[magenta]보라색[/]", "시스템 위기 / 외환 패닉")
    table.add_row("", "1450 ≤ 환율 < 1500", "[red]빨간색[/]", "위험 구간 (당국 개입 및 자본 유출 우려)")
    table.add_row("", "1400 ≤ 환율 < 1450", "[orange3]주황색[/]", "구조적 고환율 (경제 부담 가중)")
    table.add_row("", "1300 ≤ 환율 < 1400", "[green]초록색[/]", "강달러 뉴노멀 (현재 시장 중립 구간)")
    table.add_row("", "1200 ≤ 환율 < 1300", "[cyan]청록색[/]", "안정화 (원화 강세 전환)")
    table.add_row("", "환율 < 1200", "[blue]파란색[/]", "초강세 원화 (수출 기업 실적 부담)")
    table.add_section()

    table.add_row("VIX 변동성 지수명", "지수 < 15", "[green]초록색[/]", "안정 (평균 수준/골디락스장)")
    table.add_row("", "15 ≤ 지수 < 20", "[yellow]노란색[/]", "경계 진입 (단기 변동성 확대/노이즈)")
    table.add_row("", "20 ≤ 지수 < 30", "[orange3]주황색[/]", "위험 구간 (추세 훼손/조정장 진입)")
    table.add_row("", "30 ≤ 지수 < 40", "[red]빨간색[/]", "공포/패닉 (급락장/베어마켓)")
    table.add_row("", "지수 ≥ 40", "[magenta]보라색[/]", "시스템 위기 (블랙스완/투매)")
    table.add_section()

    table.add_row("비트코인 등 암호화폐", "고점 대비 낙폭 ≤ 10%", "[red]빨간색[/]", "신고가 랠리 (크립토 불장)")
    table.add_row("", "10% < 고점 대비 낙폭 ≤ 25%", "[orange3]주황색[/]", "건전한 조정 (높은 변동성 허용 구간)")
    table.add_row("", "25% < 고점 대비 낙폭 ≤ 40%", "[yellow]노란색[/]", "투심 위축 / 하락 추세 전환 경계")
    table.add_row("", "고점 대비 낙폭 > 40%", "[blue]파란색[/]", "크립토 윈터 / 깊은 침체장")
    table.add_section()

    table.add_row("금 (Gold) 지수명", "고점 대비 낙폭 ≤ 3%", "[red]빨간색[/]", "신고가 랠리 (안전자산 선호/인플레 헷지)")
    table.add_row("", "3% < 고점 대비 낙폭 ≤ 8%", "[orange3]주황색[/]", "건전한 조정")
    table.add_row("", "8% < 고점 대비 낙폭 ≤ 15%", "[yellow]노란색[/]", "단기 약세/추세 둔화")
    table.add_row("", "고점 대비 낙폭 > 15%", "[blue]파란색[/]", "하락장 (위험자산 선호/달러 초강세)")
    table.add_section()

    table.add_row("은/구리 지수명", "고점 대비 낙폭 ≤ 5%", "[red]빨간색[/]", "신고가 랠리 (경기 확장/원자재 랠리)")
    table.add_row("", "5% < 고점 대비 낙폭 ≤ 15%", "[orange3]주황색[/]", "건전한 조정")
    table.add_row("", "15% < 고점 대비 낙폭 ≤ 25%", "[yellow]노란색[/]", "수요 둔화/기술적 조정기")
    table.add_row("", "고점 대비 낙폭 > 25%", "[blue]파란색[/]", "경기 침체 우려 (닥터 코퍼 경고)")
    table.add_section()

    table.add_row("SOX 반도체 지수명", "고점 대비 낙폭 ≤ 5%", "[red]빨간색[/]", "신고가 랠리 및 초강세")
    table.add_row("", "5% < 고점 대비 낙폭 ≤ 10%", "[orange3]주황색[/]", "건전한 조정")
    table.add_row("", "10% < 고점 대비 낙폭 ≤ 20%", "[yellow]노란색[/]", "기술적 조정기 진입")
    table.add_row("", "고점 대비 낙폭 > 20%", "[blue]파란색[/]", "반도체 하락 사이클/침체")
    table.add_section()

    table.add_row("NBI 바이오 지수명", "고점 대비 낙폭 ≤ 5%", "[red]빨간색[/]", "신고가 랠리 및 초강세")
    table.add_row("", "5% < 고점 대비 낙폭 ≤ 15%", "[orange3]주황색[/]", "건전한 조정")
    table.add_row("", "15% < 고점 대비 낙폭 ≤ 25%", "[yellow]노란색[/]", "기술적 조정기 진입")
    table.add_row("", "고점 대비 낙폭 > 25%", "[blue]파란색[/]", "바이오 하락 사이클/침체")
    table.add_section()

    table.add_row("BKX 은행 지수명", "고점 대비 낙폭 ≤ 5%", "[red]빨간색[/]", "신고가 랠리 및 초강세")
    table.add_row("", "5% < 고점 대비 낙폭 ≤ 10%", "[orange3]주황색[/]", "건전한 조정")
    table.add_row("", "10% < 고점 대비 낙폭 ≤ 20%", "[yellow]노란색[/]", "기술적 조정기 진입")
    table.add_row("", "고점 대비 낙폭 > 20%", "[blue]파란색[/]", "은행업/경제 하락 사이클/침체")
    table.add_section()

    table.add_row("DJU 유틸/전력 지수명", "고점 대비 낙폭 ≤ 5%", "[red]빨간색[/]", "신고가 랠리 및 초강세 (AI 전력 수요 폭발)")
    table.add_row("", "5% < 고점 대비 낙폭 ≤ 10%", "[orange3]주황색[/]", "건전한 조정 및 인프라 숨고르기")
    table.add_row("", "10% < 고점 대비 낙폭 ≤ 15%", "[yellow]노란색[/]", "기술적 조정기 진입 (금리 부담 가중)")
    table.add_row("", "고점 대비 낙폭 > 15%", "[blue]파란색[/]", "유틸리티 하락 사이클 및 방어주 침체")
    table.add_section()

    table.add_row("DRG 제약 지수명", "고점 대비 낙폭 ≤ 5%", "[red]빨간색[/]", "신고가 랠리 및 초강세 (방어주 부각)")
    table.add_row("", "5% < 고점 대비 낙폭 ≤ 10%", "[orange3]주황색[/]", "건전한 조정 및 파이프라인 숨고르기")
    table.add_row("", "10% < 고점 대비 낙폭 ≤ 15%", "[yellow]노란색[/]", "기술적 조정기 진입 (임상/약가 리스크)")
    table.add_row("", "고점 대비 낙폭 > 15%", "[blue]파란색[/]", "제약/헬스케어 하락 사이클 및 침체")
    table.add_section()

    table.add_row("DJT 운송 지수명", "고점 대비 낙폭 ≤ 5%", "[red]빨간색[/]", "신고가 랠리 및 초강세 (실물 경기 호황)")
    table.add_row("", "5% < 고점 대비 낙폭 ≤ 10%", "[orange3]주황색[/]", "건전한 조정")
    table.add_row("", "10% < 고점 대비 낙폭 ≤ 20%", "[yellow]노란색[/]", "기술적 조정기 진입 (경기 둔화 우려 반영)")
    table.add_row("", "고점 대비 낙폭 > 20%", "[blue]파란색[/]", "운송업 및 실물경제 하락 사이클/침체")
    table.add_section()

    table.add_row("XAL 항공 지수명", "고점 대비 낙폭 ≤ 5%", "[red]빨간색[/]", "신고가 랠리 및 초강세 (여행 수요 폭발)")
    table.add_row("", "5% < 고점 대비 낙폭 ≤ 15%", "[orange3]주황색[/]", "건전한 조정 및 유가/환율 숨고르기")
    table.add_row("", "15% < 고점 대비 낙폭 ≤ 25%", "[yellow]노란색[/]", "기술적 조정기 진입 (운영비 증가 우려)")
    table.add_row("", "고점 대비 낙폭 > 25%", "[blue]파란색[/]", "항공업 하락 사이클/침체 (외부 쇼크 반영)")
    table.add_section()

    table.add_row("XOI 에너지 지수명", "고점 대비 낙폭 ≤ 5%", "[red]빨간색[/]", "신고가 랠리 및 초강세 (유가 상승 수혜)")
    table.add_row("", "5% < 고점 대비 낙폭 ≤ 10%", "[orange3]주황색[/]", "건전한 조정")
    table.add_row("", "10% < 고점 대비 낙폭 ≤ 20%", "[yellow]노란색[/]", "기술적 조정기 진입 (원유 수요 둔화 우려)")
    table.add_row("", "고점 대비 낙폭 > 20%", "[blue]파란색[/]", "에너지/정유업 하락 사이클/침체")
    table.add_section()

    table.add_row("HUI 금광 지수명", "고점 대비 낙폭 ≤ 5%", "[red]빨간색[/]", "신고가 랠리 및 초강세 (안전자산 선호 극대화)")
    table.add_row("", "5% < 고점 대비 낙폭 ≤ 15%", "[orange3]주황색[/]", "건전한 조정 및 금값 단기 숨고르기")
    table.add_row("", "15% < 고점 대비 낙폭 ≤ 30%", "[yellow]노란색[/]", "기술적 조정기 진입 (고금리/강달러 압박)")
    table.add_row("", "고점 대비 낙폭 > 30%", "[blue]파란색[/]", "금광업 하락 사이클 및 투심 위축")
    table.add_section()

    table.add_row("유럽 등 주요 지수", "고점 대비 낙폭 ≤ 3%", "[red]빨간색[/]", "신고가 근접/초강세장")
    table.add_row("(FTSE, CAC, DAX, STOXX)", "3% < 고점 대비 낙폭 ≤ 8%", "[orange3]주황색[/]", "안정적인 상승장/건전한 조정")
    table.add_row("", "8% < 고점 대비 낙폭 ≤ 20%", "[yellow]노란색[/]", "일반 조정/중립 구간")
    table.add_row("", "고점 대비 낙폭 > 20%", "[blue]파란색[/]", "침체/약세장 진입")
    table.add_section()

    table.add_row("등락폭/등락률", "상승 (> 0)", "[red]빨간색[/]", "전일 대비 상승")
    table.add_row("", "하락 (< 0)", "[blue]파란색[/]", "전일 대비 하락")
    table.add_row("", "보합 (== 0)", "[white]흰색[/]", "전일 대비 보합")
    table.add_section()

    table.add_row("52주 고점대비", "등락률 > -3.0%", "[red]빨간색[/]", "신고가 근접 (초강세)")
    table.add_row("", "등락률 < -20.0%", "[blue]파란색[/]", "침체/약세장 진입")
    table.add_row("", "-3.0% ~ -20.0%", "[white]흰색[/]", "일반 조정/중립")
    table.add_section()

    table.add_row("종목명 색상", "이평선 정배열 & ADX ≥ 40 & RSI ≥ 70 & CCI ≥ 100", "[magenta]보라색[/]", "과열/하락 반전 주의")
    table.add_row("", "이평선 정배열 & 현재가 > 5일선 & ADX ≥ 30 & RSI ≥ 55 & CCI ≥ 100", "[red]빨간색[/]", "강력한 상승 추세")
    table.add_row("", "이평선 역배열 & 현재가 > 5일선 & ADX ≥ 20 & RSI ≥ 45 & CCI ≥ 0", "[orange3]주황색[/]", "바닥권 상승 반전 시도")
    table.add_row("", "이평선 20선 > 60선 > 5선 & ADX ≥ 30 & RSI ≤ 30 & CCI ≤ 100", "[blue]파란색[/]", "하락 심화/매도 우위")
    table.add_section()

    buy_score = config.ANALYSIS_THRESHOLDS["BUY_SCORE"]
    rise_score = config.ANALYSIS_THRESHOLDS["RISE_SCORE"]
    buy_rsi = config.ANALYSIS_THRESHOLDS["BUY_RSI_MAX"]
    table.add_row("종목 분류", f"{buy_score}점 이상 & RSI<{buy_rsi}", "[red]매수[/]", "강력 매수 구간 (분할 진입)")
    table.add_row("", f"{rise_score} ~ {buy_score}점 미만 (상승 추세)", "[orange3]상승[/]", "상승 초입/지속 (대기/소량)")
    table.add_row("", "정렬 미완성 + 추세전환 초기신호 ≥3개 (위험신호 없음)", "[green]관심[/]", "태동 단계/수동 스윙 모니터링 (120일선 아래도 포착)")
    table.add_row("", "방향성 불명확 단계", "[white]관망[/]", "방향성 탐색 (거래 비권장)")
    table.add_row("", "추세 이탈 / 단기 하락", "[yellow]주의[/]", "신규매수 자제/비중축소 고려")
    table.add_row("", "장기추세 붕괴 및 과열", "[blue]매도[/]", "적극 매도/손절 고려 (위험)")
    table.add_section()

    table.add_row("현재가 (추세)", "현재가 > 20일선 & 20일선 > 60일선", "[red]빨간색[/]", "강세: 중장기 상승 추세 속 단기 강세")
    table.add_row("", "현재가 < 20일선 & 20일선 < 60일선", "[blue]파란색[/]", "약세: 중장기 하락 추세 속 단기 약세")
    table.add_row("", "현재가 > 20일선 & 20일선 < 60일선", "[orange3]주황색[/]", "반등 시도: 장기 약세 속 단기 추세 전환 시도")
    table.add_row("", "현재가 < 20일선 & 20일선 > 60일선", "[white]흰색[/]", "눌림목 조정: 장기 강세 속 단기 조정")
    table.add_section()

    table.add_row("체결강도", "150% 이상", "[magenta]보라색[/]", "강력한 수급: 공격적인 매수세 유입, 주가 급등 가능성 높음")
    table.add_row("", "120% ~ 150%", "[red]빨간색[/]", "매수 우위: 상승 추세 강화, 단기 모멘텀 발생")
    table.add_row("", "100% ~ 120%", "[orange3]주황색[/]", "점진적 유입: 완만한 매수세, 주가 하방 지지력 강함")
    table.add_row("", "100%", "[white]흰색[/]", "균형: 매수와 매도의 힘이 팽팽하게 맞서는 상태")
    table.add_row("", "80% ~ 100%", "[yellow]노란색[/]", "매도 우위: 주가 탄력 둔화, 관망세 확산")
    table.add_row("", "80% 미만", "[blue]파란색[/]", "하락 압력: 공격적인 매도세, 추가 하락 경계 필요")
    table.add_section()

    table.add_row("52주 위치", "90% 이상", "[red]빨간색[/]", "신고가 근접/초강세")
    table.add_row("", "80% 이상", "[orange3]주황색[/]", "상승세 우위")
    table.add_row("", "50% 이하", "[yellow]노란색[/]", "약세/바닥권 진입")
    table.add_row("", "30% 이하", "[blue]파란색[/]", "신저가 근접/침체")
    table.add_row("", "그 외 (50~80%)", "[white]흰색[/]", "중립")
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
    table.add_row("", "", "", "")
    table.add_row("", "SAR 는 추세가 끝났는지 아닌지 빠르게 알려주는 지표", "", "추세 유지/종료 판단용")
    table.add_row("", "SAR 전환 발생 + 종가 기준 EMA60 이탈 + RSI 60 이상", "", "추세 종료 확정")
    table.add_row("", "SAR 상승 중 + EMA60 위 + RSI 50~65", "", "보유")
    table.add_row("", "SAR 하향 전환 +  EMA60 유지 + RSI 55 ", "", "관망")
    table.add_row("", "SAR 하향 전환 +  EMA60 종가 이탈 + RSI 65 이상", "", "정리")
    table.add_row("", "SAR은 추세 없을 때 쓰면 안됨", "", "ADX 필수확인")
    table.add_section()

    table.add_row("추세SMO", "S (SAR)", "[red]⬆[/] / [blue]⬇[/]", "상승 / 하락")
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
    table.add_row("", "", "", "")
    table.add_row("", "SAR이 알려주는 전환이 과열/과매도 구간인지 확인", "", "SAR 전환 해석")
    table.add_row("", "RSI 70 이상 / 과매수구간", "", "SAR 전환시 단기고점/매수금지/분할매도 ")
    table.add_row("", "RSI 60 ~ 70 부근 / 강한상승구간", "", "SAR 전환시 조정가능성 높음/단기조정/횡보")
    table.add_row("", "RSI 50 ~ 60 부근 / 약한상승구간", "", "SAR 전환시 가짜신호 확률높음")
    table.add_row("", "RSI 40 ~ 50 부근 / 강세조정구간", "", "SAR 전환시 눌림후 재상승 가능성/분할매수")
    table.add_row("", "RSI 30 ~ 40 부근 / 약세진입구간", "", "SAR 전환시 하락추세 전환가능성 증가/신규매수금지")
    table.add_row("", "RSI 30 이하 / 과매도구간", "", "SAR 전환시 단기반등 시그널/신규매수금지")
    table.add_section()

    table.add_row("ADX", "0 ~ 15 미만", "[white]흰색[/]", "추세 없음 (횡보/박스권)")
    table.add_row("", "15 ~ 20 미만", "[yellow]노란색[/]", "추세 형성 중 (CCI 방향 확인)")
    table.add_row("", "20 ~ 30 미만", "[orange3]주황색[/]", "안정적 추세 (매매 최적)")
    table.add_row("", "30 ~ 40 미만", "[red]빨간색[/]", "강한 추세 (과열 주의)")
    table.add_row("", "40 이상", "[magenta]보라색[/]", "과열 (조정 주의)")
    table.add_row("", "", "", "")
    table.add_row("", "ADX가 지금 SAR를 써도 되는지 알려줌", "", "SAR 신뢰도")
    table.add_row("", "ADX 15 미만", "", "SAR 사용금지")
    table.add_row("", "ADX 15 이상 20 미만 ", "", "주의")
    table.add_row("", "ADX 20 이상", "", "SAR 사용가능")
    table.add_section()

    cci_upper = config.INDICATOR_PARAMS["CCI_UPPER"]
    cci_lower = config.INDICATOR_PARAMS["CCI_LOWER"]

    table.add_row("CCI", f"CCI ≥ {cci_upper}", "[red]빨간색[/]", "과열 (추격 금물)")
    table.add_row("", f"0 < CCI < {cci_upper}", "[orange3]주황색[/]", "상승 방향시 (추세 매매)")
    table.add_row("", f"{cci_lower} < CCI < 0", "[yellow]노란색[/]", "상승 방향시 (반등 시도)")
    table.add_row("", f"CCI ≤ {cci_lower}", "[blue]파란색[/]", "과매도 (저점 탐색)")
    table.add_row("", "", "", "")
    table.add_row("", "SAR 타이밍 정밀화 용도로 사용", "", "SAR 해석")
    table.add_row("", "+100선 이상", "", "SAR 추세 연장")
    table.add_row("", "0선 하향 + SAR 반전", "", "SAR 추세 종료")
    table.add_row("", "-100선 근처", "", "하락 가속 가능")
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
    ma_p = regime.get('REGIME_MA_PERIOD', 60)
    adx_th = regime.get('REGIME_ADX_THRESHOLD', 20)
    
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
        
            r_map = {"Bull": "[red]강세장[/]", "Bear": "[blue]약세장[/]", "Sideways": "[yellow]횡보장[/]"}
            k_r_str = r_map.get(kospi_regime, kospi_regime) + "*"
            q_r_str = r_map.get(kosdaq_regime, kosdaq_regime) + "*"

            market_status_info = {
                "kospi_str": k_r_str, "kospi_adj": kospi_adj,
                "kosdaq_str": q_r_str, "kosdaq_adj": kosdaq_adj
            }
            
            # [추가] 실시간 필터링 상태 계산
            if getattr(config, 'USE_MARKET_FILTER', True):
                filter_info = {}
                ma_period_filter = getattr(config, 'MARKET_FILTER_MA', 50)
                for m_type in ["KOSPI", "KOSDAQ"]:
                    try:
                        df = analysis.get_domestic_index_data(m_type)
                        if df is not None and not df.empty and len(df) >= ma_period_filter:
                            ma_val = df['close'].rolling(window=ma_period_filter).mean().iloc[-1]
                            current_idx = df['close'].iloc[-1]
                            filter_info[m_type] = current_idx >= ma_val
                    except:
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

    # 1. Trend Factor
    score_table.add_row("Trend Factor", "현재가 > 20일선", f"+{0.5 * r_trend:.1f}", "단기 지지")
    score_table.add_row("(추세 4.0)", "EMA 5일선 > 20일선", f"+{0.5 * r_trend:.1f}", "5일선이 20일선 상회 (단기 추세 전환)")
    score_table.add_row("", "20/60/120선 정배열", f"+{1.0 * r_trend:.1f}", "중장기 이평선 정배열 (0.5+0.5)")
    score_table.add_row("", "주가 > 60선 돌파 or 단기급등", f"+{0.5 * r_trend:.1f}", "역배열 초기 돌파 또는 주가>5>20>60 정배열 급등")
    score_table.add_row("", "MACD > Signal", f"+{0.5 * r_trend:.1f}", "골든크로스")
    score_table.add_row("", "MACD 히스토그램 개선", f"+{0.5 * r_trend:.1f}", "히스토그램 양수 또는 상승 (MACD 선행)")
    score_table.add_row("", "주가 > SAR", f"+{0.5 * r_trend:.1f}", "파라볼릭 매수")
    score_table.add_section()

    # 2. Momentum Factor
    score_table.add_row("Momentum Factor", "50 ≤ RSI ≤ 75", f"+{0.5 * r_mom:.1f}", "강세 구간 (주도주)")
    score_table.add_row("(모멘텀 2.5)", "30≤RSI<50 반등 or RSI≥60", f"+{0.5 * r_mom:.1f}", "바닥 반등 시도 또는 모멘텀 확장")
    score_table.add_row("", "CCI > 0", f"+{0.5 * r_mom:.1f}", "상승 추세")
    score_table.add_row("", "CCI -100 탈출 or CCI≥50", f"+{0.5 * r_mom:.1f}", "과매도권 탈출 또는 강한 상승 모멘텀 심화")
    score_table.add_row("", "+DI > -DI 교차", f"+{0.5 * r_mom:.1f}", "매수세가 매도세 역전 (모멘텀 발생)")
    score_table.add_section()

    # 3. Strength & Volume
    score_table.add_row("Strength/Volume", "ADX ≥ 20", f"+{0.5 * r_str:.1f}", "추세 형성 확인")
    score_table.add_row("(강도/수급 1.5)", "거래량 폭증 or 5일>20일 추세상승", f"+{0.5 * r_str:.1f}", "단기 거래량 모멘텀 개선 (안정적 수급)")
    score_table.add_row("", "OBV 상승 or 스마트머니", f"+{0.5 * r_str:.1f}", "보조 지표 및 메이저 수급 턴어라운드")
    score_table.add_section()

    # 4. Synergy Bonus
    score_table.add_row("Synergy Bonus", "주가>60선 + MACD골든 + ADX≥15", f"+{1.0 * r_syn:.1f}", "추세 시작 시너지")
    score_table.add_row("(가산점 2.0)", "MACD골든 + RSI강세 + OBV", f"+{1.0 * r_syn:.1f}", "모멘텀 폭발 (Thrust)")
    score_table.add_section()

   # [병합] 점수대별 의미
    score_table.add_section()
    score_table.add_row("점수대별 의미", "8.5 ~ 10.0점", "[red]매수[/]", "강력 매수. 모든 지표 상승 및 상관관계 완벽. 비중 확대 가능.")
    score_table.add_row("", "7.0 ~ 8.0점", "[orange3]상승[/]", "매수. 추세 확실하나 일부 지표 후행. 분할 매수 권장.")
    score_table.add_row("", "5.5 ~ 6.5점", "[white]관망[/]", "관망/준비. 상승 초입 또는 추세 약화. 7점대 진입 대기.")
    score_table.add_row("", "5.0점 미만", "[blue]매도[/]", "매도/진입 금지. 하락 추세 또는 방향성 없는 횡보장.")
    
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
    score_table.add_row(f"적응형 임계값 ({adaptive_status})", f"강세장: 지수 > EMA {ma_p}일선 & 이평선우상향 & ADX≥{adx_th}", "[red]완화[/]", f"매수 기준 {regime['BULL_SCORE_ADJ']:+.1f}점 적용")
    score_table.add_row("", f"약세장: 지수 < EMA {ma_p}일선", "[blue]강화[/]", f"매수 기준 {regime['BEAR_SCORE_ADJ']:+.1f}점 적용")
    score_table.add_row("", "횡보장: 그 외 구간", "[yellow]유지[/]", f"매수 기준 {regime['SIDEWAYS_SCORE_ADJ']:+.1f}점 적용")
    
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
    ma_period = getattr(config, 'MARKET_FILTER_MA', 50)
    score_table.add_row(f"시장 필터링 ({filter_status})", f"KOSPI/KOSDAQ 지수 < SMA {ma_period}일 이평선", "[blue]보류[/]", "하락장 감지 시 신규 매수 중단")
    
    if filter_info is None and getattr(config, 'USE_MARKET_FILTER', True):
        score_table.add_row("현재 필터링 상태", "확인 불가", "-", "-")
    elif filter_info:
        k_stat = "[green]허용[/]" if filter_info.get("KOSPI", True) else "[red]보류[/]"
        q_stat = "[green]허용[/]" if filter_info.get("KOSDAQ", True) else "[red]보류[/]"
        score_table.add_row("현재 필터링 상태", f"KOSPI: {k_stat} / KOSDAQ: {q_stat}", "-", "실시간 필터링 적용 여부")

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
    
    use_mr = config.ANALYSIS_THRESHOLDS.get("USE_MEAN_REVERSION", True)
    mr_status = "[green]ON[/green]" if use_mr else "[red]OFF[/red]"
    mr_disp = config.ANALYSIS_THRESHOLDS.get("MR_DISPARITY_MAX", 90.0)
    mr_rsi = config.ANALYSIS_THRESHOLDS.get("MR_RSI_MAX", 40.0)
    mr_vol = config.ANALYSIS_THRESHOLDS.get("MR_VOL_STRENGTH", 120.0)
    score_table.add_row(f"매수 - 역추세 ({mr_status})", f"이격도 ≤ {mr_disp}% & RSI ≤ {mr_rsi} 반등 & 체결 > {mr_vol}%", "[magenta]역매수[/]", "낙폭과대 기술적 반등 노리기")
    
    use_super = config.ANALYSIS_THRESHOLDS.get("SUPER_MOMENTUM_USE", True)
    super_status = "[green]ON[/green]" if use_super else "[red]OFF[/red]"
    super_score = config.ANALYSIS_THRESHOLDS.get("SUPER_MOMENTUM_SCORE", 8.5)
    super_w52 = config.ANALYSIS_THRESHOLDS.get("SUPER_MOMENTUM_W52_POS", 90.0)
    super_buy_rsi = config.ANALYSIS_THRESHOLDS.get("SUPER_BUY_RSI_MAX", 75.0)
    super_sell_rsi = config.SELL_STRATEGY.get("SUPER_TAKE_PROFIT_RSI", 85.0)
    score_table.add_row(f"매수 - 슈퍼 모멘텀 ({super_status})", f"종합 점수 ≥ {super_score}점 & 52주 고점 {super_w52}% 이상 근접", "[magenta]강매수[/]", f"주도주 랠리 추종. 매수 RSI {super_buy_rsi}, 과열 매도 RSI {super_sell_rsi} 까지 허용")

    score_table.add_row("관망 - 상승", f"{rise_score}점 ≤ 종합 점수 < {buy_score}점", "[orange3]상승[/]", "상승 초입/지속 (대기/소량)")
    score_table.add_row("관망 - 중립", f"종합 점수 < {rise_score}점", "[white]관망[/]", "방향성 탐색 (거래 비권장)")
    
    # [추가] 매도 규칙 섹션
    score_table.add_section()
    sell_score = config.SELL_STRATEGY["SELL_SCORE"]
    stop_loss = config.SELL_STRATEGY["STOP_LOSS_RATE"]
    take_profit = config.SELL_STRATEGY["TAKE_PROFIT_RATE"]
    take_profit_rsi = config.SELL_STRATEGY["TAKE_PROFIT_RSI"]
    ts_activation = config.SELL_STRATEGY.get("TRAILING_STOP_ACTIVATION_RATE", 10.0)
    ts_callback = config.SELL_STRATEGY.get("TRAILING_STOP_CALLBACK_RATE", 3.0)
    use_atr = config.SELL_STRATEGY.get("USE_ATR_STOP", False)
    atr_mult = config.SELL_STRATEGY.get("ATR_STOP_MULTIPLIER", 2.0)
    half_tp_use = config.SELL_STRATEGY.get("HALF_TAKE_PROFIT_USE", True)
    time_stop_use = config.SELL_STRATEGY.get("TIME_STOP_USE", True)
    time_stop_days = config.SELL_STRATEGY.get("TIME_STOP_DAYS", 5)
    time_stop_min_profit = config.SELL_STRATEGY.get("TIME_STOP_MIN_PROFIT_RATE", 3.0)
    bep_activation = config.SELL_STRATEGY.get("BREAK_EVEN_PROFIT_RATE", 7.0)
    bep_stop = config.SELL_STRATEGY.get("BREAK_EVEN_STOP_RATE", 0.5)

    score_table.add_row("매도 - 익절", f"수익률 +{take_profit}% 도달", "[red]익절[/]", "목표 수익 달성 (최우선)")
    
    half_tp_status = "[green]ON[/green]" if half_tp_use else "[red]OFF[/red]"
    score_table.add_row(f"매도 - 반익절 ({half_tp_status})", f"수익률 +{take_profit/2:.1f}% 도달", "[red]반익절[/]", "절반(50%) 선매도로 수익 확보")
    
    fixed_sl_status = "[red]OFF[/red]" if use_atr else "[green]ON[/green]"
    score_table.add_row(f"매도 - 고정손절 ({fixed_sl_status})", f"손실률 {stop_loss}% 도달", "[blue]손절[/]", "손실 제한 (고정 손절)")
    
    atr_status = "[green]ON[/green]" if use_atr else "[red]OFF[/red]"
    score_table.add_row(f"매도 - ATR손절 ({atr_status})", f"매수가 - (ATR x {atr_mult})", "[blue]손절[/]", "변동성 기반 동적 손절")
    
    score_table.add_row("매도 - 본전청산", f"수익 +{bep_activation}% 달성 후 하락 시", "[blue]본전청산[/]", f"손실 방지 (손절선을 +{bep_stop}%로 끌어올림)")
    
    time_stop_status = "[green]ON[/green]" if time_stop_use else "[red]OFF[/red]"
    score_table.add_row(f"매도 - 시간청산 ({time_stop_status})", f"보유 {time_stop_days}일 경과 & 수익 < {time_stop_min_profit}% (최근 5일 고점 갱신 부재)", "[blue]시간청산[/]", "장기 횡보 종목 기회비용 보전")
    
    def_half_status = "[green]ON[/green]" if config.SELL_STRATEGY.get("DEFENSIVE_HALF_SELL_USE", True) else "[red]OFF[/red]"
    score_table.add_row(f"매도 - 방어적 반매도 ({def_half_status})", f"주가 < SAR & 주가 < 5일선 동시 이탈 시", "[blue]반매도[/]", "하락 반전 신호 감지 시 50% 덜어내기 (리스크 방어)")

    score_table.add_row("매도 - 트레일링", f"수익 {ts_activation}% 도달 후 고점 대비 하락 시", "[blue]매도[/]", "수익 보전 (ATR 사용 시 동적 변동폭 적용)")
    score_table.add_row("매도 - 과열", f"RSI > {take_profit_rsi}", "[red]익절[/]", "RSI 과열 시 이익 실현")
    score_table.add_row("매도 - 추세이탈", f"종합 점수 < {sell_score}점 or 위험 상태", "[blue]매도[/]", "추세 붕괴 시 청산")

    # [추가] 주문 집행 상세 섹션
    score_table.add_section()
    slippage_rate = getattr(config, 'SLIPPAGE_RATE', 0.003)
    if slippage_rate > 0:
        slippage_val = slippage_rate * 100
        score_table.add_row("주문 집행", "매수 주문 시", f"[red]+{slippage_val:.2f}%[/]", "체결 확률 확보 (현재가 + 슬리피지)")
        score_table.add_row("", "매도 주문 시", f"[blue]-{slippage_val:.2f}%[/]", "즉시 체결 유도 (현재가 - 슬리피지)")
    else:
        score_table.add_row("주문 집행", "매수 주문 시", "[dim]미사용[/]", "현재가로 주문 (슬리피지 없음)")
        score_table.add_row("", "매도 주문 시", "[dim]미사용[/]", "현재가로 주문 (슬리피지 없음)")
    
    use_vol = getattr(config, 'USE_VOLATILITY_TARGETING', True)
    use_risk = getattr(config, 'SYSTEM_RISK_PER_TRADE', 0) > 0
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
        except:
            pass

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
"""
    )
    parser.add_argument('--mode', choices=['1', '2', '3'], help='투자 모드 선택 (1: 모의투자, 2: 한투증권, 3: 토스증권)\n지정하지 않으면 실행 시 모드 선택 화면이 출력됩니다.')
    parser.add_argument('--auto', action='store_true', help='프로그램 시작 시 시스템 트레이딩 자동 실행 및 로그 뷰어 활성화')
    parser.add_argument('--no-bot', action='store_true', help='텔레그램 봇 명령어 수신(폴링) 비활성화 (알림 전송 기능은 유지)')
    args = parser.parse_args()

    # [추가] 로깅 설정 초기화
    config.setup_logging()

    # [추가] 프로그램 구동 시작 로그 기록 (mystock.log 생성 보장)
    logging.info("=== MyStock HTS 프로그램 구동 시작 ===")

    # [추가] 메인 스레드 ID 등록 (토큰 발급 권한 제어용)
    context.MAIN_THREAD_ID = threading.get_ident()

    # [수정] 초기화 로직 통합 및 사전 점검 추가
    # 1. 환경 설정 로드 (모드 선택)
    config.session.initialize(mode=args.mode)

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
        api.prefetch_watchlists_async() # [수정] 관심종목 예열도 초기화 이후로 지연

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
            grid.add_column(justify="left")
            grid.add_column(justify="left", style="dim")
            grid.add_row("[0] 시스템 설정", "(Settings)")
            grid.add_row("[1] 시장 지수 조회", "(Market Indices)")
            grid.add_row("[2] 종목 시세 분석", "(Stock Analysis)")
            grid.add_row("[3] 종목 차트 분석", "(Chart Analysis)")
            grid.add_row("[4] 전략 백테스팅", "(Backtesting)")
            grid.add_row("[5] 시스템 트레이딩", f"(System Trading){trader_status}")
            grid.add_row("[6] 종목 트랜드 분석", "(Stock Trend Analysis)")
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
                    "4": "전략 백테스팅", "5": "시스템 트레이딩", "6": "종목 트랜드 분석",
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
                            indices_list = market.ALL_INDICES
                            dict_list = [{'name': n, 'code': c} for n, c in indices_list]
                            idx, item = utils.search_stock_in_list(dict_list, title="시장 지수 목록")
                            if item:
                                target_name, target_code = item['name'], item['code']
                                target_ovs = True
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
                            
                            menu_items_type = [("1", "일봉", "Daily"), ("2", "시봉", "Hourly"), ("3", "분봉", "Intraday")]
                            c_type = utils.show_menu("차트 유형을 선택하세요", menu_items_type, default_choice="1")
                            
                            if c_type.lower() not in ['b', 'q']:
                                type_map = dict((k, v) for k, v, _ in menu_items_type)
                                context.USER_ACTION_BREADCRUMB.append(f"[{c_type}] {type_map.get(c_type, '')}")
                                
                                p_type = 'daily'
                                if c_type == '2': p_type = 'hourly'
                                elif c_type == '3': p_type = 'intraday'

                                # 일봉은 표시 기간 선택 (기본 6개월)
                                c_months = 6
                                if p_type == 'daily':
                                    menu_items_period = [("1", "6개월", "6 Months"), ("2", "1년", "1 Year")]
                                    c_period = utils.show_menu("표시 기간을 선택하세요", menu_items_period, default_choice="1")
                                    if c_period.lower() in ['b', 'q']:
                                        continue
                                    c_months = 12 if c_period == '2' else 6
                                    context.USER_ACTION_BREADCRUMB.append(f"[{c_period}] {'1년' if c_months == 12 else '6개월'}")

                                chart.generate_visual_chart(target_code, target_name, target_ovs, period_type=p_type, months=c_months)
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
                    # [추가] 치명적 오류 발생 시 텔레그램 긴급 알림 및 로그 전송
                    try:
                        from modules.auto_trade import get_mystock_log_tail
                        log_tail = get_mystock_log_tail(20)
                        msg = f"🛑 [치명적 시스템 오류] 메인 프로그램 강제 종료\n\n원인: {err_text}\n\n📜 [최근 시스템 로그 (mystock.log)]\n```\n{log_tail}```"
                        api.send_telegram_message(msg)
                    except: pass
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
