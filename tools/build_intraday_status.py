"""장중 시점별 판정을 미리 계산해 캐시한다 (진입 체결 시점 검증용).

실매매는 주기마다 **미확정 장중 봉**으로 다시 채점하므로, 같은 날 안에서 매수 신호가
켜졌다 꺼지면 실제 체결은 "그날 몇 시에 스캔했는가"에 좌우된다. 일봉 백테스트에는
없는 자유도다. tools/audit_intraday_signal_stability.py 가 그 **빈도**를 30분봉 60일로
쟀지만 손익은 재지 못했다 — 관측 구간이 짧아서다. 3년치 분봉이 생겼으므로
시점별 판정을 통째로 미리 계산해 두면 포트폴리오 시뮬레이터가 그대로 쓸 수 있다.

38종목 × 715일 × 7봉 ≈ 19만 회 채점이라 프로세스 병렬로 돈다(1회 약 5ms).

[선행] tools/fetch_intraday_tv.py 로 분봉을 먼저 캐시할 것.
[실행] python3 tools/build_intraday_status.py --interval 60m --workers 8
"""
import argparse
import os
import sys
import time
from concurrent.futures import ProcessPoolExecutor

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import config  # noqa: E402
from modules import intraday_bars as ib  # noqa: E402

_TH = None


def _thresholds():
    return {
        "BUY_SCORE": config.ANALYSIS_THRESHOLDS["BUY_SCORE"],
        "BUY_RSI_MAX": config.ANALYSIS_THRESHOLDS["BUY_RSI_MAX"],
        "RISE_SCORE": config.ANALYSIS_THRESHOLDS["RISE_SCORE"],
        "WEIGHTS": config.SCORING_WEIGHTS,
    }


def _one(args):
    code, interval, days, lookback, force = args
    if not force and ib.load_status(code, interval) is not None:
        return code, -1
    raw = ib.load(code, interval)
    if raw is None:
        return code, 0
    import pandas as pd
    d = ib.precompute_intraday_status(code, ib.by_day(raw), _thresholds(), days, lookback)
    if not d:
        return code, 0
    pd.to_pickle(d, ib.status_cache_path(code, interval))
    return code, sum(len(v) for v in d.values())


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--interval", default="60m")
    ap.add_argument("--days", type=int, default=1200)
    ap.add_argument("--lookback", type=int, default=260,
                    help="실매매가 들고 있는 봉 수. 늘리면 백테스트 쪽에 유리해진다")
    ap.add_argument("--workers", type=int, default=8)
    ap.add_argument("--force", action="store_true")
    args = ap.parse_args()

    config.session.load_stock_config()
    stocks = [x for k in ("stocks_kr", "etfs_kr")
              for x in config.session.stock_data.get(k, [])]
    names = {s["code"]: s["name"] for s in stocks}
    codes = [s["code"] for s in stocks if ib.load(s["code"], args.interval) is not None]
    print(f"[대상] 분봉 있는 {len(codes)}종목 · {args.interval} · 룩백 {args.lookback}봉")

    t0 = time.time()
    jobs = [(c, args.interval, args.days, args.lookback, args.force) for c in codes]
    done = 0
    with ProcessPoolExecutor(max_workers=args.workers) as ex:
        for code, n in ex.map(_one, jobs):
            done += 1
            tag = "캐시" if n == -1 else (f"{n:,}시점" if n else "실패")
            print(f"  [{done}/{len(codes)}] {names.get(code, code)} {tag}")
    print(f"[완료] {time.time() - t0:.0f}초 · {ib.CACHE_DIR}")


if __name__ == "__main__":
    main()
