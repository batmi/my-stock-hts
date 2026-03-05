# tools/search_indices_yfinance.py
import sys
import os
import yfinance as yf
from rich.console import Console
from rich.table import Table
from rich.prompt import Prompt

# 프로젝트 루트 경로 추가
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import config

console = Console()

# 주요 지수 목록 (yfinance 티커 기준)
MAJOR_INDICES = [
    {"ticker": "^KS11", "name": "KOSPI", "country": "South Korea"},
    {"ticker": "^KQ11", "name": "KOSDAQ", "country": "South Korea"},
    {"ticker": "^KS200", "name": "KOSPI200", "country": "South Korea"},
    {"ticker": "^DJI", "name": "Dow Jones Industrial Average", "country": "USA"},
    {"ticker": "^IXIC", "name": "NASDAQ Composite", "country": "USA"},
    {"ticker": "^GSPC", "name": "S&P 500", "country": "USA"},
    {"ticker": "^RUT", "name": "Russell 2000", "country": "USA"},
    {"ticker": "^VIX", "name": "CBOE Volatility Index", "country": "USA"},
    {"ticker": "^N225", "name": "Nikkei 225", "country": "Japan"},
    {"ticker": "^HSI", "name": "Hang Seng Index", "country": "Hong Kong"},
    {"ticker": "000001.SS", "name": "SSE Composite Index", "country": "China"},
    {"ticker": "^STI", "name": "STI Index", "country": "Singapore"},
    {"ticker": "^TWII", "name": "TSEC weighted index", "country": "Taiwan"},
    {"ticker": "^FTSE", "name": "FTSE 100", "country": "UK"},
    {"ticker": "^GDAXI", "name": "DAX PERFORMANCE-INDEX", "country": "Germany"},
    {"ticker": "^FCHI", "name": "CAC 40", "country": "France"},
    {"ticker": "^STOXX50E", "name": "ESTX 50 PR.EUR", "country": "Europe"},
    {"ticker": "^AORD", "name": "ALL ORDINARIES", "country": "Australia"},
    {"ticker": "^BSESN", "name": "S&P BSE SENSEX", "country": "India"},
    {"ticker": "^JKSE", "name": "Jakarta Composite Index", "country": "Indonesia"},
    {"ticker": "^KLSE", "name": "FTSE Bursa Malaysia KLCI", "country": "Malaysia"},
    {"ticker": "^NZ50", "name": "S&P/NZX 50 INDEX GROSS", "country": "New Zealand"},
    {"ticker": "^BVSP", "name": "IBOVESPA", "country": "Brazil"},
    {"ticker": "^MXX", "name": "IPC Mexico", "country": "Mexico"},
    {"ticker": "^MERV", "name": "MERVAL", "country": "Argentina"},
    {"ticker": "^TA125.TA", "name": "TA-125", "country": "Israel"},
    # Commodities / Futures
    {"ticker": "GC=F", "name": "Gold", "country": "Commodity"},
    {"ticker": "SI=F", "name": "Silver", "country": "Commodity"},
    {"ticker": "CL=F", "name": "Crude Oil", "country": "Commodity"},
    {"ticker": "NG=F", "name": "Natural Gas", "country": "Commodity"},
    {"ticker": "HG=F", "name": "Copper", "country": "Commodity"},
    {"ticker": "ZC=F", "name": "Corn", "country": "Commodity"},
    {"ticker": "ZW=F", "name": "Wheat", "country": "Commodity"},
    {"ticker": "ZS=F", "name": "Soybean", "country": "Commodity"},
    {"ticker": "CC=F", "name": "Cocoa", "country": "Commodity"},
    {"ticker": "KC=F", "name": "Coffee", "country": "Commodity"},
    {"ticker": "SB=F", "name": "Sugar", "country": "Commodity"},
    {"ticker": "CT=F", "name": "Cotton", "country": "Commodity"},
    # Currencies
    {"ticker": "DX-Y.NYB", "name": "US Dollar Index", "country": "Currency"},
    {"ticker": "KRW=X", "name": "USD/KRW", "country": "Currency"},
    {"ticker": "EURUSD=X", "name": "EUR/USD", "country": "Currency"},
    {"ticker": "JPY=X", "name": "USD/JPY", "country": "Currency"},
    {"ticker": "GBPUSD=X", "name": "GBP/USD", "country": "Currency"},
    {"ticker": "BTC-USD", "name": "Bitcoin USD", "country": "Crypto"},
    {"ticker": "ETH-USD", "name": "Ethereum USD", "country": "Crypto"},
]

def search_indices_yf():
    console.print("[bold cyan]=== yfinance 지수 검색 도구 ===[/bold cyan]")
    console.print("[dim]Yahoo Finance 기반의 주요 지수 목록을 제공합니다.[/dim]\n")

    while True:
        console.print("\n[bold]검색 옵션:[/bold]")
        console.print("[1] 전체 목록 보기")
        console.print("[2] 국가/카테고리별 검색")
        console.print("[3] 지수명/티커 검색")
        console.print("[4] 상세 정보 조회 (현재가/등락률)")
        console.print("[q] 종료")
        
        choice = Prompt.ask("선택", choices=["1", "2", "3", "4", "q"], default="1")
        
        if choice == 'q':
            break
        
        filtered = []
        
        if choice == '1':
            filtered = MAJOR_INDICES
        elif choice == '2':
            keyword = Prompt.ask("국가/카테고리 입력 (예: USA, Korea, Crypto)")
            if keyword:
                filtered = [i for i in MAJOR_INDICES if keyword.lower() in i['country'].lower()]
        elif choice == '3':
            keyword = Prompt.ask("검색어 입력")
            if keyword:
                filtered = [i for i in MAJOR_INDICES if keyword.lower() in i['name'].lower() or keyword.lower() in i['ticker'].lower()]
        elif choice == '4':
            ticker = Prompt.ask("조회할 티커 입력 (예: ^KS11)")
            if ticker:
                show_ticker_info(ticker)
                continue
        
        if filtered:
            print_indices(filtered)
        elif choice != '4':
            console.print("[yellow]검색 결과가 없습니다.[/yellow]")

def print_indices(indices):
    table = Table(show_header=True, header_style="bold magenta", box=None)
    table.add_column("Country", style="cyan")
    table.add_column("Name", style="white")
    table.add_column("Ticker", style="yellow")

    for item in indices:
        table.add_row(item['country'], item['name'], item['ticker'])
    
    console.print(table)

def show_ticker_info(ticker):
    try:
        with console.status(f"[green]{ticker} 정보 조회 중...[/green]"):
            t = yf.Ticker(ticker)
            info = t.fast_info
            last = info.last_price
            prev = info.regular_market_previous_close
            
            if last and prev:
                diff = last - prev
                rate = (diff / prev) * 100
                color = "[red]" if diff > 0 else ("[blue]" if diff < 0 else "[white]")
                console.print(f"\n[bold]{ticker}[/bold]")
                console.print(f"현재가: {last:,.2f}")
                console.print(f"등락: {color}{diff:+.2f} ({rate:+.2f}%)[/color]")
            else:
                console.print(f"[red]데이터를 가져올 수 없습니다.[/red]")
    except Exception as e:
        console.print(f"[red]오류 발생: {e}[/red]")

if __name__ == "__main__":
    try:
        search_indices_yf()
    except KeyboardInterrupt:
        console.print("\n[yellow]작업이 중단되었습니다.[/yellow]")