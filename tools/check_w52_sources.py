#!/usr/bin/env python3
"""52주 위치(표 '52주' 컬럼)가 mode 2(KIS)와 mode 3(TOSS)에서 크게 어긋나는 원인 특정 도구.

배경: _analyze_table_row의 52주 산출 경로가 셋으로 갈린다.
  (a) KIS 개별 현재가 → API가 주는 w52_hgpr/w52_lwpr 를 그대로 사용
  (b) KIS 멀티시세(_src='multi') → API가 52주를 안 주므로 차트 tail(250) 고저로 보강
  (c) TOSS → 차트 '전체' 고저로 보강 (tail(250) 미적용)
경로마다 창(window)과 원천이 달라 같은 시각에 다른 값이 나온다. 이 도구는 종목별로
차트 길이·기간·고저와 API가 준 52주를 나란히 찍어 어느 쪽이 틀렸는지 눈으로 가른다.

사용법:
  python tools/check_w52_sources.py 2                 # KIS(실전)
  python tools/check_w52_sources.py 3                 # TOSS
  python tools/check_w52_sources.py 2 030200 207940   # 종목 직접 지정
"""
import os
import sys
from datetime import datetime, timedelta

sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import config
import api

# 기본 샘플: 2026-07-22 20:02 mode2/mode3 '52주'가 크게 어긋났던 종목
DEFAULT_CODES = ["030200", "207940", "329180", "494670", "006400", "091170", "005930"]


def _pos(cur, h, l):
    if h and l and h > l:
        return (cur - l) / (h - l) * 100
    return float('nan')


def main():
    args = list(sys.argv[1:])
    mode = "2"
    if args and args[0] in ("1", "2", "3"):
        mode = args.pop(0)
    codes = args or DEFAULT_CODES

    config.session.initialize(mode=mode)
    print("=" * 100)
    print(f"실행 시각 : {datetime.now()}   mode={mode}  is_toss={config.session.is_toss}")
    print(f"CHART_LOOKBACK_DAYS={config.INDICATOR_PARAMS.get('CHART_LOOKBACK_DAYS')}")
    print("=" * 100)

    hdr = (f"{'코드':>8s} {'현재가':>10s} | {'봉수':>4s} {'차트시작':>9s} {'차트끝':>9s} "
           f"| {'전체高':>10s} {'전체低':>10s} {'pos(전체)':>9s} "
           f"| {'250高':>10s} {'250低':>10s} {'pos(250)':>9s} "
           f"| {'API高':>10s} {'API低':>10s} {'pos(API)':>9s} | src")
    print(hdr)
    print("-" * len(hdr))

    for code in codes:
        try:
            df = api.get_chart_data(code, is_overseas=False, period_type='daily', realtime=False)
        except Exception as e:
            print(f"{code:>8s}  차트 조회 실패: {e}")
            continue
        try:
            cur_res = api.get_current_price_data(code, False)
        except Exception as e:
            cur_res = None
            print(f"{code:>8s}  현재가 조회 실패: {e}")

        out = (cur_res or {}).get('output', {}) if (cur_res or {}).get('rt_cd') == '0' else {}
        try:
            cur = float(out.get('ats_prpr') or 0) or float(out.get('stck_prpr') or 0)
        except Exception:
            cur = 0.0

        if df is None or df.empty:
            print(f"{code:>8s} {cur:>10,.0f} | 차트 없음")
            continue

        h_all, l_all = float(df['high'].max()), float(df['low'].min())
        t = df.tail(250)
        h250, l250 = float(t['high'].max()), float(t['low'].min())
        try:
            api_h = float(out.get('w52_hgpr') or 0)
            api_l = float(out.get('w52_lwpr') or 0)
        except Exception:
            api_h = api_l = 0.0

        d0 = str(df.iloc[0]['date'])[:10]
        d1 = str(df.iloc[-1]['date'])[:10]
        print(f"{code:>8s} {cur:>10,.0f} | {len(df):>4d} {d0:>9s} {d1:>9s} "
              f"| {h_all:>10,.0f} {l_all:>10,.0f} {_pos(cur, h_all, l_all):>8.1f}% "
              f"| {h250:>10,.0f} {l250:>10,.0f} {_pos(cur, h250, l250):>8.1f}% "
              f"| {api_h:>10,.0f} {api_l:>10,.0f} {_pos(cur, api_h, api_l):>8.1f}% "
              f"| {out.get('_src', '-')}")

    # 52주(=1년) 경계 참고: 오늘 기준 365일 전 날짜
    print("-" * len(hdr))
    print(f"참고: 오늘-365일 = {(datetime.now() - timedelta(days=365)).strftime('%Y-%m-%d')} "
          f"(차트시작이 이보다 과거면 '52주'보다 넓은 창을 쓰고 있는 것)")


if __name__ == "__main__":
    main()
