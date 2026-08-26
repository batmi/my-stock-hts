"""KIS 실효 TPS 한도 실측 — 고정 속도 스윕(rate sweep).

[왜 필요한가] EGW00201(초당 거래건수 초과) 진단에서 원인 후보를 하나씩 지웠다.
  · 측정 아티팩트  → 기각 (전송시각 == 수신시각, RTT 0.01~0.02s)
  · 게이트 아래 누출 → 기각 (게이트 미경유 재전송 0건)
  · 중복 프로세스   → 기각 (앱키 잠금)
  · 엔드포인트 하위 한도 → 기각 (국내 FHKST01010100·해외 HHDFS00000300 모두 거부)
  · '자기 캡(8.5) 포화 때만 거부' → 기각 (주말·유휴 상태에서 5건/초에도 거부, 2026-08-09)
남은 후보는 '실효 한도가 명목(20 TPS)보다 훨씬 낮다' 또는 '이 기기 밖에서 같은 앱키를
쓰는 무언가가 있다' 둘인데, 운영 로그로는 어느 쪽인지 알 수 없다. 앱의 전송 속도는
그때그때 부하가 정하지, 우리가 정하는 값이 아니기 때문이다. 한도를 알려면
**낮은 속도에서도 거부되는지**를 봐야 하고, 그러려면 속도를 통제해서 밀어야 한다.

[무엇을 하나] 1→20 TPS를 한 단계씩 올리며 각 속도로 N초씩 보내고 거부율을 기록한다.
 거부율이 0에서 벗어나는 지점이 이 계정의 실효 한도다. ThrottledSession(게이트)을
 우회해 직접 보내므로 속도가 정확히 통제된다.

[주의]
 · 실계좌 유량을 실제로 소비한다. **본 프로그램(run.sh)을 내린 상태에서** 돌려야 한다.
   앱이 같이 돌면 그쪽 트래픽이 섞여 측정이 무의미해진다.
 · 조회 TR만 쓴다(현재가). 주문·계좌 API는 건드리지 않는다.

사용:
  python3 tools/probe_kis_tps.py --mode 2 --seconds 12
  python3 tools/probe_kis_tps.py --mode 2 --market overseas --rates 3,5,7,9,11
"""
import argparse
import concurrent.futures
import math
import os
import sys
import threading
import time
from collections import Counter

from rich.console import Console
from rich.table import Table
from rich import box

sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import config
import api
from core import constants
from core import utils

console = Console()


def _build_target(market):
    """(url, headers, params, tr_id) — 가장 가벼운 조회 TR을 쓴다."""
    if market == "overseas":
        tr_id = constants.TR_ID_CONFIG["overseas"]["quotations"]["price"]["real"]
        url = f"{config.session.url_base}/{constants.API_URLS['OVERSEAS']['QUOTATIONS']['PRICE']}"
        params = {"AUTH": "", "EXCD": "NAS", "SYMB": "AAPL"}
    else:
        tr_id = constants.TR_ID_CONFIG["domestic"]["quotations"]["price"]["real"]
        url = f"{config.session.url_base}/{constants.API_URLS['DOMESTIC']['QUOTATIONS']['PRICE']}"
        params = {"fid_cond_mrkt_div_code": "J", "fid_input_iscd": "005930"}
    return url, utils.get_common_headers(tr_id), params, tr_id


def _classify(res):
    """응답 한 건을 성공/거부/기타로 가른다. EGW00201은 HTTP 500으로도 온다."""
    try:
        if res.status_code != 200:
            body = res.text[:300]
            if 'EGW00201' in body or 'EGW00215' in body:
                return "reject", "EGW00201"
            return "other", f"HTTP_{res.status_code}"
        data = res.json()
        if data.get('rt_cd') == '0':
            return "ok", None
        msg = data.get('msg_cd') or "UNKNOWN"
        if msg in ('EGW00201', 'EGW00215'):
            return "reject", msg
        return "other", msg
    except Exception as e:
        return "other", type(e).__name__


def _run_one_rate(session, target, rate, seconds):
    """정확히 rate건/초로 seconds초 동안 보낸다.

    전송 시각을 t0 + i/rate 로 미리 정해 두고 스레드풀에 던진다 — 응답이 늦어도
    다음 전송이 밀리지 않아야 '보낸 속도'가 우리가 정한 값 그대로가 된다.
    """
    url, headers, params, _tr = target
    total = max(1, int(rate * seconds))
    counts = Counter()
    details = Counter()
    lock = threading.Lock()
    first_reject_at = [None]
    t0 = time.time()

    def send(i):
        due = t0 + i / float(rate)
        delay = due - time.time()
        if delay > 0:
            time.sleep(delay)
        sent_at = time.time()
        try:
            res = session.get(url, headers=headers, params=params, timeout=8)
            kind, detail = _classify(res)
        except Exception as e:
            kind, detail = "other", type(e).__name__
        with lock:
            counts[kind] += 1
            if detail:
                details[detail] += 1
            if kind == "reject" and first_reject_at[0] is None:
                first_reject_at[0] = sent_at - t0
        return kind

    # 응답 지연이 스케줄을 막지 않도록 넉넉히. (RTT 0.5s 가정 + 여유)
    workers = min(64, max(8, int(math.ceil(rate * 0.5)) + 4))
    with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as ex:
        list(ex.map(send, range(total)))

    elapsed = max(1e-6, time.time() - t0)
    sent = sum(counts.values())
    return {
        "rate": rate,
        "sent": sent,
        "actual_tps": sent / elapsed,
        "ok": counts["ok"],
        "reject": counts["reject"],
        "other": counts["other"],
        "reject_pct": 100.0 * counts["reject"] / sent if sent else 0.0,
        "first_reject_at": first_reject_at[0],
        "details": details,
    }


def _render(rows, market):
    t = Table(title=f"\nKIS 실효 TPS 실측 ({'해외' if market == 'overseas' else '국내'} 현재가)",
              box=box.HORIZONTALS, header_style="dim", border_style="dim")
    t.add_column("목표 TPS", justify="right")
    t.add_column("실제 TPS", justify="right")
    t.add_column("전송", justify="right")
    t.add_column("성공", justify="right")
    t.add_column("EGW00201", justify="right")
    t.add_column("기타오류", justify="right")
    t.add_column("거부율", justify="right")
    t.add_column("첫 거부", justify="right")
    t.add_column("오류 상세", justify="left")
    for r in rows:
        color = "[red]" if r["reject_pct"] > 0 else "[green]"
        first = f"{r['first_reject_at']:.1f}s" if r["first_reject_at"] is not None else "-"
        detail = ", ".join(f"{k}×{v}" for k, v in r["details"].most_common(3)
                           if k not in ('EGW00201', 'EGW00215')) or "-"
        t.add_row(f"{r['rate']:g}", f"{r['actual_tps']:.1f}", f"{r['sent']:,}",
                  f"{r['ok']:,}", f"{color}{r['reject']:,}[/]",
                  f"{r['other']:,}", f"{color}{r['reject_pct']:.1f}%[/]", first, detail)
    console.print(t)


def main():
    ap = argparse.ArgumentParser(description="KIS 실효 TPS 한도 실측(고정 속도 스윕)")
    ap.add_argument('--mode', default='2', choices=['1', '2', '4'],
                    help='1: 모의, 2: 실전(REAL_APP_KEY, 기본), 4: 가상투자(VIRT_APP_KEY·실전 서버)')
    ap.add_argument('--force-token', action='store_true',
                    help='토큰을 강제 재발급한다(캐시된 토큰이 서버측에서 무효화된 경우)')
    ap.add_argument('--market', default='domestic', choices=['domestic', 'overseas', 'both'])
    ap.add_argument('--rates', default='1,2,3,4,5,6,8,10,13,16,20', help='시험할 TPS 목록(쉼표)')
    ap.add_argument('--seconds', type=int, default=12, help='속도당 지속 시간(초)')
    ap.add_argument('--gap', type=float, default=5.0, help='속도 사이 휴지(초) — 서버 카운터를 비운다')
    ap.add_argument('--yes', action='store_true', help='확인 없이 실행')
    args = ap.parse_args()

    rates = [float(x) for x in args.rates.split(',') if x.strip()]
    markets = ['domestic', 'overseas'] if args.market == 'both' else [args.market]
    est = len(markets) * (sum(args.seconds for _ in rates) + args.gap * (len(rates) - 1))

    console.print("[bold cyan]=== KIS 실효 TPS 한도 실측 ===[/bold cyan]")
    console.print("[dim]게이트를 우회해 정해진 속도로만 보낸다. 거부율이 0에서 벗어나는 지점이 실효 한도다.[/dim]")
    console.print(f"[yellow]※ 본 프로그램(run.sh)을 내린 상태에서 돌려야 한다 — 앱 트래픽이 섞이면 측정이 무의미하다.[/yellow]")
    console.print(f"[dim]예상 소요 약 {est/60:.1f}분, 총 전송 약 "
                  f"{int(sum(r * args.seconds for r in rates) * len(markets)):,}건 (조회 TR만 사용)[/dim]\n")
    if not args.yes:
        try:
            if input("실행할까요? [y/N] ").strip().lower() not in ('y', 'yes'):
                return
        except EOFError:
            return

    config.session.initialize(mode=args.mode)
    # 모드 1(가상투자)은 VIRT_APP_KEY 를 real_* 슬롯에 넣고 실전 서버를 쓴다(session.py 참조).
    #  즉 어느 모드든 토큰 종류는 'REAL'이며, 측정 대상 앱키만 달라진다.
    token = api.get_real_access_token(force_refresh=args.force_token)
    if not token:
        console.print("[red]토큰 발급 실패. 환경변수(~/.htsrc)를 확인하세요.[/red]")
        return
    console.print(f"[dim]앱키 …{str(config.session.real_app_key or config.session.app_key)[-6:]} / "
                  f"서버 {config.session.url_base}[/dim]")

    for market in markets:
        target = _build_target(market)
        console.print(f"[dim]TR={target[3]} · {target[0].split('/')[-1]}[/dim]")

        # [사전 점검] 1건만 보내 본다. 여기서 실패하면 스윕을 돌려 봐야 전 구간 '기타오류'로
        #  채워질 뿐이고, 거부율 0%가 '한도가 높다'로 오독된다(2026-08-09 라즈베리파이 실측에서
        #  실제로 700건 전부 실패했는데 표에는 거부율 0%만 보였다).
        with api.requests.Session() as _s:
            try:
                _res = _s.get(target[0], headers=target[1], params=target[2], timeout=8)
                _kind, _detail = _classify(_res)
                _body = _res.text[:200]
            except Exception as e:
                _kind, _detail, _body = "other", type(e).__name__, str(e)[:200]
        if _kind != "ok":
            console.print(f"[bold red]사전 점검 실패 — 측정을 중단합니다.[/bold red] ({_detail})")
            console.print(f"[dim]{_body}[/dim]")
            if str(_detail).startswith('EGW001'):
                console.print("[yellow]토큰 문제로 보입니다. --force-token 을 붙여 재발급 후 다시 시도하세요.[/yellow]")
                console.print("[dim]  같은 앱키로 다른 기기에서 토큰을 새로 받으면 이전 토큰이 서버에서 "
                              "무효화됩니다 — 로컬 캐시는 유효해 보여도 서버가 거부합니다.[/dim]")
            return
        console.print("[dim]사전 점검 통과(1건 성공). 스윕을 시작합니다.[/dim]")

        rows = []
        with api.requests.Session() as session:
            # 어댑터 재시도 금지 — 재전송이 섞이면 '보낸 속도'가 우리가 정한 값이 아니게 된다.
            session.mount('https://', api.requests.adapters.HTTPAdapter(
                max_retries=0, pool_connections=64, pool_maxsize=64))
            for i, rate in enumerate(rates):
                console.print(f"  · {rate:g} TPS × {args.seconds}s …", end="")
                r = _run_one_rate(session, target, rate, args.seconds)
                rows.append(r)
                console.print(f" 거부 {r['reject']}건 ({r['reject_pct']:.1f}%)")
                if i < len(rates) - 1 and args.gap > 0:
                    time.sleep(args.gap)
        _render(rows, market)

        if any(r['ok'] == 0 for r in rows):
            console.print("\n[bold red]성공 0건인 구간이 있습니다 — 이 측정은 무효입니다.[/bold red] "
                          "위 '오류 상세'를 먼저 해결하세요(거부율 0%는 한도와 무관합니다).")
            continue

        clean = [r['rate'] for r in rows if r['reject'] == 0]
        dirty = [r['rate'] for r in rows if r['reject'] > 0]
        if clean and dirty:
            console.print(f"\n[bold]실효 한도: {max(clean):g} ~ {min(dirty):g} TPS 사이[/bold] "
                          f"(무거부 최대 {max(clean):g} · 최초 거부 {min(dirty):g})")
            console.print("[dim]  → config.REAL_TX_PER_SECOND 를 이 값 기준으로 잡으면 "
                          "AIMD가 천장이 아니라 실제 한도 아래에서 수렴한다.[/dim]")
        elif not dirty:
            console.print("\n[bold green]시험한 전 구간에서 거부 없음[/bold green] — "
                          "한도는 최고 시험 속도보다 높다. --rates 를 올려 다시 재라.")
        else:
            console.print("\n[bold red]최저 속도에서도 거부됨[/bold red] — 한도가 아니라 다른 원인이다"
                          "(계정 상태·앱키 제재·서버측 이슈). KIS 고객센터 문의 대상.")


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        console.print("\n[yellow]중단됨[/yellow]")
