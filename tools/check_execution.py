# test/test_execution.py
import time
import sys
import os

# 프로젝트 루트 경로 추가
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import config
import api
from rich.console import Console

console = Console()

def test_simulation_execution():
    console.print("[bold cyan]=== 모의투자 체결 확인(Execution) 테스트 ===[/bold cyan]")

    # 1. 모의투자 모드 초기화
    config.session.initialize(mode="1")
    if not api.get_access_token():
        console.print("[bold red]토큰 발급 실패[/bold red]")
        return

    code = "005930" # 삼성전자
    qty = 1
    
    # 2. 시장가 매수 주문 (즉시 체결 유도)
    # 체결 확인 로직을 검증하려면 주문이 실제로 체결되어야 하므로 '시장가'로 주문합니다.
    console.print(f"\n[green]1. {code} 1주 시장가 매수 주문 전송...[/green]")
    # ord_dvsn="01" (시장가), price=0
    res = api.place_order("domestic", "buy", code, qty, 0, "01")
    
    if res['rt_cd'] != '0':
        console.print(f"[bold red]주문 실패: {res['msg1']}[/bold red]")
        return
        
    odno = res['output']['ODNO']
    console.print(f"[bold green]주문 접수 완료. 주문번호: {odno}[/bold green]")
    
    # 3. 체결 대기 (서버 반영)
    console.print("[dim]체결 반영 대기 중 (3초)...[/dim]")
    time.sleep(3)
    
    # 4. 체결 내역 조회 (api.get_today_history)
    console.print("\n[green]2. 체결 내역 조회 (api.get_today_history)...[/green]")
    # 모의투자: VTTC8001R (CCLD_DVSN="01") - 체결된 내역만 조회
    history = api.get_today_history(config.session.cano, config.session.acnt_prdt_cd)
    
    found = False
    if history.get('rt_cd') == '0':
        for item in history.get('output1', []):
            if item['odno'] == odno:
                ccld_qty = int(item.get('tot_ccld_qty', 0))
                if ccld_qty > 0:
                    console.print(f"[bold blue]✅ 체결 확인 성공![/bold blue]")
                    console.print(f"  - 주문번호: {item['odno']}")
                    console.print(f"  - 종목명: {item['prdt_name']}")
                    console.print(f"  - 체결수량: {ccld_qty}주")
                    console.print(f"  - 체결단가: {item['avg_prvs']}원")
                    found = True
                break
    
    if not found:
        console.print(f"[bold red]❌ 체결 내역 확인 실패 (주문번호: {odno})[/bold red]")
        console.print("[dim]미체결 상태이거나 API 응답 지연일 수 있습니다.[/dim]")

    # 5. 잔고 정리 (매도)
    if found:
        console.print("\n[green]3. 테스트 잔고 정리 (시장가 매도)...[/green]")
        time.sleep(1)
        api.place_order("domestic", "sell", code, qty, 0, "01")
        console.print("[dim]매도 주문 전송 완료[/dim]")

if __name__ == "__main__":
    try:
        test_simulation_execution()
    except KeyboardInterrupt:
        console.print("\n[yellow]테스트 중단됨[/yellow]")
    except Exception as e:
        console.print(f"\n[bold red]오류 발생: {e}[/bold red]")

