# tools/journal_sync_e2e.py
"""매매일지 웹서버 연동 실전송 검증 도구 (End-to-End).

모의투자 계좌 정보로 가상의 체결을 만들어 **실제 운영 코드 경로 그대로**
원격 매매일지 서버까지 보내고, 서버가 실제로 무엇을 받아 저장했는지 되읽어
필드 단위로 대조합니다.

    insert_trade → journal_outbox 적재 → flush_once() 배치 전송
        → GET /api/v1/trades 로 되읽기 → 보낸 값과 대조

단위 테스트(tests/test_journal_sync.py)는 HTTP 를 모킹하므로 "서버가 정말
받았는가"는 증명하지 못합니다. 이 도구는 그 마지막 한 칸을 확인합니다.

안전장치
--------
* **임시 DB** 를 쓴다 — 운영 DB(db/trade_history.db)에 가짜 체결이 남지 않는다.
* 전송 데이터는 항상 ``isSimulated=True`` — 서버의 실거래 통계·포트폴리오에 섞이지 않는다.
* ``source`` 를 별도 값(기본 ``my-stock-hts-e2e``)으로 보낸다 — 실제 봇 기록과
  구분되고, 봇의 last-sync 동기화 지점도 건드리지 않는다.
* ``--cleanup`` 으로 검증 직후 서버에서 지울 수 있다.

사용법
------
    python tools/journal_sync_e2e.py                # 전송 후 수신 확인 (서버에 남김)
    python tools/journal_sync_e2e.py --cleanup      # 확인 후 서버에서 삭제
    python tools/journal_sync_e2e.py --count 6      # 체결 건수 지정
    python tools/journal_sync_e2e.py --no-ping      # 봇 상태 Ping 검증 생략

사전 조건 (~/.htsrc)
    export SIM_ACC_NUM="50012345-01"
    export JOURNAL_API_URL="https://memo.example.com"
    export JOURNAL_API_KEY="skm_..."
"""
import argparse
import atexit
import json
import os
import sys
import tempfile
from datetime import datetime, timedelta

# 프로젝트 루트 경로 추가
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import config  # noqa: E402
from rich.console import Console  # noqa: E402
from rich.table import Table  # noqa: E402

console = Console()

DEFAULT_SOURCE = "my-stock-hts-e2e"

# 검증용 가상 종목. 마지막 항목은 해외 종목 — 거래소 코드로 현지 거래일이
# 제대로 귀속되는지(한국 날짜로 밀리지 않는지)까지 함께 확인한다.
SAMPLE_STOCKS = [
    {"code": "005930", "name": "삼성전자", "price": 71000, "qty": 10},
    {"code": "000660", "name": "SK하이닉스", "price": 183500, "qty": 3},
    {"code": "035720", "name": "카카오", "price": 41250, "qty": 20},
    {"code": "AAPL", "name": "Apple Inc.", "price": 231.75, "qty": 2},
]


def _fail(message):
    console.print(f"[bold red]✗ {message}[/bold red]")
    return False


def _check_prerequisites():
    """환경변수·계좌 설정을 먼저 확인하고 무엇이 빠졌는지 알려준다."""
    console.print("[bold cyan]═══ 1. 사전 조건 확인 ═══[/bold cyan]")

    missing = [n for n in ("JOURNAL_API_URL", "JOURNAL_API_KEY")
               if not getattr(config, n, "")]
    if missing:
        _fail(f"환경변수 미설정: {', '.join(missing)}")
        console.print("[dim]  ~/.htsrc 에 export 로 추가한 뒤 `source ~/.htsrc` 하고 다시 실행하세요.[/dim]")
        return False

    config.session.initialize(mode="1")  # 모의투자 모드 (환경변수만 읽고 네트워크는 타지 않음)
    if not config.session.cano:
        _fail("모의투자 계좌번호(SIM_ACC_NUM)가 설정되지 않았습니다.")
        console.print('[dim]  예) export SIM_ACC_NUM="50012345-01"[/dim]')
        return False

    console.print(f"  서버 주소   : [green]{config.JOURNAL_API_URL}[/green]")
    console.print(f"  API 키      : [green]{config.JOURNAL_API_KEY[:12]}…[/green]")
    console.print(f"  모의투자 계좌: [green]{config.session.cano}-{config.session.acnt_prdt_cd}[/green]"
                  f" (is_simulation={config.session.is_simulation})")
    return True


def _prepare_sandbox(source, keep_db):
    """운영 DB·설정을 건드리지 않도록 임시 환경을 구성한다."""
    console.print("\n[bold cyan]═══ 2. 격리 환경 준비 ═══[/bold cyan]")

    fd, temp_db = tempfile.mkstemp(prefix="journal_e2e_", suffix=".db")
    os.close(fd)
    config.DB_FILE_PATH = temp_db

    # 메모리상에서만 켠다 — dynamic_config.json 에 저장하지 않으므로 운영 설정은 그대로다.
    config.settings.JOURNAL_SYNC_USE = True
    config.JOURNAL_SYNC_SIMULATION = True   # 모의투자 체결도 전송 대상에 포함
    config.JOURNAL_SOURCE = source
    config.settings.SCREEN_DEBUG_LEVEL = "OFF"   # DB 마이그레이션 로그로 결과가 묻히지 않게

    # 해외 종목의 거래소 코드는 매매 유니버스(stock.json)에서 찾는다.
    # 이걸 채워야 '미국 체결의 현지 거래일 귀속'까지 실제 경로로 검증된다.
    try:
        with open(os.path.join(config.JSON_DIR, "stock.json"), encoding="utf-8") as f:
            config.session.stock_data = json.load(f)
    except Exception as e:
        console.print(f"  [yellow]※ stock.json 로드 실패({e}) — 해외 거래소 코드 검증은 생략됩니다.[/yellow]")

    # 임시 DB 정리는 atexit 로 미룬다. finally 에서 지우면 db_manager 의 종료 훅(VACUUM)이
    # 이미 지워진 파일을 열어 에러를 뿜는다. atexit 는 LIFO 라 나중에 등록된 db_manager 의
    # 훅이 먼저 돌고, 그 다음 이 정리가 실행된다.
    def _remove_temp_db():
        if keep_db:
            console.print(f"[dim]임시 DB 유지: {temp_db}[/dim]")
            return
        for suffix in ("", "-wal", "-shm"):
            try:
                os.unlink(temp_db + suffix)
            except OSError:
                pass

    atexit.register(_remove_temp_db)

    console.print(f"  임시 DB     : [dim]{temp_db}[/dim]")
    console.print(f"  source      : [green]{source}[/green] [dim](실제 봇 기록과 구분)[/dim]")
    console.print("  [dim]운영 DB와 dynamic_config.json 은 건드리지 않습니다.[/dim]")
    return temp_db


def _check_server_alive():
    console.print("\n[bold cyan]═══ 3. 서버 연결 확인 ═══[/bold cyan]")
    import requests
    from modules import journal_sync

    try:
        res = requests.get(f"{journal_sync._base_url()}/api/v1/health", timeout=8)
    except Exception as e:
        return _fail(f"서버에 연결할 수 없습니다: {e}")

    if res.status_code != 200:
        return _fail(f"health 응답이 비정상입니다 ({res.status_code}): {res.text[:200]}")

    body = res.json()
    console.print(f"  상태        : [green]{body.get('status')}[/green]")
    console.print(f"  API 버전    : {body.get('apiVersion')}")
    console.print(f"  서버 시각   : {body.get('serverTime')}")

    if journal_sync._tokens.get() is None:
        return _fail("Access Token 발급에 실패했습니다. API 키가 유효한지 확인하세요.")
    console.print("  토큰 발급   : [green]성공[/green]")
    return True


def _generate_fills(count):
    """모의투자 계좌로 가상 체결을 만들어 대기열에 적재한다.

    운영과 동일하게 db_manager.insert_trade() 를 통과시킨다 — 큐 적재 훅까지
    실제 경로를 그대로 타야 '연동이 동작한다'를 증명할 수 있다.
    """
    console.print("\n[bold cyan]═══ 4. 가상 체결 생성 (모의투자 계좌) ═══[/bold cyan]")
    from modules import db_manager

    run_tag = datetime.now().strftime("%H%M%S")
    base_time = datetime.now().replace(second=0, microsecond=0) - timedelta(minutes=count * 5)
    sent = []

    # 같은 종목을 매수 → 매도 짝으로 낸다. 매도만 따로 보내면 서버의 보유 수량 검증에
    # 걸려 needsReview 가 붙는데, 그건 이 도구가 확인하려는 '정상 흐름'이 아니다.
    for i in range(count):
        stock = SAMPLE_STOCKS[(i // 2) % len(SAMPLE_STOCKS)]
        is_sell = i % 2 == 1                      # 매수/매도를 번갈아 — 실현손익 경로도 태운다
        executed_at = base_time + timedelta(minutes=i * 5)
        odno = f"E2E{run_tag}{i:03d}"             # 실행마다 달라 이전 검증분과 섞이지 않는다

        if is_sell:
            price = round(stock["price"] * 1.05, 2)
            profit_amt = int((price - stock["price"]) * stock["qty"])
            payload_extra = dict(profit_amt=profit_amt, profit_rate=5.0, score=5.8,
                                 stop_loss_rate=-7.0, reason="[E2E 검증] 트레일링 스탑 청산")
            type_str = "매도(AUTO)"
        else:
            price = stock["price"]
            payload_extra = dict(reason="[E2E 검증] 추세 진입", score=7.2, stop_loss_rate=-7.0)
            type_str = "매수(AUTO)"

        db_manager.db.insert_trade(
            type_str, stock["code"], stock["name"], stock["qty"], price, odno,
            order_status="체결", custom_time=executed_at.strftime("%Y-%m-%d %H:%M:%S"),
            **payload_extra)

        sent.append({
            "odno": odno, "code": stock["code"], "name": stock["name"],
            "side": "SELL" if is_sell else "BUY", "price": price, "qty": stock["qty"],
            "executed_at": executed_at,
        })
        console.print(f"  [{i + 1}/{count}] {'매도' if is_sell else '매수'} "
                      f"{stock['name']}({stock['code']}) {stock['qty']}주 @{price:,g} "
                      f"[dim]odno={odno}[/dim]")

    # 멱등키는 다시 계산하지 말고 대기열에 실제로 적재된 값을 읽는다 —
    # 이래야 '보낸 값'과 '대조 기준'이 어긋날 여지가 없다.
    # (exec_id 형식: {env}:{계좌}:{체결일}:{주문번호}:{상태})
    conn = db_manager.db._get_conn()
    by_odno = {}
    for row in conn.execute("SELECT exec_id FROM journal_outbox"):
        parts = row['exec_id'].split(':')
        if len(parts) >= 4:
            by_odno[parts[3]] = row['exec_id']
    for item in sent:
        item['exec_id'] = by_odno.get(item['odno'])

    from modules import journal_sync
    queued = journal_sync.pending_count()
    console.print(f"  전송 대기열 : [green]{queued}건[/green] 적재됨")
    if queued != count:
        console.print(f"  [yellow]※ 생성 {count}건 중 {queued}건만 적재되었습니다. "
                      f"(전송 대상 필터 확인 필요)[/yellow]")
    return sent


def _flush():
    console.print("\n[bold cyan]═══ 5. 서버로 전송 ═══[/bold cyan]")
    from modules import journal_sync

    ok, fail = journal_sync.flush_once()
    remaining = journal_sync.pending_count()

    color = "green" if fail == 0 and remaining == 0 else "yellow"
    console.print(f"  전송 성공   : [{color}]{ok}건[/{color}]")
    console.print(f"  전송 실패   : {fail}건")
    console.print(f"  대기 잔량   : {remaining}건")

    if fail or remaining:
        console.print("  [yellow]※ 실패 사유는 journal_outbox.last_error 및 "
                      "logs/mystock.log 의 [Journal] 항목을 확인하세요.[/yellow]")
    return ok, fail


def _fetch_from_server(source, sent):
    """서버가 실제로 저장한 내용을 되읽는다.

    이 실행분과 이전 실행의 잔여분을 구분해서 보여준다. 잔여분이 쌓이면 같은 종목의
    보유 수량이 어긋나 이번 실행의 매도가 오버셀로 잡히므로, 그냥 넘어가면 안 된다.
    """
    console.print("\n[bold cyan]═══ 6. 서버 수신 내역 되읽기 ═══[/bold cyan]")
    from modules import journal_sync

    res = journal_sync._request(
        "GET", "/api/v1/trades",
        params={"source": source, "isSimulated": "true", "limit": 500})

    if res is None:
        _fail("조회 요청이 실패했습니다.")
        return None, None
    if res.status_code != 200:
        _fail(f"조회 응답이 비정상입니다 ({res.status_code}): {res.text[:200]}")
        return None, None

    all_trades = res.json().get("trades", [])
    run_ids = {item["exec_id"] for item in sent if item.get("exec_id")}
    this_run = [t for t in all_trades if t.get("brokerExecutionId") in run_ids]
    leftovers = [t for t in all_trades if t.get("brokerExecutionId") not in run_ids]

    console.print(f"  이번 실행분 : [green]{len(this_run)}건[/green] / 보낸 {len(sent)}건")
    if leftovers:
        console.print(f"  이전 잔여분 : [yellow]{len(leftovers)}건[/yellow] "
                      f"[dim](--cleanup 없이 반복 실행하면 쌓입니다)[/dim]")
        console.print("  [dim]  잔여분 때문에 같은 종목 보유 수량이 어긋나면 "
                      "이번 매도가 '확인필요'로 잡힐 수 있습니다.[/dim]")
    return this_run, all_trades


def _verify(sent, trades):
    """보낸 값과 서버 저장 값을 건별·필드별로 대조한다."""
    console.print("\n[bold cyan]═══ 7. 송수신 대조 ═══[/bold cyan]")

    by_exec_id = {t.get("brokerExecutionId"): t for t in trades}

    table = Table(show_header=True, header_style="bold", show_lines=False, box=None,
                  pad_edge=False, padding=(0, 1))
    table.add_column("종목", overflow="fold")
    table.add_column("구분", justify="center")
    table.add_column("보낸 값", justify="right")
    table.add_column("서버 저장 값", justify="right", overflow="fold")
    table.add_column("거래일", justify="center", no_wrap=True)
    table.add_column("결과", justify="center")

    all_ok = True
    for item in sent:
        record = by_exec_id.get(item.get("exec_id"))

        sent_repr = f"{item['qty']:g}주 @{item['price']:,g}"
        if record is None:
            all_ok = False
            table.add_row(f"{item['name']}({item['code']})", item["side"],
                          sent_repr, "[red]수신 안 됨[/red]", "-", "[red]✗[/red]")
            continue

        mismatches = []
        if record.get("symbol") != item["code"]:
            mismatches.append(f"symbol={record.get('symbol')}")
        if record.get("side") != item["side"]:
            mismatches.append(f"side={record.get('side')}")
        if abs(float(record.get("price") or 0) - item["price"]) > 1e-6:
            mismatches.append(f"price={record.get('price')}")
        if abs(float(record.get("volume") or 0) - item["qty"]) > 1e-6:
            mismatches.append(f"volume={record.get('volume')}")
        if record.get("isSimulated") is not True:
            mismatches.append("isSimulated=False")

        got_repr = f"{float(record.get('volume') or 0):g}주 @{float(record.get('price') or 0):,g}"
        if mismatches:
            all_ok = False
            table.add_row(f"{item['name']}({item['code']})", record.get("side", "-"),
                          sent_repr, f"[red]{', '.join(mismatches)}[/red]",
                          record.get("tradeDate") or "-", "[red]✗[/red]")
        else:
            table.add_row(f"{item['name']}({item['code']})", record.get("side", "-"),
                          sent_repr, got_repr, record.get("tradeDate") or "-",
                          "[green]✓[/green]")

    console.print(table)

    # 부가 필드(손익·전략점수·거래소·메모)가 실제로 넘어갔는지 매도 1건을 상세히 보여준다.
    # 매도라야 실현손익까지 채워져 있어 전 필드를 한눈에 확인할 수 있다.
    sample = next((t for t in trades if t.get("side") == "SELL"),
                  trades[0] if trades else None)
    if sample:
        console.print("\n  [bold]서버 저장 상세 (매도 1건)[/bold]")
        for label, key in (("종목명", "name"), ("체결시각(UTC)", "executedAt"),
                           ("거래일", "tradeDate"), ("거래소", "exchange"),
                           ("통화", "currency"), ("실현손익", "realizedPnl"),
                           ("실현손익률", "realizedPnlRate"), ("전략점수", "strategyScore"),
                           ("손절률", "stopLossRate"), ("주문출처", "orderOrigin"),
                           ("계좌", "subAccount"), ("메모", "memo"),
                           ("모의투자", "isSimulated"), ("확인필요", "needsReview")):
            value = sample.get(key)
            style = "yellow" if (value in (None, "") or (key == "needsReview" and value)) else ""
            shown = f"[{style}]{value}[/{style}]" if style else str(value)
            console.print(f"    {label:<14}: {shown}")

    # 해외 종목은 거래소 코드가 있어야 서버가 현지 거래일을 계산한다.
    overseas = [t for t in trades if (t.get("currency") or "KRW") != "KRW"]
    if overseas:
        resolved = [t for t in overseas if t.get("exchange")]
        if resolved:
            console.print(f"\n  [green]✓ 해외 거래소 귀속: "
                          f"{resolved[0]['symbol']} → {resolved[0]['exchange']} "
                          f"(거래일 {resolved[0].get('tradeDate')})[/green]")
        else:
            console.print("\n  [yellow]※ 해외 종목의 거래소 코드가 비어 있습니다 — "
                          "stock.json 유니버스에 없는 종목이면 거래일이 KST 기준으로 잡힙니다.[/yellow]")

    flagged = [t for t in trades if t.get("needsReview")]
    if flagged:
        console.print(f"  [yellow]※ 확인필요(needsReview) 표시 {len(flagged)}건 — "
                      f"서버 보유수량 검증에 걸렸지만 유실 없이 저장된 상태입니다.[/yellow]")

    return all_ok


def _check_ping():
    console.print("\n[bold cyan]═══ 8. 봇 상태 Ping ═══[/bold cyan]")
    from modules import journal_sync

    if journal_sync.ping("running", message="[E2E 검증] 연동 확인 중"):
        console.print("  [green]✓ Ping 전송 성공 — 웹 대시보드 표시등이 '정상 가동중'으로 바뀝니다.[/green]")
        return True
    console.print("  [red]✗ Ping 전송 실패[/red]")
    return False


def _cleanup_server(trades):
    console.print("\n[bold cyan]═══ 9. 서버 검증 데이터 삭제 ═══[/bold cyan]")
    from modules import journal_sync

    deleted = failed = 0
    for record in trades:
        res = journal_sync._request("DELETE", f"/api/v1/trades/{record['id']}")
        if res is not None and res.status_code == 204:
            deleted += 1
        else:
            failed += 1
    console.print(f"  삭제 완료   : [green]{deleted}건[/green] / 실패 {failed}건")
    if failed:
        console.print("  [yellow]※ 남은 기록은 웹 화면에서 직접 삭제하세요.[/yellow]")


def main():
    parser = argparse.ArgumentParser(
        description="매매일지 웹서버 연동 실전송 검증 (모의투자 계좌 기반)")
    parser.add_argument("--count", type=int, default=4, help="생성할 가상 체결 건수 (기본 4)")
    parser.add_argument("--cleanup", action="store_true",
                        help="검증 후 서버에 저장된 검증 데이터를 삭제")
    parser.add_argument("--source", default=DEFAULT_SOURCE,
                        help=f"서버에 기록할 source 값 (기본 {DEFAULT_SOURCE})")
    parser.add_argument("--no-ping", action="store_true", help="봇 상태 Ping 검증 생략")
    parser.add_argument("--keep-db", action="store_true", help="임시 DB 를 지우지 않음 (디버그용)")
    args = parser.parse_args()

    console.print("[bold]매매일지 웹서버 연동 실전송 검증 (E2E)[/bold]")
    console.print("[dim]모의투자 계좌 정보로 가상 체결을 만들어 실제 서버까지 보내고 되읽어 대조합니다.[/dim]\n")

    if not _check_prerequisites():
        return 1

    _prepare_sandbox(args.source, args.keep_db)

    if not _check_server_alive():
        return 1

    sent = _generate_fills(args.count)
    ok, _fail_count = _flush()
    if ok == 0:
        _fail("서버로 전송된 기록이 없습니다. 위 로그의 실패 사유를 확인하세요.")
        return 1

    trades, all_trades = _fetch_from_server(args.source, sent)
    if trades is None:
        return 1

    verified = _verify(sent, trades)
    ping_ok = True if args.no_ping else _check_ping()

    if args.cleanup:
        _cleanup_server(all_trades)   # 이전 실행 잔여분까지 함께 정리한다
    else:
        console.print(f"\n[dim]검증 데이터는 서버에 남아 있습니다 (source={args.source}). "
                      f"지우려면 --cleanup 을 붙여 다시 실행하세요.[/dim]")

    console.print()
    if verified and ping_ok:
        console.print("[bold green]✅ 검증 통과 — 웹서버가 전송 내역을 정상 수신했습니다.[/bold green]")
        return 0
    console.print("[bold red]❌ 검증 실패 — 위 대조 결과를 확인하세요.[/bold red]")
    return 1


if __name__ == "__main__":
    sys.exit(main())
