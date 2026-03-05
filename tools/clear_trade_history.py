# tools/clear_trade_history.py
import sys
import os
import sqlite3
from rich.console import Console
from rich.prompt import Prompt

# 프로젝트 루트 경로 추가
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import config

console = Console()

def clear_trade_history():
    console.print("[bold red]=== 거래 내역(History) 초기화 도구 ===[/bold red]")
    console.print("[dim]DB에 저장된 매매 내역을 영구적으로 삭제합니다.[/dim]\n")

    # 1. 대상 유형 선택 (모의/실전)
    console.print("[bold]초기화할 대상 유형을 선택하세요:[/bold]")
    console.print("[1] 모의투자 (Simulation)")
    console.print("[2] 실전투자 (Real)")
    
    choice = Prompt.ask("선택 [dim](취소: q)[/dim]", choices=["1", "2", "q"], default="1")
    if choice.lower() == 'q':
        return

    is_sim = 1 if choice == "1" else 0
    mode_label = "모의투자" if is_sim else "실전투자"

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

if __name__ == "__main__":
    try:
        clear_trade_history()
    except KeyboardInterrupt:
        console.print("\n[yellow]작업이 중단되었습니다.[/yellow]")