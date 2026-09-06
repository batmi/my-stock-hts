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
from rich.progress import BarColumn, Progress, SpinnerColumn, TextColumn
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
    # [기준 명시] 총자산은 '지금' 재평가한 값이다. 장 밖에서는 그 '지금'이 무엇인지
    #  (KRX 종가인지 NXT 최종가인지) 밝히지 않으면 자산 곡선의 마지막 행과 왜 다른지
    #  알 수 없다 — 곡선은 그날의 확정 스냅샷이고 이 값은 조회 시점 평가다.
    #  세션 이름(market_session_label)은 **표시 시세**의 기준이라 여기 쓰면 안 된다 —
    #  실제로 '휴장 · NXT 최종가'라고 적어 놓고 총자산은 KRX 확정 종가로 계산하고 있었다.
    #  평가가 실제로 무엇을 보는지(정규장=실시간 / 그 밖=확정 종가)를 그대로 적는다.
    try:
        import api
        if api.chart_overlay_enabled(False):
            _basis = " · 평가 기준: 정규장 실시간가"
        else:
            _d = api.krx_last_settled_day()
            _d = f"{_d[4:6]}-{_d[6:8]}" if _d and len(_d) == 8 else _d
            _basis = f" · 평가 기준: KRX 확정 종가({_d})"
    except Exception:      # noqa: BLE001 - 부가 표기는 실패해도 화면을 막지 않는다
        _basis = ""
    config.console.print(f"[dim]개설 {perf['started_at']} · 시세 소스: 한국투자증권(실전) · "
                         f"실주문 차단{_basis}[/dim]\n")

    ret_color = "red" if perf["total_return"] > 0 else ("blue" if perf["total_return"] < 0 else "white")
    t = Table(box=box.HORIZONTALS, header_style="dim", border_style="dim", show_header=False)
    t.add_column("항목", style="cyan"); t.add_column("값", justify="right"); t.add_column("비고", style="dim")
    t.add_row("가상 시드 (누적 투입)", f"{perf['seed']:,.0f}원", "")
    t.add_row("현재 총자산", f"{perf['total']:,.0f}원",
              f"현금 {perf['cash']:,.0f}원 + 주식 {perf['total']-perf['cash']:,.0f}원")
    t.add_row("누적 수익률", f"[{ret_color}]{perf['total_return']:+.2f}%[/]",
              f"{perf['total']-perf['seed']:+,.0f}원")
    t.add_row("최대 낙폭(MDD)", f"[blue]{perf['mdd']:.2f}%[/]", "일별 스냅샷 + 현재값")
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


class _HeatShim:
    """compute_portfolio_heat 가 참조하는 최소 표면(락·트레일링 고점 캐시).

    [왜 실물 트레이더를 안 쓰는가] 이 화면은 자동매매 루프가 돌지 않아도 열린다.
    그렇다고 리스크 산식을 여기서 다시 쓰면 화면과 실제 판정이 갈라진다 — 이 파일이
    TS 표시선에서 이미 겪은 문제다(engine.compute_trailing_stop 을 그대로 쓰는 이유).
    그래서 산식은 건드리지 않고 **호출에 필요한 껍데기만** 만든다.
    """

    def __init__(self, highs, equity):
        import threading
        self._lock = threading.RLock()
        self.trailing_stop_cache = dict(highs or {})
        self.current_total_asset = equity
        self.initial_asset = equity
        self.portfolio_heat_amt = 0.0
        self.portfolio_heat_unknown = False
        # [리스크 스케일] 실효 캡 = SYSTEM_MAX_PORTFOLIO_RISK × 배수다. 배수를 1.0으로 두면
        #  화면이 명목 캡(10%)을 보여주는데 엔진은 축소된 캡(예: 8.5%)으로 매수를 막는다 —
        #  "왜 막혔는지 모르겠다"가 정확히 이 어긋남에서 나온다. 돌고 있는 트레이더가
        #  있으면 그 값을 그대로 빌리고, 없으면 1.0으로 두되 호출부가 '명목'이라고 밝힌다.
        self.risk_scale = 1.0
        self.risk_scale_known = False
        # [표시=판정] 엔진은 직전 주기 매도 판정이 실제로 쓴 손절률·ATR로 오픈 리스크를
        #  잰다(engine.compute_portfolio_heat live_map). 화면이 그걸 안 보면 역산 근사로
        #  더 작은 리스크를 띄워, 엔진이 캡으로 막은 이유가 화면에서 사라진다.
        self.live_risk_map = {}
        try:
            import modules.auto_trade as _at
            inst = getattr(_at.AutoTrader, "_instance", None)
            scale = getattr(inst, "risk_scale", None) if inst is not None else None
            if scale and 0 < float(scale) <= 1.0:
                self.risk_scale = float(scale)
                self.risk_scale_known = True
            live = getattr(inst, "holding_risk_cache", None) if inst is not None else None
            if isinstance(live, dict):
                self.live_risk_map = dict(live)
        except Exception:
            pass

    def log(self, *a, **k):
        pass


def _position_open_risk(positions):
    """종목별 (손절선, 오픈 리스크)·남은 예산·실효 캡(%)·스케일 반영 여부.
    실패 시 ({}, None, None, False).

    [detail] 종전에는 총합에서 손절선을 되짚었다(충분히 높은 가격을 넣어 0 클립을 피한 뒤
    빼는 역산). 히트 기준이 매수가로 바뀌면서 그 트릭은 성립하지 않는다 — 이익이 잠긴
    포지션은 리스크가 0이라 되짚을 것이 없다. 엔진이 손절선을 직접 돌려주게 했다.
    """
    from modules import db_manager
    from modules.auto_trade import common, engine
    try:
        highs = db_manager.db.get_all_trailing_stops() or {}
        equity = paper_broker.get_performance()["total"]
        shim = _HeatShim(highs, equity)
        rm = engine.RiskManager(shim)
        codes = [p["code"] for p in positions]
        buy_map = db_manager.db.get_buy_trades_for_current_holdings(codes) or {}

        entries = [{'pdno': p["code"], 'hldg_qty': str(p["qty"]),
                    'pchs_avg_pric': f'{p["avg_price"]:.4f}',
                    'prpr': str(int(paper_broker.valuation_price(code=p["code"],
                                                                 fallback=p["avg_price"])))}
                   for p in positions]
        total, detail = rm.compute_portfolio_heat(
            entries, {p["code"]: buy_map.get(p["code"]) or [] for p in positions},
            live_map=shim.live_risk_map, detail=True)
        risks = {c: (stop, risk) for c, (stop, risk) in detail.items()}

        shim.portfolio_heat_amt = total
        return (risks, rm.portfolio_risk_budget_left(), rm.effective_portfolio_cap(),
                shim.risk_scale_known)
    except Exception as e:
        logger.debug(f"[PAPER] 오픈 리스크 산출 실패: {e}")
        return {}, None, None, False


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
    from modules.auto_trade import common, engine

    highs = {}
    try:
        highs = db_manager.db.get_all_trailing_stops() or {}
    except Exception as e:
        logger.debug(f"[PAPER] 트레일링 고점 조회 실패: {e}")

    use_time_stop = config.SELL_STRATEGY.get("TIME_STOP_USE", True)

    pt = Table(title="\n보유 포지션 상세 (판정 상태)", box=box.HORIZONTALS,
               header_style="dim", border_style="dim", collapse_padding=True)
    #  [열 구성 · 2026-09-05] 수량·평가금액을 넣으면서 표 폭 상한(메뉴 2-1 실측 135열)을
    #   넘겼다. 열을 지우기 전에 접는다는 순서대로 두 번 접었다:
    #    · TS 상태와 TS 기준을 한 열로 합쳤다(둘 다 항상 같이 읽는 값이다).
    #    · 최고가(원)를 지우고 트레일링 열의 '최고 +x%'만 남겼다 — 같은 값을 절대가와
    #      상대율로 두 번 쓰고 있었고, 무장 여부를 가르는 것은 상대율 쪽이다.
    #   결과 134열. 헤더에 두 숫자의 뜻을 적어(발동/최고) 칸 안의 설명 글자를 덜어냈다.
    for col, just in (("종목", "left"), ("일수", "right"), ("수량", "right"), ("평단", "right"),
                      ("현재가", "right"), ("평가금액", "right"), ("수익률", "right"),
                      ("증액", "right"), ("트레일링(발동/최고)", "right"),
                      ("기한", "right"), ("손절선", "right"), ("여유", "right"),
                      ("리스크", "right")):
        pt.add_column(col, justify=just, no_wrap=True)

    # [왜 손절선을 함께 보여주는가] 위 TS 열은 '무장 전'이면 아직 없는 선이다. 그 상태에서
    #  실제로 포지션을 지키는 것은 ATR 손절선인데, 종전 표에는 그것이 없었다 — 4종목 전부
    #  '대기'인 화면에서 **작동 중인 방어선이 하나도 표시되지 않는** 상태였다.
    #  오픈 리스크는 그 선까지의 잠재손실(= 히트 캡이 세는 값)이고, 예산비는 그 종목이
    #  계좌 전체 리스크 예산에서 차지하는 몫이다. 한 종목이 예산을 삼키면 다른 종목의
    #  신규 진입·증액이 막히므로([[heat-cap-formula-divergence]]) 여기서 보여야 한다.
    #  [진행 표시 · 2026-09-05] 이 표는 종목마다 차트를 받아 ATR 을 다시 낸다
    #   (_position_indicators → 원격 조회). 보유가 몇 종목만 돼도 화면이 수 초 멈춰 있어
    #   멈춘 것인지 계산 중인지 구분되지 않았다. 다른 원격 조회 화면(manage/insider·
    #   disclosure)과 같은 방식으로 진행률을 보여준다. transient=True 라 끝나면 사라지고
    #   표만 남는다.
    with Progress(SpinnerColumn(), TextColumn("[progress.description]{task.description}"),
                  BarColumn(), TextColumn("[progress.percentage]{task.percentage:>3.0f}%"),
                  console=config.console, transient=True) as progress:
        #  +1 은 리스크 예산 산출(포지션 전체를 한 번에 계산한다).
        task = progress.add_task("[cyan]포지션 판정 상태 계산 중...[/cyan]",
                                 total=len(positions) + 1)
        risk_by_code, budget_left, heat_cap, scale_known = _position_open_risk(positions)
        heat_cap_amt = (perf["total"] * heat_cap / 100.0) if heat_cap else 0.0
        progress.advance(task)

        for p in positions:
            # 총자산과 같은 규칙으로 평가한다(장 종료 후 = KRX 확정 종가). 종전에는
            #  _current_price 라 표만 NXT 최종가였고, 총자산과 합계가 어긋났다.
            cur = paper_broker.valuation_price(p["code"], p["avg_price"])
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
                ts_txt = "[dim]추적 전[/dim]"
            elif ts["armed"]:
                # 무장했으면 '지금 이 가격 아래로 내려가면 판다' — 실제 청산선을 그대로
                #  보여준다. 뒤 숫자는 콜백폭이다.
                ts_txt = f"[red]무장[/] {ts['stop_price']:,.0f} / -{ts['callback']:.1f}%"
            else:
                # 아직이면 '얼마나 더 올라야 무장하는가'(발동선)와 '지금 어디까지
                #  올라와 있나'(최고)를 나란히 둔다.
                ts_txt = (f"[dim]대기[/dim] +{ts['activation']:.1f}% "
                          f"/ +{ts['max_profit_rate']:.1f}%")

            if use_time_stop and held is not None:
                #  기한은 그 종목에 실제로 적용되는 값이다 — 개별 룰이 바꾸면 청산 판정도
                #  그 값을 쓴다(common.effective_time_stop_days). 전역만 보면 D-n 이 거짓말한다.
                left = common.effective_time_stop_days(p["code"]) - held
                stop_txt = f"D-{left}" if left > 0 else "[yellow]도달[/]"
            else:
                stop_txt = "[dim]-[/dim]"

            # 손절선·오픈리스크. 산출 실패는 '0'이 아니라 '모름'으로 적는다 — 리스크를 0으로
            #  보여주면 없는 안전을 있는 것처럼 읽힌다(히트 산식의 fail-closed와 같은 이유).
            sl_price, risk = risk_by_code.get(p["code"], (None, None))
            if sl_price is None:
                sl_txt = room_txt = risk_txt = "[dim]?[/dim]"
            else:
                room = (sl_price - cur) / cur * 100 if cur else 0.0
                sl_txt = f"{sl_price:,.0f}"
                room_txt = f"[blue]{room:.1f}%[/]" if room < 0 else f"[red]+{room:.1f}%[/]"
                # 예산비 = 이 종목이 계좌 리스크 예산(히트 캡)에서 차지하는 몫.
                share = f" ({risk / heat_cap_amt * 100:.0f}%)" if heat_cap_amt else ""
                risk_txt = f"{risk:,.0f}{share}"

            pc = "red" if profit > 0 else ("blue" if profit < 0 else "white")
            pt.add_row(f"{p['name']}", f"{held if held is not None else '-'}",
                       f"{p['qty']:,}", f"{p['avg_price']:,.0f}", f"{cur:,.0f}",
                       f"{cur * p['qty']:,.0f}", f"[{pc}]{profit:+.2f}%[/]",
                       ("?" if pyr < 0 else f"{pyr}차"),
                       ts_txt, stop_txt, sl_txt, room_txt, risk_txt)
            if pt.row_count % 5 == 0 and pt.row_count < len(positions):
                pt.add_section()
            progress.advance(task)
    config.console.print(pt)

    # 리스크 예산 — 개별 행의 '예산비'가 무엇의 몫인지 여기서 분모를 밝힌다.
    #  캡이 차면 신규 매수도 증액도 막히므로, 막혔을 때 원인을 이 줄에서 바로 읽을 수 있다.
    total_risk = sum(r for _s, r in risk_by_code.values())
    if risk_by_code and heat_cap:
        used = (total_risk / heat_cap_amt * 100) if heat_cap_amt else 0.0
        color = "red" if used >= 100 else ("yellow" if used >= 80 else "white")
        left_txt = (f"여유 {budget_left:,.0f}원" if (budget_left or 0) > 0
                    else "[red]초과 — 신규 매수·증액 차단 중[/red]")
        # 자동매매가 돌고 있지 않으면 리스크 스케일(약세·드로다운 축소)을 알 수 없다.
        #  그때는 명목 캡이라고 밝힌다 — 실제 캡은 이보다 작을 수 있다(축소 방향뿐이다).
        cap_label = "캡" if scale_known else "명목 캡"
        note = "" if scale_known else " [dim](자동매매 미동작 — 리스크 스케일 미반영)[/dim]"
        config.console.print(
            f"  [bold]리스크 예산[/bold] 오픈 리스크 {total_risk:,.0f}원 / "
            f"{cap_label} {heat_cap:.1f}% {heat_cap_amt:,.0f}원 → "
            f"[{color}]{used:.0f}% 소진[/] · {left_txt}{note}")
    elif risk_by_code:
        config.console.print(
            f"  [bold]리스크 예산[/bold] 오픈 리스크 {total_risk:,.0f}원 [dim](캡 미사용)[/dim]")
    if risk_by_code:
        # 표 밖으로 나온 요약 줄과 아래 범례(※)를 붙여 두면 한 덩어리로 읽힌다. 한 줄 띄운다.
        config.console.print()

    config.console.print(
        "[dim]※ TS 청산선은 실제 청산 판정과 같은 함수(engine.compute_trailing_stop)로 "
        "계산합니다. '추적 전'은 최고가 기록이 아직 없다는 뜻입니다(매수 직후·재시작 직후).[/dim]")
    config.console.print(
        "[dim]※ 손절선=수량가중 평균 손절률 기준 청산가(TS 무장 시 그 선). 여유=현재가 대비 거리. "
        "오픈리스크=그 선까지 내려갈 때의 잠재손실(히트 캡이 세는 값).[/dim]")


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
    # 보유 수·실현손익·매매는 체결 원장을 되감아 얻는다. 매매가 없던 날은 보유만 직전 값을 잇는다.
    ledger = paper_broker.daily_ledger()
    slots = getattr(config, 'SYSTEM_MAX_HOLDINGS', 4)
    cur_seed = paper_broker.get_seed()
    seed_approx = any(e.get("seed") is None for e in curve[-40:])

    t = Table(title="\n일별 가상 자산 추이", box=box.HORIZONTALS, header_style="dim", border_style="dim")
    # '변동'은 전일이 아니라 **직전 스냅샷** 대비다 — 이 표는 트레이딩 루프가 돈 날만 있어
    #  주말·미실행일이 통째로 빠진다(예: 금 → 월). '전일대비'로 적으면 거짓말이 된다.
    for col in ("일자", "현금", "주식평가", "총자산", "변동", "누적", "주식비중", "보유",
                "고점대비", "실현손익", "매매"):
        t.add_column(col, justify="left" if col in ("일자", "매매") else "right",
                     no_wrap=(col == "매매"))
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
        day = ledger.get(e["date"]) or {}
        held = day.get("holdings", held)

        # [왜 실현손익을 나누어 적는가] '변동'만으로는 그날 무슨 일이 있었는지 알 수 없다.
        #  실측 2026-08-26: 변동 -0.62%였지만 실현손익이 -102,356원이었다 — 즉 보유분 평가는
        #  오히려 +40,220 올랐고, 자산이 준 것은 손절이 확정됐기 때문이다. 추세추종에서
        #  '손절 규칙이 정상 작동한 날'과 '시장이 나빴던 날'은 완전히 다른 사건인데,
        #  이 열이 없으면 둘이 구분되지 않는다. (평가분 = 변동 − 실현손익)
        rp = day.get("realized") or 0.0
        rp_txt = f"[{'red' if rp > 0 else 'blue'}]{rp:+,.0f}[/]" if rp else "[dim]-[/dim]"
        # 종목명까지 적으면 표가 2-1 기준 폭(135)을 넘는다. 건수만 남긴다 —
        #  "그날 매매가 있었나"는 여기서, "무엇을 샀나"는 5-4 체결 내역에서 본다.
        evs = day.get("events") or []
        nb = sum(1 for x in evs if x.startswith("+"))
        ns = len(evs) - nb
        ev = " ".join(t for t in (f"매수{nb}" if nb else "", f"매도{ns}" if ns else "") if t) or "-"

        t.add_row(e["date"], f"{e['cash']:,.0f}", f"{e['stock_value']:,.0f}",
                  f"{e['total']:,.0f}", chg_txt, cum_txt,
                  f"{expo:.0f}%", f"{held}/{slots}", f"[{c}]{dd:.2f}%[/]",
                  rp_txt, f"[dim]{ev}[/dim]")
        prev_total = e["total"]
        # 5행마다 구분선 — 다른 목록 표(종목 표·테마 표)와 같은 규칙. 마지막 행 뒤에는
        #  넣지 않는다(표 하단 테두리와 겹쳐 두 줄로 보인다).
        if t.row_count % 5 == 0 and t.row_count < len(rows):
            t.add_section()
    config.console.print(t)

    # ── 요약 — 표를 위아래로 훑지 않아도 기간 전체의 모양이 잡히게 한다. 열을 늘리지 않고
    #    밀도를 올리는 자리라, 표에 넣기 애매한 '기간 집계'는 전부 여기로 모은다.
    totals = [e["total"] for e in curve]
    p, mdd, under = totals[0], 0.0, 0
    for v in totals:
        p = max(p, v)
        d = (v - p) / p * 100 if p else 0.0
        mdd = min(mdd, d)
        under += (d < 0)
    expos = [(e["stock_value"] / e["total"] * 100) for e in curve if e["total"]]
    realized = sum((v.get("realized") or 0.0) for v in ledger.values())
    first_seed = next((e["seed"] for e in curve if e.get("seed")), cur_seed)
    cum_all = (totals[-1] - cur_seed) / cur_seed * 100 if cur_seed else 0.0
    # 노출 상한은 사이징 구조가 정한다 — 기초비중 × 변동성 배수 하한 × 슬롯 수.
    #  슬롯이 다 차도 이 위로는 못 간다. '왜 현금이 노는가'의 답이 이 한 줄에 있다.
    expo_cap = (config.resolve_invest_ratio()
                * getattr(config, 'VOLATILITY_SCALING_MIN', 0.4) * slots * 100)

    cc = "red" if cum_all > 0 else "blue" if cum_all < 0 else "white"
    config.console.print(
        f"\n[bold]기간 요약[/bold] [dim]{curve[0]['date']} ~ {curve[-1]['date']} "
        f"({len(curve)}일) · 시드 {cur_seed:,.0f}원"
        + (f" (시작 {first_seed:,.0f}원)" if first_seed != cur_seed else "") + "[/dim]")
    config.console.print(
        f"  누적 [{cc}]{cum_all:+.2f}%[/] · MDD [blue]{mdd:.2f}%[/] · "
        f"고점 아래 {under}/{len(totals)}일")
    if expos:
        config.console.print(
            f"  노출 평균 {sum(expos) / len(expos):.0f}% · 최대 {max(expos):.0f}% "
            f"[dim](사이징 구조 상한 {expo_cap:.0f}%)[/dim]")
    config.console.print(
        f"  실현손익 누적 [{'red' if realized > 0 else 'blue' if realized else 'white'}]"
        f"{realized:+,.0f}원[/] · 평가분 {totals[-1] - cur_seed - realized:+,.0f}원")

    # 행의 평가 기준을 밝힌다 — 마감 스냅샷이 찍히려면 그 시각에 자동매매가 돌고
    #  있어야 한다. 15:20 전에 세우면 그날 행은 장중가로 남는다(숨기지 않는다).
    #  [줄바꿈 2026-09-06] 종전에는 한 줄로 이어 붙여 터미널 폭에서 **아무 데서나**
    #   접혔다 — 항목이 줄 끝에 걸려 "고점대비=자산 고점 대비" / "하락률" 처럼 갈렸다.
    #   열 설명은 항목 단위로 읽히는 글이므로 접히는 자리를 우리가 정한다.
    config.console.print(
        "\n[dim]※ 각 행은 그날 KRX 확정 종가 기준(마감 시각에 자동매매가 돌고 있었을 때)\n"
        "    · 변동 = 직전 스냅샷 대비(휴장·미실행일은 행이 없어 하루가 아닐 수 있음)\n"
        "    · 누적 = 시드 대비  · 주식비중 = 총자산 중 주식 평가액(노출)\n"
        "    · 고점대비 = 자산고점 대비 하락률  · 평가분 = 변동 − 실현손익[/dim]")
    if seed_approx:
        # 옛 행에는 그 시점 시드가 없다. 입출금이 있었다면 누적 열이 그만큼 틀어진다 —
        #  분모를 숨기지 않고 밝힌다(모르는 것을 아는 척하지 않는다).
        config.console.print(
            f"\n[dim yellow]※ 시드 기록이 없는 과거 행은 현재 시드({cur_seed:,.0f}원)로 "
            f"누적을 계산했습니다.\n"
            f"     그 사이 입출금이 있었다면 해당 행의 누적은 부정확합니다.[/dim yellow]")
    if len(curve) > 40:
        config.console.print(f"[dim]※ 최근 40일만 표시 (전체 {len(curve)}일)[/dim]")
    utils.pause()
