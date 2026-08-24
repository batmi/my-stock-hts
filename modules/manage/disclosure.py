# modules/manage/disclosure.py
"""관심종목 공시 모니터링 (OpenDART list.json 기반).

- 공시 조회: 관심종목 최근 공시를 중요도 분류해 표시 + (선택) Gemini AI 요약
- 스케줄러 알림(scheduler)에서도 분류 로직을 재사용한다.
"""
import concurrent.futures
import contextlib
from datetime import datetime, timedelta

from rich.table import Table
from rich import box
from rich.progress import Progress, SpinnerColumn, TextColumn, BarColumn

import config
import api
from core import utils

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


_DETAIL_LIMIT = 12          # 상세 조회 대상 최대 건수 (진행바 total 예약분과 같아야 한다)
_PROGRESS_LABEL = "[cyan]공시 조회 중...[/cyan]"


def _make_progress():
    """공시 조회용 진행바 (조회 → 상세 단계가 공유한다)."""
    return Progress(SpinnerColumn(), TextColumn("[progress.description]{task.description}"),
                    BarColumn(), TextColumn("[progress.percentage]{task.percentage:>3.0f}%"),
                    console=config.console, transient=True)


def _task_done(progress, task):
    """진행바 task의 현재 완료 수량."""
    t = next((x for x in progress.tasks if x.id == task), None)
    return int(t.completed) if t is not None else 0


def _gather(codes, days, min_level, progress=None, task=None, quiet=False):
    """관심종목 공시를 병렬 수집.

    progress/task를 받으면 그 진행바에 이어서 진행한다(호출측이 '조회 → 상세' 여러 단계를
    하나의 진행바로 합칠 수 있게 함). 없으면 자체 진행바를 만든다.

    quiet=True면 진행바를 아예 만들지 않는다. 텔레그램 명령처럼 **운영자 콘솔이 아닌
    곳에서 호출**되는 경로용이다 — 진행바는 config.console에 그려지므로, 백그라운드
    스레드에서 띄우면 운용자가 보고 있던 화면 위에 남의 진행바가 끼어든다.
    """
    events = []
    if not codes:
        return events
    with contextlib.ExitStack() as stack:
        if progress is None and not quiet:
            progress = stack.enter_context(_make_progress())
            task = progress.add_task(_PROGRESS_LABEL, total=len(codes))
        with concurrent.futures.ThreadPoolExecutor(max_workers=8) as ex:
            futs = {ex.submit(collect_disclosures, c, n, days, min_level): c for c, n in codes}
            for fut in concurrent.futures.as_completed(futs):
                try:
                    events.extend(fut.result())
                except Exception:
                    pass
                if progress is not None:
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
    if e["category"] == "증자·감자" and ("유상증자" in nm or "감자결정" in nm or "감자 결정" in nm):
        return True
    if e["category"] == "메자닌(CB/BW)":
        return True
    if e["category"] == "수주·공급계약" and "공급계약" in nm:
        return True
    if e["category"] == "자기주식" and "결정" in nm:
        return True
    if e["category"] == "무상증자" and "결정" in nm:
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


def _supply_contract_note(e):
    """단일판매ㆍ공급계약체결 원문 -> '계약 1,234억 · 매출대비 12.3% · 상대 · ~종료일' 요약.

    공시 서식 표에 계약금액 총액·최근 매출액·매출액 대비(%)가 포함되어 있어 원문에서 직접 추출한다.
    """
    text = api.get_dart_document_text(e["rcept_no"])
    if not text:
        return ""
    import re
    lines = [ln.strip() for ln in text.splitlines() if ln.strip()]

    def _next_num(i, limit=6):
        for nxt in lines[i + 1:i + 1 + limit]:
            n = _num(nxt.replace(" ", "").rstrip("%"))
            if n is not None and n != 0:
                return n
        return None

    def _next_text(i, limit=4):
        for nxt in lines[i + 1:i + 1 + limit]:
            t = nxt.strip()
            if t and t != "-" and _num(t.replace(" ", "")) is None:
                return t
        return None

    amount = sales_pct = end_date = None
    counterparty = None
    for i, ln in enumerate(lines):
        compact = ln.replace(" ", "")
        if amount is None and ("계약금액총액" in compact or compact == "계약금액" or "계약금액(원)" in compact):
            amount = _next_num(i)
        elif sales_pct is None and "매출액대비" in compact:
            sales_pct = _next_num(i)
        elif counterparty is None and ("계약상대방" in compact or compact == "계약상대"):
            counterparty = _next_text(i)
        elif end_date is None and compact in ("종료일", "종료일자"):
            for nxt in lines[i + 1:i + 5]:
                m = re.search(r"(20\d{2})[.\-/년\s]*(\d{1,2})[.\-/월\s]*(\d{1,2})", nxt)
                if m:
                    end_date = f"{m.group(1)}-{int(m.group(2)):02d}-{int(m.group(3)):02d}"
                    break
    parts = []
    if amount:
        parts.append(f"계약 {_fmt_eok(amount)}")
    if sales_pct:
        color = "red" if sales_pct >= 10 else "yellow"
        parts.append(f"[{color}]매출대비 {sales_pct:.1f}%[/]")
    if counterparty:
        parts.append(counterparty[:16])
    if end_date:
        parts.append(f"~{end_date}")
    return " · ".join(parts)


def _treasury_note(e):
    """자기주식 취득/처분/신탁 결정 -> 예정금액·수량·기간 요약."""
    bgn, end = _detail_date_range(e)
    rows = api.get_dart_treasury_decisions(e["code"], bgn, end)
    row = next((r for r in rows if r.get("rcept_no") == e["rcept_no"]), rows[0] if rows else None)
    if not row:
        return ""
    parts = [row["kind"]]
    if row.get("amount"):
        parts.append(f"예정 {_fmt_eok(row['amount'])}")
    if row.get("qty"):
        parts.append(f"{row['qty']:,.0f}주")
    if row.get("bgd") and row.get("edd"):
        parts.append(f"{row['bgd'][:10]}~{row['edd'][:10]}")
    return " · ".join(parts)


def _free_increase_note(e):
    """무상증자결정 -> 1주당 배정·신주 수·기준일 요약."""
    bgn, end = _detail_date_range(e)
    rows = api.get_dart_free_increase_detail(e["code"], bgn, end)
    row = next((r for r in rows if r.get("rcept_no") == e["rcept_no"]),
               max(rows, key=lambda r: r.get("rcept_no", "")) if rows else None)
    if not row:
        return ""
    parts = []
    ratio = _num(row.get("nstk_ascnt_ps_ostk"))
    if ratio:
        parts.append(f"1주당 {ratio:g}주 배정")
    new_cnt = _num(row.get("nstk_ostk_cnt"))
    if new_cnt:
        parts.append(f"신주 {new_cnt:,.0f}주")
    std = (row.get("nstk_asstd") or "").strip()
    if std:
        parts.append(f"기준일 {std[:10]}")
    return " · ".join(parts)


def _capital_reduction_note(e):
    """감자결정 -> 감자비율·방법·기준일 요약."""
    bgn, end = _detail_date_range(e)
    rows = api.get_dart_capital_reduction_detail(e["code"], bgn, end)
    row = next((r for r in rows if r.get("rcept_no") == e["rcept_no"]),
               max(rows, key=lambda r: r.get("rcept_no", "")) if rows else None)
    if not row:
        return ""
    parts = []
    rt = _num(row.get("cr_rt_ostk"))
    if rt:
        parts.append(f"감자비율 {rt:g}%")
    mth = " ".join((row.get("cr_mth") or "").split())
    if mth:
        parts.append(mth[:16])
    std = (row.get("cr_std") or "").strip()
    if std:
        parts.append(f"기준일 {std[:10]}")
    return " · ".join(parts)


def build_detail_note(e):
    """공시 1건의 상세 요약 문자열 (부적합/실패 시 '')."""
    try:
        nm = e["report_nm"]
        if e["category"] == "실적·IR" and any(k in nm for k in _EARNINGS_DOC_KEYWORDS):
            return _earnings_note(e)
        if e["category"] == "증자·감자" and "유상증자" in nm:
            return _paid_increase_note(e)
        if e["category"] == "증자·감자" and "감자" in nm:
            return _capital_reduction_note(e)
        if e["category"] == "메자닌(CB/BW)":
            return _bond_note(e)
        if e["category"] == "수주·공급계약" and "공급계약" in nm:
            return _supply_contract_note(e)
        if e["category"] == "자기주식" and "결정" in nm:
            return _treasury_note(e)
        if e["category"] == "무상증자" and "결정" in nm:
            return _free_increase_note(e)
    except Exception:
        pass
    return ""


def _enrich_details(events, limit=_DETAIL_LIMIT, progress=None, task=None, quiet=False):
    """표시 대상 공시 중 상세정보 대상만 병렬 조회해 e['note']를 채운다.

    progress/task를 받으면 새 진행바를 만들지 않고 그 막대를 이어서 채운다. 이때
    total은 '이미 끝난 양 + 남은 대상 수'로 다시 잡는다. 호출측이 상세 단계 몫까지
    미리 예약해 두므로(_DETAIL_LIMIT) 퍼센트가 뒤로 되감기지 않고, 설명 문구도
    바꾸지 않아 하나의 막대로 보인다. (단계마다 라벨이 바뀌고 퍼센트가 되감기면
    사용자에게는 진행바가 두 개 뜬 것처럼 보인다.)
    quiet=True면 진행바를 만들지 않는다 — _gather의 같은 인자와 뜻이 같다.
    """
    targets = [e for e in events if _detail_eligible(e)][:limit]
    if not targets:
        return
    with contextlib.ExitStack() as stack:
        if progress is None and not quiet:
            progress = stack.enter_context(_make_progress())
            task = progress.add_task(_PROGRESS_LABEL, total=len(targets))
        elif progress is not None:
            progress.update(task, total=_task_done(progress, task) + len(targets))
        with concurrent.futures.ThreadPoolExecutor(max_workers=4) as ex:
            futs = {ex.submit(build_detail_note, e): e for e in targets}
            for fut in concurrent.futures.as_completed(futs):
                try:
                    futs[fut]["note"] = fut.result()
                except Exception:
                    futs[fut]["note"] = ""
                if progress is not None:
                    progress.advance(task)


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
    # [진행바] 조회·상세 두 단계가 하나의 막대를 공유한다(단계마다 새 막대가 뜨지 않게).
    shown = []
    with _make_progress() as progress:
        # 상세 단계 몫(_DETAIL_LIMIT)을 total에 미리 얹어 막대가 100%까지 찼다가
        # 되감기는 일이 없게 한다. 되감기면 새 진행바가 뜬 것처럼 보인다.
        task = progress.add_task(_PROGRESS_LABEL, total=len(codes) + _DETAIL_LIMIT)
        events = _gather(codes, days, min_level=1, progress=progress, task=task)
        if events:
            events.sort(key=lambda e: (e["level"], e["date"]), reverse=True)  # 중요도↓, 최신순
            shown = events[:40]
            _enrich_details(shown, progress=progress, task=task)
        progress.update(task, total=max(_task_done(progress, task), 1))  # 예약분 정리

    if not events:
        config.console.print(f"[dim]최근 {days}일간 주요 공시가 없습니다. (임원·지분 등 일반 공시는 제외)[/dim]")
        return

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


# 한 번에 보낼 최대 건수. 전송 계층이 4000자마다 쪼개는데(telegram_notify), 본문 길이만으로
#  잡으면 안 된다 — 종목코드가 <a href> 링크로 바뀌며 건당 60자 넘게 불어난다(실측 20건
#  기준 본문 2,600자 → 전송 4,320자로 두 조각). 링크 팽창까지 넣어 한 통에 들어가게 잡는다.
_TELEGRAM_LIMIT = 15


def build_telegram_message(days=14, min_level=1, limit=_TELEGRAM_LIMIT):
    """텔레그램 /disclosure 용 메시지 — 관심종목 최근 공시 (메뉴 6-6과 같은 소스).

    화면 6-6과 같은 수집·분류·상세추출을 쓰되 표 대신 줄글로 낸다. 다른 점은 둘이다.
      · 진행바를 만들지 않는다(quiet) — 운영자 콘솔에 남의 진행바가 끼어들면 안 된다.
      · 건수를 limit으로 끊는다(_TELEGRAM_LIMIT 주석 참조). 40건을 그대로 보내면
        조각 메시지가 줄줄이 오고, 정작 중요한 위쪽이 묻힌다.
    AI 요약(_maybe_ai_summary)은 붙이지 않는다 — 대화형 확인 절차라 봇 경로와 맞지 않는다.
    """
    codes = _kr_watchlist()
    if not codes:
        return "📄 [공시 모니터링]\n등록된 국내 관심종목이 없습니다."
    if not config.DART_API_KEY:
        return ("📄 [공시 모니터링]\n⚠️ DART API 키가 설정되지 않았습니다. "
                "(환경변수 DART_API_KEY)")

    events = _gather(codes, days, min_level, quiet=True)
    if not events:
        return (f"📄 [공시 모니터링] 최근 {days}일\n"
                f"주요 공시가 없습니다. (임원·지분 등 일반 공시는 제외)")

    events.sort(key=lambda e: (e["level"], e["date"]), reverse=True)  # 중요도↓, 최신순
    shown = events[:limit]
    _enrich_details(shown, quiet=True)

    lines = [f"📄 [공시 모니터링] 최근 {days}일 · {len(events)}건", ""]
    for e in shown:
        lines.append(f"{e['icon']} {_fmt_date(e['date'])} {e['name']} ({e['code']}) — {e['category']}")
        lines.append(f"  {e['report_nm']}")
        note = e.get("note", "")
        if note:
            lines.append(f"  · 상세: {note}")
        rcept = e.get("rcept_no")
        if rcept:
            lines.append(f"  {DART_VIEWER_URL.format(rcept)}")
        lines.append("")

    if len(events) > len(shown):
        lines.append(f"※ 중요도순 상위 {len(shown)}건만 표시했습니다 (전체 {len(events)}건).")
    lines.append("🔴 중대  🟠 증자/메자닌  🟢 호재성  🟡 이벤트  🔵 실적")
    return "\n".join(lines)


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
