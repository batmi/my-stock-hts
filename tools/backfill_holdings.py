"""보유 종목의 거래 내역을 증권사 체결 기록에서 DB로 복원한다.

지금 보유 중인 수량을 설명하는 만큼만 복원한다 — 최근 체결부터 거슬러 올라가 현재
포지션이 열린 시점까지다. 그보다 과거는 이미 청산된 다른 포지션이라 건드리지 않는다.

기본은 **계획만 출력**한다. 실제 기록은 --apply 를 붙여야 한다.
같은 주문번호의 '체결' 기록이 이미 있으면 건너뛰므로 여러 번 실행해도 중복되지 않는다.

실행:
  python tools/backfill_holdings.py                 # 수동 계좌 계획 확인
  python tools/backfill_holdings.py --account auto  # 자동매매 계좌
  python tools/backfill_holdings.py --apply         # 실제 기록
  python tools/backfill_holdings.py --mode 3        # 토스(거래 계좌 = 자동매매 계좌)

시스템 계좌에 기록하는 외부 순매수 종목은 제한 종목으로도 올린다(시스템 매매 제외).
"""
import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import api  # noqa: E402
import config  # noqa: E402
from core import utils  # noqa: E402
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
                    help="세션 모드 (2=한투 / 3=토스). 1=가상투자는 복원 대상이 아니다. 기본 2")
    args = ap.parse_args()

    # 세션을 명시적으로 연다 — 이걸 빼면 계좌·앱키가 빈 채로 조회가 실패한다.
    config.session.initialize(args.mode)
    config.session.load_stock_config()

    if not hb.supports_broker_history():
        console.print("[yellow]이 모드는 증권사 체결 이력이 없어 복원할 수 없습니다.[/yellow] "
                      "[dim](가상투자는 paper DB에 자체 원장이 있습니다)[/dim]")
        return 0

    cano, acnt = _account(args.account)
    console.print(f"\n[bold]보유분 거래내역 복원[/bold] — 계좌 {cano}-{acnt} · 최근 {args.months}개월")

    #  체결 조회도 같은 계좌 문맥 안에서 한다 — 앱키·토큰은 cano 인자가 아니라 thread-local
    #  이 고른다([[account-context-in-threads]]). 종전에는 잔고만 문맥 안이라 --account auto 의
    #  체결을 수동 계좌 앱키로 물었다(KIS 거절 → '조회 실패', 빈 목록이면 '부분 복원' 오진).
    with utils.AccountContext(cano):
        holdings, _ = api.get_domestic_balance(cano, acnt)
        if holdings is None:
            #  조회 실패는 '보유 없음'이 아니다([[unknown-vs-empty]]).
            console.print("[red]잔고를 조회하지 못했습니다 — '보유 없음'이 아닙니다. 다시 실행하세요.[/red]")
            return 1
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
            mark = "[dim](이미 있음)[/dim]" if hb._exists(r['odno'], on_date=hb.odno_scope_date(r)) else "[green]신규[/green]"
            line = f"  {r['time']}  {r['type']:12s} {r['qty']:>5}주 @ {int(r['price']):>9,}원  {mark}"
            if r['profit_amt']:
                line += f"  손익 {r['profit_amt']:+,}원 ({r['profit_rate']:+.2f}%)"
            console.print(line)

    console.print(f"\n신규 기록 대상 [bold]{total_new}건[/bold]")
    if not args.apply:
        console.print("[dim]계획만 출력했습니다. 실제로 기록하려면 --apply 를 붙이세요.[/dim]")
        return 0

    # 시스템 계좌(토스는 거래 계좌 = 자동매매 계좌)의 외부 순매수는 제한 종목으로도 올린다.
    #  기록만 하면 다음 기동 동기화가 '이미 기록됨'으로 보고 제한을 건너뛴다(apply_and_restrict).
    written, skipped, restricted = hb.apply_and_restrict(
        plans, cano=cano, acnt_prdt_cd=acnt, register_restrictions=hb.is_system_account(cano))
    console.print(f"[green]기록 완료: {written}건[/green] / 건너뜀 {skipped}건")
    if restricted:
        console.print(f"[yellow]제한 종목 등록(시스템 매매 제외): {', '.join(restricted)}[/yellow]")
    return 0


if __name__ == "__main__":
    sys.exit(main())
