"""관심종목 분봉을 TradingView에서 받아 캐시하고, KRX 확정 일봉과 대조한다.

로직은 modules/intraday_bars.py 가 갖고 있고 이 파일은 얇은 CLI다.

[실행] python3 tools/fetch_intraday_tv.py --interval 60m
      python3 tools/fetch_intraday_tv.py --interval 30m --validate-only
"""
import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import config  # noqa: E402
from modules import intraday_bars as ib  # noqa: E402


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--interval", default="60m", choices=sorted(ib.INTERVALS))
    ap.add_argument("--bars", type=int, default=5000)
    ap.add_argument("--force", action="store_true", help="캐시를 무시하고 다시 받는다")
    ap.add_argument("--validate-only", action="store_true")
    args = ap.parse_args()

    config.session.load_stock_config()
    stocks = config.session.stock_data.get("stocks_kr", [])
    codes = [s["code"] for s in stocks]
    names = {s["code"]: s["name"] for s in stocks}
    print(f"[대상] {len(codes)}종목 · {args.interval} · 최대 {args.bars}봉")

    if not args.validate_only:
        have, failed = ib.ensure(codes, args.interval, args.bars, force=args.force)
        print(f"[수집] 성공 {len(have)} / 실패 {len(failed)}"
              + (f" — {', '.join(names.get(c, c) for c in failed)}" if failed else ""))

    print(f"\n{'종목':<14}{'봉수':>7}{'시작':>12}{'끝':>12}{'대조일':>7}{'OHLC일치%':>10}{'종가%':>8}")
    bad = []
    for code in codes:
        df = ib.load(code, args.interval)
        if df is None:
            print(f"{names.get(code, code):<14}{'없음':>7}")
            bad.append(code)
            continue
        n, ok_all, ok_c = ib.validate(code, args.interval)
        flag = "" if ok_all >= 98.0 else "  ← 낮음"
        if ok_all < 98.0:
            bad.append(code)
        print(f"{names.get(code, code):<14}{len(df):>7}{str(df.index[0].date()):>12}"
              f"{str(df.index[-1].date()):>12}{n:>7}{ok_all:>10.1f}{ok_c:>8.1f}{flag}")
    print(f"\n[판정] 사용 불가/의심 {len(bad)}종목"
          + (f" — {', '.join(names.get(c, c) for c in bad)}" if bad else " (없음)"))
    print("일치율은 수정주가 소급 시점 차이로 100%가 되지 않는다. 98% 미만만 제외 대상.")


if __name__ == "__main__":
    main()
