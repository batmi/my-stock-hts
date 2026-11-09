# modules/manage/toss_rankings.py
"""[6-9] 투자자 순매수 · 섹터 순위 (토스 Open API 1.2.28, 토스 모드 전용).

- 투자자별 순매수·순매도 상위: 외국인·기관·개인 각각, KRX·NXT 통합 거래액
- 섹터 등락 순위: 토스 자체 섹터 분류의 기간 등락률 상위 → 구성 종목(시가총액순)

[화면 전용이다 — 판단에 넣지 않는다]
 · 투자자 랭킹은 **최신 영업일만** 준다(과거 날짜 조회 없음). 과거를 못 받으니 백테스트로 검증할 수
   없고, 진입 필터는 같은 차단율의 무작위 대조로만 채택한다([[entry-filter-random-control]]).
 · 섹터는 토스 자체 분류이고 **현재 분류만** 준다(과거 소속 없음). 방어주 배제·탐색 업종은 DART
   업종코드(KSIC 11차)로 감사했으므로 이 분류로 바꾸면 그 근거가 무효가 된다.
 · 투자자마다 기준 시각(rankedAt)이 다르다 — 외국인·기관은 장중 잠정치, 개인은 정규장 종료 후에 나오고
   그 전에는 직전 영업일 값이다. 20시경까지 NXT 반영으로 바뀐다. 그래서 표마다 기준 시각을 적는다.
"""
import logging

from rich import box
from rich.prompt import Prompt
from rich.table import Table

import api
import config
from core import utils

logger = logging.getLogger(__name__)

_INVESTORS = (("foreigner", "외국인"), ("institution", "기관"), ("individual", "개인"))
_DURATIONS = (("1", "1d", "1일"), ("2", "1w", "1주"), ("3", "1mo", "1개월"), ("4", "3mo", "3개월"))
_DEPTH_LABEL = {0: "대", 1: "중", 2: "소"}


def available():
    """이 화면을 쓸 수 있는 모드인가 — 토스 모드에서만 이 장부가 있다(메뉴 회색 판단용)."""
    return bool(getattr(config.session, 'is_toss', False))


def _num(v):
    """토스 수치(문자열 decimal) → float. 못 읽으면 None — 0 은 '값이 0'이라는 사실이다."""
    if v is None:
        return None
    try:
        return float(str(v).replace(',', ''))
    except (TypeError, ValueError):
        return None


def _won(v):
    """원 → 1조 이상은 '2.41조', 아래는 '3,864.9억'. 모르면 '-' (0 으로 찍으면 '거래가 없었다'가 된다)."""
    n = _num(v)
    if n is None:
        return "-"
    return f"{n / 1e12:,.2f}조" if abs(n) >= 1e12 else f"{n / 1e8:,.1f}억"


def _label(code, names):
    """'삼성전자(005930)'. 이름을 모르면 코드만 — '402340(402340)'처럼 두 번 찍지 않는다."""
    name = names.get(code)
    return f"{name}({code})" if name and name != code else code


def _rate(v):
    """소수 비율 → '+1.79%'(오름 빨강·내림 파랑, 한국식). 모르면 '-'."""
    n = _num(v)
    if n is None:
        return "[dim]-[/dim]"
    color = "red" if n > 0 else ("blue" if n < 0 else "white")
    return f"[{color}]{n * 100:+.2f}%[/{color}]"


def _when(ts):
    """'2026-10-20T15:41:19+09:00' → '10-20 15:41'. 없으면 '제공 안 됨'."""
    s = str(ts or "")
    return f"{s[5:10]} {s[11:16]}" if len(s) >= 16 else "제공 안 됨"


def _my_codes():
    """(관심종목 코드 집합, 보유종목 코드 집합). 보유 조회가 실패하면 보유 표시만 뺀다."""
    watch = {str(s.get("code")) for s in config.session.stock_data.get("stocks_kr", []) if s.get("code")}
    held = set()
    try:
        rows, _ = api.get_domestic_balance()
        held = {str(r.get('pdno') or '').strip() for r in (rows or []) if r.get('pdno')}
    except Exception as e:      # 표시 보조 정보 — 실패해도 표는 그린다
        logger.debug(f"[토스 순위] 보유 종목 조회 실패(표식만 생략): {e}")
    return watch, held


def _mark(code, watch, held):
    if code in held:
        return "[bold magenta]보유[/bold magenta]"
    if code in watch:
        return "[cyan]관심[/cyan]"
    return ""


def _names(symbols):
    try:
        return api._toss_name_map(list(symbols))
    except Exception as e:
        logger.debug(f"[토스 순위] 종목명 조회 실패: {e}")
        return {}


def show_investor_rankings(count=15):
    """외국인·기관·개인 순매수/순매도 상위 표(투자자마다 하나). 폭 ≈ 90열."""
    from brokers import toss_api
    config.console.print("\n[bold cyan][투자자별 순매수·순매도 상위] (토스 · KRX+NXT 통합 거래액)[/bold cyan]")
    config.console.print("[dim]최신 영업일만 제공됩니다. 외국인·기관은 장중 잠정치, 개인은 정규장 종료 후 "
                         "집계이며 20시경까지 바뀔 수 있습니다.[/dim]\n")
    try:
        res = toss_api.get_investor_trading_rankings(count=count)
    except toss_api.TossApiError as e:
        config.console.print(f"[red]투자자별 순위를 받지 못했습니다: {e}[/red]")
        return False
    if not isinstance(res, dict):
        config.console.print("[red]투자자별 순위 응답을 해석하지 못했습니다 — '순위 없음'이 아닙니다.[/red]")
        return False

    symbols = []
    for key, _ in _INVESTORS:
        blk = res.get(key) or {}
        symbols += [r.get('symbol') for r in (blk.get('netBuyRankings') or []) + (blk.get('netSellRankings') or [])]
    names = _names(s for s in symbols if s)
    watch, held = _my_codes()

    for key, label in _INVESTORS:
        blk = res.get(key) or {}
        buys = blk.get('netBuyRankings') or []
        sells = blk.get('netSellRankings') or []
        table = Table(title=f"{label} · 기준 {_when(blk.get('rankedAt'))}", title_justify="left",
                      box=box.SIMPLE_HEAVY, header_style="bold")
        table.add_column("#", justify="right", width=3)
        table.add_column("순매수 종목", width=22, no_wrap=True)
        table.add_column("금액", justify="right", width=10)
        table.add_column("", width=4)
        table.add_column("순매도 종목", width=22, no_wrap=True)
        table.add_column("금액", justify="right", width=10)
        table.add_column("", width=4)
        if not buys and not sells:
            config.console.print(f"[yellow]{label}: 지금은 토스가 이 투자자 순위를 제공하지 않습니다.[/yellow]\n")
            continue
        for i in range(max(len(buys), len(sells))):
            b = buys[i] if i < len(buys) else {}
            s = sells[i] if i < len(sells) else {}
            bc, sc = str(b.get('symbol') or ''), str(s.get('symbol') or '')
            table.add_row(
                str(i + 1),
                _label(bc, names) if bc else "",
                f"[red]{_won(b.get('netBuyAmount'))}[/red]" if bc else "",
                _mark(bc, watch, held),
                _label(sc, names) if sc else "",
                f"[blue]{_won(s.get('netSellAmount'))}[/blue]" if sc else "",
                _mark(sc, watch, held))
        config.console.print(table)
    return True


def _sector_index():
    """{sectorId: (depth, parent_name)} — 섹터 랭킹 응답에는 계층이 없어 목록으로 채운다. 실패 시 빈 dict."""
    from brokers import toss_api
    try:
        rows = (toss_api.get_sectors() or {}).get('sectors') or []
    except toss_api.TossApiError as e:
        logger.debug(f"[토스 섹터] 목록 조회 실패(분류 표시 생략): {e}")
        return {}
    by_id = {str(r.get('sectorId')): r for r in rows}
    out = {}
    for sid, r in by_id.items():
        parent = by_id.get(str(r.get('parentSectorId'))) if r.get('parentSectorId') else None
        out[sid] = (r.get('depth'), (parent or {}).get('name', ''))
    return out


def _show_sector_stocks(sector_id, sector_name, watch, held, limit=20):
    from brokers import toss_api
    try:
        res = toss_api.get_sector_stocks(sector_id, "KR")
    except toss_api.TossApiError as e:
        config.console.print(f"[red]구성 종목을 받지 못했습니다: {e}[/red]")
        return
    stocks = (res or {}).get('stocks') if isinstance(res, dict) else None
    if stocks is None:
        config.console.print("[red]구성 종목 응답을 해석하지 못했습니다 — '종목 없음'이 아닙니다.[/red]")
        return
    if not stocks:
        config.console.print(f"[yellow]{sector_name}: 국내 구성 종목이 없습니다.[/yellow]")
        return
    table = Table(title=f"{sector_name} 구성 종목 (시가총액순 상위 {min(limit, len(stocks))}/{len(stocks)})",
                  title_justify="left", box=box.SIMPLE_HEAVY, header_style="bold")
    table.add_column("#", justify="right", width=3)
    table.add_column("종목", width=24, no_wrap=True)
    table.add_column("시가총액", justify="right", width=10)
    table.add_column("거래대금(당일)", justify="right", width=12)
    table.add_column("", width=4)
    for i, s in enumerate(stocks[:limit]):
        code = str(s.get('symbol') or '')
        table.add_row(str(i + 1), f"{s.get('name', code)}({code})", _won(s.get('marketCapitalization')),
                      _won(s.get('tradingAmount')), _mark(code, watch, held))
    config.console.print(table)


def show_sector_rankings():
    """토스 섹터 등락률 상위(대·중분류) → 번호를 고르면 구성 종목. 폭 ≈ 85열."""
    from brokers import toss_api
    config.console.print("\n[bold cyan][섹터 등락 순위] (토스 자체 섹터 분류 · 국내)[/bold cyan]")
    config.console.print("[dim]토스 분류이며 한 종목이 여러 섹터에 속합니다. 현재 분류만 제공되어 매매 판단"
                         "(방어주 배제 등)에는 쓰지 않습니다.[/dim]\n")
    keys = [k for k, _, _ in _DURATIONS]
    menu = "  ".join(f"[{k}] {lab}" for k, _, lab in _DURATIONS)
    config.console.print(menu)
    pick = Prompt.ask("기간", choices=keys, default="1")
    duration, dlabel = next((d, lab) for k, d, lab in _DURATIONS if k == pick)
    try:
        res = toss_api.get_sector_rankings("TOP_GAINERS", "KR", duration)
    except toss_api.TossApiError as e:
        config.console.print(f"[red]섹터 순위를 받지 못했습니다: {e}[/red]")
        return False
    rows = (res or {}).get('rankings') if isinstance(res, dict) else None
    if rows is None:
        config.console.print("[red]섹터 순위 응답을 해석하지 못했습니다 — '순위 없음'이 아닙니다.[/red]")
        return False
    if not rows:
        config.console.print("[yellow]이 기간에는 집계된 섹터 순위가 없습니다.[/yellow]")
        return True

    index = _sector_index()
    rows = rows[:30]
    table = Table(title=f"{dlabel} 등락률 상위", title_justify="left", box=box.SIMPLE_HEAVY, header_style="bold")
    table.add_column("#", justify="right", width=3)
    table.add_column("섹터", width=20, no_wrap=True)
    table.add_column("분류", width=16, no_wrap=True)
    table.add_column("등락률", justify="right", width=9)
    table.add_column("거래대금", justify="right", width=12)
    table.add_column("시가총액", justify="right", width=10)
    for r in rows:
        depth, parent = index.get(str(r.get('sectorId')), (None, ''))
        cls = _DEPTH_LABEL.get(depth, "")
        if parent:
            cls = f"{cls} · {parent}" if cls else parent
        table.add_row(str(r.get('rank', '')), str(r.get('name', '')), f"[dim]{cls}[/dim]",
                      _rate(r.get('changeRate')), _won(r.get('tradingAmount')),
                      _won(r.get('marketCapitalization')))
    config.console.print(table)

    watch, held = _my_codes()
    choices = [str(r.get('rank')) for r in rows if r.get('rank') is not None]
    while True:
        sel = Prompt.ask("구성 종목을 볼 섹터 번호 [dim](끝내려면 Enter)[/dim]", default="", show_default=False)
        if not sel.strip():
            return True
        if sel.strip() not in choices:
            config.console.print("[yellow]표의 번호를 입력하세요.[/yellow]")
            continue
        r = next(x for x in rows if str(x.get('rank')) == sel.strip())
        _show_sector_stocks(str(r.get('sectorId')), str(r.get('name', '')), watch, held)


def show_toss_rankings():
    """[6-9] 하위 메뉴."""
    if not available():
        config.console.print("[yellow]토스 모드(--mode 3)에서만 제공됩니다.[/yellow]")
        return False
    from core import context
    base_len = len(context.USER_ACTION_BREADCRUMB)
    items = [("1", "투자자별 순매수·순매도 상위", "Investor Net-Buy Ranking"),
             ("2", "섹터 등락 순위", "Sector Ranking")]
    last = "1"
    while True:
        context.USER_ACTION_BREADCRUMB = context.USER_ACTION_BREADCRUMB[:base_len]
        choice = utils.show_menu("투자자 순매수 · 섹터 순위 (Toss)", items, default_choice=last)
        if choice.lower() in ('b', 'q'):
            context.USER_ACTION_BREADCRUMB = context.USER_ACTION_BREADCRUMB[:base_len]
            return True
        names = {k: v for k, v, _ in items}
        if choice not in names:
            continue
        context.USER_ACTION_BREADCRUMB.append(f"[{choice}] {names[choice]}")
        if choice == '1':
            show_investor_rankings()
        else:
            show_sector_rankings()
        last = choice
        utils.pause()
