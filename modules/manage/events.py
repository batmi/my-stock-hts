# modules/manage/events.py
"""관심종목 배당/실적 캘린더 조회.

- 국내(stocks_kr/etfs_kr): OpenDART 'alotMatter'로 최근 확정 배당 정보 조회.
- 해외(stocks_us/etfs_us): yfinance로 예정 배당락일/실적발표일 조회.
"""
import calendar as _cal
import concurrent.futures
from datetime import datetime, date, timedelta

from rich.table import Table
from rich import box
from rich.progress import Progress, SpinnerColumn, TextColumn, BarColumn

import config
import api
import utils

try:
    import yfinance as yf
except Exception:  # pragma: no cover
    yf = None


def _kr_year_end_holiday(year):
    """KRX 연말 휴장일 = 그 해 마지막 평일(12/31이 주말이면 직전 평일)."""
    d = date(year, 12, 31)
    while d.weekday() >= 5:
        d -= timedelta(days=1)
    return d


def _is_kr_trading_day(d):
    """한국 증시 거래일 여부 (주말/공휴일/KRX 연말 휴장 제외)."""
    if d.weekday() >= 5:
        return False
    if d == _kr_year_end_holiday(d.year):  # KRX 연말 폐장(공휴일 라이브러리엔 없음)
        return False
    try:
        if api.get_holiday_name(d.strftime("%Y%m%d"), country="KR"):
            return False
    except Exception:
        pass
    return True


def _prev_trading_day(d):
    cur = d - timedelta(days=1)
    for _ in range(20):
        if _is_kr_trading_day(cur):
            return cur
        cur -= timedelta(days=1)
    return cur


def _month_end_ex_date(year, month):
    """해당 월말(배당기준일)에 대응하는 '예상' 배당락일 (= 폐장일의 직전 거래일)."""
    close = date(year, month, _cal.monthrange(year, month)[1])
    while not _is_kr_trading_day(close):   # 폐장일(그 달 마지막 거래일)로 롤백
        close -= timedelta(days=1)
    return _prev_trading_day(close)


def _kr_yf_dividends(code):
    """yfinance 배당 지급 이력(배당락일 index) 반환. 없으면 None. .KS→.KQ 순."""
    if yf is None:
        return None
    try:
        for sfx in (".KS", ".KQ"):
            try:
                div = yf.Ticker(code + sfx).dividends
            except Exception:
                div = None
            if div is not None and len(div) > 0:
                return div
    except Exception:
        pass
    return None


def _yf_ex_dates(div_series):
    """배당 시계열 index를 tz-naive date 리스트로 변환."""
    if div_series is None or len(div_series) == 0:
        return []
    idx = div_series.index
    try:
        if getattr(idx, "tz", None) is not None:
            idx = idx.tz_localize(None)
    except Exception:
        pass
    out = []
    for ts in idx:
        try:
            out.append(ts.date() if hasattr(ts, "date") else ts)
        except Exception:
            pass
    return out


def _kr_dividend_count(div_series, today):
    """최근 약 13개월(400일) 배당 지급 횟수 (주기 추정용)."""
    cutoff = today - timedelta(days=400)
    return sum(1 for d in _yf_ex_dates(div_series) if d >= cutoff)


def _project_next_ex_date(div_series, today):
    """과거 배당락일 패턴을 1년 미뤄 '오늘 이후' 가장 가까운 실제 패턴 기반 배당락일 추정."""
    cands = []
    for d in _yf_ex_dates(div_series):
        if d < today - timedelta(days=400):
            continue
        try:
            nd = date(d.year + 1, d.month, d.day)
        except ValueError:  # 2/29 등
            nd = date(d.year + 1, d.month, 28)
        while not _is_kr_trading_day(nd):  # 휴장일이면 직전 거래일로
            nd -= timedelta(days=1)
        cands.append(nd)
    fut = sorted(c for c in cands if c >= today)
    return fut[0] if fut else None


def _kr_dividend_plan(pay_count, acc_month):
    """연간 배당 지급 횟수 + 결산월 -> (기준일 월 목록, 주기 라벨).

    월배당=매월말, 분기배당=3·6·9·12월말, 반기배당=6·12월말(법정), 연배당=결산월말.
    """
    if pay_count >= 10:
        return list(range(1, 13)), "월배당"
    if pay_count >= 4:
        return [3, 6, 9, 12], "분기배당"
    if pay_count >= 2:
        return [6, 12], "반기배당"
    try:
        m = int(acc_month)
    except Exception:
        m = 12
    return [m], "연배당"


def _next_kr_ex_date(months, today):
    """주기별 기준일 월들 중 '오늘 이후' 가장 가까운 예상 배당락일."""
    cands = []
    for y in (today.year, today.year + 1):
        for m in months:
            try:
                cands.append(_month_end_ex_date(y, m))
            except Exception:
                pass
    cands = sorted(c for c in cands if c >= today)
    return cands[0] if cands else None


def _collect_kr(code, name):
    """국내 종목의 확정 배당 정보(DART) + 배당주기 반영 다음 예상 배당락일(추정)."""
    info = api.get_dart_dividend(code)
    if not info:
        return None
    acc = api.get_dart_acc_month(code)
    today = datetime.now().date()
    div = _kr_yf_dividends(code)
    count = _kr_dividend_count(div, today)
    months, freq_label = _kr_dividend_plan(count, acc)

    # 1) 실제 과거 배당락일 패턴으로 정밀 추정 → 2) 실패 시 결산월/주기 일반 규칙 폴백
    ex_date = _project_next_ex_date(div, today)
    exact = ex_date is not None
    if ex_date is None:
        try:
            ex_date = _next_kr_ex_date(months, today)
        except Exception:
            ex_date = None
    return {
        "code": code, "name": name, "overseas": False,
        "year": info.get("year", "-"),
        "dps": info.get("주당배당금", 0.0),
        "yield": info.get("시가배당률", 0.0),
        "acc_month": acc,
        "ex_date": ex_date,
        "freq_label": freq_label,
        "exact": exact,
    }


def _parse_us_date(val):
    """yfinance가 돌려주는 다양한 날짜 표현을 datetime.date로 정규화."""
    if val is None:
        return None
    try:
        # list(예: Earnings Date)면 가장 가까운 미래 값 사용
        if isinstance(val, (list, tuple)):
            val = val[0] if val else None
            if val is None:
                return None
        if isinstance(val, (int, float)):  # unix timestamp
            return datetime.fromtimestamp(val).date()
        if isinstance(val, datetime):
            return val.date()
        if hasattr(val, "date"):  # pandas.Timestamp
            return val.date()
        return datetime.strptime(str(val)[:10], "%Y-%m-%d").date()
    except Exception:
        return None


def _collect_us(code, name):
    """해외 종목의 예정 배당락일/실적발표일(yfinance, best-effort)."""
    if yf is None:
        return None
    events = []
    try:
        tk = yf.Ticker(code)
        cal = {}
        try:
            cal = tk.calendar or {}
        except Exception:
            cal = {}

        ex_div = _parse_us_date(cal.get("Ex-Dividend Date") if isinstance(cal, dict) else None)
        earnings = _parse_us_date(cal.get("Earnings Date") if isinstance(cal, dict) else None)

        # calendar가 비면 info로 폴백
        if ex_div is None or earnings is None:
            try:
                info = tk.info or {}
            except Exception:
                info = {}
            if ex_div is None:
                ex_div = _parse_us_date(info.get("exDividendDate"))
            if earnings is None:
                earnings = _parse_us_date(info.get("earningsTimestamp") or info.get("earningsTimestampStart"))

        if ex_div:
            events.append({"code": code, "name": name, "type": "배당락", "date": ex_div})
        if earnings:
            events.append({"code": code, "name": name, "type": "실적발표", "date": earnings})
    except Exception:
        return None
    return events or None


def _gather_watchlist():
    kr, us = [], []
    for key in ("stocks_kr", "etfs_kr"):
        for s in config.session.stock_data.get(key, []):
            if s.get("code"):
                kr.append((s["code"], s.get("name", s["code"])))
    for key in ("stocks_us", "etfs_us"):
        for s in config.session.stock_data.get(key, []):
            if s.get("code"):
                us.append((s["code"], s.get("name", s["code"])))
    return kr, us


def show_calendar():
    """관심종목 배당/실적 캘린더 출력."""
    utils.clear_screen()
    config.console.print("\n[bold cyan]📅 [관심종목 배당 · 실적 캘린더][/bold cyan]\n")

    kr, us = _gather_watchlist()
    if not kr and not us:
        config.console.print("[yellow]등록된 관심종목이 없습니다. 먼저 관심종목을 추가해주세요.[/yellow]")
        return

    if kr and not config.DART_API_KEY:
        config.console.print("[dim yellow]※ DART API 키가 설정되지 않아 국내 배당 정보는 조회되지 않습니다. (환경변수 DART_API_KEY)[/dim yellow]\n")

    us_events = []
    kr_rows = []

    with Progress(
        SpinnerColumn(), TextColumn("[progress.description]{task.description}"),
        BarColumn(), console=config.console, transient=True
    ) as progress:
        task = progress.add_task("[cyan]배당/실적 일정 조회 중...[/cyan]", total=len(kr) + len(us))

        with concurrent.futures.ThreadPoolExecutor(max_workers=8) as ex:
            futures = {}
            if config.DART_API_KEY:
                for code, name in kr:
                    futures[ex.submit(_collect_kr, code, name)] = ("kr", code)
            for code, name in us:
                futures[ex.submit(_collect_us, code, name)] = ("us", code)

            for fut in concurrent.futures.as_completed(futures):
                kind, _ = futures[fut]
                try:
                    res = fut.result()
                except Exception:
                    res = None
                if res:
                    if kind == "kr":
                        kr_rows.append(res)
                    else:
                        us_events.extend(res)
                progress.advance(task)
            # 진행률 보정 (DART 키 없을 때 kr 미제출분)
            progress.update(task, completed=len(kr) + len(us))

    # 국내 예상 배당락일(추정)을 예정 일정에 합류
    events = list(us_events)
    for r in kr_rows:
        if r.get("ex_date"):
            events.append({
                "code": r["code"], "name": r["name"], "type": "배당락",
                "date": r["ex_date"], "estimated": True,
                "freq": r.get("freq_label", ""),
                "exact": r.get("exact", False),
            })

    _render_upcoming(events)
    _render_kr_dividends(kr_rows)


def _render_upcoming(events):
    """예정 일정(해외 배당락/실적 + 국내 예상 배당락) - 날짜순."""
    today = datetime.now().date()
    upcoming = sorted([e for e in events if e["date"] >= today], key=lambda e: e["date"])

    config.console.print("[bold]▸ 예정 일정[/bold]")
    config.console.print("  [dim]※ 실적발표 일정은 해외 종목만 제공됩니다. (국내 기업은 예정일을 공시하지 않음)[/dim]")
    if not upcoming:
        config.console.print("  [dim]표시할 예정 일정이 없습니다.[/dim]\n")
        return

    table = Table(box=box.HORIZONTALS, header_style="dim", border_style="dim")
    table.add_column("일자", justify="center")
    table.add_column("D-day", justify="center")
    table.add_column("종목", justify="left")
    table.add_column("구분", justify="center")
    table.add_column("비고", justify="left")

    has_estimated = False
    for e in upcoming:
        d = (e["date"] - today).days
        dday = "D-DAY" if d == 0 else f"D-{d}"
        type_color = "yellow" if e["type"] == "배당락" else "cyan"
        icon = "💰" if e["type"] == "배당락" else "📊"
        note = ""
        if e.get("estimated"):
            freq = e.get("freq", "")
            basis = "전년패턴" if e.get("exact") else "추정"
            note = f"[dim]{freq}·{basis}[/dim]" if freq else f"[dim]{basis}[/dim]"
            has_estimated = True
        table.add_row(
            e["date"].strftime("%Y-%m-%d (%a)"),
            f"[bold]{dday}[/bold]" if d <= 3 else dday,
            f"{e['name']} ({e['code']})",
            f"[{type_color}]{icon} {e['type']}[/]",
            note,
        )
    config.console.print(table)
    if has_estimated:
        config.console.print("  [dim]※ '추정' 배당락일은 결산월·거래일(T+2)로 계산한 예상치입니다. "
                             "선배당후투자 도입·분기/반기 배당 기업은 실제와 다를 수 있습니다.[/dim]")
    config.console.print()


def _render_kr_dividends(rows):
    """국내 배당 정보(최근 확정) - 시가배당률 높은 순."""
    config.console.print("[bold]▸ 국내 배당 정보 (최근 확정 / DART)[/bold]")
    if not rows:
        config.console.print("  [dim]조회된 국내 배당 정보가 없습니다.[/dim]\n")
        return

    rows = sorted(rows, key=lambda r: r["yield"], reverse=True)
    table = Table(box=box.HORIZONTALS, header_style="dim", border_style="dim")
    table.add_column("종목", justify="left")
    table.add_column("기준연도", justify="center")
    table.add_column("주당배당금", justify="right")
    table.add_column("시가배당률", justify="right")
    table.add_column("배당주기", justify="center")

    freq_color = {"월배당": "magenta", "분기배당": "cyan", "반기배당": "green", "연배당": "white"}
    for r in rows:
        y_color = "red" if r["yield"] >= 3.0 else ("orange3" if r["yield"] >= 1.0 else "white")
        freq = r.get("freq_label") or "-"
        table.add_row(
            f"{r['name']} ({r['code']})",
            str(r["year"]),
            f"{r['dps']:,.0f}원" if r["dps"] else "-",
            f"[{y_color}]{r['yield']:.2f}%[/]" if r["yield"] else "-",
            f"[{freq_color.get(freq, 'white')}]{freq}[/]",
        )
    config.console.print(table)
    config.console.print("  [dim]※ 배당주기는 최근 1년 실제 지급 횟수(yfinance) 기반 추정입니다.[/dim]")
    config.console.print()
