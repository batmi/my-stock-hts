#!/usr/bin/env python3
"""
모의투자 계좌 정보 상세 조회 도구
사용법: python tools/check_simulation_balance.py
"""
import sys
import os
import json
import time

# 프로젝트 루트 경로를 sys.path에 추가하여 모듈 임포트 가능하게 설정
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import config
import api
from rich.console import Console
from rich.syntax import Syntax

console = Console()

def print_json(data):
    try:
        json_str = json.dumps(data, indent=2, ensure_ascii=False)
        console.print(Syntax(json_str, "json", theme="monokai", word_wrap=True))
    except:
        console.print(str(data))

def main():
    console.print("[bold yellow]=== 모의투자 계좌 정보 진단 도구 ===[/bold yellow]")

    # 1. 설정 로드 (모의투자 모드 강제)
    config.session.initialize(mode='1') 
    config.session.load_stock_config()
    
    # 2. 토큰 발급
    console.print("\n[bold green][1] 토큰 발급 (모의투자)[/bold green]")
    if not api.get_access_token():
        console.print("[red]모의투자 토큰 발급 실패.[/red]")
        return

    cano = config.session.cano
    acnt = config.session.acnt_prdt_cd
    console.print(f"대상 계좌: {cano}-{acnt}")

    # 3. API 호출 테스트
    
    # (1) 주식잔고조회 (VTTC8434R)
    console.print("\n[bold green][2] 주식잔고조회 (inquire-balance / VTTC8434R)[/bold green]")
    # api.get_domestic_balance 내부에서 호출됨
    params = {
        "CANO": cano, "ACNT_PRDT_CD": acnt, "AFHR_FLPR_YN": "N", "OFL_YN": "N", 
        "INQR_DVSN": "02", "UNPR_DVSN": "01", "FUND_STTL_ICLD_YN": "N", 
        "FNCG_AMT_AUTO_RDPT_YN": "N", "PRCS_DVSN": "00", 
        "CTX_AREA_FK100": "", "CTX_AREA_NK100": ""
    }
    res = api.call_api("uapi/domestic-stock/v1/trading/inquire-balance", "domestic", "inquiry", "balance", params=params)
    print_json(res)

    # (2) 주문가능금액 조회 (VTTC8908R)
    console.print("\n[bold green][3] 주문가능금액 조회 (inquire-psbl-order / VTTC8908R)[/bold green]")
    res = api.get_deposit(cano, acnt)
    print_json(res)

if __name__ == "__main__":
    main()