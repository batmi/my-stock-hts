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
from datetime import datetime

from rich import box
from rich.prompt import Prompt
from rich.table import Table

import config
from core import context
from core import utils
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
    """가상투자 관리 (메뉴 9-6).

    다른 메뉴와 동일하게 utils.show_menu로 그린다 — 그래야 시스템 시간·경로 헤더가
    서브메뉴에서도 유지된다. 성과 현황은 진입 즉시 쏟아내지 않고 [5] 항목으로 두어
    메뉴 화면 자체는 짧게 유지한다.

    반환값: 'q'(메인 메뉴 점프)면 False, 그 외에는 True.
    """
    if not paper_broker.is_active():
        config.console.print(
            "\n[yellow]가상투자 모드에서만 사용할 수 있습니다.[/yellow]\n"
            "[dim]프로그램을 다시 시작하고 접속 서버에서 [4] 가상투자를 선택하세요.[/dim]\n"
            "[dim]현재 모드에서는 5-4(트레이딩 평가)·9-1(자산 조회)·9-2(보유 잔고)를 그대로 쓰시면 됩니다.[/dim]")
        utils.pause()
        return True

    menu_items = [
        ("1", "가상계좌 입금", "Deposit"),
        ("2", "가상계좌 출금", "Withdraw"),
        ("3", "페이퍼 트레이딩 초기화", "Reset Account"),
        ("4", "자산 곡선", "Equity Curve"),
        ("5", "성과 현황", "Performance"),
    ]
    menu_map = {key: name for key, name, _ in menu_items}
    base_breadcrumb_len = len(context.USER_ACTION_BREADCRUMB)
    last_choice = "5"

    while True:
        context.USER_ACTION_BREADCRUMB = context.USER_ACTION_BREADCRUMB[:base_breadcrumb_len]
        # 계좌 개설·시세 소스 정보는 [5] 성과 현황에서 보여준다(메뉴 화면은 짧게 유지).
        choice = utils.show_menu(None, menu_items, default_choice=last_choice)
        if choice.lower() == "q":
            return False
        if choice.lower() == "b":
            return True

        last_choice = choice
        context.USER_ACTION_BREADCRUMB.append(f"[{choice}] {menu_map[choice]}")
        utils.clear_screen()
        utils.print_breadcrumb()

        if choice == "1":
            _adjust_seed(deposit=True)
        elif choice == "2":
            _adjust_seed(deposit=False)
        elif choice == "3":
            _reset_account()
        elif choice == "4":
            _show_equity_curve()
        elif choice == "5":
            _print_status()
            utils.pause()


def _print_status():
    perf = paper_broker.get_performance()
    config.console.print("[bold cyan]성과 현황 (Paper Trading)[/bold cyan]")
    config.console.print(f"[dim]개설 {perf['started_at']} · 시세 소스: 한국투자증권(실전) · "
                         f"실주문 차단[/dim]\n")

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
    _print_verification_detail(perf)

    config.console.print(
        "[dim]※ 체결 내역·일별 성과는 [5-4] 트레이딩 평가, 잔고·평가손익은 [9-1] 자산 조회·"
        "[9-2] 보유 잔고에서 그대로 확인할 수 있습니다.[/dim]")


def _holding_days(started):
    """보유 일수(달력일). 시간 청산이 달력일 기준이므로 같은 기준으로 센다."""
    if not started:
        return None
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d", "%Y%m%d"):
        try:
            return (datetime.now() - datetime.strptime(str(started)[:19], fmt)).days
        except ValueError:
            continue
    return None


def _position_indicators(code, current_price):
    """관찰모드 보유 종목의 기술적 지표. 실패하면 None(호출부는 고정 임계값으로 폴백).

    9-2 잔고 화면(engine.analyze_holdings)과 **같은 순서**로 만든다:
      ① 일봉 조회 → ② 당일 봉을 실시간가로 덮음 → ③ 지표 산출
    한 단계라도 빠지면 같은 포지션의 TS 상태가 화면마다 갈린다.

    관찰모드는 국내 전용이다(paper_broker 전 경로가 is_overseas=False).
    """
    try:
        import api
        from modules import backtest
        from modules.auto_trade import indicators

        # days 는 워밍업 길이를 정할 뿐 잘라내지 않는다(get_backtest_data: days + 400).
        df = backtest.get_backtest_data(code, is_overseas=False, days=60)
        if df is None or df.empty:
            return None
        indicators.apply_realtime_price(df, api.chart_overlay_price(current_price, False))
        return indicators.calculate_indicators(df)
    except Exception as e:
        logger.debug(f"[PAPER] 지표 산출 실패 ({code}): {e}")
        return None


def _print_verification_detail(perf):
    """검증용 상세 — '지금 무엇이 돌고 있나'를 청산 없이도 확인할 수 있게 한다.

    [왜 필요한가] 위 표는 청산이 쌓여야 채워진다(PF·승률·연속손절). 운용 초기에는 전부
    0이라, 화면만 보면 시스템이 일하고 있는지 멈춰 있는지 구분되지 않는다. 그런데 이
    모드의 존재 이유가 '실매매와 같은 판정을 하는지' 확인하는 것이므로, 판정의 산출물인
    **포지션 상태**(보유일·증액 차수·TS 무장·청산선)를 그대로 보여준다. 표시선은 실제
    청산 로직과 같은 함수(engine.compute_trailing_stop)로 계산한다 — 표시용으로 다시
    쓰면 화면과 실제 청산선이 갈라진다.
    """
    positions = paper_broker.get_positions()
    curve = paper_broker.get_equity_curve()

    # 1) 표본 적정성 — 위의 '백테스트 분포 대비'가 3년 경로와의 비교라 반드시 함께 읽어야 한다.
    days = _holding_days(perf.get("started_at"))
    parts = []
    if days is not None:
        parts.append(f"운용 {days}일")
    parts.append(f"자산 스냅샷 {len(curve)}개")
    parts.append(f"청산 {perf['sell_count']}건")
    config.console.print(f"\n[bold]운용 표본[/bold] [dim]({' · '.join(parts)})[/dim]")
    if perf["sell_count"] < 20 or (days is not None and days < 180):
        config.console.print(
            "[dim yellow]※ 위 분포 대비는 3년 경로와의 비교입니다. 표본이 이만큼일 때는 "
            "위치(백분위)를 성과 판정으로 읽지 마세요 — 지금 읽을 값은 '규칙대로 동작하는가'입니다."
            "[/dim yellow]")

    # 2) 노출 — 백테스트가 재현하는 슬롯 경쟁·현금 제약이 실제로 걸리고 있는가.
    slots = getattr(config, "SYSTEM_MAX_HOLDINGS", 4) or 4
    cash_ratio = (perf["cash"] / perf["total"] * 100) if perf["total"] else 0.0
    config.console.print(
        f"[dim]· 슬롯 {len(positions)}/{slots} 사용 · 현금 비중 {cash_ratio:.1f}% "
        f"(슬롯이 차 있는데 현금이 많으면 증액 여력이 남았다는 뜻입니다)[/dim]")

    if not positions:
        return

    # 3) 포지션 상세 — 판정 로직의 현재 출력물
    from modules import db_manager
    from modules.auto_trade import engine

    highs = {}
    try:
        highs = db_manager.db.get_all_trailing_stops() or {}
    except Exception as e:
        logger.debug(f"[PAPER] 트레일링 고점 조회 실패: {e}")

    time_stop_days = config.SELL_STRATEGY.get("TIME_STOP_DAYS", 15)
    use_time_stop = config.SELL_STRATEGY.get("TIME_STOP_USE", True)

    pt = Table(title="\n보유 포지션 상세 (판정 상태)", box=box.HORIZONTALS,
               header_style="dim", border_style="dim")
    for col, just in (("종목", "left"), ("보유", "right"), ("평단", "right"),
                      ("현재가", "right"), ("수익률", "right"), ("증액", "right"),
                      ("최고가", "right"), ("TS", "left"), ("TS 기준", "right"),
                      ("시간청산", "right")):
        pt.add_column(col, justify=just, no_wrap=True)

    for p in positions:
        cur = paper_broker._current_price(p["code"], p["avg_price"])
        profit = (cur - p["avg_price"]) / p["avg_price"] * 100 if p["avg_price"] else 0.0
        held = _holding_days(p.get("first_buy_at"))
        try:
            pyr = db_manager.db.get_pyramid_count(p["code"])
        except Exception:
            pyr = -1
        high = float(highs.get(p["code"]) or 0.0)

        ts = None
        if high:
            # 트레일링 스탑 계산에 필요한 기술적 지표(ATR)를 구한다.
            #  변동성을 반영하지 않으면 전역 고정 발동선(5%)으로 폴백해 9-2 화면('대기')과
            #  이 화면('무장')이 갈린다.
            #
            #  [당일 봉 반영 · 2026-08-29] 9-2(analyze_holdings)는 차트를 받은 뒤
            #   apply_realtime_price 로 당일 봉을 실시간가로 덮고 지표를 낸다. 여기서
            #   그 한 줄을 빠뜨리면 소스가 pykrx/FDR 이라 **장중 당일이 통째로 빠진 채**
            #   ATR 이 계산된다(krx_daily 주석: pykrx·FDR 은 장중 당일 값을 주지 않는다).
            #   변동성이 큰 날 — 정확히 이 표기를 고치게 만든 상황 — 에 발동선이 다시
            #   어긋나므로 같은 보정을 태운다. chart_overlay_price 가 정규장 밖에서는
            #   0.0 을 돌려주므로 장 종료 후에는 KRX 확정 종가가 그대로 남는다.
            ind = _position_indicators(p["code"], cur)
            ts = engine.compute_trailing_stop(high, p["avg_price"], cur, ind=ind)

        if ts is None:
            ts_txt, ts_ref = "[dim]추적 전[/dim]", "[dim]-[/dim]"
        elif ts["armed"]:
            # 무장했으면 '지금 이 가격 아래로 내려가면 판다' — 실제 청산선을 그대로 보여준다.
            ts_txt = "[red]무장[/]"
            ts_ref = f"청산 {ts['stop_price']:,.0f} (-{ts['callback']:.1f}%)"
        else:
            # 아직이면 '얼마나 더 올라야 무장하는가'가 확인할 값이다.
            ts_txt = "[dim]대기[/dim]"
            ts_ref = f"발동 +{ts['activation']:.1f}% (최고 +{ts['max_profit_rate']:.1f}%)"

        if use_time_stop and held is not None:
            left = time_stop_days - held
            stop_txt = f"D-{left}" if left > 0 else "[yellow]도달[/]"
        else:
            stop_txt = "[dim]-[/dim]"

        pc = "red" if profit > 0 else ("blue" if profit < 0 else "white")
        pt.add_row(f"{p['name']}", f"{held if held is not None else '-'}일",
                   f"{p['avg_price']:,.0f}", f"{cur:,.0f}", f"[{pc}]{profit:+.2f}%[/]",
                   ("?" if pyr < 0 else f"{pyr}차"), f"{high:,.0f}" if high else "[dim]-[/dim]",
                   ts_txt, ts_ref, stop_txt)
        if pt.row_count % 5 == 0 and pt.row_count < len(positions):
            pt.add_section()
    config.console.print(pt)
    config.console.print(
        "[dim]※ TS 청산선은 실제 청산 판정과 같은 함수(engine.compute_trailing_stop)로 "
        "계산합니다. '추적 전'은 최고가 기록이 아직 없다는 뜻입니다(매수 직후·재시작 직후).[/dim]")


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
        f"[dim]※ 오늘 시작 자산 기준선(일일 손실 한도·드로다운 판정 기준)도 함께 초기화됩니다.[/dim]\n"
        f"[dim]※ 5-4 트레이딩 평가가 보는 매매 기록(trades)·트레일링 최고가·반익절 이력·"
        f"예약 주문·매매일지 전송 대기열도 함께 삭제됩니다. 실계좌 DB는 파일이 달라 영향 없습니다.[/dim]\n"
        f"[dim]※ 가상 계좌에 걸린 트레이딩 제한 종목도 해제됩니다(실계좌 제한은 유지).[/dim]\n"
        f"[dim]※ 이미 매매일지 웹서버로 전송된 기록은 그대로 남습니다(웹에서 직접 삭제).[/dim]")
    if Prompt.ask("정말 초기화할까요?", choices=["y", "n"], default="n") != "y":
        config.console.print("[dim]취소했습니다.[/dim]")
        utils.pause()
        return

    config.console.print(
        f"\n[dim]새로 시작할 가상 시드를 입력하세요. "
        f"(설정 기본값 {int(getattr(config, 'PAPER_SEED_CAPITAL', 10_000_000)):,}원)[/dim]")
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
    # 보유 종목 수는 체결 원장을 되감아 얻는다. 매매가 없던 날은 직전 값을 잇는다.
    held_map = paper_broker.holdings_count_by_date()
    slots = getattr(config, 'SYSTEM_MAX_HOLDINGS', 4)
    cur_seed = paper_broker.get_seed()
    seed_approx = any(e.get("seed") is None for e in curve[-40:])

    t = Table(title="\n일별 가상 자산 추이", box=box.HORIZONTALS, header_style="dim", border_style="dim")
    # '변동'은 전일이 아니라 **직전 스냅샷** 대비다 — 이 표는 트레이딩 루프가 돈 날만 있어
    #  주말·미실행일이 통째로 빠진다(예: 금 → 월). '전일대비'로 적으면 거짓말이 된다.
    for col in ("일자", "현금", "주식평가", "총자산", "변동", "누적", "주식비중", "보유", "고점대비"):
        t.add_column(col, justify="left" if col == "일자" else "right")
    rows = curve[-40:]
    prev_total, held = None, 0
    for e in rows:
        peak = max(peak, e["total"])
        dd = (e["total"] - peak) / peak * 100 if peak else 0.0
        c = "blue" if dd < 0 else "white"

        if prev_total:
            chg = (e["total"] - prev_total) / prev_total * 100
            chg_txt = f"[{'red' if chg > 0 else 'blue' if chg < 0 else 'white'}]{chg:+.2f}%[/]"
        else:
            chg_txt = "[dim]-[/]"

        seed = e.get("seed") or cur_seed
        cum = (e["total"] - seed) / seed * 100 if seed else 0.0
        cum_txt = f"[{'red' if cum > 0 else 'blue' if cum < 0 else 'white'}]{cum:+.2f}%[/]"

        # 주식비중 = 노출. 슬롯이 다 차도 사이징 층(기초비중 × 변동성 배수)이 상한을 정한다.
        expo = (e["stock_value"] / e["total"] * 100) if e["total"] else 0.0
        held = held_map.get(e["date"], held)

        t.add_row(e["date"], f"{e['cash']:,.0f}", f"{e['stock_value']:,.0f}",
                  f"{e['total']:,.0f}", chg_txt, cum_txt,
                  f"{expo:.0f}%", f"{held}/{slots}", f"[{c}]{dd:.2f}%[/]")
        prev_total = e["total"]
        # 5행마다 구분선 — 다른 목록 표(종목 표·테마 표)와 같은 규칙. 마지막 행 뒤에는
        #  넣지 않는다(표 하단 테두리와 겹쳐 두 줄로 보인다).
        if t.row_count % 5 == 0 and t.row_count < len(rows):
            t.add_section()
    config.console.print(t)
    config.console.print(
        "[dim]※ 변동=직전 스냅샷 대비(휴장·미실행일은 행이 없어 하루가 아닐 수 있음) · "
        "누적=시드 대비 · 주식비중=총자산 중 주식 평가액(노출) · 고점대비=자산 고점 대비 하락률[/dim]")
    if seed_approx:
        # 옛 행에는 그 시점 시드가 없다. 입출금이 있었다면 누적 열이 그만큼 틀어진다 —
        #  분모를 숨기지 않고 밝힌다(모르는 것을 아는 척하지 않는다).
        config.console.print(
            f"[dim yellow]※ 시드 기록이 없는 과거 행은 현재 시드({cur_seed:,.0f}원)로 누적을 계산했습니다 "
            f"— 그 사이 입출금이 있었다면 해당 행의 누적은 부정확합니다.[/dim yellow]")
    if len(curve) > 40:
        config.console.print(f"[dim]※ 최근 40일만 표시 (전체 {len(curve)}일)[/dim]")
    utils.pause()
