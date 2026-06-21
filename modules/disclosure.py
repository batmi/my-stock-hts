# modules/disclosure.py
"""관심종목 공시 모니터링 / 실적 발표 추적 (OpenDART list.json 기반).

- 공시 조회: 관심종목 최근 공시를 중요도 분류해 표시 + (선택) Gemini AI 요약
- 실적 추적: 정기보고서·잠정실적 공시 + 결산월 기반 예상 제출기한
- 스케줄러 알림(scheduler)에서도 분류 로직을 재사용한다.
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

DART_VIEWER_URL = "https://dart.fss.or.kr/dsaf001/main.do?rcpNo={}"

# 분류 규칙: (키워드들, 중요도 level[2=중대/1=관심/0=일반], 아이콘, 카테고리)
# 위에서부터 첫 매칭 우선 → 순서 중요(상장위험을 최상단에).
_RULES = [
    (("상장폐지", "관리종목", "거래정지", "상장적격성", "감사의견거절", "감사의견한정",
      "감사의견부적정", "부도", "영업정지", "회생절차", "파산"), 2, "🔴", "상장위험"),
    (("횡령", "배임"), 2, "🔴", "횡령·배임"),
    (("불성실공시법인",), 2, "🔴", "불성실공시"),
    (("유상증자", "감자결정", "자본감소", "주식병합"), 2, "🟠", "증자·감자"),
    (("전환사채", "신주인수권부사채", "교환사채"), 1, "🟠", "메자닌(CB/BW)"),
    (("자기주식", "자사주", "주식소각"), 1, "🟢", "자기주식"),
    (("무상증자",), 1, "🟢", "무상증자"),
    (("합병", "분할", "영업양수", "영업양도", "주식교환", "주식이전"), 1, "🟡", "지배구조"),
    (("최대주주", "경영권"), 1, "🟡", "최대주주변동"),
    (("단일판매", "공급계약", "수주"), 1, "🟢", "수주·공급계약"),
    (("배당",), 1, "🟡", "배당"),
    (("잠정실적", "손익구조", "매출액또는", "기업설명회", "IR개최"), 1, "🔵", "실적·IR"),
    (("소송", "청구"), 1, "🟡", "소송"),
    (("사업보고서", "분기보고서", "반기보고서"), 0, "⚪", "정기보고서"),
    (("주주총회",), 0, "⚪", "주주총회"),
    (("임원ㆍ주요주주", "최대주주등소유주식변동", "대량보유"), 0, "⚪", "지분공시"),
]


def classify_disclosure(report_nm):
    """공시 제목 -> (level, icon, category)."""
    nm = report_nm or ""
    for keywords, level, icon, category in _RULES:
        if any(k in nm for k in keywords):
            return level, icon, category
    return 0, "⚪", "기타"


def _kr_watchlist():
    """관심종목 중 국내(주식+ETF) [(code, name), ...]."""
    out = []
    for key in ("stocks_kr", "etfs_kr"):
        for s in config.session.stock_data.get(key, []):
            if s.get("code"):
                out.append((s["code"], s.get("name", s["code"])))
    return out


def _fmt_date(yyyymmdd):
    s = str(yyyymmdd)
    return f"{s[:4]}-{s[4:6]}-{s[6:]}" if len(s) == 8 and s.isdigit() else s


# ---------------------------------------------------------------------------
# 공시 조회
# ---------------------------------------------------------------------------
def collect_disclosures(code, name, days=14, min_level=0):
    """단일 종목의 최근 공시를 분류해 반환 (level>=min_level만)."""
    rows = api.get_dart_disclosures(code, days=days)
    out = []
    for r in rows:
        level, icon, category = classify_disclosure(r["report_nm"])
        if level < min_level:
            continue
        out.append({
            "code": code, "name": name,
            "date": r["rcept_dt"], "report_nm": r["report_nm"],
            "level": level, "icon": icon, "category": category,
            "rcept_no": r["rcept_no"],
        })
    return out


def _gather(codes, days, min_level):
    events = []
    if not codes:
        return events
    with Progress(SpinnerColumn(), TextColumn("[progress.description]{task.description}"),
                  BarColumn(), console=config.console, transient=True) as progress:
        task = progress.add_task("[cyan]공시 조회 중...[/cyan]", total=len(codes))
        with concurrent.futures.ThreadPoolExecutor(max_workers=8) as ex:
            futs = {ex.submit(collect_disclosures, c, n, days, min_level): c for c, n in codes}
            for fut in concurrent.futures.as_completed(futs):
                try:
                    events.extend(fut.result())
                except Exception:
                    pass
                progress.advance(task)
    return events


def show_disclosures(days=14):
    """관심종목 최근 공시 조회 (중요도순)."""
    utils.clear_screen()
    config.console.print(f"\n[bold cyan]📰 [관심종목 공시 모니터링] (최근 {days}일)[/bold cyan]\n")

    codes = _kr_watchlist()
    if not codes:
        config.console.print("[yellow]등록된 국내 관심종목이 없습니다.[/yellow]")
        return
    if not config.DART_API_KEY:
        config.console.print("[yellow]※ DART API 키가 설정되지 않았습니다. (환경변수 DART_API_KEY)[/yellow]")
        return

    # 관심/중대 공시(level>=1)만 표시하여 임원·지분 등 일상 공시 노이즈 제거
    events = _gather(codes, days, min_level=1)
    if not events:
        config.console.print(f"[dim]최근 {days}일간 주요 공시가 없습니다. (임원·지분 등 일반 공시는 제외)[/dim]")
        return

    events.sort(key=lambda e: (e["level"], e["date"]), reverse=True)  # 중요도↓, 최신순

    table = Table(box=box.HORIZONTALS, header_style="dim", border_style="dim")
    table.add_column("일자", justify="center")
    table.add_column("중요", justify="center")
    table.add_column("종목", justify="left")
    table.add_column("구분", justify="left")
    table.add_column("공시 제목", justify="left")

    level_style = {2: "bold red", 1: "white", 0: "dim"}
    for e in events[:40]:
        table.add_row(
            _fmt_date(e["date"]),
            e["icon"],
            f"{e['name']} ({e['code']})",
            f"[{level_style[e['level']]}]{e['category']}[/]",
            e["report_nm"],
        )
    config.console.print(table)
    config.console.print("[dim]🔴 중대  🟠 증자/메자닌  🟢 호재성  🟡 이벤트  🔵 실적 · 원문: dart.fss.or.kr[/dim]")

    _maybe_ai_summary(events)


def check_and_alert_disclosures(min_level=2, days=2):
    """신규 중대 공시를 텔레그램으로 푸시 (scheduler 백그라운드용, UI 출력 없음).

    중복방지는 DB(notified_disclosures, 접수번호 기준). 반환: 발송 건수.
    """
    if not config.DART_API_KEY:
        return 0
    codes = _kr_watchlist()
    if not codes:
        return 0

    from modules import db_manager
    events = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=8) as ex:
        futs = [ex.submit(collect_disclosures, c, n, days, min_level) for c, n in codes]
        for fut in concurrent.futures.as_completed(futs):
            try:
                events.extend(fut.result())
            except Exception:
                pass

    sent = 0
    for e in sorted(events, key=lambda x: x["date"]):
        rcept = e.get("rcept_no")
        if not rcept or db_manager.db.is_disclosure_notified(rcept):
            continue
        msg = (f"{e['icon']} [공시 알림] {e['name']}({e['code']})\n"
               f"· 구분: {e['category']}\n"
               f"· {e['report_nm']}\n"
               f"· 일자: {_fmt_date(e['date'])}\n"
               f"{DART_VIEWER_URL.format(rcept)}")
        try:
            api.send_telegram_message(msg)
            db_manager.db.mark_disclosure_notified(rcept)
            sent += 1
        except Exception:
            pass
    return sent


def _maybe_ai_summary(events):
    from rich.prompt import Prompt
    config.console.print()
    if Prompt.ask("🤖 AI 공시 요약·해석(호재/악재)을 받으시겠습니까?", choices=["y", "n"], default="n") != "y":
        return

    # 중요도 높은 것 우선 최대 20건 요약 의뢰
    top = sorted(events, key=lambda e: (e["level"], e["date"]), reverse=True)[:20]
    lines = [f"- {_fmt_date(e['date'])} {e['name']}({e['code']}) [{e['category']}] {e['report_nm']}" for e in top]
    items_text = "\n".join(lines)

    from modules import theme_analysis
    from rich.markdown import Markdown
    from rich.panel import Panel
    from rich.padding import Padding
    with Progress(SpinnerColumn(), TextColumn("[progress.description]{task.description}"),
                  BarColumn(), console=config.console, transient=True) as progress:
        progress.add_task(f"[cyan]Gemini가 공시를 분석 중...[/cyan]\n[dim]  (모델: {config.GEMINI_MODEL})[/dim]", total=None)
        answer = theme_analysis.summarize_disclosures_with_gemini(items_text)
    if answer:
        if answer.startswith("⚠️"):
            config.console.print(f"\n{answer}")
        else:
            config.console.print()
            config.console.print(Padding(Panel(Markdown(answer), title="🤖 AI 공시 해석", border_style="cyan", padding=(1, 2), width=120), (0, 4)))


# ---------------------------------------------------------------------------
# 실적 발표 추적
# ---------------------------------------------------------------------------
def next_earnings_deadline(acc_month, today):
    """결산월 기준 다음 정기보고서 법정 제출기한 (분기/반기 +45일, 사업 +90일)."""
    try:
        fy = int(acc_month)
    except Exception:
        fy = 12
    q_months = sorted({((fy - off - 1) % 12) + 1 for off in (9, 6, 3, 0)})
    half_month = ((fy - 6 - 1) % 12) + 1
    cands = []
    for y in (today.year - 1, today.year, today.year + 1):
        for m in q_months:
            qend = date(y, m, _cal.monthrange(y, m)[1])
            offset = 90 if m == fy else 45
            cands.append((qend + timedelta(days=offset), m))
    fut = sorted([c for c in cands if c[0] >= today])
    if not fut:
        return None, None
    dl, m = fut[0]
    if m == fy:
        label = "사업보고서"
    elif m == half_month:
        label = "반기보고서"
    else:
        label = "분기보고서"
    return dl, label


def _collect_earnings(code, name):
    acc = api.get_dart_acc_month(code)
    today = date.today()
    deadline, rpt = next_earnings_deadline(acc, today)
    # 최근 실적/정기보고서 공시 1건
    recent = None
    for r in api.get_dart_disclosures(code, days=120):
        _, _, cat = classify_disclosure(r["report_nm"])
        if cat in ("실적·IR", "정기보고서"):
            recent = r
            break
    return {
        "code": code, "name": name, "acc_month": acc,
        "deadline": deadline, "report_type": rpt, "recent": recent,
    }


def show_earnings():
    """관심종목 실적 발표 추적 (최근 실적공시 + 예상 제출기한)."""
    utils.clear_screen()
    config.console.print("\n[bold cyan]📊 [관심종목 실적 발표 추적][/bold cyan]\n")

    codes = _kr_watchlist()
    if not codes:
        config.console.print("[yellow]등록된 국내 관심종목이 없습니다.[/yellow]")
        return
    if not config.DART_API_KEY:
        config.console.print("[yellow]※ DART API 키가 설정되지 않았습니다. (환경변수 DART_API_KEY)[/yellow]")
        return

    rows = []
    with Progress(SpinnerColumn(), TextColumn("[progress.description]{task.description}"),
                  BarColumn(), console=config.console, transient=True) as progress:
        task = progress.add_task("[cyan]실적 일정 조회 중...[/cyan]", total=len(codes))
        with concurrent.futures.ThreadPoolExecutor(max_workers=8) as ex:
            futs = {ex.submit(_collect_earnings, c, n): c for c, n in codes}
            for fut in concurrent.futures.as_completed(futs):
                try:
                    rows.append(fut.result())
                except Exception:
                    pass
                progress.advance(task)

    rows = [r for r in rows if r.get("deadline")]
    rows.sort(key=lambda r: r["deadline"])
    today = date.today()

    config.console.print("[dim]※ DART는 실적 '예정일'을 제공하지 않아, 법정 제출기한(분기+45일/사업+90일)으로 산출한 추정치입니다.[/dim]\n")
    table = Table(box=box.HORIZONTALS, header_style="dim", border_style="dim")
    table.add_column("종목", justify="left")
    table.add_column("다음 예상 제출기한", justify="center")
    table.add_column("D-day", justify="center")
    table.add_column("보고서", justify="center")
    table.add_column("최근 실적 공시", justify="left")

    for r in rows:
        d = (r["deadline"] - today).days
        dday = "D-DAY" if d == 0 else f"D-{d}"
        recent_str = "-"
        if r["recent"]:
            recent_str = f"{_fmt_date(r['recent']['rcept_dt'])} {r['recent']['report_nm']}"
        table.add_row(
            f"{r['name']} ({r['code']})",
            r["deadline"].strftime("%Y-%m-%d (%a)"),
            f"[bold]{dday}[/bold]" if d <= 7 else dday,
            r["report_type"] or "-",
            recent_str,
        )
    config.console.print(table)
