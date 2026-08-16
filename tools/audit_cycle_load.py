"""관심종목을 늘리면 감시 주기가 얼마나 길어지는가 — 유니버스의 실질 상한.

[왜 이것이 수익률보다 먼저인가] trader._record_cycle_duration 주석 그대로다.
 "실제 청산 감시 간격 = 주기 소요 시간 + SYSTEM_TRADING_INTERVAL"이고, 종목이 늘면
 소요 시간만 길어져 손절·트레일링 확인이 그만큼 늦어진다. 백테스트가 80종목을
 지지해도(tools/audit_universe.py) 라즈베리파이3에서 주기가 몇 분씩 걸리면 그 수치는
 종이 위의 것이다. **유니버스 상한은 수익률이 아니라 이 값이 정한다.**

[무엇을 재는가] 실제 코드 경로(`AutoTrader._analyze_candidates`)를 그대로 돌린다.
 API만 계측 스텁으로 갈아끼워 호출 수를 세고, 네트워크 지연은 `--api-latency`로 준다.
  · CPU 축 — 스텁 지연 0으로 돌린 순수 연산 시간. **기계마다 다르므로 파이에서 직접 재야
    의미가 있다.** 이 도구를 파이에서 그대로 실행하면 그 기계의 곡선이 나온다.
  · API 축 — 종목당 호출 수 × 지연 ÷ 병렬도. 호출 수는 세고, 지연은 인자로 받는다.
  · 검증 — `--logs`로 실제 로그의 "모니터링 완료 (소요 N초)" 분포를 함께 출력해,
    현재 종목 수에서 모델이 실측과 맞는지 대조한다.

[한계] 스텁은 캐시된 일봉을 즉시 돌려주므로 API의 실제 지연·재시도·TPS 대기는
 재현하지 않는다. 이 도구가 답하는 것은 "종목 수에 따라 무엇이 얼마나 늘어나는가"이지
 "파이에서 정확히 몇 초인가"가 아니다. 후자는 --api-latency 를 실측값으로 넣어야 한다.

[실행] python3 tools/audit_cycle_load.py --sizes 20,44,60,80,100,120 --repeat 3
       python3 tools/audit_cycle_load.py --api-latency 0.25 --logs 'logs/autotrade_*.log' 
"""
import argparse
import glob
import os
import re
import statistics
import sys
import time
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import config  # noqa: E402

CALLS = {}


def counted(name, fn, latency=0.0):
    def wrapper(*a, **kw):
        CALLS[name] = CALLS.get(name, 0) + 1
        if latency:
            time.sleep(latency)
        return fn(*a, **kw)
    return wrapper


def load_frames(codes, days):
    """캐시된 일봉을 실제 형태로 준비한다 — 지표 계산 비용을 진짜로 치르게 하려는 것."""
    from modules import backtest
    out = {}
    for code in codes:
        try:
            df = backtest.get_backtest_data(code, False, days)
        except Exception:
            df = None
        if df is not None and not df.empty:
            out[code] = df
    return out


def parse_logs(patterns):
    """실제 로그의 주기 소요 시간 분포 — 모델을 현실에 붙들어 매는 유일한 끈."""
    vals = []
    for pat in patterns:
        for path in glob.glob(pat):
            try:
                with open(path, encoding="utf-8", errors="ignore") as f:
                    for line in f:
                        # 로그 문구는 버전에 따라 다르다("모니터링 완료. 대기 중..."에는
                        #  소요 시간이 없다). 소요 숫자만 잡는다.
                        m = re.search(r"소요 ([0-9.]+)초", line)
                        if m:
                            vals.append(float(m.group(1)))
            except OSError:
                continue
    return vals


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--sizes", default="20,44,60,80,100,120")
    ap.add_argument("--repeat", type=int, default=3)
    ap.add_argument("--days", type=int, default=400, help="워커에 넘길 일봉 길이(지표 워밍업 포함)")
    ap.add_argument("--api-latency", type=float, default=0.0,
                    help="스텁 API 1회당 지연(초). 실측값을 넣으면 파이의 실제 주기에 가까워진다")
    ap.add_argument("--logs", nargs="*", default=[],
                    help="실측 대조용 로그 glob (자동매매 로그는 logs/autotrade_*.log)")
    args = ap.parse_args()

    sizes = [int(x) for x in args.sizes.split(",") if x]
    need = max(sizes)

    config.session.load_stock_config()
    live = [(s["code"], s["name"]) for s in config.session.stock_data.get("stocks_kr", [])]
    n_watch = len(live)     # 실측 로그는 '지금의 관심종목 수'에서 나온 값이다
    codes = [c for c, _n in live]
    if len(codes) < need:
        # 관심종목이 모자라면 시총 상위에서 채운다 — 종목 수만 늘리면 되는 축이다.
        import FinanceDataReader as fdr
        krx = fdr.StockListing("KRX")
        krx = krx[krx["Market"].isin(["KOSPI", "KOSDAQ"])].dropna(subset=["Marcap"])
        krx = krx.sort_values("Marcap", ascending=False)
        have = set(codes)
        for _, r in krx.iterrows():
            if len(codes) >= need:
                break
            if r["Code"] not in have and r["Code"].endswith("0"):
                codes.append(r["Code"])
                live.append((r["Code"], r["Name"]))
                have.add(r["Code"])

    print(f"[준비] 종목 {len(codes)}개 일봉 로드 중 ({args.days}일)", flush=True)
    frames = load_frames(codes[:need], args.days)
    codes = [c for c in codes if c in frames][:need]
    live = [(c, n) for c, n in live if c in frames][:need]
    print(f"[준비] 사용 {len(codes)}종목 · API 지연 {args.api_latency}s/호출", flush=True)

    from modules.auto_trade import AutoTrader
    AutoTrader._instance = None
    trader = AutoTrader()
    # 워커는 정지 상태면 즉시 빠지고, 시장 상태 캐시가 없으면 fail-closed로 스킵한다.
    #  둘 다 '분석을 하지 않는' 경로라 그대로 두면 0초가 찍힌다. 실제 주기와 같은
    #  조건(가동 중 · 두 시장 모두 건전)을 만들어 놓고 잰다.
    trader.is_running = True
    for m in ("KOSPI", "KOSDAQ"):
        trader.market_index_status[m] = {"is_healthy": True}

    lat = args.api_latency
    stubs = {
        "get_chart_data": lambda code, is_overseas=False, **kw: frames.get(code),
        "get_realtime_vol_strength": lambda code, **kw: 100.0,
        "get_current_price": lambda code, is_overseas=False, **kw: float(
            frames[code]["close"].iloc[-1]) if code in frames else 0.0,
        "get_ask_bid_ratio": lambda code, is_overseas=False, **kw: 1.0,
        "is_nxt_tradeable": lambda code, **kw: True,
        "prefetch_multiple_current_prices": lambda codes_, **kw: None,
        "send_telegram_message": lambda *a, **kw: None,
        "fetch_buyable_quantity": lambda *a, **kw: 0,
    }
    patchers = []
    for name, fn in stubs.items():
        from modules.auto_trade import api as at_api
        if hasattr(at_api, name):
            patchers.append(patch(f"modules.auto_trade.api.{name}",
                                  side_effect=counted(name, fn, lat)))
    # 지수 모멘텀·국면은 종목 수와 무관한 고정 비용이라 스텁으로 고정한다.
    patchers.append(patch("modules.auto_trade.analysis.get_index_momentum", return_value=0.0))
    patchers.append(patch("modules.auto_trade.analysis.get_market_regime",
                          return_value=("중립", 0.0)))
    for p in patchers:
        p.start()

    print(f"\n{'종목수':>7}{'주기(초)':>10}{'종목당(ms)':>12}{'API호출':>9}{'호출/종목':>10}"
          f"{'감시간격(초)':>13}")
    interval = getattr(config, "SYSTEM_TRADING_INTERVAL", 60)
    curve = []
    try:
        for n in sizes:
            if n > len(codes):
                continue
            targets = [{"code": c, "name": nm} for c, nm in live[:n]]
            best = None
            for _ in range(args.repeat):
                CALLS.clear()
                t0 = time.perf_counter()
                trader._analyze_candidates(targets, set(), {}, {}, {}, {},
                                           restricted_stocks=set(), stop_exit_prices={})
                el = time.perf_counter() - t0
                best = el if best is None else min(best, el)
            calls = sum(CALLS.values())
            curve.append((n, best, calls))
            print(f"{n:>7}{best:>10.2f}{best / n * 1000:>12.1f}{calls:>9}{calls / n:>10.2f}"
                  f"{best + interval:>13.1f}")
    finally:
        for p in patchers:
            p.stop()

    if len(curve) >= 2:
        (n0, t0c, _), (n1, t1c, _) = curve[0], curve[-1]
        slope = (t1c - t0c) / max(1, n1 - n0)
        print(f"\n[모델] 종목당 한계비용 {slope * 1000:.1f}ms · "
              f"고정비용 {max(0.0, t0c - slope * n0):.2f}초")
        print(f"[해석] 이 기계 기준. 80종목 주기 {t0c + slope * (80 - n0):.1f}초 → "
              f"청산 감시 간격 {t0c + slope * (80 - n0) + interval:.0f}초")
        print("[주의] API 지연 0으로 쟀다면 이 값은 CPU 축만이다. 파이에서 --api-latency에"
              " 실측 지연을 넣어 다시 재야 한다.")

    if args.logs:
        vals = parse_logs(args.logs)
        if vals:
            vals.sort()
            print(f"\n[실측 대조] 로그 {len(vals)}회 · 중앙 {statistics.median(vals):.1f}초 · "
                  f"P90 {vals[int(len(vals) * 0.9)]:.1f}초 · 최대 {vals[-1]:.1f}초 "
                  f"(그 로그를 남긴 관심종목 {n_watch}개 기준)")
        else:
            print("\n[실측 대조] 로그에서 '소요 …초'를 찾지 못했다 "
                  "(자동매매 로그는 logs/autotrade_*.log 에 있다).")


if __name__ == "__main__":
    main()
