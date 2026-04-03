# tools/verify_autotrader_cancel.py
import sys
import os
import sqlite3
from datetime import datetime, timedelta
from unittest.mock import patch

# 프로젝트 루트 경로 추가
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import config
import api
from modules.auto_trade import AutoTrader, OrderStatus
from modules import db_manager
from rich.console import Console

console = Console()

def verify_autotrader_cancel_logic():
    console.print("[bold cyan]=== AutoTrader 미체결 강제 취소 로직 검증 ===[/bold cyan]")

    # 1. 초기화
    console.print("[dim]1. 시스템 초기화 (모의투자 모드)...[/dim]")
    config.session.initialize(mode="1")
    if not api.get_access_token():
        console.print("[red]토큰 발급 실패[/red]")
        return

    # AutoTrader 인스턴스 생성
    trader = AutoTrader()
    
    # 2. 테스트 주문 전송 (삼성전자, 현재가 -20%)
    code = "005930"
    name = "삼성전자"
    qty = 1
    
    console.print(f"[dim]   - {name}({code}) 현재가 조회 중...[/dim]")
    current_price = api.get_current_price(code, is_overseas=False)
    if current_price <= 0:
        console.print("[red]현재가 조회 실패[/red]")
        return
        
    order_price = int(current_price * 0.8)
    order_price = (order_price // 100) * 100 # 호가 단위 절삭
    
    console.print(f"[dim]2. 테스트 주문 전송: {qty}주 @ {order_price:,}원[/dim]")
    res = api.place_order("domestic", "buy", code, qty, order_price, "00")
    
    if res['rt_cd'] != '0':
        console.print(f"[red]주문 실패: {res['msg1']}[/red]")
        return
        
    odno = res['output']['ODNO']
    console.print(f"[green]주문 성공! 주문번호: {odno}[/green]")
    
    # 3. 로컬 상태 및 DB 설정 (과거 주문으로 조작)
    console.print("[dim]3. 로컬 상태 및 DB 조작 (5분 전 주문으로 설정)...[/dim]")
    
    # 3-1. AutoTrader 메모리 상태 등록 (OrderManager 사용)
    # OrderManager의 pending_orders에 직접 주입
    with trader.order_manager._lock:
        if code not in trader.order_manager.pending_orders:
            trader.order_manager.pending_orders[code] = {}
        trader.order_manager.pending_orders[code][odno] = OrderStatus.ORDER_SENT
    
    # 3-2. DB에 주문 내역 저장 (시간을 5분 전으로)
    past_time = (datetime.now() - timedelta(minutes=5)).strftime("%Y-%m-%d %H:%M:%S")
    
    # insert_trade 호출 (기본적으로 현재 시간으로 들어감)
    db_manager.db.insert_trade(
        "buy(AUTO)", code, name, qty, str(order_price), odno, 
        order_status="접수", reason="테스트주문"
    )
    
    # DB 시간 강제 업데이트 (직접 쿼리 실행)
    try:
        with sqlite3.connect(config.DB_FILE_PATH) as conn:
            cursor = conn.cursor()
            cursor.execute("UPDATE trades SET time = ? WHERE odno = ?", (past_time, odno))
            conn.commit()
        console.print(f"[dim]   - DB 시간 업데이트 완료: {past_time}[/dim]")
    except Exception as e:
        console.print(f"[red]DB 업데이트 실패: {e}[/red]")
        return
    
    # 4. API 모킹 및 로직 실행
    console.print("[dim]4. API 모킹 (미체결 내역 없음) 및 관리 로직 실행...[/dim]")
    
    # api.get_unfilled_orders가 빈 리스트를 반환하도록 패치하여
    # AutoTrader가 로컬 상태를 기반으로 취소를 시도하도록 유도
    with patch('api.get_unfilled_orders', return_value=[]):
        # AutoTrader의 미체결 관리 로직 실행
        trader.order_manager.manage_unfilled_orders()
        
    # 5. 결과 검증
    console.print("[dim]5. 결과 검증...[/dim]")
    
    # 5-1. pending_orders에서 제거되었는지 확인
    is_removed = False
    with trader.order_manager._lock:
        if code not in trader.order_manager.pending_orders or odno not in trader.order_manager.pending_orders[code]:
            is_removed = True
        
    if is_removed:
        console.print("[bold green]✅ 검증 성공: 로컬 메모리에서 주문이 정상적으로 제거되었습니다.[/bold green]")
        console.print("[dim]   (강제 취소 로직이 실행되어 상태가 정리됨)[/dim]")
    else:
        console.print("[bold red]❌ 검증 실패: 로컬 메모리에 주문이 여전히 남아있습니다.[/bold red]")
        console.print("[dim]   (강제 취소 로직이 실행되지 않았거나 실패함)[/dim]")
        
    # 5-2. 실제 취소 여부 확인 (API로 확인)
    console.print("[dim]   - 실제 취소 여부 확인 (중복 취소 시도)...[/dim]")
    # 이미 취소되었다면 "취소할 수량이 없습니다" 등의 메시지가 나와야 함
    cancel_res = api.revise_cancel_order("domestic", "cancel", odno, code, qty, "0", "02", "00")
    
    msg1 = cancel_res.get('msg1', '')
    msg_cd = cancel_res.get('msg_cd', '')
    
    # 40330000: 정정/취소할 수량이 없습니다 (이미 취소됨)
    if cancel_res['rt_cd'] != '0' and (msg_cd == '40330000' or "수량이 없습니다" in msg1):
         console.print(f"[bold green]✅ 검증 성공: 서버에서도 이미 취소된 것으로 확인됩니다.[/bold green]")
         console.print(f"[dim]   응답: {msg1} ({msg_cd})[/dim]")
    elif cancel_res['rt_cd'] == '0':
         console.print("[bold yellow]⚠️ 경고: 테스트 로직에선 취소되었다고 판단했으나, 실제로는 취소되지 않아 방금 취소되었습니다.[/bold yellow]")
         console.print("[dim]   (API 호출이 실제로 이루어지지 않았을 가능성 확인 필요)[/dim]")
    else:
         console.print(f"[red]확인 불가: {msg1} ({msg_cd})[/red]")

if __name__ == "__main__":
    try:
        verify_autotrader_cancel_logic()
    except KeyboardInterrupt:
        console.print("\n[yellow]테스트 중단됨[/yellow]")
    except Exception as e:
        console.print(f"\n[bold red]오류 발생: {e}[/bold red]")