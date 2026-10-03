# modules/chart_batch.py
"""[3]-7 일괄 차트 생성 — 여러 종목을 골라 같은 유형의 분석 차트를 한 번에 그린다.

[설계 · 2026-10-03]
 · 화질은 단건과 같다. generate_visual_chart 에 dpi 를 넘기지 않아 CHART_DPI 를 그대로 쓴다
   (텔레그램 전송처럼 dpi=100 으로 낮추는 경로와 다르다).
 · 한 장씩 순차로 그린다. 렌더는 어차피 _serialized_render 락으로 직렬이고, 병렬로 돌리면
   데이터 조회만 겹쳐 TPS 한도와 1GB 라즈베리파이의 메모리 봉우리를 키운다.
 · 한 종목의 실패가 나머지를 막지 않는다. 결과는 종목별로 모아 끝에 표 하나로 보여준다.
 · 그리기 전에 막히는 조합(토스 시봉, 지수 시봉·분봉)은 '건너뜀'으로 분류한다 — 단건 경로의
   안내문이 종목 수만큼 반복되지 않게 한다.
 · 뷰어는 열지 않는다(open_file=False). --webchart 면 갤러리에 쌓이고, 아니면 저장 경로를 알린다.
 · 로직(generate_batch_charts)과 화면(batch_chart_menu)을 나눈다 — 다른 진입점(텔레그램 등)이
   로직만 다시 쓸 수 있게.
"""
import logging
import os
import re

from rich.progress import Progress, SpinnerColumn, BarColumn, TextColumn, MofNCompleteColumn, TimeElapsedColumn
from rich.prompt import Prompt
from rich.table import Table
from rich import box
from rich.markup import escape

import api
import config
from core import context
from core import utils
from modules import chart, web_dashboard

logger = logging.getLogger(__name__)

STATUS_OK = "ok"
STATUS_SKIP = "skip"
STATUS_FAIL = "fail"
STATUS_CANCEL = "cancel"

PERIOD_LABEL = {"weekly": "주봉", "daily": "일봉", "hourly": "시봉", "intraday": "분봉"}

# 국내 종목코드: 6자리, 첫 자리는 숫자(0080G0 같은 문자 포함 신규 코드까지)
_KR_CODE_RE = re.compile(r"^\d[0-9A-Z]{5}$")


def skip_reason(code, period_type):
    """그리기 전에 막히는 조합이면 사유, 아니면 None. generate_visual_chart 의 사전 차단과 같은 규칙."""
    if period_type == "hourly" and config.session.is_toss:
        return "토스는 시봉 미제공"
    if period_type in ("hourly", "intraday") and api.index_source_kind(code):
        return "지수는 일봉·주봉만"
    return None


def generate_batch_charts(targets, period_type="daily", months=6, on_progress=None):
    """targets([{code, name, ovs}]) 를 순서대로 그린다. 종목별 결과 리스트를 돌려준다.

    결과 항목: {code, name, ovs, status(ok/skip/fail/cancel), path, reason}
    on_progress(i, target) 는 i번째 종목을 그리기 직전에 불린다(건너뜀 포함).
    Ctrl+C 를 받으면 거기서 멈추고, 이미 그린 차트는 남긴 채 나머지를 'cancel' 로 돌려준다.
    """
    results = []
    for i, t in enumerate(targets):
        code, name, ovs = t["code"], t["name"], t["ovs"]
        row = {"code": code, "name": name, "ovs": ovs, "status": STATUS_FAIL, "path": None, "reason": ""}
        if on_progress:
            on_progress(i, t)
        reason = skip_reason(code, period_type)
        if reason:
            row.update(status=STATUS_SKIP, reason=reason)
            results.append(row)
            continue
        try:
            path = chart.generate_visual_chart(code, name, ovs, open_file=False, quiet=True,
                                               period_type=period_type, months=months)
        except KeyboardInterrupt:
            logger.info(f"[chart_batch] 사용자 중단 — {i}/{len(targets)} 에서 멈춤")
            for rest in targets[i:]:
                results.append({"code": rest["code"], "name": rest["name"], "ovs": rest["ovs"],
                                "status": STATUS_CANCEL, "path": None, "reason": "사용자 중단"})
            break
        except Exception as e:      # noqa: BLE001 - 한 종목의 실패가 나머지를 막지 않는다
            logger.warning(f"[chart_batch] {name}({code}) 차트 생성 실패: {e}", exc_info=True)
            row["reason"] = f"오류: {e}"
            results.append(row)
            continue
        if path and os.path.exists(path):
            row.update(status=STATUS_OK, path=path)
        else:
            row["reason"] = "데이터 없음"
        results.append(row)
    return results


# ==========================================================
# [화면] 대상 바구니를 채우고 → 유형을 한 번 고르고 → 일괄 생성
# ==========================================================
_SOURCE_KEYS = {"1": "stocks_kr", "2": "etfs_kr", "3": "stocks_us", "4": "etfs_us"}


def _add_to_basket(basket, items):
    """중복((code, ovs))을 빼고 상한까지만 담는다. (담은 수, 상한 때문에 못 담은 수)."""
    seen = {(b["code"], b["ovs"]) for b in basket}
    added = dropped = 0
    for it in items:
        key = (it["code"], it["ovs"])
        if key in seen:
            continue
        if len(basket) >= config.BATCH_CHART_MAX:
            dropped += 1
            continue
        basket.append(it)
        seen.add(key)
        added += 1
    return added, dropped


def parse_direct_codes(text):
    """'005930, 000660 NVDA' → [(code, ovs)]. 국내 6자리 코드는 국내, 나머지는 해외 티커."""
    out = []
    for tok in re.split(r"[,\s]+", (text or "").strip()):
        if not tok:
            continue
        tok = tok.upper()
        out.append((tok, not bool(_KR_CODE_RE.match(tok))))
    return out


def _pick_direct():
    raw = Prompt.ask("종목코드·티커를 쉼표나 공백으로 구분해 입력 [dim](예: 005930, 000660, NVDA · 이전: b)[/dim]")
    config.console.print()
    if not raw or raw.strip().lower() in ("b", "q"):
        return []
    items = []
    for code, ovs in parse_direct_codes(raw):
        try:
            name = api.get_stock_name_by_code(code, ovs) or code
        except Exception as e:      # noqa: BLE001 - 이름은 표시용, 못 찾으면 코드로 그린다
            logger.debug(f"[chart_batch] 종목명 조회 실패({code}): {e}")
            name = code
        items.append({"code": code, "name": name, "ovs": ovs})
    return items


def _pick_indices():
    from modules import market
    dict_list = [{"name": n, "code": c} for n, c in market.selectable_indices()]
    picked = utils.select_multiple_from_list(
        dict_list, title="시장 지수 목록", display_func=lambda i, s: f"[{i+1}] {s.get('name', 'Unknown')}")
    items = []
    for it in picked or []:
        code, ovs = market.resolve_index_source(it["name"], it["code"])
        items.append({"code": code, "name": it["name"], "ovs": ovs})
    return items


def _pick_from_watchlist(src):
    s_list = config.session.stock_data.get(_SOURCE_KEYS[src], [])
    if not s_list:
        config.console.print("[yellow]목록이 비어있습니다.[/yellow]")
        utils.pause()
        return []
    picked = utils.select_multiple_from_list(s_list, title="관심종목 목록")
    ovs = src in ("3", "4")
    return [{"code": it["code"], "name": it["name"], "ovs": ovs} for it in picked or []]


def _basket_text(basket):
    if not basket:
        return f"[dim]선택된 종목 없음 — 목록에서 여러 종목을 담은 뒤 [8] 로 생성합니다 (최대 {config.BATCH_CHART_MAX}개)[/dim]\n"
    names = ", ".join(escape(b["name"]) for b in basket)
    return f"[bold cyan]선택 {len(basket)}/{config.BATCH_CHART_MAX}개:[/bold cyan] {names}\n"


def _ask_period():
    """(period_type, months). 취소면 None. 단건 메뉴와 같은 선택지·기본값."""
    menu_items_type = [("1", "주봉", "Weekly"), ("2", "일봉", "Daily"), ("3", "시봉", "Hourly"), ("4", "분봉", "Intraday")]
    c_type = utils.show_menu("차트 유형을 선택하세요", menu_items_type, default_choice="2")
    if c_type.lower() in ("b", "q"):
        return None
    p_type = {"1": "weekly", "2": "daily", "3": "hourly", "4": "intraday"}.get(c_type, "daily")
    months = 6
    if p_type == "daily":
        menu_items_period = [("1", "6개월", "6 Months"), ("2", "1년", "1 Year")]
        c_period = utils.show_menu("표시 기간을 선택하세요", menu_items_period, default_choice="1")
        if c_period.lower() in ("b", "q"):
            return None
        months = 12 if c_period == "2" else 6
    return p_type, months


def _print_summary(results, period_str):
    table = Table(box=box.SIMPLE_HEAD, title=f"일괄 차트 생성 결과 — {period_str}", title_justify="left")
    table.add_column("#", justify="right", style="dim")
    table.add_column("종목")
    table.add_column("코드", style="dim")
    table.add_column("결과")
    mark = {STATUS_OK: "[green]✅ 생성[/green]", STATUS_SKIP: "[yellow]⏭ 건너뜀[/yellow]",
            STATUS_FAIL: "[red]❌ 실패[/red]", STATUS_CANCEL: "[dim]⏹ 중단[/dim]"}
    for i, r in enumerate(results, 1):
        cell = mark[r["status"]] + (f" [dim]({escape(r['reason'])})[/dim]" if r["reason"] else "")
        table.add_row(str(i), escape(r["name"]), escape(r["code"]), cell)
    config.console.print(table)

    cnt = {s: sum(1 for r in results if r["status"] == s) for s in mark}
    config.console.print(f"생성 [green]{cnt[STATUS_OK]}[/green] · 건너뜀 [yellow]{cnt[STATUS_SKIP]}[/yellow] · "
                         f"실패 [red]{cnt[STATUS_FAIL]}[/red]"
                         + (f" · 중단 {cnt[STATUS_CANCEL]}" if cnt[STATUS_CANCEL] else ""))
    if not cnt[STATUS_OK]:
        return
    if getattr(config, "WEBCHART_ACTIVE", False) and web_dashboard.is_web_server_running():
        config.console.print(f"[bold cyan]🌐 갤러리에서 확인하세요 — {web_dashboard.web_server_url()}[/bold cyan]")
    else:
        config.console.print(f"[cyan]📁 저장 위치: {config.CHART_DIR}[/cyan]")


def run_batch(basket, period_type, months):
    """진행률을 보여주며 일괄 생성하고 결과표를 출력한다. 결과 리스트를 돌려준다."""
    period_str = PERIOD_LABEL.get(period_type, period_type)
    if period_type == "daily":
        period_str += f"({'1년' if months == 12 else '6개월'})"
    logger.info(f"[chart_batch] 일괄 생성 시작: {len(basket)}개 · {period_str} · DPI {config.CHART_DPI}")

    with Progress(SpinnerColumn(), TextColumn("[progress.description]{task.description}"), BarColumn(),
                  MofNCompleteColumn(), TimeElapsedColumn(), console=config.console, transient=True) as progress:
        task = progress.add_task("[cyan]차트 생성 준비...[/cyan]", total=len(basket))

        def _on_progress(i, t):
            progress.update(task, completed=i,
                            description=f"[cyan]{escape(t['name'])} {period_str} 차트 생성 중...[/cyan] [dim](중단: Ctrl+C)[/dim]")

        results = generate_batch_charts(basket, period_type, months, on_progress=_on_progress)
        progress.update(task, completed=len(basket))

    logger.info("[chart_batch] 일괄 생성 종료: " + ", ".join(
        f"{s}={sum(1 for r in results if r['status'] == s)}" for s in (STATUS_OK, STATUS_SKIP, STATUS_FAIL, STATUS_CANCEL)))
    config.console.print()
    _print_summary(results, period_str)
    return results


def batch_chart_menu():
    """[3]-7 일괄 차트 생성 메뉴. 바구니는 메뉴를 나갈 때까지 유지된다(같은 종목으로 유형만 바꿔 다시 그리기)."""
    basket = []
    base_len = len(context.USER_ACTION_BREADCRUMB)
    while True:
        context.USER_ACTION_BREADCRUMB = context.USER_ACTION_BREADCRUMB[:base_len]
        menu_items = [
            ("1", "국내 주식", "Domestic Stock"), ("2", "국내 ETF", "Domestic ETF"),
            ("3", "미국 주식", "US Stock"), ("4", "미국 ETF", "US ETF"),
            ("5", "시장 지수", "Market Indices"), ("6", "직접 입력", "여러 코드를 쉼표로"),
            ("7", "선택 비우기", "Clear"), ("8", "차트 생성 실행", "Run"),
        ]
        choice = utils.show_menu("일괄 차트 생성 (Batch Chart)", menu_items,
                                 default_choice="8" if basket else "1",
                                 text_before=_basket_text(basket),
                                 disabled=None if basket else {"7", "8"})
        if choice.lower() in ("b", "q"):
            return
        sub_map = dict((k, v) for k, v, _ in menu_items)
        context.USER_ACTION_BREADCRUMB.append(f"[{choice}] {sub_map.get(choice, '')}")

        if choice in _SOURCE_KEYS or choice in ("5", "6"):
            if choice in _SOURCE_KEYS:
                items = _pick_from_watchlist(choice)
            elif choice == "5":
                items = _pick_indices()
            else:
                items = _pick_direct()
            added, dropped = _add_to_basket(basket, items)
            if dropped:
                config.console.print(f"[yellow]최대 {config.BATCH_CHART_MAX}개까지 담을 수 있어 {dropped}개는 빠졌습니다 "
                                     f"(BATCH_CHART_MAX 로 조정).[/yellow]")
                utils.pause()
            elif items and not added:
                config.console.print("[dim]이미 담긴 종목입니다.[/dim]")
                utils.pause()
            continue

        if choice == "7":
            basket.clear()
            continue

        if choice == "8" and basket:
            sel = _ask_period()
            if sel is None:
                continue
            p_type, months = sel
            logging.info(f"운영자 실행: {' - '.join(context.USER_ACTION_BREADCRUMB)} "
                         f"({len(basket)}개: {', '.join(b['code'] for b in basket)})")
            run_batch(basket, p_type, months)
            utils.pause()
