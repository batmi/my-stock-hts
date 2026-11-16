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
import string

from rich.progress import Progress, SpinnerColumn, BarColumn, TextColumn, MofNCompleteColumn, TimeElapsedColumn
from rich.prompt import Prompt
from rich.table import Table
from rich import box
from rich.markup import escape

import api
import config
from core import context
from core import jsonio
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


def _remove_from_basket(basket, picked):
    """picked 에 든 종목((code, ovs))을 바구니에서 뺀다. 뺀 개수."""
    keys = {(p["code"], p["ovs"]) for p in picked or []}
    before = len(basket)
    basket[:] = [b for b in basket if (b["code"], b["ovs"]) not in keys]
    return before - len(basket)


def _pick_to_remove(basket):
    """[8] 바구니에서 뺄 종목을 고른다. 고른 항목 리스트, 취소면 [].

    번호(`1,3,5-8`)·`0`(모두 비우기)·종목명/코드 검색을 받는다. 담을 때의 `all` 도 '모두'로 받는다.
    [공용 select_multiple_from_list 를 쓰지 않는 이유] 거기서는 빈 Enter 가 '빈 검색어'로 읽혀
     모든 항목에 맞는다. 담을 때는 무해하지만, 뺄 때는 한 종목만 담긴 바구니가 Enter 한 번에 빈다.
     그래서 여기서는 빈 Enter 를 취소로 읽고, 검색이 여러 종목에 맞으면 빼지 않고 번호를 알려 준다.
     범위 밖 번호(예: 9)는 그 숫자가 든 종목코드(005930)를 검색하지 않는다 — 코드와 같을 때만 그 종목이다.
    """
    while True:
        config.console.print(f"[bold]담긴 종목 {len(basket)}개[/bold]")
        for i, b in enumerate(basket, 1):
            config.console.print(f"[{i}] {escape(b['name'])} ({escape(b['code'])})")
        config.console.print("[0] 모두 비우기")
        config.console.print()
        sel = Prompt.ask("뺄 번호 [dim](예: 1,3,5-8 · 모두 비우기: 0)[/dim] 또는 종목명·코드 "
                         "[dim](취소: Enter·b)[/dim]", default="", show_default=False).strip()
        config.console.print()
        if not sel or sel.lower() in ("b", "q"):
            return []
        if sel == "0":
            return list(basket)
        try:
            idx = utils.parse_multi_selection(sel, len(basket))
        except ValueError as e:
            config.console.print(f"[red]{e}[/red]\n")
            continue
        if idx is not None:
            return [basket[i] for i in idx]
        if sel.isdigit():
            hits = [i for i, b in enumerate(basket) if b["code"] == sel]
            if not hits:
                config.console.print(f"[red]'{sel}' 는 목록 범위(1~{len(basket)})를 벗어납니다.[/red]\n")
                continue
        else:
            hits = [i for i, b in enumerate(basket)
                    if sel.lower() in b["name"].lower() or sel.upper() in b["code"].upper()]
        if len(hits) == 1:
            return [basket[hits[0]]]
        if not hits:
            config.console.print(f"[yellow]'{escape(sel)}' 에 맞는 종목이 없습니다.[/yellow]\n")
        else:
            config.console.print(f"[yellow]'{escape(sel)}' 이(가) {len(hits)}개에 맞습니다"
                                 f"({', '.join(str(i + 1) for i in hits)}번) — 번호로 골라 주세요.[/yellow]\n")


# ==========================================================
# [그룹 · 2026-11-16] 바구니를 A~Z 슬롯에 저장해 두고 다음에 불러와 바로 그린다
# ==========================================================
#  · 저장 파일은 config.CHART_GROUPS_FILE(json/chart_groups.json) 하나다. 관심종목(stock.json)에
#    섞지 않는다 — 그건 자동매매가 읽는 매매 대상 목록이고, 그룹에는 지수·직접 입력 티커도 든다.
#  · 항목은 바구니와 같은 모양({code, name, ovs})으로 둔다. 불러올 때 종목명을 다시 조회하지 않는다.
#  · 읽기 실패(None)와 그룹 없음({})을 가른다. 읽지 못했으면 저장·삭제하지 않는다 — 다른 슬롯을
#    빈 것으로 알고 덮으면 저장된 그룹이 통째로 사라진다.
#  · 저장·삭제는 파일을 다시 읽어 그 슬롯만 바꾼다(다른 모드의 프로세스가 같은 파일을 쓸 수 있다).
#  · 슬롯을 묻는 자리의 취소는 빈 Enter 하나다. b·q 는 B·Q 슬롯이라 취소로 읽지 않는다.
#    대신 덮어쓰기·삭제는 한 번 더 묻는다.
GROUP_SLOTS = tuple(string.ascii_uppercase)
_GROUPS_VERSION = 1


def _clean_item(it):
    """저장된 항목 하나를 바구니 항목으로. 모양이 틀리면 None."""
    if not isinstance(it, dict):
        return None
    code, name, ovs = it.get("code"), it.get("name"), it.get("ovs")
    if not isinstance(code, str) or not code or not isinstance(ovs, bool):
        return None
    return {"code": code, "name": name if isinstance(name, str) and name else code, "ovs": ovs}


def load_groups():
    """저장된 그룹 {슬롯: [{code, name, ovs}]}. 파일이 없으면 {}, 읽지 못했으면 None.

    모양이 틀린 슬롯·항목은 로그를 남기고 건너뛴다(나머지는 쓴다).
    """
    path = config.CHART_GROUPS_FILE
    if not os.path.exists(path):
        return {}
    data = jsonio.load_json(path, default=None)
    raw = data.get("groups") if isinstance(data, dict) else None
    if not isinstance(raw, dict):
        logger.error(f"[chart_batch] 그룹 파일을 읽지 못했습니다({path}): 형식이 다릅니다")
        return None
    groups = {}
    for slot, items in raw.items():
        if slot not in GROUP_SLOTS or not isinstance(items, list):
            logger.warning(f"[chart_batch] 그룹 파일의 알 수 없는 슬롯을 건너뜁니다: {slot!r}")
            continue
        cleaned = [c for c in (_clean_item(it) for it in items) if c]
        if len(cleaned) != len(items):
            logger.warning(f"[chart_batch] 그룹 {slot} 의 형식이 틀린 항목 {len(items) - len(cleaned)}개를 건너뜁니다")
        if cleaned:
            groups[slot] = cleaned
    return groups


def write_group(slot, items):
    """slot 을 items 로 저장한다(items 가 None 이면 슬롯을 지운다). 바뀐 전체 그룹, 실패면 None.

    파일을 다시 읽어 그 슬롯만 바꾼다. 읽지 못하면 쓰지 않는다(다른 슬롯을 지우지 않게).
    """
    groups = load_groups()
    if groups is None:
        return None
    if items is None:
        groups.pop(slot, None)
    else:
        groups[slot] = [{"code": i["code"], "name": i["name"], "ovs": bool(i["ovs"])} for i in items]
    data = {"version": _GROUPS_VERSION, "groups": {k: groups[k] for k in GROUP_SLOTS if k in groups}}
    if not jsonio.save_json(config.CHART_GROUPS_FILE, data):
        return None
    return groups


def _keys(items):
    return [(i["code"], i["ovs"]) for i in items]


def _group_line(slot, items, width=70):
    names = ", ".join(i["name"] for i in items)
    if len(names) > width:
        names = names[:width - 1] + "…"
    return f"[{slot}] {len(items)}개 · {escape(names)}"


def _groups_text(groups):
    if groups is None:
        return "[red]그룹 파일을 읽지 못했습니다 — 저장·삭제를 막았습니다 (상세는 로그 참조)[/red]\n"
    if not groups:
        return "[dim]저장된 그룹 없음 — 종목을 담은 뒤 [2] 로 A~Z 중 한 슬롯에 저장합니다[/dim]\n"
    lines = [_group_line(s, groups[s]) for s in GROUP_SLOTS if s in groups]
    return "[bold]저장된 그룹[/bold]\n" + "\n".join(lines) + "\n"


def _ask_slot(groups, question, must_exist):
    """A~Z 슬롯 하나. 취소(빈 Enter)면 None. must_exist 면 저장된 슬롯만 받는다."""
    while True:
        sel = Prompt.ask(f"{question} [dim](A~Z · 취소: Enter)[/dim]", default="", show_default=False).strip()
        config.console.print()
        if not sel:
            return None
        slot = sel.upper()
        if slot not in GROUP_SLOTS:
            config.console.print(f"[red]'{escape(sel)}' — 슬롯은 A~Z 한 글자입니다.[/red]\n")
            continue
        if must_exist and slot not in (groups or {}):
            config.console.print(f"[yellow]슬롯 {slot} — 비어 있습니다.[/yellow]\n")
            continue
        return slot


def _ask_yes(question):
    ans = Prompt.ask(f"{question} \\[y/N]", default="n", show_default=False).strip().lower()
    config.console.print()
    return ans in ("y", "yes")


def _load_group(basket, groups):
    """그룹을 바구니에 넣는다. 담긴 종목이 있으면 교체(기본)/추가를 묻는다. 불러온 슬롯, 취소면 None.

    바구니가 이미 그 그룹과 같으면 묻지 않는다 — 교체든 추가든 결과가 같아 질문이 뜻이 없다.
    """
    slot = _ask_slot(groups, "불러올 그룹", must_exist=True)
    if slot is None:
        return None
    if basket and _keys(basket) == _keys(groups[slot]):
        #  문구는 슬롯 글자 뒤에 조사를 붙이지 않는다(L·M·N·R 은 받침으로 읽혀 '와/로/를'이 틀린다).
        config.console.print(f"[dim]담긴 {len(basket)}개가 이미 그룹 {slot} 내용과 같습니다.[/dim]")
        utils.pause()
        return slot
    replace = True
    if basket:
        ans = Prompt.ask(f"이미 {len(basket)}개가 담겨 있습니다. [1] 비우고 그룹 {slot} 불러오기  "
                         f"[2] 담긴 종목 뒤에 그룹 {slot} 추가 [dim](취소: b)[/dim]",
                         choices=["1", "2", "b", "q"], default="1").strip().lower()
        config.console.print()
        if ans in ("b", "q"):
            return None
        replace = ans == "1"
    if replace:
        basket.clear()
    added, dropped = _add_to_basket(basket, [dict(i) for i in groups[slot]])
    if dropped:
        config.console.print(f"[yellow]최대 {config.BATCH_CHART_MAX}개까지 담을 수 있어 그룹 {slot} 의 {dropped}개는 "
                             f"빠졌습니다 (BATCH_CHART_MAX 로 조정).[/yellow]")
        utils.pause()
    logger.info(f"[chart_batch] 그룹 {slot} 불러오기({'교체' if replace else '추가'}): {added}개 담음")
    return slot


def _save_group(basket, groups):
    """바구니를 슬롯에 저장한다. (바뀐 그룹, 저장한 슬롯). 취소·실패면 (groups, None)."""
    slot = _ask_slot(groups, "저장할 슬롯", must_exist=False)
    if slot is None:
        return groups, None
    if slot in (groups or {}) and not _ask_yes(f"그룹 {slot}({len(groups[slot])}개)에 지금 선택 {len(basket)}개를 덮어쓸까요?"):
        return groups, None
    new = write_group(slot, basket)
    if new is None:
        config.console.print(f"[red]그룹 {slot} 저장 실패 — 바뀌지 않았습니다 (상세는 로그 참조)[/red]")
        utils.pause()
        return groups, None
    logger.info(f"[chart_batch] 그룹 {slot} 저장: {', '.join(b['code'] for b in basket)}")
    return new, slot


def _delete_group(groups):
    """슬롯 하나를 지운다. (바뀐 그룹, 지운 슬롯). 취소·실패면 (groups, None)."""
    slot = _ask_slot(groups, "지울 그룹", must_exist=True)
    if slot is None or not _ask_yes(f"그룹 {slot}({len(groups[slot])}개) — 지울까요?"):
        return groups, None
    new = write_group(slot, None)
    if new is None:
        config.console.print(f"[red]그룹 {slot} 삭제 실패 — 바뀌지 않았습니다 (상세는 로그 참조)[/red]")
        utils.pause()
        return groups, None
    logger.info(f"[chart_batch] 그룹 {slot} 삭제")
    return new, slot


def _group_menu(basket, groups, origin):
    """[7] 그룹 설정 — 불러오기·저장·삭제 중 하나를 하고 3-7 메뉴로 돌아간다. (바뀐 그룹, 바구니의 출처 슬롯)."""
    menu_items = [("1", "그룹 불러오기", "Load"), ("2", "현재 선택을 그룹으로 저장", "Save"),
                  ("3", "그룹 삭제", "Delete")]
    disabled = set()
    if not groups:
        disabled |= {"1", "3"}
    if not basket or groups is None:
        disabled.add("2")
    if groups is None:
        disabled.add("3")
    default = next((k for k, _, _ in menu_items if k not in disabled), "1")
    choice = utils.show_menu("그룹 설정 (Groups Setting)", menu_items, default_choice=default,
                             text_before=_basket_text(basket, origin, groups) + _groups_text(groups),
                             disabled=disabled)
    if choice.lower() in ("b", "q") or choice in disabled:
        return groups, origin
    context.USER_ACTION_BREADCRUMB.append(f"[{choice}] {dict((k, v) for k, v, _ in menu_items)[choice]}")
    if choice == "1":
        slot = _load_group(basket, groups)
        return groups, (slot or origin)
    if choice == "2":
        groups, slot = _save_group(basket, groups)
        return groups, (slot or origin)
    groups, slot = _delete_group(groups)
    return groups, (None if slot and slot == origin else origin)


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


def _basket_text(basket, origin=None, groups=None):
    """메뉴 위 바구니 줄. origin 은 바구니를 불러오거나 저장한 그룹 슬롯이다.
    그 그룹과 내용이 달라졌으면 '수정됨'을 붙인다 — 저장하지 않은 변경이 있다는 표시다."""
    if not basket:
        return f"[dim]선택된 종목 없음 — 목록에서 여러 종목을 담은 뒤 [9] 로 생성합니다 (최대 {config.BATCH_CHART_MAX}개)[/dim]\n"
    tag = ""
    if origin and groups and origin in groups:
        tag = f" (그룹 {origin}{' · 수정됨' if _keys(basket) != _keys(groups[origin]) else ''})"
    names = ", ".join(escape(b["name"]) for b in basket)
    return f"[bold cyan]선택 {len(basket)}/{config.BATCH_CHART_MAX}개{tag}:[/bold cyan] {names}\n"


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
    """[3]-7 일괄 차트 생성 메뉴. 바구니는 메뉴를 나갈 때까지 유지된다(같은 종목으로 유형만 바꿔 다시 그리기).
    [7] 그룹 설정으로 바구니를 A~Z 슬롯에 저장해 두고 다음에 불러온다."""
    basket = []
    groups = load_groups()
    origin = None
    base_len = len(context.USER_ACTION_BREADCRUMB)
    #  [2026-11-16] 저장된 그룹이 있으면 처음 기본값을 7번(그룹)으로 둔다 — 들어와서 Enter 두 번이면
    #   불러오기까지 간다. 그 뒤로는 종전처럼 마지막 선택이 기본값이다.
    last_choice = "7" if groups else "1"
    while True:
        context.USER_ACTION_BREADCRUMB = context.USER_ACTION_BREADCRUMB[:base_len]
        #  [2026-11-16] 순서: 담기(1~6) → 그룹(7) → 선택 삭제(8) → 실행(9). 실행이 늘 맨 끝이다.
        menu_items = [
            ("1", "국내 주식", "Domestic Stock"), ("2", "국내 ETF", "Domestic ETF"),
            ("3", "미국 주식", "US Stock"), ("4", "미국 ETF", "US ETF"),
            ("5", "시장 지수", "Market Indices"), ("6", "직접 입력", "Direct Input, comma-separated"),
            ("7", "그룹 설정", "Groups Setting"),
            ("8", "선택 삭제", "Delete"), ("9", "차트 생성 실행", "Run"),
        ]
        #  [2026-10-15] 다른 메뉴와 같이 마지막 선택을 기본값으로 둔다(종전엔 바구니가 차면 늘 실행 고정).
        #   비활성 항목(바구니가 비었을 때의 8·9, 바구니도 그룹도 없을 때의 7)이 마지막 선택이면 1번으로 돌아간다.
        #   그룹을 못 읽었으면(None) 7번을 열어 둔다 — 들어가서 읽기 실패 안내를 본다.
        disabled = set() if basket else {"8", "9"}
        if not basket and groups == {}:
            disabled.add("7")
        default = "1" if last_choice in disabled else last_choice
        choice = utils.show_menu("일괄 차트 생성 (Batch Chart)", menu_items,
                                 default_choice=default,
                                 text_before=_basket_text(basket, origin, groups),
                                 disabled=disabled or None)
        if choice.lower() in ("b", "q"):
            return
        last_choice = choice
        sub_map = dict((k, v) for k, v, _ in menu_items)
        context.USER_ACTION_BREADCRUMB.append(f"[{choice}] {sub_map.get(choice, '')}")

        if choice == "7":
            groups, origin = _group_menu(basket, groups, origin)
            continue

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

        if choice == "8":
            #  [2026-11-09] 종전에는 묻지 않고 전부 비웠다. 이제 골라 빼거나 0 으로 비운다.
            #   남은 종목은 메뉴 위 '선택 N개' 줄에 바로 보인다.
            if basket:
                _remove_from_basket(basket, _pick_to_remove(basket))
            continue

        if choice == "9" and basket:
            sel = _ask_period()
            if sel is None:
                continue
            p_type, months = sel
            logging.info(f"운영자 실행: {' - '.join(context.USER_ACTION_BREADCRUMB)} "
                         f"({len(basket)}개: {', '.join(b['code'] for b in basket)})")
            run_batch(basket, p_type, months)
            utils.pause()
