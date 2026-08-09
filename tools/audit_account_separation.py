#!/usr/bin/env python3
"""수동 계좌 / 자동매매 계좌 분리가 실제로 성립하는지 감사한다.

mode 2에서 REAL_ACC_NUM(수동)과 AUTO_ACC_NUM(자동매매)을 나눠 쓰면, 다음 두 가지가
동시에 성립해야만 분리가 의미를 갖는다.

  1) 라우팅 — 시스템 트레이딩의 모든 주문·수량조회가 자동 계좌를 향한다.
     계좌 판정은 context.trade_context(threading.local)로 하는데 이 값은 스레드 간
     상속되지 않는다. 매도 판정은 워커 풀에서 돌기 때문에, 전파가 없으면 매도만
     수동 계좌로 새고 매수는 자동 계좌로 간다. '자동 계좌로 사고 자동 계좌에서 못
     파는' 상태가 되어 손절이 수량 0으로 조용히 취소된다.

  2) 유량(TPS) — KIS 한도 20 TPS는 계좌가 아니라 앱키 단위다. 두 키가 다르면 예산도
     따로 잡아야 운용자 조회가 매매 예산을 잠식하지 않는다.

사용법:
    python tools/audit_account_separation.py            # 오프라인 검증만 (네트워크 없음)
    python tools/audit_account_separation.py --live     # + 두 계좌 잔고 읽기 전용 조회

--live는 조회 전용이다. 주문은 내지 않는다. 디스크에 캐시된 토큰을 재사용하므로
운용 인스턴스가 떠 있어도 토큰 발급(앱키당 1분 1회) 경합을 일으키지 않는다.
"""
import argparse
import concurrent.futures
import os
import sys

sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import config
import context
from rich.console import Console
from rich.table import Table

console = Console()

_FAIL = []


def check(ok, label, detail=""):
    mark = "[green]OK[/green]  " if ok else "[bold red]FAIL[/bold red]"
    console.print(f"  {mark} {label}" + (f"  [dim]{detail}[/dim]" if detail else ""))
    if not ok:
        _FAIL.append(label)
    return ok


def section(title):
    console.print(f"\n[bold cyan]{title}[/bold cyan]")


def audit_config():
    section("A. 설정 — 계좌·앱키가 실제로 나뉘어 있는가")
    s = config.session
    same_acc = s.auto_cano == s.cano
    same_key = (not s.auto_app_key) or (s.auto_app_key == s.real_app_key)

    console.print(f"  [dim]수동 계좌 {s.cano}-{s.acnt_prdt_cd} / 자동 계좌 {s.auto_cano}-{s.auto_acnt_prdt_cd}[/dim]")
    check(not same_acc, "계좌번호 분리 (AUTO_ACC_NUM ≠ REAL_ACC_NUM)",
          "동일하면 분리 자체가 없는 구성이다" if same_acc else "")
    check(not same_key, "앱키 분리 (AUTO_APP_KEY ≠ REAL_APP_KEY)",
          "동일하면 TPS 예산을 나눌 수 없다(한도가 앱키 단위)" if same_key else "")
    return not same_acc, not same_key


def audit_routing(acc_split):
    section("B. 라우팅 — 워커 스레드에서도 자동 계좌로 가는가")
    import api
    import utils

    def resolved():
        return api._prepare_account_params(None, None)[0]

    target = utils.system_trading_account()[0]
    check(target == config.session.auto_cano,
          "utils.system_trading_account() → 자동 계좌", f"→ {target}")

    # 워커 스레드로 전파되는가
    with utils.AccountContext(target):
        task = utils.inherit_account_context(resolved)
        with concurrent.futures.ThreadPoolExecutor(max_workers=4, thread_name_prefix="at_sell") as ex:
            got = {f.result() for f in [ex.submit(task) for _ in range(8)]}
    check(got == {target}, "at_sell 워커 스레드가 자동 계좌를 유지", f"→ {got}")

    # 주문 경로 최종 방어선: send_order는 어느 스레드에서든 자동 계좌로 고정
    from unittest.mock import patch
    from modules.auto_trade import engine
    import threading

    seen = []

    def fake_place_order(market, action, code, qty, price, ord_dvsn, exchange_code=None):
        seen.append((action, resolved(), utils.get_common_headers("TTTC0802U")["appKey"]))
        return {"rt_cd": "1", "msg1": "audit(발주 안 함)", "msg_cd": "AUDIT", "output": {}}

    class _T:
        trade_history = []
        trailing_stop_cache = {}
        _lock = threading.RLock()

        def log(self, *a, **k):
            pass

    om = engine.OrderManager.__new__(engine.OrderManager)
    om.trader = _T()
    om._lock = threading.RLock()
    om.pending_orders = {}
    om.orders_sent_count = 0
    om.order_fail_alerted = {}

    with patch.object(api, 'place_order', fake_place_order):
        with concurrent.futures.ThreadPoolExecutor(max_workers=2, thread_name_prefix="at_sell") as ex:
            for side in ("sell", "buy"):
                ex.submit(om.send_order, "005930", 1, side, name="감사용", price=70000).result()

    ok = all(c == config.session.auto_cano for _, c, _ in seen)
    check(ok, "send_order가 스레드와 무관하게 자동 계좌로 발주",
          " / ".join(f"{a}→{c}" for a, c, _ in seen))
    if config.session.auto_app_key:
        ok_key = all(k == config.session.auto_app_key for _, _, k in seen)
        check(ok_key, "주문 헤더가 자동매매 앱키를 사용")


def audit_tps(key_split):
    section("C. 유량 — TPS 예산이 앱키별로 갈리는가")
    import api

    sess = api.ThrottledSession()
    context.trade_context.use_auto_account = False
    manual_key = sess._real_bucket_key()
    context.trade_context.use_auto_account = True
    auto_key = sess._real_bucket_key()
    context.trade_context.use_auto_account = False

    if key_split:
        check(manual_key != auto_key, "수동/자동이 서로 다른 TPS 버킷을 쓴다",
              f"{manual_key} / {auto_key}")
        b_m = sess._real_buckets[manual_key]
        b_a = sess._real_buckets[auto_key]
        b_m.adaptive_limit = b_a.adaptive_limit = 18.0
        sess._tps_on_rate_limit_real(url="audit", tr_id="AUDIT")
        check(b_a.adaptive_limit == 18.0,
              "한쪽 키의 EGW00201이 다른 키 예산을 깎지 않는다",
              f"수동 {b_m.adaptive_limit:.2f} / 자동 {b_a.adaptive_limit:.2f}")
        console.print(f"  [dim]이론 총량: {config.REAL_TX_PER_SECOND:.0f} TPS × 2키 "
                      f"= {config.REAL_TX_PER_SECOND * 2:.0f} TPS[/dim]")
    else:
        check(manual_key == auto_key, "앱키가 같으므로 단일 버킷 (합산 초과 방지)",
              f"{manual_key}")


def audit_live():
    section("D. 실계좌 — 두 계좌를 읽기 전용으로 대조 (주문 없음)")
    import api
    import utils

    s = config.session
    rows = []
    holdings_by_acc = {}

    for label, cano, acnt in (("수동", s.cano, s.acnt_prdt_cd),
                              ("자동매매", s.auto_cano, s.auto_acnt_prdt_cd)):
        if not cano:
            continue
        try:
            with utils.AccountContext(cano):
                holdings, summary = api.get_domestic_balance(cano, acnt)
                dep = api.get_deposit_balance(cano, acnt)
        except Exception as e:
            console.print(f"  [bold red]FAIL[/bold red] {label} 계좌 조회 실패: {type(e).__name__}: {e}")
            _FAIL.append(f"{label} 계좌 조회")
            continue

        holdings = holdings or []
        holdings_by_acc[label] = {h.get('pdno'): h.get('prdt_name') for h in holdings}
        evlu = api.safe_int((summary or [{}])[0].get('tot_evlu_amt', 0)) if summary else 0
        # get_deposit_balance는 KIS 원문이 아니라 평탄화된 dict를 돌려준다
        #  ({deposit, d2_deposit, order_possible, ...}). output을 뒤지면 항상 0이 나온다.
        cash = api.safe_int((dep or {}).get('deposit', 0))
        ordable = api.safe_int((dep or {}).get('order_possible', 0))
        rows.append((label, f"{cano}-{acnt}", len(holdings), evlu, cash, ordable))

    if rows:
        t = Table(box=None)
        for c in ("구분", "계좌", "보유종목", "총평가금액", "예수금", "주문가능"):
            t.add_column(c)
        for label, acc, n, evlu, cash, ordable in rows:
            t.add_row(label, acc, str(n), f"{evlu:,}", f"{cash:,}", f"{ordable:,}")
        console.print(t)

    check(len(rows) == 2, "두 계좌 모두 조회 성공")

    # 같은 종목을 양쪽에 들고 있으면, 라우팅이 틀렸을 때 수동 보유분이 대신 팔린다
    if len(holdings_by_acc) == 2:
        a, b = holdings_by_acc.values()
        dup = set(a) & set(b)
        check(not dup, "양 계좌에 중복 보유 종목 없음",
              f"중복: {', '.join(f'{c}({a[c]})' for c in dup)}" if dup else "")
        if dup:
            console.print("  [yellow]중복 보유는 라우팅 결함이 있을 때 수동 보유분이 "
                          "대신 매도되는 경로다. 분리 운용 중에는 피하는 편이 안전하다.[/yellow]")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--live", action="store_true", help="두 계좌 잔고를 읽기 전용으로 조회")
    args = ap.parse_args()

    config.session.initialize('2')
    config.session.load_stock_config()

    acc_split, key_split = audit_config()
    audit_routing(acc_split)
    audit_tps(key_split)
    if args.live:
        audit_live()
    else:
        console.print("\n[dim]D. 실계좌 대조는 건너뜀 (--live 로 실행)[/dim]")

    console.print()
    if _FAIL:
        console.print(f"[bold red]실패 {len(_FAIL)}건[/bold red]: " + " / ".join(_FAIL))
        sys.exit(1)
    console.print("[bold green]전 항목 통과 — 계좌 분리가 라우팅·유량 양쪽에서 성립한다[/bold green]")


if __name__ == "__main__":
    main()
