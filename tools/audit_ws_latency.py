"""웹소켓이 체결 인지를 얼마나 앞당기는가 — fill_latency 원장을 읽는다.

[왜 백테스트가 아닌가] 웹소켓(KIS 체결통보·토스 주문 이벤트)의 이득은 전략이 아니라
 **지연**에 있다. 백테스트에는 '체결을 언제 알아챘는가'가 없으므로 그 승패로 재면
 아무것도 재지 않은 채 결론이 난다([[audit-scale-fn-contamination]] 계열의 실수).
 그래서 실매매의 체결 감시(ConclusionMonitor)가 체결을 알아챌 때마다 한 행을 남긴다
 (db fill_latency · modules/auto_trade/conclusion.py _record_fill_latency).

[읽는 법]
  · 푸시→인지 지연 : recognized_at - ws_event_at. WS가 깨운 주기라면 수 초 안이어야 한다.
  · WS 미고지     : ws_event_at 이 NULL 인 행. 토스는 무손실이라 0 이어야 하고, KIS는
                    HTS ID 구독이 실패한 날에 생긴다. 이 비율이 곧 '웹소켓이 실제로 붙어
                    있었나'다 — 로그보다 정직하다.
  · 폴링 상한     : poll_interval. WS가 없었다면 최대 이만큼 늦었을 대기다(대기 모드는
                    기본 300초). 지연 중앙값과 나란히 놓으면 이득의 크기가 보인다.

[실행] python3 tools/audit_ws_latency.py [--days 30] [--broker toss|kis]
       python3 tools/audit_ws_latency.py --db db/trade_history.db   # 파이에서 복사해 온 DB
"""
import argparse
import os
import sys
from datetime import datetime, timedelta

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def _fmt_ts(ts):
    return datetime.fromtimestamp(ts).strftime('%m-%d %H:%M:%S') if ts else "—"


def _pct(v, p):
    return float(np.percentile(v, p)) if len(v) else float('nan')


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=30)
    ap.add_argument("--broker", default=None, choices=[None, "toss", "kis"])
    ap.add_argument("--db", default=None, help="다른 DB 파일(파이에서 복사한 것 등)")
    ap.add_argument("--rows", action="store_true", help="행 전부 출력")
    args = ap.parse_args()

    if args.db:
        import config
        config.DB_FILE_PATH = args.db
    from modules import db_manager

    start = (datetime.now() - timedelta(days=args.days)).strftime('%Y%m%d')
    rows = db_manager.db.get_fill_latency(start_date=start, broker=args.broker)
    if rows is None:
        raise SystemExit("[계측 실패] fill_latency 를 읽지 못했다 — 표가 비어서가 아니라 조회가 실패한 것이다.")
    if not rows:
        print(f"[없음] 최근 {args.days}일 체결 인지 기록이 0행이다. "
              f"체결이 없었거나, 이 계측이 들어간 뒤로 프로세스가 재시작되지 않았다.")
        return

    print(f"[체결 인지 지연] 최근 {args.days}일 · {len(rows)}건"
          f"{' · ' + args.broker if args.broker else ''}")
    by_broker = {}
    for r in rows:
        by_broker.setdefault(r['broker'], []).append(r)

    for broker, rs in sorted(by_broker.items()):
        announced = [r for r in rs if r['ws_event_at'] is not None]
        silent = [r for r in rs if r['ws_event_at'] is None]
        lat = np.array([r['recognized_at'] - r['ws_event_at'] for r in announced], dtype=float)
        woke = sum(1 for r in rs if r['woke_by_ws'])
        polls = np.array([r['poll_interval'] for r in rs if r['poll_interval'] is not None], dtype=float)

        print(f"\n=== {broker.upper()} · {len(rs)}건 ===")
        print(f"  WS가 먼저 알린 체결 : {len(announced)}/{len(rs)}"
              f"  ({len(announced) / len(rs) * 100:.0f}%)   ← 100% 가 아니면 그날 WS가 붙어 있지 않았다")
        print(f"  WS가 깨운 주기      : {woke}/{len(rs)}")
        if len(lat):
            print(f"  푸시→인지 지연(초)  : 중앙 {np.median(lat):.1f} · p90 {_pct(lat, 90):.1f} · "
                  f"최대 {lat.max():.1f} · 최소 {lat.min():.1f}")
            late = int((lat > 10).sum())
            if late:
                print(f"    ※ 10초 넘게 걸린 건 {late}건 — WS가 알렸는데 감시기가 늦게 돌았다. "
                      f"콜백이 막혔거나 주기 안에서 REST 가 실패했을 수 있다.")
        if len(polls):
            print(f"  폴링 상한(초)       : 중앙 {np.median(polls):.0f} · 최대 {polls.max():.0f}"
                  f"   ← WS 없이는 최대 이만큼 기다렸을 대기")
        if silent:
            print(f"  WS 미고지 {len(silent)}건 — 이 체결들은 폴링이 잡았다:")
            for r in silent[:8]:
                print(f"    {_fmt_ts(r['recognized_at'])} {r['code']} {r['side'] or ''} "
                      f"(주문 {r['odno']}, 폴링 {r['poll_interval']}s)")
            if len(silent) > 8:
                print(f"    … {len(silent) - 8}건 더")

    if args.rows:
        print(f"\n{'인지 시각':<16}{'브로커':<6}{'종목':<8}{'구분':<5}{'WS→인지':>9}{'WS깨움':>7}{'폴링':>6}  주문")
        for r in rows:
            lat = (r['recognized_at'] - r['ws_event_at']) if r['ws_event_at'] else None
            print(f"{_fmt_ts(r['recognized_at']):<16}{r['broker']:<6}{(r['code'] or ''):<8}"
                  f"{(r['side'] or ''):<5}{(f'{lat:.1f}s' if lat is not None else '—'):>9}"
                  f"{('O' if r['woke_by_ws'] else '·'):>7}{(r['poll_interval'] or 0):>6.0f}  {r['odno']}")

    print("\n[판정] WS가 먼저 알린 비율이 높고 푸시→인지 중앙값이 폴링 상한보다 훨씬 작으면 "
          "웹소켓이 값을 한 것이다. 비율이 낮으면 지연 수치 이전에 연결부터 볼 것.")


if __name__ == "__main__":
    main()
