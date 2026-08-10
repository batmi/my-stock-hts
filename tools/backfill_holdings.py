"""보유 종목의 거래 내역을 증권사 체결 기록에서 DB로 복원한다.

지금 보유 중인 수량을 설명하는 만큼만 복원한다 — 최근 체결부터 거슬러 올라가 현재
포지션이 열린 시점까지다. 그보다 과거는 이미 청산된 다른 포지션이라 건드리지 않는다.

기본은 **계획만 출력**한다. 실제 기록은 --apply 를 붙여야 한다.
같은 주문번호의 '체결' 기록이 이미 있으면 건너뛰므로 여러 번 실행해도 중복되지 않는다.

실행:
  python tools/backfill_holdings.py                 # 수동 계좌 계획 확인
  python tools/backfill_holdings.py --account auto  # 자동매매 계좌
  python tools/backfill_holdings.py --apply         # 실제 기록
"""
import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import api  # noqa: E402
import config  # noqa: E402
import utils  # noqa: E402
from modules import holdings_backfill as hb  # noqa: E402

console = config.console


def _account(which):
    s = config.session
    if which == "auto":
        return utils.system_trading_account()
    return s.cano, s.acnt_prdt_cd


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--account", choices=["manual", "auto"], default="manual",
                    help="manual=운용자 계좌 / auto=시스템 트레이딩 계좌")
    ap.add_argument("--months", type=int, default=12, help="체결 내역 조회 기간(개월)")
    ap.add_argument("--apply", action="store_true", help="실제로 DB에 기록한다(기본은 계획만)")
    ap.add_argument("--mode", default="2",
                    help="세션 모드 (1=모의 / 2=실전 / 3=토스 / 4=관찰). 기본 2")
    args = ap.parse_args()

    # 세션을 명시적으로 연다 — 이걸 빼면 계좌·앱키가 빈 채로 조회가 실패한다.
    config.session.initialize(args.mode)
    config.session.load_stock_config()

    if not hb.supports_broker_history():
        console.print("[yellow]이 모드는 증권사 체결 이력이 없어 복원할 수 없습니다.[/yellow] "
                      "[dim](가상투자는 paper DB에 자체 원장이 있고, 토스는 KIS 체결조회 TR이 없습니다)[/dim]")
        return 0

    cano, acnt = _account(args.account)
    console.print(f"\n[bold]보유분 거래내역 복원[/bold] — 계좌 {cano}-{acnt} · 최근 {args.months}개월")

    with utils.AccountContext(cano):
        holdings, _ = api.get_domestic_balance(cano, acnt)
    if not holdings:
        console.print("[yellow]보유 종목이 없습니다. 복원할 것이 없습니다.[/yellow]")
        return 0

    plans = hb.plan(holdings, cano=cano, acnt_prdt_cd=acnt, months=args.months)
    if not plans:
        console.print("[yellow]복원 대상이 없습니다.[/yellow]")
        return 0

    total_new = 0
    for p in plans:
        new = len(p['records']) - p['already']
        total_new += new
        head = f"\n[cyan]{p['name']}({p['code']})[/cyan] 보유 {p['qty']}주"
        if p['missing'] > 0:
            head += (f"  [yellow]— 부분 복원: {p['missing']}주가 조회 구간({args.months}개월)보다 "
                     f"과거에 진입[/yellow]")
        console.print(head)
        if not p['records']:
            console.print("  [dim]체결 내역을 찾지 못했습니다.[/dim]")
            continue
        for r in p['records']:
            mark = "[dim](이미 있음)[/dim]" if hb._exists(r['odno']) else "[green]신규[/green]"
            line = f"  {r['time']}  {r['type']:12s} {r['qty']:>5}주 @ {int(r['price']):>9,}원  {mark}"
            if r['profit_amt']:
                line += f"  손익 {r['profit_amt']:+,}원 ({r['profit_rate']:+.2f}%)"
            console.print(line)

    console.print(f"\n신규 기록 대상 [bold]{total_new}건[/bold]")
    if not args.apply:
        console.print("[dim]계획만 출력했습니다. 실제로 기록하려면 --apply 를 붙이세요.[/dim]")
        return 0

    written, skipped = hb.apply(plans, cano=cano, acnt_prdt_cd=acnt)
    console.print(f"[green]기록 완료: {written}건[/green] / 건너뜀 {skipped}건")
    return 0


if __name__ == "__main__":
    sys.exit(main())
