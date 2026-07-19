#!/usr/bin/env python3
"""
토스증권 API 최대 TPS(초당 요청 한도) 실측 도구.

목적: toss_api.py의 클라이언트 리미터(_TOSS_MAX_RPS, 그룹별 _GROUP_RPS)가 서버 실한도와
  맞는지 검증한다. 내부 리미터/재시도를 우회한 '원시 호출'로 요청 속도를 단계적으로
  올리며(램프업) 각 단계의 성공/429 비율과 지연을 측정해 무429 최대 지속 TPS를 찾는다.

안전 설계 (읽기 전용·점진 램프·즉시 중단):
  - 시세 조회 등 읽기 전용 엔드포인트만 사용한다 (주문/계좌 그룹은 프로브 금지).
  - 낮은 속도부터 짧게(기본 4초) 시도하고, 429 비율이 50%를 넘으면 즉시 램프를 멈춘다.
  - 인증 오류(401/403)나 5xx 급증 시 전체 중단. 단계 사이 쿨다운으로 서버 부담 최소화.
  - 총 요청 수는 기본 설정에서 ~360건 수준.

사용법:
  python tools/toss_tps_probe.py                          # MARKET_DATA(/prices) 기본 램프
  python tools/toss_tps_probe.py --group chart            # MARKET_DATA_CHART(/candles) 프로브
  python tools/toss_tps_probe.py --rates 6,10,14 --duration 5
  python tools/toss_tps_probe.py --symbol 005930

검증 대상 그룹(모두 account 헤더 불필요·읽기 전용):
  price  → GET /api/v1/prices        (MARKET_DATA 그룹)
  chart  → GET /api/v1/candles      (MARKET_DATA_CHART 그룹)
  stock  → GET /api/v1/stocks       (STOCK 그룹)
"""
import sys
import os
import time
import argparse
import statistics
import threading
import concurrent.futures

sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import requests
from rich.console import Console
from rich.table import Table
from rich import box

import config

console = Console()

# 그룹별 프로브 엔드포인트 (읽기 전용, account 헤더 불필요)
ENDPOINTS = {
    "price": {"path": "/api/v1/prices", "params": lambda sym: {"symbols": sym}, "group": "MARKET_DATA"},
    "chart": {"path": "/api/v1/candles", "params": lambda sym: {"symbol": sym, "interval": "1d", "count": 2}, "group": "MARKET_DATA_CHART"},
    "stock": {"path": "/api/v1/stocks", "params": lambda sym: {"symbols": sym}, "group": "STOCK"},
    "market_info": {"path": "/api/v1/exchange-rate", "params": lambda sym: {"baseCurrency": "USD", "quoteCurrency": "KRW"}, "group": "MARKET_INFO"},
    "ranking": {"path": "/api/v1/rankings", "params": lambda sym: {"type": "MARKET_TRADING_AMOUNT", "marketCountry": "KR", "duration": "realtime", "count": 5}, "group": "RANKING"},
}
# 주문(ORDER*)·계좌(ACCOUNT/ASSET) 그룹은 안전상 프로브 대상에서 제외한다.

RATE_HEADER_KEYS = ("X-RateLimit-Limit", "X-RateLimit-Remaining", "X-RateLimit-Reset", "Retry-After")


def probe_phase(url, headers, params, rate, duration, abort_flag):
    """지정 속도(rate rps)로 duration초 동안 원시 호출을 보내고 결과를 수집한다."""
    total = int(rate * duration)
    results = []          # (status_code|None, latency_sec)
    seen_headers = {}
    lock = threading.Lock()
    t0 = time.monotonic()

    def one(i):
        # 슬롯 시각까지 대기해 요청 간격을 균등 유지 (버스트가 아닌 '지속 TPS' 측정)
        slot = t0 + i / rate
        delay = slot - time.monotonic()
        if delay > 0:
            time.sleep(delay)
        if abort_flag.is_set():
            return
        st = time.monotonic()
        try:
            res = requests.get(url, headers=headers, params=params, timeout=5)
            lat = time.monotonic() - st
            with lock:
                results.append((res.status_code, lat))
                for k in RATE_HEADER_KEYS:
                    if k in res.headers:
                        seen_headers[k] = res.headers[k]
            if res.status_code in (401, 403):
                abort_flag.set()
        except requests.exceptions.RequestException:
            with lock:
                results.append((None, time.monotonic() - st))

    workers = min(32, max(4, int(rate) * 2))
    with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as ex:
        list(ex.map(one, range(total)))
    return results, seen_headers


def summarize(results):
    ok = sum(1 for s, _ in results if s is not None and 200 <= s < 300)
    r429 = sum(1 for s, _ in results if s == 429)
    err = len(results) - ok - r429
    lats = sorted(l for s, l in results if s is not None and 200 <= s < 300)
    p50 = statistics.median(lats) * 1000 if lats else 0.0
    p95 = lats[int(len(lats) * 0.95) - 1] * 1000 if len(lats) >= 2 else p50
    return ok, r429, err, p50, p95


def main():
    ap = argparse.ArgumentParser(description="토스 API 최대 TPS 실측")
    ap.add_argument("--group", choices=list(ENDPOINTS), default="price")
    ap.add_argument("--symbol", default="005930")
    ap.add_argument("--rates", default="4,6,8,10,12,14,16,20", help="램프 단계(rps, 쉼표 구분)")
    ap.add_argument("--duration", type=float, default=4.0, help="단계별 지속 시간(초)")
    ap.add_argument("--gap", type=float, default=2.0, help="단계 사이 쿨다운(초)")
    ap.add_argument("--full", action="store_true", help="429 다수 발생 후에도 램프 계속")
    args = ap.parse_args()

    config.session.initialize(mode="3")
    import toss_api  # session 초기화 후 임포트 (토큰 경로 의존)

    token = toss_api.get_access_token()
    if not token:
        console.print("[red]토스 액세스 토큰 발급 실패 — TOSS_APP_KEY/SECRET(~/.htsrc)과 허용 IP를 확인하세요.[/red]")
        return 1

    ep = ENDPOINTS[args.group]
    url = f"{config.TOSS_URL}{ep['path']}"
    headers = {"Authorization": f"Bearer {token}"}
    params = ep["params"](args.symbol)

    # 사전 1회 검증 호출 (토큰 유효성·엔드포인트 정상 확인, 실패 시 즉시 중단)
    res = requests.get(url, headers=headers, params=params, timeout=5)
    if res.status_code == 401:
        # 캐시 토큰 만료 가능 → 강제 재발급 1회
        token = toss_api.get_access_token(force_refresh=True)
        headers = {"Authorization": f"Bearer {token}"} if token else headers
        res = requests.get(url, headers=headers, params=params, timeout=5) if token else res
    if res.status_code != 200:
        console.print(f"[red]사전 검증 실패 (HTTP {res.status_code}): {res.text[:200]}[/red]")
        return 1

    console.print(f"[bold]토스 TPS 프로브[/bold] — 그룹 [cyan]{ep['group']}[/cyan] ({ep['path']}), "
                  f"심볼 {args.symbol}, 단계 {args.rates} rps × {args.duration}s")
    console.print(f"[dim]현재 클라이언트 설정: _TOSS_MAX_RPS={toss_api._TOSS_MAX_RPS}, "
                  f"{ep['group']}={toss_api._GROUP_RPS.get(ep['group'])}[/dim]\n")

    table = Table(box=box.HORIZONTALS, header_style="dim")
    for col in ["목표 rps", "요청", "성공", "429", "기타오류", "실효 성공 TPS", "p50(ms)", "p95(ms)"]:
        table.add_column(col, justify="right")

    abort_flag = threading.Event()
    rows = []
    max_clean = None      # 429 없이 통과한 최대 rps
    first_throttle = None  # 429가 처음 나타난 rps
    last_headers = {}

    for rate in [float(r) for r in args.rates.split(",") if r.strip()]:
        results, hdrs = probe_phase(url, headers, params, rate, args.duration, abort_flag)
        last_headers.update(hdrs)
        ok, r429, err, p50, p95 = summarize(results)
        eff = ok / args.duration
        rows.append((rate, len(results), ok, r429, err, eff, p50, p95))
        table.add_row(f"{rate:g}", str(len(results)), str(ok),
                      f"[red]{r429}[/red]" if r429 else "0", str(err),
                      f"{eff:.1f}", f"{p50:.0f}", f"{p95:.0f}")

        if abort_flag.is_set():
            console.print("[red]인증 오류(401/403) 감지 — 프로브를 중단합니다.[/red]")
            break
        if r429 == 0 and err <= len(results) * 0.1:
            max_clean = rate
        if r429 > 0 and first_throttle is None:
            first_throttle = rate
        # 429가 절반을 넘으면 한도 초과가 명확 → 서버 부담을 줄이기 위해 램프 중단
        if not args.full and r429 >= len(results) * 0.5:
            break
        time.sleep(args.gap + (2.0 if r429 else 0.0))

    console.print(table)

    if last_headers:
        console.print(f"[dim]서버 Rate-Limit 헤더: {last_headers}[/dim]")

    console.print()
    if first_throttle is not None:
        console.print(f"→ 429 최초 발생: [red]{first_throttle:g} rps[/red] / 무429 최대 지속: [green]{max_clean:g} rps[/green]"
                      if max_clean else f"→ 429 최초 발생: [red]{first_throttle:g} rps[/red] (무429 구간 없음)")
    elif max_clean is not None:
        console.print(f"→ 테스트한 전 구간({max_clean:g} rps까지) [green]429 없음[/green] — 한도는 그 이상입니다. "
                      f"--rates로 더 높은 단계를 시도해보세요.")

    cur = toss_api._GROUP_RPS.get(ep['group'])
    if max_clean is not None and cur:
        if max_clean > cur:
            console.print(f"→ 클라이언트 설정({cur} rps)이 실측치({max_clean:g} rps)보다 보수적입니다. 상향 여지 있음.")
        elif first_throttle is not None and cur >= first_throttle:
            console.print(f"→ [red]클라이언트 설정({cur} rps)이 실측 한도({max_clean:g} rps)를 초과[/red] — 하향을 권합니다.")
        else:
            console.print(f"→ 클라이언트 설정({cur} rps)은 실측 한도와 정합합니다.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
