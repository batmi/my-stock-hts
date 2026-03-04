#!/usr/bin/env python3
"""
예수금 관련 API 상세 조회 및 값 추적 도구
사용법: python tools/check_deposit_apis.py
"""
import sys
import os
import json
import time
import re

# 프로젝트 루트 경로를 sys.path에 추가하여 모듈 임포트 가능하게 설정
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import config
import api
from rich.console import Console
from rich.panel import Panel
from rich.syntax import Syntax

console = Console()

# 찾고자 하는 목표 값 (콤마 제거)
TARGET_VALUES = [4984138, "4984138", "4,984,138", 4934988, "4934988", "4,934,988"]
FOUND_LOGS = []

def print_json(data, title="JSON"):
    try:
        json_str = json.dumps(data, indent=2, ensure_ascii=False)
        # 목표 값 하이라이팅
        for val in TARGET_VALUES:
            s_val = str(val)
            if s_val in json_str:
                json_str = json_str.replace(f': "{s_val}"', f': "[bold red]"{s_val}"[/bold red]"')
                json_str = json_str.replace(f': {s_val}', f': [bold red]{s_val}[/bold red]')
        
        console.print(Syntax(json_str, "json", theme="monokai", word_wrap=True))
    except:
        console.print(str(data))

def intercept_request(method, url, *args, **kwargs):
    """API 요청을 가로채서 로그를 출력하고 실행하는 래퍼 함수"""
    
    # 1. Request 정보 출력
    console.print(f"\n[bold cyan]>>> REQUEST ({method})[/bold cyan] {url}")
    
    headers = kwargs.get('headers', {})
    # 민감 정보 마스킹
    masked_headers = headers.copy()
    if 'appKey' in masked_headers: masked_headers['appKey'] = masked_headers['appKey'][:5] + "***"
    if 'appSecret' in masked_headers: masked_headers['appSecret'] = "***"
    if 'authorization' in masked_headers: masked_headers['authorization'] = "Bearer ***"
    
    console.print("[dim]Headers:[/dim]")
    print_json(masked_headers)
    
    # TR_ID 검증
    tr_id = headers.get('tr_id')
    if "inquire-psbl-order" in url and tr_id != "TTTC8908R" and tr_id != "VTTC8908R":
        console.print(f"[bold red]!!! 경고: 주문가능금액 조회에 잘못된 TR_ID({tr_id})가 사용되었습니다. (예상: TTTC8908R) !!![/bold red]")
    elif "inquire-psbl-order" in url:
        console.print(f"[bold green]✓ 올바른 TR_ID({tr_id})가 사용되었습니다.[/bold green]")

    if kwargs.get('params'):
        console.print("[dim]Params:[/dim]")
        print_json(kwargs['params'])
    
    if kwargs.get('data'):
        console.print("[dim]Body:[/dim]")
        try:
            body_json = json.loads(kwargs['data'])
            print_json(body_json)
        except:
            console.print(kwargs['data'])

    # 2. 실제 요청 실행
    response = original_request(method, url, *args, **kwargs)
    
    # 3. Response 정보 출력
    console.print(f"\n[bold magenta]<<< RESPONSE ({response.status_code})[/bold magenta]")
    
    try:
        res_json = response.json()
        json_str = json.dumps(res_json, ensure_ascii=False)
        
        # 목표 값 포함 여부 확인
        found = False
        for val in TARGET_VALUES:
            if str(val).replace(',', '') in json_str.replace(',', ''):
                found = True
                break
        
        # 주문가능금액 필드 확인
        if "ord_psbl_amt" in json_str:
            found = True
            console.print(Panel(f"[bold green]✓ 주문가능금액 필드(ord_psbl_amt) 발견됨[/bold green]", border_style="green"))
        
        if found:
            console.print(Panel(f"[bold red]!!! 목표 값({TARGET_VALUES[0]}) 발견됨 !!![/bold red]", border_style="red"))
            FOUND_LOGS.append(f"URL: {url}\nTR_ID: {headers.get('tr_id')}")
        
        print_json(res_json)
        
    except Exception as e:
        console.print(f"[red]Response Parsing Error: {e}[/red]")
        console.print(response.text[:500])

    return response

# api.session.request 메서드를 몽키패치하여 가로채기
original_request = api.session.request
api.session.request = intercept_request

def main():
    console.print("[bold yellow]=== 예수금 관련 API 정밀 진단 도구 ===[/bold yellow]")
    console.print(f"찾는 값: [bold red]{TARGET_VALUES[0]}[/bold red]\n")

    # 1. 설정 로드 (실전투자 모드 강제)
    config.session.initialize(mode='2') 
    config.session.load_stock_config()
    
    # 2. 토큰 발급
    console.print("\n[bold green][1] 토큰 발급[/bold green]")
    if not api.get_real_access_token():
        console.print("[red]실전투자 토큰 발급 실패. 환경변수(REAL_APP_KEY 등)를 확인하세요.[/red]")
        return

    cano = config.session.cano
    acnt = config.session.acnt_prdt_cd
    console.print(f"대상 계좌: {cano}-{acnt}")

    # 3. API 호출 테스트
    
    # (1) 주문가능금액 조회 (inquire-psbl-order / TTTC8908R)
    console.print("\n[bold green][2] 주문가능금액 조회 (inquire-psbl-order)[/bold green]")
    api.get_deposit(cano, acnt)
    
    # (2) 주식잔고조회 (inquire-balance / TTTC8434R) - 조회구분 01 (대출일별)
    console.print("\n[bold green][3] 주식잔고조회 (inquire-balance, INQR_DVSN=01)[/bold green]")
    # api.get_domestic_balance 내부에서 INQR_DVSN='01'로 호출됨 (실전 모드 시)
    api.get_domestic_balance(cano, acnt)
    
    # (3) 계좌잔고평가 (inquire-account-balance / CTRP6548R 추정)
    # api.get_foreign_deposit 함수가 이 URL을 사용함
    console.print("\n[bold green][4] 계좌잔고평가 (inquire-account-balance)[/bold green]")
    api.get_foreign_deposit(cano, acnt)

    # (4) 해외주식 잔고 (inquire-balance / JTTT8801R)
    console.print("\n[bold green][5] 해외주식 잔고 (overseas/inquire-balance)[/bold green]")
    api.get_overseas_balance(cano, acnt)

    # 결과 요약
    console.print("\n" + "="*60)
    console.print("[bold]진단 결과 요약[/bold]")
    if FOUND_LOGS:
        console.print(f"[bold red]목표 값({TARGET_VALUES[0]})을 포함하는 API {len(FOUND_LOGS)}개를 찾았습니다![/bold red]")
        for log in FOUND_LOGS:
            console.print(f"- {log}")
    else:
        console.print(f"[yellow]목표 값({TARGET_VALUES[0]})을 반환하는 API를 찾지 못했습니다.[/yellow]")
        console.print("값이 포맷팅(예: 4984.138)되어 있거나 다른 필드에 있을 수 있습니다. 위 로그를 상세히 확인해주세요.")

if __name__ == "__main__":
    main()