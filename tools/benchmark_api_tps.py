import sys
import os
import time
import concurrent.futures
import threading
from rich.console import Console
from rich.prompt import Prompt
from rich.table import Table
from rich.live import Live
from rich.panel import Panel
from rich.layout import Layout
from rich.align import Align
from rich import box
from collections import Counter

# 프로젝트 루트 경로 추가
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import config
import api
import constants
import utils

console = Console()

# [Fix 2026-08-09] 통계 갱신용 락. 종전에는 워커가 `with threading.Lock():` 을 썼는데,
#  이는 호출할 때마다 **새 락을 만들어** 잠그는 것이라 상호배제가 전혀 없다. 30스레드가
#  같은 카운터를 경합해 집계가 어긋났다(최종 표의 성공 3,518 과 TPS 27.74 가 서로 안 맞던 원인).
_stats_lock = threading.Lock()

# 실시간 통계를 위한 전역 변수
stats = {
    "success": 0,
    "rate_limit": 0,
    "other_errors": 0,
    "total": 0,
    "elapsed": 0.0,
    "tps": 0.0,
    "stop_flag": False,
    "error_details": Counter()
}

def make_request(session, url, headers, params):
    """ThrottledSession을 우회하여 직접 API를 요청하는 워커.

    [Fix 2026-08-09] 레이트리밋을 '기타 오류'로 세던 결함 둘을 고친다.
      1) KIS는 EGW00201(초당 거래건수 초과)을 **HTTP 500** 으로 내려준다. 종전에는 비200
         응답의 본문을 보지 않고 HTTP_500 으로만 분류해, 실제로는 전량 레이트리밋인데
         '호출 제한 0건 / 기타 오류 14,748건'으로 보고했다.
      2) 어댑터 재시도가 status_forcelist=[500,...] 이라 그 500을 예외로 바꿔 버렸다.
         그래서 에러 상세가 100% MaxRetriesExceeded 로 찍혔다 — 원인이 통째로 가려진다.
         (아래 benchmark_worker 에서 total=0 으로 봉인한다)
    """
    try:
        res = session.get(url, headers=headers, params=params, timeout=5)
        if res.status_code != 200:
            body = res.text[:300]
            if 'EGW00201' in body or 'EGW00215' in body:
                return "rate_limit", "EGW00201"
            console.log(f"[bold red]HTTP Error:[/bold red] {res.status_code} {body[:120]}", style="dim")
            return "other_errors", f"HTTP_{res.status_code}"

        data = res.json()
        if data.get('rt_cd') == '0':
            return "success", None
        msg_cd = data.get('msg_cd', 'UNKNOWN_API_ERROR')
        if msg_cd in ('EGW00201', 'EGW00215'):
            return "rate_limit", msg_cd
        error_details = f"rt_cd: {data.get('rt_cd')}, msg_cd: {msg_cd}, msg1: {data.get('msg1')}"
        console.log(f"[bold red]API Error:[/bold red] {error_details}", style="dim")
        return "other_errors", msg_cd
    except Exception as e:
        exc_name = type(e).__name__
        # Max retries exceeded 에러는 더 구체적으로 분류
        if 'Max retries exceeded' in str(e):
            exc_name = "MaxRetriesExceeded"
            
        # [수정] Max retries exceeded 에러는 화면에 너무 많이 출력되므로 로그 생략
        if exc_name != "MaxRetriesExceeded":
            console.log(f"[bold red]Request Exception:[/bold red] {e}", style="dim")
        return "other_errors", exc_name

def benchmark_worker():
    """지속적으로 API 요청을 보내는 워커 스레드 함수"""
    # 스레드별로 독립된 세션을 사용하여 경합 방지
    with api.requests.Session() as session:
        # [Fix 2026-08-09] 재시도 봉인(total=0). 500을 재시도 대상에 두면 EGW00201 응답이
        #  예외(MaxRetriesExceeded)로 바뀌어 본문을 볼 수 없고, 재전송이 섞여 전송률도 흐려진다.
        retry_strategy = api.requests.adapters.Retry(total=0, backoff_factor=0.1, status_forcelist=[])
        adapter = api.requests.adapters.HTTPAdapter(max_retries=retry_strategy)
        session.mount('https://', adapter)

        # 헤더 및 파라미터 사전 준비 (가벼운 현재가 조회 API 사용)
        tr_id = constants.TR_ID_CONFIG["domestic"]["quotations"]["price"]["real"]
        headers = utils.get_common_headers(tr_id)
        params = {"fid_cond_mrkt_div_code": "J", "fid_input_iscd": "005930"}
        url = f"{config.session.url_base}/{constants.API_URLS['DOMESTIC']['QUOTATIONS']['PRICE']}"

        while not stats["stop_flag"]:
            category, detail = make_request(session, url, headers, params)
            with _stats_lock:
                stats[category] += 1
                stats["total"] += 1
                # 에러 발생 시 상세 내역 카운트
                if detail:
                    stats["error_details"][detail] += 1

def generate_stats_table() -> Table:
    """실시간 통계 테이블 생성"""
    table = Table(title="Live Benchmark Stats", box=box.ROUNDED)
    table.add_column("Metric", justify="right", style="cyan", no_wrap=True)
    table.add_column("Value", justify="left", style="magenta")

    table.add_row("Elapsed Time", f"{stats['elapsed']:.2f} s")
    table.add_row("Total Requests", f"{stats['total']:,}")
    table.add_row("✅ Success", f"{stats['success']:,}")
    table.add_row("⏳ Rate Limited", f"{stats['rate_limit']:,}")
    table.add_row("❌ Other Errors", f"{stats['other_errors']:,}")
    table.add_row("🚀 Actual TPS", f"[bold green]{stats['tps']:.2f}[/bold green]")
    
    return table

def run_benchmark():
    console.print("[bold cyan]=== KIS API 서버 성능 벤치마크 (TPS) ===[/bold cyan]")
    console.print("[dim]이 도구는 클라이언트의 TPS 제한을 무시하고 서버의 최대 처리 성능을 측정합니다.[/dim]")
    
    mode = Prompt.ask("테스트할 투자 모드를 선택하세요 (1: 모의, 2: 실전)", choices=["1", "2", "q"], default="1")
    if mode == 'q': return

    if mode == "1":
        console.print("[dim]1. 모의투자 모드로 초기화 중...[/dim]")
        config.session.initialize(mode="1")
        if not api.get_access_token():
            console.print("[red]토큰 발급 실패. 모의투자 API Key 등 환경변수를 확인하세요.[/red]")
            return
        mode_name = "모의투자"
        num_workers = 10 # 모의서버는 부하에 약하므로 적당히
    else:
        console.print("[dim]1. 한투증권 모드로 초기화 중...[/dim]")
        config.session.initialize(mode="2")
        if not api.get_real_access_token():
            console.print("[red]토큰 발급 실패. 한투증권 API Key 등 환경변수를 확인하세요.[/red]")
            return
        mode_name = "한투증권"
        num_workers = 30 # 실전서버는 성능이 좋으므로 높게

    duration = int(Prompt.ask("테스트 지속 시간(초)을 입력하세요", default="20"))
    # [추가 2026-08-09] 워커 수 = 동시 연결 수(워커마다 독립 Session). 이 값이 결과를 크게
    #  바꾼다 — 30스레드 폭주에서는 성공 처리량이 고정 속도 측정(무릎 6 TPS)보다 훨씬 높게
    #  나온다. 유량 제한이 '초당 건수'만이 아니라 동시성에도 걸려 있다는 뜻이라, 값을 바꿔
    #  가며 재는 것이 이 도구의 용도다. 1 → 4 → 16 → 30 으로 훑으면 성능이 연결 수에
    #  비례하는지 바로 보인다.
    num_workers = int(Prompt.ask("동시 워커(=연결) 수", default=str(num_workers)))
    
    console.print(f"\n[bold green]벤치마크 시작: {mode_name} 서버 / {num_workers}개 스레드 / {duration}초 동안 실행[/bold green]")
    console.print("[yellow]Ctrl+C를 눌러 언제든지 중단할 수 있습니다.[/yellow]\n")

    # 통계 초기화
    global stats
    stats = {k: 0 if isinstance(v, (int, float)) else (Counter() if isinstance(v, Counter) else False) for k, v in stats.items()}

    start_time = time.time()
    
    layout = Layout(name="root")
    layout.split_row(Layout(name="left"), Layout(name="right"))
    layout["right"].update(Panel(f"Running for 0 / {duration} seconds...", border_style="green", title="Status"))

    with Live(layout, screen=True, redirect_stderr=False) as live:
        try:
            with concurrent.futures.ThreadPoolExecutor(max_workers=num_workers) as executor:
                for _ in range(num_workers):
                    executor.submit(benchmark_worker)

                while stats["elapsed"] < duration:
                    stats["elapsed"] = time.time() - start_time
                    if stats["elapsed"] > 0:
                        stats["tps"] = stats["success"] / stats["elapsed"]
                    
                    layout["left"].update(generate_stats_table())
                    layout["right"].update(Panel(f"Running for {int(stats['elapsed'])} / {duration} seconds...", border_style="green", title="Status"))
                    time.sleep(0.1)
        
        except KeyboardInterrupt:
            console.print("\n[yellow]사용자에 의해 벤치마크가 중단되었습니다.[/yellow]")
        finally:
            stats["stop_flag"] = True
    
    # 최종 결과 출력
    console.print("\n" + "="*50)
    console.print("[bold green]벤치마크 종료[/bold green]")
    
    final_table = Table(title="Final Benchmark Results", box=box.DOUBLE_EDGE, show_header=False)
    final_table.add_column("Metric", style="cyan")
    final_table.add_column("Value", style="bold")
    
    final_table.add_row("서버", mode_name)
    final_table.add_row("총 실행 시간", f"{stats['elapsed']:.2f} 초")
    final_table.add_row("동시 워커(연결) 수", f"{num_workers}")
    final_table.add_row("총 요청 수", f"{stats['total']:,}")
    offered = (stats['total'] / stats['elapsed']) if stats['elapsed'] > 0 else 0
    final_table.add_row("전송률 (offered)", f"{offered:.2f} /s")
    final_table.add_row("✅ 성공", f"{stats['success']:,}")
    final_table.add_row("⏳ 호출 제한", f"{stats['rate_limit']:,}")
    final_table.add_row("❌ 기타 오류", f"{stats['other_errors']:,}")
    success_rate = (stats['success'] / stats['total'] * 100) if stats['total'] > 0 else 0
    final_table.add_row("성공률", f"{success_rate:.2f} %")
    # [Fix] 최종 TPS는 라이브 루프의 마지막 스냅샷이 아니라 확정 집계로 다시 계산한다.
    final_tps = (stats['success'] / stats['elapsed']) if stats['elapsed'] > 0 else 0
    stats['tps'] = final_tps
    final_table.add_row("성공 처리량 (TPS)", f"[bold green]{final_tps:.2f}[/bold green]")

    console.print(final_table)

    if final_tps > 0:
        rl_pct = (stats['rate_limit'] / stats['total'] * 100) if stats['total'] else 0
        console.print(f"\n[bold]💡 결론: 전송 {offered:.1f}/s 를 밀어 넣어 "
                      f"[cyan]{final_tps:.1f} TPS[/cyan] 가 통과했습니다 "
                      f"(레이트리밋 {rl_pct:.1f}%, 워커 {num_workers}개).[/bold]")
        console.print("[dim]  ※ 이 도구는 '한도까지 밀어 넣었을 때의 처리량'을 잰다. 거부 없이 쓸 수 있는\n"
                      "     안전 속도가 궁금하면 tools/probe_kis_tps.py (고정 속도 스윕)를 쓸 것.[/dim]")
    else:
        console.print(f"\n[bold red]⚠️ 결론: 요청이 한 건도 성공하지 못했습니다. API Key 또는 네트워크 상태를 확인하세요.[/bold red]")
        
    # [추가] 에러 상세 내역 테이블 출력
    if stats['error_details']:
        console.print()
        error_table = Table(title="에러 상세 내역 (Error Breakdown)", box=box.ROUNDED, show_header=True, header_style="bold red")
        error_table.add_column("에러 코드 / 유형", justify="left", style="yellow")
        error_table.add_column("횟수", justify="right")
        error_table.add_column("비중", justify="right")
        
        total_errors = stats['rate_limit'] + stats['other_errors']
        
        for error, count in stats['error_details'].most_common():
            percentage = (count / total_errors * 100) if total_errors > 0 else 0
            error_table.add_row(error, f"{count:,}", f"{percentage:.2f}%")
            
        console.print(error_table)

if __name__ == "__main__":
    run_benchmark()