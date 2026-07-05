# modules/auto_trade/menu.py
"""시스템 트레이딩 터미널 메뉴 (트레이딩 룰/제한 종목 관리 UI)

기존 modules/auto_trade.py 에서 분해. 외부 인터페이스는 패키지(__init__)가 재수출한다.
"""
import threading
import concurrent.futures
import logging
import time
import requests
import json
import jsonio
import os
import sqlite3 # [추가] DB 직접 접근용
from datetime import datetime, timedelta
from collections import Counter
from rich.prompt import Prompt
from rich.markup import escape
from rich.table import Table
from rich import box
from rich.progress import Progress, SpinnerColumn, BarColumn, TextColumn, TimeRemainingColumn
import config
import context # [추가]
import api
import utils
import indicators
from modules import analysis, account # [수정] account 모듈 재사용
import math # [추가] math 모듈
from modules import db_manager # [추가] DB 매니저
from modules import chart # [추가] 차트 모듈
import re # [추가] 정규식 모듈
import pandas as pd

from modules.auto_trade.common import (_enrich_rules_with_weights, add_restricted_stock, remove_restricted_stock)

console = config.console

logger = logging.getLogger(__name__)


def _pkg():
    """패키지(modules.auto_trade) 네임스페이스 접근자.

    분해 전에는 모듈 전역 조회였던 상호 호출을 패키지 속성 조회로 유지해,
    테스트의 patch('modules.auto_trade.X') 가 분해 전과 동일하게 내부 호출에도
    적용되도록 한다. (지연 import라 순환 없음)
    """
    import modules.auto_trade as _at
    return _at


def _select_stock_for_rules():
    """룰 설정을 위한 종목 선택 헬퍼"""
    menu_items = [
        ("1", "국내 주식", "Domestic Stock"), ("2", "국내 ETF", "Domestic ETF"),
        ("3", "미국 주식", "US Stock"), ("4", "미국 ETF", "US ETF"), ("5", "직접 입력", "Direct Input")
    ]
    choice = utils.show_menu("개별 설정할 대상을 선택하세요", menu_items, default_choice="5")
    if choice.lower() in ['b', 'q']: return None, None, False
    
    menu_map = dict((k, v) for k, v, _ in menu_items)
    context.USER_ACTION_BREADCRUMB.append(f"[{choice}] {menu_map.get(choice, '')}")

    code, name, is_overseas = None, None, False
    
    if choice == '5':
        utils.print_breadcrumb()
        raw_input = Prompt.ask("종목코드(6자리/티커) 입력 [dim](이전: b, 메인: q)[/dim]")
        if raw_input and raw_input.lower() not in ['b', 'q']:
            context.USER_ACTION_BREADCRUMB.append(f"[직접입력] {raw_input}")
            if raw_input.isdigit() and len(raw_input) == 6:
                code = raw_input
                name = api.get_stock_name_by_code(code, False) or code
                is_overseas = False
            else:
                code = raw_input.upper()
                name = api.get_stock_name_by_code(code, True) or code
                is_overseas = True
                
            if not utils.validate_and_confirm_stock(code, name, is_overseas, "이 종목을 선택하시겠습니까?"):
                return None, None, False
    elif choice in ["1", "2", "3", "4"]:
        key_map = {"1": "stocks_kr", "2": "etfs_kr", "3": "stocks_us", "4": "etfs_us"}
        s_list = config.session.stock_data.get(key_map[choice], [])
        if s_list:
            idx, item = utils.search_stock_in_list(s_list, title=f"{menu_map[choice]} 목록")
            if not item: return None, None, False
            code, name = item['code'], item['name']
            is_overseas = (choice in ["3", "4"])
            context.USER_ACTION_BREADCRUMB.append(f"[종목선택] {name}")
        else:
            console.print("[yellow]목록이 비어있습니다.[/yellow]")
            return None, None, False
            
    return code, name, is_overseas

def _view_stock_rules():
    """설정된 룰 조회"""
    custom_rules = db_manager.db.get_all_stock_strategies()
    custom_rules = _enrich_rules_with_weights(custom_rules) # [추가] 가중치 보강
    if not custom_rules:
        console.print("\n[yellow]설정된 개별 룰이 없습니다.[/yellow]")
        return

    table = Table(title="종목별 개별 트레이딩 룰 목록", box=box.HORIZONTALS, header_style="dim", border_style="dim")
    table.add_column("종목명(코드)", justify="left")
    table.add_column("매수(점수/RSI/체결/비대칭)", justify="center")
    table.add_column("청산(익절/TS/RSI/기한)", justify="center")
    table.add_column("리스크(비중/손절)", justify="center")
    table.add_column("가중치", justify="center")
    table.add_column("메모", justify="left", style="dim")
    table.add_column("수정일", justify="center", style="dim")
    
    for i, r in enumerate(custom_rules):
        w_str = "기본"
        if r.get('weights'):
            w = r['weights']
            # [Fix] 가중치가 JSON 문자열인 경우 딕셔너리로 변환
            if isinstance(w, str):
                try: w = json.loads(w)
                except Exception: pass
            
            if isinstance(w, dict):
                w_str = f"{w.get('TREND',0):.1f}/{w.get('MOMENTUM',0):.1f}/{w.get('STRENGTH',0):.1f}/{w.get('SYNERGY',0):.1f}"

        sl_str = f"ATR(x{r.get('atr_stop_multiplier', 2.0)})" if r.get('use_atr_stop') else f"{r['stop_loss']}%"
        ratio_str = f"{r.get('invest_ratio', config.settings.SYSTEM_INVEST_PER_STOCK) * 100:.0f}%"
        half_tp_str = " (반익절 O)" if r.get('half_take_profit_use', 1) else " (반익절 X)"

        table.add_row(
            f"{r['name']}({r['code']})",
            f"{r['buy_score']}점 / {r.get('buy_rsi', 65.0)} / {r.get('buy_vol_strength', config.ANALYSIS_THRESHOLDS.get('BUY_VOL_STRENGTH', 100.0))}% / {r.get('buy_ask_bid_ratio', config.ANALYSIS_THRESHOLDS.get('BUY_ASK_BID_RATIO', 1.0))}배",
            f"+{r['take_profit']}%{half_tp_str} / TS(+{r['ts_activation']}/-{r['ts_callback']}) / {r.get('take_profit_rsi', 75.0)} / {r.get('time_stop_days', 10)}일",
            f"{ratio_str} / {sl_str}",
            w_str,
            r.get('memo', ''),
            r['updated_at']
        )
        if (i + 1) % 5 == 0 and (i + 1) < len(custom_rules):
            table.add_section()
    console.print(table)

def _input_and_save_rule(code, name):
    """(내부함수) 룰 입력 및 저장 공통 로직"""
    utils.print_breadcrumb()
    console.print(f"[bold green]선택 종목: {name} ({code})[/bold green]")
    
    # [추가] 현재가 조회 (예상 가격 계산용)
    is_overseas = not (len(code) == 6 and code[0].isdigit() and code.isalnum())
    current_price = 0
    # [수정] 단순 조회이므로 status 사용
    with Progress(
        SpinnerColumn(),
        TextColumn("[progress.description]{task.description}"),
        BarColumn(),
        console=console,
        transient=True
    ) as progress:
        progress.add_task("[cyan]현재가 조회 중...[/cyan]", total=None)
        current_price = api.get_current_price(code, is_overseas)

    if current_price > 0:
        p_fmt = f"${current_price:,.2f}" if is_overseas else f"{int(current_price):,}원"
        console.print(f"[dim]현재가: {p_fmt} (기준)[/dim]")
    
    # 기존 설정 로드
    existing = db_manager.db.get_stock_strategy(code)
    if existing:
        existing = _enrich_rules_with_weights([existing])[0] # [추가] 가중치 보강

    defaults = {
        "buy_score": config.ANALYSIS_THRESHOLDS["BUY_SCORE"],
        "buy_rsi": config.ANALYSIS_THRESHOLDS["BUY_RSI_MAX"],
        "buy_vol_strength": config.ANALYSIS_THRESHOLDS.get("BUY_VOL_STRENGTH", 100.0), # [수정] 안전한 접근
        "auto_adjust_ask_bid_ratio": 1 if config.ANALYSIS_THRESHOLDS.get("AUTO_ADJUST_ASK_BID_RATIO", True) else 0,
        "buy_ask_bid_ratio": config.ANALYSIS_THRESHOLDS.get("BUY_ASK_BID_RATIO", 1.0),
        "sell_score": config.SELL_STRATEGY["SELL_SCORE"],
        "stop_loss": config.SELL_STRATEGY["STOP_LOSS_RATE"],
        "take_profit": config.SELL_STRATEGY["TAKE_PROFIT_RATE"],
        "take_profit_rsi": config.SELL_STRATEGY["TAKE_PROFIT_RSI"],
        "ts_activation": config.SELL_STRATEGY.get("TRAILING_STOP_ACTIVATION_RATE", 15.0),
        "ts_callback": config.SELL_STRATEGY.get("TRAILING_STOP_CALLBACK_RATE", 4.0),
        "memo": "",
        "weights": None,
        "invest_ratio": config.settings.SYSTEM_INVEST_PER_STOCK,
        "time_stop_days": config.SELL_STRATEGY.get("TIME_STOP_DAYS", 10),
        "use_atr_stop": 1 if config.SELL_STRATEGY.get("USE_ATR_STOP", True) else 0,
        "atr_stop_multiplier": config.SELL_STRATEGY.get("ATR_STOP_MULTIPLIER", 2.0),
        "half_take_profit_use": 1 if config.SELL_STRATEGY.get("HALF_TAKE_PROFIT_USE", True) else 0
    }
    
    current = existing if existing else defaults.copy()
    
    # [추가] 기존 데이터에 신규 필드가 누락된 경우 기본값으로 채움 (DB 스키마 변경 대응)
    for key, val in defaults.items():
        if key not in current:
            current[key] = val
            
    # DB에서 가져온 값이 None일 경우 빈 문자열로 처리
    if 'memo' not in current or current['memo'] is None:
        current['memo'] = ""
    
    console.print("\n[설정값 입력 (Enter: 현재값 유지, 이전: b, 메인: q)]")
    
    new_strategy = {}
    
    class QuitInput(Exception): pass

    def ask_val(key, desc, help_text, type_func):
        val = Prompt.ask(f"{desc} [dim](현재: {current[key]})\n[dim]{help_text}[/dim]", default=str(current[key]))
        if val.lower() in ['b', 'q']: raise QuitInput()
        return type_func(val)

    try:
        console.print("\n[bold]1. 기본 매수 타점 설정[/bold]")
        new_strategy['buy_score'] = ask_val('buy_score', f"매수 기준 점수 (기본: {defaults['buy_score']}점)", "이 점수 이상일 때 매수 진입 (지표 종합 점수)", float)
        new_strategy['buy_rsi'] = ask_val('buy_rsi', f"매수 허용 RSI 상한 (기본: {defaults['buy_rsi']})", "RSI가 이 값보다 낮아야 매수 (과열 방지)", float)
        if config.session.is_toss:
            # [추가] 토스: 체결강도 미제공 → 체결강도/자동연동은 미사용 고정(프롬프트 생략).
            #   수급 확인은 매도잔량비(ask_bid_ratio) 게이트로만 수행한다.
            new_strategy['buy_vol_strength'] = 0.0
            new_strategy['auto_adjust_ask_bid_ratio'] = 0
            console.print("[dim]토스증권: 체결강도 미제공 → 체결강도/자동연동 미사용(0). 수급은 매도잔량비로 판단합니다.[/dim]")
        else:
            new_strategy['buy_vol_strength'] = ask_val('buy_vol_strength', "매수 체결강도 기준(%) (기본: 100.0, 0: 미사용)", "수급 확인 (이 값 이상이어야 매수)", float)

            curr_auto = "y" if current.get('auto_adjust_ask_bid_ratio', defaults['auto_adjust_ask_bid_ratio']) else "n"
            val_auto = Prompt.ask(f"매도잔량비 자동 연동 (y: 사용 / n: 미사용) [dim](현재: {curr_auto})\n[dim]체결강도 설정값에 비례하여 최저 1.0배 자동 조정[/dim]", choices=["y", "n", "b", "q"], default=curr_auto)
            if val_auto.lower() in ['b', 'q']: raise QuitInput()
            new_strategy['auto_adjust_ask_bid_ratio'] = 1 if val_auto.lower() == 'y' else 0

        default_ratio = config.ANALYSIS_THRESHOLDS.get('BUY_ASK_BID_RATIO', 1.0)
        ratio_help = "수급 확인: 매도잔량/매수잔량 비율(이 값 이상이어야 매수)" if config.session.is_toss else "가짜 체결강도 방어 (체결강도 100% 기준 매도/매수잔량 비율)"
        new_strategy['buy_ask_bid_ratio'] = ask_val('buy_ask_bid_ratio', f"매수 매도잔량비 기준 (기본: {default_ratio}배, 0: 미사용)", ratio_help, float)

        console.print("\n[bold]2. 기본 청산 타점 설정[/bold]")
        new_strategy['take_profit'] = ask_val('take_profit', f"익절 수익률(%) (기본: {defaults['take_profit']}%)", "수익이 이 비율에 도달하면 이익 실현 (0: 미사용)", float)
        
        curr_half_tp = "y" if current.get('half_take_profit_use', defaults['half_take_profit_use']) else "n"
        val = Prompt.ask(f"반익절 사용 (y: 사용 / n: 미사용) [dim](현재: {curr_half_tp})\n[dim]목표 익절 수익률의 절반 도달 시 50% 선매도[/dim]", choices=["y", "n", "b", "q"], default=curr_half_tp)
        if val.lower() in ['b', 'q']: raise QuitInput()
        new_strategy['half_take_profit_use'] = 1 if val.lower() == 'y' else 0
        
        new_strategy['take_profit_rsi'] = ask_val('take_profit_rsi', f"익절 RSI 기준 (기본: {defaults['take_profit_rsi']})", "RSI가 이 값을 초과하면 과열로 판단하여 매도", float)
        new_strategy['sell_score'] = ask_val('sell_score', f"매도(추세이탈) 기준 점수 (기본: {defaults['sell_score']}점)", "점수가 이 값 미만으로 떨어지면 매도", float)
        new_strategy['ts_activation'] = ask_val('ts_activation', f"트레일링 스탑 발동 수익률(%) (기본: {defaults['ts_activation']}%)", "수익률이 이 값 이상일 때 트레일링 스탑 감시 시작", float)
        new_strategy['ts_callback'] = ask_val('ts_callback', f"트레일링 스탑 하락 감지율(%) (기본: {defaults['ts_callback']}%)", "최고가 대비 이 비율만큼 하락 시 매도", float)
        new_strategy['time_stop_days'] = ask_val('time_stop_days', f"시간 청산 기한(일) (기본: {defaults['time_stop_days']}일)", "매수 후 목표 기간 내 수익 미달 시 강제 청산 (0: 미사용)", int)
            
        console.print("\n[bold]3. 리스크 관리 및 자산 비중 설정[/bold]")
        curr_ratio_pct = current.get('invest_ratio', config.settings.SYSTEM_INVEST_PER_STOCK) * 100
        val = Prompt.ask(f"종목별 투자 비중(%) [dim](현재: {curr_ratio_pct:.0f})\n[dim]전체 자산 대비 이 종목에 투자할 비중 한도[/dim]", default=str(int(curr_ratio_pct)))
        if val.lower() in ['b', 'q']: raise QuitInput()
        new_strategy['invest_ratio'] = float(val) / 100.0
        
        curr_use_atr = "y" if current.get('use_atr_stop', 1 if config.SELL_STRATEGY.get("USE_ATR_STOP", True) else 0) else "n"
        val = Prompt.ask(f"손절 방식 (y: ATR 동적 손절 / n: 고정 손절률) [dim](현재: {curr_use_atr})\n[dim]종목의 변동성에 비례하여 손절폭 자동 계산 여부[/dim]", choices=["y", "n", "b", "q"], default=curr_use_atr)
        if val.lower() in ['b', 'q']: raise QuitInput()
        use_atr = (val.lower() == 'y')
        new_strategy['use_atr_stop'] = 1 if use_atr else 0
        
        if use_atr:
            new_strategy['atr_stop_multiplier'] = ask_val('atr_stop_multiplier', "ATR 손절 배수 (기본: 2.0)", "ATR 값의 몇 배를 손절폭으로 할지 설정 (0: 미사용)", float)
            new_strategy['stop_loss'] = current.get('stop_loss', defaults['stop_loss']) # 고정 손절률은 숨김
        else:
            new_strategy['stop_loss'] = ask_val('stop_loss', "손절 수익률(%) (기본: -7.0%)", "손실이 이 비율에 도달하면 손절매 (0: 미사용)", float)
            new_strategy['atr_stop_multiplier'] = current.get('atr_stop_multiplier', defaults['atr_stop_multiplier']) # 배수는 숨김
        
        # [추가] 가중치 설정 입력
        console.print("\n[bold]4. 스코어링 가중치 설정[/bold]")
        curr_weights = current.get('weights')
        
        # 4개 팩터(추세4/모멘텀2.5/강도1.5/시너지2.0=10) 입력. 합계는 정확히 10.0점이어야 한다.
        WEIGHT_FACTORS = [
            ("TREND", "추세 (TREND)"),
            ("MOMENTUM", "모멘텀 (MOMENTUM)"),
            ("STRENGTH", "강도 (STRENGTH)"),
            ("SYNERGY", "시너지 (SYNERGY)"),
        ]
        while True:
            # 현재 설정값 또는 전역 설정값 로드
            temp_weights = curr_weights.copy() if curr_weights else config.SCORING_WEIGHTS.copy()

            console.print("[dim]각 팩터의 가중치를 입력하세요. 합계가 정확히 10.0점이 되어야 합니다.[/dim]")
            console.print()

            def ask_weight(key, desc, default_val):
                v = Prompt.ask(f"{desc} [dim](현재: {default_val})[/dim]", default=str(default_val))
                if v.lower() in ['b', 'q']: raise QuitInput()
                return float(v)

            try:
                entered = {}
                for key, desc in WEIGHT_FACTORS:
                    entered[key] = ask_weight(key, desc, temp_weights.get(key, config.SCORING_WEIGHTS.get(key, 0.0)))
            except ValueError:
                console.print("[red]잘못된 입력입니다. 숫자를 입력해주세요.[/red]")
                continue

            total_score = round(sum(entered.values()), 2)
            if abs(total_score - 10.0) > 0.01:
                console.print(f"\n[bold red]경고: 가중치 합계가 {total_score:.1f}점입니다. (합계 10.0점)[/bold red]")
                console.print("[yellow]합계가 정확히 10.0점이 되도록 다시 입력해주세요.[/yellow]")
                curr_weights = entered  # 입력값 유지하여 재입력 시 보여줌
                continue

            new_strategy['weights'] = entered
            break

        # [추가] 메모 입력
        new_strategy['memo'] = ask_val('memo', "메모 (Memo)", "종목에 대한 투자 아이디어 및 참고사항", str)
        
        # [추가] 기본값과 동일 여부 확인
        if new_strategy == defaults:
            console.print(f"\n[yellow]입력된 설정이 시스템 기본값과 동일합니다.[/yellow]")
            if existing:
                console.print("[dim]변경된 내용이 없어 저장하지 않았습니다. (기존 룰 유지)[/dim]")
                console.print("[dim]기본값을 적용하려면 '삭제' 기능을 이용해주세요.[/dim]")
            else:
                console.print("[dim]별도의 개별 룰로 저장하지 않습니다. (기본 설정 자동 적용)[/dim]")
            return

        db_manager.db.save_stock_strategy(code, name, new_strategy)
        # [추가] 가중치 정보 별도 저장 (DB 스키마 보정)
        _pkg()._save_rule_weights(code, new_strategy.get('weights'))

        console.print(f"\n[bold green]'{name}' 종목의 트레이딩 룰이 저장되었습니다.[/bold green]")
        
        console.print()
        table = Table(title=f"[{name}] 설정 결과 요약", box=box.HORIZONTALS, show_header=True, header_style="dim", border_style="dim")
        table.add_column("구분", justify="center", style="cyan")
        table.add_column("설정값", justify="left")
        
        auto_str = "ON" if new_strategy.get('auto_adjust_ask_bid_ratio', 1) else "OFF"
        table.add_row("매수 타점", f"점수 {new_strategy['buy_score']}점↑ / RSI {new_strategy['buy_rsi']}↓ / 체결 {new_strategy['buy_vol_strength']}%↑ / 비대칭 {new_strategy['buy_ask_bid_ratio']}배↑ (자동연동: {auto_str})")
        half_tp_str = "ON" if new_strategy['half_take_profit_use'] else "OFF"
        table.add_row("청산 타점", f"익절 +{new_strategy['take_profit']}% (반익절: {half_tp_str}) / 과열 RSI {new_strategy['take_profit_rsi']}↑ / 시간청산 {new_strategy['time_stop_days']}일")
        table.add_row("트레일링", f"+{new_strategy['ts_activation']}% 도달 후 -{new_strategy['ts_callback']}% 하락 시")
        
        if new_strategy['use_atr_stop']:
            sl_disp = f"ATR 동적 손절 (배수: x{new_strategy['atr_stop_multiplier']})"
        else:
            sl_disp = f"고정 손절 ({new_strategy['stop_loss']}%)"
        
        table.add_row("리스크 관리", f"투자 비중 {new_strategy['invest_ratio']*100:.0f}% / {sl_disp}")
        
        w_disp = "기본 설정"
        if new_strategy.get('weights'):
            w = new_strategy['weights']
            w_disp = f"{w.get('TREND',0):.1f}/{w.get('MOMENTUM',0):.1f}/{w.get('STRENGTH',0):.1f}/{w.get('SYNERGY',0):.1f}"
        table.add_row("가중치 (추세/모멘텀/강도/시너지)", w_disp)
        table.add_row("메모", new_strategy['memo'])
        
        console.print(table)
        
    except QuitInput:
        console.print("\n[yellow]입력이 취소되었습니다.[/yellow]")
        return
    except ValueError:
        console.print("\n[red]잘못된 입력입니다. 숫자를 입력해주세요.[/red]")

def _set_stock_rules():
    """룰 설정 (신규/검색)"""
    code, name, _ = _pkg()._select_stock_for_rules()
    if not code: return False
    if _pkg()._input_and_save_rule(code, name) is False: return False

def _modify_stock_rules():
    """룰 변경 (기존 목록에서 선택)"""
    custom_rules = db_manager.db.get_all_stock_strategies()
    custom_rules = _enrich_rules_with_weights(custom_rules) # [추가] 가중치 보강
    if not custom_rules:
        console.print("\n[yellow]저장된 개별 룰이 없습니다.[/yellow]")
        return

    def _disp_func(i, r):
        sl_str = f"ATR(x{r.get('atr_stop_multiplier', 2.0)})" if r.get('use_atr_stop') else f"{r['stop_loss']}%"
        return f"[{i+1}] {r['name']} ({r['code']}) | 매수: {r['buy_score']}점 | 익절: +{r['take_profit']}% | 손절: {sl_str}"
        
    idx, target = utils.search_stock_in_list(custom_rules, title="변경할 룰을 선택하세요", display_func=_disp_func)
    if target:
        context.USER_ACTION_BREADCRUMB.append(f"[종목선택] {target['name']}")
        if _pkg()._input_and_save_rule(target['code'], target['name']) is False: return False
    else: return False

def _delete_stock_rules():
    """룰 삭제"""
    custom_rules = db_manager.db.get_all_stock_strategies()
    if not custom_rules:
        console.print("\n[yellow]삭제할 룰이 없습니다.[/yellow]")
        return

    idx, target = utils.search_stock_in_list(custom_rules, title="삭제할 룰을 선택하세요")
    if target:
        context.USER_ACTION_BREADCRUMB.append(f"[종목선택] {target['name']}")
        utils.print_breadcrumb()
        if Prompt.ask(f"정말 '{target['name']}'의 룰을 삭제하시겠습니까?", choices=["y", "n"], default="n") == "y":
            db_manager.db.delete_stock_strategy(target['code'])
            console.print(f"\n[bold green]삭제되었습니다.[/bold green]")
            
            console.print("\n[bold cyan]>> 현재 설정된 트레이딩 룰 리스트입니다.[/bold cyan]")
            _pkg()._view_stock_rules()
        else: return False
    else: return False

def _view_restricted_stocks():
    """트레이딩 제한 종목 목록 및 후행지표 조회"""
    data = _pkg().load_restricted_stocks()
    if not data:
        console.print("\n[yellow]트레이딩 제한 종목이 없습니다.[/yellow]")
        return

    console.print()
    title = "트레이딩 제한 종목"
    table = Table(title=title, box=box.HORIZONTALS, header_style="dim", border_style="dim")
    table.add_column("종목명", justify="left")
    table.add_column("코드", justify="center", style="dim")
    table.add_column("계좌종류", justify="center")
    table.add_column("계좌번호", justify="center")
    table.add_column("현재가", justify="right")
    table.add_column("등락률(등락폭)", justify="right")
    table.add_column("52주", justify="right")
    table.add_column("메모", justify="left")
    table.add_column("등록일", justify="center", style="dim")

    with Progress(
        SpinnerColumn(),
        TextColumn("[progress.description]{task.description}"),
        BarColumn(),
        TextColumn("[progress.percentage]{task.percentage:>3.0f}%"),
        TimeRemainingColumn(),
        console=console,
        transient=True
    ) as progress:
        # [수정] (종목 × 계좌 범위) 단위로 평탄화하여 글로벌/계좌별 제한을 각각 한 행으로 펼쳐 보여준다.
        #        기존에는 한 셀에 '\n'으로 묶어 2번째 줄부터 종목명·코드가 비어 보였다(해제 화면과 동일한 표기로 통일).
        entries = []  # {code, name, type, acc_str, memo, is_overseas, date}
        for code, info in data.items():
            name = info.get('name', code)
            is_overseas = info.get('is_overseas')
            if is_overseas is None:
                is_overseas = (len(code) != 6)
            code_date = info.get('date', '-')

            global_memo = info.get('memo', '')
            if global_memo:
                entries.append({
                    "code": code, "name": name, "type": "전체", "acc_str": "-",
                    "memo": global_memo, "is_overseas": is_overseas, "date": code_date,
                })

            for acc, acc_info in info.get('accounts', {}).items():
                if isinstance(acc_info, str):
                    a_type, a_memo, a_date = "지정계좌", acc_info, code_date
                else:
                    a_type, a_memo = acc_info.get("type", "지정계좌"), acc_info.get("memo", "")
                    a_date = acc_info.get("date") or code_date  # 스코프 날짜 없으면 최초 등록일로 폴백
                entries.append({
                    "code": code, "name": name, "type": a_type, "acc_str": acc.rstrip('-'),
                    "memo": a_memo, "is_overseas": is_overseas, "date": a_date,
                })

        task = progress.add_task("[cyan]데이터 조회 및 지표 계산 중...[/cyan]", total=len(entries))

        # [최적화] 동일 종목이 여러 범위(글로벌/계좌별)로 나뉠 수 있으므로 시세는 종목당 1회만 조회한다.
        price_cache = {}

        for e in entries:
            code = e["code"]
            is_overseas = e["is_overseas"]
            reg_date = e["date"]

            if code not in price_cache:
                price_str = diff_str = w52_str = "-"
                df = api.get_chart_data(code, is_overseas)
                if df is not None and not df.empty:
                    current_price = float(df.iloc[-1]['close'])
                    prev_price = float(df.iloc[-2]['close']) if len(df) > 1 else current_price
                    diff = current_price - prev_price
                    rate = (diff / prev_price) * 100 if prev_price > 0 else 0.0

                    price_str = f"{int(current_price):,}" if not is_overseas else f"{current_price:,.2f}"

                    c_color = "[red]" if diff > 0 else ("[blue]" if diff < 0 else "[white]")
                    if is_overseas:
                        diff_str = f"{c_color}{rate:+.2f}% ({diff:+.2f})[/]"
                    else:
                        diff_str = f"{c_color}{rate:+.2f}% ({int(diff):+})[/]"

                    w52_pos_val = 0.0
                    recent_df = df.tail(250)
                    h52 = recent_df['high'].max()
                    l52 = recent_df['low'].min()
                    if h52 > l52:
                        w52_pos_val = (current_price - l52) / (h52 - l52) * 100

                    w_color = "[white]"
                    if w52_pos_val >= 90: w_color = "[red]"
                    elif w52_pos_val >= 80: w_color = "[orange3]"
                    elif w52_pos_val <= 20: w_color = "[blue]"
                    w52_str = f"{w_color}{w52_pos_val:.1f}%[/]"
                price_cache[code] = (price_str, diff_str, w52_str)

            price_str, diff_str, w52_str = price_cache[code]

            table.add_row(
                e["name"], code, e["type"], e["acc_str"],
                price_str, diff_str, w52_str, e["memo"] or "-", reg_date,
            )
            progress.advance(task)

    console.print(table)

def _add_restricted_stock():
    """트레이딩 제한 종목 추가"""
    code, name, is_overseas = _pkg()._select_stock_for_rules()
    if not code: return False
    
    data = _pkg().load_restricted_stocks()
    if code in data:
        console.print(f"\n[yellow]이미 제한 목록에 있는 종목입니다.[/yellow]")
        utils.print_breadcrumb()
        if Prompt.ask("메모를 수정하시겠습니까?", choices=["y", "n"], default="y") == "n":
            return False
            
    utils.print_breadcrumb()
    console.print("\n[cyan]어떤 계좌에 제한을 적용하시겠습니까?[/cyan]")
    console.print("1. 전체 계좌 (Global)")
    console.print("2. 현재 시스템 트레이딩 계좌")
    console.print("3. 계좌 직접 입력")
    choice = Prompt.ask("선택", choices=["1", "2", "3"], default="1")
    
    cano, acnt, account_type = None, None, None
    if choice == "2":
        if getattr(config.session, 'is_toss', False):
            cano = config.session.cano
            acnt = config.session.acnt_prdt_cd
            account_type = "토스"
        elif config.session.is_simulation:
            cano = config.session.cano
            acnt = config.session.acnt_prdt_cd
            account_type = "모의"
        else:
            cano = getattr(config.session, 'auto_cano', config.session.cano)
            acnt = getattr(config.session, 'auto_acnt_prdt_cd', config.session.acnt_prdt_cd)
            account_type = "한투-자동"
    elif choice == "3":
        account_type = Prompt.ask("계좌종류 선택", choices=["모의", "한투-자동", "한투-수동", "토스"], default="한투-자동")
        cano = Prompt.ask("계좌 앞 8자리")
        acnt = Prompt.ask("계좌 뒤 2자리", default="01")
        
    utils.print_breadcrumb()
    memo = Prompt.ask("제한 사유(메모) 입력")
    
    add_restricted_stock(code, name, memo, is_overseas=is_overseas, cano=cano, acnt=acnt, account_type=account_type)
    
    if cano is not None:
        acnt_str = acnt if acnt is not None else ""
        account_str = f"{cano}-{acnt_str}".rstrip("-")
        console.print(f"\n[green]'{name}' 종목이 {account_str}({account_type}) 계좌 전용 제한 목록에 추가되었습니다.[/green]")
    else:
        console.print(f"\n[green]'{name}' 종목이 전체 계좌 트레이딩 제한 목록에 추가되었습니다.[/green]")
    
    console.print("\n[bold cyan]>> 현재 설정된 트레이딩 제한 종목 리스트입니다.[/bold cyan]")
    _pkg()._view_restricted_stocks()

def _remove_restricted_stock():
    """트레이딩 제한 종목 해제 (계좌 범위별 개별 해제)"""
    data = _pkg().load_restricted_stocks()
    if not data:
        console.print("\n[yellow]삭제할 종목이 없습니다.[/yellow]")
        return False

    # [수정] (종목 × 계좌 범위) 단위로 평탄화하여 계좌별 정밀 해제를 지원한다.
    #        등록은 글로벌/지정계좌 스코프로 이루어지므로 해제도 같은 단위여야,
    #        다계좌 운영 시 다른 계좌(또는 글로벌)의 제한이 함께 삭제되는 과다 삭제를 막는다.
    entries = []  # {code, name, scope('global'|'account'), cano, acnt, type, acc_str, memo, is_overseas, date}
    for code, info in data.items():
        name = info.get('name', code)
        is_overseas = info.get('is_overseas')
        if is_overseas is None:
            is_overseas = (len(code) != 6)
        code_date = info.get('date', '-')

        global_memo = info.get('memo', '')
        if global_memo:
            entries.append({
                "code": code, "name": name, "scope": "global",
                "cano": None, "acnt": None, "type": "전체", "acc_str": "-",
                "memo": global_memo, "is_overseas": is_overseas, "date": code_date,
            })

        for acc, acc_info in info.get('accounts', {}).items():
            if isinstance(acc_info, str):
                a_type, a_memo, a_date = "지정계좌", acc_info, code_date
            else:
                a_type, a_memo = acc_info.get("type", "지정계좌"), acc_info.get("memo", "")
                a_date = acc_info.get("date") or code_date  # 스코프 날짜 없으면 최초 등록일로 폴백
            # account_key 형식: "{cano}-{acnt}" (acnt가 빈 문자열일 수 있음)
            cano, _, acnt = acc.partition('-')
            entries.append({
                "code": code, "name": name, "scope": "account",
                "cano": cano, "acnt": acnt, "type": a_type, "acc_str": acc.rstrip('-'),
                "memo": a_memo, "is_overseas": is_overseas, "date": a_date,
            })

    console.print()
    table = Table(title="트레이딩 제한 해제 대상 목록", box=box.HORIZONTALS, header_style="dim", border_style="dim")
    table.add_column("No.", justify="right", style="cyan", width=4)
    table.add_column("종목명", justify="left")
    table.add_column("코드", justify="center", style="dim")
    table.add_column("계좌종류", justify="center")
    table.add_column("계좌번호", justify="center")
    table.add_column("현재가", justify="right")
    table.add_column("등락률(등락폭)", justify="right")
    table.add_column("52주", justify="right")
    table.add_column("메모", justify="left")
    table.add_column("등록일", justify="center", style="dim")

    # [최적화] 동일 종목이 여러 범위(글로벌/계좌별)로 나뉠 수 있으므로 시세는 종목당 1회만 조회한다.
    price_cache = {}

    with Progress(
        SpinnerColumn(),
        TextColumn("[progress.description]{task.description}"),
        BarColumn(),
        TextColumn("[progress.percentage]{task.percentage:>3.0f}%"),
        TimeRemainingColumn(),
        console=console,
        transient=True
    ) as progress:
        task = progress.add_task("[cyan]데이터 조회 및 지표 계산 중...[/cyan]", total=len(entries))

        for i, e in enumerate(entries):
            code = e["code"]
            is_overseas = e["is_overseas"]
            reg_date = e["date"]

            if code not in price_cache:
                price_str = diff_str = w52_str = "-"
                df = api.get_chart_data(code, is_overseas)
                if df is not None and not df.empty:
                    current_price = float(df.iloc[-1]['close'])
                    prev_price = float(df.iloc[-2]['close']) if len(df) > 1 else current_price
                    diff = current_price - prev_price
                    rate = (diff / prev_price) * 100 if prev_price > 0 else 0.0

                    price_str = f"{int(current_price):,}" if not is_overseas else f"{current_price:,.2f}"

                    c_color = "[red]" if diff > 0 else ("[blue]" if diff < 0 else "[white]")
                    if is_overseas:
                        diff_str = f"{c_color}{rate:+.2f}% ({diff:+.2f})[/]"
                    else:
                        diff_str = f"{c_color}{rate:+.2f}% ({int(diff):+})[/]"

                    w52_pos_val = 0.0
                    recent_df = df.tail(250)
                    h52 = recent_df['high'].max()
                    l52 = recent_df['low'].min()
                    if h52 > l52:
                        w52_pos_val = (current_price - l52) / (h52 - l52) * 100

                    w_color = "[white]"
                    if w52_pos_val >= 90: w_color = "[red]"
                    elif w52_pos_val >= 80: w_color = "[orange3]"
                    elif w52_pos_val <= 20: w_color = "[blue]"
                    w52_str = f"{w_color}{w52_pos_val:.1f}%[/]"
                price_cache[code] = (price_str, diff_str, w52_str)

            price_str, diff_str, w52_str = price_cache[code]

            table.add_row(
                str(i + 1), e["name"], code, e["type"], e["acc_str"],
                price_str, diff_str, w52_str, e["memo"] or "-", reg_date,
            )
            progress.advance(task)

    console.print(table)
    console.print()

    utils.print_breadcrumb()
    choice = Prompt.ask("해제할 번호 선택 (여러 개는 콤마로 구분) [dim](이전: b, 메인: q)[/dim]")
    if choice.lower() in ['b', 'q']: return False

    selected_indices = []
    for part in choice.split(","):
        part = part.strip()
        if part.isdigit():
            idx = int(part)
            if 1 <= idx <= len(entries):
                selected_indices.append(idx - 1)

    if not selected_indices:
        console.print("\n[red]유효한 번호가 없습니다.[/red]")
        return False

    removed_names = []
    for idx in sorted(set(selected_indices)):  # 중복 제거
        e = entries[idx]
        # [수정] 선택한 범위(글로벌/지정계좌)만 정밀 해제한다.
        #        remove_restricted_stock 내부에서 글로벌·계좌 사유가 모두 비면 종목 자체를 삭제한다.
        if e["scope"] == "global":
            remove_restricted_stock(e["code"])
        else:
            remove_restricted_stock(e["code"], cano=e["cano"], acnt=e["acnt"])
        removed_names.append(f"{e['name']}({e['type']})")

    if removed_names:
        context.USER_ACTION_BREADCRUMB.append(f"[해제] {', '.join(removed_names)}")
        console.print(f"\n[green]해제 완료 ({len(removed_names)}개): {', '.join(removed_names)}[/green]")
        
        console.print("\n[bold cyan]>> 현재 설정된 트레이딩 제한 종목 리스트입니다.[/bold cyan]")
        _pkg()._view_restricted_stocks()

def manage_stock_rules():
    """종목별 트레이딩 룰 관리 메뉴"""
    menu_items = [("1", "룰 조회", "View"), ("2", "룰 설정", "Set"), ("3", "룰 변경", "Modify"), ("4", "룰 삭제", "Delete")]
    choice = utils.show_menu("종목별 트레이딩 룰 관리 (Manage Stock Rules)", menu_items, default_choice="1")
    if choice.lower() in ['b', 'q']: return False
    
    menu_map_dict = dict((k, v) for k, v, _ in menu_items)
    context.USER_ACTION_BREADCRUMB.append(f"[{choice}] {menu_map_dict.get(choice, '')}")

    if choice == "1":
        _pkg()._view_stock_rules()
    elif choice == "2":
        if _pkg()._set_stock_rules() is False: return False
    elif choice == "3":
        if _modify_stock_rules() is False: return False
    elif choice == "4":
        if _delete_stock_rules() is False: return False

def manage_restricted_stocks_menu():
    """트레이딩 제한 종목 관리 메뉴"""
    menu_items = [("1", "제한 종목 조회", "List"), ("2", "제한 종목 추가", "Add"), ("3", "제한 종목 해제", "Remove")]
    choice = utils.show_menu("트레이딩 제한 종목 관리 (Restricted Stocks)", menu_items, default_choice="1")
    if choice.lower() in ['b', 'q']: return False
    
    menu_map_dict = dict((k, v) for k, v, _ in menu_items)
    context.USER_ACTION_BREADCRUMB.append(f"[{choice}] {menu_map_dict.get(choice, '')}")
    
    if choice == "1": 
        _pkg()._view_restricted_stocks()
    elif choice == "2": 
        if _add_restricted_stock() is False: return False
    elif choice == "3": 
        if _remove_restricted_stock() is False: return False

def system_trading_menu():
    """시스템 트레이딩 메뉴"""

    trader = _pkg().AutoTrader()

    base_breadcrumb_len = len(context.USER_ACTION_BREADCRUMB)
    last_choice = "3"
    while True:
        context.USER_ACTION_BREADCRUMB = context.USER_ACTION_BREADCRUMB[:base_breadcrumb_len]
        
        trader_status = ""
        if trader.is_running:
            if trader.is_market_open():
                trader_status = " [bold green](RUNNING"
            else:
                trader_status = " [bold yellow](WAITING"
        else:
            trader_status = " [bold red](STOPPED"
            
        menu_items = [
            ("1", "트레이딩 실행", "Start"), ("2", "트레이딩 중단", "Stop"), ("3", "트레이딩 상태", f"Status){trader_status}"),
            ("4", "트레이딩 평가", "Report"), ("5", "트레이딩 로그", "Log Viewer"),
            ("6", "종목별 트레이딩 룰", "Rule"), ("7", "트레이딩 제한 종목", "Restrict")
        ]
        
        try:
            choice = utils.show_menu("시스템 트레이딩 (System Trading)", menu_items, default_choice=last_choice)
            
            if choice.lower() in ['b', 'q']: return False
            if choice.lower() == 'h':
                if getattr(utils, 'show_help', None):
                    utils.show_help()
                    utils.pause()
                continue
            
            last_choice = choice
            
            menu_map = dict((k, v) for k, v, _ in menu_items)
            if choice in menu_map:
                context.USER_ACTION_BREADCRUMB.append(f"[{choice}] {menu_map[choice]}")
                
        except KeyboardInterrupt:
            console.print()
            return False

        logger.info(f"운영자 실행: {' - '.join(context.USER_ACTION_BREADCRUMB)}")
        
        if choice == "1":
            trader.start()
            utils.pause()
        elif choice == "2":
            trader.stop()
            utils.pause()
        elif choice == "3":
            trader.print_status()
            utils.pause()
        elif choice == "4":
            if trader.print_report() is not False: utils.pause()
        elif choice == "5":
            trader.view_log_file()
        elif choice == "6":
            if manage_stock_rules() is not False: utils.pause()
        elif choice == "7":
            if manage_restricted_stocks_menu() is not False: utils.pause()
