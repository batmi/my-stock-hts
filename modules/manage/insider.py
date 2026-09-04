# modules/manage/insider.py
"""관심종목 수급 · 물량 신호 종합 (OpenDART 기반).

- 자기주식 취득/처분/신탁계약 결정: 회사 단위 수급 신호 (내부자 개인 매매보다 강함)
- 메자닌(CB/BW/EB) 오버행: 전환가 vs 현재가로 잠재 매도물량 감시 (+전환청구권행사 감지)
- 무상증자 결정: 단기 모멘텀 이벤트
- 임원·주요주주 소유상황 / 대량보유(5%) 보고: 내부자 지분 변동 추적
"""
import concurrent.futures
from collections import defaultdict
from datetime import datetime, timedelta

from rich.table import Table
from rich import box
from rich.progress import Progress, SpinnerColumn, TextColumn, BarColumn

import config
import api
from core import utils
from modules.manage.scan import ScanFailures

def _show_fake_progress(desc, count=1):
    """테이블 출력 전 짧은 시각적 분리를 위한 프로그래스바 애니메이션."""
    with Progress(
        SpinnerColumn(), TextColumn("[progress.description]{task.description}"),
        BarColumn(), TextColumn("[progress.percentage]{task.percentage:>3.0f}%"), console=config.console, transient=True
    ) as progress:
        task = progress.add_task(f"[cyan]{desc}[/cyan]", total=max(count, 1))
        import time
        for _ in range(max(count, 1)):
            time.sleep(0.02)
            progress.advance(task)


_MEZZ_LOOKBACK_DAYS = 365 * 3   # 메자닌 오버행 발행 이력 조회 기간 (통상 만기 3~5년)
_EXERCISE_LOOKBACK_DAYS = 90    # 전환청구권/신주인수권 행사 공시 감지 기간

# 회사 일괄 이벤트(우리사주·스톡그랜트·무상신주·주식배당) 판정 기준.
# 임원 다수가 같은 날 같은 방향으로 보고하면 개별 매매가 아니라 회사가 일괄 배정한 것이다.
# 실측(2026-07-22): 삼성전자 2026-07-21 743명 동시 증가, SK텔레콤 50명, 현대로템 37명.
# 반면 SK하이닉스 2026-06-09는 5명이지만 3증가/2감소로 방향이 갈려 실제 매매다.
_BULK_MIN_REPORTERS = 5
_BULK_SAME_DIR_RATIO = 0.9


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
    # keep_baseline: cutoff 직전 보고 1건을 보유수량 기준선으로 함께 받아
    # 기간 첫 보고의 실제 증감을 차분으로 복원한다(_apply_real_chg).
    for r in api.get_dart_insider_trades(code, since=cutoff, keep_baseline=True):
        if r["rcept_dt"] >= cutoff or r.get("baseline"):
            r = dict(r, code=code, name=name)
            insiders.append(r)
    for r in api.get_dart_major_holdings(code):
        if r["rcept_dt"] >= cutoff:
            r = dict(r, code=code, name=name)
            majors.append(r)
    return insiders, majors


def _get_rem_shares(code):
    """최근 전환청구권행사 공시를 파싱하여 회차별 잔존 주식수 맵을 반환. 없으면 None."""
    import re
    docs = api.get_dart_disclosures(code, days=730)
    exercise_doc = None
    for d in docs:
        nm = d.get('report_nm', '')
        if '전환청구권행사' in nm or '신주인수권행사' in nm or '전환청구권 행사' in nm:
            exercise_doc = d
            break
            
    if not exercise_doc:
        return None, None
        
    text = api.get_dart_document_text(exercise_doc['rcept_no'])
    text_clean = re.sub('<[^<]+>', '\n', text)
    lines = [line.strip() for line in text_clean.split('\n') if line.strip()]
    
    start_idx = -1
    for i, line in enumerate(lines):
        if '미전환사채 잔액' in line or '전환사채 잔액' in line or '미상환 잔액' in line or '미상환 사채' in line:
            if i + 3 < len(lines) and (str(lines[i+1]).isdigit() or str(lines[i+2]).isdigit() or str(lines[i+3]).isdigit()):
                start_idx = i
                break
    if start_idx == -1:
        for i, line in enumerate(lines):
            if line in ['1', '2', '3', '4', '5'] and i + 1 < len(lines) and '000,000,000' in lines[i+1]:
                start_idx = i - 5
                break
                
    if start_idx == -1:
        return None, None
        
    rem_map = {}
    idx = start_idx
    while idx < len(lines):
        line = lines[idx]
        if line in ['1', '2', '3', '4', '5', '6', '7', '8', '9', '10']:
            round_num = line
            offset = 1
            while idx+offset < len(lines) and 'KRW' not in lines[idx+offset]:
                offset += 1
            offset2 = offset + 1
            while idx+offset2 < len(lines) and 'KRW' not in lines[idx+offset2]:
                offset2 += 1
                
            if idx+offset2+2 < len(lines):
                shares_str = lines[idx+offset2+2].replace(',', '')
                if shares_str.isdigit():
                    rem_map[round_num] = int(shares_str)
            idx += offset2 + 2
        else:
            idx += 1
        if idx > start_idx + 150:
            break
            
    return rem_map, exercise_doc['rcept_dt'].replace('-', '')


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
                "round_num": str(r.get("bd_tm", "")).strip()
            })

    exercised = 0
    cur_price = None
    total_shares = None
    if mezz:
        rem_map, exercise_dt = _get_rem_shares(code)
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
        #  [Fix 2026-09-04] 잔존 물량을 모를 때는 **전량 남았다고 본다**. 종전에는
        #   rem_map.get(round, 0) 이라 회차 번호가 맵에 없으면 오버행이 조용히 '0주'가 됐다.
        #   _get_rem_shares 는 공시 원문을 줄 번호 산술로 훑는 휴리스틱이라 부분적으로만
        #   채워지는 일이 실제로 있고(회차 표기가 '3' 이 아니라 '제3회'인 경우 등), 그때
        #   잠재 매도물량이 없는 것처럼 보인다. 오버행은 '모르면 위험 쪽'으로 두어야 한다.
        #   대신 확인된 값이 아니라는 사실을 표에 함께 밝힌다(rem_estimated).
        if not rem_map or m["rcept_dt"] > exercise_dt:
            m["rem_shares"] = m["shares"]
            m["rem_estimated"] = not rem_map
        else:
            found = rem_map.get(m["round_num"])
            m["rem_shares"] = m["shares"] if found is None else found
            m["rem_estimated"] = found is None

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
    failures = ScanFailures("수급·물량")
    with Progress(SpinnerColumn(), TextColumn("[progress.description]{task.description}"),
                  BarColumn(), TextColumn("[progress.percentage]{task.percentage:>3.0f}%"), console=config.console, transient=True) as progress:
        task = progress.add_task("[cyan]자기주식 취득·처분 내역 조회 중...[/cyan]", total=len(codes) * 2)
        with concurrent.futures.ThreadPoolExecutor(max_workers=8) as ex:
            futs = {ex.submit(_collect, c, n, cutoff): ("insider", c) for c, n in codes}
            futs.update({ex.submit(_collect_supply, c, n, days): ("supply", c) for c, n in codes})
            for fut in concurrent.futures.as_completed(futs):
                kind, fcode = futs[fut]
                try:
                    if kind == "insider":
                        ins, majs = fut.result()
                        insiders.extend(ins)
                        majors.extend(majs)
                    else:
                        tr, fr, mz = fut.result()
                        treasury.extend(tr)
                        frees.extend(fr)
                        mezz.extend(mz)
                except Exception as e:      # noqa: BLE001 - 일부라도 보여 주되 실패는 밝힌다
                    failures.record(fcode, e)
                progress.advance(task)

    #  실패를 먼저 밝힌다. '보고가 없습니다'보다 앞이어야 한다 — 뒤에 두면 그 문장을
    #  읽고 화면을 닫는다. 오버행·자기주식은 '없다'로 읽는 순간 판단이 반대로 간다.
    failures.announce()

    if not any((insiders, majors, treasury, frees, mezz)):
        if not failures:
            config.console.print(f"[dim]최근 {days}일간 수급·물량 관련 보고가 없습니다.[/dim]")
        else:
            config.console.print("[dim]조회에 성공한 종목에는 수급·물량 보고가 없습니다.[/dim]")
        return

    # 실제 증감(차분)·일괄 이벤트 판정은 요약/상세가 같은 기준을 쓰도록 여기서 한 번만 계산
    _apply_real_chg(insiders)
    bulk = _bulk_event_keys(insiders)

    # 이 각주는 특정 표가 아니라 아래 출력 전체에 걸리는 읽는 법이다 — 표 밑에 붙여 두면
    #  마지막 표(대주주)만의 단서로 읽힌다. 들여쓰기 없이 표보다 먼저 둔다.
    config.console.print("[dim]※ 증감(+)=취득, (-)=처분 · DART는 변동사유를 제공하지 않아 "
                         "일괄 지급·재보고를 패턴으로 걸러냅니다 · 원문: dart.fss.or.kr[/dim]\n")
    _render_treasury(treasury)
    _render_free_increase(frees)
    _render_overhang(mezz)
    _render_summary(insiders, bulk=bulk)
    _render_insiders(insiders, bulk=bulk)
    _render_majors(majors)


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
    config.console.print("  [dim]※ 취득·신탁체결=매수 수급(+), 처분=잠재 매도물량(-). 소각 목적 여부는 공시 원문 확인.[/dim]\n")


def _render_free_increase(rows):
    """무상증자 결정 (단기 모멘텀 이벤트)."""
    if not rows:
        return
    _show_fake_progress("무상증자 결정 내역 정리 중...", len(rows))
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
    _show_fake_progress("메자닌 오버행 현황 분석 중...", len(rows))
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
    table.add_column("잔존물량(주)", justify="right")
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
        rem = r.get("rem_shares", r["shares"])
        #  rem 이 0 이면 '전량 전환 완료'라는 정보다 — '-'로 지우면 모른다는 뜻이 되어
        #  잔존을 확인 못 한 행과 구별되지 않는다.
        pct = f"{rem / ts * 100:.1f}%" if ts and rem is not None else "-"
        rem_str = f"{rem:,.0f}"
        if r.get("rem_estimated"):
            #  공시에서 잔존을 확인하지 못해 전량으로 본 값이다(과소평가하지 않기 위해서).
            rem_str = f"[dim]~[/]{rem_str}"
        if r.get("exercised"):
            status += f" [yellow]행사공시 {r['exercised']}건[/]"
        table.add_row(
            _fmt_date(r["rcept_dt"]), f"{r['name']} ({r['code']})", r["kind"],
            _fmt_eok(r["amount"]), f"{prc:,.0f}", f"{cur:,.0f}" if cur else "-",
            f"{r['shares']:,.0f}", rem_str, pct, status,
        )
    config.console.print(table)
    if len(rows) > limit:
        config.console.print(f"  [dim]… 외 {len(rows) - limit}건[/dim]")
    config.console.print("  [dim]※ 현재가 ≥ 전환가면 상승 시 전환·매도 물량(오버행)이 상방 저항이 될 수 있습니다. "
                         "전환가는 발행 결정 시점 기준(리픽싱 미반영), 행사공시=최근 90일 전환청구권·신주인수권 행사. "
                         "잔존물량 앞의 ~ 는 공시에서 확인하지 못해 전량으로 본 값입니다.[/dim]\n")


def _apply_real_chg(insiders):
    """보고된 '증감' 대신 보유수량 차분으로 실제 증감(r['real_chg'])을 채운다.

    DART 소유상황보고의 증감 칸은 신규 보고·재보고(지분율이 5%/10% 선을 다시 넘는 등)
    때 보유 전량이 그대로 들어온다. 실측(2026-07-22) 국민연금공단 한미약품:
      05-22 보유 1,269,470 / 증감 -137,654
      06-01 보유 1,281,813 / 증감 +1,281,813  ← 전량 재기재. 실제 변동은 +12,343
    이 값을 그대로 합산하면 90일 순증감이 +2,458,181(순취득)로 뒤집혀 나오지만
    차분으로 계산하면 -88,036(순처분)이 실제다. 그래서 직전 보고 대비 차분을 쓴다.

    직전 보고가 전혀 없는 최초 보고는 차분을 낼 수 없어 보고값을 그대로 쓰되,
    증감이 보유 전량과 같으면(= 전량 기재로 의심) 0으로 둔다. 신규 임원의 소액
    매수를 놓치는 손실보다 수백만 주 허위 취득 신호를 내는 쪽이 훨씬 위험하다.
    """
    seq = defaultdict(list)
    for r in insiders:
        seq[(r["code"], r["repror"])].append(r)
    for rows in seq.values():
        rows.sort(key=lambda r: (r["rcept_dt"], r["rcept_no"]))
        prev = None
        for r in rows:
            qty, chg = r["qty"], r["chg"] or 0
            if prev is not None and qty is not None:
                r["real_chg"] = qty - prev
            elif qty is not None and chg > 0 and abs(chg - qty) < 1e-9:
                r["real_chg"] = 0.0        # 최초 보고에 보유 전량이 증감으로 기재됨
            else:
                r["real_chg"] = float(chg)
            if qty is not None:
                prev = qty


def _bulk_event_keys(insiders):
    """회사 일괄 이벤트로 판정된 (종목코드, 접수일) 집합.

    우리사주·스톡그랜트·무상신주·주식배당은 임원 수십~수백 명이 같은 날 같은 방향으로
    보고된다. 개별 매매 의사와 무관하므로 수급 신호에서 빼야 한다.
    """
    groups = defaultdict(list)
    for r in insiders:
        if not r.get("baseline"):
            groups[(r["code"], r["rcept_dt"])].append(r.get("real_chg") or 0)
    keys = set()
    for key, chgs in groups.items():
        if len(chgs) < _BULK_MIN_REPORTERS:
            continue
        up = sum(1 for c in chgs if c > 0)
        dn = sum(1 for c in chgs if c < 0)
        # 방향 비율은 변동이 있는 보고만 놓고 본다. 차분 보정으로 0이 된 재보고가
        # 분모에 끼면 일괄 지급인데도 비율이 희석되어 통과해 버린다.
        moved = up + dn
        if moved and max(up, dn) >= moved * _BULK_SAME_DIR_RATIO:
            keys.add(key)
    return keys


def _render_summary(insiders, bulk=None):
    """종목별 임원·주요주주 순증감 요약 (매매 외 사유 제외, 실제 증감 기준)."""
    if not insiders:
        return
    bulk = _bulk_event_keys(insiders) if bulk is None else bulk

    agg = {}
    excluded = 0
    for r in insiders:
        if r.get("baseline"):
            continue                       # 기준선 전용 행(기간 이전 보고)
        if (r["code"], r["rcept_dt"]) in bulk:
            excluded += 1
            continue
        key = (r["code"], r["name"])
        a = agg.setdefault(key, {"net": 0.0, "cnt": 0, "last": ""})
        a["net"] += r.get("real_chg") or 0
        a["cnt"] += 1
        if r["rcept_dt"] > a["last"]:
            a["last"] = r["rcept_dt"]
    if not agg:
        return
    # 이 화면의 다른 표와 같이 최신순. 같은 날이면 순증감 규모가 큰 쪽을 위로.
    rows = sorted(agg.items(), key=lambda kv: (kv[1]["last"], abs(kv[1]["net"])), reverse=True)

    _show_fake_progress("내부자 순증감 요약 분석 중...", len(rows))
    config.console.print("[bold]▸ 종목별 내부자 순증감 요약[/bold] [dim](매매 외 사유 제외)[/dim]")
    table = Table(box=box.HORIZONTALS, header_style="dim", border_style="dim")
    table.add_column("최근 보고일", justify="center")   # 다른 표와 같이 일자를 맨 앞에 둔다
    table.add_column("종목", justify="left")
    table.add_column("보고 건수", justify="center")
    table.add_column("순증감(주)", justify="right")
    table.add_column("신호", justify="center")
    for (code, name), a in rows:
        net = a["net"]
        signal = ("[red]▲ 순취득[/]" if net > 0 else
                  "[blue]▼ 순처분[/]" if net < 0 else "[dim]중립[/dim]")
        table.add_row(_fmt_date(a["last"]), f"{name} ({code})",
                      str(a["cnt"]), _fmt_chg(net), signal)
    config.console.print(table)
    if excluded:
        config.console.print(f"  [dim]※ 순증감은 보유수량 차분 기준(신규·재보고의 전량 기재 제외). "
                             f"임원 {_BULK_MIN_REPORTERS}인 이상이 같은 날 같은 방향으로 보고한 "
                             f"일괄 지급·배정 {excluded}건은 매매가 아니라 제외했습니다.[/dim]")
    else:
        config.console.print("  [dim]※ 순증감은 보유수량 차분 기준(신규·재보고의 전량 기재 제외).[/dim]")
    config.console.print()


def _render_insiders(insiders, limit=30, bulk=None):
    """임원·주요주주 소유상황 보고 상세 (최신순, 매매 외 사유 제외).

    일괄 지급·배정 건은 제외한다. 제외하지 않으면 우리사주 지급 한 번에 수백 건이
    쏟아져(실측: 삼성전자 743건) 최신순 상위 목록을 통째로 덮어버린다.
    """
    bulk = _bulk_event_keys(insiders) if bulk is None else bulk
    # 기준선 행은 기간 밖 보조 데이터라 '제외 건수'에 넣지 않는다
    dropped = sum(1 for r in insiders
                  if not r.get("baseline") and (r["code"], r["rcept_dt"]) in bulk)
    insiders = [r for r in insiders
                if not r.get("baseline") and (r["code"], r["rcept_dt"]) not in bulk]
    _show_fake_progress("임원·주요주주 소유상황 정리 중...", len(insiders))
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
        real = r.get("real_chg")
        chg_str = _fmt_chg(real if real is not None else r["chg"])
        # 보고값이 전량 기재라 차분과 다르면 원인을 알 수 있게 표시
        if real is not None and abs(real - (r["chg"] or 0)) > 1e-9:
            chg_str += " [dim](재보고)[/dim]"
        table.add_row(
            _fmt_date(r["rcept_dt"]),
            f"{r['name']} ({r['code']})",
            r["repror"] or "-",
            who,
            chg_str,
            f"{r['qty']:,.0f}" if r["qty"] is not None else "-",
            f"{r['rate']:.2f}%" if r["rate"] is not None else "-",
        )
    config.console.print(table)
    if len(insiders) > limit:
        config.console.print(f"  [dim]… 외 {len(insiders) - limit}건[/dim]")
    if dropped:
        config.console.print(f"  [dim]※ 일괄 지급·배정 등 매매 외 보고 {dropped}건은 제외했습니다.[/dim]")
    config.console.print()


def _render_majors(majors, limit=20):
    """대량보유(5%) 상황 보고 상세 (최신순)."""
    _show_fake_progress("대량보유 상황 정리 중...", len(majors))
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
