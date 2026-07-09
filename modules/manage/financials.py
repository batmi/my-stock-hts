# modules/manage/financials.py
"""관심종목 재무 스냅샷 (OpenDART fnlttSinglAcnt 단일회사 주요계정 기반).

법정 제출기한이 지난 가장 최근 정기보고서를 자동 선택해
매출액·영업이익·당기순이익과 전기(동기) 대비 증감률을 표시한다.
"""
import concurrent.futures
from datetime import date

from rich.table import Table
from rich import box
from rich.progress import Progress, SpinnerColumn, TextColumn, BarColumn

import config
import api
import utils

_REPRT_LABEL = {"11011": "사업(연간)", "11012": "반기", "11013": "1분기", "11014": "3분기"}


def _kr_stocks():
    """관심종목 중 국내 주식만 [(code, name), ...]. (ETF는 재무제표 없음)"""
    out = []
    for s in config.session.stock_data.get("stocks_kr", []):
        if s.get("code"):
            out.append((s["code"], s.get("name", s["code"])))
    return out


def _report_candidates(today):
    """오늘 기준 공시가 완료됐을 정기보고서 후보 (최신 우선, 최대 3개).

    법정 제출기한: 1분기 5/15, 반기 8/14, 3분기 11/14, 사업보고서 익년 3/31.
    """
    y = today.year
    schedule = [
        (date(y, 11, 15), (y, "11014")),
        (date(y, 8, 15), (y, "11012")),
        (date(y, 5, 16), (y, "11013")),
        (date(y, 4, 1), (y - 1, "11011")),
        (date(y - 1, 11, 15), (y - 1, "11014")),
        (date(y - 1, 8, 15), (y - 1, "11012")),
    ]
    return [cand for deadline, cand in schedule if today >= deadline][:3]


def _num(s):
    try:
        t = str(s).replace(",", "").strip()
        return float(t) if t not in ("", "-") else None
    except Exception:
        return None


def _extract_is(rows):
    """주요계정 rows에서 손익계산서(매출/영업이익/순이익) 추출. 연결(CFS) 우선."""
    result = {}
    for fs in ("CFS", "OFS"):
        for r in rows:
            if r.get("fs_div") != fs or r.get("sj_div") != "IS":
                continue
            nm = (r.get("account_nm") or "").replace(" ", "")
            key = None
            if nm in ("매출액", "영업수익", "수익(매출액)"):
                key = "rev"
            elif nm in ("영업이익", "영업이익(손실)"):
                key = "op"
            elif nm in ("당기순이익", "당기순이익(손실)", "분기순이익", "반기순이익",
                        "분기순이익(손실)", "반기순이익(손실)"):
                key = "net"
            if key and key not in result:
                result[key] = (_num(r.get("thstrm_amount")), _num(r.get("frmtrm_amount")),
                               (r.get("thstrm_nm") or "").strip())
        if result:
            result["fs"] = "연결" if fs == "CFS" else "개별"
            return result
    return None


def _collect(code, name, candidates):
    """종목별 최신 이용 가능 보고서의 손익 요약."""
    for year, reprt in candidates:
        rows = api.get_dart_financials(code, year, reprt)
        if not rows:
            continue
        data = _extract_is(rows)
        if data:
            return {
                "code": code, "name": name,
                "basis": f"{year} {_REPRT_LABEL[reprt]}·{data['fs']}",
                "rev": data.get("rev"), "op": data.get("op"), "net": data.get("net"),
            }
    return None


def _fmt_cell(pair):
    """(당기, 전기) -> '1,234억 (+12.3%)' 셀 문자열."""
    if not pair or pair[0] is None:
        return "-"
    cur, prev = pair[0], pair[1]
    cell = f"{cur / 1e12:,.1f}조" if abs(cur) >= 1e13 else f"{cur / 1e8:,.0f}억"
    if prev not in (None, 0):
        if prev < 0 and cur >= 0:
            tag, color = "흑전", "red"
            cell += f" [{color}]({tag})[/]"
        elif prev > 0 and cur < 0:
            cell += " [blue](적전)[/]"
        else:
            pct = (cur - prev) / abs(prev) * 100
            color = "red" if pct >= 0 else "blue"
            cell += f" [{color}]({pct:+.1f}%)[/]"
    return cell


def show_financial_snapshot():
    """관심종목 재무 스냅샷 (최근 정기보고서 손익 + 전기 대비)."""
    utils.clear_screen()
    config.console.print("\n[bold cyan][관심종목 재무 스냅샷][/bold cyan]\n")

    codes = _kr_stocks()
    if not codes:
        config.console.print("[yellow]등록된 국내 관심종목(주식)이 없습니다.[/yellow]")
        return
    if not config.DART_API_KEY:
        config.console.print("[yellow]※ DART API 키가 설정되지 않았습니다. (환경변수 DART_API_KEY)[/yellow]")
        return

    candidates = _report_candidates(date.today())
    rows = []
    with Progress(SpinnerColumn(), TextColumn("[progress.description]{task.description}"),
                  BarColumn(), console=config.console, transient=True) as progress:
        task = progress.add_task("[cyan]재무 정보 조회 중...[/cyan]", total=len(codes))
        with concurrent.futures.ThreadPoolExecutor(max_workers=8) as ex:
            futs = [ex.submit(_collect, c, n, candidates) for c, n in codes]
            for fut in concurrent.futures.as_completed(futs):
                try:
                    r = fut.result()
                    if r:
                        rows.append(r)
                except Exception:
                    pass
                progress.advance(task)

    if not rows:
        config.console.print("[dim]조회된 재무 정보가 없습니다.[/dim]")
        return

    rows.sort(key=lambda r: (r["rev"][0] if r.get("rev") and r["rev"][0] else 0), reverse=True)

    config.console.print("[dim]※ 가장 최근 정기보고서(연결 우선) 기준, 괄호는 전기(동기) 대비 증감률입니다. 분기·반기 손익은 누적 기준일 수 있습니다.[/dim]\n")
    table = Table(box=box.HORIZONTALS, header_style="dim", border_style="dim")
    table.add_column("종목", justify="left")
    table.add_column("기준", justify="center")
    table.add_column("매출액", justify="right")
    table.add_column("영업이익", justify="right")
    table.add_column("당기순이익", justify="right")
    for r in rows:
        table.add_row(
            f"{r['name']} ({r['code']})",
            r["basis"],
            _fmt_cell(r.get("rev")),
            _fmt_cell(r.get("op")),
            _fmt_cell(r.get("net")),
        )
    config.console.print(table)
    config.console.print()
