# test_unfilled_orders.py
import time
import sys
import os

# 프로젝트 루트 경로를 sys.path에 추가하여 모듈 임포트 가능하게 설정
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import config
import api
from rich.console import Console

console = Console()

def test_simulation_unfilled_orders():
    console.print("[bold cyan]=== 모의투자 미체결 내역 조회 테스트 ===[/bold cyan]")

    # 1. 모의투자 모드로 초기화
    console.print("[dim]시스템 초기화 중...[/dim]")
    # 모의투자 모드('1')로 강제 초기화
    config.session.initialize(mode="1") 
    
    if not config.session.is_simulation:
        console.print("[bold red]오류: 모의투자 모드(1)로 초기화되지 않았습니다.[/bold red]")
        return

    # API 토큰 발급
    console.print("[dim]API 토큰 발급 중...[/dim]")
    if not api.get_access_token():
        console.print("[bold red]토큰 발급 실패. 앱 키/시크릿 설정을 확인하세요.[/bold red]")
        return

    # 2. 테스트 종목 및 가격 설정
    # 삼성전자(005930)를 현재가보다 20% 낮게 주문하여 미체결 유도
    code = "005930" 
    console.print(f"[dim]{code} 현재가 조회 중...[/dim]")
    current_price = api.get_current_price(code, is_overseas=False)
    
    if current_price <= 0:
        console.print(f"[bold red]{code} 현재가 조회 실패. 장 운영 시간이 아니거나 API 오류입니다.[/bold red]")
        return
    
    # 호가 단위 맞추기 (100원 단위로 가정)
    test_price = int(current_price * 0.8) 
    test_price = (test_price // 100) * 100 
    
    qty = 1
    
    console.print(f"\n[yellow]테스트 주문 정보:[/yellow]")
    console.print(f"  - 종목: 삼성전자({code})")
    console.print(f"  - 현재가: {current_price:,}원")
    console.print(f"  - 주문가: {test_price:,}원 (현재가 대비 -20%)")
    console.print(f"  - 수량: {qty}주")
    
    # 3. 주문 전송
    console.print("\n[green]1. 매수 주문 전송 중...[/green]")
    # ord_dvsn="00" (지정가)
    res = api.place_order("domestic", "buy", code, qty, test_price, "00")
    
    if res['rt_cd'] != '0':
        console.print(f"[bold red]주문 실패: {res['msg1']} (Code: {res.get('msg_cd')})[/bold red]")
        return
        
    odno = res['output']['ODNO']
    console.print(f"[bold green]주문 접수 완료. 주문번호: {odno}[/bold green]")
    
    # 4. 대기 (서버 반영 시간)
    console.print("\n[dim]서버 반영 대기 중 (3초)...[/dim]")
    time.sleep(3)
    
    # 5. 미체결 내역 조회 (변경된 로직 검증)
    console.print("\n[green]2. 미체결 내역 조회 (api.get_unfilled_orders)...[/green]")
    # 내부적으로 모의투자일 때 inquire-daily-ccld (VTTC8001R) 호출
    unfilled_list = api.get_unfilled_orders()
    
    found_order = None
    console.print(f"조회된 미체결 건수: {len(unfilled_list)}")
    
    for order in unfilled_list:
        r_odno = order.get('odno')
        r_code = order.get('pdno')
        r_qty = int(order.get('rmn_qty', 0))
        
        if str(r_odno) == str(odno):
            found_order = order
            break
            
    if found_order:
        console.print(f"\n[bold cyan]✅ 테스트 성공! 미체결 내역에서 주문을 확인했습니다.[/bold cyan]")
        console.print(f"  - 주문번호: {found_order.get('odno')}")
        console.print(f"  - 종목명: {found_order.get('prdt_name')}")
        console.print(f"  - 주문단가: {int(found_order.get('ord_unpr', 0)):,}원")
        console.print(f"  - 미체결잔량: {found_order.get('rmn_qty')}주")
    else:
        console.print(f"\n[bold red]❌ 테스트 실패: 미체결 내역에서 주문번호({odno})를 찾을 수 없습니다.[/bold red]")
        console.print("[dim]참고: 모의투자 서버 지연이나 API 로직 문제일 수 있습니다.[/dim]")

    # 6. 테스트 주문 취소 (청소)
    if found_order:
        console.print("\n[green]3. 테스트 주문 취소 (정리)...[/green]")
        # 취소 주문: org_no, code, qty, price="0", type_cd="02"(취소), ord_dvsn="00"
        cancel_res = api.revise_cancel_order("domestic", "cancel", odno, code, qty, "0", "02", "00")
        
        if cancel_res['rt_cd'] == '0':
            console.print("[bold green]주문 취소 완료.[/bold green]")
        else:
            console.print(f"[bold red]주문 취소 실패: {cancel_res['msg1']}[/bold red]")
            console.print(f"[dim]HTS나 MTS에서 수동으로 취소해주세요. (주문번호: {odno})[/dim]")

if __name__ == "__main__":
    try:
        test_simulation_unfilled_orders()
    except KeyboardInterrupt:
        console.print("\n[yellow]테스트 중단됨[/yellow]")
    except Exception as e:
        console.print(f"\n[bold red]오류 발생: {e}[/bold red]")
