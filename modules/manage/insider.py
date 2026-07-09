# modules/manage/insider.py
"""관심종목 내부자 매매 동향 (OpenDART elestock/majorstock 기반).

- 임원·주요주주 특정증권등 소유상황 보고: 내부자의 장내 매수/매도 추적
- 대량보유(5%) 상황 보고: 주요 주주의 지분 변동 추적
"""
import concurrent.futures
from datetime import datetime, timedelta

from rich.table import Table
from rich import box
from rich.progress import Progress, SpinnerColumn, TextColumn, BarColumn

import config
import api
import utils


def _kr_stocks():
    """관심종목 중 국내 주식만 [(code, name), ...]. (ETF는 내부자 보고 대상 아님)"""
    out = []
    for s in config.session.stock_data.get("stocks_kr", []):
        if s.get("code"):
            out.append((s["code"], s.get("name", s["code"])))
    return out


def _fmt_date(yyyymmdd):
    s = str(yyyymmdd)
    return f"{s[:4]}-{s[4:6]}-{s[6:]}" if len(s) == 8 and s.isdigit() else s


def _fmt_chg(chg):
    """증감 수량 -> '+1,234' (증가=red, 감소=blue, 한국식 색상)."""
    if chg is None or chg == 0:
        return "[dim]-[/dim]"
    color = "red" if chg > 0 else "blue"
    return f"[{color}]{chg:+,.0f}[/]"


def _collect(code, name, cutoff):
    """단일 종목의 내부자/대량보유 보고를 cutoff 이후만 수집."""
    insiders, majors = [], []
    for r in api.get_dart_insider_trades(code, since=cutoff):
        if r["rcept_dt"] >= cutoff:
            r = dict(r, code=code, name=name)
            insiders.append(r)
    for r in api.get_dart_major_holdings(code):
        if r["rcept_dt"] >= cutoff:
            r = dict(r, code=code, name=name)
            majors.append(r)
    return insiders, majors


def show_insider_trades(days=90):
    """관심종목 내부자(임원·주요주주) 매매 및 대량보유 변동 조회."""
    utils.clear_screen()
    config.console.print(f"\n[bold cyan]🕵️ [관심종목 내부자 매매 동향] (최근 {days}일)[/bold cyan]\n")

    codes = _kr_stocks()
    if not codes:
        config.console.print("[yellow]등록된 국내 관심종목(주식)이 없습니다.[/yellow]")
        return
    if not config.DART_API_KEY:
        config.console.print("[yellow]※ DART API 키가 설정되지 않았습니다. (환경변수 DART_API_KEY)[/yellow]")
        return

    cutoff = (datetime.now() - timedelta(days=days)).strftime("%Y%m%d")
    insiders, majors = [], []
    with Progress(SpinnerColumn(), TextColumn("[progress.description]{task.description}"),
                  BarColumn(), console=config.console, transient=True) as progress:
        task = progress.add_task("[cyan]내부자 보고 조회 중...[/cyan]", total=len(codes))
        with concurrent.futures.ThreadPoolExecutor(max_workers=8) as ex:
            futs = [ex.submit(_collect, c, n, cutoff) for c, n in codes]
            for fut in concurrent.futures.as_completed(futs):
                try:
                    ins, majs = fut.result()
                    insiders.extend(ins)
                    majors.extend(majs)
                except Exception:
                    pass
                progress.advance(task)

    if not insiders and not majors:
        config.console.print(f"[dim]최근 {days}일간 내부자 매매/대량보유 변동 보고가 없습니다.[/dim]")
        return

    _render_summary(insiders)
    _render_insiders(insiders)
    _render_majors(majors)
    config.console.print("[dim]※ 증감(+)=취득, (-)=처분 · 무상신주·스톡옵션 등 매매 외 사유 포함 가능 · 원문: dart.fss.or.kr[/dim]")


def _render_summary(insiders):
    """종목별 임원·주요주주 순증감 합계 (신호 요약)."""
    if not insiders:
        return
    agg = {}
    for r in insiders:
        key = (r["code"], r["name"])
        a = agg.setdefault(key, {"net": 0.0, "cnt": 0})
        a["net"] += r["chg"] or 0
        a["cnt"] += 1
    rows = sorted(agg.items(), key=lambda kv: abs(kv[1]["net"]), reverse=True)

    config.console.print("[bold]▸ 종목별 내부자 순증감 요약[/bold]")
    table = Table(box=box.HORIZONTALS, header_style="dim", border_style="dim")
    table.add_column("종목", justify="left")
    table.add_column("보고 건수", justify="center")
    table.add_column("순증감(주)", justify="right")
    table.add_column("신호", justify="center")
    for (code, name), a in rows:
        net = a["net"]
        signal = ("[red]▲ 순취득[/]" if net > 0 else
                  "[blue]▼ 순처분[/]" if net < 0 else "[dim]중립[/dim]")
        table.add_row(f"{name} ({code})", str(a["cnt"]), _fmt_chg(net), signal)
    config.console.print(table)
    config.console.print()


def _render_insiders(insiders, limit=30):
    """임원·주요주주 소유상황 보고 상세 (최신순)."""
    config.console.print("[bold]▸ 임원 · 주요주주 소유상황 보고[/bold]")
    if not insiders:
        config.console.print("  [dim]해당 기간 보고가 없습니다.[/dim]\n")
        return
    insiders = sorted(insiders, key=lambda r: r["rcept_dt"], reverse=True)

    table = Table(box=box.HORIZONTALS, header_style="dim", border_style="dim")
    table.add_column("일자", justify="center")
    table.add_column("종목", justify="left")
    table.add_column("보고자", justify="left")
    table.add_column("직위/구분", justify="left")
    table.add_column("증감(주)", justify="right")
    table.add_column("보유(주)", justify="right")
    table.add_column("비율", justify="right")
    for r in insiders[:limit]:
        who = r["ofcps"] or r["main_shrholdr"] or "-"
        table.add_row(
            _fmt_date(r["rcept_dt"]),
            f"{r['name']} ({r['code']})",
            r["repror"] or "-",
            who,
            _fmt_chg(r["chg"]),
            f"{r['qty']:,.0f}" if r["qty"] is not None else "-",
            f"{r['rate']:.2f}%" if r["rate"] is not None else "-",
        )
    config.console.print(table)
    if len(insiders) > limit:
        config.console.print(f"  [dim]… 외 {len(insiders) - limit}건[/dim]")
    config.console.print()


def _render_majors(majors, limit=20):
    """대량보유(5%) 상황 보고 상세 (최신순)."""
    config.console.print("[bold]▸ 대량보유(5%) 상황 보고[/bold]")
    if not majors:
        config.console.print("  [dim]해당 기간 보고가 없습니다.[/dim]\n")
        return
    majors = sorted(majors, key=lambda r: r["rcept_dt"], reverse=True)

    table = Table(box=box.HORIZONTALS, header_style="dim", border_style="dim")
    table.add_column("일자", justify="center")
    table.add_column("종목", justify="left")
    table.add_column("보고자", justify="left")
    table.add_column("보고사유", justify="left")
    table.add_column("증감(주)", justify="right")
    table.add_column("보유비율", justify="right")
    table.add_column("비율증감", justify="right")
    for r in majors[:limit]:
        rate_chg = r["rate_chg"]
        rate_chg_str = "-"
        if rate_chg:
            color = "red" if rate_chg > 0 else "blue"
            rate_chg_str = f"[{color}]{rate_chg:+.2f}%p[/]"
        table.add_row(
            _fmt_date(r["rcept_dt"]),
            f"{r['name']} ({r['code']})",
            r["repror"] or "-",
            (r["reason"] or "-")[:20],
            _fmt_chg(r["chg"]),
            f"{r['rate']:.2f}%" if r["rate"] is not None else "-",
            rate_chg_str,
        )
    config.console.print(table)
    if len(majors) > limit:
        config.console.print(f"  [dim]… 외 {len(majors) - limit}건[/dim]")
    config.console.print()
