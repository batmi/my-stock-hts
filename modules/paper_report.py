"""가상투자(페이퍼 트레이딩) 계좌 관리 — 메뉴 9-6.

[역할 분담] 가상투자 모드는 api 층에서 잔고·예수금·주문을 가로채므로
(api.get_domestic_balance 등) **기존 메뉴가 그대로 가상 계좌를 대상으로 동작한다.**
  · 5-3 트레이딩 상태 / 5-4 트레이딩 평가 → 가상 체결이 표준 trades 테이블에 쌓이므로 그대로
  · 8 주문 관리 / 9 자산 관리 → 가로챈 잔고·예수금을 그대로
따라서 이 화면은 성과 리포트를 중복해 만들지 않고, 실계좌에는 없는 **가상 계좌 고유의
관리 기능**만 담당한다: 시드 입출금, 계좌 초기화, 그리고 실계좌 화면이 알 수 없는
'시드 대비 성과'와 백테스트 분포 대비 위치.
"""
import logging

from rich import box
from rich.prompt import Prompt
from rich.table import Table

import config
import utils
from modules import paper_broker

logger = logging.getLogger(__name__)

# 백테스트 실측 분포 (2026-08-03 · 유니버스 59종목 · 3년 경로 208개 · 현행 설정).
#  추세추종은 승률 18%·PF 1.17 구조라 성적만 봐서는 정상인지 이상인지 알 수 없다.
#  이 기준선과 나란히 놓아야 "지금이 하위 몇 분위인가"를 판단할 수 있다.
BACKTEST_REFERENCE = {
    "CAGR": {"p10": -4.58, "p25": -2.17, "p50": 2.74, "p75": 13.53, "p90": 26.82},
    "MDD": {"p10": -29.77, "p25": -26.69, "p50": -23.16, "p75": -19.66, "p90": -17.09},
    "손실 경로 비율": 35.1,
    "PF 1.0 미만 비율": 44.7,
    "최장 연속 손절": 29,
}


def _percentile_label(value, dist):
    if value <= dist["p10"]:
        return "[red]하위 10% 이하[/red]"
    if value <= dist["p25"]:
        return "[yellow]하위 10~25%[/yellow]"
    if value <= dist["p50"]:
        return "[white]하위 25~50%[/white]"
    if value <= dist["p75"]:
        return "[green]상위 25~50%[/green]"
    if value <= dist["p90"]:
        return "[green]상위 10~25%[/green]"
    return "[bold green]상위 10% 이내[/bold green]"


def show_paper_menu():
    """가상투자 관리 (메뉴 9-6)."""
    if not paper_broker.is_active():
        config.console.print(
            "\n[yellow]가상투자 모드에서만 사용할 수 있습니다.[/yellow]\n"
            "[dim]프로그램을 다시 시작하고 접속 서버에서 [4] 가상투자를 선택하세요.[/dim]\n"
            "[dim]현재 모드에서는 5-4(트레이딩 평가)·9-1(자산 조회)·9-2(보유 잔고)를 그대로 쓰시면 됩니다.[/dim]")
        utils.pause()
        return

    while True:
        utils.clear_screen()
        _print_status()
        menu = Table.grid(padding=(0, 2))
        menu.add_column(style="cyan"); menu.add_column(style="dim")
        menu.add_row("[1] 가상계좌 입금", "(시드 추가 — 투입원금 증가)")
        menu.add_row("[2] 가상계좌 출금", "(시드 인출 — 투입원금 감소)")
        menu.add_row("[3] 페이퍼 트레이딩 초기화", "(포지션·체결·자산곡선 전체 삭제)")
        menu.add_row("[4] 자산 곡선", "(일별 가상 자산 추이)")
        config.console.print()
        config.console.print(menu)
        config.console.print("[dim][B] 뒤로[/dim]")
        choice = Prompt.ask("선택", choices=["1", "2", "3", "4", "b", "B"], default="b")
        if choice.lower() == "b":
            return
        if choice == "1":
            _adjust_seed(deposit=True)
        elif choice == "2":
            _adjust_seed(deposit=False)
        elif choice == "3":
            _reset_account()
        elif choice == "4":
            _show_equity_curve()


def _print_status():
    perf = paper_broker.get_performance()
    config.console.print(
        f"\n[bold cyan]가상투자 관리 (Paper Trading)[/bold cyan]  "
        f"[dim]개설 {perf['started_at']} · 시세 소스: 토스증권 · 실주문 차단[/dim]\n")

    ret_color = "red" if perf["total_return"] > 0 else ("blue" if perf["total_return"] < 0 else "white")
    t = Table(box=box.HORIZONTALS, header_style="dim", border_style="dim", show_header=False)
    t.add_column("항목", style="cyan"); t.add_column("값", justify="right"); t.add_column("비고", style="dim")
    t.add_row("가상 시드 (누적 투입)", f"{perf['seed']:,.0f}원", "")
    t.add_row("현재 총자산", f"{perf['total']:,.0f}원",
              f"현금 {perf['cash']:,.0f}원 + 주식 {perf['total']-perf['cash']:,.0f}원")
    t.add_row("누적 수익률", f"[{ret_color}]{perf['total_return']:+.2f}%[/]",
              f"{perf['total']-perf['seed']:+,.0f}원")
    t.add_row("최대 낙폭(MDD)", f"[blue]{perf['mdd']:.2f}%[/]", "일별 스냅샷 기준")
    t.add_row("Profit Factor", f"{perf['pf']:.2f}" if perf["pf"] != float("inf") else "∞",
              f"{perf['win']}승 {perf['loss']}패 · 승률 {perf['win_rate']:.1f}%")
    t.add_row("최장 연속 손절", f"{perf['max_loss_streak']}건",
              f"보유 {perf['positions']}종목 · 청산 {perf['sell_count']}건")
    config.console.print(t)

    # 백테스트 분포 대비 — 실계좌 화면이 제공하지 못하는 유일한 정보
    config.console.print("\n[bold]백테스트 분포 대비[/bold] [dim](59종목 · 3년 경로 208개 실측)[/dim]")
    bt = Table(box=box.HORIZONTALS, header_style="dim", border_style="dim")
    bt.add_column("지표"); bt.add_column("현재", justify="right")
    for q in ("p10", "p25", "p50", "p75", "p90"):
        bt.add_column(q, justify="right", style="dim")
    bt.add_column("위치")
    d = BACKTEST_REFERENCE["CAGR"]
    bt.add_row("누적 수익률", f"{perf['total_return']:+.2f}%",
               *[f"{d[q]:+.1f}%" for q in ("p10", "p25", "p50", "p75", "p90")],
               _percentile_label(perf["total_return"], d))
    d2 = BACKTEST_REFERENCE["MDD"]
    bt.add_row("MDD", f"{perf['mdd']:.2f}%",
               *[f"{d2[q]:.1f}%" for q in ("p10", "p25", "p50", "p75", "p90")],
               _percentile_label(perf["mdd"], d2))
    config.console.print(bt)
    ref = BACKTEST_REFERENCE["최장 연속 손절"]
    note = "정상 범위" if perf["max_loss_streak"] <= ref else "[yellow]기댓값 초과 — 점검 권장[/yellow]"
    config.console.print(
        f"[dim]※ 연속 손절 {perf['max_loss_streak']}건 (백테스트 10년 중앙값 {ref}건) → {note}[/dim]")
    config.console.print(
        f"[dim]※ 백테스트에서 3년을 손실로 끝낸 경로 {BACKTEST_REFERENCE['손실 경로 비율']}%, "
        f"PF 1.0 미만 {BACKTEST_REFERENCE['PF 1.0 미만 비율']}%. 손실 구간은 설계상 정상입니다.[/dim]")
    config.console.print(
        "[dim]※ 체결 내역·일별 성과는 [5-4] 트레이딩 평가, 잔고·평가손익은 [9-1] 자산 조회·"
        "[9-2] 보유 잔고에서 그대로 확인할 수 있습니다.[/dim]")


def _adjust_seed(deposit=True):
    """가상 입출금. 실계좌에 돈을 넣고 빼는 것과 같은 취급이라 포지션·이력은 유지된다.

    시드(누적 투입원금)와 가상 현금을 함께 움직인다 — 그래야 수익률 분모가 맞는다.
    (입금으로 현금만 늘리면 수익률이 저절로 좋아 보이는 착시가 생긴다.)
    """
    label = "입금" if deposit else "출금"
    cash = paper_broker.get_cash()
    seed = paper_broker.get_seed()
    config.console.print(
        f"\n[bold]가상계좌 {label}[/bold]\n"
        f"[dim]현재 시드(누적 투입) {seed:,.0f}원 · 가상 현금 {cash:,.0f}원[/dim]")
    if deposit:
        config.console.print("[dim]입금액만큼 시드와 현금이 함께 늘어납니다. (수익률 분모 = 시드)[/dim]")
    else:
        config.console.print(f"[dim]출금 가능액은 가상 현금 {cash:,.0f}원까지입니다. "
                             f"(보유 주식은 인출 대상이 아님)[/dim]")
    val = Prompt.ask(f"{label} 금액(원)", default="0")
    if str(val).strip().lower() in ("b", "q", "0", ""):
        return
    try:
        amount = abs(int(str(val).replace(",", "").strip()))
    except (TypeError, ValueError):
        config.console.print("[yellow]숫자가 아니어서 취소합니다.[/yellow]")
        utils.pause()
        return
    if amount == 0:
        return
    ok, msg = paper_broker.adjust_seed(amount if deposit else -amount)
    config.console.print(f"[green]{msg}[/green]" if ok else f"[red]{msg}[/red]")
    if ok:
        config.console.print(
            f"[dim]→ 시드 {paper_broker.get_seed():,.0f}원 · 가상 현금 {paper_broker.get_cash():,.0f}원[/dim]")
    utils.pause()


def _reset_account():
    """페이퍼 트레이딩 초기화. 시드 크기를 다시 정할 수 있다.

    시드를 바꿔 다시 시작하는 것이 이 메뉴의 주 용도다 — 실계좌 투입 예정액이 바뀌면
    1주도 못 사서 버려지는 진입 기회·유휴현금 비율이 달라지므로, 같은 조건에서 관찰하려면
    시드를 맞춘 뒤 새로 시작해야 한다.
    """
    current_seed = int(paper_broker.get_seed())
    perf = paper_broker.get_performance()
    config.console.print(
        f"\n[bold yellow]페이퍼 트레이딩을 초기화합니다.[/bold yellow]\n"
        f"[dim]현재 시드 {current_seed:,}원 · 총자산 {perf['total']:,.0f}원 "
        f"({perf['total_return']:+.2f}%) · 청산 {perf['sell_count']}건[/dim]\n"
        f"[dim]보유 포지션·체결 내역·자산 곡선이 모두 삭제되며 되돌릴 수 없습니다.[/dim]\n"
        f"[dim]※ 5-4 트레이딩 평가에 쓰이는 매매 기록(trades)은 남습니다 — "
        f"완전히 지우려면 가상투자 DB 파일을 삭제하세요.[/dim]")
    if Prompt.ask("정말 초기화할까요?", choices=["y", "n"], default="n") != "y":
        config.console.print("[dim]취소했습니다.[/dim]")
        utils.pause()
        return

    config.console.print(
        f"\n[dim]새로 시작할 가상 시드를 입력하세요. "
        f"(설정 기본값 {int(getattr(config, 'PAPER_SEED_CAPITAL', 5_000_000)):,}원)[/dim]")
    val = Prompt.ask("가상 시드(원)", default=str(current_seed))
    try:
        seed = max(1, int(str(val).replace(",", "").strip()))
    except (TypeError, ValueError):
        seed = current_seed
        config.console.print(f"[yellow]숫자가 아니어서 현재 시드 {seed:,}원으로 진행합니다.[/yellow]")
    paper_broker.reset(seed)
    config.console.print(f"[green]초기화 완료. 가상 시드 {seed:,}원으로 새로 시작합니다.[/green]")
    utils.pause()


def _show_equity_curve():
    """일별 가상 자산 추이. 시드 대비 MDD는 이 스냅샷에서만 나온다."""
    curve = paper_broker.get_equity_curve()
    if not curve:
        config.console.print(
            "\n[dim]자산 스냅샷이 아직 없습니다. 트레이딩(5-1)을 실행하면 주기마다 기록됩니다.[/dim]")
        utils.pause()
        return
    peak = curve[0]["total"]
    t = Table(title="\n일별 가상 자산 추이", box=box.HORIZONTALS, header_style="dim", border_style="dim")
    for col in ("일자", "현금", "주식평가", "총자산", "고점대비"):
        t.add_column(col, justify="left" if col == "일자" else "right")
    for e in curve[-40:]:
        peak = max(peak, e["total"])
        dd = (e["total"] - peak) / peak * 100 if peak else 0.0
        c = "blue" if dd < 0 else "white"
        t.add_row(e["date"], f"{e['cash']:,.0f}", f"{e['stock_value']:,.0f}",
                  f"{e['total']:,.0f}", f"[{c}]{dd:.2f}%[/]")
    config.console.print(t)
    if len(curve) > 40:
        config.console.print(f"[dim]※ 최근 40일만 표시 (전체 {len(curve)}일)[/dim]")
    utils.pause()
