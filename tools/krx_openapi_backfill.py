"""KRX Open API 날짜별 스냅샷 백필 — 첫 적재와 큰 결손을 한 번에 채운다.

앱은 한 요청당 KRX_OPENAPI_MAX_INLINE_CALLS(기본 60)만 부르고, 그보다 크게 비어 있으면
Open API 를 쓰지 않고 종전 소스로 폴백한다(구멍 난 시계열 금지 — modules/krx_openapi 독스트링).
그래서 수년치는 여기서 한 번 받는다. 로직은 전부 modules/krx_openapi 에 있고 이 파일은 얇은
CLI 다([[no-subprocess-for-features]]).

    python3 tools/krx_openapi_backfill.py --days 1200                 # 종목 일봉 2시장 + 지수·파생지수·선물·금
    python3 tools/krx_openapi_backfill.py --days 4050 --only stocks   # 감사용 10년+워밍업, 종목만
    python3 tools/krx_openapi_backfill.py --status                    # 저장소 커버리지만

비용: 평일 하루당 서비스 1콜. 종목 2시장 × 4,050일 ≈ 5,800콜(한도 10,000/일). 0.2초 간격이면
약 20분. 라즈베리파이는 여기서 만든 data/krx_openapi.db 를 복사해 가면 된다.
"""
import argparse
import os
import sys
import time
from datetime import datetime, timedelta

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from modules import krx_openapi as oa  # noqa: E402

GROUPS = {
    "stocks": list(oa.STOCK_APIS),
    "index": ["kospi_dd_trd", "kosdaq_dd_trd", "drvprod_dd_trd"],
    "futures": ["fut_bydd_trd"],
    "gold": ["gold_bydd_trd"],
    "base": list(oa.BASE_INFO_APIS),      # 스냅샷이라 최신 하루만
}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=1200, help="오늘부터 거슬러 채울 달력일 수")
    ap.add_argument("--only", default="stocks,index,futures,gold,base",
                    help="쉼표 구분: " + ",".join(GROUPS))
    ap.add_argument("--max-calls", type=int, default=9000, help="이번 실행의 호출 상한(일 한도 10,000)")
    ap.add_argument("--workers", type=int, default=3, help="동시 호출 수(응답이 2초 남짓이라 직렬은 느리다)")
    ap.add_argument("--status", action="store_true", help="적재 상태만 보고 끝낸다")
    args = ap.parse_args()

    ok, msg = oa.status_text()
    print(f"[상태] {msg}")
    cov = oa.coverage()
    for api_id, (first, last, n) in sorted(cov.items()):
        print(f"  {api_id:20s} {first}~{last}  {n:,}일")
    if args.status:
        return
    if not ok:
        raise SystemExit(1)

    end_dd = oa.latest_available_dd()
    start_dd = (datetime.strptime(end_dd, "%Y%m%d") - timedelta(days=args.days)).strftime("%Y%m%d")
    budget = args.max_calls
    t0 = time.time()
    for group in [g.strip() for g in args.only.split(",") if g.strip()]:
        apis = GROUPS.get(group)
        if not apis:
            print(f"[건너뜀] 모르는 그룹 {group}")
            continue
        s_dd = end_dd if group == "base" else start_dd
        todo = oa.missing_days(apis, s_dd, end_dd)
        print(f"\n[{group}] {s_dd}~{end_dd} 결손 {len(todo):,}건 (예산 {budget:,})")
        if not todo:
            continue
        done = [0]

        def _progress(api_id, dd, n, calls, total):
            done[0] = calls
            if calls % 50 == 0 or calls == total:
                print(f"  {calls:,}/{total:,}  {api_id} {dd} {n}행  ({time.time() - t0:.0f}s)", flush=True)

        try:
            remaining, calls = oa.ensure(apis, s_dd, end_dd, max_calls=budget, progress=_progress,
                                         workers=args.workers)
        except oa.OpenAPIError as e:
            print(f"  [중단] {e}")
            if getattr(e, "status", None) in (401, 403):
                print("  → openapi.krx.co.kr 마이페이지에서 이 서비스의 이용신청·승인 상태를 확인할 것")
            break
        budget -= calls
        print(f"  받음 {calls:,} · 남음 {remaining:,}")
        if budget <= 0:
            print("[예산 소진] 내일 다시 실행하면 이어서 받는다")
            break

    print("\n[적재 상태]")
    for api_id, (first, last, n) in sorted(oa.coverage().items()):
        print(f"  {api_id:20s} {first}~{last}  {n:,}일")


if __name__ == "__main__":
    main()
