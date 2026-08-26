import sys
import os
import time
import concurrent.futures
from rich.console import Console
from rich.prompt import Prompt
from rich.table import Table

# 프로젝트 루트 경로 추가
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import config
import api
from modules import analysis
from core import indicators

console = Console()

def test_worker_real(stock):
    """실제 API를 호출하여 차트 데이터를 받아오고 지표를 계산하는 워커"""
    try:
        df = api.get_chart_data(stock['code'], is_overseas=False)
        if df is not None and not df.empty:
            # 실제 분석 시 발생하는 연산 부하 부여
            _ = indicators.calculate_indicators(df)
            return True
    except Exception:
        pass
    return False

def run_simulation():
    console.print("[bold cyan]=== 멀티스레드 성능 시뮬레이션 (종목 분석) ===[/bold cyan]")
    
    console.print("[dim]1. 한투증권 모드로 초기화 중... (한도: 20 TPS)[/dim]")
    config.session.initialize(mode="2")
    if not api.get_real_access_token():
        console.print("[red]토큰 발급 실패. 한투증권 API Key 등 환경변수를 확인하세요.[/red]")
        return
    thread_counts = [0] + list(range(1, 21)) # 0은 스레드 미사용(순차 처리)
    tps_limit = 20
    mode_name = "한투증권"
    sample_size = 100


    # 2. 테스트 종목 리스트 로드 (KOSPI 마스터 파일 사용)
    console.print(f"[dim]테스트용 종목 리스트 로드 중... (KOSPI 상위 {sample_size}개 샘플)[/dim]")
    full_stock_list = analysis._get_master_stock_list("KOSPI")
    if not full_stock_list:
        console.print("[red]종목 리스트 로드 실패.[/red]")
        return
        
    sample_stocks = full_stock_list[:sample_size]
    console.print(f"[green]테스트 종목 {sample_size}개 준비 완료.[/green]\n")

    results = []

    for max_workers in thread_counts:
        if max_workers == 0:
            console.print("스레드 [bold yellow]미사용[/bold yellow] (순차 처리)로 분석 시작...")
        else:
            console.print(f"스레드 [bold yellow]{max_workers:>2}[/bold yellow]개로 분석 시작...")
        
        start_time = time.time()
        success_count = 0
        
        if max_workers == 0:
            # 순차 처리 (단일 스레드)
            for stock in sample_stocks:
                if test_worker_real(stock):
                    success_count += 1
        else:
            # 멀티스레드 병렬 처리
            with concurrent.futures.ThreadPoolExecutor(max_workers=max_workers) as executor:
                futures = {executor.submit(test_worker_real, stock): stock for stock in sample_stocks}
                for future in concurrent.futures.as_completed(futures):
                    if future.result():
                        success_count += 1
        end_time = time.time()
        elapsed_time = end_time - start_time
        tps = success_count / elapsed_time if elapsed_time > 0 else 0
        
        results.append({
            "workers": max_workers,
            "elapsed": elapsed_time,
            "success": success_count,
            "tps": tps
        })
        
        console.print(f" -> 소요 시간: {elapsed_time:.2f}초 (초당 처리건수: {tps:.1f} TPS)\n")
        
        # 다음 테스트 전 API Rate Limit 초기화를 위한 쿨다운
        console.print("[dim]API 호출 쿨다운 (3초 대기)...[/dim]")
        time.sleep(3)

    # 결과 테이블 출력
    table = Table(title=f"스레드 개수별 분석 성능 결과 ({mode_name})", show_header=True, header_style="bold magenta")
    table.add_column("스레드 수 (Workers)", justify="center")
    table.add_column("소요 시간 (초)", justify="right")
    table.add_column("초당 처리 건수 (TPS)", justify="right")
    table.add_column("성공 건수", justify="right")

    # 가장 빠른 스레드 수 찾기
    best_time = float('inf')
    best_worker = 0

    for res in results:
        if res['elapsed'] < best_time:
            best_time = res['elapsed']
            best_worker = res['workers']

    for res in results:
        is_best = (res['workers'] == best_worker)
        color = "green" if is_best else "white"
        worker_str = "순차 처리" if res['workers'] == 0 else str(res['workers'])
        table.add_row(
            f"[{color}]{worker_str}[/{color}]",
            f"[{color}]{res['elapsed']:.2f} s[/{color}]",
            f"[{color}]{res['tps']:.1f} TPS[/{color}]",
            f"{res['success']}/{sample_size}"
        )

    console.print(table)
    best_worker_str = "순차 처리" if best_worker == 0 else f"{best_worker}개 스레드"
    console.print(f"\n[bold green]💡 최적의 처리 방식: {best_worker_str} (소요시간: {best_time:.2f}초)[/bold green]")
    console.print(f"[dim]* KIS API {mode_name} Rate Limit({tps_limit} TPS)의 영향을 받으므로 스레드 증가가 성능 향상으로 직결되지 않을 수 있습니다.[/dim]")
    console.print(f"[dim]* 초당 {tps_limit}건 제한에 걸릴 경우 발생하는 강제 지연(sleep) 오버헤드도 소요 시간에 포함되어 있습니다.[/dim]")

if __name__ == "__main__":
    run_simulation()