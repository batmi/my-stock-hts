# tools/clear_trade_history.py
import sys
import os
import sqlite3
from rich.console import Console
from rich.prompt import Prompt
from rich.table import Table
from rich import box

# 프로젝트 루트 경로 추가
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import config
from modules import db_manager

console = Console()

def clear_all_trades():
    console.print("[bold red]=== 거래 내역(History) 초기화 도구 ===[/bold red]")
    console.print("[dim]DB에 저장된 매매 내역을 영구적으로 삭제합니다.[/dim]\n")

    # 1. 대상 유형 선택 (폐기된 모의투자 기록 / 실전)
    console.print("[bold]초기화할 대상 유형을 선택하세요:[/bold]")
    console.print("[1] 옛 모의투자 기록 (폐기된 모드 · is_sim=1)")
    console.print("[2] 한투증권 (Real)")

    choice = Prompt.ask("선택 [dim](취소: q)[/dim]", choices=["1", "2", "q"], default="2")
    if choice.lower() == 'q':
        return

    is_sim = 1 if choice == "1" else 0
    mode_label = "옛 모의투자 기록" if is_sim else "한투증권"

    db_path = config.DB_FILE_PATH
    if not os.path.exists(db_path):
        console.print(f"[red]DB 파일을 찾을 수 없습니다: {db_path}[/red]")
        return

    try:
        with sqlite3.connect(db_path) as conn:
            cursor = conn.cursor()
            
            # 테이블 존재 여부 확인
            cursor.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='trades'")
            if not cursor.fetchone():
                console.print("[yellow]trades 테이블이 존재하지 않습니다. (데이터 없음)[/yellow]")
                return

            # 2. 해당 유형의 계좌 목록 조회
            cursor.execute("SELECT DISTINCT account FROM trades WHERE is_sim = ?", (is_sim,))
            rows = cursor.fetchall()
            
            # None이나 빈 문자열 계좌 처리
            accounts = [r[0] for r in rows if r[0]]
            
            if not accounts:
                console.print(f"\n[yellow]{mode_label} 유형으로 저장된 거래 내역이 없습니다.[/yellow]")
                return

            console.print(f"\n[bold]{mode_label} 내역이 존재하는 계좌 목록:[/bold]")
            for idx, acc in enumerate(accounts):
                console.print(f"[{idx+1}] {acc}")
            
            console.print(f"[{len(accounts)+1}] 전체 삭제 (해당 유형의 모든 계좌)")
            
            # 3. 초기화 대상 계좌 선택
            sel = Prompt.ask("\n초기화할 번호를 선택하세요 [dim](취소: q)[/dim]")
            if sel.lower() == 'q': return
            
            target_acc = None
            if sel.isdigit():
                idx = int(sel) - 1
                if 0 <= idx < len(accounts):
                    target_acc = accounts[idx]
                elif idx == len(accounts):
                    target_acc = "ALL"
            
            if not target_acc:
                console.print("[red]잘못된 번호입니다.[/red]")
                return

            # 4. 최종 확인 및 삭제
            target_disp = f"계좌 [{target_acc}]" if target_acc != "ALL" else f"모든 {mode_label} 계좌"
            
            console.print(f"\n[bold red]!!! 경고 !!![/bold red]")
            console.print(f"선택하신 [bold yellow]{target_disp}[/bold yellow]의 거래 내역이 영구적으로 삭제됩니다.")
            confirm = Prompt.ask("정말 삭제하시겠습니까?", choices=["y", "n"], default="n")
            
            if confirm.lower() == 'y':
                if target_acc == "ALL":
                    cursor.execute("DELETE FROM trades WHERE is_sim = ?", (is_sim,))
                else:
                    cursor.execute("DELETE FROM trades WHERE is_sim = ? AND account = ?", (is_sim, target_acc))
                
                deleted_rows = cursor.rowcount
                conn.commit()
                
                console.print(f"\n[bold green]삭제 완료: 총 {deleted_rows}건의 내역이 삭제되었습니다.[/bold green]")
                
                # DB 최적화 (선택 사항)
                console.print("[dim]DB 최적화(VACUUM) 진행 중...[/dim]")
                cursor.execute("VACUUM")
                console.print("[dim]완료.[/dim]")
            else:
                console.print("[dim]작업이 취소되었습니다.[/dim]")

    except Exception as e:
        console.print(f"[bold red]DB 작업 중 오류 발생: {e}[/bold red]")

def delete_individual_trade():
    choice = Prompt.ask("\n조회할 환경을 선택하세요 [1: 옛 모의투자 기록, 2: 한투증권, q: 취소]", choices=["1", "2", "q"], default="2")
    if choice == 'q':
        return
        
    is_sim = True if choice == "1" else False
    env_str = "옛 모의투자 기록" if is_sim else "한투증권"

    # 최근 50건 조회하여 표시
    trades = db_manager.db.get_trades(limit=50, is_sim=is_sim)
    if not trades:
        console.print(f"\n[yellow]{env_str}에 저장된 거래 내역이 없습니다.[/yellow]")
        return
        
    table = Table(title=f"최근 {env_str} 거래 내역 (최대 50건)", box=box.HORIZONTALS, header_style="dim", border_style="dim")
    table.add_column("ID", justify="right", style="cyan", width=4)
    table.add_column("일시", justify="center")
    table.add_column("유형", justify="center")
    table.add_column("종목명(코드)", justify="left")
    table.add_column("수량", justify="right")
    table.add_column("단가", justify="right")
    table.add_column("상태", justify="center")
    
    for t in trades:
        type_str = t['type']
        t_color = "red" if "buy" in type_str.lower() or "매수" in type_str else "blue"
        
        try: price_str = f"{int(float(t['price'])):,}"
        except: price_str = str(t['price'])
        
        table.add_row(
            str(t['id']),
            t['time'][5:16], # MM-DD HH:MM
            f"[{t_color}]{type_str}[/]",
            f"{t['name']} ({t['code']})",
            str(int(float(t['qty']))),
            price_str,
            t.get('order_status', '접수')
        )
        
    console.print()
    console.print(table)
    
    target_input = Prompt.ask("\n삭제할 내역의 [bold cyan]ID[/bold cyan]를 입력하세요 (쉼표로 다중 선택 가능) [dim](취소: q)[/dim]")
    if target_input.lower() == 'q':
        return
        
    target_ids = [tid.strip() for tid in target_input.split(',') if tid.strip()]
    if not target_ids or not all(tid.isdigit() for tid in target_ids):
        console.print("[red]ID는 쉼표로 구분된 숫자여야 합니다.[/red]")
        return
        
    target_ids = list(set(target_ids)) # 중복 제거
    target_trades = []
    not_found_ids = []
    
    for tid in target_ids:
        target_trade = next((t for t in trades if str(t['id']) == tid), None)
        if target_trade:
            target_trades.append(target_trade)
        else:
            try:
                conn = db_manager.db._get_conn()
                cursor = conn.cursor()
                cursor.execute("SELECT id, name, type, time FROM trades WHERE id = ?", (int(tid),))
                row = cursor.fetchone()
                if row:
                    target_trades.append(dict(row))
                else:
                    not_found_ids.append(tid)
            except Exception as e:
                console.print(f"[red]ID {tid} 조회 중 오류 발생: {e}[/red]")
                not_found_ids.append(tid)
                
    if not_found_ids:
        console.print(f"[yellow]다음 ID는 찾을 수 없어 제외됩니다: {', '.join(not_found_ids)}[/yellow]")
        
    if not target_trades:
        console.print("[red]유효한 삭제 대상이 없습니다.[/red]")
        return
        
    console.print(f"\n[bold]삭제 대상 ({len(target_trades)}건):[/bold]")
    for t in target_trades:
        console.print(f" - ID [[bold]{t['id']}[/bold]] [cyan]{t['name']} / {t['type']}[/cyan]")
        
    confirm = Prompt.ask("\n위 내역들을 정말 삭제하시겠습니까?", choices=["y", "n"], default="n")
    if confirm.lower() == 'y':
        success_count = 0
        for t in target_trades:
            if db_manager.db.delete_trade_by_id(int(t['id'])):
                success_count += 1
        console.print(f"\n[bold green]총 {success_count}건의 내역이 성공적으로 삭제되었습니다.[/bold green]")

def main():
    while True:
        console.print("\n[bold magenta]=== 거래 내역(Trade History) 삭제 관리 ===[/bold magenta]")
        grid = Table.grid(padding=(0, 2))
        grid.add_column(justify="left")
        grid.add_column(justify="left", style="dim")
        grid.add_row("[1] 전체 내역 일괄 삭제", "(Delete All Trades)")
        grid.add_row("[2] 개별 내역 선택 삭제", "(Delete Individual Trade)")
        console.print(grid)
        console.print()
        
        choice = Prompt.ask("선택 [dim](종료: q)[/dim]", choices=["1", "2", "q"], default="q")
        
        if choice.lower() == 'q':
            console.print("[dim]종료합니다.[/dim]")
            break
            
        if choice == "1":
            clear_all_trades()
        elif choice == "2":
            delete_individual_trade()

if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        console.print("\n[yellow]작업이 중단되었습니다.[/yellow]")