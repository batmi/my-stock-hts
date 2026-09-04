# modules/manage/discover.py
"""[7-5] 관심 종목 탐색 (Discover Candidates).

[왜 이 기능이 있는가] 2026-08-16 감사에서 **관심종목 수가 지금까지 측정한 어떤 다이얼보다
 큰 레버**로 나왔다. 슬롯(4)은 그대로 두고 후보 풀만 넓혔을 때, 생존 편향을 보정한 10년
 짝비교에서 44종목 276.4%/MAR 8.05 → 80종목 605.7%/MAR 17.75 (30승 0무 6패)였다.
 원인은 커버리지다 — 현금 비중이 61.6 → 58.0%로 내려간다. 44종목으로는 4슬롯을 상시
 채울 후보가 모자라 슬롯을 놀린다. (tools/audit_universe.py)

[왜 '고르지 않고' 뽑는가] 같은 감사에서 **시총 상위 40 추가(480%)보다 상위 500 내 무작위
 40 추가(813%)가 더 좋았다.** 측정된 것은 '종목을 잘 고르는 효과'가 아니라 '후보가 많아
 슬롯을 채우는 효과'다. 사람이 유망해 보이는 종목을 골라 넣으면 검증된 적 없는 선택 편향을
 새로 들이는 것이므로, 이 화면은 **규칙으로 거르고 폭으로 채우는** 방식만 제공한다.

[배제 규칙의 근거] 화면에서 사용자에게 그대로 설명한다(_print_rules). 특히 방어주 배제는
 실거래 재현에서 19전 0승 18패 1무·-220만원이 나온 항목이라 기본 ON이다.
"""
import logging
import random

from rich import box
from rich.progress import BarColumn, Progress, SpinnerColumn, TextColumn
from rich.prompt import Prompt
from rich.table import Table

import config
from core import context
from core import utils

logger = logging.getLogger(__name__)

# 업종(KRX 표준산업분류) 문자열로 방어주를 가린다. tools/audit_defensive_sector.py 가
#  확정한 13종목이 전부 아래 키워드에 걸리고, 대조군(경기민감 13종목)은 하나도 안 걸린다.
#  ※ 제약·바이오는 한국에서 성장주로 거래되므로 방어주에 넣지 않는다(감사와 동일 기준).
DEFENSIVE_KEYWORDS = [
    ("전기 통신업", "통신"),
    ("전기업", "전기"),
    ("가스 제조 및 배관공급업", "가스"),
    ("담배 제조업", "음식료"),
    ("식품 제조업", "음식료"),
    ("음료 및 얼음 제조업", "음식료"),
    ("알코올음료 제조업", "음식료"),
    ("종합 소매업", "필수소비 유통"),
]
# 지주회사·투자회사 배제. tools/audit_holding_company.py(2026-08-16)로 측정했다.
#  신호 성질은 관심종목과 큰 차이가 없지만(전방 20일 +2.37% vs +2.73%) 꼬리가 얇고
#  (P99 54.4 vs 59.2 · 중앙 0.14 vs 0.47), 슬롯 경쟁에서는 배제가 6개 창 모두 이긴다.
#  ※ '관심종목이 아닌 대형주를 넣었다 빼면 좋아진다'는 대조군 효과가 절반쯤 섞여 있다
#    (시총 이웃 대조군 배제도 전체창 29-0-7). 지주 배제는 그보다 크고 구간 일관성이
#    다르다(6/6 vs 3/6). 근거는 '신호가 흐려진다'가 아니라 '꼬리가 얇아 슬롯을 못 이긴다'.
HOLDING_KEYWORDS = ["지주회사", "기타 금융업"]


def _defensive_label(industry):
    """방어주면 (True, 분류명), 아니면 (False, None)."""
    text = str(industry or "")
    for key, label in DEFENSIVE_KEYWORDS:
        if key in text:
            return True, label
    return False, None


def _print_rules(target, pool, exclude_holding):
    """배제 규칙 상세 설명 — 무엇을 왜 거르는지를 화면에 남긴다."""
    console = config.console
    console.print("\n[bold cyan]🔎 관심 종목 탐색 (Discover Candidates)[/bold cyan]\n")
    console.print("[bold]■ 배제 규칙[/bold]")
    rules = Table(box=box.SIMPLE, padding=(0, 2), border_style="dim", header_style="dim")
    rules.add_column("규칙", style="cyan", no_wrap=True)
    rules.add_column("내용")
    rules.add_column("근거")
    rules.add_row("방어주", "통신 · 전기 · 가스 · 음식료 · 종합소매",
                  "실거래 재현 검증으로 관심종목에서 제외된 종목군.\n"
                  "진입 신호 전방 20일 +0.02% (관심종목 +3.04%)")
    rules.add_row("우선주·스팩·리츠", "종목명/코드 규칙으로 제외",
                  "추세추종 대상이 아니고 유동성 성격이 다름")
    rules.add_row("관리·투자주의", "관리종목 · 투자주의환기종목",
                  "상장폐지 위험 · 거래 제약")
    if exclude_holding:
        rules.add_row("지주회사", "지주회사 · 기타 금융업",
                      "실거래 재현 검증에서 슬롯 경쟁에 밀림(6개 창 모두).\n"
                      "진입 신호의 꼬리가 얇음 (상위 1% +54% vs 관심종목 +59%)")
    rules.add_row("데이터 부족", "10년 일봉이 안 잡히는 종목",
                  "백테스트·지표 워밍업 불가")
    rules.add_row("중복", "이미 관심종목에 있는 종목", "—")
    console.print(rules)

    console.print("[bold]■ 선정 방식[/bold]")
    console.print(
        f"  시가총액 상위 [cyan]{pool}[/cyan]위 이내에서 위 규칙을 통과한 종목을 모은 뒤,\n"
        f"  시총 구간에 고르게 퍼지도록 [cyan]{target}[/cyan]종목을 뽑습니다(상위 편중 방지).\n")


def _spread_pick(rows, target, rng):
    """시총 구간에 고르게 뽑는다. 상위부터 채우면 대형주만 남아 감사 조건과 어긋난다."""
    if len(rows) <= target:
        return list(rows)
    buckets = [[] for _ in range(target)]
    size = len(rows) / target
    for i, r in enumerate(rows):
        buckets[min(int(i / size), target - 1)].append(r)
    return [rng.choice(b) for b in buckets if b]


def _fetch_candidates(target, pool, exclude_holding, seed=None):
    """후보를 만들고, 단계별로 몇 개가 왜 걸러졌는지 함께 돌려준다.

    seed=None이면 실행할 때마다 다른 후보가 나온다. 규칙을 통과한 종목이 수백 개인데
    한 번에 보여주는 것은 수십 개뿐이라, 씨드를 고정하면 나머지를 영영 못 본다.
    돌려서 마음에 드는 조합이 나올 때까지 다시 실행할 수 있어야 한다.
    """
    import FinanceDataReader as fdr

    have = set()
    for key in ("stocks_kr", "etfs_kr"):
        have |= {s["code"] for s in config.session.stock_data.get(key, [])}

    krx = fdr.StockListing("KRX")
    krx = krx[krx["Market"].isin(["KOSPI", "KOSDAQ"])].dropna(subset=["Marcap"])
    krx = krx.sort_values("Marcap", ascending=False).head(pool)
    desc = fdr.StockListing("KRX-DESC").set_index("Code")

    steps = []
    n0 = len(krx)

    rows, cut_dup = [], 0
    for _, r in krx.iterrows():
        if r["Code"] in have:
            cut_dup += 1
            continue
        rows.append(r)
    steps.append(("이미 보유 중인 관심종목", cut_dup))

    kept, cut_type, cut_def, cut_hold, cut_admin, defs = [], 0, 0, 0, 0, []
    # KOSDAQ 소속부(Dept)에 관리종목·투자주의환기·SPAC이 표기된다. KOSPI는 결측이라
    #  이 경로로는 안 걸리지만, KOSDAQ 쪽 위험 종목만으로도 실익이 크다.
    dept = dict(zip(krx["Code"], krx.get("Dept", krx["Code"] * 0)))
    for r in rows:
        code, name = r["Code"], r["Name"]
        d_txt = str(dept.get(code) or "")
        if "관리종목" in d_txt or "투자주의환기" in d_txt:
            cut_admin += 1
            continue
        if "스팩" in name or "리츠" in name or "SPAC" in d_txt or not code.endswith("0"):
            cut_type += 1
            continue
        industry = desc.loc[code, "Industry"] if code in desc.index else ""
        if hasattr(industry, "iloc"):
            industry = industry.iloc[0]
        is_def, label = _defensive_label(industry)
        if is_def:
            cut_def += 1
            defs.append((name, label))
            continue
        if exclude_holding and any(k in str(industry) for k in HOLDING_KEYWORDS):
            cut_hold += 1
            continue
        products = desc.loc[code, "Products"] if code in desc.index else ""
        if hasattr(products, "iloc"):
            products = products.iloc[0]
        kept.append({"code": code, "name": name,
                     "exchange": r["Market"], "marcap": float(r["Marcap"]),
                     "industry": str(industry or "-"),
                     "products": str(products or "-")})
    steps.append(("관리종목·투자주의환기", cut_admin))
    steps.append(("우선주·스팩·리츠", cut_type))
    steps.append(("방어주", cut_def))
    if exclude_holding:
        steps.append(("지주회사·기타 금융업", cut_hold))

    rng = random.Random(seed)   # seed=None → 매 실행마다 다른 조합
    # 데이터 확인(_verify_data)이 앞에서부터 채우다 target 개에서 멈추므로, 여유분을
    #  단순히 뒤에 붙이면 **뒤쪽(소형주) 절반은 영영 안 뽑힌다**. 2배로 고르게 뽑은 뒤
    #  짝수 번째를 본진, 홀수 번째를 예비로 갈라 놓는다 — 둘 다 시총 전 구간에 걸치므로
    #  데이터 확인에서 몇 개가 탈락해도 분포가 한쪽으로 쏠리지 않는다.
    spread = _spread_pick(kept, target * 2, rng)
    return spread[0::2] + spread[1::2], steps, defs, n0, len(kept)


def _enrich(c, df):
    """일봉에서 추세추종 적합도 지표를 뽑는다. 데이터 확인 때 어차피 받으므로 추가 비용이 없다.

    선정 이유를 '규칙을 통과했다'로만 적으면 운영자가 판단할 근거가 없다. 이 시스템이
    무엇을 좋아하는지(변동성 있는 추세)를 그대로 수치로 보여준다.
    """
    from modules import backtest as _bt
    try:
        d = _bt.compute_price_indicators(df.copy())
    except Exception:
        d = df
    last = d.iloc[-1]
    close = float(last.get("close", 0) or 0)
    atr = float(last.get("ATR", 0) or 0)
    c["atr_pct"] = (atr / close * 100) if close > 0 else 0.0
    hi52 = float(d["high"].tail(252).max()) if len(d) >= 20 else close
    lo52 = float(d["low"].tail(252).min()) if len(d) >= 20 else close
    c["w52"] = ((close - lo52) / (hi52 - lo52) * 100) if hi52 > lo52 else 0.0
    ema60 = float(last.get("EMA60", 0) or 0)
    ema120 = float(last.get("EMA120", 0) or 0)
    c["above60"] = close > ema60 > 0
    c["align"] = close > ema60 > ema120 > 0
    c["years"] = round(len(d) / 246, 1)
    # 60일선 위 유지 비율 — 스코어링의 TREND_PERSIST와 같은 뜻의 간이 지표
    try:
        c["persist"] = float((d["close"].tail(120) > d["EMA60"].tail(120)).mean() * 100)
    except Exception:
        c["persist"] = 0.0
    return c


def _fit_score(c):
    """추천 순 정렬용 적합도. 이 시스템이 무엇에 반응하는지를 그대로 반영한다.

    [측정됨] tools/audit_discover_fit.py(2026-08-16). 과거 시점 데이터로만 점수를 매기고
     이후 12개월을 돌리는 walk-forward로 쟀다. 상위 20 > 무작위 20 > 하위 20 순서가
     평균 수익에서 일관되게 나온다(씨드 3개 평균 상위 30.6% · 무작위 11.6% · 하위 6.3%,
     창별 승패 17/24). 사분위별 신호 전방 20일도 상위일수록 꼬리가 두껍다(P90 27.2 → 16.9).
    [한계] 6개월 재조정으로 창을 잘게 쪼개면 9/17로 반타작이고 창별 편차가 크다
     (-26.8%p ~ +51.3%p). '위쪽만 골라 담으라'고 말할 만큼 강하지 않다 — 확실한 것은
     **아래쪽을 피하는 쪽**이다. 폭을 넓히는 것이 여전히 본래 목적이다.
    """
    atr = min(float(c.get("atr_pct", 0) or 0), 10.0)
    score = atr / 10.0 * 3.0                       # 변동성 — 추세추종의 재료
    if c.get("align"):
        score += 2.0                               # 정배열(종가>60일선>120일선)
    elif c.get("above60"):
        score += 1.0
    score += float(c.get("persist", 0) or 0) / 100.0 * 2.0   # 60일선 위 유지 비율
    score += float(c.get("w52", 0) or 0) / 100.0 * 1.5       # 52주 위치(주도주 근접)
    if float(c.get("years", 0) or 0) < 3:
        score -= 1.5                               # 상장 3년 미만은 이력이 얕다
    return score


def _reason(c):
    """선정 이유 — 규모 + 이 시스템이 반응하는 성질을 짧게. 터미널 폭을 위해 토큰 3개 이내."""
    cap = c["marcap"] / 1e12
    tags = ["초대형" if cap >= 20 else "대형" if cap >= 5 else "중형" if cap >= 1 else "중소형"]
    if c.get("align"):
        tags.append("정배열")
    elif c.get("above60"):
        tags.append("60일선↑")
    if c.get("atr_pct", 0) >= 4.0:
        tags.append("고변동")
    elif c.get("persist", 0) >= 70:
        tags.append("추세지속")
    if c.get("years", 0) < 3:
        tags.append("신규")
    return " ".join(tags[:3])


def _verify_data(cands, target, days=3650):
    """10년 일봉 확보를 확인하고, 같은 데이터로 표시용 지표까지 계산한다."""
    from modules import backtest
    ok, cut = [], 0
    with Progress(
        SpinnerColumn(),
        TextColumn("[progress.description]{task.description}"),
        BarColumn(),
        console=config.console,
        transient=True
    ) as progress:
        task = progress.add_task("[cyan]일봉 데이터 확인 및 지표 계산 중...[/cyan]",
                                 total=min(len(cands), max(target, 1) * 2))
        for c in cands:
            if len(ok) >= target:
                break
            progress.update(task, description=f"[cyan]{c['name']} 확인 중...[/cyan]")
            try:
                df = backtest.get_backtest_data(c["code"], False, days)
                if df is None or df.empty or len(df) < 300:
                    cut += 1
                    continue
            except Exception:
                cut += 1
                continue
            ok.append(_enrich(c, df))
            progress.advance(task)   # 탈락분은 total 에 잡히지 않으므로 여기서만 올린다
    return ok, cut


def _parse_selection(text, n):
    """'1,3,5-8' / 'all' / '' 을 인덱스 집합으로. 잘못된 토큰은 그대로 돌려준다."""
    text = (text or "").strip().lower()
    if not text:
        return set(), []
    if text in ("all", "a", "전체"):
        return set(range(n)), []
    picked, bad = set(), []
    for tok in text.replace(" ", "").split(","):
        if not tok:
            continue
        if "-" in tok:
            a, _, b = tok.partition("-")
            if a.isdigit() and b.isdigit():
                lo, hi = sorted((int(a), int(b)))
                rng = [i for i in range(lo, hi + 1) if 1 <= i <= n]
                if rng:
                    picked |= {i - 1 for i in rng}
                    continue
            bad.append(tok)
        elif tok.isdigit() and 1 <= int(tok) <= n:
            picked.add(int(tok) - 1)
        else:
            bad.append(tok)
    return picked, bad


def _print_prompt_help():
    """선택 안내 — 초기 출력과 개별 분석 복귀 출력이 같은 문구를 쓰도록 한 군데에 둔다."""
    config.console.print(
        "\n[dim]추가할 번호를 고르세요. 예: [/dim][cyan]1,3,5-8[/cyan]"
        "[dim] · 전체 [/dim][cyan]all[/cyan]"
        "[dim] · 개별 분석 [/dim][cyan]d3[/cyan][dim](3번 종목) · 취소는 엔터[/dim]")


def _render_result(picked, cur):
    """탐색 결과 표를 그린다. 개별 종목 분석을 다녀오면 목록이 화면 밖으로 밀리므로
    복귀 시에도 같은 함수로 다시 그린다(운영자가 스크롤을 되짚지 않게)."""
    console = config.console
    console.print(f"\n[bold cyan]📋 탐색 결과 {len(picked)}종목[/bold cyan] "
                  f"[dim](추천 순 · 추가 시 관심종목 {cur} → {cur + len(picked)}개)[/dim]\n")
    # [스타일] 유망 종목 표(analysis.py)와 같은 규약 — HORIZONTALS · dim 헤더 · 5행마다 실선.
    table = Table(box=box.HORIZONTALS, header_style="dim", border_style="dim")
    # 숫자 열은 폭을 고정해 보호한다 — 안 그러면 rich가 이쪽부터 줄여 값이 '1…'로 뭉개진다.
    #  가변으로 두는 것은 업종 하나뿐이고, 종목명은 최소 폭을 줘 이름이 잘리지 않게 한다.
    table.add_column("No.", justify="right", width=4)
    table.add_column("종목명(코드)", justify="left", no_wrap=True, min_width=26)
    table.add_column("업종", justify="left", no_wrap=True, overflow="ellipsis")
    table.add_column("시장", justify="center", width=6)
    table.add_column("시총", justify="right", width=7)
    table.add_column("ATR%", justify="right", width=6)
    table.add_column("52주(위치)", justify="right", width=10)
    table.add_column("60일유지", justify="right", width=8)
    table.add_column("선정 이유", justify="left", no_wrap=True, min_width=18)
    for i, c in enumerate(picked):
        w52 = c.get("w52", 0)
        w52_s = f"[green]{w52:.1f}%[/green]" if w52 >= 70 else (
            f"[red]{w52:.1f}%[/red]" if w52 <= 30 else f"{w52:.1f}%")
        atr = c.get("atr_pct", 0)
        atr_s = f"[green]{atr:.1f}[/green]" if atr >= 4.0 else (
            f"[dim]{atr:.1f}[/dim]" if atr < 2.0 else f"{atr:.1f}")
        table.add_row(
            str(i + 1),
            f"{c['name']} [dim]({c['code']})[/dim]",
            c["industry"],
            c["exchange"],
            f"{c['marcap'] / 1e12:,.1f}조",
            atr_s,
            w52_s,
            f"{c.get('persist', 0):.0f}%",
            _reason(c),
        )
        # 5개마다 실선 추가
        if (i + 1) % 5 == 0 and (i + 1) < len(picked):
            table.add_section()
    console.print(table, crop=False)
    console.print(
        "[dim]ATR% = 일간 변동폭/주가(2% 미만은 회색 — 이 시스템은 변동성 있는 추세에서 수익이 납니다)\n"
        "52주(위치) = 52주 최저~최고 사이 현재가 위치 · 60일유지 = 최근 120일 중 종가가 60일선 위였던 비율\n"
        "'신규' = 상장 3년 미만 · 주요제품·재무 등 상세는 [/dim][cyan]d<번호>[/cyan][dim]로 개별 종목 분석을 보세요.[/dim]"
    )


def discover_candidates():
    """[7-5] 규칙 기반 관심 종목 탐색 → 번호 다중 선택 → 자동 추가."""
    if config.SCREEN_DEBUG_LEVEL in ["TRACE", "DEBUG"]:
        config.console.print("[dim cyan][TRACE] 관심 종목 탐색 메뉴 진입[/dim cyan]")

    console = config.console
    cur = len(config.session.stock_data.get("stocks_kr", []))
    utils.clear_screen()

    # 조건 입력 — 설명을 먼저 보여주고 나서 묻는다.
    _print_rules(target=16, pool=500, exclude_holding=True)
    console.print(f"[dim]현재 국내 주식 관심종목 {cur}개 · 권장 목표 80개[/dim]\n")

    def _ask_int(label, default, lo, hi):
        v = Prompt.ask(label, default=str(default))
        if v.lower() in ("b", "q"):
            return None
        try:
            return max(lo, min(hi, int(v)))
        except ValueError:
            return default

    target = _ask_int(f"몇 종목을 찾을까요? [dim](권장: {max(0, 80 - cur)} = 80개까지)[/dim]",
                      max(4, min(40, 80 - cur)), 1, 100)
    if target is None:
        return False
    pool = _ask_int("시가총액 상위 몇 위까지 볼까요?", 500, 50, 2000)
    if pool is None:
        return False
    excl_hold = Prompt.ask("지주회사·기타 금융업도 제외할까요?", choices=["y", "n"], default="y") == "y"

    logger.info("운영자 실행: " + " - ".join(context.USER_ACTION_BREADCRUMB))

    try:
        with Progress(
            SpinnerColumn(),
            TextColumn("[progress.description]{task.description}"),
            BarColumn(),
            console=console,
            transient=True
        ) as progress:
            progress.add_task("[cyan]종목 목록·업종 조회 중...[/cyan]", total=None)
            cands, steps, defs, n0, n_kept = _fetch_candidates(target, pool, excl_hold)
    except Exception as e:
        console.print(f"\n[bold red]종목 목록 조회 실패: {type(e).__name__} — {e}[/bold red]")
        return False

    console.print(f"\n[bold]■ 필터링 결과[/bold]  시총 상위 {n0}종목에서 출발")
    for label, cnt in steps:
        if cnt:
            console.print(f"  [dim]-[/dim] {label} [red]{cnt}종목[/red] 제외")
    console.print(f"  [dim]→[/dim] 규칙 통과 [green]{n_kept}종목[/green]")

    picked, cut_data = _verify_data(cands, target)
    # 추천(적합도) 높은 순으로 올린다 — 번호는 이 순서로 다시 매겨진다.
    picked.sort(key=_fit_score, reverse=True)
    if cut_data:
        console.print(f"  [dim]-[/dim] 10년 일봉 확보 실패 [red]{cut_data}종목[/red] 제외")
    if not picked:
        console.print("\n[yellow]조건을 만족하는 종목이 없습니다. 시총 범위를 넓혀 보세요.[/yellow]")
        return False
    console.print(f"  [dim]→[/dim] 이 중 [cyan]{len(picked)}종목[/cyan]을 뽑았습니다 "
                  f"[dim](같은 조건으로 다시 실행하면 다른 조합이 나옵니다)[/dim]")

    _render_result(picked, cur)

    _print_prompt_help()
    while True:
        sel = Prompt.ask("선택", default="").strip()
        low = sel.lower()
        if low in ("b", "q"):
            return False
        # [연계] 고르기 전에 개별 종목 분석으로 확인할 수 있게 한다. 분석 후 이 화면으로 돌아온다.
        if low.startswith(("d", "?")) and low[1:].strip().isdigit():
            n = int(low[1:].strip())
            if not 1 <= n <= len(picked):
                console.print(f"[red]1~{len(picked)} 범위의 번호를 입력하세요.[/red]")
                continue
            c = picked[n - 1]
            base_len = len(context.USER_ACTION_BREADCRUMB)
            context.USER_ACTION_BREADCRUMB.append(f"[개별분석] {c['name']}")
            try:
                from modules import analysis
                analysis.diagnose_stock(target_code=c["code"], target_name=c["name"],
                                        target_is_overseas=False)
            except Exception as e:
                console.print(f"[red]분석 실패: {type(e).__name__} — {e}[/red]")
            finally:
                context.USER_ACTION_BREADCRUMB = context.USER_ACTION_BREADCRUMB[:base_len]
            utils.pause()
            utils.clear_screen()
            console.print("\n[dim]탐색 결과로 돌아왔습니다.[/dim]")
            _render_result(picked, cur)
            _print_prompt_help()
            continue
        idxs, bad = _parse_selection(sel, len(picked))
        if bad:
            console.print(f"[red]인식할 수 없는 입력: {', '.join(bad)}[/red]")
            continue
        break
    if not idxs:
        console.print("\n[dim]추가하지 않고 돌아갑니다.[/dim]")
        return False

    chosen = [picked[i] for i in sorted(idxs)]
    console.print(f"\n[bold]{len(chosen)}종목을 추가합니다:[/bold] "
                  + ", ".join(c["name"] for c in chosen))
    if Prompt.ask("진행할까요?", choices=["y", "n"], default="y") != "y":
        console.print("\n[dim]취소했습니다.[/dim]")
        return False

    have = {s["code"] for s in config.session.stock_data.get("stocks_kr", [])}
    added = 0
    for c in chosen:
        if c["code"] in have:
            continue
        config.session.stock_data["stocks_kr"].append(
            {"name": c["name"], "code": c["code"], "exchange": c["exchange"]})
        have.add(c["code"])
        added += 1
    config.session.save_stock_config(config.session.stock_data)
    config.session.load_stock_config()

    total = len(config.session.stock_data.get("stocks_kr", []))
    logger.info(f"관심 종목 탐색으로 {added}종목 추가 (총 {total}종목)")
    console.print(f"\n[green]{added}종목을 추가했습니다. 국내 주식 관심종목 {total}개.[/green]")
    if total < 60:
        console.print("[dim]감사 기준으로 60종목부터 뚜렷한 개선이 나타납니다.[/dim]")
    console.print("[dim]자동매매가 돌고 있다면 다음 감시 주기부터 반영됩니다. "
                  "종목이 늘었으니 주기 소요 시간을 한 번 확인하세요.[/dim]")
    return True
