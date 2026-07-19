# modules/manage/insider.py
"""관심종목 수급 · 물량 신호 종합 (OpenDART 기반).

- 자기주식 취득/처분/신탁계약 결정: 회사 단위 수급 신호 (내부자 개인 매매보다 강함)
- 메자닌(CB/BW/EB) 오버행: 전환가 vs 현재가로 잠재 매도물량 감시 (+전환청구권행사 감지)
- 무상증자 결정: 단기 모멘텀 이벤트
- 임원·주요주주 소유상황 / 대량보유(5%) 보고: 내부자 지분 변동 추적
"""
import concurrent.futures
from datetime import datetime, timedelta

from rich.table import Table
from rich import box
from rich.progress import Progress, SpinnerColumn, TextColumn, BarColumn

import config
import api
import utils

_MEZZ_LOOKBACK_DAYS = 365 * 3   # 메자닌 오버행 발행 이력 조회 기간 (통상 만기 3~5년)
_EXERCISE_LOOKBACK_DAYS = 90    # 전환청구권/신주인수권 행사 공시 감지 기간


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


def _collect_supply(code, name, days):
    """단일 종목의 자기주식/무상증자/메자닌 오버행 신호 수집."""
    end = datetime.now().strftime("%Y%m%d")
    bgn = (datetime.now() - timedelta(days=days)).strftime("%Y%m%d")

    treasury = [dict(r, code=code, name=name)
                for r in api.get_dart_treasury_decisions(code, bgn, end)]
    frees = [dict(r, code=code, name=name)
             for r in api.get_dart_free_increase_detail(code, bgn, end)]

    # 메자닌 오버행: 최근 3년 발행 결정 + 전환가 vs 현재가
    mezz = []
    mezz_bgn = (datetime.now() - timedelta(days=_MEZZ_LOOKBACK_DAYS)).strftime("%Y%m%d")
    for kind in ("CB", "BW", "EB"):
        for r in api.get_dart_bond_issue_detail(code, mezz_bgn, end, kind=kind):
            amt = _num(r.get("bd_fta"))
            prc = _num(r.get("cv_prc")) or _num(r.get("ex_prc"))
            if not amt or not prc or prc <= 0:
                continue
            mezz.append({
                "code": code, "name": name, "kind": kind,
                "rcept_dt": (r.get("rcept_dt") or "").replace("-", "").strip(),
                "amount": amt, "price": prc, "shares": amt / prc,
            })

    exercised = 0
    cur_price = None
    total_shares = None
    if mezz:
        # 전환청구권/신주인수권 행사 공시 감지 (실제 물량 출회 신호)
        for d in api.get_dart_disclosures(code, days=_EXERCISE_LOOKBACK_DAYS):
            nm = d.get("report_nm", "")
            if "전환청구권행사" in nm or "신주인수권행사" in nm or "전환청구권 행사" in nm:
                exercised += 1
        try:
            cur_price = api.get_current_price(code, is_overseas=False)
        except Exception:
            cur_price = None
        total_shares, _ = api.get_dart_shares_outstanding(code)
    for m in mezz:
        m["cur_price"] = cur_price
        m["total_shares"] = total_shares
        m["exercised"] = exercised

    return treasury, frees, mezz


def _num(s):
    try:
        t = str(s).replace(",", "").replace("△", "-").replace("▲", "-").strip()
        return float(t) if t not in ("", "-") else None
    except Exception:
        return None


def _fmt_eok(won):
    if won is None:
        return "-"
    if abs(won) >= 1e13:
        return f"{won / 1e12:,.1f}조"
    return f"{won / 1e8:,.0f}억"


def show_insider_trades(days=90):
    """관심종목 수급·물량 신호 종합 (자기주식/오버행/무상증자 + 내부자/5% 보고)."""
    utils.clear_screen()
    config.console.print(f"\n[bold cyan][관심종목 수급 · 물량 신호] (최근 {days}일)[/bold cyan]\n")

    codes = _kr_stocks()
    if not codes:
        config.console.print("[yellow]등록된 국내 관심종목(주식)이 없습니다.[/yellow]")
        return
    if not config.DART_API_KEY:
        config.console.print("[yellow]※ DART API 키가 설정되지 않았습니다. (환경변수 DART_API_KEY)[/yellow]")
        return

    cutoff = (datetime.now() - timedelta(days=days)).strftime("%Y%m%d")
    insiders, majors = [], []
    treasury, frees, mezz = [], [], []
    with Progress(SpinnerColumn(), TextColumn("[progress.description]{task.description}"),
                  BarColumn(), TextColumn("[progress.percentage]{task.percentage:>3.0f}%"), console=config.console, transient=True) as progress:
        task = progress.add_task("[cyan]수급·물량 신호 조회 중...[/cyan]", total=len(codes) * 2)
        with concurrent.futures.ThreadPoolExecutor(max_workers=8) as ex:
            futs = {ex.submit(_collect, c, n, cutoff): "insider" for c, n in codes}
            futs.update({ex.submit(_collect_supply, c, n, days): "supply" for c, n in codes})
            for fut in concurrent.futures.as_completed(futs):
                try:
                    if futs[fut] == "insider":
                        ins, majs = fut.result()
                        insiders.extend(ins)
                        majors.extend(majs)
                    else:
                        tr, fr, mz = fut.result()
                        treasury.extend(tr)
                        frees.extend(fr)
                        mezz.extend(mz)
                except Exception:
                    pass
                progress.advance(task)

    if not any((insiders, majors, treasury, frees, mezz)):
        config.console.print(f"[dim]최근 {days}일간 수급·물량 관련 보고가 없습니다.[/dim]")
        return

    _render_treasury(treasury)
    _render_free_increase(frees)
    _render_overhang(mezz)
    _render_summary(insiders)
    _render_insiders(insiders)
    _render_majors(majors)
    config.console.print("[dim]※ 증감(+)=취득, (-)=처분 · 무상신주·스톡옵션 등 매매 외 사유 포함 가능 · 원문: dart.fss.or.kr[/dim]")


def _render_treasury(rows):
    """자기주식 취득·처분·신탁계약 결정 (회사 단위 수급 신호)."""
    config.console.print("[bold]▸ 자기주식 취득 · 처분 결정[/bold]")
    if not rows:
        config.console.print("  [dim]해당 기간 결정 공시가 없습니다.[/dim]\n")
        return
    rows = sorted(rows, key=lambda r: r["rcept_dt"], reverse=True)
    table = Table(box=box.HORIZONTALS, header_style="dim", border_style="dim")
    table.add_column("일자", justify="center")
    table.add_column("종목", justify="left")
    table.add_column("구분", justify="center")
    table.add_column("예정금액", justify="right")
    table.add_column("예정수량(주)", justify="right")
    table.add_column("기간", justify="center")
    table.add_column("목적", justify="left", overflow="fold")
    for r in rows:
        kind = r["kind"]
        kind_str = (f"[red]▲ {kind}[/]" if kind in ("취득", "신탁체결") else f"[blue]▼ {kind}[/]")
        period = f"{r['bgd'][:10]}~{r['edd'][:10]}" if r.get("bgd") and r.get("edd") else "-"
        table.add_row(
            _fmt_date(r["rcept_dt"]), f"{r['name']} ({r['code']})", kind_str,
            _fmt_eok(r.get("amount")),
            f"{r['qty']:,.0f}" if r.get("qty") else "-",
            period, (r.get("note") or "-")[:24],
        )
    config.console.print(table)
    config.console.print("  [dim]취득·신탁체결=매수 수급(+), 처분=잠재 매도물량(-). 소각 목적 여부는 공시 원문 확인.[/dim]\n")


def _render_free_increase(rows):
    """무상증자 결정 (단기 모멘텀 이벤트)."""
    if not rows:
        return
    config.console.print("[bold]▸ 무상증자 결정[/bold]")
    table = Table(box=box.HORIZONTALS, header_style="dim", border_style="dim")
    table.add_column("일자", justify="center")
    table.add_column("종목", justify="left")
    table.add_column("1주당 배정", justify="right")
    table.add_column("신주(주)", justify="right")
    table.add_column("배정기준일", justify="center")
    for r in sorted(rows, key=lambda r: str(r.get("rcept_dt", "")), reverse=True):
        ratio = _num(r.get("nstk_ascnt_ps_ostk"))
        new_cnt = _num(r.get("nstk_ostk_cnt"))
        table.add_row(
            _fmt_date(str(r.get("rcept_dt", "")).replace("-", "")),
            f"{r['name']} ({r['code']})",
            f"[red]{ratio:g}주[/]" if ratio else "-",
            f"{new_cnt:,.0f}" if new_cnt else "-",
            (r.get("nstk_asstd") or "-")[:10],
        )
    config.console.print(table)
    config.console.print()


def _render_overhang(rows, limit=20):
    """메자닌(CB/BW/EB) 오버행 — 전환가 vs 현재가로 잠재 매도물량 감시."""
    config.console.print(f"[bold]▸ 메자닌 오버행 현황[/bold] [dim](최근 {_MEZZ_LOOKBACK_DAYS // 365}년 발행 결정 기준)[/dim]")
    if not rows:
        config.console.print("  [dim]최근 발행된 CB/BW/EB가 없습니다.[/dim]\n")
        return
    rows = sorted(rows, key=lambda r: r["rcept_dt"], reverse=True)
    table = Table(box=box.HORIZONTALS, header_style="dim", border_style="dim")
    table.add_column("발행결정일", justify="center")
    table.add_column("종목", justify="left")
    table.add_column("종류", justify="center")
    table.add_column("권면총액", justify="right")
    table.add_column("전환/행사가", justify="right")
    table.add_column("현재가", justify="right")
    table.add_column("잠재물량(주)", justify="right")
    table.add_column("유통대비", justify="right")
    table.add_column("상태", justify="left")
    for r in rows[:limit]:
        cur = r.get("cur_price")
        prc = r["price"]
        if cur and cur > 0:
            gap = (cur / prc - 1) * 100
            status = (f"[red]전환권 행사가능 ({gap:+.0f}%)[/]" if gap >= 0
                      else f"[green]전환가 하회 ({gap:+.0f}%)[/]")
        else:
            status = "[dim]-[/dim]"
        ts = r.get("total_shares")
        pct = f"{r['shares'] / ts * 100:.1f}%" if ts else "-"
        if r.get("exercised"):
            status += f" [yellow]행사공시 {r['exercised']}건[/]"
        table.add_row(
            _fmt_date(r["rcept_dt"]), f"{r['name']} ({r['code']})", r["kind"],
            _fmt_eok(r["amount"]), f"{prc:,.0f}", f"{cur:,.0f}" if cur else "-",
            f"{r['shares']:,.0f}", pct, status,
        )
    config.console.print(table)
    if len(rows) > limit:
        config.console.print(f"  [dim]… 외 {len(rows) - limit}건[/dim]")
    config.console.print("  [dim]현재가 ≥ 전환가면 상승 시 전환·매도 물량(오버행)이 상방 저항이 될 수 있습니다. "
                         "전환가는 발행 결정 시점 기준(리픽싱 미반영), 행사공시=최근 90일 전환청구권·신주인수권 행사.[/dim]\n")


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
