# modules/manage/disclosure.py
"""관심종목 공시 모니터링 (OpenDART list.json 기반).

- 공시 조회: 관심종목 최근 공시를 중요도 분류해 표시 + (선택) Gemini AI 요약
- 스케줄러 알림(scheduler)에서도 분류 로직을 재사용한다.
"""
import concurrent.futures
from datetime import datetime, timedelta

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


# ---------------------------------------------------------------------------
# 공시 상세정보 (잠정실적 수치 / 유상증자 희석률 / 메자닌 발행조건)
# ---------------------------------------------------------------------------
_EARNINGS_DOC_KEYWORDS = ("잠정실적", "손익구조", "잠정)실적")
_FDPP_LABELS = (("fdpp_fclt", "시설자금"), ("fdpp_bsninh", "영업양수"),
                ("fdpp_op", "운영자금"), ("fdpp_dtrp", "채무상환"),
                ("fdpp_ocsa", "타법인증권취득"), ("fdpp_etc", "기타자금"))


def _num(s):
    try:
        t = str(s).replace(",", "").replace("△", "-").replace("▲", "-").strip()
        return float(t) if t not in ("", "-") else None
    except Exception:
        return None


def _fmt_eok(won):
    """원 단위 금액 -> '1,234억' / '171.0조' 문자열."""
    if won is None:
        return "-"
    if abs(won) >= 1e13:
        return f"{won / 1e12:,.1f}조"
    return f"{won / 1e8:,.0f}억"


def _fmt_pct(s):
    """증감률 셀('12.3', '1,810.26', '△5.1', '흑전' 등) -> '+12.3%' 형태."""
    t = str(s).replace(",", "").replace("△", "-").replace("▲", "-").rstrip("%").strip()
    try:
        return f"{float(t):+.1f}%"
    except Exception:
        return str(s)


def _detail_eligible(e):
    """공시 원문/전용 API로 상세정보를 뽑을 수 있는 공시인지."""
    nm = e["report_nm"]
    if e["category"] == "실적·IR" and any(k in nm for k in _EARNINGS_DOC_KEYWORDS):
        return True
    if e["category"] == "증자·감자" and "유상증자" in nm:
        return True
    if e["category"] == "메자닌(CB/BW)":
        return True
    return False


def _earnings_note(e):
    """잠정실적/손익구조 공시 -> '매출 1,234억(+12.3%) · 영업익 ...' 요약."""
    brief = api.get_dart_earnings_brief(e["rcept_no"])
    if not brief:
        return ""
    unit, name_map = brief["unit"], {"매출액": "매출", "영업이익": "영업익", "당기순이익": "순익"}
    parts = []
    for key in ("매출액", "영업이익", "당기순이익"):
        row = brief["rows"].get(key)
        if not row or row[0] is None:
            continue
        cur, base, pct = row
        if pct:
            pct_s = _fmt_pct(pct)
        elif base:  # 증감률 셀이 없으면 비교값으로 직접 계산
            pct_s = f"{(cur - base) / abs(base) * 100:+.1f}%"
        else:
            pct_s = ""
        parts.append(f"{name_map[key]} {_fmt_eok(cur * unit)}" + (f"({pct_s})" if pct_s else ""))
    return " · ".join(parts)


def _detail_date_range(e):
    """상세조회 API용 접수일 범위. 정정공시는 원공시 접수일이 과거라 45일 전부터 조회."""
    end = str(e["date"])
    try:
        bgn = (datetime.strptime(end, "%Y%m%d") - timedelta(days=45)).strftime("%Y%m%d")
    except Exception:
        bgn = end
    return bgn, end


def _paid_increase_note(e):
    """유상증자결정 -> 신주 수·희석률·증자방식·자금목적 요약."""
    bgn, end = _detail_date_range(e)
    rows = api.get_dart_paid_increase_detail(e["code"], bgn, end)
    row = next((r for r in rows if r.get("rcept_no") == e["rcept_no"]),
               max(rows, key=lambda r: r.get("rcept_no", "")) if rows else None)
    if not row:
        return ""
    new = (_num(row.get("nstk_ostk_cnt")) or 0) + (_num(row.get("nstk_estk_cnt")) or 0)
    base = (_num(row.get("bfic_tisstk_ostk")) or 0) + (_num(row.get("bfic_tisstk_estk")) or 0)
    parts = []
    if new:
        parts.append(f"신주 {new:,.0f}주")
    if new and base:
        parts.append(f"희석 {new / base * 100:.1f}%")
    method = (row.get("ic_mthn") or "").strip()
    if method:
        parts.append(method)
    amounts = [(label, _num(row.get(f)) or 0) for f, label in _FDPP_LABELS]
    top = max(amounts, key=lambda x: x[1])
    if top[1] > 0:
        parts.append(top[0])
    return " · ".join(parts)


def _bond_note(e):
    """CB/BW/EB 발행결정 -> 권면총액·전환(행사)가·발행방법 요약."""
    nm = e["report_nm"]
    kind = ("CB" if "전환사채" in nm else
            "BW" if "신주인수권" in nm else
            "EB" if "교환사채" in nm else None)
    if not kind:
        return ""
    bgn, end = _detail_date_range(e)
    rows = api.get_dart_bond_issue_detail(e["code"], bgn, end, kind=kind)
    row = next((r for r in rows if r.get("rcept_no") == e["rcept_no"]),
               max(rows, key=lambda r: r.get("rcept_no", "")) if rows else None)
    if not row:
        return ""
    parts = []
    amt = _num(row.get("bd_fta"))
    if amt:
        parts.append(f"권면 {_fmt_eok(amt)}")
    prc = _num(row.get("cv_prc")) or _num(row.get("ex_prc"))
    if prc:
        parts.append(f"{'전환가' if kind == 'CB' else '행사가' if kind == 'BW' else '교환가'} {prc:,.0f}원")
    method = (row.get("bdis_mthn") or "").strip()
    if method:
        parts.append(method)
    return " · ".join(parts)


def build_detail_note(e):
    """공시 1건의 상세 요약 문자열 (부적합/실패 시 '')."""
    try:
        nm = e["report_nm"]
        if e["category"] == "실적·IR" and any(k in nm for k in _EARNINGS_DOC_KEYWORDS):
            return _earnings_note(e)
        if e["category"] == "증자·감자" and "유상증자" in nm:
            return _paid_increase_note(e)
        if e["category"] == "메자닌(CB/BW)":
            return _bond_note(e)
    except Exception:
        pass
    return ""


def _enrich_details(events, limit=12):
    """표시 대상 공시 중 상세정보 대상만 병렬 조회해 e['note']를 채운다."""
    targets = [e for e in events if _detail_eligible(e)][:limit]
    if not targets:
        return
    with Progress(SpinnerColumn(), TextColumn("[progress.description]{task.description}"),
                  console=config.console, transient=True) as progress:
        progress.add_task("[cyan]공시 상세정보 조회 중...[/cyan]", total=None)
        with concurrent.futures.ThreadPoolExecutor(max_workers=4) as ex:
            futs = {ex.submit(build_detail_note, e): e for e in targets}
            for fut in concurrent.futures.as_completed(futs):
                try:
                    futs[fut]["note"] = fut.result()
                except Exception:
                    futs[fut]["note"] = ""


def show_disclosures(days=14):
    """관심종목 최근 공시 조회 (중요도순)."""
    utils.clear_screen()
    config.console.print(f"\n[bold cyan][관심종목 공시 모니터링] (최근 {days}일)[/bold cyan]\n")

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
    shown = events[:40]
    _enrich_details(shown)

    table = Table(box=box.HORIZONTALS, header_style="dim", border_style="dim")
    table.add_column("일자", justify="center")
    table.add_column("중요", justify="center")
    table.add_column("종목", justify="left")
    table.add_column("구분", justify="left")
    table.add_column("공시 제목", justify="left")
    table.add_column("상세", justify="left")

    level_style = {2: "bold red", 1: "white", 0: "dim"}
    for e in shown:
        table.add_row(
            _fmt_date(e["date"]),
            e["icon"],
            f"{e['name']} ({e['code']})",
            f"[{level_style[e['level']]}]{e['category']}[/]",
            e["report_nm"],
            f"[dim]{e.get('note', '')}[/dim]",
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
        note = build_detail_note(e) if _detail_eligible(e) else ""
        detail_line = f"· 상세: {note}\n" if note else ""
        msg = (f"{e['icon']} [공시 알림] {e['name']}({e['code']})\n"
               f"· 구분: {e['category']}\n"
               f"· {e['report_nm']}\n"
               f"· 일자: {_fmt_date(e['date'])}\n"
               f"{detail_line}"
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
